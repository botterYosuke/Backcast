"""Standard-library-only Python ABI wrapper for browser replay strategies.

The module is intentionally independent of Pyodide.  The Worker calls
``handle_json`` and native pytest exercises the same boundary.
"""

from __future__ import annotations

import contextlib
import inspect
import io
import json
import traceback
from collections.abc import Callable, Mapping
from types import TracebackType
from typing import Any, Final

PROTOCOL_VERSION: Final[int] = 1
MAX_SOURCE_BYTES: Final[int] = 256 * 1024
MAX_SOURCE_SCALARS: Final[int] = 256 * 1024
MAX_SOURCE_LINES: Final[int] = 8192
MAX_SOURCE_LINE_SCALARS: Final[int] = 4096
MAX_REQUEST_BYTES: Final[int] = 256 * 1024
MAX_RESPONSE_BYTES: Final[int] = 64 * 1024
MAX_COMMANDS_PER_EVENT: Final[int] = 32
MAX_COMMANDS_PER_RUN: Final[int] = 10_000
MAX_STRING_BYTES: Final[int] = 128
MAX_STREAM_BYTES: Final[int] = 256 * 1024
MAX_DIAGNOSTIC_MESSAGE_BYTES: Final[int] = 2 * 1024
MAX_NESTING_DEPTH: Final[int] = 8
MAX_CONTAINER_ITEMS: Final[int] = 512
COMMAND_TYPES: Final[tuple[str, ...]] = (
    "submit_market",
    "submit_limit",
    "cancel_strategy_orders",
)
SIDES: Final[frozenset[str]] = frozenset({"buy", "sell"})


class StrategyRunnerError(Exception):
    """A bounded, user-visible ABI or lifecycle error."""

    def __init__(self, category: str, message: str) -> None:
        super().__init__(message)
        self.category = category


class BoundedTextStream(io.TextIOBase):
    """UTF-8 byte-bounded text sink which never stores a partial scalar."""

    def __init__(self, maximum_bytes: int) -> None:
        super().__init__()
        self.maximum_bytes = maximum_bytes
        self._parts: list[str] = []
        self._bytes = 0
        self.truncated = False

    def writable(self) -> bool:
        return True

    def write(self, text: str) -> int:
        if not isinstance(text, str):
            raise TypeError("stream writes must be text")
        original_length = len(text)
        remaining = max(0, self.maximum_bytes - self._bytes)
        accepted = truncate_utf8(text, remaining)
        if accepted:
            self._parts.append(accepted)
            self._bytes += len(accepted.encode("utf-8"))
        if accepted != text:
            self.truncated = True
        return original_length

    def getvalue(self) -> str:
        return "".join(self._parts)


class StrategyRunner:
    """Own one normalized function- or object-form strategy instance."""

    def __init__(
        self, callbacks: Mapping[str, Callable[[dict[str, Any]], Any]]
    ) -> None:
        self._callbacks = dict(callbacks)
        self.accepted_commands = 0
        self.seen_intent_ids: set[str] = set()

    @property
    def callback_names(self) -> list[str]:
        return [
            name
            for name in ("on_start", "on_event", "on_end")
            if name in self._callbacks
        ]

    async def invoke(self, operation: str, payload: dict[str, Any]) -> Any:
        callback_name = {
            "start": "on_start",
            "event": "on_event",
            "end": "on_end",
        }.get(operation)
        if callback_name is None:
            raise StrategyRunnerError(
                "lifecycle_error", "unsupported lifecycle operation"
            )
        callback = self._callbacks.get(callback_name)
        if callback is None:
            if callback_name == "on_event":
                raise StrategyRunnerError("abi_error", "on_event is required")
            return None
        result = callback(payload)
        if inspect.isawaitable(result):
            result = await result
        return result

    def accept_commands(self, value: Any) -> list[dict[str, Any]]:
        commands = validate_commands(value)
        intent_ids = {command["client_intent_id"] for command in commands}
        if self.seen_intent_ids & intent_ids:
            raise StrategyRunnerError(
                "duplicate_intent_id",
                "client_intent_id must be unique for the complete strategy run",
            )
        next_count = self.accepted_commands + len(commands)
        if next_count > MAX_COMMANDS_PER_RUN:
            raise StrategyRunnerError(
                "run_command_limit",
                f"strategy run exceeds {MAX_COMMANDS_PER_RUN} accepted commands",
            )
        self.accepted_commands = next_count
        self.seen_intent_ids.update(intent_ids)
        return commands


_ACTIVE_RUNNER: StrategyRunner | None = None


