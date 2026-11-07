"""Tests for the opening-window tick cache (synthetic DuckDB tick files only)."""

from __future__ import annotations

from pathlib import Path

import duckdb
import numpy as np
import pytest

from trading_rl import cache as cache_mod
from trading_rl.cache import (
    CACHE_WINDOW_US,
    CachedSessionUnavailable,
    build_cache,
    cache_status_counts,
    index_tick_files,
    iter_cached_sessions,
    load_cached_session,
)
from trading_rl.data import load_session_ticks

D1, D2, D3 = "2026-08-17", "2026-08-18", "2026-08-19"


def _make_ticks(path: Path, code: str, ticks: list[tuple[str, float, int]]) -> None:
    con = duckdb.connect(str(path))
    con.execute(
        "CREATE TABLE stocks_board (Price DOUBLE, Qty BIGINT, Type VARCHAR, "
        "source VARCHAR, Code VARCHAR, Timestamp VARCHAR, PRIMARY KEY (Code, Timestamp))"
    )
    for ts, price, qty in ticks:
        con.execute(
            "INSERT INTO stocks_board VALUES (?, ?, '1', 'x', ?, ?)", [price, qty, code, ts]
        )
    con.close()


def _write_universe(cache_dir: Path, rows: list[tuple[str, int, str, str]]) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    con.execute("CREATE TABLE u (date DATE, rank INTEGER, stem VARCHAR, code VARCHAR)")
    con.executemany("INSERT INTO u VALUES (?,?,?,?)", rows)
    con.execute(
        f"COPY u TO '{(cache_dir / 'universe.parquet').as_posix()}' (FORMAT PARQUET)"
    )
    con.close()


@pytest.fixture
def env(tmp_path: Path):
    trades = tmp_path / "trades"
    trades.mkdir()
    # 1111: D1 opens 09:00:00.5, window edge ticks; D2 opens late (09:03:00)
    _make_ticks(trades / "1111.duckdb", "11110", [
        (f"{D1} 08:59:59.900000", 99.0, 1),       # pre-open: excluded
        (f"{D1} 09:00:00.500000", 100.0, 5000),   # T_open (auction)
        (f"{D1} 09:00:01.000000", 101.0, 10),
        (f"{D1} 09:10:00.500000", 102.0, 20),     # exactly T_open + 600 s: included
        (f"{D1} 09:10:00.500001", 103.0, 30),     # 1 us later: excluded
        (f"{D1} 09:30:00.000000", 104.0, 40),
        (f"{D2} 09:03:00.066771", 200.0, 7000),   # delayed open
        (f"{D2} 09:05:00.000000", 201.0, 10),
        (f"{D3} 12:30:00.000000", 300.0, 10),     # afternoon only -> no_morning_trade
    ])
    # 2222: only has D1; D2/D3 have no rows at all -> no_data
    _make_ticks(trades / "2222.duckdb", "22220", [
        (f"{D1} 09:00:00.100000", 50.0, 900),
        (f"{D1} 09:00:02.000000", 51.0, 10),
    ])
    # 285a: lower-case file for an upper-case stem; 3333 has no file at all
    _make_ticks(trades / "285a.duckdb", "285A0", [(f"{D1} 09:00:00.000000", 10.0, 1)])
    (trades / "5555.duckdb").write_bytes(b"not a duckdb file")  # corrupt -> error
    cache = tmp_path / "cache"
    _write_universe(cache, [
        (D1, 1, "1111", "11110"), (D1, 2, "2222", "22220"), (D1, 3, "3333", "33330"),
        (D1, 4, "285a", "285A0"), (D1, 5, "5555", "55550"),
        (D2, 1, "1111", "11110"), (D2, 2, "2222", "22220"),
        (D3, 1, "1111", "11110"),
    ])
    cache_mod.clear_shard_cache()
    return trades, cache


def _build(env, **kw):
    trades, cache = env
    return build_cache(cache_dir=cache, trades_dir=trades, workers=2, **kw)


def test_statuses_are_recorded_never_dropped(env):
    _, cache = env
    summary = _build(env)
    assert summary.sessions_total == 8
    counts = cache_status_counts(cache)
    assert counts == {
        "ok": 4,  # 1111 D1, 1111 D2, 2222 D1, 285a D1
        "no_tick_file": 1,
        "no_morning_trade": 1,
        "no_data": 1,
        "error": 1,
    }
    assert sum(counts.values()) == 8


