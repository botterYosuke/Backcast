"""FastAPI application serving the tick replay UI and its data endpoints.

Startup is fail-fast (Step 7 of
``.agents/docs/plans/duckdb-server-cache.md``): the ``lifespan`` hook
resolves config and constructs the repository *before* the app finishes
starting, runs Step 4/5's orphan ``.part``/``sidecar.tmp`` recovery once, and
closes the repository/HTTP client on shutdown. A bad config now means the
process never finishes starting — there is no more lazy, first-request
construction and no soft ``{"ok": false}`` degradation for that case.

``/api/status`` is a pure in-memory snapshot of one stem's download progress
(Step 1's ``serverEpoch``/``operationId``/``revision``/state scheme) — it
never constructs the repository, performs network I/O, or globs a directory.
A request that needs a download (``/api/session``, ``/api/symbols/{stem}``)
starts (or reuses) a background operation and returns immediately with
``{"pending": true, "operationId": ...}`` rather than blocking a request
thread on a multi-GB transfer; the client polls ``/api/status`` and re-issues
the original request once the operation's state is ``"fresh"`` (Step 7's
operation-start handshake, consumed by Step 8's frontend polling).
"""

from __future__ import annotations

import mimetypes
import threading
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from datetime import date as Date
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import cache, cache_commit, minute_context
from .config import build_http_client, resolve_cache_config
from .repository import (
    SymbolAvailabilityUnknownError,
    SymbolNotFoundError,
    SymbolUnavailableError,
    TickRepository,
)

STATIC_DIR = Path(__file__).resolve().parent / "static"


# --------------------------------------------------------------------------
# Operation tracking (Step 1's serverEpoch/operationId/revision scheme)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class StemSnapshot:
    """`/api/status`'s response for one stem — a pure in-memory snapshot."""

    state: str  # "missing" | "downloading" | "fresh" | "stale-served" | "corrupt"
    operation_id: int | None
    revision: int
    bytes_received: int | None = None
    total_bytes: int | None = None
    error: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "state": self.state,
            "operationId": self.operation_id,
            "revision": self.revision,
            "bytesReceived": self.bytes_received,
            "totalBytes": self.total_bytes,
            "error": self.error,
        }


_MISSING_SNAPSHOT = StemSnapshot(state="missing", operation_id=None, revision=0)


@dataclass(frozen=True)
class OperationOutcome:
    """Terminal state returned by a successful background operation."""

    state: str
    error: str | None = None


class OperationTracker:
    """Tracks in-flight/completed downloads for `/api/status` to poll.

    ``server_epoch`` is generated once per process; the client resets its
    own last-seen state whenever it sees a different epoch (Step 1/Step 8) —
    that is how it detects a server restart rather than misreading a lower
    ``operationId`` from a fresh process as merely "stale".
    """

    def __init__(self) -> None:
        self.server_epoch = uuid.uuid4().hex
        self._lock = threading.Lock()
        self._next_operation_id = 1
        self._snapshots: dict[str, StemSnapshot] = {}
        self._inflight: set[str] = set()

    def snapshot(self, stem: str) -> StemSnapshot:
        with self._lock:
            return self._snapshots.get(stem, _MISSING_SNAPSHOT)

    def operation_id_if_inflight(self, stem: str) -> int | None:
        """The id of ``stem``'s in-flight operation, if any — checked
        *before* the repository's own (lock-acquiring) existence check, so
        a request arriving while another is already downloading this stem
        never blocks behind that download's `symbol_lock` just to learn
        the operationId it should reuse.
        """
        with self._lock:
            if stem not in self._inflight:
                return None
            existing = self._snapshots[stem]
            assert existing.operation_id is not None
            return existing.operation_id

    def start_if_needed(self, stem: str, run: Callable[[int], OperationOutcome]) -> int:
        """Start a new background operation for ``stem``, or return the id
        of one already in flight. ``run(operation_id)`` executes on a
        background thread and returns its terminal outcome; exceptions are
        caught and recorded as "corrupt" — callers never see them directly.
        """
        with self._lock:
            if stem in self._inflight:
                existing = self._snapshots[stem]
                assert existing.operation_id is not None
                return existing.operation_id
            operation_id = self._next_operation_id
            self._next_operation_id += 1
            self._inflight.add(stem)
            self._snapshots[stem] = StemSnapshot(
                state="downloading", operation_id=operation_id, revision=0
            )

        def worker() -> None:
            try:
                outcome = run(operation_id)
            except Exception as error:  # noqa: BLE001 - reported via snapshot, not raised
                self._finish(stem, operation_id, state="corrupt", error=str(error))
            else:
                self._finish(
                    stem, operation_id, state=outcome.state, error=outcome.error
                )

        threading.Thread(target=worker, daemon=True).start()
        return operation_id

    def report_progress(
        self, stem: str, operation_id: int, bytes_received: int, total_bytes: int | None
    ) -> None:
        with self._lock:
            current = self._snapshots.get(stem)
            if current is None or current.operation_id != operation_id:
                return  # a stale callback from a superseded operation
            self._snapshots[stem] = replace(
                current,
                revision=current.revision + 1,
                bytes_received=bytes_received,
                total_bytes=total_bytes,
            )

    def _finish(
        self, stem: str, operation_id: int, *, state: str, error: str | None = None
    ) -> None:
        with self._lock:
            current = self._snapshots.get(stem)
            if current is None or current.operation_id != operation_id:
                return
            self._snapshots[stem] = replace(
                current, state=state, revision=current.revision + 1, error=error
            )
            self._inflight.discard(stem)


