#!/usr/bin/env bash
# End-to-end SKU audit: capture Google + ChatGPT + Alexa + (optional)
# Sparky chat + Walmart 1-star reviews, then paste into a PPTX.
#
# Usage:
#   ./audit.sh "<product 1 title or URL>" \
#              "<product 2 Amazon URL>" \
#              "<sku folder name>" \
#              ["<walmart URL>"] \
#              ["<pptx path>"]
#
# Examples:
#   ./audit.sh "Little People Barbie..." \
#              "https://www.amazon.com/dp/B0CPN91M7R" \
#              mattel_audit
#
#   ./audit.sh "Horizon Chantilly Coffee Creamer" \
#              "https://www.amazon.com/dp/B0FHX7M7GS" \
#              chantilly_audit \
#              "https://www.walmart.com/ip/17584108701" \
#              "/path/to/ClaudeMilk.pptx"
#
# Sparky chat requires:
#   - The Android Studio AVD ("sparky_avd") booted
#   - The Walmart Android app open on the SAME product PDP
#   - You pre-load this before running — script reads what's on screen
#
# Walmart 1-star reviews come from the desktop Chrome at the URL you
# pass as the 4th arg.

set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

if [[ $# -lt 3 ]]; then
  echo "Usage: $0 <product1> <product2-amazon-url> <sku> [walmart-url] [pptx-path]" >&2
  exit 2
fi

P1="$1"
P2="$2"
SKU="$3"
WALMART_URL="${4:-}"
PPTX="${5:-/Users/colesonharvey/Library/CloudStorage/OneDrive-ReFiBuy,Inc/ClaudeMattel.pptx}"

echo "=== SKU audit: $SKU ==="
echo "Product 1 (Google + ChatGPT): $P1"
echo "Product 2 (Alexa):            $P2"
echo "Walmart URL (reviews):        ${WALMART_URL:-<none — skipping Sparky + reviews>}"
echo "PPTX target:                  $PPTX"
echo

echo "--- Step 1/2: capturing screenshots ---"
if [[ -n "$WALMART_URL" ]]; then
  .venv/bin/python src/capture.py "$P1" \
    --alexa-product "$P2" \
    --sparky-url "$WALMART_URL" \
    --sku "$SKU" \
    --source all
else
  .venv/bin/python src/capture.py "$P1" \
    --alexa-product "$P2" \
    --sku "$SKU" \
    --source all
fi

echo
echo "--- Step 2/2: pasting into PPTX ---"
.venv/bin/python src/paste_to_pptx.py --sku "$SKU" --pptx "$PPTX"

echo
echo "=== Done ==="
echo "Screenshots: samples/$SKU/"
echo "PPTX:        $PPTX"
