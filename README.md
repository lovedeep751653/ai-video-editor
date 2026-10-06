# AI Video Editor

A mobile-first app that turns raw phone footage into a finished short video,
automatically. Upload videos and photos; it measures every moment, drops the
dark, blurry, shaky and empty parts, keeps the best ones, trims and arranges
them, adds transitions, a colour look, slow motion, a title, music cut to the
beat and captions, then renders an MP4 to preview and download.

## What it does

- **Automatic editing** — sharpness, lighting, contrast, movement, camera
  shake, loudness and scene changes are measured for every moment of every
  file; the best sections win.
- **Shapes** — 9:16, 16:9, 1:1, or automatic from the footage.
- **45 transitions** across four styles (smooth, creative, cinematic, straight
  cuts), 8 colour looks plus automatic light/colour correction, slow motion,
  Ken Burns on photos, on-screen title.
- **Music** — finds the tempo and phase of a track and makes every cut land on
  a beat.
- **Captions** — speech transcribed by Google AI and burned in, with 45 styles
  (15 looks × simple / pop-in / word-highlight) in English, Hindi (Devanagari)
  and Punjabi (Gurmukhi), plus Hinglish.
- **Typed requests** — "make it cinematic, 30 seconds, vertical, punjabi
  subtitles" becomes settings: via Google AI when a key is set, via a built-in
  phrase list otherwise.
- **AI creation** — pictures (Imagen / Gemini image models), video clips (Veo),
  and a whole edited video from one idea.
- **Installable** — add to the phone's home screen (PWA), or install the
  Android APK built from `android/`.

## Layout

| Path | What it is |
| --- | --- |
| `app/engine/analyze.py` | measures every moment of every file |
| `app/engine/plan.py` | scores moments and builds the editing plan |
| `app/engine/music.py` | tempo and beat-grid detection |
| `app/engine/effects.py` | colour looks, transition sets, title text |
| `app/engine/captions.py` | 45 caption styles, three scripts, ASS output |
| `app/engine/genai.py` | Google AI: understanding, pictures, video, speech |
| `app/engine/intent.py` | typed request → settings, without any AI |
| `app/engine/render.py` | FFmpeg rendering, transitions, music, captions |
| `app/engine/pipeline.py` | runs the whole job and reports real progress |
| `app/server.py` | web API, job queue, access code, settings |
| `app/static/` | the phone screen |
| `app/fonts/` | caption fonts (Latin, Devanagari, Gurmukhi) |
| `android/` | Android app (a full-screen window onto the editor) |
| `tests/` | engine and end-to-end app tests |

## Running it

Locally: `./run.sh` (Python 3.11+, FFmpeg, `pip install -r requirements.txt`),
then open `http://localhost:8000`.

Anywhere with Docker: `docker build -t editor . && docker run -p 7860:7860 editor`.

| Variable | Meaning |
| --- | --- |
| `APP_PASSWORD` | access code required before the app can be used (set this when hosting publicly) |
| `GEMINI_API_KEY` | Google AI key; can also be saved in the app's Settings |
| `EDITOR_DATA` | where jobs are stored (default `data/`) |
| `EDITOR_KEEP_HOURS` | how long finished videos are kept (default 24) |

## Tests

- `python3 tests/test_engine.py` — editing, beat sync, looks, captions, typed
  requests (~4 minutes).
- `python3 tests/test_app.py` — the whole web app against a stand-in for
  Google's AI, so nothing is charged (~2 minutes).

## Android APK

`android/` is a small Android app that opens the editor full screen, handles
uploads from the phone's gallery and saves downloads to Movies. The workflow in
`.github/workflows/android.yml` builds it on GitHub and publishes
`AI-Video-Editor.apk` as a release, so no Android tooling is needed locally. On
first launch the app asks once for the editor's web address.
