"""WooCommerce support.

WooCommerce stores publish their catalog through the public Store API (no login needed):

    /wp-json/wc/store/v1/products?per_page=100&page=N
    /wp-json/wc/store/v1/products/categories
    /wp-json/wc/store/v1/products?type=variation&parent=<id>

Sites using "plain" permalinks serve the same routes at /?rest_route=/wc/store/v1/products.

This module reads those routes and converts each product into the same dict shape the Shopify
exporters use (`handle`, `title`, `variants`, `images`, ...), plus a `_woo` dict holding the
WooCommerce-specific details, so every export format works for both platforms.
"""

import html as html_lib
import json
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode, urljoin, urlparse

import scraper
from scraper import ScrapeError, _fetch, _never_stop, _sleep, new_session

PRODUCTS_ROUTE = "/wc/store/v1/products"
CATEGORIES_ROUTE = "/wc/store/v1/products/categories"
PAGE_SIZE = 100
PROBE_STATUSES = (400, 401, 403, 404, 405, 410)
BLOCKED_CODES = {"rest_login_required", "rest_cannot_access", "rest_forbidden", "rest_not_logged_in"}


# --------------------------------------------------------------------------- small helpers


def _text(value):
    return html_lib.unescape(str(value if value is not None else "")).strip()


def _int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _json(body):
    """Parse a JSON body, tolerating the BOM or stray notices some WordPress sites print first."""
    text = body.decode("utf-8", errors="replace").lstrip("﻿ \t\r\n")
    try:
        return json.loads(text)
    except ValueError:
        for opener in ("[", "{"):
            i = text.find(opener)
            if i > 0:
                try:
                    return json.loads(text[i:])
                except ValueError:
                    continue
    return None


def _wp_message(body):
    data = _json(body)
    return _text(data.get("message")) if isinstance(data, dict) else ""


def _same_site(base, url):
    a = (urlparse(base).hostname or "").lower().removeprefix("www.")
    b = (urlparse(url).hostname or "").lower().removeprefix("www.")
    return bool(a) and a == b


# --------------------------------------------------------------------------- API discovery


class WooApi:
    """Builds Store API URLs for either permalink style."""

    def __init__(self, root):
        self.root = root.rstrip("/")

    def url(self, route, **params):
        query = urlencode({k: v for k, v in params.items() if v is not None})
        joiner = "&" if "rest_route=" in self.root else "?"
        return f"{self.root}{route}" + (joiner + query if query else "")


def _probe_json(session, url):
    """GET a URL and return (status, parsed JSON or None) without raising on HTTP errors."""
    status, body = _fetch(session, url, retries=1, allow_statuses=PROBE_STATUSES)
    return status, _json(body)


def _root_from_href(base, href):
    href = urljoin(base + "/", href)
    if not _same_site(base, href):
        return None
    if "rest_route=" in href:
        return href[: href.index("rest_route=") + len("rest_route=")]
    return href.rstrip("/")


def _discover_root(base, session):
    """Find the REST API address the site advertises in its <link rel="https://api.w.org/"> tag."""
    try:
        status, body = _fetch(session, base + "/", retries=1, allow_statuses=PROBE_STATUSES)
    except ScrapeError:
        return None
    if status != 200:
        return None
    page = body.decode("utf-8", errors="replace")[:300000]
    for tag in re.findall(r"<link\b[^>]*>", page, re.I):
        if re.search(r"""rel=["']https://api\.w\.org/?["']""", tag, re.I):
            m = re.search(r"""href=["']([^"']+)["']""", tag, re.I)
            if m:
                return _root_from_href(base, html_lib.unescape(m.group(1)))
    return None


