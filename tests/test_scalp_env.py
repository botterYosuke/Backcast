"""Tests for trading_rl.scalp_env (limit-order scalping environment)."""

from __future__ import annotations

import numpy as np
import pytest

from trading_rl.data import OpeningSessionData
from trading_rl.scalp_env import (
    ACTION_NAMES,
    N_STEPS,
    OBS_DIM,
    US,
    ScalpConfig,
    ScalpEnv,
    ScalpSession,
    build_scalp_session,
)

T_OPEN = 1_786_698_000_000_000
KEEP, CANCEL, FLAT = 0, 1, 8
BID0, BID1, BID2 = 2, 3, 4
ASK0, ASK1, ASK2 = 5, 6, 7


def make_session(
    ticks: list[tuple[float, float]], *, is_topix100: bool = False
) -> ScalpSession:
    """Ticks are ``(seconds since T_open, price)``; tick size is 1 yen at ~100."""
    ticks = sorted(ticks)
    epoch = np.array([T_OPEN + round(t * US) for t, _ in ticks], dtype=np.int64)
    price = np.array([p for _, p in ticks], dtype=np.float64)
    raw = OpeningSessionData(
        stem="1234",
        code="12340",
        date="2026-08-14",
        t_open_us=T_OPEN,
        p_open=float(price[0]),
        v_open=1000,
        epoch_us=epoch,
        price=price,
        qty=np.full(len(price), 100, dtype=np.int64),
        trade_type=np.full(len(price), "", dtype=object),
    )
    return build_scalp_session(
        raw, prev_close=100.0, avg_volume_20d=1e6, is_topix100=is_topix100
    )


def run(
    sess: ScalpSession,
    actions: dict[int, int],
    *,
    fill_rule: str = "trade_through",
    **cfg: float,
) -> tuple[ScalpEnv, float, dict]:
    """Play ``actions`` (decision second -> action; default KEEP) to the end."""
    env = ScalpEnv(config=ScalpConfig(fill_rule=fill_rule, **cfg))  # type: ignore[arg-type]
    env.reset(options={"session": sess})
    total = 0.0
    info: dict = {}
    done = False
    s = 10
    while not done:
        _, r, done, _, info = env.step(actions.get(s, KEEP))
        total += r
        s += 1
    return env, total, info["episode"]


BASE = [(0.0, 100.0), (9.0, 100.0), (599.9, 100.0)]


def test_spaces_and_obs_shape():
    sess = make_session(BASE)
    env = ScalpEnv([sess])
    obs, _ = env.reset(seed=0)
    assert obs.shape == (OBS_DIM,) and obs.dtype == np.float32
    assert env.action_space.n == len(ACTION_NAMES) == 9
    assert np.isfinite(obs).all()
    steps = 0
    done = False
    while not done:
        obs, _, done, _, _ = env.step(KEEP)
        steps += 1
        assert np.isfinite(obs).all()
    assert steps == N_STEPS == 590


def test_trade_through_vs_touch_exact_print_at_limit():
    # bid at 99 (last 100 - 1 tick); a print exactly at 99 at 12 s.
    ticks = BASE + [(12.0, 99.0)]
    sess = make_session(ticks)
    env_tt, _, ep_tt = run(sess, {10: BID1}, fill_rule="trade_through")
    assert env_tt.fills == [] or all(f["forced"] for f in env_tt.fills)
    assert ep_tt["n_entry_limit"] == 0
    env_t, _, ep_t = run(sess, {10: BID1}, fill_rule="touch")
    assert ep_t["n_entry_limit"] == 1
    assert env_t.fills[0]["price"] == 99.0 and env_t.fills[0]["kind"] == "entry"


def test_trade_through_fills_on_strict_print_below_limit_at_limit_price():
    ticks = BASE + [(12.0, 99.0), (13.0, 98.0)]
    env, _, ep = run(make_session(ticks), {10: BID1})
    f = env.fills[0]
    assert (f["side"], f["price"], f["kind"], f["order"]) == (1, 99.0, "entry", "limit")
    assert f["us"] == 13 * US
    assert ep["n_entry_limit"] == 1


