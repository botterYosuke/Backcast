"""Read-only access to the per-symbol tick DuckDB files, server-cache-backed.

Layout (verified against the live data set):

* ``<cache_dir>/stocks_trades/<stem>.duckdb`` holds ``stocks_board`` and
  ``stocks_board_metadata``, downloaded on demand from the file server
  (``cache.py``/``cache_commit.py``, Steps 4-5 of
  ``.agents/docs/plans/duckdb-server-cache.md``) and cached locally. The
  ``stocks_trades`` subdirectory mirrors the server's own layout so that
  pointing ``BACKCAST_DUCKDB_CACHE_DIR`` at an existing ``jp``-style data
  root recognizes files already present under ``jp/stocks_trades`` as
  cached, rather than redownloading them.
* ``stocks_board``: ``Price DOUBLE, Qty BIGINT, Type VARCHAR, source VARCHAR,
  Code VARCHAR, Timestamp VARCHAR`` with a primary key on ``(Code, Timestamp)``.
  That primary key is what makes a single-session range scan fast even on the
  4 GB files, so every query filters on ``Code`` and a ``Timestamp`` range.
* A file can contain more than one ``Code`` (e.g. ``3823.duckdb`` carries both
  ``38230`` with 508k rows and a two-row ``3823`` artifact), so the canonical
  code is taken from the metadata row with the largest ``record_count`` rather
  than from the filename.

``Timestamp`` is stored as text without a timezone. It is converted to epoch
microseconds by interpreting it as UTC, which keeps the stored wall clock
intact when the browser renders it (Lightweight Charts formats in UTC). No
claim is made about the true timezone of the source data.

Every generation-dependent operation — a commit (``cache_commit.py``) and
every read below — holds exactly one lock for its whole span:
``symbol_lock(stem)``. Fixed lock order project-wide (Step 6 of the plan):
symbol lock -> connection-pool lock -> info-cache lock; the info-cache lock
no longer exists as a separate lock at all, since the whole
query-then-cache-write span already runs under the symbol lock (this is what
fixes the read-after-invalidate race a plain "invalidate on commit" would
leave open). A stem's blocking download work (Step 4) always happens before
the pool is ever touched.
"""

from __future__ import annotations

import json
import os
import re
import threading
from collections import OrderedDict
from dataclasses import dataclass
from datetime import date as Date
from datetime import datetime, timedelta
from pathlib import Path

import duckdb
import httpx

from . import cache
from .cache_commit import CommitCoordinator

# Symbol files are 4-5 character alphanumeric codes. Anything else in a
# listing is a sync artifact (e.g. "130A_ADMIN_..._Conflict.duckdb"). The
# server's own listing (``GET /api/stocks-trades``) is re-filtered through
# this, regardless of the server's looser pattern — defense in depth, no
# server trust assumed (Step 1 of the plan).
SYMBOL_STEM_RE = re.compile(r"^[0-9A-Z]{4,5}$")

TRADES_TABLE = "stocks_board"
METADATA_TABLE = "stocks_board_metadata"

MAX_OPEN_CONNECTIONS = 8
MAX_SESSION_PROBE_STEPS = 20

LISTING_CACHE_FILENAME = "_listing.json"

_METADATA_SELECT = (
    f"SELECT Code, from_timestamp, to_timestamp, record_count "
    f"FROM {METADATA_TABLE} ORDER BY record_count DESC"
)


class SymbolNotFoundError(LookupError):
    """The stem is authoritatively absent from the live server listing."""


class SymbolAvailabilityUnknownError(LookupError):
    """Existence cannot be confirmed right now.

    The live listing could not be fetched and there is no cached evidence
    the stem exists or does not (Step 1's existence-vs-availability policy).
    Maps to HTTP 503, never 404 — a 404 must never be returned purely
    because of a local/network failure to check.
    """


class SymbolUnavailableError(RuntimeError):
    """A prior commit for this stem failed unrecoverably (Step 5 of the
    plan); reads are refused until the process restarts."""


