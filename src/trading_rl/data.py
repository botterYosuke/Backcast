"""DuckDB tick extraction pipeline for post-open 15-minute window.

Extracts chronological execution ticks strictly bounded to the first 15 minutes
from when a stock actually opens (T_open -> T_open + 900s), handling delayed opens
due to special quotes (特別気配).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date as Date
from datetime import datetime, timedelta
from pathlib import Path

import duckdb
import numpy as np

# Exact 15 minutes in microseconds (15 * 60 * 1,000,000)
OPENING_WINDOW_US: int = 900_000_000

TRADES_TABLE = "stocks_board"
METADATA_TABLE = "stocks_board_metadata"


class NoMorningSessionError(ValueError):
    """Raised when no morning executions (>= 09:00:00) exist for the symbol/date."""


@dataclass(frozen=True)
class OpeningSessionData:
    """Bounded tick session covering [T_open, T_open + 15m]."""

    stem: str
    code: str
    date: str
    t_open_us: int
    p_open: float
    v_open: int
    epoch_us: np.ndarray  # int64
    price: np.ndarray  # float64
    qty: np.ndarray  # int64
    trade_type: np.ndarray  # object or str

    def __len__(self) -> int:
        return len(self.epoch_us)

    def to_records(self) -> np.ndarray:
        """Convert tick series into a contiguous structured NumPy array."""
        dtype = np.dtype(
            [
                ("epoch_us", np.int64),
                ("price", np.float64),
                ("qty", np.int64),
                ("trade_type", "U8"),
            ]
        )
        records = np.empty(len(self.epoch_us), dtype=dtype)
        records["epoch_us"] = self.epoch_us
        records["price"] = self.price
        records["qty"] = self.qty
        records["trade_type"] = self.trade_type
        return records

    def to_dict(self) -> dict[str, object]:
        return {
            "stem": self.stem,
            "code": self.code,
            "date": self.date,
            "t_open_us": self.t_open_us,
            "p_open": self.p_open,
            "v_open": self.v_open,
            "count": len(self.epoch_us),
        }


@contextmanager
def _open_duckdb(
    source: Path | str | duckdb.DuckDBPyConnection,
    stem: str | None = None,
) -> Iterator[duckdb.DuckDBPyConnection]:
    """Yield a DuckDB connection, creating a read-only one if given a path."""
    if isinstance(source, duckdb.DuckDBPyConnection):
        yield source
        return

    path = Path(source)
    if path.is_dir():
        # Check source/stocks_trades/<stem>.duckdb or source/<stem>.duckdb
        if stem is None:
            raise ValueError(
                f"stem must be specified when source is a directory: {source}"
            )
        candidates = [
            path / "stocks_trades" / f"{stem}.duckdb",
            path / f"{stem}.duckdb",
        ]
        target = next((c for c in candidates if c.is_file()), candidates[0])
    else:
        target = path

    con = duckdb.connect(str(target), read_only=True)
    try:
        yield con
    finally:
        con.close()


def _resolve_code(con: duckdb.DuckDBPyConnection, requested_code: str | None) -> str:
    """Resolve the canonical stock code from metadata or trades table."""
    if requested_code is not None:
        return requested_code

    # Try metadata table first (largest record count)
    try:
        rows = con.execute(
            f"SELECT Code FROM {METADATA_TABLE} ORDER BY record_count DESC LIMIT 1"
        ).fetchall()
        if rows and rows[0][0]:
            return str(rows[0][0])
    except duckdb.Error:
        pass

    # Fall back to distinct code in trades table
    try:
        rows = con.execute(
            f"SELECT DISTINCT Code FROM {TRADES_TABLE} LIMIT 1"
        ).fetchall()
        if rows and rows[0][0]:
            return str(rows[0][0])
    except duckdb.Error:
        pass

    raise ValueError("Could not determine stock code from DuckDB database")


def _parse_timestamp(ts: str) -> datetime:
    """Parse DuckDB timestamp string into datetime."""
    # Common format: '2026-08-14 09:00:00.100000'
    clean = ts.strip()
    try:
        return datetime.fromisoformat(clean)
    except ValueError:
        # Fallback format handling
        if "." in clean:
            return datetime.strptime(clean[:26], "%Y-%m-%d %H:%M:%S.%f")
        return datetime.strptime(clean, "%Y-%m-%d %H:%M:%S")


def load_session_ticks(
    source: Path | str | duckdb.DuckDBPyConnection,
    date: str | Date,
    *,
    stem: str | None = None,
    code: str | None = None,
    window_us: int = OPENING_WINDOW_US,
) -> OpeningSessionData:
    """Load ticks for the dynamic post-open 15-minute window [T_open, T_open + 15m].

    Detects T_open as the first execution in the morning session (Timestamp >= 09:00:00).
    Captures all ticks in [T_open, T_open + window_us] inclusive.

    Args:
        source: DuckDB connection, path to .duckdb file, or directory.
        date: Session date as 'YYYY-MM-DD' or datetime.date.
        stem: Symbol stem (e.g. '7203'). If omitted, inferred from file path.
        code: Symbol code (e.g. '72030'). If omitted, resolved from metadata.
        window_us: Window length in microseconds (default: 900_000_000 for 15 min).

    Returns:
        OpeningSessionData containing contiguous arrays and session opening metadata.

    Raises:
        NoMorningSessionError: If no executions >= 09:00:00 exist for the given day.
    """
    day_str = date.isoformat() if isinstance(date, Date) else str(date)

    if stem is None and not isinstance(source, duckdb.DuckDBPyConnection):
        stem = Path(source).stem

    with _open_duckdb(source, stem) as con:
        resolved_code = _resolve_code(con, code)
        if stem is None:
            stem = resolved_code

        # 1. Detect T_open (first morning execution >= 09:00:00)
        morning_start = f"{day_str} 09:00:00.000000"
        day_end = f"{day_str} 23:59:59.999999"

        first_rows = con.execute(
            f"SELECT Timestamp, epoch_us(TRY_CAST(Timestamp AS TIMESTAMP)) AS us, Price, Qty, Type "
            f"FROM {TRADES_TABLE} "
            f"WHERE Code = ? AND Timestamp >= ? AND Timestamp <= ? "
            f"ORDER BY Timestamp LIMIT 1",
            [resolved_code, morning_start, day_end],
        ).fetchall()

        if not first_rows:
            raise NoMorningSessionError(
                f"No morning session executions found for code {resolved_code} on {day_str} (>= 09:00:00)"
            )

        t_open_ts, t_open_us_val, p_open_val, v_open_val, _ = first_rows[0]
        if t_open_us_val is None or p_open_val is None:
            raise NoMorningSessionError(
                f"Invalid opening execution for code {resolved_code} on {day_str}"
            )

        t_open_us = int(t_open_us_val)
        p_open = float(p_open_val)
        v_open = int(v_open_val or 0)
        t_open_dt = _parse_timestamp(str(t_open_ts))

        # 2. Compute 15-minute window boundary
        t_end_us = t_open_us + window_us
        t_end_dt = t_open_dt + timedelta(microseconds=window_us)
        t_end_ts = t_end_dt.strftime("%Y-%m-%d %H:%M:%S.%f")

        # 3. Query all ticks in [T_open, T_open + window_us]
        rows = con.execute(
            f"SELECT epoch_us(TRY_CAST(Timestamp AS TIMESTAMP)) AS us, Price, Qty, Type "
            f"FROM {TRADES_TABLE} "
            f"WHERE Code = ? AND Timestamp >= ? AND Timestamp <= ? "
            f"ORDER BY Timestamp",
            [resolved_code, str(t_open_ts), t_end_ts],
        ).fetchall()

        us_list: list[int] = []
        price_list: list[float] = []
        qty_list: list[int] = []
        type_list: list[str] = []

        for row_us, row_price, row_qty, row_type in rows:
            if row_us is None or row_price is None:
                continue
            val_us = int(row_us)
            # Enforce exact microsecond window boundary
            if val_us < t_open_us or val_us > t_end_us:
                continue

            us_list.append(val_us)
            price_list.append(float(row_price))
            qty_list.append(int(row_qty or 0))
            type_list.append("" if row_type is None else str(row_type))

        if not us_list:
            raise NoMorningSessionError(
                f"No valid ticks found within 15m window for code {resolved_code} on {day_str}"
            )

        return OpeningSessionData(
            stem=stem,
            code=resolved_code,
            date=day_str,
            t_open_us=t_open_us,
            p_open=p_open,
            v_open=v_open,
            epoch_us=np.array(us_list, dtype=np.int64),
            price=np.array(price_list, dtype=np.float64),
            qty=np.array(qty_list, dtype=np.int64),
            trade_type=np.array(type_list, dtype=object),
        )


def get_session_dates(
    source: Path | str | duckdb.DuckDBPyConnection,
    *,
    stem: str | None = None,
    code: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
) -> list[str]:
    """Find all dates with morning trading activity (>= 09:00:00) in the DuckDB table."""
    with _open_duckdb(source, stem) as con:
        resolved_code = _resolve_code(con, code)
        where_clauses = [
            "Code = ?",
            "substr(Timestamp, 12, 8) >= '09:00:00'",
        ]
        params: list[object] = [resolved_code]
        if start_date is not None:
            where_clauses.append("Timestamp >= ?")
            params.append(f"{start_date} 00:00:00")
        if end_date is not None:
            where_clauses.append("Timestamp <= ?")
            params.append(f"{end_date} 23:59:59")

        where_sql = " AND ".join(where_clauses)
        rows = con.execute(
            f"SELECT DISTINCT substr(Timestamp, 1, 10) AS dt "
            f"FROM {TRADES_TABLE} "
            f"WHERE {where_sql} "
            f"ORDER BY dt",
            params,
        ).fetchall()
        return [str(row[0]) for row in rows if row[0]]


def extract_opening_session_from_ticks(
    stem: str,
    code: str,
    date: str | Date,
    us: list[int] | np.ndarray,
    price: list[float] | np.ndarray,
    qty: list[int] | np.ndarray,
    trade_type: list[str] | np.ndarray,
    *,
    window_us: int = OPENING_WINDOW_US,
) -> OpeningSessionData:
    """Extract bounded [T_open, T_open + 15m] ticks from in-memory session ticks.

    Finds T_open as the first execution in the morning session (Timestamp >= 09:00:00 JST / UTC epoch).
    """
    day_str = date.isoformat() if isinstance(date, Date) else str(date)
    morning_start_dt = datetime.fromisoformat(f"{day_str} 09:00:00+00:00")
    morning_start_us = int(morning_start_dt.timestamp() * 1_000_000)
    day_end_dt = datetime.fromisoformat(f"{day_str} 23:59:59.999999+00:00")
    day_end_us = int(day_end_dt.timestamp() * 1_000_000)

    us_arr = np.asarray(us, dtype=np.int64)
    price_arr = np.asarray(price, dtype=np.float64)
    qty_arr = np.asarray(qty, dtype=np.int64)
    type_arr = np.asarray(trade_type, dtype=object)

    if len(us_arr) == 0:
        raise NoMorningSessionError(f"No executions found for code {code} on {day_str}")

    morning_mask = (us_arr >= morning_start_us) & (us_arr <= day_end_us)
    morning_indices = np.where(morning_mask)[0]
    if len(morning_indices) == 0:
        raise NoMorningSessionError(
            f"No morning session executions found for code {code} on {day_str} (>= 09:00:00)"
        )

    first_idx = morning_indices[0]
    t_open_us = int(us_arr[first_idx])
    p_open = float(price_arr[first_idx])
    v_open = int(qty_arr[first_idx])
    t_end_us = t_open_us + window_us

    window_mask = (us_arr >= t_open_us) & (us_arr <= t_end_us)
    sliced_indices = np.where(window_mask)[0]
    if len(sliced_indices) == 0:
        raise NoMorningSessionError(
            f"No valid ticks found within 15m window for code {code} on {day_str}"
        )

    return OpeningSessionData(
        stem=stem,
        code=code,
        date=day_str,
        t_open_us=t_open_us,
        p_open=p_open,
        v_open=v_open,
        epoch_us=us_arr[sliced_indices],
        price=price_arr[sliced_indices],
        qty=qty_arr[sliced_indices],
        trade_type=type_arr[sliced_indices],
    )

