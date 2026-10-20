"""Fetch product, collection and detail data from a public Shopify storefront.

Shopify storefronts expose their catalog as JSON:
    /products.json?limit=250&page=N                  all products (no page limit here)
    /collections.json?limit=250&page=N               collection list
    /collections/<handle>/products.json?limit=250    products in one collection
    /products/<handle>.js                            storefront product JSON (media, selling plans)
    /products/<handle>                               product page (SEO tags, JSON-LD structured data)
"""

import html as html_lib
import ipaddress
import json
import os
import re
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser
from urllib.parse import parse_qs, urljoin, urlparse

import requests

# Only set this to "1" for local testing against a mock store.
ALLOW_PRIVATE = os.environ.get("ALLOW_PRIVATE_HOSTS") == "1"

USER_AGENT = "Mozilla/5.0 (compatible; ShopifyProductExporter/1.0)"
PAGE_SIZE = 250
MAX_RESPONSE_BYTES = 25 * 1024 * 1024
MAX_REDIRECTS = 5
HANDLE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._~%-]*$")


class ScrapeError(Exception):
    """An error whose message is safe to show to the user."""

    def __init__(self, message, status=400):
        super().__init__(message)
        self.message = message
        self.status = status


def _never_stop():
    return False


# Politeness delays between requests can be scaled (tests set this to 0).
DELAY_SCALE = float(os.environ.get("SCRAPE_DELAY_SCALE", "1"))


def _sleep(seconds):
    if DELAY_SCALE > 0:
        time.sleep(seconds * DELAY_SCALE)


# --------------------------------------------------------------------------- input


def parse_store_input(raw):
    """Turn user input into (base_url, collection_handle_or_None).

    Accepts `store.com`, `https://store.com/`, or a collection URL such as
    `https://store.com/collections/shoes`.
    """
    raw = (raw or "").strip()
    if not raw:
        raise ScrapeError("Enter a Shopify store domain, for example store.com.")
    if "://" not in raw:
        raw = "https://" + raw
    try:
        parsed = urlparse(raw)
        host = (parsed.hostname or "").lower().rstrip(".")
        port = parsed.port
    except ValueError:
        raise ScrapeError("That doesn't look like a valid store address.")
    if not host or (("." not in host) and not ALLOW_PRIVATE):
        raise ScrapeError("That doesn't look like a valid store domain.")
    if parsed.scheme not in ("http", "https"):
        raise ScrapeError("Only http and https addresses are supported.")

    if ALLOW_PRIVATE:
        netloc = host + (f":{port}" if port else "")
        base = f"{parsed.scheme}://{netloc}"
    else:
        if port not in (None, 80, 443):
            raise ScrapeError("Custom ports are not supported.")
        base = f"https://{host}"

    collection = None
    # Collapse repeated slashes so pasted URLs like https://store.com//collections/jackets work.
    path = re.sub(r"/{2,}", "/", parsed.path or "")
    # An optional language prefix is allowed: /en/collections/x, /en-gb/collections/x.
    m = re.match(r"^(?:/[A-Za-z]{2}(?:-[A-Za-z]{2,4})?)?/collections/([^/?#]+)", path)
    if m and HANDLE_RE.match(m.group(1)):
        collection = m.group(1)
    else:
        # WooCommerce category links: /product-category/clothing/hoodies/ -> "hoodies",
        # or the plain-permalink form ?product_cat=hoodies.
        wm = re.search(r"/product-category/([^?#]+)", path)
        slug = wm.group(1).rstrip("/").split("/")[-1] if wm else ""
        if not slug:
            slug = (parse_qs(parsed.query).get("product_cat") or [""])[0]
        if slug and HANDLE_RE.match(slug):
            collection = slug
    return base, collection


# --------------------------------------------------------------------------- HTTP


def _check_host(host):
    """Refuse to connect to private or internal addresses (SSRF protection)."""
    if ALLOW_PRIVATE:
        return
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        raise ScrapeError(f"Could not find a website at {host}.")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            raise ScrapeError("That address isn't allowed.")


