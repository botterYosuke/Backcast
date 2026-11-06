"""Gymnasium trading environment for the post-open 15-minute window.

Implements single-net position day trading with market orders, slippage,
and mandatory square-off at the 15-minute mark.
"""

from __future__ import annotations

from enum import IntEnum
from typing import Any

import gymnasium as gym
import numpy as np
from gymnasium import spaces

from .data import OPENING_WINDOW_US, OpeningSessionData
from .features import extract_features


class Action(IntEnum):
    HOLD = 0
    BUY = 1
    SELL = 2


class TickTradingEnv(gym.Env):
    """Gymnasium environment for tick-by-tick trading in the post-open 15-minute window.

    Action Space:
        Discrete(3):
            0: HOLD (maintain position)
            1: BUY  (if flat: open Long; if short: cover to flat [or flip if allow_flip]; if long: no-op)
            2: SELL (if flat: open Short; if long: sell to flat [or flip if allow_flip]; if short: no-op)

    Observation Space:
        Box(shape=(5 + K,), dtype=float32):
            [0]: Price return from open: (P_t - P_open) / P_open
            [1]: Normalized time progress: tau = (t - T_open) / 900s in [0, 1]
            [2]: Current net position: -1.0 (Short), 0.0 (Flat), +1.0 (Long)
            [3]: Unrealized PnL return: (P_t - avg_price) / avg_price * position (0 if flat)
            [4]: Normalized time held: holding_steps / total_episode_steps (0 if flat)
            [5:]: Optional extra feature vector (length K)
    """

    metadata = {"render_modes": []}

    def __init__(
        self,
        session_data: OpeningSessionData | list[OpeningSessionData],
        *,
        slippage: float = 0.0,
        trade_penalty: float = 0.0,
        forced_square_off_penalty: float = 0.0,
        allow_flip: bool = False,
        extra_features: np.ndarray | list[np.ndarray] | None = None,
        auto_features: bool = False,
    ) -> None:
        super().__init__()

        if isinstance(session_data, list):
            if not session_data:
                raise ValueError("session_data list cannot be empty")
            self._sessions = session_data
        else:
            self._sessions = [session_data]

        self.slippage = float(slippage)
        self.trade_penalty = float(trade_penalty)
        self.forced_square_off_penalty = float(forced_square_off_penalty)
        self.allow_flip = bool(allow_flip)

        # Handle extra features aligned with sessions
        self._extra_features_list: list[np.ndarray | None]
        if extra_features is not None:
            if isinstance(extra_features, list):
                self._extra_features_list = extra_features
                self._feature_dim = (
                    extra_features[0].shape[1] if extra_features[0] is not None else 0
                )
            else:
                self._extra_features_list = [extra_features]
                self._feature_dim = extra_features.shape[1]
        elif auto_features:
            self._extra_features_list = [extract_features(s) for s in self._sessions]
            self._feature_dim = (
                self._extra_features_list[0].shape[1]
                if self._extra_features_list
                else 0
            )
        else:
            self._extra_features_list = [None] * len(self._sessions)
            self._feature_dim = 0

        obs_dim = 5 + self._feature_dim
        self.observation_space = spaces.Box(
            low=-np.inf,
            high=np.inf,
            shape=(obs_dim,),
            dtype=np.float32,
        )
        self.action_space = spaces.Discrete(3)

        # Active session state
        self._session_idx = 0
        self._current_session: OpeningSessionData = self._sessions[0]
        self._current_extra: np.ndarray | None = self._extra_features_list[0]

        # Episode tracking variables
        self._step_idx = 0
        self._position = 0  # -1, 0, +1
        self._avg_price = 0.0
        self._realized_pnl = 0.0
        self._holding_steps = 0
        self._trades_count = 0
        self._prev_total_pnl = 0.0

    @property
    def current_session(self) -> OpeningSessionData:
        return self._current_session

    def set_session(
        self,
        session_data: OpeningSessionData,
        extra_features: np.ndarray | None = None,
        auto_features: bool = False,
    ) -> None:
        """Set a specific session as active."""
        self._sessions = [session_data]
        self._session_idx = 0
        self._current_session = session_data
        if extra_features is None and auto_features:
            extra_features = extract_features(session_data)
        self._current_extra = extra_features
        self._extra_features_list = [extra_features]
        if extra_features is not None:
            self._feature_dim = extra_features.shape[1]
            self.observation_space = spaces.Box(
                low=-np.inf,
                high=np.inf,
                shape=(5 + self._feature_dim,),
                dtype=np.float32,
            )

    def _unrealized_pnl(self, current_price: float) -> float:
        if self._position == 0 or self._avg_price <= 0:
            return 0.0
        return (current_price - self._avg_price) * float(self._position)

    def _total_pnl(self, current_price: float) -> float:
        return self._realized_pnl + self._unrealized_pnl(current_price)

    def _get_obs(self) -> np.ndarray:
        price = float(self._current_session.price[self._step_idx])
        epoch_us = int(self._current_session.epoch_us[self._step_idx])
        p_open = self._current_session.p_open
        t_open_us = self._current_session.t_open_us
        total_steps = len(self._current_session.price)

        # 1. Price return from open
        price_return = (price - p_open) / p_open if p_open > 0 else 0.0

        # 2. Normalized time progress tau in [0, 1]
        tau = min(1.0, max(0.0, float(epoch_us - t_open_us) / OPENING_WINDOW_US))

        # 3. Position status: -1.0, 0.0, +1.0
        pos_status = float(self._position)

        # 4. Unrealized PnL return since entry
        if self._position != 0 and self._avg_price > 0:
            unrealized_return = (
                (price - self._avg_price) / self._avg_price
            ) * pos_status
        else:
            unrealized_return = 0.0

        # 5. Normalized holding time
        time_held = (
            float(self._holding_steps / max(1, total_steps))
            if self._position != 0
            else 0.0
        )

        base_features = [
            np.float32(price_return),
            np.float32(tau),
            np.float32(pos_status),
            np.float32(unrealized_return),
            np.float32(time_held),
        ]

        if self._current_extra is not None and self._feature_dim > 0:
            extra = self._current_extra[self._step_idx].astype(np.float32)
            return np.concatenate([np.array(base_features, dtype=np.float32), extra])

        return np.array(base_features, dtype=np.float32)

    def reset(
        self,
        *,
        seed: int | None = None,
        options: dict[str, Any] | None = None,
    ) -> tuple[np.ndarray, dict[str, Any]]:
        super().reset(seed=seed)

        # Select session if multiple are present
        if len(self._sessions) > 1:
            self._session_idx = int(self.np_random.integers(0, len(self._sessions)))
        else:
            self._session_idx = 0

        self._current_session = self._sessions[self._session_idx]
        self._current_extra = self._extra_features_list[self._session_idx]

        # Reset episode state
        self._step_idx = 0
        self._position = 0
        self._avg_price = 0.0
        self._realized_pnl = 0.0
        self._holding_steps = 0
        self._trades_count = 0
        self._prev_total_pnl = 0.0

        obs = self._get_obs()
        info = {
            "step": 0,
            "epoch_us": int(self._current_session.epoch_us[0]),
            "price": float(self._current_session.price[0]),
            "position": 0,
            "avg_price": 0.0,
            "realized_pnl": 0.0,
            "unrealized_pnl": 0.0,
            "total_pnl": 0.0,
            "trades_count": 0,
            "forced_square_off": False,
        }
        return obs, info

    def step(
        self,
        action: int | Action,
    ) -> tuple[np.ndarray, float, bool, bool, dict[str, Any]]:
        action_val = int(action)
        if not self.action_space.contains(action_val):
            raise ValueError(f"Invalid action {action_val} for {self.action_space}")

        total_ticks = len(self._current_session.price)
        current_price = float(self._current_session.price[self._step_idx])

        prev_trades = self._trades_count
        forced_square_off = False

        # 1. Execute agent action at current tick price
        if action_val == Action.BUY:
            if self._position == 0:
                # Open 1 Long position
                exec_price = current_price + self.slippage
                self._position = 1
                self._avg_price = exec_price
                self._holding_steps = 0
                self._trades_count += 1
            elif self._position == -1:
                # Covering short
                exec_price = current_price + self.slippage
                # Short realized PnL = (avg_price - exec_price) * 1
                pnl = (self._avg_price - exec_price) * 1.0
                self._realized_pnl += pnl

                if self.allow_flip:
                    # Reverse position directly to Long (+1)
                    self._position = 1
                    self._avg_price = exec_price
                    self._holding_steps = 0
                    self._trades_count += 2
                else:
                    # Return to Flat (0)
                    self._position = 0
                    self._avg_price = 0.0
                    self._holding_steps = 0
                    self._trades_count += 1
            # If already Long (+1), Action.BUY is a no-op

        elif action_val == Action.SELL:
            if self._position == 0:
                # Open 1 Short position
                exec_price = current_price - self.slippage
                self._position = -1
                self._avg_price = exec_price
                self._holding_steps = 0
                self._trades_count += 1
            elif self._position == 1:
                # Closing long
                exec_price = current_price - self.slippage
                # Long realized PnL = (exec_price - avg_price) * 1
                pnl = (exec_price - self._avg_price) * 1.0
                self._realized_pnl += pnl

                if self.allow_flip:
                    # Reverse position directly to Short (-1)
                    self._position = -1
                    self._avg_price = exec_price
                    self._holding_steps = 0
                    self._trades_count += 2
                else:
                    # Return to Flat (0)
                    self._position = 0
                    self._avg_price = 0.0
                    self._holding_steps = 0
                    self._trades_count += 1
            # If already Short (-1), Action.SELL is a no-op

        # 2. Advance time index to next tick (or check if already at end)
        if self._step_idx >= total_ticks - 1:
            terminated = True
            is_last_tick = True
        else:
            self._step_idx += 1
            is_last_tick = self._step_idx >= total_ticks - 1
            terminated = is_last_tick

        next_price = float(self._current_session.price[self._step_idx])

        # 3. Update holding steps
        if self._position != 0:
            self._holding_steps += 1
        else:
            self._holding_steps = 0

        # 4. Mandatory square-off at the final tick of the 15-minute window
        if is_last_tick and self._position != 0:
            forced_square_off = True
            if self._position == 1:
                # Liquidate Long at market - slippage
                exec_price = next_price - self.slippage
                pnl = (exec_price - self._avg_price) * 1.0
                self._realized_pnl += pnl
            else:
                # Liquidate Short at market + slippage
                exec_price = next_price + self.slippage
                pnl = (self._avg_price - exec_price) * 1.0
                self._realized_pnl += pnl

            self._position = 0
            self._avg_price = 0.0
            self._holding_steps = 0
            self._trades_count += 1

        # 5. Calculate transition PnL and reward shaping
        current_total_pnl = self._total_pnl(next_price)
        step_pnl = current_total_pnl - self._prev_total_pnl
        self._prev_total_pnl = current_total_pnl

        trade_cost = self.trade_penalty if (self._trades_count > prev_trades) else 0.0
        forced_cost = self.forced_square_off_penalty if forced_square_off else 0.0
        reward = float(step_pnl - trade_cost - forced_cost)

        truncated = False
        obs = self._get_obs()
        info = {
            "step": self._step_idx,
            "epoch_us": int(self._current_session.epoch_us[self._step_idx]),
            "price": float(next_price),
            "position": int(self._position),
            "avg_price": float(self._avg_price),
            "realized_pnl": float(self._realized_pnl),
            "unrealized_pnl": float(self._unrealized_pnl(next_price)),
            "total_pnl": float(current_total_pnl),
            "trades_count": int(self._trades_count),
            "forced_square_off": bool(forced_square_off),
        }

        return obs, reward, terminated, truncated, info
