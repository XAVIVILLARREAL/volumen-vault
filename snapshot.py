"""
snapshot.py — Motor de snapshots de volúmenes Docker → pCloud
============================================================
Corre como un contenedor con acceso al socket de Docker (/var/run/docker.sock).
Para cada volumen que matchea el patrón:
  1. Levanta un contenedor efímero alpine con el volumen montado
  2. Comprime el contenido a tar.gz con timestamps preservados
  3. Sube el tar.gz a pCloud en /volumen-vault/<fecha>/<volumen>.tar.gz
  4. Registra metadata en /volumen-vault/_index.json
  5. Purga snapshots que excedan RETENTION_SNAPSHOTS

Modos de uso:
  python snapshot.py             # corre el ciclo una vez
  python snapshot.py --serve     # arranca API REST (api.py)
  python snapshot.py --list      # lista snapshots remotos desde pCloud
  python snapshot.py --restore VOLUMEN FECHA  # descarga un snapshot a /tmp
"""
import argparse
import gzip
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests


# ─── Config ──────────────────────────────────────────────────────────────
def load_env(path: Path = Path(".env")):
    """Carga KEY=VALUE de un .env sin librerías externas."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


load_env()

PCLOUD_EMAIL = os.environ.get("PCLOUD_EMAIL", "")
PCLOUD_PASSWORD = os.environ.get("PCLOUD_PASSWORD", "")
PCLOUD_REGION = os.environ.get("PCLOUD_REGION", "us")
PCLOUD_VAULT_FOLDER_ID = int(os.environ.get("PCLOUD_VAULT_FOLDER_ID", "0") or "0")

INCLUDE_PATTERN = re.compile(os.environ.get("INCLUDE_PATTERN", ".*"))
EXCLUDE_PATTERN = re.compile(os.environ.get("EXCLUDE_PATTERN", r"^$"))

RETENTION_SNAPSHOTS = int(os.environ.get("RETENTION_SNAPSHOTS", "30"))

API_BASE = "https://eapi.pcloud.com/" if PCLOUD_REGION == "eu" else "https://api.pcloud.com/"

logging.basicConfig(
    level=logging.DEBUG if os.environ.get("DEBUG") == "true" else logging.INFO,
    format="[%(asctime)s] %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("volumen-vault")


# ─── pCloud client ───────────────────────────────────────────────────────
class PCloudError(Exception):
    pass


class PCloud:
    def __init__(self, email: str, password: str):
        self.email = email
        self.password = password
        self.token: Optional[str] = None
        if not email or not password:
            raise PCloudError("PCLOUD_EMAIL y PCLOUD_PASSWORD son requeridos (env vars)")
        self._authenticate()

    def _authenticate(self):
        r = self._call("getdigest", {"username": self.email}, authless=True)
        digest = r["digest"]
        r = self._call(
            "login",
            {"username": self.email, "digest": digest, "password": self.password, "getauth": 1},
            authless=True,
        )
        self.token = r["auth"]
        log.info(f"✓ pCloud autenticado (region={PCLOUD_REGION})")

    def _call(self, endpoint: str, params: dict | None = None, authless: bool = False) -> dict:
        p = dict(params or {})
        if not authless:
            p["auth"] = self.token
        r = requests.get(f"{API_BASE}{endpoint}", params=p, timeout=60)
        r.raise_for_status()
        data = r.json()
        if data.get("result") != 0:
            raise PCloudError(f"pCloud {endpoint}: {data}")
        return data

    def list_folder(self, folder_id: int = 0) -> list[dict]:
        r = self._call("listfolder", {"folderid": folder_id, "recursive": "yes"})
        return [
            {
                "id": item.get("folderid") or item.get("fileid"),
                "name": item.get("name"),
                "is_folder": item.get("isfolder") is True,
                "size": item.get("size", 0),
                "modified": item.get("modified"),
                "path": item.get("path"),
            }
            for item in r["metadata"].get("contents", [])
        ]

    def find_or_create_folder(self, name: str, parent_id: int) -> int:
        existing = {f["name"]: f["id"] for f in self.list_folder(parent_id) if f["is_folder"]}
        if name in existing:
            return existing[name]
        r = self._call("createfolder", {"name": name, "folderid": parent_id})
        return r["metadata"]["folderid"]

    def upload_file(self, local_path: Path, folder_id: int, rename: Optional[str] = None) -> dict:
        with open(local_path, "rb") as f:
            r = requests.post(
                f"{API_BASE}uploadfile",
                params={
                    "auth": self.token,
                    "folderid": folder_id,
                    "filename": rename or local_path.name,
                },
                files={"file": f},
                timeout=3600,
            )
        r.raise_for_status()
        data = r.json()
        if data.get("result") != 0:
            raise PCloudError(f"pCloud upload: {data}")
        meta = data["metadata"][0]
        return {
            "id": meta.get("fileid") or meta.get("folderid"),
            "name": meta.get("name"),
            "size": meta.get("size", 0),
        }

    def delete_file(self, file_id: int):
        self._call("deletefile", {"fileid": file_id})

    def get_download_link(self, file_id: int) -> str:
        r = self._call("getfilelink", {"fileid": file_id})
        return r.get("link", "")


# ─── Docker helpers ──────────────────────────────────────────────────────
def docker_available() -> bool:
    """Detecta si estamos corriendo con acceso al socket de Docker."""
    return Path("/var/run/docker.sock").exists() or shutil.which("docker") is not None


def list_docker_volumes() -> list[str]:
    """Lista nombres de volúmenes Docker."""
    try:
        out = subprocess.run(
            ["docker", "volume", "ls", "--format", "{{.Name}}"],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        return [v.strip() for v in out.stdout.splitlines() if v.strip()]
    except Exception as e:
        log.error(f"No se pudo listar volúmenes: {e}")
        return []


def snapshot_volume(volume_name: str, dest_tar: Path) -> tuple[int, str]:
    """
    Levanta un contenedor efímero con el volumen montado en /data, hace tar
    y lo baja al host. Devuelve (size_bytes, sha256).
    """
    container_name = f"snap-{volume_name}-{int(time.time())}"
    log.info(f"  → Levantando contenedor efímero para '{volume_name}'...")

    # 1. Crear contenedor
    subprocess.run(
        [
            "docker",
            "run",
            "-d",
            "--name",
            container_name,
            "--mount",
            f"source={volume_name},target=/data",
            "alpine:latest",
            "sleep",
            "300",
        ],
        check=True,
        capture_output=True,
        timeout=60,
    )

    try:
        # 2. Hacer tar dentro del contenedor, stream a stdout
        log.info(f"  → Comprimiendo contenido...")
        sha = hashlib.sha256()
        with open(dest_tar, "wb") as out_f:
            proc = subprocess.Popen(
                [
                    "docker",
                    "exec",
                    container_name,
                    "tar",
                    "czf",
                    "-",
                    "-C",
                    "/data",
                    ".",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            assert proc.stdout is not None
            while True:
                chunk = proc.stdout.read(64 * 1024)
                if not chunk:
                    break
                out_f.write(chunk)
                sha.update(chunk)
            proc.wait(timeout=1800)
            if proc.returncode != 0:
                err = proc.stderr.read().decode(errors="ignore") if proc.stderr else ""
                raise RuntimeError(f"tar failed: {err}")
        size = dest_tar.stat().st_size
        log.info(f"  ✓ {size:,} bytes  sha256={sha.hexdigest()[:16]}...")
        return size, sha.hexdigest()
    finally:
        # 3. Cleanup del contenedor efímero
        subprocess.run(
            ["docker", "rm", "-f", container_name],
            capture_output=True,
            timeout=30,
        )


# ─── Snapshot orchestrator ───────────────────────────────────────────────
class Vault:
    def __init__(self):
        self.pc = PCloud(PCLOUD_EMAIL, PCLOUD_PASSWORD)
        self.vault_root_id = self.pc.find_or_create_folder("volumen-vault", PCLOUD_VAULT_FOLDER_ID)
        log.info(f"✓ Vault root folder ID: {self.vault_root_id}")

    def run_once(self) -> dict:
        """Corre un ciclo completo de snapshot. Devuelve reporte."""
        if not docker_available():
            raise RuntimeError("Docker socket no disponible — montar /var/run/docker.sock")

        date_tag = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        date_folder_name = date_tag
        date_folder_id = self.pc.find_or_create_folder(date_folder_name, self.vault_root_id)
        log.info(f"📦 Snapshot run {date_tag} → pCloud folder id={date_folder_id}")

        all_volumes = list_docker_volumes()
        matched = [
            v for v in all_volumes
            if INCLUDE_PATTERN.search(v) and not EXCLUDE_PATTERN.search(v)
        ]
        log.info(f"  {len(matched)}/{len(all_volumes)} volúmenes matchean (include={INCLUDE_PATTERN.pattern}, exclude={EXCLUDE_PATTERN.pattern})")

        report = {
            "date": date_tag,
            "matched": len(matched),
            "snapshots": [],
            "errors": [],
        }

        for vol in matched:
            log.info(f"[{vol}]")
            try:
                with tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False) as tmp:
                    tmp_path = Path(tmp.name)

                size, sha = snapshot_volume(vol, tmp_path)

                # Subir a pCloud
                log.info(f"  → Subiendo a pCloud...")
                meta = self.pc.upload_file(
                    tmp_path,
                    folder_id=date_folder_id,
                    rename=f"{vol}.tar.gz",
                )
                log.info(f"  ✓ Uploaded: file_id={meta['id']}")

                report["snapshots"].append({
                    "volume": vol,
                    "file_id": meta["id"],
                    "name": meta["name"],
                    "size": size,
                    "sha256": sha,
                    "timestamp": date_tag,
                })

                tmp_path.unlink(missing_ok=True)

            except Exception as e:
                log.error(f"  ✗ Error en {vol}: {e}")
                report["errors"].append({"volume": vol, "error": str(e)})

        # Update index
        self._update_index(report)

        # Retention
        if RETENTION_SNAPSHOTS > 0:
            self._apply_retention()

        return report

    def _update_index(self, new_report: dict):
        """Sube/actualiza _index.json con todos los snapshots."""
        index_id = self.pc.find_or_create_folder("_index", self.vault_root_id)
        try:
            existing = self.pc.list_folder(index_id)
            existing_files = {f["name"]: f for f in existing if not f["is_folder"]}
        except Exception:
            existing_files = {}

        # Por simplicidad: descargamos, modificamos, subimos
        # (pCloud no tiene JSON merge nativo — alternativa: usar _index.json como
        # una lista de reports, uno por día)
        # Aquí guardamos UN archivo por día con ese día
        idx_name = f"index-{new_report['date']}.json"
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w") as tmp:
            json.dump(new_report, tmp, indent=2)
            tmp_path = Path(tmp.name)

        try:
            self.pc.upload_file(tmp_path, folder_id=index_id, rename=idx_name)
            log.info(f"✓ Index actualizado: {idx_name}")
        finally:
            tmp_path.unlink(missing_ok=True)

    def _apply_retention(self):
        """Borra los snapshots más viejos por volumen, manteniendo RETENTION_SNAPSHOTS."""
        log.info(f"🧹 Aplicando retención ({RETENTION_SNAPSHOTS} snapshots por volumen)...")

        # Listar todas las carpetas de fecha dentro del vault root
        date_folders = [f for f in self.pc.list_folder(self.vault_root_id) if f["is_folder"] and f["name"] != "_index"]

        # Agrupar por volumen
        by_volume: dict[str, list[dict]] = {}
        for date_folder in date_folders:
            try:
                contents = self.pc.list_folder(date_folder["id"])
                for item in contents:
                    if not item["is_folder"] and item["name"].endswith(".tar.gz"):
                        vol_name = item["name"][:-7]  # strip ".tar.gz"
                        by_volume.setdefault(vol_name, []).append({
                            **item,
                            "date_folder_id": date_folder["id"],
                            "date": date_folder["name"],
                        })
            except Exception as e:
                log.warning(f"  ⚠ no pude leer {date_folder['name']}: {e}")

        # Ordenar y borrar excedentes
        deleted = 0
        for vol, items in by_volume.items():
            items.sort(key=lambda x: x["date"], reverse=True)  # más nuevos primero
            for item in items[RETENTION_SNAPSHOTS:]:
                try:
                    self.pc.delete_file(item["id"])
                    log.info(f"  🗑  {vol} {item['date']} (file_id={item['id']})")
                    deleted += 1
                except Exception as e:
                    log.warning(f"  ⚠ no pude borrar {item['id']}: {e}")

            # También borrar la carpeta de fecha si quedó vacía
            # (pCloud no tiene delete empty folder directo, pero al borrar el último file queda vacía)

        log.info(f"✓ Retención aplicada: {deleted} snapshots purgados")

    def list_snapshots(self, volume: Optional[str] = None) -> list[dict]:
        """Lista todos los snapshots. Opcionalmente filtra por volumen."""
        all_snapshots = []
        date_folders = [f for f in self.pc.list_folder(self.vault_root_id) if f["is_folder"] and f["name"] != "_index"]

        for date_folder in date_folders:
            try:
                contents = self.pc.list_folder(date_folder["id"])
                for item in contents:
                    if not item["is_folder"] and item["name"].endswith(".tar.gz"):
                        vol_name = item["name"][:-7]
                        if volume is None or vol_name == volume:
                            all_snapshots.append({
                                "volume": vol_name,
                                "date": date_folder["name"],
                                "file_id": item["id"],
                                "size": item["size"],
                                "name": item["name"],
                            })
            except Exception:
                pass

        # Ordenar por fecha desc
        all_snapshots.sort(key=lambda x: x["date"], reverse=True)
        return all_snapshots

    def restore(self, volume: str, date: str, dest_dir: Path = Path("/tmp/restore")) -> dict:
        """
        Descarga un snapshot específico de pCloud y lo extrae en dest_dir/<volume>/.
        Útil para que un humano o una IA ejecute desde la API.
        """
        # Buscar el file_id
        date_folder_id = None
        for f in self.pc.list_folder(self.vault_root_id):
            if f["name"] == date and f["is_folder"]:
                date_folder_id = f["id"]
                break
        if date_folder_id is None:
            raise PCloudError(f"Date folder '{date}' no existe")

        target_name = f"{volume}.tar.gz"
        target_file = None
        for f in self.pc.list_folder(date_folder_id):
            if f["name"] == target_name:
                target_file = f
                break
        if target_file is None:
            raise PCloudError(f"Snapshot {target_name} no existe en {date}")

        # Descargar via getfilelink
        link_info = self.pc._call("getfilelink", {"fileid": target_file["id"]})
        rel_path = link_info["path"]

        dest_dir.mkdir(parents=True, exist_ok=True)
        target_path = dest_dir / volume
        target_path.mkdir(exist_ok=True)

        local_tar = dest_dir / target_name
        log.info(f"📥 Descargando {target_name} desde pCloud...")
        r = requests.get(
            f"{API_BASE}{rel_path}",
            params={"auth": self.pc.token},
            stream=True,
            timeout=3600,
        )
        r.raise_for_status()
        with open(local_tar, "wb") as f:
            for chunk in r.iter_content(chunk_size=64 * 1024):
                f.write(chunk)

        # Extraer
        log.info(f"📂 Extrayendo a {target_path}...")
        with tarfile.open(local_tar, "r:gz") as tar:
            tar.extractall(path=target_path, filter="data")

        local_tar.unlink(missing_ok=True)

        return {
            "ok": True,
            "volume": volume,
            "date": date,
            "size": target_file["size"],
            "restored_to": str(target_path),
        }


# ─── CLI ─────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="volumen-vault snapshot engine")
    parser.add_argument("--serve", action="store_true", help="Arrancar API REST")
    parser.add_argument("--list", action="store_true", help="Listar snapshots remotos")
    parser.add_argument("--list-volumes", action="store_true", help="Listar volúmenes Docker locales")
    parser.add_argument("--restore", nargs=2, metavar=("VOLUMEN", "FECHA"), help="Restaurar snapshot")
    parser.add_argument("--once", action="store_true", help="Correr un snapshot inmediato")
    args = parser.parse_args()

    if args.list_volumes:
        for v in list_docker_volumes():
            print(v)
        return

    if args.serve:
        from api import start_api
        start_api(Vault)
        return

    vault = Vault()

    if args.list:
        snaps = vault.list_snapshots()
        print(json.dumps(snaps, indent=2))
        return

    if args.restore:
        vol, date = args.restore
        result = vault.restore(vol, date)
        print(json.dumps(result, indent=2))
        return

    # Default: un ciclo de snapshot
    report = vault.run_once()
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
