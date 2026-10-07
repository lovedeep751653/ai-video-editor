"""Understands a typed request such as "make it cinematic, 30 seconds, vertical,
smooth transitions, title 'Goa 2026'" and turns it into editing options.

The request is first read by the on-phone AI (genai.interpret), which understands
free-form wording; this built-in phrase list is the fallback if it can't answer."""

from __future__ import annotations

import re

from .effects import LOOK_NAMES

KEYS = ("format", "length", "style", "look", "transitions", "slowmo", "title", "captions", "caption_lang")


def _has(text: str, *words: str) -> bool:
    return any(re.search(rf"(?<![a-z]){re.escape(w)}(?![a-z])", text) for w in words)


def parse_keywords(request: str) -> dict:
    t = " " + request.lower().replace("’", "'") + " "
    o: dict = {}
    # Shape
    if _has(t, "9:16", "9x16", "vertical", "portrait", "reel", "reels", "shorts", "youtube short", "tiktok",
            "instagram story", "insta story", "whatsapp status", "stories"):
        o["format"] = "9:16"
    elif _has(t, "16:9", "16x9", "landscape", "horizontal", "widescreen", "wide", "youtube", "tv"):
        o["format"] = "16:9"
    elif _has(t, "1:1", "1x1", "square", "instagram post", "feed post"):
        o["format"] = "1:1"
    # Length
    m = re.search(r"(\d+(?:\.\d+)?)\s*(?:-|\s)?(min|mins|minute|minutes)\b", t)
    if m:
        o["length"] = str(min(1800, float(m.group(1)) * 60))
    else:
        m = re.search(r"(\d+)\s*(?:-|\s)?(s|sec|secs|second|seconds)\b", t)
        if m:
            o["length"] = str(min(1800, max(5, int(m.group(1)))))
    # Pace
    if _has(t, "fast", "quick", "energetic", "hype", "upbeat", "fast-paced", "fast paced", "punchy"):
        o["style"] = "fast"
    elif _has(t, "slow paced", "calm", "relaxed", "relaxing", "chill", "gentle", "peaceful", "smooth pace", "slow pace"):
        o["style"] = "smooth"
    # Look
    looks = [
        ("bw", ["black and white", "black & white", "b&w", "monochrome", "grayscale", "greyscale"]),
        ("vintage", ["vintage", "retro", "old film", "film grain", "old school", "90s", "80s"]),
        ("cinematic", ["cinematic", "movie", "film look", "filmic", "hollywood"]),
        ("dramatic", ["dramatic", "moody", "intense", "dark mood"]),
        ("vivid", ["vivid", "colorful", "colourful", "vibrant", "bright colors", "bright colours", "saturated"]),
        ("warm", ["warm", "golden", "sunset look", "cozy"]),
        ("cool", ["cool tone", "cool look", "cold", "blue tone", "icy"]),
        ("none", ["no filter", "natural colors", "natural colours", "original colors", "original colours"]),
    ]
    for key, words in looks:
        if _has(t, *words):
            o["look"] = key
            break
    # Transitions
    if _has(t, "no transitions", "no transition", "hard cuts", "jump cuts", "just cuts", "simple cuts"):
        o["transitions"] = "cuts"
    elif _has(t, "flashy", "creative transitions", "cool transitions", "dynamic transitions", "fancy transitions",
              "many transitions", "different transitions", "all transitions"):
        o["transitions"] = "dynamic"
    elif _has(t, "smooth transitions", "smooth transition", "soft transitions", "fade", "fades"):
        o["transitions"] = "soft"
    # Slow motion
    if _has(t, "no slow motion", "no slow-mo", "no slowmo", "without slow motion"):
        o["slowmo"] = "off"
    elif _has(t, "slow motion", "slow-mo", "slowmo", "slow mo"):
        o["slowmo"] = "on"
    # Captions / subtitles
    if _has(t, "no captions", "no subtitles", "without captions", "without subtitles"):
        o["captions"] = "off"
    elif _has(t, "captions", "subtitles", "subtitle", "subs"):
        o["captions"] = "on"
    if o.get("captions") == "on":
        for key, words in (("pa", ["punjabi", "gurmukhi"]), ("hinglish", ["hinglish", "roman"]),
                           ("hi", ["hindi", "devanagari"]), ("en", ["english"])):
            if _has(t, *words):
                o["caption_lang"] = key
                break
    # Title: text in quotes after "title"/"text"/"caption", or "title: ..."
    m = re.search(r"(?:title|text|heading)[^\"'“”‘]{0,25}[\"“‘']([^\"”’']{1,80})[\"”’']", request, re.I)
    if not m:
        m = re.search(r"(?:title)\s*(?:is|:|=)\s*([^,.\n]{1,80})", request, re.I)
    if m:
        o["title"] = m.group(1).strip()
    return o


def describe(o: dict, music: bool) -> list[str]:
    """Plain-language list of what was understood."""
    out = []
    if "format" in o:
        out.append({"9:16": "Tall 9:16", "16:9": "Wide 16:9", "1:1": "Square 1:1"}[o["format"]])
    if "length" in o:
        out.append(f"About {round(float(o['length']))} seconds long")
    if "style" in o:
        out.append("Fast-paced cuts" if o["style"] == "fast" else "Smooth, relaxed pace")
    if "look" in o:
        out.append(f"Look: {LOOK_NAMES.get(o['look'], o['look'])}")
    if "transitions" in o:
        out.append({"cuts": "Straight cuts, no transitions", "soft": "Smooth transitions",
                    "dynamic": "Creative, flashy transitions", "cinematic": "Cinematic transitions"}[o["transitions"]])
    if o.get("slowmo") == "on":
        out.append("Slow motion on action shots")
    if o.get("slowmo") == "off":
        out.append("No slow motion")
    if o.get("title"):
        out.append(f'Title: "{o["title"]}"')
    if o.get("captions") == "on":
        lang = {"auto": "", "en": " in English", "hi": " in Hindi", "pa": " in Punjabi",
                "hinglish": " in Hinglish"}[o.get("caption_lang", "auto")]
        out.append("Captions" + lang)
    if o.get("captions") == "off":
        out.append("No captions")
    return out


def wants_music(request: str) -> bool:
    return _has(" " + request.lower() + " ", "music", "beat", "beats", "song", "rhythm")


def clean(o: dict) -> dict:
    """Keeps only valid values (protects against anything unexpected from the AI service)."""
    valid = {
        "format": {"9:16", "16:9", "1:1"}, "style": {"smooth", "fast"}, "look": set(LOOK_NAMES) - {"enhance"},
        "transitions": {"cuts", "soft", "dynamic", "cinematic"}, "slowmo": {"on", "off"},
        "captions": {"on", "off"}, "caption_lang": {"en", "hi", "pa", "hinglish"},
    }
    out = {}
    for k, v in (o or {}).items():
        if v in (None, "", "auto"):
            continue
        if k in valid and v in valid[k]:
            out[k] = v
        elif k == "length":
            try:
                out[k] = str(min(1800.0, max(5.0, float(v))))
            except (TypeError, ValueError):
                pass
        elif k == "title" and isinstance(v, str):
            out[k] = v.strip()[:80]
    return out