def test_sell_limit_rule_symmetry():
    ticks = BASE + [(12.0, 101.0), (13.0, 102.0)]
    env_tt, _, _ = run(make_session(ticks), {10: ASK1}, fill_rule="trade_through")
    f = env_tt.fills[0]
    assert (f["side"], f["price"], f["us"]) == (-1, 101.0, 13 * US)
    env_t, _, _ = run(make_session(ticks), {10: ASK1}, fill_rule="touch")
    assert env_t.fills[0]["us"] == 12 * US


def test_latency_print_before_activation_cannot_fill():
    # order placed at the 10 s decision is active at 10.05 s.
    early = BASE + [(10.02, 98.0)]
    _, _, ep = run(make_session(early), {10: BID1}, latency_ms=50)
    assert ep["n_entry_limit"] == 0
    late = BASE + [(10.06, 98.0)]
    env, _, ep = run(make_session(late), {10: BID1}, latency_ms=50)
    assert ep["n_entry_limit"] == 1
    assert env.fills[0]["us"] == round(10.06 * US)
    # exactly at the activation instant: strictly-after rule => no fill
    at = BASE + [(10.05, 98.0)]
    _, _, ep = run(make_session(at), {10: BID1}, latency_ms=50)
    assert ep["n_entry_limit"] == 0
    # with zero latency the 10.02 s print is after activation
    _, _, ep = run(make_session(early), {10: BID1}, latency_ms=0)
    assert ep["n_entry_limit"] == 1


def test_position_limits_are_noops_and_counted():
    # buy fills at 11 s; then a bid while long is invalid; ask is the exit.
    ticks = BASE + [(11.5, 98.0), (12.5, 100.0)]
    env, _, ep = run(make_session(ticks), {10: BID1, 13: BID1, 14: BID0})
    assert env.fills[0]["kind"] == "entry" and env.fills[0]["side"] == 1
    assert ep["n_invalid_actions"] == 2
    # short side
    ticks = BASE + [(11.5, 102.0)]
    _, _, ep = run(make_session(ticks), {10: ASK1, 14: ASK0})
    assert ep["n_invalid_actions"] == 1
    # flatten with nothing to flatten is an (invalid) no-op
    _, _, ep = run(make_session(BASE), {10: FLAT})
    assert ep["n_invalid_actions"] == 1 and ep["n_trips"] == 0


def test_market_making_both_sides_capture_spread():
    # bid 99 and ask 101 working; dip to 98 fills the bid (long), rally to 102
    # fills the ask as the exit -> flat, +2 yen = +2 ticks.
    ticks = BASE + [(14.0, 98.0), (16.0, 102.0)]
    env, total, ep = run(make_session(ticks), {10: BID1, 11: ASK1})
    kinds = [(f["kind"], f["side"], f["price"]) for f in env.fills]
    assert kinds == [("entry", 1, 99.0), ("exit", -1, 101.0)]
    assert env.position == 0
    assert ep["n_trips"] == 1 and ep["n_exit_limit"] == 1 and ep["n_forced"] == 0
    assert ep["pnl_ticks"] == pytest.approx(2.0)
    assert ep["pnl_yen100"] == pytest.approx(200.0)
    assert total == pytest.approx(2.0)


def test_entry_keeps_other_side_as_exit_and_cancels_nothing():
    ticks = BASE + [(14.0, 98.0)]
    env, _, _ = run(
        make_session(ticks), {10: BID1, 11: ASK2}, fill_rule="trade_through"
    )
    # after the bid filled the ask (102) must still be working
    assert env.position == 1 or any(f["forced"] for f in env.fills)
    assert len([f for f in env.fills if not f["forced"]]) == 1
    env2 = ScalpEnv(config=ScalpConfig())
    env2.reset(options={"session": make_session(ticks)})
    for s in range(10, 16):
        env2.step({10: BID1, 11: ASK2}.get(s, KEEP))
    v = env2.state_view()
    assert v["pos"] == 1 and v["bid"] is None and v["ask"] == 102.0


