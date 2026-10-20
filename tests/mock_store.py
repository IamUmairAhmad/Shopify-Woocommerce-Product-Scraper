"""A fake Shopify storefront for testing the exporter offline.

Serves products.json, collections.json, per-collection feeds, product pages with SEO tags and
JSON-LD, and /products/<handle>.js. The catalog is large so tests prove there is no page cap.
"""

import json
import os
import sys

from flask import Flask, abort, jsonify, request

app = Flask(__name__)
N_PRODUCTS = int(os.environ.get("MOCK_PRODUCTS", "10500"))  # 42 pages of 250: past the old 40-page cap


def make_products():
    products = []
    for i in range(1, N_PRODUCTS + 1):
        if i % 3 == 0:
            products.append(
                {
                    "id": 1000 + i,
                    "title": f'Runner {i} "Pro", Edition',
                    "handle": f"runner-{i}",
                    "body_html": "<p>Fast, <b>light</b> shoe.</p><ul><li>Mesh upper</li><li>Foam sole</li></ul>",
                    "vendor": "Acme",
                    "product_type": "Shoes",
                    "tags": ["running", "summer"],
                    "published_at": "2026-01-02T10:00:00-05:00",
                    "created_at": "2026-01-01T10:00:00-05:00",
                    "updated_at": "2026-03-01T10:00:00-05:00",
                    "options": [
                        {"name": "Size", "position": 1, "values": ["8", "9"]},
                        {"name": "Color", "position": 2, "values": ["Black", "Red"]},
                    ],
                    "variants": [
                        {"id": i * 10 + 1, "title": "8 / Black", "option1": "8", "option2": "Black", "sku": f"R{i}-8B",
                         "price": "89.00", "compare_at_price": "120.00", "grams": 800, "available": True,
                         "requires_shipping": True, "taxable": True, "position": 1,
                         "featured_image": {"src": f"https://cdn.example.com/r{i}-black.jpg"}},
                        {"id": i * 10 + 2, "title": "9 / Black", "option1": "9", "option2": "Black", "sku": f"R{i}-8B",
                         "price": "89.00", "compare_at_price": None, "grams": 800, "available": False,
                         "requires_shipping": True, "taxable": True, "position": 2},
                        {"id": i * 10 + 3, "title": "9 / Red", "option1": "9", "option2": "Red", "sku": "",
                         "price": "95.50", "compare_at_price": None, "grams": 0, "available": True,
                         "requires_shipping": True, "taxable": True, "position": 3},
                    ],
                    "images": [
                        {"position": 2, "src": f"https://cdn.example.com/r{i}-b.jpg"},
                        {"position": 1, "src": f"https://cdn.example.com/r{i}-a.jpg", "alt": "Front"},
                    ],
                }
            )
        else:
            products.append(
                {
                    "id": 1000 + i,
                    "title": f"Sock {i}",
                    "handle": f"sock-{i}",
                    "body_html": "Warm socks.",
                    "vendor": "Acme",
                    "product_type": "Accessories",
                    "tags": "wool, winter",  # some stores return tags as a string
                    "published_at": "2026-01-02T10:00:00-05:00",
                    "created_at": "2026-01-01T10:00:00-05:00",
                    "updated_at": "2026-03-01T10:00:00-05:00",
                    "options": [{"name": "Title", "position": 1, "values": ["Default Title"]}],
                    "variants": [
                        {"id": 5_000_000 + i, "title": "Default Title", "option1": "Default Title",
                         "sku": f"S{i}", "price": "9.99", "compare_at_price": None, "grams": 100,
                         "available": True, "requires_shipping": True, "taxable": True, "position": 1}
                    ],
                    "images": [{"position": 1, "src": f"https://cdn.example.com/s{i}.jpg"}],
                }
            )
    return products


PRODUCTS = make_products()
BY_HANDLE = {p["handle"]: p for p in PRODUCTS}
SHOES = [p for p in PRODUCTS if p["product_type"] == "Shoes"]
SOCKS = [p for p in PRODUCTS if p["product_type"] == "Accessories"]
SALE = [p for p in PRODUCTS if any(v.get("compare_at_price") for v in p["variants"])]
COLLECTIONS = {
    "shoes": ("Shoes", SHOES),
    "socks": ("Socks & Accessories", SOCKS),
    "sale": ("Sale", SALE),
}