def truncate_utf8(text: str, maximum_bytes: int) -> str:
    """Return the longest UTF-8 prefix no larger than ``maximum_bytes``."""

    if maximum_bytes <= 0:
        return ""
    encoded = text.encode("utf-8")
    if len(encoded) <= maximum_bytes:
        return text
    return encoded[:maximum_bytes].decode("utf-8", errors="ignore")


def canonicalize_source(source: str) -> str:
    """Apply the v1 byte identity's BOM and newline canonicalization."""

    canonical = source[1:] if source.startswith("\ufeff") else source
    return canonical.replace("\r\n", "\n").replace("\r", "\n")


def _bounded_message(message: object) -> str:
    text = str(message) or type(message).__name__
    return truncate_utf8(text, MAX_DIAGNOSTIC_MESSAGE_BYTES)


def _diagnostic_range(
    error: BaseException,
    traceback_value: TracebackType | None,
) -> dict[str, dict[str, int]] | None:
    if isinstance(error, SyntaxError) and error.lineno is not None:
        start_column = max(0, (error.offset or 1) - 1)
        end_column = max(start_column + 1, (error.end_offset or error.offset or 1) - 1)
        end_line = error.end_lineno or error.lineno
        return {
            "start": {"line": error.lineno, "column": start_column},
            "end": {"line": end_line, "column": end_column},
        }
    if traceback_value is None:
        return None
    frames = traceback.extract_tb(traceback_value)
    user_frames = [frame for frame in frames if frame.filename.endswith("strategy.py")]
    frame = user_frames[-1] if user_frames else (frames[-1] if frames else None)
    if frame is None or frame.lineno is None:
        return None
    line = frame.lineno
    column = max(0, frame.colno or 0)
    end_column = max(column + 1, frame.end_colno or column + 1)
    end_line = int(frame.end_lineno or line)
    return {
        "start": {"line": line, "column": column},
        "end": {"line": end_line, "column": end_column},
    }


def _diagnostic(
    phase: str,
    category: str,
    error: BaseException | str,
) -> dict[str, Any]:
    traceback_value = error.__traceback__ if isinstance(error, BaseException) else None
    return {
        "protocol_version": PROTOCOL_VERSION,
        "phase": phase,
        "severity": "error",
        "category": category,
        "message": _bounded_message(error),
        "range": _diagnostic_range(error, traceback_value)
        if isinstance(error, BaseException)
        else None,
    }


def _validate_plain(value: Any, path: str = "$", depth: int = 0) -> None:
    if depth > MAX_NESTING_DEPTH:
        raise StrategyRunnerError("payload_limit", f"{path} is nested too deeply")
    if value is None or isinstance(value, (str, bool)):
        if isinstance(value, str) and len(value.encode("utf-8")) > MAX_STRING_BYTES:
            raise StrategyRunnerError("payload_limit", f"{path} string is too large")
        return
    if isinstance(value, int):
        if abs(value) > 2**53 - 1:
            raise StrategyRunnerError(
                "payload_limit", f"{path} integer is not bridge-safe"
            )
        return
    if isinstance(value, float):
        raise StrategyRunnerError("payload_limit", f"{path} floats are not ABI values")
    if isinstance(value, list):
        if len(value) > MAX_CONTAINER_ITEMS:
            raise StrategyRunnerError("payload_limit", f"{path} has too many items")
        for index, item in enumerate(value):
            _validate_plain(item, f"{path}[{index}]", depth + 1)
        return
    if isinstance(value, dict):
        if len(value) > MAX_CONTAINER_ITEMS:
            raise StrategyRunnerError("payload_limit", f"{path} has too many fields")
        for key, item in value.items():
            if not isinstance(key, str):
                raise StrategyRunnerError(
                    "payload_limit", f"{path} has a non-string key"
                )
            if len(key.encode("utf-8")) > MAX_STRING_BYTES:
                raise StrategyRunnerError("payload_limit", f"{path} key is too large")
            _validate_plain(item, f"{path}.{key}", depth + 1)
        return
    raise StrategyRunnerError("payload_limit", f"{path} is not plain JSON data")


def _assert_exact_keys(
    command: Mapping[str, Any],
    required: set[str],
    optional: set[str] | None = None,
) -> None:
    allowed = required | (optional or set())
    missing = required - command.keys()
    unknown = command.keys() - allowed
    if missing:
        raise StrategyRunnerError(
            "invalid_commands", f"missing command fields: {sorted(missing)}"
        )
    if unknown:
        raise StrategyRunnerError(
            "invalid_commands", f"unknown command fields: {sorted(unknown)}"
        )


