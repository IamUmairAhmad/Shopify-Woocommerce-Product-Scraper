"""End-to-end test: exporter app -> mock Shopify store. Run with `python tests/test_app.py`."""

import csv
import io
import os
import subprocess
import sys
import time

os.environ["ALLOW_PRIVATE_HOSTS"] = "1"
os.environ["SCRAPE_DELAY_SCALE"] = "0"
os.environ["NO_PROXY"] = "127.0.0.1,localhost"
os.environ["no_proxy"] = "127.0.0.1,localhost"
os.environ["RATE_LIMIT_PER_MINUTE"] = "1000"

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import requests  # noqa: E402

import app as exporter  # noqa: E402
import exporters  # noqa: E402
import scraper  # noqa: E402

PORT = 5055
STORE = f"http://127.0.0.1:{PORT}"
N = 10500
failures = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        failures.append(name)


def run_job(client, payload, timeout=240):
    r = client.post("/api/search", json=payload)
    if r.status_code != 200:
        return r, None
    job_id = r.json["job_id"]
    deadline = time.time() + timeout
    while time.time() < deadline:
        j = client.get(f"/api/job/{job_id}").json
        if j["status"] != "running":
            return r, j
        time.sleep(0.05)
    raise TimeoutError("job did not finish")


def rows_of(resp):
    return list(csv.DictReader(io.StringIO(resp.get_data(as_text=True))))


def unit_checks():
    """Offline checks that need no mock store."""
    parse = scraper.parse_store_input
    check("url: double slash", parse("https://myevostore.com//collections/jackets") == ("https://myevostore.com", "jackets"))
    check("url: language prefix", parse("https://myevostore.com/en/collections/jackets") == ("https://myevostore.com", "jackets"))
    check("url: regional language prefix + query", parse("https://shop.com/en-gb/collections/new-in?page=2") == ("https://shop.com", "new-in"))
    check("url: product inside a collection", parse("shop.com/collections/jackets/products/x") == ("https://shop.com", "jackets"))
    check("url: not a collection path", parse("https://shop.com/pages/collections/jackets") == ("https://shop.com", None))
    check("url: bare domain", parse("shop.com") == ("https://shop.com", None))

    # Variant images: from featured_image, or only from images[].variant_ids.
    product = {
        "id": 1, "handle": "x", "title": "X", "options": [{"name": "Size", "values": ["S", "M", "L"]}],
        "images": [
            {"src": "https://cdn/a.jpg", "position": 1},
            {"src": "https://cdn/b.jpg", "position": 2, "variant_ids": [11]},
            {"src": "https://cdn/c.jpg", "position": 3, "variant_ids": [12]},
        ],
        "variants": [
            {"id": 11, "option1": "S", "price": "1.00", "featured_image": None},
            {"id": 12, "option1": "M", "price": "1.00", "featured_image": {"src": "https://cdn/own.jpg"}},
            {"id": 13, "option1": "L", "price": "1.00"},
        ],
    }
    shop = list(csv.DictReader(io.StringIO(exporters.to_shopify_csv([product]))))
    check("variant image: taken from images[].variant_ids when featured_image is null", shop[0]["Variant Image"] == "https://cdn/b.jpg", shop[0]["Variant Image"])
    check("variant image: featured_image takes priority", shop[1]["Variant Image"] == "https://cdn/own.jpg")
    check("variant image: blank when the variant has none", shop[2]["Variant Image"] == "")
    full = list(csv.DictReader(io.StringIO(exporters.to_full_csv([product], "https://x.com"))))
    check("variant image: full-details CSV uses the fallback", full[0]["Variant Image"] == "https://cdn/b.jpg")
    woo = list(csv.DictReader(io.StringIO(exporters.to_woocommerce_csv([product]))))
    check("variant image: WooCommerce variation uses the fallback", woo[1]["Images"] == "https://cdn/b.jpg", woo[1]["Images"])


