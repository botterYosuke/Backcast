"""Tests for trading_rl.scalp_baselines."""

from __future__ import annotations

import numpy as np
import pytest
from test_scalp_env import BASE, make_session

from trading_rl.scalp_baselines import (
    DoNothingPolicy,
    RandomScalpPolicy,
    RuleScalper,
    RuleScalperParams,
    make_baseline,
    rule_scalper_grid,
)
from trading_rl.scalp_env import ScalpConfig, ScalpEnv


def play(policy, sess, **cfg):
    env = ScalpEnv(config=ScalpConfig(**cfg))
    obs, _ = env.reset(options={"session": sess})
    policy.reset()
    total, done, ep = 0.0, False, {}
    while not done:
        obs, r, done, _, info = env.step(policy.act(env, obs))
        total += r
        ep = info.get("episode", ep)
    return env, total, ep


def test_do_nothing_is_exactly_zero():
    env, total, ep = play(DoNothingPolicy(), make_session(BASE + [(50.0, 90.0)]))
    assert total == 0.0 and ep["pnl_ticks"] == 0.0 and env.fills == []


def test_random_policy_is_seeded_and_covers_actions():
    p1, p2 = RandomScalpPolicy(5), RandomScalpPolicy(5)
    a1 = [p1.act(None, None) for _ in range(200)]  # type: ignore[arg-type]
    a2 = [p2.act(None, None) for _ in range(200)]  # type: ignore[arg-type]
    assert a1 == a2 and set(a1) == set(range(9))


def test_rule_scalper_round_trip_take_profit():
    # bid at 99 fills on the 98 print, exit ask 100 (entry + 1 tick) then fills.
    ticks = BASE + [(14.0, 98.0), (20.0, 99.5), (25.0, 101.0)]
    sess = make_session(ticks)
    pol = RuleScalper(RuleScalperParams(mode="long", k_entry=1, tp=1, requote_s=500))
    env, total, ep = play(pol, sess)
    assert ep["n_trips"] == 1 and ep["n_exit_limit"] == 1
    assert ep["pnl_ticks"] == pytest.approx(1.0)
    assert total == pytest.approx(1.0)


def test_rule_scalper_stop_loss_and_timeout_use_market_exit():
    ticks = BASE + [(14.0, 98.0), (20.0, 94.0)]
    pol = RuleScalper(RuleScalperParams(k_entry=1, tp=1, sl=3.0, requote_s=500))
    env, _, ep = play(pol, make_session(ticks))
    assert ep["n_exit_market"] == 1 and ep["pnl_ticks"] < 0
    ticks = BASE + [(14.0, 98.0)]
    pol = RuleScalper(
        RuleScalperParams(k_entry=1, tp=3, sl=float("inf"), timeout_s=30, requote_s=500)
    )
    env, _, ep = play(pol, make_session(ticks))
    exits = [f for f in env.fills if f["kind"] == "exit"]
    assert exits and exits[0]["order"] == "market" and not exits[0]["forced"]
    assert exits[0]["us"] >= 14_000_000 + 28_000_000


def test_rule_scalper_mm_quotes_both_sides():
    pol = RuleScalper(RuleScalperParams(mode="mm", k_entry=1))
    env = ScalpEnv(config=ScalpConfig())
    obs, _ = env.reset(options={"session": make_session(BASE)})
    for _ in range(3):
        obs, *_ = env.step(pol.act(env, obs))
    v = env.state_view()
    assert v["bid"] == 99.0 and v["ask"] == 101.0


def test_rule_scalper_no_entry_near_the_end_and_flat_at_end():
    ticks = BASE + [(14.0, 98.0)]
    env, _, ep = play(RuleScalper(), make_session(ticks))
    assert env.position == 0
    assert all(f["us"] <= 600_000_000 for f in env.fills)


def test_grid_and_factory():
    grid = rule_scalper_grid()
    assert len(grid) == 216 and len({g for g in grid}) == 216
    pol = make_baseline({"name": "RuleScalper", "params": grid[0].as_dict()})
    assert isinstance(pol, RuleScalper) and pol.p == grid[0]
    inf = [g for g in grid if g.sl == float("inf")][0]
    assert make_baseline({"name": "RuleScalper", "params": inf.as_dict()}).p == inf
    assert np.isfinite(grid[0].tp)


# ----------------------------------------------------------------- v2 / modes
from trading_rl.scalp_env import ORDER_MODES, n_actions  # noqa: E402


def test_grid_shrinks_for_market_mode():
    assert len(rule_scalper_grid(order_mode="limit")) == 216
    g = rule_scalper_grid(order_mode="market")
    assert len(g) == 36 and {x.k_entry for x in g} == {0} and {x.requote_s for x in g} == {5}


def test_random_policy_follows_the_env_action_set():
    for mode in ORDER_MODES:
        env = ScalpEnv([make_session(BASE)], ScalpConfig(order_mode=mode))
        pol = RandomScalpPolicy(1)
        acts = {pol.act(env, None) for _ in range(300)}  # type: ignore[arg-type]
        assert acts == set(range(n_actions(mode)))


def test_rule_scalper_market_mode_round_trip():
    # buy at 100.5 on the first decision; +3 at 20 s >= tp 2 -> market flatten 102.5
    ticks = BASE + [(20.0, 103.0)]
    pol = RuleScalper(RuleScalperParams(mode="long", tp=2, sl=float("inf"), timeout_s=500))
    env, _, ep = play(pol, make_session(ticks), order_mode="market")
    first = env._trips[0]  # noqa: SLF001
    assert first["pnl_ticks"] == pytest.approx(2.0) and first["exit_order"] == "market"
    assert ep["n_entry_limit"] == 0 and ep["n_entry_market"] >= 1
    assert ep["n_invalid_actions"] == 0 and env.position == 0


