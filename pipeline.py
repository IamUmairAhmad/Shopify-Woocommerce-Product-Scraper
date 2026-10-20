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
    base, *, collections=None, handles=None, search=None, page_from=1, page_to=None,
    include_collections=True, deep=False, reviews=False, platform="auto", variations=True,
    report=None, should_stop=_never, products=None,
):
    """Scrape a store and fill `products` (pass a list to keep partial results if it fails).

    platform: "auto", "shopify" or "woocommerce". Returns the platform that was scraped.
    `collections` are Shopify collection handles or WooCommerce category slugs.
    `handles` are specific product handles (slugs); when given, only those products are fetched.
    `search` is a keyword: every word must match (WooCommerce searches on the store, Shopify filters locally).
    """
    collections = [c for c in (collections or []) if c]
    handles = [h for h in (handles or []) if h]
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

    sel = dict(collections=collections, handles=handles, search=(search or "").strip(),
               page_from=page_from, page_to=page_to)
    if platform == "shopify":
        _run_shopify(base, sel, include_collections, deep, reviews, report, should_stop, products)
    else:
        _run_woo(api, base, sel, include_collections, deep, reviews, variations, report, should_stop, products)
    return platform


def _warn_missing(report, missing):
    if missing:
        shown = ", ".join(missing[:10]) + (f" and {len(missing) - 10} more" if len(missing) > 10 else "")
        report.warn(f"{len(missing)} product(s) were not found: {shown}")


def _merge(products, seen, items):
    for it in items:
        if it.get("id") not in seen:
            seen.add(it.get("id"))
            products.append(it)


def _attach_page_reviews(products):
    for p in products:
        p["_reviews"] = list((p.get("_details") or {}).get("reviews") or [])


def _run_shopify(base, sel, include_collections, deep, reviews, report, stop, products):
    report.phase("products")

    def on_products(page, count, total=None):
        report.update(pages=page, products=count)
        report.message(f"Fetching products… page {page}, {count} found")

    seen = set()
    if sel["handles"]:
        _, missing = scraper.fetch_products_by_handle(
            base, sel["handles"], progress=on_products, should_stop=stop, out=products
        )
        _warn_missing(report, missing)
    elif len(sel["collections"]) > 1:
        # One list per collection so overlap between collections can't end a collection early.
        for col in sel["collections"]:
            if stop():
                break
            part = []
            try:
                scraper.fetch_products(base, col, sel["page_from"], sel["page_to"], progress=on_products,
                                       should_stop=stop, out=part)
            finally:
                _merge(products, seen, part)
    else:
        col = sel["collections"][0] if sel["collections"] else None
        scraper.fetch_products(
            base, col, sel["page_from"], sel["page_to"], progress=on_products, should_stop=stop, out=products
        )
    if sel["search"]:
        products[:] = [p for p in products if scraper.product_matches(p, sel["search"])]
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

    if (deep or reviews) and not stop():
        report.phase("details" if deep else "reviews")

        def on_details(done, total):
            report.update(details_done=done, details_total=total)
            report.message(f"Fetching product {'details' if deep else 'reviews'}… {done}/{total}")

        scraper.fetch_details(base, products, progress=on_details, should_stop=stop, page_only=not deep)
        failed = sum(1 for p in products if (p.get("_details") or {}).get("page_error"))
        if failed:
            report.warn(f"{failed} product page(s) could not be read" + (" for SEO details." if deep else " for reviews."))
        if reviews:
            _attach_page_reviews(products)
            if not any(p.get("_reviews") for p in products):
                report.warn(
                    "No reviews were found. Shopify has no public reviews feed: reviews come from apps such as "
                    "Judge.me or Yotpo, and only appear here when the product page publishes them as structured data."
                )


def _run_woo(api, base, sel, include_collections, deep, reviews, variations, report, stop, products):
    categories, paths, category_ids = [], {}, []
    collections = sel["collections"]

    if collections or include_collections:
        report.phase("categories")
        report.message("Reading categories…")
        try:
            categories = woo.fetch_categories(api, should_stop=stop)
            paths = woo.category_paths(categories)
        except ScrapeError as e:
            if collections:
                raise
            report.warn(f"Category hierarchy unavailable, so categories are listed without parents: {e.message}")
    category_ids = [woo.resolve_category(categories, c) for c in collections]

    report.phase("products")

    def on_products(page, count, total=None):
        report.update(pages=page, products=count)
        report.message(
            f"Fetching products… page {page}, {count}" + (f" of {total}" if total else "") + " found"
        )

    raw = []
    try:
        if sel["handles"]:
            _, missing = woo.fetch_products_by_slug(
                api, sel["handles"], progress=on_products, should_stop=stop, out=raw
            )
            _warn_missing(report, missing)
        elif len(category_ids) > 1:
            seen = set()
            for cid in category_ids:
                if stop():
                    break
                part = []
                try:
                    woo.fetch_products(api, cid, sel["page_from"], sel["page_to"], progress=on_products,
                                       should_stop=stop, out=part, search=sel["search"])
                finally:
                    _merge(raw, seen, part)
        else:
            woo.fetch_products(
                api, category_ids[0] if category_ids else None, sel["page_from"], sel["page_to"],
                progress=on_products, should_stop=stop, out=raw, search=sel["search"],
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

    if reviews and not stop():
        report.phase("reviews")

        def on_reviews(done, total):
            report.update(reviews_done=done, reviews_total=total)
            report.message(f"Fetching reviews… page {done}/{total}")

        found, reason = woo.fetch_reviews(api, progress=on_reviews, should_stop=stop)
        by_product = {}
        for r in found:
            by_product.setdefault(r.get("product_id"), []).append(r)
        for p in products:
            p["_reviews"] = by_product.get(p.get("id"), [])
        if reason:
            report.warn(reason)
        elif not any(p["_reviews"] for p in products):
            report.warn("The store has no reviews for the selected products.")

    if deep and not stop():
        report.phase("details")

        def on_details(done, total):
            report.update(details_done=done, details_total=total)
            report.message(f"Fetching product pages… {done}/{total}")

        woo.fetch_details(base, products, progress=on_details, should_stop=stop)
        failed = sum(1 for p in products if (p.get("_details") or {}).get("page_error"))
        if failed:
            report.warn(f"{failed} product page(s) could not be read for SEO, weight and dimensions.")
