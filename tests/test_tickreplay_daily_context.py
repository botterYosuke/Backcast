"""Contract tests for the official per-stem daily-history loader."""

from __future__ import annotations

import hashlib
import threading
from datetime import date, timedelta
from pathlib import Path

import duckdb
import httpx
import pytest

from tickreplay import daily_context
from tickreplay.cache import Sidecar


def daily_row(
    code: str,
    day: str,
    open_: float,
    high: float,
    low: float,
    close: float,
    volume: float,
) -> tuple:
    adjusted = (open_ + 1000, high + 1000, low + 1000, close + 1000, volume + 1000)
    return (code, day, open_, high, low, close, volume, *adjusted)


def load(
    cache_dir: Path,
    http_client: httpx.Client,
    *,
    stem: str = "285A",
    before_date: str = "2024-01-06",
    limit: int = 500,
    local_authoritative: bool = False,
) -> daily_context.DailyContextResult:
    return daily_context.load_daily_context(
        cache_dir,
        http_client,
        stem=stem,
        before_date=before_date,
        limit=limit,
        local_authoritative=local_authoritative,
    )


def wrong_schema_database(tmp_path: Path, name: str) -> bytes:
    path = tmp_path / name
    with duckdb.connect(str(path)) as connection:
        connection.execute("CREATE TABLE wrong_table (value INTEGER)")
    return path.read_bytes()


@pytest.mark.parametrize(
    ("stem", "before_date", "limit"),
    [
        ("../A", "2024-01-06", 1),
        ("123", "2024-01-06", 1),
        ("123456", "2024-01-06", 1),
        ("285A", "06/01/2024", 1),
        ("285A", "2024-01-06", 0),
        ("285A", "2024-01-06", 501),
    ],
)
def test_load_daily_context_rejects_invalid_input(
    cache_dir: Path,
    http_client: httpx.Client,
    stem: str,
    before_date: str,
    limit: int,
):
    with pytest.raises(ValueError):
        load(
            cache_dir,
            http_client,
            stem=stem,
            before_date=before_date,
            limit=limit,
        )


def test_daily_context_is_strict_before_raw_cross_code_and_deterministic(
    cache_dir: Path, http_client: httpx.Client, daily_db_factory
):
    exact = daily_row("285A0", "2024-01-02", 10, 12, 9, 11, 100)
    daily_db_factory(
        "285A",
        [
            exact,
            exact,
            daily_row("285A", "2024-01-03", 20, 22, 19, 21, 200),
            daily_row("285A", "2024-01-04", 30, 32, 29, 31, 300),
            daily_row("285A0", "2024-01-04", 30, 33, 29, 31, 300),
            daily_row("285A", "2024-01-05", 40, 39, 38, 41, 400),
            daily_row("285A", "2024-01-06", 900, 999, 800, 950, 9999),
        ],
    )

    result = load(cache_dir, http_client)

    assert result.available is True
    assert [bar.time for bar in result.bars] == ["2024-01-02", "2024-01-03"]
    assert result.bars[0].as_dict() == {
        "time": "2024-01-02",
        "open": 10.0,
        "high": 12.0,
        "low": 9.0,
        "close": 11.0,
        "volume": 100.0,
    }
    assert all(bar.close < 1000 for bar in result.bars)


def test_invalid_row_invalidates_the_whole_date_group(
    cache_dir: Path, http_client: httpx.Client, daily_db_factory
):
    daily_db_factory(
        "285A",
        [
            daily_row("285A", "2024-01-02", 10, 12, 9, 11, 100),
            daily_row("285A0", "2024-01-02", 10, 12, 9, 11, -1),
        ],
    )

    result = load(cache_dir, http_client)

    assert result == daily_context.DailyContextResult(bars=(), available=True)


@pytest.mark.parametrize(
    ("open_", "high", "low", "close", "volume"),
    [
        (0, 12, 9, 11, 100),
        (10, float("inf"), 9, 11, 100),
        (10, 12, float("nan"), 11, 100),
        (10, 12, 9, -1, 100),
        (10, 9, 8, 11, 100),
        (10, 12, 11, 9, 100),
        (10, 12, 9, 11, -1),
    ],
)
def test_invalid_ohlcv_invariants_omit_the_date_but_keep_database_available(
    cache_dir: Path,
    http_client: httpx.Client,
    daily_db_factory,
    open_: float,
    high: float,
    low: float,
    close: float,
    volume: float,
):
    daily_db_factory(
        "285A", [daily_row("285A", "2024-01-02", open_, high, low, close, volume)]
    )

    result = load(cache_dir, http_client)

    assert result == daily_context.DailyContextResult(bars=(), available=True)