def _validate_intent_id(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise StrategyRunnerError(
            "invalid_commands", "client_intent_id must be a non-empty string"
        )
    if len(value.encode("utf-8")) > MAX_STRING_BYTES:
        raise StrategyRunnerError("invalid_commands", "client_intent_id is too large")
    return value


def _validate_positive_integer(value: Any, name: str) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= 2**53 - 1
    ):
        raise StrategyRunnerError(
            "invalid_commands", f"{name} must be a positive safe integer"
        )
    return value


def _validate_command(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise StrategyRunnerError(
            "invalid_commands", "each command must be a plain dict"
        )
    command_type = value.get("type")
    common = {"type", "client_intent_id"}
    if command_type == "submit_market":
        _assert_exact_keys(value, common | {"side", "quantity"})
    elif command_type == "submit_limit":
        _assert_exact_keys(value, common | {"side", "quantity", "limit_price_level"})
    elif command_type == "cancel_strategy_orders":
        _assert_exact_keys(value, common, {"side"})
    else:
        raise StrategyRunnerError("invalid_commands", "unsupported command type")
    intent_id = _validate_intent_id(value["client_intent_id"])
    side = value.get("side")
    if side is not None and side not in SIDES:
        raise StrategyRunnerError("invalid_commands", "side must be buy or sell")
    result: dict[str, Any] = {"type": command_type, "client_intent_id": intent_id}
    if side is not None:
        result["side"] = side
    if command_type in {"submit_market", "submit_limit"}:
        result["quantity"] = _validate_positive_integer(value["quantity"], "quantity")
    if command_type == "submit_limit":
        result["limit_price_level"] = _validate_positive_integer(
            value["limit_price_level"], "limit_price_level"
        )
    return result


def validate_commands(value: Any) -> list[dict[str, Any]]:
    """Validate the complete callback result before returning any command."""

    if value is None:
        return []
    if not isinstance(value, list):
        raise StrategyRunnerError(
            "invalid_commands", "on_event must return a list or None"
        )
    if len(value) > MAX_COMMANDS_PER_EVENT:
        raise StrategyRunnerError(
            "invalid_commands",
            f"on_event may return at most {MAX_COMMANDS_PER_EVENT} commands",
        )
    commands = [_validate_command(command) for command in value]
    intent_ids = [command["client_intent_id"] for command in commands]
    if len(intent_ids) != len(set(intent_ids)):
        raise StrategyRunnerError(
            "invalid_commands", "duplicate client_intent_id in event"
        )
    encoded = json.dumps(commands, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )
    if len(encoded) > MAX_RESPONSE_BYTES:
        raise StrategyRunnerError(
            "response_limit", "encoded commands exceed the response limit"
        )
    return commands


def _discover_callbacks(
    namespace: Mapping[str, Any],
) -> dict[str, Callable[[dict[str, Any]], Any]]:
    strategy_type = namespace.get("Strategy")
    owner: Any = namespace
    if strategy_type is not None:
        if inspect.isclass(strategy_type):
            try:
                owner = strategy_type()
            except Exception as error:
                raise StrategyRunnerError(
                    "abi_error", f"Strategy() failed: {error}"
                ) from error
        else:
            owner = strategy_type
    callbacks: dict[str, Callable[[dict[str, Any]], Any]] = {}
    for name in ("on_start", "on_event", "on_end"):
        callback = (
            owner.get(name)
            if isinstance(owner, Mapping)
            else getattr(owner, name, None)
        )
        if callback is not None:
            if not callable(callback):
                raise StrategyRunnerError("abi_error", f"{name} must be callable")
            callbacks[name] = callback
    if "on_event" not in callbacks:
        raise StrategyRunnerError("abi_error", "on_event is required")
    return callbacks


def load_strategy(source: str, filename: str = "strategy.py") -> StrategyRunner:
    """Compile, execute, discover and return one fresh strategy runner."""

    canonical = canonicalize_source(source)
    source_bytes = canonical.encode("utf-8")
    if not source_bytes:
        raise StrategyRunnerError("abi_error", "strategy source is empty")
    if len(source_bytes) > MAX_SOURCE_BYTES:
        raise StrategyRunnerError(
            "source_limit", f"source exceeds {MAX_SOURCE_BYTES} UTF-8 bytes"
        )
    if len(canonical) > MAX_SOURCE_SCALARS:
        raise StrategyRunnerError("source_limit", "source has too many Unicode scalars")
    lines = canonical.splitlines()
    if len(lines) > MAX_SOURCE_LINES:
        raise StrategyRunnerError("source_limit", "source has too many lines")
    if any(len(line) > MAX_SOURCE_LINE_SCALARS for line in lines):
        raise StrategyRunnerError("source_limit", "source has a line that is too long")
    code = compile(canonical, filename, "exec")
    namespace: dict[str, Any] = {
        "__builtins__": __builtins__,
        "__name__": "__tickreplay_user_strategy__",
        "__file__": filename,
    }
    exec(code, namespace, namespace)
    return StrategyRunner(_discover_callbacks(namespace))


def dispose_strategy() -> None:
    """Release the active Python strategy state; safe to call repeatedly."""

    global _ACTIVE_RUNNER
    _ACTIVE_RUNNER = None


async def _execute(request: dict[str, Any]) -> dict[str, Any]:
    global _ACTIVE_RUNNER
    operation = request.get("operation")
    stdout = BoundedTextStream(MAX_STREAM_BYTES)
    stderr = BoundedTextStream(MAX_STREAM_BYTES)
    try:
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            if operation == "load":
                source = request.get("source")
                if not isinstance(source, str):
                    raise StrategyRunnerError("abi_error", "load source must be text")
                filename = request.get("filename", "strategy.py")
                if not isinstance(filename, str):
                    raise StrategyRunnerError("abi_error", "filename must be text")
                candidate = load_strategy(source, filename)
                _ACTIVE_RUNNER = candidate
                result: dict[str, Any] = {
                    "ok": True,
                    "callbacks": candidate.callback_names,
                }
            else:
                if _ACTIVE_RUNNER is None:
                    raise StrategyRunnerError(
                        "lifecycle_error", "no strategy is loaded"
                    )
                if not isinstance(operation, str):
                    raise StrategyRunnerError(
                        "lifecycle_error", "operation must be text"
                    )
                payload = request.get("payload", {})
                _validate_plain(payload)
                callback_result = await _ACTIVE_RUNNER.invoke(operation, payload)
                result = {"ok": True}
                if operation == "event":
                    result["commands"] = _ACTIVE_RUNNER.accept_commands(callback_result)
                elif operation == "start" and callback_result is not None:
                    raise StrategyRunnerError("abi_error", "on_start must return None")
        return {
            **result,
            "stdout": stdout.getvalue(),
            "stderr": stderr.getvalue(),
            "stdout_truncated": stdout.truncated,
            "stderr_truncated": stderr.truncated,
        }
    except SyntaxError as error:
        category = "syntax_error"
        return _error_result(operation or "load", category, error, stdout, stderr)
    except StrategyRunnerError as error:
        return _error_result(operation or "load", error.category, error, stdout, stderr)
    except Exception as error:
        return _error_result(
            operation or "load", "python_exception", error, stdout, stderr
        )


def _error_result(
    phase: str,
    category: str,
    error: BaseException,
    stdout: BoundedTextStream,
    stderr: BoundedTextStream,
) -> dict[str, Any]:
    return {
        "ok": False,
        "diagnostic": _diagnostic(phase, category, error),
        "stdout": stdout.getvalue(),
        "stderr": stderr.getvalue(),
        "stdout_truncated": stdout.truncated,
        "stderr_truncated": stderr.truncated,
    }


async def handle_json(request_json: str) -> str:
    """Handle one bounded JSON request and return one JSON string."""

    request_bytes = request_json.encode("utf-8")
    try:
        request = json.loads(request_json)
    except (json.JSONDecodeError, UnicodeError) as error:
        result = _error_result(
            "load",
            "request_invalid",
            error,
            BoundedTextStream(MAX_STREAM_BYTES),
            BoundedTextStream(MAX_STREAM_BYTES),
        )
        return json.dumps(result, ensure_ascii=False, separators=(",", ":"))
    operation = request.get("operation") if isinstance(request, dict) else None
    if len(request_bytes) > MAX_REQUEST_BYTES and operation != "load":
        result = _error_result(
            str(operation or "load"),
            "request_limit",
            StrategyRunnerError(
                "request_limit", "request exceeds the encoded byte limit"
            ),
            BoundedTextStream(MAX_STREAM_BYTES),
            BoundedTextStream(MAX_STREAM_BYTES),
        )
    elif not isinstance(request, dict):
        result = _error_result(
            "load",
            "request_invalid",
            StrategyRunnerError("request_invalid", "request must be an object"),
            BoundedTextStream(MAX_STREAM_BYTES),
            BoundedTextStream(MAX_STREAM_BYTES),
        )
    else:
        result = await _execute(request)
    return json.dumps(result, ensure_ascii=False, separators=(",", ":"))
