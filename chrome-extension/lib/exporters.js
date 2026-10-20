// Product data -> Shopify CSV, WooCommerce CSV, full-details CSV and JSON. Mirrors the Python exporters.
import { writeCsv, pf, unescapeHtml } from "./util.js";
import { writeXlsx } from "./xlsx.js";

export const SHOPIFY_HEADERS = [
  "Handle", "Title", "Body (HTML)", "Vendor", "Product Category", "Type", "Tags",
  "Published", "Option1 Name", "Option1 Value", "Option2 Name", "Option2 Value",
  "Option3 Name", "Option3 Value", "Variant SKU", "Variant Grams",
  "Variant Inventory Tracker", "Variant Inventory Qty", "Variant Inventory Policy",
  "Variant Fulfillment Service", "Variant Price", "Variant Compare At Price",
  "Variant Requires Shipping", "Variant Taxable", "Variant Barcode", "Image Src",
  "Image Position", "Image Alt Text", "Gift Card", "SEO Title", "SEO Description",
  "Google Shopping / Google Product Category", "Google Shopping / Gender",
  "Google Shopping / Age Group", "Google Shopping / MPN",
  "Google Shopping / AdWords Grouping", "Google Shopping / AdWords Labels",
  "Google Shopping / Condition", "Google Shopping / Custom Product",
  "Google Shopping / Custom Label 0", "Google Shopping / Custom Label 1",
  "Google Shopping / Custom Label 2", "Google Shopping / Custom Label 3",
  "Google Shopping / Custom Label 4",
  "Variant Image", "Variant Weight Unit", "Variant Tax Code", "Cost per item", "Status",
];

export const LINK_HEADER = "Product URL";

export const REVIEW_HEADERS = [
  "Product ID", "Product Handle", "Product Title", "Product URL", "Review ID", "Reviewer", "Rating",
  "Date", "Title", "Review", "Verified", "Source",
];

const WOO_ATTRIBUTES = 3;
const WOO_BASE_HEADERS = [
  "ID", "Type", "SKU", "Name", "Published", "Is featured?", "Visibility in catalog",
  "Short description", "Description", "Date sale price starts", "Date sale price ends",
  "Tax status", "Tax class", "In stock?", "Stock", "Backorders allowed?",
  "Sold individually?", "Weight (kg)", "Length (cm)", "Width (cm)", "Height (cm)",
  "Allow customer reviews?", "Purchase note", "Sale price", "Regular price",
  "Categories", "Tags", "Shipping class", "Images", "Download limit",
  "Download expiry days", "Parent", "Grouped products", "Upsells", "Cross-sells",
  "External URL", "Button text", "Position",
];

export function wooHeaders(n = WOO_ATTRIBUTES) {
  const h = [...WOO_BASE_HEADERS];
  for (let i = 1; i <= n; i++) {
    h.push(`Attribute ${i} name`, `Attribute ${i} value(s)`, `Attribute ${i} visible`, `Attribute ${i} global`, `Attribute ${i} default`);
  }
  return h;
}

export const FULL_HEADERS = [
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
  "Platform", "Source Product Type", "Stock Status", "Low Stock Remaining", "Sold Individually",
  "Short Description (HTML)", "Short Description (text)", "Length (cm)", "Width (cm)",
  "Height (cm)", "Attributes", "Category Paths", "External URL", "Button Text",
];

// --------------------------------------------------------------------------- helpers

const has = (o, k) => Object.prototype.hasOwnProperty.call(o, k);
const orEmpty = (v) => (v === null || v === undefined || v === false || v === 0 || v === "" ? "" : v);

function tagsOf(p) {
  let tags = p.tags || [];
  if (typeof tags === "string") tags = tags.split(",").map((t) => t.trim());
  return tags.filter(Boolean);
}

/** Parsed decimal or null; keeps the original text for output. */
function dec(value) {
  if (value === null || value === undefined) return null;
  const s = String(value).trim();
  if (!/^[+-]?(\d+\.?\d*|\.\d+)$/.test(s)) return null;
  return { s: s.replace(/^\+/, ""), n: Number(s) };
}

function imagesOf(p) {
  return (p.images || []).filter((i) => i.src).map((i, idx) => ({ i, idx }))
    .sort((a, b) => (a.i.position || 0) - (b.i.position || 0) || a.idx - b.idx).map((x) => x.i);
}

