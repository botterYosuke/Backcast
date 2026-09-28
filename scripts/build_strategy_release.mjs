import { createHash } from 'node:crypto';
import {
  existsSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  readdirSync,
  rmSync,
  writeFileSync,
} from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join, relative, resolve, sep } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const STATIC_ROOT = join(ROOT, 'src/tickreplay/static');
const RELEASES_ROOT = join(STATIC_ROOT, 'strategy/releases');
const RELEASE_ID_PATTERN = /^[0-9a-f]{64}$/;
const CODEMIRROR_ID = '86539626cbc6254b2a37368d3a957a91a2c5f15de951779567207f76d6878d56';
const CODEMIRROR_MANIFEST_SHA256 = '65e7257469ab641cf111994c54ff0ed00634138811ddf896f5137eb47605d91c';
const PYODIDE_VERSION = '0.27.6';
const PYODIDE_ID = '1404bcc925dad5900b1205e9e7a127d85a654137554c6e76492eca5a2b80cff0';
const PYODIDE_MANIFEST_SHA256 = 'c5c5e67cd0713b41980a9076ccccf189630da6dd092a9d7461cb93bd3714636e';
const CODEMIRROR_BASE = `/static/vendor/codemirror/${CODEMIRROR_ID}/`;
const PYODIDE_BASE = `/static/vendor/pyodide/${PYODIDE_VERSION}/${PYODIDE_ID}/`;

const AUTHORED_FILES = Object.freeze([
  ['strategy-workspace.mjs', 'src/tickreplay/static/strategy/strategy-workspace.mjs'],
  ['strategy-editor.mjs', 'src/tickreplay/static/strategy/strategy-editor.mjs'],
  ['strategy-source-store.mjs', 'src/tickreplay/static/strategy/strategy-source-store.mjs'],
  ['strategy-selection-store.mjs', 'src/tickreplay/static/strategy/strategy-selection-store.mjs'],
  ['strategy-controller.mjs', 'src/tickreplay/static/strategy/strategy-controller.mjs'],
  ['strategy-protocol.mjs', 'src/tickreplay/static/strategy/strategy-protocol.mjs'],
  ['strategy-trace-protocol.mjs', 'src/tickreplay/static/strategy/strategy-trace-protocol.mjs'],
  ['strategy-trace-controller.mjs', 'src/tickreplay/static/strategy/strategy-trace-controller.mjs'],
  ['strategy-worker.mjs', 'src/tickreplay/static/strategy/strategy-worker.mjs'],
  ['strategy-runner.py', 'src/tickreplay/static/strategy/strategy-runner.py'],
]);

function sha256(bytes) {
  return createHash('sha256').update(bytes).digest('hex');
}

function stableJson(value) {
  if (Array.isArray(value)) return `[${value.map(stableJson).join(',')}]`;
  if (value && typeof value === 'object') {
    return `{${Object.keys(value).sort().map((key) => `${JSON.stringify(key)}:${stableJson(value[key])}`).join(',')}}`;
  }
  return JSON.stringify(value);
}

function prettyJson(value) {
  return `${JSON.stringify(value, null, 2)}\n`;
}

function localAssetPath(url) {
  if (!url.startsWith('/static/') || url.includes('..') || url.includes('?') || url.includes('#')) {
    throw new Error(`asset URL is not fixed same-origin: ${url}`);
  }
  return join(STATIC_ROOT, url.slice('/static/'.length));
}

function verifyManifest(baseUrl, expectedHash, expectedId, idField) {
  const path = localAssetPath(`${baseUrl}manifest.json`);
  const bytes = readFileSync(path);
  if (sha256(bytes) !== expectedHash) throw new Error(`${baseUrl} manifest hash differs`);
  const manifest = JSON.parse(bytes);
  if (manifest[idField] !== expectedId) throw new Error(`${baseUrl} content ID differs`);
}

