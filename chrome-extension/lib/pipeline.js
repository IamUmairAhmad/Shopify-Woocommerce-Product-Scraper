// Runs a scrape end to end for either platform.
import { ScrapeError, Stopped, NO_CTX } from "./util.js";
import * as shopify from "./shopify.js";
import * as woo from "./woo.js";

const noop = () => {};
export const nullReport = { platform: noop, phase: noop, message: noop, update: noop, warn: noop };

/**
 * Scrape a store and fill `products` (pass an array to keep partial results if it fails or is stopped).
 * platform: "auto" | "shopify" | "woocommerce". Resolves to the platform that was scraped.
 * `collections` are Shopify collection handles or WooCommerce category slugs; `handles` are specific
 * product handles (when given, only those are fetched); `search` is a keyword (every word must match).
 */
export async function run(base, opts = {}, report = nullReport, ctx = NO_CTX, products = []) {
  const {
    pageFrom = 1, pageTo = null, includeCollections = true, deep = false, reviews = false,
    platform: wanted = "auto", variations = true,
  } = opts;
  const collections = (opts.collections || []).filter(Boolean);
  const handles = (opts.handles || []).filter(Boolean);
  const search = String(opts.search || "").trim();
  report = { ...nullReport, ...report };
  let platform = wanted, api = null;
  try {
    if (platform === "auto") {
      report.phase("detect");
      report.message("Detecting platform…");
      ({ platform, api } = await woo.detectPlatform(base, ctx));
    } else if (platform === "woocommerce") {
      const found = await woo.findApi(base, ctx);
      if (!found.api) throw new ScrapeError(found.reason);
      api = found.api;
    } else if (platform !== "shopify") {
      throw new ScrapeError("Unknown platform.");
    }
    report.platform(platform);
    const sel = { collections, handles, search, pageFrom, pageTo };
    if (platform === "shopify") {
      await runShopify(base, sel, includeCollections, deep, reviews, report, ctx, products);
    } else {
      await runWoo(api, base, sel, includeCollections, deep, reviews, variations, report, ctx, products);
    }
  } catch (e) {
    if (!(e instanceof Stopped)) throw e; // Stop keeps whatever was fetched
  }
  return platform;
}

function warnMissing(report, missing) {
  if (!missing.length) return;
  const shown = missing.slice(0, 10).join(", ") + (missing.length > 10 ? ` and ${missing.length - 10} more` : "");
  report.warn(`${missing.length} product(s) were not found: ${shown}`);
}

function merge(products, seen, items) {
  for (const it of items) if (!seen.has(it.id)) { seen.add(it.id); products.push(it); }
}

const attachPageReviews = (products) => { for (const p of products) p._reviews = [...((p._details || {}).reviews || [])]; };

