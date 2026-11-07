"""Point-in-time daily trading universe (top-N by PREVIOUS day's turnover).

For every trading date ``D`` the universe is the ``top_n`` individual stocks
ranked by ``TurnoverValue`` on the previous trading day ``P(D)``.  Nothing from
``D`` itself is used, and delisted symbols are kept (whatever has a row on
``P(D)`` is ranked), so there is no survivorship filtering.

Calendar
    ``P(D)`` is the latest date ``< D`` in the *daily calendar*, defined as the
    union of dates over all ``stocks_daily`` files that carry at least
    ``MIN_CALENDAR_BREADTH`` symbols.  A union (not a single reference stem) is
    used so a halt of any one liquid stock cannot punch a hole in the calendar;
    the breadth floor ignores stray rows on non-trading days.

Funds
    ETFs/ETNs/REITs/infrastructure funds are identified from J-Quants listed
    info: ``MarketCode == '0109'`` ("その他" market), which holds exactly the
    funds (``Sector33Code == '9999'``).  The point-in-time monthly snapshot
    (``listed_info_pit.duckdb``, newest snapshot ``<= P(D)``) is used; see
    :func:`build_universe` for the fallbacks.

Tick size
    :func:`tick_size` implements the TSE price-tick tables (general table vs
    the TOPIX100 table).
"""

from __future__ import annotations

import os
import shutil
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date as Date
from pathlib import Path

import duckdb
import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CACHE_DIR = REPO_ROOT / ".cache" / "trading_rl"
DEFAULT_DAILY_DIR = Path("S:/jp/stocks_daily")
DEFAULT_TRADES_DIR = Path("S:/jp/stocks_trades")
DEFAULT_LISTED_PIT = Path("S:/jp/listed_info_pit.duckdb")
DEFAULT_LISTED = Path("S:/jp/listed_info.duckdb")

PANEL_FILE = "daily_panel.parquet"
UNIVERSE_FILE = "universe.parquet"
AUDIT_FILE = "universe_audit.parquet"

# A date only counts as a trading day if at least this many symbols have a bar.
MIN_CALENDAR_BREADTH = 500
# Daily history loaded before ``--start`` so avg_volume_20d is defined from day 1.
PANEL_LOOKBACK_START = "2023-09-01"
VOLUME_AVG_DAYS = 20
FUND_MARKET_CODE = "0109"
TOPIX100_SCALES = ("TOPIX Core30", "TOPIX Large70")

Progress = Callable[[str], None]


# --------------------------------------------------------------------------- #
# TSE tick size
# --------------------------------------------------------------------------- #
# (upper bound inclusive, tick).  Price <= bound -> tick.  Final entry is open.
# General table ("その他の銘柄").
_TICK_GENERAL: tuple[tuple[float, float], ...] = (
    (3_000, 1),
    (5_000, 5),
    (30_000, 10),
    (50_000, 50),
    (300_000, 100),
    (500_000, 500),
    (3_000_000, 1_000),
    (5_000_000, 5_000),
    (30_000_000, 10_000),
    (50_000_000, 50_000),
    (float("inf"), 100_000),
)
# TOPIX100 constituents table (TOPIX Core30 + Large70).  The tiers above
# 10,000,000 yen are irrelevant for equities and given best effort.
_TICK_TOPIX100: tuple[tuple[float, float], ...] = (
    (1_000, 0.1),
    (3_000, 0.5),
    (10_000, 1),
    (30_000, 5),
    (100_000, 10),
    (300_000, 50),
    (1_000_000, 100),
    (3_000_000, 500),
    (10_000_000, 1_000),
    (20_000_000, 5_000),
    (30_000_000, 10_000),
    (100_000_000, 50_000),
    (float("inf"), 100_000),
)


def _table_arrays(
    table: tuple[tuple[float, float], ...],
) -> tuple[np.ndarray, np.ndarray]:
    return (
        np.array([b for b, _ in table], dtype=np.float64),
        np.array([t for _, t in table], dtype=np.float64),
    )


_GENERAL_BOUNDS, _GENERAL_TICKS = _table_arrays(_TICK_GENERAL)
_TOPIX100_BOUNDS, _TOPIX100_TICKS = _table_arrays(_TICK_TOPIX100)


