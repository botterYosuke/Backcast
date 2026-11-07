"""Unit tests for the post-open 15-minute Gymnasium trading environment (trading_rl.env)."""

from __future__ import annotations

import numpy as np
import pytest
from gymnasium.utils.env_checker import check_env

from trading_rl.data import OpeningSessionData
from trading_rl.env import Action, TickTradingEnv


def make_sample_session(
    prices: list[float],
    *,
    stem: str = "7203",
    code: str = "72030",
    date: str = "2026-08-14",
    t_open_us: int = 1786698000000000,
    interval_s: int = 60,
    start_offset_s: int = 20,
) -> OpeningSessionData:
    """Helper to synthesize a test OpeningSessionData with predictable prices."""
    n = len(prices)
    epoch_us = np.array(
        [t_open_us + (start_offset_s + i * interval_s) * 1_000_000 for i in range(n)], dtype=np.int64
    )
    price_arr = np.array(prices, dtype=np.float64)
    qty_arr = np.ones(n, dtype=np.int64) * 100
    trade_type_arr = np.array(["1"] * n, dtype=object)

    return OpeningSessionData(
        stem=stem,
        code=code,
        date=date,
        t_open_us=t_open_us,
        p_open=float(prices[0]),
        v_open=1000,
        epoch_us=epoch_us,
        price=price_arr,
        qty=qty_arr,
        trade_type=trade_type_arr,
    )


def test_gymnasium_api_conformance():
    """Verify environment conforms to Gymnasium specifications via check_env."""
    session = make_sample_session([100.0, 101.0, 102.0, 103.0, 104.0])
    env = TickTradingEnv(session)

    # Built-in Gymnasium test suite
    check_env(env)

    obs, info = env.reset()
    assert isinstance(obs, np.ndarray)
    assert obs.shape == (5,)
    assert obs.dtype == np.float32
    assert env.observation_space.contains(obs)
    assert info["position"] == 0
    assert info["price"] == 100.0


def test_long_entry_exit_and_realized_pnl():
    """Test entering Long, holding, and closing with profit (matching paper-trading.mjs)."""
    # Step 0: BUY @ 100 -> next tick 105: unrealized +5, reward +5
    # Step 1: SELL @ 105 -> close Long (+5 realized), next tick 110: flat, reward 0
    # Step 2: HOLD @ 110 -> next tick 108: flat, terminated
    session = make_sample_session([100.0, 105.0, 110.0, 108.0])
    env = TickTradingEnv(session)
    env.reset()

    # Step 0: BUY at 100.0, advances to tick 1 (105.0)
    obs, reward, terminated, truncated, info = env.step(Action.BUY)
    assert info["position"] == 1
    assert info["avg_price"] == 100.0
    assert info["price"] == 105.0
    assert info["realized_pnl"] == 0.0
    assert info["unrealized_pnl"] == pytest.approx(5.0)
    assert reward == pytest.approx(5.0)
    assert not terminated
    # Observation reflects state at tick 1 (price 105.0)
    assert obs[0] == pytest.approx(0.05)  # (105 - 100)/100
    assert obs[2] == 1.0  # Long
    assert obs[3] == pytest.approx(0.05)  # unrealized return

    # Step 1: SELL at 105.0 (closes Long to flat), advances to tick 2 (110.0)
    obs, reward, terminated, truncated, info = env.step(Action.SELL)
    assert info["position"] == 0
    assert info["realized_pnl"] == pytest.approx(5.0)
    assert info["unrealized_pnl"] == 0.0
    assert reward == pytest.approx(0.0)
    assert obs[2] == 0.0
    assert not terminated

    # Step 2: HOLD at 110.0, advances to final tick 3 (108.0)
    obs, reward, terminated, truncated, info = env.step(Action.HOLD)
    assert terminated
    assert info["realized_pnl"] == pytest.approx(5.0)
    assert info["total_pnl"] == pytest.approx(5.0)
    assert not info["forced_square_off"]