def find_api(base, session=None):
    """Return (WooApi, None) if the store's public product API works, else (None, reason)."""
    session = session or new_session()
    blocked = False
    tried = set()

    def roots():
        yield f"{base}/wp-json"
        found = _discover_root(base, session)
        if found:
            yield found
        yield f"{base}/?rest_route="

    for root in roots():
        if root in tried:
            continue
        tried.add(root)
        api = WooApi(root)
        status, data = _probe_json(session, api.url(PRODUCTS_ROUTE, per_page=1))
        if status == 200 and isinstance(data, list):
            return api, None
        if status in (401, 403) or (isinstance(data, dict) and data.get("code") in BLOCKED_CODES):
            blocked = True
    if blocked:
        return None, "This store's public product API is switched off or blocked, so its products can't be read."
    return None, "No WooCommerce product API was found at that address (it needs a reasonably recent WooCommerce)."


def detect_platform(base, session=None):
    """Return ("shopify", None) or ("woocommerce", WooApi); raise ScrapeError if neither."""
    session = session or new_session()
    status, data = _probe_json(session, f"{base}/products.json?limit=1")
    if status == 200 and isinstance(data, dict) and isinstance(data.get("products"), list):
        return "shopify", None
    api, reason = find_api(base, session)
    if api:
        return "woocommerce", api
    raise ScrapeError(
        "Couldn't detect a Shopify or WooCommerce store at that address. "
        f"{reason} If you know which platform it is, choose it manually."
    )


# --------------------------------------------------------------------------- categories


def fetch_categories(api, session=None, should_stop=_never_stop, delay=0.3):
    session = session or new_session()
    out, seen = [], set()
    page = 1
    while not should_stop():
        headers = {}
        status, body = _fetch(
            session, api.url(CATEGORIES_ROUTE, per_page=PAGE_SIZE, page=page),
            headers_out=headers, allow_statuses=(400,),
        )
        if status == 400:
            if page > 1:
                break
            raise ScrapeError(_wp_message(body) or "The store rejected the category request.")
        data = _json(body)
        if not isinstance(data, list):
            raise ScrapeError("The store didn't return a category list.")
        added = 0
        for c in data:
            if isinstance(c, dict) and c.get("id") not in seen:
                seen.add(c.get("id"))
                added += 1
                out.append(
                    {
                        "id": c.get("id"),
                        "slug": c.get("slug") or "",
                        "title": _text(c.get("name")),
                        "parent": c.get("parent") or 0,
                        "count": c.get("count"),
                    }
                )
        total_pages = _int(headers.get("x-wp-totalpages"))
        done = page >= total_pages if total_pages else len(data) < PAGE_SIZE
        if added == 0 or done:
            break
        page += 1
        _sleep(delay)
    return out


def category_paths(categories):
    """Map category id -> "Parent > Child" path."""
    by_id = {c["id"]: c for c in categories}
    paths = {}
    for c in categories:
        parts, seen, cur = [], set(), c
        while cur and cur["id"] not in seen:
            seen.add(cur["id"])
            parts.append(cur["title"])
            cur = by_id.get(cur.get("parent"))
        paths[c["id"]] = " > ".join(reversed(parts))
    return paths


def resolve_category(categories, value):
    """Turn a category slug (or numeric id) into the id the Store API filters by."""
    value = (value or "").strip()
    if value.isdigit():
        return int(value)
    for c in categories:
        if c["slug"].lower() == value.lower():
            return c["id"]
    raise ScrapeError("That category wasn't found in the store.", 404)


# --------------------------------------------------------------------------- products


def _slim(item):
    """Drop bulky fields we never export (responsive image sets, rendered price HTML)."""
    for img in item.get("images") or []:
        for key in ("srcset", "sizes", "thumbnail"):
            img.pop(key, None)
    item.pop("price_html", None)
    return item


