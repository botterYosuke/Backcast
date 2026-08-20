"""Read-only access to the per-symbol tick DuckDB files.

Layout (verified against the live data set):

* ``<root>/stocks_trades/<stem>.duckdb`` holds ``stocks_board`` and
  ``stocks_board_metadata``.
* ``stocks_board``: ``Price DOUBLE, Qty BIGINT, Type VARCHAR, source VARCHAR,
  Code VARCHAR, Timestamp VARCHAR`` with a primary key on ``(Code, Timestamp)``.
  That primary key is what makes a single-session range scan fast even on the
  4 GB files, so every query filters on ``Code`` and a ``Timestamp`` range.
* A file can contain more than one ``Code`` (e.g. ``3823.duckdb`` carries both
  ``38230`` with 508k rows and a two-row ``3823`` artifact), so the canonical
  code is taken from the metadata row with the largest ``record_count`` rather
  than from the filename.

``Timestamp`` is stored as text without a timezone. It is converted to epoch
microseconds by interpreting it as UTC, which keeps the stored wall clock
intact when the browser renders it (Lightweight Charts formats in UTC). No
claim is made about the true timezone of the source data.
"""

from __future__ import annotations

import re
import threading
from collections import OrderedDict
from dataclasses import dataclass
from datetime import date as Date
from datetime import datetime, timedelta
from pathlib import Path

import duckdb

# Symbol files are 4-5 character alphanumeric codes. Anything else in the
# directory is a sync artifact (e.g. "130A_ADMIN_..._Conflict.duckdb").
SYMBOL_STEM_RE = re.compile(r"^[0-9A-Z]{4,5}$")

TRADES_TABLE = "stocks_board"
METADATA_TABLE = "stocks_board_metadata"

MAX_OPEN_CONNECTIONS = 8
MAX_SESSION_PROBE_STEPS = 20


class SymbolNotFoundError(LookupError):
    """The requested symbol has no DuckDB file under the trades directory."""


@dataclass(frozen=True)
class SymbolInfo:
    """Identity and coverage of one tick file."""

    stem: str
    code: str
    first_timestamp: datetime
    last_timestamp: datetime
    record_count: int
    other_codes: tuple[str, ...] = ()

    @property
    def first_date(self) -> str:
        return self.first_timestamp.date().isoformat()

    @property
    def last_date(self) -> str:
        return self.last_timestamp.date().isoformat()

    def as_dict(self) -> dict[str, object]:
        return {
            "stem": self.stem,
            "code": self.code,
            "firstDate": self.first_date,
            "lastDate": self.last_date,
            "recordCount": self.record_count,
            "otherCodes": list(self.other_codes),
        }


@dataclass(frozen=True)
class SessionTicks:
    """One trading session of executions, in column form."""

    stem: str
    code: str
    date: str
    us: list[int]
    price: list[float]
    qty: list[int]
    trade_type: list[str]

    def as_dict(self) -> dict[str, object]:
        return {
            "stem": self.stem,
            "code": self.code,
            "date": self.date,
            "count": len(self.us),
            "us": self.us,
            "price": self.price,
            "qty": self.qty,
            "type": self.trade_type,
        }


class _ConnectionPool:
    """Bounded cache of read-only DuckDB connections.

    Opening a multi-gigabyte file costs more than the query itself, so
    connections are reused. DuckDB connections are not safe for concurrent use,
    hence the per-pool lock around every statement.
    """

    def __init__(self, max_size: int = MAX_OPEN_CONNECTIONS) -> None:
        self._max_size = max_size
        self._lock = threading.Lock()
        self._connections: OrderedDict[Path, duckdb.DuckDBPyConnection] = OrderedDict()

    def query(
        self, path: Path, sql: str, params: list[object] | None = None
    ) -> list[tuple]:
        with self._lock:
            connection = self._connections.pop(path, None)
            if connection is None:
                connection = duckdb.connect(str(path), read_only=True)
            self._connections[path] = connection
            while len(self._connections) > self._max_size:
                _, evicted = self._connections.popitem(last=False)
                evicted.close()
            return connection.execute(sql, params or []).fetchall()

    def close(self) -> None:
        with self._lock:
            for connection in self._connections.values():
                connection.close()
            self._connections.clear()


