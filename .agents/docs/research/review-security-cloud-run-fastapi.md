# Security Review: cloud-run FastAPI Migration

## Decision

**Merge recommendation: block pending mitigation or explicit security-owner risk acceptance.** There are no Critical findings, but one High availability finding remains reachable on the documented public, unauthenticated deployment. The High finding is preserved from the pre-migration implementation rather than introduced by this worktree; it is still merge-relevant because this migration continues to ship that endpoint under a single-worker, unlimited-timeout runtime.

Finding counts: **Critical 0 / High 1 / Medium 4 / Low 3**.

## Scope and Method

Reviewed worktree files only:

- `cloud-run/.env.example`
- `cloud-run/Dockerfile`
- `cloud-run/main.py`
- `cloud-run/requirements.txt`
- `docs/tick-replay.md`
- `pyproject.toml`
- `tests/test_cloud_run_main.py`
- `uv.lock`

The supplied patch, shared security/testing/CLI rules, current state/design files, and the historical DuckDB cache plan were used as context. Product, test, and configuration files were not edited. Dynamic probes used temporary directories and the in-process FastAPI `TestClient`; no live deployment, real dataset, or external network was touched.

Classification vocabulary:

- **Introduced**: caused by this worktree migration.
- **Preserved/pre-existing**: present in the `HEAD` implementation and retained by this migration.
- **Uncertain**: evidence is insufficient to establish provenance.

## Findings

### [High] SEC-H1 — Public GraphQL permits unbounded, expensive DuckDB work

- **Classification:** Preserved/pre-existing.
- **Status:** Confirmed missing bounds; denial-of-service impact is a high-confidence inference from the query plan and deployment topology. No destructive load test was run.
- **Merge relevance:** **Blocking under the team-review High policy.** Although the resolver and lack of authentication predate this migration, the new image continues to publish it and explicitly runs one worker with no request timeout. Acceptable alternatives are to mitigate it before merge or obtain an explicit, recorded risk acceptance with a compensating ingress/rate-limit control.
- **Evidence:** The deployment is documented as public and unauthenticated at `docs/tick-replay.md:27` and `docs/tick-replay.md:32-33`. The resolver accepts caller-controlled `sort_by`, `order`, `limit`, and date range without length or numeric bounds at `cloud-run/main.py:229-244`; caller-controlled lag values are expanded into SQL window expressions at `cloud-run/main.py:246-250`; and the query scans/ranks the dataset and interpolates the unbounded `limit` at `cloud-run/main.py:268-310`. The router is published without complexity, alias-count, rate, or authentication controls at `cloud-run/main.py:324-325`. The complete Docker command uses one worker and `--timeout 0` at `cloud-run/Dockerfile:14-17`.
- **Safe reproduction:** Against an empty temporary data directory, an unauthenticated GraphQL request using `sortBy: "Close[-1000000]"` and `limit: 2147483647` returned HTTP 200 with a DuckDB open error; the arguments reached database logic instead of being rejected. No real query was executed.
- **Impact:** A remote client can request very large lag windows/date ranges/limits and repeat the resolver through GraphQL aliases, consuming CPU, memory, and DuckDB I/O. A finite GraphQL `Int` type prevents SQL token injection but still permits values large enough to exhaust the single instance.
- **Recommendation:** Enforce small positive bounds for `limit`, lag, formula length, and date span; add GraphQL depth/alias/complexity limits; add per-client rate/concurrency limits at the ingress; use a finite request/query timeout with cancellation; and require authentication or an allowlist if the endpoint is not intended for arbitrary internet clients.

### [Medium] SEC-M1 — GraphQL returns raw DuckDB errors containing absolute server paths

- **Classification:** Preserved/pre-existing.
- **Status:** Confirmed dynamically.
- **Evidence:** DuckDB opens the environment-derived absolute path at `cloud-run/main.py:163` and `cloud-run/main.py:276-310`. Strawberry is mounted directly at `cloud-run/main.py:324-325`. The generic application handler at `cloud-run/main.py:157-160` does not sanitize exceptions that Strawberry converts into GraphQL error responses.
- **Reproduction:** With `mother.duckdb` absent, an unauthenticated `stockRankingRange` request returned HTTP 200 and `errors[0].message` containing the full temporary `STOCKDATA_CACHE_DIR` path.
- **Impact:** Remote callers learn filesystem layout and raw database error details, making further exploitation and operational reconnaissance easier.
- **Recommendation:** Configure Strawberry error processing/formatting to return a stable generic client message and keep the full exception only in private logs. Add a regression test asserting paths and SQL/database details are absent from GraphQL responses.

