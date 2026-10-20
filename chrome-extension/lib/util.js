// Shared helpers: errors, HTTP with retries, entity decoding, CSV writing.
// Works in the extension page and in Node (for tests).

export class ScrapeError extends Error {
  constructor(message, status = 400) {
    super(message);
    this.name = "ScrapeError";
    this.status = status;
  }
}

// Thrown inside a run when the person pressed Stop; the pipeline keeps what was fetched.
export class Stopped extends Error {
  constructor() { super("Stopped"); this.name = "Stopped"; }
}

// Politeness delays can be scaled (tests set this to 0).
export const config = { delayScale: 1 };

export function createCtx() {
  const controller = new AbortController();
  let stopped = false;
  return {
    signal: controller.signal,
    stop() { stopped = true; controller.abort(); },
    stopped() { return stopped; },
    check() { if (stopped) throw new Stopped(); },
  };
}
export const NO_CTX = createCtx();

export function sleep(seconds, ctx = NO_CTX) {
  const ms = seconds * 1000 * config.delayScale;
  if (ms <= 0 || ctx.stopped()) return Promise.resolve();
  return new Promise((resolve) => {
    const t = setTimeout(done, ms);
    function done() { clearTimeout(t); ctx.signal.removeEventListener("abort", done); resolve(); }
    ctx.signal.addEventListener("abort", done);
  });
}

export async function pool(items, workers, fn) {
  let next = 0;
  const run = async () => {
    while (next < items.length) {
      const i = next++;
      await fn(items[i], i);
    }
  };
  await Promise.all(Array.from({ length: Math.max(1, Math.min(workers, items.length)) }, run));
}

// --------------------------------------------------------------------------- HTTP

const MAX_RESPONSE_CHARS = 30 * 1024 * 1024;

function lowerHeaders(resp) {
  const out = {};
  resp.headers.forEach((v, k) => { out[k.toLowerCase()] = v; });
  return out;
}

function withTimeout(ctx, ms) {
  const t = AbortSignal.timeout ? AbortSignal.timeout(ms) : null;
  if (t && AbortSignal.any) return AbortSignal.any([ctx.signal, t]);
  return ctx.signal;
}

/** GET a URL; resolves to {status, body}. `allowStatuses` are handed back instead of raising. */
export async function fetchRaw(url, opts = {}, ctx = NO_CTX) {
  const { retries = 4, allow404 = false, allowStatuses = [], headersOut = null, accept = "application/json" } = opts;
  let attempt = 0;
  for (;;) {
    ctx.check();
    let resp;
    try {
      resp = await fetch(url, {
        credentials: "omit", cache: "no-store", redirect: "follow",
        headers: { Accept: accept }, signal: withTimeout(ctx, 30000),
      });
    } catch (e) {
      if (ctx.stopped()) throw new Stopped();
      attempt++;
      if (attempt > retries) {
        throw new ScrapeError("Could not reach that store. Check the domain and your connection, then try again.");
      }
      await sleep(1.5 * attempt, ctx);
      continue;
    }

    let finalPath = "";
    try { finalPath = new URL(resp.url || url).pathname.replace(/\/+$/, ""); } catch { /* ignore */ }
    if (finalPath === "/password") throw new ScrapeError("This store is password protected.");

    const status = resp.status;
    const readBody = async () => {
      let text;
      try { text = await resp.text(); } catch (e) {
        if (ctx.stopped()) throw new Stopped();
        throw new ScrapeError("The connection dropped while reading the store's response.");
      }
      if (text.length > MAX_RESPONSE_CHARS) throw new ScrapeError("The store returned too much data in one response.");
      return text;
    };

    if (allowStatuses.includes(status)) {
      if (headersOut) Object.assign(headersOut, lowerHeaders(resp));
      return { status, body: await readBody() };
    }
    if ([429, 500, 502, 503, 504].includes(status)) {
      attempt++;
      if (attempt > retries) {
        throw new ScrapeError("The store is rate limiting requests. Wait a few minutes and try again.", 429);
      }
      let wait = parseFloat(resp.headers.get("Retry-After"));
      wait = Number.isFinite(wait) ? Math.min(wait, 20) : 2 * attempt;
      try { await resp.arrayBuffer(); } catch { /* ignore */ }
      await sleep(wait, ctx);
      continue;
    }
    if (status === 404) {
      if (allow404) return { status: 404, body: "" };
      throw new ScrapeError("No product feed found. This may not be a Shopify store, or the collection doesn't exist.", 404);
    }
    if (status === 401 || status === 403) throw new ScrapeError("The store blocked access to its product feed.", 403);
    if (status !== 200) throw new ScrapeError(`The store returned an unexpected response (${status}).`);
    if (headersOut) Object.assign(headersOut, lowerHeaders(resp));
    return { status: 200, body: await readBody() };
  }
}

