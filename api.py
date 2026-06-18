"""
api.py — API REST para volumen-vault
======================================
Expone endpoints para:
  - Health check
  - Listar snapshots remotos (todos o por volumen)
  - Disparar snapshot manual
  - Restaurar un snapshot a una ruta específica
  - Listar volúmenes Docker disponibles

Pensada para que un agente IA (LangGraph, etc.) pueda consultar
"qué snapshots tengo disponibles" y decidir restaurar uno.
"""
import os
import threading
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from snapshot import Vault, load_env, log

load_env()

API_KEY = os.environ.get("API_KEY", "")
API_PORT = int(os.environ.get("API_PORT", "8095"))
SNAPSHOT_CRON = os.environ.get("SNAPSHOT_CRON", "0 2 * * *")

app = FastAPI(
    title="volumen-vault",
    description="Volume backup & restore for Docker volumes → pCloud",
    version="1.0.0",
)


def check_api_key(x_api_key: Optional[str] = Header(None)):
    if not API_KEY:
        return
    if x_api_key != API_KEY:
        raise HTTPException(401, "Invalid API key")


# ─── Vault singleton (lazy) ─────────────────────────────────────────────
_vault: Optional[Vault] = None
_vault_lock = threading.Lock()


def get_vault() -> Vault:
    global _vault
    if _vault is None:
        with _vault_lock:
            if _vault is None:
                _vault = Vault()
    return _vault


# ─── Scheduler ──────────────────────────────────────────────────────────
scheduler: Optional[BackgroundScheduler] = None


def start_scheduler():
    """Arranca el scheduler APScheduler si SNAPSHOT_CRON está definido."""
    global scheduler
    if not SNAPSHOT_CRON.strip():
        log.info("Scheduler deshabilitado (SNAPSHOT_CRON vacío)")
        return

    scheduler = BackgroundScheduler(daemon=True)
    # Parse "min hour dom mon dow"
    parts = SNAPSHOT_CRON.split()
    trigger = CronTrigger(
        minute=parts[0] if len(parts) > 0 else "*",
        hour=parts[1] if len(parts) > 1 else "*",
        day=parts[2] if len(parts) > 2 else "*",
        month=parts[3] if len(parts) > 3 else "*",
        day_of_week=parts[4] if len(parts) > 4 else "*",
    )

    def scheduled_job():
        try:
            vault = get_vault()
            report = vault.run_once()
            log.info(f"✓ Snapshot programado: {report['matched']} vols, {len(report['snapshots'])} subidos")
        except Exception as e:
            log.exception(f"✗ Snapshot programado falló: {e}")

    scheduler.add_job(scheduled_job, trigger)
    scheduler.start()
    log.info(f"✓ Scheduler arrancado: '{SNAPSHOT_CRON}'")


def start_api(VaultClass=None):
    """Función entry-point para `python snapshot.py --serve`."""
    import uvicorn

    start_scheduler()
    uvicorn.run(app, host="0.0.0.0", port=API_PORT, log_level="info")


# ─── Endpoints ──────────────────────────────────────────────────────────
@app.get("/healthz")
def healthz():
    return {
        "ok": True,
        "service": "volumen-vault",
        "scheduler": SNAPSHOT_CRON if SNAPSHOT_CRON else "disabled",
    }


@app.get("/api/vault/volumes")
def list_docker_volumes_local(_: None = None):
    """Lista volúmenes Docker del host (útil para IAs que quieren saber qué hay)."""
    check_api_key()
    from snapshot import list_docker_volumes
    return {"volumes": list_docker_volumes()}


@app.get("/api/vault/snapshots")
def list_snapshots(
    volume: Optional[str] = Query(None, description="Filtrar por nombre de volumen"),
    limit: int = Query(100, description="Máximo de snapshots a devolver"),
):
    """Lista todos los snapshots disponibles en pCloud."""
    check_api_key()
    try:
        vault = get_vault()
        snaps = vault.list_snapshots(volume=volume)
        return {
            "count": len(snaps[:limit]),
            "total": len(snaps),
            "snapshots": snaps[:limit],
        }
    except Exception as e:
        raise HTTPException(500, f"Error listando: {e}")


class SnapshotRequest(BaseModel):
    note: Optional[str] = None  # nota libre para identificar este run


@app.post("/api/vault/snapshot")
def trigger_snapshot(req: SnapshotRequest, x_api_key: Optional[str] = Header(None)):
    """Dispara un snapshot inmediato (no espera al cron)."""
    check_api_key(x_api_key)
    try:
        vault = get_vault()
        report = vault.run_once()
        if req.note:
            report["note"] = req.note
        return {"ok": True, "report": report}
    except Exception as e:
        raise HTTPException(500, f"Snapshot falló: {e}")


class RestoreRequest(BaseModel):
    volume: str
    date: str  # ej. "20260618-143022"
    dest_dir: Optional[str] = "/tmp/restore"  # dentro del contenedor vault


@app.post("/api/vault/restore")
def restore_snapshot(req: RestoreRequest, x_api_key: Optional[str] = Header(None)):
    """
    Descarga un snapshot de pCloud y lo extrae en dest_dir/<volume>/.
    Devuelve la ruta absoluta donde quedó.
    """
    check_api_key(x_api_key)
    try:
        vault = get_vault()
        result = vault.restore(req.volume, req.date, Path(req.dest_dir))
        return result
    except Exception as e:
        raise HTTPException(404 if "no existe" in str(e) else 500, str(e))


@app.get("/api/vault/info")
def vault_info():
    """Metadata del vault: cuántos snapshots, espacio usado, etc."""
    check_api_key()
    try:
        vault = get_vault()
        snaps = vault.list_snapshots()
        by_volume = {}
        for s in snaps:
            v = s["volume"]
            by_volume.setdefault(v, {"count": 0, "total_size": 0, "latest": None})
            by_volume[v]["count"] += 1
            by_volume[v]["total_size"] += s["size"]
            if by_volume[v]["latest"] is None or s["date"] > by_volume[v]["latest"]:
                by_volume[v]["latest"] = s["date"]

        return {
            "vault_folder_id": vault.vault_root_id,
            "total_snapshots": len(snaps),
            "volumes": by_volume,
        }
    except Exception as e:
        raise HTTPException(500, str(e))


# Arrancar scheduler cuando se carga el módulo (para Gunicorn/Uvicorn)
import atexit
atexit.register(lambda: scheduler.shutdown(wait=False) if scheduler else None)
