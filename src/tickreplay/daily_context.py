"""Best-effort loader for official per-stem ``stocks_daily`` history.

Daily history supplements replay data and must never make session loading
fail. Input mistakes remain explicit ``ValueError`` instances; file, network,
DuckDB, and row-conversion failures instead produce ``available=False``.
Successful queries remain distinguishable from those failures even when no
eligible date exists before the cutoff.
"""

from __future__ import annotations

import os
import re
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import date as Date
from enum import Enum
from hashlib import sha256
from pathlib import Path

import duckdb
import httpx

from . import cache
from .repository import SYMBOL_STEM_RE

DIRNAME = "stocks_daily"
TABLE = "stocks_daily"
MAX_BARS = 500
DOWNLOAD_CHUNK_BYTES = 1024 * 1024
DOWNLOAD_TIMEOUT_SECONDS = 15.0
DOWNLOAD_TOTAL_SECONDS = 30.0
MAX_DAILY_FILE_BYTES = 64 * 1024 * 1024
MAX_CONCURRENT_DOWNLOADS = 4
LOCK_STRIPE_COUNT = 64
NEGATIVE_CACHE_CAPACITY = 256
NEGATIVE_MISS_TTL_SECONDS = 60.0
REVALIDATED_CACHE_CAPACITY = 8192

_ISO_DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
_LOCK_STRIPES = tuple(threading.Lock() for _ in range(LOCK_STRIPE_COUNT))
_DOWNLOAD_SLOTS = threading.BoundedSemaphore(MAX_CONCURRENT_DOWNLOADS)
_state_guard = threading.Lock()
_negative_misses: OrderedDict[tuple[Path, str], tuple[float, float]] = OrderedDict()
_revalidated: OrderedDict[tuple[Path, str], None] = OrderedDict()
_monotonic = time.monotonic

_DAILY_QUERY = f"""
WITH source_rows AS (
    SELECT
        TRY_CAST("Date" AS DATE) AS bar_date,
        TRY_CAST("Open" AS DOUBLE) AS open_value,
        TRY_CAST("High" AS DOUBLE) AS high_value,
        TRY_CAST("Low" AS DOUBLE) AS low_value,
        TRY_CAST("Close" AS DOUBLE) AS close_value,
        TRY_CAST("Volume" AS DOUBLE) AS volume_value
    FROM {TABLE}
    WHERE TRY_CAST("Date" AS DATE) < CAST(? AS DATE)
),
eligible_dates AS (
    SELECT
        bar_date,
        MIN(open_value) AS open_value,
        MIN(high_value) AS high_value,
        MIN(low_value) AS low_value,
        MIN(close_value) AS close_value,
        MIN(volume_value) AS volume_value
    FROM source_rows
    WHERE bar_date IS NOT NULL
    GROUP BY bar_date
    HAVING BOOL_AND(
        open_value IS NOT NULL
        AND high_value IS NOT NULL
        AND low_value IS NOT NULL
        AND close_value IS NOT NULL
        AND volume_value IS NOT NULL
        AND ISFINITE(open_value)
        AND ISFINITE(high_value)
        AND ISFINITE(low_value)
        AND ISFINITE(close_value)
        AND ISFINITE(volume_value)
        AND open_value > 0
        AND high_value > 0
        AND low_value > 0
        AND close_value > 0
        AND volume_value >= 0
        AND low_value <= LEAST(open_value, close_value)
        AND GREATEST(open_value, close_value) <= high_value
    )
    AND MIN(open_value) = MAX(open_value)
    AND MIN(high_value) = MAX(high_value)
    AND MIN(low_value) = MAX(low_value)
    AND MIN(close_value) = MAX(close_value)
    AND MIN(volume_value) = MAX(volume_value)
),
newest_dates AS (
    SELECT bar_date, open_value, high_value, low_value, close_value, volume_value
    FROM eligible_dates
    ORDER BY bar_date DESC
    LIMIT ?
)
SELECT
    STRFTIME(bar_date, '%Y-%m-%d') AS bar_time,
    open_value,
    high_value,
    low_value,
    close_value,
    volume_value
FROM newest_dates
ORDER BY bar_date ASC
"""


