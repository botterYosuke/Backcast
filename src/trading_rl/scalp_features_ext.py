"""Extra observation-feature groups K-Q for :mod:`trading_rl.scalp_features`.

Same contract as groups A-I (see that module): every column is causal -- only
prints with time <= the decision time, only daily / event data strictly before the
session date (or, for disclosures, strictly before the stock's ``T_open``) -- and
NaN/inf -> 0, clipped.  Spec letters::

    K  index real-time    ETF 1570 / 1321 / 1306 look-back returns, since-open, gap and
                          move vs previous close on the wall clock; stock residual
                          (stock return - rolling TOPIX beta * 1306 return)
    L  sector peers       real-time returns of same-Sector33 peers in that date's
                          universe (self excluded), stock minus peers
    M  disclosures        TDnet / J-Quants earnings disclosures between the previous
                          day's 15:00 and ``T_open`` (+ earnings-due flags, forecast
                          revision / earnings surprise)
    N  index regime       TOPIX / 1321 prior-day, 5/20-day return, 20-day vol, distance
                          from the 25-day MA, stock beta / correlation to TOPIX (60 d)
    O  large prints       share of volume in large prints and signed (tick-rule) large
                          print imbalance over 30 / 60 s and since open
    P  previous close     previous day's last-30-min return, close vs VWAP, last-30-min
                          volume share (J-Quants minute bars)
    Q  calendar           day of week, SQ day / major SQ, month end / start, gaps

All cached inputs live in the NEW directory ``<cache_dir>/ext`` (built once by
``scripts/build_scalp_ext_cache.py``); nothing existing is touched.  Missing data
gives zeros plus a ``*_known`` flag of 0.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import date as Date
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import duckdb
import numpy as np

from .gate_features import START_S, WINDOW_S

US = 1_000_000
EXT_GROUPS = "KLMNOPQ"
EXT_DIR = "ext"
DAILY_ADJ_FILE = "daily_adj.parquet"
TOPIX_FILE = "topix_daily.parquet"
PREVDAY_FILE = "prevday.parquet"
DISC_FILE = "disclosures.parquet"
SCHED_FILE = "earn_sched.parquet"
TDNET_DAYS_FILE = "tdnet_days.parquet"
ETF_SUBDIR = "etf"
PEER_SUBDIR = "peer"
UNIVERSE_FILE = "universe.parquet"  # <cache_dir>/universe.parquet (existing)

ETF_STEMS = ("1570", "1321", "1306")
ETF_GRID_LEN = 9001  # wall seconds 09:00:00 .. 11:30:00
BETA_DAYS = 60
BETA_MIN_OBS = 30
MAX_STALE_DAYS = 10
CALENDAR_BREADTH = 500
TDNET_START = "2016-02-13"
TDNET_END = "2026-07-17"  # TDnet index coverage ends here -> M covered=0 afterwards
LARGE_FRAC_ADV = 2e-4  # large print: qty >= this fraction of avg_volume_20d ...
LARGE_MIN_QTY = 500.0  # ... and at least this many shares

_DEC = np.arange(START_S, WINDOW_S, dtype=np.int64)  # decision seconds (590)

EXT_NAMES: dict[str, list[str]] = {
    "K": [
        "k_known_1570", "k_known_1321", "k_known_1306", "k_beta_known",
        "k_1570_ret5_bps", "k_1570_ret15_bps", "k_1570_ret30_bps", "k_1570_ret60_bps",
        "k_1570_since_open_bps", "k_1570_gap_bps", "k_1570_vs_prev_close_bps",
        "k_1321_ret15_bps", "k_1321_ret60_bps", "k_1321_since_open_bps",
        "k_1321_gap_bps", "k_1321_vs_prev_close_bps",
        "k_1306_ret15_bps", "k_1306_ret60_bps", "k_1306_since_open_bps",
        "k_1306_gap_bps", "k_1306_vs_prev_close_bps",
        "k_idx_ret_since_topen_bps", "k_beta60",
        "k_resid_ret15_bps", "k_resid_ret60_bps", "k_resid_since_open_bps",
    ],
    "L": [
        "l_known", "l_log1p_n_peers",
        "l_peer_ret15_bps", "l_peer_ret60_bps", "l_peer_since_open_bps",
        "l_rel_ret15_bps", "l_rel_ret60_bps", "l_rel_since_open_bps",
        "l_peer_frac_up",
    ],
    "M": [
        "m_covered", "m_log1p_n_disc", "m_earn_disc", "m_up_rev", "m_down_rev",
        "m_buyback", "m_dividend", "m_other", "m_earn_due_today", "m_earn_due_prev",
        "m_rev_known", "m_rev_pct", "m_surprise_known", "m_op_surprise",
    ],
    "N": [
        "n_known",
        "n_topix_ret1_pct", "n_topix_ret5_pct", "n_topix_ret20_pct",
        "n_topix_vol20_pct", "n_topix_dist_ma25_pct",
        "n_nk_ret1_pct", "n_nk_ret5_pct", "n_nk_ret20_pct",
        "n_nk_vol20_pct", "n_nk_dist_ma25_pct",
        "n_beta_known", "n_beta60", "n_corr60",
    ],
    "O": [
        "o_known",
        "o_large_share30", "o_large_share60", "o_large_share_open",
        "o_large_imb30", "o_large_imb60", "o_large_imb_open",
        "o_log1p_nlarge60", "o_log1p_nlarge_open",
    ],
    "P": ["p_known", "p_last30_ret_bps", "p_close_vs_vwap_bps", "p_last30_vol_share"],
    "Q": [
        "q_dow_mon", "q_dow_tue", "q_dow_wed", "q_dow_thu", "q_dow_fri",
        "q_sq_day", "q_sq_major", "q_month_end", "q_month_start",
        "q_after_gap2", "q_after_long",
    ],
}  # fmt: skip


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def day_start_us(date: str) -> int:
    """Epoch microseconds of ``date 09:00:00`` in the cache's (naive-as-UTC) clock."""
    return int(datetime.fromisoformat(f"{date} 09:00:00+00:00").timestamp()) * US