def tick_size(price: float, is_topix100: bool) -> float:
    """TSE price tick (yen) at ``price``; bounds are inclusive ("以下")."""
    if not (price > 0) or price != price:
        raise ValueError(f"price must be positive, got {price!r}")
    bounds, ticks = (
        (_TOPIX100_BOUNDS, _TOPIX100_TICKS)
        if is_topix100
        else (_GENERAL_BOUNDS, _GENERAL_TICKS)
    )
    return float(ticks[int(np.searchsorted(bounds, price, side="left"))])


def _tick_sql(price: str, flag: str) -> str:
    """SQL CASE mirroring :func:`tick_size` (single source: the tick tables)."""

    def branch(table: tuple[tuple[float, float], ...]) -> str:
        whens = " ".join(
            f"WHEN {price} <= {b!r} THEN {t!r}" for b, t in table if b != float("inf")
        )
        return f"CASE {whens} ELSE {table[-1][1]!r} END"

    return (
        f"CASE WHEN {price} IS NULL OR {price} <= 0 THEN NULL "
        f"WHEN {flag} THEN {branch(_TICK_TOPIX100)} ELSE {branch(_TICK_GENERAL)} END"
    )


def tick_sizes(prices: np.ndarray, is_topix100: np.ndarray) -> np.ndarray:
    """Vectorised :func:`tick_size`; non-positive / NaN prices give NaN."""
    p = np.asarray(prices, dtype=np.float64)
    t100 = np.asarray(is_topix100, dtype=bool)
    valid = np.isfinite(p) & (p > 0)
    safe = np.where(valid, p, 1.0)
    general = _GENERAL_TICKS[np.searchsorted(_GENERAL_BOUNDS, safe, side="left")]
    topix = _TOPIX100_TICKS[np.searchsorted(_TOPIX100_BOUNDS, safe, side="left")]
    out = np.where(t100, topix, general)
    return np.where(valid, out, np.nan)


# --------------------------------------------------------------------------- #
# Daily panel (one pass over ~5,100 stocks_daily files)
# --------------------------------------------------------------------------- #
def _sql_str(value: object) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _scan_one_daily(
    path: Path, out_path: Path, since: str
) -> tuple[str, int, str | None]:
    stem = path.stem.lower()
    con = duckdb.connect(":memory:")
    try:
        con.execute(f"ATTACH {_sql_str(path.as_posix())} AS src (READ_ONLY)")
        con.execute(
            f"COPY (SELECT {_sql_str(stem)} AS stem, CAST(Code AS VARCHAR) AS code, "
            f"CAST(CAST(Date AS DATE) AS VARCHAR) AS d, "
            f"CAST(Close AS DOUBLE) AS close, CAST(Volume AS DOUBLE) AS volume, "
            f"CAST(TurnoverValue AS DOUBLE) AS tv "
            f"FROM src.stocks_daily WHERE CAST(Date AS DATE) >= DATE {_sql_str(since)}"
            f") TO {_sql_str(out_path.as_posix())} (FORMAT PARQUET)"
        )
        n = con.execute(
            f"SELECT count(*) FROM read_parquet({_sql_str(out_path.as_posix())})"
        ).fetchone()
        return stem, int(n[0]) if n else 0, None
    except Exception as exc:  # noqa: BLE001 - recorded, never silently dropped
        return stem, 0, f"{type(exc).__name__}: {exc}"[:300]
    finally:
        con.close()


