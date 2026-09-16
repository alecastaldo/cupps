"""Platform and device conversations (TS 01.04.0004 sections 26.6, 30.6).

Two session types implement the prototypical conversations of Figures 26.2
and 26.3:

``PlatformSession``
    handshake -> ``<authenticateRequest>`` -> device token -> platform use ->
    ``<byeRequest>`` -> close.  Exactly one per application instance: section
    26.6 is emphatic that ``<authenticateRequest>`` is sent ONCE and only on
    the platform connection.

``DeviceSession``
    handshake -> ``<deviceAcquireRequest>`` (authenticating with the device
    token) -> ``<interfaceModeRequest>`` -> device use ->
    ``<deviceReleaseRequest>`` -> close.  One per device per airline.

Both are context managers so the required closing handshake happens even on
an exception path.
"""

from __future__ import annotations

import logging
import threading
from typing import Callable, Iterable, Optional

from . import params, results, xmlmsg
from .errors import (
    ConnectionClosed,
    CuppsError,
    DeviceLocked,
    RequestFailed,
    TokenInvalidated,
)
from .model import Device, DeviceStatus, RuntimeEnvironment
from .transport import Connection

log = logging.getLogger("cupps.session")

#: Interface levels this client implements, best first.
SUPPORTED_INTERFACE_LEVELS = ("01.04", "01.03", "01.01")

#: Version of the handshake schema this client speaks (Listing 28.1).
HANDSHAKE_XSD_VERSION = "01.00.0120"

NotificationHandler = Callable[[xmlmsg.Message], None]


class _BaseSession:
    """Shared handshake and request plumbing for both session types."""

    def __init__(
        self,
        host: str,
        port: int,
        *,
        name: str,
        outstanding_limit: int = params.PLT_STREAM_OUT_MSGS,
    ) -> None:
        self._connection = Connection(
            host, port, outstanding_limit=outstanding_limit, name=name
        )
        self.interface_level: str = ""
        self._notification_handlers: list[NotificationHandler] = []
        self._connection.on_notification = self._on_notification

    # -- notifications ----------------------------------------------------

    def add_notification_handler(self, handler: NotificationHandler) -> None:
        """Register a callback for unsolicited platform messages.

        Handlers run on the reader thread, so they must not block: hand work
        to the application's own queue and return.
        """
        self._notification_handlers.append(handler)

    def _on_notification(self, message: xmlmsg.Message) -> None:
        for handler in list(self._notification_handlers):
            try:
                handler(message)
            except Exception:  # pragma: no cover - handler is app code
                log.exception("notification handler raised")

    # -- plumbing ---------------------------------------------------------

    @property
    def connected(self) -> bool:
        return self._connection.connected

    def _request(
        self,
        message_name: str,
        *,
        attrs: Optional[dict] = None,
        body: Optional[xmlmsg.Element] = None,
        timeout: float,
        expect_ok: bool = True,
    ) -> xmlmsg.Message:
        message = xmlmsg.build(
            message_name,
            self._connection.allocate_message_id(),
            body=body,
            attrs=attrs,
            interface_level=self.interface_level or "01.04",
        )
        response = self._connection.send_request(message, timeout=timeout)
        if expect_ok and not response.is_ok:
            raise RequestFailed(message_name, response.result or "<missing>")
        return response

    def _handshake(self) -> None:
        """Negotiate the interface level (sections 26.4, 28.2, 28.3).

        The first action on any connection, platform or device alike.
        """
        available = self._request(
            "interfaceLevelsAvailableRequest",
            attrs={"hsXsdVersion": HANDSHAKE_XSD_VERSION},
            timeout=params.INT_LVL_MAX_TIME,
        )

        offered: list[str] = []
        for element in available.body.findall("interfaceLevel"):
            level = element.get("level")
            if level:
                offered.append(level)

        chosen = next(
            (level for level in SUPPORTED_INTERFACE_LEVELS if level in offered),
            None,
        )
        if chosen is None:
            # Section 28.3: log it, tell the end-user, and exit. Raising here
            # lets the caller do all three.
            raise CuppsError(
                "no mutually supported interface level; platform offers "
                f"{offered or ['<none>']}, this application implements "
                f"{list(SUPPORTED_INTERFACE_LEVELS)}"
            )

        self._request(
            "interfaceLevelRequest",
            attrs={"level": chosen},
            timeout=params.INT_LVL_MAX_TIME,
        )
        self.interface_level = chosen
        self._connection.interface_level = chosen
        log.info("%s negotiated interface level %s", self._connection.name, chosen)

    def close(self) -> None:
        self._connection.close()