const optionsOf = (p) => (p.options || []).filter((o) => o.name).slice(0, 3);

function isDefaultOnly(p) {
  const o = optionsOf(p);
  return o.length === 1 && o[0].name === "Title" && JSON.stringify(o[0].values || []) === JSON.stringify(["Default Title"]);
}

function variantImage(p, v) {
  const img = v.featured_image;
  if (img && typeof img === "object" && img.src) return img.src;
  if (v.id !== null && v.id !== undefined) {
    for (const image of imagesOf(p)) if ((image.variant_ids || []).includes(v.id)) return image.src;
  }
  return "";
}

const detailsOf = (p) => p._details || {};
const collectionsOf = (p) => (p._collections || []).filter((c) => c.title).map((c) => c.title);

function jsVariants(p) {
  const js = detailsOf(p).storefront_js || {};
  return new Map((js.variants || []).filter((v) => v && typeof v === "object").map((v) => [v.id, v]));
}

function barcode(p, v) {
  if (v.barcode) return v.barcode;
  const code = (jsVariants(p).get(v.id) || {}).barcode;
  if (code) return code;
  return v.sku ? ((detailsOf(p).gtin_by_sku || {})[String(v.sku)] || "") : "";
}

function mediaUrls(p) {
  const js = detailsOf(p).storefront_js || {};
  const urls = [];
  for (const m of js.media || []) {
    const kind = m.media_type;
    if (kind === "video" || kind === "model") {
      for (const s of m.sources || []) if (s.url) urls.push(s.url);
    } else if (kind === "external_video") {
      const vid = m.external_id, host = m.host;
      if (host === "youtube" && vid) urls.push(`https://www.youtube.com/watch?v=${vid}`);
      else if (host === "vimeo" && vid) urls.push(`https://vimeo.com/${vid}`);
      else if (vid) urls.push(String(vid));
    }
  }
  return urls;
}

export function plainText(html) {
  let t = (html || "").replace(/<br\s*\/?>|<\/p>|<\/li>|<\/div>|<\/h[1-6]>/gi, "\n");
  t = t.replace(/<[^>]+>/g, "");
  t = unescapeHtml(t);
  return t.replace(/[ \t]+/g, " ").replace(/\n\s*\n+/g, "\n").trim();
}

const unique = (seq) => { const seen = new Set(); return seq.filter((x) => x && !seen.has(x) && seen.add(x)); };
const kg = (grams) => (grams ? pf(Math.round((grams / 1000) * 1000) / 1000) : "");

// --------------------------------------------------------------------------- Shopify

const productUrl = (p, baseUrl = "") => p._url || (p.handle && baseUrl ? `${baseUrl}/products/${p.handle}` : "");

/** `includeLink` adds a trailing "Product URL" column on each product's first row. */
export function toShopifyCsv(products, includeLink = false, baseUrl = "") {
  const rows = [];
  for (const p of products) {
    const handle = p.handle || String(p.id);
    const variants = p.variants && p.variants.length ? p.variants : [{}];
    const images = imagesOf(p);
    const opts = optionsOf(p).length ? optionsOf(p) : [{ name: "Title" }];
    const d = detailsOf(p);
    const n = Math.max(variants.length, images.length, 1);
    for (let i = 0; i < n; i++) {
      const row = { Handle: handle };
      if (i === 0) {
        Object.assign(row, {
          Title: p.title || "", "Body (HTML)": p.body_html || "", Vendor: p.vendor || "", Type: p.product_type || "",
          Tags: tagsOf(p).join(", "), Published: "TRUE", "Gift Card": "FALSE",
          "SEO Title": d.seo_title || "", "SEO Description": d.seo_description || "", Status: "active",
        });
        if (includeLink) row[LINK_HEADER] = productUrl(p, baseUrl);
      }
      if (i < variants.length) {
        const v = variants[i];
        for (let idx = 0; idx < 3; idx++) {
          if (i === 0 && idx < opts.length) row[`Option${idx + 1} Name`] = opts[idx].name || "";
          if (idx < opts.length) row[`Option${idx + 1} Value`] = v[`option${idx + 1}`] || "";
        }
        Object.assign(row, {
          "Variant SKU": v.sku || "",
          "Variant Grams": v.grams !== null && v.grams !== undefined ? v.grams : "",
          "Variant Inventory Policy": "deny",
          "Variant Fulfillment Service": "manual",
          "Variant Price": v.price ?? "",
          "Variant Compare At Price": v.compare_at_price || "",
          "Variant Requires Shipping": (has(v, "requires_shipping") ? v.requires_shipping : true) ? "TRUE" : "FALSE",
          "Variant Taxable": (has(v, "taxable") ? v.taxable : true) ? "TRUE" : "FALSE",
          "Variant Barcode": barcode(p, v),
          "Variant Image": variantImage(p, v),
          "Variant Weight Unit": "kg",
        });
      }
      if (i < images.length) {
        row["Image Src"] = images[i].src;
        row["Image Position"] = i + 1;
        row["Image Alt Text"] = images[i].alt || "";
      }
      rows.push(row);
    }
  }
  return writeCsv(includeLink ? [...SHOPIFY_HEADERS, LINK_HEADER] : SHOPIFY_HEADERS, rows);
}