def _read_capped(response):
    chunks, size = [], 0
    for chunk in response.iter_content(65536):
        size += len(chunk)
        if size > MAX_RESPONSE_BYTES:
            raise ScrapeError("The store returned too much data in one response.")
        chunks.append(chunk)
    return b"".join(chunks)


def new_session(accept="application/json"):
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT, "Accept": accept})
    return s


def _fetch(session, url, retries=4, allow_404=False, allow_statuses=(), headers_out=None):
    """GET a URL; return (status, body). Redirects are followed manually so every hop is checked.

    `allow_statuses` lists extra HTTP statuses to hand back instead of raising an error.
    `headers_out`, if given, is filled with the response headers (names lower-cased).
    """
    attempt = 0
    hops = 0
    while True:
        _check_host(urlparse(url).hostname or "")
        try:
            resp = session.get(url, timeout=(6, 30), allow_redirects=False, stream=True)
        except requests.exceptions.SSLError:
            raise ScrapeError("The store's SSL certificate could not be verified.")
        except requests.exceptions.RequestException:
            attempt += 1
            if attempt > retries:
                raise ScrapeError("Could not reach that store. Check the domain and try again.")
            time.sleep(1.5 * attempt)
            continue

        try:
            if 300 <= resp.status_code < 400:
                hops += 1
                location = resp.headers.get("Location")
                if not location or hops > MAX_REDIRECTS:
                    raise ScrapeError("The store redirected too many times.")
                url = urljoin(url, location)
                if urlparse(url).path.rstrip("/") == "/password":
                    raise ScrapeError("This store is password protected.")
                continue

            if resp.status_code in allow_statuses:
                if headers_out is not None:
                    headers_out.update({k.lower(): v for k, v in resp.headers.items()})
                return resp.status_code, _read_capped(resp)

            if resp.status_code in (429, 500, 502, 503, 504):
                attempt += 1
                if attempt > retries:
                    raise ScrapeError(
                        "The store is rate limiting requests. Wait a few minutes and try again.",
                        status=429,
                    )
                try:
                    wait = min(float(resp.headers.get("Retry-After", "")), 20)
                except ValueError:
                    wait = 2 * attempt
                time.sleep(wait)
                continue

            if resp.status_code == 404:
                if allow_404:
                    return 404, b""
                raise ScrapeError(
                    "No product feed found. This may not be a Shopify store, "
                    "or the collection doesn't exist.",
                    status=404,
                )
            if resp.status_code in (401, 403):
                raise ScrapeError("The store blocked access to its product feed.", status=403)
            if resp.status_code != 200:
                raise ScrapeError(f"The store returned an unexpected response ({resp.status_code}).")

            if headers_out is not None:
                headers_out.update({k.lower(): v for k, v in resp.headers.items()})
            return 200, _read_capped(resp)
        finally:
            resp.close()


def _get_json(session, url, retries=4):
    _, body = _fetch(session, url, retries)
    try:
        return json.loads(body.decode("utf-8", errors="replace"))
    except ValueError:
        raise ScrapeError(
            "The store didn't return product data. It may not be a Shopify store, "
            "or its product feed is disabled."
        )


# --------------------------------------------------------------------------- products


