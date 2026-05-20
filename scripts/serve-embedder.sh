#!/usr/bin/env bash
# Serve Jina Embeddings v4 locally via llama.cpp (Apple Silicon / Metal).
#
# Prereq:
#   brew install llama.cpp
#   mkdir -p models
#   # Download a Jina v4 GGUF into models/, e.g.:
#   #   huggingface-cli download <jina-v4-gguf-repo> jina-embeddings-v4.Q4_K_M.gguf --local-dir models/
#
# Listens on $EMBEDDER_PORT (default 8080). Endpoints exposed:
#   POST /embedding   { "input": "text" }  ->  { "embedding": [...] }
#
# Task-LoRA dual mode is handled at the client layer (nexus/ingest/embedder.py)
# by prepending the appropriate instruction prefix per chunk type.

set -euo pipefail

MODEL_PATH="${EMBEDDER_MODEL:-models/jina-embeddings-v4.Q4_K_M.gguf}"
PORT="${EMBEDDER_PORT:-8080}"
CTX_SIZE="${EMBEDDER_CTX:-8192}"

if ! command -v llama-server >/dev/null 2>&1; then
  echo "ERROR: llama-server not found. Install via: brew install llama.cpp" >&2
  exit 127
fi

if [ ! -f "$MODEL_PATH" ]; then
  echo "ERROR: model not found at $MODEL_PATH" >&2
  echo "Download a Jina v4 GGUF into models/ first." >&2
  exit 1
fi

echo "Starting embedder on :$PORT (model=$MODEL_PATH)"
exec llama-server \
  --model "$MODEL_PATH" \
  --port "$PORT" \
  --ctx-size "$CTX_SIZE" \
  --embedding \
  --pooling mean \
  --batch-size 32 \
  --n-gpu-layers 999
