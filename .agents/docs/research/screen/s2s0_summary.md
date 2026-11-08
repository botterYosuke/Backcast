# Feature screening `s2s0`

- generated 2026-11-07 18:50 by `scripts/feature_screen.py`; 6 arms, `--train-steps 10000000`, shared args: `--order-mode market --reward-mode realized --gamma 1.0 --gae-lambda 1.0 --unrealized-loss-penalty 0.01 --inventory-penalty 0 --idle-penalty 0 --keep-penalty 0 --action-bonus 0.25 --fill-curriculum 0.0,0.5 --ent-start 0.03 --ent-end 0.005 --bc-from rule --bc-steps 2000 --bc-sessions 300 --bc-critic --seed 0`; every arm runs `--no-test` (test split never loaded).
- PPO evaluated deterministically on trade_through fills (raw PnL); yen/sess = mean per session [95% day-bootstrap CI]; gross ticks/trip = mean per-round-trip PnL before market slippage; forced = share of exits that are the 600 s close.
- single seed per arm: differences within the CI are not evidence.

## val_seen (sorted by val_seen yen/sess)
| arm | spec | obs dim | yen/sess [95% CI] | ticks/sess | trips/sess | gross ticks/trip | hold s | forced share | best @steps | sessions |
|---|---|---|---|---|---|---|---|---|---|---|
| C | `base+C` | 42 | -252.9 [-567.6, +37.7] | -1.086 | 3.34 | 0.676 | 56.7 | 7.5% | 10002432 | 4936 |
| H | `base+H` | 42 | -294.7 [-739.8, +146.7] | -0.812 | 1.32 | 0.390 | 160.8 | 27.1% | 8503296 | 4936 |
| G | `base+G` | 44 | -370.6 [-752.5, -8.0] | -1.749 | 2.67 | 0.344 | 69.5 | 10.8% | 10002432 | 4936 |
| C+D+G+H | `base+C+D+G+H` | 55 | -490.9 [-986.5, -32.0] | -1.727 | 2.06 | 0.165 | 192.1 | 31.8% | 10002432 | 4936 |
| D | `base+D` | 41 | -497.7 [-1,061.6, +60.1] | -1.635 | 2.49 | 0.360 | 118.0 | 16.5% | 9003008 | 4936 |
| base | `base` | 38 | -563.7 [-990.5, -120.7] | -1.968 | 4.99 | 0.602 | 51.7 | 6.3% | 7004160 | 4936 |

## val_held (sorted by val_seen yen/sess)
| arm | spec | obs dim | yen/sess [95% CI] | ticks/sess | trips/sess | gross ticks/trip | hold s | forced share | best @steps | sessions |
|---|---|---|---|---|---|---|---|---|---|---|
| C | `base+C` | 42 | -121.9 [-748.7, +457.8] | -0.330 | 1.75 | 0.814 | 97.8 | 14.3% | 10002432 | 1236 |
| H | `base+H` | 42 | -216.5 [-773.7, +324.4] | -0.584 | 0.81 | 0.296 | 206.4 | 36.7% | 8503296 | 1236 |
| G | `base+G` | 44 | -86.0 [-615.8, +428.4] | -0.801 | 1.85 | 0.569 | 93.9 | 15.1% | 10002432 | 1236 |
| C+D+G+H | `base+C+D+G+H` | 55 | -1,012.3 [-1,883.6, -197.7] | -1.321 | 1.36 | 0.035 | 285.5 | 47.8% | 10002432 | 1236 |
| D | `base+D` | 41 | -316.8 [-950.9, +307.5] | -1.506 | 1.61 | 0.080 | 162.5 | 23.0% | 9003008 | 1236 |
| base | `base` | 38 | -447.9 [-1,054.7, +187.4] | -1.896 | 3.08 | 0.389 | 84.7 | 11.2% | 7004160 | 1236 |

## baselines (val_seen, trade_through, same for every arm)
| policy | yen/sess | ticks/sess | trips/sess | gross ticks/trip |
|---|---|---|---|---|
| DoNothing | +0.0 | +0.000 | 0.00 | n/a |
| Random | -53148.5 | -148.865 | 148.44 | 0.003 |
| RuleScalper(tuned) | -3936.8 | -11.825 | 12.74 | 0.079 |
| Momentum(tuned) | -442.7 | -0.981 | 1.00 | 0.025 |
