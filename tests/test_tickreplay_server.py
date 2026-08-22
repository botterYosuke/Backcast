"""Tests for the HTTP API (Steps 6-7 of the plan)."""

from __future__ import annotations

import hashlib
import threading
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from tickreplay import server
from tickreplay.config import CACHE_DIR_ENV_VAR, CacheConfigError

ROWS = [
    ("2026-08-14 09:00:00.100000", 100.0, 100, "1", "13010"),
    ("2026-08-17 09:00:00.000000", 102.0, 300, "1", "13010"),
    ("2026-08-17 09:00:30.500000", 103.0, 400, "2", "13010"),
]


@pytest.fixture
def client(cache_dir, http_client, tick_db_factory, monkeypatch):
    tick_db_factory("1301", ROWS)
    monkeypatch.setenv(CACHE_DIR_ENV_VAR, str(cache_dir))
    # get_repository() builds its own httpx.Client via build_http_client;
    # redirect that to the shared mock-transport client instead of letting
    # it construct a real one against the (default) production server URL.
    monkeypatch.setattr(server, "build_http_client", lambda config: http_client)
    server.reset_for_tests()
    with TestClient(server.app) as test_client:
        # Pre-warm "1301" synchronously so ordinary endpoint tests below
        # aren't about the async pending/operationId path (Step 7) — that
        # path has its own dedicated tests further down.
        server.get_repository().symbol_info("1301")
        yield test_client
    server.reset_for_tests()


# ------------------------------------------------------------- fail-fast


def test_status_before_lifespan_does_not_construct_repository(monkeypatch):
    def unexpected_construction():
        raise AssertionError("status must not construct the repository")

    monkeypatch.setattr(server, "resolve_cache_config", unexpected_construction)
    server.reset_for_tests()

    body = server.read_status(stem="9999")

    assert body["state"] == "missing"
    assert body["operationId"] is None
    with pytest.raises(RuntimeError, match="lifespan"):
        server.get_repository()


def test_startup_fails_fast_with_an_invalid_cache_dir(monkeypatch, tmp_path):
    monkeypatch.delenv(CACHE_DIR_ENV_VAR, raising=False)
    monkeypatch.setattr("tickreplay.config.REPO_ROOT", tmp_path)
    server.reset_for_tests()
    try:
        with pytest.raises(CacheConfigError):
            with TestClient(server.app):
                pass
    finally:
        server.reset_for_tests()


def test_orphan_recovery_runs_once_at_startup_not_per_request(
    cache_dir, http_client, tick_db_factory, monkeypatch
):
    tick_db_factory("1301", ROWS)
    monkeypatch.setenv(CACHE_DIR_ENV_VAR, str(cache_dir))
    monkeypatch.setattr(server, "build_http_client", lambda config: http_client)

    calls = {"discard": 0, "reconcile": 0}
    real_discard = server.cache.discard_orphaned_part_files
    real_reconcile = server.cache_commit.reconcile_all_at_startup

    def counting_discard(directory):
        calls["discard"] += 1
        return real_discard(directory)

    def counting_reconcile(directory, repository):
        calls["reconcile"] += 1
        return real_reconcile(directory, repository)

    monkeypatch.setattr(server.cache, "discard_orphaned_part_files", counting_discard)
    monkeypatch.setattr(
        server.cache_commit, "reconcile_all_at_startup", counting_reconcile
    )

    server.reset_for_tests()
    try:
        with TestClient(server.app) as test_client:
            test_client.get("/api/status", params={"stem": "1301"})
            test_client.get("/api/status", params={"stem": "1301"})
            test_client.get("/api/symbols")
        assert calls == {"discard": 1, "reconcile": 1}
    finally:
        server.reset_for_tests()


def test_lifespan_reentry_constructs_a_new_open_repository(cache_dir, monkeypatch):
    monkeypatch.setenv(CACHE_DIR_ENV_VAR, str(cache_dir))
    clients: list[httpx.Client] = []

    def build_client(config) -> httpx.Client:
        client = httpx.Client(
            base_url="http://cache-server.invalid",
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json={"stems": []})
            ),
        )
        clients.append(client)
        return client

    monkeypatch.setattr(server, "build_http_client", build_client)
    server.reset_for_tests()
    try:
        with TestClient(server.app):
            first_repository = server.get_repository()
            assert not first_repository.http_client.is_closed
            with TestClient(server.app):
                assert server.get_repository() is first_repository
            assert server.get_repository() is first_repository
            assert not first_repository.http_client.is_closed

        assert first_repository.http_client.is_closed
        with pytest.raises(RuntimeError, match="lifespan"):
            server.get_repository()

        with TestClient(server.app):
            second_repository = server.get_repository()
            assert second_repository is not first_repository
            assert not second_repository.http_client.is_closed

        assert second_repository.http_client.is_closed
        assert len(clients) == 2
    finally:
        server.reset_for_tests()


