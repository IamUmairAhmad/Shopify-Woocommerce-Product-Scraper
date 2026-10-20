// Runs a scrape end to end for either platform.
import { ScrapeError, Stopped, NO_CTX } from "./util.js";
import * as shopify from "./shopify.js";
import * as woo from "./woo.js";

const noop = () => {};
export const nullReport = { platform: noop, phase: noop, message: noop, update: noop, warn: noop };

/**
 * Scrape a store and fill `products` (pass an array to keep partial results if it fails or is stopped).
 * platform: "auto" | "shopify" | "woocommerce". Resolves to the platform that was scraped.
 */
export async function run(base, opts = {}, report = nullReport, ctx = NO_CTX, products = []) {
  const {
    collection = null, pageFrom = 1, pageTo = null, includeCollections = true, deep = false,
    platform: wanted = "auto", variations = true,
  } = opts;
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
    if (platform === "shopify") {
      await runShopify(base, { collection, pageFrom, pageTo, includeCollections, deep }, report, ctx, products);
    } else {
      await runWoo(api, base, { collection, pageFrom, pageTo, includeCollections, deep, variations }, report, ctx, products);
    }
  } catch (e) {
    if (!(e instanceof Stopped)) throw e; // Stop keeps whatever was fetched
  }
  return platform;
}

async function runShopify(base, o, report, ctx, products) {
  report.phase("products");
  await shopify.fetchProducts(base, {
    collection: o.collection, pageFrom: o.pageFrom, pageTo: o.pageTo, out: products,
    progress: ({ page, count }) => {
      report.update({ pages: page, products: count });
      report.message(`Fetching products… page ${page}, ${count} found`);
    },
  }, ctx);
  if (!products.length) throw new ScrapeError("No products found for that selection.", 404);

  if (o.includeCollections && !ctx.stopped()) {
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

  if (o.deep && !ctx.stopped()) {
    report.phase("details");
    await shopify.fetchDetails(base, products, {
      progress: ({ done, total }) => {
        report.update({ details_done: done, details_total: total });
        report.message(`Fetching product details… ${done}/${total}`);
      },
    }, ctx);
    const failed = products.filter((p) => (p._details || {}).page_error).length;
    if (failed) report.warn(`${failed} product page(s) could not be read for SEO details.`);
  }
}

async function runWoo(api, base, o, report, ctx, products) {
  let categories = [], paths = new Map(), categoryId = null;
  if (o.collection || o.includeCollections) {
    report.phase("categories");
    report.message("Reading categories…");
    try {
      categories = await woo.fetchCategories(api, {}, ctx);
      paths = woo.categoryPaths(categories);
    } catch (e) {
      if (e instanceof Stopped) throw e;
      if (o.collection || !(e instanceof ScrapeError)) throw e;
      report.warn(`Category hierarchy unavailable, so categories are listed without parents: ${e.message}`);
    }
  }
  if (o.collection) categoryId = woo.resolveCategory(categories, o.collection);

  report.phase("products");
  const raw = [];
  try {
    await woo.fetchProducts(api, {
      categoryId, pageFrom: o.pageFrom, pageTo: o.pageTo, out: raw,
      progress: ({ page, count, total }) => {
        report.update({ pages: page, products: count });
        report.message(`Fetching products… page ${page}, ${count}` + (total ? ` of ${total}` : "") + " found");
      },
    }, ctx);
    if (!raw.length) throw new ScrapeError("No products found for that selection.", 404);

    const nVariable = raw.filter((r) => r.type === "variable").length;
    if (o.variations && nVariable && !ctx.stopped()) {
      report.phase("variations");
      const failed = await woo.fetchVariations(api, raw, {
        progress: ({ done, total }) => {
          report.update({ variations_done: done, variations_total: total });
          report.message(`Fetching variation prices and SKUs… ${done}/${total}`);
        },
      }, ctx);
      if (failed) report.warn(`${failed} variable product(s) had variations that couldn't be read; those variations have no price or SKU.`);
    } else if (nVariable && !o.variations) {
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

  if (o.deep && !ctx.stopped()) {
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
