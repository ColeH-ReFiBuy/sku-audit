"""Capture a Google AI Mode screenshot for a product, then crop.

Usage:
    python src/capture.py "product title" --sku <name>

Workflow:
    1. Connect (or launch) a real Google Chrome instance with a remote
       debugging port + dedicated profile, then attach via CDP. Using real
       Chrome (not Playwright's Chromium) avoids Google's bot detection.
    2. Navigate to Google AI Mode (udm=50) with the query
       'I really want the <product title>'.
    3. Wait for the AI answer to finish streaming.
    4. Click the first underlined product link in the answer column.
    5. Wait for the product-detail panel to appear on the right.
    6. Take a full-page screenshot.
    7. Run the existing crop detector (src/audit.py) over the screenshot.

First-time setup:
    On the first run the script launches a separate Chrome window using a
    dedicated profile at `.chrome-profile/`. You sign in to Google once in
    that window. Subsequent runs reuse the saved session.
"""
from __future__ import annotations

import argparse
import re
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlencode, urlparse

from playwright.sync_api import Browser, Page, sync_playwright


PRODUCT_TITLE_FETCH_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en-US,en;q=0.9",
}

# Retailer-name suffixes / prefixes seen in product-page <title> tags.
TITLE_SUFFIX_PATTERNS = [
    r"\s*[:|–-]\s*Target\s*$",
    r"\s*[-|]\s*Walmart\.com\s*$",
    r"\s*[-|]\s*Walmart\s*$",
    r"\s*[-|]\s*Best Buy\s*$",
    r"\s*[-|]\s*Kohl'?s\s*$",
    r"\s*[-|]\s*Costco\s*$",
    r"\s*[-|]\s*Amazon\.com\s*$",
    r"\s*[-|]\s*eBay\s*$",
    r"\s*[-|]\s*REI\s*$",
]
TITLE_PREFIX_PATTERNS = [
    r"^Amazon\.com\s*:\s*",
    r"^Amazon\.com\s*-\s*",
]


def is_url(s: str) -> bool:
    try:
        u = urlparse(s)
        return u.scheme in ("http", "https") and bool(u.netloc)
    except Exception:
        return False


def extract_product_title(url: str) -> str:
    """Fetch a retailer product page and return a cleaned-up title for the
    AI Mode search query. Tries og:title first, then <title>, strips known
    retailer suffixes, and trims overly long titles at the first ' : '
    separator (used by Target etc. for the long-form description tail).
    """
    req = urllib.request.Request(url, headers=PRODUCT_TITLE_FETCH_HEADERS)
    with urllib.request.urlopen(req, timeout=20) as resp:
        html = resp.read().decode("utf-8", errors="replace")

    og = re.search(
        r'<meta[^>]*property="og:title"[^>]*content="([^"]+)"',
        html, re.IGNORECASE,
    )
    raw = og.group(1) if og else None
    if not raw:
        t = re.search(r"<title[^>]*>([^<]+)</title>", html, re.IGNORECASE)
        raw = t.group(1) if t else ""
    raw = raw.strip()
    if not raw:
        return ""

    title = raw
    for pat in TITLE_PREFIX_PATTERNS:
        title = re.sub(pat, "", title, flags=re.IGNORECASE).strip()
    for pat in TITLE_SUFFIX_PATTERNS:
        title = re.sub(pat, "", title, flags=re.IGNORECASE).strip()

    # Retailers (notably Target) tack a long marketing description after
    # the product name separated by ': '. If the result is long, cut at
    # the first ': ' to keep just the product name portion.
    if len(title) > 80 and ": " in title:
        head = title.split(": ", 1)[0].strip()
        if len(head) >= 20:
            title = head

    return title

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PROFILE = ROOT / ".chrome-profile"
CHROME_PATH = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
CDP_PORT = 9222


def find_first_product_link(page: Page) -> bool:
    """Click the first product-entry card in the left/center AI answer column.

    Google AI Mode product cards have hrefs like
    `https://www.google.com/search?ibp=oshop&prds=...` and a class signature
    including `amIOac` (subject to change — Google's class names are
    auto-generated). We pick the topmost such card with substantial size
    (width >= 400, height >= 60) — small inline product-viewer links are
    also `ibp=oshop` but are tiny and shouldn't be clicked.
    """
    # Try the big-card class first (most common), then any SmjhRb link (used
    # for narrower product-viewer-style entries), then a generic ibp=oshop
    # fallback. Pick the topmost-leftmost qualifying link.
    viewport = page.viewport_size or {"width": 1440, "height": 900}
    left_threshold = viewport["width"] * 0.6

    selector_attempts = [
        ('a.amIOac[href*="ibp=oshop"]', 400, 60),
        ('a.SmjhRb[href*="ibp=oshop"]', 100, 15),
        ('a[href*="ibp=oshop"]', 100, 15),
        ('a[href*="/shopping/product/"]', 100, 15),
    ]

    for sel, min_w, min_h in selector_attempts:
        links = page.locator(sel)
        try:
            count = links.count()
        except Exception:
            continue
        if count == 0:
            continue

        candidates = []
        for i in range(min(count, 30)):
            link = links.nth(i)
            try:
                if not link.is_visible():
                    continue
                box = link.bounding_box()
                if box is None or box["width"] < min_w or box["height"] < min_h:
                    continue
                # Stay in the left/center column of the viewport.
                if box["x"] + box["width"] / 2 > left_threshold:
                    continue
                text = (link.text_content() or "").strip()
                if len(text) < 8:
                    continue
                candidates.append((box["y"], box, text, link))
            except Exception:
                continue
        if not candidates:
            continue
        candidates.sort(key=lambda c: c[0])
        _, box, text, link = candidates[0]
        print(f'  selector "{sel}" matched: "{text[:60]}" at ({box["x"]:.0f}, {box["y"]:.0f})')
        link.scroll_into_view_if_needed(timeout=3000)
        link.click(timeout=5000)
        return True
    return False


