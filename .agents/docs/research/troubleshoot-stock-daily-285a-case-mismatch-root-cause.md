# Root Cause Analysis: stock_daily 285A/285a case mismatch

## Summary

The defect is a missing physical-name resolution step at the local authoritative
Daily boundary. The application correctly canonicalizes the logical symbol to
uppercase, but `daily_context` then assumes that the physical `.duckdb` basename
uses the same spelling. On a case-sensitive filesystem, `285A` therefore misses
the valid `stocks_daily/285a.duckdb` and is reported as unavailable. The
`local_authoritative` no-HTTP policy is intentional and tested; it exposes the
bad absence classification but is not itself the root cause.

The safest fix is exact-first, bounded case-variant resolution only for the
authoritative Daily read path. Remote refresh/download/sidecar destinations must
remain canonical uppercase paths. A data-wide rename is materially broader,
platform-sensitive, and can drift again unless every producer is changed.

## Execution Flow

1. The load-button and symbol-change handlers trim the symbol and call
   `toUpperCase()`, so user input `285a` and `285A` both become logical stem
   `285A` (`src/tickreplay/static/app.js:2069-2087`).
2. `loadSession()` sends that stem to `/api/session`; the backend uppercases it
   again and returns the resolved session, whose `session.stem` is staged in the
   Daily identity (`src/tickreplay/static/app.js:1952-1985`,
   `src/tickreplay/server.py:437-458`).
3. Entering Daily calls `loadDailyContext()`; `dailyContextUrl()` copies the
   identity stem into `/api/daily-context?stem=285A...`
   (`src/tickreplay/static/app.js:641-670`,
   `src/tickreplay/static/daily-chart.mjs:572-611`).
4. `/api/daily-context` uppercases the stem once more, then passes the
   repository cache directory and `repository.local_authoritative` into
   `load_daily_context()` (`src/tickreplay/server.py:500-542`).
5. Input validation accepts only canonical uppercase stems through
   `SYMBOL_STEM_RE`; after validation, `_live_path()` constructs exactly
   `<cache>/stocks_daily/285A.duckdb` (`src/tickreplay/repository.py:67`,
   `src/tickreplay/daily_context.py:155-169,240-247,485-490`).
6. `_ensure_ready_locked()` checks only that exact `Path`. Under case-sensitive
   semantics the path is absent when the sole directory entry is
   `285a.duckdb`; `existing=False`, `usable=False`, and
   `local_authoritative=True` returns `None` before `_refresh_ready()` can run
   (`src/tickreplay/daily_context.py:424-450`).
7. `load_daily_context()` converts `None` to `_UNAVAILABLE`, the API returns
   HTTP 200 with `available=false,bars=[]`, and the client commits phase
   `unavailable` (`src/tickreplay/daily_context.py:469-515`,
   `src/tickreplay/static/daily-chart.mjs:164-170,742-749`,
   `src/tickreplay/static/app.js:510-524`).

The captured current-code probe confirms the decisive transition: it prints
`available=False, bars=0` and exits 1 after asserting availability
(`.agents/logs/troubleshoot-repro-stock-daily-285a-case-mismatch-initial.log:1-12`).
The same loader against the real file succeeds on native Windows because the
filesystem folds case (`.agents/docs/research/troubleshoot-stock-daily-285a-case-mismatch-context.md:48-57`).

## Hypotheses Evaluated

### 1. Boundary/path-resolution defect — CONFIRMED

Evidence for:

- Every public input path canonicalizes to uppercase, but `_live_path()` maps
  that logical identifier directly to one same-cased physical basename
  (`src/tickreplay/static/app.js:2069-2087`,
  `src/tickreplay/server.py:521-539`,
  `src/tickreplay/daily_context.py:240-247`).
- The physical inventory contains exact `285a.duckdb`, not exact
  `285A.duckdb`, and the file is a readable DuckDB with 405 rows
  (`.agents/docs/research/troubleshoot-stock-daily-285a-case-mismatch-context.md:91-103`).
- The deterministic case-sensitive observation reproduces the failure, while
  Windows case folding masks it (repro log above and context lines 41-57).
- Git blame attributes both exact-only `_live_path()` and the authoritative
  early return to the Daily feature's introducing commit `0bdf8682`
  (`.agents/docs/research/troubleshoot-stock-daily-285a-case-mismatch-context.md:152-154`).

Evidence against or limitation: Docker/Linux execution was unavailable, and
the actual failing process OS was not captured. This limits environment
confirmation, not the deterministic branch-level conclusion.