class PlatformSession(_BaseSession):
    """The application's single connection to the CUPPS platform."""

    def __init__(
        self,
        host: str,
        port: int,
        *,
        airline: str,
        event_token: str,
        applications: Iterable[tuple[str, str]] = (),
        platform_defined_parameter: str = "",
    ) -> None:
        super().__init__(host, port, name="platform")
        self.airline = airline
        self.event_token = event_token
        self.applications = list(applications)
        self.platform_defined_parameter = platform_defined_parameter
        self.environment: Optional[RuntimeEnvironment] = None
        self._said_bye = False

    @property
    def device_token(self) -> str:
        """The token issued at authentication, for device acquisition.

        Valid only while this platform connection lives (section 26.7).
        """
        if self.environment is None:
            raise TokenInvalidated("not authenticated yet")
        if not self._connection.connected or self._said_bye:
            raise TokenInvalidated(
                "the platform connection has ended; the device token is "
                "invalidated and every device session must be rebuilt"
            )
        return self.environment.device_token

    def open(self) -> RuntimeEnvironment:
        """Connect, handshake and authenticate; returns the run-time picture."""
        self._connection.connect()
        self._handshake()
        return self._authenticate()

    def _authenticate(self) -> RuntimeEnvironment:
        body = xmlmsg.Element(
            "authenticateRequest",
            {
                "airline": self.airline,
                "eventToken": self.event_token,
            },
        )
        if self.platform_defined_parameter:
            body.attrs["platformDefinedParameter"] = self.platform_defined_parameter
        if self.applications:
            application_list = body.add(xmlmsg.Element("applicationList"))
            for app_name, app_version in self.applications:
                application_list.add(
                    xmlmsg.Element(
                        "application",
                        {
                            "applicationName": app_name,
                            "applicationVersion": app_version,
                        },
                    )
                )

        response = self._request(
            "authenticateRequest",
            body=body,
            timeout=params.AUTH_REQ_MAX_TIME,
            expect_ok=False,
        )
        # Section 29.3: the platform always supplies a deviceToken, even on
        # failure, so the result attribute is the only reliable verdict.
        if not response.is_ok:
            raise RequestFailed(
                "authenticateRequest",
                response.result or "<missing>",
                "platform refused authentication",
            )
        self.environment = RuntimeEnvironment.from_response(response)
        log.info(
            "authenticated as %s on %s (platform %s %s)",
            self.airline,
            self.environment.workstation.name,
            self.environment.platform.vendor,
            self.environment.platform.version,
        )
        return self.environment

    def query_devices(
        self,
        *,
        device_name: Optional[str] = None,
        device_type: Optional[str] = None,
        proximity: Optional[str] = None,
    ) -> list[Device]:
        """Run a ``<deviceQueryRequest>`` (section 29.5, Table 29.1).

        Criteria combine with AND: a device must match all of them.
        """
        attrs = {}
        if device_name:
            attrs["deviceName"] = device_name
        if device_type:
            attrs["deviceType"] = device_type
        if proximity:
            attrs["proximity"] = proximity
        response = self._request(
            "deviceQueryRequest", attrs=attrs, timeout=params.MAX_DEV_STS_TIME
        )
        device_list = response.body.find("deviceList")
        if device_list is None:
            return []
        return [Device.from_element(e) for e in device_list.findall("device")]

    def bye(self) -> None:
        """Send ``<byeRequest>`` and wait for the response (section 29.1).

        After this the device token is invalid and no further messages may be
        sent on this connection.
        """
        if self._said_bye or not self._connection.connected:
            return
        try:
            self._request("byeRequest", timeout=params.APP_SPG_TIME)
        except (CuppsError, OSError) as exc:
            # A platform that has already gone is not a reason to fail the
            # shutdown path; the socket close below still tidies up.
            log.warning("byeRequest did not complete cleanly: %s", exc)
        finally:
            self._said_bye = True

    def close(self) -> None:
        """Say goodbye, then close the socket (section 26.6)."""
        try:
            self.bye()
        finally:
            super().close()

    def __enter__(self) -> "PlatformSession":
        self.open()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


