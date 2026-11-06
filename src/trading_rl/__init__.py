"""RL Trading Agent for Opening 15-Minute Window.

This package provides high-performance data extraction from DuckDB tick files
and a Gymnasium trading environment specialized for the post-open 15-minute window.
"""

from .data import (
    NoMorningSessionError,
    OpeningSessionData,
    get_session_dates,
    load_session_ticks,
)
from .env import Action, TickTradingEnv
from .evaluator import BuyAndHoldPolicy, EvaluationResult, RandomPolicy, evaluate_policy
from .policy import ActorCritic
from .trainer import PPOConfig, PPOTrainer

__all__ = [
    "Action",
    "ActorCritic",
    "BuyAndHoldPolicy",
    "EvaluationResult",
    "NoMorningSessionError",
    "OpeningSessionData",
    "PPOConfig",
    "PPOTrainer",
    "RandomPolicy",
    "TickTradingEnv",
    "evaluate_policy",
    "get_session_dates",
    "load_session_ticks",
]
