// WooCommerce Store API reader. Produces the same product shape as the Shopify reader, plus `_woo`.
import { ScrapeError, Stopped, fetchRaw, parseJson, sleep, pool, text, toInt, isObj, unescapeHtml, NO_CTX } from "./util.js";
import { pageDetails } from "./html.js";

export const PRODUCTS_ROUTE = "/wc/store/v1/products";
export const CATEGORIES_ROUTE = "/wc/store/v1/products/categories";
export const PAGE_SIZE = 100;
const PROBE_STATUSES = [400, 401, 403, 404, 405, 410];
const BLOCKED_CODES = new Set(["rest_login_required", "rest_cannot_access", "rest_forbidden", "rest_not_logged_in"]);

const wpMessage = (body) => { const d = parseJson(body); return isObj(d) ? text(d.message) : ""; };
const stripWww = (h) => (h || "").toLowerCase().replace(/^www\./, "");
function sameSite(base, url) {
  try { const a = stripWww(new URL(base).hostname); return !!a && a === stripWww(new URL(url).hostname); } catch { return false; }
}

export class WooApi {
  constructor(root) { this.root = root.replace(/\/+$/, ""); }
  url(route, params = {}) {
    const q = new URLSearchParams();
    for (const [k, v] of Object.entries(params)) if (v !== null && v !== undefined) q.append(k, String(v));
    const query = q.toString();
    const joiner = this.root.includes("rest_route=") ? "&" : "?";
    return `${this.root}${route}` + (query ? joiner + query : "");
  }
}

async function probeJson(url, ctx) {
  const { status, body } = await fetchRaw(url, { retries: 1, allowStatuses: PROBE_STATUSES }, ctx);
  return { status, data: parseJson(body) };
}

function rootFromHref(base, href) {
  let abs;
  try { abs = new URL(href, base + "/").href; } catch { return null; }
  if (!sameSite(base, abs)) return null;
  const i = abs.indexOf("rest_route=");
  return i >= 0 ? abs.slice(0, i + "rest_route=".length) : abs.replace(/\/+$/, "");
}

