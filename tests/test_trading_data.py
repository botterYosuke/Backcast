"""Unit tests for the DuckDB tick data extraction pipeline (trading_rl.data)."""

from __future__ import annotations

import duckdb
import numpy as np
import pytest

from trading_rl.data import (
    OPENING_WINDOW_US,
    NoMorningSessionError,
    get_session_dates,
    load_session_ticks,
)


@pytest.fixture
def memory_db():
    con = duckdb.connect(":memory:")
    con.execute(
        """
        CREATE TABLE stocks_board (
            Price DOUBLE,
            Qty BIGINT,
            Type VARCHAR,
            source VARCHAR,
            Code VARCHAR,
            Timestamp VARCHAR
        );
        CREATE TABLE stocks_board_metadata (
            Code VARCHAR,
            from_timestamp TIMESTAMP,
            to_timestamp TIMESTAMP,
            record_count BIGINT,
            last_updated TIMESTAMP
        );
        """
    )
    yield con
    con.close()


def test_detect_t_open_normal_0900(memory_db):
    """Detect T_open starting promptly at 09:00:00."""
    ticks = [
        # Pre-market off-hours trade (should be ignored)
        ("2026-08-14 08:59:59.900000", 2500.0, 100, "1", "72030"),
        # Normal morning opening match
        ("2026-08-14 09:00:00.000000", 2510.0, 5000, "1", "72030"),
        ("2026-08-14 09:01:00.000000", 2512.0, 200, "2", "72030"),
        ("2026-08-14 09:14:59.000000", 2520.0, 300, "1", "72030"),
        # Beyond 15 minutes (09:15:00.000001+)
        ("2026-08-14 09:16:00.000000", 2525.0, 400, "1", "72030"),
    ]
    memory_db.executemany(
        "INSERT INTO stocks_board VALUES (?, ?, ?, 'manual', ?, ?)",
        [(p, q, t, c, ts) for ts, p, q, t, c in ticks],
    )
    memory_db.execute(
        "INSERT INTO stocks_board_metadata VALUES ('72030', '2026-08-14', '2026-08-14', 5, CURRENT_TIMESTAMP)"
    )

    session = load_session_ticks(memory_db, "2026-08-14", stem="7203")

    assert session.stem == "7203"
    assert session.code == "72030"
    assert session.date == "2026-08-14"
    assert session.p_open == 2510.0
    assert session.v_open == 5000

    # 09:00:00.000000 in epoch microseconds
    expected_t_open_us = memory_db.execute(
        "SELECT epoch_us(TRY_CAST('2026-08-14 09:00:00.000000' AS TIMESTAMP))"
    ).fetchone()[0]
    assert session.t_open_us == expected_t_open_us

    # Exact 3 ticks within [09:00:00, 09:15:00]
    assert len(session) == 3
    np.testing.assert_array_equal(session.price, [2510.0, 2512.0, 2520.0])
    np.testing.assert_array_equal(session.qty, [5000, 200, 300])


def test_detect_t_open_delayed_special_quotes(memory_db):
    """Detect T_open for delayed open (e.g. 09:03:15 due to special quotes)."""
    ticks = [
        # Pre-market off-hours
        ("2026-08-14 08:58:00.000000", 1000.0, 100, "1", "99840"),
        # First trade delayed until 09:03:15.500000
        ("2026-08-14 09:03:15.500000", 1050.0, 10000, "1", "99840"),
        ("2026-08-14 09:05:00.000000", 1052.0, 500, "2", "99840"),
        ("2026-08-14 09:18:15.500000", 1060.0, 800, "1", "99840"),  # Exactly +15 min
        ("2026-08-14 09:18:16.000000", 1061.0, 300, "1", "99840"),  # Beyond +15 min
    ]
    memory_db.executemany(
        "INSERT INTO stocks_board VALUES (?, ?, ?, 'manual', ?, ?)",
        [(p, q, t, c, ts) for ts, p, q, t, c in ticks],
    )

    session = load_session_ticks(memory_db, "2026-08-14", stem="9984", code="99840")

    assert session.p_open == 1050.0
    assert session.v_open == 10000

    expected_t_open = memory_db.execute(
        "SELECT epoch_us(TRY_CAST('2026-08-14 09:03:15.500000' AS TIMESTAMP))"
    ).fetchone()[0]
    assert session.t_open_us == expected_t_open

    # Must include ticks up to 09:18:15.500000 (3 ticks) and exclude 09:18:16.000000
    assert len(session) == 3
    np.testing.assert_array_equal(session.price, [1050.0, 1052.0, 1060.0])


