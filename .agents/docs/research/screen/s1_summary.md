# Feature screening `s1`

- generated 2026-11-07 15:16 by `scripts/feature_screen.py`; 11 arms, `--train-steps 3000000`, shared args: `--order-mode market --reward-mode realized --gamma 1.0 --gae-lambda 1.0 --unrealized-loss-penalty 0.01 --inventory-penalty 0 --idle-penalty 0 --keep-penalty 0 --action-bonus 0.25 --fill-curriculum 0.0,0.5 --ent-start 0.03 --ent-end 0.005 --bc-from rule --bc-steps 2000 --bc-sessions 300 --bc-critic --seed 0`; every arm runs `--no-test` (test split never loaded).
- PPO evaluated deterministically on trade_through fills (raw PnL); yen/sess = mean per session [95% day-bootstrap CI]; gross ticks/trip = mean per-round-trip PnL before market slippage; forced = share of exits that are the 600 s close.
- single seed per arm: differences within the CI are not evidence.

## val_seen (sorted by val_seen yen/sess)
| arm | spec | obs dim | yen/sess [95% CI] | ticks/sess | trips/sess | gross ticks/trip | hold s | forced share | best @steps | sessions |
|---|---|---|---|---|---|---|---|---|---|---|
| G | `base+G` | 44 | -266.5 [-691.9, +141.4] | -1.277 | 2.32 | 0.452 | 106.9 | 6.6% | 3006464 | 4936 |
| F | `base+F` | 41 | -454.9 [-783.6, -121.9] | -1.199 | 2.13 | 0.444 | 68.8 | 4.4% | 1507328 | 4936 |
| A | `base+A` | 45 | -579.7 [-947.8, -218.5] | -2.094 | 2.52 | 0.171 | 74.9 | 2.9% | 2506752 | 4936 |
| H | `base+H` | 42 | -672.7 [-1,128.3, -214.8] | -2.724 | 4.83 | 0.436 | 43.1 | 1.0% | 3006464 | 4936 |
| C | `base+C` | 42 | -745.0 [-1,220.5, -304.0] | -2.165 | 3.62 | 0.408 | 60.9 | 1.2% | 3006464 | 4936 |
| D | `base+D` | 41 | -844.0 [-1,256.5, -429.7] | -1.975 | 4.39 | 0.562 | 53.0 | 2.1% | 3006464 | 4936 |
| base | `base` | 38 | -903.4 [-1,373.1, -493.4] | -3.314 | 4.71 | 0.300 | 67.1 | 7.5% | 3006464 | 4936 |
| B | `base+B` | 42 | -941.0 [-1,326.9, -573.9] | -3.669 | 5.98 | 0.395 | 45.7 | 2.1% | 3006464 | 4936 |
| -J | `base-J` | 30 | -1,017.5 [-1,496.4, -565.2] | -3.706 | 5.23 | 0.291 | 65.3 | 4.9% | 2506752 | 4936 |
| E | `base+E` | 44 | -1,395.5 [-1,975.1, -850.8] | -4.260 | 6.32 | 0.332 | 65.5 | 4.4% | 2506752 | 4936 |
| I | `base+I` | 40 | -1,727.5 [-2,190.0, -1,267.2] | -5.760 | 5.91 | 0.029 | 96.9 | 13.9% | 0 | 4936 |

## val_held (sorted by val_seen yen/sess)
| arm | spec | obs dim | yen/sess [95% CI] | ticks/sess | trips/sess | gross ticks/trip | hold s | forced share | best @steps | sessions |
|---|---|---|---|---|---|---|---|---|---|---|
| G | `base+G` | 44 | -562.6 [-1,182.1, +52.8] | -0.982 | 1.63 | 0.397 | 152.9 | 12.2% | 3006464 | 1236 |
| F | `base+F` | 41 | -498.9 [-1,177.0, +122.6] | -1.178 | 1.29 | 0.107 | 91.8 | 6.4% | 1507328 | 1236 |
| A | `base+A` | 45 | -1,027.1 [-1,632.5, -453.3] | -2.083 | 1.73 | -0.205 | 94.7 | 3.1% | 2506752 | 1236 |
| H | `base+H` | 42 | -286.7 [-1,157.2, +455.9] | -0.761 | 2.67 | 0.715 | 63.0 | 1.2% | 3006464 | 1236 |
| C | `base+C` | 42 | -947.3 [-1,691.6, -240.7] | -1.438 | 2.49 | 0.425 | 88.1 | 1.7% | 3006464 | 1236 |
| D | `base+D` | 41 | -531.4 [-1,078.8, -10.0] | -2.209 | 2.87 | 0.243 | 86.3 | 4.5% | 3006464 | 1236 |
| base | `base` | 38 | -1,608.4 [-2,413.8, -859.8] | -3.436 | 3.26 | -0.051 | 93.5 | 11.1% | 3006464 | 1236 |
| B | `base+B` | 42 | -954.3 [-1,814.8, -243.6] | -2.243 | 3.75 | 0.405 | 61.7 | 2.7% | 3006464 | 1236 |
| -J | `base-J` | 30 | -900.6 [-1,593.4, -249.9] | -2.055 | 3.85 | 0.467 | 88.6 | 7.4% | 2506752 | 1236 |
| E | `base+E` | 44 | -1,378.1 [-2,216.2, -572.3] | -3.426 | 4.59 | 0.268 | 91.5 | 6.1% | 2506752 | 1236 |
| I | `base+I` | 40 | -2,040.1 [-2,717.0, -1,349.3] | -4.199 | 4.75 | 0.117 | 120.8 | 17.4% | 0 | 1236 |

## baselines (val_seen, trade_through, same for every arm)
| policy | yen/sess | ticks/sess | trips/sess | gross ticks/trip |
|---|---|---|---|---|
| DoNothing | +0.0 | +0.000 | 0.00 | n/a |
| Random | -53148.5 | -148.865 | 148.44 | 0.003 |
| RuleScalper(tuned) | -3936.8 | -11.825 | 12.74 | 0.079 |
| Momentum(tuned) | -442.7 | -0.981 | 1.00 | 0.025 |
