"""Web server: receives uploads from the phone, runs automatic edits and AI
creation in the background, reports real progress and serves the results."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import shutil
import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from engine import captions, genai, intent, pipeline
from engine.effects import LOOK_NAMES
from engine.plan import FORMATS

ROOT = Path(__file__).resolve().parent
DATA_ROOT = Path(os.environ.get("EDITOR_DATA", ROOT.parent / "data"))
DATA = DATA_ROOT / "jobs"
DATA.mkdir(parents=True, exist_ok=True)
SETTINGS = DATA_ROOT / "settings.json"
KEEP_HOURS = float(os.environ.get("EDITOR_KEEP_HOURS", 24))
PASSWORD = os.environ.get("APP_PASSWORD", "")
MAX_FILES = 40

app = FastAPI(title="AI editor open source")
jobs: dict[str, dict] = {}
lock = threading.Lock()
worker = ThreadPoolExecutor(max_workers=1)  # one job at a time; others wait their turn


# ---------- access code (only when APP_PASSWORD is set, e.g. when the app is online) ----------

def _token() -> str:
    return hmac.new(PASSWORD.encode(), b"video-editor", hashlib.sha256).hexdigest()


@app.middleware("http")
async def require_password(request: Request, call_next):
    path = request.url.path
    if PASSWORD and path.startswith("/api/") and path not in ("/api/login", "/api/health"):
        if not hmac.compare_digest(request.cookies.get("editor_auth", ""), _token()):
            return JSONResponse({"detail": "Please enter the access code.", "login": True}, status_code=401)
    return await call_next(request)


@app.post("/api/login")
def login(code: str = Form(...)):
    if not PASSWORD:
        return {"ok": True}
    if not hmac.compare_digest(code.strip(), PASSWORD):
        raise HTTPException(401, "That access code is wrong.")
    res = JSONResponse({"ok": True})
    res.set_cookie("editor_auth", _token(), max_age=60 * 60 * 24 * 365, httponly=True, samesite="lax")
    return res


# ---------- settings (Google AI key) ----------

def _settings() -> dict:
    try:
        return json.loads(SETTINGS.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _ai() -> tuple[str, dict]:
    key = os.environ.get("GEMINI_API_KEY") or _settings().get("gemini_key", "")
    models = _settings().get("models") or {}
    if key and not models:
        try:
            models = genai.check_key(key)
            s = _settings()
            s["models"] = models
            SETTINGS.write_text(json.dumps(s))
        except genai.AIError:
            models = {}
    return key, models


@app.get("/api/settings")
def get_settings():
    key, models = _ai()
    return {"ai_ready": bool(key and models), "key_hint": ("…" + key[-4:]) if key else "",
            "can_text": bool(models.get("text")), "can_image": bool(models.get("image")),
            "can_video": bool(models.get("video")), "models": models}


@app.post("/api/settings")
def save_settings(gemini_key: str = Form("")):
    key = gemini_key.strip()
    if not key:
        s = _settings()
        s.pop("gemini_key", None)
        s.pop("models", None)
        SETTINGS.write_text(json.dumps(s))
        return get_settings()
    try:
        models = genai.check_key(key)
    except genai.AIError as e:
        raise HTTPException(400, str(e))
    s = _settings()
    s.update(gemini_key=key, models=models)
    SETTINGS.write_text(json.dumps(s))
    SETTINGS.chmod(0o600)
    return get_settings()


# ---------- jobs ----------

def _save(job_id: str) -> None:
    (DATA / job_id / "status.json").write_text(json.dumps(jobs[job_id]))


def _update(job_id: str, **kw) -> None:
    with lock:
        jobs[job_id].update(kw)
        _save(job_id)


def _cleanup() -> None:
    cutoff = time.time() - KEEP_HOURS * 3600
    for d in DATA.iterdir():
        if d.is_dir() and d.stat().st_mtime < cutoff:
            shutil.rmtree(d, ignore_errors=True)
            jobs.pop(d.name, None)


def _load_existing() -> None:
    for d in DATA.iterdir():
        f = d / "status.json"
        if f.exists():
            job = json.loads(f.read_text())
            if job.get("state") in ("waiting", "working"):
                job.update(state="failed", error="The editor restarted while this was being made. Please try again.")
            jobs[d.name] = job


def _new_job(kind: str, options: dict) -> str:
    _cleanup()
    job_id = uuid.uuid4().hex[:12]
    (DATA / job_id / "inputs").mkdir(parents=True)
    with lock:
        jobs[job_id] = {"id": job_id, "kind": kind, "state": "waiting", "stage": "Waiting to start",
                        "progress": 0.0, "created": time.time(), "options": options}
        _save(job_id)
    return job_id


def _run_job(job_id: str, fn) -> None:
    _update(job_id, state="working", stage="Starting", progress=0.0)
    last = [0.0]

    def report(stage: str, frac: float) -> None:
        now = time.time()
        if now - last[0] > 0.4 or frac >= 1.0:
            last[0] = now
            _update(job_id, stage=stage, progress=round(min(max(frac, 0.0), 1.0), 4))

    try:
        result = fn(report)
        _update(job_id, state="done", stage="Done", progress=1.0, result=result)
    except Exception as e:  # report the real reason to the user
        traceback.print_exc()
        known = (ValueError, genai.AIError)
        msg = str(e) if isinstance(e, known) else f"Something went wrong: {e}"
        _update(job_id, state="failed", error=msg)
    finally:
        shutil.rmtree(DATA / job_id / "inputs", ignore_errors=True)


def _store(upload: UploadFile, folder: Path, i: int) -> Path:
    name = Path(upload.filename or f"file{i}").name.replace("/", "_")[:120] or f"file{i}"
    dest = folder / f"{i:02d}_{name}"
    with dest.open("wb") as out:
        shutil.copyfileobj(upload.file, out, 1024 * 1024)
    return dest


def _understand(request: str, report) -> tuple[dict, list[str], str]:
    """Typed request → options. Uses Google AI when set up, else the built-in phrase list."""
    if not request.strip():
        return {}, [], ""
    key, models = _ai()
    if key and models.get("text"):
        report("Reading your request", 0.0)
        try:
            return intent.clean(genai.interpret(key, models["text"], request)), [], "ai"
        except genai.AIError as e:
            return intent.clean(intent.parse_keywords(request)), [f"Google AI couldn't read the request ({e}); "
                                                                   "used the built-in understanding instead"], "basic"
    return intent.clean(intent.parse_keywords(request)), [], "basic"


def _edit_options(form: dict) -> dict:
    if form["format"] not in ("auto", *FORMATS):
        raise HTTPException(400, "Unknown video shape.")
    if form["style"] not in ("auto", "smooth", "fast"):
        raise HTTPException(400, "Unknown editing pace.")
    if form["look"] not in ("auto", *LOOK_NAMES):
        raise HTTPException(400, "Unknown look.")
    if form["transitions"] not in ("auto", "soft", "dynamic", "cinematic", "cuts"):
        raise HTTPException(400, "Unknown transition style.")
    if form["length"] != "auto":
        try:
            if not 5 <= float(form["length"]) <= 180:
                raise ValueError
        except ValueError:
            raise HTTPException(400, "Length must be between 5 and 180 seconds.")
    if form["captions"] not in ("auto", "on", "off") or form["caption_lang"] not in captions.LANGS \
            or form["caption_style"] not in captions.STYLES or form["caption_pos"] not in captions.POSITIONS:
        raise HTTPException(400, "Unknown caption setting.")
    return {k: form[k] for k in ("format", "style", "length", "look", "transitions", "slowmo", "title",
                                 "captions", "caption_lang", "caption_style", "caption_pos")}


@app.post("/api/jobs")
async def create_job(
    files: list[UploadFile] = File(...),
    music: UploadFile | None = File(None),
    format: str = Form("auto"), style: str = Form("auto"), length: str = Form("auto"),
    look: str = Form("auto"), transitions: str = Form("auto"), slowmo: str = Form("auto"),
    title: str = Form(""), request: str = Form(""),
    captions_mode: str = Form("auto", alias="captions"), caption_lang: str = Form("auto"),
    caption_style: str = Form(captions.DEFAULT_STYLE), caption_pos: str = Form("bottom"),
):
    if not files:
        raise HTTPException(400, "Please add at least one video or photo.")
    if len(files) > MAX_FILES:
        raise HTTPException(400, f"Please add at most {MAX_FILES} files at once.")
    options = _edit_options(dict(format=format, style=style, length=length, look=look,
                                 transitions=transitions, slowmo=slowmo, title=title[:80],
                                 captions=captions_mode, caption_lang=caption_lang,
                                 caption_style=caption_style, caption_pos=caption_pos))
    job_id = _new_job("edit", {**options, "request": request[:1000]})
    folder = DATA / job_id / "inputs"
    inputs = [_store(f, folder, i) for i, f in enumerate(files)]
    music_path = _store(music, folder, 99) if music is not None and music.filename else None

    def work(report):
        typed, notes, how = _understand(request, report)
        merged = {**options, **typed}
        result = pipeline.run(inputs, DATA / job_id, merged, report, music_path=music_path, ai=_ai())
        result["understood"] = intent.describe(typed, bool(music_path))
        result["understood_by"] = how
        if request.strip() and not typed:
            notes.append("Your typed request didn't contain anything the editor could act on, "
                         "so everything was decided automatically")
        if request.strip() and intent.wants_music(request) and not music_path:
            notes.append("To match cuts to music, add a music file")
        result["notes"] = notes + result["notes"]
        return result

    worker.submit(_run_job, job_id, work)
    return {"id": job_id}


@app.post("/api/create")
async def create_ai(
    mode: str = Form(...),          # image | video | story
    prompt: str = Form(...),
    format: str = Form("9:16"),
    source: str = Form("pictures"),  # story only: pictures | clips
    count: int = Form(4),
    request: str = Form(""),
):
    if mode not in ("image", "video", "story"):
        raise HTTPException(400, "Unknown creation type.")
    if not prompt.strip():
        raise HTTPException(400, "Please describe what you want to create.")
    if format not in FORMATS:
        raise HTTPException(400, "Unknown shape.")
    key, models = _ai()
    if not key:
        raise HTTPException(400, "AI creation needs a Google AI key. Open Settings to add one.")
    need = "video" if mode == "video" or (mode == "story" and source == "clips") else "image"
    if not models.get(need):
        raise HTTPException(400, f"Your Google AI key can't make {'videos' if need == 'video' else 'pictures'}. "
                                 "This usually means billing isn't turned on for it in Google AI Studio.")
    count = max(1, min(count, 3 if need == "video" else 8))
    job_id = _new_job(mode, {"prompt": prompt[:1000], "format": format, "source": source, "count": count})
    work_dir = DATA / job_id

    def make(report, prompts):
        files = []
        for i, p in enumerate(prompts):
            label = f"Creating {'video clip' if need == 'video' else 'picture'} {i + 1} of {len(prompts)}"
            base = i / len(prompts)
            report(label, base * (0.6 if mode == "story" else 1))
            if need == "video":
                f = genai.generate_video(key, models["video"], p, format, work_dir / f"ai_clip_{i + 1}.mp4",
                                         lambda x: report(label, (base + x / len(prompts)) * (0.6 if mode == "story" else 1)))
            else:
                f = genai.generate_image(key, models["image"], p, format, work_dir / f"ai_picture_{i + 1}")
            files.append(f)
        return files

    def work(report):
        if mode in ("image", "video"):
            files = make(report, [prompt] * count if count == 1 else genai.shot_ideas(key, models.get("text"), prompt, count))
            return {"files": [{"name": f.name, "kind": "video" if f.suffix == ".mp4" else "image"} for f in files]}
        report("Planning the shots", 0.0)
        shots = genai.shot_ideas(key, models.get("text"), prompt, count)
        files = make(report, shots)
        typed, notes, how = _understand(request, report)
        opts = {**typed, "format": format}  # the shape picked for the AI shots stays
        result = pipeline.run(files, work_dir, {**opts, "captions": "off"},
                              lambda s, f: report(s, 0.6 + 0.4 * f), ai=_ai())
        result["files"] = [{"name": f.name, "kind": "video" if f.suffix == ".mp4" else "image"} for f in files]
        result["shots"] = shots
        typed.pop("format", None)
        result["understood"] = intent.describe(typed, False)
        result["notes"] = notes + result["notes"]
        return result

    worker.submit(_run_job, job_id, work)
    return {"id": job_id}


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404, "This was not found. It may have expired.")
    return JSONResponse(job)


def _final(job_id: str) -> Path:
    job = jobs.get(job_id)
    path = DATA / job_id / "final.mp4"
    if not job or job.get("state") != "done" or not path.exists():
        raise HTTPException(404, "The video isn't ready.")
    return path


@app.get("/api/jobs/{job_id}/video")
def job_video(job_id: str):
    return FileResponse(_final(job_id), media_type="video/mp4")


@app.get("/api/jobs/{job_id}/download")
def job_download(job_id: str):
    return FileResponse(_final(job_id), media_type="video/mp4", filename=f"ai-edit-{job_id[:6]}.mp4")


@app.get("/api/jobs/{job_id}/files/{name}")
def job_file(job_id: str, name: str, download: bool = False):
    job = jobs.get(job_id)
    allowed = {f["name"] for f in ((job or {}).get("result") or {}).get("files", [])}
    path = DATA / job_id / name
    if name not in allowed or not path.exists():
        raise HTTPException(404, "File not found.")
    return FileResponse(path, filename=name if download else None)


@app.get("/api/jobs/{job_id}/thumb")
def job_thumb(job_id: str):
    p = DATA / job_id / "thumb.jpg"
    if not p.exists():
        raise HTTPException(404)
    return FileResponse(p, media_type="image/jpeg")


PREVIEW_TEXT = {"en": "This is my best day ever", "hi": "यह मेरा सबसे अच्छा दिन है",
                "pa": "ਇਹ ਮੇਰਾ ਸਭ ਤੋਂ ਵਧੀਆ ਦਿਨ ਹੈ", "hinglish": "Yeh mera best din hai", "auto": "This is my best day ever"}


@app.get("/api/caption-preview/{style_id}")
def caption_preview(style_id: str, lang: str = "en"):
    """A real picture of the caption style, drawn by the same renderer the videos use."""
    if style_id not in captions.STYLES or lang not in PREVIEW_TEXT:
        raise HTTPException(404)
    cache = DATA_ROOT / "previews"
    cache.mkdir(exist_ok=True)
    png = cache / f"{style_id}_{lang}.png"
    if not png.exists():
        ass = cache / f"{style_id}_{lang}.ass"
        ass.write_text(captions.build_ass([{"start": 0, "end": 2, "text": PREVIEW_TEXT[lang]}], style_id,
                                          1080, 1080, "middle"), encoding="utf-8")
        import subprocess
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                        "gradients=s=1080x1080:c0=0x3b4a6b:c1=0x7a5c3e:d=2", "-vf",
                        f"subtitles=filename='{ass}':fontsdir='{captions.FONTS_DIR}',crop=1080:360:0:360,scale=480:-2",
                        "-ss", "1.2", "-frames:v", "1", str(png)], capture_output=True)
        ass.unlink(missing_ok=True)
    if not png.exists():
        raise HTTPException(500, "Preview failed")
    return FileResponse(png, media_type="image/png", headers={"Cache-Control": "max-age=86400"})


@app.get("/api/caption-styles")
def caption_styles():
    return {"styles": captions.style_list(), "langs": captions.LANGS, "default": captions.DEFAULT_STYLE}


@app.get("/api/health")
def health():
    return {"ok": True, "ffmpeg": shutil.which("ffmpeg") is not None, "password": bool(PASSWORD)}


_load_existing()
app.mount("/", StaticFiles(directory=ROOT / "static", html=True), name="static")
