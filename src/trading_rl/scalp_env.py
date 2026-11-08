"""Limit-order scalping environment for the opening 10 minutes (P3 of plan v2).

One episode is one (date, stem) opening session.  The agent decides once per
second from ``T_open + 10 s``; the last decision is at ``T_open + 599 s`` and the
episode ends at ``T_open + 600 s`` with a forced market flatten.  Between two
decisions every tick is processed in time order to fill working limit orders.

Conventions
    * Position: net -1 / 0 / +1 unit (1 unit = 100 shares).  Working orders: at
      most one buy limit and one sell limit.  Flat may hold both (market making),
      long only a sell limit (exit), short only a buy limit (exit).  Actions that
      break this are no-ops counted in ``info["n_invalid_actions"]``.
    * Latency: every action (place / replace / cancel / market) takes effect at
      ``decision_time + latency_ms``.  Until then the previous orders stay
      working; a new order can only be hit by ticks strictly after its activation.
    * Fill rule (``trade_through``, default): buy limit at P fills when a tick
      prints ``< P``, sell limit at P when a tick prints ``> P``; fill price P,
      full unit.  ``touch`` (reference only): ``<= P`` / ``>= P``.  Queue position
      is not modelled; prints are the only liquidity information, so
      ``trade_through`` is the conservative rule (a print at exactly P could have
      been filled by earlier queue members).  Neither rule models a marketable
      limit order (a buy at the last price while the ask is lower) filling
      immediately.
      ``queue_est`` (calibrated, :mod:`trading_rl.fill_model`): on arrival the order
      is marketable with the session's precomputed probability ``fill_pmkt`` (then it
      fills at once at its price); otherwise it waits behind ``fill_qhat`` shares and
      fills once prints at exactly its price exceed ``fill_qhat + 100`` shares, or on
      a print through its price.  The draw uses the env RNG (seed it for evaluation).
    * ``sequential_cancel`` (kabu has no order amendment): replacing a working order
      on the same side, or a market order while limit orders are working, first
      cancels (effective at ``decision + latency``) and only then sends the new order
      (effective at ``decision + 2 * latency``).
    * Session length: ``ScalpSession.window_s`` / ``start_s`` (default 600 / 10, the
      opening cache).  Morning sessions (:func:`morning_window_s`) end at 11:29:00.
    * Market orders: buy at ``last + 0.5 tick``, sell at ``last - 0.5 tick`` using
      the last print at activation time and the tick size of that price.  Market
      buy / sell (``order_mode`` "market" or "both") are position-changing orders
      with the same slippage as market-flatten: buy takes flat->long and
      short->flat, sell takes flat->short and long->flat.  Every market order
      cancels all working limit orders.
    * Order modes: ``limit`` (9 actions, the original), ``market`` (keep, market
      buy, market sell, market flatten) and ``both`` (the 9 limit actions followed
      by market buy / sell).  Action indices are stable per mode; checkpoints store
      ``order_mode`` and ``action_names`` so replay can decode them.
    * Action mask: :meth:`ScalpEnv.action_mask` marks actions that are legal in the
      current state (e.g. no bid while long).  Illegal actions stay harmless no-ops
      counted in ``info["n_invalid_actions"]``; an action that was legal at decision
      time but became illegal during the latency window (an order filled in
      between) is counted in ``n_latency_noops`` instead.
    * Reward shaping (training only; reported PnL is always raw): per decision step
      ``reward -= (inventory_penalty_ticks_per_s * |position| +
      idle_penalty_ticks_per_s * [flat and nothing working]) * dt`` with dt = 1 s and
      the position / working state taken at decision time.  The idle penalty is for
      comparison only; it rewards trading for its own sake and is not recommended.
      Reward base (``reward_mode``): ``"mtm"`` = per-step change of the
      mark-to-market PnL (unrealized included); ``"realized"`` = only the PnL a fill
      locks in (paid on the step a position is closed, incl. the forced close), so
      no reward flows while a position is open and the credit for a closed trade
      reaches the decisions that led to it through the discounted return.  Both
      sum to the same final PnL.
      Unrealized-loss penalty: after each step, while a position is still open,
      ``reward -= unrealized_loss_penalty_per_s * max(0, -unrealized_ticks) * dt``
      (unrealized gains are not rewarded; exiting on a step avoids it for that step).
      Action-based shaping (independent of the position / order state):
      ``reward -= keep_penalty_ticks`` on every ``keep`` decision and
      ``reward += action_bonus_ticks`` on every non-keep action that took effect
      (not a mask violation / latency no-op).  Since each step is exactly one of the
      two, a keep penalty p equals an action bonus p minus a constant.
    * Observation: ``ScalpConfig.features`` is a feature spec (``"base"`` = the 28 gate
      features + 10 agent-state dims, ``"base+A+C-J"`` etc., see
      :mod:`trading_rl.scalp_features`); the size is per config (:func:`obs_dim`),
      sessions must be built with the same spec.  Trip dicts carry ``gross_ticks``,
      the round-trip PnL before the market slippage.
    * PnL is booked in yen per share internally; reported as yen per 100 shares
      (``* 100``) and in ticks of the session reference tick (tick size at the
      opening price) so that the per-step reward telescopes exactly to the final
      PnL in ticks.  Commission per fill (yen per unit) defaults to 0.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import gymnasium as gym
import numpy as np
from gymnasium import spaces

from .data import OpeningSessionData
from .gate_features import FEATURE_NAMES, compute_session_grid
from .scalp_features import (
    ContextStore,
    SessionArrays,
    compute_extras,
    default_store,
    kept_base_index,
    market_names,
    parse_feature_spec,
)
from .universe import tick_size, tick_sizes

WINDOW_S: int = 600
START_S: int = 10
US: int = 1_000_000
DECISION_SECONDS = np.arange(START_S, WINDOW_S, dtype=np.int64)  # 590 decisions
N_STEPS: int = len(DECISION_SECONDS)

# Global action ids (= the "both" layout; "limit" is its first 9 entries).
G_KEEP, G_CANCEL = 0, 1
G_BID0, G_BID1, G_BID2 = 2, 3, 4
G_ASK0, G_ASK1, G_ASK2 = 5, 6, 7
G_FLATTEN, G_MBUY, G_MSELL = 8, 9, 10
GLOBAL_ACTION_NAMES: list[str] = [
    "keep",
    "cancel_all",
    "bid_k0",
    "bid_k1",
    "bid_k2",
    "ask_k0",
    "ask_k1",
    "ask_k2",
    "market_flatten",
    "market_buy",
    "market_sell",
]
ORDER_MODES: tuple[str, ...] = ("limit", "market", "both")
ACTION_IDS: dict[str, tuple[int, ...]] = {
    "limit": tuple(range(9)),
    "market": (G_KEEP, G_MBUY, G_MSELL, G_FLATTEN),
    "both": tuple(range(11)),
}
# global id -> index within a mode's action set
ACTION_INDEX: dict[str, dict[int, int]] = {
    m: {g: i for i, g in enumerate(ids)} for m, ids in ACTION_IDS.items()
}
DT_S: float = 1.0  # seconds per decision step (penalty arithmetic)
FILL_RULES: tuple[str, ...] = ("trade_through", "touch", "queue_est")
MORNING_END_TOD_S: int = 11 * 3600 + 29 * 60  # forced flatten 11:29:00 (close 11:30)
MORNING_CACHE_S: int = 9000  # .cache/trading_rl_am window after T_open
_EPS = 1e-6


def tod_seconds(t_open_us: int) -> float:
    """Seconds since midnight of ``t_open_us`` (naive JST epoch, see trading_rl.data)."""
    return (int(t_open_us) % (86400 * US)) / US


OBS_TAPES: tuple[str, ...] = ("tick", "push20")
PUSH_GAP_US: int = 20_000


def push_tape_index(epoch_us: np.ndarray, gap_us: int = PUSH_GAP_US) -> np.ndarray:
    """Last-print index of each pseudo kabu push: greedy groups of prints that arrive
    within ``gap_us`` of the group's first print (20 ms reproduces the measured
    1.3-1.6 prints per volume-changing push, findings scalp-fill-calibration.md)."""
    t = np.asarray(epoch_us, dtype=np.int64)
    ends = []
    i, n = 0, len(t)
    while i < n:
        j = int(np.searchsorted(t, t[i] + gap_us, side="left"))
        j = max(j, i + 1)
        ends.append(j - 1)
        i = j
    return np.asarray(ends, dtype=np.int64)


def push_tape(sess: OpeningSessionData, gap_us: int = PUSH_GAP_US) -> OpeningSessionData:
    """The session as kabu PUSH would show it: one print per push (last price,
    summed volume, time of the push's last print)."""
    from dataclasses import replace

    ends = push_tape_index(sess.epoch_us, gap_us)
    q = np.asarray(sess.qty, dtype=np.float64)
    cq = np.concatenate([[0.0], np.cumsum(q)])
    starts = np.concatenate([[0], ends[:-1] + 1])
    return replace(
        sess,
        epoch_us=np.ascontiguousarray(np.asarray(sess.epoch_us)[ends]),
        price=np.ascontiguousarray(np.asarray(sess.price)[ends]),
        qty=np.ascontiguousarray((cq[ends + 1] - cq[starts]).astype(np.int64)),
        trade_type=np.full(len(ends), "", dtype=object),
    )


def morning_window_s(t_open_us: int, cap: int = MORNING_CACHE_S) -> int:
    """Seconds from ``T_open`` to 11:29:00 (whole seconds, capped at the cache window)."""
    w = int(np.floor(MORNING_END_TOD_S - tod_seconds(t_open_us)))
    return max(0, min(int(cap), w))


def action_names(order_mode: str) -> list[str]:
    return [GLOBAL_ACTION_NAMES[g] for g in ACTION_IDS[order_mode]]


def n_actions(order_mode: str) -> int:
    return len(ACTION_IDS[order_mode])


def action_valid(g: int, pos: int, has_bid: bool, has_ask: bool) -> bool:
    """Is global action ``g`` legal for this (position, working orders) state?"""
    if g == G_KEEP:
        return True
    if g == G_CANCEL:
        return has_bid or has_ask
    if G_BID0 <= g <= G_BID2:
        return pos <= 0
    if G_ASK0 <= g <= G_ASK2:
        return pos >= 0
    if g == G_FLATTEN:
        return pos != 0
    if g == G_MBUY:
        return pos <= 0
    if g == G_MSELL:
        return pos >= 0
    raise ValueError(f"unknown action {g}")


def _build_mask_tables() -> dict[str, dict[tuple[int, bool, bool], np.ndarray]]:
    out: dict[str, dict[tuple[int, bool, bool], np.ndarray]] = {}
    for mode, ids in ACTION_IDS.items():
        tab = {}
        for pos in (-1, 0, 1):
            for hb in (False, True):
                for ha in (False, True):
                    tab[(pos, hb, ha)] = np.array(
                        [action_valid(g, pos, hb, ha) for g in ids], dtype=bool
                    )
        out[mode] = tab
    return out


_MASK_TABLES = _build_mask_tables()

# Legacy (limit-mode) names kept for existing callers / checkpoints.
A_KEEP, A_CANCEL, A_FLATTEN = 0, 1, 8
N_ACTIONS: int = 9
ACTION_NAMES: list[str] = action_names("limit")
STATE_NAMES: list[str] = [
    "position",
    "unrealized_ticks",
    "has_bid",
    "bid_dist_ticks",
    "has_ask",
    "ask_dist_ticks",
    "since_entry_60s",
    "since_bid_60s",
    "since_ask_60s",
    "time_remaining_frac",
]
# Module constants describe the default ``base`` feature spec (28 + 10).  Per-config
# sizes come from :func:`obs_dim` / :func:`obs_names` (``ScalpConfig.features``).
N_MARKET = len(FEATURE_NAMES)
OBS_NAMES: list[str] = list(FEATURE_NAMES) + STATE_NAMES
OBS_DIM: int = len(OBS_NAMES)


def obs_names(features: str = "base") -> list[str]:
    """Observation names of a feature spec (market columns + agent state)."""
    return market_names(features) + STATE_NAMES


def obs_dim(features: str = "base") -> int:
    return len(market_names(features)) + len(STATE_NAMES)

# Fixed (mean, std) for the agent-state observation dims; market dims are fitted.
STATE_MEAN = np.zeros(len(STATE_NAMES), dtype=np.float32)
STATE_STD = np.array(
    [1.0, 3.0, 1.0, 2.0, 1.0, 2.0, 1.0, 1.0, 1.0, 1.0], dtype=np.float32
)


@dataclass(frozen=True)
class ScalpConfig:
    latency_ms: int = 50
    fill_rule: str = "trade_through"  # or "touch"
    market_slip_ticks: float = 0.5
    commission_yen: float = 0.0  # per fill per 100-share unit
    inventory_penalty: float = 0.0  # legacy alias of inventory_penalty_ticks_per_s
    order_mode: str = "limit"  # "limit" | "market" | "both"
    # Shaping (training only): ticks per second of holding / of idling.
    inventory_penalty_ticks_per_s: float = 0.0
    idle_penalty_ticks_per_s: float = 0.0
    # Action-based shaping (training only): ticks per keep decision / per effective
    # non-keep action.  Does not look at the position or working orders.
    keep_penalty_ticks: float = 0.0
    action_bonus_ticks: float = 0.0
    reward_mode: str = "mtm"  # "mtm" | "realized" (training reward base)
    unrealized_loss_penalty_per_s: float = 0.0  # x |unrealized loss| ticks per s
    # Observation feature spec ("base", "base+A+C-J", see scalp_features); not a
    # reward / fill setting, only the market part of the observation.
    features: str = "base"
    # kabu has no amendment: replace = cancel, then a new order one latency later.
    sequential_cancel: bool = False
    # "opening" (600 s cache) | "morning" (T_open .. 11:29:00, trading_rl_am cache);
    # used by the session loaders and recorded in the metadata.
    window: str = "opening"
    # tape the observation features are computed from: "tick" (every print) or
    # "push20" (kabu PUSH granularity, see push_tape).  Fills always use every print.
    obs_tape: str = "tick"

    def __post_init__(self) -> None:
        parse_feature_spec(self.features)  # validates
        if self.fill_rule not in FILL_RULES:
            raise ValueError(f"unknown fill_rule {self.fill_rule!r}")
        if not 0 <= self.latency_ms < 1000:
            raise ValueError("latency_ms must be in [0, 1000)")
        if self.sequential_cancel and 2 * self.latency_ms >= 1000:
            raise ValueError("sequential_cancel needs 2 * latency_ms < 1000")
        if self.window not in ("opening", "morning"):
            raise ValueError(f"unknown window {self.window!r}")
        if self.obs_tape not in OBS_TAPES:
            raise ValueError(f"unknown obs_tape {self.obs_tape!r}")
        if self.reward_mode not in ("mtm", "realized"):
            raise ValueError(f"unknown reward_mode {self.reward_mode!r}")
        if self.order_mode not in ORDER_MODES:
            raise ValueError(f"unknown order_mode {self.order_mode!r}")
        if (
            min(
                self.inventory_penalty,
                self.inventory_penalty_ticks_per_s,
                self.idle_penalty_ticks_per_s,
                self.keep_penalty_ticks,
                self.action_bonus_ticks,
                self.unrealized_loss_penalty_per_s,
            )
            < 0
        ):
            raise ValueError("penalties must be >= 0")

    def metadata(self) -> dict[str, Any]:
        return {
            "window_seconds": WINDOW_S if self.window == "opening" else "to 11:29:00",
            "window": self.window,
            "obs_tape": self.obs_tape,
            "sequential_cancel": self.sequential_cancel,
            "opening_warmup_seconds": START_S,
            "decision_interval_seconds": 1,
            "fill_rule": self.fill_rule,
            "latency_ms": self.latency_ms,
            "slippage": f"market orders at last +/- {self.market_slip_ticks} tick",
            "commission_yen_per_unit_per_fill": self.commission_yen,
            "inventory_penalty": self.inventory_penalty,
            "inventory_penalty_ticks_per_s": self.inventory_penalty_ticks_per_s,
            "idle_penalty_ticks_per_s": self.idle_penalty_ticks_per_s,
            "keep_penalty_ticks": self.keep_penalty_ticks,
            "action_bonus_ticks": self.action_bonus_ticks,
            "reward_mode": self.reward_mode,
            "unrealized_loss_penalty_per_s": self.unrealized_loss_penalty_per_s,
            "order_mode": self.order_mode,
            "features": parse_feature_spec(self.features).canonical,
            "feature_names": obs_names(self.features),
            "action_names": action_names(self.order_mode),
            "action_ids": list(ACTION_IDS[self.order_mode]),
            "unit_shares": 100,
        }


@dataclass
class ScalpSession:
    """Precomputed per-session arrays; the only thing the env needs."""

    date: str
    stem: str
    is_topix100: bool
    p_open: float
    ref_tick: float
    t_rel: np.ndarray  # (n,) int64 microseconds since T_open, ticks <= 600 s
    price: np.ndarray  # (n,) float64
    cnt: np.ndarray  # (601,) int64: number of ticks with t_rel <= s * 1e6
    last: np.ndarray  # (601,) float64: last print at each second
    tick: np.ndarray  # (601,) float64: tick size at that price
    X: np.ndarray  # (window_s - start_s, n_market(features)) float32 at decisions
    t_open_us: int = 0
    meta: dict[str, Any] = field(default_factory=dict)
    window_s: int = WINDOW_S  # forced flatten at T_open + window_s
    start_s: int = START_S  # first decision second
    qty: np.ndarray | None = None  # (n,) shares per print (queue_est)
    # queue_est fill model, (n_decisions, 2 [buy, sell], 3 [k]) float32
    fill_pmkt: np.ndarray | None = None
    fill_qhat: np.ndarray | None = None


def build_scalp_session(
    sess: OpeningSessionData,
    *,
    prev_close: float,
    avg_volume_20d: float,
    is_topix100: bool,
    features: str = "base",
    store: ContextStore | None = None,
    window_s: int = WINDOW_S,
    fill_model: Any = None,
    obs_tape: str = "tick",
) -> ScalpSession:
    """Build a :class:`ScalpSession` (causal gate features on the 1 s grid).

    ``window_s``: session length after ``T_open`` (600 = opening cache; the feature
    groups A-Q are only defined for 600).  ``fill_model``: a
    :class:`trading_rl.fill_model.FillModel` to precompute the ``queue_est`` arrays.
    ``obs_tape``: ``"push20"`` computes the market features from the kabu-PUSH-like
    tape (:func:`push_tape`); fills / prices of the env always use every print.

    ``features`` is a feature spec (``"base"`` = the original 28 columns, identical
    to before); groups that need daily / cross-sectional context read it from
    ``store`` (default: the process-wide store of the default cache dir).
    """
    spec = parse_feature_spec(features)
    W = int(window_s)
    if W <= START_S + 1:
        raise ValueError(f"window_s={W} too short")
    if spec.groups and W != WINDOW_S:
        raise ValueError("feature groups A-Q are defined for the 600 s window only")
    t_rel_all = np.asarray(sess.epoch_us, dtype=np.int64) - int(sess.t_open_us)
    keep = (t_rel_all >= 0) & (t_rel_all <= W * US)
    t_rel = np.ascontiguousarray(t_rel_all[keep])
    price = np.ascontiguousarray(np.asarray(sess.price, dtype=np.float64)[keep])
    if len(t_rel) == 0:
        raise ValueError("empty session")
    if obs_tape not in OBS_TAPES:
        raise ValueError(f"unknown obs_tape {obs_tape!r}")
    if spec.groups and obs_tape != "tick":
        raise ValueError("feature groups A-Q are computed from the tick tape only")
    obs_sess = push_tape(sess) if obs_tape == "push20" else sess
    grid = compute_session_grid(
        obs_sess,
        prev_close=prev_close,
        avg_volume_20d=avg_volume_20d,
        is_topix100=is_topix100,
        s_grid=np.arange(START_S, W, dtype=np.int64),
        window_s=W,
    )
    secs = np.arange(0, W + 1, dtype=np.int64)
    cnt = np.searchsorted(t_rel, secs * US, side="right").astype(np.int64)
    last = price[np.maximum(cnt - 1, 0)]
    tks = tick_sizes(last, np.full(len(last), bool(is_topix100)))
    X = grid.X
    if spec.drops:
        X = np.ascontiguousarray(X[:, kept_base_index(spec)])
    if spec.groups:
        store = store or default_store()
        arrays = SessionArrays(
            date=str(sess.date),
            t_open_us=int(sess.t_open_us),
            p_open=float(sess.p_open),
            v_open=float(sess.v_open),
            t_rel=t_rel,
            price=price,
            qty=np.ascontiguousarray(np.asarray(sess.qty, dtype=np.float64)[keep]),
            cnt=cnt,
            P=last,
            prev_close=float(prev_close or 0.0),
            avg_volume_20d=float(avg_volume_20d or 0.0),
            is_topix100=bool(is_topix100),
            stem=str(sess.stem),
        )
        needs_ctx = any(g in spec.groups for g in "CD")
        needs_daily = any(g in spec.groups for g in "ADEG")
        daily = (
            store.daily(str(sess.stem), str(sess.date), str(sess.code))
            if needs_daily
            else None
        )
        ctx = store.date_context(str(sess.date)) if needs_ctx else None
        extra = compute_extras(spec, arrays, daily=daily, ctx=ctx, store=store)
        X = np.concatenate([X, extra], axis=1)
    qty = np.ascontiguousarray(np.asarray(sess.qty, dtype=np.float64)[keep])
    pmkt = qhat = None
    if fill_model is not None:
        from .fill_model import session_fill_arrays

        dec = np.arange(START_S, W, dtype=np.int64)
        pmkt, qhat = session_fill_arrays(
            fill_model, t_rel, price, qty, dec, tks[dec], tod_seconds(sess.t_open_us)
        )
    return ScalpSession(
        date=str(sess.date),
        stem=str(sess.stem),
        is_topix100=bool(is_topix100),
        p_open=float(sess.p_open),
        ref_tick=float(tick_size(float(sess.p_open), bool(is_topix100))),
        t_rel=t_rel,
        price=price,
        cnt=cnt,
        last=last,
        tick=tks,
        X=X,
        t_open_us=int(sess.t_open_us),
        meta={"features": spec.canonical, "obs_tape": obs_tape},
        window_s=W,
        start_s=START_S,
        qty=qty,
        fill_pmkt=pmkt,
        fill_qhat=qhat,
    )


class ScalpEnv(gym.Env):
    """Gymnasium limit-order scalping environment (see module docstring)."""

    metadata = {"render_modes": []}

    def __init__(
        self,
        sessions: list[ScalpSession] | None = None,
        config: ScalpConfig | None = None,
        seed: int | None = None,
    ) -> None:
        super().__init__()
        self.config = config or ScalpConfig()
        self.sessions = sessions if sessions is not None else []
        self._ids = ACTION_IDS[self.config.order_mode]
        self._mask_tab = _MASK_TABLES[self.config.order_mode]
        self.action_space = spaces.Discrete(len(self._ids))
        self.n_market = len(market_names(self.config.features))
        self.obs_dim = self.n_market + len(STATE_NAMES)
        self.observation_space = spaces.Box(
            -np.inf, np.inf, shape=(self.obs_dim,), dtype=np.float32
        )
        self._lat_us = int(self.config.latency_ms * 1000)
        self._tt = self.config.fill_rule == "trade_through"
        self._queue = self.config.fill_rule == "queue_est"
        self._seq = bool(self.config.sequential_cancel)
        self._inv_rate = (
            self.config.inventory_penalty + self.config.inventory_penalty_ticks_per_s
        )
        self._idle_rate = self.config.idle_penalty_ticks_per_s
        self._keep_pen = self.config.keep_penalty_ticks
        self._act_bonus = self.config.action_bonus_ticks
        self._realized_reward = self.config.reward_mode == "realized"
        self._uloss_rate = self.config.unrealized_loss_penalty_per_s
        self.fill_rule = self.config.fill_rule
        self._rng = np.random.default_rng(seed)
        self._sess: ScalpSession | None = None
        self._obs = np.zeros(self.obs_dim, dtype=np.float32)
        self.done = True

    # ------------------------------------------------------------------ reset
    def reset(
        self,
        *,
        seed: int | None = None,
        options: dict[str, Any] | None = None,
    ) -> tuple[np.ndarray, dict[str, Any]]:
        super().reset(seed=seed)
        if seed is not None:
            self._rng = np.random.default_rng(seed)
        options = options or {}
        if "session" in options:
            sess = options["session"]
        elif "index" in options:
            sess = self.sessions[int(options["index"])]
        else:
            if not self.sessions:
                raise RuntimeError("ScalpEnv has no sessions")
            sess = self.sessions[int(self._rng.integers(len(self.sessions)))]
        if sess.X.shape[1] != self.n_market:
            raise ValueError(
                f"session has {sess.X.shape[1]} market features but the env "
                f"(features={self.config.features!r}) expects {self.n_market}"
            )
        self._sess = sess
        # Per-episode fill rule override (training curriculum); default = config.
        rule = str(options.get("fill_rule", self.config.fill_rule))
        if rule not in FILL_RULES:
            raise ValueError(f"unknown fill_rule {rule!r}")
        self.fill_rule = rule
        self._tt = rule == "trade_through"
        self._queue = rule == "queue_est"
        if self._queue and (sess.fill_qhat is None or sess.qty is None):
            raise ValueError("fill_rule='queue_est' needs sessions built with a fill_model")
        self._W = int(sess.window_s)
        self._S = int(sess.start_s)
        self._nsteps = self._W - self._S
        self._t = sess.t_rel
        self._p = sess.price
        self._q = sess.qty
        self._pmkt = sess.fill_pmkt
        self._qhat = sess.fill_qhat
        self._bid_need = 0.0
        self._ask_need = 0.0
        self._n_marketable = 0
        self._cnt = sess.cnt.tolist()
        self._last = sess.last.tolist()
        self._tick = sess.tick.tolist()
        self._ref_tick = sess.ref_tick
        self._d = self._S  # current decision second
        self._pos = 0
        self._entry = 0.0
        self._entry_us = 0
        self._entry_d = 0
        self._entry_slip = 0.0  # market slippage paid on the entry fill, yen/share
        self._cur_slip = 0.0  # slippage of the fill being booked (set by _market_fill)
        self._bid: float | None = None
        self._ask: float | None = None
        self._bid_d = 0
        self._ask_d = 0
        self._real = 0.0  # realized PnL, yen per share (commission included)
        self._prev_mtm = 0.0  # ticks
        self._prev_real = 0.0  # realized PnL, ticks (reward_mode="realized")
        self._fills: list[dict[str, Any]] = []
        self._step_fills: list[dict[str, Any]] = []
        self._trips: list[dict[str, Any]] = []
        self._n_invalid = 0
        self._n_latency_noops = 0
        self._n_orders = 0
        self._shaped = 0.0  # sum of shaped rewards (ticks)
        self._penalty = 0.0  # sum of shaping penalties (ticks)
        self._bonus = 0.0  # sum of action bonuses (ticks)
        self._dec: tuple[int, bool, bool] = (0, False, False)
        self.done = False
        self._write_obs()
        return self._obs.copy(), {}

    # ---------------------------------------------------------- observations
    def _write_obs(self) -> None:
        d = self._d
        o = self._obs
        idx = min(d, self._W - 1) - self._S
        j = self.n_market
        o[:j] = self._sess.X[idx]  # type: ignore[union-attr]
        last = self._last[d]
        tk = self._tick[d]
        pos = self._pos
        o[j] = pos
        unreal = pos * (last - self._entry) / self._ref_tick if pos else 0.0
        o[j + 1] = max(-20.0, min(20.0, unreal))
        if self._bid is None:
            o[j + 2] = 0.0
            o[j + 3] = 0.0
            o[j + 7] = 0.0
        else:
            o[j + 2] = 1.0
            o[j + 3] = max(-10.0, min(10.0, (last - self._bid) / tk))
            o[j + 7] = min(10.0, (d - self._bid_d) / 60.0)
        if self._ask is None:
            o[j + 4] = 0.0
            o[j + 5] = 0.0
            o[j + 8] = 0.0
        else:
            o[j + 4] = 1.0
            o[j + 5] = max(-10.0, min(10.0, (self._ask - last) / tk))
            o[j + 8] = min(10.0, (d - self._ask_d) / 60.0)
        o[j + 6] = min(10.0, (d - self._entry_d) / 60.0) if pos else 0.0
        o[j + 9] = (self._W - d) / float(self._nsteps)

    # --------------------------------------------------------------- helpers
    def _mtm_ticks(self, last: float) -> float:
        total = self._real
        if self._pos:
            total += self._pos * (last - self._entry)
        return total / self._ref_tick

    def _fill(
        self, side: int, price: float, t_us: int, order: str, forced: bool
    ) -> None:
        """Apply one fill (side +1 buy / -1 sell) to the position book."""
        comm = self.config.commission_yen / 100.0
        self._real -= comm
        if self._pos == 0:
            kind = "entry"
            self._pos = side
            self._entry = price
            self._entry_us = t_us
            self._entry_d = self._d
            self._entry_slip = self._cur_slip
            trip = None
        else:
            kind = "exit"
            gross = self._pos * (price - self._entry)
            self._real += gross
            pnl_yen_share = gross - 2.0 * comm
            slip_paid = self._entry_slip + self._cur_slip
            trip = {
                "pnl_yen100": pnl_yen_share * 100.0,
                "pnl_ticks": pnl_yen_share / self._ref_tick,
                # round-trip PnL before the market slippage (limit fills pay none)
                "gross_ticks": (pnl_yen_share + slip_paid) / self._ref_tick,
                "hold_s": (t_us - self._entry_us) / US,
                "exit_order": order,
                "forced": forced,
                "dir": self._pos,
            }
            self._trips.append(trip)
            self._pos = 0
            self._entry = 0.0
            self._entry_slip = 0.0
        rec = {
            "us": int(t_us),
            "abs_us": int(t_us) + int(self._sess.t_open_us),  # type: ignore[union-attr]
            "side": side,
            "price": price,
            "kind": kind,
            "order": order,
            "forced": forced,
        }
        if trip is not None:
            rec["pnl_ticks"] = trip["pnl_ticks"]
        self._fills.append(rec)
        self._step_fills.append(rec)

    def _advance(self, i0: int, i1: int) -> None:
        """Process ticks ``i0 <= i < i1`` against the working limit orders."""
        if i1 <= i0 or (self._bid is None and self._ask is None):
            return
        pr = self._p[i0:i1]
        qv = self._q[i0:i1] if self._queue else None
        base = i0
        tt = self._tt
        queue = self._queue
        while True:
            jb = js = -1
            cb = ca = None
            bid, ask = self._bid, self._ask
            if bid is not None:
                if queue:
                    cb = np.cumsum(np.where(np.abs(pr - bid) < _EPS, qv, 0.0))
                    m = (pr < bid - _EPS) | (cb >= self._bid_need)
                else:
                    m = (pr < bid) if tt else (pr <= bid)
                j = int(m.argmax())
                if m[j]:
                    jb = j
            if ask is not None:
                if queue:
                    ca = np.cumsum(np.where(np.abs(pr - ask) < _EPS, qv, 0.0))
                    m = (pr > ask + _EPS) | (ca >= self._ask_need)
                else:
                    m = (pr > ask) if tt else (pr >= ask)
                j = int(m.argmax())
                if m[j]:
                    js = j
            if jb < 0 and js < 0:
                if cb is not None:
                    self._bid_need -= float(cb[-1])
                if ca is not None:
                    self._ask_need -= float(ca[-1])
                return
            if js < 0 or (0 <= jb <= js):
                j, side, px = jb, 1, bid
                self._bid = None
                if ca is not None:
                    self._ask_need -= float(ca[j])
            else:
                j, side, px = js, -1, ask
                self._ask = None
                if cb is not None:
                    self._bid_need -= float(cb[j])
            self._fill(side, float(px), int(self._t[base + j]), "limit", False)  # type: ignore[arg-type]
            base += j + 1
            pr = pr[j + 1 :]
            if qv is not None:
                qv = qv[j + 1 :]
            if len(pr) == 0 or (self._bid is None and self._ask is None):
                return

    def _market_fill(self, side: int, i_ta: int, t_us: int, forced: bool) -> None:
        """Market order ``side`` at last +/- slippage (last print before ``i_ta``)."""
        last = float(self._p[max(i_ta - 1, 0)])
        tk = tick_size(last, self._sess.is_topix100)  # type: ignore[union-attr]
        slip = self.config.market_slip_ticks * tk
        px = last + slip if side > 0 else last - slip
        self._cur_slip = slip
        try:
            self._fill(side, px, t_us, "market", forced)
        finally:
            self._cur_slip = 0.0

    def _market_close(self, i_ta: int, t_us: int, forced: bool) -> None:
        """Flatten at market using the last print at ``i_ta`` (exclusive index)."""
        if self._pos == 0:
            return
        self._market_fill(-self._pos, i_ta, t_us, forced)

    def _bad(self, g: int) -> None:
        """Count a no-op: a mask violation, or a latency-window race."""
        if action_valid(g, *self._dec):
            self._n_latency_noops += 1
        else:
            self._n_invalid += 1

    def _place(self, side: int, k: int, px: float, t_act: int) -> bool:
        """queue_est arrival: True if the order is marketable and already filled."""
        if not self._queue:
            return False
        si = 0 if side > 0 else 1
        idx = self._d - self._S
        if self._rng.random() < float(self._pmkt[idx, si, k]):  # type: ignore[index]
            self._n_marketable += 1
            self._fill(side, float(px), int(t_act), "limit", False)
            return True
        need = float(self._qhat[idx, si, k]) + 100.0  # type: ignore[index]
        if side > 0:
            self._bid_need = need
        else:
            self._ask_need = need
        return False

    def _apply_action(self, g: int, t_act: int = 0) -> None:
        """Apply keep / cancel / place limit actions (global id ``g``).

        Market actions (flatten / market buy / sell) are handled in ``step``.
        """
        d = self._d
        if g == G_KEEP:
            return
        if g == G_CANCEL:
            self._bid = self._ask = None
            return
        if G_BID0 <= g <= G_BID2:
            if self._pos > 0:
                self._bad(g)
                return
            px = round(self._last[d] - (g - G_BID0) * self._tick[d], 4)
            if px <= 0:
                self._bad(g)
                return
            self._n_orders += 1
            self._bid = None
            if self._place(1, g - G_BID0, px, t_act):
                return
            self._bid = px
            self._bid_d = d
            return
        if G_ASK0 <= g <= G_ASK2:
            if self._pos < 0:
                self._bad(g)
                return
            px = round(self._last[d] + (g - G_ASK0) * self._tick[d], 4)
            self._n_orders += 1
            self._ask = None
            if self._place(-1, g - G_ASK0, px, t_act):
                return
            self._ask = px
            self._ask_d = d
            return
        raise ValueError(f"invalid action {g}")

    def _apply_market(self, g: int, i_ta: int, t_act: int) -> None:
        """Market flatten / buy / sell at the activation time."""
        if g == G_FLATTEN:
            self._bid = self._ask = None  # legacy: orders drop even when flat
            if self._pos != 0:
                self._market_close(i_ta, t_act, False)
            else:
                self._bad(g)
            return
        side = 1 if g == G_MBUY else -1
        # Intent is fixed at decision time: if a limit order filled during the
        # latency window the position changed and the market order is dropped
        # (otherwise a "sell to exit" could flip the book short).
        if (
            self._pos != self._dec[0]
            or (side > 0 and self._pos > 0)
            or (side < 0 and self._pos < 0)
        ):
            self._bad(g)
            return
        self._bid = self._ask = None
        self._market_fill(side, i_ta, t_act, False)
        self._n_orders += 1

    def _cancel_first(self, g: int) -> bool:
        """sequential_cancel: does the (valid) action ``g`` need a cancel round-trip?"""
        if G_BID0 <= g <= G_BID2:
            return self._bid is not None and self._pos <= 0
        if G_ASK0 <= g <= G_ASK2:
            return self._ask is not None and self._pos >= 0
        if g >= G_FLATTEN:
            if self._bid is None and self._ask is None:
                return False
            if g == G_FLATTEN:
                return self._pos != 0
            if self._pos != self._dec[0]:
                return False
            return self._pos <= 0 if g == G_MBUY else self._pos >= 0
        return False

    def action_mask(self) -> np.ndarray:
        """Boolean mask over this mode's actions: True = legal in the current state."""
        return self._mask_tab[
            (self._pos, self._bid is not None, self._ask is not None)
        ].copy()

    def action_index(self, global_id: int) -> int:
        """Index of global action ``global_id`` in this env's action set."""
        return ACTION_INDEX[self.config.order_mode][global_id]

    # ------------------------------------------------------------------ step
    def step(self, action: int) -> tuple[np.ndarray, float, bool, bool, dict[str, Any]]:
        if self.done:
            raise RuntimeError("step() called on a finished episode; reset()")
        a = int(action)
        if not 0 <= a < len(self._ids):
            raise ValueError(f"invalid action {action} for order_mode")
        g = self._ids[a]
        d = self._d
        cnt = self._cnt
        t_dec = d * US
        t_act = t_dec + self._lat_us
        self._step_fills = []
        pos0 = self._pos
        working = self._bid is not None or self._ask is not None
        self._dec = (pos0, self._bid is not None, self._ask is not None)
        bad0 = self._n_invalid + self._n_latency_noops

        if working or g >= G_BID0:
            i_ta = int(np.searchsorted(self._t, t_act, side="right"))
            if working:
                self._advance(cnt[d], i_ta)
            if self._seq and self._cancel_first(g):
                # cancel effective now, the new order one more latency later
                if g >= G_FLATTEN:
                    self._bid = self._ask = None
                elif g <= G_BID2:
                    self._bid = None
                else:
                    self._ask = None
                t_act += self._lat_us
                i_ta2 = int(np.searchsorted(self._t, t_act, side="right"))
                if self._bid is not None or self._ask is not None:
                    self._advance(i_ta, i_ta2)
                i_ta = i_ta2
            if g >= G_FLATTEN:
                self._apply_market(g, i_ta, t_act)
            else:
                self._apply_action(g, t_act)
            if self._bid is not None or self._ask is not None:
                self._advance(i_ta, cnt[d + 1])
        # a == KEEP / CANCEL with nothing working needs no tick processing

        d += 1
        self._d = d
        W = self._W
        terminated = d >= W
        if terminated:
            self._bid = self._ask = None
            if self._pos != 0:
                self._market_close(cnt[W], W * US, True)
        last = self._last[d]
        mtm = self._mtm_ticks(last)
        if self._realized_reward:
            real = self._real / self._ref_tick
            reward = real - self._prev_real
            self._prev_real = real
        else:
            reward = mtm - self._prev_mtm
        pen = 0.0
        if pos0 != 0:
            pen += self._inv_rate * abs(pos0) * DT_S
        elif not working:
            pen += self._idle_rate * DT_S
        if self._uloss_rate and self._pos != 0:
            unreal = self._pos * (last - self._entry) / self._ref_tick
            if unreal < 0:
                pen += self._uloss_rate * (-unreal) * DT_S
        bonus = 0.0
        if g == G_KEEP:
            pen += self._keep_pen
        elif self._n_invalid + self._n_latency_noops == bad0:
            bonus = self._act_bonus
        reward -= pen
        reward += bonus
        self._penalty += pen
        self._bonus += bonus
        self._shaped += reward
        self._prev_mtm = mtm
        info: dict[str, Any] = {}
        if self._step_fills:
            info["fills"] = self._step_fills
        if terminated:
            self.done = True
            info["episode"] = self.episode_summary()
        self._write_obs()
        return self._obs.copy(), float(reward), terminated, False, info

    # --------------------------------------------------------------- summary
    @property
    def fills(self) -> list[dict[str, Any]]:
        return self._fills

    @property
    def position(self) -> int:
        return self._pos

    @property
    def entry_price(self) -> float:
        return self._entry

    @property
    def seconds(self) -> int:
        """Current decision second since T_open."""
        return self._d

    def price_at(self, sec: int) -> float:
        """Last print at second ``sec`` since T_open (clamped to ``0 .. now``).

        Only seconds up to the current decision second are readable, so a rule
        baseline cannot peek at the future of the tape.
        """
        return self._last[max(0, min(int(sec), self._d))]

    def state_view(self) -> dict[str, Any]:
        """Agent-visible state (own orders/position) for rule-based baselines."""
        d = self._d
        last = self._last[d]
        return {
            "d": d,
            "pos": self._pos,
            "entry": self._entry,
            "since_entry": d - self._entry_d if self._pos else 0,
            "bid": self._bid,
            "ask": self._ask,
            "since_bid": d - self._bid_d if self._bid is not None else 0,
            "since_ask": d - self._ask_d if self._ask is not None else 0,
            "last": last,
            "tick": self._tick[d],
            "unreal_ticks": (
                self._pos * (last - self._entry) / self._ref_tick if self._pos else 0.0
            ),
            "remaining": self._W - d,
        }

    def episode_summary(self) -> dict[str, Any]:
        trips = self._trips
        pnl_ticks = self._prev_mtm
        wins = [t["pnl_yen100"] for t in trips if t["pnl_yen100"] > 0]
        losses = [t["pnl_yen100"] for t in trips if t["pnl_yen100"] <= 0]
        fills = self._fills
        return {
            "date": self._sess.date,  # type: ignore[union-attr]
            "stem": self._sess.stem,  # type: ignore[union-attr]
            "pnl_ticks": pnl_ticks,
            "pnl_yen100": self._real * 100.0,
            "n_trips": len(trips),
            "n_wins": len(wins),
            "gross_win_yen": float(sum(wins)),
            "gross_loss_yen": float(-sum(losses)),
            "hold_s_sum": float(sum(t["hold_s"] for t in trips)),
            "gross_ticks_sum": float(sum(t["gross_ticks"] for t in trips)),
            "n_entry_limit": sum(
                1 for f in fills if f["kind"] == "entry" and f["order"] == "limit"
            ),
            "n_entry_market": sum(
                1 for f in fills if f["kind"] == "entry" and f["order"] == "market"
            ),
            "n_exit_limit": sum(1 for t in trips if t["exit_order"] == "limit"),
            "n_exit_market": sum(
                1 for t in trips if t["exit_order"] == "market" and not t["forced"]
            ),
            "n_forced": sum(1 for t in trips if t["forced"]),
            "n_orders": self._n_orders,
            "n_invalid_actions": self._n_invalid,
            "n_latency_noops": self._n_latency_noops,
            "fill_rule": self.fill_rule,
            "n_limit_marketable": self._n_marketable,
            "window_s": self._W,
            # shaped (training) return vs raw PnL: pnl_ticks is always raw
            "shaped_return_ticks": self._shaped,
            "penalty_ticks": self._penalty,
            "bonus_ticks": self._bonus,
        }


# --------------------------------------------------------------------------- #
# Dataset helpers (universe rows -> sessions, symbol holdout)
# --------------------------------------------------------------------------- #
SessionRow = tuple[
    str, str, float, float, bool
]  # date, stem, prev_close, avg_vol, t100


MORNING_CACHE_DIR = Path(__file__).resolve().parents[2] / ".cache" / "trading_rl_am"


def is_heldout(stem: str) -> bool:
    """Deterministic ~20% symbol holdout (same rule as ``predictability_gate``)."""
    import hashlib

    return hashlib.md5(stem.lower().encode()).digest()[0] % 5 == 0


def universe_session_rows(
    start: str, end: str, cache_dir: Any = None
) -> list[SessionRow]:
    """Universe rows of ``start <= date <= end`` that have an ``ok`` cached session."""
    from .cache import CachedSessionUnavailable, load_cached_session
    from .universe import DEFAULT_CACHE_DIR, load_universe

    cache_dir = cache_dir or DEFAULT_CACHE_DIR
    out: list[SessionRow] = []
    for r in load_universe(cache_dir, start, end):
        date, stem = str(r["date"]), str(r["stem"])
        try:
            load_cached_session(date, stem, cache_dir)
        except CachedSessionUnavailable:
            continue
        out.append(
            (
                date,
                stem,
                float(r["prev_close"] or 0.0),  # type: ignore[arg-type]
                float(r["avg_volume_20d"] or 0.0),  # type: ignore[arg-type]
                bool(r["is_topix100"]),
            )
        )
    return out


def load_scalp_session(
    row: SessionRow,
    cache_dir: Any = None,
    features: str = "base",
    store: ContextStore | None = None,
    window: str = "opening",
    fill_model: Any = None,
    obs_tape: str = "tick",
) -> ScalpSession:
    """``window="morning"``: read ``cache_dir`` (default ``MORNING_CACHE_DIR``) and
    trade to 11:29:00 (:func:`morning_window_s`)."""
    from .cache import load_cached_session
    from .universe import DEFAULT_CACHE_DIR

    date, stem, prev_close, avg_vol, t100 = row
    if window == "morning":
        cache_dir = cache_dir or MORNING_CACHE_DIR
    raw = load_cached_session(date, stem, cache_dir or DEFAULT_CACHE_DIR)
    if store is None and parse_feature_spec(features).groups:
        store = default_store(cache_dir)
    window_s = morning_window_s(raw.t_open_us) if window == "morning" else WINDOW_S
    return build_scalp_session(
        raw,
        prev_close=prev_close,
        avg_volume_20d=avg_vol,
        is_topix100=t100,
        features=features,
        store=store,
        window_s=window_s,
        fill_model=fill_model,
        obs_tape=obs_tape,
    )


def load_scalp_sessions(
    rows: list[SessionRow],
    cache_dir: Any = None,
    progress_every: int = 0,
    features: str = "base",
    window: str = "opening",
    fill_model: Any = None,
    obs_tape: str = "tick",
) -> list[ScalpSession]:
    out = []
    store = default_store(cache_dir) if parse_feature_spec(features).groups else None
    for i, row in enumerate(rows):
        try:
            out.append(
                load_scalp_session(
                    row, cache_dir, features, store, window, fill_model, obs_tape
                )
            )
        except Exception:  # noqa: BLE001 - skip unusable sessions (counted by caller)
            continue
        if progress_every and (i + 1) % progress_every == 0:
            print(f"  loaded {i + 1}/{len(rows)} sessions", flush=True)
    return out
