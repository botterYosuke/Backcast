"""Unit tests for Opening 15-Minute RL benchmark and training script (scripts/train_and_eval_rl.py)."""

from __future__ import annotations

import sys
from pathlib import Path

# Add project root and src
PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"
SCRIPTS_DIR = PROJECT_ROOT / "scripts"

for p in (str(SRC_DIR), str(SCRIPTS_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

import duckdb  # noqa: E402
import numpy as np  # noqa: E402
from train_and_eval_rl import (  # noqa: E402
    format_metrics_table,
    main,
    run_benchmarks,
    split_sessions_chronologically,
)

from trading_rl.data import OpeningSessionData  # noqa: E402
from trading_rl.env import TickTradingEnv  # noqa: E402
from trading_rl.policy import ActorCritic  # noqa: E402


def _make_synthetic_session(
    date: str,
    start_price: float = 2000.0,
    slope: float = 1.0,
    length: int = 30,
) -> OpeningSessionData:
    t_open_us = 1786698000000000
    prices = [start_price + i * slope for i in range(length)]
    epoch_us = np.array(
        [t_open_us + i * 15_000_000 for i in range(length)], dtype=np.int64
    )
    price_arr = np.array(prices, dtype=np.float64)
    qty_arr = np.ones(length, dtype=np.int64) * 100
    trade_type_arr = np.array(["1"] * length, dtype=object)

    return OpeningSessionData(
        stem="7203",
        code="72030",
        date=date,
        t_open_us=t_open_us,
        p_open=float(prices[0]),
        v_open=1000,
        epoch_us=epoch_us,
        price=price_arr,
        qty=qty_arr,
        trade_type=trade_type_arr,
    )


def test_split_sessions_chronological_ordering():
    """Verify chronological split guarantees train < val < test without leaks."""
    dates = [f"2026-08-{i:02d}" for i in range(1, 21)]  # 20 dates

    train, val, test = split_sessions_chronologically(
        dates, train_ratio=0.70, val_ratio=0.15
    )

    assert len(train) == 14
    assert len(val) == 3
    assert len(test) == 3

    assert max(train) < min(val)
    assert max(val) < min(test)


def test_split_sessions_with_max_constraints():
    """Verify max_train_sessions and max_test_sessions extract most recent partitions."""
    dates = [f"2026-07-{i:02d}" for i in range(1, 31)]  # 30 dates

    train, val, test = split_sessions_chronologically(
        dates,
        max_train_sessions=5,
        max_test_sessions=3,
        max_val_sessions=0,
    )

    assert len(train) == 5
    assert len(val) == 0
    assert len(test) == 3

    assert max(train) < min(test)
    # Ensure they come from the recent end
    assert test[-1] == "2026-07-30"


def test_run_benchmarks_and_metrics_table():
    """Verify benchmark execution produces results for all 3 policies."""
    sessions = [_make_synthetic_session(f"2026-08-{i:02d}", length=25) for i in range(1, 4)]
    env = TickTradingEnv(sessions, auto_features=True)
    policy = ActorCritic(obs_dim=env.observation_space.shape[0], action_dim=3)

    results = run_benchmarks(policy=policy, test_env=env, test_sessions=sessions, seed=42)

    assert "Random Agent" in results
    assert "Buy-and-Hold" in results
    assert "PPO Agent (Ours)" in results

    table_md = format_metrics_table(results)
    assert "Random Agent" in table_md
    assert "Buy-and-Hold" in table_md
    assert "PPO Agent (Ours)" in table_md
    assert "Total PnL (JPY)" in table_md


def test_cli_end_to_end_with_synthetic_duckdb(tmp_path, monkeypatch):
    """Verify CLI main() runs end-to-end on a synthetic DuckDB database."""
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
        """
    )

    # Insert 4 days of morning data
    days = ["2026-08-11", "2026-08-12", "2026-08-13", "2026-08-14"]
    for d in days:
        con.execute(
            f"INSERT INTO stocks_board VALUES (2500.0, 100, '1', 'src', '72030', '{d} 09:00:00.000000')"
        )
        con.execute(
            f"INSERT INTO stocks_board VALUES (2510.0, 200, '1', 'src', '72030', '{d} 09:05:00.000000')"
        )
        con.execute(
            f"INSERT INTO stocks_board VALUES (2520.0, 300, '1', 'src', '72030', '{d} 09:14:59.000000')"
        )

    con.execute(
        f"INSERT INTO stocks_board_metadata VALUES ('72030', '{days[0]} 09:00:00', '{days[-1]} 09:15:00', 12, CURRENT_TIMESTAMP)"
    )
    con.close()

    report_file = tmp_path / "research_report.md"
    model_file = tmp_path / "model_out.pt"

    test_args = [
        "train_and_eval_rl.py",
        "--stem", "7203",
        "--db-path", str(db_file),
        "--max-train-sessions", "2",
        "--max-test-sessions", "2",
        "--iterations", "1",
        "--output-report", str(report_file),
        "--model-path", str(model_file),
    ]

    monkeypatch.setattr(sys, "argv", test_args)

    ret = main()
    assert ret == 0

    assert report_file.is_file()
    assert model_file.is_file()

    content = report_file.read_text(encoding="utf-8")
    assert "Benchmark & Evaluation Report" in content
    assert "PPO Agent (Ours)" in content
    assert "Buy-and-Hold" in content