def fetch_products(
    api, category_id=None, page_from=1, page_to=None, delay=0.4,
    progress=None, should_stop=_never_stop, out=None,
):
    """Fetch products page by page with no cap on the number of pages.

    Stops at the page count the store reports (X-WP-TotalPages), or on a short or empty page when
    it doesn't report one. `out` may be passed in so a caller keeps partial results on failure.
    """
    session = new_session()
    products = out if out is not None else []
    seen = {p.get("id") for p in products}
    first = page = max(1, int(page_from or 1))
    while True:
        if should_stop():
            break
        if page_to and page > int(page_to):
            break
        headers = {}
        url = api.url(PRODUCTS_ROUTE, per_page=PAGE_SIZE, page=page, category=category_id)
        status, body = _fetch(session, url, headers_out=headers, allow_statuses=(400,))
        if status == 400:
            if page > first:
                break  # asked for a page past the end
            raise ScrapeError(_wp_message(body) or "The store rejected the product request.")
        items = _json(body)
        if not isinstance(items, list):
            raise ScrapeError("The store returned data in an unexpected format.")
        added = 0
        for raw in items:
            if isinstance(raw, dict) and raw.get("id") not in seen:
                seen.add(raw.get("id"))
                products.append(_slim(raw))
                added += 1
        total_pages = _int(headers.get("x-wp-totalpages"))
        if progress:
            progress(page=page, count=len(products), total=_int(headers.get("x-wp-total")) or None)
        done = page >= total_pages if total_pages else len(items) < PAGE_SIZE
        if added == 0 or done:
            break
        page += 1
        _sleep(delay)
    return products


def _product_variations(api, session, product):
    """Fetch every variation of one variable product. Returns (variations, number_missing)."""
    ids = [v.get("id") for v in product.get("variations") or [] if v.get("id") is not None]
    wanted = set(ids)
    got = {}
    page = 1
    # One request per product when the store supports type=variation&parent=...
    while True:
        status, body = _fetch(
            session,
            api.url(PRODUCTS_ROUTE, type="variation", parent=product["id"], per_page=PAGE_SIZE, page=page),
            allow_statuses=(400, 404),
        )
        if status != 200:
            break
        items = _json(body)
        if not isinstance(items, list) or not items:
            break
        new = 0
        for it in items:
            if isinstance(it, dict) and it.get("id") in wanted and it["id"] not in got:
                got[it["id"]] = _slim(it)
                new += 1
        if len(items) < PAGE_SIZE or new == 0:
            break
        page += 1
    # Otherwise (or for any that are missing) ask for each variation by id.
    for vid in ids:
        if vid in got:
            continue
        status, body = _fetch(session, api.url(f"{PRODUCTS_ROUTE}/{vid}"), allow_statuses=(400, 404))
        if status == 200:
            it = _json(body)
            if isinstance(it, dict) and it.get("id") == vid:
                got[vid] = _slim(it)
    return [got[i] for i in ids if i in got], len(ids) - len(got)


def fetch_variations(api, products, workers=4, delay=0.15, progress=None, should_stop=_never_stop):
    """Attach `_variations` (price, SKU, image, stock per variation) to each variable product.

    Returns the number of products whose variations could not all be read.
    """
    targets = [p for p in products if p.get("type") == "variable" and p.get("variations")]
    local = threading.local()
    lock = threading.Lock()
    state = {"done": 0, "failed": 0}
    total = len(targets)

    def work(p):
        if should_stop():
            return
        if not hasattr(local, "session"):
            local.session = new_session()
        failed = False
        try:
            found, missing = _product_variations(api, local.session, p)
            p["_variations"] = found
            if missing:
                p["_variation_error"] = f"{missing} variation(s) could not be read"
                failed = True
        except ScrapeError as e:
            p["_variation_error"] = e.message
            failed = True
        _sleep(delay)
        with lock:
            state["done"] += 1
            state["failed"] += 1 if failed else 0
            if progress:
                progress(done=state["done"], total=total)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(work, targets))
    return state["failed"]


# --------------------------------------------------------------------------- normalising


def to_money(value, minor=2):
    """Store API prices are integers in the currency's minor unit ("1800" -> "18.00")."""
    if value is None or value == "":
        return ""
    try:
        minor = int(minor)
        amount = Decimal(str(value)) / (Decimal(10) ** minor)
    except (InvalidOperation, ValueError, TypeError):
        return ""
    return f"{amount:.{minor}f}"


