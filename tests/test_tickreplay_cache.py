"""Tests for `tickreplay.cache` — download staging (Step 4 of the plan)."""

from __future__ import annotations

import errno
from pathlib import Path

import duckdb
import httpx
import pytest

from tickreplay import cache


def _valid_duckdb_bytes(tmp_path: Path) -> bytes:
    """Build a minimal, structurally valid DuckDB file's raw bytes."""
    path = tmp_path / "_seed.duckdb"
    connection = duckdb.connect(str(path))
    connection.execute("CREATE TABLE t (x INTEGER)")
    connection.execute("INSERT INTO t VALUES (1)")
    connection.close()
    return path.read_bytes()


def _client(handler) -> httpx.Client:
    return httpx.Client(
        base_url="http://cache-server.invalid", transport=httpx.MockTransport(handler)
    )


# --------------------------------------------------------------------- happy


def test_stage_download_writes_a_verified_part_file(tmp_path):
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    body = _valid_duckdb_bytes(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/jp/stocks_trades/7203.duckdb"
        return httpx.Response(
            200,
            headers={
                "Content-Length": str(len(body)),
                "ETag": '"abc123"',
                "Last-Modified": "Fri, 21 Aug 2026 00:00:00 GMT",
            },
            content=body,
        )

    result = cache.stage_download("7203", cache_dir=cache_dir, client=_client(handler))

    assert result.not_modified is False
    assert result.part_path == cache.staged_part_path(cache_dir, "7203")
    assert result.part_path.read_bytes() == body
    assert result.content_length == len(body)
    assert result.etag == '"abc123"'
    assert result.last_modified == "Fri, 21 Aug 2026 00:00:00 GMT"
    assert len(result.sha256) == 64


def test_progress_callback_is_invoked_with_increasing_byte_counts(
    tmp_path, monkeypatch
):
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    body = _valid_duckdb_bytes(tmp_path)
    monkeypatch.setattr(cache, "DOWNLOAD_CHUNK_BYTES", 8)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"Content-Length": str(len(body))}, content=body
        )

    seen: list[tuple[int, int | None]] = []
    cache.stage_download(
        "7203",
        cache_dir=cache_dir,
        client=_client(handler),
        progress_cb=lambda done, total: seen.append((done, total)),
    )

    assert seen
    assert all(total == len(body) for _, total in seen)
    assert [done for done, _ in seen] == sorted(done for done, _ in seen)
    assert seen[-1][0] == len(body)


# ------------------------------------------------------------- conditional


def test_conditional_get_sends_if_none_match_from_the_live_sidecar(tmp_path):
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    sidecar = cache.Sidecar(
        etag='"abc"', last_modified=None, sha256="x" * 64, generation=1
    )
    cache.live_sidecar_path(cache_dir, "7203").write_bytes(sidecar.to_json_bytes())

    seen_headers = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen_headers.update(request.headers)
        return httpx.Response(304)

    result = cache.stage_download(
        "7203", cache_dir=cache_dir, client=_client(handler), conditional=True
    )

    assert result.not_modified is True
    assert result.part_path is None
    assert seen_headers.get("if-none-match") == '"abc"'
    assert not cache.staged_part_path(cache_dir, "7203").exists()


def test_conditional_get_falls_back_to_last_modified_without_an_etag(tmp_path):
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    sidecar = cache.Sidecar(
        etag=None,
        last_modified="Fri, 21 Aug 2026 00:00:00 GMT",
        sha256="x" * 64,
        generation=1,
    )
    cache.live_sidecar_path(cache_dir, "7203").write_bytes(sidecar.to_json_bytes())

    seen_headers = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen_headers.update(request.headers)
        return httpx.Response(304)

    cache.stage_download(
        "7203", cache_dir=cache_dir, client=_client(handler), conditional=True
    )

    assert seen_headers.get("if-modified-since") == "Fri, 21 Aug 2026 00:00:00 GMT"


def test_a_corrupt_sidecar_is_treated_as_absent_not_an_error(tmp_path):
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    cache.live_sidecar_path(cache_dir, "7203").write_bytes(b"{not json")

    assert cache.read_sidecar(cache.live_sidecar_path(cache_dir, "7203")) is None