def test_unparseable_date_row_is_omitted_from_a_successful_query(
    cache_dir: Path, http_client: httpx.Client, daily_db_factory
):
    daily_db_factory("285A", [daily_row("285A", "not-a-date", 10, 12, 9, 11, 100)])

    assert load(cache_dir, http_client) == daily_context.DailyContextResult(
        bars=(), available=True
    )


def test_query_limits_newest_rows_in_duckdb_then_returns_ascending(
    cache_dir: Path, http_client: httpx.Client, daily_db_factory
):
    first = date(2022, 1, 1)
    rows = [
        daily_row(
            "285A" if index % 2 else "285A0",
            (first + timedelta(days=index)).isoformat(),
            100 + index,
            102 + index,
            99 + index,
            101 + index,
            1000 + index,
        )
        for index in range(501)
    ]
    daily_db_factory("285A", rows)

    result = load(cache_dir, http_client, before_date="2025-01-01")

    assert result.available is True
    assert len(result.bars) == 500
    assert result.bars[0].time == (first + timedelta(days=1)).isoformat()
    assert result.bars[-1].time == (first + timedelta(days=500)).isoformat()
    assert list(result.bars) == sorted(result.bars, key=lambda bar: bar.time)


def test_limit_one_returns_the_newest_eligible_date(
    cache_dir: Path, http_client: httpx.Client, daily_db_factory
):
    daily_db_factory(
        "285A",
        [
            daily_row("285A0", "2024-01-02", 10, 12, 9, 11, 100),
            daily_row("285A", "2024-01-03", 20, 22, 19, 21, 200),
        ],
    )

    result = load(cache_dir, http_client, limit=1)

    assert [bar.time for bar in result.bars] == ["2024-01-03"]


def test_adjacent_strict_before_pages_are_disjoint_and_end_with_available_empty(
    cache_dir: Path, http_client: httpx.Client, daily_db_factory
):
    first = date(2024, 1, 1)
    daily_db_factory(
        "285A",
        [
            daily_row(
                "285A" if index % 2 else "285A0",
                (first + timedelta(days=index)).isoformat(),
                100 + index,
                102 + index,
                99 + index,
                101 + index,
                1_000 + index,
            )
            for index in range(6)
        ],
    )

    newest_page = load(
        cache_dir,
        http_client,
        before_date="2024-01-07",
        limit=3,
    )
    older_page = load(
        cache_dir,
        http_client,
        before_date=newest_page.bars[0].time,
        limit=3,
    )
    exhausted_page = load(
        cache_dir,
        http_client,
        before_date=older_page.bars[0].time,
        limit=3,
    )

    newest_dates = [bar.time for bar in newest_page.bars]
    older_dates = [bar.time for bar in older_page.bars]
    assert newest_page.available is True
    assert older_page.available is True
    assert newest_dates == sorted(newest_dates)
    assert older_dates == sorted(older_dates)
    assert len(newest_dates) == len(older_dates) == 3
    assert set(newest_dates).isdisjoint(older_dates)
    assert max(older_dates) < min(newest_dates)
    assert exhausted_page == daily_context.DailyContextResult(
        bars=(), available=True
    )


def test_executed_sql_is_bounded_raw_only_and_has_no_code_filter(
    cache_dir: Path, http_client: httpx.Client, daily_db_factory, monkeypatch
):
    daily_db_factory("285A", [daily_row("285A", "2024-01-02", 10, 12, 9, 11, 100)])
    real_connect = daily_context.duckdb.connect
    statements: list[str] = []

    class RecordingConnection:
        def __init__(self, *args, **kwargs):
            self.connection = real_connect(*args, **kwargs)

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            self.connection.close()

        def execute(self, sql, parameters=None):
            statements.append(sql)
            if parameters is None:
                return self.connection.execute(sql)
            return self.connection.execute(sql, parameters)

    monkeypatch.setattr(daily_context.duckdb, "connect", RecordingConnection)

    assert load(cache_dir, http_client).available is True
    query = next(sql for sql in statements if "WITH source_rows" in sql)
    folded = query.casefold()
    assert "limit ?" in folded
    assert '"date"' in folded and "< cast(? as date)" in folded
    assert "adjustment" not in folded
    assert '"code"' not in folded


def test_valid_database_with_no_eligible_rows_is_available(
    cache_dir: Path, http_client: httpx.Client, daily_db_factory
):
    daily_db_factory("285A", [daily_row("285A", "2024-01-06", 10, 12, 9, 11, 100)])

    assert load(cache_dir, http_client) == daily_context.DailyContextResult(
        bars=(), available=True
    )


