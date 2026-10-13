"""Store Product Exporter: a web app that exports a Shopify or WooCommerce store's public catalog."""

import json
import logging
import os
import re
import secrets
import threading
import time
from collections import defaultdict, deque

from flask import Flask, Response, jsonify, render_template, request

import exporters
import pipeline
import scraper
import woo
from scraper import ScrapeError

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 256 * 1024
log = logging.getLogger("exporter")

CACHE_TTL = 60 * 60
CACHE_MAX_ENTRIES = 20
RATE_LIMIT_WINDOW = 60
RATE_LIMIT_MAX = int(os.environ.get("RATE_LIMIT_PER_MINUTE", "12"))
MAX_RUNNING_JOBS = int(os.environ.get("MAX_RUNNING_JOBS", "4"))
JOB_TTL = 60 * 60
PLATFORMS = ("auto", "shopify", "woocommerce")

_cache = {}
_cache_lock = threading.Lock()
_jobs = {}
_jobs_lock = threading.Lock()
_hits = defaultdict(deque)
_hits_lock = threading.Lock()


# --------------------------------------------------------------------------- helpers


def _rate_limited():
    now = time.time()
    with _hits_lock:
        q = _hits[request.remote_addr or "unknown"]
        while q and now - q[0] > RATE_LIMIT_WINDOW:
            q.popleft()
        if len(q) >= RATE_LIMIT_MAX:
            return True
        q.append(now)
    return False


def _error(message, status=400):
    resp = jsonify({"error": message})
    resp.status_code = status
    return resp


def _platform_arg(value):
    value = (value or "auto").strip().lower() if isinstance(value, str) or value is None else ""
    if value not in PLATFORMS:
        raise ScrapeError("Unknown platform.")
    return value


def _cache_put(store, products):
    token = secrets.token_urlsafe(16)
    now = time.time()
    with _cache_lock:
        for k in [k for k, v in _cache.items() if now - v["ts"] > CACHE_TTL]:
            del _cache[k]
        while len(_cache) >= CACHE_MAX_ENTRIES:
            del _cache[min(_cache, key=lambda k: _cache[k]["ts"])]
        _cache[token] = {"ts": now, "store": store, "products": products}
    return token


def _cache_get(token):
    with _cache_lock:
        entry = _cache.get(token)
        if entry and time.time() - entry["ts"] <= CACHE_TTL:
            return entry
    return None


def _summary(base, p):
    variants = p.get("variants") or []
    prices = []
    for v in variants:
        try:
            prices.append(float(v.get("price")))
        except (TypeError, ValueError):
            pass
    info = p.get("_woo") or {}
    if not prices and info.get("price_min") is not None:
        prices = [info["price_min"], info.get("price_max") or info["price_min"]]
    images = p.get("images") or []
    image = images[0].get("src") if images else None
    if image and not re.match(r"^https?://", image):
        image = None
    url = p.get("_url") or (f"{base}/products/{p.get('handle')}" if p.get("handle") else None)
    if url and not re.match(r"^https?://", url):
        url = None
    return {
        "id": p.get("id"),
        "title": p.get("title") or "",
        "handle": p.get("handle") or "",
        "vendor": p.get("vendor") or "",
        "product_type": p.get("product_type") or "",
        "collections": [c.get("title", "") for c in (p.get("_collections") or [])],
        "image": image,
        "image_count": len(images),
        "price_min": min(prices) if prices else None,
        "price_max": max(prices) if prices else None,
        "variants": len(variants),
        "available": any(v.get("available") for v in variants),
        "url": url,
    }


# --------------------------------------------------------------------------- background jobs


class Job:
    def __init__(self, store):
        self.id = secrets.token_urlsafe(12)
        self.store = store
        self.created = time.time()
        self.status = "running"  # running | done | error
        self.platform = ""
        self.phase = "products"
        self.message = "Starting…"
        self.progress = {}
        self.warnings = []
        self.error = None
        self.result = None
        self.stop = threading.Event()
        self.stopped_early = False

    def public(self):
        out = {
            "status": self.status,
            "platform": self.platform,
            "phase": self.phase,
            "message": self.message,
            "progress": dict(self.progress),
            "warnings": list(self.warnings),
        }
        if self.error:
            out["error"] = self.error
        if self.result:
            out["result"] = self.result
        return out


class _JobReport:
    """Lets the pipeline report progress onto a Job."""

    def __init__(self, job):
        self.job = job

    def platform(self, name):
        self.job.platform = name

    def phase(self, name):
        self.job.phase = name

    def message(self, text):
        self.job.message = text

    def update(self, **values):
        self.job.progress.update(values)

    def warn(self, text):
        self.job.warnings.append(text)


def _prune_jobs():
    now = time.time()
    with _jobs_lock:
        for k in [k for k, j in _jobs.items() if j.status != "running" and now - j.created > JOB_TTL]:
            del _jobs[k]


def _job_result(job, base, products, stopped_early, has_collections, has_details):
    return {
        "token": _cache_put(base, products),
        "store": base,
        "platform": job.platform,
        "count": len(products),
        "stopped_early": stopped_early,
        "has_collections": has_collections,
        "has_details": has_details,
        "products": [_summary(base, p) for p in products],
    }


