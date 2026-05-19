"""Walmart Ask Sparky capture flow via Android Studio AVD + ADB.

Walmart's Sparky chat is only served to mobile devices that pass Google
Play Integrity. Headless web emulation (mobile UA on desktop Chrome)
gets quietly degraded — Walmart serves a working PDP but flips
`enableSparky: false`. The only reliable automation path is a real
Android emulator with Google Play certification (AVD). BlueStacks 5
also passes Play Integrity but locks down ADB (only screencap/dumpsys
allowed), so we use Google's AVD instead.

Pre-requisites (one-time setup, not handled by this script):
  - Android Studio installed, AVD named `sparky_avd` created with the
    Pixel 6 / Google Play / API 34 ARM64 image.
  - AVD booted (`emulator -avd sparky_avd`) and Walmart app installed
    + signed in.
  - ADB connected to `emulator-5554`.

Flow (mirrors the Alexa 6-prompt structure):
  1. Open product PDP in the Walmart app via deep link.
  2. Dismiss the "Reorder from past purchases" tooltip if present.
  3. Tap the Ask Sparky bottom-nav tab — Sparky auto-loads the current
     product as conversation context ("About this item").
  4. For each of 6 prompts (likes / dislikes / certifications /
     materials / alternatives / budget competitor): type the question
     into the chat input, tap send, wait for the response text to
     stabilize, screenshot.

Output: samples/<sku>/sparky_<name>.png × 6.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parent.parent
ADB_DEVICE = "emulator-5554"

PROMPTS = [
    ("what do customers like about this product", "sparky_likes.png"),
    ("what do customers dis-like about this product", "sparky_dislikes.png"),
    ("What are the most common defects or quality issues reported "
     "with this product?", "sparky_defects.png"),
]
NUM_BAD_REVIEW_SHOTS = 3


def extract_walmart_product_id(url: str) -> str | None:
    """Pull the trailing /ip/.../<id> from a Walmart product URL."""
    m = re.search(r"/ip/[^/?]*/(\d+)", url)
    if m:
        return m.group(1)
    m = re.search(r"/ip/(\d+)", url)
    return m.group(1) if m else None


def adb(*args: str, timeout: int = 30) -> str:
    """Run an adb command targeted at the AVD, return stdout (stderr
    merged in)."""
    cmd = ["adb", "-s", ADB_DEVICE, "exec-out", *args]
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return (res.stdout or "") + (res.stderr or "")


def adb_screencap(path: Path) -> None:
    """Save a device screenshot to `path`."""
    res = subprocess.run(
        ["adb", "-s", ADB_DEVICE, "exec-out", "screencap", "-p"],
        capture_output=True, timeout=30,
    )
    path.write_bytes(res.stdout)


def adb_devices_has_avd() -> bool:
    res = subprocess.run(["adb", "devices"], capture_output=True, text=True, timeout=10)
    return ADB_DEVICE in res.stdout


def uiautomator_dump() -> str:
    """Return the current UI hierarchy XML."""
    return adb("uiautomator", "dump", "--compressed", "/dev/stdout")


def find_node_center(xml: str, *,
                     resource_id: str | None = None,
                     content_desc: str | None = None,
                     text_contains: str | None = None,
                     ) -> tuple[int, int] | None:
    """Return the (cx, cy) center of the first node matching any of the
    given attribute filters."""
    for n in re.finditer(r"<node\s+([^>]*?)/?>", xml):
        attrs = n.group(1)
        if resource_id and resource_id not in attrs:
            continue
        if content_desc:
            m = re.search(r'content-desc="([^"]*)"', attrs)
            if not m or content_desc.lower() not in m.group(1).lower():
                continue
        if text_contains:
            m = re.search(r'text="([^"]*)"', attrs)
            if not m or text_contains.lower() not in m.group(1).lower():
                continue
        bounds = re.search(r'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', attrs)
        if not bounds:
            continue
        x1, y1, x2, y2 = map(int, bounds.groups())
        return ((x1 + x2) // 2, (y1 + y2) // 2)
    return None


def adb_tap(x: int, y: int) -> None:
    subprocess.run(["adb", "-s", ADB_DEVICE, "exec-out", "input", "tap", str(x), str(y)],
                   capture_output=True, timeout=10)


def adb_input_text(text: str) -> None:
    """Send text via `input text`. Escape spaces as %s (ADB convention)
    and quote shell-unsafe chars."""
    escaped = text.replace(" ", "%s").replace("&", "\\&").replace("'", "\\'")
    subprocess.run(
        ["adb", "-s", ADB_DEVICE, "exec-out", "input", "text", escaped],
        capture_output=True, timeout=15,
    )


def adb_keyevent(key: str) -> None:
    subprocess.run(["adb", "-s", ADB_DEVICE, "exec-out", "input", "keyevent", key],
                   capture_output=True, timeout=10)


def adb_swipe(x1: int, y1: int, x2: int, y2: int, dur_ms: int = 400) -> None:
    subprocess.run(
        ["adb", "-s", ADB_DEVICE, "exec-out", "input", "swipe",
         str(x1), str(y1), str(x2), str(y2), str(dur_ms)],
        capture_output=True, timeout=10,
    )


def adb_screencap_bytes() -> bytes:
    res = subprocess.run(
        ["adb", "-s", ADB_DEVICE, "exec-out", "screencap", "-p"],
        capture_output=True, timeout=30,
    )
    return res.stdout


def capture_full_sparky_response(out_path: Path) -> None:
    """Capture the latest Sparky response. Sparky shows a 'Scroll up
    chat' arrow (id=unified_scroll_button, despite the misleading
    desc, it jumps to the latest message) once a response renders
    that's taller than the viewport. If present, tap it so we land
    at the bottom of the response before screenshotting."""
    xml = uiautomator_dump()
    pos = find_node_center(xml, resource_id="unified_scroll_button")
    if pos:
        print(f"    tapping scroll-to-latest arrow at {pos}")
        adb_tap(*pos)
        time.sleep(1.2)
    adb_screencap(out_path)


def dismiss_tooltip_if_present() -> None:
    """Walmart shows a 'Reorder from past purchases' tooltip on first
    PDP visit. Match it specifically — close the tooltip only if the
    description confirms it's the tooltip dismiss, NOT a back/navigation
    button. Matching just text='close' is too broad and can hit the
    PDP's back arrow."""
    xml = uiautomator_dump()
    pos = find_node_center(xml, content_desc="Close, My Items information")
    if not pos:
        # Some Walmart versions use different desc text; still require
        # 'tooltip' or 'past purchases' to be in the description.
        for desc_hint in ("close, my items", "tooltip", "past purchases"):
            pos = find_node_center(xml, content_desc=desc_hint)
            if pos:
                break
    if pos:
        print(f"  dismissing 'reorder' tooltip at {pos}")
        adb_tap(*pos)
        time.sleep(1)


