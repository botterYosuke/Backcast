"""Rule-based baselines for :class:`~trading_rl.scalp_env.ScalpEnv`.

Every baseline exposes ``reset()`` and ``act(env, obs) -> int`` and may only read
the agent's own state through ``env.state_view()`` (position, own orders, last
price, tick) -- never the future of the tape.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

import numpy as np

from .scalp_env import (
    G_ASK0,
    G_BID0,
    G_CANCEL,
    G_FLATTEN,
    G_KEEP,
    G_MBUY,
    G_MSELL,
    N_ACTIONS,
    ScalpEnv,
)

BID0, ASK0 = G_BID0, G_ASK0  # global ids of bid_k0 / ask_k0


class DoNothingPolicy:
    name = "DoNothing"

    def reset(self) -> None:
        return None

    def act(self, env: ScalpEnv, obs: np.ndarray) -> int:
        return G_KEEP  # index 0 = keep in every order mode


class RandomScalpPolicy:
    """Uniform over all actions of the env's mode (many are no-ops by the rules)."""

    name = "Random"

    def __init__(self, seed: int | None = None) -> None:
        self._rng = np.random.default_rng(seed)

    def reset(self) -> None:
        return None

    def act(self, env: ScalpEnv, obs: np.ndarray) -> int:
        n = int(env.action_space.n) if env is not None else N_ACTIONS
        return int(self._rng.integers(n))


@dataclass(frozen=True)
class RuleScalperParams:
    mode: str = "long"  # "long" = bid only, "mm" = bid and ask when flat
    k_entry: int = 1  # entry quote distance from last, ticks (0..2)
    tp: int = 1  # take-profit distance from the entry price, ticks
    sl: float = 4.0  # market exit when unrealized <= -sl ticks (inf = off)
    timeout_s: int = 60  # market exit after holding this long
    requote_s: int = 10  # re-place a stale entry/exit limit after this long
    no_entry_last_s: int = 30  # no new entry quotes in the last seconds

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        if d["sl"] == float("inf"):
            d["sl"] = "inf"
        return d


class RuleScalper:
    """Quote near the touch, take ``tp`` ticks, cut at ``sl`` / ``timeout_s``.

    Flat: place a bid at ``last - k_entry`` ticks (and, in ``mm`` mode, an ask at
    ``last + k_entry``); stale quotes are re-placed every ``requote_s``.  In
    position: an exit limit at ``entry +/- tp`` ticks (clipped to the action
    menu's ``last +/- 0..2`` ticks, so far targets are approached by re-quoting)
    and a market exit when the unrealized loss reaches ``sl`` or the holding time
    exceeds ``timeout_s``.

    The policy emits indices of the env's action set (``config.order_mode``).
    ``limit`` and ``both`` use the limit logic above.  In ``market`` mode there is
    nothing to rest: flat -> market entry (``long``: buy; ``mm``: alternate
    buy / sell per entry), in position -> market flatten once the unrealized PnL
    reaches ``+tp`` ticks, ``-sl`` ticks or ``timeout_s`` (``k_entry`` and
    ``requote_s`` are unused).
    """

    name = "RuleScalper"

    def __init__(self, params: RuleScalperParams | None = None) -> None:
        self.p = params or RuleScalperParams()
        self._flip = 0

    def reset(self) -> None:
        self._flip = 0

    def act(self, env: ScalpEnv, obs: np.ndarray) -> int:
        ix = env.action_index
        if env.config.order_mode == "market":
            return self._act_market(env, ix)
        p = self.p
        v = env.state_view()
        pos, last, tk = v["pos"], v["last"], v["tick"]
        if pos == 0:
            if v["remaining"] <= p.no_entry_last_s:
                return (
                    ix(G_CANCEL)
                    if (v["bid"] is not None or v["ask"] is not None)
                    else ix(G_KEEP)
                )
            if v["bid"] is None:
                return ix(BID0 + p.k_entry)
            if v["since_bid"] >= p.requote_s:
                return ix(BID0 + p.k_entry)
            if p.mode == "mm":
                if v["ask"] is None:
                    return ix(ASK0 + p.k_entry)
                if v["since_ask"] >= p.requote_s:
                    return ix(ASK0 + p.k_entry)
            return ix(G_KEEP)
        # in position
        if v["unreal_ticks"] <= -p.sl or v["since_entry"] >= p.timeout_s:
            return ix(G_FLATTEN)
        entry = v["entry"]
        if pos > 0:
            target = entry + p.tp * tk
            k = int(np.clip(round((target - last) / tk), 0, 2))
            if v["ask"] is None or v["since_ask"] >= p.requote_s:
                return ix(ASK0 + k)
        else:
            target = entry - p.tp * tk
            k = int(np.clip(round((last - target) / tk), 0, 2))
            if v["bid"] is None or v["since_bid"] >= p.requote_s:
                return ix(BID0 + k)
        return ix(G_KEEP)

    def _act_market(self, env: ScalpEnv, ix: Any) -> int:
        p = self.p
        v = env.state_view()
        if v["pos"] == 0:
            if v["remaining"] <= p.no_entry_last_s:
                return ix(G_KEEP)
            buy = p.mode != "mm" or self._flip % 2 == 0
            self._flip += 1
            return ix(G_MBUY if buy else G_MSELL)
        if (
            v["unreal_ticks"] >= p.tp
            or v["unreal_ticks"] <= -p.sl
            or v["since_entry"] >= p.timeout_s
        ):
            return ix(G_FLATTEN)
        return ix(G_KEEP)