def _midnight_us(date: str) -> int:
    return int(datetime.fromisoformat(f"{date} 00:00:00+00:00").timestamp()) * US


def _clip(x: np.ndarray, lo: float, hi: float) -> np.ndarray:
    return np.clip(np.nan_to_num(x, nan=0.0, posinf=hi, neginf=lo), lo, hi)


def _full(v: float) -> np.ndarray:
    return np.full(len(_DEC), v, dtype=np.float64)


def _sql_str(value: object) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _d64(date: str) -> np.datetime64:
    return np.datetime64(date, "D")


def disc_window_start_us(date: str) -> int:
    """Start of the disclosure window of a session: 15:00 of the previous trading day.

    (Fixed at 15:00 as specified, also after the 2024-11-05 close extension to 15:30:
    most listed companies still release at 15:00, so 15:00-15:30 items belong to the
    window.)"""
    return _midnight_us(date) + 15 * 3600 * US


def _boundaries(keys: np.ndarray) -> list[tuple[int, int]]:
    """(start, end) of each run of equal values of a sorted key array."""
    cut = np.flatnonzero(keys[1:] != keys[:-1]) + 1
    return list(zip(np.concatenate([[0], cut]), np.concatenate([cut, [len(keys)]]), strict=True))


# --------------------------------------------------------------------------- #
# Data containers
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class EtfDay:
    """One date's ETF last-price grids (``(len(ETF_STEMS), ETF_GRID_LEN)`` float32).

    ``grid[i, g]`` is the last print with time <= 09:00:00 + g s (NaN before the first
    print of that ETF)."""

    date: str
    stems: tuple[str, ...]
    grid: np.ndarray

    def series(self, stem: str) -> np.ndarray | None:
        if stem not in self.stems:
            return None
        g = self.grid[self.stems.index(stem)]
        return g if np.isfinite(g).any() else None


@dataclass(frozen=True)
class PeerDay:
    """Return-since-own-open grids (bps, NaN unknown) of one date's universe."""

    date: str
    stems: tuple[str, ...]
    sector: tuple[str, ...]
    open_off: np.ndarray  # (n,) seconds since 09:00:00
    R: np.ndarray  # (n, G) float32: (P / p_open - 1) * 1e4 on the wall-clock grid


@dataclass
class _Series:
    d: np.ndarray  # datetime64[D] ascending
    c: np.ndarray
    adjf: np.ndarray
    _ret: np.ndarray | None = None

    def ret(self) -> np.ndarray:
        """Split-adjusted close-to-close return of each row vs the previous row."""
        if self._ret is None:
            r = np.full(len(self.c), np.nan)
            if len(self.c) > 1:
                prev = self.c[:-1] * np.where(np.isfinite(self.adjf[1:]), self.adjf[1:], 1.0)
                with np.errstate(divide="ignore", invalid="ignore"):
                    r[1:] = np.where(prev > 0, self.c[1:] / prev - 1.0, np.nan)
            self._ret = r
        return self._ret


@dataclass(frozen=True)
class Disc:
    """TDnet / J-Quants disclosures of one stem (sorted by ``ts`` us)."""

    ts: np.ndarray
    flags: np.ndarray  # (n, 7) kessan, up, down, buyback, dividend, other, fs
    rev: np.ndarray  # forecast revision fraction (NaN unknown)
    sur: np.ndarray  # earnings surprise fraction (NaN unknown)
    is_rev: np.ndarray  # row is an EarnForecastRevision record


_FLAG_COLS = ("is_kessan", "is_up", "is_down", "is_buyback", "is_dividend", "is_other", "is_fs")


