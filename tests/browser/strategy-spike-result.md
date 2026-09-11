# Strategy compatibility spike result

Date: 2026-09-10

## Verdict

The Wave 1 implementation gate is **GO** for the same-origin, trusted-current-user
design on Chromium and Chromium mobile emulation. The production release gate is
still closed until the pinned Firefox/WebKit CI matrix, real-device assistive
technology checks, 10,000-event memory trend, and Cloud Run staging rollback drill
have run. Those are unavailable-environment results, not observed product failures.

The ordinary route remains non-isolated and therefore exposes hard stop only.
COOP `same-origin` plus COEP `require-corp` is viable on the isolated spike route,
but is not approved for production activation yet.

## Tested artifacts and environment

- Host: macOS 13.7.8 x86_64.
- Browser: installed Google Chrome 152.0.7977.83, recorded in
  `tests/browser/browser-lock.json`.
- Mobile performance class: Playwright Pixel 7 emulation on the same browser with
  Chrome DevTools Protocol CPU throttling rate 4.
- CodeMirror bundle:
  `86539626cbc6254b2a37368d3a957a91a2c5f15de951779567207f76d6878d56`.
- Pyodide asset set:
  `0.27.6/1404bcc925dad5900b1205e9e7a127d85a654137554c6e76492eca5a2b80cff0`.
- Maximum source: 262,144 UTF-8 bytes, SHA-256
  `0568b94934fa6e2831b41e66ec7595e90f237e9b0a1fc5f3ca71e1fec0076896`.
- Maximum event measured: 256 visible orders plus 256 fills and 128-byte bounded
  identifiers, 141,697 encoded bytes.

## Measured results

Command:

```text
STRATEGY_SPIKE_USE_SYSTEM_CHROME=1 npm_config_cache=.agents/logs/npm-cache npm run test:strategy-browser -- --project=chromium --project=mobile-chromium
```

Result: 12 passed in 1.0 minute.

| Metric | Desktop Chrome | Pixel 7 emulation (4x CPU) | Frozen threshold |
| --- | ---: | ---: | ---: |
| CodeMirror first open | 127.9 ms | 479.8 ms | 350 / 800 ms |
| Input latency p95, 100 edits | 19.0 ms | 16.7 ms | 32 / 64 ms |
| Diagnostics | 9.7 ms | 73.1 ms | 100 / 250 ms |
| Pyodide cold start including maximum-source compile | 2,989.4 ms | 4,356.3 ms | 8,000 / 15,000 ms |
| Maximum-source compile component | 72.3 ms | 196.2 ms | included in cold-start budget |
| Warm event round trip p95 | 6.3 ms | 13.4 ms | 10 / 30 ms |
| JS-to-Python conversion p95 | 4.1 ms | 6.5 ms | 10 ms |
| Sustained events/second | 200.28 | 97.90 | 100 / 30 minimum |
| Hard termination | 0 ms | 0 ms | 500 ms |
| Frames during Worker busy loop | 7 | 6 | at least 2 |
| Main-thread long tasks during Worker busy loop | 0 | 0 | 0 |
| Fresh Worker restart | 2,300 ms | 3,926 ms | 10,000 / 18,000 ms |
| CSP violations | 0 | 0 | 0 |

Chromium additionally passed a cooperative interrupt in 8.55 ms on the isolated
route, below the 250 ms limit. Bootstrap fetched neither editor nor Pyodide; first
editor open fetched CodeMirror but no Pyodide; first explicit Load created the one
module Worker and fetched Pyodide. All observed runtime requests were same-origin.

The executable suite also verifies Python highlighting, line numbers, bracket
matching and insertion, undo/redo, four-space indentation, search, ABI-only
completion, externally supplied diagnostics, exactly one accessible editor,
selection/focus recovery, editable degradation, raw-copy recovery, strict MIME and
cache classes, 200% zoom, 44 CSS-pixel controls, lifecycle primitives, bounded
stdout, custom Python filenames and Python exception transfer.

## Completed application closure evidence

The final application was rerun against content-addressed release
`1a45c99b4a6b884fbfc17240ae65759f20601dc86267df3d2b9a160de63a45da`, not the
ID-neutral spike modules:

```text
STRATEGY_SPIKE_USE_SYSTEM_CHROME=1 npm run test:strategy-browser -- --project=chromium
9 passed, 1 skipped in 36.5 s

STRATEGY_SPIKE_USE_SYSTEM_CHROME=1 npm run test:strategy-browser -- --project=mobile-chromium
10 passed in 42.5 s
```

These runs cover eager immutable release bootstrap; editor/Pyodide lazy boundaries;
Load, Start and persisted BFCache recovery; narrow CodeMirror and Pyodide failures;
release-bootstrap raw-copy recovery; same-origin requests; zero policy violations;
and mobile layout/touch targets. Current-run measurements were:

| Metric | Desktop Chrome | Pixel 7 emulation (4x CPU) | Frozen threshold |
| --- | ---: | ---: | ---: |
| CodeMirror first open | 141.0 ms | 303.1 ms | 350 / 800 ms |
| Input latency p95 | 8.5 ms | 10.9 ms | 32 / 64 ms |
| Diagnostics | 12.5 ms | 24.4 ms | 100 / 250 ms |
| Pyodide cold start | 3,091.0 ms | 4,173.9 ms | 8,000 / 15,000 ms |
| Maximum-source compile | 80.3 ms | 161.5 ms | included above |
| Warm callback p95 | 6.3 ms | 10.6 ms | 10 / 30 ms |
| JS-to-Python conversion p95 | 4.1 ms | 7.1 ms | 10 ms |
| Sustained events/second | 196.73 | 116.50 | 100 / 30 minimum |
| Fresh Worker restart | 2,312.9 ms | 3,261.4 ms | 10,000 / 18,000 ms |
| CSP violations | 0 | 0 | 0 |

The desktop isolated fixture additionally stopped cooperatively in 6.44 ms. Both
classes showed at least seven animation frames and zero main-thread long tasks
during the Worker busy loop. These application runs supersede the earlier
ID-neutral timing numbers for local go/no-go, while the pinned multi-engine and
real-device gates below remain unchanged.

## Unrun release gates

- The exact unmodified matrix command exits 1 because the pinned Playwright browser
  executables are absent locally. `npx playwright install chromium firefox webkit`
  reports that Playwright does not support Chromium on this `mac13` host. The
  digest-pinned CI image is the authoritative acquisition path.
- Firefox 155, WebKit 26.6, iPhone 14 WebKit emulation, Safari/VoiceOver,
  iOS/iPadOS VoiceOver, Android Chrome/TalkBack, software keyboard, rotation and
  native selection handles require the pinned CI image or real devices.
- The 10,000-event host-memory-growth gate needs the CI browser environment with an
  accepted measurement API; it was not inferred from the 100-event throughput run.
- Docker `27.2.0` is installed but its local daemon is unavailable. The `gcloud`
  CLI, Cloud Run credentials and a staging target are absent, so image digests,
  staging promotion, delayed
  loads across rollback, and the supported-age deletion drill remain release gates.

The pinned full-matrix job is `.github/workflows/strategy-assets.yml`. Failure of
any unrun browser, accessibility, memory, or deployment gate changes this verdict
to NO-GO before production traffic, without invalidating the Step 1A baseline or
rebuilding the committed vendor bytes.