def test_zero_volume_row_remains_available_and_preserves_zero(
    cache_dir: Path, http_client: httpx.Client, daily_db_factory
):
    daily_db_factory("285A", [daily_row("285A", "2024-01-02", 10, 12, 9, 11, 0)])

    result = load(cache_dir, http_client)

    assert result.available is True
    assert len(result.bars) == 1
    assert result.bars[0].volume == 0.0


def test_missing_remote_file_is_unavailable(cache_dir: Path, http_client: httpx.Client):
    assert load(cache_dir, http_client) == daily_context.DailyContextResult(
        bars=(), available=False
    )


def test_authoritative_missing_file_never_uses_loopback_http(cache_dir: Path):
    def unexpected_request(request: httpx.Request) -> httpx.Response:
        raise AssertionError("authoritative daily cache must be local-only")

    with httpx.Client(
        transport=httpx.MockTransport(unexpected_request), base_url="http://loopback"
    ) as client:
        assert load(
            cache_dir, client, local_authoritative=True
        ) == daily_context.DailyContextResult(bars=(), available=False)


def test_unreachable_remote_file_is_unavailable(cache_dir: Path):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    with httpx.Client(
        transport=httpx.MockTransport(handler), base_url="http://invalid"
    ) as client:
        assert load(cache_dir, client) == daily_context.DailyContextResult(
            bars=(), available=False
        )


def test_part_cleanup_permission_error_propagates_to_controlled_unavailability(
    cache_dir: Path, monkeypatch
):
    part_path = cache_dir / "stocks_daily" / "285A.duckdb.part"
    part_path.parent.mkdir()
    part_path.write_bytes(b"stale")
    real_unlink = Path.unlink

    def deny_part_cleanup(path: Path, *, missing_ok: bool = False) -> None:
        if path == part_path:
            raise PermissionError("cleanup denied")
        real_unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", deny_part_cleanup)

    with pytest.raises(PermissionError, match="cleanup denied"):
        daily_context._discard(part_path)

    def unexpected_request(request: httpx.Request) -> httpx.Response:
        raise AssertionError("download must not start after cleanup fails")

    with httpx.Client(
        transport=httpx.MockTransport(unexpected_request), base_url="http://invalid"
    ) as client:
        assert load(cache_dir, client) == daily_context.DailyContextResult(
            bars=(), available=False
        )


def test_corrupt_download_is_unavailable_and_not_committed(cache_dir: Path):
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content=b"bad")
        ),
        base_url="http://invalid",
    )
    try:
        result = load(cache_dir, client)
    finally:
        client.close()

    assert result.available is False
    assert not (cache_dir / "stocks_daily" / "285A.duckdb").exists()
    assert not (cache_dir / "stocks_daily" / "285A.duckdb.part").exists()


def test_query_failure_is_unavailable(cache_dir: Path, http_client: httpx.Client):
    path = cache_dir / "stocks_daily" / "285A.duckdb"
    path.parent.mkdir()
    with duckdb.connect(str(path)) as connection:
        connection.execute("CREATE TABLE wrong_table (value INTEGER)")

    assert load(cache_dir, http_client) == daily_context.DailyContextResult(
        bars=(), available=False
    )


def test_remote_cache_revalidates_once_per_process_and_refreshes_changed_origin(
    cache_dir: Path, http_client: httpx.Client, daily_db_factory
):
    daily_db_factory("285A", [daily_row("285A", "2024-01-02", 10, 12, 9, 11, 100)])
    first = load(cache_dir, http_client)
    daily_db_factory("285A", [daily_row("285A", "2024-01-02", 20, 22, 19, 21, 200)])

    daily_context.reset_for_tests()
    refreshed = load(cache_dir, http_client)

    assert first.bars[0].close == 11
    assert refreshed.bars[0].close == 21


def test_authoritative_cache_never_revalidates_existing_local_file(
    cache_dir: Path, daily_db_factory
):
    original = daily_db_factory(
        "285A", [daily_row("285A", "2024-01-02", 10, 12, 9, 11, 100)]
    )
    path = cache_dir / "stocks_daily" / "285A.duckdb"
    path.parent.mkdir()
    path.write_bytes(original)

    def unexpected_request(request: httpx.Request) -> httpx.Response:
        raise AssertionError("authoritative daily cache must not fetch")

    with httpx.Client(
        transport=httpx.MockTransport(unexpected_request), base_url="http://loopback"
    ) as client:
        result = load(cache_dir, client, local_authoritative=True)

    assert result.available is True
    assert result.bars[0].close == 11


