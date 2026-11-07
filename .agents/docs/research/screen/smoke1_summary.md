# Feature screening `smoke1`

- generated 2026-11-07 12:55 by `scripts/feature_screen.py`; 3 arms, `--train-steps 100000`, shared args: `--smoke --order-mode market --workers 2 --threads 2`; every arm runs `--no-test` (test split never loaded).
- PPO evaluated deterministically on trade_through fills (raw PnL); yen/sess = mean per session [95% day-bootstrap CI]; gross ticks/trip = mean per-round-trip PnL before market slippage; forced = share of exits that are the 600 s close.
- single seed per arm: differences within the CI are not evidence.

## val_seen (sorted by val_seen yen/sess)
| arm | spec | obs dim | yen/sess [95% CI] | ticks/sess | trips/sess | gross ticks/trip | hold s | forced share | best @steps | sessions |
|---|---|---|---|---|---|---|---|---|---|---|
| base | `base` | 38 | +0.0 [+0.0, +0.0] | +0.000 | 0.00 | n/a | n/a | n/a% | 106496 | 490 |
| A | `base+A` | 45 | +0.0 [+0.0, +0.0] | +0.000 | 0.00 | n/a | n/a | n/a% | 106496 | 490 |
| C | `base+C` | 42 | +0.0 [+0.0, +0.0] | +0.000 | 0.00 | n/a | n/a | n/a% | 106496 | 490 |

## val_held (sorted by val_seen yen/sess)
| arm | spec | obs dim | yen/sess [95% CI] | ticks/sess | trips/sess | gross ticks/trip | hold s | forced share | best @steps | sessions |
|---|---|---|---|---|---|---|---|---|---|---|
| base | `base` | 38 | +0.0 [+0.0, +0.0] | +0.000 | 0.00 | n/a | n/a | n/a% | 106496 | 106 |
| A | `base+A` | 45 | +0.0 [+0.0, +0.0] | +0.000 | 0.00 | n/a | n/a | n/a% | 106496 | 106 |
| C | `base+C` | 42 | +0.0 [+0.0, +0.0] | +0.000 | 0.00 | n/a | n/a | n/a% | 106496 | 106 |

## baselines (val_seen, trade_through, same for every arm)
| policy | yen/sess | ticks/sess | trips/sess | gross ticks/trip |
|---|---|---|---|---|
| DoNothing | +0.0 | +0.000 | 0.00 | n/a |
| Random | -52735.4 | -150.974 | 149.19 | -0.002 |
| RuleScalper(tuned) | -3564.8 | -11.450 | 14.28 | 0.218 |
| Momentum(tuned) | +300.5 | +1.648 | 0.98 | 2.682 |
