"""Actor-Critic policy network for discrete action trading (HOLD, BUY, SELL)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import torch
import torch.nn as nn
from torch.distributions.categorical import Categorical


def layer_init(
    layer: nn.Linear, std: float = np.sqrt(2), bias_const: float = 0.0
) -> nn.Linear:
    """Orthogonal weight initialization for stable policy gradients."""
    nn.init.orthogonal_(layer.weight, std)
    nn.init.constant_(layer.bias, bias_const)
    return layer


OBS_CLIP: float = 10.0
MASK_FILL: float = -1e9  # logit of an illegal action (finite: keeps grads NaN-free)


def apply_action_mask(
    logits: torch.Tensor, mask: torch.Tensor | np.ndarray | None
) -> torch.Tensor:
    """Set the logits of illegal actions (``mask`` False) to a large negative value.

    ``mask=None`` returns ``logits`` unchanged (unmasked / legacy path).  A row must
    have at least one legal action.
    """
    if mask is None:
        return logits
    m = torch.as_tensor(mask, dtype=torch.bool, device=logits.device)
    return logits.masked_fill(~m, MASK_FILL)


class ActorCritic(nn.Module):
    """Actor-Critic network with shared MLP representation for PPO.

    Observation normalization lives inside the model (buffers ``obs_mean`` /
    ``obs_std``, identity by default) so checkpoints are self-contained: every
    forward path applies ``clip((obs - mean) / std, -10, 10)``.
    """

    def __init__(
        self,
        obs_dim: int,
        action_dim: int = 3,
        hidden_dim: int = 64,
        activation: str = "silu",
    ) -> None:
        super().__init__()
        self.obs_dim = obs_dim
        self.action_dim = action_dim
        self.hidden_dim = hidden_dim

        act_layer = nn.SiLU if activation.lower() == "silu" else nn.Tanh

        self.feature_extractor = nn.Sequential(
            layer_init(nn.Linear(obs_dim, hidden_dim)),
            nn.LayerNorm(hidden_dim),
            act_layer(),
            layer_init(nn.Linear(hidden_dim, hidden_dim)),
            nn.LayerNorm(hidden_dim),
            act_layer(),
        )

        self.actor = layer_init(nn.Linear(hidden_dim, action_dim), std=0.01)
        self.critic = layer_init(nn.Linear(hidden_dim, 1), std=1.0)

        self.register_buffer("obs_mean", torch.zeros(obs_dim))
        self.register_buffer("obs_std", torch.ones(obs_dim))

    def set_obs_normalizer(
        self,
        mean: np.ndarray | torch.Tensor,
        std: np.ndarray | torch.Tensor,
    ) -> None:
        """Install observation normalization statistics (std < 1e-8 -> 1)."""
        mean_t = torch.as_tensor(np.asarray(mean), dtype=torch.float32).reshape(-1)
        std_t = torch.as_tensor(np.asarray(std), dtype=torch.float32).reshape(-1)
        if mean_t.numel() != self.obs_dim or std_t.numel() != self.obs_dim:
            raise ValueError(
                f"normalizer must have {self.obs_dim} dims, got "
                f"{mean_t.numel()} / {std_t.numel()}"
            )
        std_t = torch.where(std_t < 1e-8, torch.ones_like(std_t), std_t)
        self.obs_mean.copy_(mean_t.to(self.obs_mean.device))
        self.obs_std.copy_(std_t.to(self.obs_std.device))

    def _normalize(self, obs: torch.Tensor) -> torch.Tensor:
        return torch.clamp((obs - self.obs_mean) / self.obs_std, -OBS_CLIP, OBS_CLIP)

    def forward(self, obs: torch.Tensor) -> tuple[torch.Tensor, torch.Tensor]:
        """Compute action logits and state value estimate."""
        features = self.feature_extractor(self._normalize(obs))
        logits = self.actor(features)
        value = self.critic(features).squeeze(-1)
        return logits, value

    def get_value(self, obs: torch.Tensor) -> torch.Tensor:
        """Estimate state value V(s)."""
        features = self.feature_extractor(self._normalize(obs))
        return self.critic(features).squeeze(-1)

    def get_action_and_value(
        self,
        obs: torch.Tensor,
        action: torch.Tensor | None = None,
        mask: torch.Tensor | np.ndarray | None = None,
    ) -> tuple[torch.Tensor, torch.Tensor, torch.Tensor, torch.Tensor]:
        """Compute action, log_prob, entropy, and value.

        Args:
            obs: Observation tensor of shape (batch, obs_dim) or (obs_dim,).
            action: Optional ground truth action tensor of shape (batch,).
            mask: Optional bool tensor (batch, action_dim), True = legal.  Illegal
                actions get probability 0 in sampling, log-prob and entropy.

        Returns:
            Tuple of (action, log_prob, entropy, value).
        """
        features = self.feature_extractor(self._normalize(obs))
        logits = apply_action_mask(self.actor(features), mask)
        dist = Categorical(logits=logits)
        if action is None:
            action = dist.sample()
        value = self.critic(features).squeeze(-1)
        return action, dist.log_prob(action), dist.entropy(), value

    @torch.no_grad()
    def predict(
        self,
        obs: np.ndarray | torch.Tensor,
        deterministic: bool = True,
        mask: np.ndarray | torch.Tensor | None = None,
    ) -> int:
        """Select action for a single observation (useful for environment evaluation).

        With ``mask`` (True = legal) only legal actions can be returned.
        """
        self.eval()
        if isinstance(obs, np.ndarray):
            obs_tensor = torch.as_tensor(obs, dtype=torch.float32)
        else:
            obs_tensor = obs.float()

        if obs_tensor.dim() == 1:
            obs_tensor = obs_tensor.unsqueeze(0)

        obs_tensor = obs_tensor.to(self.obs_mean.device)
        features = self.feature_extractor(self._normalize(obs_tensor))
        logits = self.actor(features)
        if mask is not None:
            logits = apply_action_mask(logits, torch.as_tensor(mask).reshape(1, -1))
        if deterministic:
            action = torch.argmax(logits, dim=-1)
        else:
            action = Categorical(logits=logits).sample()
        return int(action.item())


def state_dict_with_default_buffers(
    policy: ActorCritic, state_dict: dict[str, Any]
) -> dict[str, Any]:
    """Fill ``obs_mean`` / ``obs_std`` (identity) when an old checkpoint lacks them."""
    fixed = dict(state_dict)
    if "obs_mean" not in fixed:
        fixed["obs_mean"] = torch.zeros(policy.obs_dim)
    if "obs_std" not in fixed:
        fixed["obs_std"] = torch.ones(policy.obs_dim)
    return fixed


def load_policy_from_checkpoint(
    path: str | Path, map_location: str | torch.device = "cpu"
) -> tuple[ActorCritic, dict[str, Any]]:
    """Load ``(policy, checkpoint)`` from a PPOTrainer checkpoint.

    Tolerates old checkpoints without observation-normalization buffers
    (identity normalization) and without stored ``hidden_dim`` (inferred from
    the first layer weights).
    """
    checkpoint = torch.load(str(path), weights_only=False, map_location=map_location)
    state = checkpoint["policy_state_dict"]
    first_w = state["feature_extractor.0.weight"]
    hidden_dim = int(checkpoint.get("hidden_dim", first_w.shape[0]))
    obs_dim = int(checkpoint.get("obs_dim", first_w.shape[1]))
    action_dim = int(checkpoint.get("action_dim", state["actor.weight"].shape[0]))
    policy = ActorCritic(obs_dim=obs_dim, action_dim=action_dim, hidden_dim=hidden_dim)
    policy.load_state_dict(state_dict_with_default_buffers(policy, state))
    policy.to(map_location)
    policy.eval()
    return policy, checkpoint
