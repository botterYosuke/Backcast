# Existing DuckDB Cache Revalidation: History and Impact

The observed background operation is consistent with the intended normal-cache policy, not evidence by itself of a cache miss. The defect-like part is observability: a conditional revalidation that transfers zero bytes and a multi-gigabyte 200 response share the same `downloading` state and UI text.

## Git History

- `bab0473` (2026-08-21, `.duckdb` server-cache cutover) introduced the remote `stocks_trades` cache, sidecar validators, `_revalidated`, `needs_download`, and the pending/status handshake. Current blame still attributes the normal-cache condition at `repository.py:440` and the conditional branch at `repository.py:486-488` to this commit. The once-per-repository/process first-access revalidation therefore predates `local_authoritative`.
- `61d1f6f` (2026-08-21) moved live files, sidecars, staging files, and local listing discovery from the cache root to `<cache_dir>/stocks_trades/`. A legacy `<cache_dir>/<stem>.duckdb` is invisible to current code and is treated as missing. Its commit message used “download skip” language for an existing `jp` tree, but the code still performed the first conditional GET; it avoided the unconditional missing-file path, not all network revalidation.
- `08877b4` (2026-08-22) merged the tick-replay app into the file-serving FastAPI deployment and configured the loopback/self-served topology that made revalidating a file against itself unsafe.
- `85f381a` (2026-08-22) made 285A the browser bootstrap preference. This explains why a fresh page selects 285A without a user click.
- `d285db0` (2026-08-23, current `HEAD`) explicitly added `BACKCAST_DUCKDB_LOCAL_AUTHORITATIVE`. Its commit message says normal caches continue conditional GET, generic size/Last-Modified identity inference is rejected, and only the self-served authoritative tree skips the per-file GET. It also added the corresponding config, repository, Cloud Run, documentation, design, and regression tests.

## Intent

- `local_authoritative=false` is the conservative default and means “this directory is a replica of a remote origin,” not “ignore existing files.” A live file is conditionally revalidated on its first access by each fresh `TickRepository`; later accesses through that same repository skip the network (`config.py:83-98,149-163`; `repository.py:287-291,418-490`).
- `local_authoritative=true` is deliberately narrower: the cache directory must be the exact authoritative tree served by the configured file server. Revalidating that topology against itself rewrites the origin and destabilizes mtime-derived validators. It is not a generic “trust my cache” preference (`.agents/docs/DESIGN.md`, decision dated 2026-08-22; `d285db0` commit message).
- An existing normal-cache file with a valid matching sidecar still enters the background operation, but can receive 304 and transfer no body. An absent, unreadable, corrupt, or validator-less sidecar yields no conditional headers, so the nominal conditional request can receive 200 and stream the full file (`cache.py:143-157,233-335`).
- Missing files and DuckDB-open/query failures download unconditionally in both modes. That repair behavior is intentional and must not be removed (`repository.py:484-487,538-549`).

## Blast Radius

Affected paths and users:

- Every normal remote `stocks_trades` cache (`false` or unset), for the first access to each stem in a fresh repository/application process.
- Both `/api/symbols/{stem}` and `/api/session`, because both call `_ensure_ready_or_pending`; the browser bootstrap reaches `/api/symbols/285A` first.
- Manually seeded or migrated live files without an app-created sidecar. Existence avoids the missing-file branch, but does not supply a validator; a 200/full transfer is expected.
- Legacy root-level cache files created before `61d1f6f`. Current code only recognizes `<cache_dir>/stocks_trades/<stem>.duckdb` and the colocated `.sidecar.json`.
- Existing files whose sidecar validator is stale relative to the remote object. A 200 replacement is correct freshness behavior.
- Corrupt DuckDB files. A query failure forces exactly one unconditional repair download even under `local_authoritative=true`.
- Cloud Run or other explicit override deployments where URL/cache settings cause the safe default to remain false. `cloud-run/main.py` enables authority automatically only when both built-in self-serving defaults were adopted.

Unaffected or bounded cases:

