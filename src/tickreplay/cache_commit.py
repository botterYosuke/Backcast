"""Atomic commit coordinator for the DuckDB server-cache (Step 5 of the plan).

`CommitCoordinator.refresh` owns the entire "a verified staged download
(`cache.StagingResult`) becomes the live file for a stem" sequence, under one
per-symbol lock (`TickRepository.symbol_lock`) held start to finish. That lock
is the *only* gate to the connection pool, the info cache, and the generation
counter for that stem — Step 4's blocking network work happens before it is
ever acquired.

Crash-safe commit ordering, all under the lock:

1. write the new sidecar to ``sidecar.tmp``
2. evict the stem's pooled connection
3. ``os.replace`` the staged file onto the live file
4. ``os.replace`` ``sidecar.tmp`` onto the live sidecar
5. publish the new generation (bumps the in-memory counter, drops the cached
   `SymbolInfo`)
6. release the lock

A crash between steps 1-2 leaves the live file/sidecar untouched (only an
orphan ``sidecar.tmp`` to clean up). A crash between steps 3-4 leaves the live
*file* already new but the *sidecar* still describing the old one.
`reconcile_stem` detects exactly which of those states the on-disk evidence
represents and either finishes the commit or discards the orphan — it makes
no other assumption, so it is correct whether it runs at startup (Step 7's
lifespan hook, via `reconcile_all_at_startup`) or in-process, from inside
`refresh` itself, after a non-crash exception between steps 3 and 5 (the
process keeps running, so startup reconciliation would never otherwise fire
for it). `reconcile_stem` always assumes its caller already holds the
stem's symbol lock; it never acquires it, so both call sites can share it
without a reentrant-lock hazard.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from . import cache

if TYPE_CHECKING:
    # repository.py imports this module (it owns the CommitCoordinator), so
    # this import must stay TYPE_CHECKING-only to avoid a cycle; nothing
    # here needs the class itself at runtime, only duck-typed method calls.
    from .repository import TickRepository

_HASH_CHUNK_BYTES = 1024 * 1024


class CommitFailedError(RuntimeError):
    """`refresh` could not complete, even after reconciliation.

    The stem's on-disk/in-memory state is always left consistent (or the
    stem is marked unavailable via `TickRepository.mark_unavailable`) before
    this is raised — see the module docstring.
    """


@dataclass(frozen=True)
class ReconcileOutcome:
    stem: str
    action: str  # "clean" | "completed" | "failed"
    detail: str = ""


def _sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(_HASH_CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def _unlink_ignore_missing(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def reconcile_stem(
    cache_dir: Path, stem: str, repository: TickRepository
) -> ReconcileOutcome:
    """Bring one stem's on-disk/in-memory state back to a consistent point.

    Caller must already hold ``repository.symbol_lock(stem)``. Covers two
    distinct gaps, both driven purely by on-disk evidence (so it behaves
    identically whether called at startup or in-process):

    - ``sidecar.tmp`` still exists: steps 1-4 or crash recovery. If the live
      file's hash already matches what ``sidecar.tmp`` describes, step 3
      completed but step 4 did not — finish it. Otherwise step 3 never ran —
      discard the orphan; the live file is untouched and still correct.
    - ``sidecar.tmp`` is already gone but the live sidecar's recorded
      generation is ahead of the repository's in-memory one: steps 3-4
      completed but step 5 (the in-memory publish) did not — republish it.
    """
    tmp_path = cache.staged_sidecar_tmp_path(cache_dir, stem)
    live_sidecar_path = cache.live_sidecar_path(cache_dir, stem)

    try:
        tmp_bytes = tmp_path.read_bytes()
    except FileNotFoundError:
        return _reconcile_without_tmp(stem, live_sidecar_path, repository)

    try:
        staged_sidecar = cache.Sidecar.from_json_bytes(tmp_bytes)
    except (ValueError, KeyError, json.JSONDecodeError) as error:
        # sidecar.tmp itself is incomplete/corrupt: a crash during step 1's
        # own write. The live file/sidecar were never touched.
        _unlink_ignore_missing(tmp_path)
        return ReconcileOutcome(
            stem, "clean", f"discarded corrupt sidecar.tmp: {error}"
        )

    live_file = cache.live_file_path(cache_dir, stem)
    live_hash = _live_file_hash_or_none(live_file)
    if live_hash != staged_sidecar.sha256:
        # Step 3 (the file replace) never happened for this attempt.
        _unlink_ignore_missing(tmp_path)
        return ReconcileOutcome(
            stem, "clean", "live file predates this commit; discarded sidecar.tmp"
        )

    # Step 3 completed but step 4 (the sidecar replace) did not: finish it.
    try:
        os.replace(tmp_path, live_sidecar_path)
    except OSError as error:
        return ReconcileOutcome(
            stem, "failed", f"could not finish sidecar replace: {error}"
        )
    repository.publish_generation(stem, staged_sidecar.generation)
    return ReconcileOutcome(stem, "completed", "finished the interrupted commit")


def _reconcile_without_tmp(
    stem: str, live_sidecar_path: Path, repository: TickRepository
) -> ReconcileOutcome:
    sidecar = cache.read_sidecar(live_sidecar_path)
    if sidecar is None or sidecar.generation <= repository.generation(stem):
        return ReconcileOutcome(stem, "clean", "nothing to reconcile")
    # Steps 3-4 completed (the live sidecar already reflects the new
    # generation) but step 5, the in-memory publish, did not.
    repository.publish_generation(stem, sidecar.generation)
    return ReconcileOutcome(
        stem, "completed", "republished a generation step 5 had missed"
    )


def _live_file_hash_or_none(live_file: Path) -> str | None:
    if not live_file.is_file():
        return None
    try:
        return _sha256_of(live_file)
    except OSError:
        return None


def _reconcile_or_mark_unavailable(
    cache_dir: Path, stem: str, repository: TickRepository
) -> ReconcileOutcome:
    """Reconcile under the caller-held symbol lock, failing closed on errors."""
    try:
        outcome = reconcile_stem(cache_dir, stem, repository)
    except Exception as error:  # noqa: BLE001 - converted to fail-closed state
        outcome = ReconcileOutcome(
            stem,
            "failed",
            f"reconciliation raised {type(error).__name__}: {error}",
        )
    if outcome.action == "failed":
        repository.mark_unavailable(stem, outcome.detail)
    return outcome


def reconcile_all_at_startup(cache_dir: Path, repository: TickRepository) -> list[str]:
    """Reconcile every stem with a leftover ``sidecar.tmp``.

    Wired via Step 7's lifespan hook; must run once, before any request is
    served. Each stem's lock is still acquired for uniformity with the
    in-process path, even though nothing else can be running yet at this
    point. Returns the stems visited.
    """
    stems = sorted(
        path.name[: -len(".duckdb.sidecar.json.tmp")]
        for path in cache.stocks_trades_dir(cache_dir).glob("*.duckdb.sidecar.json.tmp")
    )
    for stem in stems:
        with repository.symbol_lock(stem):
            _reconcile_or_mark_unavailable(cache_dir, stem, repository)
    return stems


StepHook = Callable[[int], None]


class CommitCoordinator:
    """Owns the "staged download becomes the live file" sequence for one
    cache directory + repository pair."""

    def __init__(self, *, cache_dir: Path, repository: TickRepository) -> None:
        self._cache_dir = cache_dir
        self._repository = repository

    def refresh(
        self,
        stem: str,
        staging: cache.StagingResult,
        *,
        on_step: StepHook | None = None,
    ) -> int:
        """Commit an already-staged, verified download as the new live file.

        Acquires the stem's symbol lock itself for the whole sequence —
        callers must not pre-acquire it (use `refresh_locked` instead if the
        caller already holds it, e.g. a read that is about to force a
        redownload under its own lock acquisition — this lock is a plain
        `threading.Lock`, not reentrant). See `refresh_locked` for the full
        contract and the crash-safe step sequence.
        """
        with self._repository.symbol_lock(stem):
            return self.refresh_locked(stem, staging, on_step=on_step)

    def refresh_locked(
        self,
        stem: str,
        staging: cache.StagingResult,
        *,
        on_step: StepHook | None = None,
    ) -> int:
        """Commit an already-staged, verified download as the new live file.

        Caller must already hold ``repository.symbol_lock(stem)`` — this
        method never acquires it itself, so callers that already hold the
        lock (e.g. `TickRepository`'s own read paths, Step 6) can call this
        directly without a reentrant-lock hazard; `refresh` is the
        lock-acquiring convenience wrapper for everyone else.

        ``staging`` must come from `cache.stage_download` with
        ``not_modified=False`` (a real download, not a 304 short-circuit).
        Returns the new generation number.

        Raises `CommitFailedError` if the commit could not complete even
        after reconciliation; the stem is marked unavailable
        (`TickRepository.mark_unavailable`) in that case rather than left in
        an unproven state.
        """
        if staging.not_modified or staging.part_path is None or staging.sha256 is None:
            raise ValueError(
                "refresh_locked() requires a completed download (not_modified=False)"
            )

        notify = on_step or (lambda step: None)
        new_generation = self._repository.generation(stem) + 1
        sidecar = cache.Sidecar(
            etag=staging.etag,
            last_modified=staging.last_modified,
            sha256=staging.sha256,
            generation=new_generation,
        )
        tmp_sidecar_path = cache.staged_sidecar_tmp_path(self._cache_dir, stem)
        live_file = cache.live_file_path(self._cache_dir, stem)
        live_sidecar_path = cache.live_sidecar_path(self._cache_dir, stem)

        try:
            tmp_sidecar_path.write_bytes(sidecar.to_json_bytes())
            notify(1)

            self._repository.evict(stem)
            notify(2)

            os.replace(staging.part_path, live_file)
            notify(3)

            os.replace(tmp_sidecar_path, live_sidecar_path)
            notify(4)

            self._repository.publish_generation(stem, new_generation)
            notify(5)
        except Exception as error:
            outcome = _reconcile_or_mark_unavailable(
                self._cache_dir, stem, self._repository
            )
            raise CommitFailedError(
                f"{stem}: commit failed ({error!r}); reconciliation={outcome.action} "
                f"({outcome.detail})"
            ) from error
        notify(6)

        return new_generation
