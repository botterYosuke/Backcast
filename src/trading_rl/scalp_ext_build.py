"""One-time builders of the ``<cache_dir>/ext`` caches used by feature groups K-Q.

Read-only on ``S:`` (J-Quants bulk CSVs, TDnet index JSON, ETF tick DuckDB files) and
on the existing caches; writes only NEW files under ``<cache_dir>/ext``.  See
:mod:`trading_rl.scalp_features_ext` for the schemas.  Run through
``scripts/build_scalp_ext_cache.py``.
"""

from __future__ import annotations

import csv
import json
import re
import time
from collections.abc import Callable
from pathlib import Path

import duckdb
import numpy as np

from .scalp_features_ext import (
    DAILY_ADJ_FILE,
    DISC_FILE,
    ETF_GRID_LEN,
    ETF_STEMS,
    ETF_SUBDIR,
    EXT_DIR,
    PEER_SUBDIR,
    PREVDAY_FILE,
    SCHED_FILE,
    TDNET_DAYS_FILE,
    TDNET_END,
    TOPIX_FILE,
    _sql_str,
    day_start_us,
)

BARS_DIR = Path("S:/j-quants")
TDNET_DIR = Path("S:/tdnet/yanoshin_index")
TRADES_DIR = Path("S:/jp/stocks_trades")
PANEL_START = "2023-09-01"
PANEL_END = "2026-08-31"
THREADS = 2

Progress = Callable[[str], None]


def _say(msg: str) -> None:
    print(msg, flush=True)


def lower_priority() -> None:
    """Run below normal priority so concurrent training is not starved (Windows)."""
    try:
        import ctypes

        k32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        k32.SetPriorityClass(k32.GetCurrentProcess(), 0x00004000)  # BELOW_NORMAL
    except Exception:  # noqa: BLE001
        pass


def _con() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(":memory:")
    con.execute(f"PRAGMA threads={THREADS}")
    return con


def ext_dir(cache_dir: Path | str) -> Path:
    d = Path(cache_dir) / EXT_DIR
    d.mkdir(parents=True, exist_ok=True)
    return d


def _atomic_copy(con: duckdb.DuckDBPyConnection, select: str, path: Path) -> None:
    tmp = path.with_suffix(".tmp.parquet")
    con.execute(f"COPY ({select}) TO {_sql_str(tmp.as_posix())} (FORMAT parquet)")
    tmp.replace(path)


# --------------------------------------------------------------------------- #
# daily bars / TOPIX
# --------------------------------------------------------------------------- #
def build_daily_adj(cache_dir: Path | str, bars_dir: Path = BARS_DIR) -> Path:
    out = ext_dir(cache_dir) / DAILY_ADJ_FILE
    con = _con()
    try:
        pattern = (bars_dir / "equities_bars_daily_*.csv.gz").as_posix()
        _atomic_copy(
            con,
            "SELECT DISTINCT ON (d, stem) lower(substr(Code, 1, 4)) AS stem, "
            "CAST(Date AS DATE) AS d, O AS o, H AS h, L AS l, C AS c, Vo AS vo, "
            "Va AS va, AdjFactor AS adjf "
            f"FROM read_csv({_sql_str(pattern)}, header=true, union_by_name=true, "
            "types={'Code': 'VARCHAR', 'O': 'DOUBLE', 'H': 'DOUBLE', 'L': 'DOUBLE', "
            "'C': 'DOUBLE', 'Vo': 'DOUBLE', 'Va': 'DOUBLE', 'AdjFactor': 'DOUBLE'}) "
            "WHERE length(Code) = 5 AND substr(Code, 5, 1) = '0' "
            f"AND CAST(Date AS DATE) >= DATE {_sql_str(PANEL_START)} "
            f"AND CAST(Date AS DATE) <= DATE {_sql_str(PANEL_END)} "
            "ORDER BY stem, d",
            out,
        )
    finally:
        con.close()
    return out


def build_topix(cache_dir: Path | str, bars_dir: Path = BARS_DIR) -> Path:
    out = ext_dir(cache_dir) / TOPIX_FILE
    con = _con()
    try:
        pattern = (bars_dir / "indices_bars_daily_topix_*.csv.gz").as_posix()
        _atomic_copy(
            con,
            "SELECT DISTINCT ON (d) CAST(Date AS DATE) AS d, O AS o, H AS h, L AS l, C AS c "
            f"FROM read_csv({_sql_str(pattern)}, header=true, union_by_name=true) "
            f"WHERE CAST(Date AS DATE) >= DATE {_sql_str('2021-01-01')} "
            f"AND CAST(Date AS DATE) <= DATE {_sql_str(PANEL_END)} ORDER BY d",
            out,
        )
    finally:
        con.close()
    return out


