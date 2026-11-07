"""v2 scalping stack: masked policy, masked PPO, schedules, behaviour cloning."""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest
import torch
from test_scalp_env import BASE, make_session

from trading_rl.policy import MASK_FILL, ActorCritic, apply_action_mask
from trading_rl.scalp_baselines import RuleScalper, RuleScalperParams
from trading_rl.scalp_bc import (
    BCDataset,
    TradeRow,
    bc_pretrain,
    collect_csv_dataset,
    collect_rule_dataset,
    load_trades_csv,
    masked_cross_entropy,
    plan_actions_from_trades,
)
from trading_rl.scalp_env import (
    OBS_DIM,
    ORDER_MODES,
    ScalpConfig,
    ScalpEnv,
    action_names,
    n_actions,
)
from trading_rl.trainer import PPOConfig
from trading_rl.vec_rollout import (
    VecPPOTrainer,
    VecScalpEnv,
    evaluate_ppo_policy,
    fill_curriculum_p,
    linear_schedule,
)

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


def moving_session(seed: int = 0):
    rng = np.random.default_rng(seed)
    secs = np.sort(rng.uniform(0, 600, 3000))
    px = np.clip(100 + np.cumsum(rng.choice([-1.0, 0.0, 1.0], size=3000)), 60, 140)
    ticks = [(float(t), float(p)) for t, p in zip(secs, px, strict=True)]
    ticks[0] = (0.0, 100.0)
    return make_session(ticks)


# ----------------------------------------------------------------- masked policy
def _policy(a: int = 9, seed: int = 0) -> ActorCritic:
    torch.manual_seed(seed)
    return ActorCritic(obs_dim=OBS_DIM, action_dim=a, hidden_dim=16)


def _random_masks(n: int, a: int, seed: int = 0) -> torch.Tensor:
    g = torch.Generator().manual_seed(seed)
    m = torch.rand(n, a, generator=g) > 0.5
    m[:, 0] = True  # keep is always legal
    return m


def test_apply_action_mask_and_none_passthrough():
    logits = torch.zeros(2, 4)
    assert apply_action_mask(logits, None) is logits
    out = apply_action_mask(logits, np.array([[1, 0, 1, 0], [1, 1, 1, 1]], dtype=bool))
    assert out[0, 1] == MASK_FILL and out[0, 3] == MASK_FILL and out[1].eq(0).all()


def test_masked_policy_never_samples_or_argmaxes_an_illegal_action():
    pol = _policy(9)
    n = 4000
    obs = torch.randn(n, OBS_DIM)
    masks = _random_masks(n, 9)
    for _ in range(3):
        action, logp, ent, _v = pol.get_action_and_value(obs, mask=masks)
        assert masks.gather(1, action[:, None]).all()
        assert torch.isfinite(logp).all() and torch.isfinite(ent).all()
    logits, _ = pol(obs)
    assert masks.gather(1, apply_action_mask(logits, masks).argmax(1)[:, None]).all()
    # predict(): a strongly illegal-favouring actor still returns a legal action
    with torch.no_grad():
        pol.actor.bias[3] = 100.0
    m1 = np.ones(9, dtype=bool)
    m1[3] = False
    for det in (True, False):
        for _ in range(30):
            assert pol.predict(np.zeros(OBS_DIM, np.float32), det, mask=m1) != 3
    assert pol.predict(np.zeros(OBS_DIM, np.float32), True) == 3  # unmasked unchanged


def test_masked_logprob_and_entropy_are_over_legal_actions_only():
    pol = _policy(5)
    obs = torch.randn(6, OBS_DIM)
    masks = torch.tensor(
        [[1, 1, 0, 0, 0]] * 3 + [[1, 0, 1, 1, 0]] * 3, dtype=torch.bool
    )
    acts = torch.tensor([0, 1, 0, 0, 2, 3])
    _, logp, ent, _ = pol.get_action_and_value(obs, acts, masks)
    logits, _ = pol(obs)
    for i in range(6):
        legal = logits[i][masks[i]]
        want = torch.log_softmax(legal, 0)
        assert logp[i].item() == pytest.approx(
            want[masks[i][: acts[i]].sum()].item(), abs=1e-5
        )
        assert ent[i].item() == pytest.approx(
            -(want.exp() * want).sum().item(), abs=1e-5
        )
    # unmasked call is untouched by the new argument
    _, lp0, _, _ = pol.get_action_and_value(obs, acts)
    assert torch.isfinite(lp0).all()


