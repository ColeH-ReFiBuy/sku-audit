"""Amazon Shopping with Alexa capture.

Standalone engine — no imports from gemini/gpt/sparky/walmart.

Public entry point: capture_alexa(product_title, sku, ...).

Behavior:
  1. Connect to Chrome via CDP (common.launch_chrome_if_needed).
  2. Search amazon.com/s?k=<product> and click the first organic
     PDP, or navigate directly if a URL was passed.
  3. Open the Alexa side panel (#nav-rufus-disco).
  4. Inject CSS to pin the panel as a fixed-height left rail so the
     pills cluster + chat conversation render in a stable layout.
  5. Take 3 viewport snaps: top, inline pills, deeper "Looking for
     specific info?" Q&A pills.
  6. Send 6 follow-up prompts and snap each response.
  7. Leave the tab open so the user can continue prompting Alexa
     manually after the audit finishes.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import Browser, Page, sync_playwright

from common import (
    ROOT,
    CDP_PORT,
    DEFAULT_PROFILE,
    _open_background_page,
    is_amazon_url,
    is_url,
    launch_chrome_if_needed,
)

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
            # carousel).
            #
            # Amazon's PDP is heavily lazy-loaded: sections below the
            # fold don't render until you actually scroll past them.
            # If we teleport directly to y=5000+ with window.scrollTo,
            # the Q&A section is still empty/skeleton and the
            # screenshot comes out blank-white. Fix: walk down the
            # page incrementally first (waking up every lazy section
            # on the way), then jump to the target heading.
            print("Pre-scrolling page to trigger lazy-load...")
            try:
                doc_h = page.evaluate("document.documentElement.scrollHeight")
            except Exception:
                doc_h = 0
            step = 600
            y = 0
            while y < doc_h:
                try:
                    page.evaluate(f"window.scrollTo(0, {y})")
                except Exception:
                    break
                time.sleep(0.25)
                y += step
            # One more pass at the very bottom to be sure
            try:
                page.evaluate("window.scrollTo(0, document.documentElement.scrollHeight)")
            except Exception:
                pass
            time.sleep(2)

            # Best-effort Q&A pill section snap. Match the heading
            # text, scroll there, snap. Some PDPs render the section,
            # some don't — when the snap is sparse the user can grab
            # it manually from the live Amazon tab (which we leave open
            # after capture).
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
                time.sleep(3)
                print(f"Taking Q&A snap -> {qa_path.name}")
                page.screenshot(path=str(qa_path), full_page=False)
                saved.append(qa_path)
            else:
                print("  'Looking for specific info?' Q&A section not "
                      "found on this PDP — skipping Q&A snap",
                      file=sys.stderr)
                try:
                    if qa_path.exists():
                        qa_path.unlink()
                        print(f"  removed stale {qa_path.name}")
                except Exception:
                    pass

            # Quick-test shortcut: set ALEXA_PILLS_ONLY=1 to stop here
            # after the three pill snaps without sending Alexa prompts.
            if os.environ.get("ALEXA_PILLS_ONLY") == "1":
                print("ALEXA_PILLS_ONLY set — stopping before prompt sends.")
                return saved

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
            # Leave the Amazon PDP / Alexa tab open after capture so the
            # user can continue prompting Alexa for hallucinations.
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


