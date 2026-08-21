"""Tests for the server-cache-backed tick repository (Step 6 of the plan)."""

from __future__ import annotations

import hashlib
import threading
import time
from datetime import date

import duckdb
import httpx
import pytest

from tickreplay import cache, cache_commit
from tickreplay.repository import (
    SymbolAvailabilityUnknownError,
    SymbolNotFoundError,
    SymbolUnavailableError,
    TickRepository,
)

# 2026-08-14 (木) と 2026-08-17 (月) にデータ、間の週末は無し。
ROWS = [
    ("2026-08-14 09:00:00.100000", 100.0, 100, "1", "13010"),
    ("2026-08-14 09:00:00.200000", 101.0, 200, "2", "13010"),
    ("2026-08-17 09:00:00.000000", 102.0, 300, "1", "13010"),
    ("2026-08-17 09:00:30.500000", 103.0, 400, "2", "13010"),
    ("2026-08-17 09:01:00.000000", 99.0, 500, "1", "13010"),
]


@pytest.fixture(autouse=True)
def _seed(tick_db_factory):
    tick_db_factory("1301", ROWS)


def test_symbol_listing_skips_sync_artifacts(repository, remote_store):
    remote_store["130A_ADMIN_Aug-03-200208-2026_Conflict"] = b""

    assert repository.list_symbol_stems() == ["1301"]


def test_symbol_listing_includes_every_live_stem(repository, tick_db_factory):
    tick_db_factory("130A0", ROWS)

    assert repository.list_symbol_stems() == ["1301", "130A0"]


def test_canonical_code_comes_from_the_largest_metadata_row(
    repository, tick_db_factory
):
    # 実データの 3823.duckdb と同じ形: 本体コードと少数行の別コードが同居する。
    rows = ROWS + [("2026-01-22 18:38:40.000000", 1.0, 1, "1", "1301")]
    tick_db_factory("1301", rows)

    info = repository.symbol_info("1301")

    assert info.code == "13010"
    assert info.other_codes == ("1301",)
    assert (info.first_date, info.last_date) == ("2026-08-14", "2026-08-17")


def test_unknown_symbol_confirmed_absent_from_the_live_listing_raises(repository):
    with pytest.raises(SymbolNotFoundError):
        repository.symbol_info("9999")


def test_a_malformed_symbol_identifier_raises_symbol_not_found(repository):
    with pytest.raises(SymbolNotFoundError):
        repository.path_for("../secrets")


def test_a_first_access_downloads_the_file_into_the_cache_dir(repository, cache_dir):
    repository.symbol_info("1301")

    assert cache.live_file_path(cache_dir, "1301").is_file()
    assert cache.live_sidecar_path(cache_dir, "1301").is_file()


def test_corrupt_existing_cache_forces_redownload_and_publishes_one_generation(
    cache_dir, remote_store, http_client
):
    stem = "1301"
    expected_body = remote_store[stem]
    expected_sha256 = hashlib.sha256(expected_body).hexdigest()
    cache.live_file_path(cache_dir, stem).write_bytes(b"not a duckdb file")
    cache.live_sidecar_path(cache_dir, stem).write_bytes(
        cache.Sidecar(
            etag=f'"{expected_sha256}"',
            last_modified=None,
            sha256="0" * 64,
            generation=0,
        ).to_json_bytes()
    )
    repo = TickRepository(
        cache_dir=cache_dir,
        server_url="http://cache-server.invalid",
        http_client=http_client,
    )

    try:
        session = repo.resolve_and_load_session("1301", date(2026, 8, 17), 0)
    finally:
        repo.close()

    assert session is not None
    assert session.price == [102.0, 103.0, 99.0]
    assert repo.generation(stem) == 1
    sidecar = cache.read_sidecar(cache.live_sidecar_path(cache_dir, stem))
    assert sidecar is not None
    assert sidecar.generation == 1
    assert sidecar.sha256 == expected_sha256
    assert cache.live_file_path(cache_dir, stem).read_bytes() == expected_body


def test_resolve_and_load_session_returns_only_that_day(repository):
    session = repository.resolve_and_load_session("1301", date(2026, 8, 17), 0)

    assert session is not None
    assert session.code == "13010"
    assert session.date == "2026-08-17"
    assert session.price == [102.0, 103.0, 99.0]
    assert session.qty == [300, 400, 500]
    assert session.trade_type == ["1", "2", "1"]


