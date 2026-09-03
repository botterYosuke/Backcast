# Impact Assessment: `stock_daily` 285A/285a Case Mismatch

## Summary

- The direct defect is confined to the local-authoritative `stocks_daily` read boundary. The API canonicalizes `285a` to `285A`, while the loader tests only `stocks_daily/285A.duckdb`; on an exact miss, `local_authoritative=True` returns `available=false` before HTTP or DuckDB is attempted (`src/tickreplay/server.py:500-545`, `src/tickreplay/daily_context.py:240-247,424-450`).
- The defect was introduced when `daily_context.py` was created in commit `0bdf8682d1e0997f56388f0fdfee28c09a7d14fb` on 2026-08-30. The no-HTTP authoritative policy is intentional, but the exact-case filename assumption is an omission: the repository already knew that dataset stems have inconsistent case and had added bounded server-side fallback in `eb0d8a4393781c6e86fed68e2357a46cea5fab8b` on 2026-08-22.
- Current `S:/jp/stocks_daily` inventory has 447 letter-bearing symbol files: 293 contain lowercase letters and 154 are uppercase. There are no case-insensitive duplicate groups in Daily. All 293 lowercase Daily symbols have a Trades counterpart; 195 currently have an uppercase Trades filename and can reach normal session loading, making those 195 the directly UI-reachable current blast radius. `285A` is one of them.
- The safest fix boundary is an exact-first, same-directory lowercase fallback only when reading a local-authoritative Daily file. Remote HTTP, live/part/sidecar destinations, and download commits must remain on their current uppercase canonical paths. Bulk renaming is materially riskier and is not required.
- Minute and Trades have related case inconsistencies but are not the same direct defect. Minute exact misses can fall back through HTTP and the cloud server's case resolver; Trades has a separate raw-listing/uppercase-membership exposure. Both should be tracked separately rather than folded into the Daily fix.

## Git History

### Introducing commit

- `git log --follow -- src/tickreplay/daily_context.py` returns one commit only: `0bdf8682d1e0997f56388f0fdfee28c09a7d14fb` (`2026-08-30T21:21:21+09:00`).
- `git show 0bdf8682^:src/tickreplay/daily_context.py` fails because the file does not exist in the parent. No checkout-based bisect is needed or appropriate.
- `git blame -L 240,247 -L 424,450 -- src/tickreplay/daily_context.py` assigns `_dataset_dir`, exact-only `_live_path`, `_ensure_ready_locked`, and the authoritative early return entirely to `0bdf8682`.
- The commit's stated intent was the Daily chart/history/paging feature: bounded strict-before Daily retrieval, SMA computation, and continued replay. It added `daily_context.py`, its server route, and 712 lines of Daily-context tests. Its message and design records do not state that uppercase physical filenames are authoritative (`.agents/docs/DESIGN.md`, FR-TICKREPLAY-8 through FR-TICKREPLAY-10).

### Related commits and policy intent

- `eb0d8a4393781c6e86fed68e2357a46cea5fab8b` (2026-08-22) added `cloud-run.main._stem_case_variants` and `_open_first_existing`. Its commit message explicitly names `285A.duckdb -> 285a.duckdb`, exact-first precedence, same-directory/same-suffix containment, and the Windows `Path` equality trap (`cloud-run/main.py:229-276`; `tests/test_cloud_run_main.py:306-345`).
- `git merge-base --is-ancestor eb0d8a43 0bdf8682` exits 0, proving that the case-fallback behavior predated the Daily loader.
- `d285db0921cd04340aa82c807917358628aa4dd5` (2026-08-23) established the explicit `local_authoritative` mode to avoid loopback revalidation against the same served tree. The intended invariant is local authority/no revalidation, not uppercase-only storage (`src/tickreplay/repository.py:479-537`, `.agents/docs/DESIGN.md` local-authoritative decision).
- Conclusion on intent: preserving the no-HTTP authoritative contract is intentional; treating an exact-case miss as authoritative absence despite a case-equivalent local file is not supported by the commit intent and conflicts with the already documented dataset behavior.

