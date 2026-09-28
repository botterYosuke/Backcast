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

import hashlib
import ipaddress
import json
import mimetypes
import threading
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from datetime import date as Date
from functools import partial
from pathlib import Path
from typing import cast

import anyio
import anyio.to_thread
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from . import cache, cache_commit, daily_context, minute_context
from .bundled_rl_runtime import (
    BundledRlProcess,
    BundledRlRuntimeError,
    self_check,
)
from .bundled_rl_service import (
    BundledRlJobNotFoundError,
    BundledRlManager,
    BundledRlManagerClosedError,
    BundledRlQueueFullError,
    BundledRlUnavailableError,
)
from .config import (
    build_http_client,
    resolve_bundled_rl_config,
    resolve_cache_config,
)
from .repository import (
    SymbolAvailabilityUnknownError,
    SymbolNotFoundError,
    SymbolUnavailableError,
    TickRepository,
)
from .strategy_trace import (
    CONTRACT_VERSION,
    PRESET,
    STEM_RE,
    TraceStore,
    TraceStoreError,
    TraceValidationError,
)

STATIC_DIR = Path(__file__).resolve().parent / "static"
DAILY_CONTEXT_IO_CONCURRENCY = 4
_DAILY_CONTEXT_LIMITER = anyio.CapacityLimiter(DAILY_CONTEXT_IO_CONCURRENCY)
IMMUTABLE_CACHE_CONTROL = "public, max-age=31536000, immutable"
MUTABLE_CACHE_CONTROL = "no-store"
MAX_NATIVE_REQUEST_BYTES = 16 * 1024
STRATEGY_REPORT_ONLY_CSP = (
    "default-src 'self'; script-src 'self' 'wasm-unsafe-eval'; "
    "style-src 'self' 'unsafe-inline'; worker-src 'self'; connect-src 'self'; "
    "object-src 'none'; base-uri 'self'"
)
STRATEGY_ASSET_MEDIA_TYPES = {
    ".mjs": "text/javascript",
    ".wasm": "application/wasm",
    ".zip": "application/zip",
    ".whl": "application/zip",
    ".json": "application/json",
    ".py": "text/x-python",
    ".txt": "text/plain",
    ".gz": "application/gzip",
}


class SelectiveGZipMiddleware:
    """Retain dynamic gzip except for the committed measurement sidecar.

    ``strategy-editor.mjs.gz`` is a directly downloadable provenance artifact,
    not a negotiated representation of ``strategy-editor.mjs``. Passing it
    through Starlette's gzip middleware would add a second Content-Encoding
    layer and ``Vary``, changing the bytes and validators frozen in its
    manifest.
    """

    def __init__(self, app, minimum_size: int = 2048) -> None:
        self.app = app
        self.gzip_app = GZipMiddleware(app, minimum_size=minimum_size)

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "http" and scope.get("path", "").endswith(".mjs.gz"):
            await self.app(scope, receive, send)
            return
        await self.gzip_app(scope, receive, send)


def _is_hex_identifier(value: str) -> bool:
    return len(value) == 64 and all(
        character in "0123456789abcdef" for character in value
    )


def _is_immutable_strategy_asset(path: str) -> bool:
    parts = tuple(part for part in path.replace("\\", "/").split("/") if part)
    if len(parts) >= 4 and parts[:2] == ("vendor", "codemirror"):
        return _is_hex_identifier(parts[2])
    if len(parts) >= 5 and parts[:2] == ("vendor", "pyodide"):
        return _is_hex_identifier(parts[3])
    if len(parts) >= 4 and parts[:2] == ("strategy", "releases"):
        return _is_hex_identifier(parts[2])
    return False


class StrategyStaticFiles(StaticFiles):
    """Static serving with explicit mutable/immutable strategy cache classes."""

    async def get_response(self, path: str, scope):
        response = await super().get_response(path, scope)
        if response.status_code not in {200, 304}:
            return response
        immutable = _is_immutable_strategy_asset(path)
        response.headers["Cache-Control"] = (
            IMMUTABLE_CACHE_CONTROL if immutable else MUTABLE_CACHE_CONTROL
        )
        for suffix, media_type in STRATEGY_ASSET_MEDIA_TYPES.items():
            if path.endswith(suffix):
                response.headers["Content-Type"] = media_type
                break
        if path.endswith(".mjs.gz"):
            full_path, stat_result = self.lookup_path(path)
            if stat_result is not None:
                digest = hashlib.sha256(Path(full_path).read_bytes()).hexdigest()
                response.headers["ETag"] = f'"sha256-{digest}"'
            response.headers["Content-Type"] = "application/gzip"
            if "content-encoding" in response.headers:
                del response.headers["content-encoding"]
            if "vary" in response.headers:
                del response.headers["vary"]
        return response


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
_native_manager: BundledRlManager | None = None
_lifespan_users = 0
_tracker = OperationTracker()