def test_timestamps_are_epoch_microseconds_read_as_wall_clock(repository):
    session = repository.resolve_and_load_session("1301", date(2026, 8, 17), 0)

    assert session is not None
    # 09:00:30.5 - 09:00:00 = 30.5 秒
    assert session.us[1] - session.us[0] == 30_500_000


def test_a_day_without_trades_and_direction_zero_returns_none(repository):
    # Unlike the old two-call load_session (which loaded whatever was there,
    # unconditionally), the compound resolve_and_load_session's existence
    # check applies even for direction=0 — a day genuinely without trades is
    # "not found", not "found but empty". Covered again, more directly, by
    # test_resolve_and_load_session_with_direction_zero_checks_only_that_day.
    assert repository.resolve_and_load_session("1301", date(2026, 8, 15), 0) is None


def test_unparseable_timestamps_and_null_prices_are_dropped(
    tick_db_factory, repository
):
    rows = [
        ("2026-08-17 09:00:00.000000", 100.0, 100, "1", "13010"),
        ("2026-08-17 broken", 101.0, 100, "1", "13010"),
        ("2026-08-17 09:00:02.000000", None, 100, "1", "13010"),
    ]
    tick_db_factory("1301", rows, metadata=[("13010", rows[0][0], rows[0][0], 3)])

    session = repository.resolve_and_load_session("1301", date(2026, 8, 17), 0)

    assert session is not None
    assert session.price == [100.0]


def test_identical_timestamps_are_nudged_to_stay_strictly_increasing(
    tick_db_factory, repository
):
    stamp = "2026-08-17 09:00:00.000000"
    rows = [(stamp, 100.0 + index, 100, "1", "13010") for index in range(3)]
    tick_db_factory("1301", rows, metadata=[("13010", stamp, stamp, 3)])

    session = repository.resolve_and_load_session("1301", date(2026, 8, 17), 0)

    assert session is not None
    # 同一マイクロ秒の約定を捨てず、1 マイクロ秒ずつずらして全件残す。
    assert len(session.us) == 3
    assert session.us == sorted(set(session.us))
    assert session.us[2] - session.us[0] == 2


def test_resolve_and_load_session_returns_the_day_itself_when_it_has_data(repository):
    session = repository.resolve_and_load_session("1301", date(2026, 8, 17), -1)
    assert session is not None
    assert session.date == "2026-08-17"


def test_resolve_and_load_session_walks_back_over_the_weekend(repository):
    # 日曜から遡ると、土日を飛ばして木曜のデータ日に着く。
    session = repository.resolve_and_load_session("1301", date(2026, 8, 16), -1)
    assert session is not None
    assert session.date == "2026-08-14"


def test_resolve_and_load_session_walks_forward(repository):
    session = repository.resolve_and_load_session("1301", date(2026, 8, 15), 1)
    assert session is not None
    assert session.date == "2026-08-17"


def test_resolve_and_load_session_stops_outside_the_covered_range(repository):
    assert repository.resolve_and_load_session("1301", date(2026, 8, 13), -1) is None
    assert repository.resolve_and_load_session("1301", date(2026, 8, 18), 1) is None


def test_resolve_and_load_session_with_direction_zero_checks_only_that_day(repository):
    assert repository.resolve_and_load_session("1301", date(2026, 8, 15), 0) is None
    session = repository.resolve_and_load_session("1301", date(2026, 8, 14), 0)
    assert session is not None
    assert session.date == "2026-08-14"


# --------------------------------------------------------------- concurrency


