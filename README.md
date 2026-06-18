# 📦 volumen-vault

**Volume backup & restore for Docker → pCloud**

Una "papelera de reciclaje" para tus volúmenes Docker. Si algo se borra (DB, MinIO, Redis, lo que sea), puedes recuperarlo desde pCloud con un solo comando o llamada API.

```
┌─────────────────────────────────────────────────────────────────────────┐
│  Docker host                                                            │
│                                                                         │
│  ┌──────────────┐      ┌──────────────────┐      ┌──────────────┐      │
│  │  postgres-db │─────▶│  volumen-vault   │─────▶│   pCloud     │      │
│  │  minio       │ tar  │  (cron diario)   │ tar  │ /volumen-    │      │
│  │  redis       │ gz   │  /api/vault/*    │ gz   │   vault/     │      │
│  │  evolution-* │─────▶│  8095            │─────▶│  20260618/   │      │
│  └──────────────┘      └──────────────────┘      │  ...tar.gz   │      │
│         ▲                    ▲                   └──────────────┘      │
│         │                    │                            ▲            │
│         └──── restore ───────┴────────────────────────────┘            │
└─────────────────────────────────────────────────────────────────────────┘
```

## ✨ Features

- 🗄 **Snapshot automático** de TODOS los volúmenes Docker a un tar.gz
- ☁️ **Sube a pCloud** (offsite, barato, versionado)
- 📅 **Scheduler configurable** (cron format)
- 🧹 **Retención** (mantiene N snapshots por volumen, configurable)
- 🔌 **API REST** para listar, restaurar, snapshot manual
- 🤖 **AI-friendly**: endpoints devuelven JSON listo para que un LLM decida
- 🔐 **Seguro**: credenciales solo en `.env`, contenedor con API key opcional
- 📦 **Auto-contenido**: corre en Docker, no toca el host

## 🚀 Quick start

### 1. Pre-requisitos

- Docker + docker compose
- Una cuenta de pCloud (sin 2FA)
- Un folder dedicado en pCloud para los snapshots

### 2. Crear el folder vault en pCloud

1. Entra a https://my.pcloud.com
2. Crea una carpeta llamada `volumen-vault`
3. Click derecho → "Get link" → copia el número después de `folder/` (ej. `30123456789`)

### 3. Setup

```bash
git clone https://github.com/XAVIVILLARREAL/volumen-vault.git
cd volumen-vault
cp .env.example .env
# Edita .env con tus credenciales y el ID del folder
```

### 4. Levantar

```bash
docker compose up -d --build
docker compose logs -f volumen-vault
```

Vas a ver:
```
[volumen-vault] ✓ pCloud autenticado (region=us)
[volumen-vault] ✓ Vault root folder ID: 30123456789
[volumen-vault] ✓ Scheduler arrancado: '0 2 * * *'
INFO:     Uvicorn running on http://0.0.0.0:8095
```

### 5. Probar

```bash
# Health check
curl http://localhost:8095/healthz

# Listar volúmenes Docker del host
curl http://localhost:8095/api/vault/volumes

# Snapshot inmediato (no espera al cron)
curl -X POST http://localhost:8095/api/vault/snapshot

# Ver todos los snapshots
curl http://localhost:8095/api/vault/snapshots

# Ver info consolidada
curl http://localhost:8095/api/vault/info
```

## 📡 API Reference

| Método | Path | Descripción |
|---|---|---|
| GET | `/healthz` | Liveness check |
| GET | `/api/vault/volumes` | Lista volúmenes Docker del host |
| GET | `/api/vault/snapshots?volume=NAME` | Lista snapshots (filtro opcional) |
| GET | `/api/vault/info` | Metadata: total snapshots, por volumen, espacio usado |
| POST | `/api/vault/snapshot` | Dispara snapshot inmediato |
| POST | `/api/vault/restore` | Restaura un snapshot |

### Ejemplo: restaurar un volumen

```bash
curl -X POST http://localhost:8095/api/vault/restore \
  -H "Content-Type: application/json" \
  -d '{
    "volume": "erp-db-data",
    "date": "20260618-143022",
    "dest_dir": "/tmp/restore"
  }'

# Devuelve:
# {
#   "ok": true,
#   "volume": "erp-db-data",
#   "date": "20260618-143022",
#   "size": 123456789,
#   "restored_to": "/tmp/restore/erp-db-data"
# }
```

Después copias `/tmp/restore/erp-db-data/*` a donde esté el volumen original o lo montas en un contenedor nuevo.

## 🤖 Uso con Agentes IA (LangGraph, etc.)

La API está diseñada para que un agente pueda:

