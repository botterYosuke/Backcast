"""Switchable observation-feature groups for :class:`~trading_rl.scalp_env.ScalpEnv`.

A *feature spec* string such as ``"base+A+C-J"`` selects the market part of the
observation: ``base`` is the 28 causal gate features, ``+X`` adds a group of extra
columns, ``-J`` drops base features by name prefix.  ``"base"`` (the default) is
bit-identical to the original observation.  The spec is stored in the checkpoint
(``env_config["features"]``) and rebuilt at replay time.

Groups (every column is causal: only ticks with time <= decision time, only daily
data of sessions strictly before the session date, NaN/inf -> 0, clipped)::

    A gap size           signed gap [%], |gap| zone one-hot (edges 0.5/2/5/10 %), gap/sigma20
    B gap fill           fill progress, distance to prev close [ticks],
                         touched-prev-close and re-crossed-open flags
    C market-wide        wall-clock median return since open / fraction up of the
                         universe stocks that day, own return minus the median
    D sector-relative    own gap minus the median gap of same-Sector33 universe stocks
                         already open (market median when there is no such peer)
    E prior drift/regime 1/5/15/30-day returns / sigma20, 1321 20d vol and 5d return
    F open delay & limit log1p(T_open - 09:00:00), distance to the daily price limits
    G prev-day range     open vs previous-day high / low (J-Quants daily OHLC)
    H first minute       return and volume of the first 60 s (zeros before, + known flag)
    I tick weight        tick / price [bps], 1 s sigma of the last 60 s [ticks]
    -J                   drop ``log_ntrades_*``, ``log_vol_ratio_*``,
                         ``log_time_since_trade`` and ``imb_*``

Nothing here is fitted on data: the only fitted statistic of the observation is the
ActorCritic's mean/std normaliser, which ``train_scalp.py`` fits on train sessions.

Data sources
    * ``daily_panel.parquet`` (cache dir): closes -> sigma20 and returns (E, A, 1321).
    * ``daily_ohlc.parquet`` (cache dir, built once from the J-Quants bulk daily
      CSVs ``S:/j-quants/equities_bars_daily_*.csv.gz``): previous-day high / low (G).
    * ``sector33.parquet`` (cache dir, built once from ``S:/jp/listed_info_pit.duckdb``
      + ``listed_info.duckdb``, newest snapshot <= previous trading day): D.
    * ``ctx/<date>.npz`` (cache dir): per-date cross-section (C, D) built from the
      cached opening sessions of that date's universe.
"""

from __future__ import annotations

import bisect
import re
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import duckdb
import numpy as np

from .gate_features import FEATURE_NAMES, START_S, WINDOW_S
from .universe import (
    DEFAULT_CACHE_DIR,
    DEFAULT_LISTED,
    DEFAULT_LISTED_PIT,
    PANEL_FILE,
    UNIVERSE_FILE,
    load_universe,
    tick_size,
    tick_sizes,
)

US = 1_000_000
ADD_GROUPS = "ABCDEFGHI"
DROP_GROUPS = "J"
J_PREFIXES: tuple[str, ...] = (
    "log_ntrades_",
    "log_vol_ratio_",
    "log_time_since_trade",
    "imb_",
)
DEFAULT_BARS_DIR = Path("S:/j-quants")
BARS_GLOB = "equities_bars_daily_*.csv.gz"
OHLC_FILE = "daily_ohlc.parquet"
SECTOR_FILE = "sector33.parquet"
DAILY_FEAT_FILE = "daily_feat.npz"
CTX_DIR = "ctx"
CTX_VERSION = 1
PANEL_START = "2023-09-01"
BENCHMARK_STEMS = ("1321", "1306")
MAX_STALE_DAYS = 10  # daily row older than this (calendar days) is unusable
MIN_CROSS_SECTION = 3  # fewer open stocks -> market features are neutral
MAX_GRID_S = 6 * 3600

_DEC = np.arange(START_S, WINDOW_S, dtype=np.int64)  # decision seconds (590)


# --------------------------------------------------------------------------- #
# Spec
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class FeatureSpec:
    groups: tuple[str, ...] = ()  # added groups, alphabetical
    drops: tuple[str, ...] = ()  # subtracted groups (only "J")

    @property
    def canonical(self) -> str:
        return "base" + "".join(f"+{g}" for g in self.groups) + "".join(
            f"-{g}" for g in self.drops
        )

    @property
    def is_base(self) -> bool:
        return not self.groups and not self.drops

    @property
    def needs_context(self) -> bool:
        return bool(self.groups)


_TOKEN = re.compile(r"[+-]?[A-Za-z]+")