def ensure_daily_panel(
    daily_dir: Path = DEFAULT_DAILY_DIR,
    cache_dir: Path = DEFAULT_CACHE_DIR,
    *,
    since: str = PANEL_LOOKBACK_START,
    workers: int = 8,
    refresh: bool = False,
    progress: Progress | None = None,
) -> Path:
    """Copy (Code, Date, Close, Volume, TurnoverValue) of every daily file locally.

    One pass over the network drive; the consolidated parquet is reused on later
    runs unless ``refresh`` is set.
    """
    say = progress or (lambda _m: None)
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    panel = cache_dir / PANEL_FILE
    if panel.exists() and not refresh:
        say(f"daily panel: reuse {panel}")
        return panel

    files = sorted(
        Path(e.path)
        for e in os.scandir(daily_dir)
        if e.name.endswith(".duckdb") and e.name != "mother.duckdb"
    )
    tmp_dir = cache_dir / "_daily_parts"
    shutil.rmtree(tmp_dir, ignore_errors=True)
    tmp_dir.mkdir(parents=True)
    say(f"daily panel: scanning {len(files)} files with {workers} workers")
    t0 = time.time()
    errors: list[tuple[str, str]] = []
    rows = 0
    lock = threading.Lock()
    done = 0

    def work(path: Path) -> None:
        nonlocal rows, done
        stem, n, err = _scan_one_daily(path, tmp_dir / f"{path.stem.lower()}.parquet", since)
        with lock:
            rows += n
            done += 1
            if err:
                errors.append((stem, err))
            if done % 500 == 0:
                say(f"  daily {done}/{len(files)} rows={rows} {time.time() - t0:.0f}s")

    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(work, files))

    con = duckdb.connect(":memory:")
    try:
        tmp_out = panel.with_suffix(".tmp.parquet")
        con.execute(
            f"COPY (SELECT * FROM read_parquet({_sql_str((tmp_dir / '*.parquet').as_posix())})) "
            f"TO {_sql_str(tmp_out.as_posix())} (FORMAT PARQUET)"
        )
    finally:
        con.close()
    os.replace(tmp_out, panel)
    shutil.rmtree(tmp_dir, ignore_errors=True)
    if errors:
        (cache_dir / "daily_panel_errors.txt").write_text(
            "\n".join(f"{s}\t{e}" for s, e in errors), encoding="utf-8"
        )
    say(
        f"daily panel: {rows} rows, {len(files) - len(errors)} ok, "
        f"{len(errors)} errors, {time.time() - t0:.0f}s"
    )
    return panel


# --------------------------------------------------------------------------- #
# Universe
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class UniverseResult:
    path: Path
    audit_path: Path
    n_dates: int
    n_rows: int
    first_date: str
    last_date: str


def last_tick_date(
    trades_dir: Path = DEFAULT_TRADES_DIR,
    reference_stems: tuple[str, ...] = ("7203", "8306", "9984", "6758", "9983"),
) -> str:
    """Last date with tick data = max ``to_timestamp`` over liquid reference stems."""
    best: str | None = None
    for stem in reference_stems:
        path = Path(trades_dir) / f"{stem}.duckdb"
        if not path.is_file():
            continue
        try:
            con = duckdb.connect(str(path), read_only=True)
            try:
                row = con.execute(
                    "SELECT CAST(CAST(max(to_timestamp) AS DATE) AS VARCHAR) "
                    "FROM stocks_board_metadata"
                ).fetchone()
            finally:
                con.close()
        except duckdb.Error:
            continue
        if row and row[0] and (best is None or row[0] > best):
            best = row[0]
    if best is None:
        raise RuntimeError("could not determine last tick date from reference stems")
    return best