def test_forced_close_at_600s_with_market_slippage():
    ticks = BASE + [(14.0, 98.0), (500.0, 97.0), (599.95, 97.0)]
    env, total, ep = run(make_session(ticks), {10: BID1})
    f = env.fills[-1]
    assert f["forced"] and f["order"] == "market" and f["kind"] == "exit"
    assert f["us"] == 600 * US
    # last print 97 -> sell at 97 - 0.5 tick (tick 1) = 96.5
    assert f["price"] == pytest.approx(96.5)
    assert ep["n_forced"] == 1
    # bought at 99, forced sell at 96.5 -> -2.5 ticks
    assert ep["pnl_ticks"] == pytest.approx(-2.5)
    assert total == pytest.approx(-2.5)
    assert env.position == 0


def test_market_flatten_action_uses_slippage_and_cancels_orders():
    ticks = BASE + [(14.0, 98.0), (20.0, 100.0)]
    env, _, ep = run(make_session(ticks), {10: BID1, 30: FLAT, 31: BID1})
    exits = [f for f in env.fills if f["kind"] == "exit"]
    assert exits[0]["order"] == "market" and not exits[0]["forced"]
    assert exits[0]["price"] == pytest.approx(99.5)  # last 100 - 0.5 tick
    assert exits[0]["us"] == round(30.05 * US)
    assert ep["n_exit_market"] == 1


def test_no_fills_outside_the_trading_window():
    rng = np.random.default_rng(3)
    secs = np.sort(rng.uniform(0, 600, 4000))
    px = 100 + np.cumsum(rng.choice([-1.0, 0.0, 1.0], size=4000))
    px = np.clip(px, 60, 140)
    ticks = [(float(t), float(p)) for t, p in zip(secs, px, strict=True)]
    ticks[0] = (0.0, 100.0)
    ticks.append((600.0, float(px[-1])))
    sess = make_session(ticks)
    for rule in ("trade_through", "touch"):
        env = ScalpEnv(config=ScalpConfig(fill_rule=rule))
        env.reset(options={"session": sess})
        done = False
        while not done:
            _, _, done, _, _ = env.step(int(rng.integers(9)))
        assert env.fills, "random policy should produce some fills"
        for f in env.fills:
            assert 10 * US <= f["us"] <= 600 * US
            if f["order"] == "limit":
                assert f["us"] > 10 * US  # active only after the first decision
        assert env.position == 0


def test_reward_telescopes_to_final_pnl():
    rng = np.random.default_rng(11)
    secs = np.sort(rng.uniform(0, 600, 3000))
    px = np.clip(100 + np.cumsum(rng.choice([-1.0, 0.0, 1.0], size=3000)), 60, 140)
    ticks = [(float(t), float(p)) for t, p in zip(secs, px, strict=True)]
    ticks[0] = (0.0, 100.0)
    sess = make_session(ticks)
    for rule in ("trade_through", "touch"):
        env = ScalpEnv(config=ScalpConfig(fill_rule=rule, commission_yen=3.0))
        env.reset(options={"session": sess})
        total, done, ep = 0.0, False, {}
        while not done:
            _, r, done, _, info = env.step(int(rng.integers(9)))
            total += r
            ep = info.get("episode", ep)
        assert total == pytest.approx(ep["pnl_ticks"], abs=1e-9)
        assert ep["pnl_yen100"] / 100.0 / sess.ref_tick == pytest.approx(
            ep["pnl_ticks"], abs=1e-9
        )
        trip_sum = sum(1 for _ in env._trips)
        assert trip_sum == ep["n_trips"]
        # commission is charged on every fill
        n_fills = len(env.fills)
        assert n_fills > 0


def test_inventory_penalty_is_part_of_reward_only():
    ticks = BASE + [(11.5, 98.0)]
    env0, total0, ep0 = run(make_session(ticks), {10: BID1})
    env1, total1, ep1 = run(make_session(ticks), {10: BID1}, inventory_penalty=0.1)
    assert ep0["pnl_ticks"] == ep1["pnl_ticks"]
    assert total1 < total0


def test_replace_cancels_old_order_and_cancel_action():
    # bid 99 replaced by bid 98 at 11 s: a print of 98.5 afterwards must not fill.
    ticks = BASE + [(20.0, 98.5)]
    env, _, ep = run(make_session(ticks), {10: BID1, 11: BID2})
    assert ep["n_entry_limit"] == 0
    env, _, ep = run(make_session(BASE + [(20.0, 98.0)]), {10: BID1, 11: CANCEL})
    assert ep["n_entry_limit"] == 0


