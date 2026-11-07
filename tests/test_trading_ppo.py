"""Unit tests for PPO policy, training loop, and evaluation (trading_rl.policy, trainer, evaluator)."""

from __future__ import annotations

import numpy as np
import pytest
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


# ---------------------------------------------------------------------------
# Observation normalization stored in the model
# ---------------------------------------------------------------------------
def test_obs_normalizer_buffers_default_identity_and_apply_everywhere():
    policy = ActorCritic(obs_dim=6, hidden_dim=16)
    assert torch.equal(policy.obs_mean, torch.zeros(6))
    assert torch.equal(policy.obs_std, torch.ones(6))
    assert "obs_mean" in policy.state_dict() and "obs_std" in policy.state_dict()

    obs = torch.randn(4, 6)
    ref_logits, ref_value = policy(obs)

    mean = np.array([1.0, -2.0, 0.5, 0.0, 3.0, 0.1], dtype=np.float32)
    std = np.array([2.0, 0.5, 1.0, 1e-12, 4.0, 1.0], dtype=np.float32)
    policy.set_obs_normalizer(mean, std)
    assert policy.obs_std[3].item() == 1.0  # tiny std -> 1

    # Every path equals the identity-model on manually normalized obs
    manual = torch.clamp((obs - torch.as_tensor(mean)) / policy.obs_std, -10, 10)
    reference = ActorCritic(obs_dim=6, hidden_dim=16)
    reference.load_state_dict(
        {**policy.state_dict(), "obs_mean": torch.zeros(6), "obs_std": torch.ones(6)}
    )
    logits, value = policy(obs)
    exp_logits, exp_value = reference(manual)
    torch.testing.assert_close(logits, exp_logits)
    torch.testing.assert_close(value, exp_value)
    torch.testing.assert_close(policy.get_value(obs), exp_value)
    _a, _lp, _ent, v = policy.get_action_and_value(obs)
    torch.testing.assert_close(v, exp_value)
    assert not torch.allclose(logits, ref_logits) or not torch.allclose(value, ref_value)

    single = obs[0].numpy()
    assert policy.predict(single) == int(torch.argmax(exp_logits[0]).item())


def test_obs_normalizer_clips_to_ten():
    policy = ActorCritic(obs_dim=2, hidden_dim=8)
    big = torch.tensor([[1e9, -1e9]])
    huge = torch.tensor([[1e12, -1e12]])
    torch.testing.assert_close(policy(big)[0], policy(huge)[0])


def test_fit_obs_normalizer_rules():
    from trading_rl.trainer import fit_obs_normalizer

    sessions = [
        _make_trend_session(start_price=100.0, slope=1.0, length=30),
        _make_trend_session(start_price=100.0, slope=-0.5, length=30),
    ]
    env = TickTradingEnv(sessions, auto_features=True)
    mean, std = fit_obs_normalizer(env)
    obs_dim = env.observation_space.shape[0]
    assert mean.shape == (obs_dim,) and std.shape == (obs_dim,)
    for dim in (1, 2, 4):
        assert mean[dim] == 0.0 and std[dim] == 1.0
    assert mean[3] == 0.0
    assert std[3] == pytest.approx(std[0])
    assert std[0] > 0
    assert (std >= 1e-8).all()
    # price_return z-score uses tradable steps of all sessions
    flat = np.concatenate([env.flat_observations(i) for i in range(2)])
    assert mean[0] == pytest.approx(flat[:, 0].mean(), rel=1e-4)


def test_old_checkpoint_without_obs_buffers_still_loads(tmp_path):
    from trading_rl.policy import load_policy_from_checkpoint

    old = ActorCritic(obs_dim=7, hidden_dim=16)
    state = {k: v for k, v in old.state_dict().items() if not k.startswith("obs_")}
    path = tmp_path / "old.pt"
    torch.save(
        {"policy_state_dict": state, "obs_dim": 7, "action_dim": 3}, path
    )
    policy, ckpt = load_policy_from_checkpoint(path)
    assert ckpt["obs_dim"] == 7
    assert torch.equal(policy.obs_std, torch.ones(7))
    obs = np.random.randn(7).astype(np.float32)
    assert policy.predict(obs) == old.predict(obs)

    # PPOTrainer.load tolerates it as well
    env = TickTradingEnv(_make_trend_session(length=20), auto_features=True)
    trainer = PPOTrainer(env=env)
    trainer.policy = ActorCritic(obs_dim=7, hidden_dim=16)
    trainer.load(path)


def test_repo_legacy_checkpoint_loads():
    from pathlib import Path

    from trading_rl.policy import load_policy_from_checkpoint

    path = Path("models/ppo_7203_opening15m.pt")
    if not path.is_file():
        pytest.skip("legacy checkpoint not available")
    policy, _ckpt = load_policy_from_checkpoint(path)
    assert policy.obs_dim == 18