def _staged_replacement(cache_dir, stem: str, rows: list[tuple]) -> cache.StagingResult:
    """Build a new, valid staged file for `stem` directly (bypassing HTTP —
    equivalent to a completed `cache.stage_download`), so a test can commit
    it via `CommitCoordinator` concurrently with a read."""
    part_path = cache.staged_part_path(cache_dir, stem)
    connection = duckdb.connect(str(part_path))
    try:
        connection.execute(
            "CREATE TABLE stocks_board "
            "(Price DOUBLE, Qty BIGINT, Type VARCHAR, source VARCHAR, "
            "Code VARCHAR, Timestamp VARCHAR)"
        )
        connection.execute(
            "CREATE TABLE stocks_board_metadata "
            "(Code VARCHAR, from_timestamp TIMESTAMP, to_timestamp TIMESTAMP, "
            "record_count BIGINT, last_updated TIMESTAMP)"
        )
        for timestamp, price, qty, trade_type, code in rows:
            connection.execute(
                "INSERT INTO stocks_board VALUES (?, ?, ?, 'test', ?, ?)",
                [price, qty, trade_type, code, timestamp],
            )
        connection.execute(
            "INSERT INTO stocks_board_metadata VALUES "
            "('13010', CAST(? AS TIMESTAMP), CAST(? AS TIMESTAMP), ?, "
            "CAST('2026-08-20 00:00:00' AS TIMESTAMP))",
            [rows[0][0], rows[-1][0], len(rows)],
        )
    finally:
        connection.close()
    body = part_path.read_bytes()
    digest = hashlib.sha256(body).hexdigest()
    return cache.StagingResult(
        not_modified=False,
        part_path=part_path,
        sha256=digest,
        content_length=len(body),
        etag=f'"{digest}"',
        last_modified="Fri, 21 Aug 2026 00:00:00 GMT",
    )


def test_a_refresh_concurrent_with_a_read_never_serves_a_torn_state(
    repository, cache_dir
):
    """Directly exercises the fixed read-after-invalidate race (Step 6):
    a concurrent reader must block on the same symbol lock the commit
    coordinator holds, so it observes either the fully-old or the
    fully-new (file, sidecar, generation, info_cache) tuple — never a mix.
    """
    stem = "1301"
    old_info = repository.symbol_info(stem)
    old_generation = repository.generation(stem)

    new_rows = ROWS + [("2026-08-17 09:02:00.000000", 200.0, 700, "1", "13010")]
    staging = _staged_replacement(cache_dir, stem, new_rows)
    coordinator = cache_commit.CommitCoordinator(
        cache_dir=cache_dir, repository=repository
    )

    reached_step_2 = threading.Event()
    release = threading.Event()

    def on_step(step: int) -> None:
        if step == 2:
            reached_step_2.set()
            release.wait(timeout=5)

    commit_thread = threading.Thread(
        target=coordinator.refresh, args=(stem, staging), kwargs={"on_step": on_step}
    )
    commit_thread.start()
    assert reached_step_2.wait(timeout=5), "commit never reached step 2"

    result: dict[str, object] = {}

    def reader() -> None:
        result["info"] = repository.symbol_info(stem)

    reader_thread = threading.Thread(target=reader)
    reader_thread.start()
    time.sleep(0.1)
    # The reader must still be blocked on symbol_lock — it must not have
    # observed the mid-commit state (evicted connection, file about to be
    # replaced) as either the old or a torn info.
    assert reader_thread.is_alive()

    release.set()
    commit_thread.join(timeout=5)
    reader_thread.join(timeout=5)
    assert not reader_thread.is_alive()

    new_generation = repository.generation(stem)
    assert new_generation > old_generation
    new_info = result["info"]
    assert new_info.record_count != old_info.record_count
    assert new_info.record_count == len(new_rows)


def test_resolve_and_load_session_waits_for_same_stem_commit_generation(
    repository, cache_dir
):
    stem = "1301"
    old_session = repository.resolve_and_load_session(stem, date(2026, 8, 17), 0)
    assert old_session is not None
    old_generation = repository.generation(stem)
    new_rows = ROWS + [("2026-08-17 09:02:00.000000", 200.0, 700, "1", "13010")]
    staging = _staged_replacement(cache_dir, stem, new_rows)
    coordinator = cache_commit.CommitCoordinator(
        cache_dir=cache_dir, repository=repository
    )
    reached_step_2 = threading.Event()
    release = threading.Event()

    def on_step(step: int) -> None:
        if step == 2:
            reached_step_2.set()
            release.wait(timeout=5)

    commit_thread = threading.Thread(
        target=coordinator.refresh, args=(stem, staging), kwargs={"on_step": on_step}
    )
    commit_thread.start()
    assert reached_step_2.wait(timeout=5), "commit never reached step 2"
    result: dict[str, object] = {}

    def reader() -> None:
        result["session"] = repository.resolve_and_load_session(
            stem, date(2026, 8, 17), 0
        )

    reader_thread = threading.Thread(target=reader)
    reader_thread.start()
    time.sleep(0.1)
    assert reader_thread.is_alive()

    release.set()
    commit_thread.join(timeout=5)
    reader_thread.join(timeout=5)

    assert not reader_thread.is_alive()
    assert repository.generation(stem) > old_generation
    new_session = result["session"]
    assert new_session is not None
    assert new_session.price == [102.0, 103.0, 99.0, 200.0]