### [Medium] SEC-M2 — Concurrent cache replacement can serve new bytes under stale validators

- **Classification:** Introduced by this worktree change. Direct `StaticFiles.file_response` serving and the mounted application's shared `DATA_DIR/jp` cache are joined by this migration.
- **Status:** Confirmed dynamically.
- **Evidence:** The migration points the mounted app's cache at the file server's own tree at `cloud-run/main.py:37-45` and documents the shared location at `docs/tick-replay.md:36-42`. The download route obtains `stat_result` at `cloud-run/main.py:132-136`, then later creates a `FileResponse` from the pathname plus that earlier metadata at `cloud-run/main.py:146-154`; there is no shared per-symbol lock around this span.
- **Reproduction:** A temporary probe atomically replaced an allowed file after the route's `stat()` but before `FileResponse` opened it. The response body was the replacement (`new-content`) while its ETag remained the baseline old file's ETag.
- **Impact:** During a mounted-app refresh, clients may cache new bytes under stale ETag/Last-Modified/Content-Length metadata. Differing file sizes can also produce truncated or protocol-invalid transfers. Range/If-Range correctness can be violated during the same race.
- **Recommendation:** Coordinate serving with the same per-symbol lock used by cache commit, or open the file first and derive metadata from `fstat()` on that immutable descriptor. Prefer immutable/versioned file names where practical. Add a barrier test covering an atomic replace between metadata acquisition and response streaming.

### [Medium] SEC-M3 — Container runs the network-facing service as root

- **Classification:** Preserved/pre-existing hardening gap.
- **Status:** Confirmed by the complete 17-line Dockerfile.
- **Evidence:** The image starts from the default root user at `cloud-run/Dockerfile:1`, installs packages as root at `cloud-run/Dockerfile:9-10`, and reaches the final network-facing command at `cloud-run/Dockerfile:17` without any `USER` instruction. The service binds the port without an address restriction at `cloud-run/Dockerfile:17` and the application is intentionally unauthenticated at `docs/tick-replay.md:32-33`.
- **Impact:** A dependency or application RCE would initially have root privileges inside the container and write access to the mounted cache/data tree, increasing the blast radius for file tampering and persistence in the volume.
- **Recommendation:** Create a dedicated UID/GID, pre-create/chown only the required cache paths, and end the image with `USER`. At deployment, use `no-new-privileges`, drop capabilities, and make the root filesystem read-only while mounting only the cache path writable.

### [Medium] SEC-M4 — Production image ignores the lockfile and resolves floating dependencies

- **Classification:** Preserved/pre-existing installation pattern; the concrete FastAPI dependency set is changed by this worktree.
- **Status:** Confirmed.
- **Evidence:** The base tag is mutable at `cloud-run/Dockerfile:1`. The image copies only `requirements.txt` and runs unconstrained `pip install` at `cloud-run/Dockerfile:9-10`. Every runtime requirement is a lower bound rather than an exact pin at `cloud-run/requirements.txt:1-7`. The root lock does pin examples such as DuckDB, FastAPI, HTTPX, Starlette, Strawberry, and Uvicorn at `uv.lock:79-80`, `uv.lock:115-116`, `uv.lock:205-206`, `uv.lock:575-576`, `uv.lock:588-589`, and `uv.lock:650-651`, but the Dockerfile never copies or consumes `uv.lock`; Gunicorn is not in that lock.
- **Impact:** Identical source revisions can build materially different images, including newly released or compromised transitive packages, and there is no hash-verified production dependency closure.
- **Recommendation:** Pin the base image by digest and build from an exact, reviewed lock/constraints file with hashes. Prefer `uv sync --frozen --no-dev` or a generated fully pinned requirements file and record the resulting image digest/SBOM.

