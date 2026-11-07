# Top-50 opening-window predictability gate (P2)

- Run: 2026-11-06 22:04 / mode: **full run** / runtime 7.0 min (data+features 1.0 min)
- Command args: seed=0, train stride=5 s, max train rows=2,500,000, train day fraction=1.0, bootstrap reps=2000

## Verdict

- h=30s: **FAIL** (model logreg, theta=0.2; test seen daily mean -14.6 bps CI [-50.5, +20.8], held-out -0.4 bps CI [-17.2, +14.8])
- h=60s: **FAIL** (model logreg, theta=0.15; test seen daily mean +21.5 bps CI [-14.7, +59.8], held-out -2.3 bps CI [-21.5, +15.8])
- h=300s: **FAIL** (model gbm, theta=0.1; test seen daily mean -40.2 bps CI [-128.6, +53.3], held-out -48.7 bps CI [-98.9, -0.0])

Overall: **not all horizons pass** (30s=F, 60s=F, 300s=F). PASS rule: test mean daily PnL (bps, 1-tick round-trip cost deducted) 95% day-bootstrap CI lower bound > 0 for BOTH seen and held-out stems, with the model and theta chosen on validation only.

## Data coverage

| split | range | universe rows used | ok sessions | days | stems | skipped by status |
|---|---|---|---|---|---|---|
| train | 2024-01-04..2025-06-30 | 15064 (+3136 held-out-stem rows excluded) | 15016 | 364 | 270 | no_morning_trade=10, not_cached=38 |
| val | 2025-07-01..2025-12-31 | 4959 (+1241 held-out-stem rows excluded) | 4936 | 124 | 197 | no_data=2, not_cached=21 |
| test | 2026-01-01..2026-08-19 | 7650 | 7608 | 153 | 214 | no_data=1, no_morning_trade=11, not_cached=30 |

- Held-out stems (md5(stem)[0] % 5 == 0, excluded from train/val): 37 of 214 stems seen in test; test sessions: seen=5970, held-out=1638.
- Train grid: 15016 sessions subsampled every 5 s -> 1,684,864 rows (models fit on at most 2,500,000 non-tie rows per horizon). Validation and test use the full 1 s grid.

## Features (all causal; ticks <= t plus pre-open universe columns)

`ret_bps_1s`, `ret_bps_5s`, `ret_bps_15s`, `ret_bps_30s`, `ret_bps_60s`, `ret_tk_1s`, `ret_tk_5s`, `ret_tk_15s`, `ret_tk_30s`, `ret_tk_60s`, `ret_open_bps`, `gap_bps`, `range_pos`, `vwap_dev_bps`, `imb_5s`, `imb_30s`, `imb_60s`, `log_ntrades_5s`, `log_ntrades_30s`, `log_vol_ratio_5s`, `log_vol_ratio_30s`, `rv_bps_30s`, `rv_bps_60s`, `elapsed_s`, `log_open_vol_ratio`, `log_time_since_trade`, `is_topix100`, `log_price_ticks` (28 features; decision grid 1 s from T_open+10s to T_open+600s-h; targets: last-trade forward return over h in bps/ticks and its sign. Tie (zero-return) rows are dropped from classifier training and AUC/accuracy/Brier but kept in the economic test).

## Results per horizon and model

Classification on non-tie rows (test, seen / held-out stems):

| h | model | AUC seen | AUC held | acc seen | base seen | acc held | base held | Brier seen (const) | tie frac |
|---|---|---|---|---|---|---|---|---|---|
| 30 | logreg | 0.5105 | 0.5068 | 0.5079 | 0.5011 | 0.5063 | 0.5014 | 0.2501 (0.2500) | 0.057 |
| 30 | gbm | 0.5138 | 0.5082 | 0.5088 | 0.5011 | 0.5056 | 0.5014 | 0.2499 (0.2500) | 0.057 |
| 60 | logreg | 0.4996 | 0.4979 | 0.5015 | 0.5013 | 0.5005 | 0.5011 | 0.2502 (0.2500) | 0.041 |
| 60 | gbm | 0.5037 | 0.5025 | 0.5029 | 0.5013 | 0.5016 | 0.5011 | 0.2505 (0.2500) | 0.041 |
| 300 | logreg | 0.5003 | 0.4987 | 0.5047 | 0.5086 | 0.5077 | 0.5163 | 0.2503 (0.2499) | 0.018 |
| 300 | gbm | 0.4972 | 0.4958 | 0.4998 | 0.5086 | 0.5001 | 0.5163 | 0.2523 (0.2499) | 0.018 |