// --------------------------------------------------------------------------- WooCommerce

function wooPrice(v) {
  const price = dec(v.price), compare = dec(v.compare_at_price);
  if (price && compare && compare.n > price.n) return [price.s, compare.s];
  return ["", price ? price.s : ""];
}

const wooRowBase = () => ({
  Published: 1, "Is featured?": 0, "Visibility in catalog": "visible", "Tax status": "taxable",
  "Backorders allowed?": 0, "Sold individually?": 0, "Allow customer reviews?": 1,
});

function wooSourceRows(p, skuById) {
  const info = p._woo || {};
  const ptype = ["simple", "variable", "grouped", "external"].includes(info.type) ? info.type : "simple";
  const variants = p.variants || [];
  const attrs = info.attributes || [];
  const base = wooRowBase();
  base["Sold individually?"] = info.sold_individually ? 1 : 0;
  base["Backorders allowed?"] = info.is_on_backorder ? 1 : 0;

  const setAttr = (row, idx, a, value, dflt) => {
    row[`Attribute ${idx} name`] = a.name || "";
    row[`Attribute ${idx} value(s)`] = value;
    row[`Attribute ${idx} visible`] = 1;
    row[`Attribute ${idx} global`] = String(a.taxonomy || "").startsWith("pa_") ? 1 : 0;
    if (dflt !== undefined) row[`Attribute ${idx} default`] = dflt;
  };

  const parent = { ...base, Type: ptype, Name: p.title || "", "Short description": info.short_description || "",
    Description: p.body_html || "", "In stock?": info.is_in_stock === false ? 0 : 1,
    Categories: ((info.category_paths && info.category_paths.length) ? info.category_paths : collectionsOf(p)).join(", "),
    Tags: tagsOf(p).join(", "), Images: imagesOf(p).map((i) => i.src).join(", ") };
  attrs.forEach((a, k) => setAttr(parent, k + 1, a, (a.terms || []).join(", "), a.default || ""));

  if (ptype === "variable") {
    const real = variants.filter((v) => v.id !== p.id);
    const parentSku = info.sku || p.handle || `product-${p.id}`;
    parent.SKU = parentSku;
    const rows = [parent];
    const used = new Set([parentSku]);
    real.forEach((v, pos) => {
      const [sale, regular] = wooPrice(v);
      let sku = v.sku || "";
      if (used.has(sku)) sku = "";
      if (sku) used.add(sku);
      const row = { ...base, Type: "variation", SKU: sku, Name: `${p.title || ""} - ${v.title || ""}`,
        "In stock?": (v.available === undefined ? true : v.available) ? 1 : 0,
        "Weight (kg)": kg(v.grams), "Length (cm)": pf(v.length_cm), "Width (cm)": pf(v.width_cm), "Height (cm)": pf(v.height_cm),
        "Sale price": sale, "Regular price": regular, Images: variantImage(p, v), Parent: parentSku, Position: pos };
      const values = v._option_values || [];
      let n = 0;
      attrs.forEach((a, k) => {
        if (a.has_variations) { setAttr(row, k + 1, a, n < values.length ? values[n] : ""); n++; }
      });
      rows.push(row);
    });
    return rows;
  }

  const v = variants.length ? variants[0] : {};
  const [sale, regular] = wooPrice(v);
  Object.assign(parent, { SKU: v.sku || "", "Weight (kg)": kg(v.grams), "Length (cm)": pf(v.length_cm),
    "Width (cm)": pf(v.width_cm), "Height (cm)": pf(v.height_cm), "Sale price": sale, "Regular price": regular });
  if (ptype === "external") { parent["External URL"] = info.external_url || ""; parent["Button text"] = info.button_text || ""; }
  if (ptype === "grouped") {
    parent["Grouped products"] = (info.grouped_products || []).map((id) => skuById.get(id)).filter(Boolean).join(", ");
  }
  return [parent];
}

