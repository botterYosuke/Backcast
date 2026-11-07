#!/usr/bin/env python3
"""Build the multi-symbol opening-window RL dataset (universe + tick cache).

    uv run python scripts/build_rl_dataset.py --start 2024-01-04 --top-n 50
    uv run python scripts/build_rl_dataset.py --universe-only

Both stages are resumable: the universe is rebuilt from the local daily panel
(cheap) and the tick cache skips every (date, stem) it already holds.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass

SRC_DIR = Path(__file__).resolve().parent.parent / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from trading_rl.cache import (  # noqa: E402
    build_cache,
    cache_size_bytes,
    cache_status_counts,
)
from trading_rl.universe import (  # noqa: E402
    DEFAULT_CACHE_DIR,
    DEFAULT_DAILY_DIR,
    DEFAULT_LISTED,
    DEFAULT_LISTED_PIT,
    DEFAULT_TRADES_DIR,
    build_universe,
    last_tick_date,
)


def _say(msg: str) -> None:
    print(msg, flush=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--start", default="2024-01-04")
    ap.add_argument("--end", default=None, help="default: last date with tick data")
    ap.add_argument("--top-n", type=int, default=50)
    ap.add_argument("--universe-only", action="store_true")
    ap.add_argument("--include-funds", action="store_true",
                    help="keep ETFs/ETNs/REITs/infrastructure funds in the ranking")
    ap.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE_DIR)
    ap.add_argument("--daily-dir", type=Path, default=DEFAULT_DAILY_DIR)
    ap.add_argument("--trades-dir", type=Path, default=DEFAULT_TRADES_DIR)
    ap.add_argument("--listed-pit", type=Path, default=DEFAULT_LISTED_PIT)
    ap.add_argument("--listed", type=Path, default=DEFAULT_LISTED)
    ap.add_argument("--workers", type=int, default=4, help="tick-cache threads")
    ap.add_argument("--daily-workers", type=int, default=8, help="daily-scan threads")
    ap.add_argument("--refresh-panel", action="store_true",
                    help="rescan all stocks_daily files")
    args = ap.parse_args(argv)

    end = args.end or last_tick_date(args.trades_dir)
    _say(f"range {args.start}..{end} top_n={args.top_n} include_funds={args.include_funds}")
    uni = build_universe(
        start=args.start, end=end, top_n=args.top_n,
        include_funds=args.include_funds, daily_dir=args.daily_dir,
        trades_dir=args.trades_dir, listed_pit_path=args.listed_pit,
        listed_path=args.listed, cache_dir=args.cache_dir,
        workers=args.daily_workers, refresh_panel=args.refresh_panel,
        progress=_say,
    )
    _say(f"universe: {uni.n_dates} dates, {uni.n_rows} rows ({uni.first_date}..{uni.last_date})")
    if args.universe_only:
        return 0

    summary = build_cache(
        cache_dir=args.cache_dir, trades_dir=args.trades_dir,
        start=args.start, end=end, workers=args.workers, progress=_say,
    )
    counts = cache_status_counts(args.cache_dir, args.start, end)
    _say(
        f"cache done: extracted {summary.sessions_extracted}/{summary.sessions_total} "
        f"sessions in {summary.seconds:.0f}s; status={dict(counts)}; "
        f"size={cache_size_bytes(args.cache_dir) / 1e6:.1f} MB"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
