"""Google AI (Gemini API) connection: understands typed requests, creates
pictures (Imagen / Gemini image models) and video clips (Veo) from a prompt.

Needs a Google AI Studio API key. The best available model for each job is
picked automatically from the models the key can use."""

from __future__ import annotations

import base64
import json
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE = os.environ.get("GENAI_BASE", "https://generativelanguage.googleapis.com/v1beta")
TIMEOUT = 120


class AIError(Exception):
    pass


def _request(key: str, method: str, url: str, body: dict | None = None, raw: bool = False, timeout=TIMEOUT):
    if not url.startswith("http"):
        url = f"{BASE}/{url.lstrip('/')}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"x-goog-api-key": key, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            payload = r.read()
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")
        try:
            detail = json.loads(detail)["error"]["message"]
        except Exception:
            detail = detail[:300]
        raise AIError(_friendly(e.code, detail)) from None
    except urllib.error.URLError as e:
        raise AIError(f"Couldn't reach Google AI ({e.reason}).") from None
    return payload if raw else json.loads(payload or b"{}")


def _friendly(code: int, detail: str) -> str:
    low = detail.lower()
    if "api key" in low or "api_key" in low or code == 401 or (code == 403 and "permission" in low):
        return "Google AI refused the key. Please check it was copied fully. " + detail
    if code == 429 or "quota" in low or "billing" in low:
        return ("Google AI says this key has run out of free use or needs billing turned on for this feature. "
                + detail)
    return f"Google AI error: {detail}"


def _version(name: str) -> tuple:
    nums = re.findall(r"\d+(?:\.\d+)?", name)
    return tuple(float(n) for n in nums[:2]) or (0.0,)


def pick_models(models: list[dict]) -> dict:
    """Chooses the best text, image and video model from what the key can use."""
    def methods(m):
        return set(m.get("supportedGenerationMethods") or m.get("supported_actions") or [])

    names = [(m["name"].split("/", 1)[-1], methods(m)) for m in models if "name" in m]
    bad_text = ("image", "tts", "live", "audio", "embedding", "aqa", "vision", "exp", "preview-tts", "robotics",
                "computer", "nano", "gemma", "learnlm")
    text = [n for n, ms in names if n.startswith("gemini") and "flash" in n and "generateContent" in ms
            and not any(b in n for b in bad_text)]
    imagen = [n for n, ms in names if n.startswith("imagen") and "predict" in ms]
    gem_img = [n for n, ms in names if n.startswith("gemini") and "image" in n and "generateContent" in ms]
    veo = [n for n, ms in names if n.startswith("veo") and "predictLongRunning" in ms]

    def best(c, prefer=None):
        if not c:
            return None
        return max(c, key=lambda n: (prefer(n) if prefer else 0, "preview" not in n, _version(n), -len(n)))

    return {
        "text": best(text, lambda n: "lite" not in n),
        "image": best(imagen, lambda n: "ultra" not in n) or best(gem_img),
        # "fast" Veo models cost much less per second and are still high quality.
        "video": best(veo, lambda n: "fast" in n),
    }


def check_key(key: str) -> dict:
    out, token = [], ""
    for _ in range(10):
        res = _request(key, "GET", f"models?pageSize=1000{'&pageToken=' + token if token else ''}", timeout=30)
        out += res.get("models", [])
        token = res.get("nextPageToken", "")
        if not token:
            break
    if not out:
        raise AIError("This key works but has no AI models available.")
    return pick_models(out)


def _json_from(res: dict):
    try:
        text = "".join(p.get("text", "") for p in res["candidates"][0]["content"]["parts"])
        return json.loads(text)
    except Exception:
        raise AIError("Google AI gave an answer that couldn't be understood.") from None


