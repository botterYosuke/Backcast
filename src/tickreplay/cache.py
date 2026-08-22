"""Download staging for the DuckDB server-cache (Step 4 of the plan).

This module's only job: given a stem, produce a verified, complete file at a
staging path (``<stem>.duckdb.part``), or raise. It never touches
``TickRepository``'s connection pool or ``_info_cache`` — swapping a staged
file into place is ``cache_commit.py``'s job (Step 5), while the process-wide
destination lock in ``repository.py`` remains held.

No resumable downloads in v1: every download always starts at offset 0. A
``.part`` file left behind by a crash or a prior run is always discarded,
never resumed — see ``discard_orphaned_part_files`` and the plan's Scope
section for why resume was rejected.
"""

from __future__ import annotations

import errno
import hashlib
import json
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path

import duckdb
import httpx

STEM_PATH_TEMPLATE = "jp/stocks_trades/{stem}.duckdb"

# Local cache files live under this subdirectory of `cache_dir`, mirroring
# the server's own `jp/stocks_trades/` layout. This lets `BACKCAST_DUCKDB_
# CACHE_DIR` point straight at an existing `jp`-style data root (e.g.
# `S:\jp`) and have already-present `stocks_trades/<stem>.duckdb` files
# recognized as cached, instead of every stem looking missing and being
# redownloaded.
STOCKS_TRADES_DIRNAME = "stocks_trades"

MAX_DOWNLOAD_ATTEMPTS = 3
RETRY_BACKOFF_BASE_SECONDS = 0.5
DOWNLOAD_CHUNK_BYTES = 1024 * 1024  # 1 MiB — streamed, never buffered whole.


class DownloadError(RuntimeError):
    """Base class for every staging failure `stage_download` can raise."""


class ServerUnreachableError(DownloadError):
    """Network failure or a non-2xx/3xx/404 response, after retries."""


class StemNotOnServerError(DownloadError):
    """The server authoritatively returned 404 for this stem's file.

    Distinct from `ServerUnreachableError` on purpose: the existence-vs-
    availability policy (Step 1 of the plan) needs to tell "the server was
    asked and said no" apart from "the server could not be asked at all".
    """


class DiskFullError(DownloadError):
    """Staging failed because local disk space ran out mid-stream.

    Kept distinct from `ServerUnreachableError` so callers never conflate a
    local disk problem with the offline-fallback policy meant for network
    failures (Step 1 of the plan).
    """


class CorruptDownloadError(DownloadError):
    """The staged file failed a length, DuckDB-open, or hash check."""


class _RetryableTransportError(DownloadError):
    """Internal signal only: a network-level failure eligible for retry."""


# --------------------------------------------------------------------------
# Cache-directory layout (shared with cache_commit.py)
# --------------------------------------------------------------------------


def stocks_trades_dir(cache_dir: Path) -> Path:
    """The `stocks_trades` subdirectory of `cache_dir`, created on demand.

    Created here (not only in `config.resolve_cache_config`) because this is
    storage the app owns and manages, exactly like `cache_dir` itself — every
    caller below can write straight to the returned paths without a separate
    mkdir.
    """
    directory = cache_dir / STOCKS_TRADES_DIRNAME
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def live_file_path(cache_dir: Path, stem: str) -> Path:
    return stocks_trades_dir(cache_dir) / f"{stem}.duckdb"


def live_sidecar_path(cache_dir: Path, stem: str) -> Path:
    return stocks_trades_dir(cache_dir) / f"{stem}.duckdb.sidecar.json"


def staged_part_path(cache_dir: Path, stem: str) -> Path:
    return stocks_trades_dir(cache_dir) / f"{stem}.duckdb.part"


def staged_sidecar_tmp_path(cache_dir: Path, stem: str) -> Path:
    return stocks_trades_dir(cache_dir) / f"{stem}.duckdb.sidecar.json.tmp"


@dataclass(frozen=True)
class Sidecar:
    """Everything the commit coordinator (Step 5) records about a live file."""

    etag: str | None
    last_modified: str | None
    sha256: str
    generation: int

    def to_json_bytes(self) -> bytes:
        return json.dumps(
            {
                "etag": self.etag,
                "lastModified": self.last_modified,
                "sha256": self.sha256,
                "generation": self.generation,
            }
        ).encode("utf-8")

    @classmethod
    def from_json_bytes(cls, data: bytes) -> Sidecar:
        obj = json.loads(data)
        return cls(
            etag=obj.get("etag"),
            last_modified=obj.get("lastModified"),
            sha256=obj["sha256"],
            generation=int(obj["generation"]),
        )


