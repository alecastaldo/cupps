"""Reader drivers: BC, MS and OC (chapter 10, section 30.9).

Readers are input-only from the platform's point of view: the device pushes
data when a passenger presents something, and the platform normalises it and
makes it available.  Two behaviours come straight from the specification.

**Delimited records.**  Real readers terminate a scan with CR, LF or both,
and a burst can straddle two reads.  The driver buffers until it sees a
terminator rather than assuming one read equals one scan, because a
1D barcode arriving in two TCP segments is otherwise silently split into two
scans -- a bug that only appears under load.

**Normalisation (section 26.11.8).**  Some readers stream continuously.
``DevNormTime`` meters that: if several values arrive inside the window, only
the last is passed up.  Without it a badge held against a reader produces
hundreds of events a minute.
"""

from __future__ import annotations

import logging
import threading
from typing import Optional

from cupps import params

from .base import DeviceDriver, DriverData, DriverStatus
from .transport import Transport

log = logging.getLogger("cuppsplatform.drivers.reader")

#: Record terminators a reader may use.
TERMINATORS = (b"\r\n", b"\r", b"\n")

#: Maximum buffered bytes before the driver gives up on finding a terminator.
#: A reader emitting an unterminated stream must not grow the buffer without
#: limit; the data is flushed as a record instead.
MAX_RECORD = 8192


class ReaderDriver(DeviceDriver):
    """A delimited-record reader: barcode, magnetic stripe or optical."""

    device_types = frozenset({"BC", "MS", "OC", "SD"})

    #: What :class:`~cuppsplatform.drivers.base.DriverData` this driver emits.
    DATA_KINDS = {
        "BC": "barcode",
        "MS": "track",
        "OC": "ocr",
        "SD": "weight",
    }

    def __init__(
        self,
        device_name: str,
        device_type: str,
        transport: Transport,
        *,
        normalise: bool = False,
        normalise_window: float = params.DEV_NORM_TIME,
        **kwargs,
    ) -> None:
        super().__init__(device_name, device_type, transport, **kwargs)
        self._buffer = bytearray()
        self._buffer_lock = threading.Lock()
        #: Section 26.11.8 metering, for readers that stream continuously.
        self.normalise = normalise
        self.normalise_window = normalise_window
        self._last_emitted_at = 0.0
        self._pending: Optional[DriverData] = None
        self._normalise_timer: Optional[threading.Timer] = None

    # -- parsing ----------------------------------------------------------

    def handle_bytes(self, chunk: bytes) -> None:
        records: list[bytes] = []
        with self._buffer_lock:
            self._buffer += chunk
            while True:
                index, terminator = self._find_terminator(self._buffer)
                if index < 0:
                    break
                record = bytes(self._buffer[:index])
                del self._buffer[: index + len(terminator)]
                if record:
                    records.append(record)
            if len(self._buffer) >= MAX_RECORD:
                # An unterminated stream must not grow without bound.
                log.warning(
                    "%s buffered %d bytes with no terminator; flushing",
                    self.device_name, len(self._buffer),
                )
                records.append(bytes(self._buffer))
                self._buffer.clear()

        for record in records:
            self._emit_record(record)

    @staticmethod
    def _find_terminator(buffer: bytearray) -> tuple[int, bytes]:
        """Earliest terminator in ``buffer``, preferring the longest match."""
        best_index, best_terminator = -1, b""
        for terminator in TERMINATORS:
            index = buffer.find(terminator)
            if index < 0:
                continue
            if best_index < 0 or index < best_index or (
                index == best_index and len(terminator) > len(best_terminator)
            ):
                best_index, best_terminator = index, terminator
        return best_index, best_terminator

    def _emit_record(self, record: bytes) -> None:
        data = DriverData(
            kind=self.DATA_KINDS.get(self.device_type, "raw"),
            payload=record,
            attributes={"device": self.device_name},
        )
        if not self.normalise:
            self._emit(data)
            return
        self._normalise(data)

    # -- normalisation (section 26.11.8) ----------------------------------

    def _normalise(self, data: DriverData) -> None:
        """Hold a value for ``DevNormTime``, keeping only the last.

        Section 26.11.8: "If multiple data values are available within this
        time, then the last value is return to the application."
        """
        with self._buffer_lock:
            self._pending = data
            if self._normalise_timer is not None:
                return  # a window is already open; this value supersedes
            self._normalise_timer = threading.Timer(
                self.normalise_window, self._flush_normalised
            )
            self._normalise_timer.daemon = True
            self._normalise_timer.start()

    def _flush_normalised(self) -> None:
        with self._buffer_lock:
            data, self._pending = self._pending, None
            self._normalise_timer = None
        if data is not None:
            self._emit(data)

    def stop(self) -> None:
        timer = self._normalise_timer
        if timer is not None:
            timer.cancel()
            self._normalise_timer = None
        super().stop()

    # -- securing ---------------------------------------------------------

    def on_secured(self) -> None:
        """Drop anything part-read, so it cannot surface when unsecured.

        Section 10.4.1 is about configuration barcodes: a partial record
        buffered while the device was in use must not be completed by a scan
        made while nobody holds the device.
        """
        with self._buffer_lock:
            self._buffer.clear()
            self._pending = None

    # -- testing ----------------------------------------------------------

    def test(self) -> DriverStatus:
        """A reader test needs a real read (section 11.2.1).

        The driver cannot make a passenger present a boarding pass, so this
        reports readiness and the caller waits for data; the device test
        screen is where an operator actually scans something.
        """
        return self.status
