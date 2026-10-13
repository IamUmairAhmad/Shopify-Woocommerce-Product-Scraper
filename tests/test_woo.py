"""WooCommerce end-to-end tests against tests/mock_woo.py. Run with `python3 -I tests/test_woo.py`.

The mock replicates the documented Store API; this is NOT a test against a live store.
"""

import csv
import io
import json
import os
import subprocess
import sys
import time

os.environ["ALLOW_PRIVATE_HOSTS"] = "1"
os.environ["SCRAPE_DELAY_SCALE"] = "0"
os.environ["NO_PROXY"] = "127.0.0.1,localhost"
os.environ["no_proxy"] = "127.0.0.1,localhost"
os.environ["RATE_LIMIT_PER_MINUTE"] = "10000"

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import requests  # noqa: E402

import app as exporter  # noqa: E402
import scraper  # noqa: E402
import woo  # noqa: E402

failures = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        failures.append(name)


def run_job(client, payload, timeout=120):
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


def export(client, token, fmt, ids=None):
    body = {"token": token, "format": fmt}
    if ids is not None:
        body["ids"] = ids
    return client.post("/api/export", json=body)


def rows_of(resp):
    return list(csv.DictReader(io.StringIO(resp.get_data(as_text=True))))


def unit_checks():
    check("money: minor units", woo.to_money("1800") == "18.00", woo.to_money("1800"))
    check("money: empty", woo.to_money("") in ("", None), repr(woo.to_money("")))
    check("weight kg", woo.parse_weight_grams("0.8 kg") == 800)
    check("weight lbs", round(woo.parse_weight_grams("2 lbs")) == 907, str(woo.parse_weight_grams("2 lbs")))
    check("weight thousands", woo.parse_weight_grams("1,250 g") == 1250)
    check("weight N/A", not woo.parse_weight_grams("N/A"))
    d = woo.parse_dimensions_cm("10 × 8 × 4 in")
    check("dimensions in->cm", d and [round(x, 2) for x in d] == [25.4, 20.32, 10.16], str(d))
    check("category url", scraper.parse_store_input("https://s.com/product-category/clothing/hoodies/") == ("https://s.com", "hoodies"))
    check("category query", scraper.parse_store_input("https://s.com/shop/?product_cat=hats") == ("https://s.com", "hats"))


