FROM python:3.12-alpine

# Docker CLI para listar/snapshot volúmenes + bash para entrypoint
RUN apk add --no-cache docker-cli bash curl

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY snapshot.py api.py ./

# Healthcheck
HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
  CMD curl -fsS http://localhost:${API_PORT:-8095}/healthz || exit 1

EXPOSE 8095

# Default: arrancar API REST (que también arranca el scheduler)
CMD ["python", "snapshot.py", "--serve"]