Economics (one position per stem-day, hold h s, 1-tick round trip; theta from validation; daily = sum over that day's trades of the group, mean over test days, 95% CI by day bootstrap):

| h | model | theta | group | trades/day | bps/trade | ticks/trade | win rate | daily mean bps | CI | always-long daily bps (CI) | random-dir daily bps (CI) |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 30 | logreg* | 0.2 | seen | 2.2 | -6.61 | +0.289 | 0.415 | -14.6 | [-50.5, +20.8] | -16.1 [-44.3, +12.2] | -42.2 [-79.0, -9.9] |
| 30 | logreg* | 0.2 | heldout | 0.7 | -0.62 | -2.198 | 0.500 | -0.4 | [-17.2, +14.8] | +11.1 [-2.8, +27.1] | +0.2 [-12.3, +13.0] |
| 30 | gbm | 0.2 | seen | 7.4 | -2.47 | -0.396 | 0.229 | -18.3 | [-22.4, -14.2] | -34.4 [-40.2, -28.7] | -48.4 [-56.3, -41.1] |
| 30 | gbm | 0.2 | heldout | 0.0 | +0.00 | +0.000 | 0.000 | +0.0 | [+0.0, +0.0] | +0.0 [+0.0, +0.0] | +0.0 [+0.0, +0.0] |
| 60 | logreg* | 0.15 | seen | 1.7 | +12.77 | +0.104 | 0.484 | +21.5 | [-14.7, +59.8] | +12.9 [-17.3, +44.2] | -54.7 [-95.8, -15.9] |
| 60 | logreg* | 0.15 | heldout | 0.6 | -3.65 | -1.501 | 0.423 | -2.3 | [-21.5, +15.8] | +25.8 [+4.6, +51.0] | -12.9 [-37.4, +9.4] |
| 60 | gbm | 0.15 | seen | 2.7 | -1.59 | -0.370 | 0.296 | -4.2 | [-28.0, +21.4] | -6.9 [-29.8, +19.3] | -18.3 [-36.0, -1.4] |
| 60 | gbm | 0.15 | heldout | 0.3 | +23.20 | +13.320 | 0.549 | +7.7 | [-2.5, +18.8] | +0.3 [-10.1, +11.1] | -2.3 [-14.9, +8.3] |
| 300 | logreg | 0.075 | seen | 3.8 | +5.31 | -0.662 | 0.501 | +20.5 | [-54.8, +94.7] | -60.8 [-143.9, +21.8] | +0.2 [-65.8, +71.5] |
| 300 | logreg | 0.075 | heldout | 1.6 | -23.13 | +1.542 | 0.456 | -38.1 | [-85.3, +7.7] | +57.2 [+12.5, +101.2] | +8.8 [-41.1, +57.0] |
| 300 | gbm* | 0.1 | seen | 10.2 | -3.95 | -1.112 | 0.466 | -40.2 | [-128.6, +53.3] | -60.7 [-162.6, +41.4] | -57.5 [-131.4, +14.3] |
| 300 | gbm* | 0.1 | heldout | 3.5 | -14.04 | -1.986 | 0.492 | -48.7 | [-98.9, -0.0] | +1.5 [-51.8, +52.5] | -60.3 [-104.8, -13.3] |

`*` = model chosen on validation daily-mean bps (seen stems). Validation figures at the chosen theta:

| h | model | theta | val trades/day | val bps/trade | val daily mean bps | val AUC | val acc (base) |
|---|---|---|---|---|---|---|---|
| 30 | logreg | 0.2 | 2.1 | +3.32 | +7.0 | 0.5140 | 0.5092 (0.5026) |
| 30 | gbm | 0.2 | 9.0 | -4.04 | -36.5 | 0.5215 | 0.5138 (0.5026) |
| 60 | logreg | 0.15 | 1.7 | +17.74 | +29.8 | 0.5095 | 0.5051 (0.5050) |
| 60 | gbm | 0.15 | 3.1 | -4.77 | -14.8 | 0.5146 | 0.5101 (0.5050) |
| 300 | logreg | 0.075 | 2.2 | -5.88 | -13.1 | 0.4997 | 0.4960 (0.5091) |
| 300 | gbm | 0.1 | 7.3 | +5.13 | +37.4 | 0.5054 | 0.5008 (0.5091) |

Per-trade bps 95% CI (day bootstrap, chosen model):

- h=30s logreg: seen -6.61 bps/trade [-22.0, +9.6], held-out -0.62 [-26.0, +23.2]; test days=153
- h=60s logreg: seen +12.77 bps/trade [-8.2, +34.9], held-out -3.65 [-32.3, +26.7]; test days=153
- h=300s gbm: seen -3.95 bps/trade [-12.7, +5.2], held-out -14.04 [-28.6, -0.0]; test days=153

## GBM permutation importance (validation sample, AUC drop, top 10)

- h=30s: `ret_bps_5s` 0.0072, `ret_bps_1s` 0.0044, `ret_bps_15s` 0.0036, `imb_5s` 0.0024, `ret_bps_30s` 0.0019, `log_vol_ratio_5s` 0.0018, `log_open_vol_ratio` 0.0016, `range_pos` 0.0014, `ret_open_bps` 0.0005, `rv_bps_60s` 0.0004
- h=60s: `range_pos` 0.0068, `ret_bps_1s` 0.0062, `log_open_vol_ratio` 0.0041, `ret_bps_5s` 0.0031, `ret_open_bps` 0.0028, `vwap_dev_bps` 0.0022, `gap_bps` 0.0017, `log_price_ticks` 0.0007, `ret_tk_1s` 0.0005, `ret_tk_5s` 0.0002
- h=300s: `log_open_vol_ratio` 0.0077, `ret_open_bps` 0.0046, `gap_bps` 0.0029, `log_ntrades_30s` 0.0015, `rv_bps_60s` 0.0014, `log_vol_ratio_30s` 0.0007, `vwap_dev_bps` 0.0002, `ret_bps_5s` 0.0002, `imb_30s` 0.0001, `ret_tk_30s` 0.0001

## Bid-ask bounce caveat

Targets are last-trade prices, not mid quotes, so they include bid-ask bounce: a trade at the ask followed by one at the bid shows up as a one-tick reversal. Short-lag reversal features (`ret_*_1s`, `ret_*_5s`, imbalance) can therefore look more predictive than a real, executable strategy would be. The 1-tick round-trip cost (0.5 tick per side) removes the average bounce on the trades taken but does not remove the selection effect (signals fire right after a bounce), and entries are assumed to fill at the last trade price +/- 0.5 tick with no queue or partial-fill risk. Measured test (seen) correlation between past return in ticks and forward return in ticks: h=30s: 1s-lag -0.0127, 5s-lag -0.0084; h=60s: 1s-lag -0.0053, 5s-lag +0.0017; h=300s: 1s-lag -0.0001, 5s-lag +0.0055 (negative = reversal).

