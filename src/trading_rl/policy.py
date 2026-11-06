"""Actor-Critic policy network for discrete action trading (HOLD, BUY, SELL)."""

from __future__ import annotations

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


class ActorCritic(nn.Module):
    """Actor-Critic network with shared MLP representation for PPO."""

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

    def forward(self, obs: torch.Tensor) -> tuple[torch.Tensor, torch.Tensor]:
        """Compute action logits and state value estimate."""
        features = self.feature_extractor(obs)
        logits = self.actor(features)
        value = self.critic(features).squeeze(-1)
        return logits, value

    def get_value(self, obs: torch.Tensor) -> torch.Tensor:
        """Estimate state value V(s)."""
        features = self.feature_extractor(obs)
        return self.critic(features).squeeze(-1)

    def get_action_and_value(
        self,
        obs: torch.Tensor,
        action: torch.Tensor | None = None,
    ) -> tuple[torch.Tensor, torch.Tensor, torch.Tensor, torch.Tensor]:
        """Compute action, log_prob, entropy, and value.

        Args:
            obs: Observation tensor of shape (batch, obs_dim) or (obs_dim,).
            action: Optional ground truth action tensor of shape (batch,).

        Returns:
            Tuple of (action, log_prob, entropy, value).
        """
        features = self.feature_extractor(obs)
        logits = self.actor(features)
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
    ) -> int:
        """Select action for a single observation (useful for environment evaluation)."""
        self.eval()
        if isinstance(obs, np.ndarray):
            obs_tensor = torch.as_tensor(obs, dtype=torch.float32)
        else:
            obs_tensor = obs.float()

        if obs_tensor.dim() == 1:
            obs_tensor = obs_tensor.unsqueeze(0)

        features = self.feature_extractor(obs_tensor)
        logits = self.actor(features)
        if deterministic:
            action = torch.argmax(logits, dim=-1)
        else:
            action = Categorical(logits=logits).sample()
        return int(action.item())
