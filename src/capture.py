"""Thin dispatcher that routes --source to the right engine module.

Each engine lives in its own file:
  --source google   -> src/gemini.py        capture()
  --source chatgpt  -> src/gpt.py           capture_chatgpt()
  --source alexa    -> src/alexa.py         capture_alexa()
  --source sparky   -> src/capture_sparky.py capture_sparky()
  --source walmart_reviews -> src/capture_sparky.py capture_walmart_bad_reviews()

The engines do NOT import from each other. Shared helpers (Chrome
management, URL utilities) live in src/common.py.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from common import (
    CDP_PORT,
    DEFAULT_PROFILE,
    ROOT,
    extract_product_title,
    is_amazon_url,
    is_url,
    normalize_url,
)

from gemini import capture as _capture_google
from gpt import capture_chatgpt as _capture_chatgpt
from alexa import capture_alexa as _capture_alexa

def main() -> int:
    p = argparse.ArgumentParser(
        description="Capture + crop a Google AI Mode result for a product.",
    )
    p.add_argument("product", help="Product title OR a retailer product URL "
                                    "(Target, Walmart, Amazon, etc.). When a "
                                    "URL is given, the page <title> is "
                                    "fetched and used as the search query.")
    p.add_argument("--sku", default=None,
                   help="Output folder slug. Auto-derived from the title if "
                        "omitted.")
    p.add_argument("--source",
                   choices=["google", "chatgpt", "alexa", "sparky",
                            "walmart_reviews", "all"],
                   default="all",
                   help="Which engine(s) to capture. Default: all.")
    p.add_argument("--alexa-product",
                   default=None,
                   help="Override product for the Alexa flow only. Pass a "
                        "different product title or Amazon URL. Google + "
                        "ChatGPT still use the main 'product' argument. "
                        "Use this when auditing one product on web AI engines "
                        "and a related Amazon listing on Alexa.")
    p.add_argument("--sparky-url",
                   default=None,
                   help="Walmart product URL for Sparky + 1-star reviews. "
                        "Sparky chat runs via the AVD (must already have "
                        "the same product PDP open); 1-star reviews run via "
                        "the desktop Chrome at the same URL.")
    p.add_argument("--crop", action="store_true",
                   help="Also run the crop detector after capture. Off by "
                        "default — full screenshots are easier to edit "
                        "manually than potentially-misaligned auto-crops.")
    p.add_argument("--zoom", type=float, default=1.0,
                   help="Page zoom for the post-click Google screenshot "
                        "(default 1.0 = native, no zoom).")
    p.add_argument("--profile-dir", type=Path, default=DEFAULT_PROFILE)
    p.add_argument("--port", type=int, default=CDP_PORT,
                   help="CDP debug port (default 9222).")
    args = p.parse_args()

    # Accept bare URLs like "amazon.com/dp/B0000CF7JZ" too (no scheme).
    normalized = normalize_url(args.product)
    amazon_url: str | None = None
    if normalized:
        print(f"Fetching product title from URL: {normalized}")
        try:
            title = extract_product_title(normalized)
        except Exception as e:
            print(f"ERROR: could not fetch title — {e}", file=sys.stderr)
            return 1
        if not title:
            print(f"ERROR: no title found at {normalized}", file=sys.stderr)
            return 1
        print(f"Extracted title: {title!r}")
        if is_amazon_url(normalized):
            amazon_url = normalized
            print(f"Amazon URL detected — Alexa will navigate directly "
                  f"(skipping search step).")
    else:
        title = args.product

    sku = args.sku or re.sub(r"[^a-z0-9]+", "_", title.lower()).strip("_")[:50]
    print(f"SKU: {sku}")

    google_shot: Path | None = None
    chatgpt_shot: Path | None = None
    alexa_shots: list[Path] = []

    if args.source in ("google", "all"):
        google_shot = _capture_google(
            product_title=title,
            sku=sku,
            profile_dir=args.profile_dir,
            port=args.port,
            zoom=args.zoom,
        )

    if args.source in ("chatgpt", "all"):
        chatgpt_shot = _capture_chatgpt(
            product_title=title,
            sku=sku,
            profile_dir=args.profile_dir,
            port=args.port,
        )

    if args.source in ("alexa", "all"):
        # Allow a separate product for Alexa (e.g. when auditing one
        # product on web engines + a related Amazon listing on Alexa).
        alexa_title = title
        alexa_amazon_url = amazon_url
        if args.alexa_product:
            ap_norm = normalize_url(args.alexa_product)
            if ap_norm:
                print(f"Fetching Alexa product title from URL: {ap_norm}")
                try:
                    alexa_title = extract_product_title(ap_norm)
                except Exception as e:
                    print(f"ERROR: could not fetch Alexa product title — {e}",
                          file=sys.stderr)
                    return 1
                if is_amazon_url(ap_norm):
                    alexa_amazon_url = ap_norm
                else:
                    alexa_amazon_url = None
            else:
                alexa_title = args.alexa_product
                alexa_amazon_url = None
            print(f"Alexa flow will use product: {alexa_title!r}")

        alexa_shots = _capture_alexa(
            product_title=alexa_title,
            sku=sku,
            profile_dir=args.profile_dir,
            port=args.port,
            amazon_url=alexa_amazon_url,
        )

    sparky_shots: list[Path] = []
    walmart_review_shots: list[Path] = []
    if args.source in ("sparky", "all"):
        # Sparky chat — driven via the AVD. Requires the user to have
        # the target product PDP open in the Walmart Android app
        # already (we pass walmart_url=None so the script reads
        # whatever's on screen). The user signals "ready" by
        # pre-loading, that's the established workflow.
        try:
            from capture_sparky import capture_sparky as _capture_sparky
        except ImportError:
            import sys as _sys
            _sys.path.insert(0, str(ROOT / "src"))
            from capture_sparky import capture_sparky as _capture_sparky
        sparky_shots = _capture_sparky(walmart_url=None, sku=sku)

    if args.source in ("walmart_reviews", "all"):
        # Walmart 1-star reviews via desktop Chrome. Needs an explicit
        # walmart URL — pass --sparky-url. Skip silently if not given.
        if args.sparky_url:
            try:
                from capture_sparky import capture_walmart_bad_reviews
            except ImportError:
                import sys as _sys
                _sys.path.insert(0, str(ROOT / "src"))
                from capture_sparky import capture_walmart_bad_reviews
            walmart_review_shots = capture_walmart_bad_reviews(
                walmart_url=args.sparky_url,
                sku=sku,
                cdp_port=args.port,
                n=6,
            )
        else:
            print("\n[Walmart reviews] skipped — pass --sparky-url to enable.")

    print("\nDone.")
    if google_shot:
        print(f"  Google initial:    {google_shot.with_name('initial.png')}")
        print(f"  Google post-click: {google_shot}")
    if chatgpt_shot:
        print(f"  ChatGPT initial:   {chatgpt_shot.with_name('chatgpt_initial.png')}")
        print(f"  ChatGPT post-click: {chatgpt_shot}")
    for shot in alexa_shots:
        print(f"  Alexa:             {shot}")
    for shot in sparky_shots:
        print(f"  Sparky:            {shot}")
    for shot in walmart_review_shots:
        print(f"  Walmart review:    {shot}")

    if not args.crop or not google_shot:
        return 0

    print("\nRunning crop detector on Google screenshot...")
    audit_py = ROOT / "src" / "audit.py"
    rc = subprocess.call([
        sys.executable, str(audit_py), str(google_shot), "--sku", sku,
    ])
    return rc


if __name__ == "__main__":
    sys.exit(main())