```python
# Pseudo-código de un agente que detecta que se borró un volumen
import requests

# 1) Detectar que falta algo
volumes = requests.get("http://volumen-vault:8095/api/vault/volumes").json()["volumes"]
if "erp-db-data" not in volumes:
    # 2) Preguntar al vault qué snapshots tiene
    snaps = requests.get(
        "http://volumen-vault:8095/api/vault/snapshots",
        params={"volume": "erp-db-data", "limit": 1}
    ).json()
    if snaps["count"] > 0:
        latest = snaps["snapshots"][0]
        # 3) Restaurar
        result = requests.post(
            "http://volumen-vault:8095/api/vault/restore",
            json={
                "volume": "erp-db-data",
                "date": latest["date"],
                "dest_dir": "/var/lib/docker/volumes/erp-db-data/_data"
            }
        )
```

## ⚙️ Configuración

Variables en `.env` (ver `.env.example`):

| Variable | Default | Descripción |
|---|---|---|
| `PCLOUD_EMAIL` | (requerido) | Email de pCloud |
| `PCLOUD_PASSWORD` | (requerido) | Password (sin 2FA) |
| `PCLOUD_REGION` | `us` | `us` o `eu` |
| `PCLOUD_VAULT_FOLDER_ID` | `0` | ID del folder raíz del vault |
| `SNAPSHOT_CRON` | `0 2 * * *` | Cuándo correr (formato cron) |
| `RETENTION_SNAPSHOTS` | `30` | Cuántos snapshots mantener por volumen (0 = infinito) |
| `INCLUDE_PATTERN` | `.*` | Regex de volúmenes a incluir |
| `EXCLUDE_PATTERN` | `^($|.tmp$\|.cache$)` | Regex de volúmenes a excluir |
| `API_KEY` | (vacío) | Si se define, header `X-API-Key` requerido |
| `DEBUG` | `false` | Más logs |

## 🛡 Seguridad

- El contenedor necesita `/var/run/docker.sock` → es RIESGOSO si el contenedor es comprometido. Mantenlo en una red interna.
- Si expones el puerto 8095 al exterior, **siempre** define `API_KEY`.
- Las credenciales de pCloud viven en `.env` (server-side). NUNCA las pongas en el código.

## 📂 Estructura del vault en pCloud

```
/volumen-vault/
├── 20260618-020000/         # Carpeta por fecha (un snapshot por día)
│   ├── erp-db-data.tar.gz
│   ├── erp-minio-data.tar.gz
│   ├── chatwoot-redis-data.tar.gz
│   └── ...
├── 20260619-020000/
│   └── ...
└── _index/
    ├── index-20260618-020000.json
    └── index-20260619-020000.json
```

## 🆘 Casos de uso

### Caso 1: Se borró accidentalmente un volumen

```bash
# 1. Ver qué snapshots hay
curl http://vault:8095/api/vault/snapshots?volume=erp-db-data

# 2. Restaurar el más reciente
curl -X POST http://vault:8095/api/vault/restore \
  -H "Content-Type: application/json" \
  -d '{"volume": "erp-db-data", "date": "20260618-020000"}'

# 3. Crear el volumen de nuevo con el contenido restaurado
docker volume create erp-db-data
docker run --rm -v erp-db-data:/data -v /tmp/restore/erp-db-data:/restore alpine \
  sh -c "cp -a /restore/. /data/"
```

### Caso 2: Migrar a otro servidor

```bash
# En el server A: snapshot manual
curl -X POST http://vault-A:8095/api/vault/snapshot

# En el server B: instalar volumen-vault con las MISMAS credenciales
# (los snapshots ya están en pCloud, no necesitas transferirlos)
docker compose up -d

# Listar y restaurar lo que necesites
curl http://vault-B:8095/api/vault/snapshots
```

### Caso 3: Auditoría mensual

```bash
# Ver info consolidada
curl http://vault:8095/api/vault/info | jq

# {
#   "vault_folder_id": 30123456789,
#   "total_snapshots": 87,
#   "volumes": {
#     "erp-db-data": {
#       "count": 30,
#       "total_size": 1234567890,
#       "latest": "20260618-020000"
#     },
#     ...
#   }
# }
```

## 🧪 Testing local sin Docker

```bash
# Solo prueba la conexión a pCloud
PCLOUD_EMAIL=... PCLOUD_PASSWORD=... python snapshot.py --list-volumes

# Snapshot manual (requiere Docker)
python snapshot.py --once

# Listar snapshots remotos
python snapshot.py --list

# Restaurar uno específico
python snapshot.py --restore erp-db-data 20260618-020000
```

## 📋 Roadmap

- [ ] Snapshot incremental (solo diff desde último backup)
- [ ] Compresión con zstd (más rápido que gzip)
- [ ] Notificación a Slack/Discord cuando un snapshot falla
- [ ] Backup pre-deploy automático (hook en Dokploy)
- [ ] Verificación de integridad (checksum SHA256 en cada restore)
- [ ] Multi-cloud (R2, S3, B2 además de pCloud)

## 📄 Licencia

MIT — ver [LICENSE](LICENSE).

## 🤝 Contribuciones

PRs bienvenidos. Mantén el código Python con `black` + `ruff` si agregas features.

---

Hecho con ❤️ por [Xtreme Diagnostics](https://xtremediagnostics.com) — porque perder una DB no debería ser el fin del mundo.
