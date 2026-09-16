"""Device operations layered on a :class:`~cupps.session.DeviceSession`.

Each wrapper adds the message exchanges that only make sense for one family
of device: reads for BC/MS/OC (section 30.9), printing for PR (30.15) and the
AEA printers BP/BT (30.12), and the log conversation for ZL (30.20).
"""

from __future__ import annotations

import base64
import logging
import threading
from dataclasses import dataclass, field
from typing import Iterator, Optional

from . import crypto, params, results, xmlmsg
from .errors import CuppsError, RequestFailed
from .session import DeviceSession

log = logging.getLogger("cupps.devices")


# --------------------------------------------------------------------------
# Readers -- BC, MS, OC (section 30.9)
# --------------------------------------------------------------------------


@dataclass
class BarcodeRead:
    """One ``<bcData>`` element from a barcode reader (Listing 30.29)."""

    data: bytes
    type_code: str = "u"
    read_status: str = results.OK

    @property
    def text(self) -> str:
        """The payload decoded as text.

        Section 30.9.1 notes that all barcode data is presented as simple
        string data and the application is responsible for parsing it.
        """
        return self.data.decode("utf-8", errors="replace")

    @property
    def symbology(self) -> str:
        return results.BARCODE_TYPES.get(self.type_code, "unknown")


@dataclass
class TrackRead:
    """One decrypted magnetic-stripe track (Listing 30.30)."""

    track_id: int
    blocks: dict[int, bytes] = field(default_factory=dict)
    read_status: str = results.OK

    @property
    def data(self) -> bytes:
        """Every block of this track concatenated in block order."""
        return b"".join(self.blocks[key] for key in sorted(self.blocks))

    @property
    def text(self) -> str:
        return self.data.decode("latin-1", errors="replace")


@dataclass
class OcrRead:
    """One ``<ocTrackData>`` element from an optical reader (Listing 30.31)."""

    track_id: int
    text: str
    read_status: str = results.OK


class Reader:
    """BC, MS and OC read operations.

    Applications should wait for the ``<notify dataAvailable="true">``
    notification rather than polling with repeated reads (section 30.9
    recommendation); :meth:`on_data_available` wires that up.
    """

    def __init__(self, session: DeviceSession, *, device_token: str = "") -> None:
        self.session = session
        # MS track data is encrypted under the session's device token
        # (section 30.9.2 CRITICAL).
        self._device_token = device_token or session.device_token
        self._crypt_algorithm = results.CryptAlgorithm.AES_STRONG.value
        self._data_available = threading.Event()
        session.add_notification_handler(self._watch_for_data)

    @property
    def crypt_algorithm(self) -> str:
        return self._crypt_algorithm

    def set_crypt_algorithm(self, algorithm: str) -> None:
        """Choose the encryption algorithm for this session (section 30.3).

        Until this is sent, the platform's default from
        ``<authenticateResponse>`` applies.
        """
        self.session._request(  # noqa: SLF001 - intentional protocol passthrough
            "setCryptAlgorithmRequest",
            attrs={"mode": algorithm},
            timeout=params.MAX_DEV_STS_TIME,
        )
        self._crypt_algorithm = algorithm

    def _watch_for_data(self, message: xmlmsg.Message) -> None:
        if message.message_name != "notify":
            return
        if message.body.get_bool("dataAvailable"):
            self._data_available.set()

    def wait_for_data(self, timeout: Optional[float] = None) -> bool:
        """Block until the platform signals data is available."""
        return self._data_available.wait(timeout=timeout)

    def read(self) -> xmlmsg.Message:
        """Issue ``<readerReadRequest>`` and return the raw response.

        Reading clears the platform's pending data for the device, so a second
        read against the same scan returns nothing (section 26.13.1, step 8).
        """
        self._data_available.clear()
        return self.session._request(  # noqa: SLF001
            "readerReadRequest", timeout=params.MAX_DEV_STS_TIME
        )

    def read_barcodes(self) -> list[BarcodeRead]:
        """Read pending barcode scans (section 30.9.1)."""
        response = self.read()
        reads: list[BarcodeRead] = []
        for element in response.body.iter("bcData"):
            # Section 26.10 permits whitespace inside Base64 data.
            payload = "".join((element.text or "").split())
            reads.append(
                BarcodeRead(
                    data=base64.b64decode(payload) if payload else b"",
                    type_code=element.get("bcTypeCode", "u") or "u",
                    read_status=element.get("readStatus", results.OK) or results.OK,
                )
            )
        return reads

    def read_tracks(self) -> list[TrackRead]:
        """Read and decrypt magnetic-stripe tracks (section 30.9.2)."""
        response = self.read()
        tracks: list[TrackRead] = []
        for track_element in response.body.iter("msTrackData"):
            track = TrackRead(
                track_id=track_element.get_int("trackID", 0) or 0,
                read_status=track_element.get("readStatus", results.OK) or results.OK,
            )
            for block_element in track_element.findall("blockData"):
                block_id = block_element.get_int("blockID", 0) or 0
                payload = block_element.text or ""
                if not payload.strip():
                    continue
                track.blocks[block_id] = crypto.decrypt_track(
                    payload, self._device_token, self._crypt_algorithm
                )
            tracks.append(track)
        return tracks

    def read_ocr(self) -> list[OcrRead]:
        """Read optical-character data (section 30.9.3).

        OC data is carried as CDATA because it may contain XML special
        characters, so it arrives as plain element text.
        """
        response = self.read()
        return [
            OcrRead(
                track_id=element.get_int("trackID", 0) or 0,
                text=element.text or "",
                read_status=element.get("readStatus", results.OK) or results.OK,
            )
            for element in response.body.iter("ocTrackData")
        ]