### [Low] SEC-L1 — Allowed-name symlinks escape the configured data root

- **Classification:** Preserved/pre-existing confinement gap.
- **Status:** Confirmed dynamically; ordinary remote traversal was not reproduced.
- **Evidence:** Listing follows directory entries with `entry.is_file()` at `cloud-run/main.py:89-94`. The whitelist constrains only the request string at `cloud-run/main.py:104-130`; the route then follows filesystem links through `Path.stat()` and pathname-based serving at `cloud-run/main.py:132-154`, without resolving and re-checking containment.
- **Reproduction:** A temporary `jp/stocks_trades/7203.duckdb` symlink pointing outside `STOCKDATA_CACHE_DIR` returned HTTP 200 with the external target's contents. By contrast, tested `../`, percent-encoded dot segments/slashes/backslashes, and NUL-suffix paths all returned 404.
- **Impact:** An actor that can place a symlink in the mounted data tree can expose an arbitrary container-readable file under an allowed URL. This requires local/volume write access not provided by the reviewed HTTP routes, so severity is Low.
- **Recommendation:** Reject symlinks, resolve both root and candidate strictly, require `candidate.is_relative_to(root)`, and use no-follow/open-by-descriptor primitives where supported to avoid a second symlink race.

### [Low] SEC-L2 — HEAD and edge conditional/Range semantics regress under the FastAPI route

- **Classification:** Introduced by this worktree change.
- **Status:** Confirmed dynamically.
- **Evidence:** The download route is registered only with `@app.get` at `cloud-run/main.py:121-122`; unmatched methods fall through the catch-all mount at `cloud-run/main.py:327-332`. The custom Range regex is case-sensitive at `cloud-run/main.py:109-118`, while conditional handling is delegated without compensating edge handling at `cloud-run/main.py:146-154`. Existing tests cover common ETag/date/Range cases at `tests/test_cloud_run_main.py:194-273` but not these edges.
- **Reproduction:** An allowed-file `HEAD` returned 404, `If-None-Match: *` returned 200, and `Range: Bytes=0-1` returned 416. The prior Flask GET route supplied HEAD automatically.
- **Impact:** Standards-compliant cache/download clients may miss validators, redownload data, or treat a valid range as unsatisfiable. This is primarily interoperability and cache correctness, not a direct exploit.
- **Recommendation:** Register HEAD explicitly, handle `If-None-Match: *`, treat the Range unit case-insensitively, and add focused tests. Consider centralizing the HTTP contract instead of combining a custom pre-parser with dependency internals.

### [Low] SEC-L3 — The migration intentionally expands an unauthenticated cleartext surface

- **Classification:** Introduced expansion of a preserved/explicitly accepted policy.
- **Status:** Residual risk, not an implementation surprise.
- **Evidence:** The new remote UI mount is documented at `docs/tick-replay.md:25-30`, lack of authentication is explicit at `docs/tick-replay.md:32-33`, and remote downloads use HTTP at `docs/tick-replay.md:62-65`. The catch-all mount exposes the tickreplay app at `cloud-run/main.py:327-332`.
- **Impact:** Anyone who can reach the host can enumerate/download the allowed data and call the UI/API; network intermediaries can observe or modify cleartext traffic. The historical plan explicitly accepts the lack of transport authenticity, and the data is documented as non-private.
- **Recommendation:** Preserve the risk acceptance in durable design documentation and enforce the intended reachability at the reverse proxy/firewall. Use TLS and authentication before the deployment carries private data or mutating operations.

## Required Focus-Area Conclusions

