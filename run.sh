#!/bin/sh
# Starts the editor on this computer at http://localhost:8000 (needs Python 3.11+, FFmpeg and numpy)
cd "$(dirname "$0")/app" && exec python3 server.py
