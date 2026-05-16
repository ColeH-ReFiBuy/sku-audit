# SKU Audit Tool — Operating Notes

For any product, captures how Google AI Mode, ChatGPT, and Amazon's
Shopping with Alexa present it, producing 6 raw screenshots ready for
manual editing into a slide deck.

## Usage

```bash
.venv/bin/python src/capture.py "<product title or retailer URL>"
.venv/bin/python src/capture.py "Allbirds Wool Runners" --sku allbirds_wool
.venv/bin/python src/capture.py "https://www.target.com/p/sun-bum-..."
.venv/bin/python src/capture.py "Hydro Flask" --source alexa
```

Defaults: `--source all`, `--zoom 1.0` (no zoom), `--crop` off.
`--source` accepts: `google`, `chatgpt`, `alexa`, `all`.

Output → `samples/<sku>/` (all at ~5184px wide, 3× DSF):
- `initial.png`         Google AI Mode answer (pre-click)
- `full_page.png`       Google after clicking first product card
- `chatgpt_initial.png` ChatGPT response (pre-click)
- `chatgpt.png`         ChatGPT after clicking the inline product card
- `alexa_top.png`       Amazon PDP + Alexa side panel + product card (scroll y=0)
- `alexa_inline.png`    Amazon PDP scrolled to inline "Ask a question" Alexa pills under the product card
- `alexa_qa.png`        Amazon PDP scrolled to "Looking for specific info?" Q&A pills near Top Brand block

## Setup (one-time)

```bash
# Sign in to ChatGPT in the dedicated Chrome profile
.venv/bin/python src/setup_chatgpt_login.py

# Sign in to Amazon (required for Shopping with Alexa to render the panel)
.venv/bin/python src/setup_alexa_login.py
```

Both helpers launch Chrome on-screen at the sign-in URL, auto-detect the
authenticated state, and exit. Cookies persist in `.chrome-profile/`.

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

The `setup_*_login.py` helpers launch Chrome on-screen at 2× DSF so the
sign-in UX is a normal size. `launch_chrome_if_needed()` in capture.py
detects mismatched DSF (queries `window.devicePixelRatio` over CDP)
and kills + relaunches at 3× whenever it differs. Cookies persist
across the relaunch via `.chrome-profile/`.

### Google AI Mode priming
We send a priming message FIRST via the udm=50 URL, then submit the
actual product query via the in-page "Ask anything" chat textbox.
Priming text:

> When I ask about a product, respond with a clickable product card
> that opens a panel of retailers, prices, and reviews.

This dramatically increases the rate of `a.amIOac` (big clickable
product card with retailer panel) vs the older `a.SmjhRb` dialog
fallback that never transitions. Verified on Allbirds Wool Runners
and Patagonia Better Sweater Quarter Zip — both landed `amIOac` and
expanded 6000+px of retailer panel content. Replaces the older
double-navigation reroll, which fired off two fresh queries hoping
for variance.

Input selector list for the chat textbox (ranked, fall-through):
`textarea[placeholder*="Ask anything" i]`,
`textarea[aria-label*="Ask anything" i]`,
`textarea[placeholder*="anything" i]`,
`div[contenteditable="true"][role="textbox"]`,
`div[contenteditable="true"]`,
`textarea`,
`[role="textbox"]`.

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

## Shopping with Alexa (amazon)

Amazon rebranded Rufus → "Shopping with Alexa" but the DOM is still
rufus-prefixed: `#nav-rufus-disco`, `#nav-flyout-rufus`,
`.rufus-html-turn-contextual-pills`, etc. Don't get confused.

There are THREE Alexa pill regions per PDP, captured as three viewport
snaps:

1. **Side panel pills** — left rail (always visible in every snap thanks
   to the CSS-injected `position:fixed` layout).
2. **Inline "Ask a question" pills** — in the page body directly below
   the product card. Container `#dpx-nice-widget-container`, header
   `<h5>Ask a question</h5>`, pills `button.small-widget-pill` (last
   one has additional class `ask-pill`).
