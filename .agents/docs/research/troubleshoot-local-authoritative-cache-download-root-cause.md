# Root Cause: Existing 285A Cache Enters a Background Operation

## Execution Flow

1. The browser bootstrap loads the symbol list, prefers `285A`, and immediately
   calls `loadSymbolInfo("285A")` (`src/tickreplay/static/app.js:1689-1704`).
   `loadSymbolInfo` requests `/api/symbols/285A` through `fetchUntilReady`
   (`src/tickreplay/static/app.js:1414-1425`).
2. The FastAPI lifespan resolves the cache configuration and passes
   `config.local_authoritative` unchanged into the one process repository
   (`src/tickreplay/server.py:191-211`). The workspace `.env` sets the cache
   root to `/cache` but has no `BACKCAST_DUCKDB_LOCAL_AUTHORITATIVE` entry
   (`.env:13-16`), so the parser takes the default `False`; explicit `false`
   resolves identically (`src/tickreplay/config.py:117-163`,
   `tests/test_tickreplay_config.py:144-175`).
3. On Windows, `/cache` resolves to `C:\\cache`. The repository normalizes the
   cache root (`src/tickreplay/repository.py:223-232`, `265-279`), while the
   actual live-file helper appends `stocks_trades/<stem>.duckdb`
   (`src/tickreplay/cache.py:84-102`). The exact file tested for 285A is
   therefore `C:\\cache\\stocks_trades\\285A.duckdb`, not a root-level
   `C:\\cache\\285A.duckdb`. Commit `61d1f6f` changed the client layout from
   `<cache_dir>/<stem>.duckdb` to this `stocks_trades` subdirectory, so a file
   left in the former root-level location is invisible to current code.
4. `/api/symbols/{stem}` calls `_ensure_ready_or_pending`
   (`src/tickreplay/server.py:411-421`). After a lightweight listing-based
   existence check, that helper calls `repository.needs_download`; a `True`
   result starts the background operation and returns `{pending: true}`
   (`src/tickreplay/server.py:316-380`).
5. Every new `TickRepository` has an empty in-memory `_revalidated` set
   (`src/tickreplay/repository.py:265-291`). In normal-cache mode
   (`local_authoritative=False`), `needs_download` returns `True` for an
   existing exact-path file until that stem has been revalidated in this
   repository/process (`src/tickreplay/repository.py:418-440`). Under the
   symbol lock, `_ensure_fresh_locked` performs
   `_download_and_commit(..., conditional=True)` and then adds the stem to
   `_revalidated` (`src/tickreplay/repository.py:463-490`).
6. `conditional=True` is only an intent to use the sidecar. It is not proof
   that a conditional HTTP header was sent. `_conditional_headers` reads the
   live sidecar and returns `{}` when it is missing/corrupt or contains neither
   validator (`src/tickreplay/cache.py:143-164`, `233-283`). A 304 returns
   `not_modified=True` without a part file; a 200 streams, hashes, validates,
   and commits the whole response body (`src/tickreplay/cache.py:286-381`,
   `src/tickreplay/repository.py:492-536`).
7. The pending response makes `RequestCoordinator` poll
   `/api/status?stem=285A` until a matching terminal state and then retry the
   original symbol request (`src/tickreplay/static/request-coordinator.mjs:139-157`,
   `220-246`). `/api/status` reads only `OperationTracker` memory
   (`src/tickreplay/server.py:383-394`); the repeated GETs are a consequence,
   never the cause, of file-server I/O.

## State Transformations

