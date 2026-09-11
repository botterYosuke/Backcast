import asyncio
import importlib.util
import json
from pathlib import Path

import pytest

RUNNER_PATH = (
    Path(__file__).parents[1]
    / "src"
    / "tickreplay"
    / "static"
    / "strategy"
    / "strategy-runner.py"
)


@pytest.fixture
def runner_module():
    spec = importlib.util.spec_from_file_location(
        "tickreplay_strategy_runner", RUNNER_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.dispose_strategy()
    yield module
    module.dispose_strategy()


def invoke(module, request: dict) -> dict:
    payload = json.dumps(request, separators=(",", ":"))
    return json.loads(asyncio.run(module.handle_json(payload)))


def test_javascript_and_python_abi_constants_do_not_drift(runner_module):
    protocol_path = RUNNER_PATH.with_name("strategy-protocol.mjs")
    protocol = protocol_path.read_text(encoding="utf-8")

    assert "export const PROTOCOL_VERSION = 1;" in protocol
    assert "responseBytes: 64 * 1024" in protocol
    assert runner_module.PROTOCOL_VERSION == 1
    assert runner_module.MAX_RESPONSE_BYTES == 64 * 1024
    for command_type in runner_module.COMMAND_TYPES:
        assert f"'{command_type}'" in protocol


def test_function_form_preserves_state_and_normalizes_none(runner_module):
    source = """
counter = 0
def on_start(context):
    global counter
    counter = context["seed"]
def on_event(event):
    global counter
    counter += 1
    if counter == 2:
        return None
    return [{"type": "submit_market", "client_intent_id": f"buy-{counter}", "side": "buy", "quantity": counter}]
"""
    loaded = invoke(
        runner_module,
        {"operation": "load", "source": source, "filename": "strategy.py"},
    )
    started = invoke(runner_module, {"operation": "start", "payload": {"seed": 1}})
    first = invoke(runner_module, {"operation": "event", "payload": {"tick": 1}})
    second = invoke(runner_module, {"operation": "event", "payload": {"tick": 2}})

    assert loaded["ok"] is True
    assert loaded["callbacks"] == ["on_start", "on_event"]
    assert started["ok"] is True
    assert first["commands"] == []
    assert second["commands"][0]["client_intent_id"] == "buy-3"


def test_strategy_object_form_supports_async_callbacks(runner_module):
    source = """
class Strategy:
    def __init__(self):
        self.events = 0
    async def on_start(self, context):
        self.events = context["events"]
    async def on_event(self, event):
        self.events += 1
        return [{"type": "submit_limit", "client_intent_id": str(self.events), "side": "sell", "quantity": 2, "limit_price_level": 123}]
    async def on_end(self, summary):
        print("done", self.events)
"""
    assert invoke(runner_module, {"operation": "load", "source": source})["ok"] is True
    assert (
        invoke(runner_module, {"operation": "start", "payload": {"events": 4}})["ok"]
        is True
    )
    event = invoke(runner_module, {"operation": "event", "payload": {}})
    ended = invoke(runner_module, {"operation": "end", "payload": {}})

    assert event["commands"][0]["client_intent_id"] == "5"
    assert ended["ok"] is True
    assert "done 5" in ended["stdout"]


def test_preconstructed_strategy_object_form_is_supported(runner_module):
    source = """
class Implementation:
    def on_event(self, event):
        return []
Strategy = Implementation()
"""

    loaded = invoke(runner_module, {"operation": "load", "source": source})

    assert loaded["ok"] is True
    assert loaded["callbacks"] == ["on_event"]


@pytest.mark.parametrize(
    ("source", "category"),
    [
        ("def on_event(:\n    pass", "syntax_error"),
        ("value = 1", "abi_error"),
        ("class Strategy:\n    pass", "abi_error"),
    ],
)
def test_load_reports_bounded_typed_diagnostics(runner_module, source, category):
    result = invoke(
        runner_module, {"operation": "load", "source": source, "filename": "user.py"}
    )

    assert result["ok"] is False
    assert result["diagnostic"]["category"] == category
    assert result["diagnostic"]["phase"] == "load"
    assert (
        len(result["diagnostic"]["message"].encode())
        <= runner_module.MAX_DIAGNOSTIC_MESSAGE_BYTES
    )


def test_runtime_exception_has_source_location_and_terminal_result(runner_module):
    source = "def on_event(event):\n    raise ValueError('broken')\n"
    assert invoke(runner_module, {"operation": "load", "source": source})["ok"] is True

    result = invoke(runner_module, {"operation": "event", "payload": {}})

    assert result["ok"] is False
    assert result["diagnostic"]["category"] == "python_exception"
    assert result["diagnostic"]["range"]["start"]["line"] == 2


def test_on_start_cannot_return_commands(runner_module):
    source = """
def on_start(context):
    return [{"type": "submit_market", "client_intent_id": "early", "side": "buy", "quantity": 1}]
def on_event(event):
    return []
"""
    assert invoke(runner_module, {"operation": "load", "source": source})["ok"] is True

    result = invoke(runner_module, {"operation": "start", "payload": {}})

    assert result["ok"] is False
    assert result["diagnostic"]["category"] == "abi_error"


@pytest.mark.parametrize(
    "command",
    [
        {"type": "unknown", "client_intent_id": "x"},
        {
            "type": "submit_market",
            "client_intent_id": "x",
            "side": "hold",
            "quantity": 1,
        },
        {
            "type": "submit_market",
            "client_intent_id": "x",
            "side": "buy",
            "quantity": True,
        },
        {
            "type": "submit_limit",
            "client_intent_id": "x",
            "side": "buy",
            "quantity": 1,
            "limit_price_level": 1.5,
        },
        {"type": "cancel_strategy_orders", "client_intent_id": "x", "extra": 1},
    ],
)
def test_command_validation_rejects_complete_batch(runner_module, command):
    source = f"def on_event(event):\n    return {command!r}\n"
    assert invoke(runner_module, {"operation": "load", "source": source})["ok"] is True

    result = invoke(runner_module, {"operation": "event", "payload": {}})

    assert result["ok"] is False
    assert result["diagnostic"]["category"] == "invalid_commands"
    assert "commands" not in result


def test_duplicate_ids_and_command_limit_are_rejected(runner_module):
    duplicate_source = """
def on_event(event):
    one = {"type": "submit_market", "client_intent_id": "same", "side": "buy", "quantity": 1}
    return [one, dict(one)]
"""
    assert (
        invoke(runner_module, {"operation": "load", "source": duplicate_source})["ok"]
        is True
    )
    assert invoke(runner_module, {"operation": "event", "payload": {}})["ok"] is False

    overrun_source = """
def on_event(event):
    return [{"type": "submit_market", "client_intent_id": str(i), "side": "buy", "quantity": 1} for i in range(33)]
"""
    assert (
        invoke(runner_module, {"operation": "load", "source": overrun_source})["ok"]
        is True
    )
    result = invoke(runner_module, {"operation": "event", "payload": {}})
    assert result["ok"] is False
    assert "32" in result["diagnostic"]["message"]


def test_client_intent_id_is_unique_for_the_complete_run(runner_module):
    source = """
def on_event(event):
    return [{"type": "submit_market", "client_intent_id": "once", "side": "buy", "quantity": 1}]
"""
    assert invoke(runner_module, {"operation": "load", "source": source})["ok"] is True
    assert invoke(runner_module, {"operation": "event", "payload": {}})["ok"] is True

    duplicate = invoke(runner_module, {"operation": "event", "payload": {}})

    assert duplicate["ok"] is False
    assert duplicate["diagnostic"]["category"] == "duplicate_intent_id"


def test_run_command_limit_is_enforced_across_events(runner_module):
    runner_module.MAX_COMMANDS_PER_RUN = 2
    source = """
count = 0
def on_event(event):
    global count
    count += 1
    return [{"type": "submit_market", "client_intent_id": str(count), "side": "buy", "quantity": 1}]
"""
    assert invoke(runner_module, {"operation": "load", "source": source})["ok"] is True
    assert invoke(runner_module, {"operation": "event", "payload": {}})["ok"] is True
    assert invoke(runner_module, {"operation": "event", "payload": {}})["ok"] is True
    result = invoke(runner_module, {"operation": "event", "payload": {}})
    assert result["ok"] is False
    assert result["diagnostic"]["category"] == "run_command_limit"


def test_depth_string_and_response_byte_limits_apply_before_bridge_conversion(
    runner_module,
):
    runner_module.MAX_STRING_BYTES = 8
    source = "def on_event(event):\n    return [{'type':'submit_market','client_intent_id':'too-long-id','side':'buy','quantity':1}]\n"
    assert invoke(runner_module, {"operation": "load", "source": source})["ok"] is True
    assert invoke(runner_module, {"operation": "event", "payload": {}})["ok"] is False

    runner_module.MAX_STRING_BYTES = 128
    runner_module.MAX_RESPONSE_BYTES = 40
    assert (
        invoke(
            runner_module,
            {"operation": "load", "source": "def on_event(event):\n    return []\n"},
        )["ok"]
        is True
    )
    result = invoke(
        runner_module,
        {"operation": "event", "payload": {"nested": [[[[[[[[[1]]]]]]]]]}},
    )
    assert result["ok"] is False
    assert result["diagnostic"]["category"] in {"payload_limit", "response_limit"}


def test_stdout_and_stderr_are_independently_utf8_bounded(runner_module):
    runner_module.MAX_STREAM_BYTES = 12
    source = """
import sys
def on_event(event):
    print("あ" * 20)
    print("error" * 10, file=sys.stderr)
    return []
"""
    loaded = invoke(runner_module, {"operation": "load", "source": source})
    assert loaded["ok"] is True
    result = invoke(runner_module, {"operation": "event", "payload": {}})

    assert len(result["stdout"].encode()) <= 12
    assert len(result["stderr"].encode()) <= 12
    assert result["stdout_truncated"] is True
    assert result["stderr_truncated"] is True
    result["stdout"].encode("utf-8")


def test_request_and_source_limits_and_canonicalization(runner_module):
    assert runner_module.canonicalize_source("\ufeffa\r\nb\rc") == "a\nb\nc"
    runner_module.MAX_SOURCE_BYTES = 8
    result = invoke(
        runner_module, {"operation": "load", "source": "def on_event(event): pass"}
    )
    assert result["ok"] is False
    assert result["diagnostic"]["category"] == "source_limit"

    runner_module.MAX_SOURCE_BYTES = 256 * 1024
    runner_module.MAX_SOURCE_LINE_SCALARS = 4
    result = invoke(
        runner_module, {"operation": "load", "source": "def on_event(event): pass"}
    )
    assert result["ok"] is False
    assert result["diagnostic"]["category"] == "source_limit"

    oversized = json.dumps({"operation": "event", "payload": "x" * 50})
    runner_module.MAX_REQUEST_BYTES = 20
    parsed = json.loads(asyncio.run(runner_module.handle_json(oversized)))
    assert parsed["ok"] is False
    assert parsed["diagnostic"]["category"] == "request_limit"


def test_end_ignores_returned_commands_and_dispose_is_idempotent(runner_module):
    source = """
def on_event(event): return []
def on_end(summary):
    return [{"type": "submit_market", "client_intent_id": "late", "side": "buy", "quantity": 1}]
"""
    assert invoke(runner_module, {"operation": "load", "source": source})["ok"] is True
    ended = invoke(runner_module, {"operation": "end", "payload": {}})
    assert ended["ok"] is True
    assert "commands" not in ended
    runner_module.dispose_strategy()
    runner_module.dispose_strategy()

    result = invoke(runner_module, {"operation": "event", "payload": {}})
    assert result["ok"] is False
    assert result["diagnostic"]["category"] == "lifecycle_error"
