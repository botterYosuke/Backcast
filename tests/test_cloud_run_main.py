"""Tests for cloud-run/main.py (the home-hosted DuckDB file server).

``cloud-run/main.py`` reads ``STOCKDATA_CACHE_DIR`` once, at import time, so
every test imports it fresh (via the ``cloud_run_main`` fixture) after
pointing that env var at a per-test fixture directory.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

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
    leaks into other test files.
    """
    monkeypatch.syspath_prepend(str(CLOUD_RUN_DIR))
    sys.modules.pop("main", None)
    try:
        import main as module

        yield module
    finally:
        sys.modules.pop("main", None)


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
    return {"dir": trades_dir, "target": target, "board_target": board_target}


# --------------------------------------------------------- /api/stocks-trades


def test_list_stocks_trades_returns_sorted_deduped_stems(cloud_run_main, sample_files):
    client = cloud_run_main.app.test_client()

    body = client.get("/api/stocks-trades").get_json()

    assert body == {"stems": ["1301", "7203"]}


def test_list_stocks_trades_ignores_non_duckdb_files(cloud_run_main, sample_files):
    client = cloud_run_main.app.test_client()

    body = client.get("/api/stocks-trades").get_json()

    assert "not-a-duckdb" not in body["stems"]


def test_list_stocks_trades_is_empty_when_directory_exists_and_is_empty(
    cloud_run_main, data_dir
):
    (data_dir / "jp" / "stocks_trades").mkdir(parents=True)
    client = cloud_run_main.app.test_client()

    response = client.get("/api/stocks-trades")

    assert response.status_code == 200
    assert response.get_json() == {"stems": []}


def test_list_stocks_trades_returns_503_when_directory_is_absent(cloud_run_main):
    client = cloud_run_main.app.test_client()

    response = client.get("/api/stocks-trades")

    assert response.status_code == 503
    assert response.get_json() == {"error": "stocks_trades listing unavailable"}


def test_list_stocks_trades_returns_503_when_directory_read_fails(
    cloud_run_main, sample_files, monkeypatch
):
    def raise_permission_error(_directory):
        raise PermissionError("fixture directory is unreadable")

    monkeypatch.setattr(cloud_run_main.os, "scandir", raise_permission_error)
    client = cloud_run_main.app.test_client()

    response = client.get("/api/stocks-trades")

    assert response.status_code == 503
    assert response.get_json() == {"error": "stocks_trades listing unavailable"}


def test_list_stocks_trades_returns_503_when_entry_stat_fails(
    cloud_run_main, sample_files, monkeypatch
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
    client = cloud_run_main.app.test_client()

    response = client.get("/api/stocks-trades")

    assert response.status_code == 503
    assert response.get_json() == {"error": "stocks_trades listing unavailable"}


# --------------------------------------------------------------- existing GET


def test_download_known_file_returns_200_with_range_headers(
    cloud_run_main, sample_files
):
    client = cloud_run_main.app.test_client()

    response = client.get("/jp/stocks_trades/7203.duckdb")

    assert response.status_code == 200
    assert response.headers.get("ETag")
    assert response.headers.get("Last-Modified")
    assert response.headers.get("Accept-Ranges") == "bytes"


def test_download_unknown_path_is_404(cloud_run_main, sample_files):
    client = cloud_run_main.app.test_client()

    assert client.get("/jp/unknown/1.duckdb").status_code == 404


def test_download_unknown_symbol_is_404(cloud_run_main, sample_files):
    client = cloud_run_main.app.test_client()

    assert client.get("/jp/stocks_trades/9999.duckdb").status_code == 404


def test_download_disallowed_extension_is_404(cloud_run_main, sample_files):
    client = cloud_run_main.app.test_client()

    assert client.get("/jp/stocks_trades/7203.txt").status_code == 404


def test_download_stocks_board_file_remains_allowed(cloud_run_main, sample_files):
    client = cloud_run_main.app.test_client()

    response = client.get("/jp/stocks_board/7203.duckdb")

    assert response.status_code == 200
    assert response.data == b"board-data"


# --------------------------------------------------------------- conditional


def test_conditional_get_returns_304_for_matching_etag(cloud_run_main, sample_files):
    client = cloud_run_main.app.test_client()
    first = client.get("/jp/stocks_trades/7203.duckdb")
    etag = first.headers["ETag"]

    second = client.get(
        "/jp/stocks_trades/7203.duckdb", headers={"If-None-Match": etag}
    )

    assert second.status_code == 304


def test_conditional_get_returns_304_for_if_modified_since(
    cloud_run_main, sample_files
):
    client = cloud_run_main.app.test_client()
    first = client.get("/jp/stocks_trades/7203.duckdb")
    last_modified = first.headers["Last-Modified"]

    second = client.get(
        "/jp/stocks_trades/7203.duckdb",
        headers={"If-Modified-Since": last_modified},
    )

    assert second.status_code == 304


def test_stale_if_none_match_still_returns_200(cloud_run_main, sample_files):
    client = cloud_run_main.app.test_client()

    response = client.get(
        "/jp/stocks_trades/7203.duckdb", headers={"If-None-Match": '"stale-etag"'}
    )

    assert response.status_code == 200


# -------------------------------------------------------------------- range


def test_range_request_returns_206_partial_content(cloud_run_main, sample_files):
    client = cloud_run_main.app.test_client()

    response = client.get(
        "/jp/stocks_trades/7203.duckdb", headers={"Range": "bytes=0-9"}
    )

    assert response.status_code == 206
    assert response.data == b"0123456789"
    assert response.headers["Content-Range"] == "bytes 0-9/100"


def test_if_range_matching_etag_returns_206_partial_content(
    cloud_run_main, sample_files
):
    client = cloud_run_main.app.test_client()
    first = client.get("/jp/stocks_trades/7203.duckdb")

    response = client.get(
        "/jp/stocks_trades/7203.duckdb",
        headers={"Range": "bytes=10-19", "If-Range": first.headers["ETag"]},
    )

    assert response.status_code == 206
    assert response.data == b"0123456789"
    assert response.headers["Content-Range"] == "bytes 10-19/100"


def test_if_range_mismatching_etag_returns_full_200(cloud_run_main, sample_files):
    client = cloud_run_main.app.test_client()

    response = client.get(
        "/jp/stocks_trades/7203.duckdb",
        headers={"Range": "bytes=10-19", "If-Range": '"stale-etag"'},
    )

    assert response.status_code == 200
    assert response.data == b"0123456789" * 10
    assert "Content-Range" not in response.headers


@pytest.mark.parametrize("range_header", ["bytes=10000-10010", "bytes=abc-def"])
def test_range_request_invalid_or_unsatisfiable_returns_416(
    cloud_run_main, sample_files, range_header
):
    client = cloud_run_main.app.test_client()

    response = client.get(
        "/jp/stocks_trades/7203.duckdb", headers={"Range": range_header}
    )

    assert response.status_code == 416