async function runShopify(base, sel, includeCollections, deep, reviews, report, ctx, products) {
  report.phase("products");
  const onProducts = ({ page, count }) => {
    report.update({ pages: page, products: count });
    report.message(`Fetching products… page ${page}, ${count} found`);
  };
  if (sel.handles.length) {
    const { missing } = await shopify.fetchProductsByHandle(base, sel.handles, { progress: onProducts, out: products }, ctx);
    warnMissing(report, missing);
  } else if (sel.collections.length > 1) {
    // One list per collection so overlap between collections can't end a collection early.
    const seen = new Set();
    for (const col of sel.collections) {
      if (ctx.stopped()) break;
      const part = [];
      try {
        await shopify.fetchProducts(base, { collection: col, pageFrom: sel.pageFrom, pageTo: sel.pageTo, progress: onProducts, out: part }, ctx);
      } finally { merge(products, seen, part); }
    }
  } else {
    await shopify.fetchProducts(base, { collection: sel.collections[0] || null, pageFrom: sel.pageFrom, pageTo: sel.pageTo, progress: onProducts, out: products }, ctx);
  }
  if (sel.search) {
    const kept = products.filter((p) => shopify.productMatches(p, sel.search));
    products.splice(0, products.length, ...kept);
  }
  if (!products.length) throw new ScrapeError("No products found for that selection.", 404);

  if (includeCollections && !ctx.stopped()) {
    report.phase("collections");
    const { membership, warnings } = await shopify.fetchCollectionMembership(base, {
      progress: ({ done, total, title }) => {
        report.update({ collections_done: done, collections_total: total });
        report.message(`Mapping collections… ${done}/${total}` + (title ? ` (${title})` : ""));
      },
    }, ctx);
    warnings.slice(0, 20).forEach((w) => report.warn(w));
    if (warnings.length > 20) report.warn(`…and ${warnings.length - 20} more collection warnings.`);
    for (const p of products) p._collections = membership.get(p.id) || [];
  }

  if ((deep || reviews) && !ctx.stopped()) {
    report.phase(deep ? "details" : "reviews");
    await shopify.fetchDetails(base, products, {
      pageOnly: !deep,
      progress: ({ done, total }) => {
        report.update({ details_done: done, details_total: total });
        report.message(`Fetching product ${deep ? "details" : "reviews"}… ${done}/${total}`);
      },
    }, ctx);
    const failed = products.filter((p) => (p._details || {}).page_error).length;
    if (failed) report.warn(`${failed} product page(s) could not be read` + (deep ? " for SEO details." : " for reviews."));
    if (reviews) {
      attachPageReviews(products);
      if (!products.some((p) => p._reviews.length)) {
        report.warn("No reviews were found. Shopify has no public reviews feed: reviews come from apps such as Judge.me or Yotpo, and only appear here when the product page publishes them as structured data.");
      }
    }
  }
}

async function runWoo(api, base, sel, includeCollections, deep, reviews, variations, report, ctx, products) {
  let categories = [], paths = new Map();
  const collections = sel.collections;
  if (collections.length || includeCollections) {
    report.phase("categories");
    report.message("Reading categories…");
    try {
      categories = await woo.fetchCategories(api, {}, ctx);
      paths = woo.categoryPaths(categories);
    } catch (e) {
      if (e instanceof Stopped) throw e;
      if (collections.length || !(e instanceof ScrapeError)) throw e;
      report.warn(`Category hierarchy unavailable, so categories are listed without parents: ${e.message}`);
    }
  }
  const categoryIds = collections.map((c) => woo.resolveCategory(categories, c));

  report.phase("products");
  const onProducts = ({ page, count, total }) => {
    report.update({ pages: page, products: count });
    report.message(`Fetching products… page ${page}, ${count}` + (total ? ` of ${total}` : "") + " found");
  };
  const raw = [];
  try {
    if (sel.handles.length) {
      const { missing } = await woo.fetchProductsBySlug(api, sel.handles, { progress: onProducts, out: raw }, ctx);
      warnMissing(report, missing);
    } else if (categoryIds.length > 1) {
      const seen = new Set();
      for (const cid of categoryIds) {
        if (ctx.stopped()) break;
        const part = [];
        try {
          await woo.fetchProducts(api, { categoryId: cid, pageFrom: sel.pageFrom, pageTo: sel.pageTo, progress: onProducts, out: part, search: sel.search }, ctx);
        } finally { merge(raw, seen, part); }
      }
    } else {
      await woo.fetchProducts(api, { categoryId: categoryIds[0] ?? null, pageFrom: sel.pageFrom, pageTo: sel.pageTo, progress: onProducts, out: raw, search: sel.search }, ctx);
    }
    if (!raw.length) throw new ScrapeError("No products found for that selection.", 404);

    const nVariable = raw.filter((r) => r.type === "variable").length;
    if (variations && nVariable && !ctx.stopped()) {
      report.phase("variations");
      const failed = await woo.fetchVariations(api, raw, {
        progress: ({ done, total }) => {
          report.update({ variations_done: done, variations_total: total });
          report.message(`Fetching variation prices and SKUs… ${done}/${total}`);
        },
      }, ctx);
      if (failed) report.warn(`${failed} variable product(s) had variations that couldn't be read; those variations have no price or SKU.`);
    } else if (nVariable && !variations) {
      report.warn("Variation prices and SKUs were skipped, so variable products have no per-variation prices.");
    }
  } finally {
    // Keep whatever was fetched even if a later step failed or the run was stopped.
    for (const r of raw) products.push(woo.normalize(r, paths));
  }

  const tooMany = products.filter((p) => (p.options || []).filter((x) => x.name !== "Title").length > 3).length;
  if (tooMany) {
    report.warn(`${tooMany} product(s) have more than 3 variation attributes. Shopify supports 3, so the Shopify CSV drops the extras; the WooCommerce CSV, full-details CSV and JSON keep them all.`);
  }

  if (reviews && !ctx.stopped()) {
    report.phase("reviews");
    const { reviews: found, reason } = await woo.fetchReviews(api, {
      progress: ({ done, total }) => {
        report.update({ reviews_done: done, reviews_total: total });
        report.message(`Fetching reviews… page ${done}/${total}`);
      },
    }, ctx);
    const byProduct = new Map();
    for (const r of found) { if (!byProduct.has(r.product_id)) byProduct.set(r.product_id, []); byProduct.get(r.product_id).push(r); }
    for (const p of products) p._reviews = byProduct.get(p.id) || [];
    if (reason) report.warn(reason);
    else if (!products.some((p) => p._reviews.length)) report.warn("The store has no reviews for the selected products.");
  }

  if (deep && !ctx.stopped()) {
    report.phase("details");
    await woo.fetchDetails(base, products, {
      progress: ({ done, total }) => {
        report.update({ details_done: done, details_total: total });
        report.message(`Fetching product pages… ${done}/${total}`);
      },
    }, ctx);
    const failed = products.filter((p) => (p._details || {}).page_error).length;
    if (failed) report.warn(`${failed} product page(s) could not be read for SEO, weight and dimensions.`);
  }
}

