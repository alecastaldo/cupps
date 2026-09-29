"""The CUPPS platform: engine, drivers and wire protocol in one process.

This is where the pieces meet.  The CUPPS wire protocol -- framing,
handshake, authentication, device sessions -- is served by the same code the
simulator uses, but every decision the simulator used to fake is now made by
the platform engine:

* device locking goes through :class:`~cuppsplatform.device.ManagedDevice`,
  so the section 26.13 rules and ``DevLkdTime`` expiry are the real ones;
* reads, prints and AEA traffic go through the bound
  :mod:`~cuppsplatform.drivers`, so bytes genuinely cross a serial port, a
  socket or a pseudo-terminal;
* the platform, workstation, application and device state machines of Part II
  run for real and raise their chapter 31 events.

Only ZL and ZI stay software devices, and that is by definition: they are
the platform's own logging and messaging devices, not peripherals.

Reusing the simulator's protocol handling is deliberate.  The conformance
harness records conversations through hooks in that code, so the same
harness that airline developers use against the simulator runs unchanged
against this engine.
"""

from __future__ import annotations

import base64
import logging
import re
import threading
from pathlib import Path
from typing import Optional

from cupps import xmlmsg
from cupps.results import InterfaceMode, LockMethod

from simulator import PlatformSimulator
from simulator.platform_sim import SimulatedDevice

from .clock import Clock, RealClock
from .device import (
    AcquiredSession,
    LockingNotSupported,
    LockRefused,
    ManagedDevice,
)
from .drivers import (
    BENCH_TRANSPORTS,
    DEFAULT_BENCH_KIND,
    AeaDriver,
    BindingRegistry,
    DeviceDriver,
    DeviceSecured,
    DriverData,
    DriverStatus,
    PrintDriver,
    TransportError,
)
from .events import Event, EventBus, attach_state_machine
from .objects import ManagedApplication, ManagedWorkstation
from .states import PlatformState, new_machine

log = logging.getLogger("cuppsplatform.server")

#: Software devices the platform provides itself (section 30.20, ZI).
SOFTWARE_DEVICES = (("ZL", "10"), ("ZI", "11"))

#: ISO 7811 magnetic track framing: %track1? ;track2? ;/+track3?
_TRACK_RE = re.compile(r"(%[^?]*\?|;[^?]*\?|\+[^?]*\?)")


def split_tracks(payload: str) -> dict[int, str]:
    """Split an ISO 7811 swipe into tracks, falling back to one track."""
    found = _TRACK_RE.findall(payload)
    if not found:
        return {1: payload}
    # ISO 7811: '%' opens track 1; ';' opens track 2, and track 3 after it;
    # some readers open track 3 with '+'.
    tracks: dict[int, str] = {}
    for raw in found:
        if raw[0] == "%":
            track_id = 1
        elif raw[0] == "+":
            track_id = 3
        else:
            track_id = 3 if 2 in tracks else 2
        tracks[track_id] = raw[1:-1]
    return tracks