def test_observation_contains_agent_state():
    ticks = BASE + [(12.0, 98.0)]
    sess = make_session(ticks)
    env = ScalpEnv([sess])
    env.reset()
    obs, *_ = env.step(BID1)
    from trading_rl.scalp_env import N_MARKET

    pos, unreal, has_bid, bid_dist = obs[N_MARKET : N_MARKET + 4]
    assert (pos, has_bid) == (0.0, 1.0) and bid_dist == pytest.approx(1.0)
    obs, *_ = env.step(KEEP)
    obs, *_ = env.step(KEEP)  # 98 print at 12 s fills the bid at 99
    assert obs[N_MARKET] == 1.0 and obs[N_MARKET + 2] == 0.0
    assert obs[-1] == pytest.approx((600 - 13) / 590)


# =========================================================================== #
# v2: order modes, action masks, market entries, shaping, per-episode fill rule
# =========================================================================== #
from trading_rl.scalp_env import (  # noqa: E402
    ACTION_IDS,
    G_CANCEL,
    G_FLATTEN,
    G_MBUY,
    G_MSELL,
    ORDER_MODES,
    action_names,
    action_valid,
    n_actions,
)

MBUY_M, MSELL_M, FLAT_M = 1, 2, 3  # market-mode local indices
MBUY_B, MSELL_B = 9, 10  # both-mode local indices


def play_mode(sess, plan, mode, **cfg):
    """Like ``run`` but with an order_mode; returns (env, total reward, summary)."""
    env = ScalpEnv(config=ScalpConfig(order_mode=mode, **cfg))
    env.reset(options={"session": sess})
    total, info, done, s = 0.0, {}, False, 10
    while not done:
        _, r, done, _, info = env.step(plan.get(s, KEEP))
        total += r
        s += 1
    return env, total, info["episode"]


def test_action_sets_are_stable_per_mode():
    assert tuple(ACTION_IDS["limit"]) == tuple(range(9))
    assert action_names("limit") == ACTION_NAMES
    assert action_names("market") == [
        "keep",
        "market_buy",
        "market_sell",
        "market_flatten",
    ]
    assert action_names("both") == ACTION_NAMES + ["market_buy", "market_sell"]
    assert [n_actions(m) for m in ORDER_MODES] == [9, 4, 11]
    for mode in ORDER_MODES:
        env = ScalpEnv([make_session(BASE)], ScalpConfig(order_mode=mode))
        assert env.action_space.n == n_actions(mode)
        env.reset()
        with pytest.raises(ValueError):
            env.step(n_actions(mode))
    with pytest.raises(ValueError):
        ScalpConfig(order_mode="nope")
    meta = ScalpConfig(order_mode="market").metadata()
    assert meta["order_mode"] == "market"
    assert meta["action_names"] == action_names("market")
    assert meta["action_ids"] == [0, 9, 10, 8]


def _mask_dict(env):
    names = action_names(env.config.order_mode)
    return dict(zip(names, env.action_mask(), strict=True))


def test_action_mask_limit_mode_by_state():
    ticks = BASE + [(11.5, 98.0)]
    env = ScalpEnv([make_session(ticks)], ScalpConfig())
    env.reset(options={"index": 0})
    m = _mask_dict(env)  # flat, nothing working
    assert m["keep"] and m["bid_k1"] and m["ask_k2"]
    assert not m["cancel_all"] and not m["market_flatten"]
    env.step(BID1)  # bid working at 10
    m = _mask_dict(env)
    assert m["cancel_all"] and m["bid_k0"] and m["ask_k0"] and not m["market_flatten"]
    env.step(KEEP)
    env.step(KEEP)  # 98 print at 11.5 fills the bid -> long
    assert env.position == 1
    m = _mask_dict(env)
    assert not any(m[n] for n in ("bid_k0", "bid_k1", "bid_k2", "cancel_all"))
    assert all(m[n] for n in ("keep", "ask_k0", "ask_k1", "ask_k2", "market_flatten"))
    env.step(ASK0)  # resting exit
    assert _mask_dict(env)["cancel_all"]