def start(port, **env):
    e = dict(os.environ, **{k: str(v) for k, v in env.items()})
    p = subprocess.Popen([sys.executable, os.path.join(HERE, "mock_woo.py"), str(port)], env=e,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(100):
        try:
            requests.get(f"http://127.0.0.1:{port}/", timeout=1)
            break
        except requests.exceptions.RequestException:
            time.sleep(0.1)
    return p


def main():
    unit_checks()
    procs = [
        start(5056),
        start(5057, MOCK_MODE="plain", MOCK_BULK_VARIATIONS=0, MOCK_MAX_PER_PAGE=50),
        start(5058, MOCK_MODE="blocked"),
        start(5059, MOCK_MODE="none"),
    ]
    S = "http://127.0.0.1:5056"
    PLAIN = "http://127.0.0.1:5057"
    try:
        c = exporter.app.test_client()

        # ---- detection
        plat, api = woo.detect_platform(S)
        check("detects woocommerce", plat == "woocommerce" and api is not None)
        r = c.post("/api/search", json={"store": "http://127.0.0.1:5059", "scope": "all"})
        _, j = run_job(c, {"store": "http://127.0.0.1:5059", "scope": "all"})
        check("non-store gives clear error", j and j["status"] == "error" and j.get("error"), str(j))
        _, j = run_job(c, {"store": "http://127.0.0.1:5058", "scope": "all"})
        check("blocked api message", j and j["status"] == "error" and "blocked" in j["error"].lower(), str(j and j.get("error")))

        # ---- categories
        r = c.get("/api/collections", query_string={"store": S})
        titles = [x["title"] for x in r.json.get("collections", [])] if r.status_code == 200 else []
        check("categories with hierarchy", "Clothing > Hoodies & Tops" in titles, str(r.get_data(as_text=True))[:300])

        # ---- full scrape
        _, j = run_job(c, {"store": S, "scope": "all"})
        check("full job done", j and j["status"] == "done", str(j and j.get("error")))
        res = j["result"]
        check("platform reported", res["platform"] == "woocommerce")
        check("245 products over 3 pages", res["count"] == 245, str(res["count"]))
        check("no warnings", not j["warnings"], str(j["warnings"]))
        token = res["token"]
        byname = {p["title"]: p for p in res["products"]}
        check("entities decoded", "Beanie 1 & Co" in byname, str(list(byname)[:3]))
        hood = byname.get("Hoodie 1")
        check("variable has 4 variants", hood and hood["variants"] == 4, str(hood))
        check("price range", hood and hood["price_min"] == 25.0 and hood["price_max"] == 33.0, str(hood))
        check("collections from categories", hood and "Clothing" in hood["collections"], str(hood))

        full = rows_of(export(c, token, "full"))
        check("full csv has platform col", full and full[0].get("Platform", "").lower() == "woocommerce", str(full[0].get("Platform")))

        woo_rows = rows_of(export(c, token, "woocommerce"))
        types = {r["Type"] for r in woo_rows}
        check("woo csv types", {"simple", "variable", "variation", "external", "grouped"} <= types, str(types))
        hrows = [r for r in woo_rows if r["Name"].startswith("Hoodie 1") or r.get("Parent", "").startswith("id:")]
        attr_cols = [h for h in woo_rows[0] if h.startswith("Attribute ") and h.endswith("name")]
        names = {r[h] for r in woo_rows for h in attr_cols if r[h]}
        check("woo csv has Material/Color/Size attrs", {"Material", "Color", "Size"} <= names, str(names))
        check("woo csv variations present", sum(1 for r in woo_rows if r["Type"] == "variation") == 40)
        grouped = [r for r in woo_rows if r["Type"] == "grouped"]
        check("grouped refs SKUs", grouped and "BEAN-1" in grouped[0].get("Grouped products", ""), str(grouped[:1]))
        ext = [r for r in woo_rows if r["Type"] == "external"]
        check("external url", ext and ext[0].get("External URL", "").startswith("https://amazon.example"), str(ext[:1]))

        shop = rows_of(export(c, token, "shopify"))
        check("shopify csv has rows", len(shop) >= 245, str(len(shop)))
        check("shopify handle present", all(r["Handle"] for r in shop))
        js = json.loads(export(c, token, "json").get_data(as_text=True))
        check("json has woocommerce key", len(js) == 245 and any(i.get("woocommerce") for i in js))

        # ---- category scope by slug (child category)
        _, j = run_job(c, {"store": S, "scope": "collection", "collection": "hoodies"})
        check("category scope", j and j["status"] == "done" and j["result"]["count"] == 10, str(j and (j.get("error") or j["result"]["count"])))
        _, j = run_job(c, {"store": S + "/product-category/clothing/", "scope": "all"})
        check("category from URL (parent includes child)", j and j["status"] == "done" and j["result"]["count"] == 10, str(j and (j.get("error") or j["result"]["count"])))
        _, j = run_job(c, {"store": S, "scope": "collection", "collection": "nope"})
        check("unknown category errors", j and j["status"] == "error", str(j))

        # ---- page range
        _, j = run_job(c, {"store": S, "scope": "pages", "page_from": 2, "page_to": 2})
        check("page range", j and j["status"] == "done" and j["result"]["count"] == 100, str(j and (j.get("error") or j["result"]["count"])))

        # ---- no variations
        _, j = run_job(c, {"store": S, "scope": "all", "variations": False})
        check("no-variations warns", j and j["status"] == "done" and j["warnings"], str(j and j.get("warnings")))

        # ---- deep
        _, j = run_job(c, {"store": S, "scope": "all", "deep": True})
        check("deep job done", j and j["status"] == "done", str(j and j.get("error")))
        dtoken = j["result"]["token"]
        wr = rows_of(export(c, dtoken, "woocommerce"))
        b1 = next((r for r in wr if r["SKU"] == "BEAN-1"), None)
        check("simple weight/dimensions", b1 and b1["Weight (kg)"] in ("0.4", "0.400") and b1["Length (cm)"] in ("20", "20.0"), str(b1 and (b1["Weight (kg)"], b1["Length (cm)"])))
        v = {r["SKU"]: r for r in wr if r["Type"] == "variation"}
        check("variation weights", v.get("HOOD-1-0") and v["HOOD-1-0"]["Weight (kg)"] in ("0.8", "0.800")
              and v["HOOD-1-1"]["Weight (kg)"].startswith("0.90"), str({k: x["Weight (kg)"] for k, x in list(v.items())[:4]}))
        check("variation N/A weight blank", v["HOOD-1-2"]["Weight (kg)"] == "")
        fr = rows_of(export(c, dtoken, "full"))
        check("deep SEO title", any("SEO title" in (r.get("SEO Title") or "") for r in fr))

        # ---- plain permalinks, no bulk variations, per-page cap 50
        _, j = run_job(c, {"store": PLAIN, "scope": "all"})
        check("plain permalink job done", j and j["status"] == "done", str(j and j.get("error")))
        check("plain: 245 products despite cap 50", j and j["result"]["count"] == 245, str(j and j["result"]["count"]))
        check("plain: variations via fallback", j and next(p for p in j["result"]["products"] if p["title"] == "Hoodie 2")["variants"] == 4)

        # ---- partial results on stop
        r = c.post("/api/search", json={"store": S, "scope": "all"})
        jid = r.json["job_id"]
        c.post(f"/api/job/{jid}/stop")
        for _ in range(400):
            jj = c.get(f"/api/job/{jid}").json
            if jj["status"] != "running":
                break
            time.sleep(0.05)
        check("stop finishes", jj["status"] in ("done", "error"), jj["status"])

        # ---- shopify still detected
    finally:
        for p in procs:
            p.terminate()

    print()
    print("ALL PASSED" if not failures else f"{len(failures)} FAILED: {failures}")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