async function discoverRoot(base, ctx) {
  let res;
  try { res = await fetchRaw(base + "/", { retries: 1, allowStatuses: PROBE_STATUSES, accept: "text/html" }, ctx); }
  catch (e) { if (e instanceof Stopped) throw e; return null; }
  if (res.status !== 200) return null;
  const page = res.body.slice(0, 300000);
  for (const tag of page.match(/<link\b[^>]*>/gi) || []) {
    if (/rel=["']https:\/\/api\.w\.org\/?["']/i.test(tag)) {
      const m = /href=["']([^"']+)["']/i.exec(tag);
      if (m) return rootFromHref(base, unescapeHtml(m[1]));
    }
  }
  return null;
}

/** {api, reason}: api is a WooApi when the public product API works. */
export async function findApi(base, ctx = NO_CTX) {
  let blocked = false;
  const tried = new Set();
  const candidates = async function* () {
    yield `${base}/wp-json`;
    const found = await discoverRoot(base, ctx);
    if (found) yield found;
    yield `${base}/?rest_route=`;
  };
  for await (const root of candidates()) {
    if (tried.has(root)) continue;
    tried.add(root);
    const api = new WooApi(root);
    const { status, data } = await probeJson(api.url(PRODUCTS_ROUTE, { per_page: 1 }), ctx);
    if (status === 200 && Array.isArray(data)) return { api, reason: null };
    if (status === 401 || status === 403 || (isObj(data) && BLOCKED_CODES.has(data.code))) blocked = true;
  }
  return {
    api: null,
    reason: blocked
      ? "This store's public product API is switched off or blocked, so its products can't be read."
      : "No WooCommerce product API was found at that address (it needs a reasonably recent WooCommerce).",
  };
}

/** {platform: "shopify"|"woocommerce", api}. Throws when it is neither. */
export async function detectPlatform(base, ctx = NO_CTX) {
  const { status, data } = await probeJson(`${base}/products.json?limit=1`, ctx);
  if (status === 200 && isObj(data) && Array.isArray(data.products)) return { platform: "shopify", api: null };
  const { api, reason } = await findApi(base, ctx);
  if (api) return { platform: "woocommerce", api };
  throw new ScrapeError(`Couldn't detect a Shopify or WooCommerce store at that address. ${reason} If you know which platform it is, choose it manually.`);
}

// --------------------------------------------------------------------------- categories

export async function fetchCategories(api, { delay = 0.3 } = {}, ctx = NO_CTX) {
  const out = [], seen = new Set();
  let page = 1;
  while (!ctx.stopped()) {
    const headers = {};
    const { status, body } = await fetchRaw(api.url(CATEGORIES_ROUTE, { per_page: PAGE_SIZE, page }), { headersOut: headers, allowStatuses: [400] }, ctx);
    if (status === 400) {
      if (page > 1) break;
      throw new ScrapeError(wpMessage(body) || "The store rejected the category request.");
    }
    const data = parseJson(body);
    if (!Array.isArray(data)) throw new ScrapeError("The store didn't return a category list.");
    let added = 0;
    for (const c of data) {
      if (isObj(c) && !seen.has(c.id)) {
        seen.add(c.id); added++;
        out.push({ id: c.id, slug: c.slug || "", title: text(c.name), parent: c.parent || 0, count: c.count ?? null });
      }
    }
    const totalPages = toInt(headers["x-wp-totalpages"]);
    const done = totalPages ? page >= totalPages : data.length < PAGE_SIZE;
    if (added === 0 || done) break;
    page++;
    await sleep(delay, ctx);
  }
  return out;
}

/** category id -> "Parent > Child" */
export function categoryPaths(categories) {
  const byId = new Map(categories.map((c) => [c.id, c]));
  const paths = new Map();
  for (const c of categories) {
    const parts = [], seen = new Set();
    let cur = c;
    while (cur && !seen.has(cur.id)) { seen.add(cur.id); parts.push(cur.title); cur = byId.get(cur.parent); }
    paths.set(c.id, parts.reverse().join(" > "));
  }
  return paths;
}

export function resolveCategory(categories, value) {
  value = String(value ?? "").trim();
  if (/^\d+$/.test(value)) return parseInt(value, 10);
  for (const c of categories) if (c.slug.toLowerCase() === value.toLowerCase()) return c.id;
  throw new ScrapeError("That category wasn't found in the store.", 404);
}

// --------------------------------------------------------------------------- products

function slim(item) {
  for (const img of item.images || []) { delete img.srcset; delete img.sizes; delete img.thumbnail; }
  delete item.price_html;
  return item;
}

export async function fetchProducts(api, { categoryId = null, pageFrom = 1, pageTo = null, delay = 0.4, progress, out = [] } = {}, ctx = NO_CTX) {
  const seen = new Set(out.map((p) => p.id));
  const first = Math.max(1, parseInt(pageFrom || 1, 10));
  let page = first;
  for (;;) {
    if (ctx.stopped()) break;
    if (pageTo && page > parseInt(pageTo, 10)) break;
    const headers = {};
    const url = api.url(PRODUCTS_ROUTE, { per_page: PAGE_SIZE, page, category: categoryId });
    const { status, body } = await fetchRaw(url, { headersOut: headers, allowStatuses: [400] }, ctx);
    if (status === 400) {
      if (page > first) break; // asked for a page past the end
      throw new ScrapeError(wpMessage(body) || "The store rejected the product request.");
    }
    const items = parseJson(body);
    if (!Array.isArray(items)) throw new ScrapeError("The store returned data in an unexpected format.");
    let added = 0;
    for (const raw of items) {
      if (isObj(raw) && !seen.has(raw.id)) { seen.add(raw.id); out.push(slim(raw)); added++; }
    }
    const totalPages = toInt(headers["x-wp-totalpages"]);
    if (progress) progress({ page, count: out.length, total: toInt(headers["x-wp-total"]) || null });
    const done = totalPages ? page >= totalPages : items.length < PAGE_SIZE;
    if (added === 0 || done) break;
    page++;
    await sleep(delay, ctx);
  }
  return out;
}

async function productVariations(api, product, ctx) {
  const ids = (product.variations || []).map((v) => v.id).filter((id) => id !== null && id !== undefined);
  const wanted = new Set(ids);
  const got = new Map();
  let page = 1;
  for (;;) {
    const { status, body } = await fetchRaw(
      api.url(PRODUCTS_ROUTE, { type: "variation", parent: product.id, per_page: PAGE_SIZE, page }),
      { allowStatuses: [400, 404] }, ctx);
    if (status !== 200) break;
    const items = parseJson(body);
    if (!Array.isArray(items) || !items.length) break;
    let added = 0;
    for (const it of items) {
      if (isObj(it) && wanted.has(it.id) && !got.has(it.id)) { got.set(it.id, slim(it)); added++; }
    }
    if (items.length < PAGE_SIZE || added === 0) break;
    page++;
  }
  for (const vid of ids) {
    if (got.has(vid)) continue;
    const { status, body } = await fetchRaw(api.url(`${PRODUCTS_ROUTE}/${vid}`), { allowStatuses: [400, 404] }, ctx);
    if (status === 200) {
      const it = parseJson(body);
      if (isObj(it) && it.id === vid) got.set(vid, slim(it));
    }
  }
  return { variations: ids.filter((i) => got.has(i)).map((i) => got.get(i)), missing: ids.length - got.size };
}

/** Attach `_variations` to each variable product. Resolves to the number of products with unreadable variations. */
export async function fetchVariations(api, products, { workers = 4, delay = 0.15, progress } = {}, ctx = NO_CTX) {
  const targets = products.filter((p) => p.type === "variable" && p.variations && p.variations.length);
  let done = 0, failed = 0;
  await pool(targets, workers, async (p) => {
    if (ctx.stopped()) return;
    let bad = false;
    try {
      const { variations, missing } = await productVariations(api, p, ctx);
      p._variations = variations;
      if (missing) { p._variation_error = `${missing} variation(s) could not be read`; bad = true; }
    } catch (e) {
      if (e instanceof Stopped) return;
      if (e instanceof ScrapeError) { p._variation_error = e.message; bad = true; } else throw e;
    }
    await sleep(delay, ctx);
    done++;
    if (bad) failed++;
    if (progress) progress({ done, total: targets.length });
  });
  return failed;
}

// --------------------------------------------------------------------------- normalising

/** Store API prices are integers in the currency's minor unit ("1800" -> "18.00"). */
export function toMoney(value, minor = 2) {
  if (value === null || value === undefined || value === "") return "";
  minor = toInt(minor, 2);
  const s = String(value).trim();
  if (/^-?\d+$/.test(s)) {
    const neg = s.startsWith("-");
    let digits = s.replace("-", "").replace(/^0+(?=\d)/, "");
    if (minor === 0) return (neg ? "-" : "") + digits;
    digits = digits.padStart(minor + 1, "0");
    return (neg ? "-" : "") + digits.slice(0, -minor) + "." + digits.slice(-minor);
  }
  const n = Number(s);
  return Number.isFinite(n) ? (n / 10 ** minor).toFixed(minor) : "";
}

function pricePair(prices, onSale, minor) {
  const price = toMoney(prices.price, minor);
  const regular = toMoney(prices.regular_price, minor);
  let compare = null;
  if (onSale && price && regular && Number(regular) > Number(price)) compare = regular;
  return [price, compare];
}

function variationValues(varAttrs, listingAttrs) {
  const chosen = {};
  for (const a of listingAttrs || []) if (isObj(a)) chosen[text(a.name).toLowerCase()] = text(a.value);
  return varAttrs.map((attr) => {
    const value = chosen[text(attr.name).toLowerCase()] ?? "";
    const lookup = {};
    for (const t of attr.terms || []) {
      const name = text(t.name), slug = text(t.slug).toLowerCase();
      if (slug) lookup[slug] = name;
      lookup[name.toLowerCase()] = name;
    }
    return lookup[value.toLowerCase()] ?? value;
  });
}

/** Convert one Store API product into the shared product shape (plus `_woo`). */
export function normalize(raw, paths = new Map()) {
  const prices = raw.prices || {};
  const minor = toInt(prices.currency_minor_unit, 2);
  const ptype = raw.type || "simple";
  const inStock = raw.is_in_stock === undefined ? true : !!raw.is_in_stock;
  const onSale = !!raw.on_sale;

  const images = [];
  for (const im of raw.images || []) {
    if (isObj(im) && im.src) images.push({ id: im.id, src: im.src, position: images.length + 1, alt: im.alt || null });
  }
  const firstSrc = images.length ? images[0].src : null;

  const attrs = (raw.attributes || []).filter((a) => isObj(a) && a.name);
  attrs.sort((a, b) => Number(!a.has_variations) - Number(!b.has_variations));
  const varAttrs = attrs.filter((a) => a.has_variations);
  let options = varAttrs.map((a, i) => ({ name: text(a.name), position: i + 1, values: (a.terms || []).map((t) => text(t.name)) }));

  const variants = [];
  const listing = new Map();
  for (const v of raw.variations || []) if (isObj(v)) listing.set(v.id, v);
  if (ptype === "variable" && listing.size) {
    const fetched = new Map((raw._variations || []).map((v) => [v.id, v]));
    let pos = 0;
    for (const [vid, entry] of listing) {
      pos++;
      const vraw = fetched.get(vid) || {};
      const hasRaw = Object.keys(vraw).length > 0;
      const values = variationValues(varAttrs, entry.attributes);
      const vprices = vraw.prices || {};
      const [price, compare] = hasRaw ? pricePair(vprices, vraw.on_sale, toInt(vprices.currency_minor_unit, minor)) : ["", null];
      const vimgs = (vraw.images || []).filter((im) => isObj(im) && im.src);
      const featured = vimgs.length && vimgs[0].src !== firstSrc ? { src: vimgs[0].src } : null;
      variants.push({
        id: vid,
        title: values.filter(Boolean).join(" / ") || "Default Title",
        option1: values.length > 0 ? values[0] : null,
        option2: values.length > 1 ? values[1] : null,
        option3: values.length > 2 ? values[2] : null,
        _option_values: values,
        sku: vraw.sku || "",
        price,
        compare_at_price: compare,
        grams: null,
        available: hasRaw ? (vraw.is_in_stock === undefined ? true : !!vraw.is_in_stock) : inStock,
        position: pos,
        featured_image: featured,
      });
    }
  }
  if (!variants.length) {
    const [price, compare] = ptype === "grouped" ? ["", null] : pricePair(prices, onSale, minor);
    options = [{ name: "Title", position: 1, values: ["Default Title"] }];
    variants.push({
      id: raw.id, title: "Default Title", option1: "Default Title", sku: raw.sku || "", price,
      compare_at_price: compare, grams: null, available: inStock, position: 1, featured_image: null,
    });
  }

  const cats = (raw.categories || []).filter(isObj);
  const catPaths = cats.map((c) => paths.get(c.id) || text(c.name));
  let productType = "";
  if (cats.length) {
    let best = 0;
    catPaths.forEach((p, i) => { if ((p.match(/>/g) || []).length > (catPaths[best].match(/>/g) || []).length) best = i; });
    productType = text(cats[best].name);
  }

  const range = isObj(prices.price_range) ? prices.price_range : {};
  const lo = toMoney(range.min_amount || prices.price, minor);
  const hi = toMoney(range.max_amount || prices.price, minor);
  const atc = isObj(raw.add_to_cart) ? raw.add_to_cart : {};

  const info = {
    type: ptype,
    sku: raw.sku || "",
    short_description: raw.short_description || "",
    on_sale: onSale,
    average_rating: raw.average_rating || "",
    review_count: toInt(raw.review_count),
    is_purchasable: raw.is_purchasable ?? null,
    is_in_stock: inStock,
    is_on_backorder: !!raw.is_on_backorder,
    low_stock_remaining: raw.low_stock_remaining ?? null,
    sold_individually: !!raw.sold_individually,
    currency_code: prices.currency_code || "",
    currency_symbol: prices.currency_symbol || "",
    price_min: lo ? parseFloat(lo) : null,
    price_max: hi ? parseFloat(hi) : null,
    attributes: attrs.map((a) => ({
      name: text(a.name),
      taxonomy: a.taxonomy || "",
      has_variations: !!a.has_variations,
      terms: (a.terms || []).map((t) => text(t.name)),
      default: (() => { const t = (a.terms || []).find((x) => x.default); return t ? text(t.name) : ""; })(),
    })),
    category_paths: catPaths,
    brands: (raw.brands || []).filter(isObj).map((b) => text(b.name)),
    external_url: ptype === "external" ? atc.url : "",
    button_text: ptype === "external" ? atc.text : "",
    grouped_products: (raw.grouped_products || []).filter((i) => Number.isInteger(i)),
    variation_error: raw._variation_error || "",
    extensions: raw.extensions || {},
  };
  return {
    id: raw.id,
    handle: raw.slug || String(raw.id),
    title: text(raw.name),
    body_html: raw.description || raw.short_description || "",
    vendor: info.brands.length ? info.brands[0] : "",
    product_type: productType,
    tags: (raw.tags || []).filter(isObj).map((t) => text(t.name)),
    options,
    variants,
    images,
    _url: raw.permalink || "",
    _collections: cats.map((c) => ({ handle: c.slug || "", title: text(c.name) })),
    _woo: info,
  };
}

// --------------------------------------------------------------------------- deep details

const UNIT_GRAMS = { kg: 1000, g: 1, lbs: 453.592, lb: 453.592, oz: 28.3495 };
const UNIT_CM = { cm: 1, mm: 0.1, m: 100, in: 2.54, yd: 91.44 };

function pyRound(x) {
  const r = Math.round(x);
  return Math.abs(x % 1) === 0.5 && r % 2 !== 0 ? r - 1 : r;
}

function number(s) {
  s = s.trim();
  if (s.includes(",") && s.includes(".")) {
    s = s.lastIndexOf(",") > s.lastIndexOf(".") ? s.replace(/\./g, "").replace(",", ".") : s.replace(/,/g, "");
  } else if (s.includes(",")) {
    const i = s.lastIndexOf(",");
    const head = s.slice(0, i), tail = s.slice(i + 1);
    s = tail.length <= 2 ? `${head.replace(/,/g, "")}.${tail}` : s.replace(/,/g, "");
  }
  return /^[+-]?(\d+\.?\d*|\.\d+)$/.test(s) ? parseFloat(s) : null;
}

export function parseWeightGrams(t) {
  const m = /(\d[\d.,]*)\s*(kg|g|lbs?|oz)\b/i.exec(t || "");
  if (!m) return null;
  const n = number(m[1]);
  return n === null ? null : pyRound(n * UNIT_GRAMS[m[2].toLowerCase()]);
}

export function parseDimensionsCm(t) {
  const m = /(\d[\d.,]*)\s*[×xX]\s*(\d[\d.,]*)\s*[×xX]\s*(\d[\d.,]*)\s*(cm|mm|m|in|yd)\b/.exec(t || "");
  if (!m) return null;
  const nums = [number(m[1]), number(m[2]), number(m[3])];
  if (nums.includes(null)) return null;
  const f = UNIT_CM[m[4].toLowerCase()];
  return nums.map((n) => Math.round(n * f * 100) / 100);
}

const clean = (frag) => unescapeHtml((frag || "").replace(/<[^>]+>/g, " ")).replace(/\s+/g, " ").trim();

function rowText(page, key) {
  const m = new RegExp(`woocommerce-product-attributes-item--${key}\\b[\\s\\S]*?<td[^>]*>([\\s\\S]*?)</td>`, "i").exec(page);
  return m ? clean(m[1]) : "";
}

function variationData(page) {
  const m = /data-product_variations=(["'])([\s\S]*?)\1/.exec(page);
  if (!m) return new Map();
  const data = parseJson(unescapeHtml(m[2]));
  if (!Array.isArray(data)) return new Map();
  return new Map(data.filter(isObj).map((d) => [d.variation_id, d]));
}

function applyPageData(product, page) {
  const weightText = rowText(page, "weight");
  const dimsText = rowText(page, "dimensions");
  const per = variationData(page);
  for (const v of product.variants || []) {
    const vd = per.get(v.id) || {};
    let grams = parseWeightGrams(clean(vd.weight_html));
    if (grams === null) grams = parseWeightGrams(weightText);
    const dims = parseDimensionsCm(clean(vd.dimensions_html)) || parseDimensionsCm(dimsText);
    if (grams !== null) v.grams = grams;
    if (dims) [v.length_cm, v.width_cm, v.height_cm] = dims;
  }
}

export async function fetchDetails(base, products, { workers = 4, delay = 0.15, progress } = {}, ctx = NO_CTX) {
  let done = 0;
  await pool(products, workers, async (p) => {
    if (ctx.stopped()) return;
    let details = {};
    const url = p._url;
    if (url && sameSite(base, url)) {
      try {
        const { status, body } = await fetchRaw(url, { retries: 3, allow404: true, accept: "text/html,application/json" }, ctx);
        if (status === 200) { details = pageDetails(body); applyPageData(p, body); }
      } catch (e) {
        if (e instanceof Stopped) return;
        if (e instanceof ScrapeError) details = { page_error: e.message }; else throw e;
      }
    }
    p._details = details;
    await sleep(delay, ctx);
    done++;
    if (progress) progress({ done, total: products.length });
  });
}