def test_window_is_t_open_to_t_open_plus_600s_inclusive(env):
    _, cache = env
    _build(env)
    s = load_cached_session(D1, "1111", cache)
    assert s.p_open == 100.0 and s.v_open == 5000
    assert s.epoch_us[0] == s.t_open_us
    assert s.epoch_us[-1] - s.t_open_us == CACHE_WINDOW_US
    assert s.price.tolist() == [100.0, 101.0, 102.0]
    assert s.qty.tolist() == [5000, 10, 20]
    assert s.epoch_us.dtype == np.int64 and s.price.dtype == np.float64
    assert s.qty.dtype == np.int64 and len(s.trade_type) == 3


def test_delayed_open_uses_first_morning_trade(env):
    _, cache = env
    _build(env)
    s = load_cached_session(D2, "1111", cache)
    assert s.p_open == 200.0 and len(s) == 2


def test_matches_existing_loader_with_600s_window(env):
    trades, cache = env
    _build(env)
    ref = load_session_ticks(trades / "1111.duckdb", D1, stem="1111", window_us=CACHE_WINDOW_US)
    got = load_cached_session(D1, "1111", cache)
    assert got.t_open_us == ref.t_open_us and got.p_open == ref.p_open
    np.testing.assert_array_equal(got.epoch_us, ref.epoch_us)
    np.testing.assert_array_equal(got.price, ref.price)
    np.testing.assert_array_equal(got.qty, ref.qty)


def test_unavailable_sessions_raise(env):
    _, cache = env
    _build(env)
    for date, stem in [(D3, "1111"), (D2, "2222"), (D1, "3333"), (D1, "5555")]:
        with pytest.raises(CachedSessionUnavailable):
            load_cached_session(date, stem, cache)
    with pytest.raises(CachedSessionUnavailable):
        load_cached_session("2020-01-01", "1111", cache)


def test_rerun_skips_finished_and_retries_only_errors(env):
    trades, cache = env
    _build(env)
    second = _build(env)
    # only the stem with an `error` row is touched again
    assert second.sessions_extracted == 1
    assert second.stems_skipped == 4
    # fix the corrupt file: the retry now succeeds without touching anything else
    (trades / "5555.duckdb").unlink()
    _make_ticks(trades / "5555.duckdb", "55550", [(f"{D1} 09:00:00.000000", 5.0, 1)])
    third = _build(env)
    assert third.sessions_extracted == 1
    assert cache_status_counts(cache)["ok"] == 5
    assert "error" not in cache_status_counts(cache)


def test_incremental_extension_keeps_old_sessions(env):
    trades, cache = env
    _write_universe(cache, [(D1, 1, "1111", "11110")])
    _build(env)
    before = load_cached_session(D1, "1111", cache)
    _write_universe(cache, [(D1, 1, "1111", "11110"), (D2, 1, "1111", "11110")])
    summary = _build(env)
    assert summary.sessions_extracted == 1  # only D2
    cache_mod.clear_shard_cache()
    after = load_cached_session(D1, "1111", cache)
    np.testing.assert_array_equal(after.epoch_us, before.epoch_us)
    assert load_cached_session(D2, "1111", cache).p_open == 200.0


def test_iter_cached_sessions_order_ranks_and_skips_failed(env):
    _, cache = env
    _build(env)
    got = [(s.date, s.stem) for s in iter_cached_sessions(D1, D3, cache_dir=cache)]
    assert got == [(D1, "1111"), (D1, "2222"), (D1, "285a"), (D2, "1111")]
    only_rank1 = [(s.date, s.stem) for s in iter_cached_sessions(D1, D3, ranks=1, cache_dir=cache)]
    assert only_rank1 == [(D1, "1111"), (D2, "1111")]
    picked = [s.stem for s in iter_cached_sessions(D1, D1, ranks=[2, 4], cache_dir=cache)]
    assert picked == ["2222", "285a"]


def test_index_tick_files_is_case_insensitive_and_ignores_strays(tmp_path: Path):
    for name in ["285A.duckdb", "7203.duckdb", "7203.duckdb.wal", "72030.duckdb",
                 "130A_ADMIN_Aug-03-200208-2026_Conflict.duckdb", "7203.duckdb.sidecar.json"]:
        (tmp_path / name).write_bytes(b"x")
    assert sorted(index_tick_files(tmp_path)) == ["285a", "7203"]
