## Bug Report: stock_daily 285A/285a case mismatch

### Error
- Message: Daily history resolves to `{available: false, bars: []}` when ticker `285A` is requested against a case-sensitive authoritative cache whose physical file is `285a.duckdb`.
- Location: `src/tickreplay/daily_context.py:240-247,424-450`
- Stack trace: the deterministic probe fails at `.agents/logs/repro-stock-daily-285a-case-mismatch.py:27` after printing `available=False, bars=0`; captured in `.agents/logs/troubleshoot-repro-stock-daily-285a-case-mismatch-initial.log`.

### Reproduction
- Steps:
  1. Use the current daily loader with `stem="285A"` and `local_authoritative=True`.
  2. Model the case-sensitive filesystem observation for the exact uppercase path as `is_file() == False`, matching a directory that contains only `285a.duckdb`.
  3. Assert that the daily context is available.
- Reproducibility: always in the deterministic case-sensitive probe; native Windows succeeds because filesystem lookup folds case. A real Linux bind-mount run remains unavailable because Docker Desktop's Linux engine is stopped.

### Immediate Context
- Failing code: `_live_path()` constructs only `cache_dir / "stocks_daily" / f"{stem}.duckdb"`; `_ensure_ready_locked()` returns `None` on an exact-path miss when `local_authoritative` is true, without trying a case variant or HTTP (`src/tickreplay/daily_context.py:240-247,424-450`).
- Call chain: uppercased UI input -> `/api/session` -> uppercased `/api/daily-context` stem -> `load_daily_context()` -> `_ensure_ready_locked()` -> exact uppercase `_live_path()` -> unavailable (`src/tickreplay/static/app.js:2069-2087`, `src/tickreplay/server.py:500-545`).
- Recent changes: `0bdf8682d1e0997f56388f0fdfee28c09a7d14fb` introduced `daily_context.py` with exact-only local resolution on 2026-08-30. `eb0d8a4393781c6e86fed68e2357a46cea5fab8b` had already added server-side case fallback on 2026-08-22.

### Affected Area
- Files involved: `src/tickreplay/daily_context.py`, `src/tickreplay/server.py`, `src/tickreplay/static/app.js`, `src/tickreplay/static/daily-chart.mjs`, and the contrasting server resolver in `cloud-run/main.py`.
- Related tests: `tests/test_tickreplay_daily_context.py` (41 passed), `tests/test_cloud_run_main.py` (62 passed, 1 unrelated warning), and `tests/test_tickreplay_server.py` (31 passed, 1 unrelated warning). None covers uppercase request plus lowercase-only physical daily file in authoritative mode.
- Data scope: the configured daily directory contains 293 lowercase-letter `.duckdb` names among 447 letter-bearing names; there are no case-insensitive duplicate pairs in the observed inventory.

### Initial Hypotheses (informed by Codex analysis)
1. Boundary/path-resolution defect: the authoritative daily reader assumes canonical uppercase physical filenames while the dataset preserves lowercase spellings. This explains the platform difference, exact branch, and 285A data. Codex confidence: unavailable because the read-only wrapper call hung past 300 seconds; evidence-based confidence: high.
2. Policy defect: `local_authoritative=true` intentionally disables HTTP fallback, but the daily path treats an exact-case miss as absence rather than resolving within the authoritative directory. This amplifies hypothesis 1 and may be the appropriate fix boundary. Codex confidence: unavailable; evidence-based confidence: high.
3. Remote server allowlist or downloader defect: uppercase `285A` might be rejected or unresolved remotely. Evidence contradicts this because `HEAD /jp/stocks_daily/285A.duckdb` returns 200 and `cloud-run/main.py` already tries stem case variants. Codex confidence: unavailable; evidence-based confidence: eliminated for the reproduced branch.

### Codex Pattern Recognition
- Error pattern: intended read-only Codex analysis produced no response and was interrupted after exceeding the requested 300-second timeout. No Codex claim is used as evidence.
- Known similar patterns: the same physical-name mismatch was previously handled at the cloud distribution boundary with `_stem_case_variants()` / `_open_first_existing()`; its Windows tests compare strings because `WindowsPath` equality folds case.
- Recommended investigation priority: independently validate the local boundary defect and blast radius; compare bounded case-variant lookup against bulk filename canonicalization; preserve authoritative no-HTTP semantics.