def fetch_products(
    base,
    collection=None,
    page_from=1,
    page_to=None,
    delay=0.4,
    progress=None,
    should_stop=_never_stop,
    out=None,
):
    """Fetch every product, page by page, with no cap on the number of pages.

    Stops when a page comes back short, empty, or contains nothing new (some stores
    ignore the page parameter). `out` may be passed in so a caller keeps partial
    results if the run is stopped or fails part-way.
    """
    if collection and not HANDLE_RE.match(collection):
        raise ScrapeError("Invalid collection handle.")
    page = max(1, int(page_from or 1))
    prefix = f"{base}/collections/{collection}" if collection else base
    session = new_session()
    products = out if out is not None else []
    seen = {p.get("id") for p in products}

    while True:
        if should_stop():
            break
        if page_to and page > int(page_to):
            break
        data = _get_json(session, f"{prefix}/products.json?limit={PAGE_SIZE}&page={page}")
        items = data.get("products") if isinstance(data, dict) else None
        if not isinstance(items, list):
            raise ScrapeError("The store returned data in an unexpected format.")
        added = 0
        for p in items:
            pid = p.get("id")
            if pid not in seen:
                seen.add(pid)
                products.append(p)
                added += 1
        if progress:
            progress(page=page, count=len(products))
        if len(items) < PAGE_SIZE or added == 0:
            break
        page += 1
        _sleep(delay)
    return products


# --------------------------------------------------------------------------- collections


def fetch_collections(base, delay=0.3, should_stop=_never_stop):
    session = new_session()
    out, seen = [], set()
    page = 1
    while not should_stop():
        data = _get_json(session, f"{base}/collections.json?limit={PAGE_SIZE}&page={page}")
        items = data.get("collections") if isinstance(data, dict) else None
        if not isinstance(items, list):
            raise ScrapeError("The store didn't return a collection list.")
        added = 0
        for c in items:
            handle = c.get("handle")
            if handle and handle not in seen:
                seen.add(handle)
                added += 1
                out.append(
                    {
                        "handle": handle,
                        "title": c.get("title") or handle,
                        "products_count": c.get("products_count"),
                    }
                )
        if len(items) < PAGE_SIZE or added == 0:
            break
        page += 1
        _sleep(delay)
    return out


def fetch_collection_membership(base, progress=None, should_stop=_never_stop, delay=0.3):
    """Map product id -> list of {handle, title} for every collection that contains it.

    Returns (membership, warnings). A store that hides collections.json yields an empty map
    plus a warning rather than failing the whole run.
    """
    warnings = []
    try:
        collections = fetch_collections(base, delay=delay, should_stop=should_stop)
    except ScrapeError as e:
        return {}, [f"Collections unavailable: {e.message}"]

    membership = {}
    session = new_session()
    for idx, col in enumerate(collections, start=1):
        if should_stop():
            break
        if progress:
            progress(done=idx - 1, total=len(collections), title=col["title"])
        if not HANDLE_RE.match(col["handle"]):
            continue
        page = 1
        try:
            while not should_stop():
                url = f"{base}/collections/{col['handle']}/products.json?limit={PAGE_SIZE}&page={page}"
                data = _get_json(session, url)
                items = data.get("products") if isinstance(data, dict) else None
                if not isinstance(items, list) or not items:
                    break
                new = 0
                for p in items:
                    entry = {"handle": col["handle"], "title": col["title"]}
                    lst = membership.setdefault(p.get("id"), [])
                    if entry not in lst:
                        lst.append(entry)
                        new += 1
                if len(items) < PAGE_SIZE or new == 0:
                    break
                page += 1
                _sleep(delay)
        except ScrapeError as e:
            warnings.append(f"Collection '{col['title']}' skipped: {e.message}")
        _sleep(delay)
    if progress:
        progress(done=len(collections), total=len(collections), title="")
    return membership, warnings


# --------------------------------------------------------------------------- product details


