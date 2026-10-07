"""The app's thinking AI: understands typed requests and chat edits, looks at
video frames and writes shot ideas and picture prompts.

It runs on the phone itself (engine/brain.py: a small model through llama.cpp), so it
needs no internet, key or account. The Gemini-style requests built below are turned
into a plain conversation for that model in _local_generate."""

from __future__ import annotations

import base64
import json
import re
import tempfile
from pathlib import Path

from . import brain, ff, speech, translit

TIMEOUT = 120
FREE = "free"
FREE_MODELS = {"text": "free"}


class AIError(Exception):
    pass


def _example(g: dict) -> str:
    """A plain example of the answer's shape. The grammar already enforces the exact shape; showing the
    raw schema made the small model copy the schema itself into its answer."""
    t = g.get("type", "STRING").upper()
    if "enum" in g:
        return " or ".join(json.dumps(v, ensure_ascii=False) for v in g["enum"][:6])
    if t == "OBJECT":
        props = g.get("properties", {})
        return "{" + ", ".join(f'"{k}": {_example(v)}' for k, v in props.items()) + "}"
    if t == "ARRAY":
        n = g.get("minItems") or 1
        item = _example(g.get("items", {}))
        return "[" + ", ".join([item] * min(n, 3)) + (", ..." if n > 3 else "") + "]" + (
            f" (exactly {n} items)" if n == g.get("maxItems") else "")
    if t in ("NUMBER", "INTEGER"):
        return "0"
    if t == "BOOLEAN":
        return "true"
    return '"..."'


def _unfence(text: str) -> str:
    t = text.strip()
    m = re.match(r"^```(?:json)?\s*(.*?)\s*```$", t, re.S)
    if m:
        return m.group(1)
    starts = [i for i in (t.find("{"), t.find("[")) if i >= 0]
    return t[min(starts):] if starts and not t.startswith(("{", "[")) else t


def _local_generate(body: dict) -> dict:
    """Runs one Gemini-style request on the on-phone model and returns a Gemini-style answer."""
    cfg = body.get("generationConfig") or {}
    system = " ".join(p.get("text", "") for p in (body.get("systemInstruction") or {}).get("parts", []))
    schema = cfg.get("responseSchema")
    if schema:
        system += "\n\nAnswer with JSON only, shaped like this (replace ... with your answer):\n" + _example(schema)
    messages = [{"role": "system", "content": system.strip()}] if system.strip() else []
    images: list[str] = []
    tmp: list[Path] = []
    try:
        for c in body.get("contents", []):
            texts = []
            for p in c.get("parts", []):
                if "text" in p:
                    texts.append(p["text"])
                elif (p.get("inlineData") or {}).get("mimeType", "").startswith("image/"):
                    fd, path = tempfile.mkstemp(prefix="frame_", suffix=".jpg", dir=ff._tmpdir())
                    with open(fd, "wb") as f:
                        f.write(base64.b64decode(p["inlineData"]["data"]))
                    tmp.append(Path(path))
                    images.append(path)
                    texts.append("<image>")
            if texts:
                messages.append({"role": "assistant" if c.get("role") == "model" else "user", "content": "\n".join(texts)})
        max_tokens = int(cfg.get("maxOutputTokens") or 512)
        temperature = float(cfg.get("temperature", 0.2))
        text = ""
        for attempt in range(2):  # a small model occasionally runs out of room mid-answer: try once more
            try:
                text = brain.chat(messages, schema, max_tokens, temperature if attempt == 0 else 0.3, images)
            except brain.BrainError as e:
                raise AIError(str(e)) from None
            text = _unfence(text)
            if not schema:
                break
            try:
                json.loads(text)
                break
            except ValueError:
                continue
        return {"candidates": [{"content": {"parts": [{"text": text}]}}]}
    finally:
        for f in tmp:
            f.unlink(missing_ok=True)


