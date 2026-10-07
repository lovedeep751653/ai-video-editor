"""The editor's web server: the phone screen (app/static) talks to it.

Inside the Android app it runs on the phone itself, on 127.0.0.1, started by
android_main.py. On a computer: `python3 server.py` (see run.sh). Only the
Python standard library is used, so it runs the same everywhere.

Routes are described in docs/API.md.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import mimetypes
import os
import re
import shutil
import threading
import time
import traceback
import urllib.parse
import uuid
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from engine import analyze, captions, edits, ff, freeai, genai, intent, pipeline, render
from engine.effects import LOOK_NAMES
from engine.plan import FORMATS, Plan

ROOT = Path(__file__).resolve().parent
STATIC = Path(os.environ.get("EDITOR_STATIC") or ROOT / "static")
DATA_ROOT = Path(os.environ.get("EDITOR_DATA") or ROOT.parent / "data")
SOURCES = DATA_ROOT / "sources"
PROJECTS = DATA_ROOT / "projects"
CREATIONS = DATA_ROOT / "creations"
SETTINGS = DATA_ROOT / "settings.json"
PASSWORD = os.environ.get("APP_PASSWORD", "")
APP_TOKEN = os.environ.get("EDITOR_TOKEN", "")  # set by the Android app: only its own screen may use the API
APP_MODE = os.environ.get("EDITOR_ANDROID") == "1"
MAX_FILES = 60
MAX_MINUTES = 30
VERSION = os.environ.get("EDITOR_VERSION", "2.0")

for d in (SOURCES, PROJECTS, CREATIONS):
    d.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("EDITOR_TMP", str(DATA_ROOT / "tmp"))
shutil.rmtree(DATA_ROOT / "tmp", ignore_errors=True)
(DATA_ROOT / "tmp").mkdir(exist_ok=True)


class HTTPError(Exception):
    def __init__(self, status: int, detail: str, **extra):
        super().__init__(detail)
        self.status, self.detail, self.extra = status, detail, extra


# ================================================================ settings

_settings_lock = threading.Lock()


def settings() -> dict:
    try:
        return json.loads(SETTINGS.read_text())
    except (FileNotFoundError, ValueError):
        return {}


def save_settings(s: dict) -> None:
    with _settings_lock:
        tmp = SETTINGS.with_suffix(".tmp")
        tmp.write_text(json.dumps(s))
        os.replace(tmp, SETTINGS)
        try:
            SETTINGS.chmod(0o600)
        except OSError:
            pass


def ai() -> tuple[str, dict]:
    return genai.FREE, genai.FREE_MODELS


def quality() -> str:
    return settings().get("quality", "1080")


def speed() -> str:
    return settings().get("speed", "fast")


def settings_view() -> dict:
    return {"quality": quality(), "speed": speed()}


# ================================================================ jobs

jobs: dict[str, dict] = {}
cancels: dict[str, threading.Event] = {}
jobs_lock = threading.Lock()
worker = ThreadPoolExecutor(max_workers=1)  # one heavy job at a time; the phone stays responsive


def new_job(kind: str, project: str | None = None) -> str:
    jid = uuid.uuid4().hex[:12]
    with jobs_lock:
        jobs[jid] = {"id": jid, "kind": kind, "state": "waiting", "stage": "Waiting to start", "progress": 0.0,
                     "eta": None, "error": None, "project": project, "result": None, "created": time.time(),
                     "started": None}
        cancels[jid] = threading.Event()
    return jid


def update_job(jid: str, **kw) -> None:
    with jobs_lock:
        jobs[jid].update(kw)


def submit(jid: str, fn, on_fail=None) -> None:
    """Runs fn(report) in the background; report(stage, fraction) updates the job."""

    def run():
        ev = cancels[jid]
        if ev.is_set():
            update_job(jid, state="cancelled", stage="Cancelled")
            if on_fail:
                on_fail("Cancelled")
            return
        ff.set_cancel_event(ev)
        start = time.time()
        update_job(jid, state="working", stage="Starting", started=start)
        best = [0.0]

        def report(stage: str, frac: float) -> None:
            frac = min(max(frac, best[0]), 1.0)
            best[0] = frac
            el = time.time() - start
            eta = round(el * (1 - frac) / frac) if frac > 0.04 and el > 4 else None
            update_job(jid, stage=stage, progress=round(frac, 4), eta=eta)

        try:
            result = fn(report)
            update_job(jid, state="done", stage="Done", progress=1.0, eta=0, result=result)
        except ff.Cancelled:
            update_job(jid, state="cancelled", stage="Cancelled", error="Cancelled")
            if on_fail:
                on_fail("Cancelled")
        except Exception as e:  # tell the user the real reason
            traceback.print_exc()
            known = (ValueError, genai.AIError, freeai.FreeAIError, render.RenderError, ff.FFError, HTTPError)
            msg = (e.detail if isinstance(e, HTTPError) else str(e)) if isinstance(e, known) else \
                f"Something went wrong: {e}"
            update_job(jid, state="failed", error=msg, stage="Failed")
            if on_fail:
                on_fail(msg)
        finally:
            ff.set_cancel_event(None)

    worker.submit(run)


def busy() -> dict:
    """For the phone's notification while something is being made."""
    with jobs_lock:
        active = [j for j in jobs.values() if j["state"] in ("working", "waiting")]
        active.sort(key=lambda j: (j["state"] != "working", j["created"]))
        if not active:
            return {"working": False}
        j = active[0]
        return {"working": True, "stage": j["stage"], "progress": j["progress"], "eta": j["eta"],
                "waiting": len(active) - 1}


# ================================================================ sources

live_local: set[str] = set()  # sources the phone handed over in this run of the app


def _source_meta(sid: str) -> dict | None:
    try:
        return json.loads((SOURCES / sid / "meta.json").read_text())
    except (FileNotFoundError, ValueError):
        return None


def _write_meta(meta: dict) -> None:
    d = SOURCES / meta["id"]
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / "meta.tmp"
    tmp.write_text(json.dumps(meta))
    os.replace(tmp, d / "meta.json")


_avail: dict[str, tuple[float, bool]] = {}


def source_available(meta: dict) -> bool:
    path = meta["path"]
    if path.startswith("/saf/"):  # a file picked on the phone: ask Android (remembered for a minute)
        hit = _avail.get(path)
        if hit and time.time() - hit[0] < 60:
            return hit[1]
        ok = ff.exists(path)
        _avail[path] = (time.time(), ok)
        return ok
    if meta.get("local"):
        return meta["id"] in live_local and os.path.exists(path)
    return os.path.exists(path)


