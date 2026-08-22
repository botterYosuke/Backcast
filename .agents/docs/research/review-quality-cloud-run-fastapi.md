# Cloud-run FastAPI Migration: Correctness and Quality Review

## Decision

Request changes before describing the Flask-to-FastAPI migration as fully behaviorally compatible.

- High: 0
- Medium: 2
- Low: 3

The focused happy-path contract is preserved, but the worktree regresses several previously supported HTTP preconditions/methods and can emit a false `200` for a non-regular `.duckdb` path. No product, test, configuration, or documentation file was edited by this reviewer.

## Findings

### Medium 1 — The claimed HTTP file-serving contract is not fully preserved

Evidence: `cloud-run/main.py:121` registers only `GET`; the catch-all mount at `cloud-run/main.py:327-332` receives unmatched methods. Conditional/range handling is split between the bespoke regex at `cloud-run/main.py:109-118`, the pre-check at `cloud-run/main.py:138-144`, and `StaticFiles.file_response` at `cloud-run/main.py:146-154`. The tests cover ordinary GET, exact ETag/date validators, single ranges, one out-of-bounds range, and one nonnumeric range (`tests/test_cloud_run_main.py:166-273`), but not the compatibility cases below.

The same temporary 100-byte file was served through the historical `HEAD` Flask app and the current worktree app:

| Request | Flask at `HEAD` | FastAPI worktree | Impact |
| --- | ---: | ---: | --- |
| `HEAD /jp/stocks_trades/7203.duckdb` | `200`, file validators/length | `404`, JSON | HEAD-based probes/download metadata clients break. |
| `GET` + `If-None-Match: *` | `304` | `200`, full body | RFC wildcard cache precondition is ignored. |
| `GET` + stale `If-Match` | `412` | `200`, full body | A previously enforced precondition is ignored. |
| `GET` + `Range: bytes=10-1` | `416` + `Content-Range: bytes */100` | `400` | The stated malformed/unsatisfiable-range contract changes. |
| `GET` + `Range: Bytes=0-9` | `206` | `416` | A case-insensitive byte-range unit accepted by the baseline is rejected by the regex. |
| `GET` + `Range: bytes=0-1,5-6` | `416` | `206 multipart/byteranges` | Multiple-range behavior changes rather than being preserved. |

Exact ETag, Last-Modified, ordinary `If-None-Match`, `If-Modified-Since`, matching/mismatching `If-Range`, single-range `206`, and out-of-bounds `416` do pass. The defect is the broader claim of full compatibility, not those covered cases.

Recommended correction: expose `HEAD` explicitly; implement the full required precondition policy (including wildcard `If-None-Match` and `If-Match`) instead of relying solely on `StaticFiles.is_not_modified`; normalize/validate Range semantics once; and add old-vs-new contract tests for every row above. If the behavior changes are intentional, document them as breaking changes instead of claiming preservation.

### Medium 2 — A whitelisted non-regular path can return `200` with an empty body

Evidence: `cloud-run/main.py:132-136` accepts any successful `Path.stat()` and never checks `stat.S_ISREG`; `cloud-run/main.py:153-154` then calls `StaticFiles.file_response` directly with that stat result, bypassing the normal `StaticFiles` path/type check. With `jp/stocks_trades/7203.duckdb` created as a directory, opening the response raises `PermissionError`, but response headers have already started and the test client observes `200`, `Content-Length: 0`, and an empty body. The old `send_from_directory` path rejected a directory as not found.

This does not silently commit an empty cache in the current tickreplay client because `src/tickreplay/cache.py` verifies the staged file can be opened by DuckDB before commit, but it still converts a server-side invalid object into false success and unnecessary corruption/retry handling.

Recommended correction: require `stat.S_ISREG(stat_result.st_mode)` before constructing the response and return `404` for every non-regular object. Add a regression test beside the missing-file tests (`tests/test_cloud_run_main.py:175-191`).

## Lower-priority Findings

- **Low — test gap:** `tests/test_cloud_run_main.py:166-273` does not cover HEAD, wildcard/precondition variants, descending/case-variant/multiple ranges, or non-regular paths, allowing both Medium regressions through 22 passing tests.
- **Low — deployment drift:** `cloud-run/Dockerfile:17` uses the deprecated `uvicorn.workers.UvicornWorker`; installed Uvicorn 0.52.4 explicitly deprecates that module in favor of `uvicorn-worker`. Root `pyproject.toml:7-12,78-85` does not install Gunicorn, so root tests cannot import/smoke the actual worker, while `cloud-run/requirements.txt:1-7` uses open-ended minimums rather than the root lock. Docker's build check also reports the shell-form CMD warning. This is a maintainability/reproducibility risk, not a current runtime failure; `exec` does preserve PID 1 signal forwarding.
- **Low — formatting gate:** `cloud-run/.env.example:8-9,11` contains trailing whitespace, so scoped `git diff --check` fails. `python-dotenv` parses the spaced values correctly, making this formatting/gate debt rather than a behavior defect.

## Assertion-Replacement Verdict

All nine assertion deletions flagged by the heuristic are behaviorally replaced; test coverage was not weakened by those deletions.

| Deleted Flask assertion form | FastAPI replacement | Verdict |
| --- | --- | --- |
| Sorted stems `body == ...` | Same equality at `tests/test_cloud_run_main.py:98` | Equivalent |
| Empty listing `response.get_json()` | `response.json()` at `tests/test_cloud_run_main.py:115` | Equivalent |
| Missing directory error JSON | `response.json()` at `tests/test_cloud_run_main.py:122` | Equivalent |
| Scandir failure error JSON | `response.json()` at `tests/test_cloud_run_main.py:136` | Equivalent |
| Entry-stat failure error JSON | `response.json()` at `tests/test_cloud_run_main.py:160` | Equivalent |
| Board file `response.data` | `response.content` at `tests/test_cloud_run_main.py:191` | Equivalent |
| Single range `response.data` | `response.content` at `tests/test_cloud_run_main.py:237` | Equivalent |
| Matching If-Range `response.data` | `response.content` at `tests/test_cloud_run_main.py:250` | Equivalent |
| Mismatching If-Range full body `response.data` | `response.content` at `tests/test_cloud_run_main.py:261` | Equivalent |

