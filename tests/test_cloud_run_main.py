"""Tests for cloud-run/main.py (the home-hosted DuckDB file server, now
merged with the 歩み値リプレイ (tickreplay) FastAPI app it also mounts).

``cloud-run/main.py`` reads ``STOCKDATA_CACHE_DIR`` once, at import time, so
every test imports it fresh (via the ``cloud_run_main`` fixture) after
pointing that env var at a per-test fixture directory. Every route now runs
under an ASGI ``lifespan`` (main.py composes its own lifespan with
tickreplay's, so tickreplay's repository gets constructed) — the ``client``
fixture enters ``TestClient`` as a context manager, which is what actually
triggers ASGI startup/shutdown; a bare (non-context-manager) call would skip
lifespan entirely and any tickreplay route would then 500.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

CLOUD_RUN_DIR = Path(__file__).resolve().parents[1] / "cloud-run"


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    directory = tmp_path / "cache"
    directory.mkdir()
    monkeypatch.setenv("STOCKDATA_CACHE_DIR", str(directory))
    return directory


@pytest.fixture
def cloud_run_main(data_dir, monkeypatch):
    """Import ``cloud-run/main.py`` fresh, with ``STOCKDATA_CACHE_DIR`` set.

    The module name ("main") is generic on purpose — it is inserted into and
    removed from ``sys.path``/``sys.modules`` around the import so it never
    leaks into other test files. ``tickreplay.server`` is *not* re-imported
    per test (only "main" is) — it keeps its own module-level repository/
    tracker singleton, reset via ``reset_for_tests()`` once this fixture's
    ``client`` (below) has fully torn down its lifespan for the test.
    """
    monkeypatch.syspath_prepend(str(CLOUD_RUN_DIR))
    sys.modules.pop("main", None)
    try:
        import main as module

        yield module
    finally:
        sys.modules.pop("main", None)
        import tickreplay.server

        tickreplay.server.reset_for_tests()


@pytest.fixture
def client(cloud_run_main):
    """A ``TestClient`` entered as a context manager, so ASGI lifespan
    (main.py's own, composed with tickreplay's) actually runs for the whole
    test — required for any tickreplay route (``/``, ``/api/status``, ...)
    to work at all, and matches how a real ASGI server behaves."""
    with TestClient(cloud_run_main.app) as test_client:
        yield test_client


@pytest.fixture
def sample_files(data_dir):
    trades_dir = data_dir / "jp" / "stocks_trades"
    trades_dir.mkdir(parents=True)
    target = trades_dir / "7203.duckdb"
    target.write_bytes(b"0123456789" * 10)  # 100 bytes — easy Range math
    (trades_dir / "1301.duckdb").write_bytes(b"x")
    (trades_dir / "not-a-duckdb.txt").write_bytes(b"ignore me")
    board_dir = data_dir / "jp" / "stocks_board"
    board_dir.mkdir()
    board_target = board_dir / "7203.duckdb"
    board_target.write_bytes(b"board-data")
    # Minute files are keyed by the same stem as stocks_trades, so the set
    # here mirrors it: a digits-only stem and a letter-bearing one.
    minute_dir = data_dir / "jp" / "stocks_minute"
    minute_dir.mkdir()
    (minute_dir / "7203.duckdb").write_bytes(b"minute-data")
    (minute_dir / "285A.duckdb").write_bytes(b"minute-data-285A")
    # `130a` deliberately lowercase, and no uppercase sibling: the real
    # dataset spells some minute files in the opposite case from their
    # stocks_trades counterpart, which clients always request upper-cased.
    # (Only one case of the stem is created — on a case-insensitive host
    # filesystem the two names would be the same file.)
    (minute_dir / "130a.duckdb").write_bytes(b"minute-data-130a")
    return {
        "dir": trades_dir,
        "target": target,
        "board_target": board_target,
        "minute_dir": minute_dir,
    }


# ------------------------------------------------------------------- healthz


def test_healthz_returns_ok(client):
    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.text == "OK"


# --------------------------------------------------------- /api/stocks-trades


def test_list_stocks_trades_returns_sorted_deduped_stems(client, sample_files):
    body = client.get("/api/stocks-trades").json()

    assert body == {"stems": ["1301", "7203"]}


def test_list_stocks_trades_ignores_non_duckdb_files(client, sample_files):
    body = client.get("/api/stocks-trades").json()

    assert "not-a-duckdb" not in body["stems"]


def test_list_stocks_trades_is_empty_when_directory_exists_and_is_empty(
    client, data_dir
):
    (data_dir / "jp" / "stocks_trades").mkdir(parents=True)

    response = client.get("/api/stocks-trades")

    assert response.status_code == 200
    assert response.json() == {"stems": []}


def test_list_stocks_trades_returns_503_when_directory_is_absent(client):
    response = client.get("/api/stocks-trades")

    assert response.status_code == 503
    assert response.json() == {"error": "stocks_trades listing unavailable"}


def test_list_stocks_trades_returns_503_when_directory_read_fails(
    cloud_run_main, client, sample_files, monkeypatch
):
    def raise_permission_error(_directory):
        raise PermissionError("fixture directory is unreadable")

    monkeypatch.setattr(cloud_run_main.os, "scandir", raise_permission_error)

    response = client.get("/api/stocks-trades")

    assert response.status_code == 503
    assert response.json() == {"error": "stocks_trades listing unavailable"}


def test_list_stocks_trades_returns_503_when_entry_stat_fails(
    cloud_run_main, client, sample_files, monkeypatch
):
    class UnreadableEntry:
        name = "7203.duckdb"

        def is_file(self):
            raise PermissionError("fixture entry cannot be stat-ed")

    class StubScandir:
        def __enter__(self):
            return iter([UnreadableEntry()])

        def __exit__(self, _exc_type, _exc_value, _traceback):
            return False

    monkeypatch.setattr(cloud_run_main.os, "scandir", lambda _directory: StubScandir())

    response = client.get("/api/stocks-trades")

    assert response.status_code == 503
    assert response.json() == {"error": "stocks_trades listing unavailable"}


# --------------------------------------------------------------- existing GET


def test_download_known_file_returns_200_with_range_headers(client, sample_files):
    response = client.get("/jp/stocks_trades/7203.duckdb")

    assert response.status_code == 200
    assert response.headers.get("ETag")
    assert response.headers.get("Last-Modified")
    assert response.headers.get("Accept-Ranges") == "bytes"


def test_download_unknown_path_is_404(client, sample_files):
    assert client.get("/jp/unknown/1.duckdb").status_code == 404


def test_download_unknown_symbol_is_404(client, sample_files):
    assert client.get("/jp/stocks_trades/9999.duckdb").status_code == 404


def test_download_disallowed_extension_is_404(client, sample_files):
    assert client.get("/jp/stocks_trades/7203.txt").status_code == 404


def test_download_stocks_board_file_remains_allowed(client, sample_files):
    response = client.get("/jp/stocks_board/7203.duckdb")

    assert response.status_code == 200
    assert response.content == b"board-data"


def test_download_stocks_minute_file_is_allowed(client, sample_files):
    response = client.get("/jp/stocks_minute/7203.duckdb")

    assert response.status_code == 200
    assert response.content == b"minute-data"


def test_download_stocks_minute_file_with_letter_in_stem_is_allowed(
    client, sample_files
):
    """A letter-bearing stem's minute file must be served, same as its
    stocks_trades counterpart. The whitelist used to accept digits only for
    `stocks_minute`, which 404'd every such symbol (including the UI's
    default, `285A`) before the file on disk was ever consulted — the minute
    chart then silently came up empty for them.
    """
    response = client.get("/jp/stocks_minute/285A.duckdb")

    assert response.status_code == 200
    assert response.content == b"minute-data-285A"


def test_download_absent_stocks_minute_file_is_404(client, sample_files):
    assert client.get("/jp/stocks_minute/9999.duckdb").status_code == 404


def test_download_resolves_a_stem_stored_under_the_other_case(client, sample_files):
    """An upper-cased request must find a lowercase-only file on disk.

    Clients only ever ask for upper-cased stems, while the dataset spells
    some minute files lowercase — on the case-sensitive filesystem of the
    deployment container (unlike the Windows host the tree is authored on)
    that mismatch is a 404 for a file that exists.
    """
    response = client.get("/jp/stocks_minute/130A.duckdb")

    assert response.status_code == 200
    assert response.content == b"minute-data-130a"


def test_stem_case_variants_tries_both_cases_of_a_letter_bearing_stem(
    cloud_run_main,
):
    variants = cloud_run_main._stem_case_variants(
        Path("/data/jp/stocks_minute/285A.duckdb")
    )

    # The request as it came in stays first, so an exactly-matching file
    # always wins over a differently-cased sibling.
    assert [path.name for path in variants] == ["285A.duckdb", "285a.duckdb"]


def test_stem_case_variants_of_a_lowercase_request_includes_the_upper_case(
    cloud_run_main,
):
    variants = cloud_run_main._stem_case_variants(
        Path("/data/jp/stocks_trades/130a.duckdb")
    )

    assert [path.name for path in variants] == ["130a.duckdb", "130A.duckdb"]


def test_stem_case_variants_of_a_digits_only_stem_is_just_itself(cloud_run_main):
    variants = cloud_run_main._stem_case_variants(
        Path("/data/jp/stocks_minute/7203.duckdb")
    )

    assert [path.name for path in variants] == ["7203.duckdb"]


def test_stem_case_variants_never_leaves_the_requested_directory(cloud_run_main):
    """Only the stem's case varies — the directory and suffix are untouched,
    so this cannot widen what `ALLOWED_PATHS` already let through."""
    source = Path("/data/jp/stocks_minute/285A.duckdb")

    for variant in cloud_run_main._stem_case_variants(source):
        assert variant.parent == source.parent
        assert variant.suffix == source.suffix
        assert variant.stem.lower() == source.stem.lower()


# --------------------------------------------------------------- conditional


def test_conditional_get_returns_304_for_matching_etag(client, sample_files):
    first = client.get("/jp/stocks_trades/7203.duckdb")
    etag = first.headers["ETag"]

    second = client.get(
        "/jp/stocks_trades/7203.duckdb", headers={"If-None-Match": etag}
    )

    assert second.status_code == 304


def test_conditional_get_returns_304_for_if_modified_since(client, sample_files):
    first = client.get("/jp/stocks_trades/7203.duckdb")
    last_modified = first.headers["Last-Modified"]

    second = client.get(
        "/jp/stocks_trades/7203.duckdb",
        headers={"If-Modified-Since": last_modified},
    )

    assert second.status_code == 304


def test_stale_if_none_match_still_returns_200(client, sample_files):
    response = client.get(
        "/jp/stocks_trades/7203.duckdb", headers={"If-None-Match": '"stale-etag"'}
    )

    assert response.status_code == 200


# -------------------------------------------------------------------- range


def test_range_request_returns_206_partial_content(client, sample_files):
    response = client.get(
        "/jp/stocks_trades/7203.duckdb", headers={"Range": "bytes=0-9"}
    )

    assert response.status_code == 206
    assert response.content == b"0123456789"
    assert response.headers["Content-Range"] == "bytes 0-9/100"


def test_if_range_matching_etag_returns_206_partial_content(client, sample_files):
    first = client.get("/jp/stocks_trades/7203.duckdb")

    response = client.get(
        "/jp/stocks_trades/7203.duckdb",
        headers={"Range": "bytes=10-19", "If-Range": first.headers["ETag"]},
    )

    assert response.status_code == 206
    assert response.content == b"0123456789"
    assert response.headers["Content-Range"] == "bytes 10-19/100"


def test_if_range_mismatching_etag_returns_full_200(client, sample_files):
    response = client.get(
        "/jp/stocks_trades/7203.duckdb",
        headers={"Range": "bytes=10-19", "If-Range": '"stale-etag"'},
    )

    assert response.status_code == 200
    assert response.content == b"0123456789" * 10
    assert "Content-Range" not in response.headers


@pytest.mark.parametrize(
    "range_header",
    ["bytes=10000-10010", "bytes=abc-def", "bytes=10-1", "bytes=0-1,5-6"],
)
def test_range_request_invalid_or_unsatisfiable_returns_416(
    client, sample_files, range_header
):
    """Covers out-of-bounds, non-numeric, reversed (`bytes=10-1`), and
    multiple (`bytes=0-1,5-6`) ranges — the legacy Flask/Werkzeug route
    returned 416 for every one of these, never a 400 or a 206."""
    response = client.get(
        "/jp/stocks_trades/7203.duckdb", headers={"Range": range_header}
    )

    assert response.status_code == 416
    assert response.headers["Content-Range"] == "bytes */100"


def test_range_unit_is_case_insensitive(client, sample_files):
    response = client.get(
        "/jp/stocks_trades/7203.duckdb", headers={"Range": "Bytes=0-9"}
    )

    assert response.status_code == 206
    assert response.content == b"0123456789"


# ---------------------------------------------------------------------- HEAD


def test_head_known_file_returns_200_with_no_body(client, sample_files):
    response = client.head("/jp/stocks_trades/7203.duckdb")

    assert response.status_code == 200
    assert response.content == b""
    assert response.headers.get("ETag")
    assert response.headers.get("Last-Modified")
    assert response.headers["Content-Length"] == "100"


def test_head_unknown_symbol_is_404(client, sample_files):
    assert client.head("/jp/stocks_trades/9999.duckdb").status_code == 404


# ------------------------------------------------------- wildcard preconditions


def test_if_none_match_wildcard_returns_304(client, sample_files):
    response = client.get(
        "/jp/stocks_trades/7203.duckdb", headers={"If-None-Match": "*"}
    )

    assert response.status_code == 304


def test_stale_if_match_returns_412(client, sample_files):
    response = client.get(
        "/jp/stocks_trades/7203.duckdb", headers={"If-Match": '"stale-etag"'}
    )

    assert response.status_code == 412


def test_matching_if_match_returns_200(client, sample_files):
    first = client.get("/jp/stocks_trades/7203.duckdb")

    response = client.get(
        "/jp/stocks_trades/7203.duckdb",
        headers={"If-Match": first.headers["ETag"]},
    )

    assert response.status_code == 200


# ----------------------------------------------------------------- non-regular


def test_directory_sharing_an_allowed_name_is_404_not_empty_200(client, sample_files):
    """A directory can never legitimately live at an allowed `.duckdb`
    path, but `Path.stat()` alone can't tell it apart from a regular file —
    this must be rejected as 404, not served as a `200` with an empty
    body."""
    (sample_files["dir"] / "9999.duckdb").mkdir()

    response = client.get("/jp/stocks_trades/9999.duckdb")

    assert response.status_code == 404
    assert response.content != b""


def test_symlink_escaping_the_data_root_is_404(
    client, data_dir, sample_files, tmp_path
):
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    secret = outside_dir / "secret.txt"
    secret.write_bytes(b"should never be servable")
    link = sample_files["dir"] / "9999.duckdb"
    try:
        link.symlink_to(secret)
    except OSError:
        pytest.skip("symlink creation is not permitted on this machine")

    response = client.get("/jp/stocks_trades/9999.duckdb")

    assert response.status_code == 404


# ------------------------------------------------------------ tickreplay mount


def test_root_falls_through_to_tickreplay_index(client, sample_files):
    response = client.get("/")

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "歩み値リプレイ" in response.text


def test_tickreplay_symbols_endpoint_reachable_through_lifespan(client, sample_files):
    """Proves main.py's composed lifespan actually runs tickreplay's own
    lifespan. `/api/symbols` calls `get_repository()` (unlike `/api/status`,
    which only reads the tracker and would pass even without a repository)
    — without the composed lifespan, this 500s with "repository is
    unavailable outside the app lifespan" instead of answering normally."""
    response = client.get("/api/symbols")

    assert response.status_code == 200
    body = response.json()
    assert "symbols" in body


