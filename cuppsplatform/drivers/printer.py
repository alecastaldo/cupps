"""PR driver: Standard Mode printing through a spooler (section 30.15).

PR is the one printer CUPPS drives in Standard Mode, and Table 3.3 note 6 is
explicit that platforms must implement it "by using the Windows Spooler".
The application hands over a finished document -- a PDF or plain text from
``<printDocument>`` -- and the platform's job is to get it onto paper and
report what happened.

Three backends, chosen by configuration:

``spooler``
    The Windows print spooler, via pywin32. What a station runs.
``cups``
    ``lp``, for a printer on a Linux bench. Lets a lab drive a real printer
    without a Windows workstation in the way.
``file``
    Writes each document to a directory. For CI, and for a bench where the
    printer has not arrived yet -- you can still see exactly what the
    platform would have sent.

``PRMaxResponseTime`` (section 26.11.37) bounds the whole thing: if the
spooler has not reported completion and the printer has not started within
120 seconds, the platform answers the print request anyway and may delete the
job.  A print that hangs must not hang the position.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from cupps import params

from .base import DeviceDriver, DriverData, DriverStatus
from .transport import Transport, TransportConfig

log = logging.getLogger("cuppsplatform.drivers.printer")


class PrintError(Exception):
    """A document could not be printed."""


@dataclass
class PrintResult:
    """What became of one document."""

    #: ``OK``, ``paperOut``, ``notReady`` or ``timeout`` -- the result codes
    #: section 30.15 puts in ``<printDocumentResult>``.
    result: str
    job_id: str = ""
    detail: str = ""
    elapsed: float = 0.0

    @property
    def ok(self) -> bool:
        return self.result == "OK"


class PrintBackend:
    """Somewhere a document can be sent."""

    name = "abstract"

    def submit(self, document: bytes, *, stock: str, job_name: str) -> str:
        """Submit a document, returning a job identifier."""
        raise NotImplementedError

    def poll(self, job_id: str) -> Optional[PrintResult]:
        """Result for ``job_id``, or ``None`` while it is still printing."""
        raise NotImplementedError

    def cancel(self, job_id: str) -> bool:
        return False

    def available(self) -> bool:
        return True


class FileBackend(PrintBackend):
    """Writes documents to a directory instead of printing them."""

    name = "file"

    def __init__(self, directory: str) -> None:
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self._counter = 0
        self._lock = threading.Lock()
        self.written: list[Path] = []

    def submit(self, document: bytes, *, stock: str, job_name: str) -> str:
        with self._lock:
            self._counter += 1
            index = self._counter
        suffix = "pdf" if document[:5] == b"%PDF-" else "txt"
        target = self.directory / f"{index:05d}-{job_name}-{stock}.{suffix}"
        target.write_bytes(document)
        self.written.append(target)
        log.info("wrote %d bytes to %s", len(document), target)
        return str(target)

    def poll(self, job_id: str) -> Optional[PrintResult]:
        return PrintResult(result="OK", job_id=job_id, detail=job_id)


class CupsBackend(PrintBackend):
    """A real printer on a Linux bench, driven through ``lp``."""

    name = "cups"

    def __init__(self, printer: str = "", options: Optional[dict] = None) -> None:
        self.printer = printer
        self.options = options or {}

    def available(self) -> bool:
        return shutil.which("lp") is not None

    def submit(self, document: bytes, *, stock: str, job_name: str) -> str:
        if not self.available():
            raise PrintError("the lp command is not available on this host")
        command = ["lp", "-t", job_name]
        if self.printer:
            command += ["-d", self.printer]
        for key, value in self.options.items():
            command += ["-o", f"{key}={value}"]

        with tempfile.NamedTemporaryFile(delete=False, suffix=".doc") as handle:
            handle.write(document)
            path = handle.name
        try:
            completed = subprocess.run(
                command + [path], capture_output=True, text=True, timeout=30
            )
        except subprocess.TimeoutExpired as exc:
            raise PrintError(f"lp did not return: {exc}") from exc
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass

        if completed.returncode != 0:
            raise PrintError(f"lp failed: {completed.stderr.strip()}")
        # lp prints "request id is printer-123 (1 file(s))".
        for token in completed.stdout.split():
            if "-" in token and token[-1].isdigit():
                return token
        return completed.stdout.strip()

    def poll(self, job_id: str) -> Optional[PrintResult]:
        if shutil.which("lpstat") is None:
            return PrintResult(result="OK", job_id=job_id)
        completed = subprocess.run(
            ["lpstat", "-W", "not-completed", "-o"],
            capture_output=True, text=True, timeout=10,
        )
        if job_id in completed.stdout:
            return None      # still queued or printing
        return PrintResult(result="OK", job_id=job_id)

    def cancel(self, job_id: str) -> bool:
        if shutil.which("cancel") is None:
            return False
        return subprocess.run(
            ["cancel", job_id], capture_output=True
        ).returncode == 0


class WindowsSpoolerBackend(PrintBackend):
    """The Windows print spooler, which a station uses (Table 3.3 note 6)."""

    name = "spooler"

    def __init__(self, printer: str = "") -> None:
        self.printer = printer

    def available(self) -> bool:
        try:
            import win32print  # noqa: F401
        except ImportError:
            return False
        return True

    def submit(self, document: bytes, *, stock: str, job_name: str) -> str:
        try:
            import win32print
        except ImportError as exc:  # pragma: no cover - Windows only
            raise PrintError(
                "the Windows spooler backend needs pywin32; install it with "
                "'pip install pywin32', or use the cups or file backend"
            ) from exc

        printer = self.printer or win32print.GetDefaultPrinter()
        handle = win32print.OpenPrinter(printer)
        try:
            job = win32print.StartDocPrinter(
                handle, 1, (job_name, None, "RAW")
            )
            win32print.StartPagePrinter(handle)
            win32print.WritePrinter(handle, document)
            win32print.EndPagePrinter(handle)
            win32print.EndDocPrinter(handle)
        finally:
            win32print.ClosePrinter(handle)
        return str(job)

    def poll(self, job_id: str) -> Optional[PrintResult]:  # pragma: no cover
        try:
            import win32print
        except ImportError:
            return PrintResult(result="OK", job_id=job_id)
        printer = self.printer or win32print.GetDefaultPrinter()
        handle = win32print.OpenPrinter(printer)
        try:
            jobs = win32print.EnumJobs(handle, 0, 999, 1)
        finally:
            win32print.ClosePrinter(handle)
        for job in jobs:
            if str(job.get("JobId")) == job_id:
                return None  # still in the queue
        return PrintResult(result="OK", job_id=job_id)


#: Backends by the name a binding uses.
BACKENDS = {
    "file": FileBackend,
    "cups": CupsBackend,
    "spooler": WindowsSpoolerBackend,
}


class _NullTransport(Transport):
    """A PR device has no byte stream; the spooler is the transport."""

    def __init__(self) -> None:
        super().__init__(TransportConfig(kind="spooler"))
        self._open = False

    @property
    def is_open(self) -> bool:
        return self._open

    def open(self) -> None:
        self._open = True

    def close(self) -> None:
        self._open = False

    def write(self, data: bytes) -> int:
        return len(data)

    def read(self, size: int = 4096) -> bytes:
        time.sleep(self.config.read_timeout)
        return b""

    def describe(self) -> str:
        return "spooler"


class PrintDriver(DeviceDriver):
    """A Standard Mode printer, driven through a spooler backend."""

    device_types = frozenset({"PR"})

    def __init__(
        self,
        device_name: str,
        device_type: str,
        transport: Optional[Transport] = None,
        *,
        backend: str = "file",
        backend_options: Optional[dict] = None,
        max_response_time: float = params.PR_MAX_RESPONSE_TIME,
        **kwargs,
    ) -> None:
        super().__init__(
            device_name, device_type, transport or _NullTransport(), **kwargs
        )
        options = dict(backend_options or {})
        try:
            factory = BACKENDS[backend]
        except KeyError:
            raise PrintError(
                f"unknown print backend {backend!r}; known backends are "
                f"{sorted(BACKENDS)}"
            ) from None
        if factory is FileBackend and "directory" not in options:
            options["directory"] = tempfile.mkdtemp(prefix="cupps-print-")
        self.backend: PrintBackend = factory(**options)
        self.max_response_time = max_response_time

    def initialise(self) -> None:
        if not self.backend.available():
            raise PrintError(
                f"the {self.backend.name} print backend is not available on "
                f"this host"
            )

    def handle_bytes(self, chunk: bytes) -> None:
        """A PR device produces no input."""

    # -- printing ---------------------------------------------------------

    def print_document(
        self, document: bytes, *, stock: str = "", job_name: str = ""
    ) -> PrintResult:
        """Print one document and wait, bounded by ``PRMaxResponseTime``.

        Section 26.11.37: if the spooler has not reported completion within
        that time, the platform responds anyway rather than leaving the
        application waiting.
        """
        if self.secured:
            from .base import DeviceSecured

            raise DeviceSecured(
                f"{self.device_name} is secured; no application holds it "
                f"(section 10.4.1)"
            )

        status = self.status
        if status.paper_out:
            return PrintResult(result="paperOut", detail="the printer is out of paper")
        if not status.ready:
            return PrintResult(result="notReady", detail=status.description)

        started = time.monotonic()
        try:
            job_id = self.backend.submit(
                document, stock=stock or "default",
                job_name=job_name or self.device_name,
            )
        except PrintError as exc:
            return PrintResult(result="notReady", detail=str(exc))

        deadline = started + self.max_response_time
        while time.monotonic() < deadline:
            outcome = self.backend.poll(job_id)
            if outcome is not None:
                outcome.elapsed = time.monotonic() - started
                return outcome
            time.sleep(0.1)

        log.warning(
            "%s job %s did not complete within PRMaxResponseTime (%.0fs)",
            self.device_name, job_id, self.max_response_time,
        )
        self.backend.cancel(job_id)
        return PrintResult(
            result="timeout", job_id=job_id,
            detail=f"no completion within {self.max_response_time:.0f}s",
            elapsed=time.monotonic() - started,
        )

    def cancel(self, job_id: str) -> bool:
        """Cancel a queued document (section 30.15.5)."""
        return self.backend.cancel(job_id)

    def set_paper_out(self, paper_out: bool) -> None:
        """Report a paper condition, from the queue or from an operator.

        Section 30.2: paper status is independent of online status, so ready
        is not disturbed.
        """
        self._set_status(
            self.status.with_(
                paper_out=paper_out,
                description="out of paper" if paper_out else "ready",
            )
        )

    # -- testing ----------------------------------------------------------

    def test(self) -> DriverStatus:
        """Actually print a test page (section 11.2.1)."""
        was_secured = self.secured
        if was_secured:
            self.unsecure()
        try:
            page = (
                f"CUPPS DEVICE TEST\r\n{self.device_name}\r\n"
                f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}\r\n"
            ).encode("ascii")
            outcome = self.print_document(page, stock="TEST", job_name="devicetest")
        finally:
            if was_secured:
                self.secure()
        return self.status.with_(description=f"test print: {outcome.result}")