The new health and mounted tickreplay assertions at `tests/test_cloud_run_main.py:85-89,279-297` add coverage; they do not substitute for the missing HTTP edge cases above.

## Verified Compatibility and Structure

- ASGI route order is correct for GET paths: health, listing, file downloads, and GraphQL are registered before the `/` mount (`cloud-run/main.py:69-154,324-332`).
- Lifespan ownership is intentionally composed at `cloud-run/main.py:57-66`; the mounted sub-app's lifespan would not otherwise run, and the referenced tickreplay lifespan uses acquire/release ownership. The mounted `/api/symbols` test proves repository construction (`tests/test_cloud_run_main.py:287-297`).
- FileResponse streams via an async file context and closes it; no persistent descriptor leak was reproduced for regular-file GET/range responses.
- GraphQL changed adapters only (`cloud-run/main.py:324-325`); the ranking parser, SQL, ordering, DuckDB reads, and result mapping at `cloud-run/main.py:163-321` are unchanged from `HEAD`. Basic POST GraphQL introspection and GET GraphiQL both returned `200`.
- Docker's documented root build context matches `COPY src/ /app/src/` and the import path calculation (`cloud-run/Dockerfile:6-12`; `cloud-run/main.py:47-54`). One worker is consistent with process-local repository/tracker state (`cloud-run/Dockerfile:14-17`).
- `uv.lock` is consistent with `pyproject.toml`; removal of Flask/Werkzeug-only packages follows the dependency change.

## Minimal Reproduction

Current-worktree regression matrix (PowerShell):

```powershell
@'
import os, pathlib, sys, tempfile
from fastapi.testclient import TestClient

with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
    root = pathlib.Path(tmp)
    path = root / "jp/stocks_trades/7203.duckdb"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"0123456789" * 10)
    os.environ["STOCKDATA_CACHE_DIR"] = str(root)
    os.environ["BACKCAST_DUCKDB_CACHE_DIR"] = str(root / "jp")
    sys.path.insert(0, "cloud-run")
    import main
    client = TestClient(main.app, raise_server_exceptions=False)
    cases = {
        "HEAD": client.head("/jp/stocks_trades/7203.duckdb"),
        "If-None-Match:*": client.get("/jp/stocks_trades/7203.duckdb", headers={"If-None-Match": "*"}),
        "stale If-Match": client.get("/jp/stocks_trades/7203.duckdb", headers={"If-Match": '"stale"'}),
        "descending": client.get("/jp/stocks_trades/7203.duckdb", headers={"Range": "bytes=10-1"}),
        "upper unit": client.get("/jp/stocks_trades/7203.duckdb", headers={"Range": "Bytes=0-9"}),
        "multiple": client.get("/jp/stocks_trades/7203.duckdb", headers={"Range": "bytes=0-1,5-6"}),
    }
    print({name: response.status_code for name, response in cases.items()})
    path.unlink()
    path.mkdir()
    response = client.get("/jp/stocks_trades/7203.duckdb")
    print("directory", response.status_code, len(response.content))
    client.close()
'@ | uv run python -
```

Observed: `{'HEAD': 404, 'If-None-Match:*': 200, 'stale If-Match': 200, 'descending': 400, 'upper unit': 416, 'multiple': 206}` and `directory 200 0`. The historical Flask baseline was executed from `git show HEAD:cloud-run/main.py` against the same 100-byte fixture and produced `200, 304, 412, 416, 206, 416`, respectively.

## Codex Consultation

Required consultation was attempted only through the shared wrapper in read-only mode.

1. First result: wrapper exit `0`, but unusable. Codex received only the first line (`# Objective`) on this Windows invocation and returned a 36-character prompt-for-more-input response. Artifacts: `.agents/logs/codex/20260822T001415Z-quality-review-cloud-run-fastapi.prompt.md`, `.agents/logs/codex/20260822T001415Z-quality-review-cloud-run-fastapi.md`, and its stderr log.
2. One permitted retry: the first prompt line was made self-contained to avoid line truncation. The wrapper produced no response for several minutes and was interrupted on the parent agent's prompt-finalization instruction. `.agents/logs/codex/20260822T001507Z-quality-review-cloud-run-fastapi.md` is zero bytes. No usable Codex insight was accepted, and there was no third attempt.

Every finding above is therefore based on independent source inspection and reproduced behavior, not Codex output.

## Checks Run

- `uv run pytest tests/test_cloud_run_main.py -q` — 22 passed; one FastAPI TestClient deprecation warning.
- `uv run ruff check cloud-run/main.py tests/test_cloud_run_main.py` — passed.
- `uv run ruff format --check cloud-run/main.py tests/test_cloud_run_main.py` — passed.
- `uv lock --check` — passed, 39 packages resolved.
- `docker build --check -f cloud-run/Dockerfile .` — completed with shell-form CMD warning.
- `git diff --check -- <eight scoped files>` — failed only at `cloud-run/.env.example:8-9,11` trailing whitespace.
- Historical Flask/current FastAPI response matrices — reproduced the status/header differences listed above.
- GraphQL `POST /graphql` introspection and `GET /graphql` GraphiQL smoke — both `200`.
