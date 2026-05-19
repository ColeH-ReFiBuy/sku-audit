#!/usr/bin/env python3
"""Fix the broken XML in ReFiBuy ACO report PPTX files so PowerPoint
stops asking to repair them on open.

The ReFiBuy report generator emits hyperlink Relationship entries with
raw `&` characters in URLs (e.g. `?item=11520&name=...`). Raw `&` is
invalid inside an XML attribute value — it must be `&amp;`. PowerPoint
detects the malformed XML, prompts to "repair", and the repair often
strips images along with the bad hyperlinks.

This script rewrites the PPTX in-place:
  - For each ppt/slides/_rels/*.xml.rels, find every Target="..." that
    contains a raw `&` (not already part of an entity reference) and
    replace it with `&amp;`.
  - Original file is backed up to <name>.bak.pptx next to it.

Usage:
    python fix_rfb_pptx.py "/path/to/rfb-aco-report-...pptx"

Safe to run multiple times — already-escaped files become a no-op.
"""
from __future__ import annotations

import re
import shutil
import sys
import zipfile
from pathlib import Path


# Match `&` that is NOT already the start of an entity reference like
# `&amp;`, `&lt;`, `&#39;`, etc. We use a negative lookahead for the
# common XML entities.
RAW_AMP = re.compile(r"&(?!(?:amp|lt|gt|quot|apos|#\d+|#x[0-9A-Fa-f]+);)")


TARGET_ATTR = re.compile(r'Target="([^"]*)"')


def fix_rels_xml(xml_bytes: bytes) -> tuple[bytes, int]:
    """Return (fixed_bytes, num_urls_actually_fixed)."""
    text = xml_bytes.decode("utf-8")
    fix_count = 0

    def _sub(match: re.Match) -> str:
        nonlocal fix_count
        url = match.group(1)
        fixed = RAW_AMP.sub("&amp;", url)
        if fixed != url:
            fix_count += 1
        return f'Target="{fixed}"'

    fixed_text = TARGET_ATTR.sub(_sub, text)
    return fixed_text.encode("utf-8"), fix_count


def fix_pptx(path: Path) -> None:
    if not path.is_file():
        print(f"ERROR: not a file: {path}", file=sys.stderr)
        sys.exit(1)
    if path.suffix.lower() != ".pptx":
        print(f"ERROR: not a .pptx: {path}", file=sys.stderr)
        sys.exit(1)

    backup = path.with_name(path.stem + ".bak" + path.suffix)
    shutil.copy2(path, backup)
    print(f"Backed up -> {backup.name}")

    tmp_out = path.with_name(path.stem + ".__fixing__" + path.suffix)
    total_fixes = 0
    rels_touched = 0

    with zipfile.ZipFile(path, "r") as zin, \
            zipfile.ZipFile(tmp_out, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            data = zin.read(item.filename)
            if item.filename.startswith("ppt/slides/_rels/") \
                    and item.filename.endswith(".xml.rels"):
                new_data, n = fix_rels_xml(data)
                if n and new_data != data:
                    rels_touched += 1
                    total_fixes += n
                    print(f"  fixed {item.filename}")
                data = new_data
            zout.writestr(item, data)

    tmp_out.replace(path)
    print(f"\nFixed {total_fixes} URL(s) across {rels_touched} rels file(s).")
    print(f"Saved -> {path}")


def main() -> int:
    if len(sys.argv) != 2:
        print("Usage: fix_rfb_pptx.py <path-to-pptx>", file=sys.stderr)
        return 2
    fix_pptx(Path(sys.argv[1]).expanduser().resolve())
    return 0


if __name__ == "__main__":
    sys.exit(main())
