"""A fake WooCommerce store for testing the exporter offline.

Mimics the public Store API (pagination headers, prices in minor units, variations, categories)
and product pages with SEO tags, JSON-LD and the weight / dimensions markup.

Usage: python mock_woo.py <port>

Environment:
  MOCK_MODE             pretty (default) | plain (only /?rest_route= works) | blocked | none
  MOCK_BULK_VARIATIONS  1 (default) | 0   -> type=variation&parent=... answers 400
  MOCK_MAX_PER_PAGE     100 (default)     -> server-side cap on per_page
  MOCK_SIMPLE           230               -> number of simple products
"""

import html
import json
import math
import os
import re
import sys

from flask import Flask, abort, jsonify, request

app = Flask(__name__)
MODE = os.environ.get("MOCK_MODE", "pretty")
BULK = os.environ.get("MOCK_BULK_VARIATIONS", "1") == "1"
MAX_PER_PAGE = int(os.environ.get("MOCK_MAX_PER_PAGE", "100"))
N_SIMPLE = int(os.environ.get("MOCK_SIMPLE", "230"))
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 5056
SITE = f"http://127.0.0.1:{PORT}"

CATEGORIES = [
    {"id": 15, "name": "Clothing", "slug": "clothing", "parent": 0},
    {"id": 16, "name": "Hoodies &amp; Tops", "slug": "hoodies", "parent": 15},
    {"id": 17, "name": "Accessories", "slug": "accessories", "parent": 0},
]


def cat(cid):
    c = next(c for c in CATEGORIES if c["id"] == cid)
    return {"id": c["id"], "name": c["name"], "slug": c["slug"], "link": f"{SITE}/product-category/{c['slug']}/"}


def prices(price, regular=None, price_range=None):
    regular = price if regular is None else regular
    return {
        "price": str(price), "regular_price": str(regular), "sale_price": str(price),
        "price_range": price_range, "currency_code": "USD", "currency_symbol": "$",
        "currency_minor_unit": 2, "currency_decimal_separator": ".", "currency_thousand_separator": ",",
        "currency_prefix": "$", "currency_suffix": "",
    }


def image(iid, name):
    return {
        "id": iid, "src": f"https://cdn.example.com/{name}.jpg", "thumbnail": f"https://cdn.example.com/{name}-300.jpg",
        "srcset": "x 1x", "sizes": "100vw", "name": f"{name}.jpg", "alt": "",
    }


def product(pid, name, slug, ptype="simple", cats=(17,), sku="", price=1800, regular=None, images=None, **extra):
    regular = price if regular is None else regular
    p = {
        "id": pid, "name": name, "slug": slug, "parent": 0, "type": ptype, "variation": "",
        "permalink": f"{SITE}/product/{slug}/", "sku": sku,
        "short_description": f"<p>Short text for {slug}.</p>", "description": f"<p>Long text for {slug}.</p>",
        "on_sale": regular > price, "prices": prices(price, regular), "price_html": "<span>price</span>",
        "average_rating": "4.50" if pid % 7 == 0 else "0", "review_count": 3 if pid % 7 == 0 else 0,
        "images": images if images is not None else [image(pid, slug)],
        "categories": [cat(c) for c in cats],
        "tags": [{"id": 1, "name": "Winter", "slug": "winter", "link": ""}] if pid % 3 == 0 else [],
        "brands": [{"id": 9, "name": "Acme", "slug": "acme", "link": ""}] if pid % 2 == 0 else [],
        "attributes": [], "variations": [], "grouped_products": [], "has_options": ptype == "variable",
        "is_purchasable": True, "is_in_stock": True, "is_on_backorder": False, "low_stock_remaining": None,
        "sold_individually": False,
        "add_to_cart": {"text": "Add to cart", "description": "Add", "url": f"{SITE}/?add-to-cart={pid}",
                        "minimum": 1, "maximum": 9999, "multiple_of": 1},
        "extensions": {},
    }
    p.update(extra)
    return p


PRODUCTS, VARIATIONS = [], []

# Simple products
for i in range(1, N_SIMPLE + 1):
    sale = i % 5 == 0
    PRODUCTS.append(
        product(100 + i, f"Beanie {i} &amp; Co", f"beanie-{i}", sku=f"BEAN-{i}",
                price=1500 if sale else 1800, regular=1800, cats=(17,))
    )
PRODUCTS[2]["is_in_stock"] = False  # beanie-3 is out of stock
PRODUCTS[3]["is_on_backorder"] = True  # beanie-4 allows backorders
PRODUCTS[4]["sold_individually"] = True  # beanie-5 is sold individually

