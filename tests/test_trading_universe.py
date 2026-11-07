"""Tests for the point-in-time universe builder (synthetic DuckDB files only)."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import duckdb
import numpy as np
import pytest

from trading_rl.universe import (
    build_universe,
    load_universe,
    tick_size,
    tick_sizes,
)

# --------------------------------------------------------------------------- #
# tick_size
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("price", "expected"),
    [
        (1, 1), (3_000, 1), (3_000.5, 5), (5_000, 5), (5_001, 10),
        (30_000, 10), (30_001, 50), (50_000, 50), (50_001, 100),
        (300_000, 100), (300_001, 500), (500_000, 500), (500_001, 1_000),
        (3_000_000, 1_000), (3_000_001, 5_000), (5_000_000, 5_000),
        (5_000_001, 10_000), (30_000_000, 10_000), (30_000_001, 50_000),
        (50_000_000, 50_000), (50_000_001, 100_000),
    ],
)
def test_tick_size_general_table_boundaries(price, expected):
    assert tick_size(price, False) == expected


@pytest.mark.parametrize(
    ("price", "expected"),
    [
        (1, 0.1), (1_000, 0.1), (1_000.1, 0.5), (3_000, 0.5), (3_001, 1),
        (10_000, 1), (10_001, 5), (30_000, 5), (30_001, 10), (100_000, 10),
        (100_001, 50), (300_000, 50), (300_001, 100), (1_000_000, 100),
        (1_000_001, 500),
    ],
)
def test_tick_size_topix100_table_boundaries(price, expected):
    assert tick_size(price, True) == expected


def test_tick_size_tables_differ_where_the_tse_tables_differ():
    assert tick_size(2_500, False) == 1 and tick_size(2_500, True) == 0.5
    assert tick_size(8_000, False) == 10 and tick_size(8_000, True) == 1


@pytest.mark.parametrize("bad", [0, -1, float("nan")])
def test_tick_size_rejects_non_positive(bad):
    with pytest.raises(ValueError):
        tick_size(bad, False)


def test_tick_sizes_vectorised_matches_scalar_and_nan_for_invalid():
    prices = np.array([2_500.0, 3_000.0, 3_001.0, 8_000.0, np.nan, 0.0])
    t100 = np.array([True, False, True, False, True, True])
    out = tick_sizes(prices, t100)
    assert out[:4].tolist() == [0.5, 1.0, 1.0, 10.0]
    assert np.isnan(out[4]) and np.isnan(out[5])


# --------------------------------------------------------------------------- #
# build_universe
# --------------------------------------------------------------------------- #
DAYS = [
    date(2024, 1, 4), date(2024, 1, 5), date(2024, 1, 9),  # 1/8 = holiday
    date(2024, 1, 10), date(2024, 1, 11),
]
D1, D2, D3, D4, D5 = DAYS


def _daily(path: Path, code: str, rows: dict[date, tuple[float, int, float]]) -> None:
    con = duckdb.connect(str(path))
    con.execute(
        "CREATE TABLE stocks_daily (Code VARCHAR, Close DOUBLE, Volume BIGINT, "
        "TurnoverValue DOUBLE, Date TIMESTAMP)"
    )
    for d, (close, vol, tv) in rows.items():
        con.execute("INSERT INTO stocks_daily VALUES (?,?,?,?,?)", [code, close, vol, tv, d])
    con.close()


def _listing(path: Path, snaps: dict[str, list[tuple[str, str, str]]]) -> None:
    con = duckdb.connect(str(path))
    con.execute(
        "CREATE TABLE listed_info (Date VARCHAR, Code VARCHAR, MarketCode VARCHAR, "
        "ScaleCategory VARCHAR)"
    )
    for snap, items in snaps.items():
        for code, market, scale in items:
            con.execute("INSERT INTO listed_info VALUES (?,?,?,?)", [snap, code, market, scale])
    con.close()


@pytest.fixture
def world(tmp_path: Path):
    daily = tmp_path / "daily"
    daily.mkdir()
    # tv[d] -> turnover; rank on D uses tv[P(D)] only.
    spec = {
        # stem: (code, market, scale, {date: tv})
        "1111": ("11110", "0111", "TOPIX Core30",
                 {D1: 500, D2: 100, D3: 900, D4: 100, D5: 100}),
        "2222": ("22220", "0111", "-",
                 {D1: 400, D2: 300, D3: 100, D4: 800, D5: 100}),
        "3333": ("33330", "0111", "TOPIX Small 1",
                 {D1: 300, D2: 200, D3: 50, D4: 50, D5: 700}),
        # fund (market 0109): always the biggest
        "1570": ("15700", "0109", "-", {d: 10_000 for d in DAYS}),
        # delisted after D3: still ranked on D4 (P=D3) but absent afterwards
        "4444": ("44440", "0111", "-", {D1: 10, D2: 10, D3: 600}),
    }
    for stem, (code, _m, _s, tvs) in spec.items():
        _daily(
            daily / f"{stem}.duckdb", code,
            {d: (1000.0 + i, 100 * (i + 1), float(tv)) for i, (d, tv) in enumerate(tvs.items())},
        )
    items = [(c, m, s) for c, m, s, _ in spec.values()]
    pit = tmp_path / "pit.duckdb"
    _listing(pit, {"2024-01-04": items})
    cur = tmp_path / "cur.duckdb"  # 4-digit codes, later snapshot
    _listing(cur, {"2024-01-10": [(c[:4], m, s) for c, m, s in items]})
    return tmp_path, daily, pit, cur


def _build(world, **kw):
    tmp, daily, pit, cur = world
    kw.setdefault("top_n", 2)
    return build_universe(
        start="2024-01-04", end="2024-01-11", daily_dir=daily, trades_dir=tmp / "none",
        listed_pit_path=pit, listed_path=cur, cache_dir=tmp / "cache",
        min_breadth=3, workers=2, **kw,
    )


def _rows(world, d):
    return [r for r in load_universe(world[0] / "cache") if str(r["date"]) == d]


def test_rank_uses_only_previous_trading_day_and_skips_holiday(world):
    res = _build(world)
    # D1 has no previous day in the panel -> not a universe date.
    assert res.first_date == "2024-01-05"
    assert res.n_dates == 4
    assert [r["stem"] for r in _rows(world, "2024-01-05")] == ["1111", "2222"]  # P=D1
    # D3 (2024-01-09): P = 2024-01-05 (holiday 1/8 absent from the calendar)
    rows = _rows(world, "2024-01-09")
    assert [str(r["prev_date"]) for r in rows] == ["2024-01-05"] * 2
    assert [r["stem"] for r in rows] == ["2222", "3333"]
    # D4: P = D3, delisted 4444 (tv 600) is kept; funds excluded by default
    rows = _rows(world, "2024-01-10")
    assert [r["stem"] for r in rows] == ["1111", "4444"]
    assert [r["rank"] for r in rows] == [1, 2]
    assert rows[0]["prev_turnover_value"] == 900


def test_same_day_data_never_leaks_into_ranking(world):
    _build(world)
    # D5's own tv favours 3333 (700) but the ranking uses D4 (2222 = 800).
    rows = _rows(world, "2024-01-11")
    assert rows[0]["stem"] == "2222"


def test_funds_excluded_by_default_and_audited(world):
    _build(world)
    assert all(r["stem"] != "1570" for r in load_universe(world[0] / "cache"))
    audit_file = (world[0] / "cache" / "universe_audit.parquet").as_posix()
    con = duckdb.connect()
    got = con.execute(
        f"SELECT min(funds_in_raw_topn) FROM read_parquet('{audit_file}')"
    ).fetchone()
    assert got == (1,)  # the fund would have been rank 1 every day


def test_include_funds_flag_keeps_funds(world):
    _build(world, include_funds=True)
    rows = _rows(world, "2024-01-05")
    assert rows[0]["stem"] == "1570" and rows[0]["is_fund"] is True


def test_topix100_and_tick_size_from_prev_close(world):
    _build(world, top_n=3)
    by_stem = {r["stem"]: r for r in _rows(world, "2024-01-05")}
    assert by_stem["1111"]["is_topix100"] is True
    assert by_stem["1111"]["tick_table"] == "topix100"
    assert by_stem["1111"]["prev_close"] == 1000.0
    assert by_stem["1111"]["tick_size"] == 0.1  # TOPIX100 table, <= 1000 yen
    assert by_stem["2222"]["tick_table"] == "general"
    assert by_stem["2222"]["tick_size"] == 1.0


def test_avg_volume_20d_is_mean_over_available_calendar_days(world):
    _build(world, top_n=3)
    row = next(r for r in _rows(world, "2024-01-10") if r["stem"] == "1111")
    # 1111 volumes: 100, 200, 300 on D1..D3 (P = D3) -> mean 200 over 3 days
    assert row["avg_volume_20d"] == pytest.approx(200.0)
    assert row["n_days_20d"] == 3


def test_listing_is_point_in_time(tmp_path: Path):
    daily = tmp_path / "daily"
    daily.mkdir()
    for stem in ("1111", "2222"):
        _daily(
            daily / f"{stem}.duckdb", stem + "0",
            {d: (100.0, 10, 100.0 if stem == "1111" else 50.0) for d in DAYS},
        )
    pit = tmp_path / "pit.duckdb"
    # 1111 is TOPIX100 in the first snapshot, demoted in the second
    _listing(pit, {
        "2024-01-04": [("11110", "0111", "TOPIX Core30"), ("22220", "0111", "-")],
        "2024-01-10": [("11110", "0111", "-"), ("22220", "0111", "-")],
    })
    res = build_universe(
        start="2024-01-04", end="2024-01-11", daily_dir=daily, trades_dir=tmp_path,
        listed_pit_path=pit, listed_path=tmp_path / "missing.duckdb",
        cache_dir=tmp_path / "cache", min_breadth=2, top_n=2, workers=2,
    )
    assert res.n_rows > 0
    rows = {(str(r["date"]), r["stem"]): r for r in load_universe(tmp_path / "cache")}
    # D4 = 2024-01-10 has P = 2024-01-09 < snapshot 2024-01-10 -> old snapshot
    assert rows[("2024-01-10", "1111")]["is_topix100"] is True
    # D5 = 2024-01-11 has P = 2024-01-10 >= snapshot 2024-01-10 -> demoted
    assert rows[("2024-01-11", "1111")]["is_topix100"] is False