def test_short_entry_exit_and_realized_pnl():
    """Test entering Short, holding, and closing with profit on falling price."""
    # Step 0: SELL @ 100 -> next tick 95: unrealized +5, reward +5
    # Step 1: BUY @ 95 -> close Short (+5 realized), next tick 90: flat, reward 0
    # Step 2: HOLD @ 90 -> next tick 92: flat, terminated
    session = make_sample_session([100.0, 95.0, 90.0, 92.0])
    env = TickTradingEnv(session)
    env.reset()

    # Step 0: SELL at 100.0, advances to tick 1 (95.0)
    obs, reward, terminated, truncated, info = env.step(Action.SELL)
    assert info["position"] == -1
    assert info["avg_price"] == 100.0
    assert info["price"] == 95.0
    assert info["unrealized_pnl"] == pytest.approx(5.0)
    assert reward == pytest.approx(5.0)
    assert obs[2] == -1.0
    assert obs[3] == pytest.approx(0.05)  # (95 - 100)/100 * (-1) = +0.05

    # Step 1: BUY at 95.0 (cover Short to flat), advances to tick 2 (90.0)
    obs, reward, terminated, truncated, info = env.step(Action.BUY)
    assert info["position"] == 0
    assert info["realized_pnl"] == pytest.approx(5.0)
    assert info["unrealized_pnl"] == 0.0
    assert not terminated

    # Step 2: HOLD at 90.0, advances to final tick 3 (92.0)
    obs, reward, terminated, truncated, info = env.step(Action.HOLD)
    assert terminated
    assert info["total_pnl"] == pytest.approx(5.0)


def test_reversing_position_two_step():
    """Reversing position in 2 steps with allow_flip=False (Short -> Flat -> Long)."""
    # Step 0: 100 (SELL) -> Short @ 100, next tick 95
    # Step 1: 95 (BUY) -> Flat @ 95, Realized +5, next tick 96
    # Step 2: 96 (BUY) -> Long @ 96, next tick 105 (final tick -> square-off: +9 realized)
    session = make_sample_session([100.0, 95.0, 96.0, 105.0])
    env = TickTradingEnv(session, allow_flip=False)
    env.reset()

    env.step(Action.SELL)  # Short @ 100
    _, _, _, _, info1 = env.step(Action.BUY)  # Cover Short -> Flat (realized +5)
    assert info1["position"] == 0
    assert info1["realized_pnl"] == pytest.approx(5.0)

    _, _, terminated, _, info2 = env.step(
        Action.BUY
    )  # Open Long @ 96, reaches final tick 105 -> forced square-off
    assert terminated
    assert info2["position"] == 0
    assert info2["forced_square_off"]
    # Total realized = +5 (short) + 9 (long 96 to 105) = +14
    assert info2["realized_pnl"] == pytest.approx(14.0)


def test_reversing_position_direct_flip():
    """Reversing position directly in 1 step with allow_flip=True (Long -> Short)."""
    # Step 0: 100 (BUY) -> Long @ 100, next tick 105
    # Step 1: 105 (SELL with flip) -> Closes Long (+5), opens Short @ 105, next tick 95
    # Step 2: 95 (BUY with flip) -> Closes Short (+10), opens Long @ 95, next tick 98 (final tick -> square-off: +3)
    session = make_sample_session([100.0, 105.0, 95.0, 98.0])
    env = TickTradingEnv(session, allow_flip=True)
    env.reset()

    env.step(Action.BUY)  # Long @ 100
    _, _, _, _, info1 = env.step(Action.SELL)  # Flip directly to Short @ 105
    assert info1["position"] == -1
    assert info1["avg_price"] == 105.0
    assert info1["realized_pnl"] == pytest.approx(5.0)

    _, _, terminated, _, info2 = env.step(
        Action.BUY
    )  # Flip directly to Long @ 95, reaches final tick 98 -> square-off (+3)
    assert terminated
    assert info2["forced_square_off"]
    assert info2["position"] == 0
    # +5 (long 100->105) + 10 (short 105->95) + 3 (long 95->98) = +18
    assert info2["realized_pnl"] == pytest.approx(18.0)


