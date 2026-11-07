# Top-50 opening-window predictability gate (P2)

- Run: 2026-11-06 21:56 / mode: **DRY RUN (reduced date range, not the gate result)** / runtime 5.3 min (data+features 0.4 min)
- Command args: seed=0, train stride=5 s, max train rows=2,500,000, train day fraction=1.0, bootstrap reps=2000

## Verdict

- h=30s: **FAIL** (model gbm, theta=0.2; test seen daily mean -212.4 bps CI [-350.8, -98.2], held-out -49.4 bps CI [-157.3, +66.5])
- h=60s: **FAIL** (model logreg, theta=0.1; test seen daily mean -97.2 bps CI [-369.7, +143.0], held-out +25.4 bps CI [-138.3, +166.9])
- h=300s: **FAIL** (model gbm, theta=0.2; test seen daily mean -74.1 bps CI [-358.7, +187.4], held-out -24.6 bps CI [-330.9, +245.5])

Overall: **not all horizons pass** (30s=F, 60s=F, 300s=F). PASS rule: test mean daily PnL (bps, 1-tick round-trip cost deducted) 95% day-bootstrap CI lower bound > 0 for BOTH seen and held-out stems, with the model and theta chosen on validation only.

## Data coverage

| split | range | universe rows used | ok sessions | days | stems | skipped by status |
|---|---|---|---|---|---|---|
| train | 2026-07-01..2026-07-17 | 509 (+141 held-out-stem rows excluded) | 509 | 13 | 67 | none |
| val | 2026-07-18..2026-07-31 | 353 (+97 held-out-stem rows excluded) | 351 | 9 | 66 | no_morning_trade=2 |
| test | 2026-08-03..2026-08-19 | 600 | 600 | 12 | 100 | none |

- Held-out stems (md5(stem)[0] % 5 == 0, excluded from train/val): 18 of 100 stems seen in test; test sessions: seen=463, held-out=137.
- Train grid: 509 sessions subsampled every 5 s -> 57,109 rows (models fit on at most 2,500,000 non-tie rows per horizon). Validation and test use the full 1 s grid.

## Features (all causal; ticks <= t plus pre-open universe columns)

`ret_bps_1s`, `ret_bps_5s`, `ret_bps_15s`, `ret_bps_30s`, `ret_bps_60s`, `ret_tk_1s`, `ret_tk_5s`, `ret_tk_15s`, `ret_tk_30s`, `ret_tk_60s`, `ret_open_bps`, `gap_bps`, `range_pos`, `vwap_dev_bps`, `imb_5s`, `imb_30s`, `imb_60s`, `log_ntrades_5s`, `log_ntrades_30s`, `log_vol_ratio_5s`, `log_vol_ratio_30s`, `rv_bps_30s`, `rv_bps_60s`, `elapsed_s`, `log_open_vol_ratio`, `log_time_since_trade`, `is_topix100`, `log_price_ticks` (28 features; decision grid 1 s from T_open+10s to T_open+600s-h; targets: last-trade forward return over h in bps/ticks and its sign. Tie (zero-return) rows are dropped from classifier training and AUC/accuracy/Brier but kept in the economic test).

## Results per horizon and model

Classification on non-tie rows (test, seen / held-out stems):

| h | model | AUC seen | AUC held | acc seen | base seen | acc held | base held | Brier seen (const) | tie frac |
|---|---|---|---|---|---|---|---|---|---|
| 30 | logreg | 0.5104 | 0.5146 | 0.5019 | 0.5155 | 0.5120 | 0.5127 | 0.2513 (0.2498) | 0.052 |
| 30 | gbm | 0.5018 | 0.5110 | 0.5003 | 0.5155 | 0.5011 | 0.5127 | 0.2581 (0.2498) | 0.052 |
| 60 | logreg | 0.5125 | 0.5180 | 0.4985 | 0.5256 | 0.5104 | 0.5170 | 0.2521 (0.2493) | 0.035 |
| 60 | gbm | 0.4974 | 0.4990 | 0.4969 | 0.5256 | 0.4925 | 0.5170 | 0.2646 (0.2493) | 0.035 |
| 300 | logreg | 0.4950 | 0.5628 | 0.4727 | 0.5736 | 0.5464 | 0.5380 | 0.2584 (0.2446) | 0.015 |
| 300 | gbm | 0.5093 | 0.5269 | 0.4919 | 0.5736 | 0.4985 | 0.5380 | 0.2968 (0.2446) | 0.015 |