def interpret(key: str, model: str, request: str) -> dict:
    """Turns a typed editing request into option values."""
    schema = {
        "type": "OBJECT",
        "properties": {
            "format": {"type": "STRING", "enum": ["auto", "9:16", "16:9", "1:1"]},
            "length": {"type": "NUMBER", "description": "seconds; 0 if not mentioned"},
            "style": {"type": "STRING", "enum": ["auto", "smooth", "fast"]},
            "look": {"type": "STRING", "enum": ["auto", "none", "cinematic", "vivid", "warm", "cool", "vintage",
                                                "bw", "dramatic"]},
            "transitions": {"type": "STRING", "enum": ["auto", "cuts", "soft", "dynamic", "cinematic"]},
            "slowmo": {"type": "STRING", "enum": ["auto", "on", "off"]},
            "title": {"type": "STRING", "description": "on-screen title text, empty if none asked"},
            "captions": {"type": "STRING", "enum": ["auto", "on", "off"], "description": "subtitles of the speech"},
            "caption_lang": {"type": "STRING", "enum": ["auto", "en", "hi", "pa", "hinglish"]},
        },
        "required": ["format", "length", "style", "look", "transitions", "slowmo", "title", "captions",
                     "caption_lang"],
    }
    prompt = (
        "You configure an automatic video editor. Convert the user's request into settings. "
        "Use 'auto' for anything the user did not ask for. 9:16 = vertical/reels/shorts/tiktok/stories, "
        "16:9 = wide/youtube/landscape, 1:1 = square. style fast = quick energetic cuts, smooth = calm. "
        "Only set title if the user asks for specific on-screen text.\n\nRequest: " + request
    )
    res = _request(key, "POST", f"models/{model}:generateContent", {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"responseMimeType": "application/json", "responseSchema": schema, "temperature": 0},
    })
    o = _json_from(res)
    if not o.get("length"):
        o.pop("length", None)
    return o


def shot_ideas(key: str, model: str | None, idea: str, count: int) -> list[str]:
    """Splits an idea into a few different shot descriptions so the result isn't repetitive."""
    if not model:
        return [idea] * count
    schema = {"type": "ARRAY", "items": {"type": "STRING"}}
    prompt = (f"Write {count} short, vivid, visually different camera shot descriptions (one sentence each) "
              f"that together tell this idea as a short video, in order. No text or words on screen.\n\nIdea: {idea}")
    try:
        res = _request(key, "POST", f"models/{model}:generateContent", {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"responseMimeType": "application/json", "responseSchema": schema},
        })
        shots = [s for s in _json_from(res) if isinstance(s, str) and s.strip()]
    except AIError:
        shots = []
    return (shots + [idea] * count)[:count]


def generate_image(key: str, model: str, prompt: str, aspect: str, out: Path) -> Path:
    if model.startswith("imagen"):
        res = _request(key, "POST", f"models/{model}:predict", {
            "instances": [{"prompt": prompt}],
            "parameters": {"sampleCount": 1, "aspectRatio": aspect},
        })
        preds = res.get("predictions") or []
        if not preds or "bytesBase64Encoded" not in preds[0]:
            raise AIError("No picture came back. The prompt may have been blocked by Google's safety rules.")
        data, mime = preds[0]["bytesBase64Encoded"], preds[0].get("mimeType", "image/png")
    else:
        res = _request(key, "POST", f"models/{model}:generateContent", {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"responseModalities": ["IMAGE"], "imageConfig": {"aspectRatio": aspect}},
        })
        parts = ((res.get("candidates") or [{}])[0].get("content") or {}).get("parts") or []
        img = next((p.get("inlineData") or p.get("inline_data") for p in parts
                    if p.get("inlineData") or p.get("inline_data")), None)
        if not img:
            raise AIError("No picture came back. The prompt may have been blocked by Google's safety rules.")
        data, mime = img["data"], img.get("mimeType") or img.get("mime_type") or "image/png"
    out = out.with_suffix(".jpg" if "jpeg" in mime else ".png")
    out.write_bytes(base64.b64decode(data))
    return out


