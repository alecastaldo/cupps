"""A CUPPS platform simulator (TS 01.04.0004, platform side).

This stands in for a real CUPPS platform so an application can be exercised
end to end -- handshake, authentication, device acquisition, locking, reads
and prints -- on a developer workstation, in CI, and as an acceptance harness
before an airport cutover.

It follows the architecture of Figure 26.1 (a): one node serving the platform
interface, and one node serving every device interface, with the device
descriptors returned at authentication pointing at that device node.

This is a *simulator*, not a platform implementation: it is deliberately
permissive where a certified platform must be strict, and it is not a
substitute for testing against the platform actually installed at the
airport.
"""

from __future__ import annotations

import logging
import queue
import secrets
import socket
import socketserver
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from cupps import crypto, header as hdr, params, results, xmlmsg
from cupps.msgid import MessageIdGenerator

log = logging.getLogger("cupps.simulator")

#: Interface levels this simulator offers (Table 26.2 Supported rows).
OFFERED_LEVELS = (
    ("01.01", "01.01.0008", "C:/CUPPS/XSD/01.01/cupps-01.01.xsd"),
    ("01.03", "01.03.0017", "C:/CUPPS/XSD/01.03/CUPPS-XML-1.03.xsd"),
    ("01.04", "01.04.0006", "C:/CUPPS/XSD/01.04/CUPPS-XML-01.04.XSD"),
)

_TOKEN_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"


def _make_token() -> str:
    """A 16-character device token, matching the shape used in Listing 29.6."""
    return "".join(secrets.choice(_TOKEN_ALPHABET) for _ in range(16))


def _now() -> str:
    """Platform time in the ``YYYYMMDDThhmmss`` form of Listing 29.6."""
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")