@dataclass(frozen=True)
class DailyBar:
    """One validated raw daily OHLCV observation."""

    time: str
    open: float
    high: float
    low: float
    close: float
    volume: float

    def as_dict(self) -> dict[str, object]:
        return {
            "time": self.time,
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
        }


@dataclass(frozen=True)
class DailyContextResult:
    """Typed availability outcome for one bounded daily-history query."""

    bars: tuple[DailyBar, ...]
    available: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "bars": [bar.as_dict() for bar in self.bars],
            "available": self.available,
        }


_UNAVAILABLE = DailyContextResult(bars=(), available=False)


def _validate_inputs(stem: str, before_date: str, limit: int) -> None:
    if not SYMBOL_STEM_RE.fullmatch(stem):
        raise ValueError(f"invalid daily stem: {stem!r}")
    if not _ISO_DATE_RE.fullmatch(before_date):
        raise ValueError(f"invalid daily cutoff: {before_date!r}")
    try:
        Date.fromisoformat(before_date)
    except ValueError as error:
        raise ValueError(f"invalid daily cutoff: {before_date!r}") from error
    if (
        isinstance(limit, bool)
        or not isinstance(limit, int)
        or not 1 <= limit <= MAX_BARS
    ):
        raise ValueError(f"daily limit must be between 1 and {MAX_BARS}")


def _cache_key(cache_dir: Path, stem: str) -> tuple[Path, str]:
    return (cache_dir.resolve(), stem)


def _lock_for(cache_dir: Path, stem: str) -> threading.Lock:
    key = f"{cache_dir.resolve()}\0{stem}".encode()
    stripe = int.from_bytes(sha256(key).digest()[:8], "big") % LOCK_STRIPE_COUNT
    return _LOCK_STRIPES[stripe]


def reset_for_tests() -> None:
    """Reset process-scoped validation and negative-cache state."""
    with _state_guard:
        _negative_misses.clear()
        _revalidated.clear()


def capture_request_started_at() -> float:
    """Capture a load's arrival using the negative-cache monotonic clock."""
    return _monotonic()


def _negative_active(key: tuple[Path, str], request_started_at: float) -> bool:
    with _state_guard:
        entry = _negative_misses.get(key)
        if entry is None:
            return False
        recorded_at, expires_at = entry
        if expires_at <= _monotonic():
            del _negative_misses[key]
            return False
        if request_started_at < recorded_at:
            _negative_misses.move_to_end(key)
            return True
        del _negative_misses[key]
        return False


def _record_negative(key: tuple[Path, str]) -> None:
    with _state_guard:
        recorded_at = _monotonic()
        _negative_misses[key] = (
            recorded_at,
            recorded_at + NEGATIVE_MISS_TTL_SECONDS,
        )
        _negative_misses.move_to_end(key)
        while len(_negative_misses) > NEGATIVE_CACHE_CAPACITY:
            _negative_misses.popitem(last=False)


def _clear_negative(key: tuple[Path, str]) -> None:
    with _state_guard:
        _negative_misses.pop(key, None)


def _was_revalidated(key: tuple[Path, str]) -> bool:
    with _state_guard:
        return key in _revalidated


def _mark_revalidated(key: tuple[Path, str]) -> None:
    with _state_guard:
        _revalidated[key] = None
        _revalidated.move_to_end(key)
        while len(_revalidated) > REVALIDATED_CACHE_CAPACITY:
            _revalidated.popitem(last=False)


def _dataset_dir(cache_dir: Path) -> Path:
    directory = cache_dir / DIRNAME
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _live_path(cache_dir: Path, stem: str) -> Path:
    return _dataset_dir(cache_dir) / f"{stem}.duckdb"


def _authoritative_live_candidates(cache_dir: Path, stem: str) -> tuple[Path, ...]:
    """Exact-first local Daily paths for a case-preserving data tree."""
    directory = _dataset_dir(cache_dir)
    candidates: list[Path] = []
    seen_names: set[str] = set()
    for variant in (stem, stem.lower(), stem.upper()):
        name = f"{variant}.duckdb"
        # Windows Path equality folds case, so deduplicate filename strings.
        if name in seen_names:
            continue
        seen_names.add(name)
        candidates.append(directory / name)
    return tuple(candidates)


def _part_path(cache_dir: Path, stem: str) -> Path:
    return _dataset_dir(cache_dir) / f"{stem}.duckdb.part"


