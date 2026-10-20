import { ScrapeError, createCtx } from "./lib/util.js";
import { parseStoreInput, parseHandles } from "./lib/shopify.js";
import { run, listCollections, summarize } from "./lib/pipeline.js";
import * as ex from "./lib/exporters.js";

const MAX_ROWS = 500;
const $ = (id) => document.getElementById(id);
const state = { products: [], summaries: [], selected: new Set(), filtered: [], base: "", platform: "" };
let collectionsFor = null;
let job = null; // { ctx }

function el(tag, props = {}, children = []) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (k === "text") node.textContent = v;
    else if (k === "class") node.className = v;
    else node.setAttribute(k, v);
  }
  children.forEach((c) => node.appendChild(c));
  return node;
}

function setStatus(message, isError) {
  const s = $("status");
  s.textContent = "";
  if (message) s.appendChild(el("div", { class: isError ? "error" : "info", text: message }));
}

// The extension only asks for access to the store you are exporting (and its www / non-www twin),
// instead of every website.
async function ensureAccess(base) {
  if (typeof chrome === "undefined" || !chrome.permissions) return;
  const u = new URL(base);
  const twin = u.hostname.startsWith("www.") ? u.hostname.slice(4) : "www." + u.hostname;
  const origins = [`${u.protocol}//${u.host}/*`];
  if (u.hostname.includes(".") && !/^\d+(\.\d+){3}$/.test(u.hostname)) origins.push(`${u.protocol}//${twin}${u.port ? ":" + u.port : ""}/*`);
  if (await chrome.permissions.contains({ origins })) return;
  const ok = await chrome.permissions.request({ origins });
  if (!ok) throw new ScrapeError("Access to that store was not allowed. Click Find products again and choose Allow to let the extension read its public product data.");
}

const errText = (e) => (e instanceof ScrapeError ? e.message : "Something went wrong: " + (e && e.message ? e.message : e));

// --------------------------------------------------------------------------- scope + collections

function updateScopeUi() {
  const scope = $("scope").value;
  $("collectionField").hidden = scope !== "collections";
  $("productsField").hidden = scope !== "products";
  $("searchField").hidden = scope !== "search";
  $("pagesField").hidden = scope !== "pages";
  if (scope === "collections") loadCollections();
}

let picked = new Set();
const updatePicked = () => { $("collectionCount").textContent = picked.size ? picked.size + " selected" : ""; };

function renderCollectionList(items) {
  const box = $("collectionList");
  box.textContent = "";
  if (!items.length) { box.appendChild(el("span", { class: "picked", text: "No collections found." })); return; }
  for (const c of items) {
    const cb = el("input", { type: "checkbox", value: c.handle });
    cb.checked = picked.has(c.handle);
    cb.addEventListener("change", () => { cb.checked ? picked.add(c.handle) : picked.delete(c.handle); updatePicked(); });
    const label = el("label", {}, [cb, el("span", { text: c.title })]);
    if (c.products_count != null) label.appendChild(el("small", { text: String(c.products_count) }));
    label.dataset.search = c.title.toLowerCase();
    box.appendChild(label);
  }
}

function filterCollections() {
  const q = $("collectionFilter").value.trim().toLowerCase();
  for (const node of $("collectionList").children) {
    if (node.dataset && node.dataset.search !== undefined) node.hidden = !!q && !node.dataset.search.includes(q);
  }
}

async function loadCollections() {
  const raw = $("store").value.trim();
  const key = raw + "|" + $("platform").value;
  if (!raw || collectionsFor === key) return;
  const box = $("collectionList");
  box.textContent = "";
  box.appendChild(el("span", { class: "picked", text: "Loading…" }));
  picked = new Set();
  updatePicked();
  try {
    const { base } = parseStoreInput(raw);
    await ensureAccess(base);
    const d = await listCollections(base, $("platform").value);
    collectionsFor = key;
    renderCollectionList(d.collections);
    filterCollections();
  } catch (e) {
    box.textContent = "";
    box.appendChild(el("span", { class: "picked", text: "Couldn't load the list." }));
    setStatus(errText(e), true);
  }
}

