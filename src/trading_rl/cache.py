"""Local cache of opening-window ticks for the daily universe.

For every ``universe.parquet`` row ``(date, stem)`` the cache keeps the ticks in
``[T_open, T_open + 600 s]`` where ``T_open`` is the first *morning* trade
(``09:00:00 <= Timestamp < 12:00:00``) of that day.  Epoch convention matches
:mod:`trading_rl.data`: ``epoch_us(TRY_CAST(Timestamp AS TIMESTAMP))`` i.e. naive
JST read as UTC.

Layout (one shard per stem, so each ~GB tick file is opened exactly once)::

    <cache>/ticks/<stem>.ticks.npy   structured (t:i8, p:f8, q:i8), mmap-able
    <cache>/ticks/<stem>.index.npz   per-date status + offsets (written LAST)

The index is the commit marker: a shard without an index is ignored/rebuilt.
Per-(date, stem) status is never silently dropped:

``ok``               ticks cached
``no_tick_file``     no ``<stem>.duckdb`` in the trades directory
``no_morning_trade`` the file has trades that day but none in the morning
                     session (e.g. limit-up/halted all morning)
``no_data``          the file has no rows at all that day (coverage gap)
``error``            exception while reading (message kept in the index)

``ok``/``no_morning_trade``/``no_data``/``no_tick_file`` are final on rerun;
only ``error`` rows are retried.
"""

from __future__ import annotations

import os
import re
import threading
import time
from collections import Counter
from collections.abc import Callable, Iterable, Iterator
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date as Date
from datetime import datetime, timedelta
from pathlib import Path

import duckdb
import numpy as np

from .data import TRADES_TABLE, OpeningSessionData
from .universe import (
    DEFAULT_CACHE_DIR,
    DEFAULT_TRADES_DIR,
    load_universe,
)

CACHE_WINDOW_US: int = 600_000_000
MORNING_START = "09:00:00"
MORNING_END = "12:00:00"  # T_open must be a morning-session trade

STATUS_OK = "ok"
STATUS_NO_FILE = "no_tick_file"
STATUS_NO_MORNING = "no_morning_trade"
STATUS_NO_DATA = "no_data"
STATUS_ERROR = "error"
FINAL_STATUSES = frozenset({STATUS_OK, STATUS_NO_FILE, STATUS_NO_MORNING, STATUS_NO_DATA})

TICK_DTYPE = np.dtype([("t", "<i8"), ("p", "<f8"), ("q", "<i8")])
TICKS_SUBDIR = "ticks"
_STEM_RE = re.compile(r"^[0-9][0-9a-z]{3}$")

Progress = Callable[[str], None]


class CachedSessionUnavailable(LookupError):
    """The (date, stem) is not in the cache or its status is not ``ok``."""


# --------------------------------------------------------------------------- #
# Extraction
# --------------------------------------------------------------------------- #
@dataclass
class _SessionRow:
    date: str
    status: str
    msg: str = ""
    t_open_us: int = 0
    p_open: float = float("nan")
    v_open: int = 0
    ticks: np.ndarray | None = None


def index_tick_files(trades_dir: Path = DEFAULT_TRADES_DIR) -> dict[str, Path]:
    """Map lower-case 4-char stem -> tick file (one ``scandir``; no ``glob``)."""
    found: dict[str, tuple[int, Path]] = {}
    for entry in os.scandir(trades_dir):
        name = entry.name
        if not name.endswith(".duckdb"):
            continue
        stem = name[: -len(".duckdb")].lower()
        if not _STEM_RE.match(stem):
            continue  # 5-digit strays, *_Conflict.duckdb, ...
        size = entry.stat().st_size
        if stem not in found or size > found[stem][0]:
            found[stem] = (size, Path(entry.path))
    return {stem: path for stem, (_, path) in found.items()}


