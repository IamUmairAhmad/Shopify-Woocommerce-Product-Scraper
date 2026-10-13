"""Runs a scrape end to end for either platform. Used by the web app and the command line."""

import scraper
import woo
from scraper import ScrapeError


class NullReport:
    """Receives progress from `run`. Subclass it (or duck-type it) to show progress somewhere."""

    def platform(self, name):
        pass

    def phase(self, name):
        pass

    def message(self, text):
        pass

    def update(self, **values):
        pass

    def warn(self, text):
        pass


def _never():
    return False


def run(
    base, *, collection=None, page_from=1, page_to=None, include_collections=True, deep=False,
    platform="auto", variations=True, report=None, should_stop=_never, products=None,
):
    """Scrape a store and fill `products` (pass a list to keep partial results if it fails).

    platform: "auto", "shopify" or "woocommerce". Returns the platform that was scraped.
    `collection` is a Shopify collection handle or a WooCommerce category slug.
    """
    report = report or NullReport()
    products = products if products is not None else []
    api = None

    if platform == "auto":
        report.phase("detect")
        report.message("Detecting platform…")
        platform, api = woo.detect_platform(base)
    elif platform == "woocommerce":
        api, reason = woo.find_api(base)
        if api is None:
            raise ScrapeError(reason)
    elif platform != "shopify":
        raise ScrapeError("Unknown platform.")
    report.platform(platform)

    if platform == "shopify":
        _run_shopify(base, collection, page_from, page_to, include_collections, deep, report, should_stop, products)
    else:
        _run_woo(api, base, collection, page_from, page_to, include_collections, deep, variations,
                 report, should_stop, products)
    return platform


def _run_shopify(base, collection, page_from, page_to, include_collections, deep, report, stop, products):
    report.phase("products")

    def on_products(page, count, total=None):
        report.update(pages=page, products=count)
        report.message(f"Fetching products… page {page}, {count} found")

    scraper.fetch_products(
        base, collection, page_from, page_to, progress=on_products, should_stop=stop, out=products
    )
    if not products:
        raise ScrapeError("No products found for that selection.", 404)

    if include_collections and not stop():
        report.phase("collections")

        def on_collections(done, total, title):
            report.update(collections_done=done, collections_total=total)
            report.message(f"Mapping collections… {done}/{total}" + (f" ({title})" if title else ""))

        membership, warnings = scraper.fetch_collection_membership(
            base, progress=on_collections, should_stop=stop
        )
        for w in warnings[:20]:
            report.warn(w)
        if len(warnings) > 20:
            report.warn(f"…and {len(warnings) - 20} more collection warnings.")
        for p in products:
            p["_collections"] = membership.get(p.get("id"), [])

    if deep and not stop():
        report.phase("details")

        def on_details(done, total):
            report.update(details_done=done, details_total=total)
            report.message(f"Fetching product details… {done}/{total}")

        scraper.fetch_details(base, products, progress=on_details, should_stop=stop)
        failed = sum(1 for p in products if (p.get("_details") or {}).get("page_error"))
        if failed:
            report.warn(f"{failed} product page(s) could not be read for SEO details.")


def _run_woo(api, base, collection, page_from, page_to, include_collections, deep, variations,
             report, stop, products):
    categories, paths, category_id = [], {}, None

    if collection or include_collections:
        report.phase("categories")
        report.message("Reading categories…")
        try:
            categories = woo.fetch_categories(api, should_stop=stop)
            paths = woo.category_paths(categories)
        except ScrapeError as e:
            if collection:
                raise
            report.warn(f"Category hierarchy unavailable, so categories are listed without parents: {e.message}")
    if collection:
        category_id = woo.resolve_category(categories, collection)

    report.phase("products")

    def on_products(page, count, total=None):
        report.update(pages=page, products=count)
        report.message(
            f"Fetching products… page {page}, {count}" + (f" of {total}" if total else "") + " found"
        )

    raw = []
    try:
        woo.fetch_products(
            api, category_id, page_from, page_to, progress=on_products, should_stop=stop, out=raw
        )
        if not raw:
            raise ScrapeError("No products found for that selection.", 404)

        n_variable = sum(1 for r in raw if r.get("type") == "variable")
        if variations and n_variable and not stop():
            report.phase("variations")

            def on_variations(done, total):
                report.update(variations_done=done, variations_total=total)
                report.message(f"Fetching variation prices and SKUs… {done}/{total}")

            failed = woo.fetch_variations(api, raw, progress=on_variations, should_stop=stop)
            if failed:
                report.warn(
                    f"{failed} variable product(s) had variations that couldn't be read; "
                    "those variations have no price or SKU."
                )
        elif n_variable and not variations:
            report.warn("Variation prices and SKUs were skipped, so variable products have no per-variation prices.")
    finally:
        # Keep whatever was fetched even if a later step failed.
        products.extend(woo.normalize(r, paths) for r in raw)

    too_many = sum(
        1 for p in products if sum(1 for o in p.get("options") or [] if o.get("name") != "Title") > 3
    )
    if too_many:
        report.warn(
            f"{too_many} product(s) have more than 3 variation attributes. Shopify supports 3, so the Shopify CSV "
            "drops the extras; the WooCommerce CSV, full-details CSV and JSON keep them all."
        )

    if deep and not stop():
        report.phase("details")

        def on_details(done, total):
            report.update(details_done=done, details_total=total)
            report.message(f"Fetching product pages… {done}/{total}")

        woo.fetch_details(base, products, progress=on_details, should_stop=stop)
        failed = sum(1 for p in products if (p.get("_details") or {}).get("page_error"))
        if failed:
            report.warn(f"{failed} product page(s) could not be read for SEO, weight and dimensions.")
