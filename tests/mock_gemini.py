"""A stand-in for the free AI services the app uses, used only by tests. It
answers the same requests the real services do (OpenAI-style chat for the
thinking AI, a picture for the picture AI), so the whole AI flow can be
checked offline. POST /free/offline {"on": true} makes it act unreachable.
Run: python3 mock_gemini.py PORT"""

import json
import subprocess
import sys
import tempfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

TMP = Path(tempfile.mkdtemp())
subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=768x1344", "-frames:v", "1",
                str(TMP / "img.png")], check=True)
LOG = []
STATE = {"offline": False}


def chat_answer(text: str) -> dict:
    """Stand-in for the AI editor: understands a few test phrases, using the edit it was shown."""
    ctx = json.loads(text.split("CURRENT EDIT:\n", 1)[1].split("\n\nUSER: ", 1)[0])
    msg = text.split("\n\nUSER: ", 1)[1].lower()
    if "what" in msg and "?" in msg:
        return {"reply": f"Your video is {ctx['video']['length']:.0f} seconds long with {len(ctx['clips'])} clips.",
                "operations": []}
    if msg.strip() == "undo":
        return {"reply": "Going back.", "operations": [{"op": "undo"}]}
    if "dog" in msg:
        dog = [c["n"] for c in ctx["clips"] if "dog" in c.get("shows", "")]
        return {"reply": "Removed the parts with the dog.", "operations": [{"op": "remove_clips", "clips": dog}]}
    if "punjabi" in msg:
        return {"reply": "ਪੰਜਾਬੀ ਵਿੱਚ ਕੈਪਸ਼ਨ ਲਗਾ ਦਿੱਤੇ।", "operations": [
            {"op": "set_captions", "value": "on"}, {"op": "caption_language", "value": "pa"}]}
    if "vintage" in msg:
        return {"reply": "Gave it a vintage film look and added your text.", "operations": [
            {"op": "set_look", "value": "vintage"},
            {"op": "add_text", "text": "Best day", "start": 1, "end": 3, "position": "top"}]}
    return {"reply": "Could you say that another way?", "operations": []}


def answer(system: str, parts: list[dict]) -> str:
    """The AI's text answer for one request (parts: the last user message, Gemini-style)."""
    if "editor inside a phone video-editing app" in system:  # chat editing
        return json.dumps(chat_answer(parts[0]["text"]))
    if any(p_.get("image") for p_ in parts):  # looking at frames
        times = [float(p_["text"][2:-1]) for p_ in parts if p_.get("text", "").startswith("t=")]
        return json.dumps([{"time": t, "text": "a colourful test pattern" if t < 6 else "a red dog on a beach"}
                           for t in times])
    text = parts[0]["text"]
    if "camera shot descriptions" in text:
        return json.dumps(["A wide shot of the scene", "A close-up detail", "A slow pan at sunset"])
    if "Translate each of these" in text:
        lines = json.loads(text[text.index("["):])
        return json.dumps([f"(en) {x}" for x in lines], ensure_ascii=False)
    if "Request:" in text:
        # Stand-in "understanding": echo back whichever words appear in the request.
        req = text.split("Request:", 1)[1].lower()
        o = {"format": "auto", "length": 0, "style": "auto", "look": "auto", "transitions": "auto",
             "slowmo": "auto", "title": "", "captions": "auto", "caption_lang": "auto"}
        for word, key, val in [("wide", "format", "16:9"), ("vertical", "format", "9:16"),
                               ("square", "format", "1:1"), ("cinematic", "look", "cinematic"),
                               ("vintage", "look", "vintage"), ("fast", "style", "fast"),
                               ("slow motion", "slowmo", "on"), ("subtitle", "captions", "on"),
                               ("caption", "captions", "on"), ("punjabi", "caption_lang", "pa"),
                               ("hindi", "caption_lang", "hi")]:
            if word in req:
                o[key] = val
        return "```json\n" + json.dumps(o) + "\n```"  # real services sometimes wrap JSON like this
    return "I don't know."


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, obj=None, raw=None, ctype="application/json"):
        body = raw if raw is not None else json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        LOG.append(("GET", self.path))
        if STATE["offline"]:
            return self._send(503, {"error": "offline"})
        if self.path.startswith("/free/prompt/"):
            return self._send(200, raw=(TMP / "img.png").read_bytes(), ctype="image/png")
        self._send(200 if self.path == "/free/health" else 404, {"ok": True})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        LOG.append(("POST", self.path, body))
        if self.path == "/free/offline":
            STATE["offline"] = bool(body.get("on"))
            return self._send(200, {"ok": True})
        if STATE["offline"]:
            return self._send(503, {"error": "offline"})
        if self.path == "/free/openai":
            msgs = body["messages"]
            system = " ".join(m["content"] for m in msgs if m["role"] == "system")
            last = msgs[-1]["content"]
            parts = [{"text": last}] if isinstance(last, str) else [
                {"text": c["text"]} if c["type"] == "text" else {"image": True} for c in last]
            return self._send(200, {"choices": [{"message": {"role": "assistant",
                                                             "content": answer(system, parts)}}]})
        self._send(404, {"error": {"message": "not found"}})


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8799
    ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()