def read_sidecar(path: Path) -> Sidecar | None:
    """Read a sidecar file; a missing or corrupt one is treated as absent.

    A corrupt sidecar must never raise here — the caller (conditional-GET
    header construction, or Step 5's reconciliation) treats "absent" and
    "corrupt" identically: fall back to an unconditional, full redownload.
    """
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return None
    try:
        return Sidecar.from_json_bytes(data)
    except (ValueError, KeyError, json.JSONDecodeError):
        return None


# --------------------------------------------------------------------------
# Startup/lifespan cleanup
# --------------------------------------------------------------------------


def discard_orphaned_part_files(cache_dir: Path) -> list[str]:
    """Delete every leftover ``*.duckdb.part`` file, unconditionally.

    There is nothing to resume from (no resumable downloads in v1), so any
    ``.part`` surviving from a prior run or crash is always stale. Returns
    the stems that had an orphan removed, for startup logging.
    """
    removed: list[str] = []
    for part_path in sorted(stocks_trades_dir(cache_dir).glob("*.duckdb.part")):
        stem = part_path.name[: -len(".duckdb.part")]
        try:
            part_path.unlink()
        except FileNotFoundError:
            continue
        removed.append(stem)
    return removed


# --------------------------------------------------------------------------
# Staging a download
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class StagingResult:
    """Outcome of `stage_download`."""

    not_modified: bool
    part_path: Path | None
    sha256: str | None
    content_length: int | None
    etag: str | None
    last_modified: str | None


@dataclass
class DownloadObservation:
    """Result or failure observed by the current download caller.

    The repository deliberately degrades to an existing local file when a
    conditional refresh fails. That makes the exception invisible to its
    caller, so the server observes this narrow staging boundary to label the
    completed operation ``stale-served`` instead of ``fresh``.
    """

    result: StagingResult | None = None
    error: DownloadError | None = None


_download_observation: ContextVar[DownloadObservation | None] = ContextVar(
    "download_observation", default=None
)


@contextmanager
def observe_download() -> Iterator[DownloadObservation]:
    """Capture the download outcome for this execution context only."""
    observation = DownloadObservation()
    token = _download_observation.set(observation)
    try:
        yield observation
    finally:
        _download_observation.reset(token)


ProgressCallback = Callable[[int, "int | None"], None]


def _stage_download(
    stem: str,
    *,
    cache_dir: Path,
    client: httpx.Client,
    conditional: bool = False,
    progress_cb: ProgressCallback | None = None,
) -> StagingResult:
    """Download ``stem``'s file to a verified ``.part`` staging file.

    Always starts at offset 0 (no resume). When ``conditional`` is True and a
    live sidecar exists, sends ``If-None-Match``/``If-Modified-Since`` from
    it; a ``304`` response short-circuits with ``not_modified=True`` and no
    ``.part`` file is written or touched. ``conditional=False`` always
    performs a full unconditional redownload — used both for a stem's very
    first download and for the exactly-one forced redownload a caller (Step
    6) issues after detecting that the current live file fails a DuckDB-open
    check.

    Raises a `DownloadError` subclass on any failure; the `.part` file is
    removed on every failure path, never left half-written.
    """
    url_path = STEM_PATH_TEMPLATE.format(stem=stem)
    headers = _conditional_headers(cache_dir, stem) if conditional else {}
    part_path = staged_part_path(cache_dir, stem)

    last_error: Exception | None = None
    for attempt in range(1, MAX_DOWNLOAD_ATTEMPTS + 1):
        try:
            return _attempt_download(url_path, part_path, client, headers, progress_cb)
        except _RetryableTransportError as error:
            last_error = error
            if attempt >= MAX_DOWNLOAD_ATTEMPTS:
                break
            time.sleep(RETRY_BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)))

    raise ServerUnreachableError(
        f"{stem}: unreachable after {MAX_DOWNLOAD_ATTEMPTS} attempts: {last_error}"
    ) from last_error


def _conditional_headers(cache_dir: Path, stem: str) -> dict[str, str]:
    sidecar = read_sidecar(live_sidecar_path(cache_dir, stem))
    if sidecar is None:
        return {}
    headers: dict[str, str] = {}
    if sidecar.etag:
        headers["If-None-Match"] = sidecar.etag
    elif sidecar.last_modified:
        headers["If-Modified-Since"] = sidecar.last_modified
    return headers


