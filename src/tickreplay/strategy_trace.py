"""Canonical identities and immutable storage for bundled RL traces."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import uuid
from dataclasses import dataclass
from datetime import date as Date
from pathlib import Path
from typing import Any

CONTRACT_VERSION = "bundled_rl_trace_v1"
PROPOSAL_CONTRACT = "backcast_position_target_v1"
PRESET = {
    "id": "bundled_rl",
    "revision": "rl_scalp_evolution_v1",
    "digest": "528b9dc44928984c471731bb64ef82fa4a299a2ffd5707d1ce2a482223d28ef1",
}
MAX_DECISIONS = 20_000
MAX_ARTIFACT_BYTES = 4 * 1024 * 1024
MAX_STRING_BYTES = 128
MAX_SAFE_INTEGER = 2**53 - 1

HEX_64_RE = re.compile(r"^[0-9a-f]{64}$")
STEM_RE = re.compile(r"^[0-9A-Z]{4,5}$")


class TraceValidationError(ValueError):
    """A trace or identity input violates the public contract."""


class TraceStoreError(RuntimeError):
    """An immutable trace could not be safely stored or read."""


@dataclass(frozen=True, slots=True)
class PreparedTrace:
    trace_id: str
    data: bytes
    temporary: Path


def _fail(message: str) -> None:
    raise TraceValidationError(message)


def _exact_dict(value: Any, required: set[str], *, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        _fail(f"{path} must be an object")
    unknown = set(value) - required
    missing = required - set(value)
    if unknown:
        _fail(f"{path} has unknown field {sorted(unknown)[0]}")
    if missing:
        _fail(f"{path} is missing field {sorted(missing)[0]}")
    return value


def _string(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value:
        _fail(f"{path} must be a non-empty string")
    if len(value.encode("utf-8")) > MAX_STRING_BYTES:
        _fail(f"{path} exceeds its string limit")
    if "\x00" in value:
        _fail(f"{path} contains a forbidden NUL")
    return value


def _integer(value: Any, path: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        _fail(f"{path} must be an integer >= {minimum}")
    if value > MAX_SAFE_INTEGER:
        _fail(f"{path} exceeds the safe integer range")
    return value


def _hex_identifier(value: Any, path: str) -> str:
    if not isinstance(value, str) or HEX_64_RE.fullmatch(value) is None:
        _fail(f"{path} must be a lowercase SHA-256 identifier")
    return value


def _date(value: Any, path: str) -> str:
    text = _string(value, path)
    try:
        parsed = Date.fromisoformat(text)
    except ValueError:
        _fail(f"{path} must be an ISO date")
    if parsed.isoformat() != text:
        _fail(f"{path} must be a canonical ISO date")
    return text


def canonical_json_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise TraceValidationError("value is not finite canonical JSON") from error


def validate_preset(value: Any, *, path: str = "preset") -> dict[str, str]:
    preset = _exact_dict(value, set(PRESET), path=path)
    normalized = {
        "id": _string(preset["id"], f"{path}.id"),
        "revision": _string(preset["revision"], f"{path}.revision"),
        "digest": _hex_identifier(preset["digest"], f"{path}.digest"),
    }
    if normalized != PRESET:
        _fail(f"{path} is unsupported")
    return dict(PRESET)


def _canonical_session(session: Any) -> dict[str, object]:
    value = _exact_dict(
        session,
        {"stem", "code", "date", "count", "us", "price", "qty", "type"},
        path="session",
    )
    stem = _string(value["stem"], "session.stem")
    if STEM_RE.fullmatch(stem) is None:
        _fail("session.stem has an invalid format")
    code = _string(value["code"], "session.code")
    date = _date(value["date"], "session.date")
    count = _integer(value["count"], "session.count")
    columns = [value[name] for name in ("us", "price", "qty", "type")]
    if any(not isinstance(column, list) for column in columns):
        _fail("session columns must be arrays")
    if any(len(column) != count for column in columns):
        _fail("session column lengths must equal count")
    us = [_integer(item, f"session.us[{i}]") for i, item in enumerate(value["us"])]
    if any(left > right for left, right in zip(us, us[1:], strict=False)):
        _fail("session.us must be nondecreasing")
    prices: list[float | int] = []
    for index, item in enumerate(value["price"]):
        if isinstance(item, bool) or not isinstance(item, int | float):
            _fail(f"session.price[{index}] must be a finite positive number")
        if not math.isfinite(item) or item <= 0:
            _fail(f"session.price[{index}] must be a finite positive number")
        prices.append(item)
    quantities = [
        _integer(item, f"session.qty[{i}]") for i, item in enumerate(value["qty"])
    ]
    trade_types: list[str] = []
    for index, item in enumerate(value["type"]):
        if not isinstance(item, str) or len(item.encode("utf-8")) > MAX_STRING_BYTES:
            _fail(f"session.type[{index}] must be a bounded string")
        if "\x00" in item:
            _fail(f"session.type[{index}] contains a forbidden NUL")
        trade_types.append(item)
    return {
        "stem": stem,
        "code": code,
        "date": date,
        "count": count,
        "us": us,
        "price": prices,
        "qty": quantities,
        "type": trade_types,
    }


def canonical_session_bytes(session: Any) -> bytes:
    return canonical_json_bytes(_canonical_session(session))


def session_data_hash(session: Any) -> str:
    return hashlib.sha256(canonical_session_bytes(session)).hexdigest()


def compute_input_id(*, session_bytes: bytes, runtime_fingerprint: str) -> str:
    if not isinstance(session_bytes, bytes):
        _fail("identity session must be bytes")
    fingerprint = _hex_identifier(runtime_fingerprint, "runtime_fingerprint").encode()
    parts = (
        CONTRACT_VERSION.encode(),
        canonical_json_bytes(PRESET),
        PROPOSAL_CONTRACT.encode(),
        fingerprint,
        session_bytes,
        hashlib.sha256(session_bytes).hexdigest().encode(),
    )
    digest = hashlib.sha256(b"backcast-bundled-rl-input\x00")
    for part in parts:
        digest.update(len(part).to_bytes(8, "big"))
        digest.update(part)
    return digest.hexdigest()


def _validate_branch(value: Any, path: str, expected: int) -> dict[str, int]:
    branch = _exact_dict(value, {"positionQuantity", "targetQuantity"}, path=path)
    position = _integer(branch["positionQuantity"], f"{path}.positionQuantity")
    target = _integer(branch["targetQuantity"], f"{path}.targetQuantity")
    if position != expected:
        _fail(f"{path}.positionQuantity must be {expected}")
    if target not in {0, 100}:
        _fail(f"{path}.targetQuantity must be 0 or 100")
    return {"positionQuantity": position, "targetQuantity": target}


def validate_trace(trace: Any) -> dict[str, object]:
    value = _exact_dict(
        trace,
        {
            "contractVersion",
            "preset",
            "session",
            "inputId",
            "runtimeFingerprint",
            "proposalContract",
            "decisions",
        },
        path="trace",
    )
    if value["contractVersion"] != CONTRACT_VERSION:
        _fail("trace.contractVersion is unsupported")
    preset = validate_preset(value["preset"], path="trace.preset")
    input_id = _hex_identifier(value["inputId"], "trace.inputId")
    runtime_fingerprint = _hex_identifier(
        value["runtimeFingerprint"], "trace.runtimeFingerprint"
    )
    if value["proposalContract"] != PROPOSAL_CONTRACT:
        _fail("trace.proposalContract is unsupported")
    session = _exact_dict(
        value["session"],
        {"stem", "code", "date", "dataHash", "tickCount"},
        path="trace.session",
    )
    normalized_session = {
        "stem": _string(session["stem"], "trace.session.stem"),
        "code": _string(session["code"], "trace.session.code"),
        "date": _date(session["date"], "trace.session.date"),
        "dataHash": _hex_identifier(session["dataHash"], "trace.session.dataHash"),
        "tickCount": _integer(session["tickCount"], "trace.session.tickCount"),
    }
    if STEM_RE.fullmatch(normalized_session["stem"]) is None:
        _fail("trace.session.stem has an invalid format")
    decisions = value["decisions"]
    if not isinstance(decisions, list):
        _fail("trace.decisions must be an array")
    if len(decisions) > MAX_DECISIONS:
        _fail("trace.decisions exceeds the decision limit")
    normalized: list[dict[str, object]] = []
    previous = -1
    for index, item in enumerate(decisions):
        path = f"trace.decisions[{index}]"
        row = _exact_dict(
            item,
            {
                "observedSessionTickIndex",
                "eligibleFromSessionTick",
                "decisionTimeUs",
                "positionBranches",
            },
            path=path,
        )
        observed = _integer(
            row["observedSessionTickIndex"], f"{path}.observedSessionTickIndex"
        )
        eligible = _integer(
            row["eligibleFromSessionTick"], f"{path}.eligibleFromSessionTick"
        )
        decision_time = _integer(row["decisionTimeUs"], f"{path}.decisionTimeUs")
        if observed <= previous:
            _fail("trace decisions must be strictly increasing by observed tick")
        if observed >= normalized_session["tickCount"]:
            _fail(f"{path}.observedSessionTickIndex is out of range")
        if eligible != observed + 1:
            _fail(f"{path}.eligibleFromSessionTick must equal observed + 1")
        if eligible >= normalized_session["tickCount"]:
            _fail(f"{path}.eligibleFromSessionTick is out of range")
        branches = row["positionBranches"]
        if not isinstance(branches, list) or len(branches) != 2:
            _fail(f"{path}.positionBranches must contain exactly two branches")
        normalized.append(
            {
                "observedSessionTickIndex": observed,
                "eligibleFromSessionTick": eligible,
                "decisionTimeUs": decision_time,
                "positionBranches": [
                    _validate_branch(branches[0], f"{path}.positionBranches[0]", 0),
                    _validate_branch(branches[1], f"{path}.positionBranches[1]", 100),
                ],
            }
        )
        previous = observed
    return {
        "contractVersion": CONTRACT_VERSION,
        "preset": preset,
        "session": normalized_session,
        "inputId": input_id,
        "runtimeFingerprint": runtime_fingerprint,
        "proposalContract": PROPOSAL_CONTRACT,
        "decisions": normalized,
    }


def canonical_trace_bytes(
    trace: Any, *, max_artifact_bytes: int = MAX_ARTIFACT_BYTES
) -> bytes:
    encoded = canonical_json_bytes(validate_trace(trace)) + b"\n"
    if len(encoded) > max_artifact_bytes:
        _fail(f"trace artifact exceeds {max_artifact_bytes} bytes")
    return encoded


class TraceStore:
    """Content-addressed immutable JSON storage with atomic publication."""

    def __init__(self, root: Path, *, max_artifact_bytes: int = MAX_ARTIFACT_BYTES):
        self.root = root
        self.max_artifact_bytes = max_artifact_bytes
        if root.exists() and not root.is_dir():
            raise TraceStoreError("trace store root is not a directory")

    def _path(self, trace_id: str) -> Path:
        if HEX_64_RE.fullmatch(trace_id) is None:
            raise TraceStoreError("invalid trace identifier")
        return self.root / f"{trace_id}.json"

    def prepare(self, trace: Any) -> PreparedTrace:
        data = canonical_trace_bytes(trace, max_artifact_bytes=self.max_artifact_bytes)
        trace_id = hashlib.sha256(data).hexdigest()
        self.root.mkdir(parents=True, exist_ok=True)
        temporary = self.root / f".{trace_id}.{uuid.uuid4().hex}.tmp"
        try:
            with temporary.open("xb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as error:
            temporary.unlink(missing_ok=True)
            raise TraceStoreError("trace artifact could not be prepared") from error
        return PreparedTrace(trace_id, data, temporary)

    def commit(self, prepared: PreparedTrace) -> str:
        target = self._path(prepared.trace_id)
        if (
            prepared.temporary.parent != self.root
            or not prepared.temporary.name.startswith(f".{prepared.trace_id}.")
        ):
            raise TraceStoreError("prepared trace does not belong to this store")
        if target.exists():
            existing = target.read_bytes()
            if existing != prepared.data:
                raise TraceStoreError("trace store collision detected")
            self._verify(prepared.trace_id, existing)
            return prepared.trace_id
        try:
            os.link(prepared.temporary, target)
        except FileExistsError:
            if target.read_bytes() != prepared.data:
                raise TraceStoreError("trace store collision detected") from None
        except OSError as error:
            raise TraceStoreError("trace artifact could not be published") from error
        self._verify(prepared.trace_id, target.read_bytes())
        return prepared.trace_id

    def discard(self, prepared: PreparedTrace) -> None:
        prepared.temporary.unlink(missing_ok=True)

    def publish(self, trace: Any) -> str:
        prepared = self.prepare(trace)
        try:
            return self.commit(prepared)
        finally:
            self.discard(prepared)

    def _verify(self, trace_id: str, data: bytes) -> dict[str, object]:
        if len(data) > self.max_artifact_bytes:
            raise TraceStoreError("trace artifact exceeds its size limit")
        if hashlib.sha256(data).hexdigest() != trace_id:
            raise TraceStoreError("trace artifact hash mismatch")
        try:
            decoded = json.loads(data)
            canonical = canonical_trace_bytes(
                decoded, max_artifact_bytes=self.max_artifact_bytes
            )
        except (
            UnicodeDecodeError,
            json.JSONDecodeError,
            TraceValidationError,
        ) as error:
            raise TraceStoreError("trace artifact is corrupt") from error
        if canonical != data:
            raise TraceStoreError("trace artifact is not canonical")
        return validate_trace(decoded)

    def read_bytes(self, trace_id: str) -> bytes:
        try:
            data = self._path(trace_id).read_bytes()
        except FileNotFoundError:
            raise
        except OSError as error:
            raise TraceStoreError("trace artifact could not be read") from error
        self._verify(trace_id, data)
        return data

    def read(self, trace_id: str) -> dict[str, object]:
        data = self.read_bytes(trace_id)
        return self._verify(trace_id, data)
