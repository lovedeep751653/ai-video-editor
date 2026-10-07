"""Firely, the app's own thinking AI, running on the phone itself with no internet.

A small language model (Qwen3-VL 2B, bundled in the APK) runs through llama.cpp in
libbrain.so (android/app/src/main/cpp). It reads typed requests and chat edits,
writes shot ideas and picture prompts, translates captions and looks at video frames.
Answers that must be JSON are forced into the right shape with a grammar, so even a
small model always returns something the app can read.

Where it runs:
  - on the phone: through the Kotlin bridge Brain.kt
  - on a computer (development/tests): set BRAIN_LIB to libbrain.so, BRAIN_MODEL to the .gguf
    and optionally BRAIN_VISION to its mmproj .gguf
  - in the automatic tests: BRAIN_TEST_URL points at a stand-in (tests/mock_gemini.py)"""

from __future__ import annotations

import ctypes
import json
import os
import random
import threading
import urllib.error
import urllib.request

from . import ff

CTX = 4096
_lock = threading.Lock()
_desktop = None  # (lib, handle) on a computer


class BrainError(Exception):
    pass


# ---------------------------------------------------------------- answer shapes (JSON grammar)

_BASE = r'''
ws ::= | " "
string ::= "\"" ( [^"\\\x7F\x00-\x1F] | "\\" ( ["\\/bfnrt] | "u" [0-9a-fA-F]{4} ) ){0,600} "\"" ws
number ::= "-"? [0-9]{1,9} ( "." [0-9]{1,4} )? ws
integer ::= "-"? [0-9]{1,6} ws
boolean ::= ( "true" | "false" ) ws
'''


def grammar_for(schema: dict) -> str:
    """GBNF grammar that only allows JSON matching a (Gemini-style) schema."""
    rules: dict[str, str] = {}

    def lit(s: str) -> str:
        return json.dumps(json.dumps(s, ensure_ascii=False), ensure_ascii=False)

    def rule(s: dict, name: str) -> str:
        t = str(s.get("type", "STRING")).upper()
        if s.get("enum"):
            rules[name] = "( " + " | ".join(lit(str(v)) for v in s["enum"]) + " ) ws"
        elif t == "OBJECT":
            props = s.get("properties") or {}
            req = [k for k in (s.get("required") or []) if k in props]
            opt = [k for k in props if k not in req]
            parts = []
            for i, k in enumerate(req):
                sub = rule(props[k], f"{name}-{_slug(k)}")
                parts.append(("\",\" ws " if i else "") + f"{lit(k)} ws \":\" ws {sub}")
            for k in opt:
                sub = rule(props[k], f"{name}-{_slug(k)}")
                sep = "\",\" ws " if req else ""
                parts.append(f"( {sep}{lit(k)} ws \":\" ws {sub} )?")
            rules[name] = "\"{\" ws " + " ".join(parts) + " \"}\" ws"
        elif t == "ARRAY":
            item = rule(s.get("items") or {"type": "STRING"}, f"{name}-item")
            lo, hi = int(s.get("minItems", 0)), s.get("maxItems")
            if hi is not None and int(hi) >= 1:
                hi = int(hi)
                if lo >= 1:
                    body = f"{item} ( \",\" ws {item} ){{{lo - 1},{hi - 1}}}"
                else:
                    body = f"( {item} ( \",\" ws {item} ){{0,{hi - 1}}} )?"
            else:
                body = f"( {item} ( \",\" ws {item} )* )?" if lo == 0 else f"{item} ( \",\" ws {item} )*"
            rules[name] = f"\"[\" ws {body} \"]\" ws"
        elif t == "NUMBER":
            return "number"
        elif t == "INTEGER":
            return "integer"
        elif t == "BOOLEAN":
            return "boolean"
        else:
            return "string"
        return name

    top = rule(schema, "answer")
    lines = [f"root ::= ws {top}"] + [f"{k} ::= {v}" for k, v in rules.items()]
    return "\n".join(lines) + _BASE


def _slug(k: str) -> str:
    return "".join(c if c.isalnum() else "-" for c in k.lower()) or "x"


# ---------------------------------------------------------------- where the model runs

def _android():
    from java import jclass  # Chaquopy
    return jclass("com.lovedeep.aivideoeditor.Brain")


