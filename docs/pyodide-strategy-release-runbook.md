# Pyodide strategy release and rollback runbook

This runbook is the executable release contract for browser Python strategy v1.
It does not itself authorize a deployment. Fill every `<...>` value from the
builder or Cloud Run response, attach the command output to the release record,
and stop when any gate fails.

## Frozen release inputs

- Current strategy release ID:
  `1a45c99b4a6b884fbfc17240ae65759f20601dc86267df3d2b9a160de63a45da`
- Current entry URL:
  `/static/strategy/releases/1a45c99b4a6b884fbfc17240ae65759f20601dc86267df3d2b9a160de63a45da/strategy-release.mjs`
- Current release manifest SHA-256:
  `ab6afd52deb3b886096683859e492413021f3386a6878b72f5ac3d43b81b2146`
- CodeMirror bundle ID:
  `86539626cbc6254b2a37368d3a957a91a2c5f15de951779567207f76d6878d56`
- CodeMirror manifest SHA-256:
  `65e7257469ab641cf111994c54ff0ed00634138811ddf896f5137eb47605d91c`
- Pyodide `0.27.6`, Python `3.12.7`, standard library only
- Pyodide asset-set ID:
  `1404bcc925dad5900b1205e9e7a127d85a654137554c6e76492eca5a2b80cff0`
- Pyodide manifest SHA-256:
  `c5c5e67cd0713b41980a9076ccccf189630da6dd092a9d7461cb93bd3714636e`
- Pre-feature source revision:
  `93c239d5f64ec9301e7ba0e2626b72399d5f2377`
- Local two-image input manifest SHA-256:
  `71cd4dcb0b7e40e38312b27494ee54d42398438bb03618cd79c12199729bc104`

Never replace bytes below an existing content-addressed URL. A module, runner,
vendor ID, dependency, license, selected Pyodide file, or build recipe change
creates a new release ID. Keep old directories intact until the deletion gate is
satisfied.

## Clean reconstruction gate

Use the digest-pinned jobs in `.github/workflows/strategy-assets.yml`. Locally,
from a clean checkout, run:

```bash
npm ci
npm run check:strategy-editor
npm run test:strategy-assets
npm run check:strategy-release
uv run python scripts/fetch_pyodide_assets.py --check
uv run python scripts/build_strategy_images.py --check
uv run pytest -q tests/test_tickreplay_server.py tests/test_cloud_run_main.py
npm run test:strategy-browser
```

The editor reconstruction requires Node `24.15.0`, npm `12.0.1`, zlib
`1.3.1-e00f703`, and esbuild `0.28.2`. The committed `.mjs.gz` is a deterministic
measurement/provenance sidecar; it is never selected when an `.mjs` URL is
requested. Node and npm are build-only tools and are absent from the runtime
image.

`npm run check:strategy-release` reconstructs the complete first-party closure in
two independent temporary roots, verifies every static import, dynamic import,
Worker and runner edge, and rejects a mutable strategy-path escape. Compare its
reported ID, entry URL, file count and edge count with the frozen values above.

The exact full browser command must pass in the pinned Playwright image. The
release record must include Chromium, Firefox, WebKit, Pixel 7 Chromium and iPhone
14 WebKit results, zero unexplained `securitypolicyviolation` events, the 10,000
event memory trend, and the real-device accessibility matrix. A local system
Chrome run is useful evidence but does not replace these gates.

## Headers and cache classes

The compatibility and feature images use the same serving layer and immutable
overlay. Verify the following against both the inner FastAPI app and the outer
Cloud Run application:

- HTML, `styles.css`, `app.js`, broker/replay modules and authored
  `static/strategy/*.mjs` paths: `Cache-Control: no-store`.
- `static/strategy/releases/<64-hex>/...` and versioned CodeMirror/Pyodide
  assets/manifests: `Cache-Control: public, max-age=31536000, immutable`.
- `.mjs`: JavaScript MIME; `.wasm`: `application/wasm`; `.whl`/`.zip`:
  `application/zip`; `.py`: UTF-8 text; `.json`: JSON; directly requested `.gz`:
  `application/gzip` with no `Content-Encoding` or `Vary` negotiation.
- TickReplay gets the exact report-only policy
  `default-src 'self'; script-src 'self' 'wasm-unsafe-eval'; style-src 'self' 'unsafe-inline'; worker-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'self'`.
  There is no inline-script allowance. `connect-src 'self'` does not sandbox
  trusted strategy code from the same-origin APIs.
- Production COOP/COEP remains disabled. Exercise `COOP: same-origin` and
  `COEP: require-corp` only on the isolated staging fixture. Ordinary production
  operation must show the hard-stop-only capability and terminate its Worker.

## Build and record the two images

First generate and review `cloud-run/strategy-spike-images.json`:

```bash
uv run python scripts/build_strategy_images.py
uv run python scripts/build_strategy_images.py --check
docker build -f cloud-run/Dockerfile -t <registry>/backcast:<feature-tag> .
docker push <registry>/backcast:<feature-tag>
```

The manifest proves that the compatibility entry bytes equal the pre-feature Git
objects and that both image specifications carry the byte-identical immutable
overlay. The actual image builder must materialize those two specifications:

1. `<compatibility-tag>`: pre-feature HTML/JS/CSS and behavior, plus only the
   reviewed additive serving patch and every retained immutable closure/vendor
   set. It exposes no feature entry point.