## Blast Radius

### Production call paths

Repository-wide `rg` found one production caller of `load_daily_context`:

1. Browser symbols are uppercased on load/change (`src/tickreplay/static/app.js:2069-2087`).
2. `/api/session` uppercases the symbol and loads the independent Trades session (`src/tickreplay/server.py:419-468`).
3. Daily selection and every older-page request call `/api/daily-context` (`src/tickreplay/static/app.js:527-670`; `src/tickreplay/static/daily-chart.mjs:572-613`).
4. The server uppercases again and calls `load_daily_context` on the bounded Daily I/O pool (`src/tickreplay/server.py:500-545`).
5. On a case-sensitive authoritative root, exact uppercase `_live_path().is_file()` is false for a lowercase-only file, and the function returns `_UNAVAILABLE` (`src/tickreplay/daily_context.py:424-450,469-516`).
6. The client converts this valid HTTP-200 payload to phase `unavailable` and displays the Daily-history error state (`src/tickreplay/static/daily-chart.mjs:164-170,742-749`; `src/tickreplay/static/app.js:510-524`).

The initial Daily history, SMA25/SMA200, and older Daily paging are affected. The Trades session, replay cursor, Tick chart, tape, board, orders, and positions are separate and are designed to continue; this failure is a supplementary Daily-data availability result, not a session exception.

### Current data inventory

Read-only `Get-ChildItem` enumeration of `S:/jp/{stocks_daily,stocks_minute,stocks_trades}` classified only 4-5 ASCII-alphanumeric `.duckdb` stems and grouped them by `BaseName.ToUpperInvariant()`:

| Dataset | All `.duckdb` | Valid symbol files | Letter-bearing files | Lowercase-letter files | Uppercase-letter files | Case-insensitive collision groups |
|---|---:|---:|---:|---:|---:|---:|
| `stocks_daily` | 5,072 | 5,071 | 447 | 293 | 154 | 0 |
| `stocks_minute` | 4,734 | 4,732 | 448 | 213 | 235 | 1 |
| `stocks_trades` | 4,805 | 4,734 | 447 | 104 | 343 | 0 |

- Daily potential scope: all 293 lowercase Daily files fail exact uppercase lookup under case-sensitive local-authoritative semantics.
- Current direct UI reachability: all 293 have a Trades counterpart; 195 have an uppercase Trades filename, while 98 have a lowercase Trades filename. The 195 uppercase-Trades symbols can pass the current session lookup and then hit this Daily defect.
- `285A` inventory: Daily has only `285a.duckdb`; Trades has `285A.duckdb`; Minute currently has both `285a.duckdb` and `285A.duckdb`.
- The sole current case-insensitive collision group is Minute `285A`: `285a.duckdb` and `285A.duckdb`, both 13,381,632 bytes with the same observed UTC modification time. This is direct operational evidence against unguarded bulk canonicalization; content identity was not asserted.

### Minute and Trades comparison

- Minute uses the same exact uppercase local path (`src/tickreplay/minute_context.py:80-85,132-137`) but has no `local_authoritative` branch. On an exact miss it requests `/jp/stocks_minute/{STEM}.duckdb`; the current cloud server resolves exact/lower/upper variants (`cloud-run/main.py:229-296`). Therefore it does not have the same guaranteed terminal failure while HTTP is reachable. If HTTP is unavailable, it can still discard an existing lowercase local file and return an empty optional preload; it can also materialize an uppercase sibling, as the observed Minute collision suggests. This is a related follow-up, not part of the Daily fix.
- Direct Cloud Run downloads for Daily, Minute, and Trades already share the bounded server-side case resolver, so the HTTP serving boundary is not the immediate defect (`cloud-run/main.py:229-296`).
- Trades has a separate discovery/membership issue: the server listing preserves physical filename case (`cloud-run/main.py:95-116`), while the client accepts only uppercase stems and compares requested uppercase membership exactly (`src/tickreplay/repository.py:65,418-442`). The current 104 lowercase Trades files merit a separate investigation. Changing Daily resolution does not fix or worsen this contract.

