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
) -> OpeningSessionData:
    """Helper to synthesize a test OpeningSessionData with predictable prices."""
    n = len(prices)
    epoch_us = np.array(
        [t_open_us + i * interval_s * 1_000_000 for i in range(n)], dtype=np.int64
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