class CuppsPlatform(PlatformSimulator):
    """A CUPPS platform driving real peripherals through bound drivers."""

    PLATFORM_VENDOR = "CUPPSPLATFORM"

    def __init__(
        self,
        *,
        bindings: BindingRegistry,
        host: str = "127.0.0.1",
        platform_port: int = 0,
        device_port: int = 0,
        workstation: str = "CUPPSPLT001",
        location: str = "CUPPS platform",
        clock: Optional[Clock] = None,
        include_software_devices: bool = True,
    ) -> None:
        self.bindings = bindings
        devices = []
        for index, binding in enumerate(bindings, start=1):
            devices.append(
                SimulatedDevice(
                    name=binding.device_name,
                    device_type=binding.device_type,
                    index=str(index),
                    vendor=binding.options.get("vendor", "bound peripheral"),
                    model=binding.driver,
                    misc_info=binding.transport.kind,
                    # Status is unknown until the driver reports otherwise.
                    ready=False,
                )
            )
        if include_software_devices:
            for device_type, index in SOFTWARE_DEVICES:
                devices.append(
                    SimulatedDevice(
                        name=f"{workstation}{device_type}1",
                        device_type=device_type,
                        index=index,
                        vendor="platform",
                        model="software device",
                    )
                )

        super().__init__(
            host=host,
            platform_port=platform_port,
            device_port=device_port,
            workstation=workstation,
            location=location,
            devices=devices,
        )

        self._owns_clock = clock is None
        self.clock: Clock = clock or RealClock()
        self.bus = EventBus()
        self.machine = new_machine("platform")
        attach_state_machine(self.bus, self.machine)
        self.workstation_object = ManagedWorkstation(
            workstation, bus=self.bus, clock=self.clock
        )
        self.managed: dict[str, ManagedDevice] = {}
        self.drivers: dict[str, DeviceDriver] = {}
        self.applications: dict[int, ManagedApplication] = {}
        self._sessions: dict[tuple[str, int], AcquiredSession] = {}
        self._engine_lock = threading.RLock()
        #: Driver-level observations for the console: reads, discards, errors.
        self.activity: list[dict] = []

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> "CuppsPlatform":
        """Bootstrap: pStp -> pStg, start devices, serve, pStg -> pStd."""
        self.machine.enter(PlatformState.STG, reason="platform startup")
        for device in self.devices:
            managed = ManagedDevice(
                device.name, device.device_type, bus=self.bus, clock=self.clock
            )
            self.managed[device.name.upper()] = managed
            managed.start()

        for binding in self.bindings:
            name = binding.device_name
            driver = binding.build(
                on_data=lambda data, n=name: self._on_driver_data(n, data),
                on_status=lambda status, n=name: self._on_driver_status(n, status),
            )
            self.drivers[name.upper()] = driver
            try:
                driver.start()
            except (TransportError, OSError) as exc:
                log.warning("%s did not start: %s", name, exc)
                self._record(name, "error", f"driver did not start: {exc}")
            self._on_driver_status(name, driver.status)

        for device in self.devices:
            managed = self.managed[device.name.upper()]
            managed.started()
            driver = self.drivers.get(device.name.upper())
            if driver is not None and not driver.status.usable:
                managed.fault(driver.status.description or "not ready")

        self.workstation_object.start()
        super().start()
        self.workstation_object.started()
        self.machine.enter(PlatformState.STD, reason="platform ready")
        log.info(
            "platform %s ready: %d bound peripheral(s), %d device(s)",
            self.workstation, len(self.drivers), len(self.managed),
        )
        return self

    def stop(self) -> None:
        """pStd -> pSpg, stop everything, pSpg -> pStp."""
        if self.machine.state in (PlatformState.STD, PlatformState.ALT):
            self.machine.enter(PlatformState.SPG, reason="platform shutdown")
        for application in list(self.applications.values()):
            application.request_stop(reason="platform stopping")
            application.stopped()
        for driver in self.drivers.values():
            try:
                driver.stop()
            except Exception:  # pragma: no cover - best effort
                log.debug("stopping %s failed", driver.device_name, exc_info=True)
        for managed in self.managed.values():
            managed.stop()
            managed.stopped()
        self.workstation_object.stop()
        self.workstation_object.stopped()
        super().stop()
        if self.machine.state is PlatformState.SPG:
            self.machine.enter(PlatformState.STP, reason="platform stopped")
        if self._owns_clock:
            self.clock.stop()

    # -- driver callbacks (run on driver threads; must not block) -----------

    def _record(self, device: str, kind: str, detail: str) -> None:
        entry = {"device": device, "kind": kind, "detail": detail}
        with self._engine_lock:
            self.activity.append(entry)
            if len(self.activity) > 200:
                del self.activity[:-200]
        self.bus.raise_event(
            Event(name=f"driver:{kind}", subject=device, attributes={"detail": detail})
        )

    def _on_driver_status(self, name: str, status: DriverStatus) -> None:
        device = self.device(name)
        if device is None:
            return
        # Push the peripheral's real condition into the protocol's view and
        # notify every session holding the device (section 30.2).
        PlatformSimulator.set_status(
            self,
            name,
            ready=status.ready,
            power_off=status.power_off,
            paper_out=status.paper_out,
            paper_jam=status.paper_jam,
            disk_error=status.disk_error,
        )
        managed = self.managed.get(name.upper())
        if managed is not None:
            if status.usable and not (status.paper_out or status.paper_jam):
                managed.cleared()
            elif not status.init:
                managed.fault(status.description or "device fault")
        self._record(name, "status", status.description or "status changed")

    def _on_driver_data(self, name: str, data: DriverData) -> None:
        managed = self.managed.get(name.upper())
        if managed is not None:
            managed.touch()
        text = data.payload.decode("latin-1", "replace")
        self._record(name, data.kind, text[:120])
        device = self.device(name)
        if device is None:
            return
        try:
            if device.device_type == "MS":
                PlatformSimulator.swipe_card(self, name, split_tracks(text))
            elif device.device_type == "OC":
                element = xmlmsg.Element("readerData")
                element.add(
                    xmlmsg.Element(
                        "ocTrackData", {"trackID": "1", "readStatus": "OK"},
                        text=text,
                    )
                )
                with device._lock:
                    device.pending_reads.append(element)
                self._notify_device(
                    name, xmlmsg.Element("notify", {"dataAvailable": "true"})
                )
            elif device.device_type in ("BC", "SD"):
                PlatformSimulator.scan_barcode(self, name, text)
        except RuntimeError as exc:
            # Nobody holds the device; the driver should have been secured.
            self._record(name, "error", str(exc))

    # -- injection: drive the *peripheral*, not the protocol ----------------

    def _bench_end(self, name: str):
        """The bench transport of a device, or None for real hardware."""
        driver = self.drivers.get(name.upper())
        if driver is not None and isinstance(driver.transport, BENCH_TRANSPORTS):
            return driver.transport
        return None

    def scan_barcode(self, device_name: str, data: str, type_code: str = "6") -> None:
        """On a bench device, send the scan as real bytes."""
        port = self._bench_end(device_name)
        if port is None:
            return super().scan_barcode(device_name, data, type_code)
        port.device_write(data.encode("latin-1") + b"\r")

    def swipe_card(self, device_name: str, tracks: dict[int, str]) -> None:
        port = self._bench_end(device_name)
        if port is None:
            return super().swipe_card(device_name, tracks)
        framed = "".join(
            ("%" if track_id == 1 else ";") + content + "?"
            for track_id, content in sorted(tracks.items())
        )
        port.device_write(framed.encode("latin-1") + b"\r")

    def set_status(self, device_name: str, **flags: bool) -> None:
        driver = self.drivers.get(device_name.upper())
        if isinstance(driver, PrintDriver) and "paper_out" in flags:
            driver.set_paper_out(bool(flags["paper_out"]))
            return
        port = self._bench_end(device_name)
        if isinstance(driver, AeaDriver) and port is not None and (
            "paper_out" in flags or "paper_jam" in flags
        ):
            word = b"ST01"
            if flags.get("paper_out"):
                word += b"P"
            if flags.get("paper_jam"):
                word += b"J"
            port.device_write(word + b"\r")
            return
        super().set_status(device_name, **flags)

    # -- sessions -----------------------------------------------------------

    def _session(self, name: str, peer) -> Optional[AcquiredSession]:
        with self._engine_lock:
            return self._sessions.get((name.upper(), peer.connection_id))

    def _release_session(self, name: str, peer) -> None:
        key = (name.upper(), peer.connection_id)
        with self._engine_lock:
            self._sessions.pop(key, None)
            still_held = any(k[0] == name.upper() for k in self._sessions)
        managed = self.managed.get(name.upper())
        if managed is not None:
            managed.release(peer.connection_id)
        driver = self.drivers.get(name.upper())
        if driver is not None and not still_held:
            # Section 10.4.1: nobody holds it, so it is secured again.
            driver.secure()

    def _lock_expired_sender(self, peer):
        def send(event: Event) -> None:
            if event.name != "deviceLockExpiredEvent":
                return
            peer.send(
                xmlmsg.build(
                    "deviceLockExpiredEvent",
                    peer.ids.allocate(),
                    body=xmlmsg.Element("deviceLockExpiredEvent"),
                    interface_level=peer.interface_level or "01.04",
                )
            )
        return send

    def _unregister_peer(self, peer, state: dict) -> None:
        device = state.get("device")
        if isinstance(device, SimulatedDevice):
            self._release_session(device.name, peer)
        elif state.get("token"):
            # A platform connection went away: section 26.7 invalidates its
            # token, and the application it belonged to is gone.
            self._tokens.discard(str(state["token"]))
            application = self.applications.pop(peer.connection_id, None)
            if application is not None:
                application.request_stop(reason="platform connection closed")
                application.stopped()
        super()._unregister_peer(peer, state)

    # -- protocol handlers backed by the engine -------------------------------

    def _on_authenticateRequest(self, peer, state, message) -> None:
        super()._on_authenticateRequest(peer, state, message)
        names = [
            element.get("applicationName") or ""
            for element in message.body.iter("application")
        ]
        application = ManagedApplication(
            peer.connection_id,
            names[0] if names and names[0] else message.body.get("airline", "APP"),
            bus=self.bus,
            clock=self.clock,
            event_token=message.body.get("eventToken", "") or "",
        )
        application.start()
        application.started()
        application.authenticated(event_token=application.event_token)
        self.applications[peer.connection_id] = application

    def _on_byeRequest(self, peer, state, message) -> None:
        super()._on_byeRequest(peer, state, message)
        application = self.applications.pop(peer.connection_id, None)
        if application is not None:
            application.request_stop(reason="byeRequest")
            application.stopped()

    def _on_deviceAcquireRequest(self, peer, state, message) -> None:
        state["airline"] = message.body.get("airlineID", "") or ""
        super()._on_deviceAcquireRequest(peer, state, message)

    def _on_interfaceModeRequest(self, peer, state, message) -> None:
        super()._on_interfaceModeRequest(peer, state, message)
        device = state.get("device")
        mode = state.get("mode")
        if not isinstance(device, SimulatedDevice) or mode is None:
            return
        managed = self.managed.get(device.name.upper())
        if managed is None:
            return
        session = AcquiredSession(
            session_id=peer.connection_id,
            device_token=str(state.get("token", "")),
            airline_id=str(state.get("airline", "")),
            interface_mode=mode,
            notify=self._lock_expired_sender(peer),
        )
        with self._engine_lock:
            self._sessions[(device.name.upper(), peer.connection_id)] = session
        managed.acquire(session)
        driver = self.drivers.get(device.name.upper())
        if driver is not None:
            # An application now holds the device, so it may be used.
            driver.unsecure()

    def _on_deviceReleaseRequest(self, peer, state, message) -> None:
        device = state.get("device")
        if isinstance(device, SimulatedDevice):
            self._release_session(device.name, peer)
        peer.reply("deviceReleaseResponse", message)

    def _on_deviceLockRequest(self, peer, state, message) -> None:
        device = state.get("device")
        if not isinstance(device, SimulatedDevice):
            peer.illogical(["deviceAcquireRequest"])
            return
        managed = self.managed.get(device.name.upper())
        session = self._session(device.name, peer)
        if managed is None or session is None:
            peer.illogical(["interfaceModeRequest"])
            return
        try:
            method = LockMethod(message.body.get("lockMethod", "byConnection"))
            result = managed.lock(session, method)
        except LockingNotSupported:
            # Section 30.20 note: locking a Special Mode device is illogical.
            peer.illogical([])
            return
        except ValueError:
            peer.illogical([])
            return
        except LockRefused as refused:
            body = xmlmsg.Element("deviceLockResponse", {"result": refused.result})
            if refused.locker:
                body.add(xmlmsg.Element("deviceLockerInfo", refused.locker))
            peer.reply("deviceLockResponse", message, result=None, body=body)
            return
        peer.reply("deviceLockResponse", message, result=result)

    def _on_deviceUnlockRequest(self, peer, state, message) -> None:
        device = state.get("device")
        if not isinstance(device, SimulatedDevice):
            peer.illogical(["deviceAcquireRequest"])
            return
        managed = self.managed.get(device.name.upper())
        session = self._session(device.name, peer)
        if managed is not None and session is not None:
            try:
                managed.unlock(session)
            except LockingNotSupported:
                peer.illogical([])
                return
        peer.reply("deviceUnlockResponse", message)

    def _touch(self, state) -> None:
        device = state.get("device")
        if isinstance(device, SimulatedDevice):
            managed = self.managed.get(device.name.upper())
            if managed is not None:
                managed.touch()

    def _on_deviceStatusRequest(self, peer, state, message) -> None:
        self._touch(state)
        super()._on_deviceStatusRequest(peer, state, message)

    def _on_readerReadRequest(self, peer, state, message) -> None:
        self._touch(state)
        super()._on_readerReadRequest(peer, state, message)

    def _on_printRequest(self, peer, state, message) -> None:
        device = state.get("device")
        driver = (
            self.drivers.get(device.name.upper())
            if isinstance(device, SimulatedDevice) else None
        )
        if not isinstance(driver, PrintDriver):
            super()._on_printRequest(peer, state, message)
            return
        managed = self.managed[device.name.upper()]
        body = xmlmsg.Element("printResponse")
        any_failure = False
        for document in message.body.findall("printDocument"):
            document_id = document.get("documentID", "0") or "0"
            stock = document.get("stockName", "") or ""
            payload = _document_bytes(document)
            managed.begin_operation()
            try:
                outcome = driver.print_document(
                    payload, stock=stock, job_name=f"doc{document_id}"
                )
                result = outcome.result
            except DeviceSecured:
                result = "notReady"
            finally:
                managed.end_operation()
            if result == "timeout":
                result = "notReady"
            any_failure = any_failure or result != "OK"
            self._record(device.name, "print", f"document {document_id} on {stock}: {result}")
            self.printed.append(
                {"device": device.name, "documentID": document_id,
                 "stockName": stock, "bytes": len(payload), "result": result}
            )
            body.add(xmlmsg.Element(
                "printDocumentResult", {"documentID": document_id, "result": result}
            ))
        body.attrs["result"] = "oneOrMoreIssues" if any_failure else "OK"
        peer.reply("printResponse", message, result=None, body=body)

    def _on_aeaRequest(self, peer, state, message) -> None:
        device = state.get("device")
        driver = (
            self.drivers.get(device.name.upper())
            if isinstance(device, SimulatedDevice) else None
        )
        if not isinstance(driver, AeaDriver):
            super()._on_aeaRequest(peer, state, message)
            return
        stream = bytearray()
        for aea_message in message.body.findall("aeaMessage"):
            for child in aea_message.children:
                if child.name == "aeaText":
                    stream += (child.text or "").encode("latin-1", "replace")
                elif child.name == "aeaBinary" and child.text:
                    stream += base64.b64decode("".join(child.text.split()))
        self._touch(state)
        self.aea_commands.append(stream.decode("latin-1", "replace"))
        try:
            printable = all(0x20 <= b <= 0x7E for b in stream)
            if printable and not stream.endswith(driver.terminator):
                # A single command from the application: frame it.
                driver.send_command(bytes(stream))
            else:
                # A host stream carries its own framing: pass it through.
                driver.send_stream(bytes(stream))
            result = "OK"
        except (DeviceSecured, TransportError) as exc:
            self._record(device.name, "error", f"AEA send failed: {exc}")
            result = "notReady"
        self._record(device.name, "aea", stream.decode("latin-1", "replace")[:80])
        peer.reply("aeaResponse", message, result=result)

    # -- inspection -----------------------------------------------------------

    def snapshot(self) -> dict:
        """The whole platform's state, for the console."""
        with self._engine_lock:
            sessions = list(self._sessions.items())
            activity = list(self.activity[-60:])
        devices = []
        for device in self.devices:
            managed = self.managed.get(device.name.upper())
            driver = self.drivers.get(device.name.upper())
            holder = managed.holder if managed else None
            devices.append({
                "name": device.name,
                "type": device.device_type,
                "state": managed.state.value if managed else "-",
                "locked": bool(managed and managed.locked),
                "lockMethod": holder.method.value if holder else None,
                "acquiredBy": sum(1 for (n, _), _ in sessions if n == device.name.upper()),
                "transport": driver.transport.describe() if driver else "software device",
                "driver": type(driver).__name__ if driver else "platform",
                "secured": driver.secured if driver else None,
                "discarded": driver.discarded_while_secured if driver else 0,
                "status": {
                    "ready": device.ready, "powerOff": device.power_off,
                    "paperOut": device.paper_out, "paperJam": device.paper_jam,
                },
                "bench": self._bench_end(device.name) is not None
                or isinstance(driver, PrintDriver),
            })
        return {
            "platform": {
                "state": self.machine.state.value,
                "workstation": self.workstation,
                "workstationState": self.workstation_object.state.value,
                "platformPort": self.platform_port,
                "devicePort": self.device_port,
            },
            "applications": [
                {"id": app_id, "name": app.application_name,
                 "state": app.state.value}
                for app_id, app in sorted(self.applications.items())
            ],
            "devices": devices,
            "activity": activity,
        }