function generatedAssetConfig() {
  return `'use strict';

export const strategyAssets = Object.freeze({
  schemaVersion: 1,
  codeMirror: Object.freeze({
    bundleUrl: '${CODEMIRROR_BASE}strategy-editor.mjs',
    manifestUrl: '${CODEMIRROR_BASE}manifest.json',
    manifestSha256: '${CODEMIRROR_MANIFEST_SHA256}',
  }),
  pyodide: Object.freeze({
    version: '${PYODIDE_VERSION}',
    indexUrl: '${PYODIDE_BASE}',
    loaderUrl: '${PYODIDE_BASE}pyodide.mjs',
    manifestUrl: '${PYODIDE_BASE}manifest.json',
    manifestSha256: '${PYODIDE_MANIFEST_SHA256}',
  }),
});
`;
}

function generatedReleaseEntry() {
  return `'use strict';

import { mountStrategyWorkspace } from './strategy-workspace.mjs';

let assetsPromise = null;
let editorPromise = null;
let controllerModulePromise = null;
let traceControllerModulePromise = null;
let traceTransport = null;

function loadAssets() {
  assetsPromise ??= import('./strategy-assets.mjs').then(({ strategyAssets }) => strategyAssets);
  return assetsPromise;
}

const release = Object.freeze({
  async loadCapability() {
    const module = await (traceControllerModulePromise ??= import('./strategy-trace-controller.mjs'));
    traceTransport ??= module.createBundledRlTraceHttpTransport();
    return traceTransport.capability();
  },

  async loadEditor() {
    editorPromise ??= Promise.all([
      import('./strategy-editor.mjs'),
      loadAssets().then((assets) => import(assets.codeMirror.bundleUrl)),
    ]).then(([editorModule, toolkit]) => Object.freeze({
      createStrategyEditor: editorModule.createStrategyEditor,
      toolkit,
    }));
    return editorPromise;
  },

  async loadController({ callbacks, performanceClass, mode = 'bundled_rl' }) {
    if (mode === 'bundled_rl') {
      const module = await (traceControllerModulePromise ??= import('./strategy-trace-controller.mjs'));
      traceTransport ??= module.createBundledRlTraceHttpTransport();
      return new module.BundledRlTraceController({ transport: traceTransport, ...callbacks });
    }
    if (mode !== 'pyodide') throw new TypeError('unknown strategy execution mode');
    const [assets, controllerModule] = await Promise.all([
      loadAssets(),
      controllerModulePromise ??= import('./strategy-controller.mjs'),
    ]);
    const workerUrl = new URL('./strategy-worker.mjs', import.meta.url);
    const runnerUrl = new URL('./strategy-runner.py', import.meta.url).href;
    return new controllerModule.StrategyController({
      createWorker: () => new Worker(workerUrl, { type: 'module' }),
      assets: {
        loaderUrl: assets.pyodide.loaderUrl,
        indexUrl: assets.pyodide.indexUrl,
        runnerUrl,
      },
      performanceClass,
      ...callbacks,
    });
  },
});

export function mount({ root, host, safeCall, ...options }) {
  return mountStrategyWorkspace({ root, host, safeCall, release, ...options });
}
`;
}

function sourceFiles() {
  const files = new Map();
  for (const [destination, source] of AUTHORED_FILES) {
    files.set(destination, readFileSync(join(ROOT, source)));
  }
  files.set('strategy-assets.mjs', Buffer.from(generatedAssetConfig()));
  files.set('strategy-release.mjs', Buffer.from(generatedReleaseEntry()));
  return files;
}

function identityInput(files) {
  return {
    schema_version: 1,
    files: [...files.entries()]
      .map(([name, bytes]) => ({ name, bytes: bytes.byteLength, sha256: sha256(bytes) }))
      .sort((left, right) => left.name.localeCompare(right.name)),
    vendor: {
      codemirror: {
        asset_set_id: CODEMIRROR_ID,
        manifest_sha256: CODEMIRROR_MANIFEST_SHA256,
      },
      pyodide: {
        version: PYODIDE_VERSION,
        asset_set_id: PYODIDE_ID,
        manifest_sha256: PYODIDE_MANIFEST_SHA256,
      },
    },
  };
}

