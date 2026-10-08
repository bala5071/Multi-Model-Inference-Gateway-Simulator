# The gateway in simulator mode: no model, no GPU, nothing else to run.
#
#   docker build -t inference-gateway .
#   docker run --rm -p 8000:8000 inference-gateway
#
# For real models, point it at model servers with GATEWAY_BACKEND / GATEWAY_BACKENDS
# (see docker-compose.yml and docs/12-real-models.md).

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
RUN uv sync --locked --no-dev

# Do not run as root.
RUN useradd --create-home gateway
USER gateway

EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s --retries=3 \
    CMD ["/app/.venv/bin/python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz')"]

CMD ["/app/.venv/bin/uvicorn", "gateway.app:create_default_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