# --------------------------------------------------------------------------
# Repository singleton (lock-guarded, constructed once — normally by
# `lifespan`, at process startup, for fail-fast)
# --------------------------------------------------------------------------

_construction_lock = threading.Lock()
_repository: TickRepository | None = None
_lifespan_users = 0
_tracker = OperationTracker()


def _acquire_repository_for_lifespan() -> TickRepository:
    """Build or share the repository for one active lifespan entry.

    Construction and startup reconciliation stay under one lock so a nested
    or concurrent lifespan cannot observe a half-initialized singleton. A
    reference count prevents one lifespan exit from closing the instance
    while another re-entrant lifespan still owns it.
    """
    global _lifespan_users, _repository
    with _construction_lock:
        if _repository is None:
            config = resolve_cache_config()
            repository = TickRepository(
                cache_dir=config.cache_dir,
                server_url=config.server_url,
                http_client=build_http_client(config),
            )
            try:
                cache.discard_orphaned_part_files(repository.cache_dir)
                cache_commit.reconcile_all_at_startup(repository.cache_dir, repository)
            except BaseException:
                repository.close()
                repository.http_client.close()
                raise
            _repository = repository
        _lifespan_users += 1
        return _repository


def _release_repository_from_lifespan(repository: TickRepository) -> None:
    """Release one lifespan owner and close the last owner's repository."""
    global _lifespan_users, _repository
    to_close: TickRepository | None = None
    with _construction_lock:
        if _repository is not repository or _lifespan_users <= 0:
            raise RuntimeError("lifespan repository ownership is inconsistent")
        _lifespan_users -= 1
        if _lifespan_users == 0:
            _repository = None
            to_close = repository

    if to_close is not None:
        try:
            to_close.close()
        finally:
            to_close.http_client.close()


def get_repository() -> TickRepository:
    """Return the repository owned by the currently active lifespan.

    This accessor never constructs. Before startup or after shutdown it fails
    explicitly, which prevents status probes or test-harness reuse from
    resurrecting a closed repository outside FastAPI's lifecycle.
    """
    with _construction_lock:
        if _repository is None:
            raise RuntimeError("repository is unavailable outside the app lifespan")
        return _repository


def get_tracker() -> OperationTracker:
    """Return the process tracker without constructing the repository."""
    with _construction_lock:
        return _tracker