def _build_native_manager(cache_dir: Path) -> BundledRlManager:
    config = resolve_bundled_rl_config(cache_dir=cache_dir)
    if not config.enabled:
        return BundledRlManager(
            available=False,
            unavailable_reason="Bundled RL is disabled by configuration.",
            process=None,
            store=None,
            runtime_manifest=None,
            runtime_fingerprint=None,
        )
    process = BundledRlProcess()
    try:
        manifest, fingerprint = self_check(
            process, timeout_seconds=config.timeout_seconds
        )
    except (BundledRlRuntimeError, TimeoutError):
        return BundledRlManager(
            available=False,
            unavailable_reason="Bundled RL runtime self-check failed.",
            process=None,
            store=None,
            runtime_manifest=None,
            runtime_fingerprint=None,
        )
    return BundledRlManager(
        available=True,
        unavailable_reason=None,
        process=process,
        store=TraceStore(config.artifact_dir),
        runtime_manifest=manifest,
        runtime_fingerprint=fingerprint,
        max_concurrency=config.max_concurrency,
        max_queue=config.max_queue,
        timeout_seconds=config.timeout_seconds,
    )


def _acquire_repository_for_lifespan() -> TickRepository:
    """Build or share the repository for one active lifespan entry.

    Construction and startup reconciliation stay under one lock so a nested
    or concurrent lifespan cannot observe a half-initialized singleton. A
    reference count prevents one lifespan exit from closing the instance
    while another re-entrant lifespan still owns it.
    """
    global _lifespan_users, _native_manager, _repository
    with _construction_lock:
        if _repository is None:
            config = resolve_cache_config()
            repository = TickRepository(
                cache_dir=config.cache_dir,
                server_url=config.server_url,
                http_client=build_http_client(config),
                local_authoritative=config.local_authoritative,
            )
            try:
                cache.discard_orphaned_part_files(repository.cache_dir)
                cache_commit.reconcile_all_at_startup(repository.cache_dir, repository)
                native_manager = _build_native_manager(repository.cache_dir)
            except BaseException:
                repository.close()
                repository.http_client.close()
                raise
            _repository = repository
            _native_manager = native_manager
        elif _native_manager is None:
            raise RuntimeError("native strategy manager ownership is inconsistent")
        _lifespan_users += 1
        return _repository


def _release_repository_from_lifespan(repository: TickRepository) -> None:
    """Release one lifespan owner and close the last owner's repository."""
    global _lifespan_users, _native_manager, _repository
    to_close: TickRepository | None = None
    native_to_close: BundledRlManager | None = None
    with _construction_lock:
        if _repository is not repository or _lifespan_users <= 0:
            raise RuntimeError("lifespan repository ownership is inconsistent")
        _lifespan_users -= 1
        if _lifespan_users == 0:
            _repository = None
            native_to_close = _native_manager
            _native_manager = None
            to_close = repository

    if native_to_close is not None:
        native_to_close.shutdown()
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


def get_native_manager() -> BundledRlManager:
    """Return the lifespan-owned native manager without constructing it."""
    with _construction_lock:
        if _native_manager is None:
            raise RuntimeError(
                "native strategy manager is unavailable outside the app lifespan"
            )
        return _native_manager


def reset_for_tests() -> None:
    """Test-only: close detached state and reset the in-memory tracker."""
    global _native_manager, _repository, _tracker
    to_close: TickRepository | None = None
    native_to_close: BundledRlManager | None = None
    with _construction_lock:
        if _lifespan_users:
            raise RuntimeError("cannot reset while an app lifespan is active")
        to_close = _repository
        native_to_close = _native_manager
        _repository = None
        _native_manager = None
        _tracker = OperationTracker()
        daily_context.reset_for_tests()
    if native_to_close is not None:
        native_to_close.shutdown()
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
app.add_middleware(SelectiveGZipMiddleware, minimum_size=2048)


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


def _native_job_request(payload: object) -> dict[str, str]:
    """Reject every non-preset field before touching repository or job state."""
    if not isinstance(payload, dict) or set(payload) != {
        "contractVersion",
        "preset",
        "session",
    }:
        raise HTTPException(status_code=400, detail="invalid Bundled RL request")
    request = cast(dict[str, object], payload)
    if request["contractVersion"] != CONTRACT_VERSION or request["preset"] != PRESET:
        raise HTTPException(status_code=400, detail="unsupported Bundled RL preset")
    session = request["session"]
    if not isinstance(session, dict) or set(session) != {
        "stem",
        "code",
        "date",
        "dataHash",
    }:
        raise HTTPException(status_code=400, detail="invalid session identity")
    session_value = cast(dict[str, object], session)
    if not all(isinstance(session_value[key], str) for key in session_value):
        raise HTTPException(status_code=400, detail="invalid session identity")
    normalized = cast(dict[str, str], session_value)
    code = normalized["code"]
    if (
        STEM_RE.fullmatch(normalized["stem"]) is None
        or not code
        or "\x00" in code
        or len(code.encode("utf-8")) > 128
        or not _is_hex_identifier(normalized["dataHash"])
    ):
        raise HTTPException(status_code=400, detail="invalid session identity")
    day = _parse_date(normalized["date"])
    if day.isoformat() != normalized["date"]:
        raise HTTPException(status_code=400, detail="invalid session identity")
    return normalized