# --------------------------------------------------------------------------
# Printers -- PR in Standard Mode (section 30.15)
# --------------------------------------------------------------------------


@dataclass
class PrintDocument:
    """One document inside a ``<printRequest>`` (Listing 30.41).

    ``document_id`` sets the order in which the platform processes the
    documents in a request.
    """

    document_id: int
    stock_name: str
    #: Exactly one payload kind must be set.
    pdf: Optional[bytes] = None
    text: Optional[str] = None
    orientation: Optional[str] = None

    def to_element(self) -> xmlmsg.Element:
        attrs = {"documentID": str(self.document_id), "stockName": self.stock_name}
        if self.orientation:
            attrs["documentOrientation"] = self.orientation
        element = xmlmsg.Element("printDocument", attrs)

        if self.pdf is not None:
            element.add(
                xmlmsg.Element(
                    "pdfPrintDocument",
                    text=base64.b64encode(self.pdf).decode("ascii"),
                )
            )
        elif self.text is not None:
            # <simpleTextPrintDocument> is ASCII with CR/LF delimiters and the
            # application owns all formatting including line breaking
            # (section 30.15).
            document = element.add(xmlmsg.Element("simpleTextPrintDocument"))
            node = document.add(xmlmsg.Element("simpleTextPrintDocumentNode"))
            node.add(
                xmlmsg.Element("simpleTextPrintDocumentTextNode", text=self.text)
            )
        else:
            raise ValueError(
                f"printDocument {self.document_id} has no payload; set pdf or text"
            )
        return element


@dataclass
class PrintDocumentResult:
    """One ``<printDocumentResult>`` from a print response (Listing 30.42)."""

    document_id: int
    result: str

    @property
    def ok(self) -> bool:
        return self.result.startswith(results.OK)


class Printer:
    """Standard Mode printing on a PR device (section 30.15)."""

    def __init__(self, session: DeviceSession) -> None:
        if session.mode is not results.InterfaceMode.STANDARD:
            raise CuppsError(
                f"Standard Mode printing needs a standard-mode session, "
                f"got {session.mode.value}"
            )
        self.session = session

    @property
    def stocks(self) -> tuple:
        """Stocks the device reported in its descriptor."""
        return self.session.device.stocks

    def print_documents(
        self, documents: list[PrintDocument]
    ) -> list[PrintDocumentResult]:
        """Send a ``<printRequest>`` and return the per-document results.

        A partially successful request comes back as ``oneOrMoreIssues`` with
        one result per document, so the caller inspects the list rather than
        the envelope result.
        """
        if not documents:
            return []
        body = xmlmsg.Element("printRequest")
        for document in documents:
            body.add(document.to_element())

        response = self.session._request(  # noqa: SLF001
            "printRequest",
            body=body,
            timeout=params.PR_MAX_RESPONSE_TIME,
            expect_ok=False,
        )
        outcomes = [
            PrintDocumentResult(
                document_id=element.get_int("documentID", 0) or 0,
                result=element.get("result", "") or "",
            )
            for element in response.body.findall("printDocumentResult")
        ]
        if not outcomes and not response.is_ok:
            raise RequestFailed("printRequest", response.result or "<missing>")
        return outcomes

    def print_pdf(self, pdf: bytes, stock_name: str) -> PrintDocumentResult:
        """Convenience wrapper for a single PDF document."""
        outcomes = self.print_documents(
            [PrintDocument(document_id=1, stock_name=stock_name, pdf=pdf)]
        )
        return outcomes[0] if outcomes else PrintDocumentResult(1, results.OK)

    def print_text(self, text: str, stock_name: str) -> PrintDocumentResult:
        """Convenience wrapper for a single plain-text document."""
        outcomes = self.print_documents(
            [PrintDocument(document_id=1, stock_name=stock_name, text=text)]
        )
        return outcomes[0] if outcomes else PrintDocumentResult(1, results.OK)

    def cancel(self, document_id: int) -> str:
        """Cancel a queued document with ``<printCancelRequest>`` (30.15.5)."""
        response = self.session._request(  # noqa: SLF001
            "printCancelRequest",
            attrs={"documentID": str(document_id)},
            timeout=params.PR_MAX_RESPONSE_TIME,
            expect_ok=False,
        )
        return response.result or "<missing>"