def test_startup_reconciliation_exception_marks_stem_unavailable_and_returns_503(
    cache_dir, http_client, tick_db_factory, monkeypatch
):
    stem = "1301"
    body = tick_db_factory(stem, ROWS)
    cache_path = server.cache.live_file_path(cache_dir, stem)
    cache_path.write_bytes(body)
    server.cache.staged_sidecar_tmp_path(cache_dir, stem).write_bytes(
        server.cache.Sidecar(
            etag='"broken"',
            last_modified=None,
            sha256=hashlib.sha256(body).hexdigest(),
            generation=1,
        ).to_json_bytes()
    )
    monkeypatch.setenv(CACHE_DIR_ENV_VAR, str(cache_dir))
    monkeypatch.setattr(server, "build_http_client", lambda config: http_client)

    def broken_reconciliation(cache_dir, target_stem, repository):
        raise OSError("startup reconciliation failed")

    monkeypatch.setattr(server.cache_commit, "reconcile_stem", broken_reconciliation)
    server.reset_for_tests()
    try:
        with TestClient(server.app) as test_client:
            response = test_client.get(
                "/api/session", params={"stem": stem, "date": "2026-08-17"}
            )
            assert response.status_code == 503
            assert "startup reconciliation failed" in response.json()["detail"]
            assert server.get_repository().is_unavailable(stem) is not None
    finally:
        server.reset_for_tests()


# ------------------------------------------------------------ /api/status


def test_status_performs_no_network_io(cache_dir, monkeypatch):
    monkeypatch.setenv(CACHE_DIR_ENV_VAR, str(cache_dir))

    def never_call(request: httpx.Request) -> httpx.Response:
        raise AssertionError("/api/status must not perform network I/O")

    never_client = httpx.Client(
        base_url="http://cache-server.invalid",
        transport=httpx.MockTransport(never_call),
    )
    monkeypatch.setattr(server, "build_http_client", lambda config: never_client)

    server.reset_for_tests()
    try:
        with TestClient(server.app) as test_client:
            response = test_client.get("/api/status", params={"stem": "9999"})
        assert response.status_code == 200
        body = response.json()
        assert body["state"] == "missing"
        assert body["operationId"] is None
        assert "serverEpoch" in body
    finally:
        server.reset_for_tests()


# ------------------------------------------------------------- ordinary


def test_symbols_can_be_filtered_by_prefix(client):
    assert client.get("/api/symbols").json() == {"total": 1, "symbols": ["1301"]}
    assert client.get("/api/symbols", params={"q": "99"}).json()["symbols"] == []


def test_symbol_detail_exposes_the_canonical_code_and_range(client):
    body = client.get("/api/symbols/1301").json()

    assert body["pending"] is False
    assert body["code"] == "13010"
    assert body["firstDate"] == "2026-08-14"
    assert body["lastDate"] == "2026-08-17"


def test_unknown_symbol_is_a_404(client):
    assert client.get("/api/symbols/9999").status_code == 404


def test_session_returns_columnar_ticks(client):
    body = client.get(
        "/api/session", params={"stem": "1301", "date": "2026-08-17"}
    ).json()

    assert body["pending"] is False
    assert body["count"] == 2
    assert body["price"] == [102.0, 103.0]
    assert body["qty"] == [300, 400]
    assert len(body["us"]) == len(body["type"]) == 2


def test_session_can_snap_backwards_to_the_nearest_trading_day(client):
    body = client.get(
        "/api/session",
        params={"stem": "1301", "date": "2026-08-16", "direction": -1},
    ).json()

    assert body["date"] == "2026-08-14"


def test_session_without_snapping_is_a_404_on_an_empty_day(client):
    response = client.get(
        "/api/session", params={"stem": "1301", "date": "2026-08-15", "direction": 0}
    )

    assert response.status_code == 404


def test_a_malformed_date_is_rejected(client):
    response = client.get("/api/session", params={"stem": "1301", "date": "17/08/2026"})

    assert response.status_code == 400
    assert "invalid date" in response.json()["detail"]


# ------------------------------------------------------- /api/minute-context