- An exact-path existing file with `local_authoritative=true` skips the per-file GET and the pending/status path. The lightweight symbol-list request remains.
- Later accesses to the same stem through the same repository after its first revalidation skip the network via `_revalidated`.
- A valid matching sidecar in normal mode still causes pending/status polling, but 304 transfers no file body and does not commit a replacement.
- `/api/status` itself is a pure in-memory read and performs no remote I/O; repeated polling is a consequence of the operation, not its cause (`server.py:383-394`).
- `stocks_minute` uses a separate best-effort existence-only cache in `minute_context.py:95-137`; `local_authoritative` does not affect it.

Operationally, the user's supplied Uvicorn access log cannot distinguish these cases. It records inbound app requests only, not the cache client's outbound status or conditional headers. The still-running process's terminal snapshot later confirmed `bytesReceived=totalBytes=3881840640`, proving a full body transfer but not which precondition caused the 200 response.

## Existing Test Coverage

- Config default, true/false values, invalid values, and `.env` resolution: `tests/test_tickreplay_config.py:144-199`.
- Sidecar-derived ETag/Last-Modified headers, 304 behavior, corrupt-sidecar handling, and no-sidecar/no-validator behavior: `tests/test_tickreplay_cache.py:92-165`.
- Authoritative existing-file skip across three fresh repositories, normal-cache anti-identity-inference, authoritative missing-file download, authoritative corrupt-file repair, and normal-cache revalidation after repository restart: `tests/test_tickreplay_repository.py:1026-1236`.
- Pending/status completion and offline stale fallback: `tests/test_tickreplay_server.py:405-532`.
- Merged deployment authority defaults and explicit override behavior: `tests/test_cloud_run_main.py:613-662`.
- Client operation identity/revision guards, cache-hit no-polling, and `stale-served`: `src/tickreplay/static/request-coordinator.test.mjs:62-252`.

Read-only verification performed in this investigation:

- With `BACKCAST_DUCKDB_LOCAL_AUTHORITATIVE=false` explicitly scoped to the test process: `24 passed, 77 deselected`, one unrelated Starlette deprecation warning.
- `node --test src/tickreplay/static/request-coordinator.test.mjs`: `10 passed`.
- No real server or production cache was used; Python cases use temporary files and `httpx.MockTransport`.

Test gaps:

- No frontend test covers `showCacheStatus`; 304/no-body currently renders the same “data downloading” message as a 200 transfer, initially at `0 bytes`.
- No operation/status test exposes or asserts reason/phase (`missing-download`, `conditional-revalidation`, `corruption-repair`), validator presence, remote response outcome, or final bytes transferred.
- The no-sidecar full-200 behavior is covered at the cache layer, but not as a full repository/server/UI integration scenario for a manually seeded existing file.
- `test_failed_revalidation_serves_existing_cache_as_stale_once_per_process` does not force normal-cache mode. During this investigation the ignored root `.env` changed to `BACKCAST_DUCKDB_LOCAL_AUTHORITATIVE=true`, causing that test to return a cache hit; explicitly setting false restored 24/24. The test is coupled to a developer's `.env` and should isolate this configuration.
- No focused test covers a live file whose existing sidecar validator matches the server but whose recorded sidecar SHA-256 no longer matches the live bytes. Current conditional-header construction does not verify that relationship; this is outside the proven incident cause but is an integrity edge case worth specifying before validator-lifecycle changes.

## Documentation/Observability Gaps

- Current `docs/tick-replay.md` and `.env.example` correctly describe the default and the narrow authority trust boundary. The main gap is operational discoverability, not missing policy prose.
- Startup does not emit a safe diagnostic with resolved mode, normalized cache layout, and whether the process is using normal or authoritative policy.
- `StemSnapshot.state` calls every active ensure-fresh operation `downloading`; it has only bytes/total/error and cannot say whether the operation is checking, transferring, committing, repairing, or finishing via 304 (`server.py:55-77,100-176`).
- `showCacheStatus` maps any `downloading` snapshot to `データをダウンロード中…`, even when no response body has started (`app.js:184-201`).
- There is no durable application log for conditional mode, validator availability, outbound response status, or transferred bytes. Absolute cache paths, raw ETags, and full request headers should remain server-side or be redacted rather than exposed to the browser.