def _request(key: str, method: str, url: str, body: dict | None = None, raw: bool = False, timeout=TIMEOUT):
    return _local_generate(body or {})


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
        "generationConfig": {"responseMimeType": "application/json", "responseSchema": schema, "temperature": 0,
                             "maxOutputTokens": 200},
    })
    o = _json_from(res)
    try:
        o["length"] = max(0, min(1800, float(o.get("length") or 0)))
    except (TypeError, ValueError):
        o["length"] = 0
    if not o.get("length"):
        o.pop("length", None)
    return o


def shot_ideas(key: str, model: str | None, idea: str, count: int) -> list[str]:
    """Splits an idea into a few different shot descriptions so the result isn't repetitive."""
    if not model:
        return [idea] * count
    schema = {"type": "ARRAY", "items": {"type": "STRING"}, "minItems": count, "maxItems": count}
    prompt = (f"Write {count} short, vivid, visually different camera shot descriptions (one sentence each) "
              f"that together tell this idea as a short video, in order. No text or words on screen.\n\nIdea: {idea}")
    try:
        res = _request(key, "POST", f"models/{model}:generateContent", {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"responseMimeType": "application/json", "responseSchema": schema,
                                 "temperature": 0.7, "maxOutputTokens": 70 * count},
        })
        shots = [s for s in _json_from(res) if isinstance(s, str) and s.strip()]
    except AIError:
        shots = []
    return (shots + [idea] * count)[:count]


