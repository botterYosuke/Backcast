"""Gymnasium trading environment for the post-open trading window.

Implements single-net position day trading with market orders, slippage,
an opening warmup (no orders for the first N seconds after T_open), and
mandatory square-off at the last tick inside the window.
"""

from __future__ import annotations

from dataclasses import replace
from enum import IntEnum
from typing import Any

import gymnasium as gym
import numpy as np
from gymnasium import spaces

from .data import OPENING_WINDOW_US, OpeningSessionData
from .features import FeatureConfig, extract_features

BASE_OBS_DIM = 5


class Action(IntEnum):
    HOLD = 0
    BUY = 1
    SELL = 2


class TickTradingEnv(gym.Env):
    """Gymnasium environment for tick-by-tick trading in the post-open window.

    Action Space:
        Discrete(3):
            0: HOLD (maintain position)
            1: BUY  (if flat: open Long; if short: cover to flat [or flip if allow_flip]; if long: no-op)
            2: SELL (if flat: open Short; if long: sell to flat [or flip if allow_flip]; if short: no-op)

    Observation Space:
        Box(shape=(5 + K,), dtype=float32):
            [0]: Price return from open: (P_t - P_open) / P_open
            [1]: Normalized time progress: tau = (t - T_open) / window in [0, 1]
            [2]: Current net position: -1.0 (Short), 0.0 (Flat), +1.0 (Long)
            [3]: Unrealized PnL return: (P_t - avg_price) / avg_price * position (0 if flat)
            [4]: Normalized time held: holding_steps / total_episode_steps (0 if flat)
            [5:]: Optional extra feature vector (length K)

    Episode layout:
        Each session is truncated to ticks with ``epoch_us <= t_open + window``.
        ``reset()`` places the cursor on the first tick at or after
        ``t_open + warmup`` (earlier ticks only feed the causal features). If no
        such tick exists the episode starts on the last window tick and ends on
        the first step without any trade. The last window tick force-closes any
        open position.

    ``info["fills"]`` of every ``step()`` lists the executions of that step, the
    single source of truth for trade logs (see ``_Fill`` keys below).
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
        opening_warmup_seconds: float = 15.0,
        window_seconds: float = OPENING_WINDOW_US / 1_000_000,
        extra_features: np.ndarray | list[np.ndarray] | None = None,
        auto_features: bool = False,
    ) -> None:
        super().__init__()

        self.slippage = float(slippage)
        self.trade_penalty = float(trade_penalty)
        self.forced_square_off_penalty = float(forced_square_off_penalty)
        self.allow_flip = bool(allow_flip)
        self.opening_warmup_seconds = float(opening_warmup_seconds)
        self.opening_warmup_us = int(self.opening_warmup_seconds * 1_000_000)
        self.window_seconds = float(window_seconds)
        self.window_us = int(round(self.window_seconds * 1_000_000))

        raw_sessions: list[OpeningSessionData]
        if isinstance(session_data, list):
            if not session_data:
                raise ValueError("session_data list cannot be empty")
            raw_sessions = list(session_data)
        else:
            raw_sessions = [session_data]

        raw_extras: list[np.ndarray | None]
        if extra_features is not None:
            if isinstance(extra_features, list):
                raw_extras = list(extra_features)
            else:
                raw_extras = [extra_features]
        else:
            raw_extras = [None] * len(raw_sessions)

        self._sessions: list[OpeningSessionData] = []
        self._extra_features_list: list[np.ndarray | None] = []
        self._start_indices: list[int] = []
        self._install_sessions(raw_sessions, raw_extras, auto_features)

        self._feature_dim = self._infer_feature_dim()
        obs_dim = BASE_OBS_DIM + self._feature_dim
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
        self._step_idx = self._start_indices[0]
        self._position = 0  # -1, 0, +1
        self._avg_price = 0.0
        self._realized_pnl = 0.0
        self._holding_steps = 0
        self._trades_count = 0
        self._prev_total_pnl = 0.0

    # ------------------------------------------------------------------
    # Session management
    # ------------------------------------------------------------------
    @property
    def feature_config(self) -> FeatureConfig:
        """Feature configuration matching this env's window (used by auto_features)."""
        return FeatureConfig(window_seconds=self.window_seconds)

    @property
    def current_session(self) -> OpeningSessionData:
        return self._current_session

    @property
    def num_sessions(self) -> int:
        return len(self._sessions)

    @property
    def sessions(self) -> list[OpeningSessionData]:
        """Window-truncated sessions served by this env."""
        return list(self._sessions)

    def _truncate_session(
        self, session: OpeningSessionData, extra: np.ndarray | None
    ) -> tuple[OpeningSessionData, np.ndarray | None]:
        """Keep only ticks with epoch_us <= t_open + window (always >= 1 tick)."""
        n = len(session.epoch_us)
        if n == 0:
            raise ValueError("session has no ticks")
        limit = session.t_open_us + self.window_us
        n_keep = int(np.searchsorted(session.epoch_us, limit, side="right"))
        n_keep = max(1, n_keep)
        if n_keep >= n:
            return session, extra
        truncated = replace(
            session,
            epoch_us=session.epoch_us[:n_keep],
            price=session.price[:n_keep],
            qty=session.qty[:n_keep],
            trade_type=session.trade_type[:n_keep],
        )
        return truncated, (extra[:n_keep] if extra is not None else None)

    def _first_tradable_index(self, session: OpeningSessionData) -> int:
        """First tick index with epoch_us >= t_open + warmup (else the last tick)."""
        threshold = session.t_open_us + self.opening_warmup_us
        idx = int(np.searchsorted(session.epoch_us, threshold, side="left"))
        return min(idx, len(session.epoch_us) - 1)

    def has_tradable_tick(self, session: OpeningSessionData) -> bool:
        """True if some tick inside the (truncated) window is at/after the warmup."""
        threshold = session.t_open_us + self.opening_warmup_us
        limit = session.t_open_us + self.window_us
        return bool(np.any((session.epoch_us >= threshold) & (session.epoch_us <= limit)))

    def _install_sessions(
        self,
        sessions: list[OpeningSessionData],
        extras: list[np.ndarray | None],
        auto_features: bool,
    ) -> None:
        if len(extras) != len(sessions):
            raise ValueError("extra_features must be aligned with sessions")
        out_sessions: list[OpeningSessionData] = []
        out_extras: list[np.ndarray | None] = []
        for sess, extra in zip(sessions, extras, strict=True):
            sess_t, extra_t = self._truncate_session(sess, extra)
            if extra_t is None and auto_features:
                extra_t = extract_features(sess_t, self.feature_config)
            out_sessions.append(sess_t)
            out_extras.append(extra_t)
        self._sessions = out_sessions
        self._extra_features_list = out_extras
        self._start_indices = [self._first_tradable_index(s) for s in out_sessions]

    def _infer_feature_dim(self) -> int:
        for extra in self._extra_features_list:
            if extra is not None:
                return int(extra.shape[1])
        return 0

    def set_session(
        self,
        session_data: OpeningSessionData,
        extra_features: np.ndarray | None = None,
        auto_features: bool = False,
    ) -> None:
        """Replace the env's sessions with a single session."""
        self._install_sessions([session_data], [extra_features], auto_features)
        self._session_idx = 0
        self._current_session = self._sessions[0]
        self._current_extra = self._extra_features_list[0]
        if self._current_extra is not None:
            self._feature_dim = int(self._current_extra.shape[1])
            self.observation_space = spaces.Box(
                low=-np.inf,
                high=np.inf,
                shape=(BASE_OBS_DIM + self._feature_dim,),
                dtype=np.float32,
            )
        self._step_idx = self._start_indices[0]

    # ------------------------------------------------------------------
    # Observation helpers
    # ------------------------------------------------------------------
    def flat_observations(self, session_index: int) -> np.ndarray:
        """Observations a flat (position 0) agent sees on every tradable tick.

        Shape (T, obs_dim) for ticks ``start_index .. last`` of the session.
        Used to fit observation normalization statistics.
        """
        sess = self._sessions[session_index]
        extra = self._extra_features_list[session_index]
        start = self._start_indices[session_index]
        price = sess.price[start:]
        epoch = sess.epoch_us[start:]
        t = len(price)
        obs = np.zeros((t, BASE_OBS_DIM + self._feature_dim), dtype=np.float32)
        if sess.p_open > 0:
            obs[:, 0] = (price - sess.p_open) / sess.p_open
        obs[:, 1] = np.clip(
            (epoch - sess.t_open_us).astype(np.float64) / max(1, self.window_us),
            0.0,
            1.0,
        )
        if extra is not None and self._feature_dim > 0:
            obs[:, BASE_OBS_DIM:] = extra[start:]
        return obs

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
        tau = min(1.0, max(0.0, float(epoch_us - t_open_us) / max(1, self.window_us)))

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

    # ------------------------------------------------------------------
    # Gym API
    # ------------------------------------------------------------------
    def reset(
        self,
        *,
        seed: int | None = None,
        options: dict[str, Any] | None = None,
    ) -> tuple[np.ndarray, dict[str, Any]]:
        super().reset(seed=seed)

        forced_idx = None if options is None else options.get("session_index")
        if forced_idx is not None:
            self._session_idx = int(forced_idx)
        elif len(self._sessions) > 1:
            self._session_idx = int(self.np_random.integers(0, len(self._sessions)))
        else:
            self._session_idx = 0

        self._current_session = self._sessions[self._session_idx]
        self._current_extra = self._extra_features_list[self._session_idx]

        # Reset episode state: start at the first tradable tick (warmup handled here)
        self._step_idx = self._start_indices[self._session_idx]
        self._position = 0
        self._avg_price = 0.0
        self._realized_pnl = 0.0
        self._holding_steps = 0
        self._trades_count = 0
        self._prev_total_pnl = 0.0

        obs = self._get_obs()
        info = {
            "step": self._step_idx,
            "epoch_us": int(self._current_session.epoch_us[self._step_idx]),
            "price": float(self._current_session.price[self._step_idx]),
            "position": 0,
            "avg_price": 0.0,
            "realized_pnl": 0.0,
            "unrealized_pnl": 0.0,
            "total_pnl": 0.0,
            "trades_count": 0,
            "forced_square_off": False,
            "fills": [],
        }
        return obs, info

    @staticmethod
    def _fill(
        us: int, side: str, price: float, kind: str, pnl: float, forced: bool
    ) -> dict[str, Any]:
        return {
            "us": int(us),
            "side": side,
            "price": float(price),
            "kind": kind,
            "pnl": float(pnl),
            "forced": bool(forced),
        }

    def step(
        self,
        action: int | Action,
    ) -> tuple[np.ndarray, float, bool, bool, dict[str, Any]]:
        action_val = int(action)
        if not self.action_space.contains(action_val):
            raise ValueError(f"Invalid action {action_val} for {self.action_space}")

        total_ticks = len(self._current_session.price)
        current_price = float(self._current_session.price[self._step_idx])
        current_us = int(self._current_session.epoch_us[self._step_idx])
        t_open_us = self._current_session.t_open_us
        fills: list[dict[str, Any]] = []

        # Safety net (reset() already starts after the warmup): no orders during
        # the opening auction / first N seconds, nor on the final window tick
        # (it only exists to force-close, a new order would be an instant loss).
        if (
            current_us < t_open_us + self.opening_warmup_us
            or self._step_idx >= total_ticks - 1
        ):
            action_val = Action.HOLD

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
                fills.append(
                    self._fill(current_us, "buy", exec_price, "entry", 0.0, False)
                )
            elif self._position == -1:
                # Covering short
                exec_price = current_price + self.slippage
                # Short realized PnL = (avg_price - exec_price) * 1
                pnl = (self._avg_price - exec_price) * 1.0
                self._realized_pnl += pnl
                fills.append(
                    self._fill(current_us, "buy", exec_price, "exit", pnl, False)
                )

                if self.allow_flip:
                    # Reverse position directly to Long (+1)
                    self._position = 1
                    self._avg_price = exec_price
                    self._holding_steps = 0
                    self._trades_count += 2
                    fills.append(
                        self._fill(current_us, "buy", exec_price, "entry", 0.0, False)
                    )
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
                fills.append(
                    self._fill(current_us, "sell", exec_price, "entry", 0.0, False)
                )
            elif self._position == 1:
                # Closing long
                exec_price = current_price - self.slippage
                # Long realized PnL = (exec_price - avg_price) * 1
                pnl = (exec_price - self._avg_price) * 1.0
                self._realized_pnl += pnl
                fills.append(
                    self._fill(current_us, "sell", exec_price, "exit", pnl, False)
                )

                if self.allow_flip:
                    # Reverse position directly to Short (-1)
                    self._position = -1
                    self._avg_price = exec_price
                    self._holding_steps = 0
                    self._trades_count += 2
                    fills.append(
                        self._fill(current_us, "sell", exec_price, "entry", 0.0, False)
                    )
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
        next_us = int(self._current_session.epoch_us[self._step_idx])

        # 3. Update holding steps
        if self._position != 0:
            self._holding_steps += 1
        else:
            self._holding_steps = 0

        # 4. Mandatory square-off at the final tick of the window
        if is_last_tick and self._position != 0:
            forced_square_off = True
            if self._position == 1:
                # Liquidate Long at market - slippage
                exec_price = next_price - self.slippage
                pnl = (exec_price - self._avg_price) * 1.0
                fills.append(
                    self._fill(next_us, "sell", exec_price, "exit", pnl, True)
                )
            else:
                # Liquidate Short at market + slippage
                exec_price = next_price + self.slippage
                pnl = (self._avg_price - exec_price) * 1.0
                fills.append(
                    self._fill(next_us, "buy", exec_price, "exit", pnl, True)
                )
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
            "epoch_us": next_us,
            "price": float(next_price),
            "position": int(self._position),
            "avg_price": float(self._avg_price),
            "realized_pnl": float(self._realized_pnl),
            "unrealized_pnl": float(self._unrealized_pnl(next_price)),
            "total_pnl": float(current_total_pnl),
            "trades_count": int(self._trades_count),
            "forced_square_off": bool(forced_square_off),
            "fills": fills,
        }

        return obs, reward, terminated, truncated, info