# --------------------------------------------------------------------------- #
# ExtData: lazy loader of the ext cache
# --------------------------------------------------------------------------- #
class ExtData:
    """Reads ``<cache_dir>/ext`` lazily; every accessor returns ``None``/NaN if missing."""

    def __init__(self, cache_dir: Path | str) -> None:
        self.cache_dir = Path(cache_dir)
        self.dir = self.cache_dir / EXT_DIR
        self._lock = threading.RLock()
        self._series: dict[str, _Series | None] | None = None
        self._topix: _Series | None = None
        self._topix_loaded = False
        self._calendar: np.ndarray | None = None
        self._calendar_loaded = False
        self._prevday: dict[tuple[str, str], tuple[float, float, float]] | None = None
        self._disc: dict[str, Disc] | None = None
        self._sched: dict[str, list[tuple[np.ndarray, np.ndarray]]] | None = None
        self._tdnet_days: set[str] | None = None
        self._etf: dict[str, EtfDay | None] = {}
        self._peer: dict[str, PeerDay | None] = {}
        self._beta: dict[tuple[str, str], tuple[float, float, int]] = {}

    # ----------------------------------------------------------------- utils
    def _query(self, name: str, sql: str) -> dict[str, Any] | None:
        path = self.dir / name
        if not path.is_file():
            return None
        con = duckdb.connect(":memory:")
        try:
            return con.execute(sql.format(p=_sql_str(path.as_posix()))).fetchnumpy()
        finally:
            con.close()

    # ----------------------------------------------------------------- daily
    def _fetch_series(self, where: str) -> dict[str, _Series]:
        data = self._query(
            DAILY_ADJ_FILE,
            "SELECT stem, d, c, coalesce(adjf, 1.0) AS adjf FROM read_parquet({p}) "
            f"WHERE c IS NOT NULL AND ({where}) ORDER BY stem, d",
        )
        out: dict[str, _Series] = {}
        if data is None or not len(data["stem"]):
            return out
        stems = np.asarray(data["stem"])
        d = np.asarray(data["d"]).astype("datetime64[D]")
        c = np.ma.filled(np.ma.asarray(data["c"], dtype=np.float64), np.nan)
        af = np.ma.filled(np.ma.asarray(data["adjf"], dtype=np.float64), 1.0)
        for a, b in _boundaries(stems):
            out[str(stems[a])] = _Series(d[a:b], c[a:b], af[a:b])
        return out

    def series(self, stem: str) -> _Series | None:
        """Daily close / adjustment-factor series of one stem.

        The first call bulk-loads every stem of ``universe.parquet`` (+ the ETFs) in one
        query; other stems are fetched on demand."""
        stem = stem.lower()
        with self._lock:
            if self._series is None:
                self._series = {}
                uni = self.cache_dir / UNIVERSE_FILE
                if uni.is_file():
                    etfs = ",".join(_sql_str(e) for e in (*ETF_STEMS, "1321", "1306"))
                    self._series.update(
                        self._fetch_series(
                            "stem IN (SELECT DISTINCT lower(stem) FROM read_parquet("
                            f"{_sql_str(uni.as_posix())})) OR stem IN ({etfs})"
                        )
                    )
            if stem not in self._series:
                self._series[stem] = self._fetch_series(f"stem = {_sql_str(stem)}").get(stem)
            return self._series[stem]

    def topix(self) -> _Series | None:
        with self._lock:
            if not self._topix_loaded:
                self._topix_loaded = True
                data = self._query(
                    TOPIX_FILE,
                    "SELECT d, c FROM read_parquet({p}) WHERE c IS NOT NULL ORDER BY d",
                )
                if data is not None and len(data["d"]):
                    d = np.asarray(data["d"]).astype("datetime64[D]")
                    c = np.ma.filled(np.ma.asarray(data["c"], dtype=np.float64), np.nan)
                    self._topix = _Series(d, c, np.ones(len(c)))
            return self._topix

    def calendar(self) -> np.ndarray | None:
        """Trading dates (``datetime64[D]``): days with >= 500 daily bars."""
        with self._lock:
            if not self._calendar_loaded:
                self._calendar_loaded = True
                data = self._query(
                    DAILY_ADJ_FILE,
                    "SELECT d FROM read_parquet({p}) GROUP BY d "
                    f"HAVING count(*) >= {CALENDAR_BREADTH} ORDER BY d",
                )
                if data is not None and len(data["d"]):
                    self._calendar = np.asarray(data["d"]).astype("datetime64[D]")
            return self._calendar

    def prev_td(self, date: str) -> str | None:
        """Previous trading date strictly before ``date`` (None if unknown)."""
        cal = self.calendar()
        day = _d64(date)
        if cal is not None and len(cal) and cal[0] < day:
            i = int(np.searchsorted(cal, day, side="left")) - 1
            if i >= 0 and (day - cal[i]).astype(int) <= MAX_STALE_DAYS:
                return str(cal[i])
        return None

    # ----------------------------------------------------- stock beta / corr
    def beta_corr(self, stem: str, date: str) -> tuple[float, float, int]:
        """OLS beta / correlation of the stock's daily returns on TOPIX over the last
        ``BETA_DAYS`` sessions strictly before ``date`` (NaN, NaN, n if unknown)."""
        key = (stem.lower(), date)
        hit = self._beta.get(key)
        if hit is not None:
            return hit
        out = (float("nan"), float("nan"), 0)
        s, t = self.series(stem), self.topix()
        if s is not None and t is not None:
            day = _d64(date)
            n = int(np.searchsorted(s.d, day, side="left"))  # rows strictly before date
            lo = max(n - BETA_DAYS, 1)
            if n > lo:
                rs = np.clip(s.ret()[lo:n], -0.3, 0.3)
                ds = s.d[lo:n]
                rt = np.clip(t.ret(), -0.3, 0.3)
                j = np.searchsorted(t.d, ds)
                ok = (j < len(t.d)) & np.isfinite(rs)
                j = np.minimum(j, len(t.d) - 1)
                ok &= t.d[j] == ds
                x, y = rt[j][ok], rs[ok]
                ok2 = np.isfinite(x)
                x, y = x[ok2], y[ok2]
                if len(x) >= BETA_MIN_OBS and np.std(x) > 0:
                    cov = float(np.mean((x - x.mean()) * (y - y.mean())))
                    vx, sy = float(np.var(x)), float(np.std(y))
                    corr = cov / (np.sqrt(vx) * sy) if sy > 0 else 0.0
                    out = (cov / vx, float(corr), int(len(x)))
        with self._lock:
            if len(self._beta) > 200_000:
                self._beta.clear()
            self._beta[key] = out
        return out

    def prev_close(self, stem: str, date: str) -> float:
        """Close of the last session strictly before ``date`` (NaN if unknown/stale)."""
        s = self.series(stem)
        if s is None or len(s.d) == 0:
            return float("nan")
        day = _d64(date)
        i = int(np.searchsorted(s.d, day, side="left")) - 1
        if i < 0 or (day - s.d[i]).astype(int) > MAX_STALE_DAYS:
            return float("nan")
        return float(s.c[i])

    def regime(self, which: str, date: str) -> dict[str, float] | None:
        """Daily regime of ``'topix'`` or ``'1321'`` as of the last session < ``date``."""
        s = self.topix() if which == "topix" else self.series("1321")
        if s is None or len(s.d) < 26:
            return None
        day = _d64(date)
        i = int(np.searchsorted(s.d, day, side="left")) - 1  # last row strictly before
        if i < 25 or (day - s.d[i]).astype(int) > MAX_STALE_DAYS:
            return None
        c = s.c[: i + 1]
        r = s.ret()[: i + 1]
        r20 = r[-20:]
        if not (np.isfinite(c[-1]) and np.isfinite(r20).all()):
            return None

        def back(k: int) -> float:
            return float(c[-1] / c[-1 - k] - 1.0) if c[-1 - k] > 0 else float("nan")

        ma25 = float(np.mean(c[-25:]))
        return {
            "ret1": float(r[-1]) * 100.0,
            "ret5": back(5) * 100.0,
            "ret20": back(20) * 100.0,
            "vol20": float(np.std(r20, ddof=1)) * 100.0,
            "dist_ma25": (float(c[-1]) / ma25 - 1.0) * 100.0 if ma25 > 0 else float("nan"),
        }

    # -------------------------------------------------------------- previous day
    def prevday(self, stem: str, date: str) -> tuple[float, float, float] | None:
        """(last30 return bps, close-vs-VWAP bps, last30 volume share) of ``date``."""
        with self._lock:
            if self._prevday is None:
                self._prevday = {}
                data = self._query(
                    PREVDAY_FILE,
                    "SELECT stem, CAST(d AS VARCHAR) AS d, last30_ret_bps, vwap_dev_bps, "
                    "last30_share FROM read_parquet({p})",
                )
                if data is not None and len(data["stem"]):
                    vals = [
                        np.ma.filled(np.ma.asarray(data[k], dtype=np.float64), np.nan).tolist()
                        for k in ("last30_ret_bps", "vwap_dev_bps", "last30_share")
                    ]
                    self._prevday = {
                        (str(s), str(d)): (a, b, c)
                        for s, d, a, b, c in zip(data["stem"], data["d"], *vals, strict=True)
                    }
            hit = self._prevday.get((stem.lower(), date))
        if hit is None or not all(np.isfinite(hit)):
            return None
        return hit

    # --------------------------------------------------------------- disclosures
    def _load_disc(self) -> dict[str, Disc]:
        with self._lock:
            if self._disc is None:
                self._disc = {}
                cols = ", ".join(_FLAG_COLS)
                data = self._query(
                    DISC_FILE,
                    f"SELECT stem, ts, {cols}, rev_pct, surprise, is_rev "
                    "FROM read_parquet({p}) ORDER BY stem, ts",
                )
                if data is not None and len(data["stem"]):
                    stems = np.asarray(data["stem"])
                    ts = np.asarray(data["ts"], dtype=np.int64)
                    fl = np.stack(
                        [
                            np.ma.filled(np.ma.asarray(data[c]), 0).astype(np.int8)
                            for c in _FLAG_COLS
                        ],
                        axis=1,
                    )
                    rev = np.ma.filled(np.ma.asarray(data["rev_pct"], dtype=np.float64), np.nan)
                    sur = np.ma.filled(np.ma.asarray(data["surprise"], dtype=np.float64), np.nan)
                    isr = np.ma.filled(np.ma.asarray(data["is_rev"]), 0).astype(bool)
                    for a, b in _boundaries(stems):
                        self._disc[str(stems[a])] = Disc(
                            ts[a:b], fl[a:b], rev[a:b], sur[a:b], isr[a:b]
                        )
            return self._disc

    def disclosures(self, stem: str) -> Disc | None:
        return self._load_disc().get(stem.lower())

    def tdnet_days(self) -> set[str]:
        with self._lock:
            if self._tdnet_days is None:
                data = self._query(
                    TDNET_DAYS_FILE, "SELECT CAST(d AS VARCHAR) AS d FROM read_parquet({p})"
                )
                self._tdnet_days = set() if data is None else {str(x) for x in data["d"]}
            return self._tdnet_days

    def earn_sched(self, stem: str) -> list[tuple[np.ndarray, np.ndarray]]:
        """``[(pub_date, sch_date)]`` as parallel datetime64[D] arrays of one stem."""
        with self._lock:
            if self._sched is None:
                self._sched = {}
                data = self._query(
                    SCHED_FILE,
                    "SELECT stem, pub_date, sch_date FROM read_parquet({p}) ORDER BY stem",
                )
                if data is not None and len(data["stem"]):
                    stems = np.asarray(data["stem"])
                    pub = np.asarray(data["pub_date"]).astype("datetime64[D]")
                    sch = np.asarray(data["sch_date"]).astype("datetime64[D]")
                    for a, b in _boundaries(stems):
                        self._sched[str(stems[a])] = [(pub[a:b], sch[a:b])]
            return self._sched.get(stem.lower(), [])

    # ----------------------------------------------------------------- per date
    def etf_day(self, date: str) -> EtfDay | None:
        with self._lock:
            if date in self._etf:
                return self._etf[date]
        path = self.dir / ETF_SUBDIR / f"{date}.npz"
        day: EtfDay | None = None
        if path.is_file():
            try:
                z = np.load(path, allow_pickle=False)
                day = EtfDay(date, tuple(str(s) for s in z["stems"]), z["grid"])
            except Exception:  # noqa: BLE001 - corrupt cache -> unknown
                day = None
        with self._lock:
            if len(self._etf) >= 6:
                self._etf.pop(next(iter(self._etf)))
            self._etf[date] = day
        return day

    def peer_day(self, date: str) -> PeerDay | None:
        with self._lock:
            if date in self._peer:
                return self._peer[date]
        path = self.dir / PEER_SUBDIR / f"{date}.npz"
        day: PeerDay | None = None
        if path.is_file():
            try:
                z = np.load(path, allow_pickle=False)
                day = PeerDay(
                    date,
                    tuple(str(s) for s in z["stems"]),
                    tuple(str(s) for s in z["sector"]),
                    z["open_off"],
                    z["R"],
                )
            except Exception:  # noqa: BLE001
                day = None
        with self._lock:
            if len(self._peer) >= 6:
                self._peer.pop(next(iter(self._peer)))
            self._peer[date] = day
        return day