def _local_host_authority(value: str) -> str | None:
    authority = value.lower()
    if authority.startswith("["):
        closing = authority.find("]")
        if closing == -1 or authority[: closing + 1] != "[::1]":
            return None
        suffix = authority[closing + 1 :]
        if suffix and not suffix.startswith(":"):
            return None
        port = suffix[1:] if suffix else ""
    else:
        host, separator, port = authority.partition(":")
        if host not in {"localhost", "127.0.0.1"}:
            return None
        if separator and ":" in port:
            return None
    if port:
        if not port.isascii() or not port.isdigit() or len(port) > 5:
            return None
        if not 1 <= int(port) <= 65535:
            return None
    return authority


def _require_native_local(request: Request) -> None:
    peer = request.client.host if request.client is not None else ""
    try:
        address = ipaddress.ip_address(peer)
    except ValueError:
        address = None
    host_values = request.headers.getlist("host")
    authority = _local_host_authority(host_values[0]) if len(host_values) == 1 else None
    if address is None or not address.is_loopback or authority is None:
        raise HTTPException(status_code=403, detail="native strategy access denied")
    origins = request.headers.getlist("origin")
    if len(origins) > 1 or (
        origins and origins[0] != f"{request.url.scheme}://{authority}"
    ):
        raise HTTPException(status_code=403, detail="native strategy access denied")