# --------------------------------------------------------------------------- #
# previous-day close flow (minute bars)
# --------------------------------------------------------------------------- #
def build_prevday(
    cache_dir: Path | str,
    stems: list[str],
    bars_dir: Path = BARS_DIR,
    progress: Progress = _say,
) -> Path:
    """Per (stem, date): last-30-min return, close vs day VWAP, last-30-min volume share.

    Last-30-min window = bars with time >= close - 30 min by wall clock (15:30 close
    from 2024-11-05, 15:00 before); the closing-auction bar belongs to the window."""
    out = ext_dir(cache_dir) / PREVDAY_FILE
    files = sorted(bars_dir.glob("equities_bars_minute_*.csv.gz"))
    codes = ",".join(_sql_str(s + "0") for s in sorted(set(stems)))
    con = _con()
    try:
        con.execute(
            "CREATE TABLE pd (stem VARCHAR, d DATE, last30_ret_bps DOUBLE, "
            "vwap_dev_bps DOUBLE, last30_share DOUBLE, vol DOUBLE)"
        )
        for f in files:
            t0 = time.time()
            con.execute(
                "INSERT INTO pd "
                "WITH b AS (SELECT lower(substr(Code, 1, 4)) AS stem, CAST(Date AS DATE) AS d, "
                "Time AS t, O, C, Vo, Va FROM read_csv("
                f"{_sql_str(f.as_posix())}, header=true, types={{'Code': 'VARCHAR'}}) "
                f"WHERE lower(Code) IN ({codes})), "
                "w AS (SELECT *, CASE WHEN d >= DATE '2024-11-05' THEN TIME '15:00:00' "
                "ELSE TIME '14:30:00' END AS w0 FROM b) "
                "SELECT stem, d, "
                "(arg_max(C, t) / arg_min(O, t) FILTER (WHERE t >= w0) - 1) * 1e4, "
                "(arg_max(C, t) / (sum(Va) / nullif(sum(Vo), 0)) - 1) * 1e4, "
                "sum(Vo) FILTER (WHERE t >= w0) / nullif(sum(Vo), 0), "
                "sum(Vo) FROM w GROUP BY stem, d"
            )
            progress(f"  prevday {f.name} {time.time() - t0:.0f}s")
        _atomic_copy(
            con, "SELECT DISTINCT ON (stem, d) * FROM pd ORDER BY stem, d, vol DESC", out
        )
    finally:
        con.close()
    return out


# --------------------------------------------------------------------------- #
# disclosures
# --------------------------------------------------------------------------- #
_RX = {
    "kessan": re.compile(r"決算"),
    "up": re.compile(r"上方修正|増額修正|上振れ"),
    "down": re.compile(r"下方修正|減額修正|下振れ"),
    "buyback": re.compile(r"自己株式|自社株|自己投資口"),
    "dividend": re.compile(r"配当|増配|減配|剰余金"),
}


def classify_title(title: str) -> tuple[int, int, int, int, int, int]:
    """(kessan, up, down, buyback, dividend, other) flags of a TDnet title."""
    f = [int(bool(_RX[k].search(title))) for k in ("kessan", "up", "down", "buyback", "dividend")]
    return (*f, int(not any(f)))  # type: ignore[return-value]


def _stem_of(code: str) -> str | None:
    code = str(code)
    if len(code) == 5 and code[4] == "0":
        return code[:4].lower()
    return None


