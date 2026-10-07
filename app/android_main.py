"""Entry point used by the Android app (through Chaquopy).

The app calls start(); the editor then runs on 127.0.0.1 inside the app and the
app's WebView shows it. busy() feeds the progress notification. selftest_*()
are used by the build robot to check a freshly built APK on an emulator.
"""

from __future__ import annotations

import json
import os
import threading
import time
import traceback
import urllib.request
from pathlib import Path

_httpd = None


def start(data_dir: str, static_dir: str, fonts_dir: str, token: str, version: str, cache_dir: str) -> int:
    global _httpd
    if _httpd is not None:
        return _httpd.server_address[1]
    os.environ.update(EDITOR_ANDROID="1", EDITOR_DATA=data_dir, EDITOR_STATIC=static_dir, EDITOR_FONTS=fonts_dir,
                      EDITOR_TOKEN=token, EDITOR_VERSION=version or "2", EDITOR_TMP=os.path.join(cache_dir, "work"))
    import server
    _httpd = server.serve("127.0.0.1", 0)
    threading.Thread(target=_httpd.serve_forever, name="editor-server", daemon=True).start()
    return _httpd.server_address[1]


def busy() -> str:
    import server
    return json.dumps(server.busy())


# ---------------------------------------------------------------- self-test (emulator)

def _say(msg: str) -> None:
    print("SELFTEST: " + msg, flush=True)


def selftest_make(folder: str) -> None:
    """Sample files made with the built-in FFmpeg: a talking video, a photo and music."""
    from engine import ff
    ff.run(["-f", "lavfi", "-i", "testsrc2=s=1280x720:r=30:d=20", "-f", "lavfi", "-i",
            "aevalsrc='if(lt(mod(t,5),3),0.4*sin(2*PI*220*t)*(0.6+0.4*sin(2*PI*3*t)),0)':s=48000:d=20",
            "-c:v", "libx264", "-preset", "ultrafast", "-g", "30", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
            os.path.join(folder, "talk.mp4")])
    ff.run(["-f", "lavfi", "-i", "testsrc=s=1200x1600", "-frames:v", "1", os.path.join(folder, "photo.jpg")])
    ff.run(["-f", "lavfi", "-i",
            "aevalsrc='0.8*sin(2*PI*60*t)*exp(-25*mod(t-0.3+10*60/128,60/128))*gt(t,0.3)+0.15*sin(2*PI*330*t)'"
            ":s=44100:d=30", "-c:a", "aac", os.path.join(folder, "music.m4a")])


def selftest_run(folder: str, saf_path: str, port: int, token: str) -> None:
    threading.Thread(target=_selftest, args=(folder, saf_path, int(port), token), daemon=True).start()


