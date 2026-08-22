"""Tests for the server-cache-backed tick repository (Step 6 of the plan)."""

from __future__ import annotations

import hashlib
import threading
import time
from datetime import date
from email.utils import formatdate

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
        generation = repo.generation(stem)
    finally:
        repo.close()

    assert session is not None
    assert session.price == [102.0, 103.0, 99.0]
    assert generation == 1
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


# ----------------------- process-wide destination operation serialization


def _repository_for_transport(cache_dir, handler, *, local_authoritative=False):
    client = httpx.Client(
        base_url="http://cache-server.invalid",
        transport=httpx.MockTransport(handler),
    )
    return (
        TickRepository(
            cache_dir=cache_dir,
            server_url="http://cache-server.invalid",
            http_client=client,
            local_authoritative=local_authoritative,
        ),
        client,
    )


def test_repository_instances_serialize_one_destination_through_commit_and_query(
    tmp_path, tick_db_factory
):
    stem = "1301"
    cache_dir = tmp_path / "shared-cache"
    cache_dir.mkdir()
    cache_alias = cache_dir / "alias" / ".."
    cache_alias.parent.mkdir()
    body = tick_db_factory(stem, ROWS)
    etag = f'"{hashlib.sha256(body).hexdigest()}"'
    first_body_started = threading.Event()
    release_first_body = threading.Event()
    follower_started = threading.Event()
    statuses: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/stocks-trades":
            return httpx.Response(200, json={"stems": [stem]})
        if request.headers.get("if-none-match") == etag:
            statuses.append(304)
            return httpx.Response(304)
        statuses.append(200)
        first_body_started.set()
        assert release_first_body.wait(timeout=5)
        return httpx.Response(
            200,
            headers={"Content-Length": str(len(body)), "ETag": etag},
            content=body,
        )

    client = httpx.Client(
        base_url="http://cache-server.invalid",
        transport=httpx.MockTransport(handler),
    )
    leader_repo = TickRepository(
        cache_dir=cache_dir,
        server_url="http://cache-server.invalid",
        http_client=client,
    )
    follower_repo = TickRepository(
        cache_dir=cache_alias,
        server_url="http://cache-server.invalid",
        http_client=client,
    )
    shared_destination_lock = leader_repo.symbol_lock(
        stem
    ) is follower_repo.symbol_lock(stem)
    results = {}
    errors: dict[str, BaseException] = {}

    def read_info(label, repository, started=None):
        if started is not None:
            started.set()
        try:
            results[label] = repository.symbol_info(stem)
        except BaseException as error:  # noqa: BLE001 - surfaced via `errors`
            errors[label] = error

    leader = threading.Thread(target=read_info, args=("leader", leader_repo))
    follower = threading.Thread(
        name="same-destination-follower",
        target=read_info,
        args=("follower", follower_repo, follower_started),
    )
    leader.start()
    assert first_body_started.wait(timeout=5)
    follower.start()
    assert follower_started.wait(timeout=5)
    release_first_body.set()
    leader.join(timeout=5)
    follower.join(timeout=5)
    leader_repo.close()
    follower_repo.close()
    client.close()

    assert not leader.is_alive()
    assert not follower.is_alive()
    assert shared_destination_lock
    assert not errors
    assert set(results) == {"leader", "follower"}
    assert statuses == [200, 304]
    assert cache.live_file_path(cache_dir, stem).read_bytes() == body
    assert not cache.staged_part_path(cache_dir, stem).exists()