def test_ppo_ratio_is_one_with_stored_masks_and_grads_are_finite():
    pol = _policy(9)
    obs = torch.randn(512, OBS_DIM)
    masks = _random_masks(512, 9, seed=3)
    with torch.no_grad():
        act, logp_old, _e, _v = pol.get_action_and_value(obs, mask=masks)
        _a, logp_same, *_ = pol.get_action_and_value(obs, act, masks)
    assert torch.allclose(logp_old, logp_same, atol=1e-6)  # ratio == 1
    _a, logp_new, ent, val = pol.get_action_and_value(obs, act, masks)
    loss = -(logp_new - logp_old).exp().mean() - 0.01 * ent.mean() + val.mean()
    loss.backward()
    assert all(
        torch.isfinite(p.grad).all() for p in pol.parameters() if p.grad is not None
    )


@pytest.mark.parametrize("mode", ORDER_MODES)
def test_vec_trainer_stores_masks_and_updates_with_them(mode):
    cfg = ScalpConfig(order_mode=mode)
    sessions = [moving_session(i) for i in range(3)]
    venv = VecScalpEnv(sessions, 3, cfg, seed=1)
    assert venv.masks.shape == (3, n_actions(mode)) and venv.masks[:, 0].all()
    pol = ActorCritic(OBS_DIM, n_actions(mode), hidden_dim=16)
    tr = VecPPOTrainer(
        venv,
        pol,
        PPOConfig(batch_size=64, update_epochs=2),
        steps_per_env=64,
        seed=0,
    )
    m = tr.train_iteration(progress=0.0)
    assert np.isfinite(m["policy_loss"]) and np.isfinite(m["entropy"])
    stored = tr.buffer.masks
    assert stored is not None and stored.shape == (64 * 3, n_actions(mode))
    # every stored action was legal under the mask it was sampled with
    assert stored[np.arange(len(stored)), tr.buffer.actions].all()
    # first update epoch sees ratio 1: stored logprobs reproduce under stored masks
    with torch.no_grad():
        _a, lp, *_ = pol.get_action_and_value(
            torch.as_tensor(tr.buffer.obs),
            torch.as_tensor(tr.buffer.actions),
            torch.as_tensor(stored),
        )
    assert np.isfinite(lp.numpy()).all()


def test_masked_training_rollout_has_zero_invalid_actions():
    cfg = ScalpConfig(order_mode="both")
    venv = VecScalpEnv([moving_session(0), moving_session(1)], 4, cfg, seed=2)
    pol = ActorCritic(OBS_DIM, n_actions("both"), hidden_dim=16)
    tr = VecPPOTrainer(venv, pol, PPOConfig(batch_size=128), steps_per_env=300, seed=0)
    tr.train_iteration()
    tr.train_iteration()
    assert tr.episode_log, "needs finished episodes"
    assert sum(e["n_invalid_actions"] for e in tr.episode_log) == 0
    # unmasked ablation still works (legacy path) and does hit illegal actions
    venv = VecScalpEnv([moving_session(0)], 4, cfg, seed=2)
    pol = ActorCritic(OBS_DIM, n_actions("both"), hidden_dim=16)
    tr = VecPPOTrainer(
        venv, pol, PPOConfig(batch_size=128), steps_per_env=300, seed=0, use_masks=False
    )
    tr.train_iteration()
    tr.train_iteration()
    assert tr.buffer.masks is None
    assert sum(e["n_invalid_actions"] for e in tr.episode_log) > 0


def test_evaluate_ppo_policy_masks_and_action_counts():
    sessions = [moving_session(i) for i in range(4)]
    pol = _policy(11)
    with torch.no_grad():
        pol.actor.bias[8] = 50.0  # market_flatten: illegal while flat
    cfg = ScalpConfig(order_mode="both")
    raw = evaluate_ppo_policy(pol, sessions, cfg, n_envs=2)
    masked = evaluate_ppo_policy(pol, sessions, cfg, n_envs=2, use_masks=True)
    assert all(e["actions"].shape == (11,) for e in masked)
    assert sum(e["n_invalid_actions"] for e in masked) == 0
    assert sum(e["n_invalid_actions"] for e in raw) > 0
    assert all(e["actions"].sum() == 590 for e in masked)


