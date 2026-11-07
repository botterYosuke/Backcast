#!/usr/bin/env python
"""Convert a human day-trader's fills into the behaviour-cloning trade CSV.

Source: ``_Merino/docs/human_trader/fills.csv`` (one row per fill, minute-resolution
times, UTF-8 BOM).  Target: the ``--bc-from csv:<path>`` format of
:mod:`trading_rl.scalp_bc` (``date,stem,us,side,price,qty,kind,order``).

Pipeline
--------
1. Read fills; keep TSE (東証) 新規/返済 rows with a parseable qty/price/minute.
   Other rows (PTS, 現物 spot, unparseable) are counted and left out of pairing.
2. Pair 新規 -> 返済 FIFO per (date, stem, direction), like
   ``_Merino/scripts/human_trader_roundtrips.py`` (a close listed before its open in
   the *same minute* waits for that open).  Closes with no same-day open are
   overnight/previous-window positions and are never emitted.
3. Recover seconds from the tick cache: within the fill's minute, take the earliest
   cached tick with price == 約定単価 (else the nearest price within 1 tick) at/after
   the time assigned to the previous fill of that (date, stem).  ``us`` = tick epoch.
4. A round-trip leg is usable when both fills matched, both lie in
   ``[T_open + 10 s, T_open + 600 s]``, entry <= exit, and neither fill carries a
   contradiction flag in 備考 (image transcription problems).  Only fills of usable
   legs are written, once each.

Also writes the (date, stem) sessions he traded that are in the universe (the
leakage-guard list for test evaluation).
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date as Date
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from trading_rl.cache import CachedSessionUnavailable, load_cached_session  # noqa: E402
from trading_rl.data import OpeningSessionData  # noqa: E402
from trading_rl.universe import (  # noqa: E402
    DEFAULT_CACHE_DIR,
    load_universe,
    tick_size,
)

DEFAULT_FILLS = Path("C:/Users/sasai/Documents/_Merino/docs/human_trader/fills.csv")
DEFAULT_OUT = DEFAULT_CACHE_DIR / "human"
US = 1_000_000
START_S = 10  # env forbids orders before T_open + 10 s
WINDOW_S = 600
BC_COLUMNS = ("date", "stem", "us", "side", "price", "qty", "kind", "order")

# 備考 phrases marking an image transcription contradiction (README / HUMAN_TRADER_CASE
# "データ品質の注意"): fills are listed as shown, so these rows are unreliable.
CONTRADICTION_RE = re.compile(
    r"指値.*より(上|下)|より(上|下)で約定|受付より前|約定より後|逆指値|表示ずれ|"
    r"読み取り不能|崩れ|取消済|新済|画面は|新規・返済とも|と読める"
)

Loader = Callable[[str, str], OpeningSessionData | None]


# --------------------------------------------------------------------------- #
# Fills
# --------------------------------------------------------------------------- #
@dataclass
class Fill:
    idx: int  # file order
    date: str
    stem: str
    minute: int  # minutes since 00:00
    kind: str  # "entry" | "exit"
    side: int  # +1 buy / -1 sell
    qty: int
    price: float
    source: str
    note: str = ""
    flagged: bool = False
    us: int | None = None
    quality: str = "none"  # exact | near | unmatched | outside | no_session

    @property
    def direction(self) -> str:
        """Direction of the position this fill belongs to: L(ong) / S(hort)."""
        if self.kind == "entry":
            return "L" if self.side > 0 else "S"
        return "L" if self.side < 0 else "S"


def _minute(s: str) -> int | None:
    try:
        h, m = s.strip().split(":")[:2]
        return int(h) * 60 + int(m)
    except ValueError:
        return None


def read_fills(path: Path) -> tuple[list[Fill], Counter, set[tuple[str, str]]]:
    """Return (TSE 新規/返済 fills, skip counters, every (date, stem) in the file)."""
    fills: list[Fill] = []
    skipped: Counter = Counter()
    traded: set[tuple[str, str]] = set()
    with open(path, newline="", encoding="utf-8-sig") as fh:
        for i, r in enumerate(csv.DictReader(fh)):
            stem = r["コード"].strip().lower()
            traded.add((r["日付"].strip(), stem))
            if r["市場"].strip() != "東証":
                skipped["not_tse"] += 1
                continue
            kind = {"新規": "entry", "返済": "exit"}.get(r["取引区分"].strip())
            if kind is None:
                skipped["not_margin_open_close"] += 1
                continue
            side = {"買": 1, "売": -1}.get(r["売買"].strip())
            minute = _minute(r["約定時刻"])
            try:
                qty, px = int(r["約定数"]), float(r["約定単価"])
            except ValueError:
                skipped["unparseable_qty_price"] += 1
                continue
            if side is None or minute is None or qty <= 0:
                skipped["unparseable_side_time"] += 1
                continue
            note = r.get("備考", "") or ""
            fills.append(
                Fill(
                    idx=i,
                    date=r["日付"].strip(),
                    stem=stem,
                    minute=minute,
                    kind=kind,
                    side=side,
                    qty=qty,
                    price=px,
                    source=r["出所"].strip(),
                    note=note,
                    flagged=bool(CONTRADICTION_RE.search(note)),
                )
            )
    return fills, skipped, traded


# --------------------------------------------------------------------------- #
# Pairing
# --------------------------------------------------------------------------- #
@dataclass
class Leg:
    entry: Fill
    exit: Fill
    qty: int


def pair_fills(fills: list[Fill]) -> tuple[list[Leg], list[Fill], list[Fill]]:
    """FIFO pairing per (date, stem, direction).

    Returns ``(legs, unpaired_exits, unpaired_entries)``.  Fills are processed in file
    order.  A close with no open yet waits; an open in the *same minute* consumes it
    (the broker lists fills to the minute only).  Leftover closes are positions opened
    before the day/window; leftover opens are still open at the end of the data.
    """
    book: dict[tuple, list[list]] = defaultdict(list)  # key -> [[remaining, fill]]
    pending: dict[tuple, list[list]] = defaultdict(list)
    legs: list[Leg] = []
    for f in fills:
        key = (f.date, f.stem, f.direction)
        q = f.qty
        if f.kind == "entry":
            pend = pending[key]
            while q and pend and pend[0][1].minute == f.minute:
                p = pend[0]
                take = min(q, p[0])
                legs.append(Leg(f, p[1], take))
                p[0] -= take
                q -= take
                if not p[0]:
                    pend.pop(0)
            if q:
                book[key].append([q, f])
        else:
            lots = book[key]
            while q and lots:
                lot = lots[0]
                take = min(q, lot[0])
                legs.append(Leg(lot[1], f, take))
                lot[0] -= take
                q -= take
                if not lot[0]:
                    lots.pop(0)
            if q:
                pending[key].append([q, f])
    unpaired_exits = [p[1] for v in pending.values() for p in v]
    unpaired_entries = [p[1] for v in book.values() for p in v]
    return legs, unpaired_exits, unpaired_entries


# --------------------------------------------------------------------------- #
# Timestamp recovery
# --------------------------------------------------------------------------- #
def assign_times(
    fills: list[Fill], sess: OpeningSessionData, is_topix100: bool
) -> None:
    """Set ``us``/``quality`` on the fills of one (date, stem) from its cached ticks.

    Epoch convention: naive JST read as UTC, so ``(us // 1e6) % 86400`` is the second
    of the (JST) day.  Fills are processed by (minute, file order); each takes the
    earliest tick in its minute at/after the previous assigned time, exact price first,
    else the nearest price within one tick.
    """
    t = np.asarray(sess.epoch_us, dtype=np.int64)
    p = np.asarray(sess.price, dtype=np.float64)
    minute_of = (t // US) % 86400 // 60
    last_us = -1
    for f in sorted(fills, key=lambda x: (x.minute, x.idx)):
        in_min = minute_of == f.minute
        if not in_min.any():
            f.quality = "outside"
            continue
        cand = in_min & (t >= last_us)
        exact = np.flatnonzero(cand & (np.abs(p - f.price) < 1e-6))
        if len(exact):
            j = int(exact[0])
            f.quality = "exact"
        else:
            tk = tick_size(f.price, is_topix100)
            near_ix = np.flatnonzero(cand & (np.abs(p - f.price) <= tk + 1e-6))
            if len(near_ix) == 0:
                f.quality = "unmatched"
                continue
            d = np.abs(p[near_ix] - f.price)
            j = int(near_ix[int(np.argmin(d))])  # argmin = first on ties = earliest
            f.quality = "near"
        f.us = int(t[j])
        last_us = f.us


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
@dataclass
class Result:
    rows: list[dict] = field(default_factory=list)
    stats: Counter = field(default_factory=Counter)
    by_source_fills: Counter = field(default_factory=Counter)
    trips_by_stem: Counter = field(default_factory=Counter)
    trips_by_day: Counter = field(default_factory=Counter)
    legs: list[Leg] = field(default_factory=list)


def _plural(kind: str) -> str:
    return "entries" if kind == "entry" else "exits"


def convert(
    fills: list[Fill],
    universe: dict[tuple[str, str], bool],
    loader: Loader,
    *,
    order: str = "market",
) -> Result:
    """Pair, timestamp and filter ``fills``; ``universe`` maps (date, stem) -> topix100."""
    res = Result()
    s = res.stats
    s["fills_tse_margin"] = len(fills)
    for f in fills:
        res.by_source_fills[f.source] += 1
    legs, un_exits, un_entries = pair_fills(fills)
    s["legs_paired"] = len(legs)
    s["exits_without_same_day_open"] = len(un_exits)
    s["entries_without_close"] = len(un_entries)

    sessions: dict[tuple[str, str], OpeningSessionData | None] = {}
    groups: dict[tuple[str, str], list[Fill]] = defaultdict(list)
    for f in fills:
        groups[(f.date, f.stem)].append(f)
    for key, grp in groups.items():
        if key not in universe:
            s["fills_not_in_universe"] += len(grp)
            late = bool(universe) and key[0] > max(d for d, _ in universe)
            s["fills_after_universe_end" if late else "fills_stem_not_in_top50"] += len(grp)
            for f in grp:
                f.quality = "no_session"
            continue
        sess = loader(*key)
        sessions[key] = sess
        if sess is None:
            s["fills_no_cached_session"] += len(grp)
            for f in grp:
                f.quality = "no_session"
            continue
        assign_times(grp, sess, universe[key])

    for f in fills:
        if f.quality in ("exact", "near", "unmatched", "outside"):
            s["fills_in_cached_sessions"] += 1
            s[f"fills_{f.quality}"] += 1
        if f.quality in ("exact", "near"):
            s["fills_matched"] += 1
            t_open = sessions[(f.date, f.stem)].t_open_us  # type: ignore[union-attr]
            rel = f.us - t_open  # type: ignore[operator]
            if rel < START_S * US:
                s[f"{_plural(f.kind)}_before_10s"] += 1
            elif rel > WINDOW_S * US:
                s[f"{_plural(f.kind)}_after_600s"] += 1
            else:
                s[f"{_plural(f.kind)}_usable_window"] += 1

    def in_window(f: Fill) -> bool:
        if f.us is None:
            return False
        sess = sessions[(f.date, f.stem)]
        rel = f.us - sess.t_open_us  # type: ignore[union-attr]
        return START_S * US <= rel <= WINDOW_S * US

    used: dict[int, Fill] = {}
    for lg in legs:
        e, x = lg.entry, lg.exit
        if e.us is None or x.us is None:
            s["legs_dropped_untimed"] += 1
        elif not (in_window(e) and in_window(x)):
            s["legs_dropped_out_of_window"] += 1
        elif e.flagged or x.flagged:
            s["legs_dropped_flagged_note"] += 1
        elif e.us > x.us:
            s["legs_dropped_exit_before_entry"] += 1
        else:
            s["legs_usable"] += 1
            res.legs.append(lg)
            res.trips_by_stem[e.stem] += 1
            res.trips_by_day[e.date] += 1
            used[e.idx] = e
            used[x.idx] = x
    for f in used.values():
        res.rows.append(
            {
                "date": f.date,
                "stem": f.stem,
                "us": f.us,
                "side": "buy" if f.side > 0 else "sell",
                "price": f.price,
                "qty": f.qty,
                "kind": f.kind,
                "order": order,
            }
        )
    res.rows.sort(key=lambda r: (r["date"], r["stem"], r["us"]))
    s["bc_rows"] = len(res.rows)
    s["bc_rows_entry"] = sum(r["kind"] == "entry" for r in res.rows)
    s["bc_rows_exit"] = sum(r["kind"] == "exit" for r in res.rows)
    return res


def cached_loader(cache_dir: Path) -> Loader:
    def load(date: str, stem: str) -> OpeningSessionData | None:
        try:
            return load_cached_session(date, stem, cache_dir)
        except (CachedSessionUnavailable, FileNotFoundError):
            return None

    return load


def write_csv(path: Path, columns: tuple[str, ...], rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=columns)
        w.writeheader()
        w.writerows(rows)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--fills", type=Path, default=DEFAULT_FILLS)
    ap.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE_DIR)
    ap.add_argument("--out-dir", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--start", default="2026-01-01", help="first universe date")
    ap.add_argument("--order", choices=["market", "limit", ""], default="market")
    args = ap.parse_args(argv)

    fills, skipped, traded = read_fills(args.fills)
    uni = {
        (r["date"].isoformat() if isinstance(r["date"], Date) else str(r["date"]),
         str(r["stem"]).lower()): bool(r["is_topix100"])
        for r in load_universe(args.cache_dir, start=args.start)
    }
    res = convert(fills, uni, cached_loader(args.cache_dir), order=args.order)

    write_csv(args.out_dir / "yaman_trades.csv", BC_COLUMNS, res.rows)
    excl = sorted(k for k in traded if k in uni and k[0] >= args.start)
    write_csv(
        args.out_dir / "yaman_exclude_sessions.csv",
        ("date", "stem"),
        [{"date": d, "stem": st} for d, st in excl],
    )
    detail_cols = ("date", "stem", "kind", "side", "qty", "price", "minute", "us", "quality", "source", "flagged")
    write_csv(
        args.out_dir / "yaman_fills_detail.csv",
        detail_cols,
        [
            {
                "date": f.date, "stem": f.stem, "kind": f.kind, "side": f.side, "qty": f.qty,
                "price": f.price, "minute": f"{f.minute // 60:02d}:{f.minute % 60:02d}",
                "us": f.us if f.us is not None else "", "quality": f.quality,
                "source": f.source, "flagged": int(f.flagged),
            }
            for f in fills
        ],
    )
    print(f"read-skips: {dict(skipped)}")
    print(f"fills by source: {dict(res.by_source_fills)}")
    for k, v in sorted(res.stats.items()):
        print(f"{k}: {v}")
    print(f"usable legs by stem: {dict(res.trips_by_stem)}")
    print(f"usable legs by day: {dict(sorted(res.trips_by_day.items()))}")
    print(f"exclude sessions: {len(excl)}; dates: {sorted({d for d, _ in excl})}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
