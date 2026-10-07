"""Free AI pictures and moving AI clips that need no key or account.

Pictures come from free public image models (Pollinations' FLUX first, the
volunteer-run AI Horde as a backup). Video clips are made on the phone by
giving each AI picture smooth camera motion."""

from __future__ import annotations

import json
import random
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import os

from . import ff

UA = "AI-editor-open-source/1.0 (+https://github.com/lovedeep751653/ai-video-editor)"
QUALITY = "highly detailed, sharp focus, cinematic lighting, rich colours, professional photography"
SHOTS = ["wide establishing shot", "close-up detail shot", "medium shot", "dramatic low angle shot",
         "aerial view", "over-the-shoulder shot", "golden hour backlit shot", "soft focus background portrait shot"]
SIZES = {"9:16": (1080, 1920), "16:9": (1920, 1080), "1:1": (1440, 1440)}
HORDE_SIZES = {"9:16": (576, 1024), "16:9": (1024, 576), "1:1": (832, 832)}
CLIP_SECONDS = 5.0
POLLINATIONS = [b for b in os.environ.get("FREEAI_BASES", "https://image.pollinations.ai/prompt/,"
                                          "https://gen.pollinations.ai/image/").split(",") if b]
HORDE = os.environ.get("FREEAI_HORDE", "https://aihorde.net/api/v2")


class FreeAIError(Exception):
    pass