def _document_bytes(document: xmlmsg.Element) -> bytes:
    """The payload of a <printDocument>: PDF, or reassembled simple text."""
    pdf = document.find("pdfPrintDocument")
    if pdf is not None and pdf.text:
        return base64.b64decode("".join(pdf.text.split()))
    text_document = document.find("simpleTextPrintDocument")
    chunks: list[bytes] = []
    if text_document is not None:
        for node in text_document.iter():
            if node.name == "simpleTextPrintDocumentTextNode":
                chunks.append((node.text or "").encode("utf-8"))
            elif node.name == "simpleTextPrintDocumentBinaryNode" and node.text:
                chunks.append(base64.b64decode("".join(node.text.split())))
    return b"".join(chunks)


def bench_bindings(print_dir: Path, kind: str = DEFAULT_BENCH_KIND) -> BindingRegistry:
    """Bench peripherals, for a demonstration with no hardware.

    Each is driven by the real driver over a real operating-system channel
    (a pseudo-terminal, or a socket pair on Windows); only the far end of
    the wire is played by the console instead of a peripheral.
    """
    from .drivers import DeviceBinding

    transport = {"kind": kind}
    registry = BindingRegistry()
    for payload in (
        {"device": "CUPPSPLT001BC1", "deviceType": "BC", "transport": dict(transport)},
        {"device": "CUPPSPLT001MS1", "deviceType": "MS", "transport": dict(transport)},
        {"device": "CUPPSPLT001BP1", "deviceType": "BP", "transport": dict(transport)},
        {"device": "CUPPSPLT001PR1", "deviceType": "PR", "transport": dict(transport),
         "options": {"backend": "file",
                     "backend_options": {"directory": str(print_dir)}}},
    ):
        registry.add(DeviceBinding.from_dict(payload))
    return registry
