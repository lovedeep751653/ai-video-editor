"""Visual looks, transitions and title text."""

from __future__ import annotations

import random
import shutil
import subprocess
from pathlib import Path

# Colour looks. Each is an FFmpeg filter chain applied to every clip.
LOOKS = {
    "none": "",
    "enhance": "",  # only the automatic per-clip light/colour correction
    "cinematic": "eq=contrast=1.12:saturation=0.85,"
                 "colorbalance=rs=-0.04:bs=0.06:rh=0.06:bh=-0.05,vignette=angle=PI/5",
    "vivid": "eq=saturation=1.35:contrast=1.08",
    "warm": "colorbalance=rs=0.08:gs=0.02:bs=-0.08:rm=0.05:bm=-0.05,eq=saturation=1.1",
    "cool": "colorbalance=rs=-0.06:bs=0.08:rm=-0.04:bm=0.06,eq=saturation=1.05",
    "vintage": "curves=preset=vintage,noise=alls=8:allf=t,vignette=angle=PI/4",
    "bw": "hue=s=0,eq=contrast=1.15",
    "dramatic": "eq=contrast=1.3:saturation=0.75:brightness=-0.03,vignette=angle=PI/4",
}
LOOK_NAMES = {
    "none": "No colour change", "enhance": "Auto light & colour fix", "cinematic": "Cinematic",
    "vivid": "Vivid", "warm": "Warm", "cool": "Cool", "vintage": "Vintage film", "bw": "Black & white",
    "dramatic": "Dramatic",
}

TRANSITION_SETS = {
    "soft": ["fade", "dissolve", "smoothleft", "fadeblack", "smoothright", "hblur", "smoothup", "fadegrays"],
    "dynamic": ["slideleft", "circleopen", "wipeleft", "zoomin", "radial", "coverleft", "revealright",
                "smoothup", "diagtl", "squeezeh", "slideup", "horzopen", "circleclose", "pixelize", "vertopen"],
    "cinematic": ["fadeblack", "dissolve", "hblur", "fade", "smoothleft", "fadeslow"],
    "cuts": [],
}


def enhance_filter(brightness: float) -> str:
    """Automatic correction: lifts dark clips, tames bright ones, adds a little punch."""
    lift = max(-0.08, min(0.12, (0.47 - brightness) * 0.5))
    return f"eq=brightness={lift:.3f}:contrast=1.05:saturation=1.12"


def look_filter(look: str, brightness: float, width: int, height: int) -> str:
    parts = []
    if look != "none":
        parts.append(enhance_filter(brightness))
    if LOOKS.get(look):
        parts.append(LOOKS[look])
    if look == "cinematic" and width > height:
        bar = int((height - width / 2.39) / 2)
        if bar > 0:
            parts.append(f"drawbox=x=0:y=0:w=iw:h={bar}:color=black:t=fill,"
                         f"drawbox=x=0:y=ih-{bar}:w=iw:h={bar}:color=black:t=fill")
    return ",".join(parts)


def pick_transitions(kind: str, count: int, seed: int) -> list[str]:
    pool = TRANSITION_SETS[kind]
    if not pool:
        return []
    rng = random.Random(seed)
    out = []
    for _ in range(count):
        choices = [t for t in pool if not out or t != out[-1]] or pool
        out.append(rng.choice(choices))
    return out


def font_file() -> str | None:
    for p in ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
              "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
              "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"]:
        if Path(p).exists():
            return p
    if shutil.which("fc-match"):
        r = subprocess.run(["fc-match", "-f", "%{file}", "sans:bold"], capture_output=True, text=True)
        if r.stdout and Path(r.stdout).exists():
            return r.stdout
    return None


def title_filter(text_file: Path, width: int, height: int, duration: float = 3.0) -> str:
    """Big centred title that fades in and out at the start of the video."""
    font = font_file()
    size = int(min(width, height) * 0.085)
    fade = 0.5
    alpha = (f"if(lt(t,{fade}),t/{fade},if(lt(t,{duration - fade}),1,"
             f"if(lt(t,{duration}),({duration}-t)/{fade},0)))")
    fontpart = f"fontfile='{font}':" if font else ""
    return (f"drawtext={fontpart}textfile='{text_file}':fontsize={size}:fontcolor=white:"
            f"borderw={max(2, size // 18)}:bordercolor=black@0.6:shadowx=2:shadowy=2:"
            f"x=(w-text_w)/2:y=(h-text_h)/2:alpha='{alpha}':enable='lt(t,{duration})'")