def test_authoritative_oversize_local_file_is_unavailable_without_fetch(
    cache_dir: Path, monkeypatch
):
    monkeypatch.setattr(daily_context, "MAX_DAILY_FILE_BYTES", 8)
    path = cache_dir / "stocks_daily" / "285A.duckdb"
    path.parent.mkdir()
    path.write_bytes(b"123456789")

    def unexpected_request(request: httpx.Request) -> httpx.Response:
        raise AssertionError("authoritative daily cache must not fetch")

    with httpx.Client(
        transport=httpx.MockTransport(unexpected_request), base_url="http://loopback"
    ) as client:
        assert load(
            cache_dir, client, local_authoritative=True
        ) == daily_context.DailyContextResult(bars=(), available=False)


def test_remote_revalidation_transport_failure_serves_valid_stale_file(
    cache_dir: Path, http_client: httpx.Client, daily_db_factory
):
    daily_db_factory("285A", [daily_row("285A", "2024-01-02", 10, 12, 9, 11, 100)])
    first = load(cache_dir, http_client)
    daily_context.reset_for_tests()

    def offline(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    with httpx.Client(
        transport=httpx.MockTransport(offline), base_url="http://remote"
    ) as client:
        stale = load(cache_dir, client)

    assert stale == first


def test_invalid_staged_refresh_does_not_replace_good_live_file_and_can_retry(
    cache_dir: Path,
    http_client: httpx.Client,
    daily_db_factory,
    daily_remote_store: dict[str, bytes],
    tmp_path: Path,
):
    original = daily_db_factory(
        "285A", [daily_row("285A", "2024-01-02", 10, 12, 9, 11, 100)]
    )
    assert load(cache_dir, http_client).available is True
    live_path = cache_dir / "stocks_daily" / "285A.duckdb"

    daily_remote_store["285A"] = wrong_schema_database(tmp_path, "wrong-stage.duckdb")
    daily_context.reset_for_tests()
    assert load(cache_dir, http_client).available is False
    assert live_path.read_bytes() == original

    daily_db_factory("285A", [daily_row("285A", "2024-01-02", 30, 32, 29, 31, 300)])
    recovered = load(cache_dir, http_client)
    assert recovered.available is True
    assert recovered.bars[0].close == 31


def test_bad_live_schema_gets_exactly_one_locked_repair_download(
    cache_dir: Path, daily_db_factory, tmp_path: Path
):
    live_path = cache_dir / "stocks_daily" / "285A.duckdb"
    live_path.parent.mkdir()
    wrong = wrong_schema_database(tmp_path, "wrong-live.duckdb")
    live_path.write_bytes(wrong)
    sidecar_path = live_path.with_name("285A.duckdb.sidecar.json")
    sidecar_path.write_bytes(
        Sidecar(
            etag='"wrong"',
            last_modified=None,
            sha256=hashlib.sha256(wrong).hexdigest(),
            generation=1,
        ).to_json_bytes()
    )
    valid = daily_db_factory(
        "285A", [daily_row("285A", "2024-01-02", 40, 42, 39, 41, 400)]
    )
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.headers.get("if-none-match") == '"wrong"':
            return httpx.Response(304)
        return httpx.Response(
            200,
            headers={"Content-Length": str(len(valid)), "ETag": '"valid"'},
            content=valid,
        )

    with httpx.Client(
        transport=httpx.MockTransport(handler), base_url="http://remote"
    ) as client:
        result = load(cache_dir, client)

    assert result.available is True
    assert result.bars[0].close == 41
    assert len(requests) == 2
    assert "if-none-match" not in requests[1].headers


@pytest.mark.parametrize("declared", [True, False])
def test_download_rejects_declared_or_observed_size_over_limit(
    cache_dir: Path, monkeypatch, declared: bool
):
    monkeypatch.setattr(daily_context, "MAX_DAILY_FILE_BYTES", 8)

    class NineByteStream(httpx.SyncByteStream):
        def __iter__(self):
            yield b"123456789"

    def handler(request: httpx.Request) -> httpx.Response:
        headers = {"Content-Length": "9"} if declared else {}
        return httpx.Response(200, headers=headers, stream=NineByteStream())

    with httpx.Client(
        transport=httpx.MockTransport(handler), base_url="http://remote"
    ) as client:
        assert load(cache_dir, client) == daily_context.DailyContextResult(
            bars=(), available=False
        )

    assert not (cache_dir / "stocks_daily" / "285A.duckdb").exists()


def test_download_total_deadline_is_enforced_without_sleeping(
    cache_dir: Path, daily_db_factory, daily_remote_store, monkeypatch
):
    body = daily_db_factory(
        "285A", [daily_row("285A", "2024-01-02", 10, 12, 9, 11, 100)]
    )
    clock = iter((0.0, 0.0, 2.0))
    monkeypatch.setattr(daily_context, "DOWNLOAD_TOTAL_SECONDS", 1.0)
    monkeypatch.setattr(daily_context, "_monotonic", lambda: next(clock))

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=daily_remote_store["285A"] or body)

    with httpx.Client(
        transport=httpx.MockTransport(handler), base_url="http://remote"
    ) as client:
        assert load(cache_dir, client).available is False