def _desktop_lib():
    global _desktop
    if _desktop is None:
        lib_path, model = os.environ.get("BRAIN_LIB", ""), os.environ.get("BRAIN_MODEL", "")
        if not (lib_path and model and os.path.exists(lib_path) and os.path.exists(model)):
            raise BrainError("The on-device AI isn't installed on this computer (set BRAIN_LIB and BRAIN_MODEL).")
        lib = ctypes.CDLL(lib_path)
        lib.brain_load.argtypes = [ctypes.c_char_p, ctypes.c_int, ctypes.c_int, ctypes.c_char_p, ctypes.c_int]
        lib.brain_load.restype = ctypes.c_void_p
        lib.brain_generate.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_char_p),
                                       ctypes.POINTER(ctypes.c_char_p), ctypes.c_int,
                                       ctypes.POINTER(ctypes.c_char_p), ctypes.c_int, ctypes.c_char_p, ctypes.c_int,
                                       ctypes.c_float, ctypes.c_uint, ctypes.c_char_p, ctypes.c_int,
                                       ctypes.c_char_p, ctypes.c_int]
        lib.brain_free.argtypes = [ctypes.c_void_p]
        lib.brain_load_vision.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p,
                                          ctypes.c_int]
        lib.brain_generate.restype = ctypes.c_int
        err = ctypes.create_string_buffer(512)
        h = lib.brain_load(model.encode(), min(4, os.cpu_count() or 2), CTX, err, 512)
        if not h:
            raise BrainError(err.value.decode(errors="replace"))
        mmproj = os.environ.get("BRAIN_VISION", "")
        if mmproj and os.path.exists(mmproj):
            lib.brain_load_vision(h, mmproj.encode(), min(4, os.cpu_count() or 2), err, 512)
        _desktop = (lib, h)
    return _desktop


def _test_url(messages: list[dict], schema: dict | None, max_tokens: int, temperature: float) -> str:
    """Tests only: an OpenAI-style stand-in server answers instead of the real model."""
    req = {"model": "brain", "messages": messages, "max_tokens": max_tokens, "temperature": temperature}
    if schema:
        req["response_format"] = {"type": "json_schema", "schema": schema}
    r = urllib.request.Request(os.environ["BRAIN_TEST_URL"], data=json.dumps(req).encode(), method="POST",
                               headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r, timeout=120) as resp:
            out = json.loads(resp.read() or b"{}")
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise BrainError(f"the on-device AI isn't available ({e})") from None
    return ((out.get("choices") or [{}])[0].get("message") or {}).get("content") or ""


def available() -> bool:
    """True when the on-device AI can run here."""
    if os.environ.get("BRAIN_TEST_URL"):
        return True
    if ff.ANDROID:
        try:
            return bool(_android().available())
        except Exception:  # noqa: BLE001
            return False
    return bool(os.environ.get("BRAIN_LIB") and os.environ.get("BRAIN_MODEL"))


def chat(messages: list[dict], schema: dict | None = None, max_tokens: int = 512, temperature: float = 0.0,
         images: list[str] | None = None) -> str:
    """Runs the model on a conversation [{"role", "content"}] and returns its answer text.
    With a schema the answer is always JSON of that shape. `images` (file paths) are shown to
    the model with the last message; each "<image>" marker in it says where."""
    grammar = grammar_for(schema) if schema else ""
    seed = random.randint(1, 2**31 - 1)
    if os.environ.get("BRAIN_TEST_URL"):
        return _test_url(messages, schema, max_tokens, temperature)
    with _lock:
        if ff.ANDROID:
            out = json.loads(str(_android().chat(json.dumps(messages, ensure_ascii=False), grammar, max_tokens,
                                                 float(temperature), seed, json.dumps(images or []))))
            if out.get("error"):
                raise BrainError(out["error"])
            return out.get("text", "")
        lib, h = _desktop_lib()
        n = len(messages)
        roles = (ctypes.c_char_p * n)(*[str(m["role"]).encode() for m in messages])
        texts = (ctypes.c_char_p * n)(*[str(m["content"]).encode() for m in messages])
        pics = (ctypes.c_char_p * max(1, len(images or [])))(*[p.encode() for p in images or []])
        out = ctypes.create_string_buffer(65536)
        err = ctypes.create_string_buffer(512)
        rc = lib.brain_generate(h, roles, texts, n, pics, len(images or []), grammar.encode(), max_tokens,
                                float(temperature), seed, out, 65536, err, 512)
        if rc < 0:
            raise BrainError(err.value.decode(errors="replace"))
        return out.value.decode("utf-8", errors="replace")


def release() -> None:
    """Frees the model's memory (it loads again by itself when next needed)."""
    global _desktop
    if os.environ.get("BRAIN_TEST_URL"):
        return
    with _lock:
        if ff.ANDROID:
            try:
                _android().unload()
            except Exception:  # noqa: BLE001
                pass
        elif _desktop is not None:
            lib, h = _desktop
            lib.brain_free(ctypes.c_void_p(h))
            _desktop = None


def fits(text: str, max_tokens: int) -> bool:
    """Rough check that a request leaves room for the answer (about 3 characters per token)."""
    return len(text) / 3 + max_tokens + 200 < CTX