def test_forced_square_off_at_15m_with_penalty():
    """Test mandatory square-off at the final tick (tau=1.0) with penalty deduction."""
    # Step 0: 100 (BUY) -> Long @ 100, advances to tick 1 (102.0)
    # Step 1: 102 (HOLD) -> advances to tick 2 (106.0, final tick) -> forced liquidation of Long @ 106
    session = make_sample_session([100.0, 102.0, 106.0])
    env = TickTradingEnv(session, forced_square_off_penalty=2.0)
    env.reset()

    _, r0, _, _, _ = env.step(Action.BUY)
    assert r0 == pytest.approx(2.0)  # PnL goes from 0 to 2

    obs, r1, terminated, truncated, info = env.step(Action.HOLD)
    assert terminated
    assert info["forced_square_off"]
    assert info["position"] == 0  # Position liquidated
    assert info["realized_pnl"] == pytest.approx(6.0)  # (106 - 100)
    assert info["total_pnl"] == pytest.approx(6.0)
    # Step reward: PnL delta (+4.0) minus penalty (-2.0) = +2.0
    assert r1 == pytest.approx(2.0)


def test_slippage_penalty():
    """Verify market fill incorporates slippage penalty against the trader."""
    # Buy with slippage 0.5 at 100.0 -> fill at 100.5, advances to final tick 110.0 -> square-off at 110.0 - 0.5 = 109.5
    # Net PnL = 109.5 - 100.5 = 9.0 (vs 10.0 without slippage)
    session = make_sample_session([100.0, 110.0])
    env = TickTradingEnv(session, slippage=0.5)
    env.reset()

    _, _, terminated, _, info0 = env.step(Action.BUY)
    assert terminated
    assert info0["realized_pnl"] == pytest.approx(9.0)


def test_no_op_when_already_in_position():
    """Action BUY when already Long or SELL when already Short is a no-op."""
    session = make_sample_session([100.0, 102.0, 103.0, 104.0])
    env = TickTradingEnv(session)
    env.reset()

    env.step(Action.BUY)  # Enters Long
    _, _, _, _, info1 = env.step(Action.BUY)  # No-op
    assert info1["position"] == 1
    assert info1["trades_count"] == 1  # Not incremented


def test_telescopic_reward_sum_equals_final_pnl():
    """Sum of step rewards without penalties equals final realized PnL."""
    # Step 0: BUY @ 100, next 102
    # Step 1: HOLD @ 102, next 105
    # Step 2: SELL @ 105 (flat, realized +5), next 101
    # Step 3: SELL @ 101 (short @ 101), next 108 (final tick -> square-off @ 108: loss -7)
    session = make_sample_session([100.0, 102.0, 105.0, 101.0, 108.0])
    env = TickTradingEnv(session)
    env.reset()

    total_rewards = 0.0
    _, r0, _, _, _ = env.step(Action.BUY)
    total_rewards += r0

    _, r1, _, _, _ = env.step(Action.HOLD)
    total_rewards += r1

    _, r2, _, _, _ = env.step(Action.SELL)
    total_rewards += r2

    _, r3, terminated, _, info3 = env.step(Action.SELL)
    total_rewards += r3

    assert terminated
    # Net PnL = +5 (long) - 7 (short) = -2.0
    assert info3["realized_pnl"] == pytest.approx(-2.0)
    assert total_rewards == pytest.approx(-2.0)


def _make_warmup_session(
    offsets_s: list[float],
    prices: list[float],
    *,
    t_open_us: int = 1_700_000_000_000_000,
) -> OpeningSessionData:
    epoch_us = np.array(
        [t_open_us + int(s * 1_000_000) for s in offsets_s], dtype=np.int64
    )
    return OpeningSessionData(
        stem="7203",
        code="72030",
        date="2026-08-19",
        t_open_us=t_open_us,
        p_open=float(prices[0]),
        v_open=1000,
        epoch_us=epoch_us,
        price=np.array(prices, dtype=np.float64),
        qty=np.array([100] * len(prices), dtype=np.int64),
        trade_type=np.array(["1"] * len(prices), dtype=object),
    )