_EXT: dict[str, ExtData] = {}
_EXT_LOCK = threading.Lock()


def ext_data(cache_dir: Path | str) -> ExtData:
    """Process-wide :class:`ExtData` per cache dir."""
    key = str(Path(cache_dir).resolve())
    with _EXT_LOCK:
        hit = _EXT.get(key)
        if hit is None:
            hit = _EXT[key] = ExtData(cache_dir)
        return hit


# --------------------------------------------------------------------------- #
# Group computations
# --------------------------------------------------------------------------- #
def _own_clock(a: Any) -> float:
    """Seconds from 09:00:00 to the stock's T_open (the wall-clock grid offset)."""
    return (int(a.t_open_us) - day_start_us(str(a.date))) / US


def _stock_rets(a: Any, lags: tuple[int, ...]) -> tuple[np.ndarray, dict[int, np.ndarray]]:
    """Stock return since open and over look-backs (bps) at the decision seconds."""
    P = a.P
    ref = P[_DEC]
    since = (ref / a.p_open - 1.0) * 1e4 if a.p_open > 0 else _full(0.0)
    out = {}
    for lag in lags:
        base = P[np.maximum(_DEC - lag, 0)]
        out[lag] = (ref / base - 1.0) * 1e4
    return since, out


def _first_idx(grid: np.ndarray) -> int:
    fin = np.flatnonzero(np.isfinite(grid))
    return int(fin[0]) if len(fin) else len(grid)


