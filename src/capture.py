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


def _trim_dark_bottom(path: Path, dark_threshold: int = 35,
                      sample_x_step: int = 60,
                      keep_margin: int = 12) -> None:
    """Crop solid dark rows off the bottom of a PNG in-place.

    ChatGPT's expanded full-page screenshot is as tall as the page's
    `scrollHeight`. When the right-rail product panel is taller than
    the chat column, the area below the chat input bar is empty —
    rendered as solid dark-mode background. This walks rows upward
    from the bottom and stops at the first one with a non-dark pixel,
    then crops a bit below that for a small visual margin.

    A row is "dark" if every sampled pixel (every `sample_x_step`
    pixels horizontally) has max channel value <= `dark_threshold`.
    """
    from PIL import Image as _PILImage

    im = _PILImage.open(path).convert("RGB")
    w, h = im.size
    px = im.load()
    last_content_y = -1
    x_step = max(1, sample_x_step)
    for y in range(h - 1, -1, -1):
        for x in range(0, w, x_step):
            r, g, b = px[x, y]
            if max(r, g, b) > dark_threshold:
                last_content_y = y
                break
        if last_content_y >= 0:
            break
    if last_content_y < 0 or last_content_y >= h - keep_margin:
        return  # nothing to trim
    new_h = min(h, last_content_y + keep_margin)
    im.crop((0, 0, w, new_h)).save(path)


def _open_background_page(ctx, browser):
    """Open a new tab in the existing Chrome via CDP with
    `background: true` so it doesn't steal focus from whatever the
    user is doing. Falls back to a normal `ctx.new_page()` if the CDP
    handshake fails.

    Returns the Playwright Page object for the new tab.
    """
    try:
        cdp = browser.new_browser_cdp_session()
    except Exception:
        return ctx.new_page()
    try:
        result = cdp.send("Target.createTarget", {
            "url": "about:blank",
            "background": True,
        })
        target_id = result.get("targetId")
        # Wait briefly for Playwright's context to register the new
        # page, then find it. Match by target ID via per-page CDP.
        import time as _t
        for _ in range(30):
            for p in ctx.pages:
                try:
                    p_cdp = ctx.new_cdp_session(p)
                    info = p_cdp.send("Target.getTargetInfo")
                    p_cdp.detach()
                    if (target_id and
                            info.get("targetInfo", {}).get("targetId")
                            == target_id):
                        return p
                except Exception:
                    pass
            _t.sleep(0.1)
        # Last resort: newest page in the context.
        if ctx.pages:
            return ctx.pages[-1]
        return ctx.new_page()
    finally:
        try:
            cdp.detach()
        except Exception:
            pass


def is_url(s: str) -> bool:
    try:
        u = urlparse(s)
        return u.scheme in ("http", "https") and bool(u.netloc)
    except Exception:
        return False


def normalize_url(s: str) -> str | None:
    """Return s as a fully-qualified URL, or None if it doesn't look like
    a URL at all. Accepts already-qualified URLs and bare host paths
    like 'amazon.com/dp/B0000CF7JZ' (prepends https://)."""
    if is_url(s):
        return s
    if re.match(r"^(?:www\.)?[a-z0-9-]+(?:\.[a-z0-9-]+)+/", s, re.IGNORECASE):
        return "https://" + s
    return None


AMAZON_HOST_RE = re.compile(r"^(?:www\.)?amazon\.[a-z.]+$", re.IGNORECASE)


def is_amazon_url(s: str) -> bool:
    """True if s (raw or normalized) points at amazon.com or any
    amazon.<tld> domain."""
    norm = normalize_url(s) or s
    try:
        u = urlparse(norm)
        return bool(u.netloc) and bool(AMAZON_HOST_RE.match(u.netloc))
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