def _attempt_download(
    url_path: str,
    part_path: Path,
    client: httpx.Client,
    headers: dict[str, str],
    progress_cb: ProgressCallback | None,
) -> StagingResult:
    try:
        with client.stream("GET", "/" + url_path, headers=headers) as response:
            return _consume_response(response, part_path, url_path, progress_cb)
    except httpx.TimeoutException as error:
        _discard(part_path)
        raise _RetryableTransportError(f"{url_path}: timeout: {error}") from error
    except httpx.TransportError as error:
        _discard(part_path)
        raise _RetryableTransportError(
            f"{url_path}: transport error: {error}"
        ) from error


def _consume_response(
    response: httpx.Response,
    part_path: Path,
    url_path: str,
    progress_cb: ProgressCallback | None,
) -> StagingResult:
    if response.status_code == 304:
        return StagingResult(
            not_modified=True,
            part_path=None,
            sha256=None,
            content_length=None,
            etag=response.headers.get("ETag"),
            last_modified=response.headers.get("Last-Modified"),
        )
    if response.status_code == 404:
        raise StemNotOnServerError(f"{url_path}: server has no file for this stem")
    if response.status_code >= 500:
        raise _RetryableTransportError(
            f"{url_path}: server returned {response.status_code}"
        )
    if response.status_code != 200:
        raise ServerUnreachableError(
            f"{url_path}: unexpected status {response.status_code}"
        )

    expected_length = response.headers.get("Content-Length")
    expected_length_int = int(expected_length) if expected_length else None
    etag = response.headers.get("ETag")
    last_modified = response.headers.get("Last-Modified")

    sha256, bytes_written = _stream_to_part_file(
        response, part_path, expected_length_int, progress_cb
    )

    if expected_length_int is not None and bytes_written != expected_length_int:
        _discard(part_path)
        raise CorruptDownloadError(
            f"{url_path}: expected {expected_length_int} bytes, received {bytes_written}"
        )
    _verify_duckdb_opens(part_path, url_path)

    return StagingResult(
        not_modified=False,
        part_path=part_path,
        sha256=sha256,
        content_length=bytes_written,
        etag=etag,
        last_modified=last_modified,
    )


def _stream_to_part_file(
    response: httpx.Response,
    part_path: Path,
    expected_length_int: int | None,
    progress_cb: ProgressCallback | None,
) -> tuple[str, int]:
    digest = hashlib.sha256()
    bytes_written = 0
    try:
        with part_path.open("wb") as fh:
            for chunk in response.iter_bytes(DOWNLOAD_CHUNK_BYTES):
                fh.write(chunk)
                digest.update(chunk)
                bytes_written += len(chunk)
                if progress_cb is not None:
                    progress_cb(bytes_written, expected_length_int)
    except OSError as error:
        _discard(part_path)
        if error.errno == errno.ENOSPC:
            raise DiskFullError(f"disk full while staging: {error}") from error
        raise DownloadError(f"local write failed while staging: {error}") from error
    return digest.hexdigest(), bytes_written


def _verify_duckdb_opens(part_path: Path, url_path: str) -> None:
    try:
        with duckdb.connect(str(part_path), read_only=True) as con:
            con.execute("SELECT 1")
    except duckdb.Error as error:
        _discard(part_path)
        raise CorruptDownloadError(
            f"{url_path}: staged file failed to open as DuckDB: {error}"
        ) from error


def _discard(part_path: Path) -> None:
    try:
        part_path.unlink()
    except FileNotFoundError:
        pass


def stage_download(
    stem: str,
    *,
    cache_dir: Path,
    client: httpx.Client,
    conditional: bool = False,
    progress_cb: ProgressCallback | None = None,
) -> StagingResult:
    """Stage one verified download for one destination-owning caller.

    A successful result owns a move-only ``part_path`` which must not be
    shared. Production callers therefore serialize the whole stage/commit/use
    lifecycle by resolved cache destination; ``TickRepository.symbol_lock``
    provides that boundary. This function remains a single-caller staging
    primitive and never coalesces results itself.

    Any active ``observe_download`` scope records the caller's outcome in its
    own execution context.
    """

    try:
        result = _stage_download(
            stem,
            cache_dir=cache_dir,
            client=client,
            conditional=conditional,
            progress_cb=progress_cb,
        )
    except DownloadError as error:
        observation = _download_observation.get()
        if observation is not None:
            observation.error = error
        raise

    observation = _download_observation.get()
    if observation is not None:
        observation.result = result
    return result