// --------------------------------------------------------------------------- results table

const money = (n) => (n == null ? "" : n.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 }));
const priceText = (p) => (p.price_min == null ? "" : p.price_min === p.price_max ? money(p.price_min) : `${money(p.price_min)} – ${money(p.price_max)}`);

function applyFilter() {
  const q = $("filter").value.trim().toLowerCase();
  state.filtered = !q ? state.summaries : state.summaries.filter((p) => `${p.title} ${p.vendor} ${p.product_type}`.toLowerCase().includes(q));
  render();
}

function render() {
  const body = $("rows");
  body.textContent = "";
  const shown = state.filtered.slice(0, MAX_ROWS);
  for (const p of shown) {
    const cb = el("input", { type: "checkbox", "aria-label": "Select " + p.title });
    cb.checked = state.selected.has(p.id);
    cb.addEventListener("change", () => { cb.checked ? state.selected.add(p.id) : state.selected.delete(p.id); updateCount(); });
    const img = p.image
      ? el("img", { class: "thumb", src: p.image, alt: "", loading: "lazy", referrerpolicy: "no-referrer" })
      : el("div", { class: "thumb" });
    const titleCell = el("td", { class: "title" });
    const open = el("button", { type: "button", class: "linklike", text: p.title || "(untitled)" });
    open.addEventListener("click", () => showDetails(p));
    titleCell.appendChild(open);
    if (p.url) titleCell.appendChild(el("a", { class: "ext", href: p.url, target: "_blank", rel: "noopener noreferrer", text: "Open on the store" }));
    body.appendChild(el("tr", {}, [
      el("td", {}, [cb]), el("td", {}, [img]), titleCell,
      el("td", { text: p.vendor }), el("td", { text: p.product_type }),
      el("td", { class: "cols", text: (p.collections || []).join(", ") }),
      el("td", { class: "num", text: priceText(p) }), el("td", { class: "num", text: String(p.variants) }),
      el("td", {}, [el("span", { class: "badge" + (p.available ? " ok" : ""), text: p.available ? "In stock" : "Out of stock" })]),
    ]));
  }
  const note = $("capNote");
  if (state.filtered.length > MAX_ROWS) {
    note.hidden = false;
    note.textContent = `Showing the first ${MAX_ROWS} of ${state.filtered.length} matching products. Exports include every selected product, not just those shown.`;
  } else note.hidden = true;
  $("selectAll").checked = shown.length > 0 && shown.every((p) => state.selected.has(p.id));
  updateCount();
}

function updateCount() {
  $("count").textContent = `${state.selected.size} of ${state.summaries.length} selected for export`;
  const none = state.selected.size === 0;
  for (const id of ["expFull", "expXlsx", "expShopify", "expWoo", "expReviews", "expJson"]) $(id).disabled = none;
}

function showResult() {
  state.summaries = state.products.map((p) => summarize(state.base, p));
  state.selected = new Set(state.summaries.map((p) => p.id));
  $("filter").value = "";
  $("results").style.display = "block";
  $("inventory").hidden = true;
  const chip = $("platformChip");
  chip.hidden = !state.platform;
  chip.className = "chip " + state.platform;
  chip.textContent = state.platform === "woocommerce" ? "WooCommerce" : "Shopify";
  $("resultsTitle").textContent = `${state.products.length} products`;
  applyFilter();
  const reduce = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  $("results").scrollIntoView({ behavior: reduce ? "auto" : "smooth", block: "start" });
}

