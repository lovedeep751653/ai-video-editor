"""A stand-in for Google's AI service, used only by tests. It answers the same
requests the real service does, with real picture/video files, so the whole
AI flow can be checked without spending money. Run: python3 mock_gemini.py PORT"""

import base64
import json
import subprocess
import sys
import tempfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

KEY = "test-key-123"
TMP = Path(tempfile.mkdtemp())
subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=768x1344", "-frames:v", "1",
                str(TMP / "img.png")], check=True)
subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "mandelbrot=s=720x1280:r=24", "-f", "lavfi",
                "-i", "sine=f=330", "-t", "8", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                "-c:a", "aac", str(TMP / "vid.mp4")], check=True)
IMG = base64.b64encode((TMP / "img.png").read_bytes()).decode()
OPS = {}
LOG = []

MODELS = [
    {"name": "models/gemini-2.5-flash", "supportedGenerationMethods": ["generateContent"]},
    {"name": "models/gemini-2.5-flash-lite", "supportedGenerationMethods": ["generateContent"]},
    {"name": "models/gemini-2.5-flash-image", "supportedGenerationMethods": ["generateContent"]},
    {"name": "models/gemini-2.5-flash-preview-tts", "supportedGenerationMethods": ["generateContent"]},
    {"name": "models/imagen-4.0-generate-001", "supportedGenerationMethods": ["predict"]},
    {"name": "models/imagen-4.0-ultra-generate-001", "supportedGenerationMethods": ["predict"]},
    {"name": "models/veo-3.0-generate-001", "supportedGenerationMethods": ["predictLongRunning"]},
    {"name": "models/veo-3.0-fast-generate-001", "supportedGenerationMethods": ["predictLongRunning"]},
]


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

    def _auth(self):
        if self.headers.get("x-goog-api-key") != KEY:
            self._send(400, {"error": {"code": 400, "message": "API key not valid. Please pass a valid API key."}})
            return False
        return True

    def do_GET(self):
        LOG.append(("GET", self.path))
        if not self._auth():
            return
        p = self.path.split("?")[0]
        if p.endswith("/models"):
            return self._send(200, {"models": MODELS})
        if "/operations/" in p:
            n = OPS[p.split("/operations/")[1]] = OPS.get(p.split("/operations/")[1], 0) + 1
            if n < 2:
                return self._send(200, {"name": p, "done": False})
            port = self.server.server_address[1]
            return self._send(200, {"name": p, "done": True, "response": {"generateVideoResponse": {
                "generatedSamples": [{"video": {"uri": f"http://127.0.0.1:{port}/v1beta/files/v1:download?alt=media"}}]}}})
        if "/files/" in p:
            return self._send(200, raw=(TMP / "vid.mp4").read_bytes(), ctype="video/mp4")
        self._send(404, {"error": {"message": "not found"}})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        LOG.append(("POST", self.path, body))
        if not self._auth():
            return
        p = self.path
        if p.endswith(":predict"):
            return self._send(200, {"predictions": [{"bytesBase64Encoded": IMG, "mimeType": "image/png"}]})
        if p.endswith(":predictLongRunning"):
            op = f"op{len(OPS) + 1}"
            OPS[op] = 0
            model = p.split("/models/")[1].split(":")[0]
            return self._send(200, {"name": f"models/{model}/operations/{op}"})
        if p.endswith(":generateContent"):
            cfg = body.get("generationConfig", {})
            parts = body["contents"][-1]["parts"]
            if "systemInstruction" in body:  # chat editing
                return self._send(200, {"candidates": [{"content": {"parts": [{"text": json.dumps(
                    chat_answer(parts[0]["text"]))}]}}]})
            if any(p_.get("inlineData", {}).get("mimeType") == "image/jpeg" for p_ in parts):  # looking at frames
                times = [float(p_["text"][2:-1]) for p_ in parts if p_.get("text", "").startswith("t=")]
                return self._send(200, {"candidates": [{"content": {"parts": [{"text": json.dumps(
                    [{"time": t, "text": "a colourful test pattern" if t < 6 else "a red dog on a beach"}
                     for t in times])}]}}]})
            if "inlineData" in parts[0] and parts[0]["inlineData"]["mimeType"].startswith("audio"):
                out = json.dumps([{"start": 0.4, "end": 2.6, "text": "ਸਤ ਸ੍ਰੀ ਅਕਾਲ ਦੋਸਤੋ, ਅੱਜ ਅਸੀਂ ਪਹਾੜਾਂ ਵਿੱਚ ਹਾਂ"},
                                  {"start": 3.0, "end": 5.5, "text": "यह नज़ारा बहुत सुंदर है"},
                                  {"start": 6.0, "end": 8.0, "text": "This is the best trip ever"}])
                return self._send(200, {"candidates": [{"content": {"parts": [{"text": out}]}}]})
            text = parts[0]["text"]
            if cfg.get("responseSchema", {}).get("type") == "ARRAY":
                out = json.dumps(["A wide shot of the scene", "A close-up detail", "A slow pan at sunset"])
            elif "Request:" in text:
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
                out = json.dumps(o)
            else:
                return self._send(200, {"candidates": [{"content": {"parts": [
                    {"inlineData": {"mimeType": "image/png", "data": IMG}}]}}]})
            return self._send(200, {"candidates": [{"content": {"parts": [{"text": out}]}}]})
        self._send(404, {"error": {"message": "not found"}})


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8799
    ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()
