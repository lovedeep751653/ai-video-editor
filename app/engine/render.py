"""Builds the finished video from an editing plan with FFmpeg.

The plan is cut into parts. A part is either one clip (a photo, a slow-motion
or zoomed clip) or a run of clips from the same recording, which is rendered
in a single pass over that recording: FFmpeg reads it once and keeps only the
chosen frames. That is what makes 5–30 minute videos fast on a phone.

Two ways to finish:
- Only straight cuts (the usual for long videos): every part is rendered
  completely (title, text, captions, music, fades included) and the parts are
  joined without re-encoding. Changing one part later only re-renders that part.
- Transitions: parts are rendered first, then joined with the transitions in a
  final pass that adds everything else.

Parts are cached by what they contain, so chat edits re-render only what changed.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path

from . import effects, ff
from .analyze import Media
from .captions import FONTS_DIR, Overlay
from .plan import Clip, Plan

FPS = 30
AUDIO = ["-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-ac", "2"]
MAX_PART = 60.0  # seconds of finished video per part (smaller parts = quicker re-edits)
EDGE = 0.012     # tiny sound fade at every cut so jumps never click


class RenderError(Exception):
    pass


def frames(sec: float) -> int:
    return max(1, int(round(sec * FPS)))


def _esc(path: Path | str) -> str:
    """A file path written inside an FFmpeg filter."""
    return str(path).replace("\\", "/").replace(":", "\\:").replace("'", "\\'")


def _shape(src: str, out: str, w: int, h: int, fill: bool) -> str:
    """Filter text that turns any picture into exactly w x h."""
    if fill:
        return f"[{src}]scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},setsar=1[{out}]"
    bw, bh = max(w // 4 // 2 * 2, 2), max(h // 4 // 2 * 2, 2)
    return (
        f"[{src}]split[{out}a][{out}b];"
        f"[{out}a]scale={bw}:{bh}:force_original_aspect_ratio=increase,crop={bw}:{bh},"
        f"boxblur=10:2,scale={w}:{h},eq=brightness=-0.07[{out}bg];"
        f"[{out}b]scale={w}:{h}:force_original_aspect_ratio=decrease[{out}fg];"
        f"[{out}bg][{out}fg]overlay=(W-w)/2:(H-h)/2,setsar=1[{out}]"
    )


def _zoom(z: float, w: int, h: int) -> str:
    if z <= 1.001:
        return ""
    cw, ch = int(w / z) // 2 * 2, int(h / z) // 2 * 2
    return f"crop={cw}:{ch},scale={w}:{h},"


# ---------------------------------------------------------------- parts

@dataclass
class Part:
    clips: list[Clip]
    out_start: float = 0.0   # where this part starts in the finished video
    frames: int = 0

    @property
    def duration(self) -> float:
        return self.frames / FPS

    @property
    def grouped(self) -> bool:
        return self.clips[0].kind == "video" and all(_groupable(c) for c in self.clips)


def _groupable(c: Clip) -> bool:
    return c.kind == "video" and abs(c.speed - 1) < 1e-6 and c.zoom <= 1.001


def make_parts(plan: Plan) -> list[Part]:
    bounds = plan.boundaries()
    parts: list[Part] = []
    i, n = 0, len(plan.clips)
    while i < n:
        c = plan.clips[i]
        group = [c]
        if _groupable(c):
            dur = c.duration
            j = i + 1
            while j < n:
                d = plan.clips[j]
                prev = group[-1]
                if not (_groupable(d) and d.media_index == c.media_index and not bounds[j - 1]
                        and d.start >= prev.start + prev.duration - 1e-3 and dur + d.duration <= MAX_PART):
                    break
                group.append(d)
                dur += d.duration
                j += 1
        parts.append(Part(clips=group, frames=sum(frames(x.duration) for x in group)))
        i += len(group)
    # Where each part starts in the finished video (a transition overlaps the next part with this one).
    t = plan.transition
    pos = 0.0
    k = 0  # clips so far
    for p in parts:
        p.out_start = round(pos, 4)
        k += len(p.clips)
        overlap = t if k - 1 < len(bounds) and bounds[k - 1] else 0.0
        pos += p.duration - overlap
    return parts


def timeline_total(plan: Plan) -> float:
    parts = make_parts(plan)
    if not parts:
        return 0.0
    return round(parts[-1].out_start + parts[-1].duration, 3)


def clip_starts(plan: Plan) -> list[float]:
    """Where each clip starts in the finished video."""
    bounds = plan.boundaries()
    out, pos = [], 0.0
    for i, c in enumerate(plan.clips):
        out.append(round(pos, 3))
        pos += frames(c.duration) / FPS - (plan.transition if i < len(bounds) and bounds[i] else 0.0)
    return out


# ---------------------------------------------------------------- what goes into a part

def _content(part: Part, m: Media, plan: Plan, gain: float) -> tuple[list[str], list[str]]:
    """FFmpeg inputs and filter steps that produce [v] and [a] for one part (before finishing)."""
    w, h = plan.width, plan.height
    d = part.duration
    look = effects.look_filter(plan.look, part.clips[0].brightness, w, h)
    look = f"{look}," if look else ""
    vol = f"volume={gain:.2f}dB," if abs(gain) > 0.05 else ""
    silence = ["-f", "lavfi", "-t", f"{d + 0.5:.3f}", "-i", "anullsrc=r=48000:cl=stereo"]
    stereo = "aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo"
    end_a = f"apad,atrim=duration={d:.4f},asetpts=PTS-STARTPTS[a]"
    c = part.clips[0]

    if c.kind == "image":
        n = part.frames
        zoom_in = c.media_index % 2 == 0  # alternate zoom in / zoom out between photos
        z = f"1+0.10*on/{n}" if zoom_in else f"1.10-0.10*on/{n}"
        chain = [_shape("0:v", "big", w * 2, h * 2, c.fill),
                 f"[big]zoompan=z='{z}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=1:s={w}x{h}:fps={FPS},"
                 f"{_zoom(c.zoom, w, h)}{look}trim=end_frame={n},format=yuv420p[v]",
                 f"[1:a]{stereo},{end_a}"]
        return ["-loop", "1", "-framerate", str(FPS), "-t", f"{d + 0.5:.3f}", "-i", str(m.path), *silence], chain

    if not part.grouped:  # one clip, maybe slow motion or zoomed
        sp = c.speed
        src_d = d * sp
        slow_v = f"setpts={1 / sp:.5f}*PTS," if abs(sp - 1) > 1e-6 else ""
        tempo = []
        r = sp
        while r < 0.5 - 1e-6:  # atempo goes down to 0.5 per step
            tempo.append("atempo=0.5")
            r /= 0.5
        while r > 2.0 + 1e-6:
            tempo.append("atempo=2.0")
            r /= 2.0
        if abs(r - 1) > 1e-6:
            tempo.append(f"atempo={r:.5f}")
        slow_a = ",".join(tempo) + "," if tempo else ""
        a_src = "0:a" if m.has_audio else "1:a"
        a_fx = slow_a if m.has_audio else ""
        chain = [_shape("0:v", "s", w, h, c.fill),
                 f"[s]{slow_v}fps={FPS},{_zoom(c.zoom, w, h)}{look}tpad=stop_mode=clone:stop_duration=2,"
                 f"trim=end_frame={part.frames},format=yuv420p[v]",
                 f"[{a_src}]{a_fx}{stereo},{vol if m.has_audio else ''}afade=t=in:d={EDGE},{end_a}"]
        return ["-ss", f"{c.start:.3f}", "-t", f"{src_d + 0.3:.3f}", "-i", str(m.path), *silence], chain

    # A run of clips from one recording: one pass, keep only the chosen frames.
    span0 = part.clips[0].start
    span1 = part.clips[-1].start + part.clips[-1].duration
    ranges = []
    for x in part.clips:
        a = int(round((x.start - span0) * FPS))
        ranges.append((a, a + frames(x.duration)))
    if len(ranges) == 1:
        a, b = ranges[0]
        pick = f"trim=start_frame={a}:end_frame={b},setpts=PTS-STARTPTS"
    else:
        expr = "+".join(f"between(n\\,{a}\\,{b - 1})" for a, b in ranges)
        pick = f"select={expr},setpts=N/{FPS}/TB"
    chain = [f"[0:v]fps={FPS},{pick},tpad=stop_mode=clone:stop_duration=2,trim=end_frame={part.frames}[sel]",
             _shape("sel", "s", w, h, c.fill),
             f"[s]{look}format=yuv420p[v]"]
    if m.has_audio:
        k = len(ranges)
        if k == 1:
            a, b = ranges[0]
            chain.append(f"[0:a]{stereo},atrim=start={a / FPS:.4f}:end={b / FPS:.4f},asetpts=PTS-STARTPTS,"
                         f"{vol}afade=t=in:d={EDGE},{end_a}")
        else:
            labels = "".join(f"[s{i}]" for i in range(k))
            chain.append(f"[0:a]{stereo},asplit={k}{labels}")
            for i, (a, b) in enumerate(ranges):
                dd = (b - a) / FPS
                chain.append(f"[s{i}]atrim=start={a / FPS:.4f}:end={b / FPS:.4f},asetpts=PTS-STARTPTS,"
                             f"afade=t=in:d={EDGE},afade=t=out:st={max(dd - EDGE, 0):.4f}:d={EDGE}[r{i}]")
            chain.append("".join(f"[r{i}]" for i in range(k)) + f"concat=n={k}:v=0:a=1,{vol}{end_a}")
    else:
        chain.append(f"[1:a]{stereo},{end_a}")
    return ["-ss", f"{span0:.3f}", "-t", f"{span1 - span0 + 0.5:.3f}", "-i", str(m.path), *silence], chain


def _finish(plan: Plan, start: float, dur: float, first: bool, last: bool, ass: Path | None,
            music: dict | None, music_path: Path | None, n_inputs: int, v: str = "v", a: str = "a"
            ) -> tuple[list[str], list[str]]:
    """Inputs and filter steps that add overlays, music and the opening/closing fades. Output: [vo] [ao]."""
    inputs, chain = [], []
    vf = []
    if ass is not None:
        vf.append(f"subtitles=filename='{_esc(ass)}':fontsdir='{_esc(FONTS_DIR)}'")
    if plan.fade and first:
        vf.append("fade=t=in:d=0.3")
    if plan.fade and last:
        vf.append(f"fade=t=out:st={max(dur - 0.5, 0):.3f}:d=0.5")
    vf.append("format=yuv420p")
    chain.append(f"[{v}]{','.join(vf)}[vo]")
    af = []
    if plan.fade and first:
        af.append("afade=t=in:d=0.3")
    if plan.fade and last:
        af.append(f"afade=t=out:st={max(dur - 0.5, 0):.3f}:d=0.5")
    if music and music_path is not None:
        mdur = max(float(music.get("duration") or 0), 0.1)
        offset = (float(music.get("start") or 0) + start) % mdur
        inputs += ["-stream_loop", "-1", "-ss", f"{offset:.3f}", "-i", str(music_path)]
        mf = [f"aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,atrim=duration={dur:.4f}",
              "asetpts=PTS-STARTPTS", f"volume={plan.music_volume:.3f}"]
        if last:
            mf.append(f"afade=t=out:st={max(dur - 1.5, 0):.3f}:d=1.5")
        chain.append(f"[{a}]volume={plan.original_volume:.3f}[ao0]")
        chain.append(f"[{n_inputs}:a]{','.join(mf)}[am]")
        chain.append("[ao0][am]amix=inputs=2:duration=first:normalize=0[amx]")
        a = "amx"
    chain.append(f"[{a}]{','.join(af + ['aresample=48000'])}[ao]")
    return inputs, chain


def _run_encode(build, duration: float, progress, speed: str, height: int, intermediate: bool) -> None:
    """Encodes with the phone's video chip when allowed, falling back to the software encoder."""
    use_hw = speed == "fast" and ff.hardware_available()
    try:
        ff.run(build(ff.video_args(speed, height, intermediate, hw=use_hw)), duration, progress)
    except ff.FFError as e:
        if not use_hw:
            raise RenderError(str(e)) from e
        try:
            ff.run(build(ff.video_args(speed, height, intermediate, hw=False)), duration, progress)
        except ff.FFError as e2:
            raise RenderError(str(e2)) from e2


