#!/usr/bin/env python3
"""Benchmark and Evaluation CLI for the opening-window RL Trading Agent.

Trains a PPO agent on chronological morning sessions and benchmarks
it against Random and Buy-and-Hold baselines on an out-of-sample test set.
"""

from __future__ import annotations

import argparse
import csv
import math
import sys
import time
from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

# Ensure standard streams handle UTF-8 cleanly on Windows
if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

# Ensure project src is in python path
SRC_DIR = Path(__file__).resolve().parent.parent / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

import numpy as np  # noqa: E402
import torch  # noqa: E402

from trading_rl.data import (  # noqa: E402
    OPENING_WINDOW_US,
    OpeningSessionData,
    _open_duckdb,
    get_session_dates,
    load_session_ticks,
)
from trading_rl.env import TickTradingEnv  # noqa: E402
from trading_rl.evaluator import (  # noqa: E402
    BuyAndHoldPolicy,
    EvaluationResult,
    RandomPolicy,
    evaluate_policy,
)
from trading_rl.features import get_feature_names  # noqa: E402
from trading_rl.policy import ActorCritic  # noqa: E402
from trading_rl.trainer import PPOConfig, PPOTrainer, fit_obs_normalizer  # noqa: E402


def split_sessions_chronologically(
    dates: list[str],
    train_ratio: float = 0.70,
    val_ratio: float = 0.15,
    max_train_sessions: int | None = None,
    max_test_sessions: int | None = None,
    max_val_sessions: int | None = None,
) -> tuple[list[str], list[str], list[str]]:
    """Split session dates chronologically into train, validation, and test sets.

    Guarantees: all(train_dates) < all(val_dates) < all(test_dates).
    """
    sorted_dates = sorted(dates)
    n_total = len(sorted_dates)

    if n_total < 2:
        raise ValueError(f"Need at least 2 sessions to split, got {n_total}")

    if max_train_sessions is not None or max_test_sessions is not None:
        # Determine target counts
        req_test = (
            max_test_sessions
            if max_test_sessions is not None
            else max(1, int(n_total * (1.0 - train_ratio - val_ratio)))
        )
        req_train = (
            max_train_sessions
            if max_train_sessions is not None
            else max(1, int(n_total * train_ratio))
        )
        req_val = (
            max_val_sessions
            if max_val_sessions is not None
            else max(0, int(n_total * val_ratio))
        )

        total_req = req_train + req_val + req_test
        if total_req > n_total:
            # Scale proportionally if requested exceeds available
            req_test = max(1, min(req_test, n_total // 3))
            remaining = n_total - req_test
            req_val = min(req_val, max(0, remaining // 4))
            req_train = remaining - req_val

        # Select the latest `total_req` dates to use most recent market data
        selected = sorted_dates[-total_req:]
        train_dates = selected[:req_train]
        if req_val > 0:
            val_dates = selected[req_train : req_train + req_val]
            test_dates = selected[req_train + req_val :]
        else:
            val_dates = []
            test_dates = selected[req_train:]
    else:
        n_train = max(1, int(n_total * train_ratio))
        n_val = max(0, int(n_total * val_ratio))
        n_test = n_total - n_train - n_val
        if n_test <= 0:
            n_test = 1
            if n_train > 1:
                n_train -= 1
            elif n_val > 0:
                n_val -= 1

        train_dates = sorted_dates[:n_train]
        val_dates = sorted_dates[n_train : n_train + n_val]
        test_dates = sorted_dates[n_train + n_val :]

    return train_dates, val_dates, test_dates


def resolve_candidate_dates(
    db_path: Path,
    stem: str,
    start_date: str | None = None,
    end_date: str | None = None,
    needed_count: int | None = None,
) -> list[str]:
    """Retrieve session dates with intelligent recent-window search for large databases."""
    with _open_duckdb(db_path, stem) as con:
        # If no explicit start date is passed, check metadata table to avoid full scan
        if start_date is None and needed_count is not None:
            try:
                row = con.execute(
                    "SELECT to_timestamp FROM stocks_board_metadata ORDER BY record_count DESC LIMIT 1"
                ).fetchone()
                if row and row[0]:
                    to_dt = (
                        datetime.fromisoformat(str(row[0]))
                        if isinstance(row[0], str)
                        else row[0]
                    )
                    # Query roughly ~2.2x trading days in calendar days (~65-70 trading days)
                    lookback_days = max(60, int(needed_count * 2.2))
                    window_start = (to_dt - timedelta(days=lookback_days)).strftime(
                        "%Y-%m-%d"
                    )
                    dates = get_session_dates(
                        con,
                        stem=stem,
                        start_date=window_start,
                        end_date=end_date,
                    )
                    if len(dates) >= needed_count:
                        return dates
            except Exception:
                pass

        return get_session_dates(
            con, stem=stem, start_date=start_date, end_date=end_date
        )


def load_sessions_batch(
    db_path: Path,
    stem: str,
    dates: list[str],
    label: str = "sessions",
    *,
    window_us: int = OPENING_WINDOW_US,
    warmup_us: int | None = None,
) -> list[OpeningSessionData]:
    """Load tick sessions from DuckDB for a list of dates.

    Args:
        window_us: Window loaded after T_open (ticks with epoch <= T_open + window).
        warmup_us: If given, sessions without any tick in [T_open + warmup, T_open +
            window] are dropped (nothing can be traded on them).
    """
    sessions: list[OpeningSessionData] = []
    dropped = 0
    print(f"Loading {len(dates)} {label} from {db_path.name}...", flush=True)
    with _open_duckdb(db_path, stem) as con:
        for i, dt in enumerate(dates):
            try:
                sess = load_session_ticks(con, dt, stem=stem, window_us=window_us)
                if warmup_us is not None and not bool(
                    np.any(sess.epoch_us >= sess.t_open_us + warmup_us)
                ):
                    dropped += 1
                    print(
                        f"  [Warning] Dropping {dt}: no tick after warmup in window",
                        flush=True,
                    )
                else:
                    sessions.append(sess)
            except Exception as e:
                print(f"  [Warning] Skipping date {dt}: {e}", flush=True)
            if (i + 1) % 10 == 0 or (i + 1) == len(dates):
                print(f"  Loaded {i + 1}/{len(dates)} {label}...", flush=True)
    if dropped:
        print(f"  Dropped {dropped} {label} without tradable ticks.", flush=True)
    return sessions


def run_benchmarks(
    policy: Any,
    test_env: TickTradingEnv,
    test_sessions: list[OpeningSessionData] | None = None,
    seed: int = 42,
) -> dict[str, EvaluationResult]:
    """Run evaluation for Random, Buy-and-Hold, and Trained PPO policies.

    All three run on ``test_env``, so they share its window / warmup / slippage.
    With ``test_sessions=None`` the env's own sessions (and precomputed features)
    are used.
    """
    results: dict[str, EvaluationResult] = {}
    kwargs: dict[str, Any] = (
        {} if test_sessions is None else {"sessions": test_sessions, "auto_features": True}
    )

    # 1. Random Agent
    print("Evaluating Random Agent Baseline...", flush=True)
    results["Random Agent"] = evaluate_policy(
        RandomPolicy(seed=seed), test_env, **kwargs
    )

    # 2. Buy-and-Hold Benchmark
    print("Evaluating Buy-and-Hold Benchmark...", flush=True)
    results["Buy-and-Hold"] = evaluate_policy(BuyAndHoldPolicy(), test_env, **kwargs)

    # 3. Trained PPO Agent (Deterministic Policy)
    print("Evaluating Trained PPO Agent (Deterministic)...", flush=True)
    results["PPO Agent (Ours)"] = evaluate_policy(
        policy, test_env, deterministic=True, **kwargs
    )

    return results


def format_metrics_table(results: dict[str, EvaluationResult]) -> str:
    """Format benchmark results into a clean markdown table."""
    header = (
        "| Strategy | Total PnL (JPY) | Mean PnL / Sess (JPY) | Win Rate (%) | "
        "Profit Factor | Max Drawdown (JPY) | Trades (Exec/Closed) |"
    )
    separator = (
        "|:---|:---:|:---:|:---:|:---:|:---:|:---:|"
    )
    rows = []
    for name, res in results.items():
        pf_str = f"{res.profit_factor:.2f}" if np.isfinite(res.profit_factor) else "Inf"
        wr_str = f"{res.win_rate * 100:.1f}%"
        closed_trades = len(res.closed_trade_pnls)
        trades_str = f"{res.trade_count} / {closed_trades}"
        row = (
            f"| **{name}** | {res.total_pnl:+.2f} | {res.mean_episode_pnl:+.2f} | "
            f"{wr_str} | {pf_str} | {res.max_drawdown:.2f} | {trades_str} |"
        )
        rows.append(row)

    return "\n".join([header, separator] + rows)


def is_better_validation(
    pnl: float,
    trades: int,
    best_pnl: float | None,
    best_trades: int | None,
) -> bool:
    """Model selection rule: higher validation PnL wins, ties go to fewer trades."""
    if best_pnl is None or best_trades is None:
        return True
    if pnl > best_pnl:
        return True
    return pnl == best_pnl and trades < best_trades


def build_auto_notes(
    results: dict[str, EvaluationResult],
    *,
    warmup_seconds: float,
    window_seconds: float,
) -> list[str]:
    """Short factual notes derived only from measured results."""
    notes: list[str] = []
    ppo = results.get("PPO Agent (Ours)")
    bh = results.get("Buy-and-Hold")
    if ppo is None:
        return notes

    if ppo.trade_count == 0:
        notes.append("PPO が全セッションで無取引 (取引回数 0)。")
    elif (
        bh is not None
        and ppo.total_pnl == bh.total_pnl
        and ppo.trade_count == bh.trade_count
        and ppo.closed_trade_pnls == bh.closed_trade_pnls
    ):
        notes.append("PPO の結果が Buy-and-Hold と同一 → 方策が常時買い持ちに崩壊。")
    if bh is not None and ppo.trade_count > 0:
        rel = "上回る" if ppo.total_pnl > bh.total_pnl else (
            "同じ" if ppo.total_pnl == bh.total_pnl else "下回る"
        )
        notes.append(
            f"PPO の合計 PnL ({ppo.total_pnl:+.2f}) は Buy-and-Hold "
            f"({bh.total_pnl:+.2f}) を{rel}。"
        )
    if ppo.fill_offset_min_s is not None and ppo.fill_offset_max_s is not None:
        ok = (
            ppo.fill_offset_min_s >= warmup_seconds - 1e-9
            and ppo.fill_offset_max_s <= window_seconds + 1e-9
        )
        verdict = "許容範囲内" if ok else "許容範囲外 (要調査)"
        notes.append(
            f"PPO の約定時刻は T_open+{ppo.fill_offset_min_s:.3f}s から "
            f"T_open+{ppo.fill_offset_max_s:.3f}s (許容 [{warmup_seconds:g}s, "
            f"{window_seconds:g}s]) で{verdict}。"
        )
    return notes


def format_eval_history(history: list[dict[str, float]]) -> str:
    """Markdown table of validation evaluations performed during training."""
    header = (
        "| Iter | Timesteps | Val PnL (JPY) | Val Trades | Mean Ep Return | Entropy | KL |"
    )
    sep = "|:---:|:---:|:---:|:---:|:---:|:---:|:---:|"
    rows = [
        f"| {int(h['iteration'])} | {int(h['total_timesteps'])} | "
        f"{h['val_pnl']:+.2f} | {int(h['val_trades'])} | "
        f"{h['mean_ep_return']:+.4f} | {h['entropy']:.4f} | {h['approx_kl']:.5f} |"
        for h in history
    ]
    return "\n".join([header, sep] + rows)


def generate_markdown_report(
    *,
    stem: str,
    db_path: Path,
    train_dates: list[str],
    val_dates: list[str],
    test_dates: list[str],
    val_results: dict[str, EvaluationResult] | None,
    test_results: dict[str, EvaluationResult],
    iterations: int,
    total_timesteps: int,
    ppo_config: PPOConfig,
    window_seconds: float,
    warmup_seconds: float,
    slippage: float,
    feature_names: list[str],
    obs_dim: int,
    model_save_path: Path,
    best_iteration: int,
    best_selection: str,
    eval_history: list[dict[str, float]],
    steps_per_sec: float,
    seed: int,
    reproduce_cmd: str,
) -> str:
    """Generate the research report from measured values only."""
    timestamp_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    hp_lines = "\n".join(
        f"- `{k}`: `{v}`" for k, v in asdict(ppo_config).items()
    )
    feat_lines = ", ".join(f"`{n}`" for n in feature_names)

    def _range(dates: list[str]) -> str:
        return f"{len(dates)} sessions (`{dates[0]}` to `{dates[-1]}`)" if dates else "0 sessions"

    sections = [
        f"# Benchmark & Evaluation Report: Opening {window_seconds:g}s RL Trading Agent",
        "",
        f"**Symbol Stem:** `{stem}`  ",
        f"**Database:** `{db_path}`  ",
        f"**Generated At:** {timestamp_str}  ",
        f"**Model Checkpoint:** `{model_save_path}`",
        "",
        "## 1. Setup",
        "",
        f"- Trading window: T_open to T_open + {window_seconds:g}s",
        f"- Warmup (no orders): first {warmup_seconds:g}s after T_open",
        f"- Slippage: {slippage:g} JPY per fill; PnL is per 1 share",
        f"- Observation dim: {obs_dim} (5 base + {len(feature_names)} features)",
        f"- Features: {feat_lines}",
        f"- Seed: {seed}",
        f"- Train: {_range(train_dates)}",
        f"- Validation: {_range(val_dates)}",
        f"- Test: {_range(test_dates)}",
        "",
        "## 2. PPO Training",
        "",
        f"- Iterations: {iterations} ({total_timesteps} timesteps)",
        f"- Training throughput: {steps_per_sec:.0f} env steps/sec (rollout + update)",
        "",
        "Hyperparameters:",
        "",
        hp_lines,
        "",
        f"Selected checkpoint: **iteration {best_iteration}** ({best_selection}).",
        "",
    ]
    if eval_history:
        sections += ["Validation during training:", "", format_eval_history(eval_history), ""]

    if val_results is not None:
        sections += [
            "## 3. Validation Results (best model)",
            "",
            format_metrics_table(val_results),
            "",
        ]
        val_notes = build_auto_notes(
            val_results, warmup_seconds=warmup_seconds, window_seconds=window_seconds
        )
        sections += [f"- {n}" for n in val_notes] + [""] if val_notes else []

    sections += [
        "## 4. Out-of-Sample Test Results (best model)",
        "",
        format_metrics_table(test_results),
        "",
        "Trades (Exec/Closed): fill events / closed round trips.",
        "",
    ]
    test_notes = build_auto_notes(
        test_results, warmup_seconds=warmup_seconds, window_seconds=window_seconds
    )
    if test_notes:
        sections += ["Notes:", ""] + [f"- {n}" for n in test_notes] + [""]

    sections += [
        "## 5. Reproduce",
        "",
        "```bash",
        reproduce_cmd,
        "```",
        "",
    ]
    return "\n".join(sections)


def write_training_log_csv(path: Path, logs: list[dict[str, float]]) -> None:
    """Write per-iteration training metrics as CSV."""
    columns = [
        "iteration",
        "total_timesteps",
        "mean_ep_return",
        "episodes_completed",
        "running_ep_return",
        "policy_loss",
        "value_loss",
        "entropy",
        "approx_kl",
        "learning_rate",
        "val_pnl",
        "val_trades",
    ]
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        int_cols = {"iteration", "total_timesteps", "episodes_completed", "val_trades"}
        for row in logs:
            writer.writerow(
                {
                    c: (int(row[c]) if c in int_cols and c in row else row.get(c, ""))
                    for c in columns
                }
            )


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Train and evaluate the opening-window RL Trading Agent."
    )
    parser.add_argument("--stem", type=str, default="7203", help="Stock symbol stem (default: 7203).")
    parser.add_argument("--db-path", type=str, default=None, help="Path to DuckDB database file.")
    parser.add_argument("--max-train-sessions", type=int, default=100)
    parser.add_argument("--max-val-sessions", type=int, default=15)
    parser.add_argument("--max-test-sessions", type=int, default=20)
    parser.add_argument("--train-ratio", type=float, default=0.70)
    parser.add_argument("--val-ratio", type=float, default=0.15)
    parser.add_argument(
        "--window-seconds", type=float, default=600.0,
        help="Trading window after T_open in seconds (default: 600).",
    )
    parser.add_argument(
        "--warmup-seconds", type=float, default=15.0,
        help="No orders during the first N seconds after T_open (default: 15).",
    )
    parser.add_argument(
        "--slippage", type=float, default=0.5,
        help="Per-fill slippage in yen (default: 0.5).",
    )
    parser.add_argument(
        "--total-timesteps", type=int, default=1_000_000,
        help="Total environment steps; iterations = ceil(total / rollout-steps).",
    )
    parser.add_argument(
        "--iterations", type=int, default=None,
        help="Override: number of PPO iterations (ignores --total-timesteps).",
    )
    parser.add_argument("--rollout-steps", type=int, default=4096)
    parser.add_argument("--batch-size", type=int, default=256)
    parser.add_argument(
        "--eval-every", type=int, default=10,
        help="Iterations between validation evaluations (default: 10).",
    )
    parser.add_argument("--learning-rate", type=float, default=3e-4)
    parser.add_argument("--anneal-lr", action="store_true", help="Linear LR decay to 0.")
    parser.add_argument("--start-date", type=str, default=None)
    parser.add_argument("--end-date", type=str, default=None)
    parser.add_argument(
        "--output-report", type=str, default=None,
        help="Output markdown report (default: .agents/docs/research/rl-{stem}-opening10m-results.md).",
    )
    parser.add_argument(
        "--model-path", type=str, default=None,
        help="Checkpoint destination (default: models/ppo_{stem}_opening10m.pt).",
    )
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--device", type=str, default="cpu")
    return parser


def build_reproduce_command(args: argparse.Namespace) -> str:
    parts = [
        "uv run python scripts/train_and_eval_rl.py",
        f"--stem {args.stem}",
        f"--max-train-sessions {args.max_train_sessions}",
        f"--max-val-sessions {args.max_val_sessions}",
        f"--max-test-sessions {args.max_test_sessions}",
        f"--window-seconds {args.window_seconds:g}",
        f"--warmup-seconds {args.warmup_seconds:g}",
        f"--slippage {args.slippage:g}",
        f"--rollout-steps {args.rollout_steps}",
        f"--batch-size {args.batch_size}",
        f"--eval-every {args.eval_every}",
        f"--learning-rate {args.learning_rate:g}",
    ]
    if args.anneal_lr:
        parts.append("--anneal-lr")
    if args.iterations is not None:
        parts.append(f"--iterations {args.iterations}")
    else:
        parts.append(f"--total-timesteps {args.total_timesteps}")
    if args.start_date:
        parts.append(f"--start-date {args.start_date}")
    if args.end_date:
        parts.append(f"--end-date {args.end_date}")
    parts.append(f"--seed {args.seed}")
    return " ".join(parts)


def main() -> int:
    parser = build_arg_parser()
    args = parser.parse_args()

    stem_lower = args.stem.lower()
    model_path_str = args.model_path or f"models/ppo_{stem_lower}_opening10m.pt"
    report_path_str = (
        args.output_report
        or f".agents/docs/research/rl-{stem_lower}-opening10m-results.md"
    )

    # Set seeds
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)

    window_us = int(round(args.window_seconds * 1_000_000))
    warmup_us = int(round(args.warmup_seconds * 1_000_000))

    # 1. Resolve DuckDB Path
    if args.db_path:
        db_path = Path(args.db_path)
    else:
        # Default priority: S:/jp/stocks_trades/<stem>.duckdb -> data/<stem>.duckdb
        candidates = [
            Path(f"S:/jp/stocks_trades/{args.stem}.duckdb"),
            Path(f"data/{args.stem}.duckdb"),
            Path(f"{args.stem}.duckdb"),
        ]
        db_path = next((c for c in candidates if c.is_file()), candidates[0])

    if not db_path.is_file():
        print(f"Error: Database file not found at {db_path}", file=sys.stderr)
        return 1

    print(f"=== Opening {args.window_seconds:g}s RL Trading Agent Benchmark ===")
    print(f"Symbol Stem: {args.stem}")
    print(f"Database: {db_path}")

    # 2. Retrieve session dates
    needed_sessions = (
        (args.max_train_sessions or 30)
        + (args.max_test_sessions or 15)
        + (args.max_val_sessions or 0)
    )
    dates = resolve_candidate_dates(
        db_path=db_path,
        stem=args.stem,
        start_date=args.start_date,
        end_date=args.end_date,
        needed_count=needed_sessions,
    )
    print(f"Discovered {len(dates)} candidate trading sessions.")
    if len(dates) < 2:
        print(f"Error: Need at least 2 sessions, found {len(dates)}", file=sys.stderr)
        return 1

    # 3. Split sessions chronologically
    train_dates, val_dates, test_dates = split_sessions_chronologically(
        dates=dates,
        train_ratio=args.train_ratio,
        val_ratio=args.val_ratio,
        max_train_sessions=args.max_train_sessions,
        max_test_sessions=args.max_test_sessions,
        max_val_sessions=args.max_val_sessions,
    )
    print("Chronological Split:")
    print(f"  Train: {len(train_dates)} sessions ({train_dates[0]} -> {train_dates[-1]})")
    if val_dates:
        print(f"  Val:   {len(val_dates)} sessions ({val_dates[0]} -> {val_dates[-1]})")
    print(f"  Test:  {len(test_dates)} sessions ({test_dates[0]} -> {test_dates[-1]})")

    # 4. Load tick sessions (dropping sessions with nothing tradable)
    load_kwargs: dict[str, Any] = {"window_us": window_us, "warmup_us": warmup_us}
    train_sessions = load_sessions_batch(
        db_path, args.stem, train_dates, label="train sessions", **load_kwargs
    )
    val_sessions = (
        load_sessions_batch(db_path, args.stem, val_dates, label="val sessions", **load_kwargs)
        if val_dates
        else []
    )
    test_sessions = load_sessions_batch(
        db_path, args.stem, test_dates, label="test sessions", **load_kwargs
    )

    if not train_sessions or not test_sessions:
        print("Error: Failed to load required sessions.", file=sys.stderr)
        return 1
    # Report the dates actually used (after dropping unloadable/untradable sessions)
    train_dates = [s.date for s in train_sessions]
    val_dates = [s.date for s in val_sessions]
    test_dates = [s.date for s in test_sessions]

    # 5. Initialize Environments (all share window / warmup / slippage)
    print("\nInitializing Gym environments with auto_features=True...")
    env_kwargs: dict[str, Any] = {
        "slippage": args.slippage,
        "opening_warmup_seconds": args.warmup_seconds,
        "window_seconds": args.window_seconds,
        "auto_features": True,
    }
    train_env = TickTradingEnv(train_sessions, **env_kwargs)
    val_env = TickTradingEnv(val_sessions, **env_kwargs) if val_sessions else None
    test_env = TickTradingEnv(test_sessions, **env_kwargs)

    obs_dim = int(train_env.observation_space.shape[0])
    feature_names = get_feature_names(train_env.feature_config)

    # 6. Policy with observation normalizer fitted on the training env
    policy = ActorCritic(obs_dim=obs_dim, action_dim=int(train_env.action_space.n))
    obs_mean, obs_std = fit_obs_normalizer(train_env)
    policy.set_obs_normalizer(obs_mean, obs_std)

    ppo_cfg = PPOConfig(
        learning_rate=args.learning_rate,
        rollout_steps=args.rollout_steps,
        batch_size=args.batch_size,
        update_epochs=4,
        anneal_lr=args.anneal_lr,
    )
    trainer = PPOTrainer(
        env=train_env,
        policy=policy,
        config=ppo_cfg,
        device=args.device,
        seed=args.seed,
    )

    if args.iterations is not None:
        iterations = max(1, int(args.iterations))
    else:
        iterations = max(1, math.ceil(args.total_timesteps / args.rollout_steps))
    total_timesteps = iterations * args.rollout_steps
    eval_every = max(1, int(args.eval_every))

    env_config: dict[str, Any] = {
        "window_seconds": float(args.window_seconds),
        "opening_warmup_seconds": float(args.warmup_seconds),
        "slippage": float(args.slippage),
        "feature_names": feature_names,
        "obs_dim": obs_dim,
        "stem": args.stem,
        "seed": args.seed,
    }

    # 7. Training loop with periodic validation-based model selection
    print(f"\nTraining PPO Agent for {iterations} iterations ({total_timesteps} steps)...")
    training_logs: list[dict[str, float]] = []
    eval_history: list[dict[str, float]] = []
    best_state: dict[str, torch.Tensor] | None = None
    best_iter = 0
    best_pnl: float | None = None
    best_trades: int | None = None
    train_seconds = 0.0

    for it in range(1, iterations + 1):
        t0 = time.perf_counter()
        metrics = trainer.train_iteration(progress=(it - 1) / iterations)
        train_seconds += time.perf_counter() - t0
        metrics["iteration"] = float(it)
        training_logs.append(metrics)

        if it % eval_every == 0 or it == iterations:
            line = (
                f"  Iter {it:03d}/{iterations:03d} | steps {int(metrics['total_timesteps'])} | "
            )
            if val_env is not None:
                val_res = evaluate_policy(trainer.policy, val_env, deterministic=True)
                metrics["val_pnl"] = val_res.total_pnl
                metrics["val_trades"] = float(val_res.trade_count)
                eval_history.append(metrics)
                line += f"val pnl {val_res.total_pnl:+.2f} | val trades {val_res.trade_count} | "
                if is_better_validation(
                    val_res.total_pnl, val_res.trade_count, best_pnl, best_trades
                ):
                    best_pnl = val_res.total_pnl
                    best_trades = val_res.trade_count
                    best_iter = it
                    best_state = {
                        k: v.detach().clone()
                        for k, v in trainer.policy.state_dict().items()
                    }
            line += (
                f"mean_ep_return {metrics['mean_ep_return']:+.4f} | "
                f"entropy {metrics['entropy']:.4f} | kl {metrics['approx_kl']:.5f}"
            )
            print(line, flush=True)

    steps_per_sec = total_timesteps / max(train_seconds, 1e-9)
    print(f"\nTraining throughput: {steps_per_sec:.0f} env steps/sec")

    # 8. Select best model (validation) and save it
    if best_state is not None:
        trainer.policy.load_state_dict(best_state)
        best_selection = (
            f"best validation total PnL {best_pnl:+.2f} JPY with {best_trades} trades; "
            "ties broken by fewer trades"
        )
    else:
        best_iter = iterations
        best_selection = "no validation sessions; final iteration used"
    print(f"Selected iteration {best_iter}: {best_selection}")

    env_config["best_iteration"] = best_iter
    model_save_path = Path(model_path_str)
    model_save_path.parent.mkdir(parents=True, exist_ok=True)
    trainer.save(model_save_path, metadata=env_config)
    print(f"Saved selected model to: {model_save_path}")

    # 9. Benchmarks (validation + out-of-sample test) with the selected model
    val_results = None
    if val_env is not None:
        print(f"\nRunning validation benchmark on {len(val_sessions)} sessions...")
        val_results = run_benchmarks(
            policy=trainer.policy, test_env=val_env, seed=args.seed
        )
    print(f"\nRunning Out-of-Sample Benchmark on {len(test_sessions)} test sessions...")
    test_results = run_benchmarks(policy=trainer.policy, test_env=test_env, seed=args.seed)

    if val_results is not None:
        print("\n=== Validation Results ===")
        print(format_metrics_table(val_results))
    print("\n=== Out-of-Sample Benchmark Results ===")
    print(format_metrics_table(test_results))
    print()

    # 10. Report + training log CSV
    report_path = Path(report_path_str)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_content = generate_markdown_report(
        stem=args.stem,
        db_path=db_path,
        train_dates=train_dates,
        val_dates=val_dates,
        test_dates=test_dates,
        val_results=val_results,
        test_results=test_results,
        iterations=iterations,
        total_timesteps=total_timesteps,
        ppo_config=ppo_cfg,
        window_seconds=args.window_seconds,
        warmup_seconds=args.warmup_seconds,
        slippage=args.slippage,
        feature_names=feature_names,
        obs_dim=obs_dim,
        model_save_path=model_save_path,
        best_iteration=best_iter,
        best_selection=best_selection,
        eval_history=eval_history,
        steps_per_sec=steps_per_sec,
        seed=args.seed,
        reproduce_cmd=build_reproduce_command(args),
    )
    report_path.write_text(report_content, encoding="utf-8")
    csv_path = report_path.with_name(report_path.stem + "-training-log.csv")
    write_training_log_csv(csv_path, training_logs)
    print(f"Generated research report: {report_path}")
    print(f"Saved training log: {csv_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