## Environment Matrix

| Runtime/storage mode | Current Daily result for uppercase request + lowercase-only file | Why |
|---|---|---|
| Native Windows, default case-insensitive lookup, authoritative | Usually works; bug is masked | `Path("285A.duckdb").is_file()` resolves `285a.duckdb`. The real configured probe returned `available=True, bars=1` (`.agents/docs/research/troubleshoot-stock-daily-285a-case-mismatch-context.md`). |
| Linux or other case-sensitive volume, authoritative | Fails for the 293 lowercase Daily symbols | Exact uppercase path is absent; line 437 returns `None`; no HTTP is allowed. The deterministic repro exits 1 (`.agents/logs/troubleshoot-repro-stock-daily-285a-case-mismatch-initial.log`). |
| Case-sensitive macOS volume, authoritative | Same failure as Linux | Failure depends on filesystem case semantics, not OS identity.
| Any filesystem, authoritative, exact uppercase file exists | Works | Exact candidate remains first and current size/query validation applies.
| Non-authoritative cache with current Cloud Run origin reachable | Normally works | Exact local miss triggers uppercase HTTP; the server resolves the lowercase source and the client commits to its uppercase cache destination.
| Non-authoritative cache with origin unavailable | Unavailable unless the exact uppercase cached file exists | A lowercase-only local cache is not currently considered; stale fallback is keyed to the exact uppercase path.
| Cloud Run direct `GET/HEAD` | Works for case-equivalent stored name | `_open_first_existing` tries exact/lower/upper after allowlist validation.

The user's failing production process/OS was not captured. If it is native Windows against a genuinely case-insensitive volume, this defect alone does not explain the report and an actual `/api/daily-context` response plus server log is still required.

## Existing/Missing Tests

### Existing coverage

Phase 1 ran the three directly related modules successfully: `tests/test_tickreplay_daily_context.py` 41 passed, `tests/test_cloud_run_main.py` 62 passed, and `tests/test_tickreplay_server.py` 31 passed (134 total; two unrelated FastAPI/Starlette warnings).

- Daily authoritative exact miss/no HTTP: `tests/test_tickreplay_daily_context.py:332-341`.
- Daily authoritative exact uppercase hit/no revalidation: `tests/test_tickreplay_daily_context.py:427-445`.
- Daily authoritative oversize exact file: `tests/test_tickreplay_daily_context.py:449-466`.
- Remote refresh, corrupt download/live DB, size/deadline, negative-cache, locking, and reuse behavior: `tests/test_tickreplay_daily_context.py:326-712`.
- Endpoint accepts lowercase input, but the fixture creates uppercase `285A`; the endpoint uppercases the request and therefore does not exercise lowercase physical storage (`tests/test_tickreplay_server.py:376-405`).
- Cloud server resolves an uppercase Daily request to a lowercase physical file (`tests/test_cloud_run_main.py:276-280`) and separately checks candidate order/containment (`tests/test_cloud_run_main.py:306-345`). This proves the remote boundary, not the local Daily reader.

### Exact missing regression tests

1. `test_authoritative_uppercase_request_reads_lowercase_only_daily_file_without_http`: simulate case-sensitive candidate existence, return/query the lowercase file, assert `available=True`, and make any HTTP request fail the test.
2. `test_authoritative_exact_daily_file_wins_over_lowercase_sibling`: simulate both names independently and verify the exact requested filename is selected deterministically.
3. `test_daily_case_candidates_preserve_parent_suffix_and_string_order`: assert candidate `.name` strings are `285A.duckdb`, then `285a.duckdb`; numeric-only stems yield one candidate; every candidate retains the fixed parent and `.duckdb` suffix.
4. `test_authoritative_lowercase_candidate_still_enforces_size_and_query_validation`: fallback must not bypass `MAX_DAILY_FILE_BYTES` or convert a corrupt/wrong-schema DB into availability.
5. `test_non_authoritative_daily_refresh_keeps_uppercase_live_part_sidecar_and_get_paths`: presence/simulation of a lowercase candidate must not change the current remote GET, commit, sidecar, or returned post-refresh path. This guards against selecting a lowercase path before refresh and querying it after the refresh wrote uppercase.
6. Retain the existing authoritative-missing/no-HTTP, remote-refresh, invalid-stem, and concurrency tests as unchanged contract gates. A focused concurrent local-fallback test is optional but useful to prove both readers resolve the same candidate under the existing uppercase-stem lock without mutation.

