"""Shared plumbing for the tests."""

from __future__ import annotations

import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import server  # noqa: E402


def dashboard_html() -> str:
    return (ROOT / "offgrid-dashboard.html").read_text(encoding="utf-8")


def start_dashboard_server(
    config: "server.Config",
    html_text: str | None = None,
    host: str = "127.0.0.1",
):
    """Start the dashboard server on a free port, in a thread.

    Returns the server and the port it ended up on.
    """
    if html_text is None:
        html_text = dashboard_html()

    server.Handler.config = config
    server.Handler.html_text = html_text
    server.Handler.html_error = None

    httpd = server.Server((host, 0), server.Handler)
    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd, httpd.server_address[1]


def stop_dashboard_server(httpd) -> None:
    httpd.shutdown()
    httpd.server_close()
