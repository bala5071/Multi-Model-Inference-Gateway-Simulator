#!/usr/bin/env bash
# Download the three GGUF models (Q4_K_M) and run one llama.cpp llama-server per tier.
#
#   scripts/run_local_models.sh download   # fetch the model files (about 7.8 GB)
#   scripts/run_local_models.sh start      # download if needed, then start the servers
#   scripts/run_local_models.sh status     # which servers answer
#   scripts/run_local_models.sh stop       # stop the servers this script started
#
# Then run the gateway against them:
#
#   GATEWAY_BACKEND=llamacpp uv run uvicorn gateway.app:create_default_app --factory --port 8000
#
# Needs: bash, curl, and llama-server from llama.cpp on PATH (or LLAMA_SERVER=/path/to/it).
# On Windows run it from Git Bash or WSL. See docs/12-real-models.md, including the note
# on the Llama license: by downloading these files you accept Meta's license terms.
#
# Settings (environment variables):
#   MODELS_DIR     where model files go             (default: ./models)
#   RUN_DIR        where logs and pid files go      (default: ./.run/llama)
#   LLAMA_SERVER   the llama-server binary          (default: llama-server)
#   HOST           address the servers listen on    (default: 127.0.0.1)
#   PARALLEL       slots per server; llama-server batches across its slots (default: 4)
#   CTX_PER_SLOT   context tokens per slot          (default: 4096)
#   GPU_LAYERS     layers to offload to the GPU     (default: 0 = CPU only; 99 = all)
#   TIERS          which tiers to act on            (default: "economy standard premium")

set -euo pipefail

MODELS_DIR="${MODELS_DIR:-models}"
RUN_DIR="${RUN_DIR:-.run/llama}"
LLAMA_SERVER="${LLAMA_SERVER:-llama-server}"
HOST="${HOST:-127.0.0.1}"
PARALLEL="${PARALLEL:-4}"
CTX_PER_SLOT="${CTX_PER_SLOT:-4096}"
GPU_LAYERS="${GPU_LAYERS:-0}"
TIERS="${TIERS:-economy standard premium}"

HF="https://huggingface.co"

# tier -> port, file name, download URL. Ports match src/gateway/backends.yaml.
port_of() {
  case "$1" in
    economy) echo 8081 ;;
    standard) echo 8082 ;;
    premium) echo 8083 ;;
    *) echo "unknown tier: $1" >&2; exit 2 ;;
  esac
}

file_of() {
  case "$1" in
    economy) echo "Llama-3.2-1B-Instruct-Q4_K_M.gguf" ;;
    standard) echo "Llama-3.2-3B-Instruct-Q4_K_M.gguf" ;;
    premium) echo "Meta-Llama-3.1-8B-Instruct-Q4_K_M.gguf" ;;
    *) echo "unknown tier: $1" >&2; exit 2 ;;
  esac
}

url_of() {
  case "$1" in
    economy) echo "$HF/bartowski/Llama-3.2-1B-Instruct-GGUF/resolve/main/$(file_of "$1")" ;;
    standard) echo "$HF/bartowski/Llama-3.2-3B-Instruct-GGUF/resolve/main/$(file_of "$1")" ;;
    premium) echo "$HF/bartowski/Meta-Llama-3.1-8B-Instruct-GGUF/resolve/main/$(file_of "$1")" ;;
    *) echo "unknown tier: $1" >&2; exit 2 ;;
  esac
}

download() {
  mkdir -p "$MODELS_DIR"
  for tier in $TIERS; do
    local file="$MODELS_DIR/$(file_of "$tier")"
    if [ -f "$file" ]; then
      echo "$tier: already have $file"
      continue
    fi
    echo "$tier: downloading $(url_of "$tier")"
    # Download to a .part file and resume it if interrupted, so a half-finished file is
    # never mistaken for a model.
    curl --fail --location --continue-at - --output "$file.part" "$(url_of "$tier")"
    mv "$file.part" "$file"
  done
}

is_up() {
  curl --silent --fail --max-time 2 --output /dev/null "http://$HOST:$(port_of "$1")/health"
}

start() {
  if ! command -v "$LLAMA_SERVER" >/dev/null 2>&1; then
    echo "llama-server not found. Install llama.cpp, or set LLAMA_SERVER=/path/to/llama-server." >&2
    echo "Or use Ollama instead: see docs/12-real-models.md." >&2
    exit 1
  fi
  download
  mkdir -p "$RUN_DIR"
  for tier in $TIERS; do
    local port; port="$(port_of "$tier")"
    if is_up "$tier"; then
      echo "$tier: something already answers on port $port; leaving it alone"
      continue
    fi
    echo "$tier: starting llama-server on $HOST:$port ($PARALLEL slots)"
    # --ctx-size is shared between the slots, so give each slot CTX_PER_SLOT tokens.
    "$LLAMA_SERVER" \
      --model "$MODELS_DIR/$(file_of "$tier")" \
      --host "$HOST" --port "$port" \
      --parallel "$PARALLEL" \
      --ctx-size "$((CTX_PER_SLOT * PARALLEL))" \
      --n-gpu-layers "$GPU_LAYERS" \
      >"$RUN_DIR/$tier.log" 2>&1 &
    echo $! >"$RUN_DIR/$tier.pid"
  done

  echo "waiting for the models to load..."
  for tier in $TIERS; do
    local tries=0
    until is_up "$tier"; do
      tries=$((tries + 1))
      if [ "$tries" -gt 120 ]; then
        echo "$tier: not ready after 120 s; see $RUN_DIR/$tier.log" >&2
        exit 1
      fi
      if [ -f "$RUN_DIR/$tier.pid" ] && ! kill -0 "$(cat "$RUN_DIR/$tier.pid")" 2>/dev/null; then
        echo "$tier: llama-server exited; see $RUN_DIR/$tier.log" >&2
        exit 1
      fi
      sleep 1
    done
    echo "$tier: ready on http://$HOST:$(port_of "$tier")"
  done
}

status() {
  for tier in $TIERS; do
    if is_up "$tier"; then
      echo "$tier: up on http://$HOST:$(port_of "$tier")"
    else
      echo "$tier: not answering on port $(port_of "$tier")"
    fi
  done
}

stop() {
  for tier in $TIERS; do
    local pidfile="$RUN_DIR/$tier.pid"
    if [ ! -f "$pidfile" ]; then
      echo "$tier: not started by this script"
      continue
    fi
    local pid; pid="$(cat "$pidfile")"
    if kill "$pid" 2>/dev/null; then
      echo "$tier: stopped (pid $pid)"
    else
      echo "$tier: was not running"
    fi
    rm -f "$pidfile"
  done
}

case "${1:-}" in
  download) download ;;
  start) start ;;
  status) status ;;
  stop) stop ;;
  *)
    echo "usage: $0 {download|start|status|stop}" >&2
    exit 2
    ;;
esac
