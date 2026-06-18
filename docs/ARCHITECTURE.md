# Architecture

## Componentes

```
┌──────────────────────────────────────────────────────────────────────────┐
│                            Docker host                                   │
│                                                                          │
│  ┌─────────────────┐                                                     │
│  │  Volúmenes      │  erp-db-data, erp-minio-data,                       │
│  │  Docker         │  chatwoot-redis-data, evolution-*, ...              │
│  └────────┬────────┘                                                     │
│           │                                                              │
│           │ docker volume ls / mount                                     │
│           ▼                                                              │
│  ┌────────────────────────────────────────────────────────────┐          │
│  │  Contenedor volumen-vault (este proyecto)                 │          │
│  │                                                            │          │
│  │  ┌──────────────┐    ┌────────────────┐    ┌──────────────┐ │          │
│  │  │ APScheduler  │───▶│ snapshot.py    │───▶│ PCloud       │ │          │
│  │  │ (cron)       │    │  (core)        │    │ client       │ │          │
│  │  └──────────────┘    │                │    └──────┬───────┘ │          │
│  │                      │  • enumerate   │           │         │          │
│  │  ┌──────────────┐    │  • tar.gz      │           │         │          │
│  │  │ FastAPI      │───▶│  • upload      │           │         │          │
│  │  │ (api.py)     │    │  • retention   │           │         │          │
│  │  └──────────────┘    │  • restore     │           │         │          │
│  │                      └────────────────┘           │         │          │
│  │                                                    │         │          │
│  │  Puerto 8095 ◀──── HTTP/REST                      │         │          │
│  │  (API key opcional)                                │         │          │
│  └────────────────────────────────────────────────────┼─────────┘          │
│                                                       │                    │
└───────────────────────────────────────────────────────┼────────────────────┘
                                                        │
                                                        │ HTTPS
                                                        ▼
                                              ┌──────────────────┐
                                              │  api.pcloud.com  │
                                              │                  │
                                              │  /volumen-vault/ │
                                              │   20260618/      │
                                              │    *.tar.gz      │
                                              └──────────────────┘
```

## Flujo de snapshot

1. **Trigger** (cron o POST /api/vault/snapshot)
2. **Enumerate**: `docker volume ls` → lista nombres
3. **Filter**: aplica `INCLUDE_PATTERN` / `EXCLUDE_PATTERN`
4. **For each volume**:
   - `docker run -d --mount source=VOL,target=/data alpine sleep 300`
   - `docker exec CONTAINER tar czf - -C /data .` → stream al host
   - `requests.post(api.pcloud.com/uploadfile)` con el archivo
   - `docker rm -f CONTAINER` (cleanup)
5. **Index**: sube `_index/index-<fecha>.json` con metadata
6. **Retention**: borra snapshots > `RETENTION_SNAPSHOTS` por volumen

## Flujo de restore

1. Cliente: `POST /api/vault/restore {"volume": "X", "date": "Y"}`
2. pCloud: `getfilelink` → URL temporal
3. Download → `tar xzf` → `<dest_dir>/<volume>/`
4. Cliente monta el contenido en un volumen Docker nuevo o lo copia al original

## Decisiones de diseño

### ¿Por qué pCloud y no S3/MinIO?
- **Precio**: pCloud lifetime 500GB = $200 one-time, vs S3 ~$23/TB/month
- **Simplicidad**: una API REST, sin IAM, sin buckets
- **Ya lo usas**: integrás con tu explorador existente

### ¿Por qué un contenedor separado y no parte del docker-compose del ERP?
- **Independencia**: si Dokploy se cae entero, este contenedor sigue corriendo
- **Aislamiento de permisos**: solo este contenedor tiene acceso al socket Docker
- **Reutilizable**: lo puedes usar para otros proyectos además del ERP

### ¿Por qué retención por defecto 30?
- Suficiente para errores accidentales (te das cuenta en días)
- Suficiente para auditorías mensuales
- No llena tu quota de pCloud rápidamente
- Configurable: `RETENTION_SNAPSHOTS=0` para papelera infinita

### ¿Por qué NO ciframos los tar.gz?
- pCloud ya ofrece cifrado en tránsito (HTTPS) y opcional at-rest
- Cifrar local agrega complejidad y riesgo de perder la key
- Para datos sensibles, monta un volumen con LUKS/dm-crypt en el host