2. `<feature-tag>`: current no-store HTML/JS/CSS with the fixed release URL, plus
   exactly the same immutable overlay.

Record, do not infer, the immutable builder outputs:

```text
COMPATIBILITY_IMAGE=<registry>/backcast@sha256:<compatibility-digest>
FEATURE_IMAGE=<registry>/backcast@sha256:<feature-digest>
COMPATIBILITY_REVISION=<cloud-run-compatibility-revision>
FEATURE_REVISION=<cloud-run-feature-revision>
```

Reject either image if compressed size exceeds 536,870,912 bytes or if its actual
entry hashes/overlay differ from `cloud-run/strategy-spike-images.json`. The local
manifest intentionally contains null image digests until a running builder has
produced them.

## Staging rehearsal

Substitute reviewed values; do not paste literal angle-bracket placeholders into a
command.

```bash
gcloud run deploy <service> --project <project> --region <region> \
  --image "$COMPATIBILITY_IMAGE" --no-traffic --tag strategy-compat
gcloud run services update-traffic <service> --project <project> --region <region> \
  --to-revisions "$COMPATIBILITY_REVISION=100"
gcloud run deploy <service> --project <project> --region <region> \
  --image "$FEATURE_IMAGE" --no-traffic --tag strategy-feature
gcloud run services update-traffic <service> --project <project> --region <region> \
  --to-revisions "$FEATURE_REVISION=1,$COMPATIBILITY_REVISION=99"
```

Before feature traffic, make the compatibility revision the sole rollback-eligible
target and verify ordinary replay/manual trading plus direct retrieval of every
retained release and vendor file. Open a feature page without opening the strategy
panel or pressing Load. With that page still open:

1. Prime and repeat the complete entry/release/vendor graph.
2. Shift traffic back to `$COMPATIBILITY_REVISION=100`.
3. Only then open the panel and press Load in the old feature page. Every delayed
   workspace/editor/controller/protocol/Worker/runner/CodeMirror/Pyodide request
   must succeed through the compatibility revision with the fixed URLs.
4. Exercise idle, in-flight callback, staged command and live strategy-order
   BFCache/background cases. Require synchronous invalidation, one cleanup,
   terminated Worker, preserved draft/loaded source, reset clock anchor and an
   explicit Restart.
5. Simulate the exact 24-hour supported-page boundary. Within it, delayed loads
   must work. Beyond it, removed assets may enter the documented degraded/raw
   recovery, while a fresh navigation must load the current retained entry.

Run the golden replay at x1, x10 and x500, failure injection, header/MIME/cache
checks, policy collection, and browser/accessibility matrix before increasing the
canary. Record requests, effective headers, console and policy events, browser
versions, traces and metrics with the image digests/revisions.

## Production canary and rollback

Production promotion is authorized only after the staging rehearsal and independent
runbook reproduction are signed off. Use the same traffic form as staging, first
deploying the compatibility image at 100%, then the feature image at a reviewed
small percentage. Raise traffic incrementally only while replay, editor, Load,
Start, Stop, Restart, diagnostics, ordinary manual trading, policy and delayed-load
checks remain green.

Immediately restore `$COMPATIBILITY_REVISION=100` for any unexplained policy event,
mutable-cache leak, mixed closure, asset/MIME failure, runtime regression, stale
strategy order, replay divergence, accessibility blocker, startup/budget breach or
elevated application error. Restore its header behavior at the same time. Never
delete the failed feature generation during rollback; supported-age pages still
need its immutable assets. No older asset-incomplete revision is a valid direct
rollback target.

Keep at least three generations for at least 30 days and support already-open pages
for 24 hours. Delete a generation only after both its compatibility and feature
images are outside traffic, both numeric retention minima remain true, and no page
within the supported-age window can reference it.

## Runtime failure and recovery

- CodeMirror load/parse/init failure: retain the synchronized editable textarea
  and all source actions.
- Release/workspace import or host callback failure: show the latest synchronized
  read-only raw-copy textarea; ordinary replay/manual trading remains usable.
- Asset config/controller/Pyodide failure: retain edit/save/import/export/sample,
  disable execution and allow an explicit retry.
- Non-isolated timeout or non-cooperative code: invalidate run identity before
  `Worker.terminate()`; never claim a soft stop.
- Missing asset beyond the supported age: enter the named recovery state; fresh
  navigation must load the retained current entry.

## Dependency upgrades

For a Pyodide upgrade, pin the new release and reconstruction-image digest, review
the selected loader/Wasm/archive files and upstream licenses, run
`scripts/fetch_pyodide_assets.py`, review its normalized manifest/checksums, create
a new first-party release closure, and rerun all ABI, browser, memory, interruption,
policy, rollback and real-device gates. Do not add packages or wheels implicitly.

For a CodeMirror/esbuild upgrade, update only exact versions in `package.json`,
regenerate `package-lock.json` with the pinned Node/npm image, review the normalized
license/provenance inventory, rebuild twice, enforce raw/gzip budgets, create a new
release closure and rerun editor, browser, policy, cache and rollback gates. Never
overwrite the prior bundle ID.

Current local feasibility evidence and unavailable-environment gates are in
`tests/browser/strategy-spike-result.md`. Until builder digests, complete pinned
browser results, real devices and staging traffic evidence are recorded, this
runbook is ready for rehearsal but production promotion remains blocked.
