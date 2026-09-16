"""Local HTTP API and static UI host for the device handler.

The agent UI is a browser client, which is the web application architecture
CUPPS itself describes (section 9.3, Listing 9.1) and which suits the touch
screens section 4.3.2 requires.  The handler serves it over the loopback
adapter only: nothing here is reachable from the airport network, so an agent
position exposes no new listening surface.

Live updates use Server-Sent Events rather than polling, so a device status
change reaches the screen as soon as the platform's ``<notify>`` arrives.
"""

from __future__ import annotations

import json
import logging
import mimetypes
import queue
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import parse_qs, unquote, urlparse

from cupps import CuppsError, LockMethod, PrintDocument

from . import documents as document_layouts
from .service import CuppsService

log = logging.getLogger("cuppsd.http")

#: Where the browser client lives, alongside the package.
WEBUI_ROOT = Path(__file__).resolve().parent.parent / "webui"

#: Maximum request body accepted, which is ample for a print payload and
#: small enough that a runaway client cannot exhaust memory.
MAX_BODY_BYTES = 8 * 1024 * 1024


class ApiError(Exception):
    """A request that should produce a specific HTTP status."""

    def __init__(self, status: HTTPStatus, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


class _Handler(BaseHTTPRequestHandler):
    server_version = "cuppsd"
    protocol_version = "HTTP/1.1"

    @property
    def service(self) -> CuppsService:
        return self.server.service  # type: ignore[attr-defined]

    # -- plumbing ---------------------------------------------------------

    def log_message(self, fmt: str, *args: Any) -> None:
        log.debug("%s %s", self.address_string(), fmt % args)

    def _send_json(self, payload: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_error_json(self, status: HTTPStatus, message: str) -> None:
        self._send_json({"error": message}, status=status)

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        if length > MAX_BODY_BYTES:
            raise ApiError(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                f"request body of {length} bytes exceeds the {MAX_BODY_BYTES} limit",
            )
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise ApiError(HTTPStatus.BAD_REQUEST, f"invalid JSON body: {exc}") from exc
        if not isinstance(payload, dict):
            raise ApiError(HTTPStatus.BAD_REQUEST, "request body must be a JSON object")
        return payload

    # -- routing ----------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        try:
            if path == "/api/events":
                self._stream_events()
                return
            handler = self._get_routes().get(path)
            if handler is not None:
                self._send_json(handler(parse_qs(parsed.query)))
                return
            if path.startswith("/api/"):
                raise ApiError(HTTPStatus.NOT_FOUND, f"no such endpoint: {path}")
            self._serve_static(path)
        except ApiError as exc:
            self._send_error_json(exc.status, exc.message)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:  # pragma: no cover - defensive
            log.exception("GET %s failed", path)
            self._send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))

    def do_POST(self) -> None:  # noqa: N802 - http.server API
        path = unquote(urlparse(self.path).path)
        try:
            body = self._read_json()
            for prefix, handler in self._post_routes().items():
                if path == prefix:
                    self._send_json(handler(body))
                    return
                if prefix.endswith("/") and path.startswith(prefix):
                    self._send_json(handler(body, path[len(prefix) :]))
                    return
            raise ApiError(HTTPStatus.NOT_FOUND, f"no such endpoint: {path}")
        except ApiError as exc:
            self._send_error_json(exc.status, exc.message)
        except KeyError as exc:
            self._send_error_json(HTTPStatus.NOT_FOUND, str(exc).strip("'"))
        except CuppsError as exc:
            # A protocol-level refusal is the caller's problem to act on, so
            # it comes back as a 409 with the platform's own wording.
            self._send_error_json(HTTPStatus.CONFLICT, str(exc))
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:  # pragma: no cover - defensive
            log.exception("POST %s failed", path)
            self._send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))

    def _get_routes(self) -> dict[str, Callable[[dict], Any]]:
        return {
            "/api/state": lambda q: self.service.snapshot(),
            "/api/health": lambda q: {
                "ok": self.service.state.value == "aStd",
                "state": self.service.state.value,
                "lastError": self.service.last_error,
            },
            "/api/history": lambda q: {
                "events": self.service.history(int(q.get("limit", ["100"])[0]))
            },
            "/api/log": lambda q: {"entries": self.service.read_log()},
        }

    def _post_routes(self) -> dict[str, Callable[..., Any]]:
        return {
            "/api/print/boardingpass": lambda body: self._print_boarding_pass(body),
            "/api/print/bagtag": lambda body: self._print_bag_tag(body),
            "/api/print/receipt": lambda body: self._print_receipt(body),
            "/api/aea": lambda body: self._send_aea(body),
            "/api/device/": lambda body, rest: self._device_action(body, rest),
            "/api/_sim/": lambda body, rest: self._simulator_control(body, rest),
        }

    # -- static -----------------------------------------------------------

    def _serve_static(self, path: str) -> None:
        relative = "index.html" if path in ("/", "") else path.lstrip("/")
        target = (WEBUI_ROOT / relative).resolve()
        # Refuse anything that escapes the UI directory.
        if not str(target).startswith(str(WEBUI_ROOT.resolve())) or not target.is_file():
            raise ApiError(HTTPStatus.NOT_FOUND, f"not found: {path}")

        content_type, _ = mimetypes.guess_type(target.name)
        body = target.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type or "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    # -- events -----------------------------------------------------------

    def _stream_events(self) -> None:
        channel = self.service.subscribe()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        try:
            # Prime the client with the current picture so it never renders an
            # empty position while waiting for the first change.
            self._write_event({"kind": "snapshot", **self.service.snapshot()})
            while True:
                try:
                    event = channel.get(timeout=15.0)
                except queue.Empty:
                    # A comment frame keeps proxies and the browser from
                    # treating a quiet position as a dead connection.
                    self.wfile.write(b": keep-alive\n\n")
                    self.wfile.flush()
                    continue
                self._write_event(event.to_dict())
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            self.service.unsubscribe(channel)

    def _write_event(self, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, default=str)
        self.wfile.write(f"data: {data}\n\n".encode("utf-8"))
        self.wfile.flush()

    # -- actions ----------------------------------------------------------

    def _device_action(self, body: dict, rest: str) -> Any:
        parts = [part for part in rest.split("/") if part]
        if len(parts) != 2:
            raise ApiError(
                HTTPStatus.BAD_REQUEST,
                "expected /api/device/<deviceName>/<action>",
            )
        name, action = parts
        if action == "lock":
            method = LockMethod(
                body.get("method", LockMethod.BY_CONNECTION.value)
            )
            return {"result": self.service.lock(name, method)}
        if action == "unlock":
            self.service.unlock(name)
            return {"result": "OK"}
        if action == "status":
            return self.service.refresh_status(name)
        if action == "test":
            return self.service.test_device(name)
        raise ApiError(HTTPStatus.BAD_REQUEST, f"unknown device action {action!r}")

    def _resolve_printer(self, body: dict) -> str:
        name = body.get("device")
        if name:
            return str(name)
        handle = self.service.first_of_type("PR")
        if handle is None:
            raise ApiError(
                HTTPStatus.CONFLICT,
                "no PR device is available for Standard Mode printing",
            )
        return handle.name

    def _print_boarding_pass(self, body: dict) -> Any:
        passenger = _passenger_from(body)
        name = self._resolve_printer(body)
        handle = self.service.device(name)
        rendered = document_layouts.boarding_pass(
            passenger,
            stock_name=body.get("stock", "BP"),
            device_stocks=handle.device.stocks,
            symbology=body.get("symbology", "pdf417"),
            airline_name=self.service.config.airline_name,
        )
        results = self.service.print_documents(
            name,
            [
                PrintDocument(
                    document_id=int(body.get("documentID", 1)),
                    stock_name=rendered.stock_name,
                    pdf=rendered.pdf,
                )
            ],
        )
        return {
            "device": name,
            "results": results,
            "barcodeRendered": rendered.barcode_rendered,
            "notes": rendered.notes,
        }

    def _print_bag_tag(self, body: dict) -> Any:
        bag = document_layouts.BagDetails(
            passenger_name=str(body.get("name", "")),
            licence_plate=str(body.get("licencePlate", "")),
            origin=str(body.get("origin", "")),
            destination=str(body.get("destination", "")),
            carrier=str(body.get("carrier", "")),
            flight_number=str(body.get("flightNumber", "")),
            flight_date=str(body.get("flightDate", "")),
            weight_kg=(
                float(body["weightKg"]) if body.get("weightKg") is not None else None
            ),
            bag_number=int(body.get("bagNumber", 1)),
            bag_count=int(body.get("bagCount", 1)),
            via=str(body.get("via", "")),
        )
        if not bag.licence_plate.strip():
            raise ApiError(HTTPStatus.BAD_REQUEST, "licencePlate is required")

        name = self._resolve_printer(body)
        handle = self.service.device(name)
        rendered = document_layouts.bag_tag(
            bag, stock_name=body.get("stock", "BT"),
            device_stocks=handle.device.stocks,
        )
        results = self.service.print_documents(
            name,
            [
                PrintDocument(
                    document_id=int(body.get("documentID", 1)),
                    stock_name=rendered.stock_name,
                    pdf=rendered.pdf,
                )
            ],
        )
        return {"device": name, "results": results, "licencePlate": bag.licence_plate}

    def _print_receipt(self, body: dict) -> Any:
        passenger = _passenger_from(body)
        name = self._resolve_printer(body)
        handle = self.service.device(name)
        rendered = document_layouts.itinerary_receipt(
            passenger,
            lines=[str(line) for line in body.get("lines", [])],
            stock_name=body.get("stock", "A4"),
            device_stocks=handle.device.stocks,
            airline_name=self.service.config.airline_name,
        )
        results = self.service.print_documents(
            name,
            [
                PrintDocument(
                    document_id=int(body.get("documentID", 1)),
                    stock_name=rendered.stock_name,
                    pdf=rendered.pdf,
                )
            ],
        )
        return {"device": name, "results": results}

    def _send_aea(self, body: dict) -> Any:
        name = body.get("device")
        stream = body.get("stream", "")
        if not name:
            raise ApiError(HTTPStatus.BAD_REQUEST, "device is required")
        if not stream:
            raise ApiError(HTTPStatus.BAD_REQUEST, "stream is required")
        self.service.send_aea(str(name), str(stream))
        return {"device": name, "sent": len(str(stream))}


    # -- simulator controls ----------------------------------------------

    def _simulator_control(self, body: dict, action: str) -> Any:
        """Drive the bundled simulator: inject scans, swipes and faults.

        Mounted only when the handler was started against the simulator.  In
        production the attribute is ``None`` and every call here is refused,
        so a deployed position has no way to fabricate a device read.
        """
        simulator = getattr(self.server, "simulator", None)
        if simulator is None:
            raise ApiError(
                HTTPStatus.NOT_FOUND,
                "simulator controls are not available; this handler is "
                "connected to a real CUPPS platform",
            )
        device = str(body.get("device", ""))
        if not device:
            raise ApiError(HTTPStatus.BAD_REQUEST, "device is required")
        try:
            if action == "scan":
                simulator.scan_barcode(
                    device,
                    str(body.get("data", "")),
                    str(body.get("typeCode", "6")),
                )
            elif action == "swipe":
                tracks = {
                    int(key): str(value)
                    for key, value in (body.get("tracks") or {}).items()
                }
                simulator.swipe_card(device, tracks)
            elif action == "status":
                flags = {
                    key: bool(value)
                    for key, value in body.items()
                    if key in ("ready", "power_off", "paper_out", "paper_jam",
                               "disk_error")
                }
                simulator.set_status(device, **flags)
            else:
                raise ApiError(
                    HTTPStatus.BAD_REQUEST,
                    f"unknown simulator action {action!r}",
                )
        except (KeyError, RuntimeError, ValueError) as exc:
            raise ApiError(HTTPStatus.CONFLICT, str(exc)) from exc
        return {"device": device, "action": action, "ok": True}