def _price_pair(prices, on_sale, minor):
    """(price, compare_at_price) in the Shopify sense: compare-at is the regular price during a sale."""
    price = to_money(prices.get("price"), minor)
    regular = to_money(prices.get("regular_price"), minor)
    compare = None
    if on_sale and price and regular:
        try:
            if Decimal(regular) > Decimal(price):
                compare = regular
        except InvalidOperation:
            pass
    return price, compare


def _variation_values(var_attrs, listing_attrs):
    """Option values for one variation, resolved from term slugs to display names."""
    chosen = {}
    for a in listing_attrs or []:
        if isinstance(a, dict):
            chosen[_text(a.get("name")).lower()] = _text(a.get("value"))
    out = []
    for attr in var_attrs:
        value = chosen.get(_text(attr.get("name")).lower(), "")
        lookup = {}
        for t in attr.get("terms") or []:
            name = _text(t.get("name"))
            slug = _text(t.get("slug")).lower()
            if slug:
                lookup[slug] = name
            lookup[name.lower()] = name
        out.append(lookup.get(value.lower(), value))
    return out


def normalize(raw, paths=None):
    """Convert one Store API product into the shared product shape (plus a `_woo` details dict)."""
    paths = paths or {}
    prices = raw.get("prices") or {}
    minor = _int(prices.get("currency_minor_unit"), 2)
    ptype = raw.get("type") or "simple"
    in_stock = bool(raw.get("is_in_stock", True))
    on_sale = bool(raw.get("on_sale"))

    images = []
    for im in raw.get("images") or []:
        if isinstance(im, dict) and im.get("src"):
            images.append({"id": im.get("id"), "src": im["src"], "position": len(images) + 1, "alt": im.get("alt") or None})
    first_src = images[0]["src"] if images else None

    attrs = [a for a in raw.get("attributes") or [] if isinstance(a, dict) and a.get("name")]
    attrs.sort(key=lambda a: not a.get("has_variations"))  # variation attributes first (stable)
    var_attrs = [a for a in attrs if a.get("has_variations")]
    options = [
        {"name": _text(a["name"]), "position": i, "values": [_text(t.get("name")) for t in a.get("terms") or []]}
        for i, a in enumerate(var_attrs, start=1)
    ]

    variants = []
    listing = {v.get("id"): v for v in raw.get("variations") or [] if isinstance(v, dict)}
    if ptype == "variable" and listing:
        fetched = {v.get("id"): v for v in raw.get("_variations") or []}
        for pos, vid in enumerate(listing, start=1):
            vraw = fetched.get(vid) or {}
            values = _variation_values(var_attrs, listing[vid].get("attributes"))
            vprices = vraw.get("prices") or {}
            price, compare = (
                _price_pair(vprices, vraw.get("on_sale"), _int(vprices.get("currency_minor_unit"), minor))
                if vraw else ("", None)
            )
            vimgs = [im for im in vraw.get("images") or [] if isinstance(im, dict) and im.get("src")]
            featured = {"src": vimgs[0]["src"]} if vimgs and vimgs[0]["src"] != first_src else None
            variants.append(
                {
                    "id": vid,
                    "title": " / ".join(v for v in values if v) or "Default Title",
                    "option1": values[0] if len(values) > 0 else None,
                    "option2": values[1] if len(values) > 1 else None,
                    "option3": values[2] if len(values) > 2 else None,
                    "_option_values": values,
                    "sku": vraw.get("sku") or "",
                    "price": price,
                    "compare_at_price": compare,
                    "grams": None,
                    "available": bool(vraw.get("is_in_stock", True)) if vraw else in_stock,
                    "position": pos,
                    "featured_image": featured,
                }
            )
    if not variants:
        price, compare = ("", None) if ptype == "grouped" else _price_pair(prices, on_sale, minor)
        options = [{"name": "Title", "position": 1, "values": ["Default Title"]}]
        variants.append(
            {
                "id": raw.get("id"), "title": "Default Title", "option1": "Default Title",
                "sku": raw.get("sku") or "", "price": price, "compare_at_price": compare,
                "grams": None, "available": in_stock, "position": 1, "featured_image": None,
            }
        )

    cats = [c for c in raw.get("categories") or [] if isinstance(c, dict)]
    cat_paths = [paths.get(c.get("id")) or _text(c.get("name")) for c in cats]
    product_type = ""
    if cats:
        product_type = _text(max(zip(cat_paths, cats), key=lambda pc: pc[0].count(">"))[1].get("name"))

    price_range = prices.get("price_range") if isinstance(prices.get("price_range"), dict) else {}
    lo = to_money(price_range.get("min_amount") or prices.get("price"), minor)
    hi = to_money(price_range.get("max_amount") or prices.get("price"), minor)
    add_to_cart = raw.get("add_to_cart") if isinstance(raw.get("add_to_cart"), dict) else {}

    info = {
        "type": ptype,
        "sku": raw.get("sku") or "",
        "short_description": raw.get("short_description") or "",
        "on_sale": on_sale,
        "average_rating": raw.get("average_rating") or "",
        "review_count": _int(raw.get("review_count")),
        "is_purchasable": raw.get("is_purchasable"),
        "is_in_stock": in_stock,
        "is_on_backorder": bool(raw.get("is_on_backorder")),
        "low_stock_remaining": raw.get("low_stock_remaining"),
        "sold_individually": bool(raw.get("sold_individually")),
        "currency_code": prices.get("currency_code") or "",
        "currency_symbol": prices.get("currency_symbol") or "",
        "price_min": float(lo) if lo else None,
        "price_max": float(hi) if hi else None,
        "attributes": [
            {
                "name": _text(a["name"]),
                "taxonomy": a.get("taxonomy") or "",
                "has_variations": bool(a.get("has_variations")),
                "terms": [_text(t.get("name")) for t in a.get("terms") or []],
                "default": next((_text(t.get("name")) for t in a.get("terms") or [] if t.get("default")), ""),
            }
            for a in attrs
        ],
        "category_paths": cat_paths,
        "brands": [_text(b.get("name")) for b in raw.get("brands") or [] if isinstance(b, dict)],
        "external_url": add_to_cart.get("url") if ptype == "external" else "",
        "button_text": add_to_cart.get("text") if ptype == "external" else "",
        "grouped_products": [i for i in raw.get("grouped_products") or [] if isinstance(i, int)],
        "variation_error": raw.get("_variation_error") or "",
        "extensions": raw.get("extensions") or {},
    }
    return {
        "id": raw.get("id"),
        "handle": raw.get("slug") or str(raw.get("id")),
        "title": _text(raw.get("name")),
        "body_html": raw.get("description") or raw.get("short_description") or "",
        "vendor": info["brands"][0] if info["brands"] else "",
        "product_type": product_type,
        "tags": [_text(t.get("name")) for t in raw.get("tags") or [] if isinstance(t, dict)],
        "options": options,
        "variants": variants,
        "images": images,
        "_url": raw.get("permalink") or "",
        "_collections": [
            {"handle": c.get("slug") or "", "title": _text(c.get("name"))} for c in cats
        ],
        "_woo": info,
    }