def test_repository_operations_for_different_cache_roots_run_concurrently(
    tmp_path, tick_db_factory
):
    stem = "1301"
    body = tick_db_factory(stem, ROWS)
    cache_dirs = {"left": tmp_path / "left", "right": tmp_path / "right"}
    started = {label: threading.Event() for label in cache_dirs}

    def handler_for(label):
        other = "right" if label == "left" else "left"

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/stocks-trades":
                return httpx.Response(200, json={"stems": [stem]})
            started[label].set()
            assert started[other].wait(timeout=5)
            return httpx.Response(
                200,
                headers={"Content-Length": str(len(body))},
                content=body,
            )

        return handler

    repositories = {}
    clients = {}
    for label, directory in cache_dirs.items():
        repositories[label], clients[label] = _repository_for_transport(
            directory, handler_for(label)
        )

    results = {}
    errors: dict[str, BaseException] = {}

    def read_info(label):
        try:
            results[label] = repositories[label].symbol_info(stem)
        except BaseException as error:  # noqa: BLE001 - surfaced via `errors`
            errors[label] = error

    threads = [
        threading.Thread(target=read_info, args=(label,)) for label in cache_dirs
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
    for repository in repositories.values():
        repository.close()
    for client in clients.values():
        client.close()

    assert all(not thread.is_alive() for thread in threads)
    assert not errors
    assert set(results) == set(cache_dirs)
    for directory in cache_dirs.values():
        assert cache.live_file_path(directory, stem).read_bytes() == body
        assert not cache.staged_part_path(directory, stem).exists()


def test_same_destination_follower_retries_after_leader_download_failure(
    tmp_path, tick_db_factory, monkeypatch
):
    stem = "1301"
    cache_dir = tmp_path / "shared-cache"
    body = tick_db_factory(stem, ROWS)
    leader_request_started = threading.Event()
    release_leader = threading.Event()
    follower_started = threading.Event()
    attempts = {"leader": 0, "follower": 0}
    monkeypatch.setattr(cache, "RETRY_BACKOFF_BASE_SECONDS", 0.0)

    def leader_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/stocks-trades":
            return httpx.Response(200, json={"stems": [stem]})
        attempts["leader"] += 1
        if attempts["leader"] == 1:
            leader_request_started.set()
            assert release_leader.wait(timeout=5)
        return httpx.Response(503)

    def follower_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/stocks-trades":
            return httpx.Response(200, json={"stems": [stem]})
        attempts["follower"] += 1
        return httpx.Response(
            200,
            headers={"Content-Length": str(len(body))},
            content=body,
        )

    leader_repo, leader_client = _repository_for_transport(cache_dir, leader_handler)
    follower_repo, follower_client = _repository_for_transport(
        cache_dir, follower_handler
    )
    shared_destination_lock = leader_repo.symbol_lock(
        stem
    ) is follower_repo.symbol_lock(stem)
    results = {}
    errors: dict[str, BaseException] = {}

    def leader_work():
        try:
            leader_repo.symbol_info(stem)
        except BaseException as error:  # noqa: BLE001 - surfaced via `errors`
            errors["leader"] = error

    def follower_work():
        follower_started.set()
        try:
            results["follower"] = follower_repo.symbol_info(stem)
        except BaseException as error:  # noqa: BLE001 - surfaced via `errors`
            errors["follower"] = error

    leader = threading.Thread(target=leader_work)
    follower = threading.Thread(name="failure-follower", target=follower_work)
    leader.start()
    assert leader_request_started.wait(timeout=5)
    follower.start()
    assert follower_started.wait(timeout=5)
    release_leader.set()
    leader.join(timeout=5)
    follower.join(timeout=5)
    leader_repo.close()
    follower_repo.close()
    leader_client.close()
    follower_client.close()

    assert not leader.is_alive()
    assert not follower.is_alive()
    assert shared_destination_lock
    assert set(errors) == {"leader"}
    assert isinstance(errors["leader"], SymbolAvailabilityUnknownError)
    assert results["follower"].code == "13010"
    assert attempts == {"leader": cache.MAX_DOWNLOAD_ATTEMPTS, "follower": 1}
    assert cache.live_file_path(cache_dir, stem).read_bytes() == body


def test_forced_retry_waits_for_conditional_revalidation_and_gets_its_own_body(
    tmp_path, tick_db_factory, monkeypatch
):
    stem = "1301"
    cache_dir = tmp_path / "shared-cache"
    stale = tick_db_factory(stem, ROWS)
    fresh_rows = [
        (timestamp, price + 100.0, qty, trade_type, code)
        for timestamp, price, qty, trade_type, code in ROWS
    ]
    fresh = tick_db_factory(stem, fresh_rows)
    stale_etag = f'"{hashlib.sha256(stale).hexdigest()}"'
    cache.live_file_path(cache_dir, stem).write_bytes(stale)
    cache.live_sidecar_path(cache_dir, stem).write_bytes(
        cache.Sidecar(
            etag=stale_etag,
            last_modified=None,
            sha256=hashlib.sha256(stale).hexdigest(),
            generation=0,
        ).to_json_bytes()
    )
    conditional_started = threading.Event()
    release_conditional = threading.Event()
    forced_started = threading.Event()
    forced_headers: list[httpx.Headers] = []

    def conditional_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/stocks-trades":
            return httpx.Response(200, json={"stems": [stem]})
        assert request.headers["if-none-match"] == stale_etag
        conditional_started.set()
        assert release_conditional.wait(timeout=5)
        return httpx.Response(304)

    def forced_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/stocks-trades":
            return httpx.Response(200, json={"stems": [stem]})
        forced_headers.append(request.headers)
        return httpx.Response(
            200,
            headers={"Content-Length": str(len(fresh))},
            content=fresh,
        )

    conditional_repo, conditional_client = _repository_for_transport(
        cache_dir, conditional_handler
    )
    forced_repo, forced_client = _repository_for_transport(cache_dir, forced_handler)
    shared_destination_lock = conditional_repo.symbol_lock(
        stem
    ) is forced_repo.symbol_lock(stem)
    forced_repo._revalidated.add(stem)
    original_query = forced_repo._pool.query
    query_calls = {"n": 0}

    def fail_first_query(*args, **kwargs):
        query_calls["n"] += 1
        if query_calls["n"] == 1:
            raise duckdb.IOException("synthetic corruption")
        return original_query(*args, **kwargs)

    monkeypatch.setattr(forced_repo._pool, "query", fail_first_query)
    results = {}
    errors: dict[str, BaseException] = {}

    def conditional_work():
        try:
            conditional_repo.ensure_fresh(stem)
        except BaseException as error:  # noqa: BLE001 - surfaced via `errors`
            errors["conditional"] = error

    def forced_work():
        forced_started.set()
        try:
            results["forced"] = forced_repo.symbol_info(stem)
        except BaseException as error:  # noqa: BLE001 - surfaced via `errors`
            errors["forced"] = error

    conditional = threading.Thread(target=conditional_work)
    forced = threading.Thread(name="forced-follower", target=forced_work)
    conditional.start()
    assert conditional_started.wait(timeout=5)
    forced.start()
    assert forced_started.wait(timeout=5)
    release_conditional.set()
    conditional.join(timeout=5)
    forced.join(timeout=5)
    conditional_repo.close()
    forced_repo.close()
    conditional_client.close()
    forced_client.close()

    assert not conditional.is_alive()
    assert not forced.is_alive()
    assert shared_destination_lock
    assert not errors
    assert results["forced"].code == "13010"
    assert query_calls["n"] == 2
    assert len(forced_headers) == 1
    assert "if-none-match" not in forced_headers[0]
    assert "if-modified-since" not in forced_headers[0]
    assert cache.live_file_path(cache_dir, stem).read_bytes() == fresh
    assert not cache.staged_part_path(cache_dir, stem).exists()


def test_repository_instances_share_open_pool_and_metadata_across_refresh(
    tmp_path, tick_db_factory
):
    stem = "1301"
    cache_dir = tmp_path / "shared-cache"
    old = tick_db_factory(stem, ROWS)
    new_rows = [
        (timestamp, price + 100.0, qty, trade_type, "23010")
        for timestamp, price, qty, trade_type, _code in ROWS
    ]
    new = tick_db_factory(stem, new_rows)
    old_etag = f'"{hashlib.sha256(old).hexdigest()}"'
    new_etag = f'"{hashlib.sha256(new).hexdigest()}"'
    cache.live_file_path(cache_dir, stem).write_bytes(old)
    cache.live_sidecar_path(cache_dir, stem).write_bytes(
        cache.Sidecar(
            etag=old_etag,
            last_modified=None,
            sha256=hashlib.sha256(old).hexdigest(),
            generation=0,
        ).to_json_bytes()
    )

    def local_handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/stocks-trades"
        return httpx.Response(200, json={"stems": [stem]})

    def refresh_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/stocks-trades":
            return httpx.Response(200, json={"stems": [stem]})
        assert request.headers["if-none-match"] == old_etag
        return httpx.Response(
            200,
            headers={"Content-Length": str(len(new)), "ETag": new_etag},
            content=new,
        )

    reader, reader_client = _repository_for_transport(
        cache_dir, local_handler, local_authoritative=True
    )
    refresher, refresher_client = _repository_for_transport(cache_dir, refresh_handler)
    try:
        assert reader.symbol_info(stem).code == "13010"

        assert refresher.symbol_info(stem).code == "23010"
        assert reader.symbol_info(stem).code == "23010"
        session = reader.resolve_and_load_session(stem, date(2026, 8, 17), 0)
    finally:
        refresher.close()
        reader.close()
        refresher_client.close()
        reader_client.close()

    assert session is not None
    assert session.price == [202.0, 203.0, 199.0]
    assert cache.live_file_path(cache_dir, stem).read_bytes() == new


def test_closing_one_repository_twice_keeps_the_other_shared_pool_open(
    tmp_path, tick_db_factory
):
    stem = "1301"
    cache_dir = tmp_path / "shared-cache"
    body = tick_db_factory(stem, ROWS)
    cache.live_file_path(cache_dir, stem).write_bytes(body)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/stocks-trades"
        return httpx.Response(200, json={"stems": [stem]})

    first, first_client = _repository_for_transport(
        cache_dir, handler, local_authoritative=True
    )
    second, second_client = _repository_for_transport(
        cache_dir, handler, local_authoritative=True
    )
    try:
        assert second.symbol_info(stem).code == "13010"
        shared_pool = first._pool is second._pool

        first.close()
        first.close()
        session = second.resolve_and_load_session(stem, date(2026, 8, 17), 0)
    finally:
        first.close()
        second.close()
        first_client.close()
        second_client.close()

    assert shared_pool
    assert session is not None
    assert session.price == [102.0, 103.0, 99.0]


def test_last_close_clears_shared_session_state_before_registry_reuse(
    tmp_path, tick_db_factory
):
    stem = "1301"
    cache_dir = tmp_path / "shared-cache"
    body = tick_db_factory(stem, ROWS)
    etag = f'"{hashlib.sha256(body).hexdigest()}"'
    cache.live_file_path(cache_dir, stem).write_bytes(body)
    cache.live_sidecar_path(cache_dir, stem).write_bytes(
        cache.Sidecar(
            etag=etag,
            last_modified=None,
            sha256=hashlib.sha256(body).hexdigest(),
            generation=0,
        ).to_json_bytes()
    )
    revalidations: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/stocks-trades":
            return httpx.Response(200, json={"stems": [stem]})
        revalidations.append(request.headers["if-none-match"])
        return httpx.Response(304)

    first, first_client = _repository_for_transport(cache_dir, handler)
    assert first.symbol_info(stem).code == "13010"
    with first.symbol_lock(stem):
        first.publish_generation(stem, 7)
        first.mark_unavailable(stem, "test-only unavailable state")
    old_pool = first._pool
    first.close()
    first.close()
    first_client.close()

    second, second_client = _repository_for_transport(cache_dir, handler)
    try:
        assert second._pool is old_pool
        assert second.generation(stem) == 0
        assert second.is_unavailable(stem) is None
        assert second.symbol_info(stem).code == "13010"
    finally:
        second.close()
        second_client.close()

    assert revalidations == [etag, etag]


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


# ------------------------------------- a cache dir the server itself serves


def _self_serving_transport(root, transferred: list[str], statuses: list[int]):
    """A stand-in for `cloud-run/main.py` serving straight out of `root` —
    same ETag derivation (md5 over the full float mtime plus size) and same
    `Last-Modified`. Used to reproduce the merged deployment, where
    `BACKCAST_DUCKDB_CACHE_DIR` *is* the directory the server serves."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/stocks-trades":
            stems = sorted(
                path.name[: -len(".duckdb")]
                for path in cache.stocks_trades_dir(root).glob("*.duckdb")
            )
            return httpx.Response(200, json={"stems": stems})

        stem = request.url.path.rsplit("/", 1)[-1][: -len(".duckdb")]
        path = cache.live_file_path(root, stem)
        try:
            st = path.stat()
        except OSError:
            statuses.append(404)
            return httpx.Response(404)

        digest = hashlib.md5(
            f"{st.st_mtime}-{st.st_size}".encode(), usedforsecurity=False
        ).hexdigest()
        etag = f'"{digest}"'
        headers = {
            "ETag": etag,
            "Last-Modified": formatdate(st.st_mtime, usegmt=True),
            "Content-Length": str(st.st_size),
        }
        if request.headers.get("if-none-match") == etag:
            statuses.append(304)
            return httpx.Response(304, headers=headers)

        def stream():
            transferred.append(stem)
            yield path.read_bytes()

        statuses.append(200)
        return httpx.Response(200, headers=headers, content=stream())

    return handler


def test_local_authoritative_never_redownloads_the_file_it_serves(
    tmp_path, tick_db_factory
):
    # Reproduces the production loop: committing a download rewrites the
    # served file's mtime, which is what the server's ETag is derived from,
    # so the sidecar written from the pre-commit ETag could never match
    # again and every process start redownloaded the whole file.
    root = tmp_path / "jp"
    body = tick_db_factory("1301", ROWS)
    live = cache.live_file_path(root, "1301")
    live.write_bytes(body)
    before = live.stat()
    transferred: list[str] = []
    statuses: list[int] = []

    def open_repository() -> TickRepository:
        return TickRepository(
            cache_dir=root,
            server_url="http://cache-server.invalid",
            http_client=httpx.Client(
                base_url="http://cache-server.invalid",
                transport=httpx.MockTransport(
                    _self_serving_transport(root, transferred, statuses)
                ),
            ),
            local_authoritative=True,
        )

    # Three consecutive process starts, each with a fresh in-memory
    # revalidation set — the loop reproduced itself once per start.
    for _ in range(3):
        repository = open_repository()
        try:
            assert repository.symbol_info("1301").code == "13010"
            assert repository.needs_download("1301") is False
        finally:
            repository.close()

    assert transferred == []
    assert statuses == []  # not one request for the file itself
    assert live.stat().st_mtime == before.st_mtime
    assert live.read_bytes() == body
    assert not cache.live_sidecar_path(root, "1301").exists()


def test_remote_cache_does_not_infer_identity_from_size_and_http_date(
    tmp_path, tick_db_factory
):
    root = tmp_path / "cache"
    stale = tick_db_factory("1301", ROWS)
    fresh_rows = [
        (timestamp, price + 100.0, qty, trade_type, code)
        for timestamp, price, qty, trade_type, code in ROWS
    ]
    fresh = tick_db_factory("1301", fresh_rows)
    assert len(stale) == len(fresh)

    live = cache.live_file_path(root, "1301")
    live.write_bytes(stale)
    last_modified = formatdate(live.stat().st_mtime, usegmt=True)
    stale_etag = f'"{hashlib.sha256(stale).hexdigest()}"'
    fresh_etag = f'"{hashlib.sha256(fresh).hexdigest()}"'
    cache.live_sidecar_path(root, "1301").write_bytes(
        cache.Sidecar(
            etag=stale_etag,
            last_modified=last_modified,
            sha256=hashlib.sha256(stale).hexdigest(),
            generation=0,
        ).to_json_bytes()
    )
    file_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/stocks-trades":
            return httpx.Response(200, json={"stems": ["1301"]})
        file_requests.append(request)
        return httpx.Response(
            200,
            headers={
                "Content-Length": str(len(fresh)),
                "Last-Modified": last_modified,
                "ETag": fresh_etag,
            },
            content=fresh,
        )

    client = httpx.Client(
        base_url="http://cache-server.invalid",
        transport=httpx.MockTransport(handler),
    )
    repository = TickRepository(
        cache_dir=root,
        server_url="http://cache-server.invalid",
        http_client=client,
    )
    try:
        repository.symbol_info("1301")
    finally:
        repository.close()
        client.close()

    assert len(file_requests) == 1
    assert file_requests[0].headers["if-none-match"] == stale_etag
    assert live.read_bytes() == fresh
    sidecar = cache.read_sidecar(cache.live_sidecar_path(root, "1301"))
    assert sidecar is not None
    assert sidecar.sha256 == hashlib.sha256(fresh).hexdigest()


def test_local_authoritative_still_downloads_a_file_it_does_not_have(
    tmp_path, tick_db_factory, monkeypatch
):
    # The mode suppresses revalidation of an existing file, never the first
    # fetch — a stem the cache dir does not hold must still go over HTTP.
    root = tmp_path / "jp"
    served = tmp_path / "served"
    body = tick_db_factory("1301", ROWS)
    cache.live_file_path(served, "1301").write_bytes(body)
    transferred: list[str] = []
    statuses: list[int] = []
    conditional_values: list[bool] = []
    stage_download = cache.stage_download

    def record_download(*args, **kwargs):
        conditional_values.append(kwargs["conditional"])
        return stage_download(*args, **kwargs)

    monkeypatch.setattr(cache, "stage_download", record_download)

    repository = TickRepository(
        cache_dir=root,
        server_url="http://cache-server.invalid",
        http_client=httpx.Client(
            base_url="http://cache-server.invalid",
            transport=httpx.MockTransport(
                _self_serving_transport(served, transferred, statuses)
            ),
        ),
        local_authoritative=True,
    )
    try:
        assert repository.needs_download("1301") is True
        assert repository.symbol_info("1301").code == "13010"
    finally:
        repository.close()

    assert transferred == ["1301"]
    assert conditional_values == [False]
    assert cache.live_file_path(root, "1301").read_bytes() == body
    assert cache.live_sidecar_path(root, "1301").is_file()


def test_local_authoritative_still_forces_a_redownload_over_a_corrupt_file(
    cache_dir, remote_store, http_client, monkeypatch
):
    # A file that fails the DuckDB-open check is refetched unconditionally
    # even in this mode: the suppression is about freshness, not about
    # repairing a local file that cannot be read at all.
    stem = "1301"
    expected = remote_store[stem]
    cache.live_file_path(cache_dir, stem).write_bytes(b"not a duckdb file")
    conditional_values: list[bool] = []
    stage_download = cache.stage_download

    def record_download(*args, **kwargs):
        conditional_values.append(kwargs["conditional"])
        return stage_download(*args, **kwargs)

    monkeypatch.setattr(cache, "stage_download", record_download)
    repository = TickRepository(
        cache_dir=cache_dir,
        server_url="http://cache-server.invalid",
        http_client=http_client,
        local_authoritative=True,
    )

    try:
        session = repository.resolve_and_load_session(stem, date(2026, 8, 17), 0)
    finally:
        repository.close()

    assert session is not None
    assert session.price == [102.0, 103.0, 99.0]
    assert conditional_values == [False]
    assert cache.live_file_path(cache_dir, stem).read_bytes() == expected


def test_a_remote_cache_still_revalidates_every_process_start(
    cache_dir, http_client, remote_store
):
    # The default (a real remote server) is untouched: the freshness check
    # that `local_authoritative` suppresses must still happen there.
    repository = TickRepository(
        cache_dir=cache_dir,
        server_url="http://cache-server.invalid",
        http_client=http_client,
    )
    try:
        repository.symbol_info("1301")
    finally:
        repository.close()

    second = TickRepository(
        cache_dir=cache_dir,
        server_url="http://cache-server.invalid",
        http_client=http_client,
    )
    try:
        assert second.needs_download("1301") is True
    finally:
        second.close()