3. **"Looking for specific info?" Q&A pills** — further down near the
   "Top Brand" block. Container
   `#dpx-rex-nile-inline-default-pills-container`, pills
   `span.dpx-rex-nile-inline-pill-button`. These are Amazon's Q&A
   shortcuts, not Alexa-branded, but they're product-specific prompts
   so worth capturing.

Flow:
1. Submit search to `amazon.com/s?k=<title>`.
2. Click the first organic (non-sponsored) `/dp/...` result → PDP.
3. Click `#nav-rufus-disco` if the panel isn't already visible.
   **Panel "openness" check uses computed `visibility` + `opacity`,
   NOT bounding-rect width** — the panel sits in the DOM at
   320×540 with `visibility:hidden; opacity:0` when closed, so a
   width-based check returns a false positive.
4. Wait for `.rufus-html-turn-contextual-pills` (the side-panel pill
   block) to attach. These lazy-load after the panel opens on a PDP.
5. Inject CSS to pin the panel as a full-viewport-height left rail
   (`position: fixed; left:0; top:0; height:100vh; width:320px`) and
   force `visibility: visible; opacity: 1`. Amazon has no public
   toggle for this layout — the overflow menu only exposes chat
   history, new chat, and FAQs.
6. Snap at scroll y=0 → `alexa_top.png`.
7. Anchor on `#dpx-nice-widget-container` and scroll so it lands ~200px
   below viewport top. Snap → `alexa_inline.png`.
8. Find the smallest element whose **direct text** starts with "Looking
   for specific info" (textContent on ancestors also matches but their
   bboxes are useless for anchoring). Scroll so it lands ~200px below
   viewport top. Snap → `alexa_qa.png`.

All three snaps are `full_page=False` (viewport only). Tried
`full_page=True` once — produced a 3440×27000 stitched mess where the
side panel only rendered at the top because `position:fixed` doesn't
survive Playwright scroll-stitching. Three readable viewport snaps
beat one giant unreadable one. Future work: capture Alexa's response
after clicking a panel pill (likely a 4th snap).

## Known sad paths

1. **Google `SmjhRb` fallback doesn't always transition the panel.**
   When `a.amIOac[href*="ibp=oshop"]` doesn't match, the script falls
   back to `a.SmjhRb[href*="ibp=oshop"]` — the "Go to product viewer
   dialog" link. For some products (Glycerin Max 2, Cetaphil,
   Birkenstock), clicking this *does not* transition the right side
   from "N sites" sources card to product detail. The priming flow
   above mostly eliminates this — Patagonia Better Sweater used to fall
   into this trap and now lands `amIOac` cleanly — but a residual
   minority of products still serve only the dialog variant.

2. **ChatGPT external-link cards look identical to side-panel cards.**
   Both render as `div[role="button"].cursor-pointer`. The link variant
   navigates to the retailer's site instead of opening the side panel.
   Not detectable at click-time.

3. **ChatGPT anonymous mode returns text-only responses.** Run
   `setup_chatgpt_login.py` to fix.

4. **Alexa anonymous mode shows no panel at all.** The
   `#nav-flyout-rufus` element exists in the DOM but pills never load.
   Run `setup_alexa_login.py` to fix.

5. **Inline pill section or "Looking for specific info?" Q&A may be
   absent on some PDPs** (books, simple commodity items, gift cards).
   `capture_alexa` skips the affected snap with a warning rather than
   failing the run. The side panel snap (`alexa_top.png`) always runs.

6. **Amazon occasionally serves `ERR_BLOCKED_BY_RESPONSE` on direct
   `/dp/` navigation** from automated sessions. We mitigate by always
   going through `/s?k=...` and clicking the first result. Hitting a
   /dp/ URL directly is risky; the search-then-click path is robust.

## Files

- `src/capture.py` — main CLI, URL/title parsing, Google + ChatGPT + Alexa
- `src/setup_chatgpt_login.py` — one-time ChatGPT sign-in helper
- `src/setup_alexa_login.py` — one-time Amazon sign-in helper
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
