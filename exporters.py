"""Convert Shopify storefront product data into import-ready CSV files.

Each product dict is the raw storefront JSON, optionally enriched by the scraper with:
    _collections  list of {handle, title} for collections that contain the product
    _details      SEO tags, JSON-LD data and storefront .js data (deep mode)
"""

import csv
import html
import io
import re
from decimal import Decimal, InvalidOperation

SHOPIFY_HEADERS = [
    "Handle", "Title", "Body (HTML)", "Vendor", "Product Category", "Type", "Tags",
    "Published", "Option1 Name", "Option1 Value", "Option2 Name", "Option2 Value",
    "Option3 Name", "Option3 Value", "Variant SKU", "Variant Grams",
    "Variant Inventory Tracker", "Variant Inventory Qty", "Variant Inventory Policy",
    "Variant Fulfillment Service", "Variant Price", "Variant Compare At Price",
    "Variant Requires Shipping", "Variant Taxable", "Variant Barcode", "Image Src",
    "Image Position", "Image Alt Text", "Gift Card", "SEO Title", "SEO Description",
    # Standard Shopify template columns that stores don't expose publicly; left empty so the
    # file has the same layout as Shopify's own export and other scraper tools.
    "Google Shopping / Google Product Category", "Google Shopping / Gender",
    "Google Shopping / Age Group", "Google Shopping / MPN",
    "Google Shopping / AdWords Grouping", "Google Shopping / AdWords Labels",
    "Google Shopping / Condition", "Google Shopping / Custom Product",
    "Google Shopping / Custom Label 0", "Google Shopping / Custom Label 1",
    "Google Shopping / Custom Label 2", "Google Shopping / Custom Label 3",
    "Google Shopping / Custom Label 4",
    "Variant Image", "Variant Weight Unit", "Variant Tax Code", "Cost per item", "Status",
]

WOO_ATTRIBUTES = 3
_WOO_BASE_HEADERS = [
    "ID", "Type", "SKU", "Name", "Published", "Is featured?", "Visibility in catalog",
    "Short description", "Description", "Date sale price starts", "Date sale price ends",
    "Tax status", "Tax class", "In stock?", "Stock", "Backorders allowed?",
    "Sold individually?", "Weight (kg)", "Length (cm)", "Width (cm)", "Height (cm)",
    "Allow customer reviews?", "Purchase note", "Sale price", "Regular price",
    "Categories", "Tags", "Shipping class", "Images", "Download limit",
    "Download expiry days", "Parent", "Grouped products", "Upsells", "Cross-sells",
    "External URL", "Button text", "Position",
]


def woo_headers(n_attributes=WOO_ATTRIBUTES):
    """WooCommerce import columns with `n_attributes` attribute column groups."""
    headers = list(_WOO_BASE_HEADERS)
    for i in range(1, n_attributes + 1):
        headers += [
            f"Attribute {i} name", f"Attribute {i} value(s)", f"Attribute {i} visible",
            f"Attribute {i} global", f"Attribute {i} default",
        ]
    return headers


WOO_HEADERS = woo_headers(WOO_ATTRIBUTES)

FULL_HEADERS = [
    "Product ID", "Handle", "Product URL", "Title", "Vendor", "Brand (structured data)",
    "Product Type", "Tags", "Collections", "Collection Handles", "Published At",
    "Created At", "Updated At", "Description (HTML)", "Description (text)",
    "Option 1 Name", "Option 2 Name", "Option 3 Name", "Variant ID", "Variant Position",
    "Variant Title", "Option 1 Value", "Option 2 Value", "Option 3 Value", "SKU",
    "Barcode / GTIN", "Price", "Compare At Price", "On Sale", "Currency", "Available",
    "Requires Shipping", "Taxable", "Weight (grams)", "Weight (kg)", "Variant Image",
    "Product Min Price", "Product Max Price", "Variant Count", "Image Count",
    "Image URLs", "Image Alt Texts", "Video / 3D Media URLs", "SEO Title",
    "SEO Description", "Canonical URL", "OG Image", "Rating", "Review Count",
    "Requires Selling Plan",
    # Filled for WooCommerce stores (and "Platform" for both).
    "Platform", "Source Product Type", "Stock Status", "Low Stock Remaining", "Sold Individually",
    "Short Description (HTML)", "Short Description (text)", "Length (cm)", "Width (cm)",
    "Height (cm)", "Attributes", "Category Paths", "External URL", "Button Text",
]


