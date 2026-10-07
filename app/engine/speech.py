"""Speech to text on the phone itself, for captions. Uses Meta's Omnilingual ASR
model (Hindi, Punjabi, English and 1600 more languages) through sherpa-onnx.
No internet and no account needed."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import numpy as np

from . import ff

RATE = 16000
HOP = 320  # 20 ms
SEG_MAX = 20.0  # seconds of audio given to the model at once
MODEL_DIR = os.environ.get("EDITOR_SPEECH_MODEL", "")
_desktop = None


class SpeechError(Exception):
    pass


def _android_bridge():
    from java import jclass  # Chaquopy
    return jclass("com.lovedeep.aivideoeditor.Speech")


def _recognize(pcm: np.ndarray) -> dict:
    """{"text", "tokens", "timestamps"} for 16 kHz mono float samples."""
    global _desktop
    if os.environ.get("SPEECH_FAKE"):  # tests only: canned answer without the 365 MB model
        return json.loads(os.environ["SPEECH_FAKE"])
    if ff.ANDROID:
        fd, path = tempfile.mkstemp(prefix="speech_", suffix=".f32", dir=ff._tmpdir())
        os.close(fd)
        try:
            Path(path).write_bytes(pcm.astype("<f4").tobytes())
            out = str(_android_bridge().recognize(path))
        finally:
            os.unlink(path)
        res = json.loads(out)
        if res.get("error"):
            raise SpeechError(res["error"])
        return res
    if _desktop is None:
        try:
            import sherpa_onnx
        except ImportError:
            raise SpeechError("Speech recognition isn't installed on this computer.") from None
        model = Path(MODEL_DIR)
        if not (model / "model.int8.onnx").exists():
            raise SpeechError("The speech model isn't installed (set EDITOR_SPEECH_MODEL).")
        _desktop = sherpa_onnx.OfflineRecognizer.from_omnilingual_asr_ctc(
            model=str(model / "model.int8.onnx"), tokens=str(model / "tokens.txt"), num_threads=4)
    s = _desktop.create_stream()
    s.accept_waveform(RATE, pcm.astype(np.float32))
    _desktop.decode_stream(s)
    r = s.result
    return {"text": r.text, "tokens": list(r.tokens), "timestamps": [float(t) for t in r.timestamps]}


def _segments(pcm: np.ndarray) -> list[tuple[int, int]]:
    """Stretches of speech (sample ranges), each at most SEG_MAX long, split at the quietest moments."""
    n = len(pcm) // HOP
    if n < 5:
        return []
    db = 20 * np.log10(np.sqrt((pcm[: n * HOP].reshape(n, HOP) ** 2).mean(axis=1)) + 1e-6)
    voiced = db > max(np.percentile(db, 25) + 8, -48)
    runs, start, gap = [], None, 0
    for i, v in enumerate(voiced):
        if v:
            start = i if start is None else start
            gap = 0
        elif start is not None:
            gap += 1
            if gap > 20:  # 0.4 s of quiet ends a stretch
                runs.append([start, i - gap + 1])
                start, gap = None, 0
    if start is not None:
        runs.append([start, n - gap])
    out = []
    limit = int(SEG_MAX / 0.02)
    for a, b in runs:
        if b - a < 8:  # shorter than 0.16 s: a click, not a word
            continue
        while b - a > limit:
            lo = a + limit // 2
            cut = lo + int(np.argmin(db[lo:a + limit]))
            out.append((a, cut))
            a = cut
        out.append((a, b))
    pad = 8  # 0.16 s either side so first/last sounds aren't clipped
    return [(max(0, a - pad) * HOP, min(n, b + pad) * HOP) for a, b in out]


def _words(res: dict, offset: float) -> list[tuple[str, float, float]]:
    tokens, times = res.get("tokens") or [], res.get("timestamps") or []
    words, cur, t0, last = [], "", None, None
    for tok, t in zip(tokens, times):
        piece = tok.replace("▁", " ")
        if piece.startswith(" ") and cur.strip():
            words.append((cur.strip(), t0, last))
            cur, t0 = "", None
        cur += piece
        if piece.strip():
            t0 = t if t0 is None else t0
            last = t
    if cur.strip():
        words.append((cur.strip(), t0, last))
    if not words and res.get("text", "").strip():  # no timing: spread words evenly over the stretch
        ws = res["text"].split()
        dur = float(res.get("duration") or len(ws) * 0.4)
        words = [(w, i * dur / len(ws), (i + 1) * dur / len(ws)) for i, w in enumerate(ws)]
    out = []
    for i, (w, a, b) in enumerate(words):
        nxt = words[i + 1][1] if i + 1 < len(words) else b + 0.35
        end = min(max(b + 0.12, a + 0.15), nxt)
        out.append((w, round(offset + a, 2), round(offset + end, 2)))
    return out


def _lines(words: list[tuple[str, float, float]]) -> list[dict]:
    """Groups words into short caption lines (up to 7 words or 3.5 s, new line after a pause)."""
    lines, cur = [], []
    for w in words:
        if cur and (len(cur) >= 7 or w[2] - cur[0][1] > 3.5 or w[1] - cur[-1][2] > 0.6):
            lines.append(cur)
            cur = []
        cur.append(w)
    if cur:
        lines.append(cur)
    return [{"start": ln[0][1], "end": ln[-1][2], "text": " ".join(w[0] for w in ln)} for ln in lines]


def available() -> bool:
    return bool(os.environ.get("SPEECH_FAKE") or ff.ANDROID or (Path(MODEL_DIR) / "model.int8.onnx").exists())


def transcribe(audio: Path, progress=None) -> list[dict]:
    """Timed caption lines [{start, end, text}] for the speech in an audio or video file."""
    data = ff.raw(["-i", str(audio), "-vn", "-ac", "1", "-ar", str(RATE), "-f", "f32le"])
    pcm = np.frombuffer(data[: len(data) // 4 * 4], dtype="<f4")
    segs = _segments(pcm)
    words = []
    for i, (a, b) in enumerate(segs):
        ff.check_cancel()
        res = _recognize(pcm[a:b])
        res.setdefault("duration", (b - a) / RATE)
        words += _words(res, a / RATE)
        if progress:
            progress((i + 1) / len(segs))
    return _lines(words)
