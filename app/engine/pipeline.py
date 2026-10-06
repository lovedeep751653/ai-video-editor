"""Runs edits from start to finish.

`first_edit` studies the files, makes the plan (highlight or clean-up), adds
captions and renders version 1. `render_version` renders any later plan (after
a chat edit). Captions are kept in the original files' time, so they stay right
when clips are cut, moved or removed later.

report(stage_text, fraction) is called as work happens; fractions only go up.
"""

from __future__ import annotations

import copy
import time
from pathlib import Path

import numpy as np

from . import analyze, captions, edits, ff, genai, music as music_mod, plan as planner, render
from .analyze import Media
from .plan import Plan

CHUNK = 240.0  # seconds of speech sent to Google AI at a time (keeps timing precise)


# ---------------------------------------------------------------- files

def loudness_gain(m: Media) -> float:
    """Volume change (dB) that brings speech to a comfortable, even level without clipping."""
    if not m.has_audio or m.speech.size < 20:
        return 0.0
    db = m.speech.astype(np.float64)
    if db.max() < -60:
        return 0.0
    loud = db[db >= np.percentile(db, 60)]
    level = 10 * np.log10(np.mean(10 ** (loud / 10)) + 1e-12)
    gain = float(np.clip(-18.0 - level, -6.0, 12.0))
    gain = min(gain, -5.0 - float(db.max()))
    return round(gain, 1)


def load_media(sources: list[dict], report, lo: float, hi: float) -> tuple[list[Media], list[str]]:
    """Opens and studies every source. Each source dict: {id, name, path, dir, probe?}.
    Measurements are saved next to the source, so a file is only studied once."""
    media, skipped = [], []
    for i, s in enumerate(sources):
        try:
            m = analyze.probe(Path(s["path"]), i, info=s.get("probe"))
        except analyze.MediaError as e:
            skipped.append(str(e).replace(Path(s["path"]).name, s["name"]))
            continue
        m.name = s["name"]
        m.key = s["id"]
        media.append(m)
    if not media:
        raise ValueError("None of the files could be opened. " + " ".join(skipped))
    weights = [max(m.duration, 0.5) if m.kind == "video" else 0.3 for m in media]
    total_w, done_w = sum(weights), 0.0
    ok = []
    for m, w in zip(media, weights):
        src = next(x for x in sources if x["id"] == m.key)
        cache = Path(src["dir"]) / "analysis.npz"
        label = f"Studying {'video' if m.kind == 'video' else 'photo'}: {m.name}"
        report(label, lo + (hi - lo) * done_w / total_w)
        try:
            if m.kind == "video" and cache.exists() and analyze.load(m, cache):
                pass
            elif m.kind == "video":
                analyze.analyze_video(m, lambda f, d=done_w, w=w: report(label, lo + (hi - lo) * (d + f * w) / total_w))
                analyze.save(m, cache)
            else:
                analyze.analyze_image(m)
            ok.append(m)
        except analyze.MediaError as e:
            skipped.append(str(e))
        done_w += w
    if not ok:
        raise ValueError("None of the files could be read. " + " ".join(skipped))
    analyze.score_all(ok)
    for m in ok:
        m.gain_db = loudness_gain(m)
    report("Files studied", hi)
    return ok, skipped


def load_music(src: dict | None) -> music_mod.Music | None:
    if not src:
        return None
    return music_mod.analyze(Path(src["path"]))


def music_info(track: music_mod.Music | None, plan: Plan) -> None:
    if plan.music and track:
        plan.music["duration"] = track.duration


# ---------------------------------------------------------------- captions in source time

def _to_out(plan: Plan, starts: list[float], mi: int, t: float, as_end: bool) -> float | None:
    best = None
    for c, s in zip(plan.clips, starts):
        if c.media_index != mi or c.kind != "video" or abs(c.speed - 1) > 1e-6:
            continue
        a, b = c.start, c.start + c.duration
        if a - 1e-3 <= t <= b + 1e-3:
            return s + min(max(t - a, 0), c.duration)
        if not as_end and a > t and (best is None or s < best):
            best = s  # starts in a removed gap: show from the next kept moment
        if as_end and b < t and (best is None or s + c.duration > best):
            best = s + c.duration
    return best


