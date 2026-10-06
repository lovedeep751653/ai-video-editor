"""Checks the web app end to end: access code, settings, editing, captions and
AI creation — using a stand-in for Google's AI so no money is spent.

Run: python3 tests/test_app.py
"""

import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
PORT_AI, PORT_APP = 8891, 8892
BASE = f"http://127.0.0.1:{PORT_APP}"
COOKIE = ""


def ff(*args):
    subprocess.run(["ffmpeg", "-v", "error", "-y", *args], check=True)


def post(path, fields, files=()):
    """Sends a form the same way the phone screen does."""
    boundary = "----videoeditortest"
    body = b""
    for k, v in fields.items():
        body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n").encode()
    for k, p in files:
        body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"; filename=\"{Path(p).name}\"\r\n"
                 f"Content-Type: application/octet-stream\r\n\r\n").encode() + Path(p).read_bytes() + b"\r\n"
    body += f"--{boundary}--\r\n".encode()
    req = urllib.request.Request(BASE + path, data=body, method="POST", headers={
        "Content-Type": f"multipart/form-data; boundary={boundary}", "Cookie": COOKIE})
    return _send(req)


def get(path):
    return _send(urllib.request.Request(BASE + path, headers={"Cookie": COOKIE}))


def _send(req):
    global COOKIE
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            if r.headers.get("Set-Cookie"):
                COOKIE = r.headers["Set-Cookie"].split(";")[0]
            raw = r.read()
            ctype = r.headers.get("Content-Type", "")
            return r.status, (json.loads(raw) if "json" in ctype else raw)
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except json.JSONDecodeError:
            return e.code, raw


def wait(job_id, timeout=600):
    seen = []
    while timeout > 0:
        code, job = get(f"/api/jobs/{job_id}")
        assert code == 200, job
        seen.append(job["progress"])
        if job["state"] == "done":
            assert seen == sorted(seen), "progress went backwards"
            return job
        if job["state"] == "failed":
            raise AssertionError(job["error"])
        time.sleep(1)
        timeout -= 1
    raise AssertionError("job never finished")


def main():
    tmp = Path(tempfile.mkdtemp())
    data = tmp / "data"
    land, photo, mus = tmp / "land.mp4", tmp / "photo.jpg", tmp / "music.mp3"
    ff("-f", "lavfi", "-i", "testsrc2=s=1280x720:r=30:d=6", "-f", "lavfi", "-i", "mandelbrot=s=1280x720:r=30",
       "-f", "lavfi", "-i", "sine=f=440:d=12",
       "-filter_complex", "[1:v]trim=duration=6,setpts=PTS-STARTPTS[m];[0:v][m]concat=n=2:v=1:a=0[v]",
       "-map", "[v]", "-map", "2:a", "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-shortest", str(land))
    ff("-f", "lavfi", "-i", "testsrc=s=1600x1200", "-frames:v", "1", str(photo))
    ff("-f", "lavfi", "-i",
       "aevalsrc='0.8*sin(2*PI*60*t)*exp(-25*mod(t-0.3+10*60/128,60/128))*gt(t,0.3)+0.2*sin(2*PI*330*t)':"
       "s=44100:d=40", "-c:a", "libmp3lame", str(mus))

    ai = subprocess.Popen([sys.executable, str(HERE / "mock_gemini.py"), str(PORT_AI)])
    env = {**os.environ, "GENAI_BASE": f"http://127.0.0.1:{PORT_AI}/v1beta", "APP_PASSWORD": "1234",
           "EDITOR_DATA": str(data)}
    app = subprocess.Popen([sys.executable, "-m", "uvicorn", "server:app", "--host", "127.0.0.1",
                            "--port", str(PORT_APP)], cwd=HERE.parent / "app", env=env,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for url, check in ((f"http://127.0.0.1:{PORT_AI}/v1beta/models", False), (BASE + "/api/health", True)):
            for _ in range(80):
                try:
                    urllib.request.urlopen(url, timeout=2)
                    break
                except urllib.error.HTTPError:
                    break  # answered (400 without a key) → it is up
                except Exception:
                    time.sleep(0.5)
            else:
                raise AssertionError(f"{url} never came up")

        # Access code
        assert get("/api/settings")[0] == 401, "settings reachable without the access code"
        assert post("/api/login", {"code": "nope"})[0] == 401
        assert post("/api/login", {"code": "1234"})[0] == 200
        assert get("/api/settings")[0] == 200

        # Google AI key
        code, res = post("/api/settings", {"gemini_key": "bad"})
        assert code == 400 and "key" in res["detail"].lower(), res
        code, res = post("/api/settings", {"gemini_key": "test-key-123"})
        assert code == 200 and res["ai_ready"], res
        assert res["models"]["image"] == "imagen-4.0-generate-001", res["models"]
        assert res["models"]["video"] == "veo-3.0-fast-generate-001", res["models"]
        assert res["models"]["text"] == "gemini-2.5-flash", res["models"]

        # Edit with music + typed request + captions
        code, res = post("/api/jobs", {"request": "make it wide and cinematic", "captions": "on",
                                       "caption_lang": "pa", "caption_style": "neonpink-pop"},
                         [("files", land), ("files", photo), ("music", mus)])
        assert code == 200, res
        job = wait(res["id"])
        r = job["result"]
        assert r["format"] == "16:9" and r["look"] == "cinematic", r["format"]
        assert r["music"]["synced"], r["music"]
        assert r["captions"], "no caption lines came back"
        assert any("Captions added" in n for n in r["notes"]), r["notes"]
        assert get(f"/api/jobs/{res['id']}/video")[0] == 200
        assert get(f"/api/jobs/{res['id']}/thumb")[0] == 200

        # Bad settings are refused
        assert post("/api/jobs", {"format": "4:3"}, [("files", photo)])[0] == 400
        assert post("/api/jobs", {"length": "900"}, [("files", photo)])[0] == 400
        assert post("/api/jobs", {"caption_style": "nope"}, [("files", photo)])[0] == 400

        # AI pictures
        code, res = post("/api/create", {"mode": "image", "prompt": "a sunset", "format": "9:16", "count": "2"})
        assert code == 200, res
        job = wait(res["id"])
        assert len(job["result"]["files"]) == 2, job["result"]
        name = job["result"]["files"][0]["name"]
        assert get(f"/api/jobs/{res['id']}/files/{name}")[0] == 200
        assert get(f"/api/jobs/{res['id']}/files/../status.json")[0] in (404, 400)

        # Full video from AI clips
        code, res = post("/api/create", {"mode": "story", "prompt": "a day in the mountains", "format": "9:16",
                                         "source": "clips", "count": "2", "request": "vintage"})
        assert code == 200, res
        job = wait(res["id"], 900)
        assert job["result"]["look"] == "vintage", job["result"]["look"]
        assert job["result"]["format"] == "9:16", "the chosen shape should be kept"
        assert len(job["result"]["shots"]) == 2
        assert get(f"/api/jobs/{res['id']}/video")[0] == 200

        # Caption styles and previews
        code, res = get("/api/caption-styles")
        assert code == 200 and len(res["styles"]) >= 40
        for lang in ("en", "hi", "pa"):
            code, png = get(f"/api/caption-preview/{res['styles'][0]['id']}?lang={lang}")
            assert code == 200 and len(png) > 2000, f"preview failed for {lang}"
        assert get("/api/caption-preview/not-a-style")[0] == 404
        print("all app tests passed")
    finally:
        app.terminate()
        ai.terminate()


if __name__ == "__main__":
    main()