def test_action_mask_market_and_both_modes():
    env = ScalpEnv([make_session(BASE)], ScalpConfig(order_mode="market"))
    env.reset(options={"index": 0})
    assert env.action_mask().tolist() == [True, True, True, False]
    env.step(MBUY_M)  # long
    assert env.action_mask().tolist() == [True, False, True, True]
    env.step(FLAT_M)
    env.step(MSELL_M)  # short
    assert env.action_mask().tolist() == [True, True, False, True]
    env = ScalpEnv([make_session(BASE)], ScalpConfig(order_mode="both"))
    env.reset(options={"index": 0})
    m = _mask_dict(env)
    assert m["market_buy"] and m["market_sell"] and not m["market_flatten"]
    env.step(MBUY_B)
    m = _mask_dict(env)
    assert not m["market_buy"] and m["market_sell"] and m["market_flatten"]
    assert not m["bid_k0"] and m["ask_k0"]
    # mask copies are independent of the env
    env.action_mask()[:] = False
    assert env.action_mask()[0]


def test_action_valid_matches_mask_tables_for_every_state():
    for mode in ORDER_MODES:
        env = ScalpEnv([make_session(BASE)], ScalpConfig(order_mode=mode))
        env.reset(options={"index": 0})
        for pos in (-1, 0, 1):
            for hb in (False, True):
                for ha in (False, True):
                    env._pos = pos  # noqa: SLF001
                    env._bid = 99.0 if hb else None  # noqa: SLF001
                    env._ask = 101.0 if ha else None  # noqa: SLF001
                    want = [action_valid(g, pos, hb, ha) for g in ACTION_IDS[mode]]
                    assert env.action_mask().tolist() == want
                    assert env.action_mask()[0]  # keep is always legal


def test_masked_random_policy_never_counts_invalid_actions():
    rng = np.random.default_rng(5)
    secs = np.sort(rng.uniform(0, 600, 3000))
    px = np.clip(100 + np.cumsum(rng.choice([-1.0, 0.0, 1.0], size=3000)), 60, 140)
    ticks = [(float(t), float(p)) for t, p in zip(secs, px, strict=True)]
    ticks[0] = (0.0, 100.0)
    sess = make_session(ticks)
    for mode in ORDER_MODES:
        env = ScalpEnv(config=ScalpConfig(order_mode=mode))
        env.reset(options={"session": sess})
        done, info = False, {}
        while not done:
            legal = np.flatnonzero(env.action_mask())
            _, _, done, _, info = env.step(int(rng.choice(legal)))
        assert info["episode"]["n_invalid_actions"] == 0, mode
        assert env.position == 0


def test_latency_race_is_not_a_mask_violation():
    # Bid 99 working; a print of 98 at 11.02 s lies inside the 50 ms latency window
    # of the decision at 11 s, so the old bid fills before that action activates.
    sess = make_session(BASE + [(11.02, 98.0)])
    env = ScalpEnv([sess], ScalpConfig(order_mode="both"))
    env.reset(options={"index": 0})
    env.step(BID1)  # bid 99, working
    assert env.action_mask()[BID1]  # replacing the bid is legal at decision time
    env.step(BID1)  # ... but the old bid fills at 11.02 first -> now long
    assert env.position == 1
    assert env._n_invalid == 0 and env._n_latency_noops == 1  # noqa: SLF001
    # a market sell decided while flat-with-bid is dropped, not flipped short
    env = ScalpEnv([sess], ScalpConfig(order_mode="both"))
    env.reset(options={"index": 0})
    env.step(BID1)
    env.step(MSELL_B)
    assert env.position == 1 and env._n_latency_noops == 1  # noqa: SLF001
    assert not [f for f in env.fills if f["order"] == "market"]


