import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { readFileSync, readdirSync } from "node:fs";
import { basename, dirname, join, resolve } from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import { strategyAssets } from "./strategy-assets.mjs";

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "../..");

function sha256(bytes) {
  return createHash("sha256").update(bytes).digest("hex");
}

function readJson(path) {
  return JSON.parse(readFileSync(join(ROOT, path), "utf8"));
}

function assetDirectory(url) {
  const relativeUrl = url.startsWith("/static/") ? url.slice("/static/".length) : url;
  return join(ROOT, "src/tickreplay/static", relativeUrl);
}

test("CodeMirror identity is non-circular, complete, and within budget", () => {
  const bundlePath = assetDirectory(strategyAssets.codeMirror.bundleUrl);
  const directory = dirname(bundlePath);
  const identityBytes = readFileSync(join(directory, "identity.json"));
  const manifestBytes = readFileSync(join(directory, "manifest.json"));
  const identity = JSON.parse(identityBytes);
  const manifest = JSON.parse(manifestBytes);

  assert.equal(sha256(identityBytes), manifest.bundle_version);
  assert.equal(manifest.bundle_version, basename(directory));
  assert.equal(sha256(manifestBytes), strategyAssets.codeMirror.manifestSha256);
  assert.deepEqual(readdirSync(directory).sort(), [
    "THIRD_PARTY_LICENSES.txt",
    "identity.json",
    "manifest.json",
    "strategy-editor.mjs",
    "strategy-editor.mjs.LEGAL.txt",
    "strategy-editor.mjs.gz",
  ]);
  assert.ok(manifest.files.find(({ name }) => name === "strategy-editor.mjs").bytes <= manifest.budgets.raw_bytes_max);
  assert.ok(manifest.files.find(({ name }) => name === "strategy-editor.mjs.gz").bytes <= manifest.budgets.deterministic_gzip_bytes_max);
  assert.equal(readFileSync(join(directory, "strategy-editor.mjs.gz")).subarray(0, 10).toString("hex"), "1f8b0800000000000213");
  assert.deepEqual(
    identity.runtime_packages.filter(({ name }) => name.startsWith("@codemirror/")).map(({ name }) => name).sort(),
    ["@codemirror/autocomplete", "@codemirror/commands", "@codemirror/lang-python", "@codemirror/language", "@codemirror/lint", "@codemirror/search", "@codemirror/state", "@codemirror/view"],
  );
  assert.equal(identity.runtime_packages.some(({ name }) => name.includes("playwright")), false);
});

test("Pyodide identity pins the standard-library-only runtime closure", () => {
  const directory = assetDirectory(strategyAssets.pyodide.indexUrl);
  const identityBytes = readFileSync(join(directory, "identity.json"));
  const manifestBytes = readFileSync(join(directory, "manifest.json"));
  const identity = JSON.parse(identityBytes);
  const manifest = JSON.parse(manifestBytes);

  assert.equal(sha256(identityBytes), manifest.asset_set_id);
  assert.equal(manifest.asset_set_id, basename(directory));
  assert.equal(sha256(manifestBytes), strategyAssets.pyodide.manifestSha256);
  assert.equal(identity.pyodide_version, "0.27.6");
  assert.equal(identity.python_version, "3.12.7");
  assert.deepEqual(identity.policy.approved_wheels, []);
  assert.equal(identity.policy.standard_library_only, true);
  assert.deepEqual(manifest.runtime_files, [
    "pyodide.mjs",
    "pyodide.asm.js",
    "pyodide.asm.wasm",
    "python_stdlib.zip",
    "pyodide-lock.json",
  ]);
});

test("maximum source fixture matches the frozen canonical workload", () => {
  const decisions = readJson("tests/browser/step0-decisions.json");
  const sourceBytes = readFileSync(join(ROOT, decisions.source_limits.boundary_fixture));
  const source = sourceBytes.toString("utf8");
  const lines = source.split("\n");

  assert.equal(sourceBytes.length, decisions.source_limits.utf8_bytes_max);
  assert.equal(sha256(sourceBytes), decisions.source_limits.boundary_fixture_sha256);
  assert.ok(Array.from(source).length <= decisions.source_limits.unicode_scalars_max);
  assert.ok(lines.length - 1 <= decisions.source_limits.lines_max);
  assert.ok(Math.max(...lines.map((line) => Array.from(line).length)) <= decisions.source_limits.longest_line_scalars_max);
  assert.match(source, /日本語/);
  assert.match(source, /@traced/);
  assert.match(source, /\[price for price/);
});

test("maximum-shape event remains below the predeclared encoded-byte ceiling", () => {
  const specification = readJson("tests/fixtures/strategy-max-envelope-spec.json");
  const longString = "x".repeat(specification.string_utf8_bytes_max);
  const event = {
    ...specification.event_template,
    open_orders: Array.from({ length: specification.open_orders_max }, (_, index) => ({
      order_id: `${index}-${longString}`.slice(0, specification.string_utf8_bytes_max),
      owner: "strategy", side: index % 2 ? "buy" : "sell", quantity_remaining: 1000000,
      limit_price_level: 1000000, eligible_from_session_tick: 99999,
    })),
    fills: Array.from({ length: specification.fills_max }, (_, index) => ({
      fill_delivery_sequence: index + 1,
      order_id: `${index}-${longString}`.slice(0, specification.string_utf8_bytes_max),
      side: index % 2 ? "buy" : "sell", quantity: 1000000, price_level: 1000000,
      session_tick_index: 99999, replay_time_us: 36000000000,
    })),
  };
  const bytes = Buffer.byteLength(JSON.stringify(event));

  assert.equal(event.open_orders.length, specification.open_orders_max);
  assert.equal(event.fills.length, specification.fills_max);
  assert.ok(bytes <= specification.event_encoded_bytes_max);
});

test("browser lock matches the installed pinned Playwright metadata", () => {
  const browserLock = readJson("tests/browser/browser-lock.json");
  const browserManifest = readFileSync(join(ROOT, "node_modules/playwright-core/browsers.json"));
  const packageLock = readJson("package-lock.json");

  assert.equal(sha256(browserManifest), browserLock.browser_manifest.sha256);
  assert.equal(browserManifest.length, browserLock.browser_manifest.bytes);
  assert.equal(packageLock.packages["node_modules/@playwright/test"].version, browserLock.harness.version);
  assert.equal(packageLock.packages["node_modules/@playwright/test"].integrity, browserLock.harness.npm_integrity);
});