class AeaPrinter:
    """BP and BT printing, which are AEA Mode only (Table 3.3, note 5).

    Standard Mode printing uses the PR device; boarding-pass and bag-tag
    printers take AEA command streams instead.
    """

    def __init__(self, session: DeviceSession) -> None:
        if session.mode is not results.InterfaceMode.AEA:
            raise CuppsError(
                f"{session.device.name} must be opened in AEA mode, "
                f"got {session.mode.value}"
            )
        self.session = session

    def send(self, *parts: object) -> xmlmsg.Message:
        """Send raw AEA command parts (text and/or binary)."""
        return self.session.aea(*parts)

    def print_aea(self, command: str) -> xmlmsg.Message:
        """Send one AEA command string."""
        return self.session.aea(command)


# --------------------------------------------------------------------------
# ZL -- logging device (section 30.20)
# --------------------------------------------------------------------------


class LogDevice:
    """The platform logging device, ZL.

    Opened in Special Mode, which does not support locking: attempting to lock
    a Special Mode device draws an ``illogicalMessageErrorEvent`` (30.20 note).
    Applications may only write to application-scope logs (30.20.1 CRITICAL).
    """

    def __init__(self, session: DeviceSession) -> None:
        self.session = session
        self._open_logs: dict[str, int] = {}
        self._entry_id = 0
        self._entry_lock = threading.Lock()

    def open(
        self,
        log_id: str,
        security_token: str,
        scope: results.LogScope = results.LogScope.APPLICATION,
    ) -> None:
        """Open a log (section 30.20.1).

        The security token is recorded with every entry and must be presented
        again to retrieve them, which is what lets successive runs of an
        application read back a single chronological history.
        """
        if scope is results.LogScope.PLATFORM:
            raise CuppsError(
                "applications may not write to platform-scope logs; the "
                "platform answers notAuthorized (section 30.20.1)"
            )
        self.session._request(  # noqa: SLF001
            "logOpenRequest",
            attrs={
                "logID": log_id,
                "securityToken": security_token,
                "scope": scope.value,
            },
            timeout=params.MAX_DEV_STS_TIME,
        )
        self._open_logs[log_id] = 0

    def write(
        self,
        log_id: str,
        entries: list[tuple[str, results.LogSeverity]],
        *,
        sync: bool = False,
    ) -> None:
        """Write entries to an open log (section 30.20.2).

        Writes are asynchronous by default; ``sync=True`` asks the platform to
        commit before responding.
        """
        if log_id not in self._open_logs:
            raise CuppsError(f"log {log_id!r} is not open")
        body = xmlmsg.Element("logWriteRequest", {"logID": log_id})
        if sync:
            body.attrs["sync"] = "true"
        for text, severity in entries:
            with self._entry_lock:
                self._entry_id += 1
                entry_id = self._entry_id
            body.add(
                xmlmsg.Element(
                    "logMessage",
                    {"logEntryID": str(entry_id), "severity": severity.value},
                    text=text,
                )
            )
        self.session._request(  # noqa: SLF001
            "logWriteRequest", body=body, timeout=params.MAX_DEV_STS_TIME
        )

    def write_one(
        self,
        log_id: str,
        text: str,
        severity: results.LogSeverity = results.LogSeverity.NORMAL,
    ) -> None:
        self.write(log_id, [(text, severity)])

    def retrieve(self, log_id: str, security_token: str) -> list[str]:
        """Read entries back with ``<logRetrieveRequest>`` (section 30.20)."""
        response = self.session._request(  # noqa: SLF001
            "logRetrieveRequest",
            attrs={"logID": log_id, "securityToken": security_token},
            timeout=params.PR_MAX_RESPONSE_TIME,
        )
        return [element.text or "" for element in response.body.iter("logMessage")]

    def close(self, log_id: str) -> None:
        """Close a log with ``<logCloseRequest>``."""
        if log_id not in self._open_logs:
            return
        try:
            self.session._request(  # noqa: SLF001
                "logCloseRequest",
                attrs={"logID": log_id},
                timeout=params.MAX_DEV_STS_TIME,
            )
        finally:
            self._open_logs.pop(log_id, None)

    def close_all(self) -> None:
        for log_id in list(self._open_logs):
            self.close(log_id)