def page(items):
    limit = min(int(request.args.get("limit", 30)), 250)
    pg = max(int(request.args.get("page", 1)), 1)
    return jsonify({"products": items[(pg - 1) * limit : pg * limit]})


@app.get("/products.json")
def all_products():
    return page(PRODUCTS)


@app.get("/collections/<handle>/products.json")
def collection_products(handle):
    if handle in COLLECTIONS:
        return page(COLLECTIONS[handle][1])
    return ("not found", 404)


@app.get("/collections.json")
def collections():
    if int(request.args.get("page", 1)) > 1:
        return jsonify({"collections": []})
    return jsonify(
        {"collections": [
            {"handle": h, "title": t, "products_count": len(items)} for h, (t, items) in COLLECTIONS.items()
        ]}
    )


@app.get("/products/<handle>.json")
def product_json(handle):
    p = BY_HANDLE.get(handle)
    if not p:
        abort(404)
    return jsonify({"product": p})


@app.get("/products/<handle>.js")
def product_js(handle):
    p = BY_HANDLE.get(handle)
    if not p:
        abort(404)
    return jsonify(
        {
            "id": p["id"], "title": p["title"], "handle": handle, "description": p["body_html"],
            "requires_selling_plan": False, "selling_plan_groups": [],
            "variants": [{"id": v["id"], "sku": v["sku"], "barcode": f"BC{v['id']}" if v["id"] % 2 else ""}
                         for v in p["variants"]],
            "media": [
                {"media_type": "image", "src": p["images"][0]["src"]},
                {"media_type": "video", "sources": [{"url": f"https://cdn.example.com/{handle}.mp4", "format": "mp4"}]},
                {"media_type": "external_video", "host": "youtube", "external_id": "abc123"},
            ],
        }
    )


@app.get("/products/<handle>")
def product_page(handle):
    p = BY_HANDLE.get(handle)
    if not p or p["id"] % 50 == 0:  # every 50th product page is missing, to test non-fatal failures
        abort(404)
    ld = {
        "@context": "http://schema.org/",
        "@type": "Product",
        "name": p["title"],
        "sku": p["variants"][0]["sku"],
        "gtin13": "0123456789012",
        "brand": {"@type": "Brand", "name": "Acme Brand"},
        "aggregateRating": {"@type": "AggregateRating", "ratingValue": "4.6", "reviewCount": "27"},
        "offers": [{"@type": "Offer", "price": p["variants"][0]["price"], "priceCurrency": "USD",
                    "sku": p["variants"][0]["sku"], "gtin13": "0123456789012"}],
    }
    if p["id"] % 7 == 0:  # some products publish a few reviews as structured data
        ld["review"] = [
            {"@type": "Review", "author": {"@type": "Person", "name": "Sam"}, "datePublished": "2026-02-01",
             "name": "Love them", "reviewBody": "Fits well &amp; looks great.",
             "reviewRating": {"@type": "Rating", "ratingValue": "5"}},
            {"@type": "Review", "author": "Alex", "datePublished": "2026-02-02",
             "reviewBody": "Okay.", "reviewRating": {"@type": "Rating", "ratingValue": "3"}},
        ]
    return (
        "<!doctype html><html><head>"
        f"<title>{p['title'].replace('<', '&lt;')} – Mock Store</title>"
        f'<meta name="description" content="Buy {handle} today">'
        f'<link rel="canonical" href="http://127.0.0.1/products/{handle}">'
        f'<meta property="og:image" content="{p["images"][0]["src"]}">'
        '<meta property="og:title" content="OG title">'
        f'<script type="application/ld+json">{json.dumps(ld)}</script>'
        "</head><body>product</body></html>"
    )


if __name__ == "__main__":
    app.run(port=int(sys.argv[1]) if len(sys.argv) > 1 else 5055, threaded=True)
