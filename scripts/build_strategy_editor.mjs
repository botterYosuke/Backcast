import { createHash } from "node:crypto";
import {
  cpSync,
  existsSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  readdirSync,
  rmSync,
  statSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { basename, dirname, join, relative, resolve, sep } from "node:path";
import { gzipSync } from "node:zlib";
import { build } from "esbuild";

const ROOT = resolve(dirname(new URL(import.meta.url).pathname), "..");
const ENTRY = "src/tickreplay/frontend/strategy-editor-entry.mjs";
const LOCK = "package-lock.json";
const VENDOR_ROOT = join(ROOT, "src/tickreplay/static/vendor/codemirror");
const NODE_VERSION = "24.15.0";
const NPM_VERSION = "12.0.1";
const ZLIB_VERSION = "1.3.1-e00f703";
const ESBUILD_VERSION = "0.28.2";
const TARGETS = ["chrome120", "firefox121", "safari17.4"];
const OUTPUT_ALLOWLIST = [
  "THIRD_PARTY_LICENSES.txt",
  "identity.json",
  "manifest.json",
  "strategy-editor.mjs",
  "strategy-editor.mjs.LEGAL.txt",
  "strategy-editor.mjs.gz",
];
const RUNTIME_DIRECT_PACKAGES = [
  "@codemirror/autocomplete",
  "@codemirror/commands",
  "@codemirror/lang-python",
  "@codemirror/language",
  "@codemirror/lint",
  "@codemirror/search",
  "@codemirror/state",
  "@codemirror/view",
];
const CORE_PACKAGES = [
  "@codemirror/state",
  "@codemirror/view",
  "@codemirror/language",
];

function sha256(bytes) {
  return createHash("sha256").update(bytes).digest("hex");
}

function canonicalJson(value) {
  return `${JSON.stringify(value, null, 2)}\n`;
}

function posixPath(path) {
  return path.split(sep).join("/");
}

function packageNameForInput(input) {
  const marker = "node_modules/";
  const normalized = posixPath(input);
  const index = normalized.lastIndexOf(marker);
  if (index < 0) return null;
  const segments = normalized.slice(index + marker.length).split("/");
  return segments[0].startsWith("@")
    ? `${segments[0]}/${segments[1]}`
    : segments[0];
}

function readPackageMetadata(name, lockPackages) {
  const directory = join(ROOT, "node_modules", ...name.split("/"));
  const packageJson = JSON.parse(readFileSync(join(directory, "package.json"), "utf8"));
  const lockEntry = lockPackages[`node_modules/${name}`];
  if (!lockEntry) throw new Error(`package ${name} is absent from package-lock.json`);
  const licenseName = ["LICENSE", "LICENSE.txt", "LICENSE.md"].find((candidate) =>
    existsSync(join(directory, candidate)),
  );
  if (!licenseName) throw new Error(`package ${name} has no local license file`);
  const licenseBytes = readFileSync(join(directory, licenseName));
  const repository = typeof packageJson.repository === "string"
    ? packageJson.repository
    : packageJson.repository?.url;
  if (!repository) throw new Error(`package ${name} has no canonical repository URL`);
  return {
    name,
    version: lockEntry.version,
    npm_integrity: lockEntry.integrity,
    npm_tarball: lockEntry.resolved,
    repository,
    license: lockEntry.license ?? packageJson.license,
    license_file: licenseName,
    license_sha256: sha256(licenseBytes),
    license_text: licenseBytes.toString("utf8").replaceAll("\r\n", "\n"),
  };
}

function assertToolchain() {
  if (process.versions.node !== NODE_VERSION) {
    throw new Error(`Node ${NODE_VERSION} required; found ${process.versions.node}`);
  }
  if (process.versions.zlib !== ZLIB_VERSION) {
    throw new Error(`zlib ${ZLIB_VERSION} required; found ${process.versions.zlib}`);
  }
  const npmAgent = process.env.npm_config_user_agent;
  if (npmAgent && !npmAgent.startsWith(`npm/${NPM_VERSION} `)) {
    throw new Error(`npm ${NPM_VERSION} required; found ${npmAgent}`);
  }
}

function assertPackageCoherence(packages, metafile) {
  const names = new Set(packages.map(({ name }) => name));
  for (const name of RUNTIME_DIRECT_PACKAGES) {
    if (!names.has(name)) throw new Error(`direct runtime package ${name} was tree-shaken out`);
  }
  for (const core of CORE_PACKAGES) {
    const inputs = Object.keys(metafile.inputs).filter(
      (input) => packageNameForInput(input) === core,
    );
    const roots = new Set(
      inputs.map((input) => posixPath(input).split(`/node_modules/${core}/`)[0]),
    );
    if (roots.size !== 1) {
      throw new Error(`${core} resolved from ${roots.size} physical package roots`);
    }
  }
}

function makeLicenseInventory(packages) {
  return `${packages.map((item) => [
    `===== ${item.name}@${item.version} (${item.license}) =====`,
    `Repository: ${item.repository}`,
    `npm tarball: ${item.npm_tarball}`,
    `npm integrity: ${item.npm_integrity}`,
    "",
    item.license_text.trimEnd(),
    "",
  ].join("\n")).join("\n")}\n`;
}

async function createBuild(staging) {
  const result = await build({
    absWorkingDir: ROOT,
    bundle: true,
    charset: "utf8",
    entryPoints: [ENTRY],
    format: "esm",
    legalComments: "external",
    metafile: true,
    minify: true,
    outExtension: { ".js": ".mjs" },
    outdir: staging,
    platform: "browser",
    sourcemap: false,
    sourcesContent: false,
    splitting: false,
    target: TARGETS,
    treeShaking: true,
    write: true,
  });
  const lockBytes = readFileSync(join(ROOT, LOCK));
  const lock = JSON.parse(lockBytes);
  const packageNames = [...new Set(
    Object.keys(result.metafile.inputs).map(packageNameForInput).filter(Boolean),
  )].sort();
  const packages = packageNames.map((name) => readPackageMetadata(name, lock.packages));
  assertPackageCoherence(packages, result.metafile);

  const bundlePath = join(staging, "strategy-editor-entry.mjs");
  const legalPath = join(staging, "strategy-editor-entry.mjs.LEGAL.txt");
  const outputBundle = join(staging, "strategy-editor.mjs");
  const outputLegal = join(staging, "strategy-editor.mjs.LEGAL.txt");
  cpSync(bundlePath, outputBundle);
  if (existsSync(legalPath)) {
    cpSync(legalPath, outputLegal);
  } else {
    writeFileSync(outputLegal, "");
  }
  rmSync(bundlePath);
  rmSync(legalPath, { force: true });

  const bundleBytes = readFileSync(outputBundle);
  const legalBytes = readFileSync(outputLegal);
  const gzipBytes = gzipSync(bundleBytes, { level: 9, mtime: 0 });
  if (gzipBytes.subarray(0, 10).toString("hex") !== "1f8b0800000000000213") {
    throw new Error("deterministic gzip header does not match the frozen recipe");
  }
  writeFileSync(join(staging, "strategy-editor.mjs.gz"), gzipBytes);
  const licenseText = makeLicenseInventory(packages);
  const licenseBytes = Buffer.from(licenseText, "utf8");
  writeFileSync(join(staging, "THIRD_PARTY_LICENSES.txt"), licenseBytes);

  const runtimePackages = packages.map(({ license_text: _licenseText, ...item }) => item);
  const identity = {
    schema_version: 1,
    entry: {
      logical_path: ENTRY,
      sha256: sha256(readFileSync(join(ROOT, ENTRY))),
    },
    lock: { logical_path: LOCK, sha256: sha256(lockBytes) },
    toolchain: {
      node: NODE_VERSION,
      npm: NPM_VERSION,
      esbuild: ESBUILD_VERSION,
      zlib: ZLIB_VERSION,
    },
    recipe: {
      bundle: true,
      charset: "utf8",
      entry_count: 1,
      external: [],
      format: "esm",
      inject: [],
      legalComments: "external",
      minify: true,
      platform: "browser",
      sourcemap: false,
      sourcesContent: false,
      splitting: false,
      target: TARGETS,
      treeShaking: true,
      gzip: { implementation: "node:zlib.gzipSync", level: 9, mtime: 0 },
    },
    runtime_packages: runtimePackages,
    payloads: [
      ["THIRD_PARTY_LICENSES.txt", licenseBytes],
      ["strategy-editor.mjs", bundleBytes],
      ["strategy-editor.mjs.LEGAL.txt", legalBytes],
      ["strategy-editor.mjs.gz", gzipBytes],
    ].map(([name, bytes]) => ({ name, bytes: bytes.length, sha256: sha256(bytes) })),
  };
  const identityBytes = Buffer.from(canonicalJson(identity), "utf8");
  const bundleVersion = sha256(identityBytes);
  writeFileSync(join(staging, "identity.json"), identityBytes);

  const manifest = {
    schema_version: 1,
    kind: "codemirror-strategy-editor",
    bundle_version: bundleVersion,
    immutable_url: `/static/vendor/codemirror/${bundleVersion}/strategy-editor.mjs`,
    identity_sha256: sha256(identityBytes),
    files: readdirSync(staging).sort().map((name) => {
      const bytes = readFileSync(join(staging, name));
      return { name, bytes: bytes.length, sha256: sha256(bytes) };
    }),
    budgets: {
      raw_bytes_max: 512000,
      deterministic_gzip_bytes_max: 153600,
    },
  };
  if (bundleBytes.length > manifest.budgets.raw_bytes_max) {
    throw new Error(`bundle is ${bundleBytes.length} bytes, above raw budget`);
  }
  if (gzipBytes.length > manifest.budgets.deterministic_gzip_bytes_max) {
    throw new Error(`bundle is ${gzipBytes.length} gzip bytes, above gzip budget`);
  }
  writeFileSync(join(staging, "manifest.json"), canonicalJson(manifest));
  const outputs = readdirSync(staging).sort();
  if (JSON.stringify(outputs) !== JSON.stringify(OUTPUT_ALLOWLIST)) {
    throw new Error(`unexpected output allowlist: ${outputs.join(", ")}`);
  }
  return { bundleVersion, manifest };
}

function compareDirectory(actual, expected) {
  if (!existsSync(expected)) throw new Error(`committed asset directory is missing: ${expected}`);
  const actualNames = readdirSync(actual).sort();
  const expectedNames = readdirSync(expected).sort();
  if (JSON.stringify(actualNames) !== JSON.stringify(expectedNames)) {
    throw new Error("committed CodeMirror output allowlist differs from reconstruction");
  }
  for (const name of actualNames) {
    const actualBytes = readFileSync(join(actual, name));
    const expectedBytes = readFileSync(join(expected, name));
    if (!actualBytes.equals(expectedBytes)) throw new Error(`${name} differs from reconstruction`);
  }
}

async function main() {
  assertToolchain();
  const check = process.argv.includes("--check");
  const temporaryRoot = mkdtempSync(join(tmpdir(), "backcast-codemirror-"));
  const staging = join(temporaryRoot, "id-neutral");
  mkdirSync(staging);
  try {
    const { bundleVersion, manifest } = await createBuild(staging);
    const destination = join(VENDOR_ROOT, bundleVersion);
    if (check) {
      compareDirectory(staging, destination);
    } else if (existsSync(destination)) {
      compareDirectory(staging, destination);
    } else {
      mkdirSync(VENDOR_ROOT, { recursive: true });
      cpSync(staging, destination, { recursive: true, errorOnExist: true });
    }
    const report = {
      ok: true,
      mode: check ? "check" : "build",
      bundle_version: bundleVersion,
      destination: posixPath(relative(ROOT, destination)),
      raw_bytes: manifest.files.find(({ name }) => name === "strategy-editor.mjs").bytes,
      gzip_bytes: manifest.files.find(({ name }) => name === "strategy-editor.mjs.gz").bytes,
      outputs: OUTPUT_ALLOWLIST,
    };
    process.stdout.write(canonicalJson(report));
  } finally {
    rmSync(temporaryRoot, { recursive: true, force: true });
  }
}

await main();