class _PageMeta(HTMLParser):
    """Pulls SEO tags and JSON-LD blocks out of a product page."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.meta = {}
        self.canonical = ""
        self.json_ld = []
        self._in_title = False
        self._in_ld = False
        self._buf = []

    def handle_starttag(self, tag, attrs):
        a = {k: (v or "") for k, v in attrs}
        if tag == "title":
            self._in_title = True
        elif tag == "meta":
            key = (a.get("name") or a.get("property") or "").lower()
            if key and "content" in a and key not in self.meta:
                self.meta[key] = a["content"]
        elif tag == "link" and "canonical" in a.get("rel", "").lower().split():
            self.canonical = a.get("href", "")
        elif tag == "script" and a.get("type", "").lower() == "application/ld+json":
            self._in_ld = True
            self._buf = []

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        elif tag == "script" and self._in_ld:
            self._in_ld = False
            raw = "".join(self._buf).strip()
            try:
                self.json_ld.append(json.loads(raw))
            except ValueError:
                pass

    def handle_data(self, data):
        if self._in_title and not self.title:
            self.title = data.strip()
        elif self._in_ld:
            self._buf.append(data)


def _walk_ld(node):
    """Yield every dict inside a JSON-LD structure."""
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk_ld(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk_ld(v)


def _ld_types(obj):
    t = obj.get("@type")
    return [t] if isinstance(t, str) else [x for x in (t or []) if isinstance(x, str)]


def _ld_review(obj):
    """One schema.org Review as a plain dict (the same shape the WooCommerce reviews use)."""
    author = obj.get("author")
    name = author.get("name", "") if isinstance(author, dict) else (author or "")
    rating = obj.get("reviewRating")
    value = rating.get("ratingValue", "") if isinstance(rating, dict) else (rating or "")
    body = obj.get("reviewBody") or obj.get("description") or ""
    def plain(x):
        return re.sub(r"\s+", " ", html_lib.unescape(re.sub(r"<[^>]+>", " ", str(x)))).strip()

    return {
        "reviewer": plain(name),
        "rating": str(value).strip(),
        "date": str(obj.get("datePublished") or "").strip(),
        "title": plain(obj.get("name") or ""),
        "body": plain(body),
        "verified": "",
        "source": "structured data",
    }


def _parse_json_ld(blocks):
    info = {"brand": "", "currency": "", "rating_value": "", "review_count": "", "gtin_by_sku": {}, "reviews": []}
    seen_reviews = set()
    for block in blocks:
        for obj in _walk_ld(block):
            types = _ld_types(obj)
            if "Review" in types:
                review = _ld_review(obj)
                key = (review["reviewer"], review["date"], review["body"], review["title"])
                if (review["body"] or review["title"]) and key not in seen_reviews:
                    seen_reviews.add(key)
                    info["reviews"].append(review)
            if "Product" in types or "ProductGroup" in types:
                brand = obj.get("brand")
                if not info["brand"]:
                    info["brand"] = brand.get("name", "") if isinstance(brand, dict) else (brand or "")
                rating = obj.get("aggregateRating")
                if isinstance(rating, dict) and not info["rating_value"]:
                    info["rating_value"] = str(rating.get("ratingValue", ""))
                    info["review_count"] = str(rating.get("reviewCount") or rating.get("ratingCount") or "")
                gtin = next((obj[k] for k in ("gtin", "gtin13", "gtin12", "gtin14", "gtin8") if obj.get(k)), "")
                if gtin and obj.get("sku"):
                    info["gtin_by_sku"][str(obj["sku"])] = str(gtin)
            if "Offer" in types or "AggregateOffer" in types:
                if not info["currency"] and obj.get("priceCurrency"):
                    info["currency"] = obj["priceCurrency"]
                gtin = next((obj[k] for k in ("gtin", "gtin13", "gtin12", "gtin14", "gtin8") if obj.get(k)), "")
                if gtin and obj.get("sku"):
                    info["gtin_by_sku"][str(obj["sku"])] = str(gtin)
    return info


def _fetch_one_details(session, base, product, page_only=False):
    handle = product.get("handle")
    details = {}
    if not handle or not HANDLE_RE.match(handle):
        return details

    try:
        if page_only:
            raise ScrapeError("skipped")  # reviews only: the storefront .js adds nothing we need
        status, body = _fetch(session, f"{base}/products/{handle}.js", retries=3, allow_404=True)
        if status == 200:
            js = json.loads(body.decode("utf-8", errors="replace"))
            js.pop("description", None)
            details["storefront_js"] = js
    except (ScrapeError, ValueError) as e:
        if not page_only:
            details["js_error"] = getattr(e, "message", str(e))

    try:
        status, body = _fetch(session, f"{base}/products/{handle}", retries=3, allow_404=True)
        if status == 200:
            parser = _PageMeta()
            parser.feed(body.decode("utf-8", errors="replace"))
            meta = parser.meta
            details.update(
                {
                    "seo_title": parser.title,
                    "seo_description": meta.get("description", ""),
                    "canonical": parser.canonical,
                    "og_title": meta.get("og:title", ""),
                    "og_description": meta.get("og:description", ""),
                    "og_image": meta.get("og:image", ""),
                    "json_ld": parser.json_ld,
                }
            )
            details.update(_parse_json_ld(parser.json_ld))
    except ScrapeError as e:
        details["page_error"] = e.message
    return details


def fetch_details(base, products, workers=4, delay=0.15, progress=None, should_stop=_never_stop, page_only=False):
    """Fetch the product page and storefront JSON for each product (slower, optional).

    Results are stored in product["_details"]. Failures for a single product are recorded
    on that product and never stop the run.
    """
    local = threading.local()
    lock = threading.Lock()
    state = {"done": 0}
    total = len(products)

    def work(p):
        if should_stop():
            return
        if not hasattr(local, "session"):
            local.session = new_session("text/html,application/json")
        try:
            p["_details"] = _fetch_one_details(local.session, base, p, page_only)
        except ScrapeError as e:
            p["_details"] = {"error": e.message}
        _sleep(delay)
        with lock:
            state["done"] += 1
            if progress:
                progress(done=state["done"], total=total)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(work, products))


# --------------------------------------------------------------------------- specific products, keyword search


def parse_handles(raw):
    """Product links, handles or slugs (one per line or comma separated) -> a list of unique handles.

    Accepts https://store.com/products/some-handle, https://store.com/product/some-slug/ (WooCommerce),
    or just the handle itself.
    """
    out = []
    for token in re.split(r"[\s,]+", raw or ""):
        token = token.strip()
        if not token:
            continue
        path = urlparse(token if "://" in token else "//" + token).path if ("/" in token or "://" in token) else token
        m = re.search(r"/products?/([^/?#]+)", path)
        handle = m.group(1) if m else path.strip("/")
        if handle.lower().endswith((".json", ".js")):
            handle = handle.rsplit(".", 1)[0]
        if HANDLE_RE.match(handle) and handle not in out:
            out.append(handle)
    return out


def fetch_products_by_handle(base, handles, delay=0.2, progress=None, should_stop=_never_stop, out=None):
    """Fetch specific products by handle. Returns (products, missing_handles)."""
    session = new_session()
    products = out if out is not None else []
    seen = {p.get("id") for p in products}
    missing = []
    for i, handle in enumerate(handles, start=1):
        if should_stop():
            break
        status, body = _fetch(session, f"{base}/products/{handle}.json", allow_404=True)
        item = None
        if status == 200:
            try:
                item = json.loads(body.decode("utf-8", errors="replace")).get("product")
            except (ValueError, AttributeError):
                item = None
        if isinstance(item, dict) and item.get("id") not in seen:
            seen.add(item.get("id"))
            products.append(item)
        elif not isinstance(item, dict):
            missing.append(handle)
        if progress:
            progress(page=i, count=len(products))
        _sleep(delay)
    return products, missing


def product_matches(product, query):
    """Keyword search: every word must appear in the title, handle, vendor, type, tags or a SKU."""
    words = (query or "").lower().split()
    if not words:
        return True
    tags = product.get("tags") or []
    if isinstance(tags, str):
        tags = [tags]
    hay = " ".join(
        [str(product.get(k) or "") for k in ("title", "handle", "vendor", "product_type")]
        + [str(t) for t in tags]
        + [str(v.get("sku") or "") for v in product.get("variants") or []]
    ).lower()
    return all(w in hay for w in words)
