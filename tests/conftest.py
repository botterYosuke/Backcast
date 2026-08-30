"""Shared fixtures.

``tick_db_factory`` builds a DuckDB file with the same shape as the real
``stocks_trades`` files so the repository and API can be tested without the
multi-gigabyte production data. Since Step 6 of
``.agents/docs/plans/duckdb-server-cache.md``, ``TickRepository`` reads from
a local cache directory that it populates itself over HTTP — so
``tick_db_factory`` registers each file's bytes with ``remote_store``, an
in-memory stand-in for the file server's ``jp/stocks_trades`` directory,
served by ``mock_transport``/``http_client``.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import httpx
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

# Shape verified against a real `jp/stocks_minute/<stem>.duckdb` file
# (`stocks_minute`'s columns; `stocks_minute_metadata` is unused by
# `minute_context.py` and so not modeled here).
CREATE_MINUTE = """
CREATE TABLE stocks_minute (
    Date DATE,
    "Time" VARCHAR,
    Code VARCHAR,
    Open DOUBLE,
    High DOUBLE,
    Low DOUBLE,
    Close DOUBLE,
    Volume BIGINT,
    Value BIGINT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
"""

# Shape verified against the columns consumed by the existing Cloud Run
# ``stocks_daily`` ranking query. Adjustment values are deliberately present
# so the TickReplay loader can prove it reads only the raw OHLCV columns.
CREATE_DAILY = """
CREATE TABLE stocks_daily (
    Code VARCHAR,
    Date VARCHAR,
    Open DOUBLE,
    High DOUBLE,
    Low DOUBLE,
    Close DOUBLE,
    Volume DOUBLE,
    AdjustmentOpen DOUBLE,
    AdjustmentHigh DOUBLE,
    AdjustmentLow DOUBLE,
    AdjustmentClose DOUBLE,
    AdjustmentVolume DOUBLE
)
"""


@pytest.fixture
def cache_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "cache"
    directory.mkdir()
    return directory


@pytest.fixture
def remote_store() -> dict[str, bytes]:
    """In-memory ``{stem: bytes}`` standing in for the file server's
    ``jp/stocks_trades`` directory, served by `mock_transport`."""
    return {}


@pytest.fixture
def tick_db_factory(remote_store: dict[str, bytes], tmp_path: Path):
    """Return ``make(stem, rows, metadata=None)``, registering one tick
    file's bytes in ``remote_store`` (as the file server would serve it)."""
    import duckdb

    call_counter = {"n": 0}

    def make(
        stem: str, rows: list[tuple], metadata: list[tuple] | None = None
    ) -> bytes:
        # A unique path per call: a test may register the same stem twice
        # (e.g. to simulate the remote file changing), and re-registering
        # must not collide with an already-built local source file.
        call_counter["n"] += 1
        path = tmp_path / f"_source_{stem}_{call_counter['n']}.duckdb"
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
        data = path.read_bytes()
        remote_store[stem] = data
        return data

    return make


@pytest.fixture
def minute_remote_store() -> dict[str, bytes]:
    """In-memory ``{stem: bytes}`` standing in for the file server's
    ``jp/stocks_minute`` directory, served by `mock_transport`. Empty by
    default (and thus a 404 for every stem) unless a test populates it via
    `minute_db_factory`."""
    return {}


@pytest.fixture
def minute_db_factory(minute_remote_store: dict[str, bytes], tmp_path: Path):
    """Return ``make(stem, rows)``, registering one `stocks_minute` file's
    bytes in ``minute_remote_store``. Each row is ``(date, time, code, open,
    high, low, close, volume, value)`` — `date` and `time` as ISO strings
    (e.g. ``"2024-04-01"``, ``"09:00"``)."""
    import duckdb

    call_counter = {"n": 0}

    def make(stem: str, rows: list[tuple]) -> bytes:
        call_counter["n"] += 1
        path = tmp_path / f"_minute_source_{stem}_{call_counter['n']}.duckdb"
        connection = duckdb.connect(str(path))
        try:
            connection.execute(CREATE_MINUTE)
            for date, time, code, o, h, low, c, volume, value in rows:
                connection.execute(
                    'INSERT INTO stocks_minute (Date, "Time", Code, Open, High, '
                    "Low, Close, Volume, Value) "
                    "VALUES (CAST(? AS DATE), ?, ?, ?, ?, ?, ?, ?, ?)",
                    [date, time, code, o, h, low, c, volume, value],
                )
        finally:
            connection.close()
        data = path.read_bytes()
        minute_remote_store[stem] = data
        return data

    return make


@pytest.fixture
def daily_remote_store() -> dict[str, bytes]:
    """In-memory per-stem ``jp/stocks_daily`` file server."""
    return {}


@pytest.fixture
def daily_db_factory(daily_remote_store: dict[str, bytes], tmp_path: Path):
    """Build and register a per-stem ``stocks_daily`` DuckDB file.

    Rows contain ``(code, date, raw O/H/L/C/V, adjusted O/H/L/C/V)``.
    ``Date`` is stored as text to exercise the runtime's defensive date cast.
    """
    import duckdb

    call_counter = {"n": 0}

    def make(stem: str, rows: list[tuple]) -> bytes:
        call_counter["n"] += 1
        path = tmp_path / f"_daily_source_{stem}_{call_counter['n']}.duckdb"
        connection = duckdb.connect(str(path))
        try:
            connection.execute(CREATE_DAILY)
            connection.executemany(
                "INSERT INTO stocks_daily VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
        finally:
            connection.close()
        data = path.read_bytes()
        daily_remote_store[stem] = data
        return data

    return make


def _mock_handler(
    remote_store: dict[str, bytes],
    minute_store: dict[str, bytes],
    daily_store: dict[str, bytes],
):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/stocks-trades":
            return httpx.Response(200, json={"stems": sorted(remote_store)})
        if request.url.path.startswith("/jp/stocks_minute/"):
            stem = request.url.path.rsplit("/", 1)[-1]
            if stem.endswith(".duckdb"):
                stem = stem[: -len(".duckdb")]
            body = minute_store.get(stem)
            if body is None:
                return httpx.Response(404)
            return httpx.Response(
                200, headers={"Content-Length": str(len(body))}, content=body
            )
        if request.url.path.startswith("/jp/stocks_daily/"):
            stem = request.url.path.rsplit("/", 1)[-1]
            if stem.endswith(".duckdb"):
                stem = stem[: -len(".duckdb")]
            body = daily_store.get(stem)
            if body is None:
                return httpx.Response(404)
            etag = f'"{hashlib.sha256(body).hexdigest()}"'
            if request.headers.get("if-none-match") == etag:
                return httpx.Response(304)
            return httpx.Response(
                200,
                headers={"Content-Length": str(len(body)), "ETag": etag},
                content=body,
            )
        assert request.url.path.startswith("/jp/stocks_trades/")
        stem = request.url.path.rsplit("/", 1)[-1]
        if stem.endswith(".duckdb"):
            stem = stem[: -len(".duckdb")]
        body = remote_store.get(stem)
        if body is None:
            return httpx.Response(404)
        etag = f'"{hashlib.sha256(body).hexdigest()}"'
        if request.headers.get("if-none-match") == etag:
            return httpx.Response(304)
        return httpx.Response(
            200, headers={"Content-Length": str(len(body)), "ETag": etag}, content=body
        )

    return handler


@pytest.fixture
def mock_transport(
    remote_store: dict[str, bytes],
    minute_remote_store: dict[str, bytes],
    daily_remote_store: dict[str, bytes],
) -> httpx.MockTransport:
    """Serves `/api/stocks-trades`, `/jp/stocks_trades/<stem>.duckdb` (with
    real conditional-GET semantics), plus the per-stem minute/daily files,
    straight out of the matching in-memory stores."""
    return httpx.MockTransport(
        _mock_handler(remote_store, minute_remote_store, daily_remote_store)
    )


@pytest.fixture
def http_client(mock_transport: httpx.MockTransport):
    client = httpx.Client(
        base_url="http://cache-server.invalid", transport=mock_transport
    )
    yield client
    client.close()


@pytest.fixture
def repository(cache_dir: Path, http_client: httpx.Client):
    from tickreplay.repository import TickRepository

    repo = TickRepository(
        cache_dir=cache_dir,
        server_url="http://cache-server.invalid",
        http_client=http_client,
    )
    yield repo
    repo.close()