# --------------------------------------------------------------------------- helpers


def _tags(product):
    tags = product.get("tags") or []
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(",")]
    return [t for t in tags if t]


def _dec(value):
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError):
        return None


def _images(product):
    imgs = [i for i in (product.get("images") or []) if i.get("src")]
    return sorted(imgs, key=lambda i: i.get("position") or 0)


def _options(product):
    return [o for o in (product.get("options") or []) if o.get("name")][:3]


def _is_default_only(product):
    opts = _options(product)
    return (
        len(opts) == 1
        and opts[0].get("name") == "Title"
        and (opts[0].get("values") or []) == ["Default Title"]
    )


def _variant_image(product, variant):
    """The image assigned to a variant.

    Stores serve this in one of two ways: `variant.featured_image`, or only as
    `images[].variant_ids` with `featured_image` left null. Both are checked, with
    `featured_image` taking priority.
    """
    img = variant.get("featured_image")
    if isinstance(img, dict) and img.get("src"):
        return img["src"]
    vid = variant.get("id")
    if vid is not None:
        for image in _images(product):
            if vid in (image.get("variant_ids") or []):
                return image["src"]
    return ""


def _details(product):
    return product.get("_details") or {}


def _collections(product):
    return [c["title"] for c in (product.get("_collections") or []) if c.get("title")]


def _js_variants(product):
    js = _details(product).get("storefront_js") or {}
    return {v.get("id"): v for v in (js.get("variants") or []) if isinstance(v, dict)}


def _barcode(product, variant):
    code = variant.get("barcode")
    if code:
        return code
    code = _js_variants(product).get(variant.get("id"), {}).get("barcode")
    if code:
        return code
    sku = variant.get("sku")
    return (_details(product).get("gtin_by_sku") or {}).get(str(sku), "") if sku else ""


def _media_urls(product):
    """Video and 3D model URLs from the storefront .js media list."""
    js = _details(product).get("storefront_js") or {}
    urls = []
    for m in js.get("media") or []:
        kind = m.get("media_type")
        if kind in ("video", "model"):
            for s in m.get("sources") or []:
                if s.get("url"):
                    urls.append(s["url"])
        elif kind == "external_video":
            vid, host = m.get("external_id"), m.get("host")
            if host == "youtube" and vid:
                urls.append(f"https://www.youtube.com/watch?v={vid}")
            elif host == "vimeo" and vid:
                urls.append(f"https://vimeo.com/{vid}")
            elif vid:
                urls.append(str(vid))
    return urls


def _plain_text(html_text):
    text = re.sub(r"(?i)<br\s*/?>|</p>|</li>|</div>|</h[1-6]>", "\n", html_text or "")
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    return re.sub(r"\n\s*\n+", "\n", re.sub(r"[ \t]+", " ", text)).strip()


def _write(headers, rows):
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=headers, extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue()


def _unique(seq):
    seen, out = set(), []
    for x in seq:
        if x and x not in seen:
            seen.add(x)
            out.append(x)
    return out


# --------------------------------------------------------------------------- Shopify


