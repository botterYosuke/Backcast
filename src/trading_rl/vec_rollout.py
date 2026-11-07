"""Batched multi-env rollout for PPO on :class:`~trading_rl.scalp_env.ScalpEnv`.

``VecScalpEnv`` steps N envs with auto-reset; ``VecPPOTrainer`` reuses
:class:`PPOTrainer`'s ``update`` (clipped objective, advantage normalisation,
minibatch epochs) and GAE semantics -- ``dones[t]`` means "the episode ended
after step t", the bootstrap of step t is masked by it -- but collects the
rollout with one batched policy forward per time step.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import torch
from torch.distributions.categorical import Categorical

from .policy import ActorCritic, apply_action_mask
from .scalp_env import ScalpConfig, ScalpEnv, ScalpSession, n_actions, obs_dim
from .trainer import PPOConfig, PPOTrainer


# --------------------------------------------------------------------------- #
# Training schedules
# --------------------------------------------------------------------------- #
def linear_schedule(start: float, end: float, progress: float) -> float:
    """Linear interpolation ``start -> end`` for ``progress`` clipped to [0, 1]."""
    f = min(1.0, max(0.0, float(progress)))
    return float(start + (end - start) * f)


def fill_curriculum_p(step: int, total_steps: int, p0: float, frac: float) -> float:
    """P(trade_through) for a new training episode.

    Rises linearly from ``p0`` at step 0 to 1.0 at ``frac * total_steps`` and stays
    at 1.0 afterwards (``frac <= 0`` means trade_through from the start).
    """
    if frac <= 0:
        return 1.0
    return linear_schedule(p0, 1.0, step / (frac * max(1, total_steps)))


class VecScalpEnv:
    """N independent envs sampling sessions at random, auto-reset on done."""

    def __init__(
        self,
        sessions: list[ScalpSession],
        n_envs: int,
        config: ScalpConfig | None = None,
        seed: int = 0,
        stagger: bool = True,
        p_trade_through: float | None = None,
    ) -> None:
        """``p_trade_through`` (training curriculum): probability that a *new*
        episode uses trade_through fills, else touch.  ``None`` = always the
        config's fill rule.  Change it with :meth:`set_p_trade_through`.
        """
        self.n = n_envs
        self.config = config or ScalpConfig()
        self.envs = [ScalpEnv(sessions, config, seed=seed + i) for i in range(n_envs)]
        self.observation_space = self.envs[0].observation_space
        self.action_space = self.envs[0].action_space
        self.n_actions = n_actions(self.config.order_mode)
        self.obs_dim = self.envs[0].obs_dim
        self.obs = np.zeros((n_envs, self.obs_dim), dtype=np.float32)
        self.masks = np.ones((n_envs, self.n_actions), dtype=bool)
        self.p_trade_through = p_trade_through
        self._rule_rng = np.random.default_rng(seed + 20_000)
        rng = np.random.default_rng(seed + 10_000)
        for i, env in enumerate(self.envs):
            o, _ = env.reset(options=self._reset_options())
            if stagger:  # de-synchronise episode phases (flat, no orders)
                for _ in range(int(rng.integers(0, 560))):
                    o, *_ = env.step(0)
            self.obs[i] = o
            self.masks[i] = env.action_mask()

    def set_p_trade_through(self, p: float | None) -> None:
        self.p_trade_through = p

    def _reset_options(self) -> dict[str, Any] | None:
        if self.p_trade_through is None:
            return None
        tt = self._rule_rng.random() < self.p_trade_through
        return {"fill_rule": "trade_through" if tt else "touch"}

    def step(
        self, actions: np.ndarray
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray, list[dict[str, Any]]]:
        rewards = np.zeros(self.n, dtype=np.float32)
        dones = np.zeros(self.n, dtype=bool)
        episodes: list[dict[str, Any]] = []
        for i, env in enumerate(self.envs):
            o, r, term, trunc, info = env.step(int(actions[i]))
            rewards[i] = r
            if term or trunc:
                dones[i] = True
                episodes.append(info["episode"])
                o, _ = env.reset(options=self._reset_options())
            self.obs[i] = o
            self.masks[i] = env.action_mask()
        return self.obs, rewards, dones, episodes


class VecPPOTrainer(PPOTrainer):
    """PPO with N parallel envs; ``steps_per_env`` steps per env per iteration."""

    def __init__(
        self,
        vec_env: VecScalpEnv,
        policy: ActorCritic | None = None,
        config: PPOConfig | None = None,
        steps_per_env: int = 256,
        device: str | torch.device = "cpu",
        seed: int | None = None,
        reward_norm: bool = True,
        use_masks: bool = True,
    ) -> None:
        cfg = config or PPOConfig()
        cfg.rollout_steps = steps_per_env * vec_env.n
        super().__init__(vec_env, policy, cfg, device=device, seed=seed)  # type: ignore[arg-type]
        self.vec_env = vec_env
        self.n_envs = vec_env.n
        self.steps_per_env = steps_per_env
        self.reward_norm = reward_norm
        self.use_masks = use_masks
        self._ret_run = np.zeros(self.n_envs, dtype=np.float64)
        self._ret_count = 1e-4
        self._ret_mean = 0.0
        self._ret_m2 = 0.0
        self.last_obs = vec_env.obs.copy()
        self.last_done = np.zeros(self.n_envs, dtype=bool)
        t, n = steps_per_env, self.n_envs
        self._o = np.zeros((t, n, vec_env.obs_dim), dtype=np.float32)
        self._a = np.zeros((t, n), dtype=np.int64)
        self._lp = np.zeros((t, n), dtype=np.float32)
        self._r = np.zeros((t, n), dtype=np.float32)
        self._v = np.zeros((t, n), dtype=np.float32)
        self._d = np.zeros((t, n), dtype=np.float32)
        self._m = np.ones((t, n, vec_env.n_actions), dtype=bool)
        self.episode_log: list[dict[str, Any]] = []

    def _scale_rewards(self, r: np.ndarray, dones: np.ndarray) -> np.ndarray:
        """Divide by the running std of the discounted return (scale only)."""
        if not self.reward_norm:
            return r
        self._ret_run = self._ret_run * self.config.gamma + r
        bc = float(len(self._ret_run))
        delta = float(self._ret_run.mean()) - self._ret_mean
        tot = self._ret_count + bc
        self._ret_mean += delta * bc / tot
        self._ret_m2 += (
            float(self._ret_run.var()) * bc + delta * delta * self._ret_count * bc / tot
        )
        self._ret_count = tot
        self._ret_run[dones] = 0.0
        std = np.sqrt(self._ret_m2 / self._ret_count + 1e-8)
        return np.clip(r / max(std, 1e-3), -10.0, 10.0).astype(np.float32)

    def collect_rollouts(self) -> dict[str, float]:  # type: ignore[override]
        self.policy.eval()
        eps: list[dict[str, Any]] = []
        for t in range(self.steps_per_env):
            obs_t = torch.as_tensor(self.last_obs)
            mask_t = torch.as_tensor(self.vec_env.masks) if self.use_masks else None
            with torch.no_grad():
                logits, value = self.policy(obs_t)
                logits = apply_action_mask(logits, mask_t)
                dist = Categorical(logits=logits)
                action = torch.multinomial(dist.probs, 1, generator=self._torch_gen)
                action = action.squeeze(-1)
                logprob = dist.log_prob(action)
            act = action.numpy()
            self._o[t] = self.last_obs
            self._m[t] = self.vec_env.masks  # masks that were used to sample act
            self._a[t] = act
            self._lp[t] = logprob.numpy()
            self._v[t] = value.numpy()
            obs, rewards, dones, episodes = self.vec_env.step(act)
            self._r[t] = self._scale_rewards(rewards, dones)
            self._d[t] = dones
            eps.extend(episodes)
            self.last_obs = obs.copy()
            self.last_done = dones
            self.total_timesteps += self.n_envs
        cap = self.steps_per_env * self.n_envs
        self.buffer.obs[:cap] = self._o.reshape(cap, self.vec_env.obs_dim)
        self.buffer.actions[:cap] = self._a.reshape(cap)
        self.buffer.logprobs[:cap] = self._lp.reshape(cap)
        self.buffer.rewards[:cap] = self._r.reshape(cap)
        self.buffer.values[:cap] = self._v.reshape(cap)
        self.buffer.dones[:cap] = self._d.reshape(cap)
        self.buffer.masks = (
            self._m.reshape(cap, -1).copy() if self.use_masks else None
        )
        self.buffer.ptr = cap
        self.episode_log.extend(eps)
        return {
            "mean_ep_return": (
                float(np.mean([e["pnl_ticks"] for e in eps])) if eps else float("nan")
            ),
            "mean_pnl_yen100": (
                float(np.mean([e["pnl_yen100"] for e in eps])) if eps else float("nan")
            ),
            "mean_trips": (
                float(np.mean([e["n_trips"] for e in eps])) if eps else float("nan")
            ),
            "episodes_completed": float(len(eps)),
            "running_ep_return": 0.0,
        }

    def compute_gae(  # type: ignore[override]
        self, next_obs: np.ndarray, next_done: np.ndarray
    ) -> tuple[np.ndarray, np.ndarray]:
        t_len, n = self.steps_per_env, self.n_envs
        with torch.no_grad():
            next_value = self.policy.get_value(torch.as_tensor(next_obs)).numpy()
        adv = np.zeros((t_len, n), dtype=np.float32)
        last = np.zeros(n, dtype=np.float32)
        g, lam = self.config.gamma, self.config.gae_lambda
        for t in reversed(range(t_len)):
            non_terminal = 1.0 - self._d[t]
            nxt = next_value if t == t_len - 1 else self._v[t + 1]
            delta = self._r[t] + g * nxt * non_terminal - self._v[t]
            last = delta + g * lam * non_terminal * last
            adv[t] = last
        returns = adv + self._v
        return adv.reshape(-1), returns.reshape(-1)


@torch.no_grad()
def evaluate_ppo_policy(
    policy: ActorCritic,
    sessions: list[ScalpSession],
    config: ScalpConfig,
    n_envs: int = 64,
    deterministic: bool = True,
    seed: int = 0,
    use_masks: bool = False,
) -> list[dict[str, Any]]:
    """Run ``policy`` once over every session (batched); returns episode summaries.

    Each summary also carries ``trips`` (round trips) and ``actions`` (counts per
    action of ``config.order_mode``).  ``use_masks`` restricts the argmax / sampling
    to legal actions (use it for checkpoints trained with masks).  Evaluation always
    runs the raw-PnL env: pass a config without shaping penalties (``pnl_ticks`` is
    raw regardless).
    """
    policy.eval()
    gen = torch.Generator().manual_seed(seed)
    n_envs = max(1, min(n_envs, len(sessions)))
    na = n_actions(config.order_mode)
    envs = [ScalpEnv(None, config) for _ in range(n_envs)]
    queue = list(range(len(sessions)))[::-1]
    obs = np.zeros((n_envs, obs_dim(config.features)), dtype=np.float32)
    active = np.zeros(n_envs, dtype=bool)
    counts = [np.zeros(na, dtype=np.int64) for _ in range(n_envs)]
    for i, env in enumerate(envs):
        if queue:
            obs[i], _ = env.reset(options={"session": sessions[queue.pop()]})
            active[i] = True
    out: list[dict[str, Any]] = []
    while active.any():
        idx = np.flatnonzero(active)
        logits, _ = policy(torch.as_tensor(obs[idx]))
        if use_masks:
            masks = np.stack([envs[i].action_mask() for i in idx])
            logits = apply_action_mask(logits, masks)
        if deterministic:
            acts = logits.argmax(dim=-1).numpy()
        else:
            acts = (
                torch.multinomial(torch.softmax(logits, dim=-1), 1, generator=gen)
                .squeeze(-1)
                .numpy()
            )
        for j, i in enumerate(idx):
            a = int(acts[j])
            counts[i][a] += 1
            o, _, term, _, info = envs[i].step(a)
            obs[i] = o
            if term:
                ep = dict(info["episode"])
                ep["trips"] = list(envs[i]._trips)  # noqa: SLF001
                ep["actions"] = counts[i]
                counts[i] = np.zeros(na, dtype=np.int64)
                out.append(ep)
                if queue:
                    obs[i], _ = envs[i].reset(
                        options={"session": sessions[queue.pop()]}
                    )
                else:
                    active[i] = False
    return out