@dataclass(frozen=True)
class MomentumParams:
    lookback_s: int = 60  # return window: last print now vs ``lookback_s`` ago
    thr_ticks: float = 1.0  # enter when |move| is strictly above this many ticks
    hold_s: int = 180  # market exit after holding this long (590 = to forced close)
    entry_s: int = 30  # no entry before T_open + entry_s

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class MomentumScalper:
    """Market-order momentum: enter in the direction of the recent move, hold.

    Flat and ``d >= entry_s``: the move is ``(last - last_{d - lookback}) / tick``
    (the reference second is clamped at T_open, so early decisions use the history
    that exists).  ``move > thr`` -> market buy, ``move < -thr`` -> market sell,
    else keep.  In position: market flatten once ``hold_s`` has elapsed; otherwise
    the forced close at T_open + 600 s ends the trade.  Re-entry after an exit is
    allowed.  Needs the market actions (``order_mode`` "market" or "both").
    """

    name = "Momentum"

    def __init__(self, params: MomentumParams | None = None) -> None:
        self.p = params or MomentumParams()

    def reset(self) -> None:
        return None

    def act(self, env: ScalpEnv, obs: np.ndarray) -> int:
        if env.config.order_mode == "limit":
            raise ValueError("Momentum needs market actions (order_mode market/both)")
        ix = env.action_index
        p = self.p
        v = env.state_view()
        if v["pos"] != 0:
            return ix(G_FLATTEN) if v["since_entry"] >= p.hold_s else ix(G_KEEP)
        d = v["d"]
        if d < p.entry_s:
            return ix(G_KEEP)
        move = (v["last"] - env.price_at(d - p.lookback_s)) / v["tick"]
        if move > p.thr_ticks:
            return ix(G_MBUY)
        if move < -p.thr_ticks:
            return ix(G_MSELL)
        return ix(G_KEEP)


def momentum_grid() -> list[MomentumParams]:
    """Validation grid searched for the tuned Momentum baseline (81 configs)."""
    return [
        MomentumParams(lookback_s=lb, thr_ticks=float(thr), hold_s=hold, entry_s=entry)
        for lb in (30, 60, 120)
        for thr in (0, 1, 2)
        for hold in (60, 180, 590)
        for entry in (10, 30, 60)
    ]