def _extract_one(
    con: duckdb.DuckDBPyConnection, code: str, day: str, window_us: int
) -> _SessionRow:
    first = con.execute(
        f"SELECT Timestamp, epoch_us(TRY_CAST(Timestamp AS TIMESTAMP)), Price, "
        f"COALESCE(Qty, 0) FROM {TRADES_TABLE} "
        f"WHERE Code = ? AND Timestamp >= ? AND Timestamp < ? "
        f"AND Price IS NOT NULL AND TRY_CAST(Timestamp AS TIMESTAMP) IS NOT NULL "
        f"ORDER BY Timestamp LIMIT 1",
        [code, f"{day} {MORNING_START}", f"{day} {MORNING_END}"],
    ).fetchall()
    if not first:
        nxt = (Date.fromisoformat(day) + timedelta(days=1)).isoformat()
        any_row = con.execute(
            f"SELECT 1 FROM {TRADES_TABLE} WHERE Code = ? AND Timestamp >= ? "
            f"AND Timestamp < ? LIMIT 1",
            [code, day, nxt],
        ).fetchall()
        return _SessionRow(day, STATUS_NO_MORNING if any_row else STATUS_NO_DATA)

    ts_str, t_open_us, p_open, v_open = first[0]
    t_open_us = int(t_open_us)
    t_end_us = t_open_us + window_us
    end_dt = datetime.fromisoformat(str(ts_str).strip()) + timedelta(microseconds=window_us)
    cols = con.execute(
        f"SELECT epoch_us(TRY_CAST(Timestamp AS TIMESTAMP)) AS t, Price AS p, "
        f"COALESCE(Qty, 0) AS q FROM {TRADES_TABLE} "
        f"WHERE Code = ? AND Timestamp >= ? AND Timestamp <= ? "
        f"AND Price IS NOT NULL AND TRY_CAST(Timestamp AS TIMESTAMP) IS NOT NULL "
        f"ORDER BY Timestamp",
        [code, str(ts_str), end_dt.strftime("%Y-%m-%d %H:%M:%S.%f")],
    ).fetchnumpy()
    t = np.ma.getdata(cols["t"]).astype(np.int64)
    keep = (t >= t_open_us) & (t <= t_end_us)
    order = np.argsort(t[keep], kind="stable")
    ticks = np.empty(int(keep.sum()), dtype=TICK_DTYPE)
    ticks["t"] = t[keep][order]
    ticks["p"] = np.ma.getdata(cols["p"]).astype(np.float64)[keep][order]
    ticks["q"] = np.ma.getdata(cols["q"]).astype(np.int64)[keep][order]
    if len(ticks) == 0:  # defensive: cannot happen, first row is inside the window
        return _SessionRow(day, STATUS_NO_MORNING)
    return _SessionRow(
        day, STATUS_OK, "", t_open_us, float(p_open), int(v_open or 0), ticks
    )


def extract_sessions(
    path: Path | str,
    code: str,
    dates: Iterable[str],
    *,
    window_us: int = CACHE_WINDOW_US,
) -> list[_SessionRow]:
    """Open ``path`` read-only ONCE and extract every date; never raises per date."""
    days = list(dates)
    try:
        con = duckdb.connect(str(path), read_only=True)
    except Exception as exc:  # noqa: BLE001
        msg = f"open: {type(exc).__name__}: {exc}"[:200]
        return [_SessionRow(d, STATUS_ERROR, msg) for d in days]
    try:
        out: list[_SessionRow] = []
        for day in days:
            try:
                out.append(_extract_one(con, code, day, window_us))
            except Exception as exc:  # noqa: BLE001
                msg = f"{type(exc).__name__}: {exc}"[:200]
                out.append(_SessionRow(day, STATUS_ERROR, msg))
        return out
    finally:
        con.close()


# --------------------------------------------------------------------------- #
# Shards
# --------------------------------------------------------------------------- #
def _paths(cache_dir: Path, stem: str) -> tuple[Path, Path]:
    d = Path(cache_dir) / TICKS_SUBDIR
    return d / f"{stem}.ticks.npy", d / f"{stem}.index.npz"


@dataclass
class _Shard:
    stem: str
    code: str
    ticks: np.ndarray  # memory-mapped structured array
    rows: dict[str, tuple[str, str, int, int, int, float, int]]
    # date -> (status, msg, offset, length, t_open_us, p_open, v_open)