def source_view(meta: dict) -> dict:
    return {"id": meta["id"], "name": meta["name"], "kind": meta.get("kind"), "duration": meta.get("duration", 0),
            "width": meta.get("width", 0), "height": meta.get("height", 0),
            "thumb": f"/api/sources/{meta['id']}/thumb" if meta.get("thumb") else None, "error": meta.get("error")}


def register_source(path: Path, name: str, local: bool, size: int | None = None) -> dict:
    """Looks at a new file: what it is, how long, a small picture."""
    name = Path(name or path.name).name[:120] or "file"
    if size is None or size < 0:
        size = path.stat().st_size if path.exists() else 0
    if local:  # the same file picked again after the app restarted: reconnect it to its projects
        for d in SOURCES.iterdir():
            meta = _source_meta(d.name)
            if meta and meta.get("local") and meta["name"] == name and meta.get("size") == size and size > 0:
                meta["path"] = str(path)
                _write_meta(meta)
                live_local.add(meta["id"])
                return meta
    sid = uuid.uuid4().hex[:12]
    d = SOURCES / sid
    d.mkdir(parents=True)
    meta = {"id": sid, "name": name, "path": str(path), "local": local, "size": size, "created": time.time(),
            "kind": None, "error": None, "thumb": False}
    info = ff.probe(path)
    if info is None:
        meta["error"] = f"{name}: this file type can't be opened"
    else:
        meta["probe"] = {"streams": info.get("streams", []), "format": {"duration": (info.get("format") or {}).get(
            "duration")}}
        try:
            m = analyze.probe(path, 0, info=info)
            meta.update(kind=m.kind, duration=round(m.duration, 2), width=m.width, height=m.height,
                        has_audio=m.has_audio)
            if m.kind == "video" and m.duration > MAX_MINUTES * 60 + 5:
                meta["error"] = (f"{name} is {round(m.duration / 60)} minutes long; "
                                 f"videos up to {MAX_MINUTES} minutes can be edited")
        except analyze.MediaError:
            streams = info.get("streams", [])
            dur = float((info.get("format") or {}).get("duration") or 0)
            if any(s.get("codec_type") == "audio" for s in streams) and dur > 0:
                meta.update(kind="audio", duration=round(dur, 2))
            else:
                meta["error"] = f"{name}: no picture or sound found in this file"
    if meta["kind"] in ("video", "image") and not meta["error"]:
        at = min(1.0, (meta.get("duration") or 0) / 3) if meta["kind"] == "video" else 0
        meta["thumb"] = render.thumbnail(path, d / "thumb.jpg", at=at, width=320)
    if local:
        live_local.add(sid)
    _write_meta(meta)
    return meta


def _project_source(meta: dict) -> dict:
    return {"id": meta["id"], "name": meta["name"], "path": meta["path"], "dir": str(SOURCES / meta["id"]),
            "probe": meta.get("probe"), "kind": meta.get("kind")}


# ================================================================ projects

_plocks: dict[str, threading.Lock] = {}


def plock(pid: str) -> threading.Lock:
    with jobs_lock:
        return _plocks.setdefault(pid, threading.Lock())


def load_project(pid: str) -> dict:
    if not re.fullmatch(r"[0-9a-f]{12}", pid or ""):
        raise HTTPError(404, "This video was not found.")
    try:
        return json.loads((PROJECTS / pid / "project.json").read_text())
    except (FileNotFoundError, ValueError):
        raise HTTPError(404, "This video was not found.") from None


def save_project(state: dict) -> None:
    state["updated"] = time.time()
    d = PROJECTS / state["id"]
    tmp = d / "project.tmp"
    tmp.write_text(json.dumps(state, ensure_ascii=False))
    os.replace(tmp, d / "project.json")


def _version(state: dict, n: int | None = None) -> dict | None:
    n = n if n is not None else state.get("current")
    return next((v for v in state.get("versions", []) if v["n"] == n), None)


def sources_ok(state: dict) -> bool:
    for s in state["sources"]:
        meta = _source_meta(s["id"])
        if not meta or not source_available(meta):
            return False
    return True


def suggestions(state: dict) -> list[str]:
    v = _version(state)
    if not v:
        return []
    p = v["plan"]
    out = []
    if v["settings"].get("captions") != "on":
        out.append("Add captions")
    if p.get("mode") == "cleanup":
        out.append("Remove more pauses")
    else:
        out.append("Make it shorter")
    if not p.get("title"):
        out.append('Add the title "My video"')
    out.append("Make it cinematic" if p.get("look") != "cinematic" else "Make it black and white")
    if len(p.get("clips", [])) > 2:
        out.append("Remove clip 2")
    if p.get("music"):
        out.append("Music quieter")
    if state.get("current", 0) > 1:
        out.append("Undo")
    return out[:6]


def project_view(state: dict, full: bool = True) -> dict:
    pid = state["id"]
    v = _version(state)
    running = state.get("job") if state.get("job") in jobs and jobs[state["job"]]["state"] in (
        "waiting", "working") else None
    has_file = bool(v and v.get("file") and (PROJECTS / pid / v["file"]).exists())
    out = {"id": pid, "title": state["title"], "mode": state["mode"], "created": state["created"],
           "updated": state["updated"], "state": "working" if running else state.get("state", "ready"),
           "error": state.get("error"), "job": running, "version": state.get("current", 0),
           "duration": v["duration"] if v else 0, "format": v["plan"]["format"] if v else None,
           "thumb": f"/api/projects/{pid}/thumb?v={v['n']}" if v and (PROJECTS / pid / f"thumb_v{v['n']}.jpg").exists()
           else None}
    if not full:
        return out
    out.update({
        "versions": [{"n": x["n"], "label": x["label"], "created": x["created"], "duration": x["duration"],
                      "available": bool(x.get("file"))} for x in state.get("versions", [])],
        "video": f"/api/projects/{pid}/video?v={v['n']}" if has_file else None,
        "download": f"/api/projects/{pid}/download?v={v['n']}" if has_file else None,
        "sources_available": sources_ok(state),
        "chat": state.get("chat", []),
        "suggestions": suggestions(state),
        "skipped": state.get("skipped", []),
        "plan": None,
    })
    if v:
        p = v["plan"]
        plan = Plan.from_dict(p)
        starts = render.clip_starts(plan)
        out["plan"] = {
            "format": p["format"], "width": p["width"], "height": p["height"], "total": v["duration"],
            "look": p["look"], "transition_kind": p["transition_kind"], "title": p.get("title", ""),
            "music": {"name": p["music"]["name"]} if p.get("music") else None, "notes": p.get("notes", []),
            "mode": p.get("mode", "highlight"),
            "clips": [{"index": i, "name": c["name"], "kind": c["kind"], "start": c["start"],
                       "duration": c["duration"], "speed": c.get("speed", 1.0), "zoom": c.get("zoom", 1.0),
                       "out_start": starts[i], "thumb": f"/api/projects/{pid}/clips/{i}/thumb?v={v['n']}"}
                      for i, c in enumerate(p["clips"])],
            "texts": [{"index": i, **t} for i, t in enumerate(p.get("texts", []))],
            "captions": v["settings"].get("captions") == "on",
            "encoder": v.get("encoder"), "render_seconds": v.get("render_seconds"),
        }
    return out