# Variable products: a non-variation attribute first, two variation attributes, one "any colour" variation
COLORS = [
    {"id": 31, "name": "Blue", "slug": "blue", "default": True},
    {"id": 32, "name": "Green", "slug": "green", "default": False},
    {"id": 33, "name": "Red", "slug": "red", "default": False},
]
SIZES = [
    {"id": 41, "name": "Small", "slug": "small", "default": False},
    {"id": 42, "name": "Medium", "slug": "medium", "default": False},
]
VARIATION_DEFS = [("blue", "small"), ("blue", "medium"), ("green", "small"), ("", "medium")]
WEIGHTS = ["0.8 kg", "2 lbs", "N/A", "1,250 g"]
DIMENSIONS = ["30 &times; 20 &times; 10 cm", "10 &times; 8 &times; 4 in", "N/A", "N/A"]
VARIATION_DATA = {}  # slug -> list for the product page

for j in range(1, 11):
    pid, slug = 1000 + j, f"hoodie-{j}"
    listing, page_data = [], []
    for k, (color, size) in enumerate(VARIATION_DEFS):
        vid = 5000 + j * 10 + k
        price = 2500 if k == 0 else 3000 + k * 100
        regular = 3000 if k == 0 else price
        v = product(
            vid, f"Hoodie {j}", f"hoodie-{j}-{k}", ptype="variation", cats=(15, 16), sku=f"HOOD-{j}-{k}",
            price=price, regular=regular,
            images=[image(vid, f"hoodie-{j}-blue")] if k == 0 else [image(pid, slug)],
            parent=pid, variation=f"Color: {color}, Size: {size}",
            # Deliberately not the same shape as the parent's listing; the exporter must not depend on it.
            attributes=[{"id": 1, "name": "Color", "taxonomy": "pa_color", "has_variations": False,
                         "terms": [{"id": 0, "name": color, "slug": color}]}],
        )
        v["is_in_stock"] = k != 2  # third variation is out of stock
        VARIATIONS.append(v)
        listing.append({"id": vid, "attributes": [{"name": "Color", "value": color}, {"name": "Size", "value": size}]})
        page_data.append({"variation_id": vid, "weight_html": WEIGHTS[k], "dimensions_html": DIMENSIONS[k]})
    PRODUCTS.append(
        product(
            pid, f"Hoodie {j}", slug, ptype="variable", cats=(15, 16), sku="" if j % 2 else f"HOOD-{j}",
            price=2500, regular=3000, variations=listing,
            prices=prices(2500, 3000, {"min_amount": "2500", "max_amount": "3300"}),
            attributes=[
                {"id": 0, "name": "Material", "taxonomy": "", "has_variations": False,
                 "terms": [{"id": 0, "name": "Cotton", "slug": "cotton", "default": False}]},
                {"id": 1, "name": "Color", "taxonomy": "pa_color", "has_variations": True, "terms": COLORS},
                {"id": 2, "name": "Size", "taxonomy": "pa_size", "has_variations": True, "terms": SIZES},
            ],
        )
    )
    VARIATION_DATA[slug] = page_data

# External and grouped products
for n in range(1, 4):
    PRODUCTS.append(
        product(2000 + n, f"Affiliate Item {n}", f"affiliate-{n}", ptype="external", price=4000,
                add_to_cart={"text": "Buy on Amazon", "description": "Buy", "url": f"https://amazon.example/dp/X{n}",
                             "minimum": 1, "maximum": 1, "multiple_of": 1})
    )
for n in range(1, 3):
    PRODUCTS.append(
        product(3000 + n, f"Gift Set {n}", f"gift-set-{n}", ptype="grouped", price=0, grouped_products=[101, 102])
    )

BY_SLUG = {p["slug"]: p for p in PRODUCTS}
BY_ID = {p["id"]: p for p in PRODUCTS + VARIATIONS}


# --------------------------------------------------------------------------- REST API


def gate():
    if MODE == "blocked":
        return jsonify({"code": "rest_login_required", "message": "Only authenticated users can access the REST API.",
                        "data": {"status": 401}}), 401
    return None


def paged(items):
    args = request.args
    per_page = max(1, min(int(args.get("per_page", 10)), MAX_PER_PAGE))
    page = max(1, int(args.get("page", 1)))
    pages = max(1, math.ceil(len(items) / per_page))
    if page > pages:
        return jsonify({"code": "rest_post_invalid_page_number",
                        "message": "The page number requested is larger than the number of pages available.",
                        "data": {"status": 400}}), 400
    resp = jsonify(items[(page - 1) * per_page: page * per_page])
    resp.headers["X-WP-Total"] = str(len(items))
    resp.headers["X-WP-TotalPages"] = str(pages)
    return resp