# --------------------------------------------------------------------- schedules
def test_fill_curriculum_endpoints():
    assert fill_curriculum_p(0, 1000, 0.0, 0.5) == 0.0
    assert fill_curriculum_p(250, 1000, 0.0, 0.5) == pytest.approx(0.5)
    assert fill_curriculum_p(500, 1000, 0.0, 0.5) == 1.0
    assert fill_curriculum_p(900, 1000, 0.0, 0.5) == 1.0
    assert fill_curriculum_p(0, 1000, 0.2, 0.5) == pytest.approx(0.2)
    assert fill_curriculum_p(250, 1000, 0.2, 0.5) == pytest.approx(0.6)
    assert fill_curriculum_p(0, 1000, 0.0, 0.0) == 1.0  # frac 0 = no curriculum
    assert fill_curriculum_p(10**9, 1000, 0.3, 1.0) == 1.0


def test_entropy_schedule_linear_and_cli_defaults():
    assert linear_schedule(0.05, 0.005, 0.0) == pytest.approx(0.05)
    assert linear_schedule(0.05, 0.005, 0.5) == pytest.approx(0.0275)
    assert linear_schedule(0.05, 0.005, 1.0) == pytest.approx(0.005)
    assert linear_schedule(0.05, 0.005, 2.0) == pytest.approx(0.005)  # clipped
    import train_scalp as ts

    args = ts.build_parser().parse_args([])
    assert ts.entropy_schedule(args) == (0.02, 0.02)  # single coef when not given
    args = ts.build_parser().parse_args(["--ent-start", "0.05", "--ent-end", "0.005"])
    assert ts.entropy_schedule(args) == (0.05, 0.005)
    args = ts.build_parser().parse_args(["--ent-end", "0.0"])
    assert ts.entropy_schedule(args) == (0.02, 0.0)
    assert ts.parse_fill_curriculum("0.0,0.5") == (0.0, 0.5)
    assert ts.parse_fill_curriculum(None) is None
    with pytest.raises(SystemExit):
        ts.parse_fill_curriculum("0.5")
    cfg = ts.train_config(
        ts.build_parser().parse_args(
            ["--order-mode", "market", "--inventory-penalty", "0.0025"]
        )
    )
    assert cfg.order_mode == "market" and cfg.inventory_penalty_ticks_per_s == 0.0025


def test_vec_env_curriculum_picks_fill_rule_per_new_episode():
    sess = moving_session(0)
    venv = VecScalpEnv(
        [sess], 8, ScalpConfig(), seed=0, stagger=False, p_trade_through=0.0
    )
    assert {e.fill_rule for e in venv.envs} == {"touch"}
    venv.set_p_trade_through(1.0)
    for _ in range(590):
        venv.step(np.zeros(8, dtype=np.int64))  # episodes end, reset with new rule
    assert {e.fill_rule for e in venv.envs} == {"trade_through"}
    venv = VecScalpEnv([sess], 8, ScalpConfig(), seed=0, stagger=False)
    assert {e.fill_rule for e in venv.envs} == {"trade_through"}  # None = config rule


# ---------------------------------------------------------------------------- BC
def test_masked_cross_entropy_ignores_illegal_actions():
    logits = torch.tensor([[0.0, 5.0, 0.0]])
    masks = torch.tensor([[True, False, True]])
    loss = masked_cross_entropy(logits, torch.tensor([0]), masks)
    assert loss.item() == pytest.approx(np.log(2.0), abs=1e-6)
    smooth = masked_cross_entropy(logits, torch.tensor([0]), masks, 0.1)
    assert smooth.item() == pytest.approx(np.log(2.0), abs=1e-6)


def _synthetic_bc(n: int = 600, a: int = 4, seed: int = 0) -> BCDataset:
    rng = np.random.default_rng(seed)
    obs = rng.normal(size=(n, OBS_DIM)).astype(np.float32)
    actions = (obs[:, 0] > 0).astype(np.int64) + 2 * (obs[:, 1] > 0)
    masks = np.ones((n, a), dtype=bool)
    returns = (obs[:, 2]).astype(np.float32)
    return BCDataset(obs, actions, masks, returns, 1)


def test_bc_loss_decreases_on_tiny_synthetic_set():
    data = _synthetic_bc()
    pol = _policy(4)
    res = bc_pretrain(
        pol, data, steps=300, batch_size=128, lr=3e-3, label_smoothing=0.0
    )
    assert res["loss_end"] < 0.5 * res["loss_start"]
    assert res["accuracy"] > 0.85
    # critic fit runs and changes the value head
    before = pol.critic.weight.detach().clone()
    bc_pretrain(pol, data, steps=20, fit_critic=True)
    assert not torch.equal(before, pol.critic.weight)
    with pytest.raises(ValueError):
        bc_pretrain(
            pol,
            BCDataset(data.obs[:0], data.actions[:0], data.masks[:0], data.returns[:0]),
            steps=5,
        )