def media_for(state: dict, report=None) -> dict:
    srcs = []
    for s in state["sources"]:
        meta = _source_meta(s["id"])
        if not meta or not source_available(meta):
            raise HTTPError(409, "The original files aren't available any more (the app was restarted). "
                                 "Add the same files again from the New edit screen, then try again.")
        srcs.append({**s, "path": meta["path"]})
    media_list, _ = pipeline.load_media(srcs, report or (lambda *_: None), 0.0, 0.05)
    return {m.index: m for m in media_list}


def _chat_add(state: dict, role: str, text: str, version: int | None = None) -> None:
    state.setdefault("chat", []).append({"role": role, "text": text, "time": time.time(), "version": version})


def _first_message(state: dict, ver: dict) -> str:
    p = ver["plan"]
    n = len(p["clips"])
    files = len(state["sources"])
    length = ver["duration"]
    mmss = f"{int(length // 60)}:{int(length % 60):02d}"
    if p.get("mode") == "cleanup":
        lead = f"I cleaned up your video: it's now {mmss} long, in {n} part{'s' if n != 1 else ''}."
    else:
        lead = f"I made a {mmss} video from {files} file{'s' if files != 1 else ''}, using {n} of the best moments."
    notes = [x for x in p.get("notes", []) if not x.startswith(("Shape", "Length chosen", "Editing pace"))]
    extra = (" " + "; ".join(notes[:4]) + ".") if notes else ""
    return lead + extra + " Tell me anything you'd like to change, the way you'd tell a video editor."


# ================================================================ handlers

def _options(raw: dict, mode: str) -> dict:
    o = {k: str(raw.get(k, "auto") if raw.get(k) not in (None, "") else "auto") for k in
         ("format", "style", "length", "look", "transitions", "slowmo", "captions", "caption_lang", "caption_pos")}
    o["title"] = str(raw.get("title") or "")[:80]
    o["caption_style"] = str(raw.get("caption_style") or captions.DEFAULT_STYLE)
    if o["format"] not in ("auto", "original", *FORMATS):
        raise HTTPError(400, "Unknown video shape.")
    if o["style"] not in ("auto", "smooth", "fast"):
        raise HTTPError(400, "Unknown editing pace.")
    if o["look"] not in ("auto", *LOOK_NAMES):
        raise HTTPError(400, "Unknown look.")
    if o["transitions"] not in ("auto", "soft", "dynamic", "cinematic", "cuts"):
        raise HTTPError(400, "Unknown transition style.")
    if o["slowmo"] not in ("auto", "on", "off"):
        raise HTTPError(400, "Unknown slow motion setting.")
    if o["length"] != "auto":
        try:
            if not 5 <= float(o["length"]) <= MAX_MINUTES * 60:
                raise ValueError
        except ValueError:
            raise HTTPError(400, f"Length must be between 5 seconds and {MAX_MINUTES} minutes.") from None
    if o["captions"] not in ("auto", "on", "off") or o["caption_lang"] not in captions.LANGS \
            or o["caption_style"] not in captions.STYLES or o["caption_pos"] not in ("auto", *captions.POSITIONS):
        raise HTTPError(400, "Unknown caption setting.")
    if o["caption_pos"] == "auto":
        o["caption_pos"] = "bottom"
    o["quality"] = quality()
    return o


def _understand(request: str, report) -> tuple[dict, list[str]]:
    """Typed wishes → options. The free AI when reachable, else the built-in phrase list."""
    if not request.strip():
        return {}, []
    key, models = ai()
    report("Reading your request", 0.0)
    try:
        return intent.clean(genai.interpret(key, models["text"], request)), []
    except genai.AIError as e:
        return intent.clean(intent.parse_keywords(request)), [
            f"The on-phone AI couldn't answer ({e}); used the built-in understanding instead"]