// --------------------------------------------------------------------------- product details

  function section(title, node) {
    return el("section", { class: "detail-sec" }, [el("h3", { text: title }), node]);
  }

  function pills(list) {
    return el("div", { class: "pills" }, list.map(function (t) { return el("span", { class: "pill", text: t }); }));
  }

  function renderDetails(d, product) {
    var body = $("detailBody");
    body.textContent = "";
    var meta = el("p", { class: "meta" });
    [["Platform", d.platform], ["Vendor", d.vendor], ["Type", d.product_type]].forEach(function (kv) {
      if (kv[1]) { meta.appendChild(el("span", {}, [document.createTextNode(kv[0] + " "), el("b", { text: kv[1] })])); }
    });
    if (d.rating) meta.appendChild(el("span", {}, [document.createTextNode("Rating "), el("b", { text: d.rating + (d.review_count ? " (" + d.review_count + ")" : "") })]));
    if (d.url) meta.appendChild(el("a", { href: d.url, target: "_blank", rel: "noopener noreferrer", text: "Open on the store" }));
    body.appendChild(meta);

    if (d.images.length) {
      var g = el("div", { class: "gallery" });
      d.images.slice(0, 30).forEach(function (im) {
        g.appendChild(el("img", { src: im.src, alt: im.alt, loading: "lazy", referrerpolicy: "no-referrer" }));
      });
      body.appendChild(g);
    }
    if (d.short_description) body.appendChild(section("Short description", el("p", { text: d.short_description })));
    if (d.description) body.appendChild(section("Description", el("p", { text: d.description })));

    var opts = d.options.filter(function (o) { return !(o.name === "Title" && o.values.join() === "Default Title"); });
    if (opts.length) {
      var wrap = el("div", {});
      opts.forEach(function (o) { wrap.appendChild(el("p", { text: o.name + ": " + o.values.join(", ") })); });
      body.appendChild(section("Options", wrap));
    }
    var others = d.attributes.filter(function (a) { return !opts.some(function (o) { return o.name === a.name; }); });
    if (others.length) {
      var aw = el("div", {});
      others.forEach(function (a) { aw.appendChild(el("p", { text: a.name + ": " + a.terms.join(", ") })); });
      body.appendChild(section("Attributes", aw));
    }

    var rows = d.variants.map(function (v) {
      return el("tr", {}, [
        el("td", { text: v.title }), el("td", { text: v.sku }),
        el("td", { class: "num", text: v.price + (v.compare_at_price ? " (was " + v.compare_at_price + ")" : "") }),
        el("td", { text: v.available ? "In stock" : "Out of stock" }),
        el("td", { text: v.barcode || "" })
      ]);
    });
    var table = el("table", { class: "vtable" }, [
      el("thead", {}, [el("tr", {}, ["Variant", "SKU", "Price", "Stock", "Barcode"].map(function (h) { return el("th", { text: h }); }))]),
      el("tbody", {}, rows)
    ]);
    body.appendChild(section("Variants (" + d.variants.length + ")", el("div", { class: "vwrap" }, [table])));

    if (d.collections.length) body.appendChild(section("Collections and categories", pills(d.collections)));
    if (d.tags.length) body.appendChild(section("Tags", pills(d.tags)));
    if (d.seo_title || d.seo_description) {
      body.appendChild(section("SEO", el("p", { text: [d.seo_title, d.seo_description].filter(Boolean).join("\n") })));
    }
    if (d.reviews.length) {
      var rw = el("div", {});
      d.reviews.forEach(function (r) {
        var head = [r.reviewer || "Anonymous", r.rating ? r.rating + "/5" : "", r.date ? r.date.slice(0, 10) : ""].filter(Boolean).join(" · ");
        rw.appendChild(el("div", { class: "review" }, [
          el("b", { text: r.title || head }), r.title ? el("small", { text: "  " + head }) : document.createTextNode(""),
          el("p", { text: r.body })
        ]));
      });
      var more = d.review_total > d.reviews.length ? " (showing " + d.reviews.length + " of " + d.review_total + ")" : "";
      body.appendChild(section("Reviews (" + d.review_total + ")" + more, rw));
    }
  }

function showDetails(summary) {
  const p = state.products.find((x) => x.id === summary.id);
  if (!p) return;
  const dlg = $("detail");
  const d = ex.detailView(p, state.base);
  $("detailTitle").textContent = d.title || "Product";
  renderDetails(d, p);
  if (!dlg.open) dlg.showModal();
}
$("detailClose").addEventListener("click", () => $("detail").close());
$("detail").addEventListener("click", function (ev) { if (ev.target === this) this.close(); });

