# Security Review: duckdb-server-cache

## Verdict

**Do not ship until the High stored-XSS finding is fixed.** The review found
0 Critical, 1 High, 6 Medium, and 2 Low findings. No hardcoded credentials,
path-traversal route, SQL-injection primitive, or broken Range/If-Range
implementation was found in the reviewed diff. The default upstream remains
plain HTTP by an explicitly accepted product decision; that decision makes
remote DuckDB contents untrusted and materially increases the impact of the
XSS and resource-boundary findings below.

## Scope

Reviewed all 22 files in `.agents/logs/review-diff-duckdb-server-cache.patch`:

- `cloud-run/.env.example`, `cloud-run/Dockerfile`, `cloud-run/main.py`,
  `cloud-run/requirements.txt`
- `pyproject.toml`, `uv.lock`
- `src/tickreplay/config.py`, `cache.py`, `cache_commit.py`, `repository.py`,
  `server.py`, `static/app.js`, `static/index.html`,
  `static/request-coordinator.mjs`, `static/request-coordinator.test.mjs`
- `tests/conftest.py`, `test_cloud_run_main.py`, `test_tickreplay_config.py`,
  `test_tickreplay_cache.py`, `test_tickreplay_cache_commit.py`,
  `test_tickreplay_repository.py`, `test_tickreplay_server.py`

Review focus included remote URL/path handling, listing trust boundaries,
Range/If-Range behavior, cache and server symlinks, deterministic partial and
sidecar paths, atomic replacement, status error disclosure, SQL construction,
frontend DOM sinks, and dependency provenance. Existing gate evidence supplied
by the lead was accepted as execution evidence; this reviewer performed a
read-only source/diff review and did not rerun the suites.

## Findings

### [High] Remote DuckDB `Type` values reach `innerHTML` without escaping

- **Location:** `src/tickreplay/static/app.js:304` and
  `src/tickreplay/static/app.js:307-311`
- **Exploit/failure condition:** `session.type` comes from the downloaded
  DuckDB's `stocks_board.Type` column. A compromised file server or on-path
  attacker on the configured plain-HTTP connection can return a structurally
  valid DuckDB containing a value such as an element with an event handler.
  `tapeRow()` concatenates that value into `row.innerHTML`, so the payload
  executes in the tick-replay origin when the row is rendered. The script can
  then call the local API and exfiltrate responses. This sink pre-existed, but
  the change converts its source from a locally managed database into
  unauthenticated remote content, making it exploitable across the new trust
  boundary.
- **Recommended fix:** Build the four `<span>` nodes with
  `document.createElement()` and assign every value with `textContent`; do not
  escape by ad-hoc string replacement. Add a browser/DOM regression test using
  a `Type` value containing markup and assert it is displayed literally and no
  element/event handler is created.

### [Medium] GET endpoints can trigger unbounded cache writes and exhaust disk

- **Location:** `src/tickreplay/server.py:375-377`,
  `src/tickreplay/server.py:409`, `src/tickreplay/server.py:431`, and
  `src/tickreplay/cache.py:348-350`
- **Exploit/failure condition:** An API caller can iterate known stems through
  the unauthenticated GET endpoints; a miss starts a multi-gigabyte background
  download. The stream has no maximum-byte or cache-budget check, and a
  malicious/upstream-intercepted response can omit `Content-Length` and stream
  until the filesystem reaches ENOSPC. Loopback binding limits ordinary remote
  access, but the CLI permits broader binding and a side-effecting GET is also
  exposed to cross-site request attempts from a browser. Removing automatic
  eviction in v1 does not require accepting unbounded new writes.
- **Recommended fix:** Require an explicit authenticated/CSRF-protected POST (or
  enforce Origin/Fetch-Metadata for the local UI), cap concurrent starts, and
  enforce configured per-file and total-cache budgets against both declared
  and actual bytes. A no-eviction policy can reject a new download when the
  budget is exceeded.

### [Medium] Deterministic cache temporary files are opened through symlinks

- **Location:** `src/tickreplay/cache.py:348`,
  `src/tickreplay/cache_commit.py:254`, and
  `src/tickreplay/repository.py:222`
- **Exploit/failure condition:** If another local principal or process can
  modify the configured cache directory, it can pre-create
  `<stem>.duckdb.part`, `<stem>.duckdb.sidecar.json.tmp`, or `_listing.json.tmp`
  as a symlink/hardlink to another file writable by the app. `open("wb")`,
  `write_bytes()`, and `write_text()` follow that link and truncate/overwrite
  the target with app privileges before the later atomic replace.
- **Recommended fix:** Require a private cache directory, reject symlinked
  cache roots and managed entries via `lstat`, and create temporary files with
  exclusive/no-follow semantics (`O_CREAT|O_EXCL|O_NOFOLLOW` where available)
  using unpredictable names in the verified cache directory. Recheck the
  resolved parent immediately before commit.

### [Medium] The public file server lists and serves symlink targets

- **Location:** `cloud-run/main.py:59` and `cloud-run/main.py:78-83`
- **Exploit/failure condition:** `os.path.isfile()` follows symlinks and
  `send_from_directory()` confines the lexical path but does not make an
  in-tree symlink's target safe. If a sync job or lower-privileged writer can
  place `jp/stocks_trades/7203.duckdb` as a link to a readable file outside
  `DATA_DIR`, the name is advertised and the target can be downloaded through
  the network service.