def parse_feature_spec(spec: str | FeatureSpec | None) -> FeatureSpec:
    """Parse ``"base+A+C-J"`` (``"base"``, ``""``, ``"-J"``, ``"A"`` are all valid)."""
    if isinstance(spec, FeatureSpec):
        return spec
    text = (spec or "base").strip().replace(" ", "")
    if text == "":
        text = "base"
    pos = 0
    adds: set[str] = set()
    drops: set[str] = set()
    first = True
    while pos < len(text):
        m = _TOKEN.match(text, pos)
        if m is None:
            raise ValueError(f"bad feature spec {spec!r} at {text[pos:]!r}")
        tok = m.group(0)
        pos = m.end()
        sign = tok[0] if tok[0] in "+-" else "+"
        name = tok.lstrip("+-")
        if name.lower() == "base":
            if not first or sign == "-":
                raise ValueError(f"'base' must come first in feature spec {spec!r}")
        elif len(name) != 1:
            raise ValueError(f"unknown feature group {name!r} in {spec!r}")
        else:
            g = name.upper()
            if sign == "+" and g in ADD_GROUPS:
                adds.add(g)
            elif sign == "-" and g in DROP_GROUPS:
                drops.add(g)
            else:
                raise ValueError(
                    f"feature group {sign}{g} is not allowed in {spec!r} "
                    f"(add {ADD_GROUPS!r}, subtract {DROP_GROUPS!r})"
                )
        first = False
    return FeatureSpec(tuple(sorted(adds)), tuple(sorted(drops)))


EXTRA_NAMES: dict[str, list[str]] = {
    "A": [
        "a_gap_pct",
        "a_zone_lt0.5",
        "a_zone_0.5_2",
        "a_zone_2_5",
        "a_zone_5_10",
        "a_zone_ge10",
        "a_gap_over_sigma20",
    ],
    "B": [
        "b_fill_progress",
        "b_dist_prev_close_ticks",
        "b_touched_prev_close",
        "b_recrossed_open",
    ],
    "C": ["c_mkt_med_ret_bps", "c_mkt_frac_up", "c_rel_to_mkt_bps", "c_mkt_log_n"],
    "D": ["d_gap_rel_bps", "d_log1p_n_sector", "d_market_fallback"],
    "E": [
        "e_ret1d_over_sigma",
        "e_ret5d_over_sigma",
        "e_ret15d_over_sigma",
        "e_ret30d_over_sigma",
        "e_mkt_vol20d_pct",
        "e_mkt_ret5d_pct",
    ],
    "F": ["f_log1p_open_delay_s", "f_limit_up_dist_pct", "f_limit_down_dist_pct"],
    "G": [
        "g_known",
        "g_above_prev_high",
        "g_below_prev_low",
        "g_range_pos",
        "g_out_dist_sigma",
        "g_prev_range_sigma",
    ],
    "H": ["h_known", "h_ret60_bps", "h_sign60", "h_log_vol60_ratio"],
    "I": ["i_tick_bps", "i_sigma60_ticks"],
}


def kept_base_index(spec: FeatureSpec) -> np.ndarray:
    """Indices of the base gate features that survive the ``-J`` drop."""
    if "J" not in spec.drops:
        return np.arange(len(FEATURE_NAMES))
    return np.array(
        [
            i
            for i, n in enumerate(FEATURE_NAMES)
            if not any(n.startswith(p) for p in J_PREFIXES)
        ],
        dtype=np.int64,
    )


def market_names(spec: FeatureSpec | str | None) -> list[str]:
    spec = parse_feature_spec(spec)
    names = [FEATURE_NAMES[i] for i in kept_base_index(spec)]
    for g in spec.groups:
        names += EXTRA_NAMES[g]
    return names


def n_market(spec: FeatureSpec | str | None) -> int:
    return len(market_names(spec))


# --------------------------------------------------------------------------- #
# Static data containers
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class DailyStatic:
    """Daily facts known before the open (NaN = unknown)."""

    sigma20: float = float("nan")  # std of 20 daily close-to-close log returns
    ret1: float = float("nan")  # simple close returns over 1/5/15/30 sessions
    ret5: float = float("nan")
    ret15: float = float("nan")
    ret30: float = float("nan")
    prev_high: float = float("nan")
    prev_low: float = float("nan")
    mkt_vol20: float = float("nan")  # benchmark (1321/1306) sigma20
    mkt_ret5: float = float("nan")
    sector: str = ""


@dataclass(frozen=True)
class DateContext:
    """Cross-section of one date's universe on the wall-clock 1 s grid."""

    date: str
    day_start_us: int
    stems: tuple[str, ...] = ()
    open_off: np.ndarray = field(default_factory=lambda: np.zeros(0))  # s since 09:00:00
    gap: np.ndarray = field(default_factory=lambda: np.zeros(0))  # bps, NaN unknown
    sector: tuple[str, ...] = ()
    med_ret: np.ndarray = field(default_factory=lambda: np.zeros(0))  # bps, per g
    frac_up: np.ndarray = field(default_factory=lambda: np.zeros(0))
    n_act: np.ndarray = field(default_factory=lambda: np.zeros(0, dtype=np.int32))

    @classmethod
    def empty(cls, date: str) -> DateContext:
        return cls(date, day_start_us(date))


def day_start_us(date: str) -> int:
    """Epoch microseconds of ``date 09:00:00`` in the cache's (naive-as-UTC) clock."""
    return int(datetime.fromisoformat(f"{date} 09:00:00+00:00").timestamp()) * US


def _sql_str(value: object) -> str:
    return "'" + str(value).replace("'", "''") + "'"


