"""One door to FFmpeg for the whole engine.

On a computer this runs the `ffmpeg` / `ffprobe` programs. Inside the Android
app there are no programs to run, so the same commands go to FFmpegKit (FFmpeg
built into the app) through Chaquopy. Nothing here streams through pipes:
results are written to files, and progress is read from FFmpeg's own progress
file, which works the same way in both places.

A job can be cancelled from another thread: `cancel_scope()` gives the running
thread an event; setting it stops the FFmpeg run in progress.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import threading
import time
from pathlib import Path

ANDROID = os.environ.get("EDITOR_ANDROID") == "1"


class FFError(Exception):
    """FFmpeg failed; the message is its last error line."""


class Cancelled(Exception):
    """The user cancelled the job."""


_local = threading.local()


def set_cancel_event(event: threading.Event | None) -> None:
    _local.cancel = event


def cancelled() -> bool:
    ev = getattr(_local, "cancel", None)
    return bool(ev and ev.is_set())


def check_cancel() -> None:
    if cancelled():
        raise Cancelled()


def _tmpdir() -> str:
    d = os.environ.get("EDITOR_TMP") or tempfile.gettempdir()
    os.makedirs(d, exist_ok=True)
    return d


# ---------------------------------------------------------------- Android (FFmpegKit)

_kit = None


def _android():
    global _kit
    if _kit is None:
        from java import jclass  # Chaquopy
        pkg = "com.antonkarpenko.ffmpegkit."
        _kit = {
            "FFmpegKit": jclass(pkg + "FFmpegKit"),
            "FFmpegSession": jclass(pkg + "FFmpegSession"),
            "FFprobeKit": jclass(pkg + "FFprobeKit"),
            "Config": jclass(pkg + "FFmpegKitConfig"),
            "ReturnCode": jclass(pkg + "ReturnCode"),
        }
    return _kit


def _native():
    from java import jclass  # Chaquopy
    return jclass("com.lovedeep.aivideoeditor.Native")


def _token(path: str) -> str:
    return path[len("/saf/"):].split(".", 1)[0]


def resolve(arg: str) -> str:
    """Files picked on the phone are named "/saf/<token>.<ext>": each FFmpeg run gets a fresh
    "saf:" address from the app, so FFmpeg reads the file in place through Android."""
    if ANDROID and arg.startswith("/saf/"):
        saf = _native().safFor(_token(arg))
        if not saf:
            raise FFError("The phone no longer lets the app read this file. Please add it again.")
        return str(saf)
    return arg


def exists(path: str | Path) -> bool:
    path = str(path)
    if path.startswith("/saf/"):
        return bool(ANDROID and _native().available(_token(path)))
    return os.path.exists(path)


def _last_error(text: str) -> str:
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    for ln in reversed(lines):
        if not ln.startswith(("frame=", "size=", "progress=", "out_time")):
            return ln[:300]
    return "the video tool failed"


# ---------------------------------------------------------------- running FFmpeg

def run(args: list[str], duration: float | None = None, progress=None) -> None:
    """Runs FFmpeg with `args` (without the program name). progress(fraction) is
    called as the output grows, when duration (seconds of output) is known."""
    check_cancel()
    fd, prog_path = tempfile.mkstemp(prefix="ffprog_", suffix=".txt", dir=_tmpdir())
    os.close(fd)
    full = ["-y", "-hide_banner", "-v", "error", "-nostats", "-progress", prog_path,
            *[resolve(str(a)) for a in args]]
    try:
        if ANDROID:
            _run_android(full, prog_path, duration, progress)
        else:
            _run_desktop(full, prog_path, duration, progress)
    finally:
        try:
            os.unlink(prog_path)
        except OSError:
            pass


def _read_progress(path: str, duration: float | None, progress) -> None:
    if not progress or not duration:
        return
    try:
        with open(path, "rb") as f:
            f.seek(max(0, os.path.getsize(path) - 2048))
            tail = f.read().decode("utf-8", "ignore")
    except OSError:
        return
    for line in reversed(tail.splitlines()):
        if line.startswith("out_time_us=") or line.startswith("out_time_ms="):
            try:
                us = int(line.split("=", 1)[1])
            except ValueError:
                continue
            progress(min(1.0, max(0.0, us / 1e6 / max(duration, 0.01))))
            return


def _run_desktop(full: list[str], prog_path: str, duration, progress) -> None:
    errf = tempfile.TemporaryFile(dir=_tmpdir())
    proc = subprocess.Popen(["ffmpeg", *full], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=errf)
    try:
        while proc.poll() is None:
            if cancelled():
                proc.kill()
                proc.wait()
                raise Cancelled()
            _read_progress(prog_path, duration, progress)
            time.sleep(0.25)
        if proc.returncode != 0:
            errf.seek(0)
            raise FFError(_last_error(errf.read().decode("utf-8", "ignore")))
    finally:
        if proc.poll() is None:
            proc.kill()
        errf.close()


def _run_android(full: list[str], prog_path: str, duration, progress) -> None:
    k = _android()
    session = k["FFmpegSession"].create(full)
    box: dict = {}

    def go():
        try:
            k["Config"].ffmpegExecute(session)
        except Exception as e:  # noqa: BLE001 - reported below
            box["error"] = e

    t = threading.Thread(target=go, daemon=True)
    t.start()
    while t.is_alive():
        if cancelled():
            k["FFmpegKit"].cancel(session.getSessionId())
            t.join()
            raise Cancelled()
        _read_progress(prog_path, duration, progress)
        t.join(0.25)
    if "error" in box:
        raise FFError(str(box["error"]))
    rc = session.getReturnCode()
    if rc is None or not rc.isValueSuccess():
        if rc is not None and rc.isValueCancel():
            raise Cancelled()
        raise FFError(_last_error(str(session.getAllLogsAsString() or "")))


def run_quiet(args: list[str]) -> bool:
    """Runs FFmpeg and only reports whether it worked."""
    try:
        run(args)
        return True
    except FFError:
        return False


def raw(args: list[str], suffix: str = ".raw") -> bytes:
    """Runs FFmpeg with its output going to a temporary file and returns the bytes.
    `args` must end where the output file name would go (it is appended)."""
    fd, out = tempfile.mkstemp(prefix="ffout_", suffix=suffix, dir=_tmpdir())
    os.close(fd)
    try:
        run([*args, out])
        return Path(out).read_bytes()
    finally:
        try:
            os.unlink(out)
        except OSError:
            pass


# ---------------------------------------------------------------- probing

def probe(path: str | Path) -> dict | None:
    """ffprobe's JSON description of a file (streams + format), or None if unreadable."""
    path = str(path)
    if ANDROID:
        k = _android()
        try:
            path = resolve(path)
        except FFError:
            return None
        session = k["FFprobeKit"].getMediaInformation(path)
        info = session.getMediaInformation()
        if info is None:
            return None
        try:
            return json.loads(str(info.getAllProperties().toString()))
        except (ValueError, AttributeError):
            return None
    res = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", "-show_format", path],
                         capture_output=True, text=True)
    if res.returncode != 0:
        return None
    try:
        return json.loads(res.stdout or "{}")
    except ValueError:
        return None