class DeviceSession(_BaseSession):
    """One application connection to one logical device."""

    def __init__(
        self,
        device: Device,
        *,
        device_token: str,
        airline_id: str,
        mode: results.InterfaceMode,
        host: Optional[str] = None,
        port: Optional[int] = None,
        ms_foid_masking: Optional[str] = None,
    ) -> None:
        # Section 26.5 defines a device's hostname to be the device's name;
        # the platform also supplies an explicit host/IP and port in the
        # device descriptor, which is preferred when present.
        resolved_host = host or device.host_name or device.ip or device.name
        resolved_port = port or device.port
        if not resolved_port:
            raise ValueError(
                f"device {device.name} has no port in its descriptor; the "
                f"platform must supply <ipAndPort>"
            )
        # A ZL device is allowed a deeper outstanding window so log writes do
        # not stall the application (26.11.36).
        limit = (
            params.PLT_STREAM_OUT_MSGS_ZL
            if device.device_type == "ZL"
            else params.PLT_STREAM_OUT_MSGS
        )
        super().__init__(
            resolved_host,
            resolved_port,
            name=f"device:{device.name}",
            outstanding_limit=limit,
        )
        self.device = device
        self.device_token = device_token
        self.airline_id = airline_id
        self.mode = mode
        self.ms_foid_masking = ms_foid_masking
        self.status = device.status
        self._lock_method: Optional[results.LockMethod] = None
        self._released = False
        self._lock_renewed_at = 0.0
        self._state_lock = threading.Lock()
        self.add_notification_handler(self._track_status)

    # -- lifecycle --------------------------------------------------------

    def open(self) -> Device:
        """Connect, handshake, acquire the device and set the interface mode."""
        self._connection.connect()
        self._handshake()
        device = self._acquire()
        self._set_interface_mode()
        return device

    def _acquire(self) -> Device:
        attrs = {
            "deviceName": self.device.name,
            "deviceToken": self.device_token,
            "airlineID": self.airline_id,
        }
        if self.ms_foid_masking:
            attrs["msFoidMasking"] = self.ms_foid_masking

        response = self._request(
            "deviceAcquireRequest",
            attrs=attrs,
            timeout=params.DEV_ACQ_MAX_TIME,
            expect_ok=False,
        )
        result = response.result or "<missing>"
        if result != results.ACQUIRE_OK:
            # Section 26.6: a non-OK acquire leaves the session unusable, so
            # drop the socket rather than leaving a half-open device session.
            self._connection.close()
            if result == results.ACQUIRE_INVALID_TOKEN:
                raise TokenInvalidated(
                    f"platform rejected the device token for {self.device.name}; "
                    f"re-authenticate and rebuild every device session"
                )
            raise RequestFailed("deviceAcquireRequest", result)

        element = response.body.find("device")
        if element is not None:
            acquired = Device.from_element(element)
            self.device = acquired
            self.status = acquired.status
            return acquired
        return self.device

    def _set_interface_mode(self) -> None:
        """Set the session's interface mode (section 30.1).

        Required immediately after acquiring, and fixed for the life of the
        session: changing mode means a new connection.
        """
        self._request(
            "interfaceModeRequest",
            attrs={"mode": self.mode.value},
            timeout=params.MAX_DEV_STS_TIME,
        )
        if self.mode is results.InterfaceMode.AEA:
            # Section 30.1 CRITICAL: an AEA session must send the EP command
            # first so the printer starts from known parameters.
            self.aea("EP")

    def release(self) -> None:
        """Send ``<deviceReleaseRequest>`` (section 30.6.2)."""
        if self._released or not self._connection.connected:
            return
        try:
            self._request("deviceReleaseRequest", timeout=params.DEV_REL_MAX_TIME)
        except (CuppsError, OSError) as exc:
            log.warning("%s release did not complete: %s", self.device.name, exc)
        finally:
            self._released = True
            self._lock_method = None

    def close(self) -> None:
        """Release the device, then close the socket.

        Section 30.6.2 requires applications to close the connection after
        releasing.
        """
        try:
            self.release()
        finally:
            super().close()

    def __enter__(self) -> "DeviceSession":
        self.open()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- locking ----------------------------------------------------------

    @property
    def lock_method(self) -> Optional[results.LockMethod]:
        return self._lock_method

    @property
    def locked(self) -> bool:
        return self._lock_method is not None

    def lock(
        self, method: results.LockMethod = results.LockMethod.BY_CONNECTION
    ) -> str:
        """Lock the device (section 26.13, 30.7).

        Renewing an existing lock with the same method is legal and resets the
        platform's ``DevLkdTime`` timer.  Requesting a *different* method while
        a lock is held is an error the spec says will draw an
        ``illogicalMessageErrorEvent``, so it is refused locally instead.
        """
        if self.mode is results.InterfaceMode.SPECIAL:
            # Section 30.20 note: Special Mode devices (ZL, ZI) do not support
            # locking, and attempting it draws an illogicalMessageErrorEvent
            # that would tear down the session.
            raise CuppsError(
                f"{self.device.name} is a Special Mode device; locking one is "
                f"an illogical message (section 30.20). The platform "
                f"guarantees concurrent use without a lock."
            )
        with self._state_lock:
            held = self._lock_method
        if held is not None and held is not method:
            raise CuppsError(
                f"{self.device.name} is locked {held.value}; switching to "
                f"{method.value} is not allowed (section 30.7) -- unlock first"
            )

        response = self._request(
            "deviceLockRequest",
            attrs={"lockMethod": method.value},
            timeout=params.MAX_DEV_LOCK_TIME,
            expect_ok=False,
        )
        result = response.result or "<missing>"
        if result == results.LOCK_DEVICE_LOCKED:
            locker_element = response.body.find("deviceLockerInfo")
            locker = dict(locker_element.attrs) if locker_element else {}
            raise DeviceLocked(locker)
        if not response.is_ok:
            raise RequestFailed("deviceLockRequest", result)

        with self._state_lock:
            self._lock_method = method
        return result

    def unlock(self) -> None:
        """Release the lock (section 30.7)."""
        with self._state_lock:
            if self._lock_method is None:
                return
        self._request("deviceUnlockRequest", timeout=params.MAX_DEV_UNL_TIME)
        with self._state_lock:
            self._lock_method = None

    # -- status -----------------------------------------------------------

    def _track_status(self, message: xmlmsg.Message) -> None:
        """Keep :attr:`status` current from asynchronous notifications."""
        if message.message_name != "notify":
            if message.message_name == "deviceLockExpiredEvent":
                # Section 26.13.2: the platform dropped our lock.
                with self._state_lock:
                    self._lock_method = None
            return
        notification = message.body.find("deviceStatusNotification")
        if notification is None:
            return
        for child in notification.children:
            if child.name.endswith("Status"):
                self.status = DeviceStatus.from_element(child)
                break

    def request_status(self) -> DeviceStatus:
        """Poll with ``<deviceStatusRequest>`` (section 30.2).

        Polling is bounded by ``DevPollMaxFreq`` and the spec calls frequent
        polling a bug: prefer the ``<notify>`` stream, which this session
        already tracks into :attr:`status`.
        """
        response = self._request(
            "deviceStatusRequest",
            timeout=params.MAX_DEV_STS_TIME,
            expect_ok=False,
        )
        if response.result == results.STATUS_POLLING_TOO_FAST:
            raise RequestFailed(
                "deviceStatusRequest",
                results.STATUS_POLLING_TOO_FAST,
                f"polling faster than DevPollMaxFreq "
                f"({params.DEV_POLL_MAX_FREQ}s)",
            )
        if not response.is_ok:
            raise RequestFailed("deviceStatusRequest", response.result or "<missing>")
        for child in response.body.children:
            if child.name.endswith("Status"):
                self.status = DeviceStatus.from_element(child)
                break
        return self.status

    # -- AEA --------------------------------------------------------------

    def aea(self, *parts: object) -> xmlmsg.Message:
        """Send an ``<aeaRequest>`` (section 30.12).

        Each argument becomes one ``<aeaMessage>``: ``str`` arguments are sent
        as ``<aeaText>``, ``bytes`` as Base64 ``<aeaBinary>``.  The receiver
        reassembles the command by concatenating the text and the decoded
        binary in order (Table 30.4).
        """
        import base64 as _base64

        if self.mode is not results.InterfaceMode.AEA:
            raise CuppsError(
                f"{self.device.name} session is in {self.mode.value} mode; "
                f"sending aeaRequest outside AEA mode is an illogical message "
                f"(section 30.15)"
            )
        body = xmlmsg.Element("aeaRequest")
        for part in parts:
            message_element = body.add(xmlmsg.Element("aeaMessage"))
            if isinstance(part, bytes):
                message_element.add(
                    xmlmsg.Element(
                        "aeaBinary",
                        text=_base64.b64encode(part).decode("ascii"),
                    )
                )
            else:
                message_element.add(xmlmsg.Element("aeaText", text=str(part)))
        return self._request(
            "aeaRequest", body=body, timeout=params.PR_MAX_RESPONSE_TIME
        )