# --------------------------------------------------------------------- graphql


def test_graphql_ranking_query_returns_ranked_rows(client, data_dir):
    import duckdb

    daily_dir = data_dir / "jp" / "stocks_daily"
    daily_dir.mkdir(parents=True)
    con = duckdb.connect(str(daily_dir / "mother.duckdb"))
    con.execute(
        "CREATE TABLE stocks_daily "
        "(Code VARCHAR, Date VARCHAR, Open DOUBLE, High DOUBLE, "
        "Low DOUBLE, Close DOUBLE, Volume DOUBLE)"
    )
    con.execute(
        "INSERT INTO stocks_daily VALUES ('7203', '2024-01-05', 102, 108, 101, 107, 1200)"
    )
    con.close()

    response = client.post(
        "/graphql",
        json={
            "query": (
                'query { stockRankingRange(fromDate: "2024-01-05", '
                'toDate: "2024-01-05") { code close rank } }'
            )
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert "errors" not in body
    assert body["data"]["stockRankingRange"] == [
        {"code": "7203", "close": 107.0, "rank": 1}
    ]


def test_graphql_internal_error_does_not_leak_server_path(client, data_dir):
    """`mother.duckdb` is absent, so DuckDB's open failure embeds the
    server's absolute cache path in its exception message — that raw
    message must never reach an unauthenticated GraphQL client."""
    (data_dir / "jp" / "stocks_trades").mkdir(parents=True)

    response = client.post(
        "/graphql",
        json={
            "query": (
                'query { stockRankingRange(fromDate: "2024-01-01", '
                'toDate: "2024-01-31") { code } }'
            )
        },
    )

    assert response.status_code == 200
    body = response.json()
    message = body["errors"][0]["message"]
    assert str(data_dir) not in message
    assert message == "stock_ranking_range: internal error"


# -------------------------------------------------------------- docker/runtime


def test_dockerfile_pins_a_single_asgi_worker():
    """`tickreplay.server` and this file's own `OperationTracker`/repository
    state are process-local module globals — a second worker process would
    silently split that state instead of failing loudly. Guards the
    Dockerfile's `CMD` against a future edit that raises `--workers`."""
    dockerfile = (CLOUD_RUN_DIR / "Dockerfile").read_text(encoding="utf-8")

    assert "--workers 1" in dockerfile
    assert "uvicorn.workers.UvicornWorker" in dockerfile


# ------------------------------------------ merged tickreplay cache defaults


@pytest.fixture
def isolated_cache_env(monkeypatch):
    """Pin the tickreplay cache vars so this repo's own ``.env`` (loaded by
    ``load_dotenv()`` at main.py import) cannot decide the outcome."""

    def apply(cache_dir, server_url):
        monkeypatch.setenv("BACKCAST_DUCKDB_CACHE_DIR", str(cache_dir))
        monkeypatch.setenv("BACKCAST_DUCKDB_SERVER_URL", server_url)
        # setenv-then-delenv, so monkeypatch has recorded the variable and
        # will remove it at teardown even though the *module import* is what
        # sets it (via `os.environ.setdefault`, which monkeypatch cannot see).
        monkeypatch.setenv("BACKCAST_DUCKDB_LOCAL_AUTHORITATIVE", "")
        monkeypatch.delenv("BACKCAST_DUCKDB_LOCAL_AUTHORITATIVE")

    return apply


def _clear_cache_env(monkeypatch):
    import dotenv

    monkeypatch.setattr(dotenv, "load_dotenv", lambda: False)
    for name in (
        "BACKCAST_DUCKDB_CACHE_DIR",
        "BACKCAST_DUCKDB_SERVER_URL",
        "BACKCAST_DUCKDB_LOCAL_AUTHORITATIVE",
    ):
        # Record each variable with monkeypatch before removing it, because
        # importing main.py populates all three via `os.environ.setdefault`.
        monkeypatch.setenv(name, "")
        monkeypatch.delenv(name)


def _import_main(monkeypatch):
    monkeypatch.syspath_prepend(str(CLOUD_RUN_DIR))
    sys.modules.pop("main", None)
    import main as module

    return module


def test_merged_defaults_explicitly_enable_local_authoritative(data_dir, monkeypatch):
    _clear_cache_env(monkeypatch)
    try:
        _import_main(monkeypatch)

        assert os.environ["BACKCAST_DUCKDB_SERVER_URL"] == "http://127.0.0.1:8080"
        assert os.environ["BACKCAST_DUCKDB_CACHE_DIR"] == str(data_dir / "jp")
        assert os.environ["BACKCAST_DUCKDB_LOCAL_AUTHORITATIVE"] == "true"
    finally:
        sys.modules.pop("main", None)


def test_explicit_server_and_cache_values_do_not_imply_authority(
    data_dir, isolated_cache_env, monkeypatch
):
    # Even values equal to this module's defaults are operator overrides.
    # The third flag must be explicit; path/URL contents are never inferred.
    isolated_cache_env(data_dir / "jp", "http://127.0.0.1:8080")
    try:
        _import_main(monkeypatch)

        assert os.environ["BACKCAST_DUCKDB_LOCAL_AUTHORITATIVE"] == "false"
    finally:
        sys.modules.pop("main", None)


@pytest.mark.parametrize("value", ["true", "false"])
def test_explicit_local_authoritative_value_is_preserved(
    data_dir, isolated_cache_env, monkeypatch, value
):
    isolated_cache_env(data_dir / "jp", "http://backcast.i234.me:8080")
    monkeypatch.setenv("BACKCAST_DUCKDB_LOCAL_AUTHORITATIVE", value)
    try:
        _import_main(monkeypatch)

        assert os.environ["BACKCAST_DUCKDB_LOCAL_AUTHORITATIVE"] == value
    finally:
        sys.modules.pop("main", None)


def test_the_mounted_repository_actually_receives_the_mode(data_dir, monkeypatch):
    """The flag is worthless unless it survives config resolution and
    reaches the repository the mounted app really uses."""
    _clear_cache_env(monkeypatch)
    module = _import_main(monkeypatch)
    try:
        import tickreplay.server

        with TestClient(module.app):
            assert tickreplay.server.get_repository().local_authoritative is True
    finally:
        sys.modules.pop("main", None)
        import tickreplay.server

        tickreplay.server.reset_for_tests()
