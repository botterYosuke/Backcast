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
import os
import threading
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from datetime import date as Date
from functools import partial
from pathlib import Path

import anyio
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import cache, cache_commit, daily_context, minute_context
from .config import build_http_client, resolve_cache_config
from .repository import (
    SymbolAvailabilityUnknownError,
    SymbolNotFoundError,
    SymbolUnavailableError,
    TickRepository,
)

STATIC_DIR = Path(__file__).resolve().parent / "static"
DAILY_CONTEXT_IO_CONCURRENCY = 4
_DAILY_CONTEXT_LIMITER = anyio.CapacityLimiter(DAILY_CONTEXT_IO_CONCURRENCY)


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
                local_authoritative=config.local_authoritative,
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
        daily_context.reset_for_tests()
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


SCALP_MODEL = Path("models/ppo_scalp_top50_open10m.pt")


def _resolve_model_checkpoint(
    stem: str, model_param: str | None = None
) -> Path | None:
    if model_param:
        p = Path(model_param)
        if p.is_file():
            return p
        models_dir_p = Path("models") / model_param
        if models_dir_p.is_file():
            return models_dir_p
        return None

    # The multi-symbol limit-order scalper works for every stem, so it comes first;
    # the single-stock market-order models follow.
    candidates = [
        SCALP_MODEL,
        Path(f"models/ppo_{stem.lower()}_opening10m.pt"),
        Path(f"models/ppo_{stem.upper()}_opening10m.pt"),
        Path(f"models/ppo_{stem.lower()}_opening15m.pt"),
        Path(f"models/ppo_{stem.upper()}_opening15m.pt"),
        Path("models/ppo_7203_opening15m.pt"),
    ]
    for c in candidates:
        if c.is_file():
            return c
    return None


_ORDER_MODE_JA = {"limit": "指値", "market": "成行", "both": "指値+成行"}
_FEATURE_GROUP_JA = {
    "A": "ギャップ区分",
    "B": "ギャップ埋め",
    "C": "市場全体",
    "D": "業種内ギャップ",
    "E": "前日までの傾向",
    "F": "寄り遅れ・値幅制限",
    "G": "前日高安",
    "H": "最初の1分",
    "I": "約定の重み",
    "J": "板活動系",
}


def _features_ja(spec: str) -> str:
    """``base+C+G-J`` -> ``市場全体+前日高安 / 板活動系なし`` (``base`` -> 追加なし)."""
    import re

    adds = [_FEATURE_GROUP_JA.get(g, g) for g in re.findall(r"\+([A-Z])", spec)]
    drops = [_FEATURE_GROUP_JA.get(g, g) for g in re.findall(r"-([A-Z])", spec)]
    text = "+".join(adds) if adds else "追加情報なし"
    if drops:
        text += " / " + "・".join(drops) + "なし"
    return text


def _model_label(path: Path, kind: str, env_config: dict | None = None) -> str:
    if kind == "scalp":
        cfg = env_config or {}
        mode = _ORDER_MODE_JA.get(str(cfg.get("order_mode", "limit")), "指値")
        prefix = "ふるい分け" if path.parent.name == "screen" else "スキャルプ"
        return f"{prefix}（{mode}・{_features_ja(str(cfg.get('features') or 'base'))}）: {path.stem}"
    return f"成行（銘柄別・旧）: {path.stem}"


def _model_file_key(path: Path) -> str:
    """Name relative to ``models/`` (``x.pt`` or ``screen/x.pt``), the ``model`` param."""
    try:
        return path.relative_to("models").as_posix()
    except ValueError:
        return path.name


#: Which AI models the app offers.  ``burst_v3B`` (default): only the five burst v3
#: arm-B policies the _Aries paper trading runs; ``all``: every model as before.
AI_MODELS_ENV = "TICKREPLAY_AI_MODELS"
AI_MODELS_V3B = "burst_v3B"


def _ai_model_set() -> str:
    return (os.environ.get(AI_MODELS_ENV) or AI_MODELS_V3B).strip()


def _v3b_models() -> list[dict[str, object]]:
    from trading_rl import burst_replay

    files = burst_replay.model_files_v3()
    return [
        {"file": burst_replay.model_key(p), "kind": burst_replay.KIND_BURST,
         "label": burst_replay.model_label(p), "default": i == 0}
        for i, p in enumerate(files)
    ]


