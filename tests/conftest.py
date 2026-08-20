"""Shared fixtures.

``tick_db_factory`` builds a DuckDB file with the same shape as the real
``stocks_trades`` files so the repository and API can be tested without the
multi-gigabyte production data.
"""

from __future__ import annotations

from pathlib import Path

import pytest

CREATE_TRADES = """
CREATE TABLE stocks_board (
    Price DOUBLE,
    Qty BIGINT,
    Type VARCHAR,
    source VARCHAR,
    Code VARCHAR,
    Timestamp VARCHAR
)
"""

CREATE_METADATA = """
CREATE TABLE stocks_board_metadata (
    Code VARCHAR,
    from_timestamp TIMESTAMP,
    to_timestamp TIMESTAMP,
    record_count BIGINT,
    last_updated TIMESTAMP
)
"""


@pytest.fixture
def trades_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "stocks_trades"
    directory.mkdir()
    return directory


@pytest.fixture
def tick_db_factory(trades_dir: Path):
    """Return ``make(stem, rows, metadata=None)`` creating one tick file.

    ``rows`` are ``(timestamp, price, qty, type, code)`` tuples. When metadata
    is not supplied it is derived from the rows of the dominant code.
    """
    import duckdb

    def make(stem: str, rows: list[tuple], metadata: list[tuple] | None = None) -> Path:
        path = trades_dir / f"{stem}.duckdb"
        connection = duckdb.connect(str(path))
        try:
            connection.execute(CREATE_TRADES)
            connection.execute(CREATE_METADATA)
            for timestamp, price, qty, trade_type, code in rows:
                connection.execute(
                    "INSERT INTO stocks_board VALUES (?, ?, ?, 'test', ?, ?)",
                    [price, qty, trade_type, code, timestamp],
                )

            if metadata is None:
                by_code: dict[str, list[str]] = {}
                for timestamp, _price, _qty, _type, code in rows:
                    by_code.setdefault(code, []).append(timestamp)
                metadata = [
                    (code, min(stamps), max(stamps), len(stamps))
                    for code, stamps in by_code.items()
                ]
            for code, first, last, count in metadata:
                connection.execute(
                    "INSERT INTO stocks_board_metadata "
                    "VALUES (?, CAST(? AS TIMESTAMP), CAST(? AS TIMESTAMP), ?, "
                    "CAST('2026-08-20 00:00:00' AS TIMESTAMP))",
                    [code, first, last, count],
                )
        finally:
            connection.close()
        return path

    return make