def create_project(body: dict) -> dict:
    ids = body.get("sources") or []
    if not ids:
        raise HTTPError(400, "Please add at least one video or photo.")
    if len(ids) > MAX_FILES:
        raise HTTPError(400, f"Please add at most {MAX_FILES} files at once.")
    mode = body.get("mode") or "highlight"
    if mode not in ("highlight", "cleanup"):
        raise HTTPError(400, "Unknown editing mode.")
    metas = []
    for sid in ids:
        meta = _source_meta(str(sid))
        if not meta:
            raise HTTPError(400, "One of the files is missing. Please add it again.")
        if meta.get("error"):
            continue
        if meta.get("kind") not in ("video", "image"):
            continue
        if not source_available(meta):
            raise HTTPError(409, f"{meta['name']} isn't available any more. Please add it again.")
        metas.append(meta)
    if not metas:
        raise HTTPError(400, "None of the added files are videos or photos that can be edited.")
    music_meta = None
    if body.get("music"):
        music_meta = _source_meta(str(body["music"]))
        if not music_meta or music_meta.get("error") or not source_available(music_meta):
            raise HTTPError(400, "The music file couldn't be used. Please add it again.")
    options = _options(body.get("options") or {}, mode)
    request = str(body.get("request") or "")[:1000]
    pid = uuid.uuid4().hex[:12]
    (PROJECTS / pid).mkdir(parents=True)
    title = options["title"] or Path(metas[0]["name"]).stem[:40] or "My video"
    state = {"id": pid, "title": title, "mode": mode, "created": time.time(), "updated": time.time(),
             "state": "working", "error": None, "sources": [_project_source(m) for m in metas],
             "music": {"id": music_meta["id"], "name": music_meta["name"], "path": music_meta["path"]}
             if music_meta else None, "options": options, "request": request,
             "settings": {"captions": "off", "caption_lang": options["caption_lang"],
                          "caption_style": options["caption_style"], "caption_pos": options["caption_pos"]},
             "version": 0, "current": 0, "versions": [], "chat": [], "transcript": None, "scenes": {}}
    if request.strip():
        _chat_add(state, "user", request)
    jid = new_job("edit", pid)
    state["job"] = jid
    save_project(state)

    def work(report):
        with plock(pid):
            st = load_project(pid)
            typed, notes = _understand(request, report)
            opts = {**st["options"], **typed}
            has_speech = any((_source_meta(s["id"]) or {}).get("has_audio") for s in st["sources"])
            cap = opts.get("captions", "auto")
            st["settings"]["captions"] = "on" if cap == "on" or (cap == "auto" and has_speech) else "off"
            if typed.get("caption_lang"):
                st["settings"]["caption_lang"] = typed["caption_lang"]
            st["options"] = opts
            if typed.get("title"):
                st["title"] = typed["title"]
            ver, _ = pipeline.first_edit(st, PROJECTS / pid, report, ai(), speed(), opts)
            st["current"] = ver["n"]
            ver["plan"]["notes"] = notes + ver["plan"]["notes"]
            st["state"], st["error"] = "ready", None
            _chat_add(st, "assistant", _first_message(st, ver), ver["n"])
            save_project(st)
            return {"project": pid, "version": ver["n"]}

    submit(jid, work, on_fail=lambda msg: _mark_failed(pid, msg))
    return {"project": pid, "job": jid}


def _mark_failed(pid: str, msg: str) -> None:
    with plock(pid):
        try:
            st = load_project(pid)
        except HTTPError:
            return
        if not st.get("versions"):
            st["state"], st["error"] = "failed", msg
        else:
            _chat_add(st, "assistant", "That didn't work: " + msg if msg != "Cancelled" else "Stopped.", None)
        st["job"] = None
        save_project(st)


def _busy_project(state: dict) -> None:
    j = state.get("job")
    if j and j in jobs and jobs[j]["state"] in ("waiting", "working"):
        raise HTTPError(409, "Still working on the last change. Please wait for it to finish.")


def project_chat(pid: str, message: str) -> dict:
    message = (message or "").strip()[:2000]
    if not message:
        raise HTTPError(400, "Please type what you'd like to change.")
    with plock(pid):
        state = load_project(pid)
        _busy_project(state)
        if not state.get("versions"):
            raise HTTPError(409, "This video isn't ready yet.")
        _chat_add(state, "user", message)
        jid = new_job("chat", pid)
        state["job"] = jid
        save_project(state)

    def work(report):
        with plock(pid):
            st = load_project(pid)
            cur = _version(st)
            plan = Plan.from_dict(cur["plan"])
            st["settings"] = dict(cur["settings"])
            try:
                media = media_for(st, report) if sources_ok(st) else None
            except HTTPError:
                media = None
            if media is None:
                # Questions can still be answered; changes need the files.
                reply = ("I need the original files to change this video, and they aren't available any more "
                         "because the app was restarted. Add the same files again from the New edit screen "
                         "(they'll reconnect to this video), then ask me again.")
                _chat_add(st, "assistant", reply)
                st["job"] = None
                save_project(st)
                return {"project": pid, "reply": reply}
            reply, ver, undo = pipeline.chat(st, plan, media, message, ai(), PROJECTS / pid, report, speed(),
                                             st["options"])
            if undo:
                reply2, ver = _undo(st, media, report)
                reply = reply2
            if ver:
                st["current"] = ver["n"]
            _chat_add(st, "assistant", reply, ver["n"] if ver else None)
            st["job"] = None
            save_project(st)
            return {"project": pid, "reply": reply, "version": ver["n"] if ver else None}

    submit(jid, work, on_fail=lambda msg: _mark_failed(pid, msg))
    return {"job": jid}


def _undo(st: dict, media: dict | None, report) -> tuple[str, dict | None]:
    cur = st.get("current", 0)
    prev = [v for v in st.get("versions", []) if v["n"] < cur]
    if not prev:
        return "This is already the first version.", None
    return _restore(st, prev[-1]["n"], media, report)


def _restore(st: dict, n: int, media: dict | None, report) -> tuple[str, dict | None]:
    v = _version(st, n)
    if not v:
        return "That version doesn't exist.", None
    if v.get("file") and (PROJECTS / st["id"] / v["file"]).exists():
        st["settings"] = dict(v["settings"])
        return f"Back to version {n}.", v
    if media is None:
        media = media_for(st, report)
    st["settings"] = dict(v["settings"])
    ver = pipeline.render_version(st, Plan.from_dict(v["plan"]), media, PROJECTS / st["id"], report, ai(),
                                  speed(), f"Back to version {n}")
    return f"Back to version {n} (made again as version {ver['n']}).", ver


def project_restore(pid: str, n: int | None) -> dict:
    with plock(pid):
        state = load_project(pid)
        _busy_project(state)
        jid = new_job("chat", pid)
        state["job"] = jid
        save_project(state)

    def work(report):
        with plock(pid):
            st = load_project(pid)
            if n is None:
                reply, ver = _undo(st, None, report)
            else:
                reply, ver = _restore(st, n, None, report)
            if ver:
                st["current"] = ver["n"]
            _chat_add(st, "assistant", reply, ver["n"] if ver else None)
            st["job"] = None
            save_project(st)
            return {"project": pid, "reply": reply}

    submit(jid, work, on_fail=lambda msg: _mark_failed(pid, msg))
    return {"job": jid}


CLIP_ACTIONS = {"remove", "move", "slowmo", "normal_speed", "zoom"}