/** Collections (Shopify) or categories (WooCommerce) for the scope dropdown. */
export async function listCollections(base, wantedPlatform = "auto", ctx = NO_CTX) {
  let platform = wantedPlatform, api = null;
  if (platform === "auto") ({ platform, api } = await woo.detectPlatform(base, ctx));
  if (platform === "woocommerce") {
    if (!api) {
      const found = await woo.findApi(base, ctx);
      if (!found.api) throw new ScrapeError(found.reason);
      api = found.api;
    }
    const cats = await woo.fetchCategories(api, {}, ctx);
    const paths = woo.categoryPaths(cats);
    const rows = cats.map((c) => ({ handle: c.slug, title: paths.get(c.id) || c.title, products_count: c.count }))
      .sort((a, b) => a.title.toLowerCase().localeCompare(b.title.toLowerCase()));
    return { platform, collections: rows };
  }
  return { platform: "shopify", collections: await shopify.fetchCollections(base, {}, ctx) };
}

/** One-row-per-product summary shown in the results table. */
export function summarize(base, p) {
  const variants = p.variants || [];
  let prices = variants.map((v) => parseFloat(v.price)).filter((n) => Number.isFinite(n));
  const info = p._woo || {};
  if (!prices.length && info.price_min !== null && info.price_min !== undefined) {
    prices = [info.price_min, info.price_max || info.price_min];
  }
  const image = (p.images || [])[0]?.src;
  const safe = (u) => (u && /^https?:\/\//.test(u) ? u : null);
  return {
    id: p.id, title: p.title || "", handle: p.handle || "", vendor: p.vendor || "", product_type: p.product_type || "",
    collections: (p._collections || []).map((c) => c.title || ""), image: safe(image), image_count: (p.images || []).length,
    price_min: prices.length ? Math.min(...prices) : null, price_max: prices.length ? Math.max(...prices) : null,
    variants: variants.length, available: variants.some((v) => v.available),
    url: safe(p._url || (p.handle ? `${base}/products/${p.handle}` : null)),
  };
}