def _etf_returns(grid: np.ndarray, g: np.ndarray, lag: int) -> tuple[np.ndarray, np.ndarray]:
    """(ret bps, valid) of an ETF grid at clock indices ``g`` over ``lag`` seconds."""
    first = _first_idx(grid)
    ok = (g >= first) & (g < len(grid)) & (first < len(grid))
    gi = np.clip(g, 0, len(grid) - 1)
    cur = grid[gi].astype(np.float64)
    bi = np.clip(np.maximum(g - lag, first), 0, len(grid) - 1)
    base = grid[bi].astype(np.float64)
    with np.errstate(divide="ignore", invalid="ignore"):
        ret = (cur / base - 1.0) * 1e4
    ok &= np.isfinite(ret)
    return np.where(ok, ret, 0.0), ok


def _group_K(a: Any, ed: ExtData) -> list[np.ndarray]:
    names = EXT_NAMES["K"]
    day = ed.etf_day(str(a.date))
    if day is None:
        return [_full(0.0) for _ in names]
    off = _own_clock(a)
    g = np.floor(off + _DEC).astype(np.int64)

    cols: dict[str, np.ndarray] = {}
    known: dict[str, np.ndarray] = {}
    plan = {"1570": (5, 15, 30, 60), "1321": (15, 60), "1306": (15, 60)}
    for stem, lags in plan.items():
        grid = day.series(stem)
        if grid is None:
            known[stem] = _full(0.0)
            for n in names:
                if n.startswith(f"k_{stem}_"):
                    cols[n] = _full(0.0)
            continue
        first = _first_idx(grid)
        ok = (g >= first) & (g < len(grid))
        known[stem] = ok.astype(np.float64)
        for lag in lags:
            r, _ = _etf_returns(grid, g, lag)
            cols[f"k_{stem}_ret{lag}_bps"] = _clip(r, -1000.0, 1000.0)
        so, _ = _etf_returns(grid, g, 10**6)  # base = the ETF's first print
        cols[f"k_{stem}_since_open_bps"] = _clip(so, -1500.0, 1500.0)
        pc = ed.prev_close(stem, str(a.date))
        if np.isfinite(pc) and pc > 0:
            gi = np.clip(g, 0, len(grid) - 1)
            vs = (grid[gi].astype(np.float64) / pc - 1.0) * 1e4
            gap0 = (float(grid[first]) / pc - 1.0) * 1e4 if first < len(grid) else 0.0
            cols[f"k_{stem}_vs_prev_close_bps"] = _clip(np.where(ok, vs, 0.0), -1500.0, 1500.0)
            cols[f"k_{stem}_gap_bps"] = _clip(np.where(ok, gap0, 0.0), -1500.0, 1500.0)
        else:
            cols[f"k_{stem}_vs_prev_close_bps"] = _full(0.0)
            cols[f"k_{stem}_gap_bps"] = _full(0.0)

    # stock residual vs 1306 (TOPIX ETF), aligned on the stock's own windows
    beta, _corr, _n = ed.beta_corr(str(a.stem), str(a.date))
    beta_ok = bool(np.isfinite(beta))
    b = float(np.clip(beta, -1.0, 4.0)) if beta_ok else 0.0
    grid6 = day.series("1306")
    stock_since, stock_lag = _stock_rets(a, (15, 60))
    idx_since = _full(0.0)
    r15 = r60 = _full(0.0)
    res_ok = np.zeros(len(_DEC), dtype=bool)
    if grid6 is not None:
        first6 = _first_idx(grid6)
        n6 = len(grid6)
        ok6 = (g >= first6) & (g < n6)
        res_ok = ok6 & beta_ok
        bi = max(int(np.floor(off)), first6)
        gi = np.clip(g, 0, n6 - 1)
        cur = grid6[gi].astype(np.float64)
        if bi < n6:
            with np.errstate(divide="ignore", invalid="ignore"):
                idx_since = np.where(ok6, (cur / float(grid6[bi]) - 1.0) * 1e4, 0.0)
        res: dict[int, np.ndarray] = {}
        for lag in (15, 60):
            gl = np.floor(off + np.maximum(_DEC - lag, 0)).astype(np.int64)
            base = grid6[np.clip(np.maximum(gl, first6), 0, n6 - 1)].astype(np.float64)
            with np.errstate(divide="ignore", invalid="ignore"):
                idx_r = np.where(ok6, (cur / base - 1.0) * 1e4, 0.0)
            res[lag] = stock_lag[lag] - b * idx_r
        r15, r60 = res[15], res[60]
    rs = stock_since - b * idx_since
    cols["k_idx_ret_since_topen_bps"] = _clip(idx_since, -1500.0, 1500.0)
    cols["k_beta60"] = _full(b)
    cols["k_resid_ret15_bps"] = _clip(np.where(res_ok, r15, 0.0), -2000.0, 2000.0)
    cols["k_resid_ret60_bps"] = _clip(np.where(res_ok, r60, 0.0), -2000.0, 2000.0)
    cols["k_resid_since_open_bps"] = _clip(np.where(res_ok, rs, 0.0), -2000.0, 2000.0)
    cols["k_known_1570"] = known["1570"]
    cols["k_known_1321"] = known["1321"]
    cols["k_known_1306"] = known["1306"]
    cols["k_beta_known"] = res_ok.astype(np.float64)
    return [cols[n] for n in names]


