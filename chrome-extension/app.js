import { ScrapeError, createCtx } from "./lib/util.js";
import { parseStoreInput } from "./lib/shopify.js";
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
  $("collectionField").hidden = scope !== "collection";
  $("pagesField").hidden = scope !== "pages";
  if (scope === "collection") loadCollections();
}

async function loadCollections() {
  const raw = $("store").value.trim();
  const key = raw + "|" + $("platform").value;
  if (!raw || collectionsFor === key) return;
  const sel = $("collection");
  sel.textContent = "";
  sel.appendChild(el("option", { value: "", text: "Loading…" }));
  try {
    const { base } = parseStoreInput(raw);
    await ensureAccess(base);
    const d = await listCollections(base, $("platform").value);
    collectionsFor = key;
    sel.textContent = "";
    if (!d.collections.length) { sel.appendChild(el("option", { value: "", text: "No collections found" })); return; }
    for (const c of d.collections) {
      const label = c.title + (c.products_count != null ? ` (${c.products_count})` : "");
      sel.appendChild(el("option", { value: c.handle, text: label }));
    }
  } catch (e) {
    sel.textContent = "";
    sel.appendChild(el("option", { value: "", text: "Couldn't load collections" }));
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
    if (p.url) titleCell.appendChild(el("a", { href: p.url, target: "_blank", rel: "noopener noreferrer", text: p.title }));
    else titleCell.textContent = p.title;
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
  for (const id of ["expFull", "expShopify", "expWoo", "expJson"]) $(id).disabled = none;
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
    const host = state.base.split("://")[1].replace(/[^A-Za-z0-9.-]+/g, "_");
    const files = {
      full: [`${host}-full-details.csv`, () => ex.toFullCsv(products, state.base), "text/csv"],
      shopify: [`${host}-shopify.csv`, () => ex.toShopifyCsv(products), "text/csv"],
      woocommerce: [`${host}-woocommerce.csv`, () => ex.toWooCommerceCsv(products), "text/csv"],
      json: [`${host}-products.json`, () => ex.toJson(products), "application/json"],
    };
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

const PHASE_LABELS = { products: "Products", collections: "Collections", variations: "Variations", details: "Deep details" };

function renderPhases(phase, platform) {
  const order = ["products"];
  if ($("optCollections").checked) order.push("collections");
  if ($("optVariations").checked && platform === "woocommerce") order.push("variations");
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
      : phase === "details" ? pct(progress.details_done, progress.details_total) : null;
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
  let collection = null, pageFrom = 1, pageTo = null;
  if (scope === "collection") {
    collection = ($("collection").value || "").trim() || parsed.collection;
    if (!collection) { setStatus("Choose a collection or category.", true); return; }
  } else if (scope === "pages") {
    pageFrom = parseInt($("pageFrom").value || "1", 10);
    pageTo = parseInt($("pageTo").value || String(pageFrom), 10);
    if (!(pageFrom >= 1) || !(pageTo >= pageFrom)) { setStatus("Enter a valid page range.", true); return; }
  } else if (parsed.collection) {
    collection = parsed.collection;
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
      collection, pageFrom, pageTo, includeCollections: $("optCollections").checked,
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
$("store").addEventListener("change", () => { if ($("scope").value === "collection") loadCollections(); });
$("platform").addEventListener("change", () => { collectionsFor = null; if ($("scope").value === "collection") loadCollections(); });
$("filter").addEventListener("input", applyFilter);
$("selectAll").addEventListener("change", function () {
  const on = this.checked;
  state.filtered.slice(0, MAX_ROWS).forEach((p) => (on ? state.selected.add(p.id) : state.selected.delete(p.id)));
  render();
});
$("expFull").addEventListener("click", function () { doExport("full", this); });
$("expShopify").addEventListener("click", function () { doExport("shopify", this); });
$("expWoo").addEventListener("click", function () { doExport("woocommerce", this); });
$("expJson").addEventListener("click", function () { doExport("json", this); });

// Opened from the toolbar button on a store page: fill in that page's address.
const preset = new URLSearchParams(location.search).get("store");
if (preset) $("store").value = preset;
$("store").focus();