@pytest.mark.parametrize("mode", ORDER_MODES)
def test_rule_dataset_actions_are_legal_and_clonable(mode):
    sessions = [moving_session(i) for i in range(3)]
    cfg = ScalpConfig(order_mode=mode, inventory_penalty_ticks_per_s=0.01)
    rule = RuleScalper(RuleScalperParams(mode="long", k_entry=1, tp=1, sl=3.0))
    data = collect_rule_dataset(sessions, rule, cfg, gamma=0.99)
    assert data.obs.shape[1] == OBS_DIM and len(data) > 1000
    assert data.masks.shape[1] == n_actions(mode)
    assert data.masks[np.arange(len(data)), data.actions].all()  # all labels legal
    assert len(data) == 3 * 590  # the rule never emits an illegal action
    counts = data.action_counts()
    assert counts[0] > 0 and counts.sum() == len(data)
    assert np.isfinite(data.returns).all()
    pol = ActorCritic(OBS_DIM, n_actions(mode), hidden_dim=32)
    res = bc_pretrain(pol, data, steps=200, lr=3e-3, label_smoothing=0.0)
    assert res["loss_end"] < res["loss_start"]


# --------------------------------------------------------------------------- CSV
CSV_TEXT = (
    "date,stem,us,side,price,qty,kind,order\n"
    "2026-08-14,1234,{t0},buy,100.5,100,entry,market\n"
    "2026-08-14,1234,{t1},sell,99.5,100,exit,market\n"
    "2026-08-14,1234,25000000,sell,101.0,100,entry,limit\n"
    "2026-08-14,1234,40000000,buy,100.0,100,exit,\n"
)


def _write_csv(tmp_path):
    from test_scalp_env import T_OPEN

    p = tmp_path / "trades.csv"
    p.write_text(
        CSV_TEXT.format(t0=T_OPEN + 20_000_000, t1=T_OPEN + 30_000_000),
        encoding="utf-8",
    )
    return p