def test_window_bounded_strictly_15_minutes(memory_db):
    """Enforce microsecond boundary: T_open + 900_000_000 inclusive, + 900_000_001 excluded."""
    ticks = [
        ("2026-08-14 09:00:00.000000", 100.0, 10, "1", "12340"),
        ("2026-08-14 09:15:00.000000", 101.0, 20, "1", "12340"),  # Exactly +900s
        ("2026-08-14 09:15:00.000001", 102.0, 30, "1", "12340"),  # +900s + 1us
    ]
    memory_db.executemany(
        "INSERT INTO stocks_board VALUES (?, ?, ?, 'manual', ?, ?)",
        [(p, q, t, c, ts) for ts, p, q, t, c in ticks],
    )

    session = load_session_ticks(memory_db, "2026-08-14", code="12340")

    assert len(session) == 2
    assert session.epoch_us[-1] - session.t_open_us == OPENING_WINDOW_US
    np.testing.assert_array_equal(session.price, [100.0, 101.0])


def test_no_morning_session_raises(memory_db):
    """Raise NoMorningSessionError when only pre-market trades exist."""
    ticks = [
        ("2026-08-14 08:30:00.000000", 100.0, 10, "1", "12340"),
        ("2026-08-14 08:59:59.999999", 100.0, 10, "1", "12340"),
    ]
    memory_db.executemany(
        "INSERT INTO stocks_board VALUES (?, ?, ?, 'manual', ?, ?)",
        [(p, q, t, c, ts) for ts, p, q, t, c in ticks],
    )

    with pytest.raises(NoMorningSessionError):
        load_session_ticks(memory_db, "2026-08-14", code="12340")


def test_structured_records_and_metadata(memory_db):
    """Verify structured NumPy array conversion and session metadata dict."""
    ticks = [
        ("2026-08-14 09:00:00.000000", 150.5, 100, "1", "72030"),
        ("2026-08-14 09:01:00.000000", 151.0, 200, "2", "72030"),
    ]
    memory_db.executemany(
        "INSERT INTO stocks_board VALUES (?, ?, ?, 'manual', ?, ?)",
        [(p, q, t, c, ts) for ts, p, q, t, c in ticks],
    )

    session = load_session_ticks(memory_db, "2026-08-14", code="72030", stem="7203")
    rec = session.to_records()

    assert isinstance(rec, np.ndarray)
    assert rec.dtype.names == ("epoch_us", "price", "qty", "trade_type")
    assert rec["price"][0] == 150.5
    assert rec["qty"][1] == 200
    assert rec["trade_type"][1] == "2"

    meta = session.to_dict()
    assert meta["stem"] == "7203"
    assert meta["count"] == 2
    assert meta["p_open"] == 150.5


def test_load_from_disk_and_auto_resolve_code(tmp_path):
    """Verify loading from a real .duckdb file on disk with automatic code resolution."""
    db_file = tmp_path / "7203.duckdb"
    con = duckdb.connect(str(db_file))
    con.execute(
        """
        CREATE TABLE stocks_board (
            Price DOUBLE, Qty BIGINT, Type VARCHAR, source VARCHAR, Code VARCHAR, Timestamp VARCHAR
        );
        CREATE TABLE stocks_board_metadata (
            Code VARCHAR, from_timestamp TIMESTAMP, to_timestamp TIMESTAMP, record_count BIGINT, last_updated TIMESTAMP
        );
        INSERT INTO stocks_board_metadata VALUES ('72030', '2026-08-14', '2026-08-14', 10, CURRENT_TIMESTAMP);
        INSERT INTO stocks_board VALUES (2800.0, 1000, '1', 'src', '72030', '2026-08-14 09:00:00.000000');
        INSERT INTO stocks_board VALUES (2805.0, 500, '2', 'src', '72030', '2026-08-14 09:02:00.000000');
        """
    )
    con.close()

    session = load_session_ticks(db_file, "2026-08-14")
    assert session.stem == "7203"
    assert session.code == "72030"
    assert len(session) == 2
    assert session.p_open == 2800.0

    dates = get_session_dates(db_file)
    assert dates == ["2026-08-14"]