def _passenger_from(body: dict) -> document_layouts.PassengerDetails:
    return document_layouts.PassengerDetails(
        name=str(body.get("name", "")),
        pnr=str(body.get("pnr", "")),
        origin=str(body.get("origin", "")),
        destination=str(body.get("destination", "")),
        origin_name=str(body.get("originName", "")),
        destination_name=str(body.get("destinationName", "")),
        carrier=str(body.get("carrier", "")),
        flight_number=str(body.get("flightNumber", "")),
        flight_date=str(body.get("flightDate", "")),
        boarding_time=str(body.get("boardingTime", "")),
        departure_time=str(body.get("departureTime", "")),
        gate=str(body.get("gate", "")),
        seat=str(body.get("seat", "")),
        cabin=str(body.get("cabin", "")),
        sequence=str(body.get("sequence", "")),
        frequent_flyer=str(body.get("frequentFlyer", "")),
        bcbp=str(body.get("bcbp", "")),
        selectee=bool(body.get("selectee", False)),
        fast_track=bool(body.get("fastTrack", False)),
    )


class ApiServer:
    """The loopback HTTP server hosting the API and the agent UI."""

    def __init__(
        self,
        service: CuppsService,
        *,
        host: str = "127.0.0.1",
        port: int = 8631,
        simulator: Any = None,
    ) -> None:
        self.service = service
        self._server = ThreadingHTTPServer((host, port), _Handler)
        self._server.daemon_threads = True
        self._server.service = service  # type: ignore[attr-defined]
        # Present only in simulator mode; see _simulator_control.
        self._server.simulator = simulator  # type: ignore[attr-defined]
        self.host, self.port = self._server.server_address[:2]
        self._thread: Optional[threading.Thread] = None

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}/"

    def start(self) -> "ApiServer":
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="cuppsd-http", daemon=True
        )
        self._thread.start()
        log.info("agent UI available at %s", self.url)
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        if self._thread:
            self._thread.join(timeout=2.0)

    def __enter__(self) -> "ApiServer":
        return self.start()

    def __exit__(self, *exc_info: object) -> None:
        self.stop()
