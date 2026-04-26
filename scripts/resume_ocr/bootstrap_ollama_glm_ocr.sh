#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if ! command -v ollama >/dev/null 2>&1; then
  echo "ollama command not found. Install Ollama first."
  exit 1
fi

ollama pull glm-ocr
ollama create glm-ocr-optimized -f "${ROOT_DIR}/configs/GLM-Config"
ollama list | grep -E "glm-ocr|glm-ocr-optimized" || true

echo "GLM-OCR local model is ready."
