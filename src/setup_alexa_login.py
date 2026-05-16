"""One-time helper: open Amazon's sign-in page in the dedicated Chrome
profile, wait for the user to authenticate, then exit. Future capture
runs (with Chrome off-screen) will inherit the saved session, which is
required for Shopping with Alexa (formerly Rufus) to load contextual
prompt pills on product detail pages.

Usage:
    python src/setup_alexa_login.py
"""
from __future__ import annotations

import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
CHROME_PATH = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
PROFILE = ROOT / ".chrome-profile"
PORT = 9222


def _cdp_ready(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.3):
            urllib.request.urlopen(
                f"http://127.0.0.1:{port}/json/version", timeout=1
            ).read()
        return True
    except Exception:
        return False


def main() -> int:
    print("Stopping any Chrome on the debug port...")
    subprocess.call(["pkill", "-9", "-f", f"remote-debugging-port={PORT}"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(2)
    for p in PROFILE.glob("Singleton*"):
        try:
            p.unlink()
        except Exception:
            pass

    print("Launching Chrome on-screen at amazon.com sign-in...")
    subprocess.Popen(
        [CHROME_PATH,
         f"--remote-debugging-port={PORT}",
         f"--user-data-dir={PROFILE}",
         "--no-first-run", "--no-default-browser-check",
         "--window-position=200,100",
         "--window-size=1500,950",
         "--force-device-scale-factor=2",
         "https://www.amazon.com/ap/signin?openid.return_to=https%3A%2F%2Fwww.amazon.com%2F"
         "&openid.identity=http%3A%2F%2Fspecs.openid.net%2Fauth%2F2.0%2Fidentifier_select"
         "&openid.assoc_handle=usflex"
         "&openid.mode=checkid_setup"
         "&openid.claimed_id=http%3A%2F%2Fspecs.openid.net%2Fauth%2F2.0%2Fidentifier_select"
         "&openid.ns=http%3A%2F%2Fspecs.openid.net%2Fauth%2F2.0"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )

    print("Waiting for Chrome to come up...")
    for _ in range(60):
        if _cdp_ready(PORT):
            break
        time.sleep(0.5)
    else:
        print("ERROR: Chrome didn't expose CDP", file=sys.stderr)
        return 1

    print("\n*** Sign in to Amazon in the Chrome window that just opened. ***")
    print("    Polling for sign-in status... will auto-exit when detected.\n")

    deadline = time.time() + 600  # 10 minutes
    signed_in = False
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{PORT}")
        ctx = browser.contexts[0]
        while time.time() < deadline:
            time.sleep(3)
            page = ctx.pages[0] if ctx.pages else None
            if page is None:
                continue
            try:
                url = page.url
                # Authenticated indicator: the nav greeting becomes
                # "Hello, <name>" (no longer "Hello, sign in"), and we
                # are off any /ap/signin path.
                on_signin = "/ap/" in url or "/signin" in url.lower()
                greeting = None
                try:
                    g = page.locator("#nav-link-accountList-nav-line-1, #nav-link-accountList .nav-line-1").first
                    if g.count() > 0:
                        greeting = (g.text_content() or "").strip()
                except Exception:
                    pass
                signed_indicator = bool(greeting) and "sign in" not in (greeting or "").lower()
                if not on_signin and "amazon.com" in url and signed_indicator:
                    print(f"Detected signed-in session: greeting={greeting!r} url={url[:80]}")
                    signed_in = True
                    break
                print(f"  (still waiting — url: {url[:80]}, greeting={greeting!r})")
            except Exception as e:
                print(f"  (poll error: {e})")

        browser.close()

    if signed_in:
        print("\nLogin saved. You can close the Chrome window if you want.")
        print("Future `capture.py` runs will use this session.")
        return 0
    print("\nTimed out waiting for sign-in.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
