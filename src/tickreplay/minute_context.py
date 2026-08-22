"""Best-effort loader for ``stocks_minute/{stem}.duckdb``.

This is supplementary context only, not a source of truth: it supplies the
handful of minute bars immediately preceding a session's first tick, so the
candlestick chart already shows candles in the pre-playback state instead of
being empty (see docs/tick-replay.md). Deliberately simpler than
``cache.py``/``cache_commit.py``'s crash-safe, generation-tracked pipeline for
``stocks_trades``:

- No sidecar/ETag revalidation. Once a stem's file is downloaded, it is
  reused locally for the rest of the process lifetime. Acceptable because
  this data is cosmetic context, not something later reads depend on for
  correctness.
- Every failure (network, 404, disk, corrupt download, bad SQL) degrades to
  an empty list rather than raising or marking the stem unavailable — a
  session load must never fail, or even display an error, just because this
  optional preload could not be fetched.
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from pathlib import Path

import duckdb
import httpx

from .repository import SYMBOL_STEM_RE

DIRNAME = "stocks_minute"
TABLE = "stocks_minute"

DOWNLOAD_CHUNK_BYTES = 1024 * 1024
DOWNLOAD_TIMEOUT_SECONDS = 15.0

_locks_guard = threading.Lock()
_locks: dict[str, threading.Lock] = {}


@dataclass(frozen=True)
class MinuteBar:
    """One aggregated minute, epoch seconds (UTC-interpreted, matching
    ``stocks_trades``' timestamp convention) at the start of the minute."""

    time: int
    open: float
    high: float
    low: float
    close: float
    volume: int

    def as_dict(self) -> dict[str, object]:
        return {
            "time": self.time,
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
        }


def _lock_for(stem: str) -> threading.Lock:
    with _locks_guard:
        lock = _locks.get(stem)
        if lock is None:
            lock = threading.Lock()
            _locks[stem] = lock
        return lock


def _dataset_dir(cache_dir: Path) -> Path:
    directory = cache_dir / DIRNAME
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _live_path(cache_dir: Path, stem: str) -> Path:
    return _dataset_dir(cache_dir) / f"{stem}.duckdb"


def _part_path(cache_dir: Path, stem: str) -> Path:
    return _dataset_dir(cache_dir) / f"{stem}.duckdb.part"


def _discard(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def _download(cache_dir: Path, client: httpx.Client, stem: str) -> bool:
    """Full download of ``stocks_minute/{stem}.duckdb`` to the local cache.

    Returns True on success; False on any failure — network, a non-200
    response (including 404, when the server simply has no minute file for
    this stem), a disk error, or a file that fails to open as DuckDB. Never
    raises.
    """
    part_path = _part_path(cache_dir, stem)
    try:
        with client.stream(
            "GET", f"/jp/{DIRNAME}/{stem}.duckdb", timeout=DOWNLOAD_TIMEOUT_SECONDS
        ) as response:
            if response.status_code != 200:
                return False
            with part_path.open("wb") as fh:
                for chunk in response.iter_bytes(DOWNLOAD_CHUNK_BYTES):
                    fh.write(chunk)
    except (httpx.HTTPError, OSError):
        _discard(part_path)
        return False

    try:
        with duckdb.connect(str(part_path), read_only=True) as con:
            con.execute("SELECT 1")
    except duckdb.Error:
        _discard(part_path)
        return False

    try:
        os.replace(part_path, _live_path(cache_dir, stem))
    except OSError:
        _discard(part_path)
        return False
    return True


def _ensure_local(cache_dir: Path, client: httpx.Client, stem: str) -> Path | None:
    path = _live_path(cache_dir, stem)
    with _lock_for(stem):
        if path.is_file():
            return path
        return path if _download(cache_dir, client, stem) else None


def bars_before(
    cache_dir: Path,
    client: httpx.Client,
    *,
    stem: str,
    code: str,
    before_date: str,
    before_time: str,
    limit: int,
) -> list[MinuteBar]:
    """The up to ``limit`` minute bars for ``code`` immediately preceding
    (``before_date``, ``before_time``), oldest first. ``stem`` selects the
    ``stocks_minute/{stem}.duckdb`` file to read (mirrors the stocks_trades
    stem, not the canonical ``code`` used inside the table).

    Returns an empty list if the stem is invalid, the file cannot be
    fetched, or there are simply no rows before that point — every failure
    mode is indistinguishable to the caller by design (see module
    docstring).
    """
    if not SYMBOL_STEM_RE.fullmatch(stem) or not code:
        return []
    path = _ensure_local(cache_dir, client, stem)
    if path is None:
        return []
    try:
        with duckdb.connect(str(path), read_only=True) as con:
            rows = con.execute(
                f"""
                SELECT
                    epoch(TRY_CAST((CAST(Date AS VARCHAR) || ' ' || Time) AS TIMESTAMP)) AS ts,
                    Open, High, Low, Close, Volume
                FROM {TABLE}
                WHERE Code = ?
                  AND (Date < ? OR (Date = ? AND Time < ?))
                ORDER BY Date DESC, Time DESC
                LIMIT ?
                """,
                [code, before_date, before_date, before_time, limit],
            ).fetchall()
    except duckdb.Error:
        return []

    bars = [
        MinuteBar(
            time=int(ts),
            open=float(o),
            high=float(h),
            low=float(low),
            close=float(c),
            volume=int(v or 0),
        )
        for ts, o, h, low, c, v in reversed(rows)
        if ts is not None
    ]
    return bars
