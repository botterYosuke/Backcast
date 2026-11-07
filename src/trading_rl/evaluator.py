"""Deterministic policy evaluator and performance metrics for trading episodes."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from .data import OpeningSessionData
from .env import TickTradingEnv
from .policy import ActorCritic, load_policy_from_checkpoint


@dataclass(frozen=True)
class EvaluationResult:
    """Summary metrics of trading policy evaluation."""

    total_pnl: float
    trade_count: int
    win_rate: float
    profit_factor: float
    max_drawdown: float
    episodes: int
    mean_episode_pnl: float
    closed_trade_pnls: tuple[float, ...] = field(default_factory=tuple)
    fill_offset_min_s: float | None = None  # earliest fill, seconds after T_open
    fill_offset_max_s: float | None = None  # latest fill, seconds after T_open

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_pnl": self.total_pnl,
            "trade_count": self.trade_count,
            "win_rate": self.win_rate,
            "profit_factor": self.profit_factor,
            "max_drawdown": self.max_drawdown,
            "episodes": self.episodes,
            "mean_episode_pnl": self.mean_episode_pnl,
        }


class RandomPolicy:
    """Baseline policy selecting actions uniformly at random."""

    def __init__(self, action_dim: int = 3, seed: int | None = None) -> None:
        self.action_dim = action_dim
        self.rng = np.random.default_rng(seed)

    def eval(self) -> None:
        pass

    def predict(self, obs: np.ndarray, deterministic: bool = True) -> int:
        return int(self.rng.integers(0, self.action_dim))


class BuyAndHoldPolicy:
    """Baseline policy: buys long when flat, then holds until square-off."""

    def eval(self) -> None:
        pass

    def predict(self, obs: np.ndarray, deterministic: bool = True) -> int:
        # obs[2] is current net position (-1.0, 0.0, 1.0)
        pos = obs[2]
        if pos == 0.0:
            return 1  # Action.BUY
        return 0  # Action.HOLD


def compute_max_drawdown(pnl_trajectory: list[float] | np.ndarray) -> float:
    """Compute maximum peak-to-trough drawdown from a cumulative PnL trajectory."""
    if len(pnl_trajectory) == 0:
        return 0.0
    arr = np.asarray(pnl_trajectory, dtype=np.float64)
    running_max = np.maximum.accumulate(arr)
    drawdowns = running_max - arr
    return float(np.max(drawdowns))


def evaluate_policy(
    policy: ActorCritic | RandomPolicy | BuyAndHoldPolicy | Any,
    env: TickTradingEnv,
    *,
    sessions: list[OpeningSessionData] | None = None,
    deterministic: bool = True,
    extra_features: list[np.ndarray] | None = None,
    auto_features: bool = False,
) -> EvaluationResult:
    """Evaluate a policy across sessions, driving the env as the only executor.

    Args:
        policy: Trained ActorCritic network or baseline policy.
        env: TickTradingEnv environment instance (its window/warmup/slippage apply).
        sessions: Optional list of sessions to evaluate sequentially. If None,
            evaluates over the env's own (truncated, feature-equipped) sessions.
        deterministic: Whether to take argmax action (True) or sample from policy (False).
        extra_features: Precomputed feature matrices aligned with ``sessions``.
        auto_features: Automatically extract microstructure features for ``sessions``.

    Returns:
        EvaluationResult with total PnL, win rate, profit factor, max drawdown, and
        trade count. Closed trades are the env's exit fills, so breakeven exits
        count towards the win-rate denominator.
    """
    policy.eval()

    num_sessions = env.num_sessions if sessions is None else len(sessions)

    total_pnl_all = 0.0
    all_trade_pnls: list[float] = []
    total_trades_count = 0
    pnl_trajectory: list[float] = [0.0]
    running_cumulative_pnl = 0.0
    offsets_s: list[float] = []

    for i in range(num_sessions):
        if sessions is None:
            obs, info = env.reset(options={"session_index": i})
        else:
            extra = extra_features[i] if extra_features is not None else None
            env.set_session(
                sessions[i], extra_features=extra, auto_features=auto_features
            )
            obs, info = env.reset()
        t_open_us = env.current_session.t_open_us
        done = False

        while not done:
            act = policy.predict(obs, deterministic=deterministic)
            next_obs, _reward, terminated, truncated, info = env.step(act)
            done = terminated or truncated

            for fill in info.get("fills", []):
                offsets_s.append((fill["us"] - t_open_us) / 1_000_000.0)
                if fill["kind"] == "exit":
                    all_trade_pnls.append(float(fill["pnl"]))

            curr_step_total = running_cumulative_pnl + float(info.get("total_pnl", 0.0))
            pnl_trajectory.append(curr_step_total)
            obs = next_obs

        ep_final_pnl = float(info.get("total_pnl", 0.0))
        total_pnl_all += ep_final_pnl
        running_cumulative_pnl += ep_final_pnl
        total_trades_count += int(info.get("trades_count", 0))


    # Metric computations
    mean_pnl = total_pnl_all / max(1, num_sessions)
    max_dd = compute_max_drawdown(pnl_trajectory)

    # Closed trades metrics
    if all_trade_pnls:
        wins = [p for p in all_trade_pnls if p > 0]
        losses = [p for p in all_trade_pnls if p < 0]
        win_rate = len(wins) / len(all_trade_pnls)
        gross_profit = sum(wins)
        gross_loss = abs(sum(losses))
        if gross_loss > 0:
            profit_factor = gross_profit / gross_loss
        elif gross_profit > 0:
            profit_factor = float("inf")
        else:
            profit_factor = 1.0
    else:
        win_rate = 0.0
        profit_factor = 1.0

    return EvaluationResult(
        total_pnl=total_pnl_all,
        trade_count=total_trades_count,
        win_rate=win_rate,
        profit_factor=profit_factor,
        max_drawdown=max_dd,
        episodes=num_sessions,
        mean_episode_pnl=mean_pnl,
        closed_trade_pnls=tuple(all_trade_pnls),
        fill_offset_min_s=min(offsets_s) if offsets_s else None,
        fill_offset_max_s=max(offsets_s) if offsets_s else None,
    )


def resolve_env_config(checkpoint: dict[str, Any]) -> dict[str, Any]:
    """Env config stored in a checkpoint, with legacy defaults filled in.

    Old checkpoints carry no ``env_config``: window 900s, warmup 15s, slippage 0.
    """
    cfg = dict(checkpoint.get("env_config") or {})
    cfg.setdefault("window_seconds", 900.0)
    cfg.setdefault("opening_warmup_seconds", 15.0)
    cfg.setdefault("slippage", 0.0)
    return cfg


def read_checkpoint_env_config(model_path: Path | str) -> dict[str, Any]:
    """Load a checkpoint and return its resolved env config."""
    _policy, checkpoint = load_policy_from_checkpoint(model_path)
    return resolve_env_config(checkpoint)


def simulate_ai_trades(
    opening_session: OpeningSessionData,
    model_path: Path | str,
    *,
    slippage: float | None = None,
    qty: int = 100,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Simulate deterministic policy execution on an opening session.

    The env is built from the checkpoint's ``env_config`` (window, warmup,
    slippage; legacy defaults 900 / 15 / 0) and is the single source of fills:
    this function only drives it and converts ``info["fills"]`` to the replay
    JSON shape (pnl scaled by ``qty``). ``slippage`` overrides the checkpoint's.
    """
    policy, checkpoint = load_policy_from_checkpoint(model_path)
    cfg = resolve_env_config(checkpoint)
    env = TickTradingEnv(
        opening_session,
        slippage=float(cfg["slippage"] if slippage is None else slippage),
        opening_warmup_seconds=float(cfg["opening_warmup_seconds"]),
        window_seconds=float(cfg["window_seconds"]),
        auto_features=True,
    )
    obs, _info = env.reset()

    trades: list[dict[str, Any]] = []
    done = False
    while not done:
        action = policy.predict(obs, deterministic=True)
        obs, _reward, terminated, truncated, info = env.step(action)
        done = terminated or truncated
        for fill in info["fills"]:
            trades.append(
                {
                    "us": int(fill["us"]),
                    "side": fill["side"],
                    "qty": qty,
                    "price": float(round(fill["price"], 2)),
                    "kind": fill["kind"],
                    "pnl": float(round(fill["pnl"] * qty, 2)),
                }
            )

    closed_pnls = [t["pnl"] for t in trades if t["kind"] == "exit"]
    wins = [p for p in closed_pnls if p > 0]
    win_rate = float(round(len(wins) / len(closed_pnls), 4)) if closed_pnls else 0.0
    total_pnl = float(round(sum(closed_pnls), 2))
    summary = {
        "totalPnl": total_pnl,
        "tradeCount": len(closed_pnls),
        "winRate": win_rate,
    }
    return trades, summary
