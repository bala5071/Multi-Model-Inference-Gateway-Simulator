"""Retake the dashboard pictures in docs/img: six screenshots and two GIFs.

    uv run python scripts/capture_demo.py

Builds the dashboard, starts the gateway in simulated live mode (seed 7), drives the pages
with Playwright (``frontend/e2e/demo.capture.ts``) and assembles the overload GIF from the
frames it saved. Then starts a second gateway, takes the frames of a tour of every tab
(``frontend/e2e/demo.tour.ts``) and assembles ``docs/img/demo.gif``. A GIF has no sound.

Needs Node 20 and a Chromium for Playwright: either ``npx playwright install
chromium`` in ``frontend/``, or ``PLAYWRIGHT_CHANNEL=chrome`` to use an installed Chrome.

Everything in the pictures is simulated; see docs/18-demo.md.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
FRONTEND = ROOT / "frontend"
FRAMES = FRONTEND / "e2e" / "results" / "frames"
GIF = ROOT / "docs" / "img" / "overload.gif"
TOUR_FRAMES = FRONTEND / "e2e" / "results" / "tour"
TOUR_GIF = ROOT / "docs" / "img" / "demo.gif"

# Small enough to sit in a README: scaled down, a short palette, about 2.5 frames a second.
GIF_WIDTH = 960
GIF_COLOURS = 64
GIF_FRAME_MS = 400


def assemble_gif(frames: list[Path], out: Path) -> None:
    images = []
    for frame in frames:
        with Image.open(frame) as image:
            height = round(image.height * GIF_WIDTH / image.width)
            small = image.convert("RGB").resize((GIF_WIDTH, height), Image.Resampling.LANCZOS)
            images.append(small.quantize(colors=GIF_COLOURS, dither=Image.Dither.NONE))
    out.parent.mkdir(parents=True, exist_ok=True)
    images[0].save(
        out,
        save_all=True,
        append_images=images[1:],
        duration=GIF_FRAME_MS,
        loop=0,
        optimize=True,
    )


def main() -> int:
    npm = shutil.which("npm")
    if npm is None:
        print("npm was not found; the dashboard needs Node 20.", file=sys.stderr)
        return 1
    for script, folder, gif in (("screenshots", FRAMES, GIF), ("tour", TOUR_FRAMES, TOUR_GIF)):
        if folder.exists():
            shutil.rmtree(folder)
        captured = subprocess.run([npm, "run", script], cwd=FRONTEND, check=False)
        if captured.returncode != 0:
            return captured.returncode
        frames = sorted(folder.glob("*.png"))
        if not frames:
            print(f"no frames were saved in {folder}", file=sys.stderr)
            return 1
        assemble_gif(frames, gif)
        size = gif.stat().st_size
        print(f"wrote {gif.relative_to(ROOT)} from {len(frames)} frames, {size:,} bytes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