@dataclass(frozen=True)
class SymbolInfo:
    """Identity and coverage of one tick file."""

    stem: str
    code: str
    first_timestamp: datetime
    last_timestamp: datetime
    record_count: int
    other_codes: tuple[str, ...] = ()

    @property
    def first_date(self) -> str:
        return self.first_timestamp.date().isoformat()

    @property
    def last_date(self) -> str:
        return self.last_timestamp.date().isoformat()

    def as_dict(self) -> dict[str, object]:
        return {
            "stem": self.stem,
            "code": self.code,
            "firstDate": self.first_date,
            "lastDate": self.last_date,
            "recordCount": self.record_count,
            "otherCodes": list(self.other_codes),
        }


@dataclass(frozen=True)
class SessionTicks:
    """One trading session of executions, in column form."""

    stem: str
    code: str
    date: str
    us: list[int]
    price: list[float]
    qty: list[int]
    trade_type: list[str]

    def as_dict(self) -> dict[str, object]:
        return {
            "stem": self.stem,
            "code": self.code,
            "date": self.date,
            "count": len(self.us),
            "us": self.us,
            "price": self.price,
            "qty": self.qty,
            "type": self.trade_type,
        }


class _ConnectionPool:
    """Bounded cache of read-only DuckDB connections.

    Opening a multi-gigabyte file costs more than the query itself, so
    connections are reused. DuckDB connections are not safe for concurrent use,
    hence the per-pool lock around every statement.
    """

    def __init__(self, max_size: int = MAX_OPEN_CONNECTIONS) -> None:
        self._max_size = max_size
        self._lock = threading.Lock()
        self._connections: OrderedDict[Path, duckdb.DuckDBPyConnection] = OrderedDict()

    def query(
        self, path: Path, sql: str, params: list[object] | None = None
    ) -> list[tuple]:
        with self._lock:
            connection = self._connections.pop(path, None)
            if connection is None:
                connection = duckdb.connect(str(path), read_only=True)
            self._connections[path] = connection
            while len(self._connections) > self._max_size:
                _, evicted = self._connections.popitem(last=False)
                evicted.close()
            return connection.execute(sql, params or []).fetchall()

    def evict(self, path: Path) -> None:
        """Close and drop the pooled connection for exactly one path, if any.

        Used by the server-cache commit coordinator (``cache_commit.py``,
        Step 5 of the plan) before ``os.replace``-ing a live file: on
        Windows, an open read-only handle to a file can block replacing it,
        so the handle must be closed first. Callers must hold that stem's
        operation lock (``TickRepository.symbol_lock``) for the whole span
        between this call and the replace, or a concurrent query could
        reopen the path (and the handle this just closed) before the
        replace runs.
        """
        with self._lock:
            connection = self._connections.pop(path, None)
        if connection is not None:
            connection.close()

    def close(self) -> None:
        with self._lock:
            for connection in self._connections.values():
                connection.close()
            self._connections.clear()


def _listing_cache_path(cache_dir: Path) -> Path:
    return cache_dir / LISTING_CACHE_FILENAME


def _read_persisted_listing(cache_dir: Path) -> set[str] | None:
    try:
        raw = _listing_cache_path(cache_dir).read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    try:
        data = json.loads(raw)
        stems = data["stems"]
    except (ValueError, KeyError, TypeError):
        return None
    if not isinstance(stems, list):
        return None
    return {str(item) for item in stems}


def _write_persisted_listing(cache_dir: Path, stems: set[str]) -> None:
    """Best-effort: a listing-cache write failure must never break a read
    that otherwise succeeded, so any `OSError` here is swallowed."""
    target = _listing_cache_path(cache_dir)
    tmp = target.with_suffix(target.suffix + ".tmp")
    try:
        tmp.write_text(json.dumps({"stems": sorted(stems)}), encoding="utf-8")
        os.replace(tmp, target)
    except OSError:
        pass


