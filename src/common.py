"""Shared helpers used by all 5 engine capture modules.

Holds Chrome management (CDP launch, port detection, DSF enforcement),
URL utilities (normalize, title extraction), and the small image
post-processor used by ChatGPT (_trim_dark_bottom).

Each engine module (gemini, gpt, alexa, sparky, walmart) imports
from here. They do NOT import from each other.
"""
from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

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

    # Clear any "Crashed" exit_type left over from an abnormal shutdown.
    # Otherwise Chrome shows a session-restore infobar on relaunch that
    # intercepts the FIRST navigation with net::ERR_ABORTED.
    import json as _json
    for prefs in profile_dir.glob("*/Preferences"):
        try:
            data = _json.loads(prefs.read_text())
            prof = data.setdefault("profile", {})
            if prof.get("exit_type") != "Normal" or prof.get("exited_cleanly") is not True:
                prof["exit_type"] = "Normal"
                prof["exited_cleanly"] = True
                prefs.write_text(_json.dumps(data))
        except Exception:
            pass

    chrome_args = [CHROME_PATH, f"--remote-debugging-port={port}",
                   f"--user-data-dir={profile_dir}",
                   "--no-first-run", "--no-default-browser-check",
                   # Suppress the session-restore bubble that races with
                   # first nav and causes ERR_ABORTED.
                   "--hide-crash-restore-bubble",
                   "--disable-session-crashed-bubble",
                   # CRITICAL: Chrome started hijacking google.com/search
                   # ?udm=50 URLs and redirecting them to its own internal
                   # chrome://contextual-tasks/ surface, which renders
                   # via Chrome's WebUI bindings instead of normal DOM.
                   # The content is visible on-screen but completely
                   # invisible to document.querySelectorAll — so no
                   # script can find/click product cards. Broad
                   # disable-features list to turn off every flag I know
                   # of that could trigger this redirect.
                   "--disable-features="
                   "ContextualSearch,ContextualPageActions,"
                   "ContextualTasks,ContextualTasksUI,"
                   "GlicSidePanel,GlicHotkey,Glic,GeminiBrowser,"
                   "AIChromeMode,AICompose,AIComposeSearchSuggestions,"
                   "TabContextualization,BrowserAIChat,"
                   "OmniboxAITasks,ChromeAITasks,"
                   "LensSearchPage,LensRegionSearch,"
                   "AISummarization",
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