# --------------------------------------------------------------------------- deep details (product pages)

_UNIT_GRAMS = {"kg": 1000.0, "g": 1.0, "lbs": 453.592, "lb": 453.592, "oz": 28.3495}
_UNIT_CM = {"cm": 1.0, "mm": 0.1, "m": 100.0, "in": 2.54, "yd": 91.44}


def _number(s):
    s = s.strip()
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".") if s.rfind(",") > s.rfind(".") else s.replace(",", "")
    elif "," in s:
        head, _, tail = s.rpartition(",")
        s = f"{head.replace(',', '')}.{tail}" if len(tail) <= 2 else s.replace(",", "")
    try:
        return float(s)
    except ValueError:
        return None


def parse_weight_grams(text):
    """"1.5 kg" / "2 lbs" / "250 g" -> grams (int), or None."""
    m = re.search(r"(\d[\d.,]*)\s*(kg|g|lbs?|oz)\b", text or "", re.I)
    if not m:
        return None
    n = _number(m.group(1))
    return None if n is None else int(round(n * _UNIT_GRAMS[m.group(2).lower()]))


def parse_dimensions_cm(text):
    """"20 × 10 × 5 cm" -> (20.0, 10.0, 5.0) in centimetres, or None."""
    m = re.search(
        r"(\d[\d.,]*)\s*[×xX]\s*(\d[\d.,]*)\s*[×xX]\s*(\d[\d.,]*)\s*(cm|mm|m|in|yd)\b", text or ""
    )
    if not m:
        return None
    nums = [_number(g) for g in m.groups()[:3]]
    if None in nums:
        return None
    factor = _UNIT_CM[m.group(4).lower()]
    return tuple(round(n * factor, 2) for n in nums)