def test_minute_context_returns_bars_before_the_cutoff(client, minute_db_factory):
    minute_db_factory(
        "1301",
        [
            ("2026-08-14", "09:00", "13010", 100.0, 101.0, 99.0, 100.5, 1000, 100500),
            ("2026-08-14", "09:01", "13010", 100.5, 102.0, 100.0, 101.5, 2000, 203000),
        ],
    )

    body = client.get(
        "/api/minute-context",
        params={
            "stem": "1301",
            "code": "13010",
            "date": "2026-08-17",
            "time": "09:00",
            "limit": 5,
        },
    ).json()

    assert [bar["close"] for bar in body["bars"]] == [100.5, 101.5]


def test_minute_context_degrades_to_an_empty_list_when_no_minute_file_exists(client):
    """No `minute_db_factory` call for this stem — the mock server 404s the
    download — this must be a 200 with an empty list, never an error: a
    session load must never fail just because this optional preload
    couldn't be fetched."""
    response = client.get(
        "/api/minute-context",
        params={"stem": "1301", "code": "13010", "date": "2026-08-17", "time": "09:00"},
    )
    assert response.status_code == 200
    assert response.json() == {"bars": []}


def test_index_and_static_assets_are_served(client):
    index = client.get("/")
    assert index.status_code == 200
    assert "歩み値リプレイ" in index.text

    library = client.get("/static/vendor/lightweight-charts.standalone.production.js")
    assert library.status_code == 200
    assert "Lightweight Charts" in library.text[:400]


def test_mjs_static_assets_are_served_with_a_javascript_content_type(client):
    """Regression test: Python's ``mimetypes`` derives its table from the OS
    registry on Windows, which commonly has no entry for ``.mjs`` and falls
    back to ``text/plain``. A browser refuses to execute a
    ``<script type="module">`` served with that Content-Type ("Failed to
    load module script"), so ``request-coordinator.mjs`` would 200 but
    silently never run — found only by loading the app in a real browser,
    not by any status-code/body assertion. See ``server.py``'s
    ``mimetypes.add_type("text/javascript", ".mjs")`` call.
    """
    response = client.get("/static/request-coordinator.mjs")
    assert response.status_code == 200
    content_type = response.headers["content-type"]
    assert content_type.split(";")[0].strip() in {
        "text/javascript",
        "application/javascript",
    }, content_type


# ------------------------------------------ pending/operationId handshake


def test_consecutive_operations_reset_revision_and_ignore_old_terminal_or_retry():
    tracker = server.OperationTracker()
    first_release = threading.Event()
    first_started = threading.Event()

    def first_run(operation_id: int) -> server.OperationOutcome:
        tracker.report_progress("1301", operation_id, 1, 2)
        tracker.report_progress("1301", operation_id, 2, 2)
        first_started.set()
        first_release.wait(timeout=5)
        return server.OperationOutcome(state="fresh")

    first_id = tracker.start_if_needed("1301", first_run)
    assert first_started.wait(timeout=5)
    assert tracker.snapshot("1301").revision == 2
    assert tracker.start_if_needed("1301", first_run) == first_id
    first_release.set()

    deadline = time.monotonic() + 5
    while tracker.snapshot("1301").state == "downloading":
        assert time.monotonic() < deadline
        time.sleep(0.01)
    first_terminal = tracker.snapshot("1301")
    assert first_terminal.operation_id == first_id
    assert first_terminal.revision == 3

    second_release = threading.Event()
    second_started = threading.Event()
    retry_runs = {"n": 0}

    def second_run(operation_id: int) -> server.OperationOutcome:
        second_started.set()
        second_release.wait(timeout=5)
        return server.OperationOutcome(state="fresh")

    def retry_run(operation_id: int) -> server.OperationOutcome:
        retry_runs["n"] += 1
        return server.OperationOutcome(state="fresh")

    second_id = tracker.start_if_needed("1301", second_run)
    assert second_started.wait(timeout=5)
    assert second_id > first_id
    assert tracker.snapshot("1301").revision == 0

    tracker.report_progress("1301", first_id, 999, 999)
    tracker._finish("1301", first_id, state="corrupt", error="old terminal")
    current = tracker.snapshot("1301")
    assert current.operation_id == second_id
    assert current.state == "downloading"
    assert current.revision == 0
    assert tracker.start_if_needed("1301", retry_run) == second_id
    assert retry_runs["n"] == 0

    second_release.set()
    deadline = time.monotonic() + 5
    while tracker.snapshot("1301").state == "downloading":
        assert time.monotonic() < deadline
        time.sleep(0.01)
    second_terminal = tracker.snapshot("1301")
    assert second_terminal.operation_id == second_id
    assert second_terminal.revision == 1
    assert second_terminal.state == "fresh"