def _read_index(index_path: Path) -> tuple[str, dict[str, tuple[str, str, int, int, int, float, int]]]:
    with np.load(index_path, allow_pickle=False) as z:
        code = str(z["code"])
        rows = {
            str(d): (str(s), str(m), int(o), int(n), int(t), float(p), int(v))
            for d, s, m, o, n, t, p, v in zip(
                z["dates"], z["status"], z["msg"], z["offset"], z["length"],
                z["t_open_us"], z["p_open"], z["v_open"], strict=True,
            )
        }
    return code, rows


def _write_shard(
    cache_dir: Path,
    stem: str,
    code: str,
    rows: dict[str, tuple[str, str, int, int, int, float, int]],
    ticks: np.ndarray,
) -> None:
    ticks_path, index_path = _paths(cache_dir, stem)
    ticks_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_t = ticks_path.with_name(ticks_path.name + ".tmp")
    tmp_i = index_path.with_name(index_path.name + ".tmp")
    with open(tmp_t, "wb") as fh:
        np.save(fh, ticks, allow_pickle=False)
    dates = sorted(rows)
    cols = [rows[d] for d in dates]
    with open(tmp_i, "wb") as fh:
        np.savez(
            fh,
            code=np.array(code),
            dates=np.array(dates, dtype="U10"),
            status=np.array([c[0] for c in cols], dtype="U24"),
            msg=np.array([c[1] for c in cols], dtype="U200"),
            offset=np.array([c[2] for c in cols], dtype=np.int64),
            length=np.array([c[3] for c in cols], dtype=np.int64),
            t_open_us=np.array([c[4] for c in cols], dtype=np.int64),
            p_open=np.array([c[5] for c in cols], dtype=np.float64),
            v_open=np.array([c[6] for c in cols], dtype=np.int64),
        )
    os.replace(tmp_t, ticks_path)
    os.replace(tmp_i, index_path)  # index last = commit


def _merge_and_write(
    cache_dir: Path, stem: str, code: str, new: list[_SessionRow]
) -> Counter[str]:
    """Merge ``new`` session rows into the stem shard (rewriting it atomically)."""
    ticks_path, index_path = _paths(cache_dir, stem)
    old_rows: dict[str, tuple[str, str, int, int, int, float, int]] = {}
    old_ticks = np.empty(0, dtype=TICK_DTYPE)
    if index_path.is_file() and ticks_path.is_file():
        _, old_rows = _read_index(index_path)
        old_ticks = np.load(ticks_path)  # fully read: no live mmap at os.replace
    replaced = {r.date for r in new}
    chunks: list[np.ndarray] = []
    rows: dict[str, tuple[str, str, int, int, int, float, int]] = {}
    offset = 0
    for day in sorted(set(old_rows) | replaced):
        if day in replaced:
            continue
        status, msg, off, n, t, p, v = old_rows[day]
        if n:
            chunks.append(np.asarray(old_ticks[off : off + n]))
        rows[day] = (status, msg, offset, n, t, p, v)
        offset += n
    for r in sorted(new, key=lambda r: r.date):
        n = 0 if r.ticks is None else len(r.ticks)
        if n:
            assert r.ticks is not None
            chunks.append(r.ticks)
        rows[r.date] = (r.status, r.msg, offset, n, r.t_open_us, r.p_open, r.v_open)
        offset += n
    ticks = np.concatenate(chunks) if chunks else np.empty(0, dtype=TICK_DTYPE)
    del old_ticks
    _write_shard(cache_dir, stem, code, rows, ticks)
    return Counter(r.status for r in new)


def shard_final_dates(cache_dir: Path, stem: str) -> set[str]:
    """Dates of ``stem`` that are already final (skipped on rerun)."""
    ticks_path, index_path = _paths(cache_dir, stem)
    if not (index_path.is_file() and ticks_path.is_file()):
        return set()
    try:
        _, rows = _read_index(index_path)
    except Exception:  # noqa: BLE001 - corrupt index -> rebuild
        return set()
    return {d for d, row in rows.items() if row[0] in FINAL_STATUSES}