def test_conditional_get_with_no_prior_sidecar_sends_no_conditional_headers(tmp_path):
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    body = _valid_duckdb_bytes(tmp_path)
    seen_headers = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen_headers.update(request.headers)
        return httpx.Response(
            200, headers={"Content-Length": str(len(body))}, content=body
        )

    cache.stage_download(
        "7203", cache_dir=cache_dir, client=_client(handler), conditional=True
    )

    assert "if-none-match" not in seen_headers
    assert "if-modified-since" not in seen_headers


# --------------------------------------------------------------- failpoints


def test_404_raises_stem_not_on_server_without_retrying(tmp_path):
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(404)

    with pytest.raises(cache.StemNotOnServerError):
        cache.stage_download("9999", cache_dir=cache_dir, client=_client(handler))

    assert calls["n"] == 1


def test_server_error_is_retried_then_raises_server_unreachable(tmp_path, monkeypatch):
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    monkeypatch.setattr(cache, "RETRY_BACKOFF_BASE_SECONDS", 0.0)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(503)

    with pytest.raises(cache.ServerUnreachableError):
        cache.stage_download("7203", cache_dir=cache_dir, client=_client(handler))

    assert calls["n"] == cache.MAX_DOWNLOAD_ATTEMPTS


def test_transport_error_is_retried_then_raises_server_unreachable(
    tmp_path, monkeypatch
):
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    monkeypatch.setattr(cache, "RETRY_BACKOFF_BASE_SECONDS", 0.0)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.ConnectError("boom", request=request)

    with pytest.raises(cache.ServerUnreachableError):
        cache.stage_download("7203", cache_dir=cache_dir, client=_client(handler))

    assert calls["n"] == cache.MAX_DOWNLOAD_ATTEMPTS


def test_content_length_mismatch_raises_corrupt_and_removes_part_file(tmp_path):
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    body = _valid_duckdb_bytes(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        # Lies about the length so the post-stream check fails.
        return httpx.Response(
            200, headers={"Content-Length": str(len(body) + 5)}, content=body
        )

    with pytest.raises(cache.CorruptDownloadError):
        cache.stage_download("7203", cache_dir=cache_dir, client=_client(handler))

    assert not cache.staged_part_path(cache_dir, "7203").exists()


def test_a_file_that_is_not_a_valid_duckdb_file_raises_corrupt(tmp_path):
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    garbage = b"not a duckdb file at all"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"Content-Length": str(len(garbage))}, content=garbage
        )

    with pytest.raises(cache.CorruptDownloadError):
        cache.stage_download("7203", cache_dir=cache_dir, client=_client(handler))

    assert not cache.staged_part_path(cache_dir, "7203").exists()


def test_disk_full_mid_stream_raises_disk_full_and_removes_part_file(
    tmp_path, monkeypatch
):
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    monkeypatch.setattr(cache, "DOWNLOAD_CHUNK_BYTES", 5)
    body = b"y" * 20

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"Content-Length": str(len(body))}, content=body
        )

    part_path = cache.staged_part_path(cache_dir, "7203")
    real_open = Path.open
    write_calls = {"n": 0}

    class FlakyFile:
        def __init__(self, fh):
            self._fh = fh

        def write(self, data):
            write_calls["n"] += 1
            if write_calls["n"] == 2:
                raise OSError(errno.ENOSPC, "No space left on device")
            return self._fh.write(data)

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            self._fh.close()
            return False

    def fake_open(self, mode="r", *args, **kwargs):
        if self == part_path and mode == "wb":
            return FlakyFile(real_open(self, mode, *args, **kwargs))
        return real_open(self, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fake_open)

    with pytest.raises(cache.DiskFullError):
        cache.stage_download("7203", cache_dir=cache_dir, client=_client(handler))

    assert not part_path.exists()


# ------------------------------------------------------------------ startup


def test_orphaned_part_files_are_deleted_not_resumed(tmp_path):
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    cache.staged_part_path(cache_dir, "7203").write_bytes(b"leftover")
    cache.staged_part_path(cache_dir, "1301").write_bytes(b"leftover")
    live = cache.live_file_path(cache_dir, "9984")
    live.write_bytes(b"still here")

    removed = cache.discard_orphaned_part_files(cache_dir)

    assert sorted(removed) == ["1301", "7203"]
    assert not cache.staged_part_path(cache_dir, "7203").exists()
    assert not cache.staged_part_path(cache_dir, "1301").exists()
    assert live.exists()  # live files are untouched by this cleanup