// --------------------------------------------------------------------------- export

function download(name, data, mime) {
  const url = URL.createObjectURL(new Blob([data], { type: mime }));
  const a = el("a", { href: url, download: name });
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function doExport(format, button) {
  if (!state.products.length) return;
  button.disabled = true;
  try {
    const all = state.selected.size === state.products.length;
    const products = all ? state.products : state.products.filter((p) => state.selected.has(p.id));
    const link = $("optLink").checked;
    const host = state.base.split("://")[1].replace(/[^A-Za-z0-9.-]+/g, "_");
    const files = {
      full: [`${host}-full-details.csv`, () => ex.toFullCsv(products, state.base), "text/csv"],
      xlsx: [`${host}-products.xlsx`, () => ex.toXlsx(products, state.base), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"],
      shopify: [`${host}-shopify.csv`, () => ex.toShopifyCsv(products, link, state.base), "text/csv"],
      woocommerce: [`${host}-woocommerce.csv`, () => ex.toWooCommerceCsv(products, link, state.base), "text/csv"],
      reviews: [`${host}-reviews.csv`, () => ex.toReviewsCsv(products, state.base), "text/csv"],
      json: [`${host}-products.json`, () => ex.toJson(products), "application/json"],
    };
    if (format === "reviews" && !products.some((p) => (p._reviews || []).length)) {
      throw new ScrapeError("No reviews were collected. Turn on \"Product reviews\" and find the products again (some stores publish none).");
    }
    const [name, build, mime] = files[format];
    download(name, build(), mime);
    setStatus("Downloaded " + name, false);
  } catch (e) {
    setStatus(errText(e), true);
  } finally {
    updateCount();
  }
}

// --------------------------------------------------------------------------- progress

const PHASE_LABELS = { products: "Products", collections: "Collections", variations: "Variations", reviews: "Reviews", details: "Deep details" };

function renderPhases(phase, platform) {
  const order = ["products"];
  if ($("optCollections").checked) order.push("collections");
  if ($("optVariations").checked && platform === "woocommerce") order.push("variations");
  if ($("optReviews").checked) order.push("reviews");
  if ($("optDeep").checked) order.push("details");
  const at = order.indexOf(phase);
  const list = $("phases");
  list.textContent = "";
  order.forEach((name, i) => {
    const cls = at === -1 ? "" : i < at ? "done" : i === at ? "current" : "";
    const li = el("li", { class: cls, text: PHASE_LABELS[name] });
    if (cls === "current") li.setAttribute("aria-current", "step");
    list.appendChild(li);
  });
}

function makeReport(warnings) {
  const progress = {};
  let phase = "products", platform = "", message = "";
  const paint = () => {
    const pct = (d, t) => (t ? (100 * d) / t : null);
    const v = phase === "collections" ? pct(progress.collections_done, progress.collections_total)
      : phase === "variations" ? pct(progress.variations_done, progress.variations_total)
      : phase === "reviews" || phase === "details" ? pct(progress.details_done, progress.details_total) : null;
    const fill = $("barFill");
    if (v === null || v === undefined) { fill.className = "fill indeterminate"; fill.style.width = ""; }
    else { fill.className = "fill"; fill.style.width = v + "%"; }
    setStatus(message, false);
    renderPhases(phase, platform);
  };
  return {
    platform: (n) => { platform = n; state.platform = n; },
    phase: (n) => { phase = n; paint(); },
    message: (m) => { message = m; paint(); },
    update: (vals) => { Object.assign(progress, vals); },
    warn: (w) => warnings.push(w),
  };
}

function setRunning(on) {
  $("progressRow").hidden = !on;
  $("searchBtn").disabled = on;
  $("stopBtn").disabled = false;
}

// --------------------------------------------------------------------------- start a scrape

async function start(ev) {
  ev.preventDefault();
  if (job) return;
  let parsed;
  try { parsed = parseStoreInput($("store").value); } catch (e) { setStatus(errText(e), true); return; }
  const scope = $("scope").value;
  let collections = [], handles = [], search = "", pageFrom = 1, pageTo = null;
  if (scope === "collections") {
    collections = Array.from(picked);
    if (!collections.length && parsed.collection) collections = [parsed.collection];
    if (!collections.length) { setStatus("Tick at least one collection or category.", true); return; }
  } else if (scope === "products") {
    handles = parseHandles($("productsText").value);
    if (!handles.length) { setStatus("Add at least one product link or handle.", true); return; }
  } else if (scope === "search") {
    search = $("query").value.trim();
    if (!search) { setStatus("Type a keyword to search for.", true); return; }
  } else if (scope === "pages") {
    pageFrom = parseInt($("pageFrom").value || "1", 10);
    pageTo = parseInt($("pageTo").value || String(pageFrom), 10);
    if (!(pageFrom >= 1) || !(pageTo >= pageFrom)) { setStatus("Enter a valid page range.", true); return; }
  } else if (parsed.collection) {
    collections = [parsed.collection];
  }

  // Must be the first await after the click so the browser accepts the permission prompt.
  try { await ensureAccess(parsed.base); } catch (e) { setStatus(errText(e), true); return; }

  const ctx = createCtx();
  job = { ctx };
  const products = [];
  const warnings = [];
  state.base = parsed.base;
  state.platform = "";
  $("results").style.display = "none";
  $("inventory").hidden = false;
  setStatus("Starting…", false);
  setRunning(true);

  let error = null;
  try {
    await run(parsed.base, {
      collections, handles, search, reviews: $("optReviews").checked, pageFrom, pageTo, includeCollections: $("optCollections").checked,
      deep: $("optDeep").checked, platform: $("platform").value, variations: $("optVariations").checked,
    }, makeReport(warnings), ctx, products);
  } catch (e) {
    error = e;
  }
  const stopped = ctx.stopped();
  job = null;
  setRunning(false);
  state.products = products;
  if (products.length) showResult();
  if (error) {
    setStatus(errText(error) + (products.length ? ` Showing the ${products.length} products fetched before the error.` : ""), true);
  } else {
    const label = state.platform === "woocommerce" ? "WooCommerce" : "Shopify";
    setStatus(`Found ${products.length} ${label} products${stopped ? " (stopped early)." : "."}`, false);
  }
  warnings.forEach((w) => $("status").appendChild(el("div", { class: "warn", text: w })));
}

// --------------------------------------------------------------------------- wiring

$("form").addEventListener("submit", start);
$("stopBtn").addEventListener("click", function () {
  if (!job) return;
  this.disabled = true;
  setStatus("Stopping…", false);
  job.ctx.stop();
});
$("scope").addEventListener("change", updateScopeUi);
$("store").addEventListener("input", () => { collectionsFor = null; });
$("store").addEventListener("change", () => { if ($("scope").value === "collections") loadCollections(); });
$("platform").addEventListener("change", () => { collectionsFor = null; if ($("scope").value === "collections") loadCollections(); });
$("filter").addEventListener("input", applyFilter);
$("selectAll").addEventListener("change", function () {
  const on = this.checked;
  state.filtered.slice(0, MAX_ROWS).forEach((p) => (on ? state.selected.add(p.id) : state.selected.delete(p.id)));
  render();
});
$("expFull").addEventListener("click", function () { doExport("full", this); });
$("expXlsx").addEventListener("click", function () { doExport("xlsx", this); });
$("expReviews").addEventListener("click", function () { doExport("reviews", this); });
$("collectionFilter").addEventListener("input", filterCollections);
$("expShopify").addEventListener("click", function () { doExport("shopify", this); });
$("expWoo").addEventListener("click", function () { doExport("woocommerce", this); });
$("expJson").addEventListener("click", function () { doExport("json", this); });

// Opened from the toolbar button on a store page: fill in that page's address.
const preset = new URLSearchParams(location.search).get("store");
if (preset) $("store").value = preset;
$("store").focus();