def wait_for_sparky_response(timeout: int = 60) -> int:
    """Poll the Sparky message list until its text-content length has
    been stable for 4 consecutive 1-second polls (response done) AND
    has grown past the baseline by at least 80 chars (response started).
    Returns the final text length."""
    # Baseline length right after submit
    time.sleep(2)
    baseline = _sparky_text_len()
    print(f"  baseline: {baseline} chars")

    deadline = time.time() + timeout
    last_len = baseline
    stable_polls = 0
    response_started = False
    while time.time() < deadline:
        time.sleep(1)
        cur = _sparky_text_len()
        if not response_started and cur > baseline + 80:
            response_started = True
            print(f"  response started ({cur} chars)")
        if cur != last_len:
            stable_polls = 0
            last_len = cur
        else:
            stable_polls += 1
        if response_started and stable_polls >= 4:
            print(f"  response done at {cur} chars")
            return cur
    print(f"  TIMEOUT — final {last_len} chars (started={response_started})",
          file=sys.stderr)
    return last_len


def _sparky_text_len() -> int:
    """Extract the concatenated visible text from the Sparky recycler
    by parsing a uiautomator dump. Faster + more accurate than screen
    OCR for tracking response streaming."""
    xml = uiautomator_dump()
    if "<node" not in xml:
        return 0
    return sum(len(m) for m in re.findall(r'text="([^"]+)"', xml))