## Regression Risk

1. **Preserve semantics and add observability — low risk, recommended first.** Add backward-compatible status fields such as `reason`/`phase`, boolean `validatorPresent`, and a terminal `outcome` (`not-modified`, `downloaded`, `stale-served`, `repaired`) while retaining `serverEpoch`, `operationId`, `revision`, and existing terminal states. Change UI text from “downloading” to “checking cache” until a 200 body actually begins. Do not expose absolute paths, validator values, or headers. Main risks are stale callbacks and schema drift; existing coordinator identity tests plus new phase/UI tests contain them.
2. **Safe validator bootstrap/lifecycle — medium-to-high implementation risk.** Existing app downloads already persist validators. A manually seeded file cannot be proven identical to the remote object from size and Last-Modified alone, and `d285db0` explicitly rejects that inference. A safe bootstrap requires trusted origin provenance or a strong content-digest manifest/protocol, plus atomic sidecar binding and migration tests. A HEAD request that returns only the current mtime/size-derived ETag is not sufficient proof of byte identity.
3. **Explicit bounded trust/TTL for normal caches — medium policy and data-freshness risk.** This can reduce startup checks, but it intentionally serves potentially stale well-formed DuckDB files until expiry. It must be a new setting separate from `local_authoritative`, persist last successful validation safely, define clock/rollback semantics, and keep missing/corrupt repair unchanged. It is a product-policy change, not a bug fix.
4. **Trust every existing normal cache forever — high/unsafe.** This hides all remote updates and deletions for well-formed files; corruption repair detects only structural/query failure, not stale market data. Reusing `local_authoritative=true` for a remote copy also violates the design trust boundary. This option should be rejected.

The smallest safe next change is option 1. It answers the user's immediate question without weakening freshness or conflating “cache exists” with “cache is current.” Options 2 or 3 require a separate design decision and explicit acceptance of their data-integrity trade-offs.

## Codex Risk Analysis

The required consultations were invoked through `codex_consult.py` with `--sandbox read-only`, unique `local-cache-impact` prompt files/labels, and bounded timeouts. No Codex call had write permission.

- `20260823T013555Z-local-cache-impact-regression-risk.md`: wrapper returned `ok=true`, but Windows/Codex CLI received only the first physical line (`# Objective`) and returned a context-loader preamble. It is non-substantive and treated as unverified.
- `20260823T013722Z-local-cache-impact-regression-risk-v2.md`: one-line retry exceeded the 180-second bound and was interrupted; response artifact is empty.
- `20260823T014148Z-local-cache-impact-fix-safety-v2.md`: fix-safety analysis included the Root Cause Analyst's conclusion, exceeded the 120-second bound, and was interrupted; response artifact is empty.

Accordingly, no Codex conclusion is used as evidence. The risk ranking above is based on independently inspected source, tests, documentation, blame, commit diffs, and focused local tests. The prompt, response, and stderr artifacts remain under `.agents/logs/codex/` for audit.

## Remaining Unknowns

- The pre-operation 285A live path and sidecar were replaced during the observed operation, so the exact full-200 trigger cannot be reconstructed.
- The outbound request/response status and validator headers were not logged. The retained operation snapshot confirms a full 3,881,840,640-byte transfer, but cannot reveal why the response was 200 rather than 304.
- The already-running application's resolved environment may differ from the current shell and current `.env`; configuration changes require a process restart.
- During this investigation `.env` changed from no local-authoritative entry to `BACKCAST_DUCKDB_LOCAL_AUTHORITATIVE=true`. This investigator did not modify it. If `C:\cache` is only a copy of `http://backcast.i234.me:8080`, that value crosses the documented authority boundary and can serve stale data; the owner and intent of that concurrent change need confirmation.
- External research is not applicable: repository history and local implementation fully define this policy, and no concrete upstream dependency issue was identified.