def _port_is_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _cdp_ready(port: int) -> bool:
    if not _port_is_open(port):
        return False
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=1).read()
        return True
    except Exception:
        return False


def _current_chrome_dsf(port: int) -> float | None:
    """Connect to a running Chrome and read its devicePixelRatio. Returns
    None on any failure (Chrome not running, no pages, etc.)."""
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
            ctx = browser.contexts[0]
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            dsf = page.evaluate("() => window.devicePixelRatio")
            browser.close()
            return float(dsf)
    except Exception:
        return None


def _kill_chrome_on_port(profile_dir: Path, port: int) -> None:
    """Terminate any Chrome bound to --remote-debugging-port=<port> and
    clean up Singleton lockfiles so the next launch can claim the
    profile."""
    subprocess.call(
        ["pkill", "-9", "-f", f"remote-debugging-port={port}"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    time.sleep(2)
    for p in profile_dir.glob("Singleton*"):
        try:
            p.unlink()
        except Exception:
            pass


# Required DSF for capture screenshots. Setup helpers launch Chrome at
# 2x (readable for sign-in UX), but captures must use 3x — without this
# screenshots come out at ~3440px wide instead of the ~5184px native
# the user expects.
REQUIRED_DSF = 3.0


def launch_chrome_if_needed(profile_dir: Path, port: int = CDP_PORT) -> bool:
    """Ensure Chrome is running on `port` with the capture-mode flags
    (off-screen window, 3x device scale factor). If an existing Chrome
    is at the wrong DSF (e.g. left over from setup_*_login.py at 2x),
    kill and relaunch — cookies persist in the profile directory.

    Returns True if we launched/relaunched, False if we reused."""
    if _cdp_ready(port):
        dsf = _current_chrome_dsf(port)
        if dsf is not None and abs(dsf - REQUIRED_DSF) < 0.1:
            print(f"Reusing existing Chrome on port {port} (DSF {dsf}).")
            return False
        print(f"Existing Chrome on port {port} at DSF {dsf}, "
              f"need {REQUIRED_DSF} — relaunching...")
        _kill_chrome_on_port(profile_dir, port)

    if not Path(CHROME_PATH).exists():
        raise RuntimeError(f"Chrome not found at {CHROME_PATH}")

    profile_dir.mkdir(parents=True, exist_ok=True)
    print(f"Launching Chrome with --remote-debugging-port={port}\n  profile: {profile_dir}")
    subprocess.Popen(
        [CHROME_PATH, f"--remote-debugging-port={port}",
         f"--user-data-dir={profile_dir}",
         "--no-first-run", "--no-default-browser-check",
         # Position off-screen + reasonable size so Chrome doesn't grab focus
         # or overlay the user's current work. Page still renders normally;
         # screenshots use the renderer buffer, not screen capture.
         "--window-position=-3000,-3000",
         "--window-size=1728,1117",
         "--force-device-scale-factor=3",
         "--high-dpi-support=1"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )

    for _ in range(60):
        if _cdp_ready(port):
            print("Chrome is up.")
            return True
        time.sleep(0.5)
    raise RuntimeError(f"Chrome didn't expose CDP on port {port} after 30s")


def capture_chatgpt(product_title: str, sku: str,
                    profile_dir: Path = DEFAULT_PROFILE,
                    port: int = CDP_PORT) -> Path:
    """Send the audit query to ChatGPT, wait for the streamed response to
    complete, and screenshot the page. Uses the same dedicated Chrome
    profile — sign into OpenAI once and the session persists."""
    query = f"I really like the {product_title} can i buy it online"

    out_dir = ROOT / "samples" / sku
    out_dir.mkdir(parents=True, exist_ok=True)
    screenshot_path = out_dir / "chatgpt.png"

    print(f"\n[ChatGPT]")
    print(f"Query:    {query}")
    print(f"Output:   {screenshot_path}")

    launch_chrome_if_needed(profile_dir, port)

    with sync_playwright() as p:
        browser: Browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
        ctx = browser.contexts[0]
        page = ctx.new_page() if not ctx.pages else ctx.pages[0]
        try:
            page.emulate_media(color_scheme="dark")
            print("Navigating to chatgpt.com...")
            page.goto("https://chatgpt.com/", wait_until="domcontentloaded", timeout=45000)
            try:
                page.wait_for_load_state("networkidle", timeout=20000)
            except Exception:
                pass
            time.sleep(3)

            # Find the chat input. ChatGPT has used <textarea id="prompt-textarea">
            # in the past and a contenteditable div more recently.
            print("Finding chat input...")
            input_selectors = [
                '#prompt-textarea',
                'textarea[data-id]',
                'div[contenteditable="true"][id*="prompt"]',
                'div[contenteditable="true"]',
                'textarea',
            ]
            inp = None
            for sel in input_selectors:
                loc = page.locator(sel)
                try:
                    if loc.count() > 0 and loc.first.is_visible():
                        inp = loc.first
                        print(f'  using selector "{sel}"')
                        break
                except Exception:
                    continue
            if inp is None:
                print("ERROR: could not find ChatGPT input box.", file=sys.stderr)
                return screenshot_path

            inp.click()
            page.keyboard.type(query, delay=8)
            time.sleep(0.5)
            page.keyboard.press("Enter")
            print("Submitted. Waiting for response to finish streaming...")

            # Wait for the stop-streaming button to disappear (response done).
            stop_selectors = [
                'button[data-testid="stop-button"]',
                'button[aria-label*="Stop"]',
                'button[aria-label*="stop"]',
            ]
            t0 = time.time()
            while time.time() - t0 < 90:
                time.sleep(1)
                streaming = False
                for sel in stop_selectors:
                    try:
                        if page.locator(sel).count() > 0 and page.locator(sel).first.is_visible():
                            streaming = True
                            break
                    except Exception:
                        continue
                if not streaming and time.time() - t0 > 5:
                    break
            time.sleep(3)

            print("Taking initial ChatGPT screenshot...")
            initial_chatgpt = screenshot_path.with_name("chatgpt_initial.png")
            page.screenshot(path=str(initial_chatgpt), full_page=True)

            # Click the first product card in the response so its detail panel
            # opens, matching the Google flow.
            print("Looking for first product card in ChatGPT response...")
            clicked = _click_first_chatgpt_product(page, sku)
            if clicked:
                print("Clicked. Waiting for product detail...")
                time.sleep(5)

                # ChatGPT's side panel loads content lazily in chunks as
                # you scroll. Single scroll-to-bottom only triggers ONE
                # chunk; the "What people are saying" section and full
                # "What to know" require multiple rounds of:
                #   scroll-incremental → wait → expand → repeat
                # until expansion finds nothing new to reveal.
                scroll_js = """
                    () => new Promise((res) => {
                      const scrollables = [];
                      document.querySelectorAll('*').forEach(el => {
                        const s = getComputedStyle(el);
                        const oy = s.overflowY;
                        if ((oy === 'auto' || oy === 'scroll') &&
                            el.scrollHeight > el.clientHeight + 5) {
                          scrollables.push(el);
                        }
                      });
                      let i = 0;
                      const next = () => {
                        if (i >= scrollables.length) { res(); return; }
                        const el = scrollables[i];
                        let pos = 0;
                        const step = 400;
                        const tick = () => {
                          el.scrollTop = pos;
                          pos += step;
                          if (pos <= el.scrollHeight + step) {
                            setTimeout(tick, 250);
                          } else {
                            el.scrollTop = el.scrollHeight;
                            setTimeout(() => {
                              el.scrollTop = 0;
                              i++;
                              setTimeout(next, 200);
                            }, 1500);
                          }
                        };
                        tick();
                      };
                      next();
                    })
                """
                expand_js = """
                    () => {
                      let total = 0;
                      const force = (el) => {
                        el.style.setProperty('max-height', 'none', 'important');
                        el.style.setProperty('height', 'auto', 'important');
                        el.style.setProperty('overflow-y', 'visible', 'important');
                        el.style.setProperty('overflow', 'visible', 'important');
                      };
                      for (let pass = 0; pass < 4; pass++) {
                        let changed = 0;
                        document.querySelectorAll('*').forEach(el => {
                          const s = getComputedStyle(el);
                          const oy = s.overflowY;
                          if (oy !== 'auto' && oy !== 'scroll' && oy !== 'hidden') return;
                          const hidden = el.scrollHeight - el.clientHeight;
                          if (hidden < 50) return;
                          force(el);
                          if (pass === 0) total += hidden;
                          changed++;
                        });
                        if (changed === 0) break;
                      }
                      return total;
                    }
                """
                total_expanded = 0
                for round_n in range(4):
                    print(f"  round {round_n + 1}: scrolling...")
                    page.evaluate(scroll_js)
                    time.sleep(2)
                    expanded = page.evaluate(expand_js)
                    total_expanded += expanded
                    print(f"    expanded {expanded}px this round")
                    if expanded < 100:
                        break
                    time.sleep(1)
                print(f"  total expanded: {total_expanded}px")
                time.sleep(2)

            print("Taking screenshot...")
            page.screenshot(path=str(screenshot_path), full_page=True)
        finally:
            browser.close()

    return screenshot_path


def _click_first_chatgpt_product(page: Page, sku: str) -> bool:
    """Find and click the first product card in a ChatGPT response.
    Returns True if clicked, False otherwise (and dumps candidate elements
    to `output/<sku>/chatgpt_candidates.txt` for debugging)."""
    # Try several known patterns. ChatGPT changes selectors frequently and
    # serves multiple shopping-card variants for different products/queries.
    selectors = [
        # Single-card variant: div[role=button] with cursor-pointer
        'section[data-testid^="conversation-turn-"] div[role="button"].cursor-pointer',
        'section[data-testid^="conversation-turn-"] div[role="button"]',
        # Carousel/widget variant — click the first shopping-product card
        'div[data-testid^="shopping-product-metadata-"]',
        'div[data-testid="products-widget"]',
        # Generic fallbacks
        '[data-testid*="product-card"]',
        '[data-testid*="product"][role="button"]',
        'div[role="link"][aria-label*="product" i]',
        'main a[target="_blank"][href*="lululemon"]',
        'main a[target="_blank"][href*="amazon"]',
        'main a[target="_blank"][href*="target.com"]',
        'main a:has(img)',
    ]
    for sel in selectors:
        try:
            loc = page.locator(sel)
            count = loc.count()
        except Exception:
            continue
        if count == 0:
            continue
        for i in range(min(count, 5)):
            link = loc.nth(i)
            try:
                if not link.is_visible():
                    continue
                box = link.bounding_box()
                if not box or box["width"] < 40 or box["height"] < 40:
                    continue
                text = (link.text_content() or "").strip()[:60]
                print(f'  selector "{sel}" -> "{text}" at ({box["x"]:.0f}, {box["y"]:.0f})')
                try:
                    link.scroll_into_view_if_needed(timeout=2000)
                except Exception:
                    pass
                # Use force=True — ChatGPT's product card animates/scrolls
                # which breaks Playwright's normal actionability check.
                link.click(timeout=5000, force=True)
                print(f"  click landed via {sel}")
                return True
            except Exception:
                continue

    # Nothing matched — dump candidates so we can tune.
    out_dir = ROOT / "output" / sku
    out_dir.mkdir(parents=True, exist_ok=True)
    candidates_path = out_dir / "chatgpt_candidates.txt"
    info = page.evaluate("""
        () => {
          const items = [];
          const seen = new Set();
          document.querySelectorAll('a, [role="link"], [role="button"], [data-testid]').forEach(el => {
            const r = el.getBoundingClientRect();
            if (r.width < 100 || r.height < 60) return;
            const key = el.tagName + '|' + (el.className || '') + '|' + Math.round(r.x) + ',' + Math.round(r.y);
            if (seen.has(key)) return;
            seen.add(key);
            items.push({
              tag: el.tagName,
              cls: (el.className || '').slice(0, 80),
              testid: el.getAttribute('data-testid') || '',
              href: el.getAttribute('href') || '',
              role: el.getAttribute('role') || '',
              text: (el.textContent || '').trim().slice(0, 80),
              x: Math.round(r.x), y: Math.round(r.y),
              w: Math.round(r.width), h: Math.round(r.height),
            });
          });
          return items.slice(0, 80);
        }
    """)
    with open(candidates_path, "w") as f:
        for it in info:
            f.write(f'{it["tag"]:6s} testid="{it["testid"]}" role="{it["role"]}" '
                    f'({it["x"]:4d},{it["y"]:4d}) {it["w"]:4d}x{it["h"]:3d}  '
                    f'cls="{it["cls"]}"\n')
            f.write(f'        text="{it["text"]}"\n')
            if it["href"]:
                f.write(f'        href={it["href"][:120]}\n')
    print(f"  no product card matched — dumped {len(info)} candidates to {candidates_path}", file=sys.stderr)
    return False


def capture_alexa(product_title: str, sku: str,
                  profile_dir: Path = DEFAULT_PROFILE,
                  port: int = CDP_PORT) -> list[Path]:
    """Capture Shopping with Alexa (formerly Rufus) contextual prompt
    pills for a product. Flow:

      1. Search amazon.com/s?k=<product_title>.
      2. Click the first organic (non-sponsored) result to land on a PDP.
      3. Open the Alexa side panel (#nav-rufus-disco).
      4. Wait for the contextual-pills section to render
         (`.rufus-html-turn-contextual-pills`).
      5. Inject CSS to pin the panel as a full-height left rail.
      6. Take TWO viewport screenshots:
           - alexa_top.png: scrolled to top (panel + product card +
             inline 'Ask a question' pills)
           - alexa_specific.png: scrolled to the 'Looking for specific
             info?' section (panel + the deeper pill set)
         Each is a readable viewport snap, not a 27000px stitched mess.

    Returns a list of saved screenshot paths (may be 0-2 entries).

    NOTE: DOM is still rufus-prefixed (`#nav-rufus-disco`,
    `#nav-flyout-rufus`, `.rufus-html-turn-contextual-pills`, etc.)
    even though Amazon's brand for it is now "Shopping with Alexa".
    Requires sign-in — anonymous sessions don't get the panel/pills.
    """
    out_dir = ROOT / "samples" / sku
    out_dir.mkdir(parents=True, exist_ok=True)
    top_path = out_dir / "alexa_top.png"
    inline_path = out_dir / "alexa_inline.png"
    qa_path = out_dir / "alexa_qa.png"
    saved: list[Path] = []

    print(f"\n[Alexa]")
    print(f"Query:    {product_title!r}")
    print(f"Outputs:  {top_path.name}, {inline_path.name}, {qa_path.name}")

    launch_chrome_if_needed(profile_dir, port)

    with sync_playwright() as p:
        browser: Browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
        ctx = browser.contexts[0]
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        try:
            # 1. Search results page
            search_url = "https://www.amazon.com/s?" + urlencode({"k": product_title})
            print(f"Searching: {search_url}")
            page.goto(search_url, wait_until="domcontentloaded", timeout=45000)
            try:
                page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                pass
            time.sleep(3)

            # 2. Find first organic result
            print("Finding first organic search result...")
            href = page.evaluate("""
                () => {
                  const results = document.querySelectorAll('div[data-component-type="s-search-result"]');
                  for (const r of results) {
                    // Skip sponsored — Amazon marks them in a few ways
                    const sponsored = r.querySelector(
                      'span.puis-sponsored-label-text, span.s-sponsored-info-icon, ' +
                      '[data-component-type="sp-sponsored-result"], ' +
                      'a[aria-label*="Sponsored" i]'
                    ) || /sponsored/i.test(r.getAttribute('data-component-type') || '');
                    if (sponsored) continue;
                    // Prefer a /dp/ link inside the title
                    const a = r.querySelector('h2 a[href*="/dp/"]') ||
                              r.querySelector('a[href*="/dp/"]');
                    if (a && a.href) return a.href;
                  }
                  // Fallback: any /dp/ link on the page
                  const fb = document.querySelector('a[href*="/dp/"]');
                  return fb ? fb.href : null;
                }
            """)
            if not href:
                print("WARN: no product result found on search page.",
                      file=sys.stderr)
                # Dump candidates for debugging
                debug_dir = ROOT / "output" / sku
                debug_dir.mkdir(parents=True, exist_ok=True)
                cand = page.evaluate("""
                    () => Array.from(document.querySelectorAll('a[href*="/dp/"]'))
                      .slice(0, 30)
                      .map(a => ({href: a.href.slice(0, 160), text: (a.textContent || '').trim().slice(0, 80)}))
                """)
                with open(debug_dir / "alexa_search_candidates.txt", "w") as f:
                    for c in cand:
                        f.write(f'{c["href"]}  text="{c["text"]}"\n')
                page.screenshot(path=str(out_dir / "alexa_no_result.png"),
                                full_page=False)
                return saved

            print(f"  first result: {href[:120]}")

            # 3. Navigate to the PDP
            page.goto(href, wait_until="domcontentloaded", timeout=45000)
            try:
                page.wait_for_load_state("networkidle", timeout=20000)
            except Exception:
                pass
            time.sleep(5)
            print(f"  landed at {page.url[:120]}")

            # 4. Make sure the Alexa panel is open. The DOM keeps the
            # panel present at all times — it just toggles visibility +
            # opacity in CSS — so a bounding-rect check is a FALSE
            # POSITIVE. Inspect computed style instead.
            panel_open = page.evaluate("""
                () => {
                  const p = document.querySelector('#nav-flyout-rufus, .rufus-panel-container');
                  if (!p) return false;
                  const cs = getComputedStyle(p);
                  return cs.visibility !== 'hidden' &&
                         parseFloat(cs.opacity || '1') > 0.5;
                }
            """)
            print(f"  panel open? {panel_open}")
            if not panel_open:
                try:
                    page.click('#nav-rufus-disco', timeout=5000)
                    print("  clicked #nav-rufus-disco to open panel")
                    time.sleep(3)
                except Exception as e:
                    print(f"  failed to open panel: {e}", file=sys.stderr)

            # 5. Wait for contextual pills. Amazon streams them in after
            # the panel opens on a PDP. They live in
            # `.rufus-html-turn-contextual-pills` inside the panel.
            print("Waiting for contextual prompt pills...")
            try:
                page.wait_for_selector(
                    '.rufus-html-turn-contextual-pills',
                    state='attached', timeout=20000,
                )
                time.sleep(4)  # let pills finish fading in
                print("  pills attached")
            except Exception:
                print("  pills didn't appear within 20s — screenshotting anyway",
                      file=sys.stderr)

            # 6. Force the Alexa panel into a full-viewport-height left
            # rail. Amazon's default state is a smaller floating popover
            # (~320x540, vertically centered) with no public toggle to
            # the wider undocked layout. We inject position:fixed CSS so
            # the panel pins to the left side at 100vh in every
            # viewport — required for both the top-of-page snap and the
            # scrolled "Looking for specific info?" snap.
            #
            # Critical overrides: visibility/opacity. The panel sits in
            # the DOM with visibility:hidden + opacity:0 even after
            # clicking #nav-rufus-disco — animation toggles those back
            # to visible only briefly. Forcing visibility:visible +
            # opacity:1 keeps it rendered.
            print("Forcing Alexa side-panel layout via CSS injection...")
            page.evaluate("""
                () => {
                  const css = `
                    #nav-flyout-rufus, .rufus-panel-container {
                      position: fixed !important;
                      left: 0 !important;
                      top: 0 !important;
                      height: 100vh !important;
                      width: 320px !important;
                      max-height: 100vh !important;
                      transform: none !important;
                      border-radius: 0 !important;
                      z-index: 999999 !important;
                      display: block !important;
                      visibility: visible !important;
                      opacity: 1 !important;
                    }
                  `;
                  let style = document.getElementById('audit-alexa-css');
                  if (!style) {
                    style = document.createElement('style');
                    style.id = 'audit-alexa-css';
                    document.head.appendChild(style);
                  }
                  style.textContent = css;
                }
            """)
            time.sleep(2)

            # 7a. Top-of-page snap — side panel + PDP product card.
            page.evaluate("window.scrollTo(0, 0)")
            time.sleep(1)
            print(f"Taking top snap -> {top_path.name}")
            page.screenshot(path=str(top_path), full_page=False)
            saved.append(top_path)

            # 7b. Scroll to the inline Alexa pills under the product
            # card. The section is wrapped in
            # #dpx-nice-widget-container, header is <h5>Ask a question</h5>,
            # pills are <button class="small-widget-pill">. Anchor on
            # the container; scroll so its top sits ~200px below the
            # viewport top (room to show the bottom of the product
            # card above + all pills below the header).
            print("Locating inline 'Ask a question' Alexa pills...")
            scrolled = page.evaluate("""
                () => {
                  const c = document.querySelector('#dpx-nice-widget-container') ||
                            document.querySelector('button.small-widget-pill')?.closest('div, section');
                  if (!c) return -1;
                  const r = c.getBoundingClientRect();
                  window.scrollTo(0, window.scrollY + r.top - 200);
                  return Math.round(window.scrollY);
                }
            """)
            if scrolled >= 0:
                print(f"  scrolled to y={scrolled}")
                time.sleep(2)
                print(f"Taking inline snap -> {inline_path.name}")
                page.screenshot(path=str(inline_path), full_page=False)
                saved.append(inline_path)
            else:
                print("  inline Alexa pills not found on this PDP — "
                      "skipping inline snap", file=sys.stderr)

            # 7c. Scroll to the "Looking for specific info?" Q&A pill
            # block (further down the page, near Top Brand / related
            # carousel). Match the smallest element whose direct text
            # starts with the phrase, so we hit the heading itself
            # rather than a huge ancestor container.
            print("Locating 'Looking for specific info?' Q&A section...")
            scrolled = page.evaluate("""
                () => {
                  let best = null;
                  document.querySelectorAll('*').forEach(el => {
                    let direct = '';
                    el.childNodes.forEach(n => {
                      if (n.nodeType === 3) direct += n.textContent;
                    });
                    direct = direct.trim();
                    if (!/looking for specific info/i.test(direct)) return;
                    const r = el.getBoundingClientRect();
                    if (r.width < 50 || r.height < 10) return;
                    if (!best || r.height < best.h) {
                      best = {el, h: r.height};
                    }
                  });
                  if (!best) return -1;
                  const r = best.el.getBoundingClientRect();
                  window.scrollTo(0, window.scrollY + r.top - 200);
                  return Math.round(window.scrollY);
                }
            """)
            if scrolled >= 0:
                print(f"  scrolled to y={scrolled}")
                time.sleep(2)
                print(f"Taking Q&A snap -> {qa_path.name}")
                page.screenshot(path=str(qa_path), full_page=False)
                saved.append(qa_path)
            else:
                print("  'Looking for specific info?' Q&A section not "
                      "found on this PDP — skipping Q&A snap",
                      file=sys.stderr)
        finally:
            browser.close()

    return saved


GOOGLE_PRIMING_PROMPT = (
    "When I ask about a product, respond with a clickable product card "
    "that opens a panel of retailers, prices, and reviews."
)


def capture(product_title: str, sku: str, profile_dir: Path = DEFAULT_PROFILE,
            port: int = CDP_PORT, zoom: float = 0.67) -> Path:
    query = f"I really like the {product_title} can i buy it online"
    priming_url = "https://www.google.com/search?" + urlencode(
        {"q": GOOGLE_PRIMING_PROMPT, "udm": "50"}
    )

    out_dir = ROOT / "samples" / sku
    out_dir.mkdir(parents=True, exist_ok=True)
    initial_path = out_dir / "initial.png"
    screenshot_path = out_dir / "full_page.png"

    print(f"Priming:  {GOOGLE_PRIMING_PROMPT}")
    print(f"Query:    {query}")
    print(f"Output:   {initial_path}, {screenshot_path}")

    launched = launch_chrome_if_needed(profile_dir, port)
    if launched:
        # On the very first run there's no Cookies file yet — wait (polling)
        # for the user to sign in to Google in the Chrome window we just
        # opened. Cookies can live in `Default/` or `Profile <N>/` depending
        # on how Chrome bucketed the session, so accept any of them.
        def _has_cookies() -> bool:
            return any(profile_dir.glob("*/Cookies"))

        if not _has_cookies():
            print(
                "\n*** FIRST RUN: sign in to Google in the new Chrome window. ***\n"
                "    Waiting up to 5 minutes for your sign-in to land...",
                file=sys.stderr,
            )
            deadline = time.time() + 300
            while time.time() < deadline and not _has_cookies():
                time.sleep(2)
            if not _has_cookies():
                print("No Google session detected; continuing anyway.", file=sys.stderr)
            else:
                time.sleep(3)

    with sync_playwright() as p:
        browser: Browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
        ctx = browser.contexts[0]
        # Reuse the existing tab if one exists. new_page() can fail when
        # Chrome is in a sign-in/error state from a prior session.
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        try:
            # Force dark mode via media-query emulation. Google AI Mode
            # respects prefers-color-scheme and serves its dark theme.
            page.emulate_media(color_scheme="dark")

            # Google AI Mode is non-deterministic about which response
            # variant it serves (big clickable `amIOac` card vs smaller
            # `SmjhRb` dialog link). To bias toward the card, we send a
            # priming message FIRST via URL, then follow up with the
            # actual product query via the in-page chat input. The
            # follow-up runs in the same AI Mode session so the priming
            # context persists.
            print("Sending priming message to Google AI Mode...")
            page.goto(priming_url, wait_until="domcontentloaded", timeout=45000)
            try:
                page.wait_for_load_state("networkidle", timeout=45000)
            except Exception:
                print("  (networkidle timed out — continuing anyway)")
            time.sleep(8)  # streaming buffer for priming response

            # Find the in-page chat textbox ("Ask anything") and send
            # the actual product query.
            print(f"Submitting follow-up product query: {query!r}")
            input_selectors = [
                'textarea[placeholder*="Ask anything" i]',
                'textarea[aria-label*="Ask anything" i]',
                'textarea[placeholder*="anything" i]',
                'div[contenteditable="true"][role="textbox"]',
                'div[contenteditable="true"]',
                'textarea',
                '[role="textbox"]',
            ]
            inp = None
            for sel in input_selectors:
                loc = page.locator(sel)
                try:
                    if loc.count() > 0 and loc.first.is_visible():
                        inp = loc.first
                        print(f'  using input selector "{sel}"')
                        break
                except Exception:
                    continue
            if inp is None:
                print("ERROR: could not find Google AI Mode chat input — "
                      "screenshots will show the priming response state.",
                      file=sys.stderr)
            else:
                inp.click()
                page.keyboard.type(query, delay=8)
                time.sleep(0.5)
                page.keyboard.press("Enter")
                print("Submitted. Waiting for follow-up response to render...")
                try:
                    page.wait_for_load_state("networkidle", timeout=45000)
                except Exception:
                    print("  (networkidle timed out — continuing anyway)")
                time.sleep(8)  # streaming buffer

            # First screenshot: AI Mode result as it loads, before click.
            print(f"Taking initial screenshot -> {initial_path.name}...")
            page.screenshot(path=str(initial_path), full_page=True)

            # Extra dwell — gives Google extra time to finish populating
            # retailer cards in the panel state. Skipping this often results
            # in only ~2 offers being visible after the click.
            print("Letting page settle before clicking...")
            time.sleep(10)

            print("Looking for first product entry link...")
            if find_first_product_link(page):
                print("Clicked. Waiting for product panel...")
                time.sleep(10)

                # Target ONLY the product card's internal scroll container,
                # not the outer page or other scrollables. The card lives
                # on the right side of the viewport and has the most hidden
                # content of any right-side scrollable (`iQYbye`).
                scroll_js = """
                    () => new Promise((res) => {
                      // Find the right-side scrollable with the most hidden
                      // content — that's the product card.
                      let panel = null;
                      let maxHidden = 0;
                      document.querySelectorAll('*').forEach(el => {
                        const s = getComputedStyle(el);
                        if (s.overflowY !== 'auto' && s.overflowY !== 'scroll') return;
                        const r = el.getBoundingClientRect();
                        if (r.width < 200 || r.x < window.innerWidth * 0.5) return;
                        const hidden = el.scrollHeight - el.clientHeight;
                        if (hidden < 200) return;
                        if (hidden > maxHidden) {
                          maxHidden = hidden;
                          panel = el;
                        }
                      });
                      if (!panel) { res(); return; }

                      let pos = 0;
                      const step = 300;
                      const tick = () => {
                        panel.scrollTop = pos;
                        pos += step;
                        if (pos <= panel.scrollHeight + step) {
                          setTimeout(tick, 300);
                        } else {
                          panel.scrollTop = panel.scrollHeight;
                          setTimeout(() => {
                            panel.scrollTop = 0;
                            setTimeout(res, 1500);
                          }, 2000);
                        }
                      };
                      tick();
                    })
                """
                expand_js = """
                    () => {
                      let total = 0;
                      const force = (el) => {
                        el.style.setProperty('max-height', 'none', 'important');
                        el.style.setProperty('height', 'auto', 'important');
                        el.style.setProperty('overflow-y', 'visible', 'important');
                        el.style.setProperty('overflow', 'visible', 'important');
                      };
                      for (let pass = 0; pass < 4; pass++) {
                        let changed = 0;
                        document.querySelectorAll('*').forEach(el => {
                          const s = getComputedStyle(el);
                          const oy = s.overflowY;
                          if (oy !== 'auto' && oy !== 'scroll' && oy !== 'hidden') return;
                          // Skip intentionally-collapsed sections (clientHeight=0).
                          // Expanding them produces visible empty space without
                          // revealing any actual content.
                          if (el.clientHeight === 0) return;
                          const hidden = el.scrollHeight - el.clientHeight;
                          if (hidden < 50) return;
                          force(el);
                          if (pass === 0) total += hidden;
                          changed++;
                        });
                        if (changed === 0) break;
                      }
                      return total;
                    }
                """
                total_expanded = 0
                for round_n in range(4):
                    print(f"  round {round_n + 1}: scrolling...")
                    page.evaluate(scroll_js)
                    time.sleep(2)
                    expanded = page.evaluate(expand_js)
                    total_expanded += expanded
                    print(f"    expanded {expanded}px this round")
                    if expanded < 100:
                        break
                    time.sleep(1)
                print(f"  total expanded: {total_expanded}px")
                time.sleep(2)

            else:
                print(
                    "WARN: no product link found to click — screenshot will "
                    "show the before-click state.",
                    file=sys.stderr,
                )
                # Dump candidate links so we can tune selectors next run.
                debug_path = ROOT / "output" / sku / "candidate_links.txt"
                debug_path.parent.mkdir(parents=True, exist_ok=True)
                links_info = page.evaluate("""
                    () => {
                      const out = [];
                      document.querySelectorAll('a[href]').forEach((a, i) => {
                        const r = a.getBoundingClientRect();
                        if (r.width === 0 || r.height === 0) return;
                        const text = (a.textContent || '').trim().slice(0, 80);
                        if (text.length < 4) return;
                        out.push({
                          i, text,
                          href: a.href.slice(0, 120),
                          x: Math.round(r.x), y: Math.round(r.y),
                          w: Math.round(r.width), h: Math.round(r.height),
                          cls: (a.className || '').slice(0, 60),
                        });
                      });
                      return out.slice(0, 60);
                    }
                """)
                with open(debug_path, "w") as f:
                    for L in links_info:
                        f.write(f'{L["i"]:3d}  ({L["x"]:4d},{L["y"]:4d}) {L["w"]:4d}x{L["h"]:3d}  '
                                f'cls="{L["cls"]}"  href={L["href"]}\n')
                        f.write(f'      text="{L["text"]}"\n')
                print(f'  → dumped link candidates to {debug_path}')

            # The right product-detail panel uses an internal scroll
            # container (`div.iQYbye` etc.) — only ~2 of its 5+ retailer
            # offers render visibly at default size. Force the container to
            # expand to its full scrollHeight so every offer is captured.
            print("Expanding panel scroll containers...")
            # Walk every element with content hidden by overflow (auto,
            # scroll, OR hidden) and expand it. Include overflow=hidden
            # parents because the panel wraps `iQYbye` (overflow:auto) in
            # `PG8i1e` (overflow:hidden), which clips the expanded child.
            # Only expand containers with substantial hidden content
            # (>= 50px) so we don't break UI clipping like rounded badges.
            expanded = page.evaluate("""
                () => {
                  let total = 0;
                  const force = (el) => {
                    el.style.setProperty('max-height', 'none', 'important');
                    el.style.setProperty('height', 'auto', 'important');
                    el.style.setProperty('overflow-y', 'visible', 'important');
                    el.style.setProperty('overflow', 'visible', 'important');
                  };
                  // Multiple passes — expanding a child can change parent
                  // scrollHeight, so iterate until stable.
                  for (let pass = 0; pass < 4; pass++) {
                    let changed = 0;
                    document.querySelectorAll('*').forEach(el => {
                      const s = getComputedStyle(el);
                      const oy = s.overflowY;
                      const ox = s.overflowX;
                      if (oy !== 'auto' && oy !== 'scroll' && oy !== 'hidden') return;
                      // Skip intentionally-collapsed sections.
                      if (el.clientHeight === 0) return;
                      const hidden = el.scrollHeight - el.clientHeight;
                      if (hidden < 50) return;
                      force(el);
                      if (pass === 0) total += hidden;
                      changed++;
                    });
                    if (changed === 0) break;
                  }
                  return total;
                }
            """)
            print(f"  expanded {expanded}px of hidden scroll content")
            time.sleep(2)

            if zoom < 1.0:
                print(f"Applying page zoom {zoom}...")
                page.evaluate(f"document.body.style.zoom = '{zoom}'")
                time.sleep(2)

            print("Taking full-page screenshot...")
            page.screenshot(path=str(screenshot_path), full_page=True)
        finally:
            # Don't close the page — it might be the only one and closing
            # the last tab terminates the browser. Just disconnect.
            browser.close()

    return screenshot_path


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
                   choices=["google", "chatgpt", "alexa", "all"],
                   default="all",
                   help="Which engine(s) to capture. Default: all "
                        "(google + chatgpt + alexa).")
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

    if is_url(args.product):
        print(f"Fetching product title from URL: {args.product}")
        try:
            title = extract_product_title(args.product)
        except Exception as e:
            print(f"ERROR: could not fetch title — {e}", file=sys.stderr)
            return 1
        if not title:
            print(f"ERROR: no title found at {args.product}", file=sys.stderr)
            return 1
        print(f"Extracted title: {title!r}")
    else:
        title = args.product

    sku = args.sku or re.sub(r"[^a-z0-9]+", "_", title.lower()).strip("_")[:50]
    print(f"SKU: {sku}")

    google_shot: Path | None = None
    chatgpt_shot: Path | None = None
    alexa_shots: list[Path] = []

    if args.source in ("google", "all"):
        google_shot = capture(
            product_title=title,
            sku=sku,
            profile_dir=args.profile_dir,
            port=args.port,
            zoom=args.zoom,
        )

    if args.source in ("chatgpt", "all"):
        chatgpt_shot = capture_chatgpt(
            product_title=title,
            sku=sku,
            profile_dir=args.profile_dir,
            port=args.port,
        )

    if args.source in ("alexa", "all"):
        alexa_shots = capture_alexa(
            product_title=title,
            sku=sku,
            profile_dir=args.profile_dir,
            port=args.port,
        )

    print("\nDone.")
    if google_shot:
        print(f"  Google initial:    {google_shot.with_name('initial.png')}")
        print(f"  Google post-click: {google_shot}")
    if chatgpt_shot:
        print(f"  ChatGPT initial:   {chatgpt_shot.with_name('chatgpt_initial.png')}")
        print(f"  ChatGPT post-click: {chatgpt_shot}")
    for shot in alexa_shots:
        print(f"  Alexa:             {shot}")

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