def project_clip_action(pid: str, body: dict) -> dict:
    action = body.get("action")
    if action not in CLIP_ACTIONS:
        raise HTTPError(400, "Unknown clip action.")
    with plock(pid):
        state = load_project(pid)
        _busy_project(state)
        cur = _version(state)
        if not cur:
            raise HTTPError(409, "This video isn't ready yet.")
        n = len(cur["plan"]["clips"])
        try:
            clip = int(body.get("clip"))
        except (TypeError, ValueError):
            raise HTTPError(400, "Which clip?") from None
        if not 0 <= clip < n:
            raise HTTPError(400, "That clip doesn't exist.")
        c = cur["plan"]["clips"][clip]
        if action == "remove":
            op = {"op": "remove_clips", "clips": [clip + 1]}
            say = f"Remove clip {clip + 1}"
        elif action == "move":
            to = int(body.get("to", clip)) + 1
            op = {"op": "move_clip", "clip": clip + 1, "to": max(1, min(n, to))}
            say = f"Move clip {clip + 1} to position {max(1, min(n, to))}"
        elif action == "slowmo":
            op = {"op": "speed", "clips": [clip + 1], "number": 0.5}
            say = f"Slow motion on clip {clip + 1}"
        elif action == "normal_speed":
            op = {"op": "speed", "clips": [clip + 1], "number": 1.0}
            say = f"Normal speed on clip {clip + 1}"
        else:
            z = 1.0 if c.get("zoom", 1.0) > 1.001 else 1.25
            op = {"op": "zoom", "clips": [clip + 1], "number": z}
            say = f"{'Zoom in on' if z > 1 else 'Remove the zoom from'} clip {clip + 1}"
        _chat_add(state, "user", say)
        jid = new_job("chat", pid)
        state["job"] = jid
        save_project(state)

    def work(report):
        with plock(pid):
            st = load_project(pid)
            cur = _version(st)
            media = media_for(st, report)
            res = edits.apply([op], Plan.from_dict(cur["plan"]), media, dict(cur["settings"]))
            ver = None
            if res.changed:
                st["settings"] = res.settings
                ver = pipeline.render_version(st, res.plan, media, PROJECTS / pid, report, ai(), speed(), say, lo=0.05)
                st["current"] = ver["n"]
            _chat_add(st, "assistant", edits.summary(res), ver["n"] if ver else None)
            st["job"] = None
            save_project(st)
            return {"project": pid}

    submit(jid, work, on_fail=lambda msg: _mark_failed(pid, msg))
    return {"job": jid}


# ---------------------------------------------------------------- AI creation

def create_ai(body: dict) -> dict:
    mode = body.get("mode")
    prompt = str(body.get("prompt") or "").strip()[:1000]
    fmt = body.get("format") or "9:16"
    source = body.get("source") or "pictures"
    request = str(body.get("request") or "")[:1000]
    try:
        count = int(body.get("count") or 1)
    except (TypeError, ValueError):
        count = 1
    if mode not in ("image", "video", "story"):
        raise HTTPError(400, "Unknown creation type.")
    if not prompt:
        raise HTTPError(400, "Please describe what you want to create.")
    if fmt not in FORMATS:
        raise HTTPError(400, "Unknown shape.")
    key, models = ai()
    need = "video" if mode == "video" or (mode == "story" and source == "clips") else "image"
    count = max(1, min(count, 3 if need == "video" else 8))
    jid = new_job(mode)
    work_dir = CREATIONS / jid
    work_dir.mkdir(parents=True)
    share = 0.6 if mode == "story" else 1.0

    def clip(i, p, out, prog):
        pic = freeai.generate_image(p, fmt, work_dir / f"ai_still_{i + 1}")
        prog(0.7)
        try:
            return freeai.motion_clip(pic, fmt, out, freeai.MOVES[i % len(freeai.MOVES)], quality=quality(),
                                      speed=speed(), progress=lambda x: prog(0.7 + 0.3 * x))
        finally:
            pic.unlink(missing_ok=True)

    def ideas(n):
        if n == 1:
            return [prompt]
        shots = genai.shot_ideas(key, models["text"], prompt, n)
        return shots if len(set(shots)) > 1 else freeai.shot_prompts(prompt, n)

    def make(report, prompts):
        files = []
        for i, p in enumerate(prompts):
            p = genai.enhance_image_prompt(key, models["text"], p)  # ChatGPT-style rich prompt for a better picture
            label = f"Creating {'video clip' if need == 'video' else 'picture'} {i + 1} of {len(prompts)}"
            base = i / len(prompts)
            report(label, base * share)
            if need == "video":
                f = clip(i, p, work_dir / f"ai_clip_{i + 1}.mp4",
                         lambda x, b=base, lb=label: report(lb, (b + x / len(prompts)) * share))
            else:
                f = freeai.generate_image(p, fmt, work_dir / f"ai_picture_{i + 1}")
            files.append(f)
        return files

    def as_sources(files):
        out = []
        for f in files:
            meta = register_source(f, f.name, local=False)
            out.append({"name": f.name, "kind": "video" if f.suffix == ".mp4" else "image",
                        "url": f"/api/sources/{meta['id']}/file", "source": meta["id"],
                        "thumb": f"/api/sources/{meta['id']}/thumb"})
        return out

    def work(report):
        if mode in ("image", "video"):
            prompts = ideas(count)
            return {"files": as_sources(make(report, prompts)), "project": None}
        report("Planning the shots", 0.0)
        shots = ideas(count)
        files = as_sources(make(report, shots))
        res = create_project({"sources": [f["source"] for f in files], "mode": "highlight", "request": request,
                              "options": {"format": fmt, "captions": "off", "title": ""}})
        return {"files": files, "project": res["project"], "edit_job": res["job"], "shots": shots}

    submit(jid, work)
    return {"job": jid}


# ---------------------------------------------------------------- caption previews

PREVIEW_TEXT = {"en": "This is my best day ever", "hi": "यह मेरा सबसे अच्छा दिन है",
                "pa": "ਇਹ ਮੇਰਾ ਸਭ ਤੋਂ ਵਧੀਆ ਦਿਨ ਹੈ", "hinglish": "Yeh mera best din hai", "auto": "This is my best day ever"}