# ---------------------------------------------------------------- the whole video

def _key(*items) -> str:
    return hashlib.sha1(json.dumps(items, sort_keys=True, default=str).encode()).hexdigest()[:20]


def render(plan: Plan, media: dict[int, Media], out: Path, cache: Path, overlay: Overlay | None = None,
           music_path: Path | None = None, speed: str = "fast", progress=None) -> dict:
    """Renders the plan to `out`. Returns {"parts", "reused", "encoder"}."""
    cache.mkdir(parents=True, exist_ok=True)
    parts = make_parts(plan)
    if not parts:
        raise RenderError("There is nothing left in the video")
    total = parts[-1].out_start + parts[-1].duration
    plan.total = round(total, 3)
    fast_join = not any(plan.boundaries())
    music = dict(plan.music or {}) if (plan.music and music_path is not None) else None
    report = progress or (lambda f: None)
    weights = [p.duration for p in parts]
    join_w = 0.0 if fast_join else total * 0.6
    all_w = sum(weights) + join_w + 0.001
    done = 0.0
    used, reused = [], 0
    hw = speed == "fast" and ff.hardware_available()
    base = dict(w=plan.width, h=plan.height, look=plan.look, fps=FPS, speed=speed, hw=hw, v=3)

    for i, p in enumerate(parts):
        m = media[p.clips[0].media_index]
        gain = float(getattr(m, "gain_db", 0.0) or 0.0)
        clips = [{k: v for k, v in asdict(c).items() if k not in ("reasons", "score", "name")} for c in p.clips]
        first, last = i == 0, i == len(parts) - 1
        ass_text = None
        if fast_join:
            if overlay and not overlay.empty(p.out_start, p.out_start + p.duration):
                ass_text = overlay.ass(p.out_start, p.out_start + p.duration)
            fin = dict(first=first and plan.fade, last=last and plan.fade, dur=round(p.duration, 4), ass=ass_text,
                       music=(music, round(p.out_start, 3), plan.music_volume, plan.original_volume) if music else None)
            name = f"{_key(base, getattr(m, 'key', str(m.path)), gain, clips, fin)}.ts"
        else:
            name = f"{_key(base, getattr(m, 'key', str(m.path)), gain, clips, 'mid')}.mp4"
        target = cache / name
        used.append(target)
        if target.exists() and target.stat().st_size > 0:
            reused += 1
            done += weights[i]
            report(done / all_w)
            continue
        ff.check_cancel()
        inputs, chain = _content(p, m, plan, gain)
        tmp = cache / ("tmp_" + name)
        cb = (lambda f, d=done, w=weights[i]: report((d + f * w) / all_w))
        if fast_join:
            ass_path = None
            if ass_text:
                ass_path = cache / f"part_{i}.ass"
                ass_path.write_text(ass_text, encoding="utf-8")
            extra, fchain = _finish(plan, p.out_start, p.duration, first, last, ass_path, music, music_path,
                                    n_inputs=_count_inputs(inputs))
            args_in = [*inputs, *extra, "-filter_complex", ";".join(chain + fchain), "-map", "[vo]", "-map", "[ao]"]

            def build(enc, args_in=args_in, tmp=tmp, d=p.duration):
                return [*args_in, *enc, "-r", str(FPS), *AUDIO, "-t", f"{d:.4f}", "-f", "mpegts", str(tmp)]
            _run_encode(build, p.duration, cb, speed, min(plan.width, plan.height), intermediate=False)
            if ass_path:
                ass_path.unlink(missing_ok=True)
        else:
            args_in = [*inputs, "-filter_complex", ";".join(chain), "-map", "[v]", "-map", "[a]"]

            def build(enc, args_in=args_in, tmp=tmp, d=p.duration):
                return [*args_in, *enc, "-r", str(FPS), *AUDIO, "-t", f"{d:.4f}", "-f", "mp4", str(tmp)]
            _run_encode(build, p.duration, cb, speed, min(plan.width, plan.height), intermediate=True)
        os.replace(tmp, target)
        done += weights[i]
        report(done / all_w)

    ff.check_cancel()
    tmp_out = out.with_name("tmp_" + out.name)
    if fast_join:
        lst = cache / "join.txt"
        lst.write_text("".join(f"file '{p.name}'\n" for p in used), encoding="utf-8")
        try:
            ff.run(["-f", "concat", "-safe", "0", "-i", str(lst), "-map", "0:v", "-map", "0:a", "-c", "copy",
                    "-movflags", "+faststart", "-f", "mp4", str(tmp_out)])
        except ff.FFError as e:
            raise RenderError(str(e)) from e
    else:
        _join(parts, used, plan, tmp_out, overlay, music, music_path, cache, speed,
              lambda f: report((done + f * join_w) / all_w))
    os.replace(tmp_out, out)
    # Forget parts that this version doesn't use (older versions keep their finished files).
    keep = {p.name for p in used}
    for f in cache.iterdir():
        if f.is_file() and f.name not in keep and f.suffix in (".ts", ".mp4", ".ass"):
            f.unlink(missing_ok=True)
    report(1.0)
    return {"parts": len(parts), "reused": reused, "encoder": "hardware" if hw else "software"}