def _sidecar_path(cache_dir: Path, stem: str) -> Path:
    return _dataset_dir(cache_dir) / f"{stem}.duckdb.sidecar.json"


def _sidecar_tmp_path(cache_dir: Path, stem: str) -> Path:
    return _dataset_dir(cache_dir) / f"{stem}.duckdb.sidecar.json.tmp"


def _discard(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        return


class _RefreshState(Enum):
    UPDATED = "updated"
    NOT_MODIFIED = "not-modified"
    NOT_FOUND = "not-found"
    UNREACHABLE = "unreachable"
    FAILED = "failed"


def _conditional_headers(cache_dir: Path, stem: str) -> dict[str, str]:
    sidecar = cache.read_sidecar(_sidecar_path(cache_dir, stem))
    if sidecar is None:
        return {}
    if sidecar.etag:
        return {"If-None-Match": sidecar.etag}
    if sidecar.last_modified:
        return {"If-Modified-Since": sidecar.last_modified}
    return {}


def _validate_database(path: Path) -> None:
    """Prove the staged file supports the exact bounded production query."""
    with duckdb.connect(str(path), read_only=True) as connection:
        connection.execute(_DAILY_QUERY, ["9999-12-31", 1]).fetchall()


def _commit_download(
    cache_dir: Path,
    stem: str,
    *,
    digest: str,
    etag: str | None,
    last_modified: str | None,
) -> None:
    part_path = _part_path(cache_dir, stem)
    sidecar_path = _sidecar_path(cache_dir, stem)
    sidecar_tmp_path = _sidecar_tmp_path(cache_dir, stem)
    previous = cache.read_sidecar(sidecar_path)
    sidecar = cache.Sidecar(
        etag=etag,
        last_modified=last_modified,
        sha256=digest,
        generation=1 if previous is None else previous.generation + 1,
    )
    _discard(sidecar_tmp_path)
    sidecar_tmp_path.write_bytes(sidecar.to_json_bytes())
    try:
        os.replace(part_path, _live_path(cache_dir, stem))
        os.replace(sidecar_tmp_path, sidecar_path)
    except OSError:
        _discard(part_path)
        _discard(sidecar_tmp_path)
        raise


def _refresh(
    cache_dir: Path,
    client: httpx.Client,
    stem: str,
    *,
    conditional: bool,
) -> _RefreshState:
    part_path = _part_path(cache_dir, stem)
    _discard(part_path)
    headers = _conditional_headers(cache_dir, stem) if conditional else {}
    digest = sha256()
    started_at = _monotonic()
    try:
        with _DOWNLOAD_SLOTS:
            with client.stream(
                "GET",
                f"/jp/{DIRNAME}/{stem}.duckdb",
                headers=headers,
                timeout=DOWNLOAD_TIMEOUT_SECONDS,
            ) as response:
                if response.status_code == 304:
                    return _RefreshState.NOT_MODIFIED
                if response.status_code == 404:
                    return _RefreshState.NOT_FOUND
                if response.status_code >= 500:
                    return _RefreshState.UNREACHABLE
                if response.status_code != 200:
                    return _RefreshState.FAILED
                declared = response.headers.get("Content-Length")
                expected_bytes = int(declared) if declared is not None else None
                if expected_bytes is not None and (
                    expected_bytes < 0 or expected_bytes > MAX_DAILY_FILE_BYTES
                ):
                    return _RefreshState.FAILED
                bytes_written = 0
                with part_path.open("wb") as file_handle:
                    for chunk in response.iter_bytes(DOWNLOAD_CHUNK_BYTES):
                        if _monotonic() - started_at > DOWNLOAD_TOTAL_SECONDS:
                            raise TimeoutError("daily download deadline exceeded")
                        bytes_written += len(chunk)
                        if bytes_written > MAX_DAILY_FILE_BYTES:
                            raise ValueError("daily download exceeds size limit")
                        file_handle.write(chunk)
                        digest.update(chunk)
                if expected_bytes is not None and bytes_written != expected_bytes:
                    raise ValueError("daily download length mismatch")
    except (httpx.HTTPError, TimeoutError):
        _discard(part_path)
        return _RefreshState.UNREACHABLE
    except (OSError, ValueError):
        _discard(part_path)
        return _RefreshState.FAILED

    try:
        _validate_database(part_path)
    except duckdb.Error:
        _discard(part_path)
        return _RefreshState.FAILED

    try:
        _commit_download(
            cache_dir,
            stem,
            digest=digest.hexdigest(),
            etag=response.headers.get("ETag"),
            last_modified=response.headers.get("Last-Modified"),
        )
    except OSError:
        _discard(part_path)
        return _RefreshState.FAILED
    return _RefreshState.UPDATED


def _refresh_ready(
    cache_dir: Path,
    client: httpx.Client,
    stem: str,
    *,
    conditional: bool,
    existing: bool,
) -> bool:
    key = _cache_key(cache_dir, stem)
    outcome = _refresh(cache_dir, client, stem, conditional=conditional)
    if outcome is _RefreshState.NOT_FOUND:
        _record_negative(key)
        return False
    if outcome is _RefreshState.UPDATED:
        _clear_negative(key)
        _mark_revalidated(key)
        return True
    if outcome is _RefreshState.NOT_MODIFIED:
        if existing:
            _mark_revalidated(key)
            return True
        return False
    if outcome is _RefreshState.UNREACHABLE and existing:
        _mark_revalidated(key)
        return True
    return False


def _ensure_ready_locked(
    cache_dir: Path,
    client: httpx.Client,
    stem: str,
    *,
    local_authoritative: bool,
    request_started_at: float,
) -> Path | None:
    key = _cache_key(cache_dir, stem)
    if local_authoritative:
        for candidate in _authoritative_live_candidates(cache_dir, stem):
            if not candidate.is_file():
                continue
            usable = 0 < candidate.stat().st_size <= MAX_DAILY_FILE_BYTES
            return candidate if usable else None
        return None

    path = _live_path(cache_dir, stem)
    existing = path.is_file()
    usable = existing and 0 < path.stat().st_size <= MAX_DAILY_FILE_BYTES
    if _negative_active(key, request_started_at):
        return None
    if usable and _was_revalidated(key):
        return path
    if not _refresh_ready(
        cache_dir,
        client,
        stem,
        conditional=usable,
        existing=usable,
    ):
        return None
    return path if path.is_file() else None


def _query_bars(path: Path, before_date: str, limit: int) -> tuple[DailyBar, ...]:
    with duckdb.connect(str(path), read_only=True) as connection:
        rows = connection.execute(_DAILY_QUERY, [before_date, limit]).fetchall()
    return tuple(
        DailyBar(
            time=str(bar_time),
            open=float(open_value),
            high=float(high_value),
            low=float(low_value),
            close=float(close_value),
            volume=float(volume_value),
        )
        for bar_time, open_value, high_value, low_value, close_value, volume_value in rows
    )


def load_daily_context(
    cache_dir: Path,
    client: httpx.Client,
    *,
    stem: str,
    before_date: str,
    limit: int,
    local_authoritative: bool = False,
    request_started_at: float | None = None,
) -> DailyContextResult:
    """Load up to ``limit`` completed raw daily bars, oldest first.

    The whole per-stem table contributes regardless of its ``Code`` values.
    The strict cutoff, validation, duplicate-date resolution, and bounding all
    execute inside DuckDB so Python never materializes more than ``limit`` rows.
    """
    if request_started_at is None:
        request_started_at = capture_request_started_at()
    _validate_inputs(stem, before_date, limit)
    try:
        with _lock_for(cache_dir, stem):
            path = _ensure_ready_locked(
                cache_dir,
                client,
                stem,
                local_authoritative=local_authoritative,
                request_started_at=request_started_at,
            )
            if path is None:
                return _UNAVAILABLE
            try:
                bars = _query_bars(path, before_date, limit)
            except duckdb.Error:
                if local_authoritative:
                    return _UNAVAILABLE
                if not _refresh_ready(
                    cache_dir,
                    client,
                    stem,
                    conditional=False,
                    existing=False,
                ):
                    return _UNAVAILABLE
                bars = _query_bars(path, before_date, limit)
    except (duckdb.Error, OSError, TypeError, ValueError, OverflowError):
        return _UNAVAILABLE
    return DailyContextResult(bars=bars, available=True)