def _clean(fragment):
    return re.sub(r"\s+", " ", html_lib.unescape(re.sub(r"<[^>]+>", " ", fragment or ""))).strip()


def _row_text(page, key):
    m = re.search(
        rf"woocommerce-product-attributes-item--{key}\b.*?<td[^>]*>(.*?)</td>", page, re.S | re.I
    )
    return _clean(m.group(1)) if m else ""


def _variation_data(page):
    """The per-variation JSON WooCommerce embeds in the add-to-cart form (small products only)."""
    m = re.search(r"""data-product_variations=(["'])(.*?)\1""", page, re.S)
    if not m:
        return {}
    data = _json(html_lib.unescape(m.group(2)).encode("utf-8"))
    if not isinstance(data, list):
        return {}
    return {d.get("variation_id"): d for d in data if isinstance(d, dict)}


def _page_details(page):
    parser = scraper._PageMeta()
    parser.feed(page)
    meta = parser.meta
    details = {
        "seo_title": parser.title,
        "seo_description": meta.get("description", ""),
        "canonical": parser.canonical,
        "og_title": meta.get("og:title", ""),
        "og_description": meta.get("og:description", ""),
        "og_image": meta.get("og:image", ""),
        "json_ld": parser.json_ld,
    }
    details.update(scraper._parse_json_ld(parser.json_ld))
    return details


def _apply_page_data(product, page):
    """Fill weight and dimensions on each variant from the product page."""
    weight_text = _row_text(page, "weight")
    dims_text = _row_text(page, "dimensions")
    per_variation = _variation_data(page)
    for v in product.get("variants") or []:
        vd = per_variation.get(v.get("id")) or {}
        grams = parse_weight_grams(_clean(vd.get("weight_html")))
        if grams is None:
            grams = parse_weight_grams(weight_text)
        dims = parse_dimensions_cm(_clean(vd.get("dimensions_html"))) or parse_dimensions_cm(dims_text)
        if grams is not None:
            v["grams"] = grams
        if dims:
            v["length_cm"], v["width_cm"], v["height_cm"] = dims


def fetch_details(base, products, workers=4, delay=0.15, progress=None, should_stop=_never_stop):
    """Read each product page for SEO tags, structured data, weight and dimensions (optional, slower)."""
    local = threading.local()
    lock = threading.Lock()
    state = {"done": 0}
    total = len(products)

    def work(p):
        if should_stop():
            return
        if not hasattr(local, "session"):
            local.session = new_session("text/html,application/json")
        details = {}
        url = p.get("_url")
        if url and _same_site(base, url):
            try:
                status, body = _fetch(local.session, url, retries=3, allow_404=True)
                if status == 200:
                    page = body.decode("utf-8", errors="replace")
                    details = _page_details(page)
                    _apply_page_data(p, page)
            except ScrapeError as e:
                details = {"page_error": e.message}
        p["_details"] = details
        _sleep(delay)
        with lock:
            state["done"] += 1
            if progress:
                progress(done=state["done"], total=total)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(work, products))