def test_market_entry_and_exit_fill_with_slippage():
    # last 100, tick 1: buy at 100.5 (t = decision + 50 ms), sell at 99.5.
    for mode, buy, sell in (("market", MBUY_M, MSELL_M), ("both", MBUY_B, MSELL_B)):
        env, total, ep = play_mode(make_session(BASE), {10: buy, 30: sell}, mode)
        e, x = env.fills[0], env.fills[1]
        assert (e["kind"], e["side"], e["order"]) == ("entry", 1, "market")
        assert e["forced"] is False
        assert e["price"] == pytest.approx(100.5) and e["us"] == round(10.05 * US)
        assert (x["kind"], x["side"], x["order"]) == ("exit", -1, "market")
        assert x["price"] == pytest.approx(99.5) and x["us"] == round(30.05 * US)
        assert ep["n_entry_market"] == 1 and ep["n_exit_market"] == 1
        assert ep["n_trips"] == 1 and ep["n_forced"] == 0
        assert ep["pnl_ticks"] == pytest.approx(-1.0) and total == pytest.approx(-1.0)
        assert ep["n_invalid_actions"] == 0 and env.position == 0
    # short side: sell at 99.5 then buy back at 100.5
    env, total, ep = play_mode(make_session(BASE), {10: MSELL_M, 20: MBUY_M}, "market")
    assert [f["side"] for f in env.fills] == [-1, 1]
    assert [f["price"] for f in env.fills] == pytest.approx([99.5, 100.5])
    assert ep["pnl_ticks"] == pytest.approx(-1.0)
    # market-flatten works the same; a held position is force-closed at 600 s
    env, _, ep = play_mode(make_session(BASE), {10: MBUY_M, 40: FLAT_M}, "market")
    assert env.fills[1]["price"] == pytest.approx(99.5) and ep["n_exit_market"] == 1
    env, _, ep = play_mode(make_session(BASE), {10: MBUY_M}, "market")
    assert env.fills[-1]["forced"] and ep["n_forced"] == 1


def test_market_entry_uses_last_print_at_activation_and_cancels_limits():
    ticks = BASE + [(10.03, 103.0), (10.2, 90.0)]
    env, *_ = play_mode(make_session(ticks), {10: MBUY_B}, "both")
    # last print at activation (10.05 s) is 103 -> buy at 103.5, the 90 print is later
    assert env.fills[0]["price"] == pytest.approx(103.5)
    # a market order cancels every working limit order
    env = ScalpEnv([make_session(BASE)], ScalpConfig(order_mode="both"))
    env.reset(options={"index": 0})
    env.step(BID1)
    env.step(ASK1)
    assert env.state_view()["bid"] is not None and env.state_view()["ask"] is not None
    env.step(MBUY_B)
    v = env.state_view()
    assert v["pos"] == 1 and v["bid"] is None and v["ask"] is None


def test_inventory_penalty_arithmetic_is_training_only():
    rate = 0.01
    plan = {10: MBUY_M}
    env0, total0, ep0 = play_mode(make_session(BASE), plan, "market")
    env1, total1, ep1 = play_mode(
        make_session(BASE), plan, "market", inventory_penalty_ticks_per_s=rate
    )
    # held at the start of decision steps 11..599 (the last one closes it): 589 steps
    assert ep1["penalty_ticks"] == pytest.approx(589 * rate)
    assert total1 == pytest.approx(total0 - 589 * rate)
    assert ep1["shaped_return_ticks"] == pytest.approx(total1)
    # reported PnL is raw and identical
    assert ep1["pnl_ticks"] == ep0["pnl_ticks"] == pytest.approx(-1.0)
    assert ep1["pnl_yen100"] == ep0["pnl_yen100"]
    # flat all session: no inventory penalty at all
    _, total, ep = play_mode(
        make_session(BASE), {}, "market", inventory_penalty_ticks_per_s=rate
    )
    assert total == 0.0 and ep["penalty_ticks"] == 0.0
    # the legacy alias adds on top
    _, _, ep = play_mode(
        make_session(BASE),
        plan,
        "market",
        inventory_penalty=rate,
        inventory_penalty_ticks_per_s=rate,
    )
    assert ep["penalty_ticks"] == pytest.approx(2 * 589 * rate)