Windows-host-safe technique: do not create only `285a.duckdb` and then assert that `285A.duckdb` is absent; that assertion is false on a default Windows filesystem. Test candidate generation through raw `.name` strings, and inject/mock the case-sensitive existence probe keyed by the exact filename string for resolver tests. Do not deduplicate candidates with `Path` equality because `WindowsPath("285A.duckdb") == WindowsPath("285a.duckdb")` is true. The existing Cloud Run tests at `tests/test_cloud_run_main.py:306-345` are the model.

## External Context

No external dependency issue is involved, so no web research was necessary. The reproduced authoritative branch returns on `Path.is_file()`/size policy before invoking HTTP or DuckDB (`src/tickreplay/daily_context.py:424-450`). `pathlib`, DuckDB, and httpx are behaving according to their normal contracts; the defect is the application's filename canonicalization assumption. There is no upstream fix, migration guide, or dependency workaround to apply.

## Regression Risk

### Option A: bounded local-authoritative read fallback

Overall risk: **low when strictly scoped; medium if generalized through `_live_path` or remote readiness**.

| Concern | Assessment | Safeguard |
|---|---|---|
| Case collisions | Daily currently has zero groups, but collisions are possible and already exist in Minute. | Exact requested path wins when both are valid; test precedence. Do not rename or merge files. |
| Path escape | Request stems are restricted to 4-5 uppercase ASCII alphanumerics (`SYMBOL_STEM_RE`), so no separator or `..` can enter the candidate. | Derive variants with `with_name`, retain the same parent/suffix, and test these invariants. Never glob based on user input. |
| Symlinks | `Path.is_file()` and DuckDB currently follow an exact-path symlink. A lowercase candidate symlink would extend that existing behavior to the fallback name, but the authoritative tree is operator-controlled. | Preserve existing symlink semantics in the minimal fix. If containment/no-follow becomes a security requirement, apply it consistently to exact and fallback paths in a separately reviewed change; fallback-only rejection would be incompatible. |
| No-HTTP authoritative contract | Safe if resolution remains inside the `local_authoritative` branch and returns unavailable when no usable candidate exists. | Keep `_refresh_ready` unreachable in this branch; keep the existing fail-on-HTTP test and add lowercase-hit/no-HTTP coverage. |
| Remote cache/sidecars | Unsafe if a generalized `_live_path` dynamically returns a lowercase existing path while refresh writes or bookkeeping use uppercase paths. A refresh could update uppercase and then query stale lowercase, or split DB and sidecar identity. | Do not change `_live_path`, `_part_path`, `_sidecar_path`, `_sidecar_tmp_path`, HTTP URL, or commit destinations. Use a distinct authoritative-read resolver only. |
| Performance | At most one additional existence/stat probe per Daily API call; negligible versus DuckDB open/query. A 5,000-entry directory scan on every paging request would not be negligible. | Use bounded candidates, not `iterdir`/glob or a casefold map. No cache is necessary, avoiding invalidation complexity. |
| Concurrency | Candidate selection and query already occur under `_lock_for(cache_dir, uppercase_stem)`. The fallback is read-only and adds no in-process writer. External sync/rename races already exist and are mapped to unavailable. | Resolve under the existing lock and avoid a new lock/cache. Do not perform filesystem mutation. |
| Compatibility | Exact uppercase users and numeric stems retain current behavior; lower-stored authoritative data becomes readable. | Scope to `local_authoritative=True`; preserve all remote semantics and availability payload shape. |

### Option B: bulk filename canonicalization

