"""Which tick table a session uses (burst v3, ported from _Aries ``aries/burst/ticks.py``).

Since 2023-06-05 the TSE applies the fine tick table to all TOPIX500 constituents
(Core30, Large70, Mid400), not only to TOPIX100.  The vendored ``tick_size(s)`` functions
take an ``is_topix100`` flag; passing "uses the fine table" (``fine``) gives the correct
tick for every price below 10 million yen (the fine table of ``universe.py`` equals the
TOPIX500 table there).  The v3 burst models were trained with that flag.

The rule (same as _Aries ``aries/burst/v3.py::fine_rule``): the size category of the
date (TOPIX Core30 / Large70 / Mid400 -> fine) unless the day's own price grid decides
the table, in which case the grid wins (the exchange rule is public, so this is no
look-ahead).
"""

from __future__ import annotations

from math import gcd

import numpy as np

FINE_SCALES = ("TOPIX Core30", "TOPIX Large70", "TOPIX Mid400")

# (upper bound inclusive, tick); the last row has no bound.
GENERAL: tuple[tuple[float, float], ...] = (
    (3_000, 1), (5_000, 5), (30_000, 10), (50_000, 50), (300_000, 100), (500_000, 500),
    (3_000_000, 1_000), (5_000_000, 5_000), (30_000_000, 10_000), (50_000_000, 50_000),
    (float("inf"), 100_000),
)
TOPIX500: tuple[tuple[float, float], ...] = (
    (1_000, 0.1), (3_000, 0.5), (10_000, 1), (30_000, 5), (100_000, 10), (300_000, 50),
    (1_000_000, 100), (3_000_000, 500), (10_000_000, 1_000), (30_000_000, 5_000),
    (float("inf"), 10_000),
)
# price points where either table changes its tick (band numbers)
BOUNDS = (1_000, 3_000, 5_000, 10_000, 30_000, 50_000, 100_000, 300_000, 500_000, 1_000_000)


def _arrays(table):
    return (np.array([b for b, _ in table], np.float64), np.array([t for _, t in table], np.float64))


_GB, _GT = _arrays(GENERAL)
_FB, _FT = _arrays(TOPIX500)


def tick_size(price: float, fine: bool) -> float:
    """Tick in yen (bounds are inclusive); ``fine`` selects the TOPIX500 table."""
    if not (price > 0) or price != price:
        raise ValueError(f"price must be positive, got {price!r}")
    b, t = (_FB, _FT) if fine else (_GB, _GT)
    return float(t[int(np.searchsorted(b, price, side="left"))])


def fine_by_scale(scale: str | None) -> bool:
    return (scale or "") in FINE_SCALES


def band_of(prices) -> np.ndarray:
    return np.searchsorted(np.asarray(BOUNDS, np.float64), np.asarray(prices, np.float64), side="left")


def grid_gcd(prices) -> dict[int, tuple[float, int]]:
    """Per price band: (gcd of the differences of the distinct prices in yen, n distinct)."""
    p = np.asarray(prices, np.float64)
    p = p[np.isfinite(p) & (p > 0)]
    if len(p) == 0:
        return {}
    q = np.unique(np.round(p * 10).astype(np.int64))
    out = {}
    bands = band_of(q / 10.0)
    for b in np.unique(bands):
        v = q[bands == b]
        if len(v) < 2:
            continue
        g = 0
        for d in np.diff(v):
            g = gcd(g, int(d))
            if g == 1:
                break
        out[int(b)] = (g / 10.0, int(len(v)))
    return out


def classify(price: float, step: float) -> str:
    """'general' if ``step`` is a multiple of the general tick, 'fine' if only of the fine
    tick, else 'other'."""
    tf, tg = tick_size(price, True), tick_size(price, False)
    k_f, k_g = step / tf, step / tg
    if abs(k_g - round(k_g)) < 1e-6 and round(k_g) >= 1:
        return "general"
    if abs(k_f - round(k_f)) < 1e-6 and round(k_f) >= 1:
        return "fine"
    return "other"


def measured_table(prices, min_distinct: int = 8) -> str:
    """'fine' when any band shows prices only the fine table allows; 'general' when a band
    with >= ``min_distinct`` prices fits the general grid; else 'other' / 'unknown'."""
    per = []
    for b, (step, n) in grid_gcd(prices).items():
        lo = 0.0 if b == 0 else BOUNDS[b - 1]
        hi = BOUNDS[b] if b < len(BOUNDS) else lo * 2
        per.append((n, classify((lo + hi) / 2 if b > 0 else hi / 2, step)))
    kinds = [k for _, k in per]
    if "fine" in kinds:
        return "fine"
    if any(k == "general" and n >= min_distinct for n, k in per):
        return "general"
    if "other" in kinds:
        return "other"
    return "unknown"


def fine_rule(scale: str | None, measured: str | None) -> bool:
    """Size category says TOPIX500 -> fine, unless the day's grid decided the table."""
    if measured in ("fine", "general"):
        return measured == "fine"
    return fine_by_scale(scale)


__all__ = [
    "FINE_SCALES",
    "classify",
    "fine_by_scale",
    "fine_rule",
    "grid_gcd",
    "measured_table",
    "tick_size",
]
