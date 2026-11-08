"""Learning-curve table (markdown) from .cache/trading_rl/burst/curve_<tag>_s<seed>.csv.

  uv run python scripts/burst/curve_table.py plain slip1
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path

D = Path(".cache/trading_rl/burst")


def main() -> None:
    for tag in sys.argv[1:]:
        print(f"\n#### {tag}\n")
        print("| 学習量 | seed | 学習中 tick/エピソード | 学習中 回/エピソード | 検証その1 tick/回 | 検証その1 回/エピソード | 保有の中央値 |")
        print("|---|---|---|---|---|---|---|")
        rows = {}
        for s in range(3):
            f = D / f"curve_{tag}_s{s}.csv"
            if f.exists():
                rows[s] = [r for r in csv.DictReader(open(f, encoding="utf-8")) if r["val_tick_trip"]]
        if not rows:
            continue
        n = min(len(v) for v in rows.values())
        for i in sorted({0, n // 6, n // 3, n // 2, 2 * n // 3, 5 * n // 6, n - 1}):
            for s, v in rows.items():
                r = v[i]
                print(f"| {int(r['steps'])/1e6:.1f}M | {s} | {float(r['train_pnl_ep']):+.2f} | {float(r['train_trips_ep']):.2f} | "
                      f"{float(r['val_tick_trip']):+.2f} | {float(r['val_trips_ep']):.2f} | {float(r['val_hold_med']):.0f}秒 |")


if __name__ == "__main__":
    main()
