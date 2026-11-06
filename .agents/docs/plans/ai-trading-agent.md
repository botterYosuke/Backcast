# Implementation Plan: High-Speed RL Trading Agent (PPO) - First 15 Minutes Post-Open Specialist

## 1. Overview & Objective

This project builds a specialized, high-performance Reinforcement Learning (RL) trading agent using Proximal Policy Optimization (PPO) focused exclusively on the **first 15 minutes from when each symbol actually opens ($T_{\text{open}} \to T_{\text{open}} + 15\text{ min}$: 寄り付きから15分間特化)** using J-Quants tick data from Backcast.

### Why Dynamic $T_{\text{open}}$ Matters:
In Japanese equities (TSE), symbols do not all open at fixed 09:00:00 JST:
- Liquid, balanced stocks often open right at 09:00:00.
- Stocks with strong order imbalances trigger Special Quotes (特別気配 / Ren-kehai) and may open at 09:03, 09:08, or later.
- Fixing the window to calendar time (e.g. 09:00-09:15) would miss or distort the trading opportunity for delayed-open stocks.
- **Dynamic Session Definition**: The episode begins at the **exact timestamp of the symbol's first execution of the day ($T_{\text{open}}$)** and runs for exactly **15 minutes ($T_{\text{open}} + 900\text{ seconds}$)**.

### Core Advantages:
- **Maximum Volatility & Momentum**: The first 15 minutes post-open exhibit the highest price discovery, volatility, and volume intensity of the day.
- **Ultra-Fast Training Efficiency**: Episode length is strictly capped at 900 seconds of trading, eliminating ~85% of full-day tick noise and accelerating PPO iteration cycles by 10x+.
- **Zero Overnight & Low Intraday Drift**: All positions are opened and strictly liquidated within 15 minutes of the opening match.