def _count_inputs(args: list[str]) -> int:
    return sum(1 for a in args if a == "-i")


def _join(parts: list[Part], files: list[Path], plan: Plan, out: Path, overlay: Overlay | None,
          music: dict | None, music_path: Path | None, cache: Path, speed: str, progress) -> None:
    """Joins rendered parts with transitions (or cuts), then finishes the whole video in one pass."""
    bounds = plan.boundaries()
    t = plan.transition
    inputs = []
    for f in files:
        inputs += ["-i", str(f)]
    chain = []
    v, a = "0:v", "0:a"
    length = parts[0].duration
    k = len(parts[0].clips)
    for i in range(1, len(parts)):
        trans = bounds[k - 1] if k - 1 < len(bounds) else False
        if trans:
            kind = plan.transitions[k - 1] if k - 1 < len(plan.transitions) else "fade"
            off = length - t
            chain.append(f"[{v}][{i}:v]xfade=transition={kind}:duration={t:.3f}:offset={off:.4f}[v{i}]")
            chain.append(f"[{a}][{i}:a]acrossfade=d={t:.3f}:c1=tri:c2=tri[a{i}]")
            length = off + parts[i].duration
        else:
            chain.append(f"[{v}][{a}][{i}:v][{i}:a]concat=n=2:v=1:a=1[v{i}][a{i}]")
            length += parts[i].duration
        v, a = f"v{i}", f"a{i}"
        k += len(parts[i].clips)
    ass_path = None
    if overlay and not overlay.empty():
        ass_path = cache / "all.ass"
        ass_path.write_text(overlay.ass(), encoding="utf-8")
    extra, fchain = _finish(plan, 0.0, length, True, True, ass_path, music, music_path, len(files), v=v, a=a)
    args_in = [*inputs, *extra, "-filter_complex", ";".join(chain + fchain), "-map", "[vo]", "-map", "[ao]"]

    def build(enc):
        return [*args_in, *enc, "-r", str(FPS), *AUDIO, "-t", f"{length:.4f}", "-movflags", "+faststart",
                "-f", "mp4", str(out)]
    _run_encode(build, length, progress, speed, min(plan.width, plan.height), intermediate=False)
    if ass_path:
        ass_path.unlink(missing_ok=True)