def find_first_product_link(page: Page, product_title: str = "") -> bool:
    """Click the product-viewer-dialog anchor that wraps Google's
    inline product card.

    The clickable anchor signature (confirmed via DOM inspection):
        <a class="SmjhRb amIOac" role="button" tabindex="0"
           id="pvlink..." aria-describedby="pvlink-desc-..."
           href="/search?ibp=oshop&prds=...">

    The `id="pvlink..."` + `aria-describedby="pvlink-desc-..."` pair
    is the most unique signal — every product-card anchor uses it,
    nothing else does. We use that as the primary selector, with
    href-based fallbacks for layout variants.

    If no such anchor exists in the response, the AI Mode response
    didn't render a product card and we bail.
    """
    # The page has multiple product-viewer-link anchors:
    #   1. ONE "big card" anchor that wraps the product tile
    #      (image + title + price + retailer + rating). Clicking
    #      it opens the product viewer dialog.
    #   2. Several "inline text" anchors that wrap just the
    #      product-name phrase elsewhere in the response. These
    #      also have id="pvlink..." but contain NO image child
    #      and clicking them either does nothing useful or
    #      scrolls.
    # The big card is the only one with an <img> descendant —
    # use that as the distinguisher. Pick the topmost matching one.
    selectors = [
        'a[id^="pvlink"][href*="ibp=oshop"]:has(img)',
        'a.amIOac[href*="ibp=oshop"]:has(img)',
        'a.SmjhRb[href*="ibp=oshop"]:has(img)',
    ]
    for sel in selectors:
        loc = page.locator(sel)
        try:
            count = loc.count()
        except Exception:
            continue
        if count == 0:
            continue
        topmost = None
        topmost_y = float("inf")
        for i in range(min(count, 20)):
            link = loc.nth(i)
            try:
                if not link.is_visible():
                    continue
                box = link.bounding_box()
                if box is None:
                    continue
                if box["y"] < topmost_y:
                    topmost_y = box["y"]
                    topmost = (link, box)
            except Exception:
                continue
        if topmost is None:
            continue
        link, box = topmost
        text = (link.text_content() or "").strip()[:80]
        print(f'  selector "{sel}" -> "{text}" at '
              f'({box["x"]:.0f}, {box["y"]:.0f})')
        try:
            link.scroll_into_view_if_needed(timeout=3000)
            link.click(timeout=5000)
            return True
        except Exception as e:
            print(f'  click failed via {sel}: {e}', file=sys.stderr)
            continue

    # If we got here, no product-viewer anchor exists -> Google didn't
    # render a clickable product card in this response.
    viewport = page.viewport_size or {"width": 1440, "height": 900}
    left_threshold = viewport["width"] * 0.6

    # Tokens kept only for the legacy debug dump path below.
    import re as _re
    tokens = [
        t.lower() for t in _re.split(r"[^A-Za-z0-9]+", product_title)
        if len(t) >= 3
    ]
    title_len = len(product_title.strip())
    max_text_len = max(80, int(title_len * 1.6)) if title_len else 120

    candidates = page.evaluate(r"""
        ({tokens, leftThreshold, maxTextLen}) => {
          const out = [];
          const seen = new WeakSet();
          const sel = 'a, button, span, [role="link"], [role="button"]';
          document.querySelectorAll(sel).forEach(el => {
            if (seen.has(el)) return;
            const r = el.getBoundingClientRect();
            if (r.width < 30 || r.height < 8) return;
            if (r.x + r.width / 2 > leftThreshold) return;  // left column only
            const text = (el.textContent || '').trim();
            if (text.length < 3 || text.length > maxTextLen) return;

            // Skip obvious user-query phrasing — these are echoed back in
            // the chat history but aren't clickable product references.
            if (/^i really (want|like) /i.test(text)) return;
            if (/^when i ask about a product/i.test(text)) return;

            // Must look clickable. Google renders inline entity links
            // (dotted-underline references) with cursor:pointer — that's
            // the universal signal regardless of how the underline dots
            // are visually drawn (text-decoration, border-bottom, or a
            // pseudo-element background pattern).
            const cs = getComputedStyle(el);
            const tag = el.tagName.toLowerCase();
            const role = (el.getAttribute('role') || '').toLowerCase();
            const isClickable =
              tag === 'a' || tag === 'button'
              || role === 'link' || role === 'button'
              || cs.cursor === 'pointer';
            if (!isClickable) return;
            // Drop obvious UI chrome (nav tabs, accessibility links,
            // "Sources", "More" menus, etc.).
            const lowerText = text.toLowerCase();
            const cls = ((el.className && el.className.toString) ?
                          el.className.toString() : '').toLowerCase();
            const href = (el.getAttribute('href') || '').toLowerCase();
            if (/c6ak7c|gypp\w*|mtpl7c/.test(cls)) return;
            if (/google\.com\/support|accessibility/.test(href)) return;
            if (/^(accessibility|skip to|ai mode|all|images|videos|news|more|sources|upgrade|share)$/i.test(lowerText)) return;

            // Visual underline check — Google renders the "dotted
            // entity underline" via a ::after pseudo-element with a
            // background-image dot pattern, so plain text-decoration
            // / border-bottom checks miss it. We also check
            // ::after / ::before for a background-image (image: url
            // or repeating gradient) on a sub-1em-height pseudo-box.
            const line = (cs.textDecorationLine || '').toLowerCase();
            const tdStyle = (cs.textDecorationStyle || '').toLowerCase();
            const bbStyle = (cs.borderBottomStyle || '').toLowerCase();
            const bbWidth = parseFloat(cs.borderBottomWidth || '0');
            let visualDotted =
              (line.includes('underline') &&
                 (tdStyle === 'dotted' || tdStyle === 'dashed'))
              || ((bbStyle === 'dotted' || bbStyle === 'dashed') && bbWidth > 0);

            if (!visualDotted) {
              for (const pe of ['::after', '::before']) {
                const ps = getComputedStyle(el, pe);
                const content = ps.content || '';
                if (content === 'none' || content === 'normal') continue;
                const bgImg = ps.backgroundImage || '';
                const tdLineP = (ps.textDecorationLine || '').toLowerCase();
                const tdStyleP = (ps.textDecorationStyle || '').toLowerCase();
                const bbStyleP = (ps.borderBottomStyle || '').toLowerCase();
                const hasDotPattern =
                  /radial-gradient|repeating-linear-gradient/.test(bgImg)
                  || (tdLineP.includes('underline')
                      && (tdStyleP === 'dotted' || tdStyleP === 'dashed'))
                  || bbStyleP === 'dotted' || bbStyleP === 'dashed';
                if (hasDotPattern) { visualDotted = true; break; }
              }
            }

            // Text-token overlap with product title — used as the
            // primary signal when the visual-dotted CSS detection
            // misses (Google renders via pseudo-element).
            let hits = 0;
            for (const t of tokens) {
              if (lowerText.includes(t)) hits++;
            }
            const textMatch = tokens.length > 0 && hits >= 2;
            if (!visualDotted && !textMatch) return;
            seen.add(el);
            out.push({
              x: r.x, y: r.y, w: r.width, h: r.height, text,
              visualDotted, textMatch, hits, totalTokens: tokens.length,
            });
          });
          return out;
        }
    """, {"tokens": tokens, "leftThreshold": left_threshold,
          "maxTextLen": max_text_len})

    if not candidates:
        # Debug dump — list every clickable element in the left column
        # with its text, classes, and inline + computed style fragments
        # so we can see what Google is using for the dotted underline.
        try:
            debug = page.evaluate(r"""
                ({leftThreshold}) => {
                  const out = [];
                  // Dump EVERY clickable element + every text-bearing
                  // span/div to see what styling Google actually uses
                  // for the dotted underline. Include pseudo-elements.
                  document.querySelectorAll('*').forEach(el => {
                    const r = el.getBoundingClientRect();
                    if (r.width < 15 || r.height < 8) return;
                    if (r.x + r.width / 2 > leftThreshold) return;
                    const text = (el.textContent || '').trim();
                    if (text.length < 3 || text.length > 120) return;
                    const cs = getComputedStyle(el);
                    const after = getComputedStyle(el, '::after');
                    const before = getComputedStyle(el, '::before');
                    // Only emit if the element OR its pseudo-elements
                    // hint at any decoration / clickability.
                    const isInteresting =
                      cs.cursor === 'pointer'
                      || cs.textDecorationLine !== 'none'
                      || cs.borderBottomStyle !== 'none'
                      || (cs.backgroundImage && cs.backgroundImage !== 'none')
                      || after.content !== 'none' && after.content !== 'normal'
                      || before.content !== 'none' && before.content !== 'normal';
                    if (!isInteresting) return;
                    out.push({
                      tag: el.tagName,
                      cls: (el.className && el.className.toString ?
                            el.className.toString() : '').slice(0, 100),
                      text: text.slice(0, 80),
                      x: Math.round(r.x), y: Math.round(r.y),
                      w: Math.round(r.width), h: Math.round(r.height),
                      td: cs.textDecorationLine + '/' + cs.textDecorationStyle,
                      bb: cs.borderBottomStyle + ' ' + cs.borderBottomWidth,
                      bg: (cs.backgroundImage || '').slice(0, 100),
                      cursor: cs.cursor,
                      after_content: (after.content || '').slice(0, 30),
                      after_bg: (after.backgroundImage || '').slice(0, 100),
                      after_td: after.textDecorationLine + '/' + after.textDecorationStyle,
                      after_bb: after.borderBottomStyle + ' ' + after.borderBottomWidth,
                      before_bg: (before.backgroundImage || '').slice(0, 100),
                      mask: (cs.maskImage || cs.webkitMaskImage || '').slice(0, 100),
                    });
                  });
                  return out.slice(0, 120);
                }
            """, {"leftThreshold": left_threshold})
            from pathlib import Path as _P
            dbg_path = _P(__file__).resolve().parent.parent / "output" / "google_dotted_debug.txt"
            dbg_path.parent.mkdir(parents=True, exist_ok=True)
            with open(dbg_path, "w") as f:
                for d in debug:
                    f.write(
                        f"{d['tag']} @({d['x']},{d['y']}) {d['w']}x{d['h']} cls={d['cls'][:60]!r} cursor={d['cursor']}\n"
                        f"  text={d['text']!r}\n"
                        f"  td={d['td']}  bb={d['bb']}\n"
                        f"  bg={d['bg']!r}\n"
                        f"  ::after content={d['after_content']!r} bg={d['after_bg']!r}\n"
                        f"  ::after td={d['after_td']}  bb={d['after_bb']}\n"
                        f"  ::before bg={d['before_bg']!r}\n"
                        f"  mask={d['mask']!r}\n\n"
                    )
            print(f"  → dumped clickable-element styles to {dbg_path}")
        except Exception as _e:
            print(f"  (style debug dump failed: {_e})", file=sys.stderr)
        return False

    # Rank: visualDotted wins outright, then highest token-hit count,
    # then topmost, then leftmost.
    candidates.sort(key=lambda d: (
        0 if d["visualDotted"] else 1,
        -d["hits"],
        d["y"],
        d["x"],
    ))
    pick = candidates[0]
    why = "dotted-underline" if pick["visualDotted"] else (
        f"text-match {pick['hits']}/{pick['totalTokens']}"
    )
    print(f'  {why}: "{pick["text"][:60]}" '
          f'at ({pick["x"]:.0f}, {pick["y"]:.0f})')
    page.mouse.click(pick["x"] + pick["w"] / 2,
                     pick["y"] + pick["h"] / 2)
    return True


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
    """Read the device scale factor for the Chrome process bound to
    --remote-debugging-port=<port>. We parse ps output instead of
    connecting via CDP — Playwright's connect_over_cdp leaves Chrome
    in a state that breaks subsequent connections (the "Browser
    context management is not supported" error)."""
    try:
        result = subprocess.run(
            ["ps", "-ax", "-o", "command"],
            capture_output=True, text=True, timeout=5,
        )
    except Exception:
        return None
    for line in result.stdout.splitlines():
        if f"remote-debugging-port={port}" not in line:
            continue
        m = re.search(r"--force-device-scale-factor=([0-9]*\.?[0-9]+)", line)
        if m:
            return float(m.group(1))
        return 1.0
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