def to_shopify_csv(products):
    """Shopify's product import format: one row per variant, extra rows for more images."""
    rows = []
    for p in products:
        handle = p.get("handle") or str(p.get("id"))
        variants = p.get("variants") or [{}]
        images = _images(p)
        opts = _options(p) or [{"name": "Title"}]
        d = _details(p)
        n = max(len(variants), len(images), 1)
        for i in range(n):
            row = {"Handle": handle}
            if i == 0:
                row.update(
                    {
                        "Title": p.get("title", ""),
                        "Body (HTML)": p.get("body_html") or "",
                        "Vendor": p.get("vendor", ""),
                        "Type": p.get("product_type", ""),
                        "Tags": ", ".join(_tags(p)),
                        "Published": "TRUE",
                        "Gift Card": "FALSE",
                        "SEO Title": d.get("seo_title", ""),
                        "SEO Description": d.get("seo_description", ""),
                        "Status": "active",
                    }
                )
            if i < len(variants):
                v = variants[i]
                for idx in range(3):
                    if i == 0 and idx < len(opts):
                        row[f"Option{idx + 1} Name"] = opts[idx].get("name", "")
                    if idx < len(opts):
                        row[f"Option{idx + 1} Value"] = v.get(f"option{idx + 1}") or ""
                row.update(
                    {
                        "Variant SKU": v.get("sku") or "",
                        "Variant Grams": v.get("grams") if v.get("grams") is not None else "",
                        "Variant Inventory Policy": "deny",
                        "Variant Fulfillment Service": "manual",
                        "Variant Price": v.get("price", ""),
                        "Variant Compare At Price": v.get("compare_at_price") or "",
                        "Variant Requires Shipping": "TRUE" if v.get("requires_shipping", True) else "FALSE",
                        "Variant Taxable": "TRUE" if v.get("taxable", True) else "FALSE",
                        "Variant Barcode": _barcode(p, v),
                        "Variant Image": _variant_image(p, v),
                        "Variant Weight Unit": "kg",
                    }
                )
            if i < len(images):
                row["Image Src"] = images[i]["src"]
                row["Image Position"] = i + 1
                row["Image Alt Text"] = images[i].get("alt") or ""
            rows.append(row)
    return _write(SHOPIFY_HEADERS, rows)


# --------------------------------------------------------------------------- WooCommerce


def _woo_price(variant):
    price = _dec(variant.get("price"))
    compare = _dec(variant.get("compare_at_price"))
    if price is not None and compare is not None and compare > price:
        return str(price), str(compare)  # (sale, regular)
    return "", str(price) if price is not None else ""


def _woo_row_base():
    return {
        "Published": 1,
        "Is featured?": 0,
        "Visibility in catalog": "visible",
        "Tax status": "taxable",
        "Backorders allowed?": 0,
        "Sold individually?": 0,
        "Allow customer reviews?": 1,
    }


def _kg(grams):
    return round(grams / 1000, 3) if grams else ""


