"""Binding logical CUPPS devices to physical peripherals.

A binding says: the device the platform calls ``LHRT4LB00302PR1`` is a PR
reached over serial on COM17 at 19200 baud.  It is configuration, not code,
for the same reason device profiles are -- a station that swaps a printer
should not need a release, and under section 17.1.2 a release that touches
the wire-facing layer costs a certification cycle.

Bindings live in JSON alongside the profiles::

    {
      "device": "LHRT4LB00302PR1",
      "deviceType": "PR",
      "driver": "aea",
      "transport": {"kind": "serial", "port": "COM17", "baudrate": 19200}
    }
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Optional

from .aea import AeaDriver
from .base import DeviceDriver
from .printer import PrintDriver
from .reader import ReaderDriver
from .transport import Transport, TransportConfig, build_transport

log = logging.getLogger("cuppsplatform.drivers.registry")

#: Driver implementations, by the name a binding uses.
DRIVERS: dict[str, type[DeviceDriver]] = {
    "aea": AeaDriver,
    "reader": ReaderDriver,
    "print": PrintDriver,
}

#: The driver to use when a binding does not name one, by device type.
DEFAULT_DRIVER_BY_TYPE = {
    "BC": "reader", "MS": "reader", "OC": "reader", "SD": "reader",
    "BP": "aea", "BT": "aea", "BG": "aea", "SN": "aea",
    "PR": "print",
}


class BindingError(ValueError):
    """A device binding is malformed or names something unknown."""


def register_driver(name: str, driver: type[DeviceDriver]) -> None:
    """Add a driver implementation.

    A site with a peripheral nothing here covers writes a driver, registers
    it under a name, and binds devices to it by that name -- without editing
    anything shipped.
    """
    DRIVERS[name] = driver


@dataclass(frozen=True)
class DeviceBinding:
    """One logical device mapped onto one physical peripheral."""

    device_name: str
    device_type: str
    driver: str
    transport: TransportConfig
    options: dict = field(default_factory=dict)
    source: Optional[Path] = None

    @classmethod
    def from_dict(
        cls, payload: dict, *, source: Optional[Path] = None
    ) -> "DeviceBinding":
        where = f" in {source}" if source else ""
        for required in ("device", "deviceType", "transport"):
            if required not in payload:
                raise BindingError(f"binding is missing {required!r}{where}")

        device_type = str(payload["deviceType"]).upper()
        driver = str(
            payload.get("driver") or DEFAULT_DRIVER_BY_TYPE.get(device_type, "")
        )
        if not driver:
            raise BindingError(
                f"no driver named for {device_type} and no default exists; "
                f"set \"driver\"{where}"
            )
        if driver not in DRIVERS:
            raise BindingError(
                f"unknown driver {driver!r}{where}; registered drivers are "
                f"{sorted(DRIVERS)}"
            )

        raw_transport = dict(payload["transport"])
        kind = str(raw_transport.pop("kind", ""))
        if not kind:
            raise BindingError(f"transport is missing 'kind'{where}")
        known = {f.name for f in TransportConfig.__dataclass_fields__.values()}
        extra = {k: v for k, v in raw_transport.items() if k not in known}
        fields = {k: v for k, v in raw_transport.items() if k in known}
        transport = TransportConfig(kind=kind, extra=extra, **fields)

        supported = DRIVERS[driver].device_types
        if supported and device_type not in supported:
            raise BindingError(
                f"driver {driver!r} does not serve {device_type} devices"
                f"{where}; it serves {sorted(supported)}"
            )

        return cls(
            device_name=str(payload["device"]),
            device_type=device_type,
            driver=driver,
            transport=transport,
            options=dict(payload.get("options", {})),
            source=source,
        )

    def build(self, **handlers) -> DeviceDriver:
        """Construct the driver and its transport, without opening them."""
        transport: Transport = build_transport(self.transport)
        driver_class = DRIVERS[self.driver]
        return driver_class(
            self.device_name,
            self.device_type,
            transport,
            **{**self.options, **handlers},
        )

    def describe(self) -> str:
        return (
            f"{self.device_name} ({self.device_type}) -> {self.driver} on "
            f"{self.transport.kind}:"
            f"{self.transport.port or self.transport.host}"
        )


class BindingRegistry:
    """The loaded set of device bindings."""

    def __init__(self, bindings: Optional[list[DeviceBinding]] = None) -> None:
        self._bindings: dict[str, DeviceBinding] = {
            binding.device_name.upper(): binding for binding in (bindings or [])
        }

    def __len__(self) -> int:
        return len(self._bindings)

    def __iter__(self) -> Iterator[DeviceBinding]:
        return iter(
            sorted(self._bindings.values(), key=lambda b: b.device_name)
        )

    @classmethod
    def load(cls, *directories: Path) -> "BindingRegistry":
        """Load ``*.json`` bindings from each directory, in order.

        A later directory overrides an earlier one for the same device, so a
        lab bench can point a device at a PTY without touching site config.
        """
        bindings: dict[str, DeviceBinding] = {}
        for directory in directories:
            directory = Path(directory)
            if not directory.is_dir():
                log.debug("binding directory %s does not exist", directory)
                continue
            for path in sorted(directory.glob("*.json")):
                try:
                    payload = json.loads(path.read_text(encoding="utf-8"))
                except json.JSONDecodeError as exc:
                    raise BindingError(f"{path} is not valid JSON: {exc}") from exc
                entries = payload if isinstance(payload, list) else [payload]
                for entry in entries:
                    binding = DeviceBinding.from_dict(entry, source=path)
                    bindings[binding.device_name.upper()] = binding
        log.info("loaded %d device binding(s)", len(bindings))
        return cls(list(bindings.values()))

    def get(self, device_name: str) -> Optional[DeviceBinding]:
        return self._bindings.get(device_name.upper())

    def require(self, device_name: str) -> DeviceBinding:
        binding = self.get(device_name)
        if binding is None:
            raise BindingError(
                f"no binding for device {device_name!r}; bound devices are "
                f"{[b.device_name for b in self]}"
            )
        return binding

    def add(self, binding: DeviceBinding) -> None:
        self._bindings[binding.device_name.upper()] = binding

    def of_type(self, device_type: str) -> list[DeviceBinding]:
        return [b for b in self if b.device_type == device_type.upper()]
