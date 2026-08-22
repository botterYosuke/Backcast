# Cloud-run FastAPI migration: independent test coverage review

Date: 2026-08-22

## Verdict

The focused suite is green, and the nine guardrail-reported removed assertions are equivalent FastAPI/httpx replacements rather than weakened checks. However, the migration is not ready to be called HTTP-contract complete: two untested legacy behaviors are confirmed regressions (`HEAD` and reversed byte ranges), and the single-worker deployment invariant is not pinned by a test.

Gap counts: **High 3 / Medium 3 / Low 2**.

## Independent execution

| Check | Result |
| --- | --- |
| Focused pytest | 40 passed, 0 failed, 0 errors; 1 warning; 7.48 s |
| Coverage pytest | 40 passed, 0 failed, 0 errors; 1 warning; 5.00 s |
| Warning | `StarletteDeprecationWarning`: FastAPI's compatibility `TestClient` import says the `httpx` integration is deprecated and recommends `httpx2` |

Coverage was measured reliably despite the dynamic `main` import by selecting the `cloud-run` source directory:

| Source | Statements | Missed | Branches | Partial | Coverage |
| --- | ---: | ---: | ---: | ---: | ---: |
| `cloud-run/main.py` | 130 | 44 | 16 | 1 | **62%** |
| `src/tickreplay/server.py` | 223 | 19 | 40 | 5 | **91%** |
| Combined | 353 | 63 | 56 | 6 | **81%** |

`cloud-run/main.py` remains below the repository's 80% per-feature target. Its uncovered lines are 159-160, 194-214, 242-311, and 336-338.

## Preserved-contract coverage

- Covered: 200 download metadata (`ETag`, `Last-Modified`, `Accept-Ranges`) at `tests/test_cloud_run_main.py:166`; 304 via matching `If-None-Match` at line 197 and `If-Modified-Since` at line 208; stale `If-None-Match` -> 200 at line 220.
- Covered: ordinary 206 with body and `Content-Range` at line 231; matching and mismatching ETag `If-Range` behavior at lines 241 and 254.
- Partially covered: 416 tests include an out-of-bounds range and a non-numeric range only (`tests/test_cloud_run_main.py:265`).
- Covered: missing/unknown file, disallowed extension, and a positive whitelist case at lines 175-195.
- Covered: `/healthz` at line 85 and root delegation to the tick-replay UI at line 279.
- Covered: the mounted app's lifespan is proven by a repository-dependent `/api/symbols` request at line 287. Nested ownership and sequential re-entry are independently exercised against the dependency at `tests/test_tickreplay_server.py:106-140`.
- Not exercised: HEAD, reversed ranges, suffix/open/multiple ranges, date-based `If-Range`, encoded traversal, GraphQL requests, and the Docker single-worker command.

## High gaps

### H1. Download HEAD contract is untested and regressed

The file route is registered only as `@app.get` (`cloud-run/main.py:121`), and the download test uses only GET (`tests/test_cloud_run_main.py:166-172`). An independent probe returned **404** for `HEAD /jp/stocks_trades/7203.duckdb`; the equivalent legacy Flask route returned **200** with an empty body. HEAD is relevant for metadata/cache clients and was implicitly provided by the pre-migration Flask GET route.

Required regression test: assert HEAD returns 200, no response body, and the same key metadata headers as GET.

### H2. A reversed byte range bypasses validation and changes 416 to 400

`_VALID_RANGE_RE` accepts any numeric `first-last` pair without verifying `first <= last` (`cloud-run/main.py:118-139`). The only malformed value under test is `bytes=abc-def` (`tests/test_cloud_run_main.py:265-273`). An independent probe of `Range: bytes=99-0` returned **400** in the FastAPI implementation; the legacy Flask/Werkzeug behavior returned **416** with `Content-Range: bytes */100`.

Required regression test: include reversed/zero-invalid numeric ranges and assert 416 plus the unsatisfied `Content-Range` header, not just the status for two representative values.

### H3. The single-worker data/state invariant is configuration-only

The production command correctly pins `--workers 1` and explains the process-local repository/tracker constraint (`cloud-run/Dockerfile:14-17`), but neither focused test file inspects the Docker command or starts the deployment configuration. A future worker-count change would split operation IDs and repository/cache coordination across processes without a failing gate.

Required regression gate: a static Dockerfile command assertion at minimum; preferably a container smoke check that confirms the ASGI worker class and one worker.

## Lower-priority gaps

- **Medium — range variants:** suffix, open-ended, multiple ranges, and date-based `If-Range` are absent from `tests/test_cloud_run_main.py:231-273`; exploratory probes currently pass (206/200 as appropriate), so these are unpinned rather than known regressions.
- **Medium — GraphQL migration:** the Flask-to-Strawberry FastAPI route and ranking resolver at `cloud-run/main.py:230-325` receive no request-level test; coverage misses the resolver body at lines 242-311.
- **Medium — traversal hardening:** whitelist tests cover an unknown directory and extension (`tests/test_cloud_run_main.py:175-185`) but no encoded `..`, encoded slash/backslash, or symlink-shaped case; encoded dot/backslash exploratory probes currently return 404.
- **Low — environment isolation:** `cloud-run/main.py:40-44` mutates two environment variables with `setdefault`, while the fresh-import fixture only owns `STOCKDATA_CACHE_DIR` (`tests/test_cloud_run_main.py:26-50`); defaults/explicit override precedence and cross-test cleanup are not asserted.
- **Low — generic 500 mapping:** the FastAPI exception handler at `cloud-run/main.py:158-161` is uncovered, so its plain-text 500/no-detail contract is not pinned.

## Removed-assertion verdict

**Retained; no weakening found.** All nine removed assertion lines have semantic replacements:

- one sorted-stem assertion is moved unchanged;
- four Flask `response.get_json()` assertions become httpx `response.json()` assertions with identical expected values;
- four Flask `response.data` byte assertions become httpx `response.content` assertions with identical expected bytes.

The migration also adds explicit assertions for `/healthz`, the delegated root UI, and the repository-dependent mounted endpoint. The guardrail count is therefore an API-adapter false positive, not evidence of deleted coverage. It does not negate the independent High gaps above.

## Commands

```text
uv run --group cloud-run-test pytest tests/test_cloud_run_main.py tests/test_tickreplay_server.py -q
uv run --with pytest-cov --group cloud-run-test pytest tests/test_cloud_run_main.py tests/test_tickreplay_server.py -q --cov=cloud-run --cov=tickreplay.server --cov-branch --cov-report=term-missing
<PowerShell here-string> | uv run --group cloud-run-test python -          # current ASGI contract probe
<PowerShell here-string> | uv run --with 'flask>=3.0' python -            # equivalent legacy Flask probe
git diff --unified=80 -- tests/test_cloud_run_main.py
git show HEAD:tests/test_cloud_run_main.py
rg -n <coverage patterns> tests/test_cloud_run_main.py tests/test_tickreplay_server.py cloud-run/main.py cloud-run/Dockerfile
```
