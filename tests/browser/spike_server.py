"""Strict-header static server for the Wave 1 browser compatibility spike."""

from __future__ import annotations

import argparse
import json
import mimetypes
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SPIKE_ROOT = Path(__file__).resolve().parent
STATIC_ROOT = REPOSITORY_ROOT / "src" / "tickreplay" / "static"
FIXTURE_ROOT = REPOSITORY_ROOT / "tests" / "fixtures"
CSP = (
    "default-src 'self'; script-src 'self' 'wasm-unsafe-eval'; "
    "style-src 'self' 'unsafe-inline'; worker-src 'self'; connect-src 'self'; "
    "object-src 'none'; base-uri 'self'"
)
IMMUTABLE_CACHE = "public, max-age=31536000, immutable"

MIME_TYPES = {
    ".css": "text/css; charset=utf-8",
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".json": "application/json",
    ".mjs": "text/javascript; charset=utf-8",
    ".py": "text/x-python; charset=utf-8",
    ".wasm": "application/wasm",
    ".zip": "application/zip",
    ".gz": "application/gzip",
}


class SpikeRequestHandler(SimpleHTTPRequestHandler):
    """Serve only the spike, source fixtures, and committed static assets."""

    server_version = "BackcastStrategySpike/1"

    @staticmethod
    def _resolved_child(root: Path, relative_name: str) -> Path:
        candidate = (root / relative_name).resolve()
        if not candidate.is_relative_to(root.resolve()):
            return SPIKE_ROOT / "__not_found__"
        return candidate

    def translate_path(self, path: str) -> str:
        request_path = urlsplit(path).path
        if request_path in {"/app", "/app/"}:
            return str(STATIC_ROOT / "index.html")
        if request_path in {"/spike", "/spike/", "/spike-isolated", "/spike-isolated/"}:
            return str(SPIKE_ROOT / "spike.html")
        if request_path.startswith("/spike/"):
            return str(
                self._resolved_child(SPIKE_ROOT, request_path.removeprefix("/spike/"))
            )
        if request_path.startswith("/spike-isolated/"):
            return str(
                self._resolved_child(
                    SPIKE_ROOT, request_path.removeprefix("/spike-isolated/")
                )
            )
        if request_path.startswith("/fixtures/"):
            return str(
                self._resolved_child(
                    FIXTURE_ROOT, request_path.removeprefix("/fixtures/")
                )
            )
        if request_path.startswith("/static/"):
            return str(
                self._resolved_child(STATIC_ROOT, request_path.removeprefix("/static/"))
            )
        return str(SPIKE_ROOT / "__not_found__")

    def _send_json(self, value: object) -> None:
        payload = json.dumps(value, separators=(",", ":")).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:  # noqa: N802 - http.server callback name
        request_path = urlsplit(self.path).path
        if request_path == "/api/symbols":
            self._send_json({"total": 1, "symbols": ["285A"]})
            return
        if request_path == "/api/symbols/285A":
            self._send_json(
                {
                    "pending": False,
                    "operationId": None,
                    "stem": "285A",
                    "code": "285A0",
                    "firstDate": "2026-09-10",
                    "lastDate": "2026-09-10",
                }
            )
            return
        if request_path == "/api/session":
            self._send_json(
                {
                    "pending": False,
                    "operationId": None,
                    "stem": "285A",
                    "code": "285A0",
                    "date": "2026-09-10",
                    "count": 5,
                    "us": [
                        1789002000000000,
                        1789002001000000,
                        1789002002000000,
                        1789002003000000,
                        1789002004000000,
                    ],
                    "price": [100.0, 101.0, 102.0, 103.0, 104.0],
                    "qty": [100, 200, 100, 50, 150],
                    "type": ["1", "2", "1", "2", "1"],
                }
            )
            return
        if request_path == "/api/minute-context":
            self._send_json({"bars": []})
            return
        if request_path == "/api/daily-context":
            self._send_json({"available": False, "bars": [], "error": "fixture"})
            return
        super().do_GET()

    def guess_type(self, path: str) -> str:
        suffix = Path(path).suffix.lower()
        return MIME_TYPES.get(
            suffix, mimetypes.guess_type(path)[0] or "application/octet-stream"
        )

    def end_headers(self) -> None:
        request_path = urlsplit(self.path).path
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        if request_path.startswith("/spike-isolated"):
            self.send_header("Cross-Origin-Opener-Policy", "same-origin")
            self.send_header("Cross-Origin-Embedder-Policy", "require-corp")
        if request_path.startswith("/static/vendor/") or request_path.startswith(
            "/static/strategy/releases/"
        ):
            self.send_header("Cache-Control", IMMUTABLE_CACHE)
        else:
            self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def log_message(self, format_string: str, *arguments: object) -> None:
        print(f"spike-server: {format_string % arguments}")


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8765, type=int)
    return parser.parse_args()


def main() -> int:
    arguments = parse_arguments()
    server = ThreadingHTTPServer((arguments.host, arguments.port), SpikeRequestHandler)
    print(f"strategy spike listening on http://{arguments.host}:{arguments.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
