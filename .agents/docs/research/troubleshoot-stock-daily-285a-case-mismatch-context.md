# stock_daily 285A/285a case-mismatch context

## Summary

- The configured authoritative daily root contains the valid physical file `S:\jp\stocks_daily\285a.duckdb`, but the UI and API normalize the symbol to `285A`, and the daily loader constructs only `stocks_daily/285A.duckdb`. On a case-sensitive filesystem, the `local_authoritative=true` branch therefore returns `available=false` without trying a case variant or HTTP (`src/tickreplay/static/app.js:2069-2076`, `src/tickreplay/server.py:522-541`, `src/tickreplay/daily_context.py:240-247`, `src/tickreplay/daily_context.py:424-437`).
- The remote file-serving path is not the immediate defect: its generic `_stem_case_variants` / `_open_first_existing` fallback applies to `stocks_daily`, and a read-only HEAD against the configured server for uppercase `285A` returned 200 (`cloud-run/main.py:229-276`, `cloud-run/main.py:280-296`; command evidence below).
- The defect was introduced with the daily feature in commit `0bdf8682` on 2026-08-30. The cloud-server case fallback predates it in `eb0d8a43` on 2026-08-22, but the new client-side authoritative daily path did not reuse or mirror that resolution rule (`git blame` and `git show` evidence below).
- This is potentially broader than one symbol: a read-only inventory found 293 lowercase-letter filenames among 447 letter-bearing daily DuckDB files, with no case-insensitive duplicate pairs. Every such file is vulnerable when read through a case-sensitive authoritative cache because requests are uppercased (inventory command below).

## Reproduction command/result

The Phase 1 `repro.py` wrapper was intentionally not used because it necessarily writes `.agents/logs/troubleshoot-repro-*.log`, while this delegated task permits writing only this analysis artifact. The following direct probe executes the current production loader and injects only the case-sensitive `is_file=False` observation that Linux would make for an uppercase request when the sole physical name is lowercase. It performs no filesystem or network write:

```powershell
$env:PYTHONPATH='src'
@'
from pathlib import Path
from unittest.mock import patch
from tickreplay import daily_context

class CaseSensitiveDailyPath:
    def is_file(self) -> bool:
        return False  # requested 285A; only physical 285a exists

with patch.object(daily_context, '_live_path', return_value=CaseSensitiveDailyPath()):
    result = daily_context.load_daily_context(
        Path('/data'), object(), stem='285A', before_date='2026-08-20',
        limit=1, local_authoritative=True,
    )
print(f'available={result.available}, bars={len(result.bars)}')
assert result.available, 'uppercase 285A did not resolve lowercase-only 285a.duckdb'
'@ | uv run python -
$probeExit=$LASTEXITCODE
Write-Output "exit_code=$probeExit"
exit $probeExit
```

Result on 2026-09-03:

```text
stdout: available=False, bars=0
stderr: Traceback (most recent call last):
          File "<stdin>", line 19, in <module>
        AssertionError: uppercase 285A did not resolve lowercase-only 285a.duckdb
exit_code=1
```

The same current loader against the real configured path on native Windows returned `result_available=True bars=1`, exit 0, because Windows folds filename case. That platform contrast is deterministic evidence that this bug requires a case-sensitive runtime:

```text
command: uv run python -c <load_daily_context(Path(r'S:\jp'), object(), stem='285A', before_date='2026-08-20', limit=1, local_authoritative=True)>
stdout: platform=Windows
        requested=S:/jp/stocks_daily/285A.duckdb
        result_available=True bars=1
        exit_code=0
```

An ephemeral real-Linux reproduction was attempted read-only, but Docker Desktop was not running. `docker image ls` failed before any container started with:

```text
error during connect: Head "http://%2F%2F.%2Fpipe%2FdockerDesktopLinuxEngine/_ping": open //./pipe/dockerDesktopLinuxEngine: The system cannot find the file specified.
```

## Execution flow and immediate cause