# --------------------------------------------------------------------------- #
# Build
# --------------------------------------------------------------------------- #
@dataclass
class BuildSummary:
    sessions_total: int
    sessions_extracted: int
    stems_total: int
    stems_skipped: int
    status: Counter[str]
    seconds: float


def build_cache(
    *,
    cache_dir: Path = DEFAULT_CACHE_DIR,
    trades_dir: Path = DEFAULT_TRADES_DIR,
    start: str | None = None,
    end: str | None = None,
    max_rank: int | None = None,
    workers: int = 4,
    window_us: int = CACHE_WINDOW_US,
    progress: Progress | None = None,
) -> BuildSummary:
    """Extract every missing universe session, one pass per stem file."""
    say = progress or (lambda _m: None)
    cache_dir = Path(cache_dir)
    clear_shard_cache()
    universe = load_universe(cache_dir, start, end)
    if max_rank is not None:
        universe = [r for r in universe if int(r["rank"]) <= max_rank]  # type: ignore[call-overload]
    by_stem: dict[str, tuple[str, set[str]]] = {}
    for r in universe:
        stem = str(r["stem"])
        code, days = by_stem.setdefault(stem, (str(r["code"]), set()))
        days.add(str(r["date"]))
    files = index_tick_files(trades_dir)

    todo: list[tuple[str, str, list[str]]] = []
    status: Counter[str] = Counter()
    skipped = 0
    for stem, (code, days) in by_stem.items():
        missing = sorted(days - shard_final_dates(cache_dir, stem))
        if not missing:
            skipped += 1
            continue
        todo.append((stem, code, missing))
    todo.sort(key=lambda x: -len(x[2]))  # biggest first -> better load balance
    total_sessions = sum(len(d) for _, d in by_stem.values())
    todo_sessions = sum(len(m) for _, _, m in todo)
    say(
        f"cache: {total_sessions} sessions over {len(by_stem)} stems; "
        f"{todo_sessions} sessions / {len(todo)} stems to extract "
        f"({skipped} stems already done), workers={workers}"
    )

    t0 = time.time()
    done_sessions = 0
    done_stems = 0
    lock = threading.Lock()

    def work(stem: str, code: str, days: list[str]) -> Counter[str]:
        path = files.get(stem)
        if path is None:
            rows = [_SessionRow(d, STATUS_NO_FILE) for d in days]
        else:
            rows = extract_sessions(path, code, days, window_us=window_us)
        return _merge_and_write(cache_dir, stem, code, rows)

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futs = {pool.submit(work, s, c, d): (s, len(d)) for s, c, d in todo}
        for fut in as_completed(futs):
            stem, n = futs[fut]
            try:
                counts = fut.result()
            except Exception as exc:  # noqa: BLE001 - shard write failure
                counts = Counter({STATUS_ERROR: n})
                say(f"  !! {stem}: shard write failed: {type(exc).__name__}: {exc}")
            with lock:
                status.update(counts)
                done_sessions += n
                done_stems += 1
                el = time.time() - t0
                say(
                    f"  [{done_stems}/{len(todo)}] {stem} +{n} "
                    f"({dict(counts)}) sessions {done_sessions}/{todo_sessions} "
                    f"{el:.0f}s ({el / max(done_sessions, 1):.2f}s/session wall)"
                )
    return BuildSummary(
        total_sessions, done_sessions, len(by_stem), skipped, status, time.time() - t0
    )


def cache_status_counts(
    cache_dir: Path = DEFAULT_CACHE_DIR,
    start: str | None = None,
    end: str | None = None,
    max_rank: int | None = None,
) -> Counter[str]:
    """Status of every universe row in range (``not_cached`` if never attempted)."""
    universe = load_universe(cache_dir, start, end)
    counts: Counter[str] = Counter()
    rows_by_stem: dict[str, dict[str, tuple[str, str, int, int, int, float, int]]] = {}
    for r in universe:
        if max_rank is not None and int(r["rank"]) > max_rank:  # type: ignore[call-overload]
            continue
        stem = str(r["stem"])
        if stem not in rows_by_stem:
            _, index_path = _paths(cache_dir, stem)
            rows_by_stem[stem] = _read_index(index_path)[1] if index_path.is_file() else {}
        row = rows_by_stem[stem].get(str(r["date"]))
        counts[row[0] if row else "not_cached"] += 1
    return counts