### 2. `local_authoritative` policy defect — ELIMINATED as root cause

The policy intentionally treats the cache tree as the origin and avoids HTTP
self-revalidation (`src/tickreplay/config.py:77-92`,
`src/tickreplay/repository.py:306-310,515-519`). The Daily tests explicitly
require an authoritative miss to avoid loopback HTTP
(`tests/test_tickreplay_daily_context.py:332-341`) and require an existing
authoritative file to be used without fetching
(`tests/test_tickreplay_daily_context.py:427-446`). Exact-case files work.

The policy is a trigger/amplifier: without HTTP fallback, a falsely classified
local miss becomes `unavailable`. The underlying defect is that a logically
existing authoritative object is classified as absent before that policy is
applied. The fix must preserve no-HTTP semantics.

### 3. Remote allowlist/downloader defect — ELIMINATED for this branch

The reproduced authoritative branch never calls HTTP. Independently, the
remote allowlist permits 4-5 ASCII alphanumeric Daily stems, and the server
tries exact, lowercase, and uppercase basenames without changing the directory
or suffix (`cloud-run/main.py:121-140,229-276,279-296`). Its HTTP regression
test proves an uppercase Daily request can read a lowercase physical file
(`tests/test_cloud_run_main.py:276-280`). The configured uppercase HEAD also
returned 200 (`.agents/docs/research/troubleshoot-stock-daily-285a-case-mismatch-context.md:118-126`).

## Definitive Root Cause

The system has an inconsistent identifier-to-path contract. The symbol is a
canonical uppercase logical identifier, while the Daily dataset permits
case-preserving physical basenames. The remote distribution boundary reconciles
those representations, but the newer local authoritative Daily reader does
not. It uses the canonical identifier as an exact physical basename, so a
case-sensitive filesystem turns a representation mismatch into a false absence.

The regression entered with `0bdf8682` (2026-08-30), which introduced
`daily_context.py`. It did not mirror the bounded resolver already added to the
remote server by `eb0d8a43` (2026-08-22). The authoritative policy itself was
introduced intentionally by `d285db09` (2026-08-23)
(`.agents/docs/research/troubleshoot-stock-daily-285a-case-mismatch-context.md:152-154`).

## Trigger Conditions

All of the following are required for this exact failure:

1. The requested Daily stem contains a letter and is canonicalized to uppercase.
2. The authoritative `stocks_daily` directory contains only another supported
   case spelling, such as `285a.duckdb`, and no exact `285A.duckdb`.
3. Lookup uses case-sensitive filesystem semantics (Linux/container or the
   deterministic equivalent). Native Windows normally masks the mismatch.
4. `local_authoritative=True`, so an exact local miss returns unavailable rather
   than asking the already case-tolerant remote server.
5. The primary replay session is otherwise reachable, allowing the user to
   enter Daily mode.

The inventory shows this is not a one-symbol anomaly: 293 of 447 letter-bearing
Daily filenames contain lowercase letters, with no observed case-insensitive
duplicate pair (`.agents/docs/research/troubleshoot-stock-daily-285a-case-mismatch-context.md:106-115`).

## Fix Alternatives

### A. Bounded authoritative Daily path resolution

After `_validate_inputs()` and only when `local_authoritative=True`, generate
candidate basenames in exact-first order: requested stem, fully lowercase stem,
then fully uppercase stem, deduplicated by string spelling. Keep the parent
directory and `.duckdb` suffix unchanged. Select the first *existing* candidate,
then apply the existing size and DuckDB-query checks to that selected path.

Important boundaries:

- Exact presence wins even if the exact file is empty, oversized, or corrupt;
  do not silently bypass a bad canonical file with a sibling variant.
- Do not use `Path` equality to deduplicate candidates. `WindowsPath` equality
  folds case; compare stem/name strings, as the remote resolver already does
  (`cloud-run/main.py:249-259`).
- Do not apply variant resolution to remote-cache readiness. `_refresh()`,
  `_commit_download()`, sidecars, and part files are all keyed to the canonical
  uppercase stem (`src/tickreplay/daily_context.py:250-259,277-315,323-389`).
  Preselecting a lowercase read path there could query stale lowercase data
  after a refresh has committed a new uppercase file.
- Keep invalid-input rejection before resolution. `SYMBOL_STEM_RE` limits the
  stem to 4-5 uppercase alphanumerics, and varying only case within the same
  parent/suffix does not add traversal capability.
- Preserve current symlink behavior. Adding fallback-only `resolve()`
  containment rules would be a separate compatibility/security-policy change.