# ---------------------------------------------------------------- sound for captions, pictures

def speech_track(plan: Plan, media: dict[int, Media], out: Path) -> float:
    """The edited video's own sound (no music), made straight from the original files,
    for listening to the speech before the video is rendered. Returns its length."""
    bounds = plan.boundaries()
    t = plan.transition
    inputs: list[str] = []
    index: dict[int, int] = {}
    uses: dict[int, int] = {}
    segs = []
    for k, c in enumerate(plan.clips):
        seg = frames(c.duration) / FPS
        if k < len(bounds) and bounds[k]:
            seg -= t
        m = media[c.media_index]
        if c.kind == "video" and m.has_audio and abs(c.speed - 1) < 1e-6:
            if c.media_index not in index:
                index[c.media_index] = _count_inputs(inputs)
                inputs += ["-i", str(m.path)]
            uses[c.media_index] = uses.get(c.media_index, 0) + 1
            segs.append((c.media_index, c.start, max(seg, 0.04)))
        else:
            segs.append((None, 0.0, max(seg, 0.04)))
    chain = []
    counters = {mi: 0 for mi in uses}
    for mi, n in uses.items():
        base = f"[{index[mi]}:a]aresample=16000,aformat=sample_fmts=fltp:channel_layouts=mono"
        if n > 1:
            chain.append(base + f",asplit={n}" + "".join(f"[m{mi}_{j}]" for j in range(n)))
        else:
            chain.append(base + f"[m{mi}_0]")
    names = []
    for j, (mi, start, d) in enumerate(segs):
        if mi is None:
            chain.append(f"aevalsrc=0:s=16000:d={d:.4f},aformat=sample_fmts=fltp:channel_layouts=mono[q{j}]")
        else:
            lab = f"m{mi}_{counters[mi]}"
            counters[mi] += 1
            chain.append(f"[{lab}]atrim=start={start:.4f}:end={start + d:.4f},asetpts=PTS-STARTPTS,"
                         f"apad,atrim=duration={d:.4f}[q{j}]")
        names.append(f"[q{j}]")
    chain.append("".join(names) + f"concat=n={len(names)}:v=0:a=1[aout]")
    total = sum(d for _, _, d in segs)
    try:
        ff.run([*inputs, "-filter_complex", ";".join(chain), "-map", "[aout]", "-c:a", "libmp3lame", "-b:a", "48k",
                str(out)], total)
    except ff.FFError as e:
        raise RenderError(str(e)) from e
    return total


def thumbnail(video: Path, out: Path, at: float = 1.0, width: int = 480) -> bool:
    return ff.run_quiet(["-ss", f"{max(at, 0):.3f}", "-i", str(video), "-frames:v", "1",
                         "-vf", f"scale={width}:-2", "-q:v", "4", str(out)])


def frames_at(video: Path, times: list[float], out_dir: Path, width: int = 320) -> list[Path]:
    """Small pictures of the video at the given moments (for the AI to look at)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    files = []
    for i, t in enumerate(times):
        f = out_dir / f"f{i:03d}_{int(t * 10)}.jpg"
        if f.exists() or ff.run_quiet(["-ss", f"{t:.2f}", "-i", str(video), "-frames:v", "1",
                                       "-vf", f"scale={width}:-2", "-q:v", "6", str(f)]):
            files.append(f)
    return files
