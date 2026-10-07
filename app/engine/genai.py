"""The app's thinking AI: understands typed requests and chat edits, looks at
video frames and writes shot ideas.

It uses a free public AI service that needs no key or account (key FREE).
The Gemini-style requests built below are translated to that service's
OpenAI-style chat format in _free_generate."""

from __future__ import annotations

import base64
import json
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import speech, translit

TIMEOUT = 120
FREE = "free"
FREE_MODELS = {"text": "free"}
FREE_TEXT = [b for b in os.environ.get("FREEAI_TEXT_BASES", "https://text.pollinations.ai/openai,"
                                       "https://gen.pollinations.ai/v1/chat/completions").split(",") if b]
UA = "AI-editor-open-source/1.0 (+https://github.com/lovedeep751653/ai-video-editor)"


class AIError(Exception):
    pass


def _schema(g: dict) -> dict:
    """Gemini schema (type: OBJECT...) to standard JSON schema (type: object...)."""
    out = {k: v for k, v in g.items() if k not in ("type", "properties", "items")}
    if "type" in g:
        out["type"] = g["type"].lower()
    if "properties" in g:
        out["properties"] = {k: _schema(v) for k, v in g["properties"].items()}
    if "items" in g:
        out["items"] = _schema(g["items"])
    return out


def _unfence(text: str) -> str:
    t = text.strip()
    m = re.match(r"^```(?:json)?\s*(.*?)\s*```$", t, re.S)
    if m:
        return m.group(1)
    starts = [i for i in (t.find("{"), t.find("[")) if i >= 0]
    return t[min(starts):] if starts and not t.startswith(("{", "[")) else t