def capture_sparky(walmart_url: str | None, sku: str) -> list[Path]:
    """End-to-end Sparky audit for one product. Returns the list of
    screenshot paths saved.

    If `walmart_url` is None, we assume the AVD is already showing the
    target PDP (user pre-loaded it). Skip the deep link.
    """
    if not adb_devices_has_avd():
        print(f"ERROR: AVD '{ADB_DEVICE}' not connected. Boot it first:\n"
              f"  ~/Library/Android/sdk/emulator/emulator -avd sparky_avd",
              file=sys.stderr)
        return []

    out_dir = ROOT / "samples" / sku
    out_dir.mkdir(parents=True, exist_ok=True)
    saved: list[Path] = []

    print(f"\n[Sparky]")
    if walmart_url:
        pid = extract_walmart_product_id(walmart_url)
        if not pid:
            print(f"ERROR: couldn't extract product ID from {walmart_url}",
                  file=sys.stderr)
            return []
        print(f"Walmart URL:  {walmart_url}")
        print(f"Product ID:   {pid}")
        print(f"Output dir:   {out_dir}")
        deep = f"https://www.walmart.com/ip/{pid}"
        print(f"Opening PDP via deep link: {deep}")
        subprocess.run(
            ["adb", "-s", ADB_DEVICE, "exec-out",
             "am", "start", "-a", "android.intent.action.VIEW", "-d", deep],
            capture_output=True, timeout=15,
        )
        time.sleep(6)
    else:
        print("Using currently-open PDP on AVD (no deep link)")
        print(f"Output dir:   {out_dir}")
        # Capture a quick screenshot for user reference
        adb_screencap(out_dir / "sparky_pdp_state.png")

    # 2. Dismiss tooltip (Reorder from past purchases) if visible
    dismiss_tooltip_if_present()

    # 3. Tap Ask Sparky bottom-nav tab. The position is stable across
    # PDPs on the 1080x2400 Pixel 6 emulator: (540, 2263).
    print("Tapping Ask Sparky tab...")
    xml = uiautomator_dump()
    pos = find_node_center(xml, content_desc="Ask Sparky")
    if pos:
        print(f"  found tab at {pos}")
        adb_tap(*pos)
    else:
        print("  Ask Sparky tab not found in UI — falling back to "
              "hardcoded (540, 2263)")
        adb_tap(540, 2263)
    time.sleep(4)

    # 4. Verify we're inside Sparky chat. Earlier this looked for
    # "About this item" but that header scrolls off-screen once the
    # chat has any history, so prefer the input field + "Ask me
    # anything" placeholder as proof of life — those persist regardless
    # of scroll position.
    xml = uiautomator_dump()
    in_sparky = (
        "unified_ui_input_field" in xml
        or "Ask me anything" in xml
        or "Minimize sparky" in xml
    )
    if not in_sparky:
        print("ERROR: Sparky did not engage — input field not visible. "
              "Aborting prompt loop. Make sure you're on a PDP before "
              "running.", file=sys.stderr)
        return saved
    if "About this item" in xml:
        print("  Sparky engaged with product context")
    else:
        print("  Sparky engaged (product header scrolled off — relying "
              "on Sparky's persistent PDP context)")

    # 5. Send each prompt, wait for response, screenshot.
    for prompt_text, fname in PROMPTS:
        print(f"\n  prompt: {prompt_text!r}")
        # Find input field by resource-id (its absolute y shifts when
        # the soft keyboard opens/closes).
        xml = uiautomator_dump()
        inp = find_node_center(xml, resource_id="unified_ui_input_field")
        if not inp:
            print("    ERROR: input field not found — skipping prompt",
                  file=sys.stderr)
            continue
        adb_tap(*inp)
        time.sleep(0.7)
        # Clear any leftover text just in case
        adb_keyevent("KEYCODE_MOVE_END")
        for _ in range(80):
            adb_keyevent("KEYCODE_DEL")
        adb_input_text(prompt_text)
        time.sleep(1)

        # Find send icon and tap (it appears once input is non-empty)
        xml = uiautomator_dump()
        send = find_node_center(xml, resource_id="unified_input_field_send_ic")
        if not send:
            print("    ERROR: send button not found — skipping prompt",
                  file=sys.stderr)
            continue
        adb_tap(*send)
        wait_for_sparky_response(timeout=75)

        out_path = out_dir / fname
        capture_full_sparky_response(out_path)
        print(f"    saved {fname}")
        saved.append(out_path)

    # 6. Bad-review screenshots: not done here. Sparky shots stop at
    # the 3 prompt responses. Walmart 1-star reviews are captured by
    # capture_walmart_bad_reviews() via desktop Chrome, called from
    # the audit wrapper script — the AVD's mobile Walmart app makes
    # filter UI hard to drive reliably, while desktop Walmart has a
    # clean "<N> ratings" link → 1-star filter flow.
    return saved