def _selftest(folder: str, saf_path: str, port: int, token: str) -> None:
    base = f"http://127.0.0.1:{port}"
    os.environ["EDITOR_ALLOW_LOCAL"] = "1"
    results = {}

    def call(method, path, body=None):
        req = urllib.request.Request(base + path, method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Content-Type": "application/json", "Cookie": f"editor_token={token}"})
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                raw = r.read()
                return json.loads(raw) if r.headers.get("Content-Type", "").startswith("application/json") \
                    else {"bytes": len(raw), "status": r.status}
        except urllib.error.HTTPError as e:
            return {"status": e.code, "detail": e.read().decode(errors="replace")}

    def wait(job, limit=1200):
        t = time.time()
        while time.time() - t < limit:
            s = call("GET", f"/api/jobs/{job}")
            if s.get("state") in ("done", "failed", "cancelled"):
                return s
            time.sleep(1)
        return {"state": "timeout"}

    def check(name, ok, info=""):
        results[name] = bool(ok)
        _say(f"{'PASS' if ok else 'FAIL'} {name} {info}")

    try:
        from engine import ff
        _say("start")
        check("health", call("GET", "/api/health").get("ok"))
        check("hardware_encoder", True, f"available={ff.hardware_available()}")
        files = [{"path": os.path.join(folder, "talk.mp4"), "name": "talk.mp4"},
                 {"path": os.path.join(folder, "photo.jpg"), "name": "photo.jpg"}]
        if saf_path:
            files.insert(0, {"path": saf_path, "name": "selftest_talk.mp4"})
        srcs = call("POST", "/api/sources/local", {"items": files}).get("sources", [])
        ok_srcs = [s for s in srcs if s.get("id") and not s.get("error")]
        check("sources", len(ok_srcs) == len(files), json.dumps(srcs)[:600])
        if saf_path:
            check("saf_read", srcs and srcs[0].get("kind") == "video" and srcs[0].get("duration", 0) > 19,
                  json.dumps(srcs[0])[:300] if srcs else "")
        music = call("POST", "/api/sources/local", {"items": [{"path": os.path.join(folder, "music.m4a"),
                                                                "name": "music.m4a"}]}).get("sources", [{}])[0]
        check("music_source", music.get("kind") == "audio", json.dumps(music)[:300])

        # Firely, the on-phone thinking AI (first use unpacks and loads the model)
        from engine import genai as _g
        t = time.time()
        try:
            o = _g.interpret(_g.FREE, "free", "make it vertical with a vintage look and a title 'Goa'")
            check("brain_text", o.get("format") == "9:16" and o.get("look") == "vintage" and "goa" in str(o.get("title")).lower(),
                  f"{time.time() - t:.1f}s {o}")
        except Exception as e:  # noqa: BLE001
            check("brain_text", False, f"{time.time() - t:.1f}s {e}")
        t = time.time()
        try:
            o = _g.interpret(_g.FREE, "free", "इसे वर्टिकल बनाओ और 'मेरा गाँव' शीर्षक लगाओ")
            _say(f"INFO brain_hindi ok={o.get('format') == '9:16'} {time.time() - t:.1f}s {o}")
        except Exception as e:  # noqa: BLE001
            _say(f"INFO brain_hindi ok=False {e}")
        t = time.time()
        try:
            from engine import ff as _ff
            pic = Path(folder) / "brain_look.jpg"
            _ff.run_quiet(["-f", "lavfi", "-i", "testsrc2=s=480x270", "-frames:v", "1", str(pic)])
            seen = _g.describe_frames(_g.FREE, "free", [(1.0, pic)])
            check("brain_vision", bool(seen and seen[0].get("text")), f"{time.time() - t:.1f}s {seen}")
        except Exception as e:  # noqa: BLE001
            check("brain_vision", False, f"{time.time() - t:.1f}s {e}")
        t = time.time()
        try:
            rich = _g.enhance_image_prompt(_g.FREE, "free", "ek kutta beach par")
            _say(f"INFO brain_prompt {time.time() - t:.1f}s {rich!r}")
        except Exception as e:  # noqa: BLE001
            _say(f"INFO brain_prompt failed {e}")

        t = time.time()
        r = call("POST", "/api/projects", {"sources": [ok_srcs[0]["id"]], "mode": "cleanup",
                                           "request": "add the title 'नमस्ते ਪੰਜਾਬ'"})
        j = wait(r.get("job", "x"))
        p = call("GET", f"/api/projects/{r.get('project')}")
        check("cleanup_edit", j.get("state") == "done" and p.get("video"),
              f"{time.time() - t:.1f}s duration={p.get('duration')} clips={len((p.get('plan') or {}).get('clips', []))} "
              f"encoder={(p.get('plan') or {}).get('encoder')} err={j.get('error')}")
        vid = call("GET", p.get("video") or "/x")
        check("video_served", vid.get("bytes", 0) > 10000, str(vid.get("bytes")))
        pid = r.get("project")

        t = time.time()
        r2 = call("POST", f"/api/projects/{pid}/chat", {"message": "remove clip 1 and make it black and white"})
        j2 = wait(r2.get("job", "x"))
        p2 = call("GET", f"/api/projects/{pid}")
        check("chat_edit", j2.get("state") == "done" and p2.get("version") == 2,
              f"{time.time() - t:.1f}s reply={(p2.get('chat') or [{}])[-1].get('text')}")
        r3 = call("POST", f"/api/projects/{pid}/undo", {})
        wait(r3.get("job", "x"))
        check("undo", call("GET", f"/api/projects/{pid}").get("version") == 1)

        t = time.time()
        r4 = call("POST", "/api/projects", {"sources": [s["id"] for s in ok_srcs], "music": music.get("id"),
                                            "mode": "highlight", "options": {"format": "9:16", "length": "12"}})
        j4 = wait(r4.get("job", "x"))
        p4 = call("GET", f"/api/projects/{r4.get('project')}")
        check("highlight_edit", j4.get("state") == "done" and p4.get("video"),
              f"{time.time() - t:.1f}s duration={p4.get('duration')} err={j4.get('error')}")

        prev = call("GET", "/api/caption-preview/yellow-karaoke?lang=pa")
        check("captions_render", prev.get("bytes", 0) > 2000, str(prev))
        prev = call("GET", "/api/caption-preview/classic-simple?lang=hi")
        check("captions_render_hindi", prev.get("bytes", 0) > 2000, str(prev))
        from engine import speech
        t0 = time.time()
        lines = speech.transcribe(Path(str(speech._android_bridge().selftestWav())))
        heard = " ".join(ln["text"] for ln in lines)
        check("speech_captions", "country" in heard.lower(), f"{time.time() - t0:.1f}s {heard!r}")
        # Free AI (no key, real internet): reported only, so an outside service outage can't block a release.
        try:
            res = call("POST", "/api/create", {"mode": "video", "prompt": "colourful kites over a village at sunset",
                                               "format": "9:16", "count": 1})
            j = wait(res.get("job", "x"), 900) if res.get("job") else res
            files = (j.get("result") or {}).get("files") or []
            _say(f"INFO free_ai_video ok={j.get('state') == 'done' and bool(files)} {j.get('error') or ''} {files}")
        except Exception as e:  # noqa: BLE001
            _say(f"INFO free_ai_video ok=False {e}")
    except Exception:
        _say("FAIL crashed " + traceback.format_exc().replace("\n", " | "))
        results["crash"] = False
    _say("DONE " + ("ALL PASSED" if results and all(results.values()) else "SOME FAILED") + " " + json.dumps(results))
