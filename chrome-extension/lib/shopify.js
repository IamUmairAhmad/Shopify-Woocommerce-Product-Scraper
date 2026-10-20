// Shopify storefront JSON: /products.json, /collections.json, per-collection feeds, product pages.
import { ScrapeError, fetchRaw, parseJson, sleep, pool, Stopped, NO_CTX } from "./util.js";
import { pageDetails } from "./html.js";

export const PAGE_SIZE = 250;
export const HANDLE_RE = /^[A-Za-z0-9][A-Za-z0-9._~%-]*$/;

/** "store.com", a full URL, or a collection / WooCommerce category URL -> {base, collection}. */
export function parseStoreInput(raw) {
  raw = String(raw ?? "").trim();
  if (!raw) throw new ScrapeError("Enter a store domain, for example store.com.");
  if (!raw.includes("://")) raw = "https://" + raw;
  let u;
  try { u = new URL(raw); } catch { throw new ScrapeError("That doesn't look like a valid store address."); }
  const host = u.hostname.toLowerCase().replace(/\.$/, "");
  if (!host || (!host.includes(".") && host !== "localhost")) {
    throw new ScrapeError("That doesn't look like a valid store domain.");
  }
  if (u.protocol !== "http:" && u.protocol !== "https:") throw new ScrapeError("Only http and https addresses are supported.");
  const base = `${u.protocol}//${u.host}`;

  const path = (u.pathname || "").replace(/\/{2,}/g, "/");
  let collection = null;
  const m = /^(?:\/[A-Za-z]{2}(?:-[A-Za-z]{2,4})?)?\/collections\/([^/?#]+)/.exec(path);
  if (m && HANDLE_RE.test(m[1])) {
    collection = m[1];
  } else {
    const wm = /\/product-category\/([^?#]+)/.exec(path);
    let slug = wm ? wm[1].replace(/\/+$/, "").split("/").pop() : "";
    if (!slug) slug = u.searchParams.get("product_cat") || "";
    if (slug && HANDLE_RE.test(slug)) collection = slug;
  }
  return { base, collection };
}

async function getJson(url, ctx, retries = 4) {
  const { body } = await fetchRaw(url, { retries }, ctx);
  try { return JSON.parse(body); } catch {
    throw new ScrapeError("The store didn't return product data. It may not be a Shopify store, or its product feed is disabled.");
  }
}

export async function fetchProducts(base, { collection = null, pageFrom = 1, pageTo = null, delay = 0.4, progress, out = [] } = {}, ctx = NO_CTX) {
  if (collection && !HANDLE_RE.test(collection)) throw new ScrapeError("Invalid collection handle.");
  let page = Math.max(1, parseInt(pageFrom || 1, 10));
  const prefix = collection ? `${base}/collections/${collection}` : base;
  const seen = new Set(out.map((p) => p.id));
  for (;;) {
    if (ctx.stopped()) break;
    if (pageTo && page > parseInt(pageTo, 10)) break;
    const data = await getJson(`${prefix}/products.json?limit=${PAGE_SIZE}&page=${page}`, ctx);
    const items = data && Array.isArray(data.products) ? data.products : null;
    if (!items) throw new ScrapeError("The store returned data in an unexpected format.");
    let added = 0;
    for (const p of items) {
      if (!seen.has(p.id)) { seen.add(p.id); out.push(p); added++; }
    }
    if (progress) progress({ page, count: out.length });
    if (items.length < PAGE_SIZE || added === 0) break;
    page++;
    await sleep(delay, ctx);
  }
  return out;
}

export async function fetchCollections(base, { delay = 0.3 } = {}, ctx = NO_CTX) {
  const out = [], seen = new Set();
  let page = 1;
  while (!ctx.stopped()) {
    const data = await getJson(`${base}/collections.json?limit=${PAGE_SIZE}&page=${page}`, ctx);
    const items = data && Array.isArray(data.collections) ? data.collections : null;
    if (!items) throw new ScrapeError("The store didn't return a collection list.");
    let added = 0;
    for (const c of items) {
      if (c.handle && !seen.has(c.handle)) {
        seen.add(c.handle); added++;
        out.push({ handle: c.handle, title: c.title || c.handle, products_count: c.products_count ?? null });
      }
    }
    if (items.length < PAGE_SIZE || added === 0) break;
    page++;
    await sleep(delay, ctx);
  }
  return out;
}

/** Map product id -> [{handle, title}] of every collection containing it. Returns {membership, warnings}. */
export async function fetchCollectionMembership(base, { progress, delay = 0.3 } = {}, ctx = NO_CTX) {
  const warnings = [];
  let collections;
  try { collections = await fetchCollections(base, { delay }, ctx); } catch (e) {
    if (e instanceof Stopped) throw e;
    return { membership: new Map(), warnings: [`Collections unavailable: ${e.message}`] };
  }
  const membership = new Map();
  for (let idx = 0; idx < collections.length; idx++) {
    if (ctx.stopped()) break;
    const col = collections[idx];
    if (progress) progress({ done: idx, total: collections.length, title: col.title });
    if (!HANDLE_RE.test(col.handle)) continue;
    let page = 1;
    try {
      while (!ctx.stopped()) {
        const data = await getJson(`${base}/collections/${col.handle}/products.json?limit=${PAGE_SIZE}&page=${page}`, ctx);
        const items = data && Array.isArray(data.products) ? data.products : null;
        if (!items || !items.length) break;
        let added = 0;
        for (const p of items) {
          const list = membership.get(p.id) || [];
          membership.set(p.id, list);
          if (!list.some((e) => e.handle === col.handle && e.title === col.title)) {
            list.push({ handle: col.handle, title: col.title });
            added++;
          }
        }
        if (items.length < PAGE_SIZE || added === 0) break;
        page++;
        await sleep(delay, ctx);
      }
    } catch (e) {
      if (e instanceof Stopped) throw e;
      if (e instanceof ScrapeError) warnings.push(`Collection '${col.title}' skipped: ${e.message}`);
      else throw e;
    }
    await sleep(delay, ctx);
  }
  if (progress) progress({ done: collections.length, total: collections.length, title: "" });
  return { membership, warnings };
}

async function fetchOneDetails(base, product, ctx) {
  const handle = product.handle;
  const details = {};
  if (!handle || !HANDLE_RE.test(handle)) return details;
  try {
    const { status, body } = await fetchRaw(`${base}/products/${handle}.js`, { retries: 3, allow404: true }, ctx);
    if (status === 200) {
      const js = JSON.parse(body);
      delete js.description;
      details.storefront_js = js;
    }
  } catch (e) {
    if (e instanceof Stopped) throw e;
    details.js_error = e.message;
  }
  try {
    const { status, body } = await fetchRaw(`${base}/products/${handle}`, { retries: 3, allow404: true, accept: "text/html,application/json" }, ctx);
    if (status === 200) Object.assign(details, pageDetails(body));
  } catch (e) {
    if (e instanceof Stopped) throw e;
    if (e instanceof ScrapeError) details.page_error = e.message; else throw e;
  }
  return details;
}

export async function fetchDetails(base, products, { workers = 4, delay = 0.15, progress } = {}, ctx = NO_CTX) {
  let done = 0;
  await pool(products, workers, async (p) => {
    if (ctx.stopped()) return;
    try {
      p._details = await fetchOneDetails(base, p, ctx);
    } catch (e) {
      if (e instanceof Stopped) return;
      if (e instanceof ScrapeError) p._details = { error: e.message }; else throw e;
    }
    await sleep(delay, ctx);
    done++;
    if (progress) progress({ done, total: products.length });
  });
}
