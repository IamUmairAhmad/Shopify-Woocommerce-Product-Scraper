// Reads SEO tags and JSON-LD structured data from a product page (regex based, no DOM needed).
import { unescapeHtml, isObj } from "./util.js";

function parseAttrs(tagText) {
  const attrs = {};
  const re = /([^\s"'<>\/=]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'=<>`]+)))?/g;
  const inner = tagText.replace(/^<\s*[a-zA-Z0-9]+/, "").replace(/\/?>$/, "");
  let m;
  while ((m = re.exec(inner))) {
    const k = m[1].toLowerCase();
    if (!(k in attrs)) attrs[k] = unescapeHtml(m[2] ?? m[3] ?? m[4] ?? "");
  }
  return attrs;
}

export function parsePageMeta(html) {
  const out = { title: "", meta: {}, canonical: "", jsonLd: [] };
  const t = /<title\b[^>]*>([\s\S]*?)<\/title>/i.exec(html);
  if (t) out.title = unescapeHtml(t[1]).trim();
  for (const tag of html.match(/<meta\b[^>]*>/gi) || []) {
    const a = parseAttrs(tag);
    const key = (a.name || a.property || "").toLowerCase();
    if (key && "content" in a && !(key in out.meta)) out.meta[key] = a.content;
  }
  for (const tag of html.match(/<link\b[^>]*>/gi) || []) {
    const a = parseAttrs(tag);
    if ((a.rel || "").toLowerCase().split(/\s+/).includes("canonical") && !out.canonical) out.canonical = a.href || "";
  }
  const re = /<script\b([^>]*)>([\s\S]*?)<\/script>/gi;
  let m;
  while ((m = re.exec(html))) {
    const a = parseAttrs("<script " + m[1] + ">");
    if ((a.type || "").toLowerCase() === "application/ld+json") {
      try { out.jsonLd.push(JSON.parse(m[2].trim())); } catch { /* skip */ }
    }
  }
  return out;
}

function* walkLd(node) {
  if (isObj(node)) {
    yield node;
    for (const v of Object.values(node)) yield* walkLd(v);
  } else if (Array.isArray(node)) {
    for (const v of node) yield* walkLd(v);
  }
}

const ldTypes = (o) => {
  const t = o["@type"];
  if (typeof t === "string") return [t];
  return Array.isArray(t) ? t.filter((x) => typeof x === "string") : [];
};
const GTIN_KEYS = ["gtin", "gtin13", "gtin12", "gtin14", "gtin8"];
const firstGtin = (o) => { for (const k of GTIN_KEYS) if (o[k]) return o[k]; return ""; };

export function parseJsonLd(blocks) {
  const info = { brand: "", currency: "", rating_value: "", review_count: "", gtin_by_sku: {} };
  for (const block of blocks) {
    for (const obj of walkLd(block)) {
      const types = ldTypes(obj);
      if (types.includes("Product") || types.includes("ProductGroup")) {
        const brand = obj.brand;
        if (!info.brand) info.brand = isObj(brand) ? (brand.name || "") : (brand || "");
        const rating = obj.aggregateRating;
        if (isObj(rating) && !info.rating_value) {
          info.rating_value = String(rating.ratingValue ?? "");
          info.review_count = String(rating.reviewCount || rating.ratingCount || "");
        }
        const gtin = firstGtin(obj);
        if (gtin && obj.sku) info.gtin_by_sku[String(obj.sku)] = String(gtin);
      }
      if (types.includes("Offer") || types.includes("AggregateOffer")) {
        if (!info.currency && obj.priceCurrency) info.currency = obj.priceCurrency;
        const gtin = firstGtin(obj);
        if (gtin && obj.sku) info.gtin_by_sku[String(obj.sku)] = String(gtin);
      }
    }
  }
  return info;
}

/** SEO tags plus structured data for one page. */
export function pageDetails(html) {
  const p = parsePageMeta(html);
  const m = p.meta;
  return {
    seo_title: p.title,
    seo_description: m.description || "",
    canonical: p.canonical,
    og_title: m["og:title"] || "",
    og_description: m["og:description"] || "",
    og_image: m["og:image"] || "",
    json_ld: p.jsonLd,
    ...parseJsonLd(p.jsonLd),
  };
}
