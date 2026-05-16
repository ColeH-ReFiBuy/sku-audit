"""Structural crop detection for Google AI Mode screenshots.

Finds three regions on a full-page screenshot:
  1. query bubble  — small lighter-gray rounded rect near top of content
  2. answer list   — left/center column content below the bubble
  3. right panel   — product detail panel anchored to the right
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw


# Color signatures observed on the dark-mode Google AI screenshot.
PAGE_BG      = (34, 36, 42)
PANEL_BG     = (40, 41, 42)
BUBBLE_FILL  = (45, 46, 53)


@dataclass
class BBox:
    x1: int
    y1: int
    x2: int
    y2: int

    @property
    def w(self) -> int: return self.x2 - self.x1
    @property
    def h(self) -> int: return self.y2 - self.y1
    def as_tuple(self): return (self.x1, self.y1, self.x2, self.y2)


def find_query_bubble(arr: np.ndarray) -> BBox | None:
    """Color-mask the bubble fill in the top portion of the image.

    Search is restricted to the horizontal center band so that browser
    chrome tabs and the macOS notification banner (top-right) don't trigger
    a false match — they share a similar dark-gray fill.
    """
    h, w = arr.shape[:2]
    # Cap the bubble search vertically by absolute pixels rather than a
    # fraction of height — Playwright captures with tall viewports (12000+
    # px) make a proportional cap skip past the bubble's real position.
    # Bubble sits at y~200-600 in 3x DPI captures; bound generously.
    y_top = 0
    y_bot = min(int(h * 0.35), 1500)
    x_lo = int(w * 0.15)            # skip left sidebar / G logo
    x_hi = int(w * 0.75)            # skip right notification corner
    region = arr[y_top:y_bot, x_lo:x_hi]

    r, g, b = region[..., 0], region[..., 1], region[..., 2]
    mask = (
        (r >= 42) & (r <= 52) &
        (g >= 43) & (g <= 53) &
        (b >= 48) & (b <= 62)
    )
    if mask.sum() < 1000:
        return None

    # The same bubble fill color appears on the panel's top-bar (close-X,
    # share, options icon row). Splitting on connected components lets us
    # pick just the actual query bubble — it's the one closest to the
    # horizontal centerline of the search region.
    import cv2
    num, labels, stats, _ = cv2.connectedComponentsWithStats(mask.astype(np.uint8), connectivity=8)
    if num <= 1:
        return None
    region_w = mask.shape[1]
    center_x = region_w // 2
    best = None
    for i in range(1, num):
        x = int(stats[i, cv2.CC_STAT_LEFT])
        y = int(stats[i, cv2.CC_STAT_TOP])
        cw = int(stats[i, cv2.CC_STAT_WIDTH])
        ch = int(stats[i, cv2.CC_STAT_HEIGHT])
        area = int(stats[i, cv2.CC_STAT_AREA])
        if area < 500:
            continue
        # Bubble is wide-but-short and reasonably centered.
        if ch > cw:
            continue
        cx = x + cw / 2
        dist = abs(cx - center_x)
        score = (dist, -area)  # closer to center wins; ties go to bigger
        if best is None or score < best[0]:
            best = (score, x, y, x + cw, y + ch)
    if best is None:
        return None
    _, x1_local, y1_local, x2_local, y2_local = best

    pad = 10
    x1 = max(0, x1_local + x_lo - pad)
    y1 = max(0, y1_local + y_top - pad)
    x2 = min(w, x2_local + x_lo + pad)
    y2 = min(h, y2_local + y_top + pad)
    return BBox(x1, y1, x2, y2)


def find_right_panel(arr: np.ndarray) -> BBox | None:
    """Find the right-side panel by anchoring on its bright product image.

    The panel itself has no continuous fill — page bg shows through between
    elements. But the product image at the top of the panel is a large, very
    bright rectangle, which is unmistakable. We find it, then expand to the
    bottom-most non-bg row within its x-range to bound the whole panel.
    """
    h, w = arr.shape[:2]

    # Bright pixels (>200 in all channels) — captures product photos / tiles.
    bright = (arr[..., 0] > 200) & (arr[..., 1] > 200) & (arr[..., 2] > 200)
    bright = bright.copy()
    # Browser chrome (tabs + URL bar + favicons) on manual Cmd+Shift+3
    # captures sits in the top ~250-300 px. On Playwright full_page captures
    # there's no browser chrome at all, but the cap is harmless. Use an
    # absolute pixel value rather than a fraction of height so tall
    # Playwright captures (12k+ px) still work.
    bright[:300] = False
    bright[:, : w // 2] = False               # restrict to right half

    # Use connected components, but FIRST apply horizontal dilation so a
    # multi-color product image (e.g. pink/blue bottles on white) doesn't
    # split into separate components.
    #
    # Before dilation, drop tiny components (text-glyph fragments that
    # straddle the image midline). Otherwise dilation can bridge them
    # diagonally to the actual product image and pull the panel left edge
    # 100+ px into the answer column.
    import cv2
    bright_u8 = bright.astype(np.uint8)

    num0, labels0, stats0, _ = cv2.connectedComponentsWithStats(bright_u8, connectivity=8)
    MIN_COMP_AREA = 200
    keep = np.zeros_like(bright_u8)
    for i in range(1, num0):
        if stats0[i, cv2.CC_STAT_AREA] >= MIN_COMP_AREA:
            keep[labels0 == i] = 1

    dilate_radius = 40
    kernel = np.ones((1, 2 * dilate_radius + 1), np.uint8)
    dilated = cv2.dilate(keep, kernel, iterations=1)

    num, labels, stats, _ = cv2.connectedComponentsWithStats(dilated, connectivity=8)
    if num <= 1:
        return None
    areas = stats[1:, cv2.CC_STAT_AREA]
    largest = int(areas.argmax()) + 1
    img_x1 = int(stats[largest, cv2.CC_STAT_LEFT])
    img_y1 = int(stats[largest, cv2.CC_STAT_TOP])
    img_x2 = img_x1 + int(stats[largest, cv2.CC_STAT_WIDTH])
    img_y2 = img_y1 + int(stats[largest, cv2.CC_STAT_HEIGHT])
    # Shrink horizontally to recover the un-dilated extent (approximate).
    img_x1 = min(img_x1 + dilate_radius, img_x2 - 1)
    img_x2 = max(img_x2 - dilate_radius, img_x1 + 1)

    panel_left = img_x1
    panel_right = img_x2
    panel_top = img_y1

    # Panel bottom: stop at the bottom edge of the "More stores ⌄" button,
    # which sits between the last offer card and the "About this product"
    # heading. The button is a pill with the same lighter fill as the
    # search bar (~RGB 55,56,63). We look for a contiguous run of rows
    # below the product image where most of the panel column is filled
    # with that pill color.
    pill_mask = (
        (arr[..., 0] >= 50) & (arr[..., 0] <= 68)
        & (arr[..., 1] >= 50) & (arr[..., 1] <= 68)
        & (arr[..., 2] >= 55) & (arr[..., 2] <= 75)
    )
    panel_pill_counts = pill_mask[:, panel_left:panel_right].sum(axis=1)
    pill_threshold = (panel_right - panel_left) * 0.5
    min_button_height = 20

    button_bottom: int | None = None
    in_run, run_start = False, 0
    # Skip ~1000 rows below the product image — that bypasses title, color
    # swatches, size-selector pills, and "Typically $X" so we anchor on a
    # later pill ("More stores"), not an early one (size).
    for i in range(min(img_y2 + 1000, h), h):
        has = panel_pill_counts[i] > pill_threshold
        if has and not in_run:
            in_run, run_start = True, i
        elif not has and in_run:
            in_run = False
            if i - run_start >= min_button_height:
                button_bottom = i
                break
    if in_run and button_bottom is None and h - run_start >= min_button_height:
        button_bottom = h

    if button_bottom is not None:
        panel_bottom = button_bottom + 30
    else:
        # Fallback: walk down, allow short gaps between sections, but stop
        # at a long sustained gap (anything that long is unrelated content
        # below the panel — macOS screenshot thumbnail, OS UI, etc.).
        strip = arr[img_y2:, panel_left:panel_right]
        not_bg = strip[..., 0] >= 38
        row_has_content = not_bg.sum(axis=1) > (panel_right - panel_left) * 0.05
        last_content = -1
        gap = 0
        for i, has in enumerate(row_has_content):
            if has:
                last_content = i
                gap = 0
            else:
                gap += 1
                # Gap threshold is scaled with image size: bigger captures
                # (3x DPI) have proportionally larger inter-card gaps. 5% of
                # height covers both 2x and 3x captures comfortably.
                if gap >= max(80, int(h * 0.05)) and last_content >= 0:
                    break
        panel_bottom = img_y2 + last_content + 1 if last_content >= 0 else h

    return BBox(panel_left, panel_top, panel_right, min(panel_bottom, h))


def find_answer_list(
    arr: np.ndarray,
    bubble: BBox,
    panel: BBox,
    pad_right: int = 30,
    min_intro_gap: int = 25,
) -> BBox | None:
    """Bound the AI answer column between the bubble and the panel.

    Top: skip the intro paragraph by finding the first vertical gap of empty
         rows after the bubble; the section heading is just below that gap.
    Left: first non-sidebar column with content below the bubble.
    Right: panel.x1 minus a generous gap so the crop doesn't run against the
           panel border.
    Bottom: locate product-thumbnail bands and cut to include 2 full entries
            plus the title/price line of the 3rd.
    """
    h, w = arr.shape[:2]
    max_right = panel.x1 - pad_right
    if max_right <= 200:
        return None

    # 1) Find the section heading by skipping the intro paragraph.
    # Look at text density per row from just below the bubble downward.
    search_top = bubble.y2 + 10
    search_strip = arr[search_top:, 150:max_right]
    bright_text = (
        (search_strip[..., 0] > 150)
        & (search_strip[..., 1] > 150)
        & (search_strip[..., 2] > 150)
    )
    row_text = bright_text.sum(axis=1) > 30  # rows with meaningful text

    # Walk: first text run = intro paragraph. First gap >= min_intro_gap means
    # we just left the intro; the next text run is the section heading.
    answer_top = None
    saw_text = False
    gap = 0
    for i, t in enumerate(row_text):
        if t:
            if saw_text and gap >= min_intro_gap:
                answer_top = search_top + i - 8  # tiny margin above heading
                break
            saw_text = True
            gap = 0
        elif saw_text:
            gap += 1
    if answer_top is None:
        answer_top = bubble.y2 + 50  # fallback

    # 2) Left edge — first non-sidebar column with content below answer_top.
    strip = arr[answer_top:, :max_right]
    not_bg = strip[..., 0] >= 50
    col_counts = not_bg.sum(axis=0)
    answer_left = None
    for x in range(150, max_right):
        if col_counts[x] > 20:
            answer_left = x
            break
    if answer_left is None:
        return None
    answer_left = max(answer_left - 20, 0)

    # 3) Bottom — primary anchor: the "Ask anything" search bar pill, which
    # sits below all answer content. Its fill is RGB ~(55,56,63), clearly
    # distinct from page bg (34,36,42) and the query bubble (45,46,53). The
    # pill spans most of the answer column width.
    sb_mask = (
        (arr[..., 0] >= 50) & (arr[..., 0] <= 68)
        & (arr[..., 1] >= 50) & (arr[..., 1] <= 68)
        & (arr[..., 2] >= 55) & (arr[..., 2] <= 75)
    )
    sb_row_counts = sb_mask[:, answer_left:max_right].sum(axis=1)
    sb_threshold = (max_right - answer_left) * 0.5
    high_rows = sb_row_counts > sb_threshold

    # The search bar pill spans tens of rows; horizontal dividers between
    # answer entries are 1–2 rows of the same fill color and produce false
    # positives. Require a contiguous block of >= MIN_SB_HEIGHT rows.
    min_sb_height = 30
    search_bar_top: int | None = None
    in_run, run_start = False, 0
    for i, h_in in enumerate(high_rows):
        if h_in and not in_run:
            in_run, run_start = True, i
        elif not h_in and in_run:
            in_run = False
            if i - run_start >= min_sb_height and run_start > answer_top + 200:
                search_bar_top = run_start
                break
    if in_run and search_bar_top is None and len(high_rows) - run_start >= min_sb_height:
        if run_start > answer_top + 200:
            search_bar_top = run_start

    if search_bar_top is not None:
        # Walk UP from just above the search bar to find the actual last row of
        # content. Pages with sparse answers (e.g. bullet-list-only responses)
        # leave a big empty band before the search bar.
        upper_bound = max(answer_top + 50, search_bar_top - 30)
        content_strip = arr[answer_top:upper_bound, answer_left:max_right]
        cs_not_bg = content_strip[..., 0] >= 50
        row_has_content = cs_not_bg.sum(axis=1) > 30
        content_rows = np.where(row_has_content)[0]
        if len(content_rows) > 0:
            answer_bottom = answer_top + int(content_rows[-1]) + 30
        else:
            answer_bottom = upper_bound

        # Refine the right edge: find the rightmost column with content in
        # this answer band. Capped at max_right (panel left - gap).
        band = arr[answer_top:answer_bottom, :max_right]
        b_not_bg = band[..., 0] >= 50
        col_has_content = b_not_bg.sum(axis=0) > 5
        content_cols = np.where(col_has_content)[0]
        if len(content_cols) > 0:
            answer_right = min(int(content_cols[-1]) + 25, max_right)
        else:
            answer_right = max_right

        return BBox(answer_left, answer_top, answer_right, min(answer_bottom, panel.y2, h))

    # Fallback: detect product thumbnail bands on the LEFT of the column.
    # Used when the search bar isn't visible (e.g. unusual capture).
    sub = arr[answer_top:, answer_left : answer_left + 280]
    sub_bright = (sub[..., 0] > 200) & (sub[..., 1] > 200) & (sub[..., 2] > 200)
    band_rows = sub_bright.sum(axis=1) > 30

    bands: list[tuple[int, int]] = []
    in_band, start = False, 0
    for i, v in enumerate(band_rows):
        if v and not in_band:
            in_band, start = True, i
        elif not v and in_band:
            in_band = False
            if i - start > 60:                       # min thumbnail height
                bands.append((start, i))
    if in_band and len(band_rows) - start > 60:
        bands.append((start, len(band_rows)))

    # Target: 3 full entries. Anchor on the Nth thumbnail, then walk down
    # row-by-row and stop at the first sustained vertical gap (>= 40 empty
    # rows). That cuts off cleanly at the end of the entry's description,
    # before unrelated content like the "Ask anything" search bar.
    target_n = 3
    n_visible = min(target_n, len(bands))
    if n_visible == 0:
        answer_bottom = min(answer_top + 1300, panel.y2)
    else:
        anchor_band = bands[n_visible - 1]
        search_from = answer_top + anchor_band[0]
        search_to = min(answer_top + anchor_band[0] + 800, panel.y2)
        col_strip = arr[search_from:search_to, answer_left:max_right]
        text_rows = (
            (col_strip[..., 0] > 150)
            & (col_strip[..., 1] > 150)
            & (col_strip[..., 2] > 150)
        ).sum(axis=1) > 20

        last_text_idx = -1
        gap = 0
        for i, t in enumerate(text_rows):
            if t:
                last_text_idx = i
                gap = 0
            else:
                gap += 1
                if gap >= 40 and last_text_idx >= 0:
                    break

        if last_text_idx >= 0:
            answer_bottom = search_from + last_text_idx + 30
        else:
            answer_bottom = search_from + 350

    answer_bottom = min(answer_bottom, panel.y2, h)
    return BBox(answer_left, answer_top, max_right, answer_bottom)


def draw_debug_overlay(
    image_path: Path,
    detected: dict[str, BBox | None],
    ground_truth: dict[str, BBox] | None,
    out_path: Path,
) -> None:
    """Write a copy of the image with detected (red) and ground-truth (green) bboxes."""
    im = Image.open(image_path).convert("RGB")
    draw = ImageDraw.Draw(im)

    for name, bbox in detected.items():
        if bbox is None:
            continue
        draw.rectangle(bbox.as_tuple(), outline=(255, 60, 60), width=8)
        draw.text((bbox.x1 + 8, bbox.y1 + 8), f"detected:{name}", fill=(255, 60, 60))
    if ground_truth:
        for name, bbox in ground_truth.items():
            draw.rectangle(bbox.as_tuple(), outline=(60, 220, 80), width=4)
            draw.text((bbox.x1 + 8, bbox.y2 - 28), f"truth:{name}", fill=(60, 220, 80))

    im.save(out_path)


def load_ground_truth(samples_dir: Path, full_path: Path) -> dict[str, BBox] | None:
    """If target_*.png files exist alongside the full page, locate them by
    template matching so we can overlay ground truth."""
    targets = {
        "query":  samples_dir / "target_query.png",
        "answer": samples_dir / "target_answer.png",
        "panel":  samples_dir / "target_panel.png",
    }
    if not all(p.exists() for p in targets.values()):
        return None

    try:
        import cv2
    except ImportError:
        return None

    full = np.array(Image.open(full_path).convert("RGB"))
    full_bgr = full[..., ::-1].copy()
    gt = {}
    for name, p in targets.items():
        t = np.array(Image.open(p).convert("RGB"))[..., ::-1].copy()
        res = cv2.matchTemplate(full_bgr, t, cv2.TM_SQDIFF_NORMED)
        _, _, min_loc, _ = cv2.minMaxLoc(res)
        x, y = min_loc
        th, tw = t.shape[:2]
        gt[name] = BBox(x, y, x + tw, y + th)
    return gt


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("image", type=Path, help="Path to the full-page screenshot")
    p.add_argument("--out-dir", type=Path, default=Path("output/_detect"))
    p.add_argument("--sku", default="sample")
    args = p.parse_args()

    out_dir = args.out_dir / args.sku
    out_dir.mkdir(parents=True, exist_ok=True)

    im = Image.open(args.image).convert("RGB")
    arr = np.array(im)

    bubble = find_query_bubble(arr)
    panel = find_right_panel(arr)
    answer = find_answer_list(arr, bubble, panel) if bubble and panel else None

    detected = {"query": bubble, "answer": answer, "panel": panel}
    for name, bb in detected.items():
        if bb is None:
            print(f"[!] {name}: NOT DETECTED")
            continue
        print(f"[+] {name}: ({bb.x1}, {bb.y1}, {bb.x2}, {bb.y2})   size={bb.w}x{bb.h}")
        crop = im.crop(bb.as_tuple())
        crop.save(out_dir / f"crop_{name}.png")

    gt = load_ground_truth(args.image.parent, args.image)
    if gt:
        print("\nGround truth (from target_*.png template match):")
        for name, bb in gt.items():
            print(f"    {name}: ({bb.x1}, {bb.y1}, {bb.x2}, {bb.y2})   size={bb.w}x{bb.h}")

    overlay_path = out_dir / "debug_overlay.png"
    draw_debug_overlay(args.image, detected, gt, overlay_path)
    print(f"\nDebug overlay: {overlay_path}")
    print(f"Crops:         {out_dir}/crop_*.png")
    return 0


if __name__ == "__main__":
    sys.exit(main())