| Stage | Input state | Transformation | Result relevant to the incident |
|---|---|---|---|
| Config | Key absent or string `false` | Boolean parser/default | `local_authoritative=False` (`config.py:149-163`) |
| Cache root | `/cache` on Windows | `Path(...).resolve(strict=False)` and live-path helper | `C:\\cache\\stocks_trades\\285A.duckdb` (`repository.py:223-232`; `cache.py:84-102`) |
| Process freshness | New repository | `_revalidated = set()` | Existing file still needs one normal-cache round trip (`repository.py:287-291`, `418-440`) |
| Server operation | `needs_download=True` | Starts tracker worker | State becomes `downloading`, even if the eventual file response is 304 (`server.py:88-157`, `359-380`) |
| Sidecar | Missing, unreadable JSON, or no ETag/Last-Modified | `_conditional_headers` returns `{}` | Nominal `conditional=True` request is effectively unconditional (`cache.py:143-164`, `274-283`) |
| File-server response | 304 | `StagingResult(not_modified=True, part_path=None)` | No body transfer/commit (`cache.py:306-320`; `repository.py:534-536`) |
| File-server response | 200 | Stream to `.part`, SHA-256, DuckDB-open validation, commit | Full body is transferred (`cache.py:321-381`; `repository.py:503-536`) |
| Later same-process access | `_revalidated` contains 285A | `needs_download=False` if file exists | No second network operation (`repository.py:440`, `486-490`) |
| Process restart | New repository | `_revalidated` resets | One new revalidation operation in normal mode (`tests/test_tickreplay_repository.py:1213-1236`) |

The UI further collapses these distinct states: any tracker state named
`downloading` is displayed as a data download, initially at zero bytes
(`src/tickreplay/static/app.js:184-200`). Consequently, an operation/polling
route does not establish that a file body was transferred.

## Hypotheses Evaluated

| Hypothesis | Evidence for | Evidence against / limit | Assessment |
|---|---|---|---|
| 1. `local_authoritative=false` caused normal-cache first-access revalidation | Direct config, repository, server, frontend, and passing test evidence above. Git blame shows the normal first-access policy was introduced by `bab0473`; `d285db0` later added only the explicit authoritative exception and deliberately preserved normal-cache revalidation. | Explains only operation entry. With a matching sidecar, it should end at 304 with no body. | **Confirmed root cause of operation entry.** |
| 2. Sidecar missing, unreadable, invalid, or validator-less | `read_sidecar` maps missing/corrupt metadata to absent and `_conditional_headers` then sends no validators (`cache.py:143-164`, `274-283`). The mocked no-sidecar test proves a 200 body path despite `conditional=True` (`tests/test_tickreplay_cache.py:140-165`). The current sidecar was created after the current 3,881,840,640-byte file during the observed operation, so it cannot prove a valid pre-operation sidecar existed. | The pre-operation sidecar was replaced and no outbound request headers/status were captured. | **Leading full-transfer explanation, mechanically proven but not incident-unique.** |
| 3. The believed cache file was at the wrong path or absent at the exact path | Current code only recognizes `<cache_dir>/stocks_trades/<stem>.duckdb` (`cache.py:84-102`). The incident context found legacy root-level files coexisting with the new layout and found no surviving alternate 285A file. Exact-path absence selects `conditional=False` (`repository.py:481-487`). | The user's pre-operation file path was not recorded; the current exact-path file was created by the observed operation. | **Plausible; cannot distinguish from hypothesis 2 after overwrite.** |
| 4. A valid sidecar validator no longer matched the remote object | Correct HTTP behavior is a 200 replacement when the stored ETag/Last-Modified is stale. Tests prove normal caches replace same-size/same-date content rather than infer identity (`tests/test_tickreplay_repository.py:1071-1132`). | No pre-operation validator or outbound/remote response headers survive. | **Plausible but unsupported for this specific incident.** |
| 5. The existing DuckDB was corrupt | Any DuckDB query/open error forces exactly one unconditional download and retry (`src/tickreplay/repository.py:538-549`); the authoritative-mode corruption test confirms this exception (`tests/test_tickreplay_repository.py:1178-1210`). | The tracked background worker only performs `ensure_fresh`; the corruption retry occurs when the subsequent symbol/session query opens the file and does not report its bytes through the existing tracker progress callback. The supplied log stops during status polling and contains no DuckDB exception or later long-running retry request. | **Low probability for the observed tracked transfer; not ruled out as a later second transfer.** |
| 6. Status polling itself caused repeated downloads | None. | `/api/status` is a pure in-memory snapshot (`server.py:383-394`), and the coordinator polls only after a pending response (`request-coordinator.mjs:139-157`, `220-246`). | **Rejected.** |
| 7. `285A`/`285a` case mismatch | None material. The browser/bootstrap uses uppercase and the server normalizes `stem.upper()` (`app.js:1696-1704`; `server.py:411-416`). The observed client path is Windows case-insensitive. | No evidence of a lower-case request or lower-case-only cache object. | **Very low / rejected as the primary cause.** |