def test_session_returns_pending_then_status_tracks_it_to_completion(
    cache_dir, remote_store, tick_db_factory, monkeypatch
):
    tick_db_factory("1301", ROWS)
    monkeypatch.setenv(CACHE_DIR_ENV_VAR, str(cache_dir))

    # A deliberately slow file download (the listing stays fast) so the
    # test has a real window to issue a second, overlapping request before
    # the first operation completes.
    release = threading.Event()

    def slow_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/stocks-trades":
            return httpx.Response(200, json={"stems": sorted(remote_store)})
        release.wait(timeout=5)
        body = remote_store["1301"]
        return httpx.Response(
            200, headers={"Content-Length": str(len(body))}, content=body
        )

    slow_client = httpx.Client(
        base_url="http://cache-server.invalid",
        transport=httpx.MockTransport(slow_handler),
    )
    monkeypatch.setattr(server, "build_http_client", lambda config: slow_client)

    server.reset_for_tests()
    try:
        with TestClient(server.app) as test_client:
            first = test_client.get(
                "/api/session", params={"stem": "1301", "date": "2026-08-17"}
            )
            body = first.json()
            assert body["pending"] is True
            operation_id = body["operationId"]
            assert operation_id is not None
            server_epoch = body["serverEpoch"]

            # A second request for the same stem while the download is
            # still in flight must reuse the same operationId, not start a
            # second one.
            second = test_client.get(
                "/api/session", params={"stem": "1301", "date": "2026-08-17"}
            )
            assert second.json()["operationId"] == operation_id

            release.set()  # let the slow download proceed to completion

            deadline = time.monotonic() + 5
            state = None
            while time.monotonic() < deadline:
                status = test_client.get("/api/status", params={"stem": "1301"}).json()
                assert status["serverEpoch"] == server_epoch
                assert status["operationId"] == operation_id
                state = status["state"]
                if state in ("fresh", "corrupt"):
                    break
                time.sleep(0.01)
            assert state == "fresh"

            final = test_client.get(
                "/api/session", params={"stem": "1301", "date": "2026-08-17"}
            )
            final_body = final.json()
            assert final_body["pending"] is False
            assert final_body["count"] == 2
    finally:
        server.reset_for_tests()


def test_failed_revalidation_serves_existing_cache_as_stale_once_per_process(
    cache_dir, tick_db_factory, monkeypatch
):
    body = tick_db_factory("1301", ROWS)
    server.cache.live_file_path(cache_dir, "1301").write_bytes(body)
    monkeypatch.setenv(CACHE_DIR_ENV_VAR, str(cache_dir))
    monkeypatch.setattr(server.cache, "RETRY_BACKOFF_BASE_SECONDS", 0.0)
    calls = {"listing": 0, "file": 0}

    def offline_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/stocks-trades":
            calls["listing"] += 1
            return httpx.Response(503)
        calls["file"] += 1
        raise httpx.ConnectError("offline", request=request)

    offline_client = httpx.Client(
        base_url="http://cache-server.invalid",
        transport=httpx.MockTransport(offline_handler),
    )
    monkeypatch.setattr(server, "build_http_client", lambda config: offline_client)

    server.reset_for_tests()
    try:
        with TestClient(server.app) as test_client:
            started = test_client.get(
                "/api/session", params={"stem": "1301", "date": "2026-08-17"}
            ).json()
            assert started["pending"] is True
            operation_id = started["operationId"]

            deadline = time.monotonic() + 5
            status = None
            while time.monotonic() < deadline:
                status = test_client.get("/api/status", params={"stem": "1301"}).json()
                if status["state"] != "downloading":
                    break
                time.sleep(0.01)

            assert status is not None
            assert status["operationId"] == operation_id
            assert status["state"] == "stale-served"
            assert "offline" in status["error"]
            assert calls["file"] == server.cache.MAX_DOWNLOAD_ATTEMPTS

            final = test_client.get(
                "/api/session", params={"stem": "1301", "date": "2026-08-17"}
            ).json()
            assert final["pending"] is False
            assert final["count"] == 2

            test_client.get(
                "/api/session", params={"stem": "1301", "date": "2026-08-17"}
            )
            assert calls["file"] == server.cache.MAX_DOWNLOAD_ATTEMPTS
    finally:
        server.reset_for_tests()
