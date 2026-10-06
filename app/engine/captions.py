"""Captions (subtitles) burned into the video: 45 styles, in English, Hindi
(Devanagari) and Punjabi (Gurmukhi) scripts, with fonts bundled in app/fonts.

The words and their timing come from Google AI listening to the speech
(see genai.transcribe). This file turns those timed lines into a styled ASS
subtitle file that FFmpeg draws onto the video."""

from __future__ import annotations

import re
from pathlib import Path

import os

FONTS_DIR = Path(os.environ.get("EDITOR_FONTS") or Path(__file__).resolve().parents[1] / "fonts")

# Font family for each font "role" and script (all fonts are in app/fonts).
FONTS = {
    "bold":      {"latin": "Poppins ExtraBold", "deva": "Mukta ExtraBold", "guru": "Mukta Mahee ExtraBold"},
    "clean":     {"latin": "Poppins SemiBold", "deva": "Mukta Bold", "guru": "Mukta Mahee Bold"},
    "rounded":   {"latin": "Baloo 2", "deva": "Baloo 2", "guru": "Baloo Paaji 2"},
    "display":   {"latin": "Anton", "deva": "Mukta ExtraBold", "guru": "Mukta Mahee ExtraBold"},
    "condensed": {"latin": "Bebas Neue", "deva": "Mukta ExtraBold", "guru": "Mukta Mahee ExtraBold"},
    "serif":     {"latin": "Noto Serif Devanagari", "deva": "Noto Serif Devanagari", "guru": "Noto Serif Gurmukhi"},
    "script":    {"latin": "Pacifico", "deva": "Baloo 2", "guru": "Baloo Paaji 2"},
}

# 15 base looks: (id, name, font role, text colour, highlight colour, outline colour, outline, shadow, box, caps)
BASES = [
    ("classic", "Classic White", "clean", "FFFFFF", "FFE14D", "000000", 3, 1, False, False),
    ("yellow", "Bold Yellow", "bold", "FFE14D", "FFFFFF", "000000", 5, 2, False, True),
    ("blackbox", "Black Box", "clean", "FFFFFF", "FFE14D", "000000", 10, 0, True, False),
    ("whitebox", "White Box", "bold", "111111", "E11D48", "FFFFFF", 10, 0, True, False),
    ("neonpink", "Neon Pink", "bold", "FF4FD8", "FFFFFF", "3A0035", 4, 3, False, True),
    ("neongreen", "Neon Green", "bold", "39FF14", "FFFFFF", "002B00", 4, 3, False, True),
    ("cinema", "Cinema", "serif", "F5F0E6", "FFD27A", "000000", 2, 1, False, False),
    ("comic", "Comic Rounded", "rounded", "FFFFFF", "34D3FF", "1B1464", 6, 3, False, False),
    ("fire", "Fire Orange", "display", "FF8A00", "FFE14D", "3B0A00", 5, 2, False, True),
    ("ice", "Ice Blue", "bold", "BDEBFF", "FFFFFF", "00264D", 5, 2, False, False),
    ("gold", "Gold Luxury", "serif", "E8C25A", "FFF3C4", "2B1D00", 3, 2, False, False),
    ("red", "Red Alert", "display", "FFFFFF", "FFE14D", "D7263D", 10, 0, True, True),
    ("pastel", "Pastel Purple", "rounded", "E9D5FF", "FFFFFF", "4C1D95", 5, 2, False, False),
    ("hand", "Handwritten", "script", "FFFFFF", "FFD1DC", "000000", 3, 2, False, False),
    ("news", "News Ticker", "condensed", "FFFFFF", "FFE14D", "1E3A8A", 10, 0, True, True),
]
ANIMS = [("simple", "Simple fade"), ("pop", "Pop-in"), ("karaoke", "Word highlight")]

STYLES = {f"{b[0]}-{a[0]}": {"name": f"{b[1]} · {a[1]}", "base": b, "anim": a[0]} for b in BASES for a in ANIMS}
DEFAULT_STYLE = "yellow-karaoke"

LANGS = {
    "auto": "Same as spoken",
    "en": "English",
    "hi": "Hindi (हिंदी)",
    "pa": "Punjabi (ਪੰਜਾਬੀ)",
    "hinglish": "Hinglish (Roman letters)",
}
POSITIONS = ("bottom", "middle", "top")


def script_of(text: str) -> str:
    if re.search(r"[਀-੿]", text):
        return "guru"
    if re.search(r"[ऀ-ॿ]", text):
        return "deva"
    return "latin"


def _ass_color(hex_rgb: str, alpha: str = "00") -> str:
    r, g, b = hex_rgb[0:2], hex_rgb[2:4], hex_rgb[4:6]
    return f"&H{alpha}{b}{g}{r}".upper()


