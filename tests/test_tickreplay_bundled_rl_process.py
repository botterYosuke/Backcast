from __future__ import annotations

import hashlib
import sys
import time

from fastapi.testclient import TestClient

from tickreplay import server
from tickreplay.bundled_rl_runtime import BundledRlProcess
from tickreplay.config import (
    CACHE_DIR_ENV_VAR,
    LOCAL_AUTHORITATIVE_ENV_VAR,
    NATIVE_ENABLED_ENV_VAR,
)
from tickreplay.strategy_trace import PRESET


def test_real_fixed_child_completes_http_trace_round_trip_from_unrelated_cwd(
    cache_dir, http_client, tick_db_factory, monkeypatch, tmp_path
):
    tick_db_factory(
        "1301",
        [
            ("2026-08-17 09:00:59.000000", 100.0, 100, "1", "13010"),
            ("2026-08-17 09:01:00.000000", 101.0, 100, "1", "13010"),
            ("2026-08-17 09:01:00.000001", 101.0, 100, "1", "13010"),
            ("2026-08-17 09:01:10.000000", 102.0, 100, "1", "13010"),
            ("2026-08-17 09:01:10.000001", 102.0, 100, "1", "13010"),
        ],
    )
    monkeypatch.setenv(CACHE_DIR_ENV_VAR, str(cache_dir))
    monkeypatch.setenv(LOCAL_AUTHORITATIVE_ENV_VAR, "false")
    monkeypatch.setenv(NATIVE_ENABLED_ENV_VAR, "true")
    monkeypatch.setattr(server, "build_http_client", lambda config: http_client)
    unrelated_cwd = tmp_path / "unrelated-cwd"
    unrelated_cwd.mkdir()
    monkeypatch.chdir(unrelated_cwd)
    assert all("_moomoo" not in path for path in sys.path)

    server.reset_for_tests()
    try:
        with TestClient(
            server.app,
            base_url="http://127.0.0.1",
            client=("127.0.0.1", 50000),
        ) as client:
            manager = server.get_native_manager()
            assert manager.available is True
            assert isinstance(manager.process, BundledRlProcess)
            assert client.get("/api/strategies/bundled-rl/capability").json() == {
                "available": True,
                "contractVersion": "bundled_rl_trace_v1",
                "preset": PRESET,
            }

            server.get_repository().symbol_info("1301")
            session = client.get(
                "/api/session", params={"stem": "1301", "date": "2026-08-17"}
            ).json()
            submitted = client.post(
                "/api/strategies/bundled-rl/jobs",
                json={
                    "contractVersion": "bundled_rl_trace_v1",
                    "preset": PRESET,
                    "session": {
                        "stem": session["stem"],
                        "code": session["code"],
                        "date": session["date"],
                        "dataHash": session["dataHash"],
                    },
                },
            )
            assert submitted.status_code in {200, 202}
            job = submitted.json()["job"]
            deadline = time.monotonic() + 15
            while job["state"] in {"queued", "running", "validating"}:
                assert time.monotonic() < deadline
                time.sleep(0.01)
                response = client.get(f"/api/strategies/bundled-rl/jobs/{job['jobId']}")
                assert response.status_code == 200
                job = response.json()

            assert job["state"] == "ready"
            assert job["traceId"] is not None
            trace_response = client.get(
                f"/api/strategies/bundled-rl/traces/{job['traceId']}"
            )
            assert trace_response.status_code == 200
            assert trace_response.headers["etag"] == f'"{job["traceId"]}"'
            assert hashlib.sha256(trace_response.content).hexdigest() == job["traceId"]
            trace = trace_response.json()
            assert trace["inputId"] == job["inputId"]
            assert trace["session"]["dataHash"] == session["dataHash"]
            assert trace["decisions"]
            assert all(
                row["eligibleFromSessionTick"]
                == row["observedSessionTickIndex"] + 1
                < session["count"]
                for row in trace["decisions"]
            )
            assert "source" not in trace_response.text
            assert "q_values" not in trace_response.text
    finally:
        server.reset_for_tests()