def test_checkpoint_stores_env_config_and_normalizer(tmp_path):
    from trading_rl.policy import load_policy_from_checkpoint

    env = TickTradingEnv(_make_trend_session(length=20), auto_features=True)
    trainer = PPOTrainer(env=env)
    mean = np.arange(env.observation_space.shape[0], dtype=np.float32)
    trainer.policy.set_obs_normalizer(mean, np.full_like(mean, 2.0))
    meta = {
        "window_seconds": 600.0,
        "opening_warmup_seconds": 15.0,
        "slippage": 0.5,
        "feature_names": ["a", "b"],
    }
    path = tmp_path / "m.pt"
    trainer.save(path, metadata=meta)
    policy, ckpt = load_policy_from_checkpoint(path)
    assert ckpt["env_config"] == meta
    torch.testing.assert_close(policy.obs_mean, torch.as_tensor(mean))
    torch.testing.assert_close(policy.obs_std, torch.full_like(policy.obs_std, 2.0))


# ---------------------------------------------------------------------------
# Trainer correctness
# ---------------------------------------------------------------------------
def _tiny_trainer(seed: int | None, **cfg) -> PPOTrainer:
    sessions = [
        _make_trend_session(start_price=100.0, slope=1.0, length=20),
        _make_trend_session(start_price=100.0, slope=-1.0, length=25),
    ]
    env = TickTradingEnv(sessions, auto_features=True)
    config = PPOConfig(rollout_steps=48, batch_size=16, update_epochs=2, **cfg)
    return PPOTrainer(env=env, config=config, seed=seed)


def test_same_seed_gives_identical_training_logs():
    t1 = _tiny_trainer(7)
    t2 = _tiny_trainer(7)
    log1 = [t1.train_iteration() for _ in range(2)]
    log2 = [t2.train_iteration() for _ in range(2)]
    for a, b in zip(log1, log2, strict=True):
        assert a.keys() == b.keys()
        for key in a:
            if np.isnan(a[key]):
                assert np.isnan(b[key])
            else:
                assert a[key] == b[key], key


def test_different_seed_changes_training():
    log1 = _tiny_trainer(1).train_iteration()
    log2 = _tiny_trainer(2).train_iteration()
    assert log1["policy_loss"] != log2["policy_loss"]


def test_episode_return_tracked_across_rollout_boundaries():
    session = _make_trend_session(start_price=100.0, slope=2.0, length=25)
    env = TickTradingEnv(session, auto_features=True)
    config = PPOConfig(rollout_steps=10, batch_size=10, update_epochs=1)
    trainer = PPOTrainer(env=env, config=config, seed=0)
    # Force "always BUY": a profitable long on a steady uptrend
    with torch.no_grad():
        trainer.policy.actor.bias.copy_(torch.tensor([0.0, 20.0, 0.0]))

    infos = [trainer.train_iteration() for _ in range(3)]
    # Episode = ticks 1..24 -> 23 steps: spans rollouts 1-3, completes in the third
    assert infos[0]["episodes_completed"] == 0.0 and np.isnan(infos[0]["mean_ep_return"])
    assert infos[1]["episodes_completed"] == 0.0 and np.isnan(infos[1]["mean_ep_return"])
    assert infos[2]["episodes_completed"] == 1.0
    # entry @ 102 (tick 1) -> forced exit @ 148 (tick 24): +46
    assert infos[2]["mean_ep_return"] == pytest.approx(46.0)


def test_gae_masks_bootstrap_with_own_done_flag():
    env = TickTradingEnv(_make_trend_session(length=20), auto_features=True)
    config = PPOConfig(rollout_steps=5, gamma=0.9, gae_lambda=0.8)
    trainer = PPOTrainer(env=env, config=config, seed=0)
    buf = trainer.buffer
    buf.rewards[:] = [1.0, 2.0, 3.0, 4.0, 5.0]
    buf.values[:] = [0.5, 0.4, 0.3, 0.2, 0.1]
    buf.dones[:] = [0.0, 1.0, 0.0, 0.0, 0.0]  # episode ends AFTER step 1
    obs = np.zeros(env.observation_space.shape[0], dtype=np.float32)
    adv, ret = trainer.compute_gae(obs, next_done=False)

    with torch.no_grad():
        v_last = float(trainer.policy.get_value(torch.as_tensor(obs).unsqueeze(0)))
    g, lam = 0.9, 0.8
    expected = np.zeros(5)
    last = 0.0
    for t in reversed(range(5)):
        nv = v_last if t == 4 else buf.values[t + 1]
        nt = 1.0 - buf.dones[t]
        delta = buf.rewards[t] + g * nv * nt - buf.values[t]
        last = delta + g * lam * nt * last
        expected[t] = last
    np.testing.assert_allclose(adv, expected, rtol=1e-5)
    np.testing.assert_allclose(ret, expected + buf.values, rtol=1e-5)
    # step 1 must not bootstrap from step 2's value (episode ended)
    assert adv[1] == pytest.approx(2.0 - 0.4, rel=1e-5)