def _run_job(job, base, collection, page_from, page_to, include_collections, deep, platform, variations):
    products = []
    try:
        pipeline.run(
            base, collection=collection, page_from=page_from, page_to=page_to,
            include_collections=include_collections, deep=deep, platform=platform,
            variations=variations, report=_JobReport(job), should_stop=job.stop.is_set,
            products=products,
        )
        job.stopped_early = job.stop.is_set()
        job.result = _job_result(job, base, products, job.stopped_early, include_collections, deep)
        job.message = "Done"
        job.status = "done"
    except ScrapeError as e:
        job.error = e.message
        job.status = "error"
        # Keep what was fetched so far so a mid-run failure doesn't lose everything.
        if products:
            job.result = _job_result(job, base, products, True, False, False)
    except Exception:  # noqa: BLE001 - never leak internals to the browser
        log.exception("scrape job failed")
        job.error = "Something went wrong while scraping. Please try again."
        job.status = "error"


# --------------------------------------------------------------------------- routes


@app.errorhandler(ScrapeError)
def _handle_scrape_error(e):
    return _error(e.message, e.status)


@app.errorhandler(413)
def _too_large(_):
    return _error("Request too large.", 413)


@app.route("/")
def index():
    return render_template("index.html")


@app.get("/api/collections")
def api_collections():
    """List a store's collections (Shopify) or categories (WooCommerce) for the dropdown."""
    if _rate_limited():
        return _error("Too many requests. Please wait a minute.", 429)
    base, _ = scraper.parse_store_input(request.args.get("store"))
    platform = _platform_arg(request.args.get("platform"))
    api = None
    if platform == "auto":
        platform, api = woo.detect_platform(base)
    if platform == "woocommerce":
        if api is None:
            api, reason = woo.find_api(base)
            if api is None:
                raise ScrapeError(reason)
        cats = woo.fetch_categories(api)
        paths = woo.category_paths(cats)
        rows = sorted(
            (
                {"handle": c["slug"], "title": paths.get(c["id"], c["title"]), "products_count": c["count"]}
                for c in cats
            ),
            key=lambda r: r["title"].lower(),
        )
        return jsonify({"store": base, "platform": platform, "collections": rows})
    return jsonify({"store": base, "platform": "shopify", "collections": scraper.fetch_collections(base)})


@app.post("/api/search")
def api_search():
    """Validate input and start a background scrape. Poll /api/job/<id> for progress."""
    if _rate_limited():
        return _error("Too many requests. Please wait a minute.", 429)
    body = request.get_json(silent=True) or {}
    base, url_collection = scraper.parse_store_input(body.get("store"))
    platform = _platform_arg(body.get("platform"))
    scope = body.get("scope", "all")

    collection = None
    page_from, page_to = 1, None
    if scope == "collection":
        collection = (body.get("collection") or "").strip() or url_collection
        if not collection:
            raise ScrapeError("Choose a collection or category.")
    elif scope == "pages":
        try:
            page_from = int(body.get("page_from") or 1)
            page_to = int(body.get("page_to") or page_from)
        except (TypeError, ValueError):
            raise ScrapeError("Page numbers must be whole numbers.")
        if page_from < 1 or page_to < page_from:
            raise ScrapeError("Enter a valid page range.")
    elif scope != "all":
        raise ScrapeError("Unknown scope.")
    elif url_collection:
        collection = url_collection

    _prune_jobs()
    with _jobs_lock:
        running = sum(1 for j in _jobs.values() if j.status == "running")
        if running >= MAX_RUNNING_JOBS:
            return _error("The server is busy with other scrapes. Try again in a minute.", 429)
        job = Job(base)
        _jobs[job.id] = job

    threading.Thread(
        target=_run_job,
        args=(
            job, base, collection, page_from, page_to,
            bool(body.get("include_collections", True)), bool(body.get("deep", False)),
            platform, bool(body.get("variations", True)),
        ),
        daemon=True,
    ).start()
    return jsonify({"job_id": job.id})


@app.get("/api/job/<job_id>")
def api_job(job_id):
    job = _jobs.get(job_id)
    if not job:
        return _error("That scrape was not found or has expired.", 404)
    return jsonify(job.public())


@app.post("/api/job/<job_id>/stop")
def api_job_stop(job_id):
    job = _jobs.get(job_id)
    if not job:
        return _error("That scrape was not found or has expired.", 404)
    job.stop.set()
    return jsonify({"ok": True})


@app.post("/api/export")
def api_export():
    body = request.get_json(silent=True) or {}
    entry = _cache_get(body.get("token") or "")
    if not entry:
        return _error("Your results expired. Please search again.", 410)

    products = entry["products"]
    ids = body.get("ids")
    if isinstance(ids, list):
        wanted = set(ids)
        products = [p for p in products if p.get("id") in wanted]
    if not products:
        return _error("No products selected.")

    fmt = body.get("format")
    base = entry["store"]
    host = re.sub(r"[^A-Za-z0-9.-]+", "_", base.split("://", 1)[-1])
    if fmt == "shopify":
        data, mime, name = exporters.to_shopify_csv(products), "text/csv", f"{host}-shopify.csv"
    elif fmt == "woocommerce":
        data, mime, name = exporters.to_woocommerce_csv(products), "text/csv", f"{host}-woocommerce.csv"
    elif fmt == "full":
        data, mime, name = exporters.to_full_csv(products, base), "text/csv", f"{host}-full-details.csv"
    elif fmt == "json":
        data = json.dumps([exporters.to_full_json_item(p) for p in products], indent=2)
        mime, name = "application/json", f"{host}-products.json"
    else:
        return _error("Unknown export format.")

    return Response(
        data,
        mimetype=mime,
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", "5000")), debug=False, threaded=True)
