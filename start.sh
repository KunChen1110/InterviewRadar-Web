#!/usr/bin/env sh
set -eu

python -m pip install -r requirements.txt
exec python -m uvicorn backend.main:app --host 0.0.0.0 --port "${PORT:-8000}"
