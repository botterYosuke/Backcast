"""Deterministic policy evaluator and performance metrics for trading episodes."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np

from .data import OpeningSessionData
from .env import TickTradingEnv
from .policy import ActorCritic


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
    """Evaluate a trained policy deterministically across sessions.

    Args:
        policy: Trained ActorCritic network.
        env: TickTradingEnv environment instance.
        sessions: Optional list of sessions to evaluate sequentially. If None,
            evaluates over the env's configured session(s).
        deterministic: Whether to take argmax action (True) or sample from policy (False).
        extra_features: Precomputed feature matrices aligned with sessions.
        auto_features: Automatically extract microstructure features for sessions.

    Returns:
        EvaluationResult with total PnL, win rate, profit factor, max drawdown, and trade count.
    """
    policy.eval()

    eval_sessions = sessions if sessions is not None else env._sessions

    total_pnl_all = 0.0
    all_trade_pnls: list[float] = []
    total_trades_count = 0
    pnl_trajectory: list[float] = [0.0]
    running_cumulative_pnl = 0.0

    for i, sess in enumerate(eval_sessions):
        extra = extra_features[i] if extra_features is not None else None
        env.set_session(sess, extra_features=extra, auto_features=auto_features)

        obs, info = env.reset()
        done = False
        prev_realized = 0.0

        while not done:
            act = policy.predict(obs, deterministic=deterministic)
            next_obs, _reward, terminated, truncated, info = env.step(act)
            done = terminated or truncated

            curr_realized = float(info.get("realized_pnl", 0.0))
            if curr_realized != prev_realized:
                trade_diff = curr_realized - prev_realized
                all_trade_pnls.append(trade_diff)
                prev_realized = curr_realized

            curr_step_total = running_cumulative_pnl + float(info.get("total_pnl", 0.0))
            pnl_trajectory.append(curr_step_total)
            obs = next_obs

        ep_final_pnl = float(info.get("total_pnl", 0.0))
        total_pnl_all += ep_final_pnl
        running_cumulative_pnl += ep_final_pnl
        total_trades_count += int(info.get("trades_count", 0))

    # Metric computations
    num_episodes = len(eval_sessions)
    mean_pnl = total_pnl_all / max(1, num_episodes)
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
        episodes=num_episodes,
        mean_episode_pnl=mean_pnl,
        closed_trade_pnls=tuple(all_trade_pnls),
    )
