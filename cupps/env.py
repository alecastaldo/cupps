"""CUPPS run-time environment variables (TS 01.04.0004 section 6.3.15).

The platform builds the environment before launching an application, layering
workstation, user and application values in that order (section 6.3.15).  An
application reads its platform address, storage areas and assigned device
names from here rather than from configuration of its own -- that is what
makes the same binary run unchanged at any CUPPS airport.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Mapping, Optional

#: Every variable of Table 6.6, with a short description.
STANDARD_VARIABLES = {
    "CUPPSACN": "Alternate computer name for this workstation",
    "CUPPSAL2": "IATA associated airline ID (2 character)",
    "CUPPSAL3": "ICAO associated airline ID (3 character)",
    "CUPPSALA": "IATA accounting code",
    "CUPPSAPD": "Application path, drive letter and path",
    "CUPPSAPL": "Application path drive letter",
    "CUPPSAPU": "Application path as UNC",
    "CUPPSBNI": "Bastion node IP address",
    "CUPPSBNP": "Bastion node port",
    "CUPPSCN": "Windows computer name",
    "CUPPSDSP": "Default Windows Spooler printer",
    "CUPPSPGSD": "Persistent global storage, drive letter and path",
    "CUPPSPGSL": "Persistent global storage, drive letter",
    "CUPPSPGSU": "Persistent global storage, UNC",
    "CUPPSPLSD": "Persistent local storage, drive letter and path",
    "CUPPSPLSL": "Persistent local storage, drive letter",
    "CUPPSPLSU": "Persistent local storage, UNC",
    "CUPPSPLT": "Platform parameter",
    "CUPPSPN": "Platform node to connect to",
    "CUPPSPP": "Platform port to connect to",
    "CUPPSTSD": "Transient storage, drive letter and path",
    "CUPPSTSL": "Transient storage, drive letter",
    "CUPPSTSU": "Transient storage, UNC",
    "CUPPSUN": "Logical user name for this session",
    "CUPPSXSDD": "XSD location, drive letter and path",
    "CUPPSXSDL": "XSD location, drive letter",
    "CUPPSXSDU": "XSD location, UNC",
}

#: Device assignment variables are ``CUPPS`` + two-letter type + index,
#: e.g. ``CUPPSPR1`` holds the name of the first assigned printer.
_DEVICE_VARIABLE_RE = re.compile(r"^CUPPS([A-Z]{2})(\d+)$")

#: Variables that are not device assignments despite matching the shape above.
_NON_DEVICE = frozenset(STANDARD_VARIABLES)


class EnvironmentError_(RuntimeError):
    """A required CUPPS environment variable is missing or malformed."""


@dataclass(frozen=True)
class CuppsEnvironment:
    """A snapshot of the CUPPS run-time environment."""

    values: Mapping[str, str]

    @classmethod
    def from_os(cls, overrides: Optional[Mapping[str, str]] = None) -> "CuppsEnvironment":
        """Read the process environment, optionally overlaying ``overrides``.

        Overrides exist so a test bench or the bundled simulator can stand in
        for a platform without having to mutate the real environment.
        """
        values = {
            key: value
            for key, value in os.environ.items()
            if key.upper().startswith("CUPPS")
        }
        if overrides:
            values.update(overrides)
        return cls(values=values)

    def get(self, name: str, default: Optional[str] = None) -> Optional[str]:
        return self.values.get(name.upper(), default)

    def require(self, name: str) -> str:
        """Read a variable that the application cannot run without."""
        value = self.values.get(name.upper())
        if not value:
            raise EnvironmentError_(
                f"{name} is not set; a CUPPS platform sets it before launching "
                f"an application (section 6.3.15). Set it manually only when "
                f"running against a test platform."
            )
        return value

    # -- platform address -------------------------------------------------

    @property
    def platform_node(self) -> str:
        """``CUPPSPN`` -- the host to open the platform connection to."""
        return self.require("CUPPSPN")

    @property
    def platform_port(self) -> int:
        """``CUPPSPP`` -- the platform's listening port.

        Section 26.5 requires it to fall in the IETF RFC 6335 user-port range.
        """
        raw = self.require("CUPPSPP")
        try:
            port = int(raw)
        except ValueError as exc:
            raise EnvironmentError_(f"CUPPSPP={raw!r} is not a port number") from exc
        if not 1024 <= port <= 49151:
            raise EnvironmentError_(
                f"CUPPSPP={port} is outside the RFC 6335 user port range "
                f"1024-49151 required by section 26.5"
            )
        return port

    # -- identity ---------------------------------------------------------

    @property
    def airline(self) -> str:
        """``CUPPSAL2`` -- the IATA airline code this session serves."""
        return self.get("CUPPSAL2", "") or ""

    @property
    def airline_icao(self) -> str:
        return self.get("CUPPSAL3", "") or ""

    @property
    def computer_name(self) -> str:
        return self.get("CUPPSCN", "") or ""

    @property
    def user_name(self) -> str:
        return self.get("CUPPSUN", "") or ""

    @property
    def default_spooler_printer(self) -> str:
        return self.get("CUPPSDSP", "") or ""

    # -- storage ----------------------------------------------------------

    @property
    def persistent_local_path(self) -> str:
        return self.get("CUPPSPLSD", "") or ""

    @property
    def persistent_global_path(self) -> str:
        return self.get("CUPPSPGSD", "") or ""

    @property
    def transient_path(self) -> str:
        return self.get("CUPPSTSD", "") or ""

    @property
    def xsd_path(self) -> str:
        """Where the platform published the interface XSDs."""
        return self.get("CUPPSXSDD", "") or ""

    # -- device assignments -----------------------------------------------

    def devices(self) -> dict[str, str]:
        """Assigned devices as ``{variable: deviceName}`` (Table 6.6).

        For example ``CUPPSPR1 -> XYZZLACKI001PR1``.
        """
        found: dict[str, str] = {}
        for key, value in self.values.items():
            key = key.upper()
            if key in _NON_DEVICE:
                continue
            if _DEVICE_VARIABLE_RE.match(key) and value:
                found[key] = value
        return found

    def device_names(self, device_type: str) -> list[str]:
        """Assigned device names of ``device_type``, in index order."""
        prefix = f"CUPPS{device_type.upper()}"
        matches: list[tuple[int, str]] = []
        for key, value in self.devices().items():
            if not key.startswith(prefix):
                continue
            match = _DEVICE_VARIABLE_RE.match(key)
            if match and match.group(1) == device_type.upper():
                matches.append((int(match.group(2)), value))
        return [name for _, name in sorted(matches)]

    def describe(self) -> str:
        """A readable dump for support staff and the CUPPSIT tool."""
        lines = []
        for name in sorted(STANDARD_VARIABLES):
            value = self.values.get(name)
            if value:
                lines.append(f"{name:<12}{value}")
        devices = self.devices()
        if devices:
            lines.append("")
            lines.append("Assigned devices:")
            for name in sorted(devices):
                lines.append(f"{name:<12}{devices[name]}")
        return "\n".join(lines) if lines else "(no CUPPS environment variables set)"