- **Recommended fix:** Exclude links with `os.path.islink`/`lstat`, resolve each
  candidate and require it to remain beneath the resolved data root, and open
  with no-follow semantics where supported. Add listing and download tests for
  an escaping symlink.

### [Medium] `/api/status` exposes raw internal exception strings

- **Location:** `src/tickreplay/server.py:142-143`,
  `src/tickreplay/server.py:339-341`, and
  `src/tickreplay/server.py:382-394`
- **Exploit/failure condition:** Download, DuckDB, commit, filesystem, and HTTP
  exceptions are converted with `str(error)`, retained in the snapshot, and
  returned to any status caller. Failures can reveal absolute cache paths,
  operating-system details, upstream host/URL details, and reconciliation
  internals. The existing server test explicitly requires the raw word from a
  mocked transport error, so this disclosure is currently contractual.
- **Recommended fix:** Store a stable public error code and generic message in
  `StemSnapshot`; log the detailed exception server-side with an opaque
  correlation ID. Test that paths, URLs, credentials, and injected transport
  text never appear in an API response.

### [Medium] Live listing responses are buffered and persisted before validation

- **Location:** `src/tickreplay/repository.py:317-321` and
  `src/tickreplay/repository.py:329-335`
- **Exploit/failure condition:** The unauthenticated upstream response is fully
  buffered by `Client.get()`, decoded without a size/schema/count limit, and
  written to `_listing.json` before the public result is filtered through
  `SYMBOL_STEM_RE`. A compromised server or HTTP on-path attacker can return a
  very large array or very large strings and consume process memory and cache
  disk even though none of the entries are valid symbols.
- **Recommended fix:** Stream with a small hard response limit, validate the
  exact JSON shape, cap item count and item length, apply `fullmatch` before
  persistence, and persist only the validated set.

### [Medium] Public GraphQL ranking inputs permit resource-exhaustion queries

- **Location:** `cloud-run/main.py:164-170`, `cloud-run/main.py:178`, and
  `cloud-run/main.py:202-236`
- **Exploit/failure condition:** The tokenizer prevents SQL-token injection and
  dates are reparsed before interpolation, but `sort_by` length/complexity,
  lag value, date span, and `limit` are unbounded. An unauthenticated caller can
  request huge lag/limit values or a broad date range, forcing expensive scans,
  windows, sorts, and result materialization on the public server.
- **Recommended fix:** Make `order` an enum and enforce conservative maximums
  for formula length/token count, lag, date range, and limit; reject reversed
  dates. Add boundary tests and request/runtime limits. Continue using the
  current token allowlist/parameterization approach.

### [Low] Production container inputs are not reproducibly pinned

- **Location:** `cloud-run/Dockerfile:1`, `cloud-run/Dockerfile:7`, and
  `cloud-run/requirements.txt:1-5`
- **Exploit/failure condition:** Every rebuild resolves a mutable base tag and
  open-ended `>=` dependency constraints outside the root `uv.lock`. A future
  compromised, vulnerable, or incompatible release can enter production
  without a source change.
- **Recommended fix:** Pin the base image by digest and install exact reviewed
  versions with hashes (or a dedicated locked export); update them through a
  controlled dependency-refresh process with vulnerability scanning.

### [Low] Stem regex checks accept a final newline instead of the exact identifier

- **Location:** `src/tickreplay/repository.py:54` and
  `src/tickreplay/repository.py:357-358`
- **Exploit/failure condition:** Python `$` can match immediately before a
  trailing newline, and callers use `SYMBOL_STEM_RE.match()`. A crafted upstream
  listing and encoded request can therefore admit a non-canonical stem such as
  `7203\n`. This does not create a slash-based traversal, but it weakens the
  stated filename contract and creates unexpected managed filenames/URLs.
- **Recommended fix:** Use `SYMBOL_STEM_RE.fullmatch(stem)` (or `\Z`) at one
  canonical validation boundary and apply the same strict ASCII contract to
  listing entries before they are persisted or used.

## Security Properties Confirmed

- The Flask download route's fixed-family whitelist plus
  `send_from_directory` blocks ordinary `..`/slash traversal; the remaining
  filesystem issue is symlink following, reported above.
- The client re-filters listing stems before normal repository use, and public
  repository read/download entry points reject slash traversal.
- Range, If-Range, If-None-Match, and If-Modified-Since behavior is delegated
  to Flask/Werkzeug with `conditional=True` and is pinned by focused tests.
- Repository session SQL uses parameters for code/date values. The cloud-run
  ranking formula has a strict token allowlist; no SQL-injection path was found.
- No hardcoded API keys, passwords, tokens, or committed `.env` secrets were
  found in the 22-file scope.

## Residual / Accepted Risks

- Plain HTTP and the absence of a signed manifest remain explicitly accepted
  product risks. The computed SHA-256 proves only that the staged bytes did not
  change after download; it does not authenticate them.
- Multi-process cache writers, automatic cache deletion, and a complete
  dependency-CVE audit were outside this review's supplied acceptance scope.
  No live vulnerability database was queried, so the dependency finding above
  is about provenance/reproducibility, not a claim that a specific locked
  version has a known CVE.