export function toWooCommerceCsv(products, includeLink = false, baseUrl = "") {
  const rows = [];
  let nAttr = WOO_ATTRIBUTES;
  const skuById = new Map();
  for (const p of products) {
    if (p._woo) {
      nAttr = Math.max(nAttr, (p._woo.attributes || []).length);
      skuById.set(p.id, p._woo.sku || "");
    }
  }
  for (const p of products) {
    const start = rows.length;
    appendWooRows(rows, p, skuById);
    if (includeLink && rows.length > start) rows[start][LINK_HEADER] = productUrl(p, baseUrl);
  }
  return writeCsv(includeLink ? [...wooHeaders(nAttr), LINK_HEADER] : wooHeaders(nAttr), rows);
}

function appendWooRows(rows, p, skuById) {
  {
    if (p._woo) { rows.push(...wooSourceRows(p, skuById)); return; }
    const variants = p.variants || [];
    const imageUrls = imagesOf(p).map((i) => i.src).join(", ");
    const categories = unique([p.product_type || "", ...collectionsOf(p)]).join(", ");
    const base = wooRowBase();
    const anyAvailable = variants.length ? variants.some((v) => (v.available === undefined ? true : v.available)) : true;

    if (!variants.length || (variants.length === 1 && isDefaultOnly(p))) {
      const v = variants.length ? variants[0] : {};
      const [sale, regular] = wooPrice(v);
      rows.push({ ...base, Type: "simple", SKU: v.sku || "", Name: p.title || "", Description: p.body_html || "",
        "In stock?": (v.available === undefined ? true : v.available) ? 1 : 0, "Weight (kg)": kg(v.grams),
        "Sale price": sale, "Regular price": regular, Categories: categories, Tags: tagsOf(p).join(", "), Images: imageUrls });
      return;
    }

    const opts = optionsOf(p);
    const parentSku = p.handle || `product-${p.id}`;
    const parent = { ...base, Type: "variable", SKU: parentSku, Name: p.title || "", Description: p.body_html || "",
      "In stock?": anyAvailable ? 1 : 0, Categories: categories, Tags: tagsOf(p).join(", "), Images: imageUrls };
    opts.forEach((opt, k) => {
      const values = opt.values || [];
      const i = k + 1;
      parent[`Attribute ${i} name`] = opt.name || "";
      parent[`Attribute ${i} value(s)`] = values.join(", ");
      parent[`Attribute ${i} visible`] = 1;
      parent[`Attribute ${i} global`] = 0;
      parent[`Attribute ${i} default`] = values.length ? values[0] : "";
    });
    rows.push(parent);

    const used = new Set([parentSku]);
    variants.forEach((v, pos) => {
      const [sale, regular] = wooPrice(v);
      let sku = v.sku || "";
      if (used.has(sku)) sku = "";
      if (sku) used.add(sku);
      const row = { ...base, Type: "variation", SKU: sku, Name: `${p.title || ""} - ${v.title || ""}`,
        "In stock?": (v.available === undefined ? true : v.available) ? 1 : 0, "Weight (kg)": kg(v.grams),
        "Sale price": sale, "Regular price": regular, Images: variantImage(p, v), Parent: parentSku, Position: pos };
      opts.forEach((opt, k) => {
        const i = k + 1;
        row[`Attribute ${i} name`] = opt.name || "";
        row[`Attribute ${i} value(s)`] = v[`option${i}`] || "";
        row[`Attribute ${i} visible`] = 1;
        row[`Attribute ${i} global`] = 0;
      });
      rows.push(row);
    });
  }
}

// --------------------------------------------------------------------------- full details

export function toFullCsv(products, baseUrl = "") {
  return writeCsv(FULL_HEADERS, fullRows(products, baseUrl));
}

