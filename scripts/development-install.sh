#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if ! command -v uv >/dev/null 2>&1; then
  echo "uv is required. Install from https://docs.astral.sh/uv/" >&2
  exit 1
fi

uv python install 3.11
uv venv --python 3.11 .venv
uv sync --all-extras
echo "Development environment ready. Activate with: source .venv/bin/activate"