def project_lines(plan: Plan, lines: list[dict]) -> list[dict]:
    """Source-time caption lines → lines on the finished video."""
    starts = render.clip_starts(plan)
    out = []
    for ln in lines:
        a = _to_out(plan, starts, ln["media"], ln["start"], False)
        b = _to_out(plan, starts, ln["media"], ln["end"], True)
        if a is None or b is None or b - a < 0.25:
            continue
        if b - a > (ln["end"] - ln["start"]) + 1.0:  # pieces far apart in the edit: keep the first piece only
            b = a + (ln["end"] - ln["start"])
        out.append({"start": round(a, 2), "end": round(b, 2), "text": ln["text"]})
    out.sort(key=lambda x: x["start"])
    for x, y in zip(out, out[1:]):
        x["end"] = min(x["end"], y["start"])
    return [x for x in out if x["end"] - x["start"] >= 0.2]


def _needed(plan: Plan, media: dict[int, Media]) -> dict[int, list[tuple[float, float]]]:
    need: dict[int, list[tuple[float, float]]] = {}
    for c in plan.clips:
        m = media.get(c.media_index)
        if c.kind == "video" and m and m.has_audio and abs(c.speed - 1) < 1e-6:
            need.setdefault(c.media_index, []).append((c.start, c.start + c.duration))
    return need


def _uncovered(need: dict, coverage: dict) -> float:
    gap = 0.0
    for mi, ranges in need.items():
        cov = coverage.get(str(mi), [])
        for a, b in ranges:
            rest = planner._subtract([(a, b)], [tuple(x) for x in cov])
            gap += sum(y - x for x, y in rest)
    return gap


def ensure_transcript(state: dict, plan: Plan, media: dict[int, Media], ai: tuple[str, dict], work: Path,
                      report, lo: float, hi: float) -> list[str]:
    """Makes sure every kept moment of speech has been listened to. Returns notes."""
    key, models = ai
    lang = state["settings"].get("caption_lang", "auto")
    tr = state.get("transcript") or {}
    if tr.get("lang") != lang:
        tr = {"lang": lang, "lines": [], "coverage": {}}
    need = _needed(plan, media)
    if not need:
        state["transcript"] = tr
        return ["No speech in the kept clips, so no captions were added"]
    if _uncovered(need, tr["coverage"]) < 2.0:
        state["transcript"] = tr
        return []
    if not (key and models.get("text")):
        return ["Captions need a Google AI key (add one in Settings)"]
    report("Listening to the speech for captions", lo)
    speech = work / "speech.mp3"
    total = render.speech_track(plan, media, speech)
    starts = render.clip_starts(plan)
    heard = []
    pos, k = 0.0, 0
    chunks = max(1, int(np.ceil(total / CHUNK)))
    try:
        while pos < total - 0.3:
            piece = work / f"speech_{k}.mp3"
            ff.run(["-ss", f"{pos:.3f}", "-t", f"{CHUNK:.3f}", "-i", str(speech), "-c", "copy", str(piece)])
            lines = genai.transcribe(key, models["text"], piece, lang)
            lines = captions.snap_to_speech(lines, piece) if lines else []
            heard += [{**ln, "start": ln["start"] + pos, "end": ln["end"] + pos} for ln in lines]
            piece.unlink(missing_ok=True)
            pos += CHUNK
            k += 1
            report("Listening to the speech for captions", lo + (hi - lo) * min(1.0, k / chunks))
    except genai.AIError as e:
        return [f"Captions couldn't be made: {e}"]
    finally:
        speech.unlink(missing_ok=True)
    # Back to source time: which clip was playing when each line was spoken.
    new_lines = []
    for ln in heard:
        mid = (ln["start"] + ln["end"]) / 2
        for c, s in zip(plan.clips, starts):
            if s <= mid < s + c.duration and c.kind == "video" and abs(c.speed - 1) < 1e-6:
                a = c.start + max(0.0, ln["start"] - s)
                new_lines.append({"media": c.media_index, "start": round(a, 2),
                                  "end": round(a + (ln["end"] - ln["start"]), 2), "text": ln["text"]})
                break
    cov = {str(mi): [list(r) for r in ranges] for mi, ranges in need.items()}

    def inside(ln):
        return any(a - 0.05 <= ln["start"] and ln["end"] <= b + 0.05 for a, b in need.get(ln["media"], []))
    tr["lines"] = sorted([x for x in tr["lines"] if not inside(x)] + new_lines,
                         key=lambda x: (x["media"], x["start"]))
    for mi, ranges in cov.items():
        tr["coverage"][mi] = tr["coverage"].get(mi, []) + ranges
    state["transcript"] = tr
    return [] if new_lines else ["No speech was heard, so no captions were added"]