function buildClosure() {
  verifyManifest(CODEMIRROR_BASE, CODEMIRROR_MANIFEST_SHA256, CODEMIRROR_ID, 'bundle_version');
  verifyManifest(PYODIDE_BASE, PYODIDE_MANIFEST_SHA256, PYODIDE_ID, 'asset_set_id');
  const files = sourceFiles();
  const identity = identityInput(files);
  const releaseId = sha256(Buffer.from(stableJson(identity)));
  const manifest = {
    schema_version: 1,
    release_id: releaseId,
    entry_url: `/static/strategy/releases/${releaseId}/strategy-release.mjs`,
    identity,
  };
  const identityDocument = {
    schema_version: 1,
    release_id: releaseId,
    identity_sha256: sha256(Buffer.from(stableJson(identity))),
  };
  files.set('identity.json', Buffer.from(prettyJson(identityDocument)));
  files.set('manifest.json', Buffer.from(prettyJson(manifest)));
  return { files, identity, manifest, releaseId };
}

function scanClosure(closure) {
  const names = new Set(closure.files.keys());
  const allowedAbsolutePrefixes = [CODEMIRROR_BASE, PYODIDE_BASE];
  const edges = [];
  for (const [name, bytes] of closure.files) {
    if (!name.endsWith('.mjs')) continue;
    const source = bytes.toString('utf8');
    for (const match of source.matchAll(/(?:from\s+|import\s*\(|new URL\s*\()(['"])([^'"]+)\1/g)) {
      const target = match[2];
      if (target.startsWith('./')) {
        const resolved = target.slice(2);
        if (!names.has(resolved)) throw new Error(`${name} has missing relative edge: ${target}`);
        edges.push({ from: name, to: resolved });
      } else if (target.startsWith('/')) {
        if (!allowedAbsolutePrefixes.some((prefix) => target.startsWith(prefix))) {
          throw new Error(`${name} escapes immutable release/vendor closure: ${target}`);
        }
        edges.push({ from: name, to: target });
      } else {
        throw new Error(`${name} has non-relative module/asset edge: ${target}`);
      }
    }
    if (source.includes('/static/strategy/') && !source.includes('/static/strategy/releases/')) {
      throw new Error(`${name} references mutable authored strategy assets`);
    }
  }
  for (const required of [
    'strategy-release.mjs', 'strategy-workspace.mjs', 'strategy-editor.mjs',
    'strategy-source-store.mjs', 'strategy-controller.mjs', 'strategy-protocol.mjs',
    'strategy-selection-store.mjs',
    'strategy-trace-controller.mjs', 'strategy-trace-protocol.mjs',
    'strategy-worker.mjs', 'strategy-runner.py', 'strategy-assets.mjs',
  ]) {
    if (!names.has(required)) throw new Error(`release closure is missing ${required}`);
  }
  return edges;
}

function writeClosure(root, closure) {
  const destination = join(root, closure.releaseId);
  mkdirSync(destination, { recursive: true });
  for (const [name, bytes] of closure.files) writeFileSync(join(destination, name), bytes);
  return destination;
}

function directoryFiles(root) {
  if (!existsSync(root)) return [];
  return readdirSync(root, { recursive: true, withFileTypes: true })
    .filter((entry) => entry.isFile())
    .map((entry) => join(entry.parentPath, entry.name))
    .sort();
}

function compareDirectory(destination, closure) {
  const actualFiles = directoryFiles(destination)
    .map((path) => relative(destination, path).split(sep).join('/'));
  const expectedFiles = [...closure.files.keys()].sort();
  if (JSON.stringify(actualFiles) !== JSON.stringify(expectedFiles)) {
    throw new Error(`release file list differs: ${JSON.stringify({ actualFiles, expectedFiles })}`);
  }
  for (const name of expectedFiles) {
    const actual = readFileSync(join(destination, name));
    if (!actual.equals(closure.files.get(name))) throw new Error(`release bytes differ: ${name}`);
  }
}

async function checkLegacyConfig(path) {
  const moduleUrl = pathToFileURL(resolve(ROOT, path));
  moduleUrl.searchParams.set('check', 'strategy-release');
  const { strategyAssets } = await import(moduleUrl.href);
  if (strategyAssets.schemaVersion !== 1) throw new Error('strategy asset config schema must be 1');
  verifyManifest(
    CODEMIRROR_BASE,
    strategyAssets.codeMirror.manifestSha256,
    CODEMIRROR_ID,
    'bundle_version',
  );
  verifyManifest(
    strategyAssets.pyodide.indexUrl,
    strategyAssets.pyodide.manifestSha256,
    PYODIDE_ID,
    'asset_set_id',
  );
  if (strategyAssets.pyodide.version !== PYODIDE_VERSION) throw new Error('unexpected Pyodide release');
  return {
    ok: true,
    mode: 'check-config',
    config: path,
    codeMirrorBundleVersion: CODEMIRROR_ID,
    pyodideAssetSetId: PYODIDE_ID,
    firstPartyReleaseGenerated: existsSync(RELEASES_ROOT),
  };
}

function argumentValue(name) {
  const index = process.argv.indexOf(name);
  if (index < 0 || !process.argv[index + 1]) throw new Error(`${name} requires a value`);
  return process.argv[index + 1];
}

async function main() {
  if (process.argv.includes('--check-config')) {
    process.stdout.write(prettyJson(await checkLegacyConfig(argumentValue('--check-config'))));
    return;
  }
  const first = buildClosure();
  const second = buildClosure();
  if (first.releaseId !== second.releaseId) throw new Error('repeat construction changed release ID');
  for (const [name, bytes] of first.files) {
    if (!bytes.equals(second.files.get(name))) throw new Error(`repeat construction changed ${name}`);
  }
  const edges = scanClosure(first);
  if (process.argv.includes('--prepare')) {
    const destinationRoot = resolve(argumentValue('--prepare'));
    const firstDestination = writeClosure(join(destinationRoot, 'first'), first);
    const secondDestination = writeClosure(join(destinationRoot, 'second'), second);
    compareDirectory(firstDestination, second);
    compareDirectory(secondDestination, first);
    process.stdout.write(prettyJson({
      ok: true,
      mode: 'prepare',
      releaseId: first.releaseId,
      entryUrl: first.manifest.entry_url,
      files: first.files.size,
      edges: edges.length,
      destinations: [firstDestination, secondDestination],
    }));
    return;
  }
  if (process.argv.includes('--write')) {
    const destination = writeClosure(RELEASES_ROOT, first);
    process.stdout.write(prettyJson({
      ok: true,
      mode: 'write',
      releaseId: first.releaseId,
      entryUrl: first.manifest.entry_url,
      files: first.files.size,
      edges: edges.length,
      destination: relative(ROOT, destination),
    }));
    return;
  }
  if (!process.argv.includes('--check')) {
    throw new Error('use --prepare <path>, --write, --check, or --check-config <path>');
  }
  const releaseDirectories = existsSync(RELEASES_ROOT)
    ? readdirSync(RELEASES_ROOT, { withFileTypes: true }).filter((entry) => entry.isDirectory())
    : [];
  if (releaseDirectories.some(({ name }) => !RELEASE_ID_PATTERN.test(name))) {
    throw new Error('release root contains a non-content-addressed directory');
  }
  compareDirectory(join(RELEASES_ROOT, first.releaseId), first);

  const temporary = mkdtempSync(join(tmpdir(), 'backcast-strategy-release-'));
  try {
    const firstRoot = join(temporary, 'first');
    const secondRoot = join(temporary, 'second');
    writeClosure(firstRoot, first);
    writeClosure(secondRoot, second);
    compareDirectory(join(firstRoot, first.releaseId), second);
    compareDirectory(join(secondRoot, second.releaseId), first);
  } finally {
    rmSync(temporary, { recursive: true, force: true });
  }
  process.stdout.write(prettyJson({
    ok: true,
    mode: 'check',
    releaseId: first.releaseId,
    entryUrl: first.manifest.entry_url,
    files: first.files.size,
    edges: edges.length,
    reconstructionRoots: 2,
  }));
}

await main();