def _woo_source_rows(p, sku_by_id):
    """Import rows for a product scraped from a WooCommerce store.

    Keeps its real type (simple, variable, grouped, external), every attribute (including
    ones that aren't used for variations), category paths ("Parent > Child") and short description.
    """
    info = p.get("_woo") or {}
    ptype = info.get("type") if info.get("type") in ("simple", "variable", "grouped", "external") else "simple"
    variants = p.get("variants") or []
    attrs = info.get("attributes") or []
    base = _woo_row_base()
    base["Sold individually?"] = 1 if info.get("sold_individually") else 0
    base["Backorders allowed?"] = 1 if info.get("is_on_backorder") else 0

    def set_attr(row, idx, a, value, default=None):
        row[f"Attribute {idx} name"] = a.get("name", "")
        row[f"Attribute {idx} value(s)"] = value
        row[f"Attribute {idx} visible"] = 1
        row[f"Attribute {idx} global"] = 1 if str(a.get("taxonomy") or "").startswith("pa_") else 0
        if default is not None:
            row[f"Attribute {idx} default"] = default

    parent = dict(base)
    parent.update(
        {
            "Type": ptype,
            "Name": p.get("title", ""),
            "Short description": info.get("short_description") or "",
            "Description": p.get("body_html") or "",
            "In stock?": 1 if info.get("is_in_stock", True) else 0,
            "Categories": ", ".join(info.get("category_paths") or _collections(p)),
            "Tags": ", ".join(_tags(p)),
            "Images": ", ".join(i["src"] for i in _images(p)),
        }
    )
    for idx, a in enumerate(attrs, start=1):
        set_attr(parent, idx, a, ", ".join(a.get("terms") or []), a.get("default") or "")

    if ptype == "variable":
        # A placeholder variant (id == product id) means the variations couldn't be read.
        real = [v for v in variants if v.get("id") != p.get("id")]
        parent_sku = info.get("sku") or p.get("handle") or f"product-{p.get('id')}"
        parent["SKU"] = parent_sku
        rows = [parent]
        used = {parent_sku}
        for pos, v in enumerate(real):
            sale, regular = _woo_price(v)
            sku = v.get("sku") or ""
            if sku in used:  # WooCommerce SKUs must be unique
                sku = ""
            if sku:
                used.add(sku)
            row = dict(base)
            row.update(
                {
                    "Type": "variation",
                    "SKU": sku,
                    "Name": f"{p.get('title', '')} - {v.get('title', '')}",
                    "In stock?": 1 if v.get("available", True) else 0,
                    "Weight (kg)": _kg(v.get("grams")),
                    "Length (cm)": v.get("length_cm", ""),
                    "Width (cm)": v.get("width_cm", ""),
                    "Height (cm)": v.get("height_cm", ""),
                    "Sale price": sale,
                    "Regular price": regular,
                    "Images": _variant_image(p, v),
                    "Parent": parent_sku,
                    "Position": pos,
                }
            )
            values = v.get("_option_values") or []
            n = 0
            for idx, a in enumerate(attrs, start=1):
                if a.get("has_variations"):
                    set_attr(row, idx, a, values[n] if n < len(values) else "")
                    n += 1
            rows.append(row)
        return rows

    v = variants[0] if variants else {}
    sale, regular = _woo_price(v)
    parent.update(
        {
            "SKU": v.get("sku") or "",
            "Weight (kg)": _kg(v.get("grams")),
            "Length (cm)": v.get("length_cm", ""),
            "Width (cm)": v.get("width_cm", ""),
            "Height (cm)": v.get("height_cm", ""),
            "Sale price": sale,
            "Regular price": regular,
        }
    )
    if ptype == "external":
        parent["External URL"] = info.get("external_url") or ""
        parent["Button text"] = info.get("button_text") or ""
    if ptype == "grouped":
        parent["Grouped products"] = ", ".join(
            s for s in (sku_by_id.get(cid) for cid in info.get("grouped_products") or []) if s
        )
    return [parent]


