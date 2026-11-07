# Benchmark & Evaluation Report: Opening 600s RL Trading Agent

**Symbol Stem:** `7203`  
**Database:** `S:\jp\stocks_trades\7203.duckdb`  
**Generated At:** 2026-11-06 19:58:45  
**Model Checkpoint:** `models\ppo_7203_opening10m.pt`

## 1. Setup

- Trading window: T_open to T_open + 600s
- Warmup (no orders): first 15s after T_open
- Slippage: 0.5 JPY per fill; PnL is per 1 share
- Observation dim: 18 (5 base + 13 features)
- Features: `tau`, `opening_delay_norm`, `return_from_open`, `cum_pos_in_range`, `roll_pos_in_range_20`, `log_ret_5`, `log_ret_20`, `log_ret_100`, `cum_vwap_dev`, `roll_vwap_dev_20`, `log_roll_vol_20`, `log_vol_rate_20`, `tick_rule_skew_20`
- Seed: 42
- Train: 100 sessions (`2026-01-30` to `2026-06-29`)
- Validation: 15 sessions (`2026-06-30` to `2026-07-21`)
- Test: 20 sessions (`2026-07-22` to `2026-08-19`)

## 2. PPO Training

- Iterations: 245 (1003520 timesteps)
- Training throughput: 1358 env steps/sec (rollout + update)

Hyperparameters:

- `learning_rate`: `0.0003`
- `gamma`: `0.99`
- `gae_lambda`: `0.95`
- `clip_ratio`: `0.2`
- `value_coef`: `0.5`
- `entropy_coef`: `0.01`
- `max_grad_norm`: `0.5`
- `update_epochs`: `4`
- `batch_size`: `256`
- `rollout_steps`: `4096`
- `anneal_lr`: `False`

Selected checkpoint: **iteration 110** (best validation total PnL +131.00 JPY with 20 trades; ties broken by fewer trades).

Validation during training:

| Iter | Timesteps | Val PnL (JPY) | Val Trades | Mean Ep Return | Entropy | KL |
|:---:|:---:|:---:|:---:|:---:|:---:|:---:|
| 10 | 40960 | +0.00 | 0 | -117.0000 | 0.5672 | 0.00412 |
| 20 | 81920 | +0.00 | 0 | -23.0000 | 0.2616 | 0.00066 |
| 30 | 122880 | +0.00 | 0 | +2.0000 | 0.2242 | 0.00037 |
| 40 | 163840 | +0.00 | 0 | -12.0000 | 0.1500 | 0.00009 |
| 50 | 204800 | +0.00 | 0 | +34.0000 | 0.1755 | 0.00035 |
| 60 | 245760 | -7.50 | 2 | +32.5000 | 0.1235 | 0.00025 |
| 70 | 286720 | +15.00 | 10 | +12.5000 | 0.1573 | 0.00079 |
| 80 | 327680 | -11.00 | 12 | +37.0000 | 0.1351 | 0.00042 |
| 90 | 368640 | -22.00 | 4 | -13.2500 | 0.2464 | 0.00056 |
| 100 | 409600 | +18.00 | 8 | +10.5000 | 0.2559 | 0.00050 |
| 110 | 450560 | +131.00 | 20 | -15.5000 | 0.5682 | 0.00110 |
| 120 | 491520 | +30.50 | 18 | -19.6667 | 0.4173 | 0.00220 |
| 130 | 532480 | +91.50 | 26 | -13.5000 | 0.5866 | 0.00235 |
| 140 | 573440 | +108.50 | 26 | +1.7500 | 0.2555 | 0.00069 |
| 150 | 614400 | +94.00 | 26 | +21.0000 | 0.0401 | 0.00020 |
| 160 | 655360 | +93.50 | 26 | -4.3333 | 0.3351 | 0.00171 |
| 170 | 696320 | +78.50 | 24 | -31.0000 | 0.1107 | 0.00150 |
| 180 | 737280 | +69.50 | 26 | +12.2500 | 0.1463 | 0.00367 |
| 190 | 778240 | +104.50 | 26 | -3.0000 | 0.3873 | 0.00247 |
| 200 | 819200 | +121.00 | 24 | +3.0000 | 0.3017 | 0.00160 |
| 210 | 860160 | +96.50 | 26 | +8.5000 | 0.1396 | 0.00083 |
| 220 | 901120 | +103.50 | 26 | -9.1667 | 0.3784 | 0.00320 |
| 230 | 942080 | +69.00 | 26 | -0.3333 | 0.2376 | 0.00175 |
| 240 | 983040 | +9.50 | 22 | +2.2500 | 0.0761 | 0.00029 |
| 245 | 1003520 | +20.50 | 24 | -33.5000 | 0.2087 | 0.00144 |

## 3. Validation Results (best model)

| Strategy | Total PnL (JPY) | Mean PnL / Sess (JPY) | Win Rate (%) | Profit Factor | Max Drawdown (JPY) | Trades (Exec/Closed) |
|:---|:---:|:---:|:---:|:---:|:---:|:---:|
| **Random Agent** | -7410.00 | -494.00 | 4.9% | 0.04 | 7410.00 | 14828 / 7414 |
| **Buy-and-Hold** | +16.00 | +1.07 | 53.3% | 1.10 | 71.00 | 30 / 15 |
| **PPO Agent (Ours)** | +131.00 | +8.73 | 60.0% | 3.98 | 32.50 | 20 / 10 |

- PPO の合計 PnL (+131.00) は Buy-and-Hold (+16.00) を上回る。
- PPO の約定時刻は T_open+15.569s から T_open+600.000s (許容 [15s, 600s]) で許容範囲内。

## 4. Out-of-Sample Test Results (best model)

| Strategy | Total PnL (JPY) | Mean PnL / Sess (JPY) | Win Rate (%) | Profit Factor | Max Drawdown (JPY) | Trades (Exec/Closed) |
|:---|:---:|:---:|:---:|:---:|:---:|:---:|
| **Random Agent** | -11050.50 | -552.52 | 4.2% | 0.04 | 11050.50 | 21978 / 10989 |
| **Buy-and-Hold** | -146.50 | -7.33 | 40.0% | 0.25 | 154.00 | 40 / 20 |
| **PPO Agent (Ours)** | -22.00 | -1.10 | 50.0% | 0.67 | 59.00 | 28 / 14 |

Trades (Exec/Closed): fill events / closed round trips.

Notes:

- PPO の合計 PnL (-22.00) は Buy-and-Hold (-146.50) を上回る。
- PPO の約定時刻は T_open+15.233s から T_open+599.996s (許容 [15s, 600s]) で許容範囲内。

## 5. Reproduce

```bash
uv run python scripts/train_and_eval_rl.py --stem 7203 --max-train-sessions 100 --max-val-sessions 15 --max-test-sessions 20 --window-seconds 600 --warmup-seconds 15 --slippage 0.5 --rollout-steps 4096 --batch-size 256 --eval-every 10 --learning-rate 0.0003 --total-timesteps 1000000 --seed 42
```