def capture_walmart_bad_reviews(walmart_url: str, sku: str,
                                cdp_port: int = 9222,
                                n: int = 6) -> list[Path]:
    """Capture the top N 1-star Walmart customer reviews for a product
    via desktop Chrome (CDP on `cdp_port`).

    Flow:
      1. Open a fresh tab on the running Chrome (clears mobile-emulation
         overrides Sparky probes may have left).
      2. Navigate to the Walmart product URL.
      3. Find and click the '<N> ratings' link near the top of the PDP
         (jumps straight to the reviews list).
      4. Click the '1 star' filter row.
      5. For each of the top N rendered cards, walk up 3 parent levels
         from `data-testid=enhanced-review-content` to the full review
         card wrapper, take an element screenshot.

    Returns the saved screenshot paths.

    NOTE: requires Playwright + Chrome on the same `cdp_port` we use
    for Google/ChatGPT/Alexa. Imported inline to keep the module
    importable in pure-ADB contexts.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        print(f"playwright not installed: {e}", file=sys.stderr)
        return []

    out_dir = ROOT / "samples" / sku
    out_dir.mkdir(parents=True, exist_ok=True)
    saved: list[Path] = []

    print(f"\n[Walmart bad reviews]")
    print(f"URL:    {walmart_url}")
    print(f"Output: {out_dir}")

    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{cdp_port}")
        ctx = browser.contexts[0]

        # Open a fresh tab in BACKGROUND mode (via CDP Target.createTarget
        # with background=true) so we don't steal focus from whatever the
        # user is doing. The code below does its own walmart.com homepage
        # warm-up before hitting the PDP, so session warmth is preserved
        # without hijacking the user's current tab.
        try:
            cdp = browser.new_browser_cdp_session()
            result = cdp.send("Target.createTarget", {
                "url": "about:blank",
                "background": True,
            })
            target_id = result.get("targetId")
            cdp.detach()
            page = None
            for _ in range(30):
                for pg in ctx.pages:
                    try:
                        p_cdp = ctx.new_cdp_session(pg)
                        info = p_cdp.send("Target.getTargetInfo")
                        p_cdp.detach()
                        if (target_id and
                                info.get("targetInfo", {}).get("targetId")
                                == target_id):
                            page = pg
                            break
                    except Exception:
                        pass
                if page:
                    break
                time.sleep(0.1)
            if page is None:
                page = ctx.pages[-1] if ctx.pages else ctx.new_page()
        except Exception:
            page = ctx.new_page()

        # Earlier flows (Alexa) leave CDP emulation overrides on the
        # tab. Clear them so Walmart sees a normal desktop fingerprint.
        try:
            cdp = page.context.new_cdp_session(page)
            for cmd in ("Emulation.clearDeviceMetricsOverride",
                        "Emulation.setUserAgentOverride"):
                try:
                    if cmd.endswith("UserAgentOverride"):
                        cdp.send(cmd, {"userAgent": ""})
                    else:
                        cdp.send(cmd)
                except Exception:
                    pass
        except Exception:
            pass

        def _check_captcha() -> bool:
            try:
                sample = page.evaluate(
                    "() => (document.body.innerText || '')"
                    ".slice(0, 600).toLowerCase()"
                )
            except Exception:
                return False
            markers = (
                "robot or human", "press & hold", "press and hold",
                "verify you are human", "are you a human",
                "let us know you're not a robot",
                "activity from your device looks suspicious",
            )
            if any(m in sample for m in markers):
                return True
            try:
                return page.locator(
                    'iframe[src*="captcha"], iframe#px-captcha, '
                    'div[id^="px-captcha"]'
                ).count() > 0
            except Exception:
                return False

        def _wait_for_solve(reason: str, max_wait: int = 180) -> bool:
            print(f"  ! Walmart {reason} — please solve it in the open "
                  f"Chrome window. Waiting up to {max_wait}s...",
                  file=sys.stderr)
            deadline = time.time() + max_wait
            while time.time() < deadline:
                time.sleep(3)
                if not _check_captcha():
                    print("  challenge cleared — continuing",
                          file=sys.stderr)
                    time.sleep(2)
                    return True
            print("  timed out waiting for solve — bailing",
                  file=sys.stderr)
            return False

        try:
            # Warm-up: visit walmart.com homepage first so we land on
            # the PDP with a real session (cookies + referer + history).
            try:
                current = page.url or ""
            except Exception:
                current = ""
            if "walmart.com" not in current:
                print("Warming up: visiting walmart.com homepage...")
                try:
                    page.goto("https://www.walmart.com/",
                              wait_until="domcontentloaded", timeout=30000)
                    time.sleep(2)
                    if _check_captcha():
                        if not _wait_for_solve("challenge on homepage"):
                            return saved
                    try:
                        page.mouse.move(400, 300)
                        page.evaluate("() => window.scrollBy(0, 400)")
                        time.sleep(1)
                    except Exception:
                        pass
                except Exception as e:
                    print(f"  homepage warm-up failed (continuing): {e}",
                          file=sys.stderr)

            print("Navigating to PDP...")
            page.goto(walmart_url, wait_until="domcontentloaded", timeout=45000)
            try:
                page.wait_for_load_state("networkidle", timeout=20000)
            except Exception:
                pass
            time.sleep(3)

            if _check_captcha():
                if not _wait_for_solve("challenge on PDP"):
                    return saved

            # 1. Click '<N> ratings' link near top of PDP
            print("Finding '<N> ratings' link...")
            ratings_links = page.evaluate(r"""
                () => {
                  const cands = [];
                  document.querySelectorAll('a, button').forEach(el => {
                    const t = (el.textContent || '').trim();
                    if (!/^\s*[\d,]+\s+ratings?\b/i.test(t)) return;
                    const r = el.getBoundingClientRect();
                    if (r.width < 10 || r.height < 5) return;
                    cands.push({
                      text: t.slice(0, 40),
                      x: Math.round(r.x), y: Math.round(r.y),
                      w: Math.round(r.width), h: Math.round(r.height),
                    });
                  });
                  return cands;
                }
            """)
            if not ratings_links:
                print("  no ratings link found — bailing", file=sys.stderr)
                return saved
            ratings_links.sort(key=lambda c: c["y"])
            target = ratings_links[0]
            print(f"  clicking {target['text']!r}")
            page.mouse.click(
                target["x"] + target["w"] // 2,
                target["y"] + target["h"] // 2,
            )
            time.sleep(4)

            # 2. Click 1-star filter row
            print("Clicking 1-star filter...")
            try:
                one_star = page.locator(
                    'a[aria-label*="rated 1 star"], a[aria-label*="rated 1 stars"]'
                ).first
                one_star.scroll_into_view_if_needed(timeout=5000)
                time.sleep(1)
                one_star.click(timeout=5000)
            except Exception as e:
                print(f"  1-star filter click failed: {e}", file=sys.stderr)
                return saved
            time.sleep(3)

            # Walmart sometimes drops a challenge after the filter click
            # specifically — re-check before counting cards.
            if _check_captcha():
                if not _wait_for_solve("challenge after 1-star filter"):
                    return saved

            # 2b. Sort by Most Helpful so the top cards are the
            # upvoted/popular ones, not just the newest. Walmart uses a
            # native <select> for this on desktop; if not found, fall
            # through to a clickable dropdown. Failure is non-fatal —
            # we just keep whatever default order Walmart returned.
            print("Sorting by Most Helpful...")
            try:
                sort_result = page.evaluate(r"""
                    () => {
                      const sel = document.querySelector(
                        'select[aria-label*="sort" i], '
                        + 'select[name*="sort" i], '
                        + 'select[data-testid*="sort" i]'
                      );
                      if (sel) {
                        const opt = Array.from(sel.options).find(
                          o => /helpful/i.test(o.textContent || '')
                        );
                        if (opt) {
                          sel.value = opt.value;
                          sel.dispatchEvent(
                            new Event('change', {bubbles: true})
                          );
                          return {mode: 'select', label: opt.textContent.trim()};
                        }
                      }
                      return null;
                    }
                """)
                if sort_result:
                    print(f"  applied via <select>: {sort_result['label']!r}")
                    time.sleep(3)
                else:
                    # Try a custom dropdown: click button labelled "Sort by"
                    # or showing current sort, then click an option with
                    # "helpful" in its text.
                    btns = page.evaluate(r"""
                        () => {
                          const out = [];
                          document.querySelectorAll(
                            'button, [role="button"], [role="combobox"]'
                          ).forEach(el => {
                            const t = (el.textContent || '').trim().toLowerCase();
                            if (!t) return;
                            if (/sort by|most relevant|most recent|most helpful/.test(t)
                                && t.length < 60) {
                              const r = el.getBoundingClientRect();
                              if (r.width >= 20 && r.height >= 10) {
                                out.push({
                                  text: t.slice(0, 60),
                                  x: Math.round(r.x + r.width / 2),
                                  y: Math.round(r.y + r.height / 2),
                                });
                              }
                            }
                          });
                          return out;
                        }
                    """)
                    if btns:
                        b = btns[0]
                        page.mouse.click(b["x"], b["y"])
                        time.sleep(1)
                        opts = page.evaluate(r"""
                            () => {
                              const out = [];
                              document.querySelectorAll(
                                '[role="option"], li, button, a'
                              ).forEach(el => {
                                const t = (el.textContent || '').trim();
                                if (!/helpful/i.test(t) || t.length > 40)
                                  return;
                                const r = el.getBoundingClientRect();
                                if (r.width >= 20 && r.height >= 10) {
                                  out.push({
                                    text: t,
                                    x: Math.round(r.x + r.width / 2),
                                    y: Math.round(r.y + r.height / 2),
                                  });
                                }
                              });
                              return out;
                            }
                        """)
                        if opts:
                            o = opts[0]
                            page.mouse.click(o["x"], o["y"])
                            print(f"  clicked dropdown option: {o['text']!r}")
                            time.sleep(3)
                        else:
                            print("  dropdown opened but no 'helpful' option "
                                  "visible — keeping default order")
                    else:
                        print("  no sort UI found — keeping default order")
            except Exception as e:
                print(f"  sort step failed (continuing): {e}",
                      file=sys.stderr)

            # 3. Screenshot top N review cards
            cards = page.locator('[data-testid="enhanced-review-content"]')
            count = cards.count()
            print(f"  found {count} review cards on filtered list")
            take = min(n, count)
            for i in range(take):
                card = cards.nth(i)
                try:
                    card.scroll_into_view_if_needed(timeout=5000)
                    time.sleep(0.5)
                    # Walk up 3 levels to the full card wrapper
                    # (date + name + stars + title + body + helpful)
                    wrapper = card.evaluate_handle(
                        "(el) => el.parentElement.parentElement.parentElement"
                    )
                    out_path = out_dir / f"walmart_bad_review_{i+1}.png"
                    wrapper.as_element().screenshot(path=str(out_path))
                    print(f"  saved {out_path.name}")
                    saved.append(out_path)
                except Exception as e:
                    print(f"  card {i+1} failed: {e}", file=sys.stderr)
        finally:
            browser.close()

    return saved


def main() -> int:
    p = argparse.ArgumentParser(description="Capture Walmart Ask Sparky responses for a product.")
    p.add_argument("walmart_url", nargs="?", default=None,
                   help="Walmart product URL. Omit to use whatever "
                        "PDP is currently open on the AVD.")
    p.add_argument("--sku", required=True, help="Sample folder name")
    args = p.parse_args()

    if args.walmart_url and not urlparse(args.walmart_url).netloc.endswith("walmart.com"):
        print("ERROR: URL must be a walmart.com product URL", file=sys.stderr)
        return 1

    paths = capture_sparky(args.walmart_url, args.sku)
    print(f"\nDone. Saved {len(paths)} screenshot(s).")
    for p in paths:
        print(f"  {p}")
    return 0 if paths else 1


if __name__ == "__main__":
    sys.exit(main())