# --------------------------------------------------------------------------- #
# Daily table (E, A, G, 1321 regime)
# --------------------------------------------------------------------------- #
_DAILY_SQL = """
WITH p AS (
  SELECT lower(stem) AS stem, CAST(d AS DATE) AS d, close
  FROM read_parquet({panel}) WHERE close IS NOT NULL AND close > 0 {stem_filter}
), r AS (
  SELECT stem, d, close,
    ln(close / lag(close) OVER w) AS lr,
    lag(close, 1) OVER w AS c1, lag(close, 5) OVER w AS c5,
    lag(close, 15) OVER w AS c15, lag(close, 30) OVER w AS c30
  FROM p WINDOW w AS (PARTITION BY stem ORDER BY d)
), s AS (
  SELECT *, stddev_samp(lr) OVER v AS sd, count(lr) OVER v AS n
  FROM r WINDOW v AS (PARTITION BY stem ORDER BY d ROWS BETWEEN 19 PRECEDING AND CURRENT ROW)
)
SELECT s.stem, s.d, s.close,
  s.close / s.c1 - 1 AS ret1, s.close / s.c5 - 1 AS ret5,
  s.close / s.c15 - 1 AS ret15, s.close / s.c30 - 1 AS ret30,
  CASE WHEN s.n >= 10 THEN s.sd END AS sigma20,
  o.h AS hi, o.l AS lo
FROM s LEFT JOIN {ohlc} o ON o.stem = s.stem AND o.d = s.d
ORDER BY s.stem, s.d
"""
_DAILY_COLS = ("close", "ret1", "ret5", "ret15", "ret30", "sigma20", "hi", "lo")
_NO_OHLC = (
    "(SELECT CAST(NULL AS VARCHAR) AS stem, CAST(NULL AS DATE) AS d, "
    "CAST(NULL AS DOUBLE) AS h, CAST(NULL AS DOUBLE) AS l WHERE false)"
)


@dataclass
class _StemDaily:
    d: np.ndarray  # datetime64[D] ascending
    cols: dict[str, np.ndarray]


def _nz(x: float, default: float = 0.0) -> float:
    return default if x is None or not np.isfinite(x) else float(x)