@app.get("/api/ai/models")
def read_ai_models() -> list[dict[str, object]]:
    """``models/*.pt`` with kind (``scalp`` limit-order / ``market`` legacy) and a label.

    ``default`` marks the checkpoint the replay uses when no ``model`` is given
    (for a stem without its own model).  Under the default model set
    (``TICKREPLAY_AI_MODELS`` unset or ``burst_v3B``) only the five v3 arm-B burst
    policies are listed, the first one as the default.
    """
    if _ai_model_set() != "all":
        return _v3b_models()
    from trading_rl.scalp_replay import checkpoint_kind, load_checkpoint_cached

    default = _resolve_model_checkpoint("")
    out: list[dict[str, object]] = []
    paths = sorted(Path("models").glob("*.pt")) + sorted(
        Path("models/screen").glob("*.pt")
    )
    for path in paths:
        try:
            _policy, checkpoint = load_checkpoint_cached(path)
            kind = checkpoint_kind(checkpoint)
        except Exception:  # noqa: BLE001 - unreadable checkpoint is not selectable
            continue
        env_config = checkpoint.get("env_config") if isinstance(checkpoint, dict) else None
        out.append(
            {
                "file": _model_file_key(path),
                "kind": kind,
                "label": _model_label(path, kind, env_config),
                "default": default is not None and path.name == default.name,
            }
        )
    from trading_rl import predictor_replay

    if predictor_replay.is_available():
        out.extend(
            {"file": p.key, "kind": predictor_replay.KIND_PREDICTOR, "label": p.label,
             "default": False}
            for p in predictor_replay.PRESETS
        )
    from trading_rl import burst_replay

    out.extend(
        {"file": burst_replay.model_key(p), "kind": burst_replay.KIND_BURST,
         "label": burst_replay.model_label(p), "default": False}
        for p in [*burst_replay.model_files(), *burst_replay.model_files_v3()]
    )
    return out


