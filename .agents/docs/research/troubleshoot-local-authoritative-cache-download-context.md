# DuckDB Existing-Cache Download Investigation Context

## Observed Configuration

- The checked workspace `.env` contains `BACKCAST_DUCKDB_CACHE_DIR= /cache` at
  `.env:13` and `BACKCAST_DUCKDB_SERVER_URL= http://backcast.i234.me:8080` at
  `.env:16`. It contains no `BACKCAST_DUCKDB_LOCAL_AUTHORITATIVE` entry. No
  `BACKCAST_DUCKDB_*` process-environment variables were visible in the
  investigation shell. Therefore the checked configuration resolves
  `local_authoritative` to its default `False`; the parser selects an explicit
  process value first, then `.env`, and otherwise assigns `False`
  (`src/tickreplay/config.py:117-125`, `127-137`, `149-163`). Explicit string
  `false` would produce the same result (`src/tickreplay/config.py:66-74`).
- On this Windows host, `Path("/cache").resolve(strict=False)` is `C:\cache`.
  `TickRepository` then normalizes the cache root with
  `expanduser().resolve(strict=False)` (`src/tickreplay/repository.py:223-232`,
  `265-279`). The live file location is not the cache root itself; it is
  `<cache_dir>/stocks_trades/<stem>.duckdb`
  (`src/tickreplay/cache.py:84-102`). Thus the exact expected 285A location is
  `C:\cache\stocks_trades\285A.duckdb`.
- At investigation time that live file exists and is 3,881,840,640 bytes. Its
  creation time is `2026-08-23T10:10:40.2148615+09:00` and last-write time is
  `2026-08-23T10:11:53.3884863+09:00`. The matching
  `285A.duckdb.sidecar.json` exists, is 192 bytes, and was created/last-written
  at `2026-08-23T10:11:55.4725643+09:00`. A recursive read-only search below
  `C:\cache` found no other `285A*` object. These timestamps show the currently
  visible file and sidecar were created/committed during the observed transfer;
  they do not prove where, or in what sidecar state, the user's earlier file
  existed.
- The still-running process retained the terminal operation snapshot for 285A:
  `state=fresh`, `operationId=1`, `revision=3704`,
  `bytesReceived=3881840640`, `totalBytes=3881840640`, and `error=null`.
  Because `/api/status` is a pure in-memory read, this confirms the incident
  operation transferred the complete 3,881,840,640-byte response rather than
  ending as a zero-body 304.

## Execution Flow

1. FastAPI lifespan startup resolves the config and constructs one repository,
   passing `config.local_authoritative` directly
   (`src/tickreplay/server.py:191-211`). The repository starts with an empty
   per-process `_revalidated` set (`src/tickreplay/repository.py:287-291`).
2. The browser first calls `/api/symbols`; that endpoint asks the repository for
   its symbol listing (`src/tickreplay/server.py:397-407`). The repository tries
   `/api/stocks-trades` and persists the returned listing, falling back to the
   persisted/local list only when the server cannot answer
   (`src/tickreplay/repository.py:359-387`). This can explain listing I/O but is
   not the multi-GB per-symbol file request.
3. Selecting 285A makes `loadSymbolInfo` call
   `/api/symbols/285A` through `fetchUntilReady`
   (`src/tickreplay/static/app.js:1414-1425`). The server runs
   `_ensure_ready_or_pending`; after confirming the symbol from the listing, it
   calls `repository.needs_download` and starts a background operation when it
   returns true (`src/tickreplay/server.py:360-380`, `410-421`).
4. With `local_authoritative=False`, an existing file still makes
   `needs_download` return true on the first access of each process because
   285A is not yet in `_revalidated`
   (`src/tickreplay/repository.py:418-440`). Under the symbol lock, the source
   of truth calls `_download_and_commit(..., conditional=True)` for that state
   and then records the stem as revalidated (`src/tickreplay/repository.py:463-490`).
5. `conditional=True` uses the sidecar's ETag or Last-Modified when a readable
   sidecar exists. A matching server validator returns 304 and writes no part
   file; a missing/invalid sidecar yields no conditional headers, and a 200
   response streams the entire body (`src/tickreplay/cache.py:233-283`,
   `286-335`). Therefore the UI/log can enter its “downloading” pending route
   even for a cheap 304 revalidation, while a missing sidecar makes the same
   route an actual full transfer.
6. The `pending` response causes `RequestCoordinator` to poll
   `/api/status?stem=285A` until the operation reaches a terminal state, then it
   reissues `/api/symbols/285A`
   (`src/tickreplay/static/request-coordinator.mjs:139-157`, `220-246`). The
   repeated status requests in the supplied log are therefore a consequence,
   not a cause, of the download decision. `/api/status` itself is a pure
   in-memory snapshot and performs no repository or network I/O
   (`src/tickreplay/server.py:383-394`).

