"""Turns the measured media into an editing plan: which parts to keep, in what
order, how long, with which transitions, look, title and music, and in which
shape (9:16, 16:9, 1:1). Every choice left on "auto" is decided here from the
media itself."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field

import numpy as np

from . import effects
from .analyze import Media, describe
from .music import Music

FORMATS = {
    "9:16": (1080, 1920),
    "16:9": (1920, 1080),
    "1:1": (1080, 1080),
}
PACES = {
    # target clip length (s), photo length (s), transition length (s)
    "smooth": (3.2, 2.6, 0.6),
    "fast": (1.6, 1.3, 0.25),
}
MIN_SCORE = 0.22  # moments scoring below this count as "boring" and are left out
SLOWMO_MOTION = 0.025  # clips with more movement than this get slow motion when it's on

DEFAULTS = {"format": "auto", "style": "auto", "length": "auto", "look": "auto",
            "transitions": "auto", "slowmo": "auto", "title": ""}


@dataclass
class Clip:
    media_index: int
    name: str
    kind: str
    start: float      # where in the original file this part starts (s)
    duration: float   # how long it lasts in the finished video (s)
    score: float
    fill: bool        # True = fill the frame (crop edges); False = whole picture on blurred background
    reasons: list[str]
    speed: float = 1.0       # 0.5 = slow motion
    brightness: float = 0.47  # measured, for the automatic light fix


@dataclass
class Plan:
    format: str
    width: int
    height: int
    style: str
    look: str
    transition_kind: str
    transition: float
    transitions: list[str]
    clips: list[Clip]
    total: float
    notes: list[str]
    title: str = ""
    music: dict | None = None  # {"name", "start", "bpm", "synced"}
    original_volume: float = 1.0

    def to_dict(self) -> dict:
        return asdict(self)


def choose_format(media: list[Media]) -> str:
    """Picks the shape most of the footage was filmed in."""
    weight = {"9:16": 0.0, "16:9": 0.0, "1:1": 0.0}
    for m in media:
        w = m.duration if m.kind == "video" else 2.5
        ar = m.width / m.height
        if ar < 0.85:
            weight["9:16"] += w
        elif ar > 1.18:
            weight["16:9"] += w
        else:
            weight["1:1"] += w
    return max(weight, key=lambda k: (weight[k], k == "9:16"))


def choose_style(media: list[Media], music: Music | None) -> str:
    if music and music.has_beat:
        return "fast" if music.bpm >= 118 else "smooth"
    vids = [m for m in media if m.kind == "video"]
    if not vids:
        return "smooth"
    motion = np.concatenate([m.motion for m in vids])
    return "fast" if float(np.median(motion)) > 0.035 else "smooth"


def choose_length(media: list[Media]) -> float:
    good = 0.0
    for m in media:
        if m.kind == "image":
            good += 2.5
        else:
            good += m.duration * float(np.mean(m.score >= 0.45)) if m.score.size else 0
    return float(np.clip(good * 0.6, 10, 60))


def choose_transitions(style: str, look: str) -> str:
    if look in ("cinematic", "dramatic", "vintage", "bw"):
        return "cinematic"
    return "dynamic" if style == "fast" else "soft"


def _fill(m: Media, width: int, height: int) -> bool:
    src, dst = m.width / m.height, width / height
    return max(src, dst) / min(src, dst) < 1.3


def _windows(m: Media, length: float, min_len: float):
    """Candidate sections of one video: (start, duration, score, i0, i1, scene_start)."""
    if m.times.size == 0:
        return
    fps = 1 / (m.times[1] - m.times[0]) if m.times.size > 1 else 4.0
    bounds = [0.0] + [c for c in m.scene_cuts if 0 < c < m.duration] + [m.duration]
    step = 0.5
    for a, b in zip(bounds, bounds[1:]):
        a2 = a + (0.25 if a == 0 else 0.1)  # skip the very first shaky moment / the cut frame
        span = b - a2 - 0.05
        if span <= 0:
            continue
        dur = min(length, span)
        if dur < min_len:
            continue
        t = a2
        while t + dur <= b - 0.05 + 1e-6:
            i0, i1 = int(t * fps), max(int((t + dur) * fps), int(t * fps) + 1)
            seg = m.score[i0:i1]
            if seg.size:
                # weakest moment matters: a section with a blurry second in it is worse
                sc = 0.75 * float(seg.mean()) + 0.25 * float(seg.min())
                yield (round(t, 3), round(dur, 3), sc, i0, i1, a)
            t += step


def make_plan(media: list[Media], options: dict | None = None, music: Music | None = None) -> Plan:
    o = {**DEFAULTS, **(options or {})}
    notes = []
    fmt = o["format"]
    if fmt == "auto":
        fmt = choose_format(media)
        notes.append(f"Shape chosen automatically: {fmt}")
    style = o["style"]
    if style == "auto":
        style = choose_style(media, music)
        notes.append(f"Editing pace chosen automatically: {'fast cuts' if style == 'fast' else 'smooth'}")
    look = o["look"]
    if look == "auto":
        look = "enhance"
        notes.append("Light and colour fixed automatically on every clip")
    tkind = o["transitions"]
    if tkind == "auto":
        tkind = choose_transitions(style, look)
    width, height = FORMATS[fmt]
    clip_len, photo_len, trans = PACES[style]
    if tkind == "cuts":
        trans = 0.0
    elif tkind == "cinematic":
        trans = max(trans, 0.5)

    # Music: cut lengths become whole numbers of beats so every cut lands on a beat.
    beat = None
    if music:
        if music.has_beat:
            beat = music.beat
            per = [1, 2, 4] if style == "fast" else [2, 4, 8]
            k = min(per, key=lambda n: abs(n * beat - clip_len))
            if trans:
                trans = min(trans, beat / 2)
            clip_len = photo_len = k * beat + trans
            notes.append(f"Cuts matched to the music beat ({round(music.bpm)} beats per minute)")
        else:
            notes.append("Music added; no steady beat was found, so cuts follow the footage instead")

    if o["length"] == "auto":
        target = choose_length(media)
        if music:
            target = min(target, music.duration - (music.first_beat if music.has_beat else 0) - 0.5)
        notes.append(f"Length chosen automatically: about {round(target)} seconds")
    else:
        target = float(o["length"])
        if music and target > music.duration:
            notes.append(f"The music is only {round(music.duration)} seconds, so the video stops there")
            target = music.duration - 0.5

    min_len = (beat + trans) if beat else min(1.0, clip_len * 0.6)
    # Every candidate section across all videos, plus each photo.
    cands = []
    for m in media:
        if m.kind == "image":
            cands.append((m, 0.0, photo_len, float(m.score[0]) if m.score.size else 0.5, 0, 1, 0.0))
        else:
            for t, d, sc, i0, i1, scene in _windows(m, clip_len, min_len):
                if beat:  # shorten to a whole number of beats
                    d = math.floor((d - trans) / beat + 1e-6) * beat + trans
                cands.append((m, t, d, sc, i0, i1, scene))
    if not cands:
        raise ValueError("None of the videos had a usable part long enough to keep")

    good = [c for c in cands if c[3] >= MIN_SCORE]
    if len(good) < len(cands):
        notes.append("Dark, blurry, shaky or empty moments were removed")
    if not good:
        good = cands
        notes.append("All footage was low quality, so the best available parts were used")

    chosen = []
    used_per_scene: dict[tuple, int] = {}
    total = 0.0

    def overlaps(c):
        return any(o_[0] is c[0] and c[1] < o_[1] + o_[2] + 0.5 and o_[1] < c[1] + c[2] + 0.5 for o_ in chosen)

    def effective(c):
        return c[3] - 0.12 * used_per_scene.get((id(c[0]), c[6]), 0)

    def take(c):
        nonlocal total
        chosen.append(c)
        used_per_scene[(id(c[0]), c[6])] = used_per_scene.get((id(c[0]), c[6]), 0) + 1
        total += c[2] - (trans if len(chosen) > 1 else 0)

    # First make sure every file gets its best moment in (if there's room)…
    best_each = {}
    for c in good:
        if id(c[0]) not in best_each or c[3] > best_each[id(c[0])][3]:
            best_each[id(c[0])] = c
    for c in sorted(best_each.values(), key=lambda c: -c[3]):
        if total + c[2] - trans <= target + 0.5 or not chosen:
            take(c)
    # …then keep adding the best remaining moments until the video is long enough.
    pool = [c for c in good if not any(c is o_ for o_ in chosen)]
    while total < target - 0.8 and pool:
        pool = [c for c in pool if not overlaps(c)]
        if not pool:
            break
        k = max(range(len(pool)), key=lambda i: effective(pool[i]))
        take(pool.pop(k))
    if total < target - 2:
        notes.append(f"There was only enough good footage for about {round(total)} seconds")

    # Tell the story in the order things were filmed / uploaded.
    chosen.sort(key=lambda c: (c[0].index, c[1]))
    slowmo = o["slowmo"] == "on" or (o["slowmo"] == "auto" and look == "cinematic")
    clips = []
    for c in chosen:
        m, t, d = c[0], c[1], c[2]
        speed = 1.0
        if slowmo and m.kind == "video" and float(np.mean(m.motion[c[4]:c[5]])) > SLOWMO_MOTION:
            speed = 0.5
            t = t + d * 0.25  # use the middle half of the section, played at half speed
        clips.append(Clip(media_index=m.index, name=m.name, kind=m.kind, start=round(t, 3), duration=round(d, 3),
                          score=round(c[3], 3), fill=_fill(m, width, height),
                          reasons=describe(m, c[4], c[5]) + (["slow motion"] if speed < 1 else []),
                          speed=speed, brightness=round(float(np.mean(m.brightness[c[4]:max(c[5], c[4] + 1)])), 3)))
    if any(c.speed < 1 for c in clips):
        notes.append("Slow motion added to the action shots")

    if len(clips) == 1:
        trans = 0.0
    if trans and not beat:
        trans = min(trans, min(c.duration for c in clips) / 2.5)
    if beat and clips:
        clips[-1].duration = round(clips[-1].duration - trans, 3)  # last clip has no transition after it
    transitions = effects.pick_transitions(tkind, max(len(clips) - 1, 0), seed=len(clips)) if trans else []
    total = sum(c.duration for c in clips) - trans * max(len(clips) - 1, 0)
    if look not in ("enhance", "none"):
        notes.append(f"Look: {effects.LOOK_NAMES[look]}")
    if trans:
        notes.append(f"{len(set(transitions))} kinds of transitions used")

    music_info = None
    if music:
        music_info = {"name": music.name, "start": music.first_beat if music.has_beat else 0.0,
                      "bpm": music.bpm, "synced": bool(beat)}
    title = (o.get("title") or "").strip()[:80]
    if title:
        notes.append(f'Title added: "{title}"')
    return Plan(format=fmt, width=width, height=height, style=style, look=look, transition_kind=tkind,
                transition=round(trans, 3), transitions=transitions, clips=clips, total=round(total, 3),
                notes=notes, title=title, music=music_info, original_volume=0.3 if music else 1.0)