def _group_L(a: Any, ed: ExtData) -> list[np.ndarray]:
    names = EXT_NAMES["L"]
    day = ed.peer_day(str(a.date))
    if day is None or len(day.stems) == 0:
        return [_full(0.0) for _ in names]
    stem = str(a.stem).lower()
    sector = day.sector[day.stems.index(stem)] if stem in day.stems else ""
    if not sector:
        return [_full(0.0) for _ in names]
    idx = np.array(
        [
            i
            for i, (s, sec) in enumerate(zip(day.stems, day.sector, strict=True))
            if s != stem and sec == sector
        ],
        dtype=np.int64,
    )
    if len(idx) == 0:
        return [_full(0.0) for _ in names]
    R = day.R[idx].astype(np.float64)  # (p, G)
    G = R.shape[1]
    off = _own_clock(a)
    g = np.floor(off + _DEC).astype(np.int64)
    inside = (g >= 0) & (g < G)
    cur = np.where(inside[None, :], R[:, np.clip(g, 0, G - 1)], np.nan)  # (p, 590)
    have = np.isfinite(cur)
    n = have.sum(axis=0)

    def mean_over(x: np.ndarray) -> np.ndarray:
        s = np.where(have, x, 0.0).sum(axis=0)
        return np.where(n > 0, s / np.maximum(n, 1), 0.0)

    def lag_ret(lag: int) -> np.ndarray:
        gl = np.floor(off + np.maximum(_DEC - lag, 0)).astype(np.int64)
        base = R[:, np.clip(gl, 0, G - 1)]
        base = np.where(np.isfinite(base), base, 0.0)  # before the peer's open: its open
        with np.errstate(invalid="ignore"):
            return ((1.0 + cur / 1e4) / (1.0 + base / 1e4) - 1.0) * 1e4

    p15 = mean_over(lag_ret(15))
    p60 = mean_over(lag_ret(60))
    pso = mean_over(cur)
    up = np.where(
        n > 0, np.where(have, cur > 0, False).sum(axis=0) / np.maximum(n, 1), 0.5
    ).astype(np.float64)
    stock_since, stock_lag = _stock_rets(a, (15, 60))
    known = n > 0
    return [
        known.astype(np.float64),
        np.log1p(n.astype(np.float64)),
        _clip(p15, -1000.0, 1000.0),
        _clip(p60, -1000.0, 1000.0),
        _clip(pso, -1500.0, 1500.0),
        _clip(np.where(known, stock_lag[15] - p15, 0.0), -2000.0, 2000.0),
        _clip(np.where(known, stock_lag[60] - p60, 0.0), -2000.0, 2000.0),
        _clip(np.where(known, stock_since - pso, 0.0), -2000.0, 2000.0),
        up,
    ]


