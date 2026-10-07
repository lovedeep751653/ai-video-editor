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


def post_json(path, obj, method="POST"):
    req = urllib.request.Request(BASE + path, data=json.dumps(obj).encode(), method=method, headers={
        "Content-Type": "application/json", "Cookie": COOKIE})
    return _send(req)


def get(path, headers=None):
    if headers:
        return _send(urllib.request.Request(BASE + path, headers={"Cookie": COOKIE, **headers}))
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
    talk, junk = tmp / "talk.mp4", tmp / "notes.txt"
    junk.write_text("not a video")
    ff("-f", "lavfi", "-i", "testsrc2=s=1280x720:r=30:d=12", "-f", "lavfi", "-i",
       "aevalsrc='if(lt(mod(t,4),2.6),0.4*sin(2*PI*220*t)*(0.6+0.4*sin(2*PI*3*t)),0)':s=48000:d=12",
       "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-shortest", str(talk))
    ff("-f", "lavfi", "-i", "testsrc2=s=1280x720:r=30:d=6", "-f", "lavfi", "-i", "mandelbrot=s=1280x720:r=30",
       "-f", "lavfi", "-i", "sine=f=440:d=12",
       "-filter_complex", "[1:v]trim=duration=6,setpts=PTS-STARTPTS[m];[0:v][m]concat=n=2:v=1:a=0[v]",
       "-map", "[v]", "-map", "2:a", "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-shortest", str(land))
    ff("-f", "lavfi", "-i", "testsrc=s=1600x1200", "-frames:v", "1", str(photo))
    ff("-f", "lavfi", "-i",
       "aevalsrc='0.8*sin(2*PI*60*t)*exp(-25*mod(t-0.3+10*60/128,60/128))*gt(t,0.3)+0.2*sin(2*PI*330*t)':"
       "s=44100:d=40", "-c:a", "libmp3lame", str(mus))

    ai = subprocess.Popen([sys.executable, str(HERE / "mock_gemini.py"), str(PORT_AI)])
    env = {**os.environ, "FREEAI_TEXT_BASES": f"http://127.0.0.1:{PORT_AI}/free/openai", "APP_PASSWORD": "1234",
           "SPEECH_FAKE": json.dumps({"text": "ਸਤ ਸ੍ਰੀ ਅਕਾਲ ਦੋਸਤੋ ਅੱਜ ਅਸੀਂ ਪਹਾੜਾਂ ਵਿੱਚ ਹਾਂ"}),
           "FREEAI_BASES": f"http://127.0.0.1:{PORT_AI}/free/prompt/", "FREEAI_HORDE": "http://127.0.0.1:9/none",
           "EDITOR_DATA": str(data)}
    env.update(HOST="127.0.0.1", PORT=str(PORT_APP))
    app = subprocess.Popen([sys.executable, "server.py"], cwd=HERE.parent / "app", env=env,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for url, check in ((f"http://127.0.0.1:{PORT_AI}/free/health", False), (BASE + "/api/health", True)):
            for _ in range(80):
                try:
                    urllib.request.urlopen(url, timeout=2)
                    break
                except urllib.error.HTTPError:
                    break  # answered → it is up
                except Exception:
                    time.sleep(0.5)
            else:
                raise AssertionError(f"{url} never came up")

        # Access code
        assert get("/api/settings")[0] == 401, "settings reachable without the access code"
        assert post_json("/api/login", {"code": "nope"})[0] == 401
        assert post_json("/api/login", {"code": "1234"})[0] == 200
        assert get("/api/settings")[0] == 200

        # No key anywhere: settings are only quality and speed
        code, res = get("/api/settings")
        assert code == 200 and set(res) == {"quality", "speed"}, res

        # Adding files
        code, res = post("/api/sources", {}, [("files", land), ("files", photo), ("files", talk), ("files", mus),
                                              ("files", junk)])
        assert code == 200, res
        src = {x["name"]: x for x in res["sources"]}
        assert src["land.mp4"]["kind"] == "video" and src["photo.jpg"]["kind"] == "image"
        assert src["music.mp3"]["kind"] == "audio" and src["notes.txt"]["error"], res
        assert get(src["land.mp4"]["thumb"])[0] == 200

        # Bad settings are refused
        ids = [src["land.mp4"]["id"]]
        assert post_json("/api/projects", {"sources": ids, "options": {"format": "4:3"}})[0] == 400
        assert post_json("/api/projects", {"sources": ids, "options": {"length": "4000"}})[0] == 400
        assert post_json("/api/projects", {"sources": ids, "options": {"caption_style": "nope"}})[0] == 400
        assert post_json("/api/projects", {"sources": []})[0] == 400

        # Highlight with music + typed request + captions in Punjabi, at 720p
        assert post_json("/api/settings", {"quality": "720"})[1]["quality"] == "720"
        code, res = post_json("/api/projects", {
            "sources": [src["land.mp4"]["id"], src["photo.jpg"]["id"], src["talk.mp4"]["id"]],
            "music": src["music.mp3"]["id"], "mode": "highlight", "request": "make it wide and cinematic",
            "options": {"captions": "on", "caption_lang": "pa", "caption_style": "neonpink-pop"}})
        assert code == 200, res
        wait(res["job"])
        code, p = get(f"/api/projects/{res['project']}")
        assert p["plan"]["format"] == "16:9" and p["plan"]["look"] == "cinematic", p["plan"]
        assert (p["plan"]["width"], p["plan"]["height"]) == (1280, 720)
        assert p["plan"]["music"] and p["plan"]["captions"], p["plan"]
        assert any("Captions" in n for n in p["plan"]["notes"]), p["plan"]["notes"]
        assert p["chat"][0]["role"] == "user" and p["chat"][-1]["role"] == "assistant"
        code, part = get(p["video"], {"Range": "bytes=0-999"})
        assert code == 206 and len(part) == 1000, "video seeking (Range) must work"
        assert get(p["download"])[0] == 200 and get(p["thumb"])[0] == 200
        assert get(p["plan"]["clips"][0]["thumb"])[0] == 200

        # Clean-up of a talking video, then chat with the (stand-in) AI editor
        code, res = post_json("/api/projects", {"sources": [src["talk.mp4"]["id"], src["land.mp4"]["id"]],
                                                "mode": "cleanup"})
        wait(res["job"])
        pid = res["project"]
        code, p = get(f"/api/projects/{pid}")
        assert p["mode"] == "cleanup" and p["version"] == 1 and len(p["plan"]["clips"]) >= 3, p["plan"]
        n_clips = len(p["plan"]["clips"])

        def chat(msg):
            code, r = post_json(f"/api/projects/{pid}/chat", {"message": msg})
            assert code == 200, r
            wait(r["job"])
            return get(f"/api/projects/{pid}")[1]

        p = chat("what is in my video?")
        assert p["version"] == 1 and "clips" in p["chat"][-1]["text"], p["chat"][-1]
        p = chat("remove the dog")  # the AI was shown pictures of the video: the "dog" is in land.mp4 after 6 s
        assert p["version"] == 2 and len(p["plan"]["clips"]) < n_clips, p["chat"][-1]
        p = chat("make it vintage with some text")
        assert p["version"] == 3 and p["plan"]["look"] == "vintage" and p["plan"]["texts"], p["plan"]
        p = chat("captions in punjabi please")
        assert p["version"] == 4 and p["plan"]["captions"] and "ਪੰਜਾਬੀ" in p["chat"][-1]["text"]
        p = chat("undo")
        assert p["version"] == 3, p["version"]
        code, r = post_json(f"/api/projects/{pid}/restore", {"version": 1})
        wait(r["job"])
        assert get(f"/api/projects/{pid}")[1]["version"] == 1
        code, r = post_json(f"/api/projects/{pid}/clips", {"action": "remove", "clip": 0})
        wait(r["job"])
        p = get(f"/api/projects/{pid}")[1]
        assert p["version"] == 5 and p["chat"][-1]["text"].startswith("Done"), p["chat"][-1]

        # Offline the chat still understands the common commands
        offline = lambda on: urllib.request.urlopen(urllib.request.Request(  # noqa: E731
            f"http://127.0.0.1:{PORT_AI}/free/offline", data=json.dumps({"on": on}).encode(), method="POST"))
        offline(True)
        p = chat("remove clip 1 and make it black and white")
        assert p["plan"]["look"] == "bw" and p["version"] == 6, p["chat"][-1]
        p = chat("tell me a joke")
        assert "Without internet" in p["chat"][-1]["text"], p["chat"][-1]
        offline(False)

        # Cancelling
        code, res = post_json("/api/projects", {"sources": [src["talk.mp4"]["id"]], "mode": "highlight"})
        post_json(f"/api/jobs/{res['job']}/cancel", {})
        job = get(f"/api/jobs/{res['job']}")[1]
        for _ in range(60):
            if job["state"] in ("cancelled", "done", "failed"):
                break
            time.sleep(0.5)
            job = get(f"/api/jobs/{res['job']}")[1]
        assert job["state"] == "cancelled", job

        # Projects list and delete
        code, res = get("/api/projects")
        assert code == 200 and len(res["projects"]) >= 3
        assert post_json(f"/api/projects/{pid}", {}, method="DELETE")[0] == 200
        assert get(f"/api/projects/{pid}")[0] == 404

        # AI pictures
        code, res = post_json("/api/create", {"mode": "image", "prompt": "a sunset", "format": "9:16", "count": 2})
        assert code == 200, res
        job = wait(res["job"])
        files = job["result"]["files"]
        assert len(files) == 2 and get(files[0]["url"])[0] == 200, files

        # Full video from AI clips
        code, res = post_json("/api/create", {"mode": "story", "prompt": "a day in the mountains", "format": "9:16",
                                              "source": "clips", "count": 2, "request": "vintage"})
        assert code == 200, res
        job = wait(res["job"], 900)
        wait(job["result"]["edit_job"], 900)
        p = get(f"/api/projects/{job['result']['project']}")[1]
        assert p["plan"]["look"] == "vintage" and p["plan"]["format"] == "9:16", p["plan"]
        assert len(job["result"]["shots"]) == 2

        # Caption styles and previews
        code, res = get("/api/caption-styles")
        assert code == 200 and len(res["styles"]) >= 40
        for lang in ("en", "hi", "pa"):
            code, png = get(f"/api/caption-preview/{res['styles'][0]['id']}?lang={lang}")
            assert code == 200 and len(png) > 2000, f"preview failed for {lang}"
        assert get("/api/caption-preview/not-a-style")[0] == 404
        # More free AI pictures and video clips
        code, res = post_json("/api/create", {"mode": "image", "prompt": "a sunset", "format": "16:9", "count": 1})
        assert code == 200, res
        files = wait(res["job"])["result"]["files"]
        assert len(files) == 1 and files[0]["kind"] == "image" and get(files[0]["url"])[0] == 200, files
        code, res = post_json("/api/create", {"mode": "video", "prompt": "a kite festival", "format": "9:16", "count": 2})
        assert code == 200, res
        files = wait(res["job"], 600)["result"]["files"]
        assert len(files) == 2 and all(f["kind"] == "video" for f in files), files
        code, mp4 = get(files[1]["url"])
        assert code == 200 and len(mp4) > 50_000, len(mp4)
        print("all app tests passed")
    finally:
        app.terminate()
        ai.terminate()


if __name__ == "__main__":
    main()
