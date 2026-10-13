"""Command-line exporter: scrape a Shopify or WooCommerce store and write every export format.

    python cli.py noelmartz.com                  # products + collections (platform auto-detected)
    python cli.py noelmartz.com --deep           # also SEO, structured data, barcodes, videos, weight
    python cli.py mystore.com --platform woocommerce --out exports
    python cli.py mystore.com/product-category/clothing/   # just that category

There is no limit on the number of products.
"""

import argparse
import json
import os
import re
import sys

import exporters
import pipeline
import scraper


class PrintReport(pipeline.NullReport):
    """Shows progress on one updating line."""

    def __init__(self):
        self._dirty = False

    def platform(self, name):
        print(f"Platform: {name}")

    def phase(self, name):
        if self._dirty:
            print()
            self._dirty = False

    def message(self, text):
        print(f"\r  {text}   ", end="", flush=True)
        self._dirty = True

    def warn(self, text):
        if self._dirty:
            print()
            self._dirty = False
        print(f"  warning: {text}")


def main():
    ap = argparse.ArgumentParser(description="Export all public product data from a Shopify or WooCommerce store.")
    ap.add_argument("store", help="store domain or collection/category URL, e.g. noelmartz.com")
    ap.add_argument("--platform", choices=("auto", "shopify", "woocommerce"), default="auto",
                    help="store platform (default: auto-detect)")
    ap.add_argument("--deep", action="store_true",
                    help="read each product page for SEO, structured data, media (and weight/dimensions on WooCommerce)")
    ap.add_argument("--no-collections", action="store_true", help="skip collections / the category tree")
    ap.add_argument("--no-variations", action="store_true",
                    help="WooCommerce: skip per-variation prices and SKUs (faster)")
    ap.add_argument("--out", default="export", help="output folder (default: export)")
    args = ap.parse_args()

    products = []
    try:
        base, collection = scraper.parse_store_input(args.store)
        print(f"Store: {base}" + (f"  (collection/category: {collection})" if collection else ""))
        report = PrintReport()
        pipeline.run(
            base, collection=collection, include_collections=not args.no_collections, deep=args.deep,
            platform=args.platform, variations=not args.no_variations, report=report, products=products,
        )
        report.phase("done")
    except scraper.ScrapeError as e:
        print()
        if not products:
            sys.exit(f"Error: {e.message}")
        print(f"Error: {e.message}\nSaving the {len(products)} products fetched so far.")
    except KeyboardInterrupt:
        print("\nStopped.")
        if not products:
            sys.exit(1)

    if not products:
        sys.exit("No products found.")

    os.makedirs(args.out, exist_ok=True)
    host = re.sub(r"[^A-Za-z0-9.-]+", "_", base.split("://", 1)[-1])
    files = {
        f"{host}-full-details.csv": exporters.to_full_csv(products, base),
        f"{host}-shopify.csv": exporters.to_shopify_csv(products),
        f"{host}-woocommerce.csv": exporters.to_woocommerce_csv(products),
        f"{host}-products.json": json.dumps([exporters.to_full_json_item(p) for p in products], indent=2),
    }
    for name, data in files.items():
        with open(os.path.join(args.out, name), "w", encoding="utf-8", newline="") as f:
            f.write(data)
        print("wrote", os.path.join(args.out, name))
    variants = sum(len(p.get("variants") or []) for p in products)
    print(f"Done: {len(products)} products, {variants} variants.")


if __name__ == "__main__":
    main()