def to_woocommerce_csv(products):
    """WooCommerce product import format with simple and variable products.

    Categories combine the product type with every collection the product belongs to.
    Variable products get a parent row plus one 'variation' row per variant, linked by
    SKU, so a SKU (the product handle) is generated for any variable product.
    Products scraped from a WooCommerce store keep their real type and attributes.
    """
    rows = []
    n_attr = WOO_ATTRIBUTES
    sku_by_id = {}
    for p in products:
        info = p.get("_woo")
        if info:
            n_attr = max(n_attr, len(info.get("attributes") or []))
            sku_by_id[p.get("id")] = info.get("sku") or ""
    for p in products:
        if p.get("_woo"):
            rows.extend(_woo_source_rows(p, sku_by_id))
            continue
        variants = p.get("variants") or []
        image_urls =", ".join(i["src"] for i in _images(p))
        categories = ", ".join(_unique([p.get("product_type") or ""] + _collections(p)))
        base = _woo_row_base()
        any_available = any(v.get("available", True) for v in variants) if variants else True

        if not variants or (len(variants) == 1 and _is_default_only(p)):
            v = variants[0] if variants else {}
            sale, regular = _woo_price(v)
            grams = v.get("grams")
            row = dict(base)
            row.update(
                {
                    "Type": "simple",
                    "SKU": v.get("sku") or "",
                    "Name": p.get("title", ""),
                    "Description": p.get("body_html") or "",
                    "In stock?": 1 if v.get("available", True) else 0,
                    "Weight (kg)": round(grams / 1000, 3) if grams else "",
                    "Sale price": sale,
                    "Regular price": regular,
                    "Categories": categories,
                    "Tags": ", ".join(_tags(p)),
                    "Images": image_urls,
                }
            )
            rows.append(row)
            continue

        opts = _options(p)
        parent_sku = p.get("handle") or f"product-{p.get('id')}"
        parent = dict(base)
        parent.update(
            {
                "Type": "variable",
                "SKU": parent_sku,
                "Name": p.get("title", ""),
                "Description": p.get("body_html") or "",
                "In stock?": 1 if any_available else 0,
                "Categories": categories,
                "Tags": ", ".join(_tags(p)),
                "Images": image_urls,
            }
        )
        for idx, opt in enumerate(opts, start=1):
            values = opt.get("values") or []
            parent[f"Attribute {idx} name"] = opt.get("name", "")
            parent[f"Attribute {idx} value(s)"] = ", ".join(values)
            parent[f"Attribute {idx} visible"] = 1
            parent[f"Attribute {idx} global"] = 0
            parent[f"Attribute {idx} default"] = values[0] if values else ""
        rows.append(parent)

        used_skus = {parent_sku}
        for pos, v in enumerate(variants):
            sale, regular = _woo_price(v)
            grams = v.get("grams")
            sku = v.get("sku") or ""
            if sku in used_skus:  # WooCommerce SKUs must be unique
                sku = ""
            if sku:
                used_skus.add(sku)
            row = dict(base)
            row.update(
                {
                    "Type": "variation",
                    "SKU": sku,
                    "Name": f"{p.get('title', '')} - {v.get('title', '')}",
                    "In stock?": 1 if v.get("available", True) else 0,
                    "Weight (kg)": round(grams / 1000, 3) if grams else "",
                    "Sale price": sale,
                    "Regular price": regular,
                    "Images": _variant_image(p, v),
                    "Parent": parent_sku,
                    "Position": pos,
                }
            )
            for idx, opt in enumerate(opts, start=1):
                row[f"Attribute {idx} name"] = opt.get("name", "")
                row[f"Attribute {idx} value(s)"] = v.get(f"option{idx}") or ""
                row[f"Attribute {idx} visible"] = 1
                row[f"Attribute {idx} global"] = 0
            rows.append(row)
    return _write(woo_headers(n_attr), rows)


# --------------------------------------------------------------------------- full details


