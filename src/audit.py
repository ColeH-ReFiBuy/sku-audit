"""SKU audit CLI — crops a Google AI Mode screenshot into 3 regions.

Usage:
    python src/audit.py <screenshot.png> --sku <name> [--aco-json X] [--template Y]

Outputs:
    output/<sku>/query.png    — the user query bubble
    output/<sku>/answer.png   — the AI answer list (heading + product entries)
    output/<sku>/panel.png    — the right-side product detail panel
    output/<sku>/debug.png    — overlay showing detected regions on the source
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
from PIL import Image

# Reach into the detection module — same directory.
sys.path.insert(0, str(Path(__file__).parent))
from detect_crops import (
    BBox,
    draw_debug_overlay,
    find_answer_list,
    find_query_bubble,
    find_right_panel,
    load_ground_truth,
)


def main() -> int:
    p = argparse.ArgumentParser(description="Crop a Google AI Mode screenshot for SKU audit.")
    p.add_argument("image", type=Path, help="Path to the full-page screenshot (PNG).")
    p.add_argument("--sku", required=True, help="SKU/product slug used for the output folder name.")
    p.add_argument("--out-dir", type=Path, default=Path("output"), help="Root output directory.")
    p.add_argument("--aco-json", type=Path, default=None,
                   help="(Reserved) Path to ACO checker JSON — wired into deck assembly later.")
    p.add_argument("--template", type=Path, default=None,
                   help="(Reserved) Path to a .pptx template — used by deck assembly later.")
    args = p.parse_args()

    if not args.image.exists():
        print(f"ERROR: screenshot not found: {args.image}", file=sys.stderr)
        return 1

    if args.aco_json and not args.aco_json.exists():
        print(f"WARN: --aco-json path doesn't exist (ignored for now): {args.aco_json}", file=sys.stderr)
    if args.template and not args.template.exists():
        print(f"WARN: --template path doesn't exist (ignored for now): {args.template}", file=sys.stderr)

    out_dir = args.out_dir / args.sku
    out_dir.mkdir(parents=True, exist_ok=True)

    im = Image.open(args.image).convert("RGB")
    arr = np.array(im)

    bubble = find_query_bubble(arr)
    panel = find_right_panel(arr)
    answer = find_answer_list(arr, bubble, panel) if bubble and panel else None

    detected: dict[str, BBox | None] = {"query": bubble, "answer": answer, "panel": panel}
    failed = [n for n, b in detected.items() if b is None]
    if failed:
        print(f"ERROR: could not detect: {', '.join(failed)}", file=sys.stderr)

    for name, bbox in detected.items():
        if bbox is None:
            continue
        im.crop(bbox.as_tuple()).save(out_dir / f"{name}.png")
        print(f"[+] {name}.png  ({bbox.w}x{bbox.h})")

    gt = load_ground_truth(args.image.parent, args.image)
    draw_debug_overlay(args.image, detected, gt, out_dir / "debug.png")
    print(f"\nWrote crops to {out_dir}/")

    # Stub for future deck assembly — record what was detected.
    manifest = {
        "sku": args.sku,
        "source_screenshot": str(args.image),
        "crops": {
            name: {"bbox": bb.as_tuple(), "size": [bb.w, bb.h]} if bb else None
            for name, bb in detected.items()
        },
        "aco_json": str(args.aco_json) if args.aco_json else None,
        "template": str(args.template) if args.template else None,
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))

    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
