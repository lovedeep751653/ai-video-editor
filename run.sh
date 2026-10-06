#!/bin/sh
# Starts the editor on this computer at http://localhost:8000
cd "$(dirname "$0")/app" && exec python3 -m uvicorn server:app --host 0.0.0.0 --port "${PORT:-8000}"