def _pick_profile_directory(profile_dir: Path) -> str | None:
    """Return the Chrome profile subdir to use (e.g. 'Profile 2'). When
    the user-data-dir contains multiple profile subdirs and no 'Default',
    Chrome shows a profile picker page — which breaks Playwright's CDP
    attach. Pass --profile-directory=<this> to skip the picker.

    Heuristic: prefer 'Default' if present, else pick the
    most-recently-modified `Profile N` (the one with the freshest
    cookies)."""
    if (profile_dir / "Default").is_dir():
        return "Default"
    candidates = []
    for sub in profile_dir.glob("Profile *"):
        if not sub.is_dir():
            continue
        cookies = sub / "Cookies"
        mtime = cookies.stat().st_mtime if cookies.exists() else sub.stat().st_mtime
        candidates.append((mtime, sub.name))
    if not candidates:
        return None
    candidates.sort(reverse=True)
    return candidates[0][1]


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
    chrome_args = [CHROME_PATH, f"--remote-debugging-port={port}",
                   f"--user-data-dir={profile_dir}",
                   "--no-first-run", "--no-default-browser-check",
                   # Position off-screen + reasonable size so Chrome doesn't
                   # grab focus or overlay the user's current work. Page
                   # still renders normally; screenshots use the renderer
                   # buffer, not screen capture.
                   "--window-position=-3000,-3000",
                   "--window-size=1728,1117",
                   "--force-device-scale-factor=3",
                   "--high-dpi-support=1"]
    # If the user-data-dir has no Default profile (only "Profile N" subdirs,
    # which happens after the setup helpers create distinct profiles),
    # Chrome boots into the profile picker — a chrome:// page Playwright
    # can't drive. Force the freshest profile.
    pd = _pick_profile_directory(profile_dir)
    if pd:
        chrome_args.append(f"--profile-directory={pd}")
        print(f"Launching Chrome with --remote-debugging-port={port}\n"
              f"  profile: {profile_dir}/{pd}")
    else:
        print(f"Launching Chrome with --remote-debugging-port={port}\n"
              f"  profile: {profile_dir} (no profile-directory specified)")
    subprocess.Popen(
        chrome_args,
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
    query = f"I really want to buy the {product_title}"

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
        # Always open a fresh tab — never reuse the user's existing
        # tabs. Use background-target CDP so we don't steal focus.
        page = _open_background_page(ctx, browser)
        # Force dark-scheme rendering on this tab only. Fresh CDP tabs
        # open in light mode even when the user's account/system prefs
        # are dark. Per-page emulation; doesn't affect other tabs.
        try:
            page.emulate_media(color_scheme="dark")
        except Exception as e:
            print(f"  could not set color scheme ({e})", file=sys.stderr)
        # New tabs created via CDP don't inherit the launch --window-size
        # and default to 1280x720, which clips ChatGPT's product card
        # screenshots. Match the Chrome window dimensions at 3x DSF so
        # captures stay at the same resolution we had when we were
        # reusing the launch tab.
        try:
            cdp = page.context.new_cdp_session(page)
            cdp.send("Emulation.setDeviceMetricsOverride", {
                "width": 1728,
                "height": 912,
                "deviceScaleFactor": 3,
                "mobile": False,
            })
        except Exception as e:
            print(f"  could not set viewport ({e}) — captures may be small",
                  file=sys.stderr)
        try:
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

            # Hide the chat composer / "Ask anything" prompt bar so it
            # doesn't appear mid-screenshot, where its position:fixed
            # background + drop shadow visually mask content rendered
            # below it. ChatGPT's product/shopping responses often
            # include extra sections (comparison tables, Explore more
            # rails) that render below the composer — hiding the
            # composer reveals them cleanly.
            try:
                page.evaluate(r"""
                    () => {
                      const ta = document.querySelector('#prompt-textarea');
                      if (!ta) return;
                      // Walk up to the nearest fixed/sticky ancestor and
                      // hide it. That's typically the composer container.
                      let el = ta.parentElement;
                      while (el && el !== document.body) {
                        const cs = getComputedStyle(el);
                        if (cs.position === 'fixed' || cs.position === 'sticky') {
                          el.style.setProperty('display', 'none', 'important');
                          return;
                        }
                        el = el.parentElement;
                      }
                      // Fallback: hide the form wrapping the textarea.
                      const form = ta.closest('form');
                      if (form) form.style.setProperty('display', 'none', 'important');
                    }
                """)
                time.sleep(0.5)
            except Exception as e:
                print(f"  could not hide composer ({e})", file=sys.stderr)

            print("Taking screenshot...")
            # Bump timeout — fully expanded ChatGPT side panel can produce
            # a screenshot tall enough to exceed the 30s default.
            page.screenshot(path=str(screenshot_path), full_page=True,
                            timeout=120000)
            # Trim solid dark rows at the bottom of the screenshot.
            # ChatGPT's side panel often runs taller than the chat
            # column, leaving the lower half of the full-page capture
            # as an empty dark expanse. Crop it off so the saved
            # image stops where actual content ends.
            try:
                _trim_dark_bottom(screenshot_path)
            except Exception as e:
                print(f"  bottom trim skipped ({e})", file=sys.stderr)
        finally:
            # Close our own tab so we don't leave audit pages in the
            # user's working window. Guard against closing the last tab
            # (which would terminate Chrome).
            try:
                if len(ctx.pages) > 1:
                    page.close()
            except Exception:
                pass
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
                  port: int = CDP_PORT,
                  amazon_url: str | None = None) -> list[Path]:
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
    likes_path = out_dir / "alexa_likes.png"
    dislikes_path = out_dir / "alexa_dislikes.png"
    certs_path = out_dir / "alexa_certs.png"
    materials_path = out_dir / "alexa_materials.png"
    alternatives_path = out_dir / "alexa_alternatives.png"
    budget_path = out_dir / "alexa_budget_alt.png"
    saved: list[Path] = []

    print(f"\n[Alexa]")
    print(f"Query:    {product_title!r}")
    print(f"Outputs:  {top_path.name}, {inline_path.name}, {qa_path.name}, "
          f"{likes_path.name}, {dislikes_path.name}, "
          f"{certs_path.name}, {materials_path.name}, "
          f"{alternatives_path.name}, {budget_path.name}")

    launch_chrome_if_needed(profile_dir, port)

    with sync_playwright() as p:
        browser: Browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
        ctx = browser.contexts[0]

        # Open a fresh tab for Alexa. Alexa's panel chat history persists
        # client-side within a tab — a new tab guarantees a clean slate
        # without needing to touch any other tabs the user has open.
        # Background tab — doesn't steal focus from the user.
        page = _open_background_page(ctx, browser)

        # Force dark-scheme rendering on this tab only. Amazon respects
        # prefers-color-scheme and we want the captured PDP to be dark
        # for visual consistency with the rest of the audit. Per-page
        # emulation does not affect the user's other tabs.
        try:
            page.emulate_media(color_scheme="dark")
        except Exception as e:
            print(f"  could not set color scheme ({e})", file=sys.stderr)

        try:
            # Explicitly set the viewport for pill snaps. CDP overrides
            # from previous runs can persist (clearDeviceMetricsOverride
            # has been unreliable), so we just always set the size we
            # want. 1728x912 @ 3x matches the launched window-size
            # minus browser chrome and produces the 5184x2736 captures
            # that previously framed the pills correctly.
            try:
                cdp = page.context.new_cdp_session(page)
                cdp.send("Emulation.setDeviceMetricsOverride", {
                    "width": 1728,
                    "height": 912,
                    "deviceScaleFactor": 3,
                    "mobile": False,
                })
            except Exception:
                pass

            # 1+2. Resolve PDP URL. When the caller provided a direct
            # Amazon URL, skip search entirely. Otherwise search by
            # title and click the first organic result.
            if amazon_url:
                href = amazon_url
                print(f"Using provided Amazon URL: {href[:120]}")
            else:
                search_url = "https://www.amazon.com/s?" + urlencode({"k": product_title})
                print(f"Searching: {search_url}")
                page.goto(search_url, wait_until="domcontentloaded", timeout=45000)
                try:
                    page.wait_for_load_state("networkidle", timeout=15000)
                except Exception:
                    pass
                time.sleep(3)

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
            # Lay the panel out as a vertical flex column so the
            # conversation in the middle becomes the scrollable region
            # (header on top, textarea pinned to bottom, conversation
            # fills + scrolls). This lets us scroll the latest user
            # question to the top of the visible area after each prompt
            # — previously the conversation just grew and the latest
            # response got clipped off the bottom of the panel.
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
                      display: flex !important;
                      flex-direction: column !important;
                      visibility: visible !important;
                      opacity: 1 !important;
                    }
                    .rufus-panel-header-container { flex: 0 0 auto !important; }
                    .rufus-textarea-container { flex: 0 0 auto !important; }
                    #nav-rufus-content,
                    #rufus-container,
                    #rufus-container-main-view,
                    #rufus-conversation-container {
                      flex: 1 1 auto !important;
                      min-height: 0 !important;
                      overflow-y: auto !important;
                      max-height: none !important;
                    }
                    #rufus-conversation-papyrus-container,
                    #rufus-react-renderer {
                      overflow: visible !important;
                      max-height: none !important;
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

            # 7d. Send two follow-up prompts via the panel textarea and
            # screenshot each response. Both prompts reference "this
            # product" — Alexa picks up the PDP context automatically
            # when the panel is open on a product page.
            #
            # We expand the viewport vertically so the entire response
            # fits in one snap. The panel's height is `100vh` (from our
            # CSS injection), so a taller viewport gives the conversation
            # more rendered area, and the latest response renders
            # without the panel's internal scroll clipping it.
            page.evaluate("window.scrollTo(0, 0)")
            time.sleep(1)
            # Expand viewport vertically while preserving the 3x device
            # scale factor. Playwright's set_viewport_size loses DSF (it
            # sends setDeviceMetricsOverride without deviceScaleFactor),
            # so we call CDP directly.
            try:
                cdp = page.context.new_cdp_session(page)
                cdp.send("Emulation.setDeviceMetricsOverride", {
                    "width": 1728,
                    "height": 2400,
                    "deviceScaleFactor": 3,
                    "mobile": False,
                })
                time.sleep(1)
                print("  expanded viewport to 1728x2400 @3x DSF for response snaps")
            except Exception as e:
                print(f"  could not expand viewport ({e}) — continuing at default",
                      file=sys.stderr)

            prompts = [
                ("what do customers like about this product.", likes_path),
                ("what do customers dis-like about this product", dislikes_path),
                ("What certifications, safety standards, or regulatory approvals "
                 "does this product have?", certs_path),
                ("What materials is this product made of and where is it "
                 "manufactured?", materials_path),
                ("Compare this product to its top alternatives.",
                 alternatives_path),
                ("Compare this product with other models from the same brand.",
                 budget_path),
            ]
            for prompt_text, out_path in prompts:
                ok = _send_alexa_prompt(page, prompt_text)
                if not ok:
                    print(f"  Alexa prompt failed: {prompt_text!r}",
                          file=sys.stderr)
                    continue
                print(f"Taking response snap -> {out_path.name}")
                page.screenshot(path=str(out_path), full_page=False)
                saved.append(out_path)
        finally:
            try:
                if len(ctx.pages) > 1:
                    page.close()
            except Exception:
                pass
            browser.close()

    return saved


