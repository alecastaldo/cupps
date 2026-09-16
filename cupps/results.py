"""Result codes, error events and status vocabulary from TS 01.04.0004."""

from __future__ import annotations

from enum import Enum

# --- Generic ------------------------------------------------------------
OK = "OK"

# --- deviceAcquireResponse (section 30.6.1) -----------------------------
ACQUIRE_OK = "OK"
ACQUIRE_INVALID_DEVICE = "invalidDevice"
ACQUIRE_INVALID_TOKEN = "invalidToken"
ACQUIRE_INVALID_FOID_MASKING = "invalidFoidMasking"

# --- deviceLockResponse (section 30.7) ----------------------------------
LOCK_OK = "OK"
LOCK_OK_ALREADY_LOCKED = "OK-deviceAlreadyLocked"
LOCK_DEVICE_LOCKED = "deviceLocked"
LOCK_SWITCHING_METHODS_NOT_ALLOWED = "switchingLockMethodsNotAllowed"

# --- deviceStatusResponse (section 30.2) --------------------------------
STATUS_POLLING_TOO_FAST = "pollingTooFast"

# --- interfaceModeResponse (section 30.1) -------------------------------
MODE_NOT_SUPPORTED = "modeNotSupportedForThisDevice"

# --- printResponse / printDocumentResult (section 30.15) ----------------
PRINT_ONE_OR_MORE_ISSUES = "oneOrMoreIssues"
PRINT_PAPER_OUT = "paperOut"
PRINT_NOT_READY = "notReady"

# --- logOpen / logWrite (section 30.20) ---------------------------------
LOG_NOT_AUTHORIZED = "notAuthorized"
LOG_CANNOT_WRITE_PLATFORM_SCOPE = "cannotWriteToPlatformScopeLog"


class LockMethod(str, Enum):
    """``lockMethod`` values for ``<deviceLockRequest>`` (section 26.13.1)."""

    BY_CONNECTION = "byConnection"
    BY_DEVICE_TOKEN = "byDeviceToken"


class InterfaceMode(str, Enum):
    """``mode`` values for ``<interfaceModeRequest>`` (section 10.2)."""

    STANDARD = "standard"
    AEA = "aea"
    RAW = "raw"
    SPECIAL = "special"


class CryptAlgorithm(str, Enum):
    """Encryption algorithms offered by the platform (section 30.3)."""

    AES_STRONG = "aes-strong"
    DES_WEAK = "des-weak"


class LogScope(str, Enum):
    """``scope`` values for ``<logOpenRequest>`` (section 30.20.1)."""

    APPLICATION = "application"
    PLATFORM = "platform"


class LogSeverity(str, Enum):
    """``severity`` values for ``<logMessage>`` (Listing 30.57)."""

    NORMAL = "normal"
    WARNING = "warning"
    CRITICAL = "critical"


class SessionErrorType(str, Enum):
    """``eventType`` values for ``<sessionErrorEvent>`` (section 26.6)."""

    HEADER_VERSION = "headerVersion"
    HEADER_LENGTH = "headerLength"
    HEADER_TIMEOUT = "headerTimeout"
    BODY_TIMEOUT = "bodyTimeout"
    BODY_PARSE_FAILURE = "bodyParseFailure"


#: Device status flags and the device types each applies to, from Table 30.1
#: and Table 30.2.  Applications must display these on screen (section 11.2.5).
DEVICE_STATUS_FLAGS = ("init", "paperJam", "paperOut", "powerOff", "ready",
                       "unknown", "diskError")

#: Status flags carried by each device type's ``<xxStatus>`` element, Table 30.1.
STATUS_FLAGS_BY_DEVICE = {
    "BC": ("init", "powerOff", "ready", "unknown"),
    "BD": ("init", "powerOff", "ready", "unknown"),
    "BG": ("init", "powerOff", "ready", "unknown"),
    "BP": ("init", "paperJam", "paperOut", "powerOff", "ready", "unknown"),
    "BT": ("init", "paperJam", "paperOut", "powerOff", "ready", "unknown"),
    "EP": ("init", "powerOff", "ready", "unknown"),
    "MS": ("init", "powerOff", "ready", "unknown"),
    "OC": ("init", "powerOff", "ready", "unknown"),
    "PR": ("init", "paperJam", "paperOut", "powerOff", "ready", "unknown"),
    "RW": ("init", "ready", "unknown"),
    "SD": ("init", "powerOff", "ready", "unknown"),
    "SN": ("init", "powerOff", "ready", "unknown"),
    "ZI": ("init", "ready", "unknown"),
    "ZL": ("diskError", "init", "ready", "unknown"),
}

#: Interface modes each device type supports, from Table 30.1.
MODES_BY_DEVICE = {
    "BC": {InterfaceMode.STANDARD},
    "BD": {InterfaceMode.AEA},
    "BG": {InterfaceMode.AEA, InterfaceMode.STANDARD},
    "BP": {InterfaceMode.AEA},
    "BT": {InterfaceMode.AEA},
    "EP": {InterfaceMode.STANDARD},
    "MS": {InterfaceMode.STANDARD},
    "OC": {InterfaceMode.STANDARD},
    "PR": {InterfaceMode.STANDARD},
    "RW": {InterfaceMode.RAW},
    "SD": {InterfaceMode.AEA, InterfaceMode.STANDARD},
    "SN": {InterfaceMode.AEA, InterfaceMode.STANDARD},
    "ZI": {InterfaceMode.SPECIAL},
    "ZL": {InterfaceMode.SPECIAL},
}

#: Barcode symbology codes returned in ``bcTypeCode`` (Table 30.3).
BARCODE_TYPES = {
    "1": "1D Code Interleaved 2 of 5",
    "2": "1D Code Industrial 2 of 5",
    "3": "1D Code 39",
    "4": "2D Data Matrix",
    "5": "QR",
    "6": "2D PDF417",
    "7": "1D Code 128 with check digit",
    "8": "1D Code 39 with check digit",
    "9": "1D Industrial 2 of 5 with check digit",
    "0": "1D Interleaved 2 of 5 with check digit",
    "A": "EAN 13 with check digit from application",
    "V": "2D Aztec",
    "u": "unknown barcode type not defined in AEA",
}