export function fullRows(products, baseUrl = "") {
  const rows = [];
  for (const p of products) {
    const d = detailsOf(p);
    const variants = p.variants && p.variants.length ? p.variants : [{}];
    const images = imagesOf(p);
    const opts = optionsOf(p);
    const handles = (p._collections || []).map((c) => c.handle);
    const prices = variants.map((v) => dec(v.price)).filter(Boolean);
    const js = d.storefront_js || {};
    const info = p._woo || {};
    const isWoo = Object.keys(info).length > 0;
    const minP = prices.length ? prices.reduce((a, b) => (b.n < a.n ? b : a)) : null;
    const maxP = prices.length ? prices.reduce((a, b) => (b.n > a.n ? b : a)) : null;
    const productRow = {
      "Product ID": p.id ?? "", Handle: p.handle || "",
      "Product URL": p._url || (p.handle && baseUrl ? `${baseUrl}/products/${p.handle}` : ""),
      Title: p.title || "", Vendor: p.vendor || "", "Brand (structured data)": d.brand || "",
      "Product Type": p.product_type || "", Tags: tagsOf(p).join(", "),
      Collections: collectionsOf(p).join(" | "), "Collection Handles": handles.join(" | "),
      "Published At": p.published_at || "", "Created At": p.created_at || "", "Updated At": p.updated_at || "",
      "Description (HTML)": p.body_html || "", "Description (text)": plainText(p.body_html),
      "Option 1 Name": opts.length > 0 ? opts[0].name : "", "Option 2 Name": opts.length > 1 ? opts[1].name : "",
      "Option 3 Name": opts.length > 2 ? opts[2].name : "",
      "Product Min Price": minP ? minP.s : "", "Product Max Price": maxP ? maxP.s : "",
      "Variant Count": (p.variants || []).length, "Image Count": images.length,
      "Image URLs": images.map((i) => i.src).join(" | "), "Image Alt Texts": images.map((i) => i.alt || "").join(" | "),
      "Video / 3D Media URLs": mediaUrls(p).join(" | "),
      "SEO Title": d.seo_title || "", "SEO Description": d.seo_description || "",
      "Canonical URL": d.canonical || "", "OG Image": d.og_image || "",
      Rating: d.rating_value || (info.review_count ? (info.average_rating ?? "") : ""),
      "Review Count": d.review_count || info.review_count || "",
      "Requires Selling Plan": js.requires_selling_plan === undefined ? "" : (js.requires_selling_plan ? "True" : "False"),
      Platform: isWoo ? "WooCommerce" : "Shopify",
      "Source Product Type": info.type || "",
      "Stock Status": isWoo ? (info.is_on_backorder ? "onbackorder" : (info.is_in_stock ? "instock" : "outofstock")) : "",
      "Low Stock Remaining": info.low_stock_remaining ?? "",
      "Sold Individually": isWoo ? (info.sold_individually ? "TRUE" : "FALSE") : "",
      "Short Description (HTML)": info.short_description || "",
      "Short Description (text)": plainText(info.short_description),
      Attributes: (info.attributes || []).map((a) => `${a.name || ""}: ${(a.terms || []).join(", ")}`).join(" | "),
      "Category Paths": (info.category_paths || []).join(" | "),
      "External URL": info.external_url || "", "Button Text": info.button_text || "",
    };
    for (const v of variants) {
      const price = dec(v.price), compare = dec(v.compare_at_price);
      const grams = v.grams;
      rows.push({
        ...productRow,
        "Variant ID": v.id ?? "", "Variant Position": v.position ?? "", "Variant Title": v.title || "",
        "Option 1 Value": v.option1 || "", "Option 2 Value": v.option2 || "", "Option 3 Value": v.option3 || "",
        SKU: v.sku || "", "Barcode / GTIN": barcode(p, v), Price: v.price ?? "", "Compare At Price": v.compare_at_price || "",
        "On Sale": price && compare && compare.n > price.n ? "TRUE" : "FALSE",
        Currency: d.currency || info.currency_code || "",
        Available: v.available ? "TRUE" : "FALSE",
        "Requires Shipping": isWoo ? "" : ((has(v, "requires_shipping") ? v.requires_shipping : true) ? "TRUE" : "FALSE"),
        Taxable: isWoo ? "" : ((has(v, "taxable") ? v.taxable : true) ? "TRUE" : "FALSE"),
        "Weight (grams)": grams !== null && grams !== undefined ? grams : "",
        "Weight (kg)": kg(grams),
        "Length (cm)": pf(v.length_cm), "Width (cm)": pf(v.width_cm), "Height (cm)": pf(v.height_cm),
        "Variant Image": variantImage(p, v),
      });
    }
  }
  return rows;
}

