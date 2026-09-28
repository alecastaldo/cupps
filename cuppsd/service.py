"""The CUPPS device handler.

This is architecture (b) of Figure 9.4: a single component that owns every
CUPPS session -- the one platform connection and one connection per device --
and presents them to the rest of the application through a local API.  Keeping
all CUPPS state in one process is what lets the agent UI be a thin client and
what makes the token lifecycle of section 26.7 tractable: when the platform
connection drops, one component rebuilds everything.

Responsibilities:

* Open and hold the platform session, and re-establish it after a drop.
* Acquire the devices the application declares, in the right interface mode.
* Track device status from the asynchronous ``<notify>`` stream rather than
  polling (section 30.2 requirement).
* Surface scans, swipes, status changes and faults as events.
* Honour a platform-directed stop, including the deferral allowance.
* Write application activity to the ZL logging device.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Iterable, Optional

from cupps import (
    BarcodeRead,
    CuppsEnvironment,
    CuppsError,
    Device,
    DeviceLocked,
    DeviceSession,
    DeviceStatus,
    InterfaceMode,
    LockMethod,
    LogDevice,
    LogSeverity,
    PlatformSession,
    PrintDocument,
    Printer,
    Reader,
    RequestFailed,
    RuntimeEnvironment,
    TokenInvalidated,
    TrackRead,
    params,
    parse_bcbp,
)
from cupps.bcbp import BcbpError
from cupps.errors import ConnectionClosed

from . import aea as aea_helpers
from .deviceprofile import DeviceProfile, ProfileRegistry, default_profile

log = logging.getLogger("cuppsd.service")

#: Device types the handler opens automatically when the platform offers them,
#: with the interface mode each requires (Table 30.1).
DEFAULT_DEVICE_MODES = {
    "BC": InterfaceMode.STANDARD,
    "MS": InterfaceMode.STANDARD,
    "OC": InterfaceMode.STANDARD,
    "PR": InterfaceMode.STANDARD,
    "BP": InterfaceMode.AEA,
    "BT": InterfaceMode.AEA,
    "BG": InterfaceMode.AEA,
    "SD": InterfaceMode.STANDARD,
    "ZL": InterfaceMode.SPECIAL,
}


class AppState(str, Enum):
    """Application states of section 9.1, as this handler observes them."""

    ATH = "aAth"   # authenticating with the platform
    CTS = "aCts"   # connecting to services
    STD = "aStd"   # steady state, in normal operation
    SPG = "aSpg"   # stopping
    STP = "aStp"   # stopped
    ZOM = "aZom"   # failed to stop cleanly


@dataclass
class Event:
    """Something the UI or another consumer needs to know about."""

    kind: str
    payload: dict[str, Any] = field(default_factory=dict)
    at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(
            timespec="seconds"
        )
    )

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "at": self.at, **self.payload}


@dataclass
class DeviceHandle:
    """One acquired device and the helpers bound to it."""

    device: Device
    session: DeviceSession
    mode: InterfaceMode
    reader: Optional[Reader] = None
    printer: Optional[Printer] = None
    log_device: Optional[LogDevice] = None
    last_error: str = ""
    #: Peripheral behaviour resolved from the profile catalogue.
    profile: Optional[DeviceProfile] = None

    @property
    def name(self) -> str:
        return self.device.name

    @property
    def device_type(self) -> str:
        return self.device.device_type

    @property
    def status(self) -> DeviceStatus:
        return self.session.status

    def status_summary(self) -> str:
        """One line for the agent's device panel, adjusted for this device.

        Section 30.2 notes that not every printer can tell a jam from an empty
        paper path. Telling an agent to clear a jam on a device that cannot
        actually detect one sends them looking for a fault that may not exist,
        so such a device reports the ambiguity instead.
        """
        status = self.status
        behaviour = (self.profile or default_profile(self.device_type)).status
        if (
            status.paper_jam
            and not behaviour.reports_paper_jam_independently
        ):
            return "Check the paper path (jam or out of paper)"
        summary = status.summary(self.device_type)
        if status.power_off and behaviour.retains_error_across_power_cycle:
            summary += "; this device keeps its error across a power cycle"
        return summary

    def to_dict(self) -> dict[str, Any]:
        status = self.status
        return {
            "name": self.name,
            "type": self.device_type,
            "typeName": self.device.type_name,
            "mode": self.mode.value,
            "vendor": self.device.vendor,
            "model": self.device.model,
            "locked": self.session.locked,
            "lockable": self.device.is_lockable,
            "lockMethod": (
                self.session.lock_method.value if self.session.lock_method else None
            ),
            "summary": self.status_summary(),
            "profile": self.profile.profile_id if self.profile else None,
            "profileVerified": bool(self.profile and self.profile.verified),
            "usable": status.usable,
            "flags": status.flags_for(self.device_type),
            "stocks": [stock.stock_name for stock in self.device.stocks],
            "lastError": self.last_error,
        }


@dataclass
class ServiceConfig:
    """How this handler connects and identifies itself."""

    airline: str
    application_name: str = "CUPPSAGENT"
    application_version: str = "01.00"
    event_token: str = ""
    #: Device types to acquire. Defaults to everything in DEFAULT_DEVICE_MODES
    #: that the platform actually offers.
    device_types: tuple[str, ...] = tuple(DEFAULT_DEVICE_MODES)
    platform_node: str = ""
    platform_port: int = 0
    #: ZL log identifier and the token needed to read entries back (30.20.1).
    log_id: str = "1"
    log_security_token: str = ""
    #: Seconds between reconnection attempts after the platform drops.
    reconnect_delay: float = 5.0
    airline_name: str = ""
    #: Extra directories of device profiles, searched after the shipped
    #: catalogue so a site can override without editing shipped files.
    profile_directories: tuple[str, ...] = ()

    @classmethod
    def from_environment(
        cls, environment: CuppsEnvironment, **overrides: Any
    ) -> "ServiceConfig":
        """Build from the CUPPS environment variables (section 6.3.15)."""
        config = cls(
            airline=environment.airline or overrides.pop("airline", ""),
            platform_node=environment.platform_node,
            platform_port=environment.platform_port,
        )
        for key, value in overrides.items():
            setattr(config, key, value)
        if not config.event_token:
            config.event_token = _make_event_token(config.application_name)
        if not config.log_security_token:
            config.log_security_token = config.event_token
        return config


def _make_event_token(seed: str) -> str:
    """A 16-character token of the shape the spec's examples use.

    Section 30.20 leaves token generation to the application supplier; this is
    stable for a given application name so successive runs can read back their
    own log history, which is the behaviour section 30.20 describes.
    """
    import hashlib

    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest().upper()
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    return "".join(alphabet[int(digest[i : i + 2], 16) % len(alphabet)] for i in range(0, 32, 2))


class CuppsService:
    """Owns every CUPPS session for one application instance."""

    def __init__(self, config: ServiceConfig) -> None:
        self.config = config
        self.state = AppState.STP
        self.platform: Optional[PlatformSession] = None
        self.runtime: Optional[RuntimeEnvironment] = None
        self.devices: dict[str, DeviceHandle] = {}
        self.last_error: str = ""

        self._subscribers: list[queue.Queue] = []
        self._subscribers_lock = threading.Lock()
        self._history: list[Event] = []
        self._state_lock = threading.RLock()
        self._stop = threading.Event()
        self._supervisor: Optional[threading.Thread] = None
        self._stop_deferrals = 0
        self._zl: Optional[DeviceHandle] = None
        self.profiles = ProfileRegistry.load(
            *(Path(d) for d in config.profile_directories)
        )
        unverified = [p.profile_id for p in self.profiles.unverified]
        if unverified:
            log.info(
                "%d device profile(s) are unverified against real hardware: %s",
                len(unverified),
                ", ".join(unverified),
            )

    # -- events -----------------------------------------------------------

    def subscribe(self) -> queue.Queue:
        """Register for the event stream; returns a queue of :class:`Event`."""
        channel: queue.Queue = queue.Queue(maxsize=256)
        with self._subscribers_lock:
            self._subscribers.append(channel)
        return channel

    def unsubscribe(self, channel: queue.Queue) -> None:
        with self._subscribers_lock:
            if channel in self._subscribers:
                self._subscribers.remove(channel)

    def emit(self, kind: str, **payload: Any) -> Event:
        """Publish an event to every subscriber and the recent history."""
        event = Event(kind=kind, payload=payload)
        with self._state_lock:
            self._history.append(event)
            # Bounded so a long-running position cannot grow without limit.
            if len(self._history) > 500:
                del self._history[: len(self._history) - 500]
        with self._subscribers_lock:
            subscribers = list(self._subscribers)
        for channel in subscribers:
            try:
                channel.put_nowait(event)
            except queue.Full:
                # A stalled consumer must not block device handling.
                pass
        return event

    def history(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._state_lock:
            return [event.to_dict() for event in self._history[-limit:]]

    # -- lifecycle --------------------------------------------------------

    def start(self) -> None:
        """Start the supervisor thread, which keeps the sessions up."""
        if self._supervisor and self._supervisor.is_alive():
            return
        self._stop.clear()
        self._supervisor = threading.Thread(
            target=self._supervise, name="cuppsd-supervisor", daemon=True
        )
        self._supervisor.start()

    def stop(self, *, timeout: float = params.APP_SPG_TIME) -> None:
        """Shut down cleanly: release devices, say goodbye, close sockets."""
        self._stop.set()
        self._set_state(AppState.SPG)
        self._teardown()
        if self._supervisor:
            self._supervisor.join(timeout=timeout)
        self._set_state(AppState.STP)

    def _supervise(self) -> None:
        """Keep the platform session and device sessions alive."""
        while not self._stop.is_set():
            try:
                self._connect()
                self._set_state(AppState.STD)
                # Steady state: the reader threads inside each session deliver
                # notifications, so this loop only watches for a drop.
                while not self._stop.is_set():
                    if self.platform is None or not self.platform.connected:
                        raise ConnectionClosed("platform connection lost")
                    self._stop.wait(1.0)
            except Exception as exc:
                if self._stop.is_set():
                    break
                self.last_error = str(exc)
                log.warning("session lost: %s", exc)
                self.emit("platformDisconnected", reason=str(exc))
                self._teardown()
                self._set_state(AppState.ATH)
                self._stop.wait(self.config.reconnect_delay)

    def _connect(self) -> None:
        self._set_state(AppState.ATH)
        platform = PlatformSession(
            self.config.platform_node,
            self.config.platform_port,
            airline=self.config.airline,
            event_token=self.config.event_token,
            applications=[
                (self.config.application_name, self.config.application_version)
            ],
        )
        platform.add_notification_handler(self._on_platform_notification)
        runtime = platform.open()

        self.platform = platform
        self.runtime = runtime
        self.last_error = ""
        self.emit(
            "platformConnected",
            workstation=runtime.workstation.name,
            location=runtime.workstation.location_description,
            platformVendor=runtime.platform.vendor,
            platformVersion=runtime.platform.version,
            interfaceLevel=platform.interface_level,
            deviceCount=len(list(runtime.all_devices())),
        )

        self._set_state(AppState.CTS)
        self._acquire_devices()
        self._open_log()
        self.log_activity(
            f"{self.config.application_name} {self.config.application_version} "
            f"connected to {runtime.platform.vendor} platform on "
            f"{runtime.workstation.name}"
        )

    def _acquire_devices(self) -> None:
        assert self.runtime is not None and self.platform is not None
        token = self.platform.device_token

        for device_type in self.config.device_types:
            preferred = DEFAULT_DEVICE_MODES.get(device_type.upper())
            if preferred is None:
                continue
            for device in self.runtime.of_type(device_type):
                if device.name in self.devices:
                    continue
                # Only open a mode the device actually advertises: asking for
                # one it does not support draws modeNotSupportedForThisDevice.
                # Resolved per device, because two devices of one type may
                # advertise different modes.
                mode = preferred
                if device.modes and mode not in device.modes:
                    mode = next(iter(device.modes), None)
                    if mode is None:
                        continue
                try:
                    self._acquire_one(device, mode, token)
                except CuppsError as exc:
                    log.warning("could not acquire %s: %s", device.name, exc)
                    self.emit(
                        "deviceUnavailable", device=device.name, reason=str(exc)
                    )

    def _acquire_one(
        self, device: Device, mode: InterfaceMode, token: str
    ) -> DeviceHandle:
        session = DeviceSession(
            device,
            device_token=token,
            airline_id=self.config.airline,
            mode=mode,
        )
        acquired = session.open()
        profile = self.profiles.resolve(acquired)
        handle = DeviceHandle(
            device=acquired, session=session, mode=mode, profile=profile
        )

        if profile.timing.warmup_seconds:
            time.sleep(profile.timing.warmup_seconds)

        if mode is InterfaceMode.AEA and profile.aea.opening_commands:
            # Sent after the mandatory EP, which cupps.session already issued.
            for command in profile.aea.opening_commands:
                try:
                    session.aea(*aea_helpers.segment(command))
                except CuppsError as exc:
                    handle.last_error = str(exc)
                    log.warning(
                        "%s rejected opening command from profile %s: %s",
                        acquired.name, profile.profile_id, exc,
                    )

        if acquired.is_reader:
            handle.reader = Reader(session, device_token=token)
        if acquired.device_type == "PR" and mode is InterfaceMode.STANDARD:
            handle.printer = Printer(session)
        if acquired.device_type == "ZL":
            handle.log_device = LogDevice(session)
            self._zl = handle

        session.add_notification_handler(
            lambda message, name=acquired.name: self._on_device_notification(
                name, message
            )
        )
        self.devices[acquired.name] = handle
        self.emit("deviceAcquired", **handle.to_dict())
        return handle

    def _open_log(self) -> None:
        if self._zl is None or self._zl.log_device is None:
            return
        try:
            self._zl.log_device.open(
                self.config.log_id, self.config.log_security_token
            )
        except CuppsError as exc:
            log.warning("could not open the ZL log: %s", exc)
            self._zl = None

    def _teardown(self) -> None:
        for handle in list(self.devices.values()):
            try:
                if handle.log_device is not None:
                    handle.log_device.close_all()
                handle.session.close()
            except Exception:  # pragma: no cover - best effort on shutdown
                log.debug("error closing %s", handle.name, exc_info=True)
        self.devices.clear()
        self._zl = None

        platform = self.platform
        self.platform = None
        if platform is not None:
            try:
                platform.close()
            except Exception:  # pragma: no cover
                log.debug("error closing the platform session", exc_info=True)

    def _set_state(self, state: AppState) -> None:
        with self._state_lock:
            if self.state is state:
                return
            previous, self.state = self.state, state
        self.emit("stateChanged", state=state.value, previous=previous.value)

    # -- notifications ----------------------------------------------------

    def _on_platform_notification(self, message) -> None:
        name = message.message_name
        if name == "applicationStopCommandRequest":
            self._handle_stop_command(message)
            return
        self.emit("platformMessage", messageName=name)

    def _handle_stop_command(self, message) -> None:
        """Respond to a platform-directed stop (section 29.2).

        An application may defer up to ``MaxSpgDeferTimes`` to finish a host
        or device transaction; beyond that it must go.
        """
        busy = any(handle.session.locked for handle in self.devices.values())
        if busy and self._stop_deferrals < params.MAX_SPG_DEFER_TIMES:
            self._stop_deferrals += 1
            self.emit(
                "stopDeferred",
                deferral=self._stop_deferrals,
                allowed=params.MAX_SPG_DEFER_TIMES,
            )
            self._respond_to_stop(message, defer=True)
            return
        self.emit("stopRequested", deferrals=self._stop_deferrals)
        self._respond_to_stop(message, defer=False)
        threading.Thread(target=self.stop, name="cuppsd-stop", daemon=True).start()

    def _respond_to_stop(self, message, *, defer: bool) -> None:
        from cupps import xmlmsg

        platform = self.platform
        if platform is None:
            return
        body = xmlmsg.Element(
            "applicationStopCommandResponse",
            {"result": "defer" if defer else "OK"},
        )
        response = xmlmsg.build(
            "applicationStopCommandResponse",
            message.message_id,
            body=body,
            interface_level=platform.interface_level,
        )
        try:
            platform._connection.send_notification(response)  # noqa: SLF001
        except CuppsError as exc:
            log.warning("could not answer the stop command: %s", exc)

    def _on_device_notification(self, device_name: str, message) -> None:
        handle = self.devices.get(device_name)
        if handle is None:
            return
        name = message.message_name

        if name == "deviceLockExpiredEvent":
            self.emit("lockExpired", device=device_name)
            return
        if name != "notify":
            return

        if message.body.find("deviceStatusNotification") is not None:
            self.emit("deviceStatus", **handle.to_dict())
            return

        if message.body.get_bool("dataAvailable"):
            # Read on a worker so the session's reader thread stays free to
            # take the response and any further notifications.
            threading.Thread(
                target=self._collect_data,
                args=(device_name,),
                name=f"cuppsd-read-{device_name}",
                daemon=True,
            ).start()

    def _collect_data(self, device_name: str) -> None:
        handle = self.devices.get(device_name)
        if handle is None or handle.reader is None:
            return
        try:
            if handle.device_type in ("BC", "BG"):
                for read in handle.reader.read_barcodes():
                    self._publish_barcode(handle, read)
            elif handle.device_type == "MS":
                tracks = handle.reader.read_tracks()
                self._publish_tracks(handle, tracks)
            elif handle.device_type == "OC":
                for read in handle.reader.read_ocr():
                    self.emit(
                        "documentRead",
                        device=handle.name,
                        trackID=read.track_id,
                        text=read.text,
                    )
        except CuppsError as exc:
            handle.last_error = str(exc)
            self.emit("deviceError", device=handle.name, reason=str(exc))

    def _publish_barcode(self, handle: DeviceHandle, read: BarcodeRead) -> None:
        payload: dict[str, Any] = {
            "device": handle.name,
            "symbology": read.symbology,
            "typeCode": read.type_code,
            "readStatus": read.read_status,
            "text": read.text,
        }
        try:
            boarding_pass = parse_bcbp(read.text)
        except BcbpError:
            # Not a boarding pass: a bag tag, a document or a loyalty card.
            self.emit("barcodeScanned", **payload)
            return

        leg = boarding_pass.first_leg
        payload["boardingPass"] = {
            "name": boarding_pass.display_name,
            "pnr": leg.pnr if leg else "",
            "flight": leg.flight if leg else "",
            "origin": leg.origin if leg else "",
            "destination": leg.destination if leg else "",
            "seat": leg.seat if leg else "",
            "cabin": leg.cabin if leg else "",
            "sequence": leg.sequence_number if leg else "",
            "status": leg.status_text if leg else "",
            "checkedIn": leg.checked_in if leg else False,
            "selectee": leg.is_selectee if leg else False,
            "date": str(leg.flight_date()) if leg and leg.flight_date() else "",
            "legs": len(boarding_pass.legs),
            "electronicTicket": boarding_pass.electronic_ticket,
        }
        self.emit("boardingPassScanned", **payload)
        self.log_activity(
            f"Boarding pass scanned: {boarding_pass.display_name} "
            f"{payload['boardingPass']['flight']} seat "
            f"{payload['boardingPass']['seat']}"
        )

    def _publish_tracks(
        self, handle: DeviceHandle, tracks: list[TrackRead]
    ) -> None:
        """Publish a card read.

        Track content is deliberately *not* put on the event bus or into the
        ZL log: it may be cardholder data, and section 6.3.12 warns that what
        an application asks to be logged is its own responsibility under PCI
        DSS.  Only the shape of the read is published.
        """
        self.emit(
            "cardRead",
            device=handle.name,
            tracks=[
                {"trackID": track.track_id, "length": len(track.data)}
                for track in tracks
            ],
        )

    # -- operations the UI calls -----------------------------------------

    def snapshot(self) -> dict[str, Any]:
        """Everything a client needs to render the current position."""
        runtime = self.runtime
        return {
            "state": self.state.value,
            "connected": bool(self.platform and self.platform.connected),
            "airline": self.config.airline,
            "airlineName": self.config.airline_name,
            "application": self.config.application_name,
            "applicationVersion": self.config.application_version,
            "lastError": self.last_error,
            "workstation": runtime.workstation.name if runtime else "",
            "location": runtime.workstation.location_description if runtime else "",
            "platformVendor": runtime.platform.vendor if runtime else "",
            "platformVersion": runtime.platform.version if runtime else "",
            "interfaceLevel": self.platform.interface_level if self.platform else "",
            "devices": [handle.to_dict() for handle in self.devices.values()],
        }

    def device(self, name: str) -> DeviceHandle:
        handle = self.devices.get(name)
        if handle is None:
            raise KeyError(f"device {name!r} is not acquired")
        return handle

    def first_of_type(self, device_type: str) -> Optional[DeviceHandle]:
        for handle in self.devices.values():
            if handle.device_type == device_type.upper():
                return handle
        return None

    def lock(self, name: str, method: LockMethod = LockMethod.BY_CONNECTION) -> str:
        handle = self.device(name)
        result = handle.session.lock(method)
        self.emit("deviceLocked", device=name, result=result)
        return result

    def unlock(self, name: str) -> None:
        handle = self.device(name)
        handle.session.unlock()
        self.emit("deviceUnlocked", device=name)

    def refresh_status(self, name: str) -> dict[str, Any]:
        """Poll a device's status (section 30.2).

        Bounded by ``DevPollMaxFreq``; the handler normally relies on the
        notification stream instead, so this is for the device-test screen.
        """
        handle = self.device(name)
        handle.session.request_status()
        payload = handle.to_dict()
        self.emit("deviceStatus", **payload)
        return payload

    def print_documents(
        self, name: str, documents: Iterable[PrintDocument]
    ) -> list[dict[str, Any]]:
        """Print through a Standard Mode PR device, taking the lock first."""
        handle = self.device(name)
        if handle.printer is None:
            raise CuppsError(
                f"{name} is a {handle.device_type} device in "
                f"{handle.mode.value} mode; Standard Mode printing needs a PR "
                f"device (Table 3.3 note 5)"
            )
        documents = list(documents)
        profile = handle.profile or default_profile(handle.device_type)
        # Stock names are site configuration, not a standard (section 30.15.3).
        for document in documents:
            document.stock_name = profile.stocks.resolve(document.stock_name)
        took_lock = False
        try:
            if not handle.session.locked:
                handle.session.lock()
                took_lock = True
            outcomes = handle.printer.print_documents(documents)
        except DeviceLocked as exc:
            self.emit("deviceBusy", device=name, locker=exc.locker)
            raise
        finally:
            if took_lock and handle.session.locked:
                try:
                    handle.session.unlock()
                except CuppsError:
                    log.debug("could not unlock %s after printing", name)

        results = [
            {"documentID": outcome.document_id, "result": outcome.result,
             "ok": outcome.ok}
            for outcome in outcomes
        ]
        self.emit("printed", device=name, results=results)
        self.log_activity(
            f"Printed {len(documents)} document(s) on {name}: "
            + ", ".join(f"{r['documentID']}={r['result']}" for r in results)
        )
        return results

    def send_aea(self, name: str, stream: str) -> None:
        """Pass a host-supplied AEA stream to a BP, BT or BG device."""
        handle = self.device(name)
        if handle.mode is not InterfaceMode.AEA:
            raise CuppsError(
                f"{name} is in {handle.mode.value} mode; AEA commands need an "
                f"AEA Mode session"
            )
        # A BG in AEA Mode is not shareable (Figure 30.1 (c)), so it is held
        # for the exchange; other AEA devices are shareable (Figure 30.1 (d))
        # and taking a lock would only block other applications needlessly.
        needs_lock = handle.device_type == "BG" and not handle.session.locked
        if needs_lock:
            handle.session.lock()
        try:
            handle.session.aea(*aea_helpers.segment(stream))
        finally:
            if needs_lock and handle.session.locked:
                try:
                    handle.session.unlock()
                except CuppsError:
                    log.debug("could not unlock %s after an AEA exchange", name)
        self.emit("aeaSent", device=name, bytes=len(stream))

    def log_activity(
        self, text: str, severity: LogSeverity = LogSeverity.NORMAL
    ) -> None:
        """Write to the platform's ZL log, if one is open.

        Failures here are logged locally and swallowed: losing an audit line
        must never take down passenger processing.
        """
        handle = self._zl
        if handle is None or handle.log_device is None:
            return
        try:
            handle.log_device.write_one(self.config.log_id, text, severity)
        except CuppsError as exc:
            log.debug("ZL write failed: %s", exc)

    def read_log(self) -> list[str]:
        handle = self._zl
        if handle is None or handle.log_device is None:
            return []
        try:
            return handle.log_device.retrieve(
                self.config.log_id, self.config.log_security_token
            )
        except CuppsError as exc:
            log.warning("could not retrieve the ZL log: %s", exc)
            return []

    def test_device(self, name: str) -> dict[str, Any]:
        """Run the device test the CUPPSIT tool exposes (section 11.2.5).

        Printers actually print, readers actually wait for data: section 11.2.1
        requires a test to exercise the device rather than just query it.
        """
        handle = self.device(name)
        started = time.monotonic()
        try:
            handle.session.request_status()
        except RequestFailed as exc:
            return {
                "device": name,
                "type": handle.device_type,
                "result": exc.result,
                "detail": str(exc),
            }

        result = "OK"
        detail = handle.status.summary(handle.device_type)

        if handle.device_type == "PR" and handle.printer is not None:
            from .documents import DEFAULT_STOCK_SIZES

            stock = next(
                (s.stock_name for s in handle.device.stocks),
                next(iter(DEFAULT_STOCK_SIZES)),
            )
            outcomes = self.print_documents(
                name,
                [
                    PrintDocument(
                        document_id=1,
                        stock_name=stock,
                        text=(
                            f"CUPPS DEVICE TEST\r\n{name}\r\n"
                            f"{datetime.now(timezone.utc).isoformat(timespec='seconds')}\r\n"
                        ),
                    )
                ],
            )
            result = outcomes[0]["result"] if outcomes else "OK"
            detail = f"test page sent on stock {stock}"

        return {
            "device": name,
            "type": handle.device_type,
            "result": result,
            "detail": detail,
            "elapsed": round(time.monotonic() - started, 3),
        }