- **Path traversal:** The anchored whitelist blocked all tested remote traversal encodings. SQL-like path input cannot escape it. The confirmed residual is symlink-based confinement (SEC-L1), which requires mounted-directory write access.
- **Range/conditional requests:** Common 200/206/304/416 and If-Range cases pass the focused tests. The stat/open replacement race is a confirmed integrity defect (SEC-M2); HEAD, wildcard ETag, and case-insensitive Range edges regress (SEC-L2).
- **SQL injection:** No SQL injection was found. Formula tokens are restricted and column/order identifiers are mapped at `cloud-run/main.py:165-214`; dates are parsed and reformatted at `cloud-run/main.py:252-262`; GraphQL's integer coercion prevents SQL-token injection through `limit`. The same fields remain a resource-exhaustion vector because their magnitudes/lengths are unbounded (SEC-H1).
- **Environment/defaults:** `BACKCAST_DUCKDB_SERVER_URL` defaults to loopback and the cache defaults to the shared local tree at `cloud-run/main.py:37-45`; neither is request-controlled. This avoids a default external self-download, but deliberately couples serving and mutation (SEC-M2). Invalid `PORT` fails import rather than silently selecting another port.
- **Mounted-app lifespan:** The parent explicitly enters and exits the child's exported lifespan at `cloud-run/main.py:57-66`; the repository-bearing child route test at `tests/test_cloud_run_main.py:287-297` passes. Startup construction performs config validation and local recovery, not a loopback request, so no startup self-deadlock was found. Residual: calling the exported function directly rather than the child's router lifespan context could miss future child startup handlers if its implementation changes.
- **Dependencies/runtime:** No vulnerability-database audit was attempted, so this report makes no CVE claim. The material issues are the floating, unlocked production install (SEC-M4) and the current FastAPI TestClient deprecation warning.
- **Container privilege/network:** Root execution is confirmed (SEC-M3). Binding to all container interfaces is intentional for the published service, but access control is external and undocumented beyond the no-auth statement (SEC-L3).

## Extra Worktree Changes and Guardrail Review

### `cloud-run/.env.example`

This was an unreported extra change but is relevant operational documentation. It contains no secret. `python-dotenv` parsed the spaced assignments at `cloud-run/.env.example:9` and `cloud-run/.env.example:12` as `/cache/jp` and `http://localhost:8080`, so the spaces do not change runtime values. It does fail `git diff --check` because of trailing whitespace at `cloud-run/.env.example:8`, `cloud-run/.env.example:9`, and `cloud-run/.env.example:11`; this is hygiene/provenance, not a security defect. Its loopback/cache defaults are consistent with `cloud-run/main.py:44-45`.

### `uv.lock`

This was an unreported extra change but is relevant to root development/test dependency closure. Removal of Flask/Werkzeug/MarkupSafe follows the migration and reduces the root test environment's unused surface; `uv lock --check` passes. It does not secure the production image because the Dockerfile does not consume it (SEC-M4). No suspicious URL or editable/path source was found in the reviewed diff.

### Removed-assertion guardrail

The nine removed-assertion heuristic hits are framework API substitutions rather than weakened expectations: Flask `response.data`/`get_json()` assertions were replaced with FastAPI/HTTPX `response.content`/`json()` assertions, and the existing status/header/body expectations remain in `tests/test_cloud_run_main.py:95-273`. New mount/lifespan assertions were added at `tests/test_cloud_run_main.py:279-297`. The guardrail is therefore a false positive for cheating detection, although SEC-L2 identifies real missing edge coverage.

## Checks Run

- `uv run pytest tests/test_cloud_run_main.py -q` — **pass**, 22 passed; one Starlette warning that `httpx` TestClient support is deprecated in favor of `httpx2`.
- `uv run ruff check cloud-run/main.py tests/test_cloud_run_main.py` — **pass**.
- `uv run ruff format --check cloud-run/main.py tests/test_cloud_run_main.py` — **pass**.
- `uv lock --check` — **pass**, 39 packages resolved.
- Scoped `git diff --check` over the eight review files — **fail only** on trailing whitespace in `cloud-run/.env.example:8`, `:9`, and `:11`.
- Dynamic temporary-directory probes — traversal encodings 404; allowed-name symlink escape confirmed; stat/open ETag race confirmed; GraphQL path disclosure confirmed; unbounded GraphQL inputs accepted; HEAD/wildcard ETag/mixed-case Range regressions confirmed.
- `git status --short` before report creation — only the eight supplied worktree files were modified.

## Unverified / Residual

- No live-server, container build/run, vulnerability database, TLS, reverse-proxy, firewall, or host volume-permission check was performed.
- No expensive GraphQL request was issued against real data.
- Dependency behavior was inspected from the locally installed Starlette 1.6.0 resolved at `uv.lock:575-576`; the production image can drift because of SEC-M4.
