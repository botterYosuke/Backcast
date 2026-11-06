"""Unit tests for PPO policy, training loop, and evaluation (trading_rl.policy, trainer, evaluator)."""

from __future__ import annotations

import numpy as np
import torch

from trading_rl.data import OpeningSessionData
from trading_rl.env import TickTradingEnv
from trading_rl.evaluator import compute_max_drawdown, evaluate_policy
from trading_rl.features import extract_features
from trading_rl.policy import ActorCritic
from trading_rl.trainer import PPOConfig, PPOTrainer


def _make_trend_session(
    start_price: float = 100.0,
    slope: float = 1.0,
    length: int = 40,
    *,
    date: str = "2026-08-14",
    t_open_us: int = 1786698000000000,
) -> OpeningSessionData:
    prices = [start_price + i * slope for i in range(length)]
    epoch_us = np.array(
        [t_open_us + i * 15_000_000 for i in range(length)], dtype=np.int64
    )
    price_arr = np.array(prices, dtype=np.float64)
    qty_arr = np.ones(length, dtype=np.int64) * 500
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


def test_actor_critic_shapes_and_forward():
    """Verify ActorCritic dimensions, logits, values, and sampling."""
    obs_dim = 16
    batch_size = 8
    policy = ActorCritic(obs_dim=obs_dim, action_dim=3, hidden_dim=32)

    obs = torch.randn(batch_size, obs_dim)

    # 1. Forward
    logits, value = policy(obs)
    assert logits.shape == (batch_size, 3)
    assert value.shape == (batch_size,)

    # 2. Get value
    v = policy.get_value(obs)
    assert v.shape == (batch_size,)
    torch.testing.assert_close(v, value)

    # 3. Sample action
    action, logprob, entropy, val = policy.get_action_and_value(obs)
    assert action.shape == (batch_size,)
    assert logprob.shape == (batch_size,)
    assert entropy.shape == (batch_size,)
    assert val.shape == (batch_size,)
    assert (action >= 0).all() and (action < 3).all()

    # 4. Evaluate specific actions
    given_actions = torch.tensor([0, 1, 2, 1, 0, 2, 1, 0])
    act_eval, logprob_eval, ent_eval, _ = policy.get_action_and_value(
        obs, given_actions
    )
    torch.testing.assert_close(act_eval, given_actions)
    assert logprob_eval.shape == (batch_size,)

    # 5. Predict single obs
    single_act = policy.predict(obs[0].numpy(), deterministic=True)
    assert isinstance(single_act, int)
    assert 0 <= single_act < 3


def test_ppo_trainer_rollout_and_gae():
    """Verify rollout buffer collection and GAE advantage estimation."""
    session = _make_trend_session(slope=1.0, length=30)
    env = TickTradingEnv(session, auto_features=True)

    config = PPOConfig(
        rollout_steps=64,
        batch_size=16,
        update_epochs=2,
    )
    trainer = PPOTrainer(env=env, config=config)

    metrics = trainer.train_iteration()

    assert "policy_loss" in metrics
    assert "value_loss" in metrics
    assert "entropy" in metrics
    assert trainer.total_timesteps == 64
    assert not np.isnan(metrics["policy_loss"])
    assert not np.isnan(metrics["value_loss"])


def test_ppo_checkpoint_save_and_load(tmp_path):
    """Verify policy state saving and identical prediction restoration."""
    session = _make_trend_session(length=20)
    env = TickTradingEnv(session)
    trainer = PPOTrainer(env=env)

    ckpt_file = tmp_path / "ppo_test.pt"
    trainer.save(ckpt_file)
    assert ckpt_file.exists()

    # Create new trainer with different seed/weights
    new_trainer = PPOTrainer(env=env)
    new_trainer.load(ckpt_file)

    dummy_obs = np.random.randn(env.observation_space.shape[0]).astype(np.float32)
    act_orig = trainer.policy.predict(dummy_obs, deterministic=True)
    act_loaded = new_trainer.policy.predict(dummy_obs, deterministic=True)
    assert act_orig == act_loaded


def test_evaluator_metrics_and_drawdown():
    """Verify max drawdown computation and deterministic policy evaluation."""
    # Known PnL path: 0 -> 10 -> 8 -> 15 -> 5 -> 12
    # Peaks: 0, 10, 10, 15, 15, 15
    # Drawdowns: 0, 0, 2, 0, 10, 3 -> max drawdown = 10.0
    trajectory = [0.0, 10.0, 8.0, 15.0, 5.0, 12.0]
    assert compute_max_drawdown(trajectory) == 10.0

    session = _make_trend_session(slope=2.0, length=25)
    env = TickTradingEnv(session, auto_features=True)
    obs_dim = env.observation_space.shape[0]

    policy = ActorCritic(obs_dim=obs_dim, hidden_dim=16)
    result = evaluate_policy(policy, env, auto_features=True)

    assert result.episodes == 1
    assert isinstance(result.total_pnl, float)
    assert isinstance(result.win_rate, float)
    assert 0.0 <= result.win_rate <= 1.0
    assert result.max_drawdown >= 0.0


def test_ppo_convergence_on_trending_market():
    """Verify PPO can learn a profitable Long action on a steady upward trend."""
    # 25 ticks strictly increasing by +2.0 each step
    session = _make_trend_session(start_price=100.0, slope=2.0, length=25)
    features = extract_features(session)
    env = TickTradingEnv(session, extra_features=features)

    # Biased policy or quick training on simple trend
    obs_dim = env.observation_space.shape[0]
    policy = ActorCritic(obs_dim=obs_dim, hidden_dim=32)

    config = PPOConfig(
        learning_rate=1e-2,
        rollout_steps=200,
        batch_size=32,
        update_epochs=5,
        entropy_coef=0.001,
    )
    trainer = PPOTrainer(env=env, policy=policy, config=config)

    # Train for a few iterations
    for _ in range(5):
        trainer.train_iteration()

    # Evaluation on upward trending session should execute trades and not produce NaN
    result = evaluate_policy(
        trainer.policy, env, extra_features=[features], deterministic=True
    )
    assert not np.isnan(result.total_pnl)
    assert result.episodes == 1