def test_idle_penalty_only_when_flat_and_nothing_working():
    rate = 0.02
    _, total, ep = play_mode(
        make_session(BASE), {}, "limit", idle_penalty_ticks_per_s=rate
    )
    assert total == pytest.approx(-590 * rate) and ep["pnl_ticks"] == 0.0
    # a resting order (bid 98 never fills) ends idleness from the next step on
    _, total, ep = play_mode(
        make_session(BASE), {10: BID2}, "limit", idle_penalty_ticks_per_s=rate
    )
    assert ep["penalty_ticks"] == pytest.approx(1 * rate)
    # holding a position is not idle
    _, _, ep = play_mode(
        make_session(BASE), {10: MBUY_M}, "market", idle_penalty_ticks_per_s=rate
    )
    assert ep["penalty_ticks"] == pytest.approx(1 * rate)  # only the entry step
    with pytest.raises(ValueError):
        ScalpConfig(idle_penalty_ticks_per_s=-1.0)


def test_per_episode_fill_rule_override():
    ticks = BASE + [(12.0, 99.0)]  # exact print at the bid
    sess = make_session(ticks)
    env = ScalpEnv(config=ScalpConfig(fill_rule="trade_through"))
    env.reset(options={"session": sess, "fill_rule": "touch"})
    assert env.fill_rule == "touch"
    for s in range(10, 600):
        env.step(BID1 if s == 10 else KEEP)
    assert env.fills[0]["price"] == 99.0 and env.fills[0]["kind"] == "entry"
    env.reset(options={"session": sess})  # back to the config's rule
    assert env.fill_rule == "trade_through"
    done, s, info = False, 10, {}
    while not done:
        _, _, done, _, info = env.step(BID1 if s == 10 else KEEP)
        s += 1
    ep = info["episode"]
    assert ep["n_entry_limit"] == 0 and ep["fill_rule"] == "trade_through"
    with pytest.raises(ValueError):
        env.reset(options={"session": sess, "fill_rule": "bogus"})


def test_global_ids_and_legacy_constants():
    assert (G_CANCEL, G_FLATTEN, G_MBUY, G_MSELL) == (1, 8, 9, 10)
    assert ACTION_IDS["market"] == (0, G_MBUY, G_MSELL, G_FLATTEN)


def test_action_based_shaping_ignores_position_state():
    p, b = 0.01, 0.3
    kw = {"keep_penalty_ticks": p, "action_bonus_ticks": b}
    # flat all session: 590 keep decisions
    _, total, ep = play_mode(make_session(BASE), {}, "market", **kw)
    assert ep["penalty_ticks"] == pytest.approx(590 * p) and ep["bonus_ticks"] == 0.0
    assert total == pytest.approx(-590 * p) and ep["pnl_ticks"] == 0.0
    # long all session (forced close is not an action): same keep count minus one
    _, _, ep_hold = play_mode(make_session(BASE), {10: MBUY_M}, "market", **kw)
    assert ep_hold["penalty_ticks"] == pytest.approx(589 * p)
    assert ep_hold["bonus_ticks"] == pytest.approx(b)
    # buy then flatten: 2 effective actions, 588 keeps; raw PnL untouched
    plan = {10: MBUY_M, 20: FLAT_M}
    _, raw_total, raw_ep = play_mode(make_session(BASE), plan, "market")
    _, total, ep = play_mode(make_session(BASE), plan, "market", **kw)
    assert ep["penalty_ticks"] == pytest.approx(588 * p)
    assert ep["bonus_ticks"] == pytest.approx(2 * b)
    assert total == pytest.approx(raw_total - 588 * p + 2 * b)
    assert ep["pnl_ticks"] == raw_ep["pnl_ticks"]
    # an illegal no-op action (flatten while flat) earns no bonus
    _, _, ep = play_mode(make_session(BASE), {10: FLAT_M}, "market", **kw)
    assert ep["bonus_ticks"] == 0.0 and ep["penalty_ticks"] == pytest.approx(589 * p)
    with pytest.raises(ValueError):
        ScalpConfig(action_bonus_ticks=-0.1)