def cache_size_bytes(cache_dir: Path = DEFAULT_CACHE_DIR) -> int:
    d = Path(cache_dir) / TICKS_SUBDIR
    return sum(e.stat().st_size for e in os.scandir(d)) if d.is_dir() else 0


# --------------------------------------------------------------------------- #
# Read API
# --------------------------------------------------------------------------- #
_shards: dict[tuple[str, str], tuple[int, _Shard]] = {}
_shards_lock = threading.Lock()


def clear_shard_cache() -> None:
    """Drop open shard memory-maps (needed before a shard is rewritten)."""
    with _shards_lock:
        _shards.clear()


def _get_shard(cache_dir: Path, stem: str) -> _Shard:
    ticks_path, index_path = _paths(cache_dir, stem)
    try:
        mtime = index_path.stat().st_mtime_ns
    except FileNotFoundError:
        raise CachedSessionUnavailable(f"no cache shard for {stem}") from None
    key = (str(Path(cache_dir).resolve()), stem)
    with _shards_lock:
        hit = _shards.get(key)
        if hit is not None and hit[0] == mtime:
            return hit[1]
    code, rows = _read_index(index_path)
    try:
        ticks = np.load(ticks_path, mmap_mode="r")
    except ValueError:  # numpy cannot mmap a zero-length array
        ticks = np.load(ticks_path)
    shard = _Shard(stem, code, ticks, rows)
    with _shards_lock:
        _shards[key] = (mtime, shard)
    return shard


def load_cached_session(
    date: str | Date, stem: str, cache_dir: Path | str = DEFAULT_CACHE_DIR
) -> OpeningSessionData:
    """Return the cached session; raises :class:`CachedSessionUnavailable`."""
    day = date.isoformat() if isinstance(date, Date) else str(date)
    stem = stem.lower()
    shard = _get_shard(Path(cache_dir), stem)
    row = shard.rows.get(day)
    if row is None:
        raise CachedSessionUnavailable(f"{stem} {day}: not cached")
    status, msg, off, n, t_open_us, p_open, v_open = row
    if status != STATUS_OK:
        raise CachedSessionUnavailable(f"{stem} {day}: status={status} {msg}".strip())
    rec = shard.ticks[off : off + n]
    return OpeningSessionData(
        stem=stem,
        code=shard.code,
        date=day,
        t_open_us=t_open_us,
        p_open=p_open,
        v_open=v_open,
        epoch_us=np.ascontiguousarray(rec["t"]),
        price=np.ascontiguousarray(rec["p"]),
        qty=np.ascontiguousarray(rec["q"]),
        trade_type=np.full(n, "", dtype=object),
    )


def iter_cached_sessions(
    start: str | Date,
    end: str | Date,
    ranks: int | Iterable[int] | None = None,
    cache_dir: Path | str = DEFAULT_CACHE_DIR,
) -> Iterator[OpeningSessionData]:
    """Yield ``ok`` sessions for ``start <= date <= end`` ordered by (date, rank).

    ``ranks``: ``None`` = every universe rank, an ``int`` N = ranks 1..N, or an
    iterable of explicit ranks.  Sessions whose status is not ``ok`` are skipped
    (use :func:`cache_status_counts` to see them).
    """
    cache_dir = Path(cache_dir)
    if ranks is None:
        wanted: set[int] | None = None
    elif isinstance(ranks, int):
        wanted = set(range(1, ranks + 1))
    else:
        wanted = {int(r) for r in ranks}
    for row in load_universe(cache_dir, start, end):
        if wanted is not None and int(row["rank"]) not in wanted:  # type: ignore[call-overload]
            continue
        try:
            yield load_cached_session(str(row["date"]), str(row["stem"]), cache_dir)
        except CachedSessionUnavailable:
            continue
