# The gateway with its dashboard, in two stages: Node builds the dashboard, Python serves it.
#
#   docker build -t inference-gateway .
#   docker run --rm -p 127.0.0.1:8000:8000 inference-gateway
#
# Publish on 127.0.0.1 only: monitoring and load control are not authenticated, and a plain
# `-p 8000:8000` publishes on every interface (docs/platform-plan.md, section 9).
#
# That is the plain simulator: no model, no GPU, nothing else to run. `docker compose up`
# starts the same image in simulated live mode, with load running (docs/18-demo.md).
# For real models, point it at model servers with GATEWAY_BACKEND / GATEWAY_BACKENDS
# (see docker-compose.yml and docs/12-real-models.md).

# --- stage 1: build the dashboard ----------------------------------------------------------
FROM node:20-slim AS dashboard

WORKDIR /build/frontend

# Dependencies first, so this layer is reused until the lock file changes.
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci

COPY frontend/ ./
# Vite writes to ../src/gateway/dashboard, which here is /build/src/gateway/dashboard.
RUN npm run build

# --- stage 2: the gateway ------------------------------------------------------------------
FROM python:3.12-slim

# uv, copied from its official image, installs exactly what uv.lock pins.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Dependencies first, so this layer is reused until the lock file changes.
COPY pyproject.toml uv.lock README.md LICENSE ./
RUN uv sync --locked --no-dev --no-install-project

COPY src ./src
# The built dashboard goes where the gateway looks for it; it is then served at /dashboard.
COPY --from=dashboard /build/src/gateway/dashboard ./src/gateway/dashboard
RUN uv sync --locked --no-dev

# The CLI that creates applications and issues keys, for use inside the container
# (docker compose exec gateway /app/.venv/bin/python scripts/manage.py ...).
COPY scripts/manage.py ./scripts/manage.py

# Do not run as root. /data holds the database (a volume in docker-compose.yml), readable by
# the gateway user only. No secret is set here: the pepper comes from the environment.
RUN useradd --create-home gateway \
    && mkdir /data && chown gateway:gateway /data && chmod 700 /data
USER gateway

EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s --retries=3 \
    CMD ["/app/.venv/bin/python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz')"]

CMD ["/app/.venv/bin/uvicorn", "gateway.app:create_default_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