## Immediate Cause

`BACKCAST_DUCKDB_LOCAL_AUTHORITATIVE=false` is sufficient to explain why an
existing file enters the background revalidation/download route on the first
285A access after each application process start. In the checked workspace the
variable is actually absent, but absence defaults to the same `False` value.
The policy is intentional: the normal cache is treated as a copy of a remote
origin, so existence alone is not evidence of freshness.

It is not, by itself, sufficient to prove why the now-confirmed full body was
transferred. A first access with a valid matching sidecar should still start
the operation but end with HTTP 304 and no body transfer. A full-body transfer
follows if any of these conditions holds:

- the file is absent from the exact expected
  `C:\cache\stocks_trades\285A.duckdb` path, which invokes
  `conditional=False` (`src/tickreplay/repository.py:481-487`);
- the file exists but the sidecar is absent, unreadable, or invalid, so the
  nominal conditional request carries no validator
  (`src/tickreplay/cache.py:274-283`);
- the sidecar validator no longer matches the server object, so the server
  correctly responds 200 with a replacement body; or
- DuckDB cannot open/query the existing file, which forces exactly one
  unconditional redownload and retry (`src/tickreplay/repository.py:538-549`).

The current 285A sidecar was created only after the current live file and both
timestamps coincide with the observed operation. The pre-operation sidecar
state is no longer available, so the evidence supports, but cannot uniquely
distinguish, “missing/wrong-path file”, “missing/invalid sidecar”, “changed
remote validator”, and “corrupt DuckDB”.

## Existing-File Skip Routes

- `local_authoritative=True`: if the exact live file exists,
  `needs_download` is false and `_ensure_fresh_locked` skips HTTP revalidation
  entirely (`src/tickreplay/repository.py:434-440`, `463-488`). This is intended
  only when the cache directory is the authoritative tree served by the file
  server itself, not as a general “trust any cache forever” switch
  (`src/tickreplay/config.py:83-98`). Missing files and corrupt DuckDB files
  still download unconditionally in this mode.
- Same-process reuse in normal mode: after the first revalidation attempt,
  `_revalidated.add(stem)` makes later accesses in that process skip the
  network (`src/tickreplay/repository.py:486-490`). Restarting the application
  resets the set and revalidates once again.
- Conditional 304 in normal mode: an existing file plus a valid matching
  sidecar avoids body download, but does not avoid the HTTP request or the UI's
  pending/status path (`src/tickreplay/cache.py:243-246`, `306-320`).
- Offline stale fallback: if conditional revalidation cannot reach the server
  and the live file still exists, the repository serves it as stale rather
  than failing the read (`src/tickreplay/repository.py:510-522`). This is a
  fallback after attempted network I/O, not an existence-only skip.

## Tests and Evidence

- Config default/accepted values are covered at
  `tests/test_tickreplay_config.py:144-199`.
- Conditional headers, 304 behavior, corrupt sidecar handling, and the
  no-sidecar/full-200 path are covered at
  `tests/test_tickreplay_cache.py:92-165`.
- The authoritative existing-file skip is covered at
  `tests/test_tickreplay_repository.py:1026-1068`; missing and corrupt-file
  exceptions are covered at `tests/test_tickreplay_repository.py:1135-1210`.
  Normal caches revalidating again after a process/repository restart are
  covered at `tests/test_tickreplay_repository.py:1213-1236`.
- The pending/status handshake and stale fallback are covered at
  `tests/test_tickreplay_server.py:405-532`.
- Safe focused validation used only local temporary files and
  `httpx.MockTransport` (no real DuckDB server):

  ```text
  uv run pytest tests/test_tickreplay_config.py tests/test_tickreplay_cache.py \
    tests/test_tickreplay_repository.py tests/test_tickreplay_server.py \
    -k "local_authoritative or conditional_get_with_no_prior_sidecar or \
    remote_cache_does_not_infer_identity_from_size_and_http_date or \
    remote_cache_still_revalidates_every_process_start or \
    failed_revalidation_serves_existing_cache_as_stale_once_per_process" -q
  Result: 24 passed, 77 deselected, 1 deprecation warning.
  ```

## Remaining Unknowns

- The supplied Uvicorn access log does not include the background HTTP client
  request, response status, or validator headers. The retained `/api/status`
  snapshot confirms a full 3,881,840,640-byte transfer, but neither source
  identifies why the file server returned a body rather than 304.
- The exact path, size, mtime, and sidecar state of the user's claimed
  pre-existing 285A file were not captured before the operation committed the
  current file. An existing file outside
  `C:\cache\stocks_trades\285A.duckdb` is invisible to this repository.
- The already-running process may have inherited an environment different from
  the investigation shell. The checked `.env` has no local-authoritative key;
  confirming the launched process's resolved config would require startup
  diagnostics or process-environment capture, neither of which was mutated or
  added during this read-only investigation.