def main():
    unit_checks()
    proc = subprocess.Popen([sys.executable, os.path.join(HERE, "mock_store.py"), str(PORT)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(100):
            try:
                requests.get(STORE + "/collections.json", timeout=1)
                break
            except requests.exceptions.RequestException:
                time.sleep(0.1)

        c = exporter.app.test_client()

        # ---- collections list
        r = c.get("/api/collections", query_string={"store": STORE})
        check("collections endpoint", r.status_code == 200 and len(r.json["collections"]) == 3, r.get_data(as_text=True))

        # ---- unlimited scrape: 10,500 products = 42 pages, beyond the old 40-page cap
        t0 = time.time()
        _, j = run_job(c, {"store": STORE, "scope": "all", "include_collections": True})
        check("job completes", j and j["status"] == "done", str(j and j.get("error")))
        res = j["result"]
        check(f"no limit: all {N} products fetched", res["count"] == N and len(res["products"]) == N, str(res["count"]))
        print(f"     ({N} products + collections in {time.time() - t0:.1f}s)")
        token = res["token"]
        s = {p["id"]: p for p in res["products"]}
        check("summary price range", s[1003]["price_min"] == 89.0 and s[1003]["price_max"] == 95.5)
        check("collections mapped (runner: Shoes + Sale)", sorted(s[1003]["collections"]) == ["Sale", "Shoes"], str(s[1003]["collections"]))
        check("collections mapped (sock: Socks & Accessories)", s[1001]["collections"] == ["Socks & Accessories"], str(s[1001]["collections"]))
        check("no deep fetch when not requested", res["has_details"] is False)

        # ---- other scopes
        _, j = run_job(c, {"store": STORE, "scope": "collection", "collection": "shoes", "include_collections": False})
        check("collection scope", j["status"] == "done" and j["result"]["count"] == N // 3)
        _, j = run_job(c, {"store": STORE + "/collections/shoes", "scope": "all", "include_collections": False})
        check("collection auto-detected from URL", j["status"] == "done" and j["result"]["count"] == N // 3)
        _, j = run_job(c, {"store": STORE + "//collections/shoes", "scope": "all", "include_collections": False})
        check("collection detected from double-slash URL", j["status"] == "done" and j["result"]["count"] == N // 3,
              str(j["result"]["count"] if j.get("result") else j))
        check("double-slash URL parses to the collection handle",
              scraper.parse_store_input("https://myevostore.com//collections/jackets") == ("https://myevostore.com", "jackets"))
        _, j = run_job(c, {"store": STORE, "scope": "pages", "page_from": 2, "page_to": 3, "include_collections": False})
        check("page range 2-3 = 500 products", j["status"] == "done" and j["result"]["count"] == 500)

        # ---- errors
        _, j = run_job(c, {"store": STORE, "scope": "collection", "collection": "nope"})
        check("missing collection -> job error", j["status"] == "error" and "error" in j, str(j))
        r, _ = run_job(c, {"store": "", "scope": "all"})
        check("empty store rejected immediately", r.status_code == 400)
        r, _ = run_job(c, {"store": STORE, "scope": "pages", "page_from": 5, "page_to": 2})
        check("bad page range rejected", r.status_code == 400)
        check("unknown job -> 404", c.get("/api/job/nope").status_code == 404)

        # ---- stop button keeps partial results
        scraper.DELAY_SCALE = 1.0
        r = c.post("/api/search", json={"store": STORE, "scope": "all", "include_collections": False})
        job_id = r.json["job_id"]
        for _ in range(200):
            if (c.get(f"/api/job/{job_id}").json["progress"].get("pages") or 0) >= 2:
                break
            time.sleep(0.05)
        c.post(f"/api/job/{job_id}/stop")
        for _ in range(200):
            j = c.get(f"/api/job/{job_id}").json
            if j["status"] != "running":
                break
            time.sleep(0.05)
        scraper.DELAY_SCALE = 0.0
        check("stop keeps partial results", j["status"] == "done" and j["result"]["stopped_early"]
              and 0 < j["result"]["count"] < N, str(j["result"]["count"]))

        # ---- deep details on page 1 (250 products)
        _, j = run_job(c, {"store": STORE, "scope": "pages", "page_from": 1, "page_to": 1,
                           "include_collections": True, "deep": True})
        check("deep job completes", j["status"] == "done" and j["result"]["has_details"], str(j.get("error")))
        dtoken = j["result"]["token"]

        # ---- Full details CSV
        r = c.post("/api/export", json={"token": dtoken, "format": "full", "ids": [1003, 1001, 1050]})
        check("full export ok", r.status_code == 200 and "text/csv" in r.content_type)
        rows = rows_of(r)
        runner = [x for x in rows if x["Handle"] == "runner-3"]
        sock1 = [x for x in rows if x["Handle"] == "sock-1"]
        sock50 = [x for x in rows if x["Handle"] == "sock-50"]
        check("full: one row per variant", len(runner) == 3 and len(sock1) == 1)
        check("full: collections", runner[0]["Collections"] == "Shoes | Sale" or runner[0]["Collections"] == "Sale | Shoes", runner[0]["Collections"])
        check("full: description text extracted", "Fast, light shoe." in runner[0]["Description (text)"] and "Mesh upper" in runner[0]["Description (text)"])
        check("full: prices + on sale", runner[0]["Price"] == "89.00" and runner[0]["Compare At Price"] == "120.00" and runner[0]["On Sale"] == "TRUE" and runner[1]["On Sale"] == "FALSE")
        check("full: product price range", runner[0]["Product Min Price"] == "89.00" and runner[0]["Product Max Price"] == "95.50")
        check("full: availability per variant", runner[0]["Available"] == "TRUE" and runner[1]["Available"] == "FALSE")
        check("full: SEO from product page", runner[0]["SEO Title"].startswith("Runner 3") and runner[0]["SEO Description"] == "Buy runner-3 today")
        check("full: canonical + og image", runner[0]["Canonical URL"].endswith("/products/runner-3") and runner[0]["OG Image"].endswith("r3-b.jpg"))  # mock serves images[0] as og:image
        check("full: structured data (brand, rating, currency)",
              runner[0]["Brand (structured data)"] == "Acme Brand" and runner[0]["Rating"] == "4.6"
              and runner[0]["Review Count"] == "27" and runner[0]["Currency"] == "USD")
        check("full: barcode from .js", runner[0]["Barcode / GTIN"] == "BC31", runner[0]["Barcode / GTIN"])  # variant id 31 (odd)
        check("full: videos from .js", "runner-3.mp4" in runner[0]["Video / 3D Media URLs"] and "youtube.com/watch?v=abc123" in runner[0]["Video / 3D Media URLs"])
        check("full: images + alt text", runner[0]["Image URLs"].startswith("https://cdn.example.com/r3-a.jpg | ") and runner[0]["Image Alt Texts"].startswith("Front"))
        check("full: dates and ids", runner[0]["Published At"].startswith("2026-01-02") and runner[0]["Variant ID"] == "31")
        check("full: product URL", runner[0]["Product URL"] == f"{STORE}/products/runner-3")
        check("full: missing product page is non-fatal", len(sock50) == 1 and sock50[0]["SEO Title"] == "" and sock50[0]["Price"] == "9.99")

        # ---- Shopify CSV
        r = c.post("/api/export", json={"token": dtoken, "format": "shopify", "ids": [1003, 1001]})
        rows = rows_of(r)
        runner = [x for x in rows if x["Handle"] == "runner-3"]
        sock = [x for x in rows if x["Handle"] == "sock-1"]
        check("shopify: runner has 3 variant rows", len(runner) == 3)
        check("shopify: title with quotes/commas round-trips", runner[0]["Title"] == 'Runner 3 "Pro", Edition')
        check("shopify: option names only on first row",
              runner[0]["Option1 Name"] == "Size" and runner[1]["Option1 Name"] == "" and runner[1]["Option1 Value"] == "9")
        check("shopify: images sorted by position", runner[0]["Image Src"].endswith("r3-a.jpg") and runner[0]["Image Position"] == "1")
        check("shopify: tags from list and string", runner[0]["Tags"] == "running, summer" and sock[0]["Tags"] == "wool, winter")
        check("shopify: default-title product is one row", len(sock) == 1 and sock[0]["Option1 Name"] == "Title")
        header = list(csv.reader(io.StringIO(r.get_data(as_text=True))))[0]
        check("shopify: template columns present (Google Shopping, tax code, cost)",
              header[header.index("SEO Description") + 1] == "Google Shopping / Google Product Category"
              and header.index("Google Shopping / Custom Label 4") < header.index("Variant Image")
              and header[-3:] == ["Variant Tax Code", "Cost per item", "Status"], str(header[-5:]))
        check("shopify: SEO + barcode filled in deep mode",
              runner[0]["SEO Description"] == "Buy runner-3 today" and runner[0]["Variant Barcode"] == "BC31")

        # ---- WooCommerce CSV
        r = c.post("/api/export", json={"token": dtoken, "format": "woocommerce", "ids": [1003, 1001]})
        rows = rows_of(r)
        types = [x["Type"] for x in rows]
        check("woo: simple + variable + 3 variations", types == ["simple", "variable", "variation", "variation", "variation"], str(types))
        simple, parent, v1, v2, v3 = rows
        check("woo: categories = type + collections", set(parent["Categories"].split(", ")) == {"Shoes", "Sale"} and simple["Categories"] == "Accessories, Socks & Accessories", f"{parent['Categories']} / {simple['Categories']}")
        check("woo: variable attributes", parent["Attribute 1 name"] == "Size" and parent["Attribute 2 value(s)"] == "Black, Red")
        check("woo: variation linked to parent SKU", v1["Parent"] == parent["SKU"] == "runner-3")
        check("woo: sale pricing", v1["Sale price"] == "89.00" and v1["Regular price"] == "120.00")
        check("woo: duplicate variant SKU blanked", v1["SKU"] == "R3-8B" and v2["SKU"] == "")
        check("woo: weight in kg", v1["Weight (kg)"] == "0.8" and v3["Weight (kg)"] == "")

        # ---- JSON export keeps every detail
        r = c.post("/api/export", json={"token": dtoken, "format": "json", "ids": [1003]})
        item = r.json[0]
        check("json: collections + details included",
              len(item["collections"]) == 2 and item["details"]["seo_title"].startswith("Runner 3")
              and item["details"]["storefront_js"]["media"][1]["media_type"] == "video"
              and item["details"]["json_ld"][0]["@type"] == "Product" and "_details" not in item)
        check("json: raw variants preserved", len(item["variants"]) == 3 and item["variants"][0]["compare_at_price"] == "120.00")

        # ---- export validation
        r = c.post("/api/export", json={"token": "bogus", "format": "shopify"})
        check("expired token -> 410", r.status_code == 410)
        r = c.post("/api/export", json={"token": dtoken, "format": "shopify", "ids": [42]})
        check("empty selection rejected", r.status_code == 400)
        r = c.post("/api/export", json={"token": dtoken, "format": "nope"})
        check("unknown format rejected", r.status_code == 400)
        check("index page served", c.get("/").status_code == 200)

        # ---- SSRF protection (private hosts blocked when ALLOW_PRIVATE is off)
        scraper.ALLOW_PRIVATE = False
        _, j = run_job(c, {"store": "http://127.0.0.1", "scope": "all"})
        check("SSRF: loopback blocked", j and j["status"] == "error" and "isn't allowed" in j["error"], str(j))
        _, j = run_job(c, {"store": "https://169.254.169.254", "scope": "all"})
        check("SSRF: cloud metadata address blocked", j and j["status"] == "error" and "isn't allowed" in j["error"], str(j))
        r, _ = run_job(c, {"store": "http://127.0.0.1:5055", "scope": "all"})
        check("SSRF: loopback with port rejected", r.status_code == 400)
        r, _ = run_job(c, {"store": "localhost", "scope": "all"})
        check("SSRF: dotless host rejected", r.status_code == 400)
        r, _ = run_job(c, {"store": "https://example.com:8443", "scope": "all"})
        check("SSRF: custom port rejected", r.status_code == 400)
    finally:
        proc.terminate()

    print()
    print("ALL PASSED" if not failures else f"{len(failures)} FAILED: {failures}")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