def _send_alexa_prompt(page: Page, prompt_text: str) -> bool:
    """Type `prompt_text` into the Alexa panel textarea, submit, and
    wait for the streaming response to finish.

    Detection of "done streaming": the loader container
    (`#rufus-conversation-loader-container`) is visible while Alexa is
    composing; once it disappears AND the conversation text hasn't
    grown for 2 polls, we consider the response complete. 30s overall
    timeout.

    Returns True if we successfully submitted + observed a response.
    """
    print(f"  prompting Alexa: {prompt_text!r}")

    # Make sure the textarea is in the DOM and focusable. Our CSS
    # injection earlier forces the panel visible.
    try:
        page.wait_for_selector('#rufus-text-area', state='attached', timeout=5000)
    except Exception:
        print("  textarea #rufus-text-area not found", file=sys.stderr)
        return False

    # Focus + clear + type via JS to bypass any visibility quirks from
    # the CSS-injected layout, then dispatch an input event so React's
    # state matches.
    ok = page.evaluate(f"""
        () => {{
          const t = document.querySelector('#rufus-text-area');
          if (!t) return false;
          t.focus();
          t.value = {prompt_text!r};
          t.dispatchEvent(new Event('input', {{bubbles: true}}));
          t.dispatchEvent(new Event('change', {{bubbles: true}}));
          return true;
        }}
    """)
    if not ok:
        return False
    time.sleep(0.4)

    # Click the submit button. Some builds disable submit unless the
    # textarea has a fresh keystroke event, so press Enter as a fallback.
    submitted = page.evaluate("""
        () => {
          const b = document.querySelector('#rufus-submit-button');
          if (b && !b.classList.contains('rufus-submit-button--disabled')) {
            b.click();
            return 'button';
          }
          return 'fallback';
        }
    """)
    if submitted == 'fallback':
        page.focus('#rufus-text-area')
        page.keyboard.press('Enter')

    # Snapshot the conversation length immediately after submit so we
    # can detect when Alexa's response actually appears (text growth
    # beyond the user's prompt). A momentary stable-but-pre-response
    # state was previously fooling the wait loop into exiting early.
    time.sleep(1)
    baseline = page.evaluate("""
        () => {
          const conv = document.querySelector('#rufus-react-renderer, .rufus-conversation-papyrus-container');
          return conv ? (conv.innerText || '').length : 0;
        }
    """)
    print(f"  baseline text_len after submit: {baseline}")

    # Wait until: (1) text has grown past baseline by at least 80 chars
    # (response started), (2) text length has been stable for 5
    # consecutive 1-second polls (5s of no growth = streaming finished),
    # (3) no loader element is currently visible. Max wait: 75s.
    deadline = time.time() + 75
    saw_loader = False
    last_text_len = baseline
    stable_polls = 0
    response_started = False
    while time.time() < deadline:
        time.sleep(1)
        state = page.evaluate("""
            () => {
              const panel = document.querySelector('#nav-flyout-rufus, .rufus-panel-container');
              let loader_visible = false;
              if (panel) {
                panel.querySelectorAll('*').forEach(el => {
                  const id = (el.id || '').toLowerCase();
                  const cls = ((el.className && el.className.baseVal) || el.className || '').toString().toLowerCase();
                  if (!(id.includes('loader') || cls.includes('loader'))) return;
                  if (el.offsetWidth > 5 && el.offsetHeight > 5) loader_visible = true;
                });
              }
              const conv = document.querySelector('#rufus-react-renderer, .rufus-conversation-papyrus-container');
              const text_len = conv ? (conv.innerText || '').length : 0;
              return {loader_visible, text_len};
            }
        """)
        if state["loader_visible"]:
            saw_loader = True

        if not response_started and state["text_len"] > baseline + 80:
            response_started = True
            print(f"  response started ({state['text_len']} chars)")

        if state["text_len"] != last_text_len:
            stable_polls = 0
            last_text_len = state["text_len"]
        else:
            stable_polls += 1

        # Done: response started, text length stable 5s, loader not showing
        if (response_started
                and stable_polls >= 5
                and not state["loader_visible"]):
            print(f"  response done at {state['text_len']} chars "
                  f"(saw_loader={saw_loader})")
            break
    else:
        print(f"  Alexa response wait timed out at {state['text_len']} chars "
              f"(response_started={response_started})", file=sys.stderr)

    # Brief dwell so any final render polish lands before screenshot.
    time.sleep(1.5)

    # Find the user-question element whose text matches what we just
    # sent, then scroll its containing scrollable ancestor so the
    # question sits at the top of the visible area. That puts the
    # latest Q+A pair at the top of the frame and prior history scrolls
    # off above. Falls back to plain scroll-to-bottom on any scrollable
    # ancestor if we can't find the matching question element.
    scrolled = page.evaluate("""
        (prompt) => {
          const panel = document.querySelector('#nav-flyout-rufus, .rufus-panel-container');
          if (!panel) return {mode: 'no-panel'};
          // Find the most recent element whose direct text matches the
          // prompt. Take the last match in document order.
          const needle = (prompt || '').toLowerCase().trim().slice(0, 40);
          let match = null;
          panel.querySelectorAll('*').forEach(el => {
            let direct = '';
            el.childNodes.forEach(n => { if (n.nodeType === 3) direct += n.textContent; });
            direct = direct.toLowerCase().trim();
            if (direct.includes(needle) && direct.length < 400) {
              const r = el.getBoundingClientRect();
              if (r.width > 30 && r.height > 10) match = el;
            }
          });
          // Find the nearest scrollable ancestor.
          const findScrollableAncestor = (el) => {
            let cur = el ? el.parentElement : null;
            while (cur) {
              const s = getComputedStyle(cur);
              if ((s.overflowY === 'auto' || s.overflowY === 'scroll')
                  && cur.scrollHeight > cur.clientHeight) {
                return cur;
              }
              cur = cur.parentElement;
            }
            return null;
          };
          if (match) {
            const scroller = findScrollableAncestor(match);
            if (scroller) {
              const elRect = match.getBoundingClientRect();
              const scRect = scroller.getBoundingClientRect();
              // Position match a little below the scroller's top.
              const delta = elRect.top - scRect.top - 12;
              scroller.scrollTop += delta;
              return {mode: 'scrolled-to-question', delta: Math.round(delta), scroller_id: scroller.id};
            }
            // No scroll ancestor — scrollIntoView as best effort.
            match.scrollIntoView({block: 'start'});
            return {mode: 'scrollIntoView'};
          }
          // Fallback: scroll every scrollable in the panel to bottom.
          const hits = [];
          panel.querySelectorAll('*').forEach(el => {
            const s = getComputedStyle(el);
            if (s.overflowY !== 'auto' && s.overflowY !== 'scroll') return;
            const hidden = el.scrollHeight - el.clientHeight;
            if (hidden < 30) return;
            el.scrollTop = el.scrollHeight;
            hits.push(el.id || el.tagName);
          });
          return {mode: 'fallback-bottom', containers: hits};
        }
    """, prompt_text)
    print(f"  panel scroll: {scrolled}")
    time.sleep(0.5)
    return True