def test_negative_miss_cache_and_lock_state_are_bounded(cache_dir: Path, monkeypatch):
    daily_context.reset_for_tests()
    monkeypatch.setattr(daily_context, "NEGATIVE_CACHE_CAPACITY", 4)
    clock = [0.0]
    monkeypatch.setattr(daily_context, "_monotonic", lambda: clock[0])
    calls = 0

    def missing(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(404)

    with httpx.Client(
        transport=httpx.MockTransport(missing), base_url="http://remote"
    ) as client:
        assert load(cache_dir, client, stem="0000").available is False
        assert load(cache_dir, client, stem="0000").available is False
        assert calls == 2
        for value in range(1, 7):
            assert load(cache_dir, client, stem=f"{value:04d}").available is False
        assert len(daily_context._negative_misses) == 4
        assert len(daily_context._LOCK_STRIPES) == daily_context.LOCK_STRIPE_COUNT
        assert not hasattr(daily_context, "_locks")

        assert calls == 8


def test_negative_miss_time_boundaries_are_strict_and_expire(monkeypatch, tmp_path):
    daily_context.reset_for_tests()
    clock = [10.0]
    monkeypatch.setattr(daily_context, "_monotonic", lambda: clock[0])
    key = (tmp_path.resolve(), "0000")

    daily_context._record_negative(key)
    clock[0] = 20.0
    assert daily_context._negative_active(key, request_started_at=9.0) is True
    assert daily_context._negative_active(key, request_started_at=10.0) is False

    daily_context._record_negative(key)
    clock[0] = 20.0 + daily_context.NEGATIVE_MISS_TTL_SECONDS
    assert daily_context._negative_active(key, request_started_at=19.0) is False


def test_negative_miss_coalesces_waiters_but_next_explicit_request_retries(
    cache_dir: Path, monkeypatch
):
    daily_context.reset_for_tests()
    worker_count = 5
    clock_lock = threading.Lock()
    clock_value = 0.0
    started_threads: set[int] = set()
    all_requests_started = threading.Event()
    request_count = 0

    def controlled_clock() -> float:
        nonlocal clock_value
        thread_id = threading.get_ident()
        with clock_lock:
            clock_value += 1.0
            started_threads.add(thread_id)
            if len(started_threads) == worker_count:
                all_requests_started.set()
            return clock_value

    monkeypatch.setattr(daily_context, "_monotonic", controlled_clock)

    def missing(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        assert all_requests_started.wait(timeout=5)
        return httpx.Response(404)

    results: list[daily_context.DailyContextResult] = []
    start = threading.Barrier(worker_count + 1)

    def worker(client: httpx.Client) -> None:
        start.wait(timeout=5)
        results.append(load(cache_dir, client, stem="0000"))

    with httpx.Client(
        transport=httpx.MockTransport(missing), base_url="http://remote"
    ) as client:
        threads = [
            threading.Thread(target=worker, args=(client,)) for _ in range(worker_count)
        ]
        for thread in threads:
            thread.start()
        start.wait(timeout=5)
        for thread in threads:
            thread.join(timeout=5)
            assert not thread.is_alive()

        assert (
            results
            == [daily_context.DailyContextResult(bars=(), available=False)]
            * worker_count
        )
        assert request_count == 1

        assert load(cache_dir, client, stem="0000").available is False
        assert request_count == 2


def test_local_cache_is_reused_without_redownloading(
    cache_dir: Path, http_client: httpx.Client, daily_db_factory
):
    daily_db_factory("285A", [daily_row("285A", "2024-01-02", 10, 12, 9, 11, 100)])
    first = load(cache_dir, http_client)

    def fail(request: httpx.Request) -> httpx.Response:
        raise AssertionError("cached daily data must not be downloaded again")

    with httpx.Client(
        transport=httpx.MockTransport(fail), base_url="http://invalid"
    ) as second_client:
        second = load(cache_dir, second_client)

    assert second == first