def test_warmup_period_prohibits_orders_in_first_15_seconds():
    """reset() starts the episode at the first tick >= T_open + 15s (no action substitution)."""
    session = _make_warmup_session(
        [0, 5, 14, 16, 30, 45], [100.0, 101.0, 102.0, 103.0, 104.0, 105.0]
    )
    env = TickTradingEnv(session, opening_warmup_seconds=15.0)
    _obs, info = env.reset()

    # The warmup ticks (0s, 5s, 14s) are skipped by reset itself
    assert info["step"] == 3
    assert info["epoch_us"] == session.t_open_us + 16_000_000
    assert info["price"] == 103.0

    # First BUY is executed immediately at the 16s tick price
    _obs, _reward, _term, _trunc, info3 = env.step(Action.BUY)
    assert info3["position"] == 1, "Order at 16s post-open should be executed"
    assert info3["avg_price"] == 103.0
    assert info3["fills"][0]["us"] == session.t_open_us + 16_000_000


def test_warmup_in_step_masking_safety_net():
    """Even if the cursor is forced into the warmup, in-step masking blocks orders."""
    session = _make_warmup_session(
        [0, 5, 14, 16, 30, 45], [100.0, 101.0, 102.0, 103.0, 104.0, 105.0]
    )
    env = TickTradingEnv(session, opening_warmup_seconds=15.0)
    env.reset()
    env._step_idx = 0  # force the cursor back to the opening match

    # Step 0 (0s = initial opening match): Action.BUY should be masked to HOLD
    _obs, _reward, _term, _trunc, info0 = env.step(Action.BUY)
    assert info0["position"] == 0, "Order at opening match must be prohibited"
    assert info0["fills"] == []

    # Step 1 (5s post-open): Action.BUY should be masked to HOLD
    _obs, _reward, _term, _trunc, info1 = env.step(Action.BUY)
    assert info1["position"] == 0, "Order at 5s post-open must be prohibited"

    # Step 2 (14s post-open): Action.BUY should be masked to HOLD
    _obs, _reward, _term, _trunc, info2 = env.step(Action.BUY)
    assert info2["position"] == 0, "Order at 14s post-open must be prohibited"

    # Step 3 (16s post-open >= 15s): Action.BUY is permitted
    _obs, _reward, _term, _trunc, info3 = env.step(Action.BUY)
    assert info3["position"] == 1
    assert info3["avg_price"] == 103.0


def test_no_fill_can_precede_warmup_end():
    """Random actions over many episodes never produce a fill outside [15s, window]."""
    offsets = [0, 3, 7, 12, 14.9, 15, 15.1, 20, 40, 80, 160, 300, 500, 599, 600, 650]
    prices = [100.0 + (i % 5) for i in range(len(offsets))]
    session = _make_warmup_session(offsets, prices)
    env = TickTradingEnv(
        session, opening_warmup_seconds=15.0, window_seconds=600.0, allow_flip=True
    )
    rng = np.random.default_rng(0)
    n_fills = 0
    for _ in range(200):
        env.reset()
        done = False
        while not done:
            _o, _r, term, trunc, info = env.step(int(rng.integers(0, 3)))
            done = term or trunc
            for fill in info["fills"]:
                n_fills += 1
                assert fill["us"] >= session.t_open_us + 15_000_000
                assert fill["us"] <= session.t_open_us + 600_000_000
    assert n_fills > 0


def test_reset_with_no_tradable_tick_terminates_without_trade():
    """If every tick is inside the warmup, the episode starts at the last tick and ends flat."""
    session = _make_warmup_session([0, 5, 10], [100.0, 101.0, 102.0])
    env = TickTradingEnv(session, opening_warmup_seconds=15.0)
    _obs, info = env.reset()
    assert info["step"] == 2
    _obs, reward, terminated, _trunc, info = env.step(Action.BUY)
    assert terminated
    assert reward == 0.0
    assert info["fills"] == []
    assert info["trades_count"] == 0
    assert info["position"] == 0


def test_single_tradable_tick_cannot_open_and_instantly_close():
    """A session whose only tradable tick is the last one yields no trade."""
    session = _make_warmup_session([0, 5, 16], [100.0, 101.0, 102.0])
    env = TickTradingEnv(session, slippage=0.5)
    env.reset()
    _obs, _reward, terminated, _trunc, info = env.step(Action.BUY)
    assert terminated
    assert info["trades_count"] == 0
    assert info["fills"] == []


