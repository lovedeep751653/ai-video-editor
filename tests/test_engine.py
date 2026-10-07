"""End-to-end checks of the automatic editor using generated sample media.

Run: python3 -m pytest tests  (or: python3 tests/test_engine.py)
"""

import json
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
from engine import captions, edits, intent, music, pipeline, render  # noqa: E402
from engine.plan import Plan  # noqa: E402


def ff(*args):
    subprocess.run(["ffmpeg", "-v", "error", "-y", *args], check=True)


def make_samples(d: Path) -> dict:
    s = {k: d / v for k, v in {
        "land": "landscape.mp4", "port": "portrait.mp4", "rot": "rotated.mp4",
        "photo": "photo.jpg", "photo2": "photo2.png", "bad": "notes.txt", "talk": "talk.mp4"}.items()}
    # 30 s of "talking" (3.5 s of sound, 2 s of silence, repeated) for the clean-up mode
    ff("-f", "lavfi", "-i", "testsrc2=s=1280x720:r=30:d=30", "-f", "lavfi", "-i",
       "aevalsrc='if(lt(mod(t,5.5),3.5),0.4*sin(2*PI*220*t)*(0.6+0.4*sin(2*PI*3*t)),0.001*sin(2*PI*50*t))':s=48000:d=30",
       "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-shortest", str(s["talk"]))
    # 6s test pattern, 4s black (boring), 6s fractal; with a tone
    ff("-f", "lavfi", "-i", "testsrc2=s=1280x720:r=30:d=6", "-f", "lavfi", "-i", "color=black:s=1280x720:r=30:d=4",
       "-f", "lavfi", "-i", "mandelbrot=s=1280x720:r=30", "-f", "lavfi", "-i", "sine=f=440:d=16",
       "-filter_complex", "[2:v]trim=duration=6,setpts=PTS-STARTPTS[m];[0:v][1:v][m]concat=n=3:v=1:a=0[v]",
       "-map", "[v]", "-map", "3:a", "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-shortest", str(s["land"]))
    ff("-f", "lavfi", "-i", "life=s=720x1280:r=30:mold=10:ratio=0.1:death_color=#202020:life_color=#ffcc00,format=yuv420p",
       "-t", "8", "-c:v", "libx264", "-preset", "ultrafast", str(s["port"]))
    ff("-display_rotation", "90", "-i", str(s["land"]), "-c", "copy", str(s["rot"]))
    ff("-f", "lavfi", "-i", "testsrc=s=1600x1200", "-frames:v", "1", str(s["photo"]))
    ff("-f", "lavfi", "-i", "smptehdbars=s=900x1600", "-frames:v", "1", str(s["photo2"]))
    s["bad"].write_text("not a video")
    s["music"] = d / "music.mp3"
    ff("-f", "lavfi", "-i",
       "aevalsrc='0.8*sin(2*PI*60*t)*exp(-25*mod(t-0.3+10*60/128,60/128))*gt(t,0.3)+0.15*sin(2*PI*330*t)"
       "+0.1*sin(2*PI*(440+110*floor(mod(t,4)))*t)':s=44100:d=40", "-c:a", "libmp3lame", str(s["music"]))
    return s


def info(path: Path) -> dict:
    out = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", str(path)],
                         capture_output=True, text=True, check=True).stdout
    streams = json.loads(out)["streams"]
    v = [x for x in streams if x["codec_type"] == "video"][0]
    a = [x for x in streams if x["codec_type"] == "audio"]
    return {"w": v["width"], "h": v["height"], "dur": float(v["duration"]),
            "adur": float(a[0]["duration"]) if a else 0.0}


def run_case(s, files, opts, work, music_path=None, mode="highlight"):
    stages = []
    sources = []
    for i, f in enumerate(files):
        d = work / "src" / str(i)
        d.mkdir(parents=True)
        sources.append({"id": f"s{i}", "name": s[f].name, "path": str(s[f]), "dir": str(d)})
    state = {"mode": mode, "sources": sources, "version": 0,
             "music": {"path": str(music_path), "name": music_path.name} if music_path else None,
             "settings": {"captions": opts.get("captions", "off"), "caption_lang": "auto",
                          "caption_style": captions.DEFAULT_STYLE, "caption_pos": "bottom"}}
    ver, media = pipeline.first_edit(state, work, lambda st, f: stages.append(f), ("", {}), "best", opts)
    assert stages == sorted(stages), "progress must only go up"
    assert stages[-1] == 1.0
    r = {**ver["plan"], "skipped": state["skipped"], "total": ver["duration"], "state": state, "media": media,
         "ver": ver}
    return r, info(work / ver["file"])


