"""Tests for `tickreplay.cache_commit` — the atomic commit coordinator
(Step 5 of the plan).
"""

from __future__ import annotations

import hashlib
import threading
from pathlib import Path

import duckdb
import httpx
import pytest

from tickreplay import cache, cache_commit
from tickreplay.repository import TickRepository


def _unused_http_client() -> httpx.Client:
    """These tests exercise the commit/reconciliation path only, which
    never makes an HTTP call — a client whose transport fails loudly if it
    is ever actually invoked."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(
            f"unexpected HTTP call in a commit-only test: {request.url}"
        )

    return httpx.Client(
        base_url="http://cache-server.invalid", transport=httpx.MockTransport(handler)
    )


def _repository(cache_dir: Path) -> TickRepository:
    return TickRepository(
        cache_dir=cache_dir,
        server_url="http://cache-server.invalid",
        http_client=_unused_http_client(),
    )


def _valid_duckdb_bytes(tmp_path: Path, name: str, value: int) -> bytes:
    path = tmp_path / f"_seed_{name}.duckdb"
    connection = duckdb.connect(str(path))
    connection.execute("CREATE TABLE t (x INTEGER)")
    connection.execute("INSERT INTO t VALUES (?)", [value])
    connection.close()
    return path.read_bytes()


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _staging(
    part_path: Path, body: bytes, *, etag: str | None = '"e1"'
) -> cache.StagingResult:
    part_path.write_bytes(body)
    return cache.StagingResult(
        not_modified=False,
        part_path=part_path,
        sha256=_sha256(body),
        content_length=len(body),
        etag=etag,
        last_modified="Fri, 21 Aug 2026 00:00:00 GMT",
    )


# ------------------------------------------------------------------- happy


def test_refresh_commits_a_first_download(tmp_path):
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    stem = "7203"
    body = _valid_duckdb_bytes(tmp_path, "a", 1)
    staging = _staging(cache.staged_part_path(cache_dir, stem), body)

    repository = _repository(cache_dir)
    coordinator = cache_commit.CommitCoordinator(
        cache_dir=cache_dir, repository=repository
    )

    generation = coordinator.refresh(stem, staging)

    assert generation == 1
    assert repository.generation(stem) == 1
    assert cache.live_file_path(cache_dir, stem).read_bytes() == body
    sidecar = cache.read_sidecar(cache.live_sidecar_path(cache_dir, stem))
    assert sidecar is not None
    assert sidecar.sha256 == staging.sha256
    assert sidecar.generation == 1
    assert not cache.staged_part_path(cache_dir, stem).exists()
    assert not cache.staged_sidecar_tmp_path(cache_dir, stem).exists()


def test_refresh_rejects_a_not_modified_staging_result(tmp_path):
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    repository = _repository(cache_dir)
    coordinator = cache_commit.CommitCoordinator(
        cache_dir=cache_dir, repository=repository
    )
    staging = cache.StagingResult(
        not_modified=True,
        part_path=None,
        sha256=None,
        content_length=None,
        etag=None,
        last_modified=None,
    )

    with pytest.raises(ValueError):
        coordinator.refresh("7203", staging)


# ------------------------------------------------------ old-file-until-swap


def test_old_file_stays_readable_and_a_concurrent_reader_blocks_on_the_same_lock(
    tmp_path,
):
    """Combines two Step 5 Verification bullets: (a) the barrier test that
    pauses immediately after step 2 (evicted, not yet replaced) and asserts
    a concurrent caller blocks on the same symbol lock rather than being
    able to reopen the old file's path — proving the stale-connection-reopen
    race (Codex round 3) cannot occur; and (b) that the old file is fully
    intact/readable right up to the moment of the replace in step 3.
    """
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    stem = "7203"
    old_body = _valid_duckdb_bytes(tmp_path, "old", 1)
    new_body = _valid_duckdb_bytes(tmp_path, "new", 2)
    cache.live_file_path(cache_dir, stem).write_bytes(old_body)
    staging = _staging(cache.staged_part_path(cache_dir, stem), new_body)

    repository = _repository(cache_dir)
    coordinator = cache_commit.CommitCoordinator(
        cache_dir=cache_dir, repository=repository
    )

    reached_step_2 = threading.Event()
    release = threading.Event()

    def on_step(step: int) -> None:
        if step == 2:
            reached_step_2.set()
            release.wait(timeout=5)

    thread = threading.Thread(
        target=coordinator.refresh, args=(stem, staging), kwargs={"on_step": on_step}
    )
    thread.start()
    try:
        assert reached_step_2.wait(timeout=5), "coordinator never reached step 2"

        # The lock is held for the whole sequence; a concurrent caller must
        # block on it rather than being able to run a pool query that would
        # reopen a connection to the (about-to-be-replaced) old file.
        lock = repository.symbol_lock(stem)
        acquired = lock.acquire(blocking=False)
        try:
            assert not acquired
        finally:
            if acquired:
                lock.release()

        # Not yet replaced: the old file is still fully intact.
        assert cache.live_file_path(cache_dir, stem).read_bytes() == old_body
    finally:
        release.set()
        thread.join(timeout=5)

    assert cache.live_file_path(cache_dir, stem).read_bytes() == new_body


# ------------------------------------------------- in-process (non-crash)


def test_in_process_failure_right_after_file_replace_is_reconciled_in_process(tmp_path):
    """Exception-injection at the step 3->4 boundary, without killing the
    process: sidecar.tmp still exists, so reconciliation finds the live
    file already matches it and finishes steps 4-5 itself."""
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    stem = "7203"
    body = _valid_duckdb_bytes(tmp_path, "a", 1)
    staging = _staging(cache.staged_part_path(cache_dir, stem), body)

    repository = _repository(cache_dir)
    coordinator = cache_commit.CommitCoordinator(
        cache_dir=cache_dir, repository=repository
    )

    def on_step(step: int) -> None:
        if step == 3:
            raise RuntimeError("simulated failure right after the file replace")

    with pytest.raises(cache_commit.CommitFailedError):
        coordinator.refresh(stem, staging, on_step=on_step)

    assert cache.live_file_path(cache_dir, stem).read_bytes() == body
    assert cache.read_sidecar(cache.live_sidecar_path(cache_dir, stem)) is not None
    assert repository.generation(stem) == 1
    assert repository.is_unavailable(stem) is None
    assert not cache.staged_sidecar_tmp_path(cache_dir, stem).exists()


def test_in_process_failure_right_after_sidecar_replace_republishes_the_generation(
    tmp_path,
):
    """Exception-injection at the step 4->5 boundary: sidecar.tmp is already
    consumed, so reconciliation must detect the gap from the live sidecar's
    generation outrunning the in-memory one, not from tmp-file presence."""
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    stem = "7203"
    body = _valid_duckdb_bytes(tmp_path, "a", 1)
    staging = _staging(cache.staged_part_path(cache_dir, stem), body)

    repository = _repository(cache_dir)
    coordinator = cache_commit.CommitCoordinator(
        cache_dir=cache_dir, repository=repository
    )

    def on_step(step: int) -> None:
        if step == 4:
            raise RuntimeError("simulated failure right after the sidecar replace")

    with pytest.raises(cache_commit.CommitFailedError):
        coordinator.refresh(stem, staging, on_step=on_step)

    assert repository.generation(stem) == 1
    assert repository.is_unavailable(stem) is None


def test_reconciliation_that_cannot_complete_marks_the_stem_unavailable(
    tmp_path, monkeypatch
):
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    stem = "7203"
    body = _valid_duckdb_bytes(tmp_path, "a", 1)
    staging = _staging(cache.staged_part_path(cache_dir, stem), body)

    repository = _repository(cache_dir)
    coordinator = cache_commit.CommitCoordinator(
        cache_dir=cache_dir, repository=repository
    )

    live_sidecar = cache.live_sidecar_path(cache_dir, stem)
    real_replace = cache_commit.os.replace

    def flaky_replace(src, dst, *args, **kwargs):
        if Path(dst) == live_sidecar:
            raise OSError("simulated: the sidecar replace can never succeed")
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(cache_commit.os, "replace", flaky_replace)

    with pytest.raises(cache_commit.CommitFailedError):
        coordinator.refresh(stem, staging)

    # The file replace (step 3) itself succeeded; only the sidecar replace,
    # both in the main sequence and in reconciliation's own retry, is broken.
    assert cache.live_file_path(cache_dir, stem).read_bytes() == body
    assert repository.is_unavailable(stem) is not None


def test_reconciliation_exception_still_marks_unavailable_under_the_symbol_lock(
    tmp_path, monkeypatch
):
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    stem = "7203"
    body = _valid_duckdb_bytes(tmp_path, "a", 1)
    staging = _staging(cache.staged_part_path(cache_dir, stem), body)
    repository = _repository(cache_dir)
    coordinator = cache_commit.CommitCoordinator(
        cache_dir=cache_dir, repository=repository
    )
    lock_was_held = {"value": False}
    real_mark_unavailable = repository.mark_unavailable

    def broken_publish(target_stem: str, generation: int) -> None:
        raise RuntimeError(f"publish failed for {target_stem} generation {generation}")

    def checked_mark_unavailable(target_stem: str, reason: str) -> None:
        symbol_lock = repository.symbol_lock(target_stem)
        acquired = symbol_lock.acquire(blocking=False)
        lock_was_held["value"] = not acquired
        if acquired:
            symbol_lock.release()
        real_mark_unavailable(target_stem, reason)

    monkeypatch.setattr(repository, "publish_generation", broken_publish)
    monkeypatch.setattr(repository, "mark_unavailable", checked_mark_unavailable)

    with pytest.raises(cache_commit.CommitFailedError, match="reconciliation=failed"):
        coordinator.refresh(stem, staging)

    assert lock_was_held["value"] is True
    assert cache.live_file_path(cache_dir, stem).read_bytes() == body
    assert cache.read_sidecar(cache.live_sidecar_path(cache_dir, stem)) is not None
    assert repository.is_unavailable(stem) is not None


# --------------------------------------------------------------- windows


def test_windows_replace_succeeds_against_an_open_readonly_duckdb_handle(tmp_path):
    """The genuinely unverified-until-now behavior the plan flags in Risks:
    does `os.replace` succeed on Windows against a file with an open
    read-only DuckDB connection? Actually exercised, not mocked."""
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    stem = "7203"
    old_body = _valid_duckdb_bytes(tmp_path, "old", 1)
    new_body = _valid_duckdb_bytes(tmp_path, "new", 2)
    live_file = cache.live_file_path(cache_dir, stem)
    live_file.write_bytes(old_body)

    repository = _repository(cache_dir)
    # Populate the pool with a live, open, read-only DuckDB connection to
    # the current live file — exactly what a prior symbol_info()/session
    # read would leave behind.
    repository._pool.query(live_file, "SELECT 1")

    coordinator = cache_commit.CommitCoordinator(
        cache_dir=cache_dir, repository=repository
    )
    staging = _staging(cache.staged_part_path(cache_dir, stem), new_body)

    coordinator.refresh(stem, staging)

    assert live_file.read_bytes() == new_body


# ------------------------------------------------------------- crash+restart


def test_startup_reconciliation_finishes_a_crash_between_file_and_sidecar_replace(
    tmp_path,
):
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    stem = "7203"
    new_body = _valid_duckdb_bytes(tmp_path, "new", 2)
    # Simulate: step 3 (file replace) completed, then the process died
    # before step 4 (sidecar.tmp -> live sidecar).
    cache.live_file_path(cache_dir, stem).write_bytes(new_body)
    sidecar = cache.Sidecar(
        etag='"new"', last_modified=None, sha256=_sha256(new_body), generation=1
    )
    cache.staged_sidecar_tmp_path(cache_dir, stem).write_bytes(sidecar.to_json_bytes())

    fresh_repository = _repository(cache_dir)  # a new process: generation starts at 0
    reconciled = cache_commit.reconcile_all_at_startup(cache_dir, fresh_repository)

    assert reconciled == [stem]
    assert not cache.staged_sidecar_tmp_path(cache_dir, stem).exists()
    assert cache.read_sidecar(cache.live_sidecar_path(cache_dir, stem)) == sidecar
    assert fresh_repository.generation(stem) == 1


def test_startup_reconciliation_discards_an_orphan_tmp_when_replace_never_ran(tmp_path):
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    stem = "7203"
    old_body = _valid_duckdb_bytes(tmp_path, "old", 1)
    cache.live_file_path(cache_dir, stem).write_bytes(old_body)  # untouched old file
    # sidecar.tmp describes bytes that were never actually written to the
    # live file: the process died before step 3 ever ran.
    sidecar = cache.Sidecar(
        etag='"new"', last_modified=None, sha256="0" * 64, generation=1
    )
    cache.staged_sidecar_tmp_path(cache_dir, stem).write_bytes(sidecar.to_json_bytes())

    fresh_repository = _repository(cache_dir)
    reconciled = cache_commit.reconcile_all_at_startup(cache_dir, fresh_repository)

    assert reconciled == [stem]
    assert not cache.staged_sidecar_tmp_path(cache_dir, stem).exists()
    assert not cache.live_sidecar_path(cache_dir, stem).exists()
    assert cache.live_file_path(cache_dir, stem).read_bytes() == old_body
    assert fresh_repository.generation(stem) == 0


def test_startup_reconciliation_discards_a_corrupt_sidecar_tmp(tmp_path):
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    stem = "7203"
    cache.staged_sidecar_tmp_path(cache_dir, stem).write_bytes(b"{not json")

    fresh_repository = _repository(cache_dir)
    reconciled = cache_commit.reconcile_all_at_startup(cache_dir, fresh_repository)

    assert reconciled == [stem]
    assert not cache.staged_sidecar_tmp_path(cache_dir, stem).exists()
    assert fresh_repository.generation(stem) == 0


def test_startup_reconciliation_is_a_no_op_when_nothing_was_interrupted(tmp_path):
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()

    fresh_repository = _repository(cache_dir)
    assert cache_commit.reconcile_all_at_startup(cache_dir, fresh_repository) == []