Economics (one position per stem-day, hold h s, 1-tick round trip; theta from validation; daily = sum over that day's trades of the group, mean over test days, 95% CI by day bootstrap):

| h | model | theta | group | trades/day | bps/trade | ticks/trade | win rate | daily mean bps | CI | always-long daily bps (CI) | random-dir daily bps (CI) |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 30 | logreg | 0.1 | seen | 52.9 | -4.64 | -0.699 | 0.455 | -245.5 | [-485.3, -33.8] | -250.7 [-500.2, +10.1] | -311.1 [-475.4, -145.9] |
| 30 | logreg | 0.1 | heldout | 26.6 | -1.40 | -1.192 | 0.464 | -37.3 | [-227.5, +113.1] | -95.7 [-252.0, +52.2] | -230.1 [-355.6, -103.8] |
| 30 | gbm* | 0.2 | seen | 67.5 | -3.15 | -0.514 | 0.407 | -212.4 | [-350.8, -98.2] | -386.4 [-533.8, -234.5] | -326.0 [-489.7, -152.5] |
| 30 | gbm* | 0.2 | heldout | 20.6 | -2.40 | -0.854 | 0.445 | -49.4 | [-157.3, +66.5] | -44.7 [-130.8, +61.3] | -26.8 [-146.3, +88.4] |
| 60 | logreg* | 0.1 | seen | 46.2 | -2.10 | -0.060 | 0.483 | -97.2 | [-369.7, +143.0] | -248.0 [-533.7, +74.6] | -126.8 [-471.7, +209.7] |
| 60 | logreg* | 0.1 | heldout | 24.8 | +1.02 | +1.249 | 0.490 | +25.4 | [-138.3, +166.9] | -167.0 [-380.9, +47.7] | -244.3 [-431.3, -60.1] |
| 60 | gbm | 0.2 | seen | 87.6 | -2.93 | -0.623 | 0.423 | -256.3 | [-588.5, +60.0] | -372.4 [-645.2, -132.0] | -517.5 [-739.0, -294.2] |
| 60 | gbm | 0.2 | heldout | 29.8 | -2.63 | -1.042 | 0.455 | -78.5 | [-207.0, +44.9] | -277.7 [-393.6, -142.5] | -248.4 [-467.6, -63.9] |
| 300 | logreg | 0.1 | seen | 26.5 | -15.07 | -3.423 | 0.443 | -399.4 | [-762.2, -1.9] | -502.9 [-864.0, -80.9] | +46.7 [-369.7, +447.9] |
| 300 | logreg | 0.1 | heldout | 8.8 | -21.62 | -6.481 | 0.457 | -189.2 | [-422.2, +52.4] | -226.0 [-528.1, +85.8] | -112.0 [-394.4, +158.9] |
| 300 | gbm* | 0.2 | seen | 37.2 | -1.99 | -0.311 | 0.487 | -74.1 | [-358.7, +187.4] | -55.3 [-495.2, +442.0] | -243.7 [-705.5, +243.9] |
| 300 | gbm* | 0.2 | heldout | 11.2 | -2.19 | -3.096 | 0.496 | -24.6 | [-330.9, +245.5] | +42.1 [-307.5, +359.2] | -457.0 [-757.7, -156.3] |

`*` = model chosen on validation daily-mean bps (seen stems). Validation figures at the chosen theta:

| h | model | theta | val trades/day | val bps/trade | val daily mean bps | val AUC | val acc (base) |
|---|---|---|---|---|---|---|---|
| 30 | logreg | 0.1 | 54.7 | -3.90 | -213.3 | 0.4973 | 0.4906 (0.5186) |
| 30 | gbm | 0.2 | 65.4 | -1.01 | -66.2 | 0.5128 | 0.5046 (0.5186) |
| 60 | logreg | 0.1 | 51.8 | -5.07 | -262.7 | 0.4838 | 0.4772 (0.5246) |
| 60 | gbm | 0.2 | 93.7 | -3.73 | -349.6 | 0.5111 | 0.5017 (0.5246) |
| 300 | logreg | 0.1 | 23.3 | -23.51 | -548.7 | 0.4695 | 0.4671 (0.5613) |
| 300 | gbm | 0.2 | 37.6 | -4.43 | -166.5 | 0.5029 | 0.4917 (0.5613) |

Per-trade bps 95% CI (day bootstrap, chosen model):

- h=30s gbm: seen -3.15 bps/trade [-5.6, -1.4], held-out -2.40 [-7.0, +3.7]; test days=12
- h=60s logreg: seen -2.10 bps/trade [-7.6, +3.3], held-out +1.02 [-4.9, +7.7]; test days=12
- h=300s gbm: seen -1.99 bps/trade [-9.6, +5.1], held-out -2.19 [-29.4, +21.5]; test days=12

## GBM permutation importance (validation sample, AUC drop, top 10)

- h=30s: `gap_bps` 0.0095, `range_pos` 0.0052, `elapsed_s` 0.0034, `log_open_vol_ratio` 0.0027, `ret_bps_30s` 0.0023, `vwap_dev_bps` 0.0019, `rv_bps_60s` 0.0017, `imb_60s` 0.0012, `log_price_ticks` 0.0010, `ret_open_bps` 0.0010
- h=60s: `gap_bps` 0.0181, `elapsed_s` 0.0043, `ret_open_bps` 0.0028, `log_price_ticks` 0.0023, `log_open_vol_ratio` 0.0017, `is_topix100` 0.0010, `ret_bps_30s` 0.0009, `ret_bps_60s` 0.0007, `ret_tk_30s` 0.0007, `log_vol_ratio_30s` 0.0004
- h=300s: `vwap_dev_bps` 0.0118, `ret_open_bps` 0.0068, `range_pos` 0.0061, `log_open_vol_ratio` 0.0055, `gap_bps` 0.0038, `elapsed_s` 0.0027, `log_ntrades_30s` 0.0009, `ret_tk_15s` 0.0004, `log_ntrades_5s` 0.0003, `ret_tk_5s` 0.0001

## Bid-ask bounce caveat

Targets are last-trade prices, not mid quotes, so they include bid-ask bounce: a trade at the ask followed by one at the bid shows up as a one-tick reversal. Short-lag reversal features (`ret_*_1s`, `ret_*_5s`, imbalance) can therefore look more predictive than a real, executable strategy would be. The 1-tick round-trip cost (0.5 tick per side) removes the average bounce on the trades taken but does not remove the selection effect (signals fire right after a bounce), and entries are assumed to fill at the last trade price +/- 0.5 tick with no queue or partial-fill risk. Measured test (seen) correlation between past return in ticks and forward return in ticks: h=30s: 1s-lag -0.0036, 5s-lag +0.0082; h=60s: 1s-lag +0.0024, 5s-lag +0.0177; h=300s: 1s-lag -0.0028, 5s-lag +0.0001 (negative = reversal).