// --------------------------------------------------------------------------- text helpers

const NAMED = {
  amp: "&", lt: "<", gt: ">", quot: '"', apos: "'", nbsp: " ", times: "×", ndash: "–", mdash: "—",
  hellip: "…", lsquo: "‘", rsquo: "’", ldquo: "“", rdquo: "”", copy: "©", reg: "®", trade: "™",
  deg: "°", plusmn: "±", frac12: "½", frac14: "¼", frac34: "¾", euro: "€", pound: "£", yen: "¥", cent: "¢",
  eacute: "é", egrave: "è", aacute: "á", agrave: "à", uuml: "ü", ouml: "ö", auml: "ä", szlig: "ß",
  ntilde: "ñ", ccedil: "ç", bull: "•", middot: "·", laquo: "«", raquo: "»", sup2: "²", sup3: "³",
};

export function unescapeHtml(s) {
  s = String(s ?? "");
  if (!s.includes("&")) return s;
  if (typeof document !== "undefined") {
    const ta = document.createElement("textarea");
    ta.innerHTML = s.replace(/</g, "&lt;");
    return ta.value;
  }
  return s.replace(/&(#x[0-9a-f]+|#\d+|[a-z][a-z0-9]*);/gi, (m, e) => {
    if (e[0] === "#") {
      const code = e[1].toLowerCase() === "x" ? parseInt(e.slice(2), 16) : parseInt(e.slice(1), 10);
      try { return String.fromCodePoint(code); } catch { return m; }
    }
    return NAMED[e] ?? NAMED[e.toLowerCase()] ?? m;
  });
}

export const text = (v) => unescapeHtml(v ?? "").trim();

export function toInt(v, d = 0) {
  if (typeof v === "number") return Number.isFinite(v) ? Math.trunc(v) : d;
  if (typeof v === "string" && /^\s*[+-]?\d+\s*$/.test(v)) return parseInt(v, 10);
  return d;
}

/** Parse a JSON body, tolerating the BOM or stray notices some WordPress sites print first. */
export function parseJson(body) {
  const s = String(body ?? "").replace(/^[﻿ \t\r\n]+/, "");
  try { return JSON.parse(s); } catch { /* fall through */ }
  for (const opener of ["[", "{"]) {
    const i = s.indexOf(opener);
    if (i > 0) { try { return JSON.parse(s.slice(i)); } catch { /* next */ } }
  }
  return null;
}

/** Python-style float text: 20 -> "20.0", 0.8 -> "0.8". Empty stays empty. */
export function pf(x) {
  if (x === "" || x === null || x === undefined) return "";
  return Number.isInteger(x) ? x.toFixed(1) : String(x);
}

export const isObj = (x) => x !== null && typeof x === "object" && !Array.isArray(x);

// --------------------------------------------------------------------------- CSV

export function writeCsv(headers, rows) {
  const esc = (v) => {
    if (v === null || v === undefined) return "";
    const s = typeof v === "boolean" ? (v ? "True" : "False") : String(v);
    return /[",\r\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
  };
  const lines = [headers.map(esc).join(",")];
  for (const r of rows) lines.push(headers.map((h) => esc(r[h])).join(","));
  return lines.join("\n") + "\n";
}
