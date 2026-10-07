# AI editor open source

An Android app that edits videos entirely on the phone: no server, no hosting,
nothing uploaded. It turns raw phone footage into a finished video,
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
- **Clean up long videos** — 5 to 30 minute videos keep their story; pauses,
  silences, shaky and dark parts are removed.
- **Chat editing** — tell the AI what to change ("remove clip 3", "black and
  white", "captions in Punjabi", "cut the part with the dog"); each change makes
  a new version you can undo, and only the parts that changed are re-rendered.
- **On the phone** — Python (Chaquopy) and FFmpeg (ffmpeg-kit) run inside the
  APK; picked files are read in place, and the phone's hardware encoder is used
  when it has one.

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
| `app/engine/edits.py` | chat edits: applies the AI's changes to the plan |
| `app/engine/ff.py` | runs FFmpeg on a computer or inside the app |
| `app/server.py` | the editor's API, job queue, projects, versions |
| `app/android_main.py` | starts the editor inside the Android app |
| `app/static/` | the phone screen |
| `app/fonts/` | caption fonts (Latin, Devanagari, Gurmukhi) |
| `android/` | Android app (runs the editor on the phone) |
| `tests/` | engine and end-to-end app tests |

## Running it

On the phone: install `AI-editor-open-source.apk` from the
[apk-latest release](https://github.com/lovedeep751653/ai-video-editor/releases/tag/apk-latest).
AI features (captions, understanding any request, pictures, video) need a
Google AI key saved in the app's Settings; everything else works offline.

On a computer, for development: `./run.sh` (Python 3.11+, FFmpeg,
`pip install -r requirements.txt`), then open `http://localhost:8000`.

| Variable | Meaning |
| --- | --- |
| `APP_PASSWORD` | access code required before the app can be used |
| `GEMINI_API_KEY` | Google AI key; can also be saved in the app's Settings |
| `EDITOR_DATA` | where projects are stored (default `data/`) |

## Tests

- `python3 tests/test_engine.py` — editing, clean-up, chat edits, beat sync,
  looks, captions, typed requests.
- `python3 tests/test_app.py` — the whole app API against a stand-in for
  Google's AI, so nothing is charged.
- On GitHub, every build is installed on an Android emulator, which runs real
  edits inside the APK (`.github/scripts/selftest.sh`).

## Android APK

`android/` holds the Android app: it starts the editor inside the app and shows
it full screen, opens the phone's gallery picker, accepts videos shared from
other apps, saves to the gallery and keeps long edits running with a
notification. `.github/workflows/android.yml` builds it, tests it on an
emulator and publishes `AI-editor-open-source.apk` (64-bit phones).
