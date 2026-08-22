"""Tests for `minute_context.py`'s best-effort stocks_minute preload.

Unlike `TickRepository`, every failure mode here (missing file, unreachable
server, corrupt download, invalid stem) must degrade to an empty list rather
than raise — see the module docstring. That contract is what these tests
pin down.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from tickreplay import minute_context

ROWS_7203 = [
    ("2024-04-01", "09:00", "72030", 3801.0, 3824.0, 3801.0, 3820.0, 1682000, 6401186800),
    ("2024-04-01", "09:01", "72030", 3820.0, 3824.0, 3813.0, 3817.0, 235700, 899600100),
    ("2024-04-01", "14:59", "72030", 3643.0, 3644.0, 3640.0, 3640.0, 351700, 1280554100),
    ("2024-04-01", "15:00", "72030", 3639.0, 3639.0, 3639.0, 3639.0, 4476400, 16289619600),
]


def test_bars_before_downloads_and_returns_chronological_order(
    cache_dir: Path, http_client: httpx.Client, minute_db_factory
):
    minute_db_factory("7203", ROWS_7203)

    bars = minute_context.bars_before(
        cache_dir,
        http_client,
        stem="7203",
        code="72030",
        before_date="2024-04-02",
        before_time="09:00",
        limit=3,
    )

    assert [b.time for b in bars] == sorted(b.time for b in bars)
    assert len(bars) == 3
    # oldest-first: the 09:01/14:59/15:00 bars, not 09:00 (limit=3 keeps the
    # 3 nearest to the cutoff).
    assert bars[-1].close == 3639.0
    assert bars[-1].volume == 4476400

    assert (cache_dir / "stocks_minute" / "7203.duckdb").is_file()


def test_bars_before_respects_before_cutoff(
    cache_dir: Path, http_client: httpx.Client, minute_db_factory
):
    minute_db_factory("7203", ROWS_7203)

    bars = minute_context.bars_before(
        cache_dir,
        http_client,
        stem="7203",
        code="72030",
        before_date="2024-04-01",
        before_time="09:01",
        limit=10,
    )

    assert len(bars) == 1
    assert bars[0].open == 3801.0


def test_bars_before_missing_remote_file_returns_empty(
    cache_dir: Path, http_client: httpx.Client
):
    # No minute_db_factory call — the mock server has nothing for "9999",
    # so the download 404s.
    bars = minute_context.bars_before(
        cache_dir,
        http_client,
        stem="9999",
        code="99990",
        before_date="2024-04-02",
        before_time="09:00",
        limit=5,
    )
    assert bars == []


def test_bars_before_unreachable_server_returns_empty(cache_dir: Path):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://fake")
    bars = minute_context.bars_before(
        cache_dir,
        client,
        stem="7203",
        code="72030",
        before_date="2024-04-02",
        before_time="09:00",
        limit=5,
    )
    assert bars == []


def test_bars_before_invalid_stem_returns_empty(
    cache_dir: Path, http_client: httpx.Client
):
    bars = minute_context.bars_before(
        cache_dir,
        http_client,
        stem="../../etc",
        code="72030",
        before_date="2024-04-02",
        before_time="09:00",
        limit=5,
    )
    assert bars == []


def test_bars_before_reuses_local_cache_without_redownloading(
    cache_dir: Path, http_client: httpx.Client, minute_db_factory
):
    minute_db_factory("7203", ROWS_7203)
    first = minute_context.bars_before(
        cache_dir, http_client, stem="7203", code="72030",
        before_date="2024-04-02", before_time="09:00", limit=3,
    )
    assert len(first) == 3

    def fail(request: httpx.Request) -> httpx.Response:
        raise AssertionError("must not re-download once cached locally")

    client2 = httpx.Client(transport=httpx.MockTransport(fail), base_url="http://fake")
    second = minute_context.bars_before(
        cache_dir, client2, stem="7203", code="72030",
        before_date="2024-04-02", before_time="09:00", limit=3,
    )
    assert second == first


def test_bars_before_corrupt_download_returns_empty(cache_dir: Path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not a duckdb file")

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://fake")
    bars = minute_context.bars_before(
        cache_dir,
        client,
        stem="7203",
        code="72030",
        before_date="2024-04-02",
        before_time="09:00",
        limit=5,
    )
    assert bars == []
    assert not (cache_dir / "stocks_minute" / "7203.duckdb").is_file()
    assert not (cache_dir / "stocks_minute" / "7203.duckdb.part").is_file()
