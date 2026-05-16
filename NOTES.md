# SKU Audit Tool — Operating Notes

For any product, captures how Google AI Mode and ChatGPT present it,
producing 4 raw screenshots ready for manual editing into a slide deck.

## Usage

```bash
.venv/bin/python src/capture.py "<product title or retailer URL>"
.venv/bin/python src/capture.py "Allbirds Wool Runners" --sku allbirds_wool
.venv/bin/python src/capture.py "https://www.target.com/p/sun-bum-..."
```

Defaults: `--source both`, `--zoom 1.0` (no zoom), `--crop` off.

Output → `samples/<sku>/`:
- `initial.png`         Google AI Mode answer (pre-click)
- `full_page.png`       Google after clicking first product card
- `chatgpt_initial.png` ChatGPT response (pre-click)
- `chatgpt.png`         ChatGPT after clicking the inline product card

## Setup (one-time)

```bash
# Sign in to ChatGPT in the dedicated Chrome profile
.venv/bin/python src/setup_chatgpt_login.py
# Sign in via the on-screen Chrome window; it auto-detects + exits.
# Cookies persist in .chrome-profile/ for future runs.
```

Google sign-in happens automatically on first `capture.py` run — you sign
in when prompted in the Chrome window that opens.

## Architecture decisions worth keeping

### Real Chrome via CDP, not Playwright's Chromium
Playwright's bundled Chromium gets bot-detected by both Google and OpenAI.
We launch the real `Google Chrome.app` binary with `--remote-debugging-port=9222`
then `p.chromium.connect_over_cdp(...)`. Real Chrome's fingerprint is normal.

### Dedicated profile at `.chrome-profile/`
Separate from your everyday Chrome so it doesn't conflict. First-run login
to both Google and ChatGPT happens here. Cookies are at
`.chrome-profile/Default/Cookies` OR `.chrome-profile/Profile 1/Cookies`
depending on Chrome's bucketing — the script accepts either.

### Off-screen window position
`--window-position=-3000,-3000` so Chrome doesn't grab focus while running.
macOS does not clamp negative coordinates back on-screen; the renderer
still works because screenshots use the renderer buffer, not screen
capture.

### 3x device scale factor
`--force-device-scale-factor=3` produces ~5184px-wide screenshots at
near-print quality. We tried CSS `body.style.zoom = 0.67` early on; it
rasterizes at the zoomed-down size and looks pixelated. We tried
`zoom = 0.85`; it crosses Google's responsive breakpoint and the panel
gets replaced by a multi-card grid. No zoom is the right answer.

### Multi-round scroll-then-expand for the product panel
Both engines lazy-load retailer offers inside an internal scroll
container (Google uses `div.iQYbye`). Single scroll-to-bottom triggers
only one chunk; needs 2–4 rounds:

```js
// Find the right-side scrollable with the most hidden content
let panel = null, maxHidden = 0;
document.querySelectorAll('*').forEach(el => {
  const s = getComputedStyle(el);
  if (s.overflowY !== 'auto' && s.overflowY !== 'scroll') return;
  const r = el.getBoundingClientRect();
  if (r.width < 200 || r.x < window.innerWidth * 0.5) return;
  const hidden = el.scrollHeight - el.clientHeight;
  if (hidden < 200) return;
  if (hidden > maxHidden) { maxHidden = hidden; panel = el; }
});
// Scroll it incrementally to bottom, wait, back to top.
```

Then expand every overflow=auto/scroll/hidden element with
`scrollHeight - clientHeight >= 50` AND `clientHeight > 0`. The
`clientHeight > 0` filter is critical — without it, intentionally-
collapsed sections (e.g. Google's `Zi8fgf`, `yPqPxc` divs) get
forced open and produce big empty gaps in the panel.

Repeat scroll → expand until a round expands less than 100px.

### Pre-click dwell
`time.sleep(10)` after the initial screenshot, before clicking the
product card. Google needs extra time to populate retailer offers in
the panel state. Without this dwell, only ~2 retailers render.

## Known sad paths

1. **Google `SmjhRb` fallback doesn't always transition the panel.**
   When `a.amIOac[href*="ibp=oshop"]` doesn't match, the script falls
   back to `a.SmjhRb[href*="ibp=oshop"]` — the "Go to product viewer
   dialog" link. For some products (Patagonia Better Sweater, Glycerin
   Max 2, Cetaphil, Birkenstock), clicking this *does not* transition
   the right side from "N sites" sources card to product detail. No
   reliable mitigation; retry sometimes serves `amIOac`.

2. **ChatGPT external-link cards look identical to side-panel cards.**
   Both render as `div[role="button"].cursor-pointer`. The link variant
   navigates to the retailer's site instead of opening the side panel.
   Not detectable at click-time.

3. **ChatGPT anonymous mode returns text-only responses.** Run
   `setup_chatgpt_login.py` to fix.

## Files

- `src/capture.py` — main CLI, URL/title parsing, both Google & ChatGPT
- `src/setup_chatgpt_login.py` — one-time ChatGPT sign-in helper
- `src/audit.py`, `src/detect_crops.py` — legacy crop detector (off by default)
- `src/debug_panel.py` — DOM inspector for diagnosing panel issues
- `.chrome-profile/` — dedicated Chrome user data dir (gitignore this)
- `samples/<sku>/` — captured screenshots per product

## Open work

- ACO optimizer integration. User has a Flask app at
  `http://127.0.0.1:5000/` that produces ACO scoring reports. Plan: add
  a Flask route there that takes a product URL, shells out to
  `capture.py`, returns the 4 screenshots paths + manifest. Need the
  directory path for the optimizer before starting.
