"""Builds the final video from an editing plan with FFmpeg:
trims each chosen part, reshapes it to the chosen format, adds a slow zoom to
photos, joins everything with transitions, evens out the sound and saves an
MP4 that plays on any phone."""

from __future__ import annotations

import subprocess
from pathlib import Path

from . import effects
from .analyze import Media
from .plan import Clip, Plan

FPS = 30


class RenderError(Exception):
    pass


def _ffmpeg(args: list[str], duration: float, progress=None) -> None:
    cmd = ["ffmpeg", "-y", "-v", "error", "-nostats", "-progress", "pipe:1", *args]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    for line in proc.stdout:
        if progress and line.startswith("out_time_us="):
            try:
                us = int(line.split("=", 1)[1])
            except ValueError:
                continue
            progress(min(1.0, max(0.0, us / 1e6 / max(duration, 0.01))))
    err = proc.stderr.read()
    if proc.wait() != 0:
        raise RenderError(err.strip().splitlines()[-1] if err.strip() else "video tool failed")


def _shape(src: str, out: str, w: int, h: int, fill: bool) -> str:
    """Filter text that turns any picture into exactly w x h."""
    if fill:
        return f"[{src}]scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},setsar=1[{out}]"
    bw, bh = w // 4 // 2 * 2, h // 4 // 2 * 2
    return (
        f"[{src}]split[{out}a][{out}b];"
        f"[{out}a]scale={bw}:{bh}:force_original_aspect_ratio=increase,crop={bw}:{bh},"
        f"boxblur=10:2,scale={w}:{h},eq=brightness=-0.07[{out}bg];"
        f"[{out}b]scale={w}:{h}:force_original_aspect_ratio=decrease[{out}fg];"
        f"[{out}bg][{out}fg]overlay=(W-w)/2:(H-h)/2,setsar=1[{out}]"
    )


def render_clip(clip: Clip, media: Media, plan: Plan, out: Path, progress=None) -> None:
    w, h, d = plan.width, plan.height, clip.duration
    enc = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p", "-r", str(FPS),
           "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2", "-t", f"{d:.3f}", str(out)]
    silence = ["-f", "lavfi", "-t", f"{d:.3f}", "-i", "anullsrc=r=48000:cl=stereo"]
    afade = f"afade=t=in:d=0.04,afade=t=out:st={max(d - 0.06, 0):.3f}:d=0.06"
    look = effects.look_filter(plan.look, clip.brightness, w, h)
    look = f"{look}," if look else ""
    if clip.kind == "image":
        frames = int(round(d * FPS))
        zoom_in = clip.media_index % 2 == 0  # alternate zoom in / zoom out between photos
        z = f"1+0.10*on/{frames}" if zoom_in else f"1.10-0.10*on/{frames}"
        vf = (
            _shape("0:v", "big", w * 2, h * 2, clip.fill)
            + f";[big]zoompan=z='{z}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=1:s={w}x{h}:fps={FPS},"
            f"{look}format=yuv420p[v];[1:a]{afade}[a]"
        )
        args = ["-loop", "1", "-framerate", str(FPS), "-t", f"{d:.3f}", "-i", str(media.path), *silence,
                "-filter_complex", vf, "-map", "[v]", "-map", "[a]", *enc]
    else:
        src_d = d * clip.speed
        slow_v = f"setpts={1 / clip.speed:.4f}*PTS," if clip.speed != 1 else ""
        slow_a = f"atempo={clip.speed:.4f}," if clip.speed != 1 and media.has_audio else ""
        audio_src = "0:a" if media.has_audio else "1:a"
        vf = (
            _shape("0:v", "s", w, h, clip.fill)
            + f";[s]{slow_v}fps={FPS},{look}tpad=stop_mode=clone:stop_duration=1,trim=duration={d:.3f},"
            f"format=yuv420p[v];"
            f"[{audio_src}]{slow_a}aresample=48000,aformat=channel_layouts=stereo,apad,atrim=duration={d:.3f},"
            f"{afade}[a]"
        )
        args = ["-ss", f"{clip.start:.3f}", "-t", f"{src_d:.3f}", "-i", str(media.path), *silence,
                "-filter_complex", vf, "-map", "[v]", "-map", "[a]", *enc]
    _ffmpeg(args, d, progress)