GOOGLE_PRIMING_PROMPT = (
    "When I ask about a product, respond with a clickable product card "
    "that opens a panel of retailers, prices, and reviews."
)


def capture(product_title: str, sku: str, profile_dir: Path = DEFAULT_PROFILE,
            port: int = CDP_PORT, zoom: float = 0.67) -> Path:
    query = f"I really want to buy the {product_title}"
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
        # Always open a fresh tab — background mode so we don't steal
        # focus from whatever the user is doing.
        page = _open_background_page(ctx, browser)
        # Force dark-scheme rendering on this tab only. Google AI Mode
        # respects prefers-color-scheme; without this the fresh CDP tab
        # opens in light mode even when the user's system / signed-in
        # preference is dark. Per-page scope means other tabs are not
        # affected.
        try:
            page.emulate_media(color_scheme="dark")
        except Exception as e:
            print(f"  could not set color scheme ({e})", file=sys.stderr)
        # New tabs created via CDP don't inherit the launch --window-size
        # and default to 1280x720, which clips the Google AI Mode panel
        # in screenshots. Match the Chrome window dimensions at 3x DSF
        # so captures stay at the same resolution we had when we were
        # reusing the launch tab.
        try:
            cdp = page.context.new_cdp_session(page)
            cdp.send("Emulation.setDeviceMetricsOverride", {
                "width": 1728,
                "height": 912,
                "deviceScaleFactor": 3,
                "mobile": False,
            })
        except Exception as e:
            print(f"  could not set viewport ({e}) — captures may be small",
                  file=sys.stderr)
        try:
            # Google AI Mode is non-deterministic about which response
            # variant it serves: sometimes a clickable product card
            # carousel, sometimes a text-only answer with no card to
            # click. We retry up to MAX_GOOGLE_RETRIES times if no
            # product card is rendered — each retry starts a fresh
            # AI Mode session (re-navigate to the priming URL).
            MAX_GOOGLE_RETRIES = 3
            input_selectors = [
                'textarea[placeholder*="Ask anything" i]',
                'textarea[aria-label*="Ask anything" i]',
                'textarea[placeholder*="anything" i]',
                'div[contenteditable="true"][role="textbox"]',
                'div[contenteditable="true"]',
                'textarea',
                '[role="textbox"]',
            ]
            got_card = False
            for attempt in range(1, MAX_GOOGLE_RETRIES + 1):
                tag = f"attempt {attempt}/{MAX_GOOGLE_RETRIES}"
                print(f"Sending priming message to Google AI Mode ({tag})...")
                page.goto(priming_url, wait_until="domcontentloaded",
                          timeout=45000)
                try:
                    page.wait_for_load_state("networkidle", timeout=45000)
                except Exception:
                    print("  (networkidle timed out — continuing anyway)")
                time.sleep(8)  # streaming buffer for priming response

                # Find the in-page chat textbox ("Ask anything") and
                # send the actual product query.
                print(f"Submitting follow-up product query: {query!r}")
                inp = None
                for sel in input_selectors:
                    loc = page.locator(sel)
                    try:
                        if loc.count() > 0 and loc.first.is_visible():
                            inp = loc.first
                            if attempt == 1:
                                print(f'  using input selector "{sel}"')
                            break
                    except Exception:
                        continue
                if inp is None:
                    print("ERROR: could not find Google AI Mode chat "
                          "input — bailing.", file=sys.stderr)
                    break

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

                # Probe: did Google render a clickable product card?
                # The signature is an <a id="pvlink..."> anchor that
                # wraps a product image.
                try:
                    card_count = page.locator(
                        'a[id^="pvlink"][href*="ibp=oshop"]:has(img)'
                    ).count()
                except Exception:
                    card_count = 0
                if card_count > 0:
                    print(f"  ✓ product card rendered on {tag}")
                    got_card = True
                    break
                if attempt < MAX_GOOGLE_RETRIES:
                    print(f"  ✗ no product card on {tag} — retrying "
                          "with a fresh AI Mode session...")
                    time.sleep(2)
                else:
                    print(f"  ✗ no product card after {attempt} attempts "
                          "— engine returned text-only, proceeding "
                          "without click.")

            # First screenshot: AI Mode result (last attempt's state),
            # before any click.
            print(f"Taking initial screenshot -> {initial_path.name}...")
            page.screenshot(path=str(initial_path), full_page=True)

            # Extra dwell — gives Google extra time to finish populating
            # retailer cards in the panel state. Skipping this often results
            # in only ~2 offers being visible after the click.
            print("Letting page settle before clicking...")
            time.sleep(10)

            print("Looking for first product entry link...")
            if find_first_product_link(page, product_title=product_title):
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

            # Detect if a product-viewer dialog is currently open. If
            # so, the dialog's content already contains the full retailer
            # list and we must NOT run the broad expand/click-outside
            # operations below — they can dismiss the modal.
            dialog_open = page.evaluate(r"""
                () => {
                  const dlg = document.querySelector(
                    'dialog[open], [role="dialog"]:not([aria-hidden="true"])'
                  );
                  if (dlg) {
                    const r = dlg.getBoundingClientRect();
                    return r.width > 100 && r.height > 100;
                  }
                  // Also check for Google's product-viewer-dialog wrapper.
                  return !!document.querySelector(
                    '[jscontroller*="ProductViewerDialog"], '
                    + '[data-attrid*="product_viewer"], '
                    + '#pvdialog, .pvdialog'
                  );
                }
            """)
            if dialog_open:
                print("  product viewer dialog detected — skipping "
                      "broad expand/click-outside steps")

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

            # Skip the broad "click More stores" pass when a dialog is
            # open — those clicks land on buttons anywhere on the page
            # and can hit the modal backdrop / outside the dialog, which
            # closes the product viewer.
            if dialog_open:
                # Don't click anything when the dialog is open — clicks
                # can land outside the modal and dismiss it. Just expand
                # the viewport vertically so the full-height dialog
                # renders more of its content per screenshot.
                print("Skipping 'More stores' clicks — dialog is open.")
                try:
                    cdp = page.context.new_cdp_session(page)
                    cdp.send("Emulation.setDeviceMetricsOverride", {
                        "width": 1728,
                        "height": 2400,
                        "deviceScaleFactor": 3,
                        "mobile": False,
                    })
                    time.sleep(1)
                    print("  expanded viewport to 1728x2400 for dialog snapshot")
                except Exception as e:
                    print(f"  could not expand viewport ({e})",
                          file=sys.stderr)
            else:
                # Click "More stores" / "Show all" / "See more" buttons to
                # reveal hidden retailer rows in the product panel. Google
                # collapses long retailer lists behind these expanders; if
                # we don't click them the screenshot only shows the first
                # few stores. Click up to 5 rounds — each click can render
                # more "More" buttons (e.g. nested expanders).
                print("Clicking 'More stores' / expand buttons...")
                total_clicks = 0
                for round_n in range(5):
                    clicks = page.evaluate(r"""
                        () => {
                          let n = 0;
                          const seen = new WeakSet();
                          const re = /^(more stores|show all|see more|show more|view all|more results|load more|more offers)\b/i;
                          document.querySelectorAll(
                            'button, a, [role="button"], div[role="button"]'
                          ).forEach(el => {
                            if (seen.has(el)) return;
                            const t = (el.textContent || '').trim();
                            if (!re.test(t) || t.length > 40) return;
                            const r = el.getBoundingClientRect();
                            if (r.width < 20 || r.height < 10) return;
                            seen.add(el);
                            try { el.click(); n++; } catch (e) {}
                          });
                          return n;
                        }
                    """)
                    if clicks == 0:
                        break
                    total_clicks += clicks
                    print(f"  round {round_n + 1}: clicked {clicks} expand button(s)")
                    time.sleep(1.5)
                print(f"  total expand clicks: {total_clicks}")
                # Re-run the panel expansion after clicks to absorb any new
                # scrollable content surfaced by the expanders.
                page.evaluate("""
                    () => {
                      document.querySelectorAll('*').forEach(el => {
                        const s = getComputedStyle(el);
                        const oy = s.overflowY;
                        if (oy !== 'auto' && oy !== 'scroll' && oy !== 'hidden') return;
                        if (el.scrollHeight - el.clientHeight < 50) return;
                        el.style.setProperty('max-height', 'none', 'important');
                        el.style.setProperty('height', 'auto', 'important');
                        el.style.setProperty('overflow-y', 'visible', 'important');
                        el.style.setProperty('overflow', 'visible', 'important');
                      });
                    }
                """)
                time.sleep(1)

            if zoom < 1.0:
                print(f"Applying page zoom {zoom}...")
                page.evaluate(f"document.body.style.zoom = '{zoom}'")
                time.sleep(2)

            # Hide Google AI Mode's "Ask anything" composer / prompt bar
            # so it doesn't sit awkwardly in the middle of a full-page
            # screenshot when the right product panel runs longer than
            # the left chat column. Same approach as ChatGPT.
            try:
                page.evaluate(r"""
                    () => {
                      const ta = document.querySelector(
                        'textarea[placeholder*="Ask anything" i], '
                        + 'textarea[aria-label*="ask" i]'
                      );
                      if (!ta) return;
                      let el = ta.parentElement;
                      while (el && el !== document.body) {
                        const cs = getComputedStyle(el);
                        if (cs.position === 'fixed' || cs.position === 'sticky') {
                          el.style.setProperty('display', 'none', 'important');
                          return;
                        }
                        el = el.parentElement;
                      }
                      const form = ta.closest('form');
                      if (form) form.style.setProperty('display', 'none', 'important');
                    }
                """)
                time.sleep(0.5)
            except Exception as e:
                print(f"  could not hide composer ({e})", file=sys.stderr)

            print("Taking full-page screenshot...")
            page.screenshot(path=str(screenshot_path), full_page=True,
                            timeout=120000)
            try:
                _trim_dark_bottom(screenshot_path)
            except Exception as e:
                print(f"  bottom trim skipped ({e})", file=sys.stderr)
        finally:
            try:
                if len(ctx.pages) > 1:
                    page.close()
            except Exception:
                pass
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

        alexa_shots = capture_alexa(
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