def build_disclosures(
    cache_dir: Path | str,
    bars_dir: Path = BARS_DIR,
    tdnet_dir: Path = TDNET_DIR,
    progress: Progress = _say,
) -> list[Path]:
    """``disclosures.parquet`` (TDnet titles + J-Quants fins_summary), ``earn_sched.parquet``
    (fins_earnings-date) and ``tdnet_days.parquet`` (dates that have a TDnet index file)."""
    d = ext_dir(cache_dir)
    rows: list[tuple] = []
    days: list[str] = []
    for f in sorted(tdnet_dir.glob("2*.json")):
        m = re.fullmatch(r"(\d{4})(\d{2})(\d{2})\.json", f.name)
        if not m:
            continue
        day = f"{m[1]}-{m[2]}-{m[3]}"
        if day < "2023-09-01" or day > TDNET_END:
            continue
        try:
            doc = json.loads(f.read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            progress(f"  skip {f.name}: {type(e).__name__}")
            continue
        days.append(day)
        for it in doc.get("items", []):
            t = it.get("Tdnet") or {}
            stem = _stem_of(t.get("company_code", ""))
            pub = str(t.get("pubdate", ""))
            if stem is None or len(pub) < 16:
                continue
            title = str(t.get("title", ""))
            ts = int(np.datetime64(pub.replace(" ", "T")[:19], "us").astype("int64"))
            k, u, dn, b, dv, o = classify_title(title)
            title = " ".join(title.split())[:200]  # one line per row
            rows.append((stem, ts, "tdnet", k, u, dn, b, dv, o, 0, None, None, 0, title))
    progress(f"  tdnet: {len(rows)} items on {len(days)} days")
    con = _con()
    try:
        con.execute(
            "CREATE TABLE disc (stem VARCHAR, ts BIGINT, src VARCHAR, is_kessan TINYINT, "
            "is_up TINYINT, is_down TINYINT, is_buyback TINYINT, is_dividend TINYINT, "
            "is_other TINYINT, is_fs TINYINT, rev_pct DOUBLE, surprise DOUBLE, "
            "is_rev TINYINT, title VARCHAR)"
        )
        if rows:  # bulk load through a temp CSV (executemany is minutes for 200k rows)
            tmp_csv = d / "tdnet_tmp.csv"
            with tmp_csv.open("w", encoding="utf-8", newline="") as fh:
                w = csv.writer(fh, quoting=csv.QUOTE_MINIMAL, lineterminator="\n")
                w.writerows([[("" if v is None else v) for v in r] for r in rows])
            con.execute(
                "INSERT INTO disc SELECT * FROM read_csv("
                f"{_sql_str(tmp_csv.as_posix())}, header=false, quote='\"', escape='\"', "
                "delim=',', auto_detect=false, columns={'stem': 'VARCHAR', 'ts': 'BIGINT', 'src': 'VARCHAR', "
                "'is_kessan': 'TINYINT', 'is_up': 'TINYINT', 'is_down': 'TINYINT', "
                "'is_buyback': 'TINYINT', 'is_dividend': 'TINYINT', 'is_other': 'TINYINT', "
                "'is_fs': 'TINYINT', 'rev_pct': 'DOUBLE', 'surprise': 'DOUBLE', "
                "'is_rev': 'TINYINT', 'title': 'VARCHAR'})"
            )
            tmp_csv.unlink()
        pat = (bars_dir / "fins_summary_*.csv.gz").as_posix()
        con.execute(
            "CREATE TEMP TABLE fs AS SELECT lower(substr(Code, 1, 4)) AS stem, "
            "epoch_us(CAST(DiscDate AS DATE) + CAST(DiscTime AS TIME)) AS ts, "
            "DocType AS doc, CurPerType AS per, CAST(CurFYEn AS DATE) AS fye, "
            "CAST(NxtFYEn AS DATE) AS nfye, OP AS op, FOP AS fop, NxFOP AS nfop, "
            "(DocType LIKE '%NonConsolidated%') AS nc "
            f"FROM read_csv({_sql_str(pat)}, header=true, union_by_name=true, "
            "types={'Code': 'VARCHAR', 'DiscNo': 'VARCHAR', 'OP': 'DOUBLE', 'FOP': 'DOUBLE', "
            "'NxFOP': 'DOUBLE'}) "
            "WHERE length(Code) = 5 AND substr(Code, 5, 1) = '0'"
        )
        # forecast tuples: (stem, nc, target fiscal-year end, forecast OP, ts)
        con.execute(
            "CREATE TEMP TABLE fc AS "
            "SELECT stem, nc, fye AS tgt, fop AS f, ts, doc FROM fs "
            "WHERE fop IS NOT NULL AND fye IS NOT NULL "
            "UNION ALL SELECT stem, nc, nfye, nfop, ts, doc FROM fs "
            "WHERE nfop IS NOT NULL AND nfye IS NOT NULL"
        )
        con.execute(
            "CREATE TEMP TABLE fcr AS SELECT *, "
            "last_value(f IGNORE NULLS) OVER (PARTITION BY stem, nc, tgt ORDER BY ts "
            "ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING) AS f_prev FROM fc"
        )
        # one row per (stem, ts): revision of the forecast vs the previous one (same FY)
        con.execute(
            "CREATE TEMP TABLE rev AS SELECT stem, ts, "
            "max_by((f - f_prev) / abs(f_prev), tgt) AS rev_pct, "
            "bool_or(doc LIKE '%EarnForecastRevision') AS is_rev "
            "FROM fcr WHERE f_prev IS NOT NULL AND f_prev <> 0 GROUP BY stem, ts"
        )
        # FY actual OP vs the latest earlier forecast of that fiscal year
        con.execute(
            "CREATE TEMP TABLE act AS SELECT a.stem, a.ts, "
            "max((a.op - c.f) / abs(c.f)) AS surprise "
            "FROM (SELECT * FROM fs WHERE per = 'FY' AND op IS NOT NULL) a "
            "ASOF JOIN fc c ON c.stem = a.stem AND c.nc = a.nc AND c.tgt = a.fye "
            "AND a.ts > c.ts WHERE c.f <> 0 GROUP BY a.stem, a.ts"
        )
        con.execute(
            "INSERT INTO disc SELECT s.stem, s.ts, 'fins', 0, 0, 0, 0, 0, 0, "
            "max(CASE WHEN s.doc LIKE '%FinancialStatements%' THEN 1 ELSE 0 END), "
            "max(r.rev_pct), max(a.surprise), "
            "max(CASE WHEN coalesce(r.is_rev, false) THEN 1 ELSE 0 END), '' "
            "FROM (SELECT DISTINCT stem, ts, doc FROM fs) s "
            "LEFT JOIN rev r ON r.stem = s.stem AND r.ts = s.ts "
            "LEFT JOIN act a ON a.stem = s.stem AND a.ts = s.ts "
            "GROUP BY s.stem, s.ts"
        )
        p1 = d / DISC_FILE
        _atomic_copy(con, "SELECT * FROM disc ORDER BY stem, ts", p1)
        pat2 = (bars_dir / "fins_earnings-date_*.csv.gz").as_posix()
        p2 = d / SCHED_FILE
        _atomic_copy(
            con,
            "SELECT DISTINCT lower(substr(Code, 1, 4)) AS stem, "
            "CAST(PubDate AS DATE) AS pub_date, CAST(SchDate AS DATE) AS sch_date "
            f"FROM read_csv({_sql_str(pat2)}, header=true, union_by_name=true, "
            "types={'Code': 'VARCHAR'}) "
            "WHERE length(Code) = 5 AND substr(Code, 5, 1) = '0' ORDER BY stem, pub_date",
            p2,
        )
        p3 = d / TDNET_DAYS_FILE
        values = ",".join(f"(DATE {_sql_str(x)})" for x in days) or "(NULL::DATE)"
        _atomic_copy(con, f"SELECT d FROM (VALUES {values}) t(d) WHERE d IS NOT NULL ORDER BY d", p3)
    finally:
        con.close()
    return [p1, p2, p3]


# --------------------------------------------------------------------------- #
# ETF grids
# --------------------------------------------------------------------------- #
def build_etf(
    cache_dir: Path | str,
    dates: list[str],
    trades_dir: Path = TRADES_DIR,
    progress: Progress = _say,
) -> int:
    """``etf/<date>.npz``: per-second last-print grid 09:00:00..11:30:00 of 1570/1321/1306.

    A print at time t belongs to grid second ceil(t), so ``grid[g]`` only contains
    prints with time <= 09:00:00 + g s (the grid never looks past its own instant)."""
    out_dir = ext_dir(cache_dir) / ETF_SUBDIR
    out_dir.mkdir(parents=True, exist_ok=True)
    want = set(dates)
    grids: dict[str, np.ndarray] = {}  # date -> (n_etf, G)
    for si, stem in enumerate(ETF_STEMS):
        path = trades_dir / f"{stem}.duckdb"
        if not path.is_file():
            progress(f"  etf {stem}: no tick file")
            continue
        t0 = time.time()
        con = duckdb.connect(path.as_posix(), read_only=True)
        con.execute(f"PRAGMA threads={THREADS}")
        try:
            data = con.execute(
                "SELECT CAST(CAST(ts AS DATE) AS VARCHAR) AS d, "
                "CAST(((us + 999999) // 1000000) % 86400 AS BIGINT) AS sod, "
                "arg_max(Price, us) AS p FROM ("
                "SELECT Price, epoch_us(TRY_CAST(Timestamp AS TIMESTAMP)) AS us, "
                "TRY_CAST(Timestamp AS TIMESTAMP) AS ts FROM stocks_board "
                "WHERE substr(Timestamp, 12, 5) >= '09:00' "
                "AND substr(Timestamp, 12, 8) <= '11:30:00') GROUP BY 1, 2 ORDER BY 1, 2"
            ).fetchnumpy()
        finally:
            con.close()
        n = 0
        pr = np.ma.filled(np.ma.asarray(data["p"], dtype=np.float64), np.nan)
        cur_d: str | None = None
        buf: np.ndarray | None = None
        for d, sod, p in zip(data["d"], data["sod"], pr, strict=True):
            d = str(d)
            if d not in want:
                continue
            g = int(sod) - 9 * 3600
            if g < 0 or g >= ETF_GRID_LEN or not np.isfinite(p):
                continue
            if d != cur_d:
                cur_d = d
                buf = grids.setdefault(
                    d, np.full((len(ETF_STEMS), ETF_GRID_LEN), np.nan, np.float32)
                )
            assert buf is not None
            buf[si, g] = p
            n += 1
        progress(f"  etf {stem}: {len(data['d'])} buckets, {n} used, {time.time() - t0:.0f}s")
    written = 0
    for d, g in grids.items():
        for i in range(len(ETF_STEMS)):  # forward-fill: a print stays valid until the next
            row = g[i]
            fin = np.flatnonzero(np.isfinite(row))
            if len(fin) == 0:
                continue
            idx = np.where(np.isfinite(row), np.arange(len(row)), 0)
            np.maximum.accumulate(idx, out=idx)
            filled = row[idx]
            filled[: fin[0]] = np.nan
            g[i] = filled
        tmp = out_dir / f"{d}.tmp.npz"
        np.savez_compressed(tmp, stems=np.array(ETF_STEMS, dtype="U4"), grid=g)
        tmp.replace(out_dir / f"{d}.npz")
        written += 1
    return written


# --------------------------------------------------------------------------- #
# sector-peer grids
# --------------------------------------------------------------------------- #
def build_peers(cache_dir: Path | str, dates: list[str], progress: Progress = _say) -> int:
    """``peer/<date>.npz``: return-since-own-open grids of the date's cached universe."""
    from .cache import CachedSessionUnavailable, load_cached_session
    from .gate_features import WINDOW_S
    from .scalp_features import MAX_GRID_S, ContextStore

    out_dir = ext_dir(cache_dir) / PEER_SUBDIR
    out_dir.mkdir(parents=True, exist_ok=True)
    store = ContextStore(cache_dir)
    us = 1_000_000
    written = 0
    uniq = sorted(set(dates))
    for k, date in enumerate(uniq):
        path = out_dir / f"{date}.npz"
        if path.is_file():
            continue
        day0 = day_start_us(date)
        stems: list[str] = []
        sectors: list[str] = []
        offs: list[float] = []
        series = []
        for r in store._universe_for(date):
            stem = str(r["stem"]).lower()
            try:
                raw = load_cached_session(date, stem, Path(cache_dir))
            except CachedSessionUnavailable:
                continue
            t = np.asarray(raw.epoch_us, dtype=np.int64)
            p = np.asarray(raw.price, dtype=np.float64)
            if len(t) == 0 or not raw.p_open > 0:
                continue
            stems.append(stem)
            sectors.append(store.sector(str(r.get("code") or stem), date))
            offs.append((int(raw.t_open_us) - day0) / us)
            series.append((t, p, float(raw.p_open), int(raw.t_open_us)))
        if not stems:
            continue
        off = np.asarray(offs)
        n_grid = int(min(np.ceil(off.max()) + WINDOW_S + 2, MAX_GRID_S))
        R = np.full((len(stems), max(n_grid, 1)), np.nan, dtype=np.float32)
        for i, (t, p, p_open, t_open) in enumerate(series):
            g0 = max(int(np.ceil((t_open - day0) / us)), 0)
            g1 = min(int(np.floor((t_open + WINDOW_S * us - day0) / us)), n_grid - 1)
            if g1 < g0:
                continue
            g = np.arange(g0, g1 + 1)
            idx = np.searchsorted(t, day0 + g * us, side="right") - 1
            ok = idx >= 0
            R[i, g[ok]] = ((p[idx[ok]] / p_open - 1.0) * 1e4).astype(np.float32)
        tmp = out_dir / f"{date}.tmp.npz"
        np.savez_compressed(
            tmp,
            stems=np.array(stems, dtype="U16"),
            sector=np.array(sectors, dtype="U8"),
            open_off=off,
            R=R,
        )
        tmp.replace(path)
        written += 1
        if (k + 1) % 50 == 0:
            progress(f"  peers {k + 1}/{len(uniq)}")
    return written
