"""Paste the 13 audit screenshots for a SKU into a target PPTX.

Slide layout assumptions (matches ClaudeMattel.pptx as of May 2026):
  - Slide 14 → 2 ChatGPT snaps (chatgpt_initial, chatgpt)
  - Slide 15 → 2 Google snaps (initial, full_page)
  - Slide 24 → 3 Alexa pill snaps (alexa_top, alexa_inline, alexa_qa)
  - Slide 25 → 3 Alexa response snaps (likes, dislikes, certs)
  - Slide 26 → 3 Alexa response snaps (materials, alternatives, budget_alt)

Images are placed in a single row along the bottom edge of each target
slide so they don't cover existing content. Existing slide content is
not modified; only new pictures are added. The user crops/repositions
manually in PowerPoint.

Usage:
    python src/paste_to_pptx.py --sku <name> --pptx <path>
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from PIL import Image as PILImage
from pptx import Presentation
from pptx.util import Inches


ROOT = Path(__file__).resolve().parent.parent

# Map 0-based slide index -> ordered list of screenshot filenames.
# Change here if the template's slide structure shifts.
#
# Layout per ClaudeMattel.pptx structure (May 2026):
#   13 = Google         14 = ChatGPT
#   21 = Sparky chat    22 = Walmart 1-star reviews
#   23 = Alexa pills    24, 25 = Alexa Q+A
LAYOUT: dict[int, list[str]] = {
    13: ["chatgpt_initial.png", "chatgpt.png"],
    14: ["initial.png", "full_page.png"],
    21: ["sparky_likes.png", "sparky_dislikes.png", "sparky_defects.png"],
    22: ["walmart_bad_review_1.png", "walmart_bad_review_2.png",
         "walmart_bad_review_3.png"],
    23: ["alexa_top.png", "alexa_inline.png", "alexa_qa.png"],
    24: ["alexa_likes.png", "alexa_dislikes.png", "alexa_certs.png"],
    25: ["alexa_materials.png", "alexa_alternatives.png", "alexa_budget_alt.png"],
}


def paste(sku: str, pptx_path: Path,
          only_slides: set[int] | None = None) -> int:
    samples = ROOT / "samples" / sku
    if not samples.is_dir():
        print(f"ERROR: samples dir not found: {samples}", file=sys.stderr)
        return 1

    if not pptx_path.is_file():
        print(f"ERROR: pptx not found: {pptx_path}", file=sys.stderr)
        return 1

    prs = Presentation(str(pptx_path))
    slide_w = prs.slide_width
    slide_h = prs.slide_height
    print(f"PPTX:    {pptx_path}")
    print(f"Samples: {samples}")
    print(f"Slide size: {slide_w/914400:.2f}x{slide_h/914400:.2f} in\n")

    margin = Inches(0.1)
    gap = Inches(0.1)
    max_h = Inches(3.0)

    placed_total = 0
    for slide_idx, filenames in LAYOUT.items():
        if only_slides is not None and slide_idx not in only_slides:
            continue
        if slide_idx >= len(prs.slides):
            print(f"WARN: slide {slide_idx + 1} doesn't exist in deck — skipping",
                  file=sys.stderr)
            continue
        slide = prs.slides[slide_idx]

        n = len(filenames)
        available_w = slide_w - 2 * margin - (n - 1) * gap
        per_w = available_w // n
        print(f"Slide {slide_idx + 1}: placing {n} image(s)")
        for i, fname in enumerate(filenames):
            path = samples / fname
            if not path.is_file():
                print(f"  ! missing {fname} — skipping", file=sys.stderr)
                continue
            im = PILImage.open(path)
            iw, ih = im.size
            aspect = ih / iw
            target_w = per_w
            target_h = int(target_w * aspect)
            if target_h > max_h:
                target_h = max_h
                target_w = int(target_h / aspect)
            x = margin + i * (per_w + gap) + (per_w - target_w) // 2
            y = slide_h - target_h - Inches(0.1)
            slide.shapes.add_picture(str(path), x, y, target_w, target_h)
            print(f"  + {fname}  {target_w/914400:.2f}x{target_h/914400:.2f}in")
            placed_total += 1

    prs.save(str(pptx_path))
    print(f"\nSaved -> {pptx_path}")
    print(f"Pasted {placed_total} image(s) total.")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--sku", required=True,
                   help="Sample folder name under samples/")
    p.add_argument("--pptx", required=True, type=Path,
                   help="Target .pptx file (overwritten in place)")
    p.add_argument("--only-slides", default=None,
                   help="Comma-separated 1-based slide numbers to paste "
                        "(e.g. '14,15'). Default: all slides in LAYOUT.")
    args = p.parse_args()
    only = None
    if args.only_slides:
        # User passes 1-based slide numbers; LAYOUT uses 0-based indices.
        only = {int(s.strip()) - 1 for s in args.only_slides.split(",")
                if s.strip()}
    return paste(args.sku, args.pptx, only_slides=only)


if __name__ == "__main__":
    sys.exit(main())