## Root Cause

The definitive operation-entry root cause is the combination of:

1. bootstrap auto-selection of 285A;
2. a fresh process/repository whose `_revalidated` set is empty; and
3. normal-cache policy because `BACKCAST_DUCKDB_LOCAL_AUTHORITATIVE` is absent
   or `false`.

In this mode, file existence alone intentionally does **not** skip the first
per-process revalidation. This is not a regression introduced by the new flag:
commit `bab0473` introduced normal first-access conditional revalidation, while
commit `d285db0` added an opt-in exception only for an authoritative self-served
tree. Commit `85f381a` later made 285A the bootstrap default, making that
existing policy run immediately at application startup. The 24 focused mocked
tests pass and confirm both sides of the policy.

The still-running process retained a terminal 285A snapshot with
`state=fresh`, `operationId=1`, `revision=3704`, and
`bytesReceived=totalBytes=3881840640`. This conclusively proves that the
incident transferred the full 3,881,840,640-byte body rather than ending at
304. The reason for that full-body response still cannot be uniquely proven:
the supplied Uvicorn log does not capture the background `httpx` request,
conditional headers, or remote response status. The most defensible bounded
conclusion is that the exact path entered normal first-access revalidation; a
missing/unusable/validator-less pre-operation sidecar is the leading 200
explanation, with exact-path absence and a changed remote validator still
viable. The commit overwrote the pre-operation file/sidecar evidence, so
selecting one branch as certain would be overclaiming.

## Trigger Conditions

The background operation is deterministic when all of these are true:

- 285A is selected (currently automatic at bootstrap);
- the process/repository is fresh, so 285A is not in `_revalidated`; and
- `local_authoritative=False` (the default and explicit `false` are identical).

The impact is shared by both `/api/symbols/{stem}` and `/api/session`, because
both call `_ensure_ready_or_pending` (`src/tickreplay/server.py:411-469`). It
applies to every normal `stocks_trades` cache on the first stem access of a
fresh repository/process, not only 285A. Same-repository reuse, an authoritative
existing file, and the separate `stocks_minute` cache path are outside this
trigger.

The same operation performs no body transfer when the exact file and a usable,
matching sidecar exist and the file server returns 304. It performs a full body
transfer when the exact file is missing, or when the request has no matching
validator and the server returns 200. A separate unconditional repair transfer
can occur if a later DuckDB query finds the live file corrupt.

Existing-file skip routes already exist, but with different guarantees:

- `local_authoritative=True` skips HTTP revalidation entirely for an existing
  exact-path file (`repository.py:434-440`, `463-488`; authoritative skip test
  at `tests/test_tickreplay_repository.py:1026-1068`). It is valid only when
  the cache tree is the origin's own served tree.
- Normal mode skips later same-process accesses after `_revalidated.add(stem)`.
- A matching sidecar yields 304/no body, but still enters the operation route.
- Offline stale fallback serves the local copy only after a failed attempted
  revalidation (`repository.py:510-522`; `tests/test_tickreplay_server.py:475-529`).

## Fix/Observability Options

### Option A — Semantics-preserving observability (recommended first)

Keep the current freshness and integrity policy, but stop presenting every
operation as an indistinguishable download. Capture and expose/log, without
validator values or secrets:

- resolved cache path and exact-file existence;
- sidecar existence/readability and whether an ETag or Last-Modified header was
  actually sent;
