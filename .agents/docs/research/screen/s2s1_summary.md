# Feature screening `s2s1`

- generated 2026-11-07 21:30 by `scripts/feature_screen.py`; 6 arms, `--train-steps 10000000`, shared args: `--order-mode market --reward-mode realized --gamma 1.0 --gae-lambda 1.0 --unrealized-loss-penalty 0.01 --inventory-penalty 0 --idle-penalty 0 --keep-penalty 0 --action-bonus 0.25 --fill-curriculum 0.0,0.5 --ent-start 0.03 --ent-end 0.005 --bc-from rule --bc-steps 2000 --bc-sessions 300 --bc-critic --seed 1`; every arm runs `--no-test` (test split never loaded).
- PPO evaluated deterministically on trade_through fills (raw PnL); yen/sess = mean per session [95% day-bootstrap CI]; gross ticks/trip = mean per-round-trip PnL before market slippage; forced = share of exits that are the 600 s close.
- single seed per arm: differences within the CI are not evidence.

## val_seen (sorted by val_seen yen/sess)
| arm | spec | obs dim | yen/sess [95% CI] | ticks/sess | trips/sess | gross ticks/trip | hold s | forced share | best @steps | sessions |
|---|---|---|---|---|---|---|---|---|---|---|
| C+D+G+H | `base+C+D+G+H` | 55 | +51.7 [-101.4, +207.8] | -0.054 | 0.92 | 0.946 | 52.7 | 6.0% | 6004736 | 4936 |
| base | `base` | 38 | +10.2 [-218.9, +251.7] | -0.101 | 0.51 | 0.804 | 143.5 | 22.2% | 8503296 | 4936 |
| D | `base+D` | 41 | -135.1 [-454.0, +190.5] | -0.565 | 1.36 | 0.585 | 100.5 | 14.9% | 8503296 | 4936 |
| H | `base+H` | 42 | -234.1 [-657.1, +187.9] | -0.549 | 1.16 | 0.556 | 178.7 | 28.9% | 8003584 | 4936 |
| G | `base+G` | 44 | -437.3 [-721.4, -179.6] | -1.269 | 1.39 | 0.078 | 21.9 | 2.7% | 10002432 | 4936 |
| C | `base+C` | 42 | -923.7 [-1,399.4, -468.4] | -2.492 | 4.52 | 0.466 | 62.6 | 9.9% | 6004736 | 4936 |

## val_held (sorted by val_seen yen/sess)
| arm | spec | obs dim | yen/sess [95% CI] | ticks/sess | trips/sess | gross ticks/trip | hold s | forced share | best @steps | sessions |
|---|---|---|---|---|---|---|---|---|---|---|
| C+D+G+H | `base+C+D+G+H` | 55 | +7.2 [-348.4, +308.2] | +0.156 | 0.49 | 1.323 | 61.6 | 7.8% | 6004736 | 1236 |
| base | `base` | 38 | -278.2 [-716.3, +108.2] | -0.576 | 0.26 | -1.183 | 195.0 | 31.3% | 8503296 | 1236 |
| D | `base+D` | 41 | -333.3 [-839.7, +177.8] | -0.467 | 0.80 | 0.416 | 156.4 | 24.2% | 8503296 | 1236 |
| H | `base+H` | 42 | -142.3 [-715.7, +439.6] | -0.330 | 0.72 | 0.561 | 239.3 | 39.7% | 8003584 | 1236 |
| G | `base+G` | 44 | -545.3 [-1,158.7, -71.8] | -1.300 | 1.12 | -0.162 | 15.5 | 2.2% | 10002432 | 1236 |
| C | `base+C` | 42 | -729.6 [-1,536.4, +33.3] | -2.282 | 3.13 | 0.285 | 90.8 | 14.7% | 6004736 | 1236 |

## baselines (val_seen, trade_through, same for every arm)
| policy | yen/sess | ticks/sess | trips/sess | gross ticks/trip |
|---|---|---|---|---|
| DoNothing | +0.0 | +0.000 | 0.00 | n/a |
| Random | -53148.5 | -148.865 | 148.44 | 0.003 |
| RuleScalper(tuned) | -3936.8 | -11.825 | 12.74 | 0.079 |
| Momentum(tuned) | -442.7 | -0.981 | 1.00 | 0.025 |