def timeline_transcript(state: dict, plan: Plan) -> list[dict]:
    tr = state.get("transcript") or {}
    return project_lines(plan, tr.get("lines", []))


# ---------------------------------------------------------------- rendering a version

def build_overlay(state: dict, plan: Plan) -> tuple[captions.Overlay, list[dict]]:
    ov = captions.Overlay(plan.width, plan.height)
    if plan.title:
        ov.add_title(plan.title)
    for t in plan.texts:
        ov.add_text(t["text"], float(t["start"]), float(t["end"]), t.get("position", "bottom"))
    lines = []
    s = state["settings"]
    if s.get("captions") == "on":
        lines = timeline_transcript(state, plan)
        if lines:
            ov.add_captions(lines, s.get("caption_style") or captions.DEFAULT_STYLE, s.get("caption_pos", "bottom"),
                            plan.total)
    return ov, lines


def render_version(state: dict, plan: Plan, media: dict[int, Media], work: Path, report, ai, speed: str,
                   label: str, lo: float = 0.0) -> dict:
    """Renders `plan` as the project's next version and records it in `state`."""
    plan.total = render.timeline_total(plan)
    notes = []
    s = state["settings"]
    if s.get("captions") == "on":
        notes += ensure_transcript(state, plan, media, ai, work, report, lo, lo + (1 - lo) * 0.15)
        lo = lo + (1 - lo) * 0.15
    overlay, lines = build_overlay(state, plan)
    if s.get("captions") == "on" and lines:
        style = captions.STYLES.get(s.get("caption_style", ""), captions.STYLES[captions.DEFAULT_STYLE])
        notes.append(f"Captions: {captions.LANGS.get(s.get('caption_lang', 'auto'))}, style {style['name']}")
    n = int(state.get("version", 0)) + 1
    out = work / f"v{n}.mp4"
    music_path = Path(state["music"]["path"]) if state.get("music") and plan.music else None
    started = time.time()
    info = render.render(plan, media, out, work / "parts", overlay, music_path, speed,
                         lambda f: report("Making the video", lo + (1 - lo) * 0.97 * f))
    render.thumbnail(out, work / f"thumb_v{n}.jpg", at=min(1.0, plan.total / 3))
    ver = {"n": n, "label": label, "created": time.time(), "duration": plan.total, "plan": plan.to_dict(),
           "settings": dict(s), "file": out.name, "render_seconds": round(time.time() - started, 1),
           "encoder": info["encoder"], "reused": info["reused"], "parts": info["parts"]}
    state.setdefault("versions", []).append(ver)
    state["version"] = n
    state["captions"] = lines
    plan.notes = [x for x in plan.notes if not x.startswith("Captions")] + notes
    ver["plan"]["notes"] = plan.notes
    _prune_versions(state, work)
    report("Done", 1.0)
    return ver


def _prune_versions(state: dict, work: Path) -> None:
    """Keeps the finished files of the last few versions (long videos take a lot of space)."""
    vers = state.get("versions", [])
    keep = 2 if vers and vers[-1]["duration"] > 300 else 4
    for v in vers[:-keep]:
        if v.get("file"):
            (work / v["file"]).unlink(missing_ok=True)
            (work / f"thumb_v{v['n']}.jpg").unlink(missing_ok=True)
            v["file"] = None


# ---------------------------------------------------------------- the first edit

def first_edit(state: dict, work: Path, report, ai, speed: str, options: dict) -> tuple[dict, dict[int, Media]]:
    """Studies the files, plans the edit and renders version 1."""
    work.mkdir(parents=True, exist_ok=True)
    report("Opening your files", 0.0)
    media_list, skipped = load_media(state["sources"], report, 0.0, 0.25)
    media = {m.index: m for m in media_list}
    track = None
    if state.get("music"):
        report("Finding the beat of your music", 0.25)
        try:
            track = load_music(state["music"])
        except music_mod.MusicError as e:
            skipped.append(str(e))
            state["music"] = None
    report("Choosing the best moments", 0.27)
    if state["mode"] == "cleanup":
        plan = planner.make_cleanup_plan(media_list, options, track)
    else:
        plan = planner.make_plan(media_list, options, track)
    music_info(track, plan)
    state["skipped"] = skipped
    ver = render_version(state, plan, media, work, report, ai, speed, "First edit", lo=0.3)
    return ver, media