def to_full_csv(products, base_url=""):
    """One row per variant with every product-level and variant-level detail available."""
    rows = []
    for p in products:
        d = _details(p)
        variants = p.get("variants") or [{}]
        images = _images(p)
        opts = _options(p)
        cols = _collections(p)
        handles = [c["handle"] for c in (p.get("_collections") or [])]
        prices = [x for x in (_dec(v.get("price")) for v in variants) if x is not None]
        js = d.get("storefront_js") or {}
        info = p.get("_woo") or {}
        product_row = {
            "Product ID": p.get("id", ""),
            "Handle": p.get("handle", ""),
            "Product URL": p.get("_url")
            or (f"{base_url}/products/{p['handle']}" if p.get("handle") and base_url else ""),
            "Title": p.get("title", ""),
            "Vendor": p.get("vendor", ""),
            "Brand (structured data)": d.get("brand", ""),
            "Product Type": p.get("product_type", ""),
            "Tags": ", ".join(_tags(p)),
            "Collections": " | ".join(cols),
            "Collection Handles": " | ".join(handles),
            "Published At": p.get("published_at", ""),
            "Created At": p.get("created_at", ""),
            "Updated At": p.get("updated_at", ""),
            "Description (HTML)": p.get("body_html") or "",
            "Description (text)": _plain_text(p.get("body_html")),
            "Option 1 Name": opts[0]["name"] if len(opts) > 0 else "",
            "Option 2 Name": opts[1]["name"] if len(opts) > 1 else "",
            "Option 3 Name": opts[2]["name"] if len(opts) > 2 else "",
            "Product Min Price": str(min(prices)) if prices else "",
            "Product Max Price": str(max(prices)) if prices else "",
            "Variant Count": len(p.get("variants") or []),
            "Image Count": len(images),
            "Image URLs": " | ".join(i["src"] for i in images),
            "Image Alt Texts": " | ".join((i.get("alt") or "") for i in images),
            "Video / 3D Media URLs": " | ".join(_media_urls(p)),
            "SEO Title": d.get("seo_title", ""),
            "SEO Description": d.get("seo_description", ""),
            "Canonical URL": d.get("canonical", ""),
            "OG Image": d.get("og_image", ""),
            "Rating": d.get("rating_value", "") or (info.get("average_rating", "") if info.get("review_count") else ""),
            "Review Count": d.get("review_count", "") or (info.get("review_count") or ""),
            "Requires Selling Plan": js.get("requires_selling_plan", ""),
            "Platform": "WooCommerce" if info else "Shopify",
            "Source Product Type": info.get("type", ""),
            "Stock Status": (
                "onbackorder" if info.get("is_on_backorder")
                else ("instock" if info.get("is_in_stock") else "outofstock")
            ) if info else "",
            "Low Stock Remaining": info.get("low_stock_remaining") if info.get("low_stock_remaining") is not None else "",
            "Sold Individually": ("TRUE" if info.get("sold_individually") else "FALSE") if info else "",
            "Short Description (HTML)": info.get("short_description", ""),
            "Short Description (text)": _plain_text(info.get("short_description")),
            "Attributes": " | ".join(
                f"{a.get('name', '')}: {', '.join(a.get('terms') or [])}" for a in info.get("attributes") or []
            ),
            "Category Paths": " | ".join(info.get("category_paths") or []),
            "External URL": info.get("external_url", ""),
            "Button Text": info.get("button_text", ""),
        }
        for v in variants:
            price, compare = _dec(v.get("price")), _dec(v.get("compare_at_price"))
            grams = v.get("grams")
            row = dict(product_row)
            row.update(
                {
                    "Variant ID": v.get("id", ""),
                    "Variant Position": v.get("position", ""),
                    "Variant Title": v.get("title", ""),
                    "Option 1 Value": v.get("option1") or "",
                    "Option 2 Value": v.get("option2") or "",
                    "Option 3 Value": v.get("option3") or "",
                    "SKU": v.get("sku") or "",
                    "Barcode / GTIN": _barcode(p, v),
                    "Price": v.get("price", ""),
                    "Compare At Price": v.get("compare_at_price") or "",
                    "On Sale": "TRUE" if price is not None and compare is not None and compare > price else "FALSE",
                    "Currency": d.get("currency", "") or info.get("currency_code", ""),
                    "Available": "TRUE" if v.get("available") else "FALSE",
                    # WooCommerce's public API doesn't say whether a product ships or is taxable.
                    "Requires Shipping": "" if info else ("TRUE" if v.get("requires_shipping", True) else "FALSE"),
                    "Taxable": "" if info else ("TRUE" if v.get("taxable", True) else "FALSE"),
                    "Weight (grams)": grams if grams is not None else "",
                    "Weight (kg)": round(grams / 1000, 3) if grams else "",
                    "Length (cm)": v.get("length_cm", ""),
                    "Width (cm)": v.get("width_cm", ""),
                    "Height (cm)": v.get("height_cm", ""),
                    "Variant Image": _variant_image(p, v),
                }
            )
            rows.append(row)
    return _write(FULL_HEADERS, rows)


def to_full_json_item(p):
    """A product with the scraper's enrichment exposed under clear key names."""
    item = {k: v for k, v in p.items() if not k.startswith("_")}
    item["collections"] = p.get("_collections") or []
    if p.get("_url"):
        item["url"] = p["_url"]
    if p.get("_woo"):
        item["woocommerce"] = p["_woo"]
    if p.get("_details"):
        item["details"] = p["_details"]
    return item