class TickRepository:
    """Read-only façade over the ``stocks_trades`` directory."""

    def __init__(self, trades_dir: Path) -> None:
        self.trades_dir = trades_dir
        self._pool = _ConnectionPool()
        self._info_cache: dict[str, SymbolInfo] = {}
        self._info_lock = threading.Lock()

    # -- symbols ---------------------------------------------------------

    def list_symbol_stems(self) -> list[str]:
        """Return the symbol file stems, cheaply (directory listing only)."""
        stems = [
            path.stem
            for path in self.trades_dir.glob("*.duckdb")
            if SYMBOL_STEM_RE.match(path.stem)
        ]
        return sorted(stems)

    def path_for(self, stem: str) -> Path:
        if not SYMBOL_STEM_RE.match(stem):
            raise SymbolNotFoundError(f"invalid symbol identifier: {stem!r}")
        path = self.trades_dir / f"{stem}.duckdb"
        if not path.is_file():
            raise SymbolNotFoundError(f"no tick file for symbol {stem}")
        return path

    def symbol_info(self, stem: str) -> SymbolInfo:
        """Read identity and coverage from the metadata table (no table scan)."""
        with self._info_lock:
            cached = self._info_cache.get(stem)
        if cached is not None:
            return cached

        path = self.path_for(stem)
        rows = self._pool.query(
            path,
            f"SELECT Code, from_timestamp, to_timestamp, record_count "
            f"FROM {METADATA_TABLE} ORDER BY record_count DESC",
        )
        if not rows:
            raise SymbolNotFoundError(f"{stem}: {METADATA_TABLE} is empty")

        code, first, last, count = rows[0]
        info = SymbolInfo(
            stem=stem,
            code=str(code),
            first_timestamp=first,
            last_timestamp=last,
            record_count=int(count),
            other_codes=tuple(str(row[0]) for row in rows[1:]),
        )
        with self._info_lock:
            self._info_cache[stem] = info
        return info

    # -- sessions --------------------------------------------------------

    def has_session(self, stem: str, day: Date) -> bool:
        path = self.path_for(stem)
        info = self.symbol_info(stem)
        rows = self._pool.query(
            path,
            f"SELECT 1 FROM {TRADES_TABLE} "
            f"WHERE Code = ? AND Timestamp >= ? AND Timestamp < ? LIMIT 1",
            [info.code, day.isoformat(), (day + timedelta(days=1)).isoformat()],
        )
        return bool(rows)

    def find_session(self, stem: str, day: Date, direction: int) -> Date | None:
        """Return the nearest day with data at or beyond ``day``.

        ``direction`` is ``+1`` (forward), ``-1`` (backward) or ``0`` (only the
        given day). Weekends are skipped without touching the database; the
        remaining probes are indexed range look-ups.
        """
        info = self.symbol_info(stem)
        first, last = info.first_timestamp.date(), info.last_timestamp.date()

        if direction == 0:
            return day if self.has_session(stem, day) else None

        current = day
        for _ in range(MAX_SESSION_PROBE_STEPS):
            if current < first or current > last:
                return None
            if current.weekday() < 5 and self.has_session(stem, current):
                return current
            current += timedelta(days=direction)
        return None

    def load_session(self, stem: str, day: Date) -> SessionTicks:
        """Load every execution of one session, ordered by timestamp."""
        path = self.path_for(stem)
        info = self.symbol_info(stem)
        rows = self._pool.query(
            path,
            f"SELECT epoch_us(TRY_CAST(Timestamp AS TIMESTAMP)) AS us, Price, Qty, Type "
            f"FROM {TRADES_TABLE} "
            f"WHERE Code = ? AND Timestamp >= ? AND Timestamp < ? "
            f"ORDER BY Timestamp",
            [info.code, day.isoformat(), (day + timedelta(days=1)).isoformat()],
        )

        us: list[int] = []
        price: list[float] = []
        qty: list[int] = []
        trade_type: list[str] = []
        previous_us = -1
        for row_us, row_price, row_qty, row_type in rows:
            # An unparseable timestamp cannot be placed on a time axis; a null
            # price cannot be drawn. Both are dropped rather than guessed.
            if row_us is None or row_price is None:
                continue
            # Executions can share a microsecond. Lightweight Charts needs a
            # strictly increasing time axis, so collisions are nudged forward
            # by one microsecond instead of being collapsed.
            value = int(row_us)
            if value <= previous_us:
                value = previous_us + 1
            previous_us = value
            us.append(value)
            price.append(float(row_price))
            qty.append(int(row_qty or 0))
            trade_type.append("" if row_type is None else str(row_type))

        return SessionTicks(
            stem=stem,
            code=info.code,
            date=day.isoformat(),
            us=us,
            price=price,
            qty=qty,
            trade_type=trade_type,
        )

    def close(self) -> None:
        self._pool.close()
