# Store Product Exporter

Exports the public product catalog of a **Shopify or WooCommerce** store (auto-detected), with **no limit on the number of products**, as a
full-details CSV, an Excel workbook, a Shopify-format CSV, a WooCommerce-format CSV, a reviews CSV, or complete JSON.

## Mac: double-click launcher (no typing)

Double-click **`launch-mac.command`**. The first run installs what it needs (about a minute, needs internet);
after that it starts in seconds and opens the tool in Chrome. Keep the Terminal window open while you use the
tool and close it to stop.

- The first time, macOS may say the file can't be opened because it's from an unidentified developer. Right-click
  the file, choose **Open**, then **Open** again. After that, double-clicking works normally.
- If macOS offers to install developer tools, click **Install**, wait, then double-click the file again.

## Run the web app

```bash
pip install -r requirements.txt
python app.py            # http://127.0.0.1:5000
```

Production: `gunicorn --workers 1 --threads 8 -b 0.0.0.0:8000 app:app`
(keep one worker: scrape jobs and results are held in memory).

## Run from the command line

```bash
python cli.py noelmartz.com            # products + collections
python cli.py noelmartz.com --deep     # plus SEO, structured data, barcodes, videos
python cli.py noelmartz.com --out exports
python cli.py mystore.com --platform woocommerce      # force a platform (default: auto)
python cli.py mystore.com/product-category/clothing/  # just one category
python cli.py mystore.com --no-variations             # WooCommerce: skip per-variation prices (faster)
```

```bash
python cli.py mystore.com --collections shoes,sale      # several collections / categories
python cli.py mystore.com --search "running shoe"       # keyword search
python cli.py mystore.com --products https://mystore.com/products/a b-handle   # specific products (links, slugs or handles)
python cli.py mystore.com --reviews --link              # collect reviews; add a Product URL column to the Shopify / WooCommerce CSVs
```

Writes `*-full-details.csv`, `*-products.xlsx`, `*-shopify.csv`, `*-woocommerce.csv` and `*-products.json` to the output
folder, plus `*-reviews.csv` when reviews were found.

## Features

- Export all products, with no limit, or only specific collections / categories, specific products (paste links or
  handles) or a keyword search.
- Export to Excel (.xlsx, with a Reviews sheet), full-details CSV, Shopify CSV, WooCommerce CSV, reviews CSV or JSON.
- Optional **Product URL** column on the Shopify and WooCommerce CSVs.
- Click a product title to see its details (description, options, variants, images, collections, SEO, reviews).
- Product reviews: WooCommerce reads the store's public reviews feed. Shopify has no public reviews feed, so only
  reviews the product page publishes as structured data are collected; apps such as Judge.me, Yotpo or Loox usually
  load theirs separately and are not captured.

## What gets collected

| Data | Source | Mode |
| --- | --- | --- |
| Title, handle, vendor, type, tags, description (HTML and plain text), created / published / updated dates | `/products.json` | always |
| All images with position and alt text | `/products.json` | always |
| Every variant: options, SKU, price, compare-at price, sale flag, weight, availability, shipping and tax flags, variant image | `/products.json` | always |
| Collections each product belongs to | `/collections.json` + each collection's feed | on by default |
| SEO title and description, canonical URL, OG image | product page | deep |
| Brand, rating, review count, currency, GTIN | product page JSON-LD | deep |
| Barcodes, videos, 3D media, selling plans | `/products/<handle>.js` | deep |

The raw JSON export contains everything above in its original structure, including the full JSON-LD.

## Chrome extension

The `chrome-extension/` folder is the same tool as a Chrome extension (load it unpacked at `chrome://extensions`); see
`chrome-extension/README.md`.

## WooCommerce stores

Read through WooCommerce's public **Store API** (`/wp-json/wc/store/v1/`, also `/?rest_route=` for plain permalinks).
Collected: name, slug, SKU, type (simple / variable / grouped / external), descriptions, price / sale price, images,
categories (with hierarchy), tags, brands, attributes, stock flags, ratings, and every variation with its own SKU,
price, stock and image. Deep mode also reads weight and dimensions from the product page (best effort).
A WooCommerce scrape can be exported as WooCommerce CSV (keeps real types and attributes) or Shopify CSV (converted).

Limits:
- The Store API must be public. Some stores disable or block it; the app says so (page scraping is not implemented).
- WordPress installed in a subdirectory (`example.com/shop/`) is not supported.
- Exact stock quantities are not public. Shopify CSV supports only 3 options; extra attributes are warned about.

## Test it

```bash
python3 -I tests/test_app.py    # mock Shopify store, 10,500 products
python3 -I tests/test_woo.py    # mock WooCommerce store (replicates documented API shapes)
python3 -I tests/test_features.py   # collections, search, specific products, reviews, Excel, link column, details
python3 -I tests/test_extension_parity.py   # Chrome extension output must match the Python tool (needs Node 20+)
```

None of the suites touch a real store. In particular, the WooCommerce `slug=` filter and `/products/reviews` route are written from the documented Store API and verified only against the mock. Try a live check with `python cli.py <store> --deep`.

## Notes

- **No product cap.** The scraper keeps paging until the store runs out of products. The Stop button ends a run
  early and keeps what was fetched.
- **Inventory counts are not public.** Stores only expose an in-stock flag per variant.
- **Deep mode is slower:** two extra requests per product, made by 4 parallel workers with short pauses.
- **Stores can block or disable the JSON feeds**, or be password protected. The app reports this clearly.
- **WooCommerce import:** variable products are linked to variations by SKU; the product handle is used as the
  parent SKU and duplicate variant SKUs are blanked, since WooCommerce requires unique SKUs. Categories combine the
  product type and collections.
- **Safety:** private and internal addresses are blocked (SSRF protection), redirects are checked hop by hop,
  response size is capped, requests are rate limited per IP (`RATE_LIMIT_PER_MINUTE`, default 12) and the number of
  concurrent scrapes is capped (`MAX_RUNNING_JOBS`, default 4).
- Only scrape data you're allowed to use. Copying another store's images and descriptions can infringe copyright
  and Shopify's terms.