def generate_video(key: str, model: str, prompt: str, aspect: str, out: Path, progress=None,
                   max_wait: float = 900) -> Path:
    aspect = aspect if aspect in ("16:9", "9:16") else "16:9"  # Veo makes wide or tall clips
    op = _request(key, "POST", f"models/{model}:predictLongRunning", {
        "instances": [{"prompt": prompt}],
        "parameters": {"aspectRatio": aspect},
    })
    name = op.get("name")
    if not name:
        raise AIError("Google AI didn't start the video.")
    started = time.time()
    while not op.get("done"):
        if time.time() - started > max_wait:
            raise AIError("The AI video took too long to make. Please try again.")
        if progress:
            progress(min(0.95, (time.time() - started) / 120))  # Veo usually takes 1–3 minutes
        time.sleep(8)
        op = _request(key, "GET", name)
    if op.get("error"):
        raise AIError(f"Google AI couldn't make this video: {op['error'].get('message', 'unknown reason')}")
    resp = op.get("response") or {}
    samples = (resp.get("generateVideoResponse") or {}).get("generatedSamples") or resp.get("generatedVideos") or []
    if not samples:
        reasons = (resp.get("generateVideoResponse") or {}).get("raiMediaFilteredReasons")
        raise AIError("No video came back." + (f" Reason: {reasons[0]}" if reasons else
                                                " The prompt may have been blocked by Google's safety rules."))
    video = samples[0].get("video") or {}
    if video.get("bytesBase64Encoded"):
        out.write_bytes(base64.b64decode(video["bytesBase64Encoded"]))
    elif video.get("uri"):
        out.write_bytes(_request(key, "GET", video["uri"], raw=True, timeout=300))
    else:
        raise AIError("The AI video couldn't be downloaded.")
    if progress:
        progress(1.0)
    return out


LANG_RULES = {
    "auto": "Write each line in the language it is spoken in: Hindi in Devanagari script, Punjabi in Gurmukhi "
            "script, English in English. Keep mixed-language (Hinglish) speech as spoken.",
    "en": "Translate everything into natural English.",
    "hi": "Write everything in Hindi using Devanagari script (translate if spoken in another language).",
    "pa": "Write everything in Punjabi using Gurmukhi script (translate if spoken in another language).",
    "hinglish": "Write everything in Hinglish: Hindi/Punjabi words written in Roman (English) letters, "
                "English words as they are.",
}


def transcribe(key: str, model: str, audio: Path, lang: str = "auto") -> list[dict]:
    """Listens to the speech and returns timed caption lines [{start, end, text}] in seconds."""
    data = base64.b64encode(audio.read_bytes()).decode()
    schema = {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
        "start": {"type": "NUMBER"}, "end": {"type": "NUMBER"}, "text": {"type": "STRING"}},
        "required": ["start", "end", "text"]}}
    prompt = ("Create subtitles for the speech in this audio. Return short lines (at most about 8 words each) "
              "with start and end times in seconds from the beginning of the audio, as precise as possible. "
              "Only include actual spoken words or sung lyrics; ignore music without words and background noise. "
              "If nobody speaks, return an empty list. " + LANG_RULES.get(lang, LANG_RULES["auto"]))
    res = _request(key, "POST", f"models/{model}:generateContent", {
        "contents": [{"role": "user", "parts": [
            {"inlineData": {"mimeType": "audio/mp3", "data": data}}, {"text": prompt}]}],
        "generationConfig": {"responseMimeType": "application/json", "responseSchema": schema, "temperature": 0},
    }, timeout=300)
    lines = []
    for s in _json_from(res) or []:
        try:
            start, end, text = float(s["start"]), float(s["end"]), str(s["text"]).strip()
        except (KeyError, TypeError, ValueError):
            continue
        if text and end > start >= 0:
            lines.append({"start": start, "end": end, "text": text})
    return sorted(lines, key=lambda x: x["start"])