1. The load button takes `symbol-input`, trims it, and calls `.toUpperCase()`, then `loadSession(stem, ...)` (`src/tickreplay/static/app.js:2069-2076`). The symbol change path uppercases identically (`src/tickreplay/static/app.js:2079-2087`).
2. Session loading requests `/api/session?stem=285A...`; the server uppercases again before repository access (`src/tickreplay/static/app.js:1952-1974`, `src/tickreplay/server.py:438-458`). The returned session stem is staged into the daily identity (`src/tickreplay/static/app.js:1975-1985`).
3. Selecting Daily calls `loadDailyContext()` (`src/tickreplay/static/app.js:641-670`). `dailyContextUrl()` takes the uppercase stem from `stem|code|date` and builds `/api/daily-context?stem=285A...` (`src/tickreplay/static/daily-chart.mjs:572-588`, `src/tickreplay/static/daily-chart.mjs:591-613`, `src/tickreplay/static/app.js:527-538`).
4. `/api/daily-context` uppercases the stem yet again and passes `repository.cache_dir` plus `repository.local_authoritative` to `load_daily_context` on a worker thread (`src/tickreplay/server.py:500-541`).
5. The loader sets `DIRNAME = "stocks_daily"`, and `_live_path(cache_dir, stem)` creates exactly `cache_dir / "stocks_daily" / f"{stem}.duckdb"`; it has no case-variant resolution (`src/tickreplay/daily_context.py:29-30`, `src/tickreplay/daily_context.py:240-247`).
6. `_ensure_ready_locked()` tests only that exact `Path`. With `local_authoritative=true`, `existing=False` makes `usable=False`, and line 437 returns `None` immediately. It does not reach `_refresh_ready()` (`src/tickreplay/daily_context.py:424-450`). `load_daily_context()` converts that `None` to `_UNAVAILABLE`, i.e. `{available: false, bars: []}` rather than raising (`src/tickreplay/daily_context.py:469-516`).
7. The client normalizer maps `{available:false,bars:[]}` to phase `unavailable`, which the UI displays as the daily-history unavailable state (`src/tickreplay/static/daily-chart.mjs:164-170`, `src/tickreplay/static/daily-chart.mjs:742-749`, `src/tickreplay/static/app.js:510-524`).

Immediate cause: the local/authoritative daily reader assumes canonical uppercase physical filenames, while the actual dataset preserves lowercase spellings. The server-side path already handles this boundary safely by varying only the stem while retaining directory and suffix (`cloud-run/main.py:229-276`), but `daily_context._live_path()` has no equivalent.

## Path/casing evidence

Secret-safe config probe (only the three relevant non-secret fields were printed):

```text
cache_dir=S:\jp
local_authoritative=true
server_url_configured=True
```

Therefore the code-resolved daily request is `S:\jp\stocks_daily\285A.duckdb` (`src/tickreplay/daily_context.py:29`, `src/tickreplay/daily_context.py:240-247`). A read-only directory enumeration reported the actual directory entry:

```text
FullName         : S:\jp\stocks_daily\285a.duckdb
Name             : 285a.duckdb
Length           : 8663040
LastWriteTimeUtc : 2026/08/19 10:31:30
```

Case-sensitive name comparison of the enumeration found `Contains285a=True` and `ContainsExact285A=False`. Native Windows `Test-Path` reports both spellings as present because the filesystem lookup is case-insensitive; that is why a directory enumeration, not `Test-Path`, is the casing evidence.

The file itself is a valid, readable DuckDB. A read-only query returned:

```text
command: SELECT COUNT(*), MIN(TRY_CAST("Date" AS DATE)), MAX(TRY_CAST("Date" AS DATE)) FROM stocks_daily
path: S:\jp\stocks_daily\285a.duckdb (duckdb.connect(..., read_only=True))
stdout: rows=405 min_date=2024-12-18 max_date=2026-08-19
exit_code=0
```

Read-only inventory of `S:\jp\stocks_daily`:

```text
TotalDuckdb=5072
LetterBearing=447                  # excluding mother
LowercaseLetterNamed=293
UppercaseLetterNamed=154
CaseInsensitiveCollisions=0
Contains285a=True
ContainsExact285A=False
```

Finally, a body-free request to the configured origin proved the uppercase remote path currently resolves:

