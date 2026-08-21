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


def _mock_handler(remote_store: dict[str, bytes]):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/stocks-trades":
            return httpx.Response(200, json={"stems": sorted(remote_store)})
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
def mock_transport(remote_store: dict[str, bytes]) -> httpx.MockTransport:
    """Serves `/api/stocks-trades` and `/jp/stocks_trades/<stem>.duckdb`
    (with real conditional-GET semantics) straight out of `remote_store`."""
    return httpx.MockTransport(_mock_handler(remote_store))


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
