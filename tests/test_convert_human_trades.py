"""Tests for scripts/convert_human_trades.py (synthetic fills + synthetic session)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import numpy as np

from trading_rl.data import OpeningSessionData
from trading_rl.scalp_bc import load_trades_csv

_SPEC = importlib.util.spec_from_file_location(
    "convert_human_trades",
    Path(__file__).resolve().parents[1] / "scripts" / "convert_human_trades.py",
)
cht = importlib.util.module_from_spec(_SPEC)  # type: ignore[arg-type]
sys.modules["convert_human_trades"] = cht
_SPEC.loader.exec_module(cht)  # type: ignore[union-attr]

US = 1_000_000
DAY = "2026-07-08"
# naive JST read as UTC: 2026-07-08 00:00 as epoch seconds
BASE_US = int(np.datetime64("2026-07-08T00:00:00", "us").astype("int64"))
T_OPEN = BASE_US + (9 * 3600) * US  # 09:00:00


def _sec(h: int, m: int, s: int) -> int:
    return BASE_US + ((h * 3600) + m * 60 + s) * US


def _session(ticks: list[tuple[int, float]]) -> OpeningSessionData:
    t = np.array([a for a, _ in ticks], dtype=np.int64)
    p = np.array([b for _, b in ticks], dtype=np.float64)
    return OpeningSessionData(
        stem="1234", code="12340", date=DAY, t_open_us=int(t[0]),
        p_open=float(p[0]), v_open=100, epoch_us=t, price=p,
        qty=np.full(len(t), 100, dtype=np.int64), trade_type=np.full(len(t), "", dtype=object),
    )


def _fill(idx, kind, side, minute, px, qty=100, note="", source="csv", stem="1234"):
    return cht.Fill(
        idx=idx, date=DAY, stem=stem, minute=minute, kind=kind, side=side, qty=qty,
        price=px, source=source, note=note, flagged=bool(cht.CONTRADICTION_RE.search(note)),
    )


def _ticks():
    return [
        (_sec(9, 0, 0), 1000.0),
        (_sec(9, 0, 5), 1001.0),
        (_sec(9, 0, 30), 1002.0),
        (_sec(9, 1, 10), 1003.0),
        (_sec(9, 1, 20), 1002.0),
        (_sec(9, 1, 40), 1003.0),
        (_sec(9, 2, 15), 1001.0),
        (_sec(9, 5, 0), 1005.0),
        (_sec(9, 10, 0), 1006.0),
    ]


def _convert(fills, ticks=None):
    sess = _session(ticks or _ticks())
    return cht.convert(
        fills, {(DAY, "1234"): False}, lambda d, s: sess if (d, s) == (DAY, "1234") else None
    )


def test_minute_to_second_exact_match_and_ordering():
    # entry in minute 9:01 at 1003 -> 09:01:10 ; exit same minute at 1003 -> next >= 09:01:10
    fills = [_fill(0, "entry", 1, 541, 1003.0), _fill(1, "exit", -1, 541, 1002.0)]
    res = _convert(fills)
    assert fills[0].us == _sec(9, 1, 10)
    assert fills[1].us == _sec(9, 1, 20)  # next tick at/after the entry time
    assert fills[0].quality == fills[1].quality == "exact"
    assert res.stats["legs_usable"] == 1
    assert [r["kind"] for r in res.rows] == ["entry", "exit"]
    assert res.rows[0]["side"] == "buy" and res.rows[1]["side"] == "sell"


def test_nearest_within_one_tick_and_unmatched():
    fills = [
        _fill(0, "entry", 1, 541, 1004.0),  # no 1004 tick in minute; 1003 is 1 tick away
        _fill(1, "exit", -1, 542, 1500.0),  # nothing near
    ]
    _convert(fills)
    assert fills[0].quality == "near" and fills[0].us == _sec(9, 1, 10)
    assert fills[1].quality == "unmatched" and fills[1].us is None


def test_window_filtering_before_10s_and_outside():
    fills = [
        _fill(0, "entry", 1, 540, 1001.0),  # tick at 09:00:05 -> before T_open+10s
        _fill(1, "exit", -1, 540, 1002.0),  # tick at 09:00:30 -> in window
        _fill(2, "entry", 1, 541, 1003.0),
        _fill(3, "exit", -1, 700, 1010.0),  # minute 11:40 not covered by cache
    ]
    res = _convert(fills)
    assert res.stats["entries_before_10s"] == 1
    assert fills[3].quality == "outside"
    assert res.stats["legs_usable"] == 0  # leg(0,1) before 10 s, entry 2 never closed
    assert res.stats["legs_dropped_out_of_window"] == 1
    assert res.rows == []


def test_fifo_pairing_and_overnight_exit_excluded():
    fills = [
        _fill(0, "exit", -1, 540, 1002.0, qty=100),  # closes a position opened earlier
        _fill(1, "entry", 1, 541, 1003.0, qty=200),
        _fill(2, "exit", -1, 542, 1001.0, qty=100),
        _fill(3, "exit", -1, 545, 1005.0, qty=100),
    ]
    legs, un_exits, un_entries = cht.pair_fills(fills)
    assert [(lg.entry.idx, lg.exit.idx, lg.qty) for lg in legs] == [(1, 2, 100), (1, 3, 100)]
    assert [f.idx for f in un_exits] == [0] and un_entries == []
    res = _convert(fills)
    assert res.stats["legs_usable"] == 2
    # the entry fill is written once; the overnight exit never appears
    assert [r["kind"] for r in res.rows].count("entry") == 1
    assert all(r["us"] != _sec(9, 0, 30) for r in res.rows)


def test_close_before_open_same_minute_waits_for_open():
    fills = [_fill(0, "exit", -1, 541, 1003.0), _fill(1, "entry", 1, 541, 1003.0)]
    legs, un_exits, _ = cht.pair_fills(fills)
    assert len(legs) == 1 and not un_exits


def test_short_direction_and_flagged_note_dropped():
    fills = [
        _fill(0, "entry", -1, 541, 1003.0, note="買いが指値より上で約定", source="image"),
        _fill(1, "exit", 1, 542, 1001.0, source="image"),
    ]
    assert fills[0].direction == fills[1].direction == "S"
    res = _convert(fills)
    assert res.stats["legs_dropped_flagged_note"] == 1 and res.rows == []


def test_not_in_universe_or_uncached_session_skipped():
    f = [_fill(0, "entry", 1, 541, 1003.0, stem="9999")]
    f[0].date = "2026-09-10"
    res = cht.convert(f, {(DAY, "1234"): False}, lambda d, s: None)
    assert res.stats["fills_not_in_universe"] == 1 and res.rows == []
    g = [_fill(0, "entry", 1, 541, 1003.0)]
    res = cht.convert(g, {(DAY, "1234"): False}, lambda d, s: None)
    assert res.stats["fills_no_cached_session"] == 1


def test_output_loads_with_bc_loader(tmp_path):
    fills = [_fill(0, "entry", 1, 541, 1003.0), _fill(1, "exit", -1, 542, 1001.0)]
    res = _convert(fills)
    path = tmp_path / "t.csv"
    cht.write_csv(path, cht.BC_COLUMNS, res.rows)
    rows = load_trades_csv(path)[(DAY, "1234")]
    assert [r.kind for r in rows] == ["entry", "exit"]
    assert rows[0].side == 1 and rows[1].side == -1 and rows[0].order == "market"