async def _read_native_json(request: Request) -> object:
    _require_native_local(request)
    content_lengths = request.headers.getlist("content-length")
    if len(content_lengths) > 1:
        raise HTTPException(status_code=400, detail="invalid content length")
    raw_length = content_lengths[0] if content_lengths else None
    if raw_length is not None:
        if not raw_length.isascii() or not raw_length.isdigit():
            raise HTTPException(status_code=400, detail="invalid content length")
        if len(raw_length) > len(str(MAX_NATIVE_REQUEST_BYTES)):
            raise HTTPException(
                status_code=413, detail="native strategy request too large"
            )
        if int(raw_length) > MAX_NATIVE_REQUEST_BYTES:
            raise HTTPException(
                status_code=413, detail="native strategy request too large"
            )
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > MAX_NATIVE_REQUEST_BYTES:
            raise HTTPException(
                status_code=413, detail="native strategy request too large"
            )
        body.extend(chunk)

    def reject_constant(_value: str) -> None:
        raise ValueError("non-finite JSON number")

    def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON field")
            result[key] = value
        return result

    try:
        return json.loads(
            body.decode("utf-8"),
            parse_constant=reject_constant,
            object_pairs_hook=reject_duplicates,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise HTTPException(
            status_code=400, detail="invalid native strategy request"
        ) from None


@app.get("/api/strategies/bundled-rl/capability")
def read_native_strategy_capability(request: Request) -> dict[str, object]:
    _require_native_local(request)
    return get_native_manager().capability()


@app.post("/api/strategies/bundled-rl/jobs")
def create_native_strategy_job(
    request: Request, payload: object = Depends(_read_native_json)
) -> JSONResponse:
    _require_native_local(request)
    requested = _native_job_request(payload)
    manager = get_native_manager()
    if not manager.available:
        raise HTTPException(
            status_code=503, detail="native strategy execution is unavailable"
        )
    repository = get_repository()
    tracker = get_tracker()
    symbol = requested["stem"].upper()
    pending = _ensure_ready_or_pending(repository, tracker, symbol)
    if pending is not None:
        raise HTTPException(
            status_code=409, detail="authoritative session is not ready"
        )
    day = _parse_date(requested["date"])
    try:
        session = repository.resolve_and_load_session(symbol, day, 0)
    except (
        SymbolNotFoundError,
        SymbolAvailabilityUnknownError,
        SymbolUnavailableError,
    ) as error:
        _reraise_as_http(error)
        raise AssertionError("unreachable") from error
    if session is None:
        raise HTTPException(
            status_code=404, detail="authoritative session was not found"
        )
    if (
        requested["stem"] != session.stem
        or requested["code"] != session.code
        or requested["date"] != session.date
        or requested["dataHash"] != session.data_hash
    ):
        raise HTTPException(status_code=409, detail="session identity mismatch")
    observable_session = {
        "stem": session.stem,
        "code": session.code,
        "date": session.date,
        "count": len(session.us),
        "us": session.us,
        "price": session.price,
        "qty": session.qty,
        "type": session.trade_type,
    }
    try:
        job = manager.submit(session=observable_session)
    except TraceValidationError as error:
        raise HTTPException(status_code=400, detail=str(error)) from None
    except BundledRlQueueFullError:
        raise HTTPException(
            status_code=429, detail="native strategy execution queue is full"
        ) from None
    except (BundledRlUnavailableError, BundledRlManagerClosedError):
        raise HTTPException(
            status_code=503, detail="native strategy execution is unavailable"
        ) from None
    status_code = 200 if job.state == "ready" else 202
    return JSONResponse({"job": job.as_dict()}, status_code=status_code)


@app.get("/api/strategies/bundled-rl/jobs/{job_id}")
def read_native_strategy_job(job_id: str, request: Request) -> dict[str, object]:
    _require_native_local(request)
    try:
        return get_native_manager().snapshot(job_id).as_dict()
    except BundledRlJobNotFoundError:
        raise HTTPException(
            status_code=404, detail="native strategy job was not found"
        ) from None


@app.delete("/api/strategies/bundled-rl/jobs/{job_id}")
def cancel_native_strategy_job(job_id: str, request: Request) -> dict[str, object]:
    _require_native_local(request)
    try:
        return {"job": get_native_manager().cancel(job_id).as_dict()}
    except BundledRlJobNotFoundError:
        raise HTTPException(
            status_code=404, detail="Bundled RL job was not found"
        ) from None


@app.get("/api/strategies/bundled-rl/traces/{trace_id}")
def read_native_strategy_trace(trace_id: str, request: Request) -> Response:
    _require_native_local(request)
    manager = get_native_manager()
    if not manager.available or manager.store is None:
        raise HTTPException(
            status_code=404, detail="native strategy trace was not found"
        )
    try:
        data = manager.store.read_bytes(trace_id)
    except (FileNotFoundError, TraceStoreError):
        raise HTTPException(
            status_code=404, detail="native strategy trace was not found"
        ) from None
    return Response(
        content=data,
        media_type="application/json",
        headers={
            "Cache-Control": "private, max-age=31536000, immutable",
            "ETag": f'"{trace_id}"',
        },
    )


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


async def _run_daily_context(
    repository: TickRepository,
    *,
    stem: str,
    before_date: str,
    limit: int,
) -> daily_context.DailyContextResult:
    request_started_at = daily_context.capture_request_started_at()
    load = partial(
        daily_context.load_daily_context,
        repository.cache_dir,
        repository.http_client,
        stem=stem,
        before_date=before_date,
        limit=limit,
        local_authoritative=repository.local_authoritative,
        request_started_at=request_started_at,
    )
    return await anyio.to_thread.run_sync(load, limiter=_DAILY_CONTEXT_LIMITER)


@app.get("/api/daily-context")
async def read_daily_context(
    stem: str = Query(..., description="daily file stem (for example 7203 or 285A)"),
    date: str = Query(..., description="strict exclusive cutoff YYYY-MM-DD"),
    limit: int = Query(default=500, ge=1, le=daily_context.MAX_BARS),
) -> dict[str, object]:
    """Best-effort completed daily bars strictly before ``date``.

    Availability failures stay an HTTP-200 supplementary-data outcome. Input
    errors remain client errors and are never disguised as unavailability.
    """
    repository = get_repository()
    try:
        result = await _run_daily_context(
            repository,
            stem=stem.upper(),
            before_date=date,
            limit=limit,
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return result.as_dict()


@app.get("/")
def read_index() -> FileResponse:
    return FileResponse(
        STATIC_DIR / "index.html",
        headers={
            "Cache-Control": MUTABLE_CACHE_CONTROL,
            "Content-Security-Policy-Report-Only": STRATEGY_REPORT_ONLY_CSP,
        },
    )


# Python's mimetypes module derives its table from the OS registry on
# Windows, which commonly has no entry for ".mjs" and falls back to
# "text/plain" — a browser refuses to execute a <script type="module">
# served with that Content-Type ("Failed to load module script"), so
# request-coordinator.mjs would 200 but silently never run. Registered
# explicitly so StaticFiles' guess_type() reports it correctly regardless
# of the host OS's registry contents.
for suffix, media_type in STRATEGY_ASSET_MEDIA_TYPES.items():
    mimetypes.add_type(media_type, suffix)

app.mount("/static", StrategyStaticFiles(directory=STATIC_DIR), name="static")
