#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

# Load .env if present (export each line)
if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

exec .venv/bin/python -m src.app