def caption_preview(style_id: str, lang: str) -> Path:
    if style_id not in captions.STYLES or lang not in PREVIEW_TEXT:
        raise HTTPError(404, "Not found")
    cache = DATA_ROOT / "previews"
    cache.mkdir(exist_ok=True)
    png = cache / f"{style_id}_{lang}.png"
    if not png.exists():
        ass = cache / f"{style_id}_{lang}.ass"
        ass.write_text(captions.build_ass([{"start": 0, "end": 2, "text": PREVIEW_TEXT[lang]}], style_id,
                                          1080, 1080, "middle"), encoding="utf-8")
        ff.run_quiet(["-f", "lavfi", "-i", "gradients=s=1080x1080:c0=0x3b4a6b:c1=0x7a5c3e:d=2", "-vf",
                      f"subtitles=filename='{render._esc(ass)}':fontsdir='{render._esc(captions.FONTS_DIR)}',"
                      "crop=1080:360:0:360,scale=480:-2", "-ss", "1.2", "-frames:v", "1", str(png)])
        ass.unlink(missing_ok=True)
    if not png.exists():
        raise HTTPError(500, "Preview failed")
    return png


# ================================================================ HTTP plumbing

def _auth_token() -> str:
    return hmac.new(PASSWORD.encode(), b"video-editor", hashlib.sha256).hexdigest()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "AIEditor/2"

    def log_message(self, fmt, *args):  # keep the phone's log quiet
        if os.environ.get("EDITOR_LOG") == "1":
            super().log_message(fmt, *args)

    # ---------- helpers
    def cookies(self) -> dict:
        out = {}
        for part in (self.headers.get("Cookie") or "").split(";"):
            if "=" in part:
                k, v = part.strip().split("=", 1)
                out[k] = v
        return out

    def send_json(self, obj, status: int = 200, headers: dict | None = None) -> None:
        data = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def send_file(self, path: Path, mime: str | None = None, download: str | None = None,
                  cache: str = "no-cache") -> None:
        try:
            size = path.stat().st_size
            f = open(path, "rb")
        except OSError:
            raise HTTPError(404, "File not found.") from None
        with f:
            mime = mime or mimetypes.guess_type(str(path))[0] or "application/octet-stream"
            start, end = 0, size - 1
            rng = self.headers.get("Range")
            status = 200
            if rng:
                m = re.match(r"bytes=(\d*)-(\d*)", rng)
                if m and (m.group(1) or m.group(2)):
                    if m.group(1):
                        start = int(m.group(1))
                        end = int(m.group(2)) if m.group(2) else size - 1
                    else:
                        start = max(0, size - int(m.group(2)))
                    end = min(end, size - 1)
                    if start > end:
                        self.send_response(416)
                        self.send_header("Content-Range", f"bytes */{size}")
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
                    status = 206
            self.send_response(status)
            self.send_header("Content-Type", mime)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(end - start + 1))
            self.send_header("Cache-Control", cache)
            if status == 206:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            if download:
                q = urllib.parse.quote(download)
                self.send_header("Content-Disposition", f"attachment; filename=\"{download}\"; filename*=UTF-8''{q}")
            self.end_headers()
            if self.command == "HEAD":
                return
            f.seek(start)
            left = end - start + 1
            try:
                while left > 0:
                    chunk = f.read(min(1 << 20, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def body_json(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        ctype = self.headers.get("Content-Type") or ""
        if "application/x-www-form-urlencoded" in ctype:
            return {k: v[-1] for k, v in urllib.parse.parse_qs(raw.decode()).items()}
        if not raw:
            return {}
        try:
            out = json.loads(raw)
            return out if isinstance(out, dict) else {}
        except ValueError:
            raise HTTPError(400, "Bad request.") from None

    def read_multipart(self, dest: Path) -> list[tuple[str, Path, str]]:
        """Streams uploaded files to disk. Returns [(field, path, original name)]."""
        ctype = self.headers.get("Content-Type") or ""
        m = re.search(r"boundary=\"?([^\";]+)\"?", ctype)
        if "multipart/form-data" not in ctype or not m:
            raise HTTPError(400, "Expected files.")
        boundary = b"--" + m.group(1).encode()
        left = int(self.headers.get("Content-Length") or 0)
        buf = b""

        def more() -> bool:
            nonlocal buf, left
            if left <= 0:
                return False
            chunk = self.rfile.read(min(1 << 20, left))
            if not chunk:
                left = 0
                return False
            left -= len(chunk)
            buf += chunk
            return True

        files = []
        while boundary not in buf and more():
            pass
        if boundary not in buf:
            raise HTTPError(400, "Upload was cut off.")
        buf = buf[buf.index(boundary) + len(boundary):]
        i = 0
        while True:
            while len(buf) < 2 and more():
                pass
            if buf.startswith(b"--"):
                break
            while b"\r\n\r\n" not in buf:
                if not more():
                    raise HTTPError(400, "Upload was cut off.")
            head, buf = buf.split(b"\r\n\r\n", 1)
            head_s = head.decode("utf-8", "replace")
            name_m = re.search(r'name="([^"]*)"', head_s)
            file_m = re.search(r'filename="([^"]*)"', head_s)
            field = name_m.group(1) if name_m else ""
            sep = b"\r\n" + boundary
            if file_m and file_m.group(1):
                orig = Path(file_m.group(1).replace("\\", "/")).name or f"file{i}"
                path = dest / f"{i:02d}_{re.sub(r'[^A-Za-z0-9._-]+', '_', orig)[-80:]}"
                i += 1
                with open(path, "wb") as out:
                    while True:
                        k = buf.find(sep)
                        if k >= 0:
                            out.write(buf[:k])
                            buf = buf[k + len(sep):]
                            break
                        keep = len(sep)
                        if len(buf) > keep:
                            out.write(buf[:-keep])
                            buf = buf[-keep:]
                        if not more():
                            raise HTTPError(400, "Upload was cut off.")
                files.append((field, path, orig))
            else:
                while sep not in buf:
                    if not more():
                        raise HTTPError(400, "Upload was cut off.")
                k = buf.index(sep)
                buf = buf[k + len(sep):]
        while more():
            pass
        return files

    # ---------- dispatch
    def do_GET(self):
        self.dispatch("GET")

    def do_HEAD(self):
        self.dispatch("GET")

    def do_POST(self):
        self.dispatch("POST")

    def do_DELETE(self):
        self.dispatch("DELETE")

    def dispatch(self, method: str) -> None:
        url = urllib.parse.urlsplit(self.path)
        path = urllib.parse.unquote(url.path)
        query = {k: v[-1] for k, v in urllib.parse.parse_qs(url.query).items()}
        try:
            if path.startswith("/api/"):
                self.check_access(path)
                for m_, rx, fn in ROUTES:
                    if m_ != method:
                        continue
                    mm = re.fullmatch(rx, path)
                    if mm:
                        out = fn(self, query, *mm.groups())
                        if out is not None:
                            self.send_json(out)
                        return
                raise HTTPError(404, "Not found.")
            if method != "GET":
                raise HTTPError(405, "Not allowed.")
            self.serve_static(path)
        except HTTPError as e:
            if self.headers.get("Content-Length") and method == "POST" and not getattr(self, "_drained", False):
                self.close_connection = True
            self.send_json({"detail": e.detail, **e.extra}, e.status)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            try:
                self.send_json({"detail": f"Something went wrong: {e}"}, 500)
            except OSError:
                pass

    def check_access(self, path: str) -> None:
        if path == "/api/health":
            return
        if APP_TOKEN and not hmac.compare_digest(self.cookies().get("editor_token", ""), APP_TOKEN):
            raise HTTPError(403, "Not allowed.")
        if PASSWORD and path != "/api/login":
            if not hmac.compare_digest(self.cookies().get("editor_auth", ""), _auth_token()):
                raise HTTPError(401, "Please enter the access code.", login=True)

    def serve_static(self, path: str) -> None:
        rel = path.lstrip("/") or "index.html"
        target = (STATIC / rel).resolve()
        if not str(target).startswith(str(STATIC.resolve())) or not target.is_file():
            target = STATIC / "index.html"
        self.send_file(target, cache="no-cache")


# ================================================================ routes

def r_health(h, q):
    return {"ok": True, "app_mode": APP_MODE, "encoder": "hardware" if (APP_MODE and speed() == "fast") else
            ff.describe_encoder(), "password": bool(PASSWORD), "version": VERSION}


def r_login(h, q):
    code = str(h.body_json().get("code", "")).strip()
    if not PASSWORD:
        return {"ok": True}
    if not hmac.compare_digest(code, PASSWORD):
        raise HTTPError(401, "That access code is wrong.")
    h.send_json({"ok": True}, headers={"Set-Cookie": f"editor_auth={_auth_token()}; Max-Age=31536000; Path=/; "
                                                     "HttpOnly; SameSite=Lax"})


def r_settings_get(h, q):
    return settings_view()


def r_settings_post(h, q):
    body = h.body_json()
    s = settings()
    if "quality" in body:
        if str(body["quality"]) not in ("720", "1080"):
            raise HTTPError(400, "Unknown quality.")
        s["quality"] = str(body["quality"])
    if "speed" in body:
        if body["speed"] not in ("fast", "best"):
            raise HTTPError(400, "Unknown speed setting.")
        s["speed"] = body["speed"]
    save_settings(s)
    return settings_view()


def r_sources_upload(h, q):
    tmp = DATA_ROOT / "uploads" / uuid.uuid4().hex[:12]
    tmp.mkdir(parents=True)
    try:
        files = h.read_multipart(tmp)
        h._drained = True
        if not files:
            raise HTTPError(400, "Please add at least one file.")
        if len(files) > MAX_FILES:
            raise HTTPError(400, f"Please add at most {MAX_FILES} files at once.")
        out = []
        for _, path, orig in files:
            sid = uuid.uuid4().hex[:12]
            d = SOURCES / sid
            d.mkdir(parents=True)
            final = d / ("file" + Path(orig).suffix.lower()[:8])
            os.replace(path, final)
            meta = register_source(final, orig, local=False)
            out.append(source_view(meta))
        return {"sources": out}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def r_sources_local(h, q):
    if not APP_MODE and os.environ.get("EDITOR_ALLOW_LOCAL") != "1":
        raise HTTPError(403, "Only the phone app can add files this way.")
    items = h.body_json().get("items") or []
    if not items or len(items) > MAX_FILES:
        raise HTTPError(400, f"Please pick between 1 and {MAX_FILES} files.")
    out = []
    for it in items:
        p = Path(str(it.get("path") or ""))
        if not ff.exists(p):
            out.append({"id": None, "name": it.get("name"), "error": f"{it.get('name')}: the phone didn't share "
                                                                      "this file", "thumb": None, "kind": None})
            continue
        try:
            size = int(it.get("size"))
        except (TypeError, ValueError):
            size = None
        meta = register_source(p, str(it.get("name") or p.name), local=True, size=size)
        out.append(source_view(meta))
    return {"sources": out}


def r_source_thumb(h, q, sid):
    meta = _source_meta(sid)
    if not meta or not meta.get("thumb"):
        raise HTTPError(404, "No picture.")
    h.send_file(SOURCES / sid / "thumb.jpg", "image/jpeg", cache="max-age=86400")


def r_source_file(h, q, sid):
    meta = _source_meta(sid)
    if not meta or meta.get("local") or not source_available(meta):
        raise HTTPError(404, "File not found.")
    h.send_file(Path(meta["path"]), download=meta["name"] if q.get("download") else None)


def r_projects(h, q):
    out = []
    for d in PROJECTS.iterdir():
        try:
            out.append(project_view(json.loads((d / "project.json").read_text()), full=False))
        except (FileNotFoundError, ValueError, KeyError):
            continue
    out.sort(key=lambda p: -p["updated"])
    return {"projects": out}


def r_project_create(h, q):
    return create_project(h.body_json())


def r_project(h, q, pid):
    return project_view(load_project(pid))


def r_project_delete(h, q, pid):
    with plock(pid):
        state = load_project(pid)
        j = state.get("job")
        if j in cancels:
            cancels[j].set()
        shutil.rmtree(PROJECTS / pid, ignore_errors=True)
    return {"ok": True}


def r_project_chat(h, q, pid):
    return project_chat(pid, str(h.body_json().get("message") or ""))


def r_project_clips(h, q, pid):
    return project_clip_action(pid, h.body_json())


def r_project_undo(h, q, pid):
    return project_restore(pid, None)


def r_project_restore(h, q, pid):
    try:
        n = int(h.body_json().get("version"))
    except (TypeError, ValueError):
        raise HTTPError(400, "Which version?") from None
    return project_restore(pid, n)


def _version_file(pid: str, q: dict) -> tuple[dict, dict, Path]:
    state = load_project(pid)
    try:
        n = int(q.get("v") or state.get("current") or 0)
    except ValueError:
        n = state.get("current") or 0
    v = _version(state, n)
    if not v or not v.get("file") or not (PROJECTS / pid / v["file"]).exists():
        raise HTTPError(404, "This version of the video isn't kept any more.")
    return state, v, PROJECTS / pid / v["file"]


def r_project_video(h, q, pid):
    _, _, path = _version_file(pid, q)
    h.send_file(path, "video/mp4", cache="max-age=3600")


def r_project_download(h, q, pid):
    state, v, path = _version_file(pid, q)
    safe = re.sub(r"[^\w\- ]+", "", state["title"]).strip()[:40] or "video"
    h.send_file(path, "video/mp4", download=f"{safe} v{v['n']}.mp4")


def r_project_thumb(h, q, pid):
    load_project(pid)
    n = q.get("v")
    p = PROJECTS / pid / f"thumb_v{int(n) if n and n.isdigit() else 0}.jpg"
    h.send_file(p, "image/jpeg", cache="max-age=86400")


def r_clip_thumb(h, q, pid, idx):
    state = load_project(pid)
    v = _version(state, int(q["v"]) if q.get("v", "").isdigit() else None)
    if not v:
        raise HTTPError(404, "No picture.")
    clips = v["plan"]["clips"]
    i = int(idx)
    if not 0 <= i < len(clips):
        raise HTTPError(404, "No picture.")
    c = clips[i]
    src = state["sources"][c["media_index"]] if c["media_index"] < len(state["sources"]) else None
    meta = _source_meta(src["id"]) if src else None
    at = c["start"] + min(0.5, c["duration"] / 2) if c["kind"] == "video" else 0
    tdir = PROJECTS / pid / "thumbs"
    tdir.mkdir(exist_ok=True)
    f = tdir / f"{src['id'] if src else 'x'}_{int(at * 10)}.jpg"
    if not f.exists():
        if c["kind"] == "image" and meta and meta.get("thumb"):
            shutil.copy(SOURCES / meta["id"] / "thumb.jpg", f)
        elif not meta or not source_available(meta) or not render.thumbnail(Path(meta["path"]), f, at=at, width=200):
            if meta and meta.get("thumb"):
                h.send_file(SOURCES / meta["id"] / "thumb.jpg", "image/jpeg")
                return None
            raise HTTPError(404, "No picture.")
    h.send_file(f, "image/jpeg", cache="max-age=86400")


def r_job(h, q, jid):
    job = jobs.get(jid)
    if not job:
        raise HTTPError(404, "This was not found. It may have finished long ago.")
    return {k: v for k, v in job.items() if k not in ("created", "started")}


def r_job_cancel(h, q, jid):
    if jid not in cancels:
        raise HTTPError(404, "Not found.")
    cancels[jid].set()
    return {"ok": True}


def r_create(h, q):
    return create_ai(h.body_json())


def r_caption_styles(h, q):
    return {"styles": [{"id": k, "name": v["name"], "anim": v["anim"]} for k, v in captions.STYLES.items()],
            "langs": captions.LANGS, "default": captions.DEFAULT_STYLE}


def r_caption_preview(h, q, style):
    h.send_file(caption_preview(style, q.get("lang", "en")), "image/png", cache="max-age=86400")


ROUTES = [
    ("GET", r"/api/health", r_health),
    ("POST", r"/api/login", r_login),
    ("GET", r"/api/settings", r_settings_get),
    ("POST", r"/api/settings", r_settings_post),
    ("POST", r"/api/sources", r_sources_upload),
    ("POST", r"/api/sources/local", r_sources_local),
    ("GET", r"/api/sources/([0-9a-f]{12})/thumb", r_source_thumb),
    ("GET", r"/api/sources/([0-9a-f]{12})/file", r_source_file),
    ("GET", r"/api/projects", r_projects),
    ("POST", r"/api/projects", r_project_create),
    ("GET", r"/api/projects/([0-9a-f]{12})", r_project),
    ("DELETE", r"/api/projects/([0-9a-f]{12})", r_project_delete),
    ("POST", r"/api/projects/([0-9a-f]{12})/chat", r_project_chat),
    ("POST", r"/api/projects/([0-9a-f]{12})/clips", r_project_clips),
    ("POST", r"/api/projects/([0-9a-f]{12})/undo", r_project_undo),
    ("POST", r"/api/projects/([0-9a-f]{12})/restore", r_project_restore),
    ("GET", r"/api/projects/([0-9a-f]{12})/video", r_project_video),
    ("GET", r"/api/projects/([0-9a-f]{12})/download", r_project_download),
    ("GET", r"/api/projects/([0-9a-f]{12})/thumb", r_project_thumb),
    ("GET", r"/api/projects/([0-9a-f]{12})/clips/(\d+)/thumb", r_clip_thumb),
    ("GET", r"/api/jobs/([0-9a-f]{12})", r_job),
    ("POST", r"/api/jobs/([0-9a-f]{12})/cancel", r_job_cancel),
    ("POST", r"/api/create", r_create),
    ("GET", r"/api/caption-styles", r_caption_styles),
    ("GET", r"/api/caption-preview/([a-z0-9-]+)", r_caption_preview),
]


def _recover() -> None:
    """After a restart: anything that was being made stopped, so say so."""
    for d in PROJECTS.iterdir():
        f = d / "project.json"
        try:
            st = json.loads(f.read_text())
        except (FileNotFoundError, ValueError):
            continue
        if st.get("job"):
            st["job"] = None
            if not st.get("versions"):
                st["state"], st["error"] = "failed", "The app was closed while this was being made. Please try again."
            save_project(st)
        for tmp in d.glob("tmp_*"):
            tmp.unlink(missing_ok=True)
    # Uploaded files that no video uses, after a day.
    used = set()
    for d in PROJECTS.iterdir():
        try:
            st = json.loads((d / "project.json").read_text())
            used |= {s["id"] for s in st.get("sources", [])} | ({st["music"]["id"]} if st.get("music") else set())
        except (FileNotFoundError, ValueError, KeyError, TypeError):
            continue
    for d in SOURCES.iterdir():
        meta = _source_meta(d.name)
        if meta and d.name not in used and time.time() - meta.get("created", 0) > 86400:
            shutil.rmtree(d, ignore_errors=True)
    shutil.rmtree(DATA_ROOT / "uploads", ignore_errors=True)


def serve(host: str = "127.0.0.1", port: int = 8000) -> ThreadingHTTPServer:
    _recover()
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    return httpd


if __name__ == "__main__":
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8000"))
    print(f"AI editor open source: http://{'localhost' if host == '0.0.0.0' else host}:{port}")
    serve(host, port).serve_forever()