class TickRepository:
    """Server-cache-backed, read-only façade over ``stocks_trades``."""

    def __init__(
        self, *, cache_dir: Path, server_url: str, http_client: httpx.Client
    ) -> None:
        self.cache_dir = cache_dir
        self.server_url = server_url
        self._client = http_client
        self._pool = _ConnectionPool()
        self._info_cache: dict[str, SymbolInfo] = {}

        self._symbol_locks_guard = threading.Lock()
        self._symbol_locks: dict[str, threading.Lock] = {}
        self._generations: dict[str, int] = {}
        self._unavailable: dict[str, str] = {}
        # Step 1's freshness policy: revalidate (conditional GET) at most
        # once per symbol per process lifetime, on that symbol's first
        # access this process.
        self._revalidated: set[str] = set()

        self._coordinator = CommitCoordinator(cache_dir=cache_dir, repository=self)

    @property
    def http_client(self) -> httpx.Client:
        """The injected client, exposed so its constructor (a test fixture,
        or Step 7's lifespan hook in production) can close it — see
        `close`."""
        return self._client

    # -- commit-coordinator support (Step 5) ------------------------------

    def symbol_lock(self, stem: str) -> threading.Lock:
        """Return the one lock that gates the pool, info cache, and
        generation counter for ``stem``, creating it on first use.

        Every generation-dependent operation for a stem — a commit
        (``cache_commit.py``) and every read below — must hold this same
        lock for its entire query-then-act span, not just around
        individual steps.
        """
        with self._symbol_locks_guard:
            lock = self._symbol_locks.get(stem)
            if lock is None:
                lock = threading.Lock()
                self._symbol_locks[stem] = lock
            return lock

    def generation(self, stem: str) -> int:
        """The stem's current generation, 0 if it has never been committed."""
        return self._generations.get(stem, 0)

    def publish_generation(self, stem: str, generation: int) -> None:
        """Record ``generation`` as current and drop any cached `SymbolInfo`.

        Caller must hold ``symbol_lock(stem)``. Takes the target generation
        explicitly (rather than incrementing) so crash/in-process-failure
        reconciliation can re-derive and re-apply the same number a
        partially completed commit already wrote to the live sidecar,
        instead of double-incrementing.
        """
        self._generations[stem] = generation
        self._info_cache.pop(stem, None)

    def evict(self, stem: str) -> None:
        """Close this stem's pooled connection, if any. Caller must hold
        ``symbol_lock(stem)`` for the whole span through the file replace
        that follows — see `_ConnectionPool.evict`."""
        self._pool.evict(cache.live_file_path(self.cache_dir, stem))

    def mark_unavailable(self, stem: str, reason: str) -> None:
        """Refuse further reads for ``stem`` until the process restarts.

        Used only when Step 5's in-process failure reconciliation itself
        cannot complete — never for an ordinary miss or a successful
        recovery. Left in place rather than silently serving a file whose
        sidecar/generation/cache state cannot be proven consistent.
        """
        self._unavailable[stem] = reason

    def is_unavailable(self, stem: str) -> str | None:
        """The reason `stem` was marked unavailable, or ``None``."""
        return self._unavailable.get(stem)

    # -- listing / existence (Step 6: server listing, not local glob) ----

    def list_symbol_stems(self) -> list[str]:
        """Return the known symbol stems: live if the server answered,
        degrading to a last-persisted listing or the local cache dir's own
        contents otherwise (never raises)."""
        stems, _was_live = self._resolve_listing()
        return sorted(stem for stem in stems if SYMBOL_STEM_RE.fullmatch(stem))

    def _resolve_listing(self) -> tuple[set[str], bool]:
        live = self._fetch_live_listing()
        if live is not None:
            _write_persisted_listing(self.cache_dir, live)
            return live, True
        persisted = _read_persisted_listing(self.cache_dir) or set()
        return persisted | self._local_cached_stems(), False

    def _fetch_live_listing(self) -> set[str] | None:
        try:
            response = self._client.get("/api/stocks-trades")
            response.raise_for_status()
            data = response.json()
            return {str(stem) for stem in data["stems"]}
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            return None

    def _local_cached_stems(self) -> set[str]:
        return {
            path.name[: -len(".duckdb")]
            for path in cache.stocks_trades_dir(self.cache_dir).glob("*.duckdb")
            if SYMBOL_STEM_RE.fullmatch(path.name[: -len(".duckdb")])
        }

    def _confirm_stem_known(self, stem: str) -> None:
        stems, was_live = self._resolve_listing()
        if stem in stems:
            return
        if was_live:
            raise SymbolNotFoundError(f"no tick file for symbol {stem}")
        raise SymbolAvailabilityUnknownError(
            f"{stem}: cannot confirm existence — server unreachable and nothing cached"
        )

    # -- download orchestration (Steps 1/4/5, invoked under symbol_lock) -

    def confirm_known(self, stem: str) -> None:
        """Confirm ``stem`` exists (or raise), without downloading its file.

        A listing check only — fast, never a multi-GB transfer — so
        ``server.py`` (Step 7) can call this synchronously before deciding
        whether the file itself needs the async pending/``operationId``
        path. Raises the same errors as `path_for`/`symbol_info`.
        """
        if not SYMBOL_STEM_RE.fullmatch(stem):
            raise SymbolNotFoundError(f"invalid symbol identifier: {stem!r}")
        with self.symbol_lock(stem):
            reason = self.is_unavailable(stem)
            if reason is not None:
                raise SymbolUnavailableError(f"{stem}: unavailable ({reason})")
            self._confirm_stem_known(stem)

    def needs_download(self, stem: str) -> bool:
        """Best-effort, lock-free peek: would the next access need a
        network round trip? Used by ``server.py`` (Step 7) to route a
        request into the async pending/``operationId`` path instead of
        blocking a request thread on a multi-GB download.

        This is advisory only — the actual decision inside
        ``_ensure_fresh_locked`` (taken under ``symbol_lock``) is always the
        source of truth. A benign race here (this says "no" but a commit is
        about to invalidate the file, or "yes" but another thread just
        finished revalidating it) only ever costs one extra fast inline
        check or one extra harmless background thread; it can never cause
        an incorrect read.
        """
        if self.is_unavailable(stem) is not None:
            return False  # let the synchronous path raise promptly instead
        path = cache.live_file_path(self.cache_dir, stem)
        return not path.is_file() or stem not in self._revalidated

    def ensure_fresh(
        self, stem: str, *, progress_cb: cache.ProgressCallback | None = None
    ) -> None:
        """Public, lock-acquiring entry point for a caller that does not
        already hold ``symbol_lock(stem)`` — e.g. the background thread
        Step 7's async download path runs. Callers that already hold the
        lock (this class's own read methods) must use
        ``_ensure_fresh_locked`` directly instead, or deadlock.

        A degraded ("stale-served") outcome — a revalidation failed and the
        existing local file was served as-is — never raises here; it is
        reported via ``cache.observe_download()`` instead (see
        ``_download_and_commit``), which Step 7's ``OperationTracker`` reads
        from the same execution context to choose the correct terminal
        state without this call's own return value needing to carry it.
        """
        if not SYMBOL_STEM_RE.fullmatch(stem):
            raise SymbolNotFoundError(f"invalid symbol identifier: {stem!r}")
        with self.symbol_lock(stem):
            self._ensure_fresh_locked(stem, progress_cb=progress_cb)

    def _ensure_fresh_locked(
        self, stem: str, *, progress_cb: cache.ProgressCallback | None = None
    ) -> tuple[Path, int]:
        """Caller must hold ``symbol_lock(stem)``. Confirms the stem is
        known (or raises), ensures the local file has been revalidated at
        least once this process lifetime, and returns ``(path, generation)``
        for the caller to use for every subsequent query in this same span.
        """
        reason = self.is_unavailable(stem)
        if reason is not None:
            raise SymbolUnavailableError(f"{stem}: unavailable ({reason})")

        self._confirm_stem_known(stem)
        path = cache.live_file_path(self.cache_dir, stem)

        if not path.is_file():
            self._download_and_commit(stem, conditional=False, progress_cb=progress_cb)
        elif stem not in self._revalidated:
            self._download_and_commit(stem, conditional=True, progress_cb=progress_cb)
        self._revalidated.add(stem)

        return path, self.generation(stem)

    def _download_and_commit(
        self,
        stem: str,
        *,
        conditional: bool,
        progress_cb: cache.ProgressCallback | None = None,
    ) -> None:
        """A degraded ("stale-served") outcome is reported to any active
        ``cache.observe_download()`` scope, not via this method's return
        value — see `ensure_fresh`."""
        try:
            staging = cache.stage_download(
                stem,
                cache_dir=self.cache_dir,
                client=self._client,
                conditional=conditional,
                progress_cb=progress_cb,
            )
        except cache.StemNotOnServerError as error:
            raise SymbolNotFoundError(str(error)) from error
        except cache.ServerUnreachableError as error:
            live_file = cache.live_file_path(self.cache_dir, stem)
            if conditional and live_file.is_file():
                # A local copy already exists; a failed revalidation is not
                # fatal — serve the (possibly stale) local file rather than
                # blocking a read on a transient network failure. The
                # transient error was already recorded by `cache.
                # stage_download` into any active `observe_download()`
                # scope before it reached us, so simply not re-raising here
                # is enough for that scope to see it.
                return
            raise SymbolAvailabilityUnknownError(
                f"{stem}: could not download — {error}"
            ) from error
        except cache.DownloadError as error:
            # Disk-full, corrupt download, and local write failures are not
            # evidence that the remote source is merely offline. Serving a
            # stale file would hide a local durability/integrity failure as a
            # healthy degraded refresh, so surface it as unavailable instead.
            raise SymbolUnavailableError(
                f"{stem}: local cache refresh failed — {error}"
            ) from error
        if staging.not_modified:
            return
        self._coordinator.refresh_locked(stem, staging)

    def _query_with_corruption_retry(
        self, stem: str, path: Path, sql: str, params: list[object]
    ) -> list[tuple]:
        """Run a pool query; on a DuckDB-open/structural failure, force
        exactly one unconditional redownload and retry once (Step 4's "a
        local file that fails the DuckDB-open check" contract)."""
        try:
            return self._pool.query(path, sql, params)
        except duckdb.Error:
            self._download_and_commit(stem, conditional=False)
            fresh_path = cache.live_file_path(self.cache_dir, stem)
            return self._pool.query(fresh_path, sql, params)

    # -- symbols -----------------------------------------------------------

    def path_for(self, stem: str) -> Path:
        """Confirm ``stem`` exists and is cached locally, returning its path.

        Raises `SymbolNotFoundError` (404) if authoritatively absent from
        the live listing, `SymbolAvailabilityUnknownError` (503) if
        existence cannot be confirmed at all, or `SymbolUnavailableError`
        if a prior commit failed unrecoverably.
        """
        if not SYMBOL_STEM_RE.fullmatch(stem):
            raise SymbolNotFoundError(f"invalid symbol identifier: {stem!r}")
        with self.symbol_lock(stem):
            path, _generation = self._ensure_fresh_locked(stem)
            return path

    def symbol_info(self, stem: str) -> SymbolInfo:
        """Read identity and coverage from the metadata table (no table scan).

        Acquires ``symbol_lock(stem)`` for the entire query-then-cache-write
        span (Step 6 of the plan) — this is what fixes the read-after-
        invalidate race a separate info-cache lock would leave open.
        """
        if not SYMBOL_STEM_RE.fullmatch(stem):
            raise SymbolNotFoundError(f"invalid symbol identifier: {stem!r}")
        with self.symbol_lock(stem):
            cached = self._info_cache.get(stem)
            if cached is not None:
                return cached
            path, _generation = self._ensure_fresh_locked(stem)
            info = self._read_symbol_info_locked(stem, path)
            self._info_cache[stem] = info
            return info

    def _read_symbol_info_locked(self, stem: str, path: Path) -> SymbolInfo:
        rows = self._query_with_corruption_retry(stem, path, _METADATA_SELECT, [])
        if not rows:
            raise SymbolNotFoundError(f"{stem}: {METADATA_TABLE} is empty")
        code, first, last, count = rows[0]
        return SymbolInfo(
            stem=stem,
            code=str(code),
            first_timestamp=first,
            last_timestamp=last,
            record_count=int(count),
            other_codes=tuple(str(row[0]) for row in rows[1:]),
        )

    # -- sessions ------------------------------------------------------------

    def resolve_and_load_session(
        self, stem: str, day: Date, direction: int
    ) -> SessionTicks | None:
        """Find the nearest session with data and load it.

        Replaces the old two-call ``find_session``/``load_session`` pattern
        with one compound method under one lock acquisition, operating on a
        single ``(path, generation)`` pair for the whole span — a second,
        independent ``path_for``/generation lookup partway through could
        otherwise straddle a concurrent commit. ``direction`` is ``+1``
        (forward), ``-1`` (backward) or ``0`` (only the given day). Returns
        ``None`` if no session was found within range.
        """
        if not SYMBOL_STEM_RE.fullmatch(stem):
            raise SymbolNotFoundError(f"invalid symbol identifier: {stem!r}")
        with self.symbol_lock(stem):
            path, _generation = self._ensure_fresh_locked(stem)
            info = self._info_cache.get(stem)
            if info is None:
                info = self._read_symbol_info_locked(stem, path)
                self._info_cache[stem] = info

            resolved_day = self._find_session_locked(stem, path, info, day, direction)
            if resolved_day is None:
                return None
            return self._load_session_locked(stem, path, info, resolved_day)

    def _has_session_locked(
        self, stem: str, path: Path, info: SymbolInfo, day: Date
    ) -> bool:
        rows = self._query_with_corruption_retry(
            stem,
            path,
            f"SELECT 1 FROM {TRADES_TABLE} "
            f"WHERE Code = ? AND Timestamp >= ? AND Timestamp < ? LIMIT 1",
            [info.code, day.isoformat(), (day + timedelta(days=1)).isoformat()],
        )
        return bool(rows)

    def _find_session_locked(
        self, stem: str, path: Path, info: SymbolInfo, day: Date, direction: int
    ) -> Date | None:
        first, last = info.first_timestamp.date(), info.last_timestamp.date()

        if direction == 0:
            return day if self._has_session_locked(stem, path, info, day) else None

        current = day
        for _ in range(MAX_SESSION_PROBE_STEPS):
            if current < first or current > last:
                return None
            if current.weekday() < 5 and self._has_session_locked(
                stem, path, info, current
            ):
                return current
            current += timedelta(days=direction)
        return None

    def _load_session_locked(
        self, stem: str, path: Path, info: SymbolInfo, day: Date
    ) -> SessionTicks:
        rows = self._query_with_corruption_retry(
            stem,
            path,
            f"SELECT epoch_us(TRY_CAST(Timestamp AS TIMESTAMP)) AS us, Price, Qty, Type "
            f"FROM {TRADES_TABLE} "
            f"WHERE Code = ? AND Timestamp >= ? AND Timestamp < ? "
            f"ORDER BY Timestamp",
            [info.code, day.isoformat(), (day + timedelta(days=1)).isoformat()],
        )

        us: list[int] = []
        price: list[float] = []
        qty: list[int] = []
        trade_type: list[str] = []
        previous_us = -1
        for row_us, row_price, row_qty, row_type in rows:
            # An unparseable timestamp cannot be placed on a time axis; a null
            # price cannot be drawn. Both are dropped rather than guessed.
            if row_us is None or row_price is None:
                continue
            # Executions can share a microsecond. Lightweight Charts needs a
            # strictly increasing time axis, so collisions are nudged forward
            # by one microsecond instead of being collapsed.
            value = int(row_us)
            if value <= previous_us:
                value = previous_us + 1
            previous_us = value
            us.append(value)
            price.append(float(row_price))
            qty.append(int(row_qty or 0))
            trade_type.append("" if row_type is None else str(row_type))

        return SessionTicks(
            stem=stem,
            code=info.code,
            date=day.isoformat(),
            us=us,
            price=price,
            qty=qty,
            trade_type=trade_type,
        )

    def close(self) -> None:
        """Close pooled DuckDB connections.

        Does not close the injected ``http_client`` — that client is owned
        by whoever constructed it (a test fixture, or Step 7's lifespan
        hook in production), which is responsible for closing it.
        """
        self._pool.close()