def join(parts: list[Path], plan: Plan, out: Path, progress=None, normalize: bool = True,
         music_path: Path | None = None) -> None:
    """Joins the parts with transitions, then adds title, music and the final sound levelling.

    normalize evens out loudness; it must be off when there is no real sound (it breaks on pure silence).
    """
    n, t, total = len(parts), plan.transition, plan.total
    inputs = []
    for p in parts:
        inputs += ["-i", str(p)]
    chain = []
    if n == 1:
        v, a = "0:v", "0:a"
    elif t <= 0:
        streams = "".join(f"[{i}:v][{i}:a]" for i in range(n))
        chain.append(f"{streams}concat=n={n}:v=1:a=1[vc][ac]")
        v, a = "vc", "ac"
    else:
        v, a = "0:v", "0:a"
        offset = 0.0
        for i in range(1, n):
            offset += plan.clips[i - 1].duration - t
            kind = plan.transitions[i - 1]
            chain.append(f"[{v}][{i}:v]xfade=transition={kind}:duration={t:.3f}:offset={offset:.3f}[v{i}]")
            chain.append(f"[{a}][{i}:a]acrossfade=d={t:.3f}:c1=tri:c2=tri[a{i}]")
            v, a = f"v{i}", f"a{i}"
    fo = max(total - 0.5, 0)
    title = ""
    if plan.title:
        tf = out.parent / "title.txt"
        tf.write_text(plan.title)
        title = "," + effects.title_filter(tf, plan.width, plan.height)
    chain.append(f"[{v}]fade=t=in:d=0.3,fade=t=out:st={fo:.3f}:d=0.5{title},format=yuv420p[vout]")
    if music_path is not None and plan.music:
        inputs += ["-ss", f"{plan.music['start']:.3f}", "-i", str(music_path)]
        mo = max(total - 1.5, 0)
        chain.append(f"[{a}]volume={plan.original_volume}[ao]")
        chain.append(f"[{n}:a]aresample=48000,aformat=channel_layouts=stereo,atrim=duration={total:.3f},"
                     f"asetpts=PTS-STARTPTS,afade=t=out:st={mo:.3f}:d=1.5[am]")
        chain.append("[ao][am]amix=inputs=2:duration=first:normalize=0[amx]")
        a, normalize = "amx", True
    level = "loudnorm=I=-14:TP=-1.5:LRA=11," if normalize else ""
    chain.append(f"[{a}]afade=t=in:d=0.3,afade=t=out:st={fo:.3f}:d=0.5,{level}aresample=48000[aout]")
    args = [*inputs, "-filter_complex", ";".join(chain), "-map", "[vout]", "-map", "[aout]",
            "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p", "-r", str(FPS),
            "-c:a", "aac", "-b:a", "192k", "-t", f"{total:.3f}", "-movflags", "+faststart", str(out)]
    _ffmpeg(args, total, progress)


def thumbnail(video: Path, out: Path) -> None:
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", "1", "-i", str(video), "-frames:v", "1",
                    "-vf", "scale=480:-2", str(out)], capture_output=True)


def speech_track(parts: list[Path], plan: Plan, out: Path) -> None:
    """The edited video's own sound (no music), for listening to speech when making captions."""
    n, t = len(parts), plan.transition
    inputs = []
    for p in parts:
        inputs += ["-i", str(p)]
    chain, a = [], "0:a"
    if n > 1 and t <= 0:
        chain.append("".join(f"[{i}:a]" for i in range(n)) + f"concat=n={n}:v=0:a=1[ac]")
        a = "ac"
    elif n > 1:
        for i in range(1, n):
            chain.append(f"[{a}][{i}:a]acrossfade=d={t:.3f}:c1=tri:c2=tri[a{i}]")
            a = f"a{i}"
    chain.append(f"[{a}]aresample=16000,aformat=channel_layouts=mono[aout]")
    _ffmpeg([*inputs, "-filter_complex", ";".join(chain), "-map", "[aout]", "-c:a", "libmp3lame", "-b:a", "48k",
             str(out)], plan.total)


def burn_captions(video: Path, ass: Path, out: Path, duration: float, progress=None) -> None:
    fonts = effects_fonts_dir()
    sub = f"subtitles=filename='{ass}':fontsdir='{fonts}'"
    _ffmpeg(["-i", str(video), "-vf", sub, "-c:v", "libx264", "-preset", "medium", "-crf", "20",
             "-pix_fmt", "yuv420p", "-c:a", "copy", "-movflags", "+faststart", str(out)], duration, progress)


def effects_fonts_dir() -> Path:
    from .captions import FONTS_DIR
    return FONTS_DIR