def reset_for_tests() -> None:
    """Test-only: close detached state and reset the in-memory tracker."""
    global _repository, _tracker
    to_close: TickRepository | None = None
    with _construction_lock:
        if _lifespan_users:
            raise RuntimeError("cannot reset while an app lifespan is active")
        to_close = _repository
        _repository = None
        _tracker = OperationTracker()
    if to_close is not None:
        try:
            to_close.close()
        finally:
            to_close.http_client.close()


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    # Fail-fast: an invalid config raises here, before the app finishes
    # starting — no request is ever served against a broken configuration.
    repository = _acquire_repository_for_lifespan()
    try:
        yield
    finally:
        _release_repository_from_lifespan(repository)


app = FastAPI(title="歩み値リプレイ", version="0.1.0", lifespan=lifespan)
# Sessions are columnar JSON of up to ~100k executions; gzip cuts the transfer
# to roughly a quarter without any client-side work.
app.add_middleware(GZipMiddleware, minimum_size=2048)


def _parse_date(value: str) -> Date:
    try:
        return Date.fromisoformat(value)
    except ValueError as error:
        raise HTTPException(
            status_code=400, detail=f"invalid date: {value!r} (expected YYYY-MM-DD)"
        ) from error


def _reraise_as_http(error: Exception) -> None:
    """Map a repository lookup/availability error to its HTTP status.

    404 only for an authoritatively-confirmed absence; 503 for "existence
    indeterminate" or "unavailable" — never 404 purely from a local/network
    failure to check (Step 1's existence-vs-availability policy).
    """
    if isinstance(error, SymbolNotFoundError):
        raise HTTPException(status_code=404, detail=str(error)) from error
    if isinstance(error, SymbolAvailabilityUnknownError | SymbolUnavailableError):
        raise HTTPException(status_code=503, detail=str(error)) from error
    raise error


def _confirm_known_or_http(repository: TickRepository, symbol: str) -> None:
    """Synchronously confirm ``symbol`` exists (a fast listing check, never
    a multi-GB transfer) before deciding whether the file itself needs the
    async pending/``operationId`` path — an unknown or unavailable symbol
    must still get an immediate 404/503, not a misleading "pending"."""
    try:
        repository.confirm_known(symbol)
    except (
        SymbolNotFoundError,
        SymbolAvailabilityUnknownError,
        SymbolUnavailableError,
    ) as error:
        _reraise_as_http(error)


def _start_download(
    repository: TickRepository, tracker: OperationTracker, stem: str
) -> int:
    def run(operation_id: int) -> OperationOutcome:
        with cache.observe_download() as observation:
            repository.ensure_fresh(
                stem,
                progress_cb=lambda done, total: tracker.report_progress(
                    stem, operation_id, done, total
                ),
            )
        if observation.error is not None:
            return OperationOutcome(state="stale-served", error=str(observation.error))
        return OperationOutcome(state="fresh")

    return tracker.start_if_needed(stem, run)


def _pending_response(
    tracker: OperationTracker, operation_id: int
) -> dict[str, object]:
    return {
        "pending": True,
        "operationId": operation_id,
        "serverEpoch": tracker.server_epoch,
    }


def _ensure_ready_or_pending(
    repository: TickRepository, tracker: OperationTracker, symbol: str
) -> dict[str, object] | None:
    """``None`` if the caller should proceed synchronously; otherwise a
    ``{"pending": True, ...}`` response to return as-is.

    Checks for an already-in-flight operation *before* the repository's own
    (lock-acquiring) existence check, so a request arriving while another is
    already downloading this stem is answered immediately rather than
    blocking behind that download's `symbol_lock`. Only a request that is
    not reusing an in-flight operation ever needs to confirm existence or
    decide whether to start a new one.
    """
    reused = tracker.operation_id_if_inflight(symbol)
    if reused is not None:
        return _pending_response(tracker, reused)

    _confirm_known_or_http(repository, symbol)

    if repository.needs_download(symbol):
        return _pending_response(tracker, _start_download(repository, tracker, symbol))
    return None