### Core Architectural Decisions:
1. **Target Window**: Dynamic **$[T_{\text{open}}, T_{\text{open}} + 15\text{ min}]$** (900 seconds from the symbol's first trade). Mandatory square-off at $T_{\text{open}} + 15\text{ min}$.
2. **Execution Layer**: Dedicated Python backend for ultra-fast headless simulation directly reading DuckDB tick files.
3. **Strategy Paradigm**: PPO (Actor-Critic) trained on specialized post-open microstructure features (gap size, opening match volume, rolling range breakouts, volume velocity).
4. **Execution Model**: Market orders (immediate fills at current tick price with slippage penalty; day-credit single net position rules).
5. **Target Universe**: High-liquidity Japanese equities (starting with benchmark stem `7203` Toyota Motor, expandable to other liquid leaders).

---

## 2. Architecture & System Design

```
+-----------------------------------------------------------------------------------+
| Backcast Tick Storage (DuckDB: stocks_trades/<stem>.duckdb)                      |
+-----------------------------------------------------------------------------------+
                                         │
                                         ▼
+-----------------------------------------------------------------------------------+
| Data Pipeline (src/trading_rl/data.py)                                            |
|   - Bounded session loader: (Timestamp, Price, Qty, Type)                         |
|   - Fast array conversion & caching (NumPy / memory-mapped)                       |
+-----------------------------------------------------------------------------------+
                                         │
                                         ▼
+-----------------------------------------------------------------------------------+
| Feature Engine (src/trading_rl/features.py)                                       |
|   - Rolling tick returns, log-price normalized by rolling ATR/std                 |
|   - Trade velocity (ticks/sec, volume bursts)                                     |
|   - Buy/Sell trade aggressor skew                                                 |
|   - VWAP deviation & rolling moving average divergences                          |
+-----------------------------------------------------------------------------------+
                                         │
                                         ▼
+-----------------------------------------------------------------------------------+
| Gym Trading Environment (src/trading_rl/env.py: TickTradingEnv)                   |
|   - State: [Market Features (N-dim) + Portfolio State (Pos, PnL, Time Progress)]  |
|   - Action Space: Discrete(3) -> 0: Hold, 1: Long/Buy, 2: Short/Sell              |
|   - Transition: Day-trading margin rules, immediate fill + slippage               |
|   - Reward: Incremental PnL - Transaction Cost Penalty - Drawdown Penalty         |
+-----------------------------------------------------------------------------------+
                 │                                                ▲
                 ▼ Observation                                    │ Action
+-----------------------------------------------------------------------------------+
| PPO Agent & Training Loop (src/trading_rl/policy.py & trainer.py)                 |
|   - PyTorch Actor-Critic Network (MLP / 1D-CNN temporal feature extractor)        |
|   - Generalized Advantage Estimation (GAE) & clipped surrogate objective          |
|   - Vectorized rollouts across multiple historical trading days                   |
+-----------------------------------------------------------------------------------+
                                         │
                                         ▼
+-----------------------------------------------------------------------------------+
| Evaluation & Reporting (src/trading_rl/evaluator.py & scripts/backtest.py)        |
|   - Out-of-sample testing across bull, bear, and range market days               |
|   - Metrics: Total PnL, Win Rate, Profit Factor (PF), Max Drawdown, Sharpe Ratio  |
+-----------------------------------------------------------------------------------+
```

---

## 3. Detailed Component Specifications

### 3.1 Data Pipeline (`src/trading_rl/data.py`)
- Reads raw trades from `stocks_trades/<stem>.duckdb` via DuckDB connection pooling.
- Extracts chronological sessions: `epoch_us`, `price` (float64), `qty` (int64), `trade_type` (str/int).
- **Dynamic Opening Detection**:
  - Finds the timestamp $T_{\text{open}}$ of the first execution of the day (morning session $\ge \text{09:00:00}$ JST).
  - Handles stocks with Special Quotes (特別気配) whose $T_{\text{open}}$ is delayed (e.g. 09:03, 09:12).
  - Records $P_{\text{open}}$ and opening match volume $V_{\text{open}}$.
- **15-Minute Slice**: Strictly filters ticks to $T_{\text{open}} \le \text{Timestamp} \le T_{\text{open}} + 15\text{ minutes}$ ($T_{\text{open}} + 900 \times 10^6 \mu\text{s}$).
- Converts the 15-minute slice to contiguous NumPy structured arrays for zero-copy indexing during episode rollouts.

### 3.2 Feature Engineering (`src/trading_rl/features.py`)
To capture the dynamics of the first 15 minutes post-open without lookahead bias ($t' \le t$):
1. **Opening Context & Gap**:
   - Initial Opening Gap: $(P_{\text{open}} - P_{\text{prev\_close}}) / P_{\text{prev\_close}}$.
   - Current Return from Open: $(P_t - P_{\text{open}}) / P_{\text{open}}$.
   - Opening match volume ratio relative to historical average opening volume.
   - Opening delay: $(T_{\text{open}} - \text{09:00:00 JST})$ in seconds (encoding whether the stock opened immediately or had special quotes).
2. **Opening Range & Breakout Indicators**:
   - High/Low of the first 1-minute, 3-minute, and 5-minute bars formed after $T_{\text{open}}$.
   - Normalized distance to current session High and Low: $(P_t - \text{Low}_t) / (\text{High}_t - \text{Low}_t + \epsilon)$.
3. **Microstructure & Order Flow**:
   - Rolling tick return (10, 50, 200 ticks).
   - Rolling VWAP deviation: $(P_t - \text{VWAP}_t) / \text{TickSize}$.
   - Volume velocity: Ratio of recent 15-second volume to expected post-open volume.
   - Trade aggressor bias (Buy-initiated vs Sell-initiated trade proportion).
4. **Time & Urgency Decay**:
   - Elapsed window fraction $\tau = (t - T_{\text{open}}) / 900 \in [0, 1]$.
   - Exponentially increasing close penalty as $\tau \to 1.0$ (incentivizing closing trades cleanly before $T_{\text{open}} + 15\text{ min}$).

### 3.3 Gymnasium Environment (`src/trading_rl/env.py`)
- **Action Space**: `Discrete(3)`
  - `0 (HOLD)`: Maintain current position.
  - `1 (BUY)`:
    - If flat: Open 1-lot Long position.
    - If short: Cover short position to flat.
    - If already long: No-op.
  - `2 (SELL)`:
    - If flat: Open 1-lot Short position.
    - If long: Sell long position to flat.
    - If already short: No-op.
- **Observation Space**: `Box(shape=(D,), dtype=float32)`
  - Opening market feature vector (dimension $K$).
  - Current position status: $-1$ (Short), $0$ (Flat), $+1$ (Long).
  - Normalized unrealized return since entry: $(P_t - P_{\text{entry}}) / P_{\text{entry}} \times \text{position}$.
  - Normalized holding time in position: steps held / total episode steps.
  - Window time progress $\tau = (t - T_{\text{open}}) / 900 \in [0, 1]$.
- **Execution & Realism Model**:
  - Filled at current tick price $P_t$.
  - Fixed slippage penalty (e.g. 0.5 ~ 1.0 minimum tick increment per round trip).
  - **Mandatory 15-Minute Square-Off**: At the final tick before or at $T_{\text{open}} + 15\text{ min}$ ($\tau = 1.0$), any active position is forcibly liquidated at market with standard slippage, terminating the episode.
- **Reward Shaping**:
  $$R_t = \Delta \text{UnrealizedPnL}_t + \text{RealizedPnL}_t - c \cdot \mathbb{I}_{\text{trade}} - \lambda \cdot \max(0, \text{PeakPnL} - \text{PnL}_t) - \mu \cdot \mathbb{I}_{\text{forced\_square\_off}}$$
  Where $\mu$ penalizes being caught in a position at $T_{\text{open}} + 15\text{ min}$, training the agent to proactively take profits or cut losses before the 15-minute deadline.

### 3.4 PPO Model & Training Loop (`src/trading_rl/policy.py`, `trainer.py`)
- **Policy Network**:
  - Shared feature extractor: 2-layer MLP (or 1D-CNN over recent tick observation window) with SiLU/ReLU activations and LayerNorm.
  - Actor Head: Linear projection to action logits (3 actions) with categorical distribution.
  - Critic Head: Linear projection to scalar state value $V(s)$.
- **PPO Hyperparameters**:
  - Learning rate: $3 \times 10^{-4}$ with linear decay.
  - Rollout horizon: 2,048 ~ 8,192 steps per iteration.
  - Generalized Advantage Estimation ($\lambda = 0.95$, $\gamma = 0.99$).
  - PPO clip parameter: $\epsilon = 0.2$.
  - Entropy coefficient: $0.01$ (annealed) to maintain exploration.
  - Value loss coefficient: $0.5$.

### 3.5 Evaluation & Benchmark Metrics (`src/trading_rl/evaluator.py`)
- **Baseline Comparators**:
  - Random Walk Agent.
  - Buy-and-Hold Benchmark (passive holding across session).
  - Simple Microstructure Rule Baseline (e.g. VWAP Breakout / OBI momentum).
- **Core KPI Acceptance Targets**:
  - **Win Rate (勝率)**: $> 52\%$ on out-of-sample test sessions.
  - **Profit Factor (PF)**: $> 1.30$.
  - **Max Drawdown (MDD)**: Strictly controlled, avoiding catastrophic tail losses.
  - **Sharpe Ratio (annualized)**: $> 1.5$.

---

## 4. Implementation Steps & Milestones

### Step 1: Environment Setup & Project Dependencies
- Add PyTorch, Gymnasium, and Polars/NumPy to `pyproject.toml` (or dedicated optional dependency group `[project.optional-dependencies] rl = ["torch", "gymnasium", "polars"]`).
- Verify hardware acceleration (CUDA/MPS/CPU fallback).

### Step 2: High-Performance Data Extraction & Environment Core
- Implement `src/trading_rl/data.py` to extract dynamically bounded $[T_{\text{open}}, T_{\text{open}} + 15\text{ min}]$ tick sessions directly from DuckDB.
- Implement `src/trading_rl/env.py` conforming to standard `gymnasium.Env`.
- Create unit tests (`tests/test_trading_env.py`) verifying:
  - Exact PnL calculations match `paper-trading.mjs` rules.
  - Mandatory $T_{\text{open}} + 15\text{ min}$ square-off with penalty enforcement.
  - Correct handling of symbols with delayed opening times ($T_{\text{open}} > \text{09:00:00}$).
  - Slippage and cost deduction correctness.

### Step 3: Feature Engineering & Precomputation
- Implement `src/trading_rl/features.py` with opening-specific indicators (gap ratio, post-open range high/low, volume velocity, opening delay, relative time decay $\tau$).
- Ensure strict causality (zero lookahead bias) and numerical stability (handling zero-division and missing values).

### Step 4: PPO Policy & Vectorized Training Engine
- Implement `src/trading_rl/policy.py` (Actor-Critic architecture).
- Implement `src/trading_rl/trainer.py` (vectorized rollouts, GAE, PPO update epoch).
- Add checkpointing to save models into `.agents/checkpoints/rl/` or `models/`.

### Step 5: Benchmark & Strategy Validation on `7203` (Toyota)
- Train on training partition (e.g. 70% of historical sessions).
- Validate on held-out validation partition (15%).
- Final out-of-sample backtest on test partition (15%).
- Generate performance report, equity curve plots, and trade logs.

---

## 5. Verification & Acceptance Criteria

1. **Determinism & Reproducibility**: Given fixed random seeds, environment transitions and backtest metrics reproduce identically.
2. **Speed & Efficiency**: Simulation environment processes $\ge 50,000$ ticks/second on CPU, enabling rapid training iterations.
3. **Execution Safety**: 0 open positions held overnight; risk limits strictly enforced.
4. **Performance Target**: Positive net PnL and Profit Factor $> 1.2$ across unseen out-of-sample test trading days.