def _universe_sql(include_funds: bool, min_breadth: int) -> str:
    fund_filter = "" if include_funds else "AND NOT is_fund"
    return f"""
CREATE TEMP TABLE panel AS
-- older rows carry 4-digit codes ('7203'); listing/ticks use 5-digit ('72030')
SELECT stem,
       CASE WHEN length(code) = 4 THEN code || '0' ELSE code END AS code,
       CAST(d AS DATE) AS d, close, volume, tv
FROM read_parquet({{panel}});

CREATE TEMP TABLE cal AS
SELECT d, row_number() OVER (ORDER BY d) AS ci,
       lag(d) OVER (ORDER BY d) AS prev_d
FROM (SELECT d FROM panel WHERE close IS NOT NULL
      GROUP BY d HAVING count(*) >= {min_breadth});

CREATE TEMP TABLE stats AS
SELECT p.stem, p.code, c.d, c.ci, p.close, p.volume, p.tv,
  avg(p.volume) OVER w AS avg_vol,
  count(p.volume) OVER w AS n_vol
FROM panel p JOIN cal c ON c.d = p.d
WINDOW w AS (PARTITION BY p.stem ORDER BY c.ci
             RANGE BETWEEN {VOLUME_AVG_DAYS - 1} PRECEDING AND CURRENT ROW);

CREATE TEMP TABLE targets AS
SELECT d, prev_d FROM cal
WHERE prev_d IS NOT NULL AND d >= DATE {{start}} AND d <= DATE {{end}};

CREATE TEMP TABLE snaps AS SELECT DISTINCT snap FROM listing;

-- newest snapshot <= P(D); before the first snapshot fall back to the earliest.
CREATE TEMP TABLE target_snap AS
SELECT t.d, t.prev_d, coalesce(s.snap, (SELECT min(snap) FROM snaps)) AS snap
FROM targets t ASOF LEFT JOIN snaps s ON t.prev_d >= s.snap;

-- fallback: latest snapshot that contains the code at all
CREATE TEMP TABLE listing_latest AS
SELECT code5, arg_max(market_code, snap) AS market_code,
       arg_max(scale, snap) AS scale, max(snap) AS snap
FROM listing GROUP BY code5;

CREATE TEMP TABLE cand AS
SELECT ts.d AS date, ts.prev_d, s.stem, s.code,
  s.tv AS prev_turnover_value, s.close AS prev_close, s.volume AS prev_volume,
  s.avg_vol AS avg_volume_20d, s.n_vol AS n_days_20d,
  CASE WHEN l.code5 IS NOT NULL THEN 'pit'
       WHEN f.code5 IS NOT NULL THEN 'latest_other'
       ELSE 'unknown' END AS listing_source,
  coalesce(l.market_code, f.market_code) AS market_code,
  coalesce(l.scale, f.scale) AS scale_category,
  coalesce(l.snap, f.snap) AS listing_snapshot
FROM target_snap ts
JOIN stats s ON s.d = ts.prev_d AND s.tv IS NOT NULL AND s.tv > 0
LEFT JOIN listing l ON l.snap = ts.snap AND l.code5 = s.code
LEFT JOIN listing_latest f ON f.code5 = s.code;

CREATE TEMP TABLE cand2 AS
SELECT *,
  CASE WHEN listing_source = 'unknown'
       THEN coalesce(
         TRY_CAST(substr(code, 1, 4) AS INTEGER) BETWEEN 1300 AND 1699
         OR TRY_CAST(substr(code, 1, 4) AS INTEGER) BETWEEN 2500 AND 2599, false)
       ELSE coalesce(market_code = '{FUND_MARKET_CODE}', false) END AS is_fund,
  coalesce(scale_category IN ('TOPIX Core30', 'TOPIX Large70'), false)
    AS is_topix100
FROM cand;

CREATE TEMP TABLE ranked AS
SELECT *, row_number() OVER (
    PARTITION BY date ORDER BY prev_turnover_value DESC, stem) AS raw_rank
FROM cand2;

CREATE TEMP TABLE final AS
SELECT *, row_number() OVER (
    PARTITION BY date ORDER BY prev_turnover_value DESC, stem) AS rank
FROM ranked WHERE true {fund_filter};
"""