Overall risk: **high operational and compatibility risk**.

- It mutates an authoritative data tree and is impossible on read-only deployments. It also expands the incident from one read boundary to every producer and consumer of Daily/Minute/Trades files.
- A preflight found no current Daily collision, but current Minute already contains both `285a.duckdb` and `285A.duckdb`. Any generic canonicalizer must stop on collisions and prove content identity; size/mtime equality is insufficient.
- Case-only renames are filesystem-dependent. Windows commonly needs a temporary intermediate name; SMB and case-sensitive shares can expose both names. Crash recovery and rollback become necessary.
- Renames can race with dataset synchronization and open DuckDB readers. Windows can reject renaming open files; Linux readers can retain an old inode while new lookups see the renamed entry.
- Sidecars and transient `.part`/`.tmp` artifacts can be separated from their database if only the `.duckdb` file is renamed. Other tools may deliberately use the physical lowercase naming convention.
- A complete canonicalization would require an inventory lock, collision manifest, sidecar-aware two-phase renames, reader downtime or coordination, post-rename validation, and a rollback manifest. That is disproportionate to the localized Daily reader defect.

## Recommended Safeguards

1. Add a dedicated authoritative-local resolver, not a dynamic replacement for `_live_path`. Candidate order should be exact, then fully lowercase (optionally fully uppercase for symmetry), deduplicated by filename string rather than `Path` equality.
2. Call it only inside `_ensure_ready_locked(..., local_authoritative=True)` while the existing uppercase-stem lock is held. Choose only a file that passes the current size policy; leave DuckDB query validation and controlled `available=false` behavior unchanged.
3. Preserve the authoritative no-HTTP invariant and all uppercase remote GET/live/part/sidecar/commit destinations.
4. Preserve exact-first precedence on a case-sensitive collision. Do not scan, rename, copy, or delete files as part of request handling.
5. Implement the six focused tests above using string-based candidate assertions and a simulated case-sensitive existence probe so they remain meaningful on Windows CI.
6. Open separate follow-up investigations for Minute's local exact-miss/download behavior and Trades' raw-case listing/membership mismatch. Do not expand this Daily fix across those different cache contracts.
7. Verify the final implementation in a real Linux/case-sensitive container or deployment. The local Docker Desktop Linux engine was unavailable during Phase 1, so this remains an acceptance check rather than current evidence.

## Codex Consultation Status

Both mandatory read-only consultations were attempted through `.agents/skills/_shared/codex_consult.py` with `--sandbox read-only --timeout 30`:

- Regression risk: timed out, `duration_sec=35.279`, response `.agents/logs/codex/20260903T131910Z-troubleshoot-regression-stock-daily-285a.md`.
- Fix safety: timed out, `duration_sec=38.159`, response `.agents/logs/codex/20260903T131949Z-troubleshoot-fix-safety-stock-daily-285a.md`; the still-running wrapper cleanup was interrupted after its timeout result was emitted.

Both responses stopped after loading shared context and asked for an Objective even though the prompt files contained one. Neither evaluated the code, risks, or proposed fix. They are unusable and are not cited as support. Prompts are retained at `.agents/logs/codex/prompt-troubleshoot-regression-stock-daily-285a.md` and `.agents/logs/codex/prompt-troubleshoot-fix-safety-stock-daily-285a.md` plus the wrapper's timestamped audit copies.

## Remaining Unknowns

- The exact OS/filesystem and raw `/api/daily-context` payload from the user's failing process were not captured. A native Windows failure would require another cause in addition to this proven case-sensitive branch.
- A real Linux bind-mount reproduction could not run because Docker Desktop's Linux engine was stopped. The deterministic current-code repro proves the branch but not the final deployment wiring.
- Inventory is a point-in-time observation and can change with dataset synchronization. The 293/195/104 counts should not be hard-coded into application behavior or tests.
- The 104 lowercase Trades files and Minute's `285a`/`285A` duplicate were not functionally reproduced in this task; they are evidence-backed follow-up risks, not conclusions that broaden the Daily fix.