@app.get("/api/status")
def read_status(
    stem: str = Query(..., description="銘柄ファイル名 (例: 7203)"),
) -> dict[str, object]:
    """A pure in-memory snapshot of one stem's download/commit progress.

    Never constructs the repository, performs network I/O, or globs a
    directory (Step 7 of the plan) — safe to poll frequently.
    """
    tracker = get_tracker()
    snapshot = tracker.snapshot(stem.upper())
    return {"serverEpoch": tracker.server_epoch, **snapshot.as_dict()}


@app.get("/api/symbols")
def list_symbols(
    q: str = Query(default="", description="銘柄コードの前方一致フィルタ"),
    limit: int = Query(default=300, ge=1, le=5000),
) -> dict[str, object]:
    repository = get_repository()
    stems = repository.list_symbol_stems()
    needle = q.strip().upper()
    if needle:
        stems = [stem for stem in stems if stem.startswith(needle)]
    return {"total": len(stems), "symbols": stems[:limit]}


@app.get("/api/symbols/{stem}")
def read_symbol(stem: str) -> dict[str, object]:
    repository = get_repository()
    tracker = get_tracker()
    symbol = stem.upper()

    pending = _ensure_ready_or_pending(repository, tracker, symbol)
    if pending is not None:
        return pending

    try:
        info = repository.symbol_info(symbol)
    except (
        SymbolNotFoundError,
        SymbolAvailabilityUnknownError,
        SymbolUnavailableError,
    ) as error:
        _reraise_as_http(error)
        raise AssertionError("unreachable") from error
    return {"pending": False, "operationId": None, **info.as_dict()}


@app.get("/api/session")
def read_session(
    stem: str = Query(..., description="銘柄ファイル名 (例: 7203)"),
    date: str = Query(..., description="対象日 YYYY-MM-DD"),
    direction: int = Query(
        default=0,
        ge=-1,
        le=1,
        description="0=その日のみ / -1=過去方向に最も近い営業日 / 1=未来方向",
    ),
) -> dict[str, object]:
    repository = get_repository()
    tracker = get_tracker()
    symbol = stem.upper()
    day = _parse_date(date)

    pending = _ensure_ready_or_pending(repository, tracker, symbol)
    if pending is not None:
        return pending

    try:
        session = repository.resolve_and_load_session(symbol, day, direction)
    except (
        SymbolNotFoundError,
        SymbolAvailabilityUnknownError,
        SymbolUnavailableError,
    ) as error:
        _reraise_as_http(error)
        raise AssertionError("unreachable") from error
    if session is None:
        raise HTTPException(
            status_code=404,
            detail=f"{symbol}: {date} 付近に約定データが見つかりません",
        )
    return {"pending": False, "operationId": None, **session.as_dict()}


@app.get("/api/minute-context")
def read_minute_context(
    stem: str = Query(..., description="銘柄ファイル名 (例: 7203)"),
    code: str = Query(..., description="canonical code (例: 72030)"),
    date: str = Query(..., description="この日時より前を取得 YYYY-MM-DD"),
    time: str = Query(..., description="この日時より前を取得 HH:MM"),
    limit: int = Query(default=30, ge=1, le=500),
) -> dict[str, object]:
    """Supplementary, best-effort minute bars preceding a session's first
    tick (see ``minute_context.py``) — never the async pending/operationId
    path: every failure degrades to an empty list, so there is nothing here
    for a client to poll or retry.
    """
    repository = get_repository()
    bars = minute_context.bars_before(
        repository.cache_dir,
        repository.http_client,
        stem=stem.upper(),
        code=code,
        before_date=date,
        before_time=time,
        limit=limit,
    )
    return {"bars": [bar.as_dict() for bar in bars]}


@app.get("/")
def read_index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


# Python's mimetypes module derives its table from the OS registry on
# Windows, which commonly has no entry for ".mjs" and falls back to
# "text/plain" — a browser refuses to execute a <script type="module">
# served with that Content-Type ("Failed to load module script"), so
# request-coordinator.mjs would 200 but silently never run. Registered
# explicitly so StaticFiles' guess_type() reports it correctly regardless
# of the host OS's registry contents.
mimetypes.add_type("text/javascript", ".mjs")

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