def test_a_slow_commit_for_one_stem_does_not_block_a_query_for_another(
    repository, cache_dir, tick_db_factory
):
    tick_db_factory("9984", ROWS)  # a second, independent stem

    stem_a = "1301"
    repository.symbol_info(stem_a)  # warm stem A's cache before blocking it
    staging = _staged_replacement(
        cache_dir, stem_a, ROWS + [("2026-08-17 09:02:00.000000", 1.0, 1, "1", "13010")]
    )
    coordinator = cache_commit.CommitCoordinator(
        cache_dir=cache_dir, repository=repository
    )

    reached_step_2 = threading.Event()
    release = threading.Event()

    def on_step(step: int) -> None:
        if step == 2:
            reached_step_2.set()
            release.wait(timeout=5)

    commit_thread = threading.Thread(
        target=coordinator.refresh, args=(stem_a, staging), kwargs={"on_step": on_step}
    )
    commit_thread.start()
    try:
        assert reached_step_2.wait(timeout=5), "commit never reached step 2"

        # Stem B's own lock is independent of stem A's — this must return
        # promptly, not wait for stem A's commit to release its lock.
        info_b = repository.symbol_info("9984")
        assert info_b.code == "13010"
    finally:
        release.set()
        commit_thread.join(timeout=5)


# --------------------------------------------------- existence-vs-availability


def test_listing_falls_back_to_a_persisted_listing_when_the_server_is_unreachable(
    cache_dir, remote_store
):
    def down_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    online_client = httpx.Client(
        base_url="http://cache-server.invalid",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"stems": ["1301"]})
        ),
    )
    repo = TickRepository(
        cache_dir=cache_dir,
        server_url="http://cache-server.invalid",
        http_client=online_client,
    )
    try:
        assert repo.list_symbol_stems() == ["1301"]
    finally:
        repo.close()
        online_client.close()

    offline_client = httpx.Client(
        base_url="http://cache-server.invalid",
        transport=httpx.MockTransport(down_handler),
    )
    repo2 = TickRepository(
        cache_dir=cache_dir,
        server_url="http://cache-server.invalid",
        http_client=offline_client,
    )
    try:
        # The server is unreachable now, but the prior process persisted
        # the listing to cache_dir — degrade to it rather than reporting
        # nothing.
        assert repo2.list_symbol_stems() == ["1301"]
    finally:
        repo2.close()
        offline_client.close()


def test_a_stem_absent_everywhere_when_offline_is_indeterminate_not_404(cache_dir):
    def down_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    offline_client = httpx.Client(
        base_url="http://cache-server.invalid",
        transport=httpx.MockTransport(down_handler),
    )
    repo = TickRepository(
        cache_dir=cache_dir,
        server_url="http://cache-server.invalid",
        http_client=offline_client,
    )
    try:
        with pytest.raises(SymbolAvailabilityUnknownError):
            repo.symbol_info("9999")
    finally:
        repo.close()
        offline_client.close()


@pytest.mark.parametrize(
    "error_type", [cache.DiskFullError, cache.CorruptDownloadError, cache.DownloadError]
)
def test_local_refresh_failures_are_not_treated_as_stale_offline_fallback(
    cache_dir, remote_store, http_client, monkeypatch, error_type
):
    stem = "1301"
    cache.live_file_path(cache_dir, stem).write_bytes(remote_store[stem])
    repo = TickRepository(
        cache_dir=cache_dir,
        server_url="http://cache-server.invalid",
        http_client=http_client,
    )

    def fail_stage(*args, **kwargs):
        raise error_type("local durability failure")

    monkeypatch.setattr(cache, "stage_download", fail_stage)
    try:
        with pytest.raises(SymbolUnavailableError, match="local cache refresh failed"):
            repo.ensure_fresh(stem)
    finally:
        repo.close()

    assert stem not in repo._revalidated