def _ts(t: float) -> str:
    t = max(t, 0)
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h)}:{int(m):02d}:{s:05.2f}"


def _escape(text: str) -> str:
    return text.replace("\\", "/").replace("{", "(").replace("}", ")").replace("\n", " ")


def split_lines(segments: list[dict], width: int, height: int) -> list[dict]:
    """Breaks long spoken sentences into short caption lines that fit the screen."""
    max_words = 4 if width < height else 6 if width == height else 8
    out = []
    for seg in segments:
        words = seg["text"].split()
        if not words:
            continue
        start, end = float(seg["start"]), float(seg["end"])
        if end <= start:
            end = start + 0.4 * len(words)
        chunks = [words[i:i + max_words] for i in range(0, len(words), max_words)]
        total_chars = sum(len(w) + 1 for w in words)
        t = start
        for ch in chunks:
            share = sum(len(w) + 1 for w in ch) / total_chars
            d = (end - start) * share
            out.append({"start": t, "end": t + d, "words": ch})
            t += d
    return out


def _caption_parts(segments: list[dict], style_id: str, width: int, height: int, position: str = "bottom",
                   duration: float | None = None) -> tuple[str, list[tuple[float, float, str]]]:
    """The caption style line and the caption events (start, end, text)."""
    style = STYLES.get(style_id) or STYLES[DEFAULT_STYLE]
    _, _, role, text_c, hi_c, out_c, outline, shadow, box, caps = style["base"]
    anim = style["anim"]
    lines = split_lines(segments, width, height)
    sample = " ".join(" ".join(l["words"]) for l in lines)
    font = FONTS[role][script_of(sample)]
    size = int(min(width, height) * (0.082 if role in ("display", "condensed") else 0.07))
    align = {"bottom": 2, "middle": 5, "top": 8}[position if position in POSITIONS else "bottom"]
    margin_v = int(height * (0.16 if width < height else 0.08)) if align != 5 else 0
    scale = min(width, height) / 1080
    o, sh = max(1, round(outline * scale)), round(shadow * scale)
    border_style = 3 if box else 1
    # Karaoke: words start in the normal colour (Secondary) and switch to the highlight (Primary) as spoken.
    primary, secondary = (hi_c, text_c) if anim == "karaoke" else (text_c, hi_c)
    style_line = (f"Style: Cap,{font},{size},{_ass_color(primary)},{_ass_color(secondary)},{_ass_color(out_c)},"
                  f"{_ass_color('000000', '80')},0,0,0,0,100,100,0,0,{border_style},{o},{sh},{align},"
                  f"{int(width * 0.06)},{int(width * 0.06)},{margin_v},1")
    events = []
    for ln in lines:
        start, end = ln["start"], ln["end"]
        if duration is not None:
            if start >= duration:
                continue
            end = min(end, duration)
        words = [w.upper() if caps and script_of(w) == "latin" else w for w in ln["words"]]
        words = [_escape(w) for w in words]
        if anim == "karaoke":
            total = max(end - start, 0.1)
            chars = sum(len(w) for w in words) or 1
            text = " ".join(f"{{\\k{max(1, round(100 * total * len(w) / chars))}}}{w}" for w in words)
        elif anim == "pop":
            text = "{\\fad(60,80)\\fscx70\\fscy70\\t(0,120,\\fscx110\\fscy110)\\t(120,220,\\fscx100\\fscy100)}" \
                   + " ".join(words)
        else:
            text = "{\\fad(150,120)}" + " ".join(words)
        events.append((start, end, text))
    return style_line, events


