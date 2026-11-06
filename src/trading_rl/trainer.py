"""Proximal Policy Optimization (PPO) trainer for tick trading environment."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import torch
import torch.nn as nn
import torch.optim as optim

from .env import TickTradingEnv
from .policy import ActorCritic


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
    ) -> None:
        self.env = env
        self.config = config or PPOConfig()
        self.device = torch.device(device)

        obs_dim = int(env.observation_space.shape[0])
        action_dim = int(env.action_space.n)

        if policy is None:
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

    def collect_rollouts(self) -> dict[str, float]:
        """Collect self.config.rollout_steps of interaction with the environment."""
        self.policy.eval()
        self.buffer.reset()

        ep_returns: list[float] = []
        current_ep_return = 0.0

        if self.last_obs is None or self.last_done:
            obs, _ = self.env.reset()
            self.last_obs = obs
            self.last_done = False

        for _ in range(self.config.rollout_steps):
            obs_tensor = (
                torch.as_tensor(self.last_obs, dtype=torch.float32)
                .unsqueeze(0)
                .to(self.device)
            )

            with torch.no_grad():
                action, logprob, _, value = self.policy.get_action_and_value(obs_tensor)

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

            current_ep_return += float(reward)
            self.total_timesteps += 1

            if done:
                ep_returns.append(current_ep_return)
                current_ep_return = 0.0
                obs, _ = self.env.reset()
                self.last_obs = obs
                self.last_done = True
            else:
                self.last_obs = next_obs
                self.last_done = False

        mean_ep_return = float(np.mean(ep_returns)) if ep_returns else 0.0
        return {
            "mean_ep_return": mean_ep_return,
            "episodes_completed": len(ep_returns),
        }

    def compute_gae(
        self,
        next_obs: np.ndarray,
        next_done: bool,
    ) -> tuple[np.ndarray, np.ndarray]:
        """Compute Generalized Advantage Estimation (GAE)."""
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
                next_non_terminal = 1.0 - self.buffer.dones[t + 1]
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

        n = len(self.buffer.obs)
        indices = np.arange(n)

        pg_losses: list[float] = []
        v_losses: list[float] = []
        entropies: list[float] = []
        approx_kls: list[float] = []

        for _ in range(self.config.update_epochs):
            np.random.shuffle(indices)
            for start in range(0, n, self.config.batch_size):
                end = start + self.config.batch_size
                batch_idx = indices[start:end]

                mb_obs = b_obs[batch_idx]
                mb_actions = b_actions[batch_idx]
                mb_logprobs = b_logprobs[batch_idx]
                mb_advantages = b_advantages[batch_idx]
                mb_returns = b_returns[batch_idx]

                _, newlogprob, entropy, newvalue = self.policy.get_action_and_value(
                    mb_obs, mb_actions
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

    def train_iteration(self) -> dict[str, float]:
        """Perform a single PPO iteration (rollout + GAE + optimization)."""
        rollout_info = self.collect_rollouts()

        assert self.last_obs is not None
        advantages, returns = self.compute_gae(self.last_obs, self.last_done)
        update_info = self.update(advantages, returns)

        metrics = {
            **rollout_info,
            **update_info,
            "total_timesteps": float(self.total_timesteps),
        }
        return metrics

    def train(self, total_timesteps: int) -> list[dict[str, float]]:
        """Run training until total_timesteps is reached."""
        logs: list[dict[str, float]] = []
        while self.total_timesteps < total_timesteps:
            info = self.train_iteration()
            logs.append(info)
        return logs

    def save(self, path: str | Path) -> None:
        """Save policy and optimizer state dicts along with config."""
        save_path = Path(path)
        save_path.parent.mkdir(parents=True, exist_ok=True)
        torch.save(
            {
                "policy_state_dict": self.policy.state_dict(),
                "optimizer_state_dict": self.optimizer.state_dict(),
                "obs_dim": self.policy.obs_dim,
                "action_dim": self.policy.action_dim,
                "config": self.config,
                "total_timesteps": self.total_timesteps,
            },
            save_path,
        )

    def load(self, path: str | Path) -> None:
        """Load weights from saved checkpoint."""
        checkpoint = torch.load(str(path), map_location=self.device, weights_only=False)
        self.policy.load_state_dict(checkpoint["policy_state_dict"])
        if "optimizer_state_dict" in checkpoint:
            self.optimizer.load_state_dict(checkpoint["optimizer_state_dict"])
        if "total_timesteps" in checkpoint:
            self.total_timesteps = checkpoint["total_timesteps"]
