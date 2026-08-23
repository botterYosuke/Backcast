## Bug Report: Existing 285A cache enters a download operation

### Error
- Message: No exception was emitted. After `GET /api/symbols/285A`, the UI entered a download/pending operation and repeatedly polled `GET /api/status?stem=285A`. A later pure status read from the same running process confirmed `bytesReceived=totalBytes=3881840640`, so the incident did transfer the complete DuckDB despite the existing-cache claim.
- Location: `src/tickreplay/repository.py:418-440` decides whether the first per-process access needs revalidation; `src/tickreplay/cache.py:274-335` decides whether that revalidation is a 304 or a full 200 transfer.
- Stack trace: Not applicable. The supplied Uvicorn access log contains successful 200 responses and no Python traceback.

### Reproduction
- Steps:
  1. Configure `BACKCAST_DUCKDB_CACHE_DIR=/cache` on Windows and leave `BACKCAST_DUCKDB_LOCAL_AUTHORITATIVE` absent or set it to `false`.
  2. Start a fresh application process so the repository `_revalidated` set is empty.
  3. Select or auto-load symbol 285A through `/api/symbols/285A`.
  4. Observe the pending response and `/api/status?stem=285A` polling.
  5. If the exact expected live file has no valid matching sidecar, observe a 200/full transfer; with a valid matching sidecar, the operation should instead complete after a 304 with no body transfer.
- Reproducibility: The operation/polling route is deterministic on the first access of each process in normal-cache mode. The incident's full body is confirmed by its retained operation snapshot, but reproducing the same 200 trigger depends on the pre-operation file path, sidecar, remote validator, and DuckDB integrity, which were not preserved.

### Immediate Context
- Failing code: This is primarily a policy/observability surprise, not a failing exception. With `local_authoritative=False`, `needs_download` returns true for an existing file until that stem has been revalidated in the current process (`src/tickreplay/repository.py:418-440`). A sidecar is the only source of conditional validators; missing or invalid sidecar data produces no conditional headers (`src/tickreplay/cache.py:274-283`).
- Call chain: browser `loadSymbolInfo` -> `GET /api/symbols/285A` -> server `_ensure_ready_or_pending` -> `TickRepository.needs_download` -> background operation -> `_ensure_fresh_locked` -> `_download_and_commit(conditional=True)` -> `RemoteDuckDBCache.stage_download` -> status polling -> retry of `/api/symbols/285A`.
- Recent changes: The server-backed DuckDB cache was introduced in commit `bab0473`; current HEAD is `d285db0`. Phase 2 will determine the specific history of the `local_authoritative` policy and path layout.

### Affected Area
- Files involved: `src/tickreplay/config.py`, `src/tickreplay/cache.py`, `src/tickreplay/repository.py`, `src/tickreplay/server.py`, `src/tickreplay/static/app.js`, and `src/tickreplay/static/request-coordinator.mjs`.
- Related tests: 24 focused cases passed on Windows/Python 3.13.11, covering config parsing, authoritative existing/missing/corrupt files, per-process normal-cache revalidation, no-sidecar conditional behavior, validator identity safety, and stale fallback. The skill-bundled repro wrapper could not run the command because this host lacks WSL `/bin/bash`; the same tests were independently executed directly with `uv run pytest`.

### Initial Hypotheses (informed by Codex analysis)
1. Normal-cache first-access revalidation: `local_authoritative=false` intentionally does not skip an existing file based on existence alone, so the download operation starts -- Codex confidence: high for operation entry.
2. Missing, unreadable, invalid, or validator-less sidecar: the nominal conditional request carries no validator and receives a full 200 response -- Codex confidence: high and the leading explanation for transferred bytes.
3. Path mismatch: the user may have inspected an older root-level file rather than `C:\cache\stocks_trades\285A.duckdb`, or the expected file was otherwise absent -- Codex confidence: medium-high.
4. Changed remote validator: a valid sidecar existed but its ETag or Last-Modified no longer matched the server object -- Codex confidence: medium.
5. Corrupt local DuckDB: query/open failure triggered the single unconditional redownload-and-retry path -- Codex confidence: low.

### Codex Pattern Recognition
- Error pattern: A two-stage cache-policy/observability issue. Entering a background operation is not equivalent to transferring the file body; the UI uses the same pending/downloading path for conditional 304 checks and full 200 downloads.
- Known similar patterns: Sidecar-dependent conditional caches fall back to unconditional body transfer when metadata is absent or unusable; in-memory once-per-process revalidation state resets on every application restart.
- Recommended investigation priority: First confirm the exact pre-operation path and sidecar state, then compare validators and server response status/byte count, and only then investigate DuckDB corruption. Those pre-operation artifacts were replaced in this incident, so history and tests must bound the conclusion rather than claim a unique cause.