def build_universe(
    *,
    start: str,
    end: str | None = None,
    top_n: int = 50,
    include_funds: bool = False,
    daily_dir: Path = DEFAULT_DAILY_DIR,
    trades_dir: Path = DEFAULT_TRADES_DIR,
    listed_pit_path: Path = DEFAULT_LISTED_PIT,
    listed_path: Path = DEFAULT_LISTED,
    cache_dir: Path = DEFAULT_CACHE_DIR,
    workers: int = 8,
    refresh_panel: bool = False,
    min_breadth: int = MIN_CALENDAR_BREADTH,
    progress: Progress | None = None,
) -> UniverseResult:
    """Build and persist ``universe.parquet`` (+ ``universe_audit.parquet``).

    ``end`` defaults to the last tick date.  Listing lookup order per
    (date, symbol): newest PIT snapshot ``<= P(D)`` (earliest snapshot if none
    precedes); else the symbol's latest snapshot in any file (``latest_other``);
    else ``unknown`` -> ETF code ranges (1300-1699, 2500-2599) count as funds
    and the symbol is non-TOPIX100.
    """
    say = progress or (lambda _m: None)
    cache_dir = Path(cache_dir)
    end = end or last_tick_date(trades_dir)
    panel = ensure_daily_panel(
        daily_dir, cache_dir, workers=workers, refresh=refresh_panel, progress=say
    )
    con = duckdb.connect(":memory:")
    try:
        # panel must reach P(end): rescan once if it is stale.
        pmax = con.execute(
            f"SELECT CAST(max(d) AS VARCHAR) FROM read_parquet({_sql_str(panel.as_posix())})"
        ).fetchone()
        if pmax and pmax[0] and str(pmax[0]) < end and not refresh_panel:
            say(f"daily panel ends {pmax[0]} < {end}: refreshing")
            panel = ensure_daily_panel(
                daily_dir, cache_dir, workers=workers, refresh=True, progress=say
            )
        _load_listing(con, listed_pit_path, listed_path)
        script = _universe_sql(include_funds, min_breadth)
        script = script.replace("{panel}", _sql_str(panel.as_posix()))
        script = script.replace("{start}", _sql_str(start))
        script = script.replace("{end}", _sql_str(end))
        con.execute(script)

        con.execute(
            "CREATE TABLE out AS SELECT date, CAST(rank AS INTEGER) AS rank, stem, code, "
            "prev_d AS prev_date, prev_turnover_value, prev_close, prev_volume, "
            "avg_volume_20d, CAST(n_days_20d AS INTEGER) AS n_days_20d, is_topix100, "
            "is_fund, listing_source, market_code, scale_category, "
            "listing_snapshot, "
            f"CAST({_tick_sql('prev_close', 'is_topix100')} AS DOUBLE) AS tick_size, "
            "CASE WHEN is_topix100 THEN 'topix100' ELSE 'general' END AS tick_table "
            f"FROM final WHERE rank <= {int(top_n)}"
        )
        out_path = cache_dir / UNIVERSE_FILE
        audit_path = cache_dir / AUDIT_FILE
        tmp = out_path.with_suffix(".tmp.parquet")
        con.execute(
            f"COPY (SELECT * FROM out ORDER BY date, rank) TO "
            f"{_sql_str(tmp.as_posix())} (FORMAT PARQUET)"
        )
        os.replace(tmp, out_path)
        # funds that would have been in the unfiltered top-N, per date
        con.execute(
            f"COPY (SELECT date, "
            f"count(*) FILTER (WHERE is_fund AND raw_rank <= {int(top_n)}) "
            f"AS funds_in_raw_topn, "
            f"count(*) FILTER (WHERE listing_source = 'unknown' AND raw_rank <= {int(top_n)}) "
            f"AS unknown_listing_in_raw_topn, "
            f"count(*) FILTER (WHERE listing_source = 'latest_other' AND raw_rank <= {int(top_n)}) "
            f"AS latest_other_in_raw_topn "
            f"FROM ranked GROUP BY date ORDER BY date) TO "
            f"{_sql_str(audit_path.as_posix())} (FORMAT PARQUET)"
        )
        stat = con.execute(
            "SELECT count(DISTINCT date), count(*), CAST(min(date) AS VARCHAR), "
            "CAST(max(date) AS VARCHAR) FROM out"
        ).fetchone()
    finally:
        con.close()
    assert stat is not None
    say(f"universe: {stat[0]} dates, {stat[1]} rows, {stat[2]}..{stat[3]} -> {out_path}")
    return UniverseResult(out_path, audit_path, int(stat[0]), int(stat[1]), stat[2], stat[3])


def _load_listing(
    con: duckdb.DuckDBPyConnection, pit_path: Path, listed_path: Path
) -> None:
    """Create TEMP table ``listing(snap, code5, market_code, scale)``."""
    con.execute(
        f"ATTACH {_sql_str(Path(pit_path).as_posix())} AS pit (READ_ONLY)"
    )
    con.execute(
        "CREATE TEMP TABLE listing AS "
        "SELECT CAST(Date AS DATE) AS snap, Code AS code5, MarketCode AS market_code, "
        "ScaleCategory AS scale FROM pit.listed_info"
    )
    if Path(listed_path).is_file():
        # recent daily snapshots (4-digit codes) extend the PIT file's last month
        con.execute(
            f"ATTACH {_sql_str(Path(listed_path).as_posix())} AS cur (READ_ONLY)"
        )
        con.execute(
            "INSERT INTO listing SELECT CAST(Date AS DATE), "
            "CASE WHEN length(Code) = 4 THEN Code || '0' ELSE Code END, "
            "MarketCode, ScaleCategory FROM cur.listed_info"
        )


def load_universe(
    cache_dir: Path = DEFAULT_CACHE_DIR,
    start: str | Date | None = None,
    end: str | Date | None = None,
) -> list[dict[str, object]]:
    """Read ``universe.parquet`` rows (dicts) for ``start <= date <= end``."""
    path = Path(cache_dir) / UNIVERSE_FILE
    con = duckdb.connect(":memory:")
    try:
        where, params = [], []
        if start is not None:
            where.append("date >= CAST(? AS DATE)")
            params.append(str(start))
        if end is not None:
            where.append("date <= CAST(? AS DATE)")
            params.append(str(end))
        sql = f"SELECT * FROM read_parquet({_sql_str(path.as_posix())})"
        if where:
            sql += " WHERE " + " AND ".join(where)
        cur = con.execute(sql + " ORDER BY date, rank", params)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]
    finally:
        con.close()