- operation reason (`missing`, `process-start-revalidation`, `corruption-repair`);
- outbound response status, bytes received, and terminal outcome; and
- UI phase (`checking freshness` versus `downloading N bytes`).

Integration points are `_ensure_fresh_locked`/`_download_and_commit`,
`_conditional_headers`/`_consume_response`, `StemSnapshot`, and
`showCacheStatus`. This is the safest immediate change because it preserves
304 behavior, remote freshness, offline stale fallback, and corruption repair,
while making the next incident uniquely diagnosable. Tests should prove that a
304 shows revalidation with zero bytes, a no-sidecar 200 shows body transfer,
and corruption repair is classified separately.

### Option B — Safe sidecar reconstruction from strong identity

For an imported exact-path file without a sidecar, add an authoritative remote
manifest containing a cryptographic content digest. Hash the local file once;
only if the digest matches may the client reconstruct validators/sidecar and
avoid a replacement transfer. A mismatch keeps today's full-download path.

This can reduce avoidable multi-GB transfers for externally populated caches,
but requires a server protocol/manifest, potentially expensive local hashing,
atomic metadata handling, and new compatibility/failure tests. Reconstructing
identity from only `Content-Length` plus second-granularity `Last-Modified` is
explicitly unsafe; equal metadata does not prove equal bytes, and the current
tests intentionally reject that inference (`tests/test_tickreplay_repository.py:1071-1132`).

### Option C — Existence-only trust for an ordinary remote cache

Generalizing `local_authoritative=True` or adding a `trust_existing` switch
would provide the requested skip, but it changes correctness: remote updates
would never be discovered, and a structurally valid but stale/wrong DuckDB
would be accepted indefinitely. The existing flag should not be redefined for
this purpose because its trust boundary is specifically “this tree is the
origin object,” documented in `src/tickreplay/config.py:83-98` and protected by
`tests/test_tickreplay_repository.py:1026-1210`.

If product requirements genuinely prefer offline/manual freshness, that should
be a separately named and explicitly documented policy (for example, a TTL or
manual-refresh mode), not a silent reuse of `local_authoritative`.

## Codex Verification

Four independent read-only consultations were issued with the required prompt
contract and unique `local-cache-root-*` labels:

1. execution-flow tracing —
   `.agents/logs/codex/prompt-local-cache-root-flow.md`;
2. hypothesis evaluation —
   `.agents/logs/codex/prompt-local-cache-root-hypotheses.md`;
3. comparison of fix/observability approaches —
   `.agents/logs/codex/prompt-local-cache-root-options.md`; and
4. correctness verification of the observability-first recommendation —
   `.agents/logs/codex/prompt-local-cache-root-verification.md`.

Each used `codex_consult.py --sandbox read-only --timeout 240`. All four
remained running beyond the stated bound, produced zero-byte response files
under `.agents/logs/codex/20260823T013343Z-local-cache-root-*.md`, and were
interrupted after more than 400 seconds (shell exit 1). They are therefore
**unverified and contribute no evidence** to this conclusion. The diagnosis
instead rests on independently read code/history plus the focused mocked test
run: 24 passed, 77 deselected, one deprecation warning in 2.20 seconds.

## Remaining Unknowns

- The pre-operation exact file path, size, timestamps, sidecar bytes, and
  validator fields were not preserved. The current file and sidecar were
  created during the observed operation.
- The outbound file-server request headers, response status, and response
  headers were not logged. The retained status snapshot supplies the completed
  byte count, but not the reason the response contained a body.
- The environment of server process 5272 was not captured. Workspace `.env`
  evidence establishes the checked configuration, but a process-level override
  would take precedence (`src/tickreplay/config.py:117-125`, `149-163`).
- No DuckDB-open exception or operation terminal snapshot was supplied, so the
  corruption-repair branch cannot be eliminated absolutely.
- The four mandated Codex consultations did not complete and must be rerun only
  after the wrapper/CLI hang is independently resolved; no indefinite retry was
  attempted here.