def test_rule_scalper_market_mm_alternates_direction_and_stops_late():
    pol = RuleScalper(RuleScalperParams(mode="mm", tp=50, sl=50.0, timeout_s=20))
    env, _, ep = play(pol, make_session(BASE), order_mode="market")
    sides = [f["side"] for f in env.fills if f["kind"] == "entry"]
    assert len(sides) >= 3 and sides[:3] == [1, -1, 1]
    last_entry = max(f["us"] for f in env.fills if f["kind"] == "entry")
    assert last_entry <= (600 - 30) * 1_000_000 + 100_000  # no_entry_last_s = 30


def test_rule_scalper_emits_only_legal_actions_in_every_mode():
    rng = np.random.default_rng(2)
    secs = np.sort(rng.uniform(0, 600, 2500))
    px = np.clip(100 + np.cumsum(rng.choice([-1.0, 0.0, 1.0], size=2500)), 60, 140)
    ticks = [(float(t), float(p)) for t, p in zip(secs, px, strict=True)]
    ticks[0] = (0.0, 100.0)
    sess = make_session(ticks)
    for mode in ORDER_MODES:
        for rmode in ("long", "mm"):
            pol = RuleScalper(RuleScalperParams(mode=rmode, k_entry=1, tp=1, sl=3.0))
            env, _, ep = play(pol, sess, order_mode=mode)
            assert ep["n_invalid_actions"] == 0, (mode, rmode)
            assert env.position == 0 and ep["n_trips"] >= 1


# ------------------------------------------------------------------ Momentum
def test_momentum_grid_and_factory():
    from trading_rl.scalp_baselines import (
        MomentumParams,
        MomentumScalper,
        momentum_grid,
    )

    grid = momentum_grid()
    assert len(grid) == len({tuple(g.as_dict().values()) for g in grid}) == 81
    assert {g.lookback_s for g in grid} == {30, 60, 120}
    assert {g.thr_ticks for g in grid} == {0.0, 1.0, 2.0}
    assert {g.hold_s for g in grid} == {60, 180, 590}
    assert {g.entry_s for g in grid} == {10, 30, 60}
    spec = {"name": "Momentum", "params": MomentumParams(lookback_s=30).as_dict()}
    pol = make_baseline(spec)
    assert isinstance(pol, MomentumScalper) and pol.p.lookback_s == 30


def test_momentum_buys_an_up_move_and_exits_on_hold_timeout():
    from trading_rl.scalp_baselines import MomentumParams, MomentumScalper

    # flat at 100, jumps to 102 at 40 s, 103 at 70 s: buy at 40 s (move +2 ticks over 30 s)
    ticks = BASE[:2] + [(40.0, 102.0), (70.0, 103.0), (599.9, 103.0)]
    pol = MomentumScalper(MomentumParams(lookback_s=30, thr_ticks=1, hold_s=60, entry_s=10))
    env, _, ep = play(pol, make_session(ticks), order_mode="market")
    entry, first_exit = env.fills[0], env.fills[1]
    assert entry["side"] == 1 and entry["order"] == "market" and entry["us"] == 40_050_000
    assert entry["price"] == pytest.approx(102.5)
    # holds 60 s: flattened at the decision second 100 (since_entry >= 60), not forced
    assert first_exit["kind"] == "exit" and first_exit["us"] == 100_050_000
    assert first_exit["forced"] is False and first_exit["price"] == pytest.approx(102.5)
    assert ep["n_exit_market"] >= 1


def test_momentum_sells_a_down_move_and_respects_entry_s_and_threshold():
    from trading_rl.scalp_baselines import MomentumParams, MomentumScalper

    ticks = BASE[:2] + [(40.0, 98.0), (599.9, 98.0)]
    mk = lambda **k: MomentumScalper(MomentumParams(**{"lookback_s": 30, "thr_ticks": 1, **k}))  # noqa: E731
    env, _, ep = play(mk(hold_s=590), make_session(ticks), order_mode="market")
    assert env.fills[0]["side"] == -1 and env.fills[0]["us"] == 40_050_000
    assert ep["n_forced"] == 1  # hold 590 s = to the forced close
    # sold at 98 - 0.5, bought back at 98 + 0.5 on a flat tape: slippage only
    assert ep["pnl_ticks"] == pytest.approx(-1.0)
    assert ep["gross_ticks_sum"] == pytest.approx(0.0)
    # nothing before entry_s: the move exists from 40 s but entry_s = 100 delays entry
    env, _, _ = play(mk(entry_s=100, lookback_s=120, hold_s=590), make_session(ticks), order_mode="market")
    assert env.fills[0]["us"] == 100_050_000
    # strict threshold: a 2-tick move does not trigger at thr = 2
    env, _, ep = play(mk(thr_ticks=2), make_session(ticks), order_mode="market")
    assert env.fills == [] and ep["n_trips"] == 0


def test_momentum_needs_market_actions_and_cannot_peek():
    from trading_rl.scalp_baselines import MomentumParams, MomentumScalper

    env = ScalpEnv(config=ScalpConfig(order_mode="limit"))
    env.reset(options={"session": make_session(BASE)})
    with pytest.raises(ValueError, match="market"):
        MomentumScalper().act(env, np.zeros(1))
    env = ScalpEnv(config=ScalpConfig(order_mode="both"))
    env.reset(options={"session": make_session(BASE + [(300.0, 150.0)])})
    d = env.seconds
    assert env.price_at(d + 200) == env.price_at(d)  # clamped to now, never the future
    assert env.price_at(-50) == env.price_at(0)
    play(MomentumScalper(MomentumParams()), make_session(BASE), order_mode="both")
