"""One-time helper: open ChatGPT's sign-in page in the dedicated Chrome
profile, wait for the user to authenticate, then exit. Future capture
runs (with Chrome off-screen) will inherit the saved session.

Usage:
    python src/setup_chatgpt_login.py
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
CHROME_PATH = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
PROFILE = ROOT / ".chrome-profile"
PORT = 9222


def main() -> int:
    # Kill any existing Chrome on the debug port so we can relaunch on-screen.
    print("Stopping any Chrome on the debug port...")
    subprocess.call(["pkill", "-9", "-f", f"remote-debugging-port={PORT}"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(2)
    for p in PROFILE.glob("Singleton*"):
        try:
            p.unlink()
        except Exception:
            pass

    print("Launching Chrome on-screen at chatgpt.com/auth/login...")
    subprocess.Popen(
        [CHROME_PATH,
         f"--remote-debugging-port={PORT}",
         f"--user-data-dir={PROFILE}",
         "--no-first-run", "--no-default-browser-check",
         "--window-position=200,100",
         "--window-size=1500,950",
         "--force-device-scale-factor=2",
         "https://chatgpt.com/auth/login"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )

    # Wait for Chrome to come up on the CDP port
    print("Waiting for Chrome to come up...")
    import urllib.request, socket
    for _ in range(60):
        try:
            with socket.create_connection(("127.0.0.1", PORT), timeout=0.3):
                urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/version", timeout=1).read()
                break
        except Exception:
            time.sleep(0.5)
    else:
        print("ERROR: Chrome didn't expose CDP", file=sys.stderr)
        return 1

    print("\n*** Sign in to ChatGPT in the Chrome window that just opened. ***")
    print("    (Use 'Continue with Google' or your usual sign-in.)")
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
                # On an authenticated session the URL leaves /auth/login
                # AND the prompt-textarea is on the page.
                on_auth_page = "/auth/login" in url or "/auth/" in url
                has_prompt = False
                try:
                    has_prompt = page.locator('#prompt-textarea').count() > 0
                except Exception:
                    pass
                if "chatgpt.com" in url and not on_auth_page and has_prompt:
                    print(f"Detected signed-in session at {url}")
                    signed_in = True
                    break
                print(f"  (still waiting — url: {url[:80]}, prompt={has_prompt})")
            except Exception as e:
                print(f"  (poll error: {e})")

        browser.close()

    if signed_in:
        print("\nLogin saved. You can close the Chrome window if you want.")
        print("Future `capture.py` runs will use this session.")
        return 0
    else:
        print("\nTimed out waiting for sign-in. Try again with setup_chatgpt_login.py.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
