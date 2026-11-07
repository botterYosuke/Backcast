"""Proximal Policy Optimization (PPO) trainer for tick trading environment."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import torch
import torch.nn as nn
import torch.optim as optim
from torch.distributions.categorical import Categorical

from .env import BASE_OBS_DIM, TickTradingEnv
from .policy import ActorCritic, state_dict_with_default_buffers


@dataclass
class PPOConfig:
    """Hyperparameters for PPO training."""

    learning_rate: float = 3e-4
    gamma: float = 0.99
    gae_lambda: float = 0.95
    clip_ratio: float = 0.2
    value_coef: float = 0.5
    entropy_coef: float = 0.01
    max_grad_norm: float = 0.5
    update_epochs: int = 4
    batch_size: int = 64
    rollout_steps: int = 1024
    anneal_lr: bool = False


def fit_obs_normalizer(env: TickTradingEnv) -> tuple[np.ndarray, np.ndarray]:
    """Fit (mean, std) observation statistics from flat-position observations.

    Uses every tradable step (tick at/after the warmup) of all env sessions.
    Base dims tau (1), position (2) and time_held (4) stay at mean 0 / std 1;
    unrealized_return (3) gets mean 0 and the std of price_return (0); price_return
    and the extra feature dims are z-scored. Any std < 1e-8 becomes 1.
    """
    chunks = [env.flat_observations(i) for i in range(env.num_sessions)]
    obs = np.concatenate(chunks, axis=0).astype(np.float64)
    if obs.shape[1] < BASE_OBS_DIM:
        raise ValueError("observation smaller than base layout")
    mean = obs.mean(axis=0)
    std = obs.std(axis=0)

    for dim in (1, 2, 4):  # tau, position, time_held
        mean[dim] = 0.0
        std[dim] = 1.0
    mean[3] = 0.0
    std[3] = std[0]
    std = np.where(std < 1e-8, 1.0, std)
    return mean.astype(np.float32), std.astype(np.float32)


class RolloutBuffer:
    """Fixed-capacity buffer storing experience collected during rollouts."""

    def __init__(self, capacity: int, obs_dim: int) -> None:
        self.capacity = capacity
        self.obs_dim = obs_dim
        self.reset()

    def reset(self) -> None:
        self.obs = np.zeros((self.capacity, self.obs_dim), dtype=np.float32)
        self.actions = np.zeros(self.capacity, dtype=np.int64)
        self.logprobs = np.zeros(self.capacity, dtype=np.float32)
        self.rewards = np.zeros(self.capacity, dtype=np.float32)
        self.values = np.zeros(self.capacity, dtype=np.float32)
        self.dones = np.zeros(self.capacity, dtype=np.float32)
        # Optional (capacity, action_dim) bool legal-action masks, set by trainers
        # that collect them; None = unmasked (the default / legacy path).
        self.masks: np.ndarray | None = None
        self.ptr = 0

    def add(
        self,
        obs: np.ndarray,
        action: int,
        logprob: float,
        reward: float,
        value: float,
        done: bool,
    ) -> None:
        if self.ptr >= self.capacity:
            raise IndexError("RolloutBuffer is full")
        self.obs[self.ptr] = obs
        self.actions[self.ptr] = action
        self.logprobs[self.ptr] = logprob
        self.rewards[self.ptr] = reward
        self.values[self.ptr] = value
        self.dones[self.ptr] = float(done)
        self.ptr += 1

    @property
    def is_full(self) -> bool:
        return self.ptr >= self.capacity


class PPOTrainer:
    """PPO algorithm implementation with GAE and clipped surrogate objective."""

    def __init__(
        self,
        env: TickTradingEnv,
        policy: ActorCritic | None = None,
        config: PPOConfig | None = None,
        device: str | torch.device = "cpu",
        seed: int | None = None,
    ) -> None:
        self.env = env
        self.config = config or PPOConfig()
        self.device = torch.device(device)
        self.seed = seed
        self._rng = np.random.default_rng(seed)
        # Private generator for action sampling: two trainers with the same seed
        # stay reproducible even if their runs interleave in one process.
        self._torch_gen = torch.Generator()
        if seed is not None:
            self._torch_gen.manual_seed(seed)

        obs_dim = int(env.observation_space.shape[0])
        action_dim = int(env.action_space.n)

        if policy is None:
            with torch.random.fork_rng(devices=[]):
                if seed is not None:
                    torch.manual_seed(seed)
                self.policy = ActorCritic(obs_dim=obs_dim, action_dim=action_dim).to(
                    self.device
                )
        else:
            self.policy = policy.to(self.device)

        self.optimizer = optim.Adam(
            self.policy.parameters(), lr=self.config.learning_rate, eps=1e-5
        )
        self.buffer = RolloutBuffer(self.config.rollout_steps, obs_dim)

        self.last_obs: np.ndarray | None = None
        self.last_done: bool = True
        self.total_timesteps: int = 0
        self._running_ep_return: float = 0.0
        self._first_reset_done: bool = False

    def _reset_env(self) -> np.ndarray:
        """Reset the env; the very first reset is seeded for reproducible sampling."""
        seed = None if self._first_reset_done else self.seed
        self._first_reset_done = True
        obs, _ = self.env.reset(seed=seed)
        return obs

    def collect_rollouts(self) -> dict[str, float]:
        """Collect self.config.rollout_steps of interaction with the environment."""
        self.policy.eval()
        self.buffer.reset()

        ep_returns: list[float] = []

        if self.last_obs is None:
            self.last_obs = self._reset_env()
            self.last_done = False
            self._running_ep_return = 0.0

        for _ in range(self.config.rollout_steps):
            obs_tensor = (
                torch.as_tensor(self.last_obs, dtype=torch.float32)
                .unsqueeze(0)
                .to(self.device)
            )

            with torch.no_grad():
                logits, value = self.policy(obs_tensor)
                dist = Categorical(logits=logits)
                action = torch.multinomial(
                    dist.probs, 1, generator=self._torch_gen
                ).squeeze(-1)
                logprob = dist.log_prob(action)

            act_int = int(action.cpu().item())
            logprob_float = float(logprob.cpu().item())
            value_float = float(value.cpu().item())

            next_obs, reward, terminated, truncated, _ = self.env.step(act_int)
            done = terminated or truncated

            self.buffer.add(
                obs=self.last_obs,
                action=act_int,
                logprob=logprob_float,
                reward=float(reward),
                value=value_float,
                done=done,
            )

            # The running episode return persists across rollout boundaries.
            self._running_ep_return += float(reward)
            self.total_timesteps += 1

            if done:
                ep_returns.append(self._running_ep_return)
                self._running_ep_return = 0.0
                self.last_obs = self._reset_env()
                self.last_done = True
            else:
                self.last_obs = next_obs
                self.last_done = False

        # NaN (not 0.0) when no episode finished inside this rollout.
        mean_ep_return = float(np.mean(ep_returns)) if ep_returns else float("nan")
        return {
            "mean_ep_return": mean_ep_return,
            "episodes_completed": float(len(ep_returns)),
            "running_ep_return": float(self._running_ep_return),
        }

    def compute_gae(
        self,
        next_obs: np.ndarray,
        next_done: bool,
    ) -> tuple[np.ndarray, np.ndarray]:
        """Compute Generalized Advantage Estimation (GAE).

        ``buffer.dones[t]`` is True when the transition taken at step ``t`` ended the
        episode, so the bootstrap term of step ``t`` is masked by ``dones[t]``.
        """
        n = self.buffer.capacity
        obs_tensor = (
            torch.as_tensor(next_obs, dtype=torch.float32).unsqueeze(0).to(self.device)
        )
        with torch.no_grad():
            next_value = float(self.policy.get_value(obs_tensor).cpu().item())

        advantages = np.zeros(n, dtype=np.float32)
        last_gae = 0.0

        for t in reversed(range(n)):
            if t == n - 1:
                next_non_terminal = 1.0 - float(next_done)
                next_val = next_value
            else:
                next_non_terminal = 1.0 - float(self.buffer.dones[t])
                next_val = self.buffer.values[t + 1]

            delta = (
                self.buffer.rewards[t]
                + self.config.gamma * next_val * next_non_terminal
                - self.buffer.values[t]
            )
            advantages[t] = last_gae = (
                delta
                + self.config.gamma
                * self.config.gae_lambda
                * next_non_terminal
                * last_gae
            )

        returns = advantages + self.buffer.values
        return advantages, returns

    def update(self, advantages: np.ndarray, returns: np.ndarray) -> dict[str, float]:
        """Run PPO optimization epochs over rollout data."""
        self.policy.train()

        # Advantage normalization
        norm_adv = (advantages - advantages.mean()) / (advantages.std() + 1e-8)

        b_obs = torch.as_tensor(self.buffer.obs, dtype=torch.float32).to(self.device)
        b_actions = torch.as_tensor(self.buffer.actions, dtype=torch.int64).to(
            self.device
        )
        b_logprobs = torch.as_tensor(self.buffer.logprobs, dtype=torch.float32).to(
            self.device
        )
        b_advantages = torch.as_tensor(norm_adv, dtype=torch.float32).to(self.device)
        b_returns = torch.as_tensor(returns, dtype=torch.float32).to(self.device)
        b_masks = (
            None
            if self.buffer.masks is None
            else torch.as_tensor(self.buffer.masks, dtype=torch.bool).to(self.device)
        )

        n = len(self.buffer.obs)
        indices = np.arange(n)

        pg_losses: list[float] = []
        v_losses: list[float] = []
        entropies: list[float] = []
        approx_kls: list[float] = []

        for _ in range(self.config.update_epochs):
            self._rng.shuffle(indices)
            for start in range(0, n, self.config.batch_size):
                end = start + self.config.batch_size
                batch_idx = indices[start:end]

                mb_obs = b_obs[batch_idx]
                mb_actions = b_actions[batch_idx]
                mb_logprobs = b_logprobs[batch_idx]
                mb_advantages = b_advantages[batch_idx]
                mb_returns = b_returns[batch_idx]

                # Stored masks keep the ratio consistent with the sampling policy.
                mask_kw = {} if b_masks is None else {"mask": b_masks[batch_idx]}
                _, newlogprob, entropy, newvalue = self.policy.get_action_and_value(
                    mb_obs, mb_actions, **mask_kw
                )

                logratio = newlogprob - mb_logprobs
                ratio = logratio.exp()

                # Approximate KL divergence for monitoring
                with torch.no_grad():
                    approx_kl = ((ratio - 1.0) - logratio).mean().item()
                    approx_kls.append(approx_kl)

                # Clipped surrogate objective
                surr1 = ratio * mb_advantages
                surr2 = (
                    torch.clamp(
                        ratio,
                        1.0 - self.config.clip_ratio,
                        1.0 + self.config.clip_ratio,
                    )
                    * mb_advantages
                )
                policy_loss = -torch.min(surr1, surr2).mean()

                # Value loss (MSE)
                value_loss = 0.5 * ((newvalue - mb_returns) ** 2).mean()

                # Entropy bonus
                entropy_loss = entropy.mean()

                loss = (
                    policy_loss
                    - self.config.entropy_coef * entropy_loss
                    + self.config.value_coef * value_loss
                )

                self.optimizer.zero_grad()
                loss.backward()
                nn.utils.clip_grad_norm_(
                    self.policy.parameters(), self.config.max_grad_norm
                )
                self.optimizer.step()

                pg_losses.append(policy_loss.item())
                v_losses.append(value_loss.item())
                entropies.append(entropy_loss.item())

        return {
            "policy_loss": float(np.mean(pg_losses)),
            "value_loss": float(np.mean(v_losses)),
            "entropy": float(np.mean(entropies)),
            "approx_kl": float(np.mean(approx_kls)),
        }

    def train_iteration(self, progress: float | None = None) -> dict[str, float]:
        """Perform a single PPO iteration (rollout + GAE + optimization).

        Args:
            progress: Training progress in [0, 1]. With ``config.anneal_lr`` the
                learning rate decays linearly: ``lr * (1 - progress)``.
        """
        if self.config.anneal_lr and progress is not None:
            frac = min(1.0, max(0.0, float(progress)))
            lr_now = self.config.learning_rate * (1.0 - frac)
            for group in self.optimizer.param_groups:
                group["lr"] = lr_now

        rollout_info = self.collect_rollouts()

        assert self.last_obs is not None
        advantages, returns = self.compute_gae(self.last_obs, self.last_done)
        update_info = self.update(advantages, returns)

        metrics = {
            **rollout_info,
            **update_info,
            "total_timesteps": float(self.total_timesteps),
            "learning_rate": float(self.optimizer.param_groups[0]["lr"]),
        }
        return metrics

    def train(self, total_timesteps: int) -> list[dict[str, float]]:
        """Run training until total_timesteps is reached."""
        logs: list[dict[str, float]] = []
        start = self.total_timesteps
        span = max(1, total_timesteps - start)
        while self.total_timesteps < total_timesteps:
            progress = (self.total_timesteps - start) / span
            logs.append(self.train_iteration(progress=progress))
        return logs

    def save(self, path: str | Path, metadata: dict[str, Any] | None = None) -> None:
        """Save policy and optimizer state dicts along with config.

        ``metadata`` (window_seconds, opening_warmup_seconds, slippage,
        feature_names, ...) is stored under ``"env_config"`` so replay can rebuild
        the exact environment the policy was trained on.
        """
        save_path = Path(path)
        save_path.parent.mkdir(parents=True, exist_ok=True)
        torch.save(
            {
                "policy_state_dict": self.policy.state_dict(),
                "optimizer_state_dict": self.optimizer.state_dict(),
                "obs_dim": self.policy.obs_dim,
                "action_dim": self.policy.action_dim,
                "hidden_dim": self.policy.hidden_dim,
                "env_config": dict(metadata) if metadata is not None else None,
                "config": self.config,
                "total_timesteps": self.total_timesteps,
            },
            save_path,
        )

    def load(self, path: str | Path) -> None:
        """Load weights from saved checkpoint (old checkpoints without obs buffers ok)."""
        checkpoint = torch.load(str(path), map_location=self.device, weights_only=False)
        self.policy.load_state_dict(
            state_dict_with_default_buffers(
                self.policy, checkpoint["policy_state_dict"]
            )
        )
        if "optimizer_state_dict" in checkpoint:
            self.optimizer.load_state_dict(checkpoint["optimizer_state_dict"])
        if "total_timesteps" in checkpoint:
            self.total_timesteps = checkpoint["total_timesteps"]
