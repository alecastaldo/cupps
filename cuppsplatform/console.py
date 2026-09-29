"""The platform console: live state, live events, and a bench.

Serves on the loopback adapter only.  Shows every Part II state machine as it
moves, streams the chapter 31 events as they are raised, and -- for devices
behind a bench transport (pseudo-terminal or socket pair) or a file print backend -- offers bench controls that
act on the *peripheral side* of the wire.  A bench scan is written as bytes
into the character device, where the real driver reads it; it never bypasses
the driver, the securing rule or the protocol.

Bench controls are refused for real hardware.  On a real reader you scan.
"""

from __future__ import annotations

import json
import logging
import queue
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional

from .events import Event
from .server import CuppsPlatform

log = logging.getLogger("cuppsplatform.console")

CONSOLE_HTML = Path(__file__).resolve().parent / "console.html"

#: A Resolution 792 record built to exact field widths, for the bench.
SAMPLE_BCBP = (
    "M1" + "SMITH/JOHN MR".ljust(20) + "E" + "XY7K2Q ".ljust(7)
    + "LHRJFKBA " + "00117" + "326" + "Y" + "032A" + "00025" + "1" + "00"
)


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "cuppsplatform-console"

    @property
    def platform(self) -> CuppsPlatform:
        return self.server.platform  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args: Any) -> None:
        log.debug(fmt, *args)

    def _json(self, payload: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            body = CONSOLE_HTML.read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        elif path == "/api/state":
            self._json(self.platform.snapshot())
        elif path == "/api/events":
            self._stream()
        else:
            self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}") if length else {}
        except ValueError:
            self._json({"error": "invalid JSON"}, HTTPStatus.BAD_REQUEST)
            return
        device = str(body.get("device", ""))
        snapshot = {d["name"]: d for d in self.platform.snapshot()["devices"]}
        entry = snapshot.get(device)
        if entry is None:
            self._json({"error": f"no device {device!r}"}, HTTPStatus.NOT_FOUND)
            return
        if not entry["bench"]:
            # Real hardware: a bench control would be a fabricated read.
            self._json(
                {"error": f"{device} is real hardware; use the peripheral itself"},
                HTTPStatus.CONFLICT,
            )
            return
        try:
            if path == "/api/bench/scan":
                self.platform.scan_barcode(device, str(body.get("data") or SAMPLE_BCBP))
            elif path == "/api/bench/swipe":
                self.platform.swipe_card(device, {
                    1: str(body.get("track1", "B4111111111111111^SMITH/JOHN^2912")),
                    2: str(body.get("track2", "4111111111111111=2912")),
                })
            elif path == "/api/bench/paper":
                self.platform.set_status(device, paper_out=bool(body.get("out")))
            else:
                self._json({"error": "unknown bench action"}, HTTPStatus.NOT_FOUND)
                return
        except Exception as exc:  # pragma: no cover - surfaced to the operator
            self._json({"error": str(exc)}, HTTPStatus.CONFLICT)
            return
        self._json({"ok": True, "device": device})

    def _stream(self) -> None:
        channel: queue.Queue = queue.Queue(maxsize=500)

        def listener(event: Event) -> None:
            try:
                channel.put_nowait(event)
            except queue.Full:
                pass

        self.server.listeners.append(listener)  # type: ignore[attr-defined]
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            while True:
                try:
                    event = channel.get(timeout=15)
                except queue.Empty:
                    self.wfile.write(b": keep-alive\n\n")
                    self.wfile.flush()
                    continue
                data = json.dumps(event.to_dict(), default=str)
                self.wfile.write(f"data: {data}\n\n".encode("utf-8"))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            listeners = self.server.listeners  # type: ignore[attr-defined]
            if listener in listeners:
                listeners.remove(listener)


class Console:
    """The console HTTP server."""

    def __init__(self, platform: CuppsPlatform, *, host: str = "127.0.0.1",
                 port: int = 8640) -> None:
        self.platform = platform
        self._server = ThreadingHTTPServer((host, port), _Handler)
        self._server.daemon_threads = True
        self._server.platform = platform  # type: ignore[attr-defined]
        self._server.listeners = []  # type: ignore[attr-defined]
        platform.bus.add_listener(self._fan_out)
        self.host, self.port = self._server.server_address[:2]
        self._thread: Optional[threading.Thread] = None

    def _fan_out(self, event: Event) -> None:
        for listener in list(self._server.listeners):  # type: ignore[attr-defined]
            listener(event)

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}/"

    def start(self) -> "Console":
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="console", daemon=True
        )
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