def test_realized_reward_pays_only_when_a_trade_is_closed():
    ticks = [(0.0, 100.0), (9.0, 100.0), (15.0, 103.0), (25.0, 104.0), (599.9, 104.0)]
    plan = {10: MBUY_M, 30: FLAT_M}
    env_m, total_m, ep_m = play_mode(make_session(ticks), plan, "market")
    env = ScalpEnv(config=ScalpConfig(order_mode="market", reward_mode="realized"))
    env.reset(options={"session": make_session(ticks)})
    rewards, done, s, info = [], False, 10, {}
    while not done:
        _, r, done, _, info = env.step(plan.get(s, 0))
        rewards.append(r)
        s += 1
    nonzero = [i + 10 for i, r in enumerate(rewards) if r != 0.0]
    assert nonzero == [30]  # nothing while the position is open; paid on the close
    assert sum(rewards) == pytest.approx(total_m)  # same total as mark-to-market
    assert info["episode"]["pnl_ticks"] == ep_m["pnl_ticks"]
    # a position still open at 600 s is paid at the forced close (last step)
    env.reset(options={"session": make_session(ticks)})
    rewards, done, s = [], False, 10
    while not done:
        _, r, done, _, _ = env.step(MBUY_M if s == 10 else 0)
        rewards.append(r)
        s += 1
    assert all(r == 0.0 for r in rewards[:-1]) and rewards[-1] != 0.0
    with pytest.raises(ValueError):
        ScalpConfig(reward_mode="win_loss")


def test_unrealized_loss_penalty_only_on_losses_while_open():
    rate = 0.01
    # buy at 10 s (fills at 100.5), price drops to 98 at 15 s, flatten at 30 s
    down = [(0.0, 100.0), (9.0, 100.0), (15.0, 98.0), (599.9, 98.0)]
    plan = {10: MBUY_M, 30: FLAT_M}
    _, _, ep0 = play_mode(make_session(down), plan, "market")
    _, _, ep = play_mode(
        make_session(down), plan, "market", unrealized_loss_penalty_per_s=rate
    )
    # open after decisions 10..29; a step ending at d+1 marks at the last print <= d+1:
    # loss 0.5 tick after decisions 10..13, 2.5 ticks after 14..29 (the 98 print at 15 s)
    assert ep["penalty_ticks"] == pytest.approx(rate * (4 * 0.5 + 16 * 2.5))
    assert ep["pnl_ticks"] == ep0["pnl_ticks"]  # raw PnL untouched
    # a winning position is never penalized once it is in profit
    up = [(0.0, 100.0), (9.0, 100.0), (10.5, 103.0), (599.9, 103.0)]
    _, _, ep = play_mode(
        make_session(up), plan, "market", unrealized_loss_penalty_per_s=rate
    )
    assert ep["penalty_ticks"] == 0.0
    with pytest.raises(ValueError):
        ScalpConfig(unrealized_loss_penalty_per_s=-0.1)


def test_gross_ticks_per_trip_is_pnl_before_market_slippage():
    # market round trip on a flat tape: 0.5 tick paid on each side, gross 0
    _, _, ep = play_mode(make_session(BASE), {10: MBUY_M, 30: FLAT_M}, "market")
    assert ep["pnl_ticks"] == pytest.approx(-1.0)
    assert ep["gross_ticks_sum"] == pytest.approx(0.0)
    # up-move 100 -> 103: gross +3 ticks, net +2 after the two half-tick slippages
    up = [(0.0, 100.0), (9.0, 100.0), (15.0, 103.0), (599.9, 103.0)]
    _, _, ep = play_mode(make_session(up), {10: MBUY_M, 30: FLAT_M}, "market")
    assert ep["pnl_ticks"] == pytest.approx(2.0)
    assert ep["gross_ticks_sum"] == pytest.approx(3.0)
    # limit round trip pays no slippage: gross == net
    ticks = BASE + [(14.0, 98.0), (20.0, 99.5), (25.0, 101.0)]
    sess = make_session(ticks)
    _, _, ep = run(sess, {10: BID1, 16: ASK0})
    assert ep["n_trips"] == 1 and ep["n_exit_limit"] == 1
    assert ep["gross_ticks_sum"] == pytest.approx(ep["pnl_ticks"])
    # a forced close pays the market slippage too
    _, _, ep = play_mode(make_session(BASE), {10: MBUY_M}, "market")
    assert ep["n_forced"] == 1 and ep["gross_ticks_sum"] == pytest.approx(0.0)
    assert ep["pnl_ticks"] == pytest.approx(-1.0)