class ContextStore:
    """Loads (and caches on disk under ``cache_dir``) the context of the groups."""

    def __init__(
        self,
        cache_dir: Path | str | None = None,
        *,
        bars_dir: Path | str = DEFAULT_BARS_DIR,
        listed_pit: Path | str = DEFAULT_LISTED_PIT,
        listed_cur: Path | str = DEFAULT_LISTED,
    ) -> None:
        self.cache_dir = Path(cache_dir or DEFAULT_CACHE_DIR)
        self.bars_dir = Path(bars_dir)
        self.listed_pit = Path(listed_pit)
        self.listed_cur = Path(listed_cur)
        self._lock = threading.RLock()
        self._daily: dict[str, _StemDaily] | None = None
        self._sector: dict[str, tuple[list[str], list[str]]] | None = None
        self._ctx: dict[str, DateContext] = {}
        self._universe_rows: dict[str, list[dict[str, Any]]] | None = None
        self.warnings: list[str] = []

    # ---------------------------------------------------------------- daily
    def _warn(self, msg: str) -> None:
        if msg not in self.warnings:
            self.warnings.append(msg)

    def _ohlc_path(self) -> Path:
        return self.cache_dir / OHLC_FILE

    def ensure_ohlc(self) -> Path | None:
        """``daily_ohlc.parquet`` (built once from the J-Quants bulk CSVs)."""
        path = self._ohlc_path()
        if path.is_file():
            return path
        files = sorted(self.bars_dir.glob(BARS_GLOB)) if self.bars_dir.is_dir() else []
        if not files:
            self._warn(f"no daily OHLC source under {self.bars_dir}; G is unknown")
            return None
        con = duckdb.connect(":memory:")
        try:
            pattern = (self.bars_dir / BARS_GLOB).as_posix()
            tmp = path.with_suffix(".tmp.parquet")
            tmp.parent.mkdir(parents=True, exist_ok=True)
            con.execute(
                "COPY (SELECT DISTINCT ON (Date, Code) "
                "lower(substr(Code, 1, 4)) AS stem, CAST(Date AS DATE) AS d, "
                "O AS o, H AS h, L AS l, C AS c "
                f"FROM read_csv({_sql_str(pattern)}, header=true, union_by_name=true, "
                "types={'Code': 'VARCHAR', 'O': 'DOUBLE', 'H': 'DOUBLE', "
                "'L': 'DOUBLE', 'C': 'DOUBLE'}) "
                f"WHERE CAST(Date AS DATE) >= DATE {_sql_str(PANEL_START)} "
                f"ORDER BY stem, d) TO {_sql_str(tmp.as_posix())} (FORMAT parquet)"
            )
            tmp.replace(path)
        except Exception as e:  # noqa: BLE001 - S: may be offline
            self._warn(f"building {OHLC_FILE} failed: {type(e).__name__}: {e}")
            return None
        finally:
            con.close()
        return path

    def _daily_query(self, stems: list[str] | None) -> dict[str, _StemDaily]:
        panel = self.cache_dir / PANEL_FILE
        if not panel.is_file():
            self._warn(f"{panel} missing: E / A sigma are unknown")
            return {}
        ohlc = self.ensure_ohlc()
        flt = ""
        if stems is not None:
            flt = "AND lower(stem) IN (" + ",".join(_sql_str(s) for s in stems) + ")"
        sql = _DAILY_SQL.format(
            panel=_sql_str(panel.as_posix()),
            stem_filter=flt,
            ohlc=f"read_parquet({_sql_str(ohlc.as_posix())})" if ohlc else _NO_OHLC,
        )
        con = duckdb.connect(":memory:")
        try:
            cur = con.execute(sql)
            data = cur.fetchnumpy()
        finally:
            con.close()
        return _group_daily(data)

    def _daily_table_path(self) -> Path:
        return self.cache_dir / DAILY_FEAT_FILE

    def _load_daily(self) -> dict[str, _StemDaily]:
        with self._lock:
            if self._daily is not None:
                return self._daily
            panel = self.cache_dir / PANEL_FILE
            path = self._daily_table_path()
            ohlc = self._ohlc_path()
            fresh = (
                path.is_file()
                and panel.is_file()
                and path.stat().st_mtime >= panel.stat().st_mtime
                and (not ohlc.is_file() or path.stat().st_mtime >= ohlc.stat().st_mtime)
            )
            if not fresh:
                stems = self._universe_stems()
                table = self._daily_query(stems)
                if table and path.parent.is_dir():
                    _save_daily_table(path, table)
                self._daily = table
            else:
                self._daily = _load_daily_table(path)
            return self._daily

    def _universe_stems(self) -> list[str] | None:
        uni = self.cache_dir / UNIVERSE_FILE
        if not uni.is_file():
            return None
        con = duckdb.connect(":memory:")
        try:
            rows = con.execute(
                f"SELECT DISTINCT lower(stem) FROM read_parquet({_sql_str(uni.as_posix())})"
            ).fetchall()
        finally:
            con.close()
        return sorted({r[0] for r in rows} | set(BENCHMARK_STEMS))

    def _stem_daily(self, stem: str) -> _StemDaily | None:
        table = self._load_daily()
        hit = table.get(stem)
        if hit is not None:
            return hit
        # stem outside the universe table: same SQL for just this stem
        with self._lock:
            if stem in table:
                return table[stem]
            got = self._daily_query([stem])
            table[stem] = got.get(stem, _StemDaily(np.zeros(0, "datetime64[D]"), {}))
            return table[stem]

    def _asof(self, stem: str, date: str) -> dict[str, float] | None:
        sd = self._stem_daily(stem.lower())
        if sd is None or len(sd.d) == 0:
            return None
        day = np.datetime64(date, "D")
        i = int(np.searchsorted(sd.d, day, side="left")) - 1  # strictly before date
        if i < 0 or (day - sd.d[i]).astype(int) > MAX_STALE_DAYS:
            return None
        return {k: float(v[i]) for k, v in sd.cols.items()}

    def daily(self, stem: str, date: str, code: str | None = None) -> DailyStatic:
        """Daily facts of ``stem`` as of the last session strictly before ``date``."""
        row = self._asof(stem, date)
        bench = None
        for b in BENCHMARK_STEMS:
            bench = self._asof(b, date)
            if bench is not None:
                break
        sector = self.sector(code or stem, date) if code is not None else ""
        nan = float("nan")
        return DailyStatic(
            sigma20=row["sigma20"] if row else nan,
            ret1=row["ret1"] if row else nan,
            ret5=row["ret5"] if row else nan,
            ret15=row["ret15"] if row else nan,
            ret30=row["ret30"] if row else nan,
            prev_high=row["hi"] if row else nan,
            prev_low=row["lo"] if row else nan,
            mkt_vol20=bench["sigma20"] if bench else nan,
            mkt_ret5=bench["ret5"] if bench else nan,
            sector=sector,
        )

    # --------------------------------------------------------------- sector
    def ensure_sector(self) -> Path | None:
        path = self.cache_dir / SECTOR_FILE
        if path.is_file():
            return path
        if not self.listed_pit.is_file():
            self._warn(f"no listing source {self.listed_pit}; D uses the market median")
            return None
        con = duckdb.connect(":memory:")
        try:
            con.execute(
                f"ATTACH {_sql_str(self.listed_pit.as_posix())} AS pit (READ_ONLY)"
            )
            con.execute(
                "CREATE TEMP TABLE sec AS SELECT CAST(Date AS DATE) AS snap, "
                "Code AS code5, Sector33Code AS sector FROM pit.listed_info "
                "WHERE Sector33Code IS NOT NULL"
            )
            if self.listed_cur.is_file():
                con.execute(
                    f"ATTACH {_sql_str(self.listed_cur.as_posix())} AS cur (READ_ONLY)"
                )
                con.execute(
                    "INSERT INTO sec SELECT CAST(Date AS DATE), "
                    "CASE WHEN length(Code) = 4 THEN Code || '0' ELSE Code END, "
                    "Sector33Code FROM cur.listed_info WHERE Sector33Code IS NOT NULL"
                )
            tmp = path.with_suffix(".tmp.parquet")
            tmp.parent.mkdir(parents=True, exist_ok=True)
            con.execute(
                "COPY (SELECT DISTINCT code5, snap, sector FROM sec ORDER BY code5, snap) "
                f"TO {_sql_str(tmp.as_posix())} (FORMAT parquet)"
            )
            tmp.replace(path)
        except Exception as e:  # noqa: BLE001
            self._warn(f"building {SECTOR_FILE} failed: {type(e).__name__}: {e}")
            return None
        finally:
            con.close()
        return path

    def _load_sector(self) -> dict[str, tuple[list[str], list[str]]]:
        with self._lock:
            if self._sector is not None:
                return self._sector
            out: dict[str, tuple[list[str], list[str]]] = {}
            path = self.ensure_sector()
            if path is not None:
                con = duckdb.connect(":memory:")
                try:
                    rows = con.execute(
                        "SELECT code5, CAST(snap AS VARCHAR), sector FROM "
                        f"read_parquet({_sql_str(path.as_posix())}) ORDER BY code5, snap"
                    ).fetchall()
                finally:
                    con.close()
                for code5, snap, sector in rows:
                    ds, ss = out.setdefault(str(code5), ([], []))
                    ds.append(snap)
                    ss.append(str(sector))
            self._sector = out
            return out

    def sector(self, code: str, date: str) -> str:
        """Sector33 of ``code`` from the newest snapshot strictly before ``date``."""
        code5 = code if len(code) >= 5 else code + "0"
        ds, ss = self._load_sector().get(code5, ([], []))
        i = bisect.bisect_left(ds, date) - 1
        return ss[i] if i >= 0 else ""

    # ----------------------------------------------------- date cross-section
    def _universe_for(self, date: str) -> list[dict[str, Any]]:
        with self._lock:
            if self._universe_rows is None:
                self._universe_rows = {}
            rows = self._universe_rows.get(date)
            if rows is None:
                uni = self.cache_dir / UNIVERSE_FILE
                rows = load_universe(self.cache_dir, date, date) if uni.is_file() else []
                self._universe_rows[date] = rows
            return rows

    def _ctx_path(self, date: str) -> Path:
        return self.cache_dir / CTX_DIR / f"{date}.npz"

    def date_context(self, date: str) -> DateContext:
        """Cross-section of ``date`` (memoised; npz-cached under ``ctx/``)."""
        with self._lock:
            hit = self._ctx.get(date)
            if hit is not None:
                return hit
            rows = self._universe_for(date)
            want = sorted(str(r["stem"]).lower() for r in rows)
            ctx = self._read_ctx(date, want)
            if ctx is None:
                ctx = self._build_ctx(date, rows)
                self._write_ctx(ctx, want)
            if len(self._ctx) >= 6:
                self._ctx.pop(next(iter(self._ctx)))
            self._ctx[date] = ctx
            return ctx

    def _read_ctx(self, date: str, want: list[str]) -> DateContext | None:
        path = self._ctx_path(date)
        if not path.is_file():
            return None
        try:
            z = np.load(path, allow_pickle=False)
            if int(z["version"]) != CTX_VERSION or [str(s) for s in z["want"]] != want:
                return None
            return DateContext(
                date=date,
                day_start_us=day_start_us(date),
                stems=tuple(str(s) for s in z["stems"]),
                open_off=z["open_off"],
                gap=z["gap"],
                sector=tuple(str(s) for s in z["sector"]),
                med_ret=z["med_ret"],
                frac_up=z["frac_up"],
                n_act=z["n_act"],
            )
        except Exception:  # noqa: BLE001 - corrupt cache -> rebuild
            return None

    def _write_ctx(self, ctx: DateContext, want: list[str]) -> None:
        path = self._ctx_path(ctx.date)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp.npz")
            np.savez_compressed(
                tmp,
                version=np.int64(CTX_VERSION),
                want=np.array(want, dtype="U16"),
                stems=np.array(ctx.stems, dtype="U16"),
                open_off=ctx.open_off,
                gap=ctx.gap,
                sector=np.array(ctx.sector, dtype="U8"),
                med_ret=ctx.med_ret,
                frac_up=ctx.frac_up,
                n_act=ctx.n_act,
            )
            tmp.replace(path)
        except OSError:
            pass  # read-only cache dir: just recompute next time

    def _build_ctx(self, date: str, rows: list[dict[str, Any]]) -> DateContext:
        from .cache import CachedSessionUnavailable, load_cached_session

        day0 = day_start_us(date)
        stems: list[str] = []
        offs: list[float] = []
        gaps: list[float] = []
        sectors: list[str] = []
        series: list[tuple[np.ndarray, np.ndarray, float, int]] = []
        for r in rows:
            stem = str(r["stem"]).lower()
            try:
                raw = load_cached_session(date, stem, self.cache_dir)
            except CachedSessionUnavailable:
                continue
            t = np.asarray(raw.epoch_us, dtype=np.int64)
            p = np.asarray(raw.price, dtype=np.float64)
            if len(t) == 0 or not raw.p_open > 0:
                continue
            pc = float(r.get("prev_close") or 0.0)
            stems.append(stem)
            offs.append((int(raw.t_open_us) - day0) / US)
            gaps.append((raw.p_open / pc - 1.0) * 1e4 if pc > 0 else float("nan"))
            sectors.append(self.sector(str(r.get("code") or stem), date))
            series.append((t, p, float(raw.p_open), int(raw.t_open_us)))
        if not stems:
            return DateContext.empty(date)
        off = np.asarray(offs, dtype=np.float64)
        n_grid = int(min(np.ceil(off.max()) + WINDOW_S + 2, MAX_GRID_S))
        R = np.full((len(stems), max(n_grid, 1)), np.nan, dtype=np.float32)
        for i, (t, p, p_open, t_open) in enumerate(series):
            g0 = max(int(np.ceil((t_open - day0) / US)), 0)
            g1 = min(int(np.floor((t_open + WINDOW_S * US - day0) / US)), n_grid - 1)
            if g1 < g0:
                continue
            g = np.arange(g0, g1 + 1)
            idx = np.searchsorted(t, day0 + g * US, side="right") - 1
            ok = idx >= 0
            R[i, g[ok]] = ((p[idx[ok]] / p_open - 1.0) * 1e4).astype(np.float32)
        n = (~np.isnan(R)).sum(axis=0).astype(np.int32)
        srt = np.sort(R, axis=0)  # NaN sorts last
        hi_i = np.clip(n // 2, 0, R.shape[0] - 1)
        lo_i = np.clip((n - 1) // 2, 0, R.shape[0] - 1)
        cols = np.arange(R.shape[1])
        med = 0.5 * (srt[lo_i, cols] + srt[hi_i, cols])
        med = np.where(n > 0, med, 0.0).astype(np.float32)
        with np.errstate(invalid="ignore"):
            up = np.where(n > 0, (R > 0).sum(axis=0) / np.maximum(n, 1), 0.5)
        return DateContext(
            date=date,
            day_start_us=day0,
            stems=tuple(stems),
            open_off=off,
            gap=np.asarray(gaps, dtype=np.float64),
            sector=tuple(sectors),
            med_ret=med,
            frac_up=up.astype(np.float32),
            n_act=n,
        )

    def precompute(self, dates: list[str], progress_every: int = 0) -> None:
        """Build / refresh the per-date cross-section cache for ``dates``."""
        for i, d in enumerate(sorted(set(dates))):
            self.date_context(d)
            if progress_every and (i + 1) % progress_every == 0:
                print(f"  context {i + 1}/{len(set(dates))} dates", flush=True)


def _group_daily(data: dict[str, Any]) -> dict[str, _StemDaily]:
    out: dict[str, _StemDaily] = {}
    stems = np.asarray(data["stem"])
    if len(stems) == 0:
        return out
    d_all = np.asarray(data["d"]).astype("datetime64[D]")
    cols = {
        c: np.ma.filled(np.ma.asarray(data[c], dtype=np.float64), np.nan)
        for c in _DAILY_COLS
    }
    bounds = np.flatnonzero(stems[1:] != stems[:-1]) + 1
    starts = np.concatenate([[0], bounds])
    ends = np.concatenate([bounds, [len(stems)]])
    for a, b in zip(starts, ends, strict=True):
        out[str(stems[a])] = _StemDaily(d_all[a:b], {c: v[a:b] for c, v in cols.items()})
    return out


def _save_daily_table(path: Path, table: dict[str, _StemDaily]) -> None:
    stems = [s for s, sd in table.items() for _ in range(len(sd.d))]
    d = np.concatenate([sd.d for sd in table.values()]).astype("datetime64[D]")
    cols = {c: np.concatenate([sd.cols[c] for sd in table.values()]) for c in _DAILY_COLS}
    tmp = path.with_name(path.stem + ".tmp.npz")
    try:
        np.savez_compressed(
            tmp, stem=np.array(stems, dtype="U8"), d=d.astype("i8"), **cols
        )
        tmp.replace(path)
    except OSError:
        pass  # read-only cache dir: recomputed next time


def _load_daily_table(path: Path) -> dict[str, _StemDaily]:
    z = np.load(path, allow_pickle=False)
    data: dict[str, Any] = {
        "stem": z["stem"],
        "d": z["d"].astype("datetime64[D]"),
        **{c: z[c] for c in _DAILY_COLS},
    }
    return _group_daily(data)


# --------------------------------------------------------------------------- #
# Feature computation
# --------------------------------------------------------------------------- #
# TSE daily price limits (値幅制限): (upper bound exclusive of prev close, width).
_LIMIT_TABLE: tuple[tuple[float, float], ...] = (
    (100, 30), (200, 50), (500, 80), (700, 100), (1_000, 150), (1_500, 300),
    (2_000, 400), (3_000, 500), (5_000, 700), (7_000, 1_000), (10_000, 1_500),
    (15_000, 3_000), (20_000, 4_000), (30_000, 5_000), (50_000, 7_000),
    (70_000, 10_000), (100_000, 15_000), (150_000, 30_000), (200_000, 40_000),
    (300_000, 50_000), (500_000, 70_000), (700_000, 100_000), (1_000_000, 150_000),
    (1_500_000, 300_000), (2_000_000, 400_000), (3_000_000, 500_000),
    (5_000_000, 700_000), (7_000_000, 1_000_000), (10_000_000, 1_500_000),
    (15_000_000, 3_000_000), (20_000_000, 4_000_000), (30_000_000, 5_000_000),
    (50_000_000, 7_000_000), (float("inf"), 10_000_000),
)
_LIMIT_BOUNDS = np.array([b for b, _ in _LIMIT_TABLE])
_LIMIT_WIDTHS = np.array([w for _, w in _LIMIT_TABLE])


def price_limit_width(prev_close: float) -> float:
    """Daily price-limit width (yen) from the previous close (standard TSE table)."""
    if not prev_close > 0:
        return float("nan")
    return float(_LIMIT_WIDTHS[int(np.searchsorted(_LIMIT_BOUNDS, prev_close, side="right"))])


@dataclass(frozen=True)
class SessionArrays:
    """What the groups need from one session (all on / derived from its ticks)."""

    date: str
    t_open_us: int
    p_open: float
    v_open: float
    t_rel: np.ndarray  # (n,) int64 us since T_open
    price: np.ndarray  # (n,)
    qty: np.ndarray  # (n,)
    cnt: np.ndarray  # (601,) ticks with t_rel <= s * 1e6
    P: np.ndarray  # (601,) last price at each second
    prev_close: float
    avg_volume_20d: float
    is_topix100: bool
    stem: str = ""


def _clip(x: np.ndarray, lo: float, hi: float) -> np.ndarray:
    return np.clip(np.nan_to_num(x, nan=0.0, posinf=hi, neginf=lo), lo, hi)


def _full(v: float) -> np.ndarray:
    return np.full(len(_DEC), v, dtype=np.float64)


def _group_A(a: SessionArrays, daily: DailyStatic) -> list[np.ndarray]:
    gap = a.p_open / a.prev_close - 1.0 if a.prev_close > 0 else 0.0
    pct = float(np.clip(gap * 100.0, -20.0, 20.0))
    z = int(np.searchsorted([0.5, 2.0, 5.0, 10.0], abs(pct), side="right"))
    onehot = [1.0 if i == z else 0.0 for i in range(5)]
    sig = daily.sigma20
    gs = float(np.clip(gap / sig, -10.0, 10.0)) if np.isfinite(sig) and sig > 0 else 0.0
    return [_full(pct), *[_full(v) for v in onehot], _full(gs)]


def _group_B(a: SessionArrays, daily: DailyStatic) -> list[np.ndarray]:
    P = a.P[_DEC]
    tk = tick_sizes(P, np.full(len(P), a.is_topix100))
    cp, o = a.prev_close, a.p_open
    ok = cp > 0
    dist = _clip((P - cp) / tk, -200.0, 200.0) if ok else _full(0.0)
    tko = tick_size(o, a.is_topix100) if o > 0 else 1.0
    has_gap = ok and abs(cp - o) >= tko
    if not has_gap:
        return [_full(0.0), dist, _full(0.0), _full(0.0)]
    progress = _clip((P - o) / (cp - o), -2.0, 3.0)
    last = np.maximum(a.cnt[_DEC] - 1, 0)
    if o > cp:  # gap up: touched = traded back down to prev close
        ext = np.minimum.accumulate(a.price)[last]
        touched = (ext <= cp).astype(np.float64)
        recross = (ext < o).astype(np.float64)
    else:
        ext = np.maximum.accumulate(a.price)[last]
        touched = (ext >= cp).astype(np.float64)
        recross = (ext > o).astype(np.float64)
    return [progress, dist, touched, recross]


def _group_C(a: SessionArrays, ctx: DateContext | None) -> list[np.ndarray]:
    own = _clip((a.P[_DEC] / a.p_open - 1.0) * 1e4, -1000.0, 1000.0)
    if ctx is None or len(ctx.n_act) == 0:
        return [_full(0.0), _full(0.5), own, _full(0.0)]
    off = (a.t_open_us - ctx.day_start_us) / US
    g = np.floor(off + _DEC).astype(np.int64)
    inside = (g >= 0) & (g < len(ctx.n_act))
    gi = np.clip(g, 0, len(ctx.n_act) - 1)
    n = np.where(inside, ctx.n_act[gi], 0)
    enough = n >= MIN_CROSS_SECTION
    med = np.where(enough, ctx.med_ret[gi], 0.0).astype(np.float64)
    up = np.where(enough, ctx.frac_up[gi], 0.5).astype(np.float64)
    return [
        _clip(med, -1000.0, 1000.0),
        up,
        _clip(own - med, -2000.0, 2000.0),
        np.log1p(n.astype(np.float64)),
    ]


def _median_prefix(vals: np.ndarray) -> np.ndarray:
    """``out[c]`` = median of ``vals[:c]`` (``out[0]`` = 0), vals in open order."""
    out = np.zeros(len(vals) + 1)
    for c in range(1, len(vals) + 1):
        out[c] = float(np.median(vals[:c]))
    return out


def _group_D(a: SessionArrays, ctx: DateContext | None, sector: str) -> list[np.ndarray]:
    own_gap = (a.p_open / a.prev_close - 1.0) * 1e4 if a.prev_close > 0 else 0.0
    if ctx is None or len(ctx.stems) == 0:
        return [_full(own_gap), _full(0.0), _full(1.0)]
    off_own = (a.t_open_us - ctx.day_start_us) / US
    known_t = off_own + _DEC  # peers whose open is <= own T_open + s
    others = np.array([s != a.stem.lower() for s in ctx.stems]) & np.isfinite(ctx.gap)
    same = others & np.array([bool(sector) and s == sector for s in ctx.sector])

    def prefix_median(mask: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        o, gp = ctx.open_off[mask], ctx.gap[mask]
        order = np.argsort(o, kind="stable")
        o, gp = o[order], gp[order]
        c = np.searchsorted(o, known_t, side="right")  # peers known at each s
        return _median_prefix(gp)[c], c

    med_sec, n_sec = prefix_median(same)
    med_all, _ = prefix_median(others)
    use_sector = n_sec > 0
    med = np.where(use_sector, med_sec, med_all)
    return [
        _clip(own_gap - med, -2000.0, 2000.0),
        np.log1p(n_sec.astype(np.float64)),
        np.where(use_sector, 0.0, 1.0),
    ]


def _group_E(daily: DailyStatic) -> list[np.ndarray]:
    sig = daily.sigma20
    ok = np.isfinite(sig) and sig > 0

    def r(x: float) -> float:
        return float(np.clip(x / sig, -10.0, 10.0)) if ok and np.isfinite(x) else 0.0

    vol = _nz(daily.mkt_vol20) * 100.0
    ret5 = _nz(daily.mkt_ret5) * 100.0
    return [
        _full(r(daily.ret1)),
        _full(r(daily.ret5)),
        _full(r(daily.ret15)),
        _full(r(daily.ret30)),
        _full(float(np.clip(vol, 0.0, 20.0))),
        _full(float(np.clip(ret5, -30.0, 30.0))),
    ]


def _group_F(a: SessionArrays, ctx_day_start: int) -> list[np.ndarray]:
    delay = max((a.t_open_us - ctx_day_start) / US, 0.0)
    width = price_limit_width(a.prev_close)
    P = a.P[_DEC]
    if np.isfinite(width) and width > 0:
        up = _clip((a.prev_close + width - P) / width * 100.0, -50.0, 300.0)
        dn = _clip((P - (a.prev_close - width)) / width * 100.0, -50.0, 300.0)
    else:
        up = dn = _full(0.0)
    return [_full(float(np.log1p(min(delay, 20_000.0)))), up, dn]


def _group_G(a: SessionArrays, daily: DailyStatic) -> list[np.ndarray]:
    hi, lo = daily.prev_high, daily.prev_low
    if not (np.isfinite(hi) and np.isfinite(lo) and hi >= lo > 0 and a.prev_close > 0):
        return [_full(0.0) for _ in range(6)]
    o = a.p_open
    sig = daily.sigma20
    unit = a.prev_close * sig if np.isfinite(sig) and sig > 0 else float("nan")
    above, below = float(o > hi), float(o < lo)
    pos = float(np.clip((o - lo) / (hi - lo), -2.0, 3.0)) if hi > lo else 0.5
    out_d = (o - hi) if o > hi else (o - lo) if o < lo else 0.0
    out_sig = float(np.clip(out_d / unit, -10.0, 10.0)) if np.isfinite(unit) else 0.0
    rng_sig = float(np.clip((hi - lo) / unit, 0.0, 10.0)) if np.isfinite(unit) else 0.0
    return [_full(v) for v in (1.0, above, below, pos, out_sig, rng_sig)]


def _group_H(a: SessionArrays) -> list[np.ndarray]:
    known = (_DEC >= 60).astype(np.float64)
    c60 = int(a.cnt[60])
    ret = (a.P[60] / a.p_open - 1.0) * 1e4 if a.p_open > 0 else 0.0
    avg_v = max(float(a.avg_volume_20d), 1.0) if np.isfinite(a.avg_volume_20d) else 1.0
    vol60 = float(np.sum(a.qty[:c60]))
    lv = float(np.log10(vol60 / avg_v + 1e-6))
    r = float(np.clip(ret, -1000.0, 1000.0))
    return [known, known * r, known * np.sign(r), known * lv]


def _group_I(a: SessionArrays) -> list[np.ndarray]:
    P = a.P
    tk = tick_sizes(P[_DEC], np.full(len(_DEC), a.is_topix100))
    bps = tk / P[_DEC] * 1e4
    d = np.zeros(len(P))
    d[1:] = np.diff(P)
    cs = np.cumsum(d * d)
    w = np.minimum(_DEC, 60)
    ss = cs[_DEC] - cs[np.maximum(_DEC - 60, 0)]
    sigma = np.sqrt(np.maximum(ss, 0.0) / np.maximum(w, 1)) / tk
    return [_clip(bps, 0.0, 500.0), _clip(sigma, 0.0, 200.0)]


def compute_extras(
    spec: FeatureSpec,
    a: SessionArrays,
    *,
    daily: DailyStatic | None,
    ctx: DateContext | None,
) -> np.ndarray:
    """``(590, n_extra)`` float32 for the groups of ``spec`` (in group order)."""
    daily = daily or DailyStatic()
    day0 = ctx.day_start_us if ctx is not None else day_start_us(a.date)
    cols: list[np.ndarray] = []
    for g in spec.groups:
        if g == "A":
            cols += _group_A(a, daily)
        elif g == "B":
            cols += _group_B(a, daily)
        elif g == "C":
            cols += _group_C(a, ctx)
        elif g == "D":
            cols += _group_D(a, ctx, daily.sector)
        elif g == "E":
            cols += _group_E(daily)
        elif g == "F":
            cols += _group_F(a, day0)
        elif g == "G":
            cols += _group_G(a, daily)
        elif g == "H":
            cols += _group_H(a)
        elif g == "I":
            cols += _group_I(a)
    if not cols:
        return np.zeros((len(_DEC), 0), dtype=np.float32)
    X = np.stack(cols, axis=1)
    return np.nan_to_num(X, nan=0.0, posinf=0.0, neginf=0.0).astype(np.float32)


# Process-wide default stores (one per cache dir) so sessions share the disk caches.
_STORES: dict[str, ContextStore] = {}


def default_store(cache_dir: Path | str | None = None) -> ContextStore:
    key = str(Path(cache_dir or DEFAULT_CACHE_DIR).resolve())
    st = _STORES.get(key)
    if st is None:
        st = _STORES[key] = ContextStore(cache_dir)
    return st