def rule_scalper_grid(
    modes: tuple[str, ...] = ("long", "mm"),
    order_mode: str = "limit",
) -> list[RuleScalperParams]:
    """Validation grid searched for the tuned RuleScalper.

    ``order_mode="market"`` drops the dimensions the market logic ignores
    (``k_entry``, ``requote_s``).
    """
    out = []
    market = order_mode == "market"
    for mode in modes:
        for k_entry in (0,) if market else (0, 1, 2):
            for tp in (1, 2, 3):
                for sl in (3.0, 6.0, float("inf")):
                    for timeout_s in (30, 90):
                        for requote_s in (5,) if market else (5, 20):
                            out.append(
                                RuleScalperParams(
                                    mode=mode,
                                    k_entry=k_entry,
                                    tp=tp,
                                    sl=sl,
                                    timeout_s=timeout_s,
                                    requote_s=requote_s,
                                )
                            )
    return out


def make_baseline(spec: dict[str, Any], seed: int | None = None) -> Any:
    """Factory used by multiprocessing workers: ``{"name": ..., "params": {...}}``."""
    name = spec["name"]
    if name == "DoNothing":
        return DoNothingPolicy()
    if name == "Random":
        return RandomScalpPolicy(seed)
    if name == "RuleScalper":
        params = dict(spec.get("params", {}))
        if params.get("sl") == "inf":
            params["sl"] = float("inf")
        return RuleScalper(RuleScalperParams(**params))
    if name == "Momentum":
        return MomentumScalper(MomentumParams(**dict(spec.get("params", {}))))
    raise ValueError(f"unknown baseline {name!r}")


def run_policy_on_session(policy: Any, env: ScalpEnv, session: Any) -> dict[str, Any]:
    """Play one session to the end with ``policy``; return the episode summary."""
    obs, _ = env.reset(options={"session": session})
    policy.reset()
    done = False
    info: dict[str, Any] = {}
    n_act = np.zeros(int(env.action_space.n), dtype=np.int64)
    while not done:
        a = policy.act(env, obs)
        n_act[a] += 1
        obs, _, done, _, info = env.step(a)
    ep = dict(info["episode"])
    ep["trips"] = list(env._trips)  # noqa: SLF001
    ep["actions"] = n_act
    return ep


def eval_baseline_rows(
    spec: dict[str, Any],
    rows: list[Any],
    config: dict[str, Any],
    seed: int = 0,
) -> list[dict[str, Any]]:
    """Worker entry point (picklable): load sessions lazily and evaluate."""
    from .scalp_env import ScalpConfig, load_scalp_session

    cfg = ScalpConfig(**config)
    env = ScalpEnv(None, cfg)
    policy = make_baseline(spec, seed=seed)
    out = []
    for row in rows:
        try:
            sess = load_scalp_session(row)
        except Exception:  # noqa: BLE001
            continue
        out.append(run_policy_on_session(policy, env, sess))
    return out


def tune_specs_on_rows(
    specs: list[dict[str, Any]],
    rows: list[Any],
    config: dict[str, Any],
) -> list[dict[str, float]]:
    """Worker for grid search: load ``rows`` once, score every spec on them."""
    from .scalp_env import ScalpConfig, load_scalp_session

    cfg = ScalpConfig(**config)
    env = ScalpEnv(None, cfg)
    sessions = []
    for row in rows:
        try:
            sessions.append(load_scalp_session(row))
        except Exception:  # noqa: BLE001
            continue
    out = []
    for spec in specs:
        policy = make_baseline(spec)
        eps = [run_policy_on_session(policy, env, s) for s in sessions]
        out.append(
            {
                "pnl_yen100": float(np.mean([e["pnl_yen100"] for e in eps])),
                "pnl_ticks": float(np.mean([e["pnl_ticks"] for e in eps])),
                "n_trips": float(np.mean([e["n_trips"] for e in eps])),
                "n": float(len(eps)),
            }
        )
    return out