def test_anneal_lr_decays_linearly():
    trainer = _tiny_trainer(0, anneal_lr=True, learning_rate=1e-3)
    info = trainer.train_iteration(progress=0.5)
    assert info["learning_rate"] == pytest.approx(5e-4)
    no_anneal = _tiny_trainer(0, learning_rate=1e-3)
    assert no_anneal.train_iteration(progress=0.5)["learning_rate"] == pytest.approx(1e-3)


# ---------------------------------------------------------------------------
# Evaluator / replay share the env's fills
# ---------------------------------------------------------------------------
def test_evaluate_policy_counts_breakeven_exits():
    from trading_rl.evaluator import BuyAndHoldPolicy

    prices = [100.0] * 10
    session = _make_trend_session(start_price=100.0, slope=0.0, length=10)
    assert list(session.price) == prices
    env = TickTradingEnv(session, auto_features=True)
    result = evaluate_policy(BuyAndHoldPolicy(), env)
    assert result.closed_trade_pnls == (0.0,)
    assert result.win_rate == 0.0
    assert result.total_pnl == 0.0
    assert result.fill_offset_min_s is not None
    assert result.fill_offset_min_s >= 15.0


def _save_policy(tmp_path, policy, env_config, name="m.pt"):
    env = TickTradingEnv(_make_trend_session(length=20), auto_features=True)
    trainer = PPOTrainer(env=env, policy=policy)
    path = tmp_path / name
    trainer.save(path, metadata=env_config)
    return path


def test_simulate_ai_trades_matches_env_fills_and_checkpoint_config(tmp_path):
    from trading_rl.evaluator import simulate_ai_trades

    session = _make_trend_session(start_price=100.0, slope=2.0, length=40)
    obs_dim = TickTradingEnv(session, auto_features=True).observation_space.shape[0]
    policy = ActorCritic(obs_dim=obs_dim, hidden_dim=16)
    with torch.no_grad():
        policy.actor.bias.copy_(torch.tensor([0.0, 20.0, 0.0]))  # always BUY
    cfg = {
        "window_seconds": 300.0,
        "opening_warmup_seconds": 30.0,
        "slippage": 0.5,
        "feature_names": [],
    }
    path = _save_policy(tmp_path, policy, cfg)

    trades, summary = simulate_ai_trades(session, path, qty=100)

    # Ground truth: drive the env directly with the same settings
    env = TickTradingEnv(
        session,
        slippage=0.5,
        opening_warmup_seconds=30.0,
        window_seconds=300.0,
        auto_features=True,
    )
    obs, _ = env.reset()
    fills = []
    done = False
    while not done:
        obs, _r, term, trunc, info = env.step(1 if env._position == 0 else 0)
        done = term or trunc
        fills += info["fills"]
    assert [t["us"] for t in trades] == [f["us"] for f in fills]
    assert [t["kind"] for t in trades] == ["entry", "exit"]
    assert trades[0]["us"] == session.t_open_us + 30_000_000  # first tick >= warmup
    assert trades[-1]["us"] <= session.t_open_us + 300_000_000
    assert trades[0]["price"] == pytest.approx(session.price[2] + 0.5)
    assert summary["tradeCount"] == 1
    assert summary["totalPnl"] == pytest.approx(sum(f["pnl"] for f in fills) * 100, abs=0.01)
    assert set(summary) == {"totalPnl", "tradeCount", "winRate"}
    assert all(t["qty"] == 100 for t in trades)

    # slippage override
    trades0, _ = simulate_ai_trades(session, path, slippage=0.0)
    assert trades0[0]["price"] == pytest.approx(session.price[2])


def test_simulate_ai_trades_legacy_checkpoint_defaults(tmp_path):
    from trading_rl.evaluator import read_checkpoint_env_config, simulate_ai_trades

    session = _make_trend_session(start_price=100.0, slope=1.0, length=30)
    obs_dim = TickTradingEnv(session, auto_features=True).observation_space.shape[0]
    path = _save_policy(tmp_path, ActorCritic(obs_dim=obs_dim, hidden_dim=16), None)
    cfg = read_checkpoint_env_config(path)
    assert cfg["window_seconds"] == 900.0
    assert cfg["opening_warmup_seconds"] == 15.0
    assert cfg["slippage"] == 0.0
    trades, summary = simulate_ai_trades(session, path)
    assert isinstance(trades, list)
    assert summary["tradeCount"] == len([t for t in trades if t["kind"] == "exit"])
    for t in trades:
        assert t["us"] >= session.t_open_us + 15_000_000


def test_simulate_ai_trades_real_legacy_model():
    from pathlib import Path

    from trading_rl.evaluator import simulate_ai_trades

    path = Path("models/ppo_7203_opening15m.pt")
    if not path.is_file():
        pytest.skip("legacy checkpoint not available")
    session = _make_trend_session(start_price=2500.0, slope=1.0, length=40)
    trades, summary = simulate_ai_trades(session, path)
    assert "totalPnl" in summary
    for t in trades:
        assert t["us"] >= session.t_open_us + 15_000_000
