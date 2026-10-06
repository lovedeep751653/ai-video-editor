"""Changes to an existing edit, asked for in the chat ("remove clip 3", "make it
black and white", "cut from 1:10 to 1:25", "add text 'Happy birthday' at the
start"...).

Google AI turns any wording into a list of operations (see genai.edit_chat);
without a key, `parse_local` understands the common commands itself. `apply`
carries out the operations on the plan; the pipeline then renders a new version.
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field

import numpy as np

from . import captions, effects, intent
from . import plan as planner
from .analyze import Media
from .plan import Clip, Plan
from .render import FPS, clip_starts

MIN_CLIP = 0.4


@dataclass
class Result:
    plan: Plan
    settings: dict
    done: list[str] = field(default_factory=list)      # plain-language list of what changed
    problems: list[str] = field(default_factory=list)  # what couldn't be done, and why
    undo: bool = False
    reedit: dict | None = None                         # {"mode", "length"} = start again from the files
    changed: bool = False


def _mmss(t: float) -> str:
    t = max(0.0, t)
    return f"{int(t // 60)}:{t % 60:04.1f}".replace(".0", "") if t >= 60 else f"{t:.1f} s".replace(".0 s", " s")


def _clip_nums(op: dict, n: int) -> list[int]:
    nums = op.get("clips") or ([op["clip"]] if op.get("clip") else [])
    out = []
    for x in nums:
        try:
            k = int(x)
        except (TypeError, ValueError):
            continue
        if 1 <= k <= n and k - 1 not in out:
            out.append(k - 1)
    return out


def _num(op: dict, key: str = "number", default: float | None = None) -> float | None:
    try:
        v = op.get(key)
        return float(v) if v is not None else default
    except (TypeError, ValueError):
        return default


def _cut_out(plan: Plan, start: float, end: float) -> float:
    """Removes start..end of the finished video. Returns seconds removed."""
    starts = clip_starts(plan)
    new: list[Clip] = []
    removed = 0.0
    for c, s in zip(plan.clips, starts):
        d = c.duration
        e = s + d
        a, b = max(start, s), min(end, e)
        if b - a <= 1e-3:
            new.append(c)
            continue
        removed += b - a
        sp = c.speed if c.kind == "video" else 1.0
        if a - s >= MIN_CLIP:  # part before the cut
            first = copy.deepcopy(c)
            first.duration = round(a - s, 3)
            new.append(first)
        if e - b >= MIN_CLIP:  # part after the cut
            second = copy.deepcopy(c)
            if c.kind == "video":
                second.start = round(c.start + (b - s) * sp, 3)
            second.duration = round(e - b, 3)
            new.append(second)
    plan.clips = new
    return removed


def _fill_for(m: Media, plan: Plan) -> bool:
    return planner._fill(m, plan.width, plan.height)


def remove_pauses(plan: Plan, media: dict[int, Media], strength: str) -> float:
    """Cuts silences out of every talking clip. Returns seconds removed."""
    limit = planner.PAUSE_LIMIT["fast" if strength == "strong" else "smooth"]
    new: list[Clip] = []
    removed = 0.0
    for c in plan.clips:
        m = media.get(c.media_index)
        if c.kind != "video" or m is None or abs(c.speed - 1) > 1e-6:
            new.append(c)
            continue
        talk = planner.speech_ranges(m, limit)
        if talk is None:
            new.append(c)
            continue
        a0, b0 = c.start, c.start + c.duration
        pieces = [(max(a, a0), min(b, b0)) for a, b in talk if min(b, b0) - max(a, a0) >= 0.5]
        if not pieces:
            new.append(c)
            continue
        kept = sum(b - a for a, b in pieces)
        removed += c.duration - kept
        for a, b in pieces:
            x = copy.deepcopy(c)
            x.start, x.duration = round(a, 3), round(b - a, 3)
            new.append(x)
    plan.clips = new
    return removed


def apply(ops: list[dict], plan: Plan, media: dict[int, Media], settings: dict) -> Result:
    """Carries out the operations on a copy of the plan."""
    p = copy.deepcopy(plan)
    r = Result(plan=p, settings=dict(settings))
    for op in ops:
        name = op.get("op")
        n = len(p.clips)
        try:
            if name == "undo":
                r.undo = True
            elif name == "remove_clips":
                idx = _clip_nums(op, n)
                if not idx:
                    r.problems.append("I couldn't tell which clips to remove")
                elif len(idx) >= n:
                    r.problems.append("I can't remove every clip; at least one has to stay")
                else:
                    p.clips = [c for i, c in enumerate(p.clips) if i not in idx]
                    r.done.append(f"removed clip{'s' if len(idx) > 1 else ''} {', '.join(str(i + 1) for i in sorted(idx))}")
            elif name == "keep_only_clips":
                idx = _clip_nums(op, n)
                if idx:
                    p.clips = [p.clips[i] for i in sorted(idx)]
                    r.done.append(f"kept only clip{'s' if len(idx) > 1 else ''} {', '.join(str(i + 1) for i in sorted(idx))}")
            elif name == "move_clip":
                idx = _clip_nums(op, n)
                to = int(_num(op, "to", 0) or 0)
                if idx and 1 <= to <= n:
                    c = p.clips.pop(idx[0])
                    p.clips.insert(to - 1, c)
                    r.done.append(f"moved clip {idx[0] + 1} to position {to}")
                else:
                    r.problems.append("I couldn't tell which clip to move where")
            elif name in ("cut_time", "keep_time"):
                total = p.total or sum(c.duration for c in p.clips)
                a, b = _num(op, "start", 0.0), _num(op, "end", total)
                a, b = max(0.0, min(a, b)), min(total, max(a, b))
                if b - a < 0.1:
                    r.problems.append("That stretch of time is too short or outside the video")
                elif name == "cut_time":
                    gone = _cut_out(p, a, b)
                    r.done.append(f"cut out {_mmss(a)} to {_mmss(b)} ({gone:.1f} s)")
                else:
                    _cut_out(p, b, total + 1)
                    _cut_out(p, 0, a)
                    r.done.append(f"kept only {_mmss(a)} to {_mmss(b)}")
            elif name in ("trim_clip_start", "trim_clip_end"):
                idx = _clip_nums(op, n)
                sec = abs(_num(op, default=1.0) or 1.0)
                for i in idx:
                    c = p.clips[i]
                    sec2 = min(sec, c.duration - MIN_CLIP)
                    if sec2 <= 0:
                        r.problems.append(f"clip {i + 1} is already very short")
                        continue
                    if name == "trim_clip_start" and c.kind == "video":
                        c.start = round(c.start + sec2 * c.speed, 3)
                    c.duration = round(c.duration - sec2, 3)
                    r.done.append(f"trimmed {sec2:.1f} s off the {'start' if name == 'trim_clip_start' else 'end'} "
                                  f"of clip {i + 1}")
            elif name == "add_source_part":
                src = int(_num(op, "source", 0) or 0) - 1
                m = media.get(src)
                if m is None:
                    r.problems.append("I couldn't find that original file")
                    continue
                if m.kind == "image":
                    a, b = 0.0, max(1.0, min(_num(op, "end", 3.0) - _num(op, "start", 0.0), 10.0))
                else:
                    a = max(0.0, _num(op, "start", 0.0) or 0.0)
                    b = min(m.duration, _num(op, "end", a + 3) or a + 3)
                if b - a < MIN_CLIP:
                    r.problems.append("That part of the file is too short")
                    continue
                fps = 1 / (m.times[1] - m.times[0]) if m.times.size > 1 else 1.0
                i0, i1 = int(a * fps), max(int(b * fps), int(a * fps) + 1)
                c = Clip(media_index=m.index, name=m.name, kind=m.kind, start=round(a, 3) if m.kind == "video" else 0.0,
                         duration=round(b - a, 3), score=float(np.mean(m.score[i0:i1])) if m.score.size else 0.5,
                         fill=_fill_for(m, p), reasons=["added by you"],
                         brightness=float(np.mean(m.brightness[i0:i1])) if m.brightness.size else 0.47)
                to = int(_num(op, "to", 0) or 0)
                pos = to - 1 if 1 <= to <= n + 1 else n
                p.clips.insert(pos, c)
                r.done.append(f"added {m.name} {_mmss(a)}–{_mmss(b)} as clip {pos + 1}")
            elif name == "speed":
                f = max(0.25, min(4.0, _num(op, default=1.0) or 1.0))
                idx = _clip_nums(op, n) or list(range(n))
                for i in idx:
                    c = p.clips[i]
                    old = c.speed if c.kind == "video" else 1.0
                    c.duration = round(c.duration * old / f, 3)
                    if c.kind == "video":
                        c.speed = round(f, 4)
                what = "the whole video" if len(idx) == n else f"clip{'s' if len(idx) > 1 else ''} " + \
                       ", ".join(str(i + 1) for i in idx)
                r.done.append(("slow motion (" + f"{f:g}x) on " if f < 1 else f"speed {f:g}x on ") + what
                              if abs(f - 1) > 1e-6 else f"normal speed on {what}")
            elif name == "zoom":
                z = max(1.0, min(2.0, _num(op, default=1.25) or 1.25))
                idx = _clip_nums(op, n) or list(range(n))
                for i in idx:
                    p.clips[i].zoom = round(z, 3)
                r.done.append(f"zoom {z:g}x on " + ("the whole video" if len(idx) == n else
                                                     "clip " + ", ".join(str(i + 1) for i in idx)))
            elif name == "set_look":
                v = str(op.get("value") or "")
                if v in effects.LOOK_NAMES:
                    p.look = v
                    r.done.append(f"look: {effects.LOOK_NAMES[v]}")
                else:
                    r.problems.append(f"I don't have a look called {v}")
            elif name == "set_format":
                v = str(op.get("value") or "")
                if v in planner.FORMATS or v == "original":
                    p.format = v
                    p.width, p.height = planner.dims(v, min(plan.width, plan.height) if plan.format != "original"
                                                     else _quality(plan), list(media.values()))
                    for c in p.clips:
                        if c.media_index in media:
                            c.fill = _fill_for(media[c.media_index], p)
                    r.done.append(f"shape: {v}")
            elif name == "set_transitions":
                v = str(op.get("value") or "")
                if v in effects.TRANSITION_SETS:
                    p.transition_kind = v
                    length = _num(op, default=0.0) or 0.0
                    p.transition = 0.0 if v == "cuts" else round(max(0.2, min(1.5, length)) if length else
                                                                 (0.25 if p.style == "fast" else 0.6), 3)
                    p.transitions = []
                    r.done.append("no transitions (straight cuts)" if v == "cuts" else f"{v} transitions")
                    if p.mode == "cleanup" and v != "cuts" and len({c.media_index for c in p.clips}) <= 1:
                        r.problems.append("in a cleaned-up video transitions play only between different files, "
                                          "and this video comes from one file")
            elif name == "set_title":
                p.title = str(op.get("text") or "").strip()[:80]
                r.done.append(f'title: "{p.title}"' if p.title else "title removed")
            elif name == "add_text":
                text = str(op.get("text") or "").strip()[:120]
                a = max(0.0, _num(op, "start", 0.0) or 0.0)
                b = _num(op, "end", 0.0) or 0.0
                if b <= a:
                    b = a + 3.0
                pos = op.get("position") if op.get("position") in ("top", "middle", "bottom") else "bottom"
                if text:
                    p.texts.append({"text": text, "start": round(a, 2), "end": round(b, 2), "position": pos})
                    r.done.append(f'text "{text}" from {_mmss(a)} to {_mmss(b)}')
            elif name == "remove_texts":
                idx = [int(x) - 1 for x in (op.get("clips") or []) if str(x).isdigit()]
                before = len(p.texts)
                p.texts = [t for i, t in enumerate(p.texts) if idx and i not in idx]
                r.done.append(f"removed {before - len(p.texts)} text{'s' if before - len(p.texts) != 1 else ''}")
            elif name == "set_captions":
                v = "on" if str(op.get("value")) in ("on", "true", "yes") else "off"
                r.settings["captions"] = v
                r.done.append("captions on" if v == "on" else "captions off")
            elif name == "caption_language":
                v = str(op.get("value") or "")
                if v in captions.LANGS:
                    r.settings["caption_lang"] = v
                    r.settings["captions"] = "on"
                    r.done.append(f"captions in {captions.LANGS[v]}")
            elif name == "caption_style":
                v = str(op.get("value") or "")
                if v in captions.STYLES:
                    r.settings["caption_style"] = v
                    r.settings["captions"] = "on"
                    r.done.append(f"caption style: {captions.STYLES[v]['name']}")
            elif name == "caption_position":
                v = str(op.get("value") or op.get("position") or "")
                if v in captions.POSITIONS:
                    r.settings["caption_pos"] = v
                    r.done.append(f"captions at the {v}")
            elif name in ("music_volume", "original_volume"):
                v = max(0.0, min(2.0, _num(op, default=1.0)))
                setattr(p, name, round(v, 3))
                if name == "music_volume" and not p.music:
                    r.problems.append("there is no music in this video yet (add a music file first)")
                else:
                    r.done.append(f"{'music' if name == 'music_volume' else 'original sound'} volume {round(v * 100)}%")
            elif name == "remove_music":
                if p.music:
                    p.music = None
                    p.original_volume = 1.0
                    r.done.append("music removed")
            elif name == "remove_pauses":
                gone = remove_pauses(p, media, str(op.get("value") or "gentle"))
                r.done.append(f"removed {gone:.1f} s of pauses" if gone > 0.2 else "no more pauses to remove")
            elif name == "shorten_to":
                target = max(3.0, _num(op, default=0.0) or 0.0)
                total = sum(c.duration for c in p.clips)
                if target >= total - 0.5:
                    r.problems.append(f"the video is already {_mmss(total)} long")
                else:
                    order = sorted(range(len(p.clips)), key=lambda i: p.clips[i].score)
                    drop = set()
                    for i in order:
                        if total <= target or len(drop) >= len(p.clips) - 1:
                            break
                        drop.add(i)
                        total -= p.clips[i].duration
                    p.clips = [c for i, c in enumerate(p.clips) if i not in drop]
                    if total > target + 0.5:  # one long clip left: cut its end
                        c = max(p.clips, key=lambda c: c.duration)
                        c.duration = round(max(MIN_CLIP, c.duration - (total - target)), 3)
                    r.done.append(f"shortened to about {_mmss(target)}")
            elif name == "reedit":
                mode = op.get("value") if op.get("value") in ("highlight", "cleanup") else p.mode
                length = _num(op, default=0.0) or 0.0
                r.reedit = {"mode": mode, "length": length}
                r.done.append("made a fresh " + ("highlight" if mode == "highlight" else "clean-up") + " edit"
                              + (f" of about {_mmss(length)}" if length else ""))
            elif name == "sort_clips":
                v = op.get("value")
                if v == "best_first":
                    p.clips.sort(key=lambda c: -c.score)
                elif v == "reverse":
                    p.clips.reverse()
                else:
                    p.clips.sort(key=lambda c: (c.media_index, c.start))
                r.done.append({"best_first": "best clips first", "reverse": "clips in reverse order"}.get(
                    v, "clips in the order they were filmed"))
            elif name == "fade":
                p.fade = str(op.get("value")) != "off"
                r.done.append("fade in and out" if p.fade else "no fade at the start and end")
        except (KeyError, ValueError, TypeError, IndexError) as e:
            r.problems.append(f"couldn't do '{name}' ({e})")
    if not p.clips:
        p.clips = copy.deepcopy(plan.clips)
        r.problems.append("that would leave nothing in the video, so I kept the clips")
    p.recount()
    r.changed = bool(r.reedit) or p.to_dict() != plan.to_dict() or r.settings != settings
    return r


def _quality(plan: Plan) -> int:
    return 720 if min(plan.width, plan.height) <= 720 else 1080


# ---------------------------------------------------------------- understanding commands without AI

_NUM_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
              "ten": 10, "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "last": -1}


def _time(s: str) -> float | None:
    s = s.strip()
    m = re.fullmatch(r"(\d+):(\d{1,2}(?:\.\d+)?)", s)
    if m:
        return int(m.group(1)) * 60 + float(m.group(2))
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(?:s|sec|secs|second|seconds)?", s)
    if m:
        return float(m.group(1))
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(?:m|min|mins|minute|minutes)", s)
    if m:
        return float(m.group(1)) * 60
    return None


TIME = r"(\d+:\d{1,2}(?:\.\d+)?|\d+(?:\.\d+)?\s*(?:s|sec|secs|seconds?|m|mins?|minutes?)?)"


def _numbers(text: str, n: int) -> list[int]:
    out = []
    for w in re.findall(r"\d+|" + "|".join(_NUM_WORDS), text):
        k = int(w) if w.isdigit() else _NUM_WORDS[w]
        if k == -1:
            k = n
        if 1 <= k <= n and k not in out:
            out.append(k)
    return out


def parse_local(message: str, plan: Plan) -> tuple[list[dict], bool]:
    """Common chat commands → operations, without any AI. Returns (operations, understood)."""
    t = " " + message.lower().replace("’", "'").strip() + " "
    n = len(plan.clips)
    ops: list[dict] = []
    quoted = re.search(r"[\"“‘']([^\"”’']{1,120})[\"”’']", message)

    if re.search(r"\b(undo|go back|previous version|wapas|pehle wala|ਵਾਪਸ|वापस)\b", t):
        return [{"op": "undo"}], True

    clip_ref = re.search(r"\b(?:clips?|parts?|shots?)\s+((?:(?:\d+|" + "|".join(_NUM_WORDS) +
                         r")(?:\s*(?:,|and|&|to|-)\s*)?)+)", t)
    nums = _numbers(clip_ref.group(1), n) if clip_ref else []
    if clip_ref and re.search(r"(\d+)\s*(?:to|-)\s*(\d+)", clip_ref.group(1)):
        a, b = map(int, re.search(r"(\d+)\s*(?:to|-)\s*(\d+)", clip_ref.group(1)).groups())
        nums = [k for k in range(min(a, b), max(a, b) + 1) if 1 <= k <= n]

    rng = re.search(r"(?:from|between)\s+" + TIME + r"\s+(?:to|and|till|until|-)\s+" + TIME, t)
    if rng:
        a, b = _time(rng.group(1)), _time(rng.group(2))
        if a is not None and b is not None:
            if re.search(r"\bkeep only\b|\bonly keep\b|\bjust keep\b", t):
                ops.append({"op": "keep_time", "start": a, "end": b})
            elif re.search(r"\b(cut|remove|delete|drop|hatao|hata do|kaat)\b", t):
                ops.append({"op": "cut_time", "start": a, "end": b})

    if nums and not rng:
        if re.search(r"\bkeep only\b|\bonly keep\b|\bjust keep\b", t):
            ops.append({"op": "keep_only_clips", "clips": nums})
        elif re.search(r"\b(remove|delete|drop|cut|get rid of|hatao|hata do)\b", t) and \
                not re.search(r"\btrim\b", t):
            ops.append({"op": "remove_clips", "clips": nums})
        m = re.search(r"\bmove\b.*?\bto\s+(?:the\s+)?(start|beginning|end|position\s+\d+|\d+)", t)
        if m:
            to = 1 if m.group(1) in ("start", "beginning") else n if m.group(1) == "end" else \
                int(re.search(r"\d+", m.group(1)).group())
            ops.append({"op": "move_clip", "clip": nums[0], "to": to})
        m = re.search(r"\btrim\b.*?" + TIME + r"?.*?\b(start|beginning|end)\b", t)
        if m:
            sec = _time(m.group(1)) if m.group(1) else 1.0
            ops.append({"op": "trim_clip_start" if m.group(2) in ("start", "beginning") else "trim_clip_end",
                        "clips": nums, "number": sec or 1.0})

    target = nums if nums else []
    if re.search(r"slow[\s-]?mo|slow motion|slowmo", t) and not re.search(r"\bno slow", t):
        ops.append({"op": "speed", "clips": target, "number": 0.5})
    m = re.search(r"(\d+(?:\.\d+)?)\s*x\b|\b(?:speed up|faster|fast forward|timelapse)\b", t)
    if m and not re.search(r"\bfaster cuts|fast cuts|fast pace\b", t):
        ops.append({"op": "speed", "clips": target, "number": float(m.group(1)) if m.group(1) else 2.0})
    if re.search(r"\bnormal speed|real speed|original speed\b", t):
        ops.append({"op": "speed", "clips": target, "number": 1.0})
    if re.search(r"\bzoom", t):
        ops.append({"op": "zoom", "clips": target, "number": 1.0 if re.search(r"\bno zoom|zoom out|remove zoom", t)
                    else 1.25})

    if re.search(r"\b(remove|cut|delete|no)\b.{0,20}\b(pauses?|silences?|gaps?|breaks?|dead air|umm?s?)\b", t) or \
            re.search(r"\bjump ?cuts?\b", t):
        ops.append({"op": "remove_pauses", "value": "strong" if re.search(r"\b(all|more|every|strong)\b", t)
                    else "gentle"})

    m = re.search(r"\b(?:make it|shorten(?: it)? to|cut it to|only|under|about)\s+" + TIME, t)
    if m and not rng:
        sec = _time(m.group(1))
        if sec and re.search(r"\d", m.group(1)):
            ops.append({"op": "shorten_to", "number": sec})
    elif re.search(r"\b(shorter|too long|chhota|chota)\b", t):
        ops.append({"op": "shorten_to", "number": round(sum(c.duration for c in plan.clips) * 0.7, 1)})

    if re.search(r"\b(remove|delete|no)\s+(the\s+)?title\b", t):
        ops.append({"op": "set_title", "text": ""})
    elif re.search(r"\btitle\b|\bheading\b", t) and quoted:
        ops.append({"op": "set_title", "text": quoted.group(1)})
    if re.search(r"\b(remove|delete|no)\s+(the\s+|all\s+)?texts?\b", t):
        ops.append({"op": "remove_texts", "clips": []})
    elif re.search(r"\b(add|put|write|show)\b.{0,15}\btext\b|\btext\b.{0,10}\bsaying\b", t) and quoted:
        at = re.search(r"\bat\s+" + TIME, t)
        dur = re.search(r"\bfor\s+" + TIME, t)
        start = _time(at.group(1)) if at else (sum(c.duration for c in plan.clips) - 3 if re.search(r"\bend\b", t) else 0.0)
        length = _time(dur.group(1)) if dur else 3.0
        pos = "top" if " top " in t else "middle" if re.search(r"\b(middle|center|centre)\b", t) else "bottom"
        ops.append({"op": "add_text", "text": quoted.group(1), "start": max(0.0, start or 0.0),
                    "end": max(0.0, start or 0.0) + (length or 3.0), "position": pos})

    if re.search(r"\b(no|remove|without|stop)\s+(the\s+)?music\b|\bmusic off\b", t):
        ops.append({"op": "remove_music"})
    elif re.search(r"\bmusic\b.{0,15}\b(louder|up|higher|more)\b|\blouder music\b", t):
        ops.append({"op": "music_volume", "number": min(2.0, plan.music_volume * 1.6 + 0.05)})
    elif re.search(r"\bmusic\b.{0,15}\b(quieter|softer|lower|down|less)\b|\bquieter music\b", t):
        ops.append({"op": "music_volume", "number": plan.music_volume * 0.55})
    if re.search(r"\b(voice|speech|my sound|original sound|talking)\b.{0,15}\b(louder|up|higher|clearer)\b", t):
        ops.append({"op": "original_volume", "number": min(2.0, plan.original_volume * 1.5)})
    elif re.search(r"\b(mute|no sound|silent|remove sound)\b", t) and "music" not in t:
        ops.append({"op": "original_volume", "number": 0.0})

    if re.search(r"\b(no|remove|without)\s+fade", t):
        ops.append({"op": "fade", "value": "off"})

    if re.search(r"\b(start again|start over|redo|re-?edit|fresh edit|new edit)\b", t):
        ops.append({"op": "reedit", "value": "cleanup" if re.search(r"clean|long|pauses", t) else "highlight"})
    elif re.search(r"\b(highlight|best moments|best parts)\b", t):
        ops.append({"op": "reedit", "value": "highlight"})
    if re.search(r"\b(best (clips|parts) first)\b", t):
        ops.append({"op": "sort_clips", "value": "best_first"})
    elif re.search(r"\breverse (the )?order\b", t):
        ops.append({"op": "sort_clips", "value": "reverse"})

    # Settings words shared with the first request: look, shape, transitions, captions.
    kw = intent.parse_keywords(message)
    if "look" in kw:
        ops.append({"op": "set_look", "value": kw["look"]})
    if "format" in kw and not re.search(r"\byoutube\b.{0,10}\bshort", t):
        ops.append({"op": "set_format", "value": kw["format"]})
    if "transitions" in kw:
        ops.append({"op": "set_transitions", "value": kw["transitions"]})
    if "captions" in kw and "remove_texts" not in [o["op"] for o in ops]:
        ops.append({"op": "set_captions", "value": kw["captions"]})
    if "caption_lang" in kw:
        ops.append({"op": "caption_language", "value": kw["caption_lang"]})
    m = re.search(r"\b(captions?|subtitles?)\b.{0,20}\b(top|middle|center|centre|bottom)\b", t)
    if m:
        ops.append({"op": "caption_position", "value": "middle" if m.group(2) in ("center", "centre") else m.group(2)})
    return ops, bool(ops)


LOCAL_HELP = ("Without a Google AI key I understand simple commands like: \"remove clip 3\", "
              "\"cut from 0:10 to 0:25\", \"move clip 4 to the start\", \"slow motion on clip 2\", "
              "\"make it black and white\", \"make it 30 seconds\", \"remove the pauses\", "
              "\"title 'Goa Trip'\", \"add text 'Happy birthday' at 0:05\", \"captions in Punjabi\", "
              "\"music quieter\", \"undo\". Add a Google AI key in Settings and I'll understand anything you say.")


def summary(result: Result) -> str:
    """A short reply describing what happened."""
    parts = []
    if result.done:
        s = "; ".join(result.done)
        parts.append("Done: " + s[0].lower() + s[1:] + ".")
    if result.problems:
        s = "; ".join(result.problems)
        parts.append(s[0].upper() + s[1:] + ".")
    return " ".join(parts) or "Nothing needed changing."


def context(plan: Plan, media: dict[int, Media], settings: dict, transcript: list[dict],
            scenes: dict[int, list[dict]]) -> dict:
    """What the AI sees about the current edit."""
    starts = clip_starts(plan)
    clips = []
    for i, (c, s) in enumerate(zip(plan.clips, starts)):
        item = {"n": i + 1, "at": round(s, 2), "length": round(c.duration, 2), "source": c.media_index + 1,
                "kind": c.kind}
        if c.kind == "video":
            item["from"] = round(c.start, 2)
            item["to"] = round(c.start + c.duration * c.speed, 2)
        if abs(c.speed - 1) > 1e-6:
            item["speed"] = c.speed
        if c.zoom > 1.001:
            item["zoom"] = c.zoom
        item["quality"] = round(c.score, 2)
        sc = scenes.get(c.media_index) or []
        if sc and c.kind == "video":
            seen = [x["text"] for x in sc if c.start - 1 <= x["time"] <= c.start + c.duration * c.speed + 1]
            if seen:
                item["shows"] = "; ".join(dict.fromkeys(seen))[:300]
        elif sc and c.kind == "image":
            item["shows"] = sc[0]["text"]
        clips.append(item)
    sources = []
    for i in sorted(media):
        m = media[i]
        src = {"source": i + 1, "name": m.name, "kind": m.kind}
        if m.kind == "video":
            src["length"] = round(m.duration, 1)
            if scenes.get(i):
                src["what_happens"] = [f"{x['time']:.0f}s: {x['text']}" for x in scenes[i]][:60]
        sources.append(src)
    return {
        "video": {"length": round(plan.total, 2), "shape": plan.format, "look": plan.look,
                  "transitions": plan.transition_kind, "title": plan.title, "fade": plan.fade,
                  "music": (plan.music or {}).get("name"), "music_volume": plan.music_volume,
                  "original_volume": plan.original_volume, "captions": settings.get("captions", "off"),
                  "caption_language": settings.get("caption_lang", "auto"),
                  "caption_style": settings.get("caption_style"), "mode": plan.mode},
        "clips": clips[:400],
        "texts": [{"n": i + 1, **t} for i, t in enumerate(plan.texts)],
        "sources": sources,
        "transcript": [{"at": round(x["start"], 1), "text": x["text"]} for x in transcript][:500],
        "looks": list(effects.LOOK_NAMES),
        "caption_styles": list(captions.STYLES)[:45],
    }