def enhance_image_prompt(key: str, model: str | None, idea: str) -> str:
    """Rewrites a short idea into a rich, detailed image prompt, the way ChatGPT does
    before it draws. This is the single biggest reason ChatGPT pictures look so good:
    a plain idea like "a dog" becomes a full visual description. Falls back to the plain
    idea if the on-phone AI can't answer."""
    idea = (idea or "").strip()
    if not model or len(idea) > 320:  # already long/detailed, or no text AI
        return idea
    schema = {"type": "OBJECT", "properties": {"prompt": {"type": "STRING"}}, "required": ["prompt"]}
    system = (
        "You are the expert prompt writer built into ChatGPT's image tool. Turn the user's idea "
        "into ONE detailed prompt for a text-to-image model, so the result looks like a professional "
        "ChatGPT / DALL-E picture. In natural sentences (no lists), describe the main subject clearly, "
        "then the setting, composition, lighting, mood, colour palette, depth of field and a fitting "
        "photographic or art style. Be concrete and visual. 40-70 words. Always write the prompt in English "
        "(picture models understand English best), even if the idea is in Hindi, Punjabi or Hinglish, and "
        "keep the subject and intent exactly. Put no text, letters, captions or watermarks in the image."
    )
    try:
        res = _request(key, "POST", f"models/{model}:generateContent", {
            "contents": [{"role": "user", "parts": [{"text": f"{system}\n\nIdea: {idea}"}]}],
            "generationConfig": {"responseMimeType": "application/json", "responseSchema": schema,
                                 "temperature": 0.7, "maxOutputTokens": 200},
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
    """Translates caption lines with the on-phone AI, keeping one line per line."""
    schema = {"type": "ARRAY", "items": {"type": "STRING"}, "minItems": len(texts), "maxItems": len(texts)}
    prompt = (f"Translate each of these {len(texts)} video caption lines into {LANG_NAMES[lang]}. Keep them short "
              "and natural. Return a JSON array with exactly one translated string per line, in the same order.\n\n"
              + json.dumps(texts, ensure_ascii=False))
    res = _request(FREE, "POST", "models/free:generateContent", {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"responseMimeType": "application/json", "responseSchema": schema, "temperature": 0,
                             "maxOutputTokens": 30 + 60 * len(texts)},
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
        for i in range(0, len(lines), 12):  # small batches suit the on-phone model
            part = lines[i:i + 12]
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


CONTEXT_CHARS = 5000  # what fits next to the guide in the on-phone model's memory


def _compact(context: dict, message: str, budget: int = CONTEXT_CHARS) -> dict:
    """Shrinks the edit description to what matters for this message, so it fits the on-phone model."""
    ctx = json.loads(json.dumps(context, ensure_ascii=False))
    words = {w for w in re.findall(r"\w+", message.lower()) if len(w) > 2}
    if not any(w in message.lower() for w in ("style", "caption", "subtitle", "कैप्शन", "ਕੈਪਸ਼ਨ")):
        ctx.pop("caption_styles", None)
    for c in ctx.get("clips", []):
        c.pop("quality", None)

    def size() -> int:
        return len(json.dumps(ctx, ensure_ascii=False, separators=(",", ":")))

    def relevant(text: str) -> bool:
        return bool(words & set(re.findall(r"\w+", text.lower())))

    for key, sub in (("transcript", None), ("sources", "what_happens")):
        while size() > budget:
            items = ctx.get(key) if sub is None else None
            if sub is not None:
                lists = [s_ for s_ in ctx.get(key, []) if len(s_.get(sub) or []) > 4]
                if not lists:
                    break
                for s_ in lists:
                    s_[sub] = s_[sub][::2]
                continue
            if not items or len(items) <= 6:
                break
            keep = [x for x in items if relevant(x.get("text", ""))]
            rest = [x for x in items if x not in keep]
            ctx[key] = sorted(keep[: len(items) // 2] + rest[::2][: max(0, len(items) // 2 - len(keep))],
                              key=lambda x: x.get("at", 0))
    while size() > budget and len(ctx.get("clips", [])) > 12:
        ctx["clips"] = ctx["clips"][::2]
        ctx["note"] = "only some clips are listed"
    for c in ctx.get("clips", []):
        if size() <= budget:
            break
        if "shows" in c:
            c["shows"] = c["shows"][:80]
    return ctx


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
    for h in history[-4:]:
        contents.append({"role": "user" if h["role"] == "user" else "model", "parts": [{"text": h["text"][:300]}]})
    ctx = _compact(context, message)
    contents.append({"role": "user", "parts": [{"text": "CURRENT EDIT:\n"
                                                + json.dumps(ctx, ensure_ascii=False, separators=(",", ":"))
                                                + "\n\nUSER: " + message}]})
    res = _request(key, "POST", f"models/{model}:generateContent", {
        "systemInstruction": {"parts": [{"text": EDIT_GUIDE}]},
        "contents": contents,
        "generationConfig": {"responseMimeType": "application/json", "responseSchema": schema, "temperature": 0.2,
                             "maxOutputTokens": 400},
    }, timeout=180)
    out = _json_from(res)
    if not isinstance(out, dict):
        raise AIError("The AI gave an answer that couldn't be understood.")
    ops = [o for o in (out.get("operations") or []) if isinstance(o, dict) and o.get("op") in EDIT_OPS]
    return {"reply": str(out.get("reply") or "").strip(), "operations": ops}


def describe_frames(key: str, model: str, frames: list[tuple[float, Path]]) -> list[dict]:
    """Looks at pictures taken from a video and says briefly what is happening at each moment."""
    out = []
    for i in range(0, len(frames), 4):  # a few pictures at a time suits the on-phone model
        batch = frames[i:i + 4]
        parts = []
        for k, (t, f) in enumerate(batch):
            parts.append({"text": f"Picture {k + 1} (at {t:.0f}s):"})
            parts.append({"inlineData": {"mimeType": "image/jpeg", "data": base64.b64encode(f.read_bytes()).decode()}})
        parts.append({"text": f"These {len(batch)} pictures are from one video. For each picture, in order, write a "
                              "very short description (max 12 words) of what is visible: people, actions, place, "
                              "objects, text on screen."})
        schema = {"type": "ARRAY", "items": {"type": "STRING"}, "minItems": len(batch), "maxItems": len(batch)}
        res = _request(key, "POST", f"models/{model}:generateContent", {
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": {"responseMimeType": "application/json", "responseSchema": schema, "temperature": 0,
                                 "maxOutputTokens": 40 * len(batch)},
        }, timeout=180)
        texts = _json_from(res) or []
        for (t, _), text in zip(batch, texts):
            if isinstance(text, str) and text.strip():
                out.append({"time": round(float(t), 1), "text": text.strip()[:120]})
    return out
