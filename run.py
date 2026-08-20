"""Launch the tick replay web app.

uv run python run.py            # http://127.0.0.1:8765 を開く
uv run python run.py --port 9000 --no-browser
"""

from __future__ import annotations

import argparse
import sys
import threading
import webbrowser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

import uvicorn  # noqa: E402  (sys.path must be set up first)


def main() -> None:
    parser = argparse.ArgumentParser(description="歩み値リプレイ アプリを起動する")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--reload", action="store_true", help="開発用オートリロード")
    parser.add_argument(
        "--no-browser", action="store_true", help="ブラウザを自動で開かない"
    )
    args = parser.parse_args()

    url = f"http://{args.host}:{args.port}/"
    if not args.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()

    print(f"歩み値リプレイ: {url}")
    uvicorn.run(
        "tickreplay.server:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level="info",
    )


if __name__ == "__main__":
    main()
