"""AEA Mode driver: BP, BT, BG, SD and SN (section 30.12).

CUPPS carries AEA traffic but does not define it; the command set lives in
the AEA specification.  The platform's job is therefore transport and
framing, not interpretation: it passes the application's command stream to
the device and returns the device's responses, tagging status as the device
reports it.

AEA messages are delimited by the device's line terminator.  Unsolicited
status reports are how an AEA printer announces paper out or a jam, and
section 30.2 recommends platforms enable them -- which is why this driver
parses status responses rather than only polling.

Section 30.2 also carries a trap worth encoding: for an AEA printer, paper
status must be reported *independently* of online status.  "When pulling out
paper without lifting the print head, the generated notify will include
paperOut=true and must also still report ready=true if the printer is still
online."  A driver that clears ready on paper-out is wrong, and applications
built against it will mis-handle a routine paper change.
"""

from __future__ import annotations

import logging
import re
import threading
from typing import Optional

from .base import DeviceDriver, DriverData, DriverStatus
from .transport import Transport

log = logging.getLogger("cuppsplatform.drivers.aea")

#: Section 30.1 CRITICAL: the first command of every AEA session.
EP_COMMAND = b"EP"

#: AEA terminates messages with a carriage return.
TERMINATOR = b"\r"

#: An AEA status response begins ST; the digits that follow are the device's
#: status word. Vendors differ in what they pack in beyond the basics, so
#: only the well-attested conditions are decoded and the rest is passed up.
_STATUS_RE = re.compile(rb"^ST(?P<code>[0-9A-Z]+)")

#: Error codes an AEA printer reports. ERR5 is called out in section 30.2 as
#: the one whose persistence across a power cycle varies by vendor, which is
#: what the device profile's retainsErrorAcrossPowerCycle flag records.
_ERROR_RE = re.compile(rb"^ERR(?P<number>\d+)")


class AeaDriver(DeviceDriver):
    """A device driven by an AEA command stream."""

    device_types = frozenset({"BP", "BT", "BG", "SD", "SN"})

    def __init__(
        self,
        device_name: str,
        device_type: str,
        transport: Transport,
        *,
        terminator: bytes = TERMINATOR,
        send_ep_on_start: bool = True,
        **kwargs,
    ) -> None:
        super().__init__(device_name, device_type, transport, **kwargs)
        self.terminator = terminator
        self.send_ep_on_start = send_ep_on_start
        self._buffer = bytearray()
        self._buffer_lock = threading.Lock()
        self._last_response: Optional[bytes] = None
        self._response_ready = threading.Event()

    # -- lifecycle --------------------------------------------------------

    def initialise(self) -> None:
        """Put the device on AEA default parameters.

        Section 30.1: an AEA session must open with EP, and the platform must
        ensure the parameters active at that point are AEA defaults. The
        device is briefly unsecured so the command can go out, because
        initialisation is the platform's own traffic rather than an
        application's.
        """
        if not self.send_ep_on_start:
            return
        was_secured = self.secured
        if was_secured:
            self.unsecure()
        try:
            self.send_command(EP_COMMAND)
        finally:
            if was_secured:
                self.secure()

    # -- commands ---------------------------------------------------------

    def send_command(self, command: bytes) -> int:
        """Send one AEA command, appending the terminator if absent."""
        if not command.endswith(self.terminator):
            command = command + self.terminator
        return self.write(command)

    def send_stream(self, stream: bytes) -> int:
        """Send a host-produced AEA stream unchanged.

        No terminator is added: the host's stream already carries its own
        framing, and appending to it would corrupt a binary payload such as a
        logo download.
        """
        return self.write(stream)

    # -- parsing ----------------------------------------------------------

    def handle_bytes(self, chunk: bytes) -> None:
        messages: list[bytes] = []
        with self._buffer_lock:
            self._buffer += chunk
            while True:
                index = self._buffer.find(self.terminator)
                if index < 0:
                    break
                message = bytes(self._buffer[:index])
                del self._buffer[: index + len(self.terminator)]
                if message:
                    messages.append(message)

        for message in messages:
            self._handle_message(message)

    def _handle_message(self, message: bytes) -> None:
        self._last_response = message
        self._response_ready.set()

        status_match = _STATUS_RE.match(message)
        if status_match:
            self._apply_status(status_match.group("code"))
        error_match = _ERROR_RE.match(message)
        if error_match:
            self._apply_error(int(error_match.group("number")))

        # Everything is passed up as well: the application's AEA dialogue is
        # between it and the device, and the platform must not swallow it.
        self._emit(
            DriverData(
                kind="aea",
                payload=message,
                attributes={"device": self.device_name},
            )
        )

    def _apply_status(self, code: bytes) -> None:
        """Decode the conditions the specification names.

        Only the well-attested ones are decoded. A vendor status word carries
        more, and guessing at it would be worse than leaving it to the device
        profile, so the rest travels up untouched.
        """
        text = code.decode("ascii", "replace")
        paper_out = "P" in text
        paper_jam = "J" in text
        # Section 30.2 CRITICAL: paper status is independent of online
        # status. A printer with no paper that still answers AEA is ready.
        self._set_status(
            self.status.with_(
                ready=True,
                unknown=False,
                power_off=False,
                paper_out=paper_out,
                paper_jam=paper_jam,
                description=f"AEA status {text}",
            )
        )

    def _apply_error(self, number: int) -> None:
        self._set_status(
            self.status.with_(
                unknown=False,
                power_off=False,
                description=f"AEA ERR{number}",
            )
        )

    # -- request / response -----------------------------------------------

    def request(self, command: bytes, timeout: float = 5.0) -> Optional[bytes]:
        """Send a command and wait for the next message back.

        AEA is not a request/response protocol in general -- unsolicited
        status arrives whenever the device feels like it -- so this returns
        whatever came next, and the caller decides whether it is an answer.
        """
        self._response_ready.clear()
        self.send_command(command)
        if not self._response_ready.wait(timeout=timeout):
            return None
        return self._last_response

    # -- testing ----------------------------------------------------------

    def test(self) -> DriverStatus:
        """Ask the device for its status, which exercises the link."""
        was_secured = self.secured
        if was_secured:
            self.unsecure()
        try:
            self.request(b"ST", timeout=3.0)
        finally:
            if was_secured:
                self.secure()
        return self.status
