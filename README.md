# AI Video Editor

Mobile-friendly web app: upload videos and photos, and it automatically
picks the best moments, removes dark/blurry/shaky/empty parts, trims,
arranges them in filmed order, adds transitions and a slow zoom on photos,
reshapes to 9:16, 16:9 or 1:1 (or picks automatically), evens out the sound,
and renders an MP4 to preview and download.

- `app/engine/analyze.py` measures sharpness, lighting, contrast, movement, shake, sound and scene changes.
- `app/engine/plan.py` scores moments and builds the editing plan (all "auto" choices are made here).
- `app/engine/render.py` renders with FFmpeg (xfade transitions, blurred-background fit, Ken Burns photos, loudness).
- `app/server.py` upload API, background job queue with real progress, video download.
- `app/static/` the phone screen.

Run locally: `./run.sh` (needs Python 3.11+, FFmpeg, `pip install -r requirements.txt`).
Run anywhere with Docker: `docker build -t editor . && docker run -p 7860:7860 editor`.
Tests: `python3 tests/test_engine.py`.
