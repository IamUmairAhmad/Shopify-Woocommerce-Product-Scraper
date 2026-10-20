// Node runner for the extension's scraper, used by tests/test_extension_parity.py.
// usage: node run-cli.mjs <store> <outdir> [--deep] [--no-variations] [--no-collections] [--platform X]
import fs from "node:fs";
import path from "node:path";
import { config, createCtx } from "../lib/util.js";
import { parseStoreInput } from "../lib/shopify.js";
import { run } from "../lib/pipeline.js";
import * as ex from "../lib/exporters.js";

config.delayScale = 0;
const args = process.argv.slice(2);
const [store, out] = args;
const flag = (f) => args.includes(f);
const platform = args.includes("--platform") ? args[args.indexOf("--platform") + 1] : "auto";
const { base, collection } = parseStoreInput(store);
const products = [];
const warnings = [];
const used = await run(base, {
  collection, deep: flag("--deep"), includeCollections: !flag("--no-collections"),
  variations: !flag("--no-variations"), platform,
}, { warn: (w) => warnings.push(w) }, createCtx(), products);

fs.mkdirSync(out, { recursive: true });
const host = base.split("://")[1].replace(/[^A-Za-z0-9.-]+/g, "_");
fs.writeFileSync(path.join(out, `${host}-full-details.csv`), ex.toFullCsv(products, base));
fs.writeFileSync(path.join(out, `${host}-shopify.csv`), ex.toShopifyCsv(products));
fs.writeFileSync(path.join(out, `${host}-woocommerce.csv`), ex.toWooCommerceCsv(products));
fs.writeFileSync(path.join(out, `${host}-products.json`), ex.toJson(products));
console.log(JSON.stringify({ platform: used, count: products.length, warnings }));