@app.get("/api/ai/replay-trades")
def read_ai_replay_trades(
    stem: str = Query(..., description="銘柄ファイル名 (例: 7203)"),
    date: str = Query(..., description="対象日 YYYY-MM-DD"),
    model: str | None = Query(
        default=None, description="モデルファイルパスまたはファイル名 (任意)"
    ),
) -> dict[str, object]:
    """Returns AI replay trades for the opening session (checkpoint window/warmup/slippage).

    Dispatches on the checkpoint kind: ``scalp`` checkpoints are replayed on
    ``ScalpEnv`` (limit orders, needs prev_close / 20d volume, see
    ``trading_rl.scalp_replay``), legacy ones on the market-order ``TickTradingEnv``.
    """
    symbol = stem.upper()
    unavailable_resp: dict[str, object] = {
        "available": False,
        "stem": stem,
        "date": date,
        "trades": [],
        "summary": None,
    }

    def unavailable(reason: str) -> dict[str, object]:
        return {**unavailable_resp, "reason": reason}

    from trading_rl import burst_replay, predictor_replay

    if not model and _ai_model_set() != "all":
        v3b = _v3b_models()
        if not v3b:
            return unavailable("burst v3 models not found (models/burst_v3/)")
        model = str(v3b[0]["file"])

    # 1. Resolve model checkpoint (or a stage-2 predictor preset / a burst-RL model)
    preset = predictor_replay.parse_key(model)
    burst_path = burst_replay.parse_key(model)
    if model and model.startswith(burst_replay.KEY_PREFIX):
        if burst_path is None or not burst_replay.is_available():
            return unavailable("burst model or detector not found (models/burst/)")
        model_path = None
    elif preset is not None:
        if not predictor_replay.is_available():
            return unavailable("predictor models not built (stage2_trade.py --fit)")
        model_path = None
    else:
        model_path = _resolve_model_checkpoint(stem, model)
        if model_path is None:
            return unavailable("model checkpoint not found")

    # 2. Resolve session ticks
    repository = get_repository()
    try:
        day = _parse_date(date)
    except HTTPException:
        return unavailable("invalid date")

    try:
        session = repository.resolve_and_load_session(symbol, day, direction=0)
    except (
        SymbolNotFoundError,
        SymbolAvailabilityUnknownError,
        SymbolUnavailableError,
        Exception,
    ):
        return unavailable("session ticks unavailable")

    if session is None or len(session.us) == 0:
        return unavailable("session ticks unavailable")

    # 3. Extract opening session ticks
    from trading_rl.data import (
        NoMorningSessionError,
        extract_opening_session_from_ticks,
    )
    from trading_rl.evaluator import resolve_env_config, simulate_ai_trades
    from trading_rl.scalp_replay import (
        checkpoint_kind,
        is_heldout,
        load_checkpoint_cached,
        lookup_session_info,
        period_of,
        simulate_scalp_trades,
    )

    if burst_path is not None:
        kind = burst_replay.KIND_BURST
        env_config: dict = {}
        window_us = burst_replay.REPLAY_WINDOW_US
    elif preset is not None:
        kind = predictor_replay.KIND_PREDICTOR
        env_config = {}
        window_us = predictor_replay.REPLAY_WINDOW_US
    else:
        try:
            _policy, checkpoint = load_checkpoint_cached(model_path)
            kind = checkpoint_kind(checkpoint)
            # Replay with the window the checkpoint was trained on (legacy: 900s).
            env_config = resolve_env_config(checkpoint)
            window_us = int(round(float(env_config["window_seconds"]) * 1_000_000))
        except Exception:
            return unavailable("checkpoint unreadable")

    scalp_info = None
    if kind in ("scalp", predictor_replay.KIND_PREDICTOR, burst_replay.KIND_BURST):
        scalp_info = lookup_session_info(session.stem, session.date)
        if scalp_info is None:
            return unavailable(
                "prev_close / avg_volume_20d unavailable (no universe row or daily panel)"
            )

    try:
        opening_data = extract_opening_session_from_ticks(
            stem=session.stem,
            code=session.code,
            date=session.date,
            us=session.us,
            price=session.price,
            qty=session.qty,
            trade_type=session.trade_type,
            window_us=window_us,
        )
    except (NoMorningSessionError, Exception):
        return unavailable("no morning session")

    # 4. Run deterministic simulation with policy
    response: dict[str, object] = {
        # market-order checkpoints have no order book / universe notion
        "orders": [],
        "in_universe": None,
        "period": None,
        "is_heldout": None,
    }
    try:
        if kind == burst_replay.KIND_BURST:
            assert scalp_info is not None and model is not None
            # v3 was trained with is_topix100 = "uses the TOPIX500 fine tick table"
            fine_info = (
                burst_replay.fine_tick_v3(session.stem, session.date, opening_data.price)
                if burst_replay.is_v3(burst_path) else None
            )
            t100 = fine_info["fine"] if fine_info else scalp_info.is_topix100
            trades, summary, episodes, env_config = burst_replay.simulate_burst_trades(
                opening_data,
                model,
                prev_close=scalp_info.prev_close,
                avg_volume_20d=scalp_info.avg_volume_20d,
                is_topix100=t100,
                held=is_heldout(session.stem),
            )
            response = {
                "orders": [],
                "in_universe": scalp_info.in_universe,
                "period": burst_replay.period_of(session.date),
                "is_heldout": is_heldout(session.stem),
                "is_topix100": t100,
                "fine_tick": fine_info,
                "session_info_source": scalp_info.source,
                "topix100_source": (
                    fine_info["source"] if fine_info else scalp_info.topix100_source
                ),
                "features": "gate28+micro11 (push20) + detector",
                "burst_episodes": episodes,
            }
        elif kind == predictor_replay.KIND_PREDICTOR:
            assert scalp_info is not None and model is not None
            trades, summary, orders, env_config = (
                predictor_replay.simulate_predictor_trades(
                    opening_data,
                    model,
                    prev_close=scalp_info.prev_close,
                    avg_volume_20d=scalp_info.avg_volume_20d,
                    is_topix100=scalp_info.is_topix100,
                )
            )
            response = {
                "orders": orders,
                "in_universe": scalp_info.in_universe,
                "period": period_of(session.date),
                "is_heldout": is_heldout(session.stem),
                "is_topix100": scalp_info.is_topix100,
                "session_info_source": scalp_info.source,
                "topix100_source": scalp_info.topix100_source,
                "features": "base+micro (push20)",
                "predictor": {k: env_config[k] for k in
                              ("horizon_s", "top_q", "threshold_ticks", "variant")},
            }
        elif kind == "scalp":
            assert scalp_info is not None
            trades, summary, orders = simulate_scalp_trades(
                opening_data,
                model_path,
                prev_close=scalp_info.prev_close,
                avg_volume_20d=scalp_info.avg_volume_20d,
                is_topix100=scalp_info.is_topix100,
            )
            response = {
                "orders": orders,
                "in_universe": scalp_info.in_universe,
                "period": period_of(session.date),
                "is_heldout": is_heldout(session.stem),
                "is_topix100": scalp_info.is_topix100,
                "session_info_source": scalp_info.source,
                "topix100_source": scalp_info.topix100_source,
                "features": env_config.get("features", "base"),
            }
        else:
            trades, summary = simulate_ai_trades(opening_data, model_path)
    except Exception as error:  # noqa: BLE001 - degrade, never 5xx the UI
        return unavailable(f"simulation failed: {type(error).__name__}")

    return {
        "available": True,
        "stem": stem,
        "date": session.date,
        "t_open_us": opening_data.t_open_us,
        "trades": trades,
        "summary": summary,
        "model": model if model_path is None else _model_file_key(model_path),
        "model_kind": kind,
        "env_config": {
            "window_s": env_config.get("window_seconds"),
            "warmup_s": env_config.get("opening_warmup_seconds"),
            "fill_rule": env_config.get("fill_rule"),
            "latency_ms": env_config.get("latency_ms"),
        },
        **response,
    }


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
    return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache, no-store, must-revalidate"})


# Python's mimetypes module derives its table from the OS registry on
# Windows, which commonly has no entry for ".mjs" and falls back to
# "text/plain" — a browser refuses to execute a <script type="module">
# served with that Content-Type ("Failed to load module script"), so
# request-coordinator.mjs would 200 but silently never run. Registered
# explicitly so StaticFiles' guess_type() reports it correctly regardless
# of the host OS's registry contents.
mimetypes.add_type("text/javascript", ".mjs")

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