def _header(width: int, height: int, styles: list[str]) -> str:
    return f"""[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
""" + "\n".join(styles) + """

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def build_ass(segments: list[dict], style_id: str, width: int, height: int, position: str = "bottom",
              duration: float | None = None) -> str:
    style_line, events = _caption_parts(segments, style_id, width, height, position, duration)
    return _header(width, height, [style_line]) + "\n".join(
        f"Dialogue: 0,{_ts(a)},{_ts(b)},Cap,,0,0,0,,{t}" for a, b, t in events) + "\n"


TEXT_FONT = {"latin": "Poppins ExtraBold", "deva": "Mukta ExtraBold", "guru": "Mukta Mahee ExtraBold"}


class Overlay:
    """Everything drawn on top of the finished video: title, text and captions.
    It can be cut into pieces matching parts of the video (see `ass`)."""

    def __init__(self, width: int, height: int):
        self.width, self.height = width, height
        u = min(width, height)
        self.styles = [
            f"Style: Title,Poppins ExtraBold,{int(u * 0.085)},&H00FFFFFF,&H00FFFFFF,&H99000000,&H80000000,"
            f"0,0,0,0,100,100,0,0,1,{max(2, int(u * 0.005))},{max(1, int(u * 0.002))},5,"
            f"{int(width * 0.06)},{int(width * 0.06)},0,1",
            f"Style: Text,Poppins ExtraBold,{int(u * 0.062)},&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,"
            f"0,0,0,0,100,100,0,0,3,{max(6, int(u * 0.012))},0,2,{int(width * 0.07)},{int(width * 0.07)},"
            f"{int(height * 0.12)},1",
        ]
        self.events: list[tuple[float, float, str, str]] = []  # (start, end, style, text)

    def add_title(self, text: str, duration: float = 3.0) -> None:
        text = _escape(text.strip())
        if text:
            self.events.append((0.0, duration, "Title", f"{{\\fn{TEXT_FONT[script_of(text)]}\\fad(500,500)}}{text}"))

    def add_text(self, text: str, start: float, end: float, position: str = "bottom") -> None:
        text = _escape(text.strip())
        if not text or end <= start:
            return
        an = {"top": 8, "middle": 5, "bottom": 2}.get(position, 2)
        self.events.append((start, end, "Text", f"{{\\an{an}\\fn{TEXT_FONT[script_of(text)]}\\fad(200,200)}}{text}"))

    def add_captions(self, segments: list[dict], style_id: str, position: str, duration: float) -> None:
        style_line, events = _caption_parts(segments, style_id, self.width, self.height, position, duration)
        self.styles = [x for x in self.styles if not x.startswith("Style: Cap,")] + [style_line]
        self.events += [(a, b, "Cap", t) for a, b, t in events]

    def empty(self, start: float = 0.0, end: float | None = None) -> bool:
        return not any(b > start and (end is None or a < end) for a, b, _, _ in self.events)

    def ass(self, start: float = 0.0, end: float | None = None) -> str:
        """The ASS file for the stretch start..end of the video, with times made relative to start."""
        lines = []
        for a, b, style, text in self.events:
            if b <= start or (end is not None and a >= end):
                continue
            a2, b2 = max(a, start) - start, (min(b, end) if end is not None else b) - start
            if a < start:  # continues from the previous part: no second fade-in, karaoke carries on
                text = re.sub(r"\\fad\((\d+),", r"\\fad(0,", text)
                if "\\k" in text:
                    text = "{\\k%d}" % round((start - a) * 100) + text
            layer = {"Cap": 0, "Text": 1, "Title": 2}[style]
            lines.append(f"Dialogue: {layer},{_ts(a2)},{_ts(b2)},{style},,0,0,0,,{text}")
        return _header(self.width, self.height, self.styles) + "\n".join(lines) + "\n"


def style_list() -> list[dict]:
    return [{"id": k, "name": v["name"]} for k, v in STYLES.items()]


def snap_to_speech(lines: list[dict], audio: Path) -> list[dict]:
    """Nudges caption timing onto where speech actually starts/stops (AI timestamps can be a bit off)."""
    import numpy as np

    from . import ff
    try:
        data = ff.raw(["-i", str(audio), "-ac", "1", "-ar", "16000", "-f", "s16le"])
    except ff.FFError:
        return lines
    pcm = np.frombuffer(data[: len(data) // 2 * 2], dtype=np.int16).astype(np.float64) / 32768
    hop = 320  # 20 ms
    n = len(pcm) // hop
    if n < 10:
        return lines
    rms = np.sqrt((pcm[: n * hop].reshape(n, hop) ** 2).mean(axis=1))
    db = 20 * np.log10(rms + 1e-6)
    voiced = db > max(np.percentile(db, 30) + 6, -50)
    out = []
    for ln in lines:
        s, e = int(ln["start"] / 0.02), int(ln["end"] / 0.02)
        s, e = min(max(s, 0), n - 1), min(max(e, 1), n)
        if not voiced[s]:  # starts in silence → move to the next voice within 0.8 s
            nxt = np.flatnonzero(voiced[s:min(n, s + 40)])
            if nxt.size:
                s += int(nxt[0])
        if e - 1 > s and not voiced[e - 1]:  # ends in silence → pull back to the last voice within 0.8 s
            prv = np.flatnonzero(voiced[max(s + 1, e - 40):e])
            if prv.size:
                e = max(s + 1, e - 40) + int(prv[-1]) + 1
        start, end = s * 0.02, max(e * 0.02, s * 0.02 + 0.5)
        out.append({**ln, "start": round(start, 2), "end": round(end + 0.15, 2)})
    for a, b in zip(out, out[1:]):  # never overlap the next line
        a["end"] = min(a["end"], b["start"])
    return out
