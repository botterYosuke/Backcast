#!/usr/bin/env python3
"""Build the ``.cache/trading_rl/ext`` caches used by feature groups K-Q (one-time).

    uv run python scripts/build_scalp_ext_cache.py --steps daily,topix,disc,prevday
    uv run python scripts/build_scalp_ext_cache.py --steps etf,peers

Read-only on ``S:`` and on the existing caches; writes only new files under
``<cache-dir>/ext``.  Runs at below-normal priority with 2 DuckDB threads.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from trading_rl import scalp_ext_build as eb  # noqa: E402
from trading_rl.universe import DEFAULT_CACHE_DIR, load_universe  # noqa: E402

ALL = ("daily", "topix", "disc", "prevday", "etf", "peers")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--steps", default=",".join(ALL))
    ap.add_argument("--cache-dir", default=str(DEFAULT_CACHE_DIR))
    ap.add_argument("--start", default="2024-01-01")
    ap.add_argument("--end", default="2026-08-31")
    args = ap.parse_args()
    eb.lower_priority()
    steps = [s.strip() for s in args.steps.split(",") if s.strip()]
    rows = load_universe(Path(args.cache_dir), args.start, args.end)
    dates = sorted({str(r["date"]) for r in rows})
    stems = sorted({str(r["stem"]).lower() for r in rows})
    print(f"universe: {len(dates)} dates, {len(stems)} stems", flush=True)
    for s in steps:
        t0 = time.time()
        if s == "daily":
            eb.build_daily_adj(args.cache_dir)
        elif s == "topix":
            eb.build_topix(args.cache_dir)
        elif s == "disc":
            eb.build_disclosures(args.cache_dir)
        elif s == "prevday":
            eb.build_prevday(args.cache_dir, stems)
        elif s == "etf":
            n = eb.build_etf(args.cache_dir, dates)
            print(f"  etf days written: {n}", flush=True)
        elif s == "peers":
            n = eb.build_peers(args.cache_dir, dates)
            print(f"  peer days written: {n}", flush=True)
        else:
            raise SystemExit(f"unknown step {s!r}")
        print(f"[{s}] done in {time.time() - t0:.0f}s", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