```text
command: HEAD <configured-server>/jp/stocks_daily/285A.duckdb
stdout: HEAD_upper_status=200
        content_length_present=True
        etag_present=True
exit_code=0
```

This agrees with `ALLOWED_PATHS` permitting 4-5 ASCII alphanumeric daily stems and `download_file()` using `_open_first_existing()` (`cloud-run/main.py:121-140`, `cloud-run/main.py:280-296`).

## Tests and history

All three directly related test modules pass on this Windows host:

```text
uv run pytest -q tests/test_tickreplay_daily_context.py  -> 41 passed, exit 0
uv run pytest -q tests/test_cloud_run_main.py            -> 62 passed, 1 warning, exit 0
uv run pytest -q tests/test_tickreplay_server.py         -> 31 passed, 1 warning, exit 0
Total                                                   -> 134 passed
```

The warnings are the same `StarletteDeprecationWarning` from FastAPI's `TestClient`; they are unrelated to path resolution.

Coverage explains why the suite is green:

- `test_download_stocks_daily_resolves_case_variant` verifies the **remote cloud server** finds a lowercase daily file for an uppercase request (`tests/test_cloud_run_main.py:276-280`). That server behavior works.
- `test_daily_context_accepts_a_lowercase_letter_bearing_stem` lowercases the **request**, but its fixture creates the physical file as uppercase `285A`; the endpoint then uppercases the request and finds the uppercase fixture (`tests/test_tickreplay_server.py:376-405`). It does not test lowercase physical storage.
- `test_authoritative_cache_never_revalidates_existing_local_file` also writes only `stocks_daily/285A.duckdb` (`tests/test_tickreplay_daily_context.py:427-445`). `test_authoritative_missing_file_never_uses_loopback_http` confirms that an authoritative miss returns unavailable without fallback (`tests/test_tickreplay_daily_context.py:332-341`).
- Repository search found no `daily_context` regression test for uppercase request plus lowercase-only physical file. A future test must avoid relying on `WindowsPath` equality/case lookup, as the existing cloud-run test deliberately does (`tests/test_cloud_run_main.py:306-315`).

Relevant git evidence:

- `eb0d8a4393781c6e86fed68e2357a46cea5fab8b` (2026-08-22) added `_stem_case_variants` / `_open_first_existing` to `cloud-run/main.py`. Its commit message explicitly names `285A.duckdb -> 285a.duckdb` and the Windows `PurePath` equality trap; `git blame cloud-run/main.py:229-276` attributes the resolver to this commit.
- `d285db0921cd04340aa82c807917358628aa4dd5` (2026-08-23) introduced the explicit `local_authoritative` policy for the primary cache. Its commit message says existing authoritative files skip HTTP and missing files still use the normal retrieval path.
- `0bdf8682d1e0997f56388f0fdfee28c09a7d14fb` (2026-08-30) created `src/tickreplay/daily_context.py` and its tests. `git blame src/tickreplay/daily_context.py:240-247,424-450` attributes the exact-only daily path and authoritative early return entirely to this commit. The same commit extended the cloud whitelist from numeric daily stems to `[0-9A-Za-z]{4,5}`, so the remote route inherited the old case fallback while the new local daily reader did not.

## Remaining unknowns

- The exact failing production process/OS was not captured. On native Windows, the real configured loader succeeds; the failure is deterministic on a case-sensitive filesystem (such as the documented Linux container). If the user's failing process is native Windows, another runtime state must be involved and the raw `/api/daily-context` response plus private server log is still needed.
- Docker Desktop's Linux engine was unavailable, so the real bind-mounted Linux path could not be exercised locally. The deterministic loader probe reproduces the branch, but a post-fix Linux/container check remains necessary.
- The user-visible error text/HTTP capture was not supplied. The code path predicts HTTP 200 with `{available:false,bars:[]}` and a Daily `unavailable` UI state, not an exception or a cloud-file 404 (`src/tickreplay/server.py:522-545`, `src/tickreplay/static/daily-chart.mjs:164-170`).
- This document establishes the immediate cause and likely introducing commit for Phase 1. It does not select or implement a fix; Phase 2 should compare safe case-variant lookup at the local daily boundary against canonicalizing all dataset filenames, including collision/security and Windows-test semantics.