def test_env_truncates_session_to_window():
    """Ticks beyond T_open + window are dropped; the last window tick force-closes."""
    # Ticks at 20s and 80s kept, 140s+ dropped for a 100s window
    session = make_sample_session([100.0, 101.0, 102.0, 103.0, 104.0], interval_s=60)
    env = TickTradingEnv(session, window_seconds=100.0)
    assert len(env.current_session.price) == 2
    env.reset()
    _obs, _reward, terminated, _trunc, info = env.step(Action.BUY)
    assert terminated
    assert info["forced_square_off"]
    assert info["price"] == 101.0
    assert info["realized_pnl"] == pytest.approx(1.0)
    exit_fill = info["fills"][-1]
    assert exit_fill["forced"] is True
    assert exit_fill["us"] <= session.t_open_us + 100_000_000


def test_window_boundary_tick_is_inclusive():
    """A tick exactly at T_open + window is the (inclusive) last tick."""
    session = _make_warmup_session([16, 100, 600, 601], [100.0, 101.0, 102.0, 103.0])
    env = TickTradingEnv(session, window_seconds=600.0)
    assert len(env.current_session.price) == 3
    assert int(env.current_session.epoch_us[-1]) == session.t_open_us + 600_000_000


def test_tau_uses_configured_window():
    session = _make_warmup_session([20, 300, 600], [100.0, 101.0, 102.0])
    env = TickTradingEnv(session, window_seconds=600.0)
    obs, _ = env.reset()
    assert obs[1] == pytest.approx(20 / 600, rel=1e-5)
    obs, _r, _t, _tr, _i = env.step(Action.HOLD)
    assert obs[1] == pytest.approx(0.5)


def test_auto_features_use_env_window():
    from trading_rl.features import FeatureConfig, extract_features

    session = _make_warmup_session([20, 300, 600], [100.0, 101.0, 102.0])
    env = TickTradingEnv(session, window_seconds=600.0, auto_features=True)
    expected = extract_features(session, FeatureConfig(window_seconds=600.0))
    obs, _ = env.reset()
    np.testing.assert_allclose(obs[5:], expected[0], rtol=1e-5)
    # tau feature (first extra feature) is relative to the 600s window
    assert obs[5] == pytest.approx(20 / 600, rel=1e-5)


def test_fills_info_entries_exits_and_flip():
    """info['fills'] is the single source of truth for executions."""
    session = make_sample_session([100.0, 105.0, 95.0, 98.0])
    env = TickTradingEnv(session, allow_flip=True, slippage=0.5)
    env.reset()

    _o, _r, _t, _tr, info0 = env.step(Action.BUY)
    assert [f["kind"] for f in info0["fills"]] == ["entry"]
    assert info0["fills"][0]["side"] == "buy"
    assert info0["fills"][0]["price"] == pytest.approx(100.5)
    assert info0["fills"][0]["pnl"] == 0.0
    assert info0["fills"][0]["forced"] is False

    # Flip: sell long (exit) + open short (entry) in the same step
    _o, _r, _t, _tr, info1 = env.step(Action.SELL)
    assert [(f["kind"], f["side"]) for f in info1["fills"]] == [
        ("exit", "sell"),
        ("entry", "sell"),
    ]
    assert info1["fills"][0]["pnl"] == pytest.approx((105.0 - 0.5) - 100.5)
    assert info1["fills"][0]["us"] == info1["fills"][1]["us"]

    # Hold into the final tick: forced buy-to-cover
    _o, _r, terminated, _tr, info2 = env.step(Action.HOLD)
    assert terminated
    assert info2["fills"][-1]["forced"] is True
    assert info2["fills"][-1]["kind"] == "exit"
    assert info2["fills"][-1]["side"] == "buy"
    total = sum(f["pnl"] for f in info0["fills"] + info1["fills"] + info2["fills"])
    assert total == pytest.approx(info2["realized_pnl"])


def test_flat_observations_cover_tradable_ticks_only():
    session = _make_warmup_session([0, 5, 16, 30], [100.0, 101.0, 102.0, 103.0])
    env = TickTradingEnv(session, window_seconds=600.0)
    flat = env.flat_observations(0)
    assert flat.shape == (2, 5)
    assert flat[0, 0] == pytest.approx(0.02)
    assert (flat[:, 2:] == 0).all()
