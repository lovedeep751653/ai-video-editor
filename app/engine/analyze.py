"""Looks at every uploaded video and photo and measures how good each moment is.

For videos we sample a few frames per second and measure, for each moment:
sharpness, lighting, contrast, movement, camera shake, sound level and scene
changes. For photos we measure the same picture-quality values once.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".tif", ".tiff", ".heic", ".heif"}

GRID = 128  # analysis frames are shrunk to GRID x GRID grey pixels
AUDIO_RATE = 8000


class MediaError(Exception):
    """A file could not be read as a video or photo."""


@dataclass
class Media:
    path: Path
    name: str
    index: int  # upload order
    kind: str  # "video" or "image"
    width: int
    height: int
    duration: float = 0.0
    has_audio: bool = False
    # Per-sample measurements (videos only). times[i] is the moment in seconds.
    times: np.ndarray = field(default_factory=lambda: np.zeros(0))
    brightness: np.ndarray = field(default_factory=lambda: np.zeros(0))
    contrast: np.ndarray = field(default_factory=lambda: np.zeros(0))
    sharpness: np.ndarray = field(default_factory=lambda: np.zeros(0))
    motion: np.ndarray = field(default_factory=lambda: np.zeros(0))
    loudness: np.ndarray = field(default_factory=lambda: np.zeros(0))
    scene_cuts: list[float] = field(default_factory=list)
    score: np.ndarray = field(default_factory=lambda: np.zeros(0))  # filled by score_all


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True)


def probe(path: Path, index: int) -> Media:
    res = _run(["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", "-show_format", str(path)])
    if res.returncode != 0:
        raise MediaError(f"{path.name}: this file type can't be opened")
    info = json.loads(res.stdout or "{}")
    streams = info.get("streams", [])
    vstreams = [s for s in streams if s.get("codec_type") == "video"]
    if not vstreams:
        raise MediaError(f"{path.name}: no picture found in this file")
    v = vstreams[0]
    w, h = int(v.get("width") or 0), int(v.get("height") or 0)
    if not w or not h:
        raise MediaError(f"{path.name}: picture size unknown")

    rotation = 0
    for sd in v.get("side_data_list", []) or []:
        if "rotation" in sd:
            rotation = int(float(sd["rotation"]))
    if not rotation and "rotate" in (v.get("tags") or {}):
        rotation = int(float(v["tags"]["rotate"]))
    if abs(rotation) % 180 == 90:
        w, h = h, w

    duration = float(info.get("format", {}).get("duration") or v.get("duration") or 0)
    frames = int(v.get("nb_frames") or 0)
    ext = path.suffix.lower()
    is_image = (
        ext in IMAGE_EXTS
        or v.get("codec_name") in {"mjpeg", "png", "webp", "bmp", "tiff", "gif"} and (duration < 0.2 or frames <= 1)
    )
    if ext == ".gif" and duration > 0.5:
        is_image = False
    if is_image:
        return Media(path=path, name=path.name, index=index, kind="image", width=w, height=h)
    if duration < 0.5:
        raise MediaError(f"{path.name}: video is too short (under half a second)")
    has_audio = any(s.get("codec_type") == "audio" for s in streams)
    return Media(path=path, name=path.name, index=index, kind="video", width=w, height=h,
                 duration=duration, has_audio=has_audio)


def _frame_metrics(f: np.ndarray) -> tuple[float, float, float, np.ndarray]:
    """brightness, contrast, sharpness and a small histogram for one grey frame (0..1)."""
    lap = 4 * f[1:-1, 1:-1] - f[:-2, 1:-1] - f[2:, 1:-1] - f[1:-1, :-2] - f[1:-1, 2:]
    hist = np.histogram(f, bins=16, range=(0, 1))[0].astype(np.float64)
    hist /= max(hist.sum(), 1)
    return float(f.mean()), float(f.std()), float(lap.var()), hist


def analyze_image(m: Media) -> None:
    cmd = ["ffmpeg", "-v", "error", "-i", str(m.path), "-frames:v", "1",
           "-vf", f"scale={GRID}:{GRID}:flags=area,format=gray", "-f", "rawvideo", "-"]
    res = subprocess.run(cmd, capture_output=True)
    if res.returncode != 0 or len(res.stdout) < GRID * GRID:
        raise MediaError(f"{m.name}: this photo format can't be read")
    f = np.frombuffer(res.stdout[: GRID * GRID], dtype=np.uint8).reshape(GRID, GRID) / 255.0
    b, c, s, _ = _frame_metrics(f)
    m.times = np.array([0.0])
    m.brightness, m.contrast, m.sharpness = np.array([b]), np.array([c]), np.array([s])
    m.motion = np.array([0.0])
    m.loudness = np.array([0.0])


def analyze_video(m: Media, progress=None) -> None:
    fps = 4.0 if m.duration <= 300 else 2.0 if m.duration <= 1200 else 1.0
    cmd = ["ffmpeg", "-v", "error", "-threads", "0", "-i", str(m.path), "-an",
           "-vf", f"fps={fps},scale={GRID}:{GRID}:flags=area,format=gray",
           "-f", "rawvideo", "-"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    size = GRID * GRID
    bright, contr, sharp, motion, cuts = [], [], [], [], []
    prev, prev_hist = None, None
    expected = max(1, int(m.duration * fps))
    i = 0
    while True:
        buf = proc.stdout.read(size)
        if len(buf) < size:
            break
        f = np.frombuffer(buf, dtype=np.uint8).reshape(GRID, GRID) / 255.0
        b, c, s, hist = _frame_metrics(f)
        mo = float(np.abs(f - prev).mean()) if prev is not None else 0.0
        if prev_hist is not None and np.abs(hist - prev_hist).sum() > 0.6 and mo > 0.08:
            cuts.append(i / fps)
        bright.append(b), contr.append(c), sharp.append(s), motion.append(mo)
        prev, prev_hist = f, hist
        i += 1
        if progress and i % 20 == 0:
            progress(min(1.0, i / expected))
    proc.wait()
    if not bright:
        raise MediaError(f"{m.name}: the video could not be decoded")
    if len(motion) > 1:
        motion[0] = motion[1]
    # A scene cut makes a big "motion" spike that isn't real movement.
    for t in cuts:
        k = int(round(t * fps))
        if 0 < k < len(motion) - 1:
            motion[k] = (motion[k - 1] + motion[k + 1]) / 2
    m.times = np.arange(len(bright)) / fps
    m.brightness, m.contrast = np.array(bright), np.array(contr)
    m.sharpness, m.motion = np.array(sharp), np.array(motion)
    m.scene_cuts = cuts
    m.loudness = _loudness(m, fps, len(bright))


def _loudness(m: Media, fps: float, n: int) -> np.ndarray:
    if not m.has_audio:
        return np.zeros(n)
    cmd = ["ffmpeg", "-v", "error", "-i", str(m.path), "-vn", "-ac", "1", "-ar", str(AUDIO_RATE),
           "-f", "s16le", "-"]
    res = subprocess.run(cmd, capture_output=True)
    pcm = np.frombuffer(res.stdout[: len(res.stdout) // 2 * 2], dtype=np.int16).astype(np.float64) / 32768
    step = int(AUDIO_RATE / fps)
    out = np.zeros(n)
    for i in range(n):
        chunk = pcm[i * step:(i + 1) * step]
        if len(chunk):
            out[i] = 20 * np.log10(np.sqrt(np.mean(chunk ** 2)) + 1e-5)  # dBFS, -100 .. 0
    return out


def _rank(values: np.ndarray) -> np.ndarray:
    """0..1 position of each value among all values (robust to outliers)."""
    if len(values) == 0:
        return values
    order = values.argsort(kind="stable")
    ranks = np.empty(len(values))
    ranks[order] = np.arange(len(values))
    return ranks / max(len(values) - 1, 1)


def score_all(media: list[Media]) -> None:
    """Gives every moment of every file a quality score between 0 and 1.

    Sharpness and sound are ranked against all uploaded media together, so the
    sharpest, liveliest moments across the whole upload win.
    """
    sizes = [len(m.times) for m in media]
    sharp = _rank(np.log1p(np.concatenate([m.sharpness * 1e4 for m in media])))
    vids = [m for m in media if m.kind == "video" and m.has_audio]
    loud_all = np.concatenate([m.loudness for m in vids]) if vids else np.zeros(0)
    loud_rank = _rank(loud_all)
    pos, lpos = 0, 0
    for m, n in zip(media, sizes):
        s = sharp[pos:pos + n]
        pos += n
        exposure = np.clip(1 - np.abs(m.brightness - 0.48) * 2.2, 0, 1)
        exposure[(m.brightness < 0.08) | (m.brightness > 0.95)] = 0
        contrast = np.clip(m.contrast / 0.22, 0, 1)
        if m.kind == "image":
            interest, shake, audio = np.full(n, 0.55), np.zeros(n), np.full(n, 0.5)
        else:
            interest = np.clip(m.motion / 0.035, 0, 1)
            shake = np.clip((m.motion - 0.12) / 0.15, 0, 1)
            if m.has_audio:
                audio = loud_rank[lpos:lpos + n]
                lpos += n
            else:
                audio = np.full(n, 0.5)
        m.score = np.clip(
            0.35 * s + 0.22 * interest + 0.2 * exposure + 0.13 * contrast + 0.1 * audio - 0.45 * shake, 0, 1)


def describe(m: Media, i0: int, i1: int) -> list[str]:
    """Plain-language reasons a section was picked."""
    reasons = []
    sl = slice(i0, max(i1, i0 + 1))
    if m.score.size and np.mean(m.score[sl]) > 0.6:
        reasons.append("high overall quality")
    if np.mean(m.sharpness[sl]) >= np.median(m.sharpness):
        reasons.append("sharp picture")
    b = np.mean(m.brightness[sl])
    if 0.3 < b < 0.7:
        reasons.append("good lighting")
    if m.kind == "video":
        mo = np.mean(m.motion[sl])
        if mo > 0.03:
            reasons.append("lots of action")
        elif mo > 0.01:
            reasons.append("steady shot")
        if m.has_audio and np.mean(m.loudness[sl]) > np.median(m.loudness) + 3:
            reasons.append("lively sound")
    return reasons or ["best available moment"]
