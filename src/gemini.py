"""Google AI Mode (Gemini) capture.

Standalone engine — no imports from gpt/alexa/sparky/walmart.

Public entry point: capture(product_title, sku, ...).

Behavior:
  1. Connect to Chrome via CDP (common.launch_chrome_if_needed).
  2. Open a fresh background tab.
  3. Send a priming message via google.com/search?udm=50 to bias
     Gemini toward clickable product cards.
  4. Submit the product query in the chat (up to 3 retries if no
     card appears within a 35s poll).
  5. Click the topmost product card (find_first_product_link).
  6. Scroll/expand the right-side panel for lazy-loaded retailers.
  7. Capture two screenshots via raw CDP:
       initial.png — full document before click
       full_page.png — full document after click + panel expand
     The post-click capture temporarily drops DSF to 1 so panels
     with 5000-8000 CSS px of internal-scroll content fit within
     Chrome's screenshot buffer.
"""
from __future__ import annotations

import base64
import os
import sys
import time
from pathlib import Path
from urllib.parse import urlencode

from playwright.sync_api import Browser, Page, sync_playwright

from common import (
    ROOT,
    CDP_PORT,
    DEFAULT_PROFILE,
    _open_background_page,
    is_url,
    extract_product_title,
    launch_chrome_if_needed,
)


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
            # Chrome redirects google.com/search?udm=50 to
            # chrome://contextual-tasks/, which makes page.goto throw
            # ERR_ABORTED even though the page loads. Drive via JS.
            try:
                page.evaluate(f"window.location.href = {priming_url!r}")
            except Exception:
                try:
                    page.goto(priming_url, wait_until="domcontentloaded", timeout=45000)
                except Exception as _e:
                    if "ERR_ABORTED" not in str(_e) and "net::" not in str(_e):
                        raise
            t0 = time.time()
            while time.time() - t0 < 30:
                try:
                    cur = page.url or ""
                    if (("udm=50" in cur and "google.com/search" in cur)
                            or cur.startswith("chrome://contextual-tasks/")):
                        break
                except Exception:
                    pass
                time.sleep(0.5)
            # Don't wait for networkidle — AI Mode keeps a streaming
            # connection open and never reaches idle, so we always hit
            # the full timeout. 2s pause for the priming response to
            # render is enough.
            time.sleep(2)

            # Find the in-page chat textbox ("Ask anything"). The product
            # query is submitted inside the retry loop below.
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
                # Submit the product query. Probe for a product card.
                # If no card after streaming completes, re-submit the
                # SAME query in the SAME chat (no re-navigation, no
                # priming resend) up to MAX_INCHAT_RETRIES times.
                # This is the "ask again in chat" reprompt the user asked
                # for — it usually nudges Gemini into rendering a card on
                # the second/third attempt.
                MAX_INCHAT_RETRIES = 3
                _card_probe = """() => {
                    return document.querySelectorAll(
                        'a.amIOac[href*="ibp=oshop"], '
                        + 'a.SmjhRb[href*="ibp=oshop"], '
                        + 'a[id^="pvlink"][href*="ibp=oshop"], '
                        + 'a[href*="ibp=oshop"]'
                    ).length;
                }"""
                for attempt in range(1, MAX_INCHAT_RETRIES + 1):
                    tag = f"attempt {attempt}/{MAX_INCHAT_RETRIES}"
                    if attempt > 1:
                        # Re-find the input — the DOM may have changed
                        # after the previous response rendered.
                        inp = None
                        for sel in input_selectors:
                            loc = page.locator(sel)
                            try:
                                if loc.count() > 0 and loc.first.is_visible():
                                    inp = loc.first
                                    break
                            except Exception:
                                continue
                        if inp is None:
                            print("  could not re-find chat input — giving up",
                                  file=sys.stderr)
                            break
                        print(f"  reprompting in chat ({tag}): {query!r}")
                    else:
                        print(f"Submitting product query ({tag}): {query!r}")
                    try:
                        inp.click(timeout=5000)
                        page.keyboard.type(query, delay=8)
                        time.sleep(0.5)
                        page.keyboard.press("Enter")
                    except Exception as _se:
                        print(f"  submit failed ({_se})", file=sys.stderr)
                        break
                    print("  Polling for product card (up to 35s)...")
                    # Skip networkidle — AI Mode streams keep the
                    # connection open. The poll below catches the card
                    # as soon as it renders.
                    # Poll for the card over 35s. Gemini's response streams
                    # in, and product cards typically render mid-stream (or
                    # right after text completes). Probing once after a
                    # fixed sleep gives a false negative — by the time the
                    # card actually renders, we've already reprompted.
                    # Polling waits for the card to appear instead of
                    # gambling on a single read.
                    card_count = 0
                    poll_deadline = time.time() + 35
                    last_log = 0
                    while time.time() < poll_deadline:
                        try:
                            card_count = page.evaluate(_card_probe)
                        except Exception:
                            card_count = 0
                        if card_count > 0:
                            break
                        # Light progress logging every ~6s
                        if time.time() - last_log > 6:
                            elapsed = int(time.time() - (poll_deadline - 35))
                            print(f"    waiting for card... ({elapsed}s)")
                            last_log = time.time()
                        time.sleep(1.5)
                    if card_count > 0:
                        print(f"  ✓ product card detected ({card_count} link(s)) "
                              f"on {tag}")
                        break
                    if attempt < MAX_INCHAT_RETRIES:
                        print(f"  ✗ no product card on {tag} after 35s poll "
                              f"— reprompting in chat...")
                        time.sleep(2)
                    else:
                        print(f"  ✗ no product card after {attempt} attempts "
                              f"— Gemini returned text-only, proceeding "
                              f"without click.")

            # First screenshot: full document from y=0 via raw CDP
            # (Playwright's full_page=True hangs on AI Mode pages).
            print(f"Taking initial screenshot -> {initial_path.name}...")
            try:
                dims = page.evaluate("""
                    () => ({
                      vw: window.innerWidth,
                      docH: document.documentElement.scrollHeight,
                    })
                """)
                cdp = page.context.new_cdp_session(page)
                import base64
                result = cdp.send("Page.captureScreenshot", {
                    "format": "png", "captureBeyondViewport": True,
                    "clip": {"x": 0, "y": 0,
                             "width": dims["vw"],
                             "height": min(dims["docH"], 16000),
                             "scale": 1},
                })
                initial_path.write_bytes(base64.b64decode(result["data"]))
                cdp.detach()
            except Exception as _ie:
                print(f"  CDP screenshot failed ({_ie}) — Playwright fallback",
                      file=sys.stderr)
                page.screenshot(path=str(initial_path),
                                full_page=False, timeout=60000)

            # Brief settle before clicking. Was 10s — Gemini's
            # response is fully rendered by the time we get here.
            print("Letting page settle before clicking...")
            time.sleep(2)

            print("Looking for first product entry link...")
            if find_first_product_link(page):
                print("Clicked. Waiting for product panel...")
                time.sleep(3)

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
                # Multi-round scroll + expand. After the first expand_js
                # forces overflow:visible, subsequent rounds find no
                # more "hidden" content via the scrollHeight delta —
                # but the panel still has lazy-loaded sections (more
                # retailers, reviews, "What people are saying") that
                # only render once you actually scroll near them. So
                # we keep scrolling even when expand_js reports 0px,
                # and only bail when the document scrollHeight stops
                # growing across 2 consecutive rounds.
                total_expanded = 0
                prev_doc_h = -1
                stable_streak = 0
                MAX_ROUNDS = 8
                for round_n in range(MAX_ROUNDS):
                    print(f"  round {round_n + 1}/{MAX_ROUNDS}: scrolling...")
                    page.evaluate(scroll_js)
                    time.sleep(2)
                    # Also bump main document scroll to bottom in case
                    # lazy loaders watch document scroll instead of the
                    # panel's internal scroll.
                    try:
                        page.evaluate(
                            "window.scrollTo({top: document.body.scrollHeight});"
                        )
                    except Exception:
                        pass
                    time.sleep(1)
                    expanded = page.evaluate(expand_js)
                    total_expanded += expanded
                    try:
                        cur_doc_h = page.evaluate(
                            "document.documentElement.scrollHeight"
                        )
                    except Exception:
                        cur_doc_h = prev_doc_h
                    grew = cur_doc_h - prev_doc_h if prev_doc_h > 0 else 0
                    print(f"    expanded {expanded}px, doc={cur_doc_h}px "
                          f"(grew {grew}px)")
                    if cur_doc_h == prev_doc_h:
                        stable_streak += 1
                    else:
                        stable_streak = 0
                    prev_doc_h = cur_doc_h
                    # Bail once doc height has stopped growing for 2
                    # consecutive rounds AND expand_js found nothing.
                    if stable_streak >= 2 and expanded < 100:
                        print(f"  doc height stable + no expansion — done")
                        break
                    time.sleep(1)
                print(f"  total expanded: {total_expanded}px, "
                      f"final doc height: {prev_doc_h}px")
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

            # Full document capture via raw CDP. Captures from y=0
            # down to the max of doc scrollHeight + right-side product
            # panel bottom so the panel isn't clipped even when it has
            # `position: fixed`/internal-scroll content that doesn't
            # extend the document scrollHeight.
            print("Taking screenshot (full document + panel)...")
            try:
                dims = page.evaluate("""
                    () => {
                      const docH = document.documentElement.scrollHeight;
                      const vw = window.innerWidth;
                      // Find max bottom of any element with image+text
                      // content — covers right-side product detail
                      // panel that uses position:fixed/sticky and
                      // doesn't push the doc scrollHeight up.
                      let maxBottom = docH;
                      const cands = document.querySelectorAll(
                        '[class*="iQYbye"], [class*="PG8i1e"], '
                        + 'aside, section, main, '
                        + '[role="complementary"], [role="dialog"]'
                      );
                      for (const el of cands) {
                        const r = el.getBoundingClientRect();
                        if (r.width < 100 || r.height < 50) continue;
                        const hasContent = el.querySelector(
                          'img, h1, h2, h3, button, [role="button"]'
                        );
                        if (!hasContent) continue;
                        const bottom = r.bottom + window.scrollY;
                        if (bottom > maxBottom) maxBottom = bottom;
                      }
                      return {vw, docH, maxBottom: Math.round(maxBottom)};
                    }
                """)
                # At 3x DSF, Chrome's screenshot buffer maxes at
                # ~12000 actual px = 4000 CSS px. Cap height there.
                # Very tall panels (5000-8000+ CSS) will be clipped at
                # the bottom — the alternative was swapping DSF to 1
                # right before screenshot, which triggered a full
                # re-layout that CDP waited on for paint stability,
                # hanging the script for minutes. Single-pass at DSF=3
                # is reliable.
                target_h = max(dims["maxBottom"], dims["docH"]) + 40
                clip_h = min(target_h, 4000)
                print(f"  capture {dims['vw']}x{clip_h} "
                      f"(doc={dims['docH']}, panel-bottom="
                      f"{dims['maxBottom']}, target was {target_h})")
                cdp = page.context.new_cdp_session(page)
                import base64
                result = cdp.send("Page.captureScreenshot", {
                    "format": "png", "captureBeyondViewport": True,
                    "clip": {
                        "x": 0, "y": 0,
                        "width": dims["vw"],
                        "height": clip_h,
                        "scale": 1,
                    },
                })
                screenshot_path.write_bytes(base64.b64decode(result["data"]))
                cdp.detach()
            except Exception as e:
                print(f"  CDP screenshot failed ({e}) — Playwright fallback",
                      file=sys.stderr)
                page.screenshot(path=str(screenshot_path),
                                full_page=False, timeout=60000)
        finally:
            # Don't close the page — it might be the only one and closing
            # the last tab terminates the browser. Just disconnect.
            browser.close()

    return screenshot_path