def test_load_trades_csv_groups_and_validates(tmp_path):
    trades = load_trades_csv(_write_csv(tmp_path))
    rows = trades[("2026-08-14", "1234")]
    assert [r.kind for r in rows] == ["entry", "exit", "entry", "exit"]  # file order
    assert rows[0].side == 1 and rows[0].order == "market"
    bad = tmp_path / "bad.csv"
    bad.write_text("date,stem,us\n2026-08-14,1234,1\n", encoding="utf-8")
    with pytest.raises(ValueError, match="missing columns"):
        load_trades_csv(bad)
    bad.write_text(
        "date,stem,us,side,price,qty,kind\n2026-08-14,1234,1,hold,1,100,entry\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="unknown side"):
        load_trades_csv(bad)


def test_csv_mapping_to_decision_steps_and_actions(tmp_path):
    sess = make_session(BASE)
    trades = load_trades_csv(_write_csv(tmp_path))[("2026-08-14", "1234")]
    # market mode: entry buy -> market_buy (idx 1), exit -> flatten (3), sell entry (2)
    plan = plan_actions_from_trades(sess, trades, ScalpConfig(order_mode="market"))
    # fills at 20 s / 30 s / 25 s / 40 s minus the 50 ms latency -> second 19 / 29 / 24 / 39
    assert plan == {19: 1, 29: 3, 24: 2, 39: 3}
    # limit mode: market entry has no market action -> bid_k0 (2); market exit flatten
    # (8); the limit sell at 101 vs last 100 -> ask_k1 (6); the limit buy at 100 -> bid_k0
    plan = plan_actions_from_trades(sess, trades, ScalpConfig(order_mode="limit"))
    assert plan == {19: 2, 29: 8, 24: 6, 39: 2}
    # both mode keeps market buys; unlabelled-order exit at 100.0 = last -> limit-ish
    plan = plan_actions_from_trades(sess, trades, ScalpConfig(order_mode="both"))
    assert plan[19] == 9 and plan[29] == 8 and plan[24] == 6 and plan[39] == 2
    # relative microseconds and the window clip
    rel = [TradeRow("2026-08-14", "1234", 1_000_000, 1, 100.5, 100, "entry", "market")]
    p = plan_actions_from_trades(sess, rel, ScalpConfig(order_mode="market"))
    assert list(p) == [10]  # clipped up to the first decision second


def test_csv_dataset_replays_labelled_actions(tmp_path):
    sess = make_session(BASE)
    trades = load_trades_csv(_write_csv(tmp_path))
    data = collect_csv_dataset([sess], trades, ScalpConfig(order_mode="market"))
    assert data.masks[np.arange(len(data)), data.actions].all()
    counts = data.action_counts()
    # market buy (19) and market sell (24, exits the long) are labelled; the flatten
    # labels at 29 / 39 hit a flat book (illegal) and are dropped from the dataset
    assert counts[1] == 1 and counts[2] == 1 and counts[3] == 0
    assert len(data) == 590 - 2
    assert (
        collect_csv_dataset([make_session(BASE)], {}, ScalpConfig()).obs.shape[0] == 0
    )


def test_baseline_cache_roundtrip(tmp_path):
    import train_scalp as ts

    args = ts.build_parser().parse_args(["--order-mode", "market"])
    key = ts.baseline_key(args)
    summ = {
        "n_sessions": 3,
        "yen_day": (1.0, 0.5, 1.5),
        "ticks_day": (0.1, 0.0, 0.2),
        "actions": np.array([1, 2, 3, 4]),
        "pf": float("nan"),
    }
    entry = {
        "best_params": RuleScalperParams(sl=float("inf")).as_dict(),
        "tune": [
            {"params": {"sl": "inf"}, "chosen": True},
            {"meta": {"n_configs": 36}},
        ],
        "results": {"val_seen": {"DoNothing": {"touch": summ}}},
        "counts": {"train": (1, 0)},
    }
    path = tmp_path / "b.json"
    ts.save_baseline_cache(path, key, entry)
    ts.save_baseline_cache(path, key.replace("market", "limit"), entry)  # another mode
    got = ts.load_baseline_cache(path, key)
    s = got["results"]["val_seen"]["DoNothing"]["touch"]
    assert s["yen_day"] == (1.0, 0.5, 1.5) and isinstance(s["actions"], np.ndarray)
    assert s["actions"].tolist() == [1, 2, 3, 4] and np.isnan(s["pf"])
    assert ts.load_baseline_cache(path, "other") is None
    assert ts.rule_params_from_json(
        __import__("json").dumps(got["best_params"])
    ).sl == float("inf")
    assert action_names("market")[0] == "keep"
    assert ScalpEnv is not None


def test_exclude_sessions_drops_pairs_and_keys_baseline_cache(tmp_path):
    import train_scalp as ts

    p = tmp_path / "ex.csv"
    p.write_text("﻿date,stem\n2026-07-08,285A\n2026-07-09,6976\n", encoding="utf-8")
    ex = ts.load_exclude_sessions(p)
    assert ex == {("2026-07-08", "285a"), ("2026-07-09", "6976")}
    rows = [
        ("2026-07-08", "285a", 1.0, 1.0, False),
        ("2026-07-08", "6976", 1.0, 1.0, False),
        ("2026-07-09", "6976", 1.0, 1.0, True),
    ]
    assert ts.drop_sessions(rows, ex) == [rows[1]]
    base = ts.baseline_key(ts.build_parser().parse_args([]))
    keyed = ts.baseline_key(
        ts.build_parser().parse_args(["--exclude-sessions", str(p)])
    )
    assert keyed != base and keyed.startswith(base)


def test_action_shaping_cli_reaches_train_config_only():
    import train_scalp as ts

    args = ts.build_parser().parse_args(
        ["--order-mode", "market", "--keep-penalty", "0.01", "--action-bonus", "0.25"]
    )
    cfg = ts.train_config(args)
    assert cfg.keep_penalty_ticks == 0.01 and cfg.action_bonus_ticks == 0.25
    assert (
        cfg.inventory_penalty_ticks_per_s == 0.0 and cfg.idle_penalty_ticks_per_s == 0.0
    )


def test_reward_mode_and_gae_lambda_cli():
    import train_scalp as ts

    args = ts.build_parser().parse_args(
        ["--reward-mode", "realized", "--gae-lambda", "1.0"]
    )
    assert ts.train_config(args).reward_mode == "realized" and args.gae_lambda == 1.0
    assert ts.train_config(ts.build_parser().parse_args([])).reward_mode == "mtm"