Pros: smallest behavior change; no data migration; preserves authoritative
no-HTTP and remote-cache semantics; constant candidate count; matches the
already deployed remote convention. Cons: intentionally handles full lower/
upper variants, not arbitrary mixed case. The current inventory contains no
mixed-case Daily basename.

### B. Canonicalize every physical Daily filename

Rename lowercase Daily databases to uppercase and enforce uppercase naming in
all ingestion/sync producers. This removes the representation mismatch at the
data layer.

Pros: one canonical physical convention and simpler readers if enforcement is
permanent. Cons: an operational migration touches hundreds of files; must
preflight collisions and migrate related sidecars/temporary artifacts; case-only
renames on Windows often require an intermediate name; active readers and
external case-sensitive consumers can break; newly synced lowercase files
reintroduce the bug unless every producer is fixed. The current zero-collision
inventory reduces but does not remove future collision/data-loss risk.

### C. Case-folded directory index with collision rejection

Build or scan a same-directory index keyed by case-folded basename, require
exactly one match, and reject ambiguity. This handles arbitrary mixed-case
spellings and makes collision handling explicit.

Pros: strongest spelling tolerance and explicit ambiguity detection. Cons:
scanning 5,072 files per request is wasteful; caching needs invalidation and
race rules; it adds substantially more code and filesystem state than the
observed lower/upper-only dataset needs. It is justified only if arbitrary
mixed-case producers become a supported requirement.

## Recommendation

Implement Alternative A at the authoritative Daily read boundary. Keep
`_live_path()` and all remote write/sidecar helpers canonical; introduce a small
exact-first resolver used only by the `local_authoritative` branch of
`_ensure_ready_locked()`. Prefer the first existing candidate and then run the
existing size/query checks so exact-file corruption is not masked.

Required regression coverage:

- Pure candidate-generation tests asserting string names, same parent/suffix,
  exact-first ordering, lowercase inclusion, and digits-only deduplication.
- A platform-independent behavioral test that mocks case-sensitive
  `is_file()`/`stat()` observations so uppercase request + lowercase-only file
  fails before the fix even on Windows, and asserts no HTTP call.
- Exact-plus-lowercase collision coverage proving exact wins, including an
  unusable exact file that must not fall through to the lowercase sibling.
- Missing, zero-size, oversize, and corrupt authoritative cases remain
  unavailable; invalid stems remain `ValueError`/HTTP 400.
- Remote mode retains uppercase live/part/sidecar destinations and existing
  conditional refresh behavior.
- Run the focused Python tests on Windows and a real lowercase-only filesystem
  test in Linux/container after Docker is available.

Do not expand this patch into primary `stocks_trades` or `stocks_minute`
resolution. The Impact Investigator found adjacent raw-case listing/lookup
risks there, but those are distinct contracts and should be a follow-up issue.

## Codex Consultation Status

Codex CLI 0.152.1 was available. Six required read-only wrapper consultations
were launched for execution flow, each of the three hypotheses, fix design, and
fix correctness using `codex_consult.py`, `--sandbox read-only`, and a finite
45-second wrapper timeout. All remained completely silent after approximately
30 seconds and created zero-byte response files timestamped
`20260903T131657Z`. At the Lead's instruction they were interrupted immediately;
all six wrapper sessions exited 1 with no stdout. No further Codex calls were
made, and no Codex output is treated as evidence.

The prompts and zero-byte response artifacts are under `.agents/logs/codex/`
with labels `troubleshoot-flow`, `troubleshoot-hypothesis-boundary`,
`troubleshoot-hypothesis-policy`, `troubleshoot-hypothesis-remote`,
`troubleshoot-fix-design`, and `troubleshoot-fix-verify`.

## Remaining Uncertainty

- The real failing process OS and raw `/api/daily-context` response were not
  captured. If the observed failure occurs in native Windows, collect the raw
  response and private server log because case sensitivity alone does not
  explain that environment.
- Docker Desktop's Linux engine was stopped, so no real bind-mounted/container
  reproduction has yet supplemented the deterministic probe
  (`.agents/docs/research/troubleshoot-stock-daily-285a-case-mismatch-context.md:59-63,156-162`).
- The inventory currently has no case-insensitive Daily collision and no mixed-
  case basename, but future data producers could introduce either. Exact-first
  semantics must be documented, and arbitrary mixed case should trigger the
  stronger indexed approach rather than unbounded guessing.
- Adjacent `stocks_trades` and `stocks_minute` case inconsistencies are outside
  this fix. They may affect which symbols can reach a replay session on Linux
  and warrant a separate investigation.

