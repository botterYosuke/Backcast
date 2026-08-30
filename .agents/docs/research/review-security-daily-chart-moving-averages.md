# Security Review: Daily Chart and Moving Averages

## Decision

**PASS.** The final fix resolves the limiter-queue burst-amplification finding while preserving the controls that resolved the prior worker-starvation and persistent cache-poisoning vulnerabilities. No remaining Critical, High, Medium, or Low security finding was identified in the reviewed scope.

Severity counts: Critical 0 / High 0 / Medium 0 / Low 0.

## Review Scope

Reviewed the approved plan and the fixed full patch, including the changed orchestration artifacts, `cloud-run/main.py`, `docs/tick-replay.md`, `src/tickreplay/daily_context.py`, `src/tickreplay/server.py`, `src/tickreplay/static/app.js`, `src/tickreplay/static/daily-chart.mjs`, HTML/CSS, and focused Python/Node tests. The review covered stem/date/limit validation, case handling and traversal, SQL parameterization, staged downloads and atomic replacement, lock/TOCTOU behavior, corrupt DuckDB handling, error disclosure, XSS, stale-session isolation, resource exhaustion, secrets/logging, and regression exposure of existing endpoints.

## Findings

None.

## Resolution of Prior Findings

### Resolved High: Blocking same-process cache fetch and shared-pool starvation

- **Fix evidence:** `src/tickreplay/server.py:500-518` makes the endpoint path asynchronous and offloads blocking work through a dedicated `anyio.CapacityLimiter(4)`, so it does not acquire the default request-serving limiter. `src/tickreplay/server.py:515` passes `repository.local_authoritative`; `src/tickreplay/daily_context.py:424-437` returns a missing authoritative file as unavailable without HTTP. Remote downloads are also capped by a process-wide four-slot semaphore (`daily_context.py:36,44,336`), 64 MiB declared/observed size checks (`daily_context.py:35,353-364`), and a 30-second total deadline layered over the 15-second HTTP read timeout (`daily_context.py:33-34,334,360`).
- **State-bound evidence:** The prior attacker-controlled lock map is replaced by 64 fixed hash stripes (`daily_context.py:37,43,176-179`). Negative misses use a 256-entry LRU-like ordered map with a 60-second expiry (`daily_context.py:38-39,194-219`), so unique invalid stems cannot create unbounded process state. Cycling beyond the negative-cache capacity can cause bounded additional remote checks but cannot reclaim the default AnyIO worker pool or exceed the dedicated four-operation concurrency bound.
- **Bypass assessment:** Client cancellation may leave an already-running worker until its bounded operation returns, but at most four daily-context operations are isolated this way; unrelated default-pool API/file work remains available. No path permits `local_authoritative=true` to reach loopback download code.

### Resolved Medium: Wrong-schema staged file and persistent cache poison

- **Fix evidence:** `src/tickreplay/daily_context.py:288-291` validates a staged database with the exact production SQL and a bound limit before commit. Only then does `_commit_download()` replace the live file and sidecar (`daily_context.py:294-320`). A live query failure is handled while the same fixed stripe remains held and triggers one unconditional repair download before one final query (`daily_context.py:489-514`). A second failure degrades to unavailable instead of looping.
- **Atomicity/TOCTOU assessment:** Download, validation, data/sidecar replacement, and query all occur inside the same destination stripe. Data is replaced before the sidecar; interruption between the two can cause conservative extra revalidation but cannot publish a new validator for old bytes. The staged file is size-bounded and production-query validated before the atomic `os.replace`. The single-worker Cloud Run contract and descriptor-based `/jp` serving prevent an in-process filename race with readers.
- **Bypass assessment:** A remote `304` for a corrupt but usable live file does not preserve the poison: the subsequent production query fails and forces an unconditional repair without validators. A wrong-schema 200 response is rejected before replacement, leaving the previous live file intact and allowing a later retry.

### Resolved Medium: Limiter-queued negative-miss burst amplification

- **Fix evidence:** `src/tickreplay/server.py:507-518` captures the request arrival timestamp before awaiting the four-slot thread limiter and passes it to the loader. The timestamp comes from `daily_context.capture_request_started_at()` (`daily_context.py:189-191`), the same monotonic clock used to record and expire misses (`daily_context.py:194-219`). Direct loader callers retain safe entry-time capture through the optional fallback (`daily_context.py:469-496`).
- **Boundary/bypass assessment:** Requests whose arrival is strictly before the recorded miss coalesce; timestamp equality is deliberately treated as a post-miss explicit retry, and `expires_at <= now` deliberately expires the entry. The public-path integration test starts eight calls against a four-slot limiter, blocks the first 404 until all eight arrivals are captured, proves one origin GET for the entire burst, and proves a later explicit request produces exactly a second GET (`tests/test_tickreplay_server.py:514-594`). No limiter-wave bypass remains.

## Severity Inventory

- **Critical:** None.
- **High:** None.
- **Medium:** None.
- **Low:** None.

## Verified Controls and Non-Findings

- Stem-to-path use is constrained by an uppercase ASCII `[0-9A-Z]{4,5}` full match in the loader. The Cloud file route uses an anchored ASCII daily-stem rule, then descriptor-based regular-file and resolved-root checks; tested separators, encoded traversal, extra extensions, underscore, and out-of-length names do not reach a file.
- Request-controlled text cannot alter cache directory, suffix, part/sidecar names, conditional headers, SQL identifiers, or origin host. Uppercasing may canonicalize a Unicode alias to the same ASCII public stem, but it cannot introduce a separator, escape the cache root, create a distinct lock/cache key, or expose non-public data.
- The cutoff is regex-checked and parsed as an ISO calendar date. The limit is bounded to 1..500 by both FastAPI and the loader.
- SQL injection was not found: the table identifier is a module constant, while cutoff and limit are DuckDB parameters. No adjustment columns are queried.
- No XSS sink was introduced: new status text is assigned with `textContent`, and chart inputs are normalized to ISO dates and finite numeric values before use. No `innerHTML`, dynamic code execution, command execution, or secret-bearing logging was added.
- Backend file/query failures are collapsed to `{bars: [], available: false}`. Client validation errors echo only the submitted stem/date, not paths, DuckDB errors, URLs, headers, or credentials.
- Frontend request generation, object-token identity, full `stem|code|actualDate` identity, mode gating, and reset-before-await behavior prevent reviewed stale responses from mutating a later session or painting the wrong chart. No cross-user server-side session data is introduced.
- Existing `/api/session` and `/api/minute-context` contracts are not modified by the endpoint addition. The daily Cloud whitelist change is confined to `stocks_daily`; the existing descriptor-based streaming checks remain in force.
- Raw-price/SMA discontinuities around splits are an explicitly accepted product behavior and are not a security finding. The selected-day candle remains replay-tick-derived and stored selected-day/future adjusted values are not queried.

## Residual Risk and Validation Note

The focused Pass 3 security selection was rerun locally: 12 passed in 2.31 seconds, covering dedicated limiter/default-pool isolation, the eight-request/four-slot public limiter burst, later explicit retry, authoritative missing-file behavior, conditional refresh, invalid-stage preservation and retry, wrong-live-schema repair, declared and observed size caps, total deadline, bounded negative state, strict timestamp/TTL boundaries, and direct-loader waiter coalescing. The prior full focused gates remain supplied as passing (Python 123 with one Windows symlink skip; Node 91/91; Ruff, formatting, and syntax checks). The shared verifier's earlier lack of JSON after ten minutes remains an orchestration/gate-observability limitation, not a security blocker. This re-review was read-only except for updating this report and the required reviewer work log.