def _get(url: str, timeout: float = 180, data: dict | None = None, headers: dict | None = None) -> tuple[bytes, str]:
    h = {"User-Agent": UA, **(headers or {})}
    body = None
    if data is not None:
        body = json.dumps(data).encode()
        h["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=h, method="POST" if body else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(), r.headers.get("Content-Type", "")


def _is_image(data: bytes) -> str | None:
    if data[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    return None


def shot_prompts(idea: str, count: int) -> list[str]:
    """Different camera shots of one idea, so several results don't look the same."""
    if count <= 1:
        return [idea]
    return [f"{idea}, {SHOTS[i % len(SHOTS)]}" for i in range(count)]


def _pollinations(prompt: str, aspect: str, seed: int) -> bytes:
    w, h = SIZES.get(aspect, SIZES["9:16"])
    q = urllib.parse.quote(f"{prompt}, {QUALITY}", safe="")
    params = urllib.parse.urlencode({"width": w, "height": h, "model": "flux", "seed": seed,
                                     "nologo": "true", "private": "true", "enhance": "true"})
    errors = []
    for base in POLLINATIONS:
        for attempt in range(2):
            try:
                data, _ = _get(f"{base}{q}?{params}", timeout=180)
                if _is_image(data) and len(data) > 20_000:
                    return data
                errors.append("no picture returned")
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                errors.append(str(getattr(e, "code", "") or getattr(e, "reason", "") or e))
                if getattr(e, "code", 0) in (401, 402, 403, 404):
                    break
            time.sleep(3 * (attempt + 1))
    raise FreeAIError("; ".join(errors[-2:]))


def _horde(prompt: str, aspect: str, seed: int, max_wait: float = 600) -> bytes:
    w, h = HORDE_SIZES.get(aspect, HORDE_SIZES["9:16"])
    hdr = {"apikey": "0000000000", "Client-Agent": "ai-editor-open-source:1.0:github"}
    req = {"prompt": f"{prompt}, {QUALITY} ### blurry, lowres, deformed, watermark, text",
           "params": {"width": w, "height": h, "steps": 30, "n": 1, "sampler_name": "k_dpmpp_2m",
                      "karras": True, "cfg_scale": 6, "seed": str(seed)},
           "models": ["AlbedoBase XL (SDXL)", "Juggernaut XL", "SDXL 1.0"],
           "nsfw": False, "censor_nsfw": True, "r2": True, "shared": False}
    raw, _ = _get(f"{HORDE}/generate/async", timeout=60, data=req, headers=hdr)
    jid = json.loads(raw).get("id")
    if not jid:
        raise FreeAIError("the backup picture service is busy")
    started = time.time()
    while True:
        ff.check_cancel()
        time.sleep(5)
        st = json.loads(_get(f"{HORDE}/generate/check/{jid}", timeout=30, headers=hdr)[0])
        if st.get("done"):
            break
        if st.get("faulted") or not st.get("is_possible", True):
            raise FreeAIError("the backup picture service couldn't make it")
        if time.time() - started > max_wait:
            raise FreeAIError("the backup picture service took too long")
    res = json.loads(_get(f"{HORDE}/generate/status/{jid}", timeout=60, headers=hdr)[0])
    gens = res.get("generations") or []
    if not gens or gens[0].get("censored"):
        raise FreeAIError("the picture was blocked by the safety filter")
    img = gens[0].get("img", "")
    data = _get(img, timeout=120)[0] if img.startswith("http") else __import__("base64").b64decode(img)
    if not _is_image(data):
        raise FreeAIError("no picture returned")
    return data


def generate_image(prompt: str, aspect: str, out: Path, seed: int | None = None) -> Path:
    """Makes one AI picture, sharpened and sized to full HD for the chosen shape."""
    seed = random.randint(1, 2**31 - 1) if seed is None else seed
    errors = []
    data = None
    for make in (_pollinations, _horde):
        try:
            data = make(prompt, aspect, seed)
            break
        except ff.Cancelled:
            raise
        except Exception as e:  # noqa: BLE001 - any failure moves on to the next free service
            errors.append(str(e))
    if data is None:
        raise FreeAIError("The free AI picture services couldn't make this right now. Please check the "
                          f"internet connection and try again. ({'; '.join(errors)[:200]})")
    raw = out.with_name(out.name + "_raw" + _is_image(data))
    raw.write_bytes(data)
    w, h = SIZES.get(aspect, SIZES["9:16"])
    final = out.with_suffix(".jpg")
    ff.run_quiet(["-i", str(raw), "-vf",
                  f"scale={w}:{h}:force_original_aspect_ratio=increase:flags=lanczos,crop={w}:{h},"
                  "unsharp=5:5:0.6:5:5:0.0", "-frames:v", "1", "-q:v", "2", str(final)])
    raw.unlink(missing_ok=True)
    if not final.exists():
        raise FreeAIError("The AI picture couldn't be saved.")
    return final


MOVES = ["in", "right", "out", "left", "up"]


def motion_clip(image: Path, aspect: str, out: Path, move: str = "in", seconds: float = CLIP_SECONDS,
                quality: str = "1080", speed: str = "fast", progress=None) -> Path:
    """Turns a still AI picture into a smooth moving video clip (camera push, pull or pan)."""
    bw, bh = SIZES.get(aspect, SIZES["9:16"])
    k = int(quality or 1080) / 1080
    w, h = int(bw * k) // 2 * 2, int(bh * k) // 2 * 2
    if aspect == "1:1":
        w = h = int(1080 * k) // 2 * 2
    fps, n = 30, int(seconds * 30)
    t = f"(on/{n})"
    ease = f"(0.5-0.5*cos(PI*{t}))"
    if move == "in":
        z, x, y = f"1+0.18*{ease}", "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"
    elif move == "out":
        z, x, y = f"1.18-0.18*{ease}", "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"
    elif move == "left":
        z, x, y = "1.15", f"(iw-iw/zoom)*(1-{ease})", "ih/2-(ih/zoom/2)"
    elif move == "up":
        z, x, y = "1.15", "iw/2-(iw/zoom/2)", f"(ih-ih/zoom)*(1-{ease})"
    else:
        z, x, y = "1.15", f"(iw-iw/zoom)*{ease}", "ih/2-(ih/zoom/2)"
    big = out.with_name(out.stem + "_big.png")
    ff.run_quiet(["-i", str(image), "-vf", f"scale={w * 2}:{h * 2}:force_original_aspect_ratio=increase:flags=lanczos,"
                  f"crop={w * 2}:{h * 2}", "-frames:v", "1", str(big)])
    if not big.exists():
        raise FreeAIError("The AI picture couldn't be read.")
    vf = f"zoompan=z='{z}':x='{x}':y='{y}':d={n}:s={w}x{h}:fps={fps},format=yuv420p"
    try:
        ff.run(["-i", str(big), "-vf", vf, "-frames:v", str(n), "-an", *ff.video_args(speed, min(w, h)),
                "-movflags", "+faststart", str(out)], duration=seconds, progress=progress)
    finally:
        big.unlink(missing_ok=True)
    return out
