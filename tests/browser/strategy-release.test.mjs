import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { readFileSync, readdirSync } from 'node:fs';
import test from 'node:test';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '../..');
const RELEASES_ROOT = join(ROOT, 'src/tickreplay/static/strategy/releases');
const CURRENT_RELEASE_ID = 'fde82a53f12e81c6fbac68d116c2afc3fd8e0254907dd7b6e93cb9750a63f14d';

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

test('every retained release directory verifies its non-circular content identity', () => {
  const releaseIds = readdirSync(RELEASES_ROOT, { withFileTypes: true })
    .filter((entry) => entry.isDirectory())
    .map(({ name }) => name);
  assert.ok(releaseIds.includes(CURRENT_RELEASE_ID));
  for (const releaseId of releaseIds) {
    assert.match(releaseId, /^[0-9a-f]{64}$/);
    const directory = join(RELEASES_ROOT, releaseId);
    const manifest = JSON.parse(readFileSync(join(directory, 'manifest.json')));
    const identity = JSON.parse(readFileSync(join(directory, 'identity.json')));
    assert.equal(manifest.release_id, releaseId);
    assert.equal(sha256(Buffer.from(stableJson(manifest.identity))), releaseId);
    assert.deepEqual(identity, {
      schema_version: 1,
      release_id: releaseId,
      identity_sha256: releaseId,
    });
    for (const file of manifest.identity.files) {
      const bytes = readFileSync(join(directory, file.name));
      assert.equal(bytes.byteLength, file.bytes, `${releaseId}/${file.name} size`);
      assert.equal(sha256(bytes), file.sha256, `${releaseId}/${file.name} hash`);
    }
  }
});

test('current release has an immutable relative closure and fixed vendor URLs', async () => {
  const directory = join(RELEASES_ROOT, CURRENT_RELEASE_ID);
  const manifest = JSON.parse(readFileSync(join(directory, 'manifest.json')));
  const names = manifest.identity.files.map(({ name }) => name).sort();
  assert.deepEqual(names, [
    'strategy-assets.mjs',
    'strategy-controller.mjs',
    'strategy-editor.mjs',
    'strategy-protocol.mjs',
    'strategy-release.mjs',
    'strategy-runner.py',
    'strategy-source-store.mjs',
    'strategy-worker.mjs',
    'strategy-workspace.mjs',
  ]);

  const entry = readFileSync(join(directory, 'strategy-release.mjs'), 'utf8');
  assert.match(entry, /import \{ mountStrategyWorkspace \} from '\.\/strategy-workspace\.mjs'/);
  assert.match(entry, /import\('\.\/strategy-editor\.mjs'\)/);
  assert.match(entry, /import\('\.\/strategy-controller\.mjs'\)/);
  assert.match(entry, /new URL\('\.\/strategy-worker\.mjs', import\.meta\.url\)/);
  assert.match(entry, /new URL\('\.\/strategy-runner\.py', import\.meta\.url\)/);
  assert.doesNotMatch(entry, /node_modules|https?:\/\/|\/static\/strategy\/(?!releases\/)/);

  const configUrl = pathToFileURL(join(directory, 'strategy-assets.mjs'));
  const { strategyAssets } = await import(configUrl.href);
  assert.equal(strategyAssets.pyodide.version, '0.27.6');
  assert.match(strategyAssets.codeMirror.bundleUrl, /^\/static\/vendor\/codemirror\/[0-9a-f]{64}\//);
  assert.match(strategyAssets.pyodide.indexUrl, /^\/static\/vendor\/pyodide\/0\.27\.6\/[0-9a-f]{64}\/$/);
});

test('release copies are byte-identical to their current authored inputs', () => {
  const directory = join(RELEASES_ROOT, CURRENT_RELEASE_ID);
  for (const name of [
    'strategy-workspace.mjs', 'strategy-editor.mjs', 'strategy-source-store.mjs',
    'strategy-controller.mjs', 'strategy-protocol.mjs', 'strategy-worker.mjs',
    'strategy-runner.py',
  ]) {
    assert.deepEqual(
      readFileSync(join(directory, name)),
      readFileSync(join(ROOT, 'src/tickreplay/static/strategy', name)),
      name,
    );
  }
});
