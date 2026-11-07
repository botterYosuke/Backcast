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
import pytest  # noqa: E402
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


def _make_db(tmp_path, days: list[str], *, step_s: int = 20, until_s: int = 700):
    """Synthetic 7203.duckdb with a wiggly uptrend from 09:00:00 to 09:00:00 + until_s."""
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
    rows = []
    for d in days:
        for k, sec in enumerate(range(0, until_s + 1, step_s)):
            hh, rem = divmod(9 * 3600 + sec, 3600)
            mm, ss = divmod(rem, 60)
            price = 2500.0 + 0.5 * (k % 7) + 0.25 * k
            rows.append(
                (price, 100, "1", "src", "72030", f"{d} {hh:02d}:{mm:02d}:{ss:02d}.000000")
            )
    con.executemany("INSERT INTO stocks_board VALUES (?, ?, ?, ?, ?, ?)", rows)
    con.execute(
        f"INSERT INTO stocks_board_metadata VALUES ('72030', '{days[0]} 09:00:00', '{days[-1]} 09:15:00', {len(rows)}, CURRENT_TIMESTAMP)"
    )
    con.close()
    return db_file


def test_default_cli_arguments():
    from train_and_eval_rl import build_arg_parser

    args = build_arg_parser().parse_args([])
    assert args.window_seconds == 600.0
    assert args.warmup_seconds == 15.0
    assert args.slippage == 0.5
    assert args.total_timesteps == 1_000_000
    assert args.rollout_steps == 4096
    assert args.batch_size == 256
    assert args.eval_every == 10
    assert args.max_val_sessions == 15
    assert args.max_train_sessions == 100
    assert args.max_test_sessions == 20
    assert args.model_path is None and args.output_report is None
    assert args.iterations is None


def test_run_benchmarks_baselines_share_env_settings():
    """Buy-and-Hold pays the env's slippage (entry + exit): 2 * slippage per session."""
    sessions = [_make_synthetic_session(f"2026-08-{i:02d}", length=25) for i in range(1, 4)]
    results = {}
    for slip in (0.0, 0.5):
        env = TickTradingEnv(sessions, slippage=slip, auto_features=True)
        policy = ActorCritic(obs_dim=env.observation_space.shape[0], action_dim=3)
        results[slip] = run_benchmarks(policy=policy, test_env=env, seed=1)
    diff = results[0.0]["Buy-and-Hold"].total_pnl - results[0.5]["Buy-and-Hold"].total_pnl
    assert diff == pytest.approx(3 * 2 * 0.5)


def test_is_better_validation_tie_prefers_fewer_trades():
    from train_and_eval_rl import is_better_validation

    assert is_better_validation(1.0, 5, None, None)
    assert is_better_validation(2.0, 9, 1.0, 1)
    assert not is_better_validation(0.5, 1, 1.0, 5)
    assert is_better_validation(1.0, 3, 1.0, 5)
    assert not is_better_validation(1.0, 5, 1.0, 5)
    assert not is_better_validation(1.0, 7, 1.0, 5)


def test_auto_notes_are_factual():
    from train_and_eval_rl import build_auto_notes

    from trading_rl.evaluator import EvaluationResult

    def res(pnl, trades, closed, lo=20.0, hi=500.0):
        return EvaluationResult(
            total_pnl=pnl, trade_count=trades, win_rate=0.0, profit_factor=1.0,
            max_drawdown=0.0, episodes=1, mean_episode_pnl=pnl,
            closed_trade_pnls=closed, fill_offset_min_s=lo, fill_offset_max_s=hi,
        )

    same = {
        "Buy-and-Hold": res(3.0, 2, (3.0,)),
        "PPO Agent (Ours)": res(3.0, 2, (3.0,)),
    }
    notes = build_auto_notes(same, warmup_seconds=15.0, window_seconds=600.0)
    assert any("Buy-and-Hold と同一" in n and "崩壊" in n for n in notes)
    assert any("許容範囲内" in n for n in notes)

    none = {
        "Buy-and-Hold": res(3.0, 2, (3.0,)),
        "PPO Agent (Ours)": EvaluationResult(0.0, 0, 0.0, 1.0, 0.0, 1, 0.0),
    }
    notes = build_auto_notes(none, warmup_seconds=15.0, window_seconds=600.0)
    assert any("全セッションで無取引" in n for n in notes)

    bad = {"PPO Agent (Ours)": res(1.0, 2, (1.0,), lo=5.0, hi=500.0)}
    notes = build_auto_notes(bad, warmup_seconds=15.0, window_seconds=600.0)
    assert any("許容範囲外" in n for n in notes)


def test_cli_selects_best_validation_model_and_writes_measured_report(tmp_path, monkeypatch):
    from trading_rl.policy import load_policy_from_checkpoint

    days = [f"2026-08-{d:02d}" for d in range(3, 13)]  # 10 sessions
    db_file = _make_db(tmp_path, days)
    report_file = tmp_path / "out" / "report.md"
    model_file = tmp_path / "out" / "model.pt"

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "train_and_eval_rl.py",
            "--stem", "7203",
            "--db-path", str(db_file),
            "--max-train-sessions", "5",
            "--max-val-sessions", "2",
            "--max-test-sessions", "3",
            "--rollout-steps", "128",
            "--batch-size", "64",
            "--iterations", "3",
            "--eval-every", "1",
            "--output-report", str(report_file),
            "--model-path", str(model_file),
        ],
    )
    assert main() == 0

    content = report_file.read_text(encoding="utf-8")
    assert "Benchmark & Evaluation Report" in content
    for needle in ("Random Agent", "Buy-and-Hold", "PPO Agent (Ours)", "Validation Results"):
        assert needle in content
    assert "tick_rule_skew_20" in content
    assert "trade_aggressor_skew" not in content
    assert "In-Depth" not in content
    assert "Selected checkpoint: **iteration" in content
    assert "--window-seconds 600" in content and "--slippage 0.5" in content

    # Checkpoint carries the env config used in training
    _policy, ckpt = load_policy_from_checkpoint(model_file)
    cfg = ckpt["env_config"]
    assert cfg["window_seconds"] == 600.0
    assert cfg["opening_warmup_seconds"] == 15.0
    assert cfg["slippage"] == 0.5
    assert len(cfg["feature_names"]) == 13
    assert 1 <= cfg["best_iteration"] <= 3

    # Training log CSV next to the report: 3 iterations, eval columns filled
    csv_file = report_file.with_name("report-training-log.csv")
    lines = csv_file.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1 + 3
    assert lines[0].startswith("iteration,total_timesteps")