def list_products():
    args = request.args
    if args.get("type") == "variation":
        if not BULK:
            return jsonify({"code": "rest_invalid_param", "message": "Invalid parameter(s): type",
                            "data": {"status": 400}}), 400
        parent = int(args.get("parent", 0))
        return paged([v for v in VARIATIONS if v["parent"] == parent])
    items = PRODUCTS
    if args.get("category"):
        cid = int(args["category"])
        ids = {cid} | {c["id"] for c in CATEGORIES if c["parent"] == cid}
        items = [p for p in items if any(c["id"] in ids for c in p["categories"])]
    return paged(items)


def list_categories():
    rows = []
    for c in CATEGORIES:
        count = sum(1 for p in PRODUCTS if any(x["id"] == c["id"] for x in p["categories"]))
        rows.append({"id": c["id"], "name": c["name"], "slug": c["slug"], "parent": c["parent"], "count": count})
    return paged(rows)


def single(pid):
    item = BY_ID.get(pid)
    if not item:
        return jsonify({"code": "woocommerce_rest_product_invalid_id", "message": "Invalid product ID.",
                        "data": {"status": 404}}), 404
    return jsonify(item)


@app.before_request
def _everything_404_when_none():
    if MODE == "none":
        abort(404)


@app.get("/products.json")
def no_shopify_feed():
    abort(404)


@app.get("/wp-json/wc/store/v1/products")
def r_products():
    if MODE == "plain":
        abort(404)
    return gate() or list_products()


@app.get("/wp-json/wc/store/v1/products/categories")
def r_categories():
    if MODE == "plain":
        abort(404)
    return gate() or list_categories()


@app.get("/wp-json/wc/store/v1/products/<int:pid>")
def r_single(pid):
    if MODE == "plain":
        abort(404)
    return gate() or single(pid)


@app.get("/")
def home():
    route = request.args.get("rest_route")
    if route:
        blocked = gate()
        if blocked:
            return blocked
        if route == "/wc/store/v1/products":
            return list_products()
        if route == "/wc/store/v1/products/categories":
            return list_categories()
        m = re.fullmatch(r"/wc/store/v1/products/(\d+)", route)
        if m:
            return single(int(m.group(1)))
        return jsonify({"code": "rest_no_route", "message": "No route was found.", "data": {"status": 404}}), 404
    root = f"{SITE}/?rest_route=/" if MODE == "plain" else f"{SITE}/wp-json/"
    return (f'<!doctype html><html><head><title>Mock Woo</title>'
            f'<link rel="https://api.w.org/" href="{root}" /></head><body>store</body></html>')


# --------------------------------------------------------------------------- product pages


@app.get("/product/<slug>/")
def product_page(slug):
    p = BY_SLUG.get(slug)
    if not p or (p["type"] == "simple" and p["id"] % 50 == 0):  # some pages are missing on purpose
        abort(404)
    ld = {
        "@context": "https://schema.org/",
        "@graph": [{
            "@type": "Product", "name": p["name"], "sku": p["sku"] or slug, "gtin13": "0123456789012",
            "brand": {"@type": "Brand", "name": "Acme Brand"},
            "aggregateRating": {"@type": "AggregateRating", "ratingValue": "4.5", "reviewCount": "3"},
            "offers": [{"@type": "Offer", "price": "18.00", "priceCurrency": "USD", "sku": p["sku"] or slug,
                        "gtin13": "0123456789012"}],
        }],
    }
    rows = ""
    form = ""
    if p["type"] == "variable":
        data = html.escape(json.dumps(VARIATION_DATA[slug]), quote=True)
        form = f'<form class="variations_form cart" data-product_id="{p["id"]}" data-product_variations="{data}"></form>'
    else:
        rows = (
            '<table class="woocommerce-product-attributes shop_attributes">'
            '<tr class="woocommerce-product-attributes-item woocommerce-product-attributes-item--weight">'
            '<th class="woocommerce-product-attributes-item__label">Weight</th>'
            '<td class="woocommerce-product-attributes-item__value">0.4 kg</td></tr>'
            '<tr class="woocommerce-product-attributes-item woocommerce-product-attributes-item--dimensions">'
            '<th class="woocommerce-product-attributes-item__label">Dimensions</th>'
            '<td class="woocommerce-product-attributes-item__value">20 &times; 10 &times; 5 cm</td></tr>'
            '</table>'
        )
    return (
        "<!doctype html><html><head>"
        f"<title>SEO title for {slug} – Mock Woo</title>"
        f'<meta name="description" content="SEO description for {slug}">'
        f'<link rel="canonical" href="{SITE}/product/{slug}/">'
        f'<meta property="og:image" content="{p["images"][0]["src"]}">'
        f'<meta property="og:title" content="OG title {slug}">'
        f'<script type="application/ld+json">{json.dumps(ld)}</script>'
        f"</head><body>{form}{rows}</body></html>"
    )


if __name__ == "__main__":
    app.run(port=PORT, threaded=True)
