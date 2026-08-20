"""Tests for the read-only tick repository."""

from __future__ import annotations

from datetime import date

import pytest

from tickreplay.repository import SymbolNotFoundError, TickRepository

# 2026-08-14 (木) と 2026-08-17 (月) にデータ、間の週末は無し。
ROWS = [
    ("2026-08-14 09:00:00.100000", 100.0, 100, "1", "13010"),
    ("2026-08-14 09:00:00.200000", 101.0, 200, "2", "13010"),
    ("2026-08-17 09:00:00.000000", 102.0, 300, "1", "13010"),
    ("2026-08-17 09:00:30.500000", 103.0, 400, "2", "13010"),
    ("2026-08-17 09:01:00.000000", 99.0, 500, "1", "13010"),
]


@pytest.fixture
def repository(trades_dir, tick_db_factory):
    tick_db_factory("1301", ROWS)
    repo = TickRepository(trades_dir)
    yield repo
    repo.close()


def test_symbol_listing_skips_sync_artifacts(trades_dir, tick_db_factory):
    tick_db_factory("1301", ROWS)
    tick_db_factory("130A0", ROWS)
    (trades_dir / "130A_ADMIN_Aug-03-200208-2026_Conflict.duckdb").write_bytes(b"")

    repo = TickRepository(trades_dir)
    try:
        assert repo.list_symbol_stems() == ["1301", "130A0"]
    finally:
        repo.close()


def test_canonical_code_comes_from_the_largest_metadata_row(
    trades_dir, tick_db_factory
):
    # 実データの 3823.duckdb と同じ形: 本体コードと少数行の別コードが同居する。
    rows = ROWS + [("2026-01-22 18:38:40.000000", 1.0, 1, "1", "1301")]
    tick_db_factory("1301", rows)

    repo = TickRepository(trades_dir)
    try:
        info = repo.symbol_info("1301")
        assert info.code == "13010"
        assert info.other_codes == ("1301",)
        assert (info.first_date, info.last_date) == ("2026-08-14", "2026-08-17")
    finally:
        repo.close()


def test_unknown_and_malformed_symbols_are_rejected(repository):
    with pytest.raises(SymbolNotFoundError):
        repository.symbol_info("9999")
    with pytest.raises(SymbolNotFoundError):
        repository.path_for("../secrets")


def test_load_session_returns_only_that_day(repository):
    session = repository.load_session("1301", date(2026, 8, 17))

    assert session.code == "13010"
    assert session.date == "2026-08-17"
    assert session.price == [102.0, 103.0, 99.0]
    assert session.qty == [300, 400, 500]
    assert session.trade_type == ["1", "2", "1"]


def test_timestamps_are_epoch_microseconds_read_as_wall_clock(repository):
    session = repository.load_session("1301", date(2026, 8, 17))

    # 09:00:30.5 - 09:00:00 = 30.5 秒
    assert session.us[1] - session.us[0] == 30_500_000


def test_a_day_without_trades_is_empty_not_an_error(repository):
    assert repository.load_session("1301", date(2026, 8, 15)).us == []


def test_unparseable_timestamps_and_null_prices_are_dropped(
    trades_dir, tick_db_factory
):
    rows = [
        ("2026-08-17 09:00:00.000000", 100.0, 100, "1", "13010"),
        ("2026-08-17 broken", 101.0, 100, "1", "13010"),
        ("2026-08-17 09:00:02.000000", None, 100, "1", "13010"),
    ]
    tick_db_factory("1301", rows, metadata=[("13010", rows[0][0], rows[0][0], 3)])

    repo = TickRepository(trades_dir)
    try:
        assert repo.load_session("1301", date(2026, 8, 17)).price == [100.0]
    finally:
        repo.close()


def test_identical_timestamps_are_nudged_to_stay_strictly_increasing(
    trades_dir, tick_db_factory
):
    stamp = "2026-08-17 09:00:00.000000"
    rows = [(stamp, 100.0 + index, 100, "1", "13010") for index in range(3)]
    tick_db_factory("1301", rows, metadata=[("13010", stamp, stamp, 3)])

    repo = TickRepository(trades_dir)
    try:
        session = repo.load_session("1301", date(2026, 8, 17))
        # 同一マイクロ秒の約定を捨てず、1 マイクロ秒ずつずらして全件残す。
        assert len(session.us) == 3
        assert session.us == sorted(set(session.us))
        assert session.us[2] - session.us[0] == 2
    finally:
        repo.close()


def test_find_session_returns_the_day_itself_when_it_has_data(repository):
    assert repository.find_session("1301", date(2026, 8, 17), -1) == date(2026, 8, 17)


def test_find_session_walks_back_over_the_weekend(repository):
    # 日曜から遡ると、土日を飛ばして木曜のデータ日に着く。
    assert repository.find_session("1301", date(2026, 8, 16), -1) == date(2026, 8, 14)


def test_find_session_walks_forward(repository):
    assert repository.find_session("1301", date(2026, 8, 15), 1) == date(2026, 8, 17)


def test_find_session_stops_outside_the_covered_range(repository):
    assert repository.find_session("1301", date(2026, 8, 13), -1) is None
    assert repository.find_session("1301", date(2026, 8, 18), 1) is None


def test_find_session_with_direction_zero_checks_only_that_day(repository):
    assert repository.find_session("1301", date(2026, 8, 15), 0) is None
    assert repository.find_session("1301", date(2026, 8, 14), 0) == date(2026, 8, 14)