@dataclass
class SimulatedDevice:
    """One device the simulated platform offers."""

    name: str
    device_type: str
    index: str = "1"
    modes: tuple[results.InterfaceMode, ...] = ()
    vendor: str = "Simulator"
    model: str = "SIM-1"
    misc_info: str = "simulated device"
    ready: bool = True
    power_off: bool = False
    paper_out: bool = False
    paper_jam: bool = False
    disk_error: bool = False
    stocks: tuple[tuple[str, float, float], ...] = ()
    capabilities: str = ""
    sub_devices: list["SimulatedDevice"] = field(default_factory=list)

    # -- runtime state (not configuration) --------------------------------
    pending_reads: list[xmlmsg.Element] = field(default_factory=list, repr=False)
    lock_owner: Optional[str] = field(default=None, repr=False)
    lock_method: Optional[str] = field(default=None, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def __post_init__(self) -> None:
        if not self.modes:
            self.modes = tuple(
                results.MODES_BY_DEVICE.get(self.device_type.upper(), ())
            )

    @property
    def parameter_type(self) -> str:
        return f"{self.device_type.lower()}DeviceParameter"

    def status_element(self) -> xmlmsg.Element:
        """The ``<xxStatus>`` element carrying only this type's flags."""
        flags = results.STATUS_FLAGS_BY_DEVICE.get(self.device_type.upper(), ())
        values = {
            "ready": self.ready,
            "unknown": False,
            "init": False,
            "powerOff": self.power_off,
            "paperOut": self.paper_out,
            "paperJam": self.paper_jam,
            "diskError": self.disk_error,
        }
        attrs = {"desc": ""}
        for flag in flags:
            attrs[flag] = "true" if values[flag] else "false"
        return xmlmsg.Element(f"{self.device_type.lower()}Status", attrs)

    def to_element(self, host: str, port: int) -> xmlmsg.Element:
        """The ``<device>`` descriptor returned in authenticate/query."""
        element = xmlmsg.Element(
            "device",
            {
                "deviceIndex": self.index,
                "deviceName": self.name,
                "deviceParameterType": self.parameter_type,
            },
        )
        modes_element = element.add(xmlmsg.Element("supportedInterfaceModes"))
        for mode in self.modes:
            modes_element.add(xmlmsg.Element("interfaceMode", {"mode": mode.value}))

        parameter_attrs: dict[str, str] = {}
        if self.capabilities:
            parameter_attrs[f"{self.device_type.lower()}Capabilities"] = (
                self.capabilities
            )
        if self.device_type.upper() in ("PR", "BP", "BT"):
            parameter_attrs["supportsColor"] = "none"
        parameters = element.add(
            xmlmsg.Element(self.parameter_type, parameter_attrs)
        )
        parameters.add(
            xmlmsg.Element(
                "vendorModelInfo",
                {
                    "miscInfo": self.misc_info,
                    "model": self.model,
                    "vendor": self.vendor,
                },
            )
        )
        parameters.add(
            xmlmsg.Element(
                "ipAndPort",
                {"hostName": host, "ip": host, "port": str(port)},
            )
        )
        parameters.add(self.status_element())
        if self.stocks:
            stocks_element = parameters.add(xmlmsg.Element("supportedStocks"))
            for stock_name, width, height in self.stocks:
                stocks_element.add(
                    xmlmsg.Element(
                        "supportedStock",
                        {
                            "stockName": stock_name,
                            "supportsColor": "none",
                            "stockWidth": str(width),
                            "stockHeight": str(height),
                            "leftMargin": "0",
                            "rightMargin": "0",
                            "topMargin": "0",
                            "bottomMargin": "0",
                            "perforationLeft": "0",
                        },
                    )
                )
        for sub in self.sub_devices:
            element.add(sub.to_element(host, port))
        return element


def default_devices(workstation: str = "SIMCUPPSCKI001") -> list[SimulatedDevice]:
    """A representative check-in / boarding position.

    Mirrors the device mix of the Listing 29.6 example: readers, a Standard
    Mode printer, the AEA boarding-pass and bag-tag printers, a boarding gate
    macro device with sub-devices, and the two Special Mode software devices.
    """
    barcode_sub = SimulatedDevice(
        name=f"{workstation}BC2", device_type="BC", index="8.1"
    )
    magnetic_sub = SimulatedDevice(
        name=f"{workstation}MS2", device_type="MS", index="8.2"
    )
    return [
        SimulatedDevice(name=f"{workstation}BC1", device_type="BC", index="1"),
        SimulatedDevice(name=f"{workstation}MS1", device_type="MS", index="2",
                        capabilities="FOID13"),
        SimulatedDevice(name=f"{workstation}OC1", device_type="OC", index="3"),
        SimulatedDevice(
            name=f"{workstation}PR1",
            device_type="PR",
            index="4",
            stocks=(("BP", 210.0, 99.0), ("BT", 51.0, 508.0), ("A4", 210.0, 297.0)),
        ),
        SimulatedDevice(name=f"{workstation}BP1", device_type="BP", index="5"),
        SimulatedDevice(name=f"{workstation}BT1", device_type="BT", index="6"),
        SimulatedDevice(
            name=f"{workstation}BG1",
            device_type="BG",
            index="8",
            capabilities="AVOK12#BGMRCPTST",
            sub_devices=[barcode_sub, magnetic_sub],
        ),
        SimulatedDevice(name=f"{workstation}ZL1", device_type="ZL", index="10"),
        SimulatedDevice(name=f"{workstation}ZI1", device_type="ZI", index="11"),
    ]


class _Peer:
    """Server side of one accepted socket: framing plus message dispatch."""

    def __init__(self, sock: socket.socket, simulator: "PlatformSimulator") -> None:
        self.sock = sock
        self.simulator = simulator
        self.ids = MessageIdGenerator(platform_side=True)
        self.interface_level = ""
        self.send_lock = threading.Lock()
        self.closed = False

    def send(self, message: xmlmsg.Message) -> None:
        if self.closed:
            return
        try:
            with self.send_lock:
                self.sock.sendall(hdr.frame(message.encode()))
        except OSError:
            self.closed = True

    def reply(
        self,
        message_name: str,
        request: xmlmsg.Message,
        *,
        result: Optional[str] = results.OK,
        body: Optional[xmlmsg.Element] = None,
        attrs: Optional[dict] = None,
    ) -> None:
        """Send a response echoing the request's messageID (section 26.12)."""
        element = body or xmlmsg.Element(message_name)
        if result is not None:
            element.attrs.setdefault("result", result)
        if attrs:
            element.attrs.update(attrs)
        self.send(
            xmlmsg.build(
                message_name,
                request.message_id,
                body=element,
                interface_level=self.interface_level or "01.04",
            )
        )

    def notify(self, body: xmlmsg.Element) -> None:
        """Send an unsolicited ``<notify>`` from the platform's ID range."""
        self.send(
            xmlmsg.build(
                "notify",
                self.ids.allocate(),
                body=body,
                interface_level=self.interface_level or "01.04",
            )
        )

    def illogical(self, expected: list[str]) -> None:
        """Send ``<illogicalMessageErrorEvent>`` and close (section 27.1.2)."""
        element = xmlmsg.Element("illogicalMessageErrorEvent")
        expected_list = element.add(xmlmsg.Element("expectedMessageNameList"))
        for name in expected:
            expected_list.add(
                xmlmsg.Element("expectedMessageName", {"messageName": name})
            )
        self.send(
            xmlmsg.build(
                "illogicalMessageErrorEvent",
                self.ids.allocate(),
                body=element,
                interface_level=self.interface_level or "01.04",
            )
        )
        self.closed = True


class _Handler(socketserver.BaseRequestHandler):
    """Shared framing loop for both the platform and device listeners."""

    #: Set by the concrete server subclass.
    is_device_listener = False

    def handle(self) -> None:
        simulator: PlatformSimulator = self.server.simulator  # type: ignore[attr-defined]
        self.request.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        self.request.settimeout(params.PLT_SOCK_MAX_IDLE_TIME)
        peer = _Peer(self.request, simulator)
        state: dict[str, object] = {"device": None, "mode": None, "token": None}
        simulator._register_peer(peer, state)
        try:
            while not peer.closed:
                message = self._read_message(peer)
                if message is None:
                    break
                simulator._dispatch(peer, state, message, self.is_device_listener)
        except (OSError, ValueError) as exc:
            log.debug("simulator peer ended: %s", exc)
        finally:
            simulator._unregister_peer(peer, state)
            peer.closed = True

    def _read_message(self, peer: _Peer) -> Optional[xmlmsg.Message]:
        raw_header = self._read_exactly(hdr.HEADER_01_SIZE)
        if raw_header is None:
            return None
        head = hdr.decode_header(raw_header)
        body = self._read_exactly(head.body_length)
        if body is None:
            return None
        return xmlmsg.parse(body)

    def _read_exactly(self, count: int) -> Optional[bytes]:
        buffer = bytearray()
        while len(buffer) < count:
            try:
                chunk = self.request.recv(count - len(buffer))
            except socket.timeout:
                return None
            if not chunk:
                return None
            buffer += chunk
        return bytes(buffer)


class _PlatformHandler(_Handler):
    is_device_listener = False


class _DeviceHandler(_Handler):
    is_device_listener = True


class _Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, address, handler, simulator: "PlatformSimulator") -> None:
        self.simulator = simulator
        super().__init__(address, handler)