def disclosure_scalars(ed: ExtData, stem: str, date: str, t_open_us: int) -> dict[str, float]:
    """Group M values of one session (scalars; zeros when not covered)."""
    names = EXT_NAMES["M"]
    out = dict.fromkeys(names, 0.0)
    prev = ed.prev_td(date)
    days = ed.tdnet_days()
    covered = (
        prev is not None
        and TDNET_START <= date <= TDNET_END
        and date in days
        and prev in days
    )
    if not covered:
        return out
    out["m_covered"] = 1.0
    start = disc_window_start_us(prev)  # type: ignore[arg-type]
    d = ed.disclosures(stem)
    if d is not None and len(d.ts):
        lo = int(np.searchsorted(d.ts, start, side="left"))
        hi = int(np.searchsorted(d.ts, int(t_open_us), side="left"))  # ts < T_open
        if hi > lo:
            fl = d.flags[lo:hi]
            out["m_log1p_n_disc"] = float(np.log1p(hi - lo))
            kessan, up, down, buy, div, oth, fs = (bool(fl[:, k].any()) for k in range(7))
            out["m_earn_disc"] = float(kessan or fs)
            out["m_buyback"] = float(buy)
            out["m_dividend"] = float(div)
            out["m_other"] = float(oth)
            rev = d.rev[lo:hi]
            rv = np.flatnonzero(np.isfinite(rev))  # revision records and statements alike
            rev_val = float(rev[rv[-1]]) if len(rv) else float("nan")
            out["m_up_rev"] = float(up or (np.isfinite(rev_val) and rev_val > 0))
            out["m_down_rev"] = float(down or (np.isfinite(rev_val) and rev_val < 0))
            if np.isfinite(rev_val):
                out["m_rev_known"] = 1.0
                out["m_rev_pct"] = float(np.clip(rev_val, -1.0, 1.0))
            sur = d.sur[lo:hi]
            sv = np.flatnonzero(np.isfinite(sur))
            if len(sv):
                out["m_surprise_known"] = 1.0
                out["m_op_surprise"] = float(np.clip(sur[sv[-1]], -1.0, 1.0))
    for pub, sch in ed.earn_sched(stem):
        known_pub = pub < _d64(date)  # schedule published before the session date
        out["m_earn_due_today"] = float(((sch == _d64(date)) & known_pub).any())
        if prev is not None:
            out["m_earn_due_prev"] = float(((sch == _d64(prev)) & known_pub).any())
    return out


def _group_M(a: Any, ed: ExtData) -> list[np.ndarray]:
    sc = disclosure_scalars(ed, str(a.stem), str(a.date), int(a.t_open_us))
    return [_full(sc[n]) for n in EXT_NAMES["M"]]


def _group_N(a: Any, ed: ExtData) -> list[np.ndarray]:
    names = EXT_NAMES["N"]
    date = str(a.date)
    tp, nk = ed.regime("topix", date), ed.regime("1321", date)
    vals = dict.fromkeys(names, 0.0)
    if tp is not None and nk is not None:
        vals["n_known"] = 1.0
        for pre, r in (("topix", tp), ("nk", nk)):
            vals[f"n_{pre}_ret1_pct"] = float(np.clip(r["ret1"], -15.0, 15.0))
            vals[f"n_{pre}_ret5_pct"] = float(np.clip(r["ret5"], -30.0, 30.0))
            vals[f"n_{pre}_ret20_pct"] = float(np.clip(r["ret20"], -50.0, 50.0))
            vals[f"n_{pre}_vol20_pct"] = float(np.clip(r["vol20"], 0.0, 10.0))
            vals[f"n_{pre}_dist_ma25_pct"] = float(np.clip(r["dist_ma25"], -30.0, 30.0))
    beta, corr, _ = ed.beta_corr(str(a.stem), date)
    if np.isfinite(beta) and np.isfinite(corr):
        vals["n_beta_known"] = 1.0
        vals["n_beta60"] = float(np.clip(beta, -1.0, 4.0))
        vals["n_corr60"] = float(np.clip(corr, -1.0, 1.0))
    return [_full(vals[n]) for n in names]


