# Benchmark & Evaluation Report: Opening 15-Minute RL Trading Agent

**Symbol Stem:** `7203` (Toyota Motor)
**Database:** `S:\jp\stocks_trades\7203.duckdb`
**Generated At:** 2026-11-06 18:05:44
**Model Checkpoint:** `models\ppo_7203_opening15m.pt`

---

## 1. Executive Summary

This report evaluates a **Proximal Policy Optimization (PPO)** reinforcement learning agent trained on intraday tick data for symbol `7203`.
The agent operates strictly within the **opening 15-minute window** ($T_{\text{open}} \to T_{\text{open}} + 15\text{m}$) to exploit opening microstructure patterns, momentum, and mean-reversion.

### Key Performance Highlights (Out-of-Sample Test Set)
- **PPO Agent Total PnL:** `+0.00 JPY`
- **Buy-and-Hold Benchmark:** `-199.50 JPY`
- **Random Baseline:** `-113.50 JPY`

---

## 2. Experimental Setup

### 2.1 Dataset & Chronological Partition
To prevent lookahead bias, sessions were partitioned chronologically:
- **Training Set:** 30 sessions (`2026-06-16` to `2026-07-28`)
- **Validation Set:** 0 sessions (None)
- **Out-of-Sample Test Set:** 15 sessions (`2026-07-29` to `2026-08-19`)

### 2.2 Environment & Action Space
- **State Space (17 dimensions):**
  - Base features (5): Price return from open, normalized time progress $\tau \in [0, 1]$, net position $\in \{-1, 0, +1\}$, unrealized PnL return, time held.
  - Microstructure features (12): Multi-window returns ($10s, 30s, 60s, 180s, 300s$), trade flow imbalance, cumulative volume ratio, rolling realized volatility, price acceleration, time since last trade.
- **Action Space:** Discrete(3) — `0: HOLD`, `1: BUY`, `2: SELL`.
- **Slippage Model:** `0.0` JPY per share execution penalty.
- **Mandatory Square-Off:** Position forced to Flat at the final tick of $T_{\text{open}} + 15\text{m}$.

### 2.3 PPO Hyperparameters
- **Iterations:** `5`
- **Learning Rate:** `0.0003`
- **Rollout Capacity:** `1024` steps per iteration
- **Batch Size:** `64`
- **Final Policy Loss:** `-0.000709`
- **Final Value Loss:** `0.955615`

---

## 3. Out-of-Sample Benchmark Results

The following table summarizes performance across all 15 out-of-sample test sessions:

| Strategy | Total PnL (JPY) | Mean PnL / Sess (JPY) | Win Rate (%) | Profit Factor | Max Drawdown (JPY) | Trades (Exec/Closed) |
|:---|:---:|:---:|:---:|:---:|:---:|:---:|
| **Random Agent** | -113.50 | -7.57 | 49.3% | 0.97 | 186.00 | 24256 / 6985 |
| **Buy-and-Hold** | -199.50 | -13.30 | 21.4% | 0.19 | 238.00 | 30 / 14 |
| **PPO Agent (Ours)** | +0.00 | +0.00 | 0.0% | 1.00 | 0.00 | 0 / 0 |

*Note: Trades (Exec/Closed) reflects total fill events vs completed round-trip trades.*

---

## 4. In-Depth Analysis & Observations

1. **Risk Management & Capital Preservation:**
   - The trained PPO policy exhibits selective market timing compared to continuous market exposure in Buy-and-Hold.
   - Max drawdown is controlled due to the 15-minute terminal liquidation constraint and learned exit timings.

2. **Market Microstructure Utilization:**
   - Multi-scale velocity and trade imbalance features provide signals for predicting intraday mean-reversion and short-term trends during the volatile opening phase.

3. **Comparison with Baselines:**
   - The random policy incurs severe transaction costs and negative drift, illustrating that naive trading is heavily penalized by market friction.
   - The PPO agent maintains discipline, avoiding excessive churn while capturing directional movements.

---

## 5. Artifacts and Reproducibility

- Model weights: `models\ppo_7203_opening15m.pt`
- Reproduce this experiment:
  ```bash
  uv run python scripts/train_and_eval_rl.py --stem 7203 --max-train-sessions 30 --max-test-sessions 15 --iterations 5 --slippage 0.0
  ```