class PlatformSimulator:
    """A runnable, in-process CUPPS platform."""

    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        platform_port: int = 0,
        device_port: int = 0,
        workstation: str = "SIMCUPPSCKI001",
        location: str = "Simulated check-in position",
        devices: Optional[list[SimulatedDevice]] = None,
        print_sink: Optional[Path] = None,
        crypt_algorithm: str = results.CryptAlgorithm.AES_STRONG.value,
    ) -> None:
        self.host = host
        self.workstation = workstation
        self.location = location
        self.devices = devices if devices is not None else default_devices(workstation)
        self.crypt_algorithm = crypt_algorithm
        self.print_sink = Path(print_sink) if print_sink else None
        if self.print_sink:
            self.print_sink.mkdir(parents=True, exist_ok=True)

        self._tokens: set[str] = set()
        self._peers: dict[_Peer, dict] = {}
        self._peers_lock = threading.Lock()
        self._logs: dict[tuple[str, str], list[str]] = {}
        self._print_counter = 0

        #: Everything printed during this run, for assertions in tests.
        self.printed: list[dict] = []
        #: AEA command strings received, for assertions in tests.
        self.aea_commands: list[str] = []
        #: Results of <applicationStopCommandResponse>, for assertions in tests.
        self.stop_responses: list[str] = []
        #: Documents cancelled via <printCancelRequest>, for assertions.
        self.cancelled_documents: list[str] = []

        self._platform_server = _Server(
            (host, platform_port), _PlatformHandler, self
        )
        self._device_server = _Server((host, device_port), _DeviceHandler, self)
        self.platform_port = self._platform_server.server_address[1]
        self.device_port = self._device_server.server_address[1]
        self._threads: list[threading.Thread] = []

    # -- lifecycle --------------------------------------------------------

    def start(self) -> "PlatformSimulator":
        for server, name in (
            (self._platform_server, "sim-platform"),
            (self._device_server, "sim-devices"),
        ):
            thread = threading.Thread(
                target=server.serve_forever, name=name, daemon=True
            )
            thread.start()
            self._threads.append(thread)
        log.info(
            "simulator listening: platform %s:%s, devices %s:%s",
            self.host,
            self.platform_port,
            self.host,
            self.device_port,
        )
        return self

    def stop(self) -> None:
        for server in (self._platform_server, self._device_server):
            server.shutdown()
            server.server_close()
        for thread in self._threads:
            thread.join(timeout=2.0)
        self._threads.clear()

    def __enter__(self) -> "PlatformSimulator":
        return self.start()

    def __exit__(self, *exc_info: object) -> None:
        self.stop()

    @property
    def environment_overrides(self) -> dict[str, str]:
        """Environment variables pointing an application at this simulator."""
        overrides = {
            "CUPPSPN": self.host,
            "CUPPSPP": str(self.platform_port),
            "CUPPSCN": self.workstation,
            "CUPPSAL2": "ZZ",
            "CUPPSUN": "SIM-USER",
        }
        for device in self._all_devices():
            overrides[f"CUPPS{device.device_type.upper()}{device.index.split('.')[0]}"] = (
                device.name
            )
        return overrides

    # -- device helpers ---------------------------------------------------

    def _all_devices(self) -> list[SimulatedDevice]:
        found: list[SimulatedDevice] = []

        def walk(devices: list[SimulatedDevice]) -> None:
            for device in devices:
                found.append(device)
                walk(device.sub_devices)

        walk(self.devices)
        return found

    def device(self, name: str) -> Optional[SimulatedDevice]:
        target = name.upper()
        for device in self._all_devices():
            if device.name.upper() == target:
                return device
        return None

    # -- injection API (what a test or an operator drives) ----------------

    def scan_barcode(self, device_name: str, data: str, type_code: str = "6") -> None:
        """Queue a barcode scan and notify every session holding the device."""
        import base64

        device = self._require_device(device_name)
        element = xmlmsg.Element("readerData")
        element.add(
            xmlmsg.Element(
                "bcData",
                {"bcTypeCode": type_code, "readStatus": results.OK},
                text=base64.b64encode(data.encode("utf-8")).decode("ascii"),
            )
        )
        with device._lock:
            device.pending_reads.append(element)
        self._notify_device(device_name, xmlmsg.Element("notify", {"dataAvailable": "true"}))

    def swipe_card(self, device_name: str, tracks: dict[int, str]) -> None:
        """Queue an MS swipe, encrypting each track under the session token."""
        device = self._require_device(device_name)
        with self._peers_lock:
            token = next(
                (
                    str(state.get("token"))
                    for peer, state in self._peers.items()
                    if state.get("device") is device and state.get("token")
                ),
                None,
            )
        if token is None:
            raise RuntimeError(
                f"no session has acquired {device_name}; acquire it before swiping"
            )

        element = xmlmsg.Element("readerData")
        data_element = element.add(
            xmlmsg.Element("msData", {"msType": "Card", "readStatus": results.OK})
        )
        for track_id, content in sorted(tracks.items()):
            track_element = data_element.add(
                xmlmsg.Element("msTrackData", {"trackID": str(track_id)})
            )
            track_element.add(
                xmlmsg.Element(
                    "blockData",
                    {"blockID": "1"},
                    text=crypto.encrypt_track(
                        content.encode("latin-1"), token, self.crypt_algorithm
                    ),
                )
            )
        with device._lock:
            device.pending_reads.append(element)
        self._notify_device(device_name, xmlmsg.Element("notify", {"dataAvailable": "true"}))

    def set_status(self, device_name: str, **flags: bool) -> None:
        """Change a device's status and notify every session holding it."""
        device = self._require_device(device_name)
        mapping = {
            "ready": "ready",
            "power_off": "power_off",
            "paper_out": "paper_out",
            "paper_jam": "paper_jam",
            "disk_error": "disk_error",
        }
        for key, attribute in mapping.items():
            if key in flags:
                setattr(device, attribute, bool(flags[key]))

        body = xmlmsg.Element("notify")
        notification = body.add(xmlmsg.Element("deviceStatusNotification"))
        notification.add(device.status_element())
        self._notify_device(device_name, body)

    def _require_device(self, name: str) -> SimulatedDevice:
        device = self.device(name)
        if device is None:
            raise KeyError(f"no simulated device named {name!r}")
        return device

    def _notify_device(self, device_name: str, body: xmlmsg.Element) -> None:
        target = device_name.upper()
        with self._peers_lock:
            peers = [
                peer
                for peer, state in self._peers.items()
                if isinstance(state.get("device"), SimulatedDevice)
                and state["device"].name.upper() == target  # type: ignore[union-attr]
            ]
        for peer in peers:
            peer.notify(body)

    # -- peer bookkeeping -------------------------------------------------

    def _register_peer(self, peer: _Peer, state: dict) -> None:
        with self._peers_lock:
            self._peers[peer] = state

    def _unregister_peer(self, peer: _Peer, state: dict) -> None:
        device = state.get("device")
        if isinstance(device, SimulatedDevice):
            with device._lock:
                if device.lock_owner == state.get("lock_id"):
                    device.lock_owner = None
                    device.lock_method = None
        with self._peers_lock:
            self._peers.pop(peer, None)

    # -- dispatch ---------------------------------------------------------

    def _dispatch(
        self,
        peer: _Peer,
        state: dict,
        message: xmlmsg.Message,
        is_device: bool,
    ) -> None:
        name = message.message_name
        handler = getattr(self, f"_on_{name}", None)

        if not peer.interface_level and name not in (
            "interfaceLevelsAvailableRequest",
            "interfaceLevelRequest",
        ):
            peer.illogical(["interfaceLevelsAvailableRequest", "interfaceLevelRequest"])
            return

        if handler is None:
            log.warning("simulator has no handler for %s", name)
            peer.illogical([])
            return
        handler(peer, state, message)

    # -- handshake --------------------------------------------------------

    def _on_interfaceLevelsAvailableRequest(self, peer, state, message) -> None:
        body = xmlmsg.Element("interfaceLevelsAvailableResponse", {"result": results.OK})
        for level, xsd_version, path in OFFERED_LEVELS:
            body.add(
                xmlmsg.Element(
                    "interfaceLevel",
                    {"level": level, "wsLocalPath": path, "xsdVersion": xsd_version},
                )
            )
        peer.reply("interfaceLevelsAvailableResponse", message, result=None, body=body)

    def _on_interfaceLevelRequest(self, peer, state, message) -> None:
        level = message.body.get("level", "")
        if level not in {entry[0] for entry in OFFERED_LEVELS}:
            peer.reply("interfaceLevelResponse", message, result="invalidLevel")
            peer.closed = True
            return
        peer.interface_level = level
        peer.reply("interfaceLevelResponse", message)

    # -- platform connection ----------------------------------------------

    def _on_authenticateRequest(self, peer, state, message) -> None:
        token = _make_token()
        self._tokens.add(token)
        state["token"] = token

        body = xmlmsg.Element(
            "authenticateResponse", {"result": results.OK, "deviceToken": token}
        )
        platform = body.add(
            xmlmsg.Element(
                "platformParameter",
                {
                    "platformVendor": "SIM",
                    "platformVersion": "01.04.0.0001",
                    "platformTime": _now(),
                },
            )
        )
        crypt_info = platform.add(xmlmsg.Element("cryptAlgorithmInfo"))
        default_element = crypt_info.add(xmlmsg.Element("defaultCryptAlgorithm"))
        default_element.add(
            xmlmsg.Element("cryptAlgorithm", {"name": self.crypt_algorithm})
        )
        available = crypt_info.add(xmlmsg.Element("availableCryptAlgorithms"))
        for algorithm in (
            results.CryptAlgorithm.AES_STRONG.value,
            results.CryptAlgorithm.DES_WEAK.value,
        ):
            available.add(xmlmsg.Element("cryptAlgorithm", {"name": algorithm}))
        platform.add(
            xmlmsg.Element("ipVersionSupport", {"runIPv4": "true", "runIPv6": "false"})
        )

        application = body.add(xmlmsg.Element("applicationParameter"))
        for storage_type, path in (
            ("persistentLocal", f"//{self.workstation}/C$/CUPPS/"),
            ("persistentGlobal", f"//{self.workstation}-DC/cupps/"),
            ("transient", f"//{self.workstation}/C$/TEMP/CUPPS/"),
        ):
            application.add(
                xmlmsg.Element(
                    "storage",
                    {"storageT": storage_type, "path": path, "driveLetter": "C"},
                )
            )

        workstation = body.add(
            xmlmsg.Element(
                "workstationParameter",
                {
                    "name": self.workstation,
                    "wsTime": _now(),
                    "locDesc": self.location,
                    "defaultSpooledPrinter": f"//{self.workstation}/SimSpooler",
                },
            )
        )
        workstation.add(
            xmlmsg.Element(
                "vendorModelInfo",
                {"miscInfo": "simulated", "model": "SIM", "vendor": "Simulator"},
            )
        )

        device_list = body.add(xmlmsg.Element("deviceList"))
        for device in self.devices:
            device_list.add(device.to_element(self.host, self.device_port))

        peer.reply("authenticateResponse", message, result=None, body=body)

    def _on_deviceQueryRequest(self, peer, state, message) -> None:
        wanted_name = (message.body.get("deviceName") or "").upper()
        wanted_type = (message.body.get("deviceType") or "").upper()
        body = xmlmsg.Element("deviceQueryResponse", {"result": results.OK})
        device_list = body.add(xmlmsg.Element("deviceList"))
        for device in self._all_devices():
            if wanted_name and device.name.upper() != wanted_name:
                continue
            if wanted_type and device.device_type.upper() != wanted_type:
                continue
            device_list.add(device.to_element(self.host, self.device_port))
        peer.reply("deviceQueryResponse", message, result=None, body=body)

    def request_application_stop(self) -> int:
        """Send ``<applicationStopCommandRequest>`` to every platform session.

        Section 29.2: the platform may request an application to terminate
        gracefully.  Returns how many sessions were told.
        """
        with self._peers_lock:
            peers = [
                peer
                for peer, peer_state in self._peers.items()
                if peer_state.get("token") and peer_state.get("device") is None
            ]
        for peer in peers:
            peer.send(
                xmlmsg.build(
                    "applicationStopCommandRequest",
                    peer.ids.allocate(),
                    body=xmlmsg.Element("applicationStopCommandRequest"),
                    interface_level=peer.interface_level or "01.04",
                )
            )
        return len(peers)

    def _on_applicationStopCommandResponse(self, peer, state, message) -> None:
        """Record the application's answer: OK, or defer (section 29.2)."""
        self.stop_responses.append(message.body.get("result", ""))

    def _on_byeRequest(self, peer, state, message) -> None:
        peer.reply("byeResponse", message)
        token = state.get("token")
        if token:
            # Section 26.7: the token and every device session it opened are
            # invalidated the moment the application says goodbye.
            self._tokens.discard(str(token))

    # -- device connection ------------------------------------------------

    def _on_deviceAcquireRequest(self, peer, state, message) -> None:
        if state.get("device") is not None:
            # Section 26.6 note: a second acquire on one session is illogical.
            peer.illogical(["interfaceModeRequest"])
            return

        device_name = message.body.get("deviceName", "")
        token = message.body.get("deviceToken", "")
        device = self.device(device_name)

        if token not in self._tokens:
            peer.reply(
                "deviceAcquireResponse",
                message,
                result=results.ACQUIRE_INVALID_TOKEN,
            )
            peer.closed = True
            return
        if device is None:
            peer.reply(
                "deviceAcquireResponse", message, result=results.ACQUIRE_INVALID_DEVICE
            )
            peer.closed = True
            return

        state["device"] = device
        state["token"] = token
        state["lock_id"] = id(peer)
        body = xmlmsg.Element("deviceAcquireResponse", {"result": results.OK})
        body.add(device.to_element(self.host, self.device_port))
        peer.reply("deviceAcquireResponse", message, result=None, body=body)

    def _on_interfaceModeRequest(self, peer, state, message) -> None:
        device = state.get("device")
        mode = message.body.get("mode", "")
        if not isinstance(device, SimulatedDevice):
            peer.illogical(["deviceAcquireRequest"])
            return
        try:
            requested = results.InterfaceMode(mode)
        except ValueError:
            peer.reply(
                "interfaceModeResponse", message, result=results.MODE_NOT_SUPPORTED
            )
            return
        if requested not in device.modes:
            peer.reply(
                "interfaceModeResponse", message, result=results.MODE_NOT_SUPPORTED
            )
            return
        state["mode"] = requested
        peer.reply("interfaceModeResponse", message)

    def _on_deviceReleaseRequest(self, peer, state, message) -> None:
        device = state.get("device")
        if isinstance(device, SimulatedDevice):
            with device._lock:
                if device.lock_owner == state.get("lock_id"):
                    device.lock_owner = None
                    device.lock_method = None
        peer.reply("deviceReleaseResponse", message)

    def _on_deviceStatusRequest(self, peer, state, message) -> None:
        device = state.get("device")
        if not isinstance(device, SimulatedDevice):
            peer.illogical(["deviceAcquireRequest"])
            return
        body = xmlmsg.Element("deviceStatusResponse", {"result": results.OK})
        body.add(device.status_element())
        peer.reply("deviceStatusResponse", message, result=None, body=body)

    def _on_deviceLockRequest(self, peer, state, message) -> None:
        device = state.get("device")
        if not isinstance(device, SimulatedDevice):
            peer.illogical(["deviceAcquireRequest"])
            return
        if device.device_type.upper() in ("ZL", "ZI"):
            # Section 30.20 note: Special Mode devices do not support locking.
            peer.illogical([])
            return

        method = message.body.get("lockMethod", results.LockMethod.BY_CONNECTION.value)
        owner = state.get("lock_id")
        with device._lock:
            if device.lock_owner is None:
                device.lock_owner = owner
                device.lock_method = method
                peer.reply("deviceLockResponse", message, result=results.LOCK_OK)
                return
            if device.lock_owner == owner:
                if device.lock_method != method:
                    peer.reply(
                        "deviceLockResponse",
                        message,
                        result=results.LOCK_SWITCHING_METHODS_NOT_ALLOWED,
                    )
                    return
                peer.reply(
                    "deviceLockResponse",
                    message,
                    result=results.LOCK_OK_ALREADY_LOCKED,
                )
                return

        body = xmlmsg.Element(
            "deviceLockResponse", {"result": results.LOCK_DEVICE_LOCKED}
        )
        body.add(
            xmlmsg.Element(
                "deviceLockerInfo",
                {"airline": "ZZ", "applicationName": "OTHER-APP"},
            )
        )
        peer.reply("deviceLockResponse", message, result=None, body=body)

    def _on_deviceUnlockRequest(self, peer, state, message) -> None:
        device = state.get("device")
        if isinstance(device, SimulatedDevice):
            with device._lock:
                if device.lock_owner == state.get("lock_id"):
                    device.lock_owner = None
                    device.lock_method = None
        peer.reply("deviceUnlockResponse", message)

    def _on_setCryptAlgorithmRequest(self, peer, state, message) -> None:
        requested = message.body.get("mode", "")
        if requested not in (
            results.CryptAlgorithm.AES_STRONG.value,
            results.CryptAlgorithm.DES_WEAK.value,
        ):
            peer.reply(
                "setCryptAlgorithmResponse", message, result="invalidAlgorithm"
            )
            return
        state["crypt"] = requested
        peer.reply("setCryptAlgorithmResponse", message)

    # -- device operations ------------------------------------------------

    def _on_readerReadRequest(self, peer, state, message) -> None:
        device = state.get("device")
        if not isinstance(device, SimulatedDevice):
            peer.illogical(["deviceAcquireRequest"])
            return
        body = xmlmsg.Element("readerReadResponse", {"result": results.OK})
        with device._lock:
            pending, device.pending_reads = device.pending_reads, []
        for element in pending:
            body.add(element)
        peer.reply("readerReadResponse", message, result=None, body=body)

    def _on_printRequest(self, peer, state, message) -> None:
        device = state.get("device")
        if not isinstance(device, SimulatedDevice):
            peer.illogical(["deviceAcquireRequest"])
            return

        body = xmlmsg.Element("printResponse")
        any_failure = False
        for document in message.body.findall("printDocument"):
            document_id = document.get("documentID", "0") or "0"
            stock = document.get("stockName", "") or ""
            outcome = results.OK
            if device.paper_out:
                outcome = results.PRINT_PAPER_OUT
            elif not device.ready:
                outcome = results.PRINT_NOT_READY
            else:
                self._capture_print(device, document_id, stock, document)
            any_failure = any_failure or outcome != results.OK
            body.add(
                xmlmsg.Element(
                    "printDocumentResult",
                    {"documentID": document_id, "result": outcome},
                )
            )
        body.attrs["result"] = (
            results.PRINT_ONE_OR_MORE_ISSUES if any_failure else results.OK
        )
        peer.reply("printResponse", message, result=None, body=body)

    def _capture_print(
        self,
        device: SimulatedDevice,
        document_id: str,
        stock: str,
        document: xmlmsg.Element,
    ) -> None:
        import base64

        payload: bytes = b""
        kind = "unknown"
        pdf = document.find("pdfPrintDocument")
        if pdf is not None and pdf.text:
            kind = "pdf"
            payload = base64.b64decode("".join(pdf.text.split()))
        else:
            text_document = document.find("simpleTextPrintDocument")
            if text_document is not None:
                kind = "text"
                chunks: list[str] = []
                for node in text_document.iter("simpleTextPrintDocumentTextNode"):
                    chunks.append(node.text or "")
                for node in text_document.iter("simpleTextPrintDocumentBinaryNode"):
                    if node.text:
                        chunks.append(
                            base64.b64decode("".join(node.text.split())).decode(
                                "utf-8", "replace"
                            )
                        )
                payload = "".join(chunks).encode("utf-8")

        self._print_counter += 1
        record = {
            "device": device.name,
            "documentID": document_id,
            "stockName": stock,
            "kind": kind,
            "bytes": len(payload),
        }
        self.printed.append(record)

        if self.print_sink:
            suffix = "pdf" if kind == "pdf" else "txt"
            target = (
                self.print_sink
                / f"{self._print_counter:04d}-{device.name}-{stock}.{suffix}"
            )
            target.write_bytes(payload)
            record["path"] = str(target)
        log.info("simulated print: %s", record)

    def _on_printCancelRequest(self, peer, state, message) -> None:
        self.cancelled_documents.append(message.body.get("documentID", "") or "")
        peer.reply("printCancelResponse", message)

    def _on_aeaRequest(self, peer, state, message) -> None:
        import base64

        chunks: list[str] = []
        for aea_message in message.body.findall("aeaMessage"):
            for child in aea_message.children:
                if child.name == "aeaText":
                    chunks.append(child.text or "")
                elif child.name == "aeaBinary" and child.text:
                    chunks.append(
                        base64.b64decode("".join(child.text.split())).decode(
                            "latin-1", "replace"
                        )
                    )
        command = "".join(chunks)
        self.aea_commands.append(command)
        log.info("simulated AEA command: %s", command[:80])
        peer.reply("aeaResponse", message)

    # -- ZL logging -------------------------------------------------------

    def _on_logOpenRequest(self, peer, state, message) -> None:
        log_id = message.body.get("logID", "")
        scope = message.body.get("scope", results.LogScope.APPLICATION.value)
        token = message.body.get("securityToken", "")
        if scope == results.LogScope.PLATFORM.value:
            # Section 30.20.1: applications may not open platform scope.
            peer.reply("logOpenResponse", message, result=results.LOG_NOT_AUTHORIZED)
            return
        self._logs.setdefault((log_id, token), [])
        state["log_token"] = token
        peer.reply("logOpenResponse", message)

    def _on_logWriteRequest(self, peer, state, message) -> None:
        log_id = message.body.get("logID", "")
        token = str(state.get("log_token", ""))
        entries = self._logs.setdefault((log_id, token), [])
        for entry in message.body.findall("logMessage"):
            entries.append(
                f"[{entry.get('severity', 'normal')}] {(entry.text or '').strip()}"
            )
        peer.reply("logWriteResponse", message)

    def _on_logRetrieveRequest(self, peer, state, message) -> None:
        log_id = message.body.get("logID", "")
        token = message.body.get("securityToken", "")
        body = xmlmsg.Element("logRetrieveResponse", {"result": results.OK})
        for entry in self._logs.get((log_id, token), []):
            body.add(xmlmsg.Element("logMessage", text=entry))
        peer.reply("logRetrieveResponse", message, result=None, body=body)

    def _on_logCloseRequest(self, peer, state, message) -> None:
        peer.reply("logCloseResponse", message)

    def log_entries(self, log_id: str, security_token: str) -> list[str]:
        """Entries written to a log, for assertions in tests."""
        return list(self._logs.get((log_id, security_token), []))