def replan(state: dict, media: dict[int, Media], mode: str, length: float, options: dict) -> Plan:
    """A fresh plan from the original files (chat: "start again", "make a highlight")."""
    track = None
    if state.get("music"):
        try:
            track = load_music(state["music"])
        except music_mod.MusicError:
            track = None
    opts = {**options, "length": str(length) if length else "auto"}
    media_list = [media[i] for i in sorted(media)]
    plan = planner.make_cleanup_plan(media_list, opts, track) if mode == "cleanup" else \
        planner.make_plan(media_list, opts, track)
    music_info(track, plan)
    return plan


# ---------------------------------------------------------------- chat

def ensure_scenes(state: dict, media: dict[int, Media], ai, work: Path, report) -> None:
    """Lets the AI see the videos: a few pictures from each file, described once and remembered."""
    key, models = ai
    if not (key and models.get("text")):
        return
    scenes = state.setdefault("scenes", {})
    for i, m in media.items():
        if str(i) in scenes:
            continue
        report(f"Looking at {m.name}", 0.05)
        if m.kind == "video":
            n = int(min(40, max(6, m.duration / 6)))
            times = [round((k + 0.5) * m.duration / n, 1) for k in range(n)]
        else:
            times = [0.0]
        frames = render.frames_at(m.path, times, work / "frames" / str(i), width=320)
        stamps = [(float(f.stem.split("_")[1]) / 10, f) for f in frames]
        try:
            scenes[str(i)] = genai.describe_frames(key, models["text"], stamps)
        except genai.AIError:
            scenes[str(i)] = []
        for f in frames:
            f.unlink(missing_ok=True)


def chat(state: dict, plan: Plan, media: dict[int, Media], message: str, ai, work: Path, report, speed: str,
         options: dict) -> tuple[str, dict | None, bool]:
    """Answers a chat message and, when the video should change, renders a new version.
    Returns (reply, new version or None, undo_requested)."""
    key, models = ai
    use_ai = bool(key and models.get("text"))
    settings = dict(state["settings"])
    if use_ai:
        ensure_scenes(state, media, ai, work, report)
        report("Thinking about your request", 0.1)
        ctx = edits.context(plan, media, settings, timeline_transcript(state, plan),
                            {int(k): v for k, v in (state.get("scenes") or {}).items()})
        hist = [h for h in state.get("chat", []) if h.get("text")][:-1]
        try:
            answer = genai.edit_chat(key, models["text"], ctx, message, hist)
        except genai.AIError as e:
            ops, ok = edits.parse_local(message, plan)
            if not ok:
                return f"I couldn't reach Google AI ({e}). " + edits.LOCAL_HELP, None, False
            answer = {"reply": "", "operations": ops}
    else:
        ops, ok = edits.parse_local(message, plan)
        if not ok:
            return "I didn't understand that. " + edits.LOCAL_HELP, None, False
        answer = {"reply": "", "operations": ops}

    ops = answer["operations"]
    if not ops:
        return answer["reply"] or "Okay.", None, False
    result = edits.apply(ops, plan, media, settings)
    if result.undo and len(ops) == 1:
        return answer["reply"] or "Going back to the previous version.", None, True
    if not result.changed:
        return (answer["reply"] + " " if answer["reply"] else "") + edits.summary(result), None, False
    new_plan = result.plan
    if result.reedit:
        new_plan = replan(state, media, result.reedit["mode"], result.reedit["length"], options)
        new_plan.title = new_plan.title or plan.title
        new_plan.texts = copy.deepcopy(plan.texts)
    state["settings"] = result.settings
    label = (message[:60] + "…") if len(message) > 60 else message
    ver = render_version(state, new_plan, media, work, report, ai, speed, label, lo=0.15)
    reply = answer["reply"] or edits.summary(result)
    if answer["reply"] and result.problems:
        reply += " (" + "; ".join(result.problems) + ")"
    return reply, ver, False
