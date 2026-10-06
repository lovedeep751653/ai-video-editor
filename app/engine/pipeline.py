"""Runs the whole automatic edit: analyse → plan → render → join."""

from __future__ import annotations

import json
from pathlib import Path

from . import analyze, captions, genai, music as music_mod, plan as planner, render

# Share of the progress bar each step takes (they add up to 1.0).
W_ANALYZE, W_PLAN, W_CLIPS, W_JOIN = 0.30, 0.02, 0.43, 0.25


def run(inputs: list[Path], work: Path, options: dict, report, music_path: Path | None = None,
        ai: tuple[str, dict] | None = None) -> dict:
    """ai = (Google AI key, chosen models) when set up; used for captions."""
    """report(stage_text, fraction_done) is called as work happens."""
    work.mkdir(parents=True, exist_ok=True)
    skipped = []

    report("Opening your files", 0.0)
    media = []
    for i, p in enumerate(inputs):
        try:
            media.append(analyze.probe(p, i))
        except analyze.MediaError as e:
            skipped.append(str(e))
    if not media:
        raise ValueError("None of the files could be opened. " + " ".join(skipped))

    # Videos take longer to study than photos, so weight progress by length.
    weights = [max(m.duration, 0.5) if m.kind == "video" else 0.3 for m in media]
    total_w = sum(weights)
    done_w = 0.0
    ok = []
    for m, w in zip(media, weights):
        label = f"Studying {'video' if m.kind == 'video' else 'photo'}: {m.name}"
        try:
            if m.kind == "video":
                analyze.analyze_video(m, lambda f, d=done_w, w=w: report(label, W_ANALYZE * (d + f * w) / total_w))
            else:
                analyze.analyze_image(m)
            ok.append(m)
        except analyze.MediaError as e:
            skipped.append(str(e))
        done_w += w
        report(label, W_ANALYZE * done_w / total_w)
    if not ok:
        raise ValueError("None of the files could be read. " + " ".join(skipped))
    analyze.score_all(ok)

    track = None
    if music_path is not None:
        report("Finding the beat of your music", W_ANALYZE)
        try:
            track = music_mod.analyze(music_path)
        except music_mod.MusicError as e:
            skipped.append(str(e))

    report("Choosing the best moments", W_ANALYZE)
    the_plan = planner.make_plan(ok, options, track)
    by_index = {m.index: m for m in ok}
    (work / "plan.json").write_text(json.dumps(the_plan.to_dict(), indent=2))

    base = W_ANALYZE + W_PLAN
    parts = []
    clip_total = sum(c.duration for c in the_plan.clips)
    done = 0.0
    for i, clip in enumerate(the_plan.clips):
        out = work / f"part_{i:03d}.mp4"
        label = f"Cutting part {i + 1} of {len(the_plan.clips)}"
        render.render_clip(clip, by_index[clip.media_index], the_plan, out,
                           lambda f, d=done, c=clip: report(label, base + W_CLIPS * (d + f * c.duration) / clip_total))
        parts.append(out)
        done += clip.duration

    final = work / "final.mp4"
    has_sound = any(by_index[c.media_index].has_audio and by_index[c.media_index].loudness.max() > -60
                    for c in the_plan.clips)
    cap_mode = options.get("captions", "auto")
    key, models = ai or ("", {})
    want_caps = cap_mode == "on" or (cap_mode == "auto" and has_sound)
    can_caps = bool(key and models.get("text"))
    w_join = W_JOIN * (0.55 if want_caps and can_caps else 1)
    report("Adding transitions and finishing", base + W_CLIPS)
    render.join(parts, the_plan, final,
                lambda f: report("Adding transitions and finishing", base + W_CLIPS + w_join * f),
                normalize=has_sound, music_path=music_path if track else None)

    caption_lines = []
    if want_caps and not can_caps:
        if cap_mode == "on" or has_sound:
            the_plan.notes.append("Captions need a Google AI key (add one in Settings)")
    elif want_caps and not has_sound:
        the_plan.notes.append("No sound in the chosen clips, so no captions were added")
    elif want_caps:
        at = base + W_CLIPS + w_join
        report("Listening to the speech for captions", at)
        speech = work / "speech.mp3"
        render.speech_track(parts, the_plan, speech)
        try:
            caption_lines = genai.transcribe(key, models["text"], speech, options.get("caption_lang", "auto"))
        except genai.AIError as e:
            the_plan.notes.append(f"Captions couldn't be made: {e}")
        if caption_lines:
            caption_lines = captions.snap_to_speech(caption_lines, speech)
            ass = work / "captions.ass"
            ass.write_text(captions.build_ass(caption_lines, options.get("caption_style", captions.DEFAULT_STYLE),
                                              the_plan.width, the_plan.height, options.get("caption_pos", "bottom"),
                                              the_plan.total), encoding="utf-8")
            report("Adding captions", at + 0.03)
            captioned = work / "final_captioned.mp4"
            render.burn_captions(final, ass, captioned, the_plan.total,
                                 lambda f: report("Adding captions", at + 0.03 + (1 - at - 0.03) * f))
            captioned.replace(final)
            style = captions.STYLES.get(options.get("caption_style", ""), captions.STYLES[captions.DEFAULT_STYLE])
            the_plan.notes.append(f"Captions added ({captions.LANGS.get(options.get('caption_lang', 'auto'))}, "
                                  f"style: {style['name']})")
        elif not any("Captions couldn't" in n for n in the_plan.notes):
            the_plan.notes.append("No speech was heard, so no captions were added")
        speech.unlink(missing_ok=True)

    for p in parts:
        p.unlink(missing_ok=True)
    render.thumbnail(final, work / "thumb.jpg")
    report("Done", 1.0)
    result = the_plan.to_dict()
    result["skipped"] = skipped
    result["captions"] = caption_lines
    return result
