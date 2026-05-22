"""ChatGPT capture.

Standalone engine — no imports from gemini/alexa/sparky/walmart.

Public entry point: capture_chatgpt(product_title, sku, ...).

Behavior:
  1. Connect to Chrome via CDP (common.launch_chrome_if_needed).
  2. Navigate to chatgpt.com, find the prompt textarea.
  3. Dismiss any Study Mode / NUX modal that intercepts pointer events.
  4. Type the product query, wait for response stream to finish.
  5. Click the first product card in the response.
  6. Expand the side panel via multi-round scroll + overflow:visible
     force.
  7. Capture two screenshots via raw CDP (Playwright's full_page=True
     hangs on heavy ChatGPT pages):
       chatgpt_initial.png — full document before click
       chatgpt.png — full document with side panel open
"""
from __future__ import annotations

import base64
import sys
import time
from pathlib import Path

from playwright.sync_api import Browser, Page, sync_playwright

from common import (
    ROOT,
    CDP_PORT,
    DEFAULT_PROFILE,
    _open_background_page,
    _trim_dark_bottom,
    launch_chrome_if_needed,
)

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
            # Use raw CDP — Playwright's full_page=True hangs/produces
            # partial output on heavy ChatGPT pages.
            try:
                cdp = page.context.new_cdp_session(page)
                doc_h = page.evaluate("document.documentElement.scrollHeight")
                vw = page.evaluate("window.innerWidth")
                import base64
                result = cdp.send("Page.captureScreenshot", {
                    "format": "png",
                    "captureBeyondViewport": True,
                    "clip": {"x": 0, "y": 0, "width": vw,
                             "height": min(doc_h, 12000), "scale": 1},
                })
                initial_chatgpt.write_bytes(base64.b64decode(result["data"]))
                cdp.detach()
            except Exception as _ie:
                print(f"  CDP screenshot failed ({_ie}) — Playwright fallback",
                      file=sys.stderr)
                page.screenshot(path=str(initial_chatgpt), full_page=True,
                                timeout=120000)

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
            # Use raw CDP — Playwright's full_page=True hangs / produces
            # partial output on heavy ChatGPT pages. Compute capture
            # height as the MAX of doc scrollHeight + side-panel bottom
            # so the position:fixed product-detail panel isn't clipped.
            try:
                cdp = page.context.new_cdp_session(page)
                dims = page.evaluate("""
                    () => {
                      const docH = document.documentElement.scrollHeight;
                      const vw = window.innerWidth;
                      // Find the max bottom of any element that has
                      // substantial visible content (excludes giant
                      // empty/dark wrappers).
                      let maxBottom = docH;
                      // Specifically check the product-detail side panel:
                      // ChatGPT puts it inside [data-testid="products-widget"]
                      // or a section to the right of the chat column.
                      const candidates = document.querySelectorAll(
                        '[data-testid*="product"], '
                        + '[data-testid*="conversation-turn"], '
                        + 'aside, section, main'
                      );
                      for (const el of candidates) {
                        const r = el.getBoundingClientRect();
                        if (r.width < 100 || r.height < 50) continue;
                        // Only count if it has meaningful descendants
                        // (img, text length, button)
                        const hasContent = el.querySelector(
                          'img, h1, h2, h3, button, [role="button"]'
                        );
                        if (!hasContent) continue;
                        const bottom = r.bottom + window.scrollY;
                        if (bottom > maxBottom) maxBottom = bottom;
                      }
                      return {
                        vw, docH, maxBottom: Math.round(maxBottom)
                      };
                    }
                """)
                height = min(max(dims["maxBottom"], dims["docH"]) + 40, 16000)
                import base64
                result = cdp.send("Page.captureScreenshot", {
                    "format": "png",
                    "captureBeyondViewport": True,
                    "clip": {"x": 0, "y": 0, "width": dims["vw"],
                             "height": height, "scale": 1},
                })
                screenshot_path.write_bytes(base64.b64decode(result["data"]))
                cdp.detach()
                print(f"  captured {dims['vw']}x{height} "
                      f"(doc={dims['docH']}, panel-bottom={dims['maxBottom']})")
            except Exception as _se:
                print(f"  CDP screenshot failed ({_se}) — Playwright fallback",
                      file=sys.stderr)
                page.screenshot(path=str(screenshot_path), full_page=True,
                                timeout=120000)
            # Trim solid dark rows at the bottom of the screenshot.
            try:
                _trim_dark_bottom(screenshot_path)
            except Exception as e:
                print(f"  bottom trim skipped ({e})", file=sys.stderr)
        finally:
            # Leave the ChatGPT tab open after capture so the user can do
            # follow-up research on the response (look for hallucinations,
            # bad takes, etc). Only disconnect Playwright from Chrome.
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


