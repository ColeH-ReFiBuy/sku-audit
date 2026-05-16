"""Inspect the right product detail panel after click — what's actually in the DOM."""
from __future__ import annotations
import sys, time
from pathlib import Path
from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).parent))
from capture import (
    CDP_PORT, DEFAULT_PROFILE, launch_chrome_if_needed, find_first_product_link,
)
from urllib.parse import urlencode


def main() -> int:
    title = sys.argv[1] if len(sys.argv) > 1 else "cool office chairs under 200 dollars"
    query = f"I really want the {title}"
    url = "https://www.google.com/search?" + urlencode({"q": query, "udm": "50"})

    launch_chrome_if_needed(DEFAULT_PROFILE, CDP_PORT)
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{CDP_PORT}")
        ctx = browser.contexts[0]
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.emulate_media(color_scheme="dark")
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
        try:
            page.wait_for_load_state("networkidle", timeout=45000)
        except Exception:
            pass
        time.sleep(6)
        if not find_first_product_link(page):
            print("ERROR: no product link clicked")
            return 1
        time.sleep(8)

        # Inspect every element that contains offer-like text. Find ones with
        # internal scroll or hidden overflow.
        info = page.evaluate("""
            () => {
              const out = { offers: [], scrollables: [] };

              // 1) Look for any text resembling a retailer offer line
              //    (something with $XX.XX or $XX price patterns)
              const all = Array.from(document.querySelectorAll('*'));
              const offerRe = /\\$\\d+(\\.\\d+)?/;
              const offers = [];
              all.forEach(el => {
                if (el.children.length > 0) return;  // text leaves only
                const txt = (el.textContent || '').trim();
                if (!txt) return;
                if (!offerRe.test(txt)) return;
                if (txt.length > 80) return;
                const r = el.getBoundingClientRect();
                if (r.width === 0 || r.height === 0) return;
                offers.push({
                  text: txt.slice(0, 80),
                  x: Math.round(r.x), y: Math.round(r.y),
                  w: Math.round(r.width), h: Math.round(r.height),
                });
              });
              out.offers = offers.filter(o => o.x > 800).slice(0, 30);

              // 2) Find any scrollable container in the right half of the page
              all.forEach(el => {
                const s = getComputedStyle(el);
                const oy = s.overflowY;
                if ((oy !== 'auto' && oy !== 'scroll' && oy !== 'hidden')) return;
                if (el.scrollHeight <= el.clientHeight + 5) return;
                const r = el.getBoundingClientRect();
                if (r.width === 0 || r.x < 700) return;
                out.scrollables.push({
                  tag: el.tagName,
                  cls: (el.className || '').slice(0, 60),
                  overflowY: oy,
                  clientHeight: el.clientHeight,
                  scrollHeight: el.scrollHeight,
                  hidden: el.scrollHeight - el.clientHeight,
                  x: Math.round(r.x), y: Math.round(r.y),
                  w: Math.round(r.width), h: Math.round(r.height),
                });
              });
              return out;
            }
        """)

        print(f"\n=== OFFER-LIKE TEXT NODES IN RIGHT HALF ({len(info['offers'])}) ===")
        for o in info["offers"]:
            print(f"  ({o['x']:4d}, {o['y']:4d})  '{o['text']}'")

        print(f"\n=== SCROLLABLE CONTAINERS IN RIGHT HALF ({len(info['scrollables'])}) ===")
        for s in info["scrollables"]:
            print(f"  {s['tag']:6s} cls='{s['cls']}'  overflow={s['overflowY']}  "
                  f"client={s['clientHeight']}  scroll={s['scrollHeight']}  "
                  f"hidden={s['hidden']}px  at ({s['x']},{s['y']})  {s['w']}x{s['h']}")

        return 0


if __name__ == "__main__":
    sys.exit(main())