def _free_generate(body: dict, timeout) -> dict:
    cfg = body.get("generationConfig") or {}
    system = " ".join(p.get("text", "") for p in (body.get("systemInstruction") or {}).get("parts", []))
    schema = cfg.get("responseSchema")
    if schema:
        system += ("\n\nAnswer with JSON only (no markdown, no explanation) that matches this JSON schema:\n"
                   + json.dumps(_schema(schema)))
    messages = [{"role": "system", "content": system.strip()}] if system.strip() else []
    for c in body.get("contents", []):
        parts = []
        for p in c.get("parts", []):
            if "text" in p:
                parts.append({"type": "text", "text": p["text"]})
            elif (p.get("inlineData") or {}).get("mimeType", "").startswith("image/"):
                d = p["inlineData"]
                parts.append({"type": "image_url", "image_url": {"url": f"data:{d['mimeType']};base64,{d['data']}"}})
        if not parts:
            continue
        content = parts[0]["text"] if len(parts) == 1 and parts[0]["type"] == "text" else parts
        messages.append({"role": "assistant" if c.get("role") == "model" else "user", "content": content})
    req = {"model": "openai", "messages": messages, "private": True, "referrer": "ai-editor-open-source"}
    if "temperature" in cfg:
        req["temperature"] = cfg["temperature"]
    if schema and schema.get("type") == "OBJECT":
        req["response_format"] = {"type": "json_object"}
    data = json.dumps(req).encode()
    errors = []
    for url in FREE_TEXT:
        for attempt in range(3):
            r = urllib.request.Request(url, data=data, method="POST",
                                       headers={"Content-Type": "application/json", "User-Agent": UA})
            try:
                with urllib.request.urlopen(r, timeout=timeout) as resp:
                    out = json.loads(resp.read() or b"{}")
                text = ((out.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
                if text.strip():
                    return {"candidates": [{"content": {"parts": [{"text": _unfence(text)}]}}]}
                errors.append("empty answer")
            except urllib.error.HTTPError as e:
                errors.append(f"HTTP {e.code}")
                if e.code == 429:
                    time.sleep(16)
                    continue
                if e.code in (401, 402, 403, 404):
                    break
            except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
                errors.append(str(getattr(e, "reason", e)))
            time.sleep(2)
    raise AIError("The free AI couldn't answer right now. Please check the internet connection. "
                  f"({'; '.join(errors[-2:])})")


def _request(key: str, method: str, url: str, body: dict | None = None, raw: bool = False, timeout=TIMEOUT):
    return _free_generate(body or {}, timeout)


def _json_from(res: dict):
    try:
        text = "".join(p.get("text", "") for p in res["candidates"][0]["content"]["parts"])
        return json.loads(text)
    except Exception:
        raise AIError("The AI gave an answer that couldn't be understood.") from None


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


def enhance_image_prompt(key: str, model: str | None, idea: str) -> str:
    """Rewrites a short idea into a rich, detailed image prompt, the way ChatGPT does
    before it draws. This is the single biggest reason ChatGPT pictures look so good:
    a plain idea like "a dog" becomes a full visual description. Falls back to the plain
    idea if the free text AI is unavailable (e.g. offline)."""
    idea = (idea or "").strip()
    if not model or len(idea) > 320:  # already long/detailed, or no text AI
        return idea
    schema = {"type": "OBJECT", "properties": {"prompt": {"type": "STRING"}}, "required": ["prompt"]}
    system = (
        "You are the expert prompt writer built into ChatGPT's image tool. Turn the user's idea "
        "into ONE detailed prompt for a text-to-image model, so the result looks like a professional "
        "ChatGPT / DALL-E picture. In natural sentences (no lists), describe the main subject clearly, "
        "then the setting, composition, lighting, mood, colour palette, depth of field and a fitting "
        "photographic or art style. Be concrete and visual. 40-70 words. Keep the user's language, "
        "subject and intent exactly. Put no text, letters, captions or watermarks in the image."
    )
    try:
        res = _request(key, "POST", f"models/{model}:generateContent", {
            "contents": [{"role": "user", "parts": [{"text": f"{system}\n\nIdea: {idea}"}]}],
            "generationConfig": {"responseMimeType": "application/json", "responseSchema": schema,
                                 "temperature": 0.8},
        })
        out = _json_from(res)
        rich = (out.get("prompt") if isinstance(out, dict) else "") or ""
        rich = " ".join(rich.split()).strip()
    except AIError:
        rich = ""
    return rich if len(rich) > len(idea) else idea


LANG_NAMES = {"en": "natural English", "hi": "Hindi written in Devanagari script",
              "pa": "Punjabi written in Gurmukhi script"}


def translate_lines(texts: list[str], lang: str) -> list[str]:
    """Translates caption lines with the free AI, keeping one line per line."""
    schema = {"type": "ARRAY", "items": {"type": "STRING"}}
    prompt = (f"Translate each of these {len(texts)} video caption lines into {LANG_NAMES[lang]}. Keep them short "
              "and natural. Return a JSON array with exactly one translated string per line, in the same order.\n\n"
              + json.dumps(texts, ensure_ascii=False))
    res = _request(FREE, "POST", "models/free:generateContent", {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"responseMimeType": "application/json", "responseSchema": schema, "temperature": 0},
    }, timeout=180)
    out = _json_from(res)
    if not isinstance(out, list) or len(out) != len(texts):
        raise AIError("The AI translation didn't match the captions.")
    return [str(x).strip() or t for x, t in zip(out, texts)]


def convert_lines(lines: list[dict], lang: str) -> list[dict]:
    """Puts recognised speech into the caption language the user picked."""
    if not lines:
        return lines
    lines = [{**ln, "text": translit.tidy(ln["text"])} for ln in lines]
    main = translit.script_of(" ".join(ln["text"] for ln in lines))
    if lang == "hinglish":
        return [{**ln, "text": translit.romanize(ln["text"])} for ln in lines]
    if lang == "auto":
        return lines
    needs_ai = (lang == "en" and main != "en") or (lang in ("hi", "pa") and main == "en")
    if lang in ("hi", "pa") and main in ("hi", "pa", "ur") and main != lang:
        needs_ai = True
    if needs_ai:
        out = []
        for i in range(0, len(lines), 40):
            part = lines[i:i + 40]
            try:
                texts = translate_lines([ln["text"] for ln in part], lang)
            except AIError:
                texts = [ln["text"] if lang == "en" else translit.to_script(ln["text"], lang) for ln in part]
            out += [{**ln, "text": t} for ln, t in zip(part, texts)]
        lines = out
    if lang in ("hi", "pa"):
        lines = [{**ln, "text": translit.to_script(ln["text"], lang)} for ln in lines]
    return lines


def transcribe(key: str, model: str, audio: Path, lang: str = "auto") -> list[dict]:
    """Listens to the speech (on the phone) and returns timed caption lines [{start, end, text}] in seconds."""
    try:
        lines = speech.transcribe(audio)
    except speech.SpeechError as e:
        raise AIError(str(e)) from None
    return convert_lines(lines, lang)


# ---------------------------------------------------------------- chat editing

EDIT_OPS = ["remove_clips", "keep_only_clips", "move_clip", "cut_time", "keep_time", "trim_clip_start",
            "trim_clip_end", "add_source_part", "speed", "zoom", "set_look", "set_format", "set_transitions",
            "set_title", "add_text", "remove_texts", "set_captions", "caption_language", "caption_style",
            "caption_position", "music_volume", "original_volume", "remove_music", "remove_pauses", "shorten_to",
            "reedit", "sort_clips", "fade", "undo"]

EDIT_GUIDE = """You are the editor inside a phone video-editing app. The user talks to you about the video
they are editing, the way they would talk to a human video editor. You can see the current edit (clips in
order, what each clip contains, the speech, on-screen text and settings) in the JSON below, and you change it
by returning operations. The app applies them and renders a new version.

Operations (all numbers are seconds unless said otherwise; clip numbers start at 1 as shown in "clips";
times called "at" are positions in the CURRENT finished video):
- remove_clips {clips:[n...]}            delete clips
- keep_only_clips {clips:[n...]}         delete every other clip
- move_clip {clip:n, to:m}               move clip n so it becomes clip m
- cut_time {start, end}                  remove that stretch of the finished video (may cut inside clips)
- keep_time {start, end}                 keep only that stretch of the finished video
- trim_clip_start {clip, number}         remove `number` seconds from the start of the clip
- trim_clip_end {clip, number}           remove `number` seconds from the end of the clip
- add_source_part {source, start, end, to}  insert a part of an original file (source number, times in that
                                         file) so it becomes clip `to` (leave `to` 0 to add at the end)
- speed {clips:[n...] (empty = whole video), number}  0.25..4 (0.5 = slow motion, 2 = twice as fast)
- zoom {clips:[n...] (empty = all), number}  1 = normal, 1.1..2 = punch in on the middle
- set_look {value}: none|enhance|cinematic|vivid|warm|cool|vintage|bw|dramatic
- set_format {value}: 9:16|16:9|1:1|original
- set_transitions {value: cuts|soft|dynamic|cinematic, number: length 0.2..1.5 (0 = default)}
- set_title {text}                       big title at the start ("" removes it)
- add_text {text, start, end, position: top|middle|bottom}  text on screen between those times
- remove_texts {clips:[n...] (text numbers from "texts"; empty = all)}
- set_captions {value: on|off}           subtitles of the speech
- caption_language {value: auto|en|hi|pa|hinglish}
- caption_style {value: one of the caption style ids listed}
- caption_position {value: top|middle|bottom}
- music_volume {number 0..2}, original_volume {number 0..2}, remove_music {}
- remove_pauses {value: gentle|strong}   cut silences and pauses out of the speech
- shorten_to {number}                    make the video about that long by leaving out the weakest parts
- reedit {value: highlight|cleanup, number: length (0 = automatic)}  start the edit again from the original
                                         files (highlight = short best moments; cleanup = keep everything
                                         good, remove pauses)
- sort_clips {value: filmed|best_first|reverse}
- fade {value: on|off}                   fade in/out at the very start and end
- undo {}                                go back to the previous version

Rules:
- Do exactly what the user asks, nothing more. If the request is unclear or impossible, ask one short
  question or explain, and return no operations.
- If they only ask a question (what is in my video, how long is it...), answer it from the JSON with no
  operations.
- Use the speech ("transcript") and the visual descriptions to find moments by what is said or shown.
- Adding music needs a music file: tell them to tap the music button; you cannot add music yourself.
- "reply" is shown to the user: one or two short, friendly sentences saying what you did (or your answer),
  in the same language and script the user wrote in (Hindi, Punjabi, Hinglish or English). No jargon.
"""


def edit_chat(key: str, model: str, context: dict, message: str, history: list[dict]) -> dict:
    """Turns a chat message about the current edit into {"reply", "operations"}."""
    op = {"type": "OBJECT", "properties": {
        "op": {"type": "STRING", "enum": EDIT_OPS},
        "clips": {"type": "ARRAY", "items": {"type": "INTEGER"}},
        "clip": {"type": "INTEGER"}, "to": {"type": "INTEGER"}, "source": {"type": "INTEGER"},
        "start": {"type": "NUMBER"}, "end": {"type": "NUMBER"}, "number": {"type": "NUMBER"},
        "value": {"type": "STRING"}, "text": {"type": "STRING"}, "position": {"type": "STRING"},
    }, "required": ["op"]}
    schema = {"type": "OBJECT", "properties": {
        "reply": {"type": "STRING"},
        "operations": {"type": "ARRAY", "items": op},
    }, "required": ["reply", "operations"]}
    contents = []
    for h in history[-8:]:
        contents.append({"role": "user" if h["role"] == "user" else "model", "parts": [{"text": h["text"][:2000]}]})
    contents.append({"role": "user", "parts": [{"text": "CURRENT EDIT:\n" + json.dumps(context, ensure_ascii=False)
                                                + "\n\nUSER: " + message}]})
    res = _request(key, "POST", f"models/{model}:generateContent", {
        "systemInstruction": {"parts": [{"text": EDIT_GUIDE}]},
        "contents": contents,
        "generationConfig": {"responseMimeType": "application/json", "responseSchema": schema, "temperature": 0.2},
    }, timeout=180)
    out = _json_from(res)
    if not isinstance(out, dict):
        raise AIError("The AI gave an answer that couldn't be understood.")
    ops = [o for o in (out.get("operations") or []) if isinstance(o, dict) and o.get("op") in EDIT_OPS]
    return {"reply": str(out.get("reply") or "").strip(), "operations": ops}


def describe_frames(key: str, model: str, frames: list[tuple[float, Path]]) -> list[dict]:
    """Looks at pictures taken from a video and says briefly what is happening at each moment."""
    if not frames:
        return []
    parts = []
    for t, f in frames:
        parts.append({"text": f"t={t:.1f}s"})
        parts.append({"inlineData": {"mimeType": "image/jpeg", "data": base64.b64encode(f.read_bytes()).decode()}})
    parts.append({"text": "These are frames from one video, each labelled with its time. For each frame write a "
                          "very short description (max 12 words) of what is visible: people, actions, place, "
                          "objects, text on screen. Return one item per frame."})
    schema = {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
        "time": {"type": "NUMBER"}, "text": {"type": "STRING"}}, "required": ["time", "text"]}}
    res = _request(key, "POST", f"models/{model}:generateContent", {
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {"responseMimeType": "application/json", "responseSchema": schema, "temperature": 0},
    }, timeout=180)
    out = []
    for item in _json_from(res) or []:
        try:
            out.append({"time": round(float(item["time"]), 1), "text": str(item["text"]).strip()[:120]})
        except (KeyError, TypeError, ValueError):
            continue
    return out