def test_all():
    with tempfile.TemporaryDirectory() as t:
        t = Path(t)
        s = make_samples(t)

        # Auto everything: mostly portrait footage → 9:16; black section left out
        r, v = run_case(s, ["land", "port", "rot", "photo", "photo2"], {}, t / "a")
        assert r["format"] == "9:16" and (v["w"], v["h"]) == (1080, 1920)
        for c in r["clips"]:
            if c["name"] == "landscape.mp4":
                assert not (c["start"] < 10 and c["start"] + c["duration"] > 6.5), "boring black part was used"
        assert abs(v["dur"] - r["total"]) < 0.3

        # Wide 16:9, photo first (silent start) then sound
        r, v = run_case(s, ["photo", "land"], {"format": "16:9", "length": "15"}, t / "b")
        assert (v["w"], v["h"]) == (1920, 1080)

        # Square, fast, silent video plus an unreadable file
        r, v = run_case(s, ["port", "bad"], {"format": "1:1", "style": "fast", "length": "15"}, t / "c")
        assert (v["w"], v["h"]) == (1080, 1080) and r["style"] == "fast" and len(r["skipped"]) == 1

        # A single photo
        r, v = run_case(s, ["photo"], {}, t / "d")
        assert r["format"] == "16:9" and v["dur"] > 2

        # Music: every cut lands on a beat
        r, v = run_case(s, ["land", "port"], {"format": "9:16"}, t / "m", music_path=s["music"])
        assert r["music"]["synced"], "beat sync should have been found"
        assert abs(v["dur"] - v["adur"]) < 0.1
        beat = 60 / r["music"]["bpm"]
        offset = 0.0
        for c in r["clips"][:-1]:
            offset += c["duration"] - r["transition"]
            assert abs(offset / beat - round(offset / beat)) < 0.02, "a cut missed the beat"
        assert abs(v["dur"] - r["total"]) < 0.3

        # Every look, transition set and slow motion renders
        for look in ["cinematic", "vintage", "bw", "vivid"]:
            r, v = run_case(s, ["land"], {"look": look, "length": "8", "slowmo": "on",
                                          "transitions": "dynamic", "title": "Test Title"}, t / f"l_{look}")
            assert v["dur"] > 5
        r, v = run_case(s, ["land", "port"], {"transitions": "cuts", "style": "fast", "length": "10"}, t / "cuts")
        assert r["transition"] == 0 and abs(v["dur"] - r["total"]) < 0.3

        # Captions without an AI key: says so instead of failing
        r, v = run_case(s, ["land"], {"captions": "on", "length": "8"}, t / "nocap")
        assert any("Google AI key" in n for n in r["notes"])

        # Nothing usable → clear error
        try:
            run_case(s, ["bad"], {}, t / "e")
            raise AssertionError("expected an error")
        except ValueError as e:
            assert "None of the files could be opened" in str(e)

        # Long-video clean-up: pauses cut out, story kept in order, picture and sound in step
        r, v = run_case(s, ["talk"], {}, t / "clean", mode="cleanup")
        assert r["mode"] == "cleanup" and len(r["clips"]) >= 4, r["clips"]
        assert all(a["start"] < b["start"] for a, b in zip(r["clips"], r["clips"][1:])), "order changed"
        assert any("Pauses and silences removed" in n for n in r["notes"]), r["notes"]
        assert (v["w"], v["h"]) == (1280, 720), "clean-up keeps the original shape"
        assert 16 < v["dur"] < 26 and abs(v["dur"] - r["total"]) < 0.15, (v["dur"], r["total"])
        assert abs(v["dur"] - v["adur"]) < 0.1, "sound and picture lengths differ"

        # Chat edits on that video: commands understood without AI, applied, rendered again quickly
        state, media = r["state"], r["media"]
        plan = Plan.from_dict(r["ver"]["plan"])
        ops, ok = edits.parse_local("remove clip 2 and make it black and white", plan)
        assert ok and {o["op"] for o in ops} == {"remove_clips", "set_look"}, ops
        res = edits.apply(ops, plan, media, state["settings"])
        assert len(res.plan.clips) == len(plan.clips) - 1 and res.plan.look == "bw"
        ops, _ = edits.parse_local("cut from 0:02 to 0:04, add text 'Hello ਦੋਸਤੋ' at 1 for 2 seconds", plan)
        res = edits.apply(ops, plan, media, state["settings"])
        assert abs(res.plan.total - (plan.total - 2)) < 0.2 and res.plan.texts[0]["text"] == "Hello ਦੋਸਤੋ", res.done
        ver = pipeline.render_version(state, res.plan, media, t / "clean", lambda *a: None, ("", {}), "best", "test")
        v2 = info(t / "clean" / ver["file"])
        assert abs(v2["dur"] - (v["dur"] - 2)) < 0.25, (v2["dur"], v["dur"])
        ops, _ = edits.parse_local("slow motion on clip 1", plan)
        res = edits.apply(ops, plan, media, state["settings"])
        ver = pipeline.render_version(state, res.plan, media, t / "clean", lambda *a: None, ("", {}), "best", "slow")
        assert ver["reused"] >= 1, "unchanged parts should be reused, not made again"
        for msg, op in [("undo", "undo"), ("make it 10 seconds", "shorten_to"), ("remove the pauses", "remove_pauses"),
                        ("move clip 3 to the start", "move_clip"), ("captions in punjabi", "caption_language"),
                        ("title 'Goa'", "set_title"), ("music quieter", "music_volume"), ("zoom in on clip 2", "zoom")]:
            ops, ok = edits.parse_local(msg, plan)
            assert ok and op in [o["op"] for o in ops], (msg, ops)
        assert not edits.parse_local("tell me a joke", plan)[1]
    # Beat detection: right tempo on music, and honest about noise
    with tempfile.TemporaryDirectory() as t2:
        t2 = Path(t2)
        s2 = {"music": t2 / "m.mp3", "noise": t2 / "n.mp3"}
        ff("-f", "lavfi", "-i",
           "aevalsrc='0.8*sin(2*PI*60*t)*exp(-25*mod(t-0.3+10*60/95,60/95))*gt(t,0.3)+0.2*sin(2*PI*330*t)'"
           ":s=44100:d=30", "-c:a", "libmp3lame", str(s2["music"]))
        ff("-f", "lavfi", "-i", "anoisesrc=d=10:c=pink", str(s2["noise"]))
        m = music.analyze(s2["music"])
        assert abs(m.bpm - 95) < 2 and m.has_beat, f"wrong tempo {m.bpm}"
        assert not music.analyze(s2["noise"]).has_beat, "noise should not count as a beat"

    # Typed requests
    o = intent.clean(intent.parse_keywords('cinematic 30 second vertical video with punjabi subtitles'))
    assert o == {"format": "9:16", "length": "30.0", "look": "cinematic", "captions": "on", "caption_lang": "pa"}, o
    o = intent.clean(intent.parse_keywords('square black and white, no transitions, title "Goa 2026"'))
    assert o["format"] == "1:1" and o["look"] == "bw" and o["transitions"] == "cuts" and o["title"] == "Goa 2026"

    # Caption styles: all 45 build valid subtitle files in all three scripts
    texts = {"en": "This is my best day", "hi": "यह मेरा सबसे अच्छा दिन है", "pa": "ਇਹ ਮੇਰਾ ਸਭ ਤੋਂ ਵਧੀਆ ਦਿਨ ਹੈ"}
    assert len(captions.STYLES) >= 40, "expected at least 40 caption styles"
    for sid in captions.STYLES:
        for lang, text in texts.items():
            ass = captions.build_ass([{"start": 0, "end": 2, "text": text}], sid, 1080, 1920)
            word = text.split()[0]
            assert "Dialogue:" in ass and (word in ass or word.upper() in ass)
            font = ass.split("Style: Cap,")[1].split(",")[0]
            assert (Path(__file__).resolve().parents[1] / "app" / "fonts").exists()
            assert font, f"no font for {sid}/{lang}"
    print("all engine tests passed")


if __name__ == "__main__":
    test_all()