// --------------------------------------------------------------------------- reviews, Excel, details view

export function reviewRows(products, baseUrl = "") {
  const rows = [];
  for (const p of products) {
    for (const r of p._reviews || []) {
      rows.push({
        "Product ID": p.id ?? "", "Product Handle": p.handle || "", "Product Title": p.title || "",
        "Product URL": productUrl(p, baseUrl), "Review ID": r.id ?? "", Reviewer: r.reviewer || "",
        Rating: r.rating ?? "", Date: r.date || "", Title: r.title || "", Review: r.body || "",
        Verified: r.verified || "", Source: r.source || "",
      });
    }
  }
  return rows;
}

export const toReviewsCsv = (products, baseUrl = "") => writeCsv(REVIEW_HEADERS, reviewRows(products, baseUrl));

const NUMBER_COLUMNS = ["Price", "Compare At Price", "Product Min Price", "Product Max Price", "Variant Count",
  "Image Count", "Weight (grams)", "Weight (kg)", "Length (cm)", "Width (cm)", "Height (cm)", "Review Count", "Rating"];

/** An Excel workbook: Products (same columns as the full-details CSV) plus Reviews when there are any. */
export function toXlsx(products, baseUrl = "") {
  const sheets = [{ name: "Products", headers: FULL_HEADERS, rows: fullRows(products, baseUrl), numberColumns: NUMBER_COLUMNS }];
  const reviews = reviewRows(products, baseUrl);
  if (reviews.length) sheets.push({ name: "Reviews", headers: REVIEW_HEADERS, rows: reviews, numberColumns: ["Rating"] });
  return writeXlsx(sheets);
}

/** Everything worth showing about one product, as plain text and numbers (safe to render without HTML). */
export function detailView(p, baseUrl = "") {
  const d = detailsOf(p);
  const info = p._woo || {};
  const isWoo = Object.keys(info).length > 0;
  return {
    title: p.title || "",
    url: productUrl(p, baseUrl),
    vendor: p.vendor || "",
    product_type: p.product_type || "",
    platform: isWoo ? "WooCommerce" : "Shopify",
    tags: tagsOf(p),
    collections: collectionsOf(p),
    description: plainText(p.body_html),
    short_description: plainText(info.short_description),
    options: optionsOf(p).map((o) => ({ name: o.name || "", values: o.values || [] })),
    attributes: (info.attributes || []).map((a) => ({ name: a.name || "", terms: a.terms || [] })),
    images: imagesOf(p).map((i) => ({ src: i.src, alt: i.alt || "" })),
    variants: (p.variants || []).map((v) => ({
      title: v.title || "", sku: v.sku || "", price: v.price || "", compare_at_price: v.compare_at_price || "",
      available: !!v.available, image: variantImage(p, v), grams: v.grams ?? null, barcode: barcode(p, v),
    })),
    rating: d.rating_value || (info.review_count ? info.average_rating : "") || "",
    review_count: d.review_count || info.review_count || "",
    reviews: (p._reviews || []).slice(0, 50).map((r) => ({
      reviewer: r.reviewer ?? "", rating: r.rating ?? "", date: r.date ?? "", title: r.title ?? "", body: r.body ?? "", verified: r.verified ?? "",
    })),
    review_total: (p._reviews || []).length,
    seo_title: d.seo_title || "",
    seo_description: d.seo_description || "",
  };
}

export function toFullJsonItem(p) {
  const item = {};
  for (const [k, v] of Object.entries(p)) if (!k.startsWith("_")) item[k] = v;
  item.collections = p._collections || [];
  if (p._url) item.url = p._url;
  if (p._woo) item.woocommerce = p._woo;
  if (p._details && Object.keys(p._details).length) item.details = p._details;
  if (p._reviews && p._reviews.length) item.reviews = p._reviews;
  return item;
}

export const toJson = (products) => JSON.stringify(products.map(toFullJsonItem), null, 2);