def _group_O(a: Any) -> list[np.ndarray]:
    names = EXT_NAMES["O"]
    adv = float(a.avg_volume_20d) if np.isfinite(a.avg_volume_20d) else 0.0
    if adv <= 0 or len(a.t_rel) == 0:
        return [_full(0.0) for _ in names]
    thr = max(LARGE_FRAC_ADV * adv, LARGE_MIN_QTY)
    q = np.asarray(a.qty, dtype=np.float64)
    p = np.asarray(a.price, dtype=np.float64)
    n = len(p)
    sgn = np.zeros(n)
    sgn[1:] = np.sign(np.diff(p))
    # tick rule: a zero tick keeps the previous sign (forward-fill the non-zero signs)
    nz = np.flatnonzero(sgn != 0.0)
    if len(nz):
        pos = np.searchsorted(nz, np.arange(n), side="right") - 1
        sgn = np.where(pos >= 0, sgn[nz[np.maximum(pos, 0)]], 0.0)
    live = np.asarray(a.t_rel) > 0  # the opening auction print(s) are excluded
    large = live & (q >= thr)
    z = np.zeros(1)
    c_all = np.concatenate([z, np.cumsum(np.where(live, q, 0.0))])
    c_lv = np.concatenate([z, np.cumsum(np.where(large, q, 0.0))])
    c_sg = np.concatenate([z, np.cumsum(np.where(large, q * sgn, 0.0))])
    c_nl = np.concatenate([z, np.cumsum(large.astype(np.float64))])
    cnt = np.asarray(a.cnt)

    def win(w: int | None) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        hi = cnt[_DEC]
        lo = np.zeros(len(_DEC), dtype=np.int64) if w is None else cnt[np.maximum(_DEC - w, 0)]
        return c_all[hi] - c_all[lo], c_lv[hi] - c_lv[lo], c_sg[hi] - c_sg[lo], c_nl[hi] - c_nl[lo]

    shares, imbs, nls = [], [], []
    for w in (30, 60, None):
        va, vl, vs, nl = win(w)
        shares.append(np.where(va > 0, vl / np.maximum(va, 1.0), 0.0))
        imbs.append(np.where(vl > 0, vs / np.maximum(vl, 1.0), 0.0))
        nls.append(nl)
    out = [_full(1.0)]
    out += [_clip(s, 0.0, 1.0) for s in shares]
    out += [_clip(i, -1.0, 1.0) for i in imbs]
    out += [np.log1p(nls[1]), np.log1p(nls[2])]
    return out


def _group_P(a: Any, ed: ExtData) -> list[np.ndarray]:
    prev = ed.prev_td(str(a.date))
    got = ed.prevday(str(a.stem), prev) if prev is not None else None
    if got is None:
        return [_full(0.0) for _ in EXT_NAMES["P"]]
    ret, vw, share = got
    return [
        _full(1.0),
        _full(float(np.clip(ret, -500.0, 500.0))),
        _full(float(np.clip(vw, -500.0, 500.0))),
        _full(float(np.clip(share, 0.0, 1.0))),
    ]


def second_friday(year: int, month: int) -> Date:
    d = Date(year, month, 1)
    first_fri = d + timedelta(days=(4 - d.weekday()) % 7)
    return first_fri + timedelta(days=7)


def calendar_scalars(ed: ExtData | None, date: str) -> dict[str, float]:
    """Group Q values; uses the trading calendar when available (weekday rule otherwise)."""
    names = EXT_NAMES["Q"]
    out = dict.fromkeys(names, 0.0)
    d = Date.fromisoformat(date)
    if d.weekday() < 5:
        out[names[d.weekday()]] = 1.0
    cal = ed.calendar() if ed is not None else None
    have_cal = cal is not None and len(cal) > 0 and cal[0] < _d64(date) < cal[-1]

    def is_td(x: Date) -> bool:
        xd = np.datetime64(x, "D")
        if have_cal and cal[0] <= xd <= cal[-1]:  # outside the calendar: weekday rule
            i = int(np.searchsorted(cal, xd))
            return i < len(cal) and cal[i] == xd
        return x.weekday() < 5

    def prev_td(x: Date) -> Date:
        y = x - timedelta(days=1)
        while not is_td(y):
            y -= timedelta(days=1)
        return y

    def next_td(x: Date) -> Date:
        y = x + timedelta(days=1)
        while not is_td(y):
            y += timedelta(days=1)
        return y

    sq = second_friday(d.year, d.month)
    if not is_td(sq):
        sq = prev_td(sq)  # SQ moves to the preceding business day on a holiday
    if d == sq:
        out["q_sq_day"] = 1.0
        out["q_sq_major"] = float(d.month in (3, 6, 9, 12))
    if next_td(d).month != d.month:
        out["q_month_end"] = 1.0
    if prev_td(d).month != d.month:
        out["q_month_start"] = 1.0
    gap = (d - prev_td(d)).days - 1  # non-trading days in between
    out["q_after_gap2"] = float(gap >= 2)
    out["q_after_long"] = float(gap >= 3)
    return out


def _group_Q(a: Any, ed: ExtData) -> list[np.ndarray]:
    sc = calendar_scalars(ed, str(a.date))
    return [_full(sc[n]) for n in EXT_NAMES["Q"]]


def ext_columns(group: str, a: Any, ed: ExtData) -> list[np.ndarray]:
    """``len(EXT_NAMES[group])`` arrays of ``len(_DEC)`` for one of the groups K-Q."""
    if group == "K":
        return _group_K(a, ed)
    if group == "L":
        return _group_L(a, ed)
    if group == "M":
        return _group_M(a, ed)
    if group == "N":
        return _group_N(a, ed)
    if group == "O":
        return _group_O(a)
    if group == "P":
        return _group_P(a, ed)
    if group == "Q":
        return _group_Q(a, ed)
    raise ValueError(f"not an extension group: {group!r}")