# ---------------------------------------------------------------- encoders

_hw_ok: bool | None = None
last_encoder = "software"


def hardware_available() -> bool:
    """Whether the phone's own video chip can encode H.264 (checked once with a tiny real encode)."""
    global _hw_ok
    if _hw_ok is None:
        if not ANDROID and os.environ.get("EDITOR_TRY_HW") != "1":
            _hw_ok = False
        else:
            fd, out = tempfile.mkstemp(suffix=".mp4", dir=_tmpdir())
            os.close(fd)
            try:
                run(["-f", "lavfi", "-i", "testsrc2=s=640x360:r=30:d=1", "-c:v", "h264_mediacodec",
                     "-b:v", "2M", "-pix_fmt", "yuv420p", out])
                _hw_ok = os.path.getsize(out) > 1000
            except (FFError, OSError):
                _hw_ok = False
            finally:
                try:
                    os.unlink(out)
                except OSError:
                    pass
    return _hw_ok


def video_args(speed: str, height: int, intermediate: bool = False, hw: bool | None = None) -> list[str]:
    """Encoder settings. speed 'fast' uses the hardware encoder when there is one (hw=False forces software).
    Intermediate files (re-encoded again later) get extra quality. height = the short side in pixels."""
    global last_encoder
    short = max(min(height, 4320), 240)
    if hw is None:
        hw = speed == "fast" and hardware_available()
    if hw:
        last_encoder = "hardware"
        mbps = (5 if short <= 720 else 9 if short <= 1080 else 30) * (2 if intermediate else 1)
        return ["-c:v", "h264_mediacodec", "-b:v", f"{mbps}M", "-g", "60", "-pix_fmt", "yuv420p"]
    last_encoder = "software"
    if intermediate:
        return ["-c:v", "libx264", "-preset", "ultrafast" if speed == "fast" else "veryfast", "-crf", "16",
                "-pix_fmt", "yuv420p"]
    preset = "veryfast" if speed == "fast" else "medium"
    return ["-c:v", "libx264", "-preset", preset, "-crf", "21" if speed == "fast" else "20", "-pix_fmt", "yuv420p"]


def describe_encoder() -> str:
    return "hardware" if (ANDROID and _hw_ok) else last_encoder
