# Store Product Exporter: Chrome extension

The same exporter as the web app and command-line tool, running entirely inside Chrome. It reads a Shopify or
WooCommerce store's public product data and exports **Shopify CSV, WooCommerce CSV, full-details CSV or JSON**,
with no limit on the number of products. No server and no Python are needed.

## Install (no store account needed)

1. Open `chrome://extensions` in Chrome (also works in Edge, Brave and other Chromium browsers).
2. Turn on **Developer mode** (top right).
3. Click **Load unpacked** and choose this `chrome-extension` folder.
4. Pin the extension, then click its icon. On a store page, the store's address is filled in for you.

The first time you export a store, Chrome asks you to allow the extension to read that store's site. It only asks
for the store you are exporting (and its www / non-www twin), never for all websites.

## How it differs from the web app

- Requests go from your browser straight to the store. Nothing is sent to any other server.
- Everything else is the same: auto-detect Shopify or WooCommerce, collections / categories, page ranges,
  variation prices, deep details (SEO, structured data, weight and dimensions), Stop-and-keep, and the same files.
- Close the tab and the results are gone; export before you leave.

## Checks

`python3 -I tests/test_extension_parity.py` (from the repository root, needs Node 20+) runs this JavaScript
scraper and the Python tool against the same mock stores and checks that every exported file is identical.
