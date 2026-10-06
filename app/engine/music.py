"""Finds the beat of a music track so cuts can land exactly on it."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np

from . import ff

RATE = 22050
HOP = 512
NFFT = 1024


class MusicError(Exception):
    pass


@dataclass
class Music:
    path: Path
    name: str
    duration: float
    bpm: float
    first_beat: float  # seconds into the track where the beat grid starts
    has_beat: bool = True  # False when no steady beat was found (music is still used, cuts aren't synced)

    @property
    def beat(self) -> float:
        return 60.0 / self.bpm


def _onsets(y: np.ndarray) -> np.ndarray:
    n = 1 + (len(y) - NFFT) // HOP
    if n < 50:
        raise MusicError("the music is too short")
    idx = np.arange(NFFT)[None, :] + HOP * np.arange(n)[:, None]
    frames = y[idx] * np.hanning(NFFT)[None, :]
    mag = np.log1p(10 * np.abs(np.fft.rfft(frames, axis=1)))
    flux = np.maximum(0, np.diff(mag, axis=0)).sum(axis=1)
    flux = np.concatenate([[0], flux])
    flux -= np.convolve(flux, np.ones(16) / 16, mode="same")  # remove slow loudness changes
    flux = np.maximum(flux, 0)
    return flux / (flux.max() + 1e-9)


def _comb(env: np.ndarray, period: float) -> tuple[float, float]:
    """How strongly the onsets line up with a beat grid of this period (in frames); best phase."""
    best, best_phase = -1.0, 0.0
    grid = np.arange(0, len(env) - 1, period)
    for phase in np.arange(0, period, 0.5):
        pos = grid + phase
        pos = pos[pos < len(env) - 1]
        i = pos.astype(int)
        frac = pos - i
        v = float(np.mean(env[i] * (1 - frac) + env[i + 1] * frac))
        if v > best:
            best, best_phase = v, phase
    return best, best_phase


def analyze(path: Path) -> Music:
    info = ff.probe(path) or {}
    duration = float((info.get("format") or {}).get("duration") or 0)
    try:
        # Two minutes is plenty to find the beat.
        data = ff.raw(["-t", "120", "-i", str(path), "-vn", "-sn", "-ac", "1", "-ar", str(RATE), "-f", "f32le"])
    except ff.FFError:
        data = b""
    if len(data) < RATE * 4 * 3:
        raise MusicError(f"{path.name}: the music file couldn't be read (or is shorter than 3 seconds)")
    y = np.frombuffer(data[: len(data) // 4 * 4], dtype=np.float32).astype(np.float64)
    duration = max(duration, len(y) / RATE)
    env = _onsets(y)
    fps = RATE / HOP

    # Tempo: autocorrelation between 70 and 180 BPM, gently preferring ~120 BPM.
    ac = np.correlate(env, env, mode="full")[len(env) - 1:]
    lags = np.arange(int(fps * 60 / 180), int(fps * 60 / 70) + 1)
    bpms = 60 * fps / lags
    weight = np.exp(-0.5 * (np.log2(bpms / 120) / 0.9) ** 2)
    lag = int(lags[np.argmax(ac[lags] * weight)])

    # Refine to a fractional period so the grid doesn't drift over a long video.
    best_score, best_period, best_phase = -1.0, float(lag), 0.0
    for period in np.arange(lag - 1, lag + 1.001, 0.02):
        s, ph = _comb(env, period)
        if s > best_score:
            best_score, best_period, best_phase = s, period, ph
    bpm = 60 * fps / best_period
    # Confidence: the real beat grid must line up far better than a grid that is clearly off-tempo.
    off = np.mean([_comb(env, q)[0] for q in np.linspace(best_period * 0.71, best_period * 0.79, 5)])
    has_beat = best_score > 2.2 * off
    return Music(path=path, name=path.name, duration=duration, bpm=round(float(bpm), 2),
                 first_beat=round(float(best_phase / fps), 3) if has_beat else 0.0, has_beat=bool(has_beat))
