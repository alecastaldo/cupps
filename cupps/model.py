"""Typed views over the device and platform descriptors the platform returns.

The ``<authenticateResponse>`` (Listing 29.6) carries the whole run-time
picture: platform identity and crypto options, the application's storage
areas, the workstation, and the device list including sub-devices.  This
module turns that XML into objects the rest of the application can use
without re-walking the tree.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterator, Optional

from . import xmlmsg
from .results import STATUS_FLAGS_BY_DEVICE, InterfaceMode

#: Device types defined by Table 3.3, with their long names.
DEVICE_TYPES = {
    "BC": "Barcode reader",
    "BD": "Baggage Drop Device",
    "BG": "Boarding Gate Reader",
    "BP": "Boarding Pass Printer",
    "BT": "Baggage Tag Printer",
    "EP": "Electronic Payment Device Subsystem",
    "MS": "Magnetic Stripe Reader",
    "OC": "Optical Character Reader",
    "PR": "Printer",
    "RW": "Raw",
    "SD": "Scale Device",
    "SN": "Snapshot Device",
    "ZI": "IATA Message Software Device",
    "ZL": "Logging Software Device",
}

#: Device types that are reader-like: data arrives asynchronously and is
#: collected with <readerReadRequest> (section 30.9).
READER_TYPES = frozenset({"BC", "MS", "OC", "SD", "SN"})

#: Device types that print.
PRINTER_TYPES = frozenset({"BP", "BT", "PR"})

#: Macro devices whose lock implicitly covers their sub-devices (26.13.3).
MACRO_TYPES = frozenset({"BG"})


@dataclass(frozen=True)
class DeviceStatus:
    """The ``<xxStatus>`` flags for one device (Table 30.2)."""

    ready: bool = False
    unknown: bool = True
    init: bool = False
    power_off: bool = False
    paper_out: bool = False
    paper_jam: bool = False
    disk_error: bool = False
    desc: str = ""

    @classmethod
    def from_element(cls, element: xmlmsg.Element) -> "DeviceStatus":
        return cls(
            ready=element.get_bool("ready"),
            unknown=element.get_bool("unknown", default=True),
            init=element.get_bool("init"),
            power_off=element.get_bool("powerOff"),
            paper_out=element.get_bool("paperOut"),
            paper_jam=element.get_bool("paperJam"),
            disk_error=element.get_bool("diskError"),
            desc=element.get("desc", "") or "",
        )

    @property
    def usable(self) -> bool:
        """True when the device can accept work right now."""
        return self.ready and not (self.unknown or self.power_off)

    def summary(self, device_type: str = "") -> str:
        """One-line status for the agent's device panel (section 11.2.5)."""
        if self.init:
            return "Initialising"
        if self.power_off:
            return "Powered off / disconnected"
        if self.unknown:
            return "Unknown"
        faults = []
        if self.paper_jam:
            faults.append("paper jam")
        if self.paper_out:
            faults.append("out of paper")
        if self.disk_error:
            faults.append("disk error")
        if self.ready:
            return "Ready" + (f" ({', '.join(faults)})" if faults else "")
        return ", ".join(faults).capitalize() if faults else "Not ready"

    def flags_for(self, device_type: str) -> dict[str, bool]:
        """Only the flags Table 30.1 defines for ``device_type``."""
        applicable = STATUS_FLAGS_BY_DEVICE.get(device_type.upper(), ())
        values = {
            "ready": self.ready,
            "unknown": self.unknown,
            "init": self.init,
            "powerOff": self.power_off,
            "paperOut": self.paper_out,
            "paperJam": self.paper_jam,
            "diskError": self.disk_error,
        }
        return {flag: values[flag] for flag in applicable}


@dataclass(frozen=True)
class SupportedStock:
    """One entry of ``<supportedStocks>`` on a PR device (Listing 29.6).

    Dimensions and margins are in millimetres, matching the standard stock
    coordinate system of section 26.14.
    """

    stock_name: str
    supports_color: str = "none"
    width: float = 0.0
    height: float = 0.0
    left_margin: float = 0.0
    right_margin: float = 0.0
    top_margin: float = 0.0
    bottom_margin: float = 0.0
    perforation_left: float = 0.0

    @classmethod
    def from_element(cls, element: xmlmsg.Element) -> "SupportedStock":
        def number(name: str) -> float:
            try:
                return float(element.get(name, "0") or 0)
            except ValueError:
                return 0.0

        return cls(
            stock_name=element.get("stockName", "") or "",
            supports_color=element.get("supportsColor", "none") or "none",
            width=number("stockWidth"),
            height=number("stockHeight"),
            left_margin=number("leftMargin"),
            right_margin=number("rightMargin"),
            top_margin=number("topMargin"),
            bottom_margin=number("bottomMargin"),
            perforation_left=number("perforationLeft"),
        )


@dataclass
class Device:
    """One logical device from the platform's device list."""

    name: str
    index: str
    parameter_type: str
    status: DeviceStatus = field(default_factory=DeviceStatus)
    modes: frozenset[InterfaceMode] = frozenset()
    vendor: str = ""
    model: str = ""
    misc_info: str = ""
    host_name: str = ""
    ip: str = ""
    ipv6: str = ""
    port: int = 0
    capabilities: str = ""
    supports_color: str = "none"
    stocks: tuple[SupportedStock, ...] = ()
    functional_groups: tuple[str, ...] = ()
    sub_devices: list["Device"] = field(default_factory=list)
    #: Raw parameter element, for attributes this model does not surface.
    raw: Optional[xmlmsg.Element] = None

    @property
    def device_type(self) -> str:
        """Two-letter device type, e.g. ``BC`` (Table 3.3).

        Derived from ``deviceParameterType`` (``bcDeviceParameter`` -> ``BC``)
        which is authoritative, rather than by slicing the device name.
        """
        if self.parameter_type.endswith("DeviceParameter"):
            return self.parameter_type[: -len("DeviceParameter")].upper()
        # Fall back to the trailing type+index of the name (section 4.6.1).
        for code in DEVICE_TYPES:
            if code in self.name.upper():
                return code
        return ""

    @property
    def type_name(self) -> str:
        return DEVICE_TYPES.get(self.device_type, "Unknown device")

    @property
    def is_reader(self) -> bool:
        return self.device_type in READER_TYPES

    @property
    def is_printer(self) -> bool:
        return self.device_type in PRINTER_TYPES

    @property
    def is_macro(self) -> bool:
        return self.device_type in MACRO_TYPES

    def supports(self, mode: InterfaceMode) -> bool:
        return mode in self.modes

    def walk(self) -> Iterator["Device"]:
        """This device and every sub-device, depth first."""
        yield self
        for sub in self.sub_devices:
            yield from sub.walk()

    @classmethod
    def from_element(cls, element: xmlmsg.Element) -> "Device":
        parameter_type = element.get("deviceParameterType", "") or ""
        parameters = element.find(parameter_type) if parameter_type else None

        modes = set()
        modes_element = element.find("supportedInterfaceModes")
        if modes_element is not None:
            for mode_element in modes_element.findall("interfaceMode"):
                raw_mode = mode_element.get("mode")
                try:
                    modes.add(InterfaceMode(raw_mode))
                except ValueError:
                    continue

        device = cls(
            name=element.get("deviceName", "") or "",
            index=element.get("deviceIndex", "") or "",
            parameter_type=parameter_type,
            modes=frozenset(modes),
            raw=parameters,
        )

        if parameters is not None:
            vendor_info = parameters.find("vendorModelInfo")
            if vendor_info is not None:
                device.vendor = vendor_info.get("vendor", "") or ""
                device.model = vendor_info.get("model", "") or ""
                device.misc_info = vendor_info.get("miscInfo", "") or ""

            ip_and_port = parameters.find("ipAndPort")
            if ip_and_port is not None:
                device.host_name = ip_and_port.get("hostName", "") or ""
                device.ip = ip_and_port.get("ip", "") or ""
                device.ipv6 = ip_and_port.get("ipv6", "") or ""
                device.port = ip_and_port.get_int("port", 0) or 0

            device.supports_color = parameters.get("supportsColor", "none") or "none"
            # Capability strings differ by device: bdCapabilities, poCapabilities
            # on a BG, msCapabilities on an MS (Listing 29.6).
            for attribute, value in parameters.attrs.items():
                if attribute.endswith("Capabilities"):
                    device.capabilities = value
                    break

            status_element = _find_status(parameters)
            if status_element is not None:
                device.status = DeviceStatus.from_element(status_element)

            stocks_element = parameters.find("supportedStocks")
            if stocks_element is not None:
                device.stocks = tuple(
                    SupportedStock.from_element(stock)
                    for stock in stocks_element.findall("supportedStock")
                )

        groups_element = element.find("functionalGroupList")
        if groups_element is not None:
            device.functional_groups = tuple(
                group.get("group", "") or ""
                for group in groups_element.findall("functionalGroup")
            )

        # Sub-devices are nested <device> elements inside a macro device's
        # <device> element (Listing 29.6, BG1 carrying BC2 and MS2).
        for child in element.findall("device"):
            device.sub_devices.append(cls.from_element(child))

        return device


def _find_status(parameters: xmlmsg.Element) -> Optional[xmlmsg.Element]:
    """Locate the ``<xxStatus>`` child of a device parameter element."""
    for child in parameters.children:
        if child.name.endswith("Status"):
            return child
    return None


@dataclass(frozen=True)
class Storage:
    """One ``<storage>`` entry of the application parameters (Listing 29.6)."""

    storage_type: str
    path: str
    drive_letter: str = ""


@dataclass(frozen=True)
class PlatformInfo:
    """``<platformParameter>`` from the authenticate response."""

    vendor: str = ""
    version: str = ""
    time: str = ""
    default_crypt_algorithm: str = ""
    available_crypt_algorithms: tuple[str, ...] = ()
    run_ipv4: bool = True
    run_ipv6: bool = False


@dataclass(frozen=True)
class WorkstationInfo:
    """``<workstationParameter>`` from the authenticate response."""

    name: str = ""
    time: str = ""
    location_description: str = ""
    default_spooled_printer: str = ""
    vendor: str = ""
    model: str = ""
    misc_info: str = ""


@dataclass
class RuntimeEnvironment:
    """Everything the platform told us at authentication time."""

    device_token: str
    platform: PlatformInfo
    workstation: WorkstationInfo
    storage: dict[str, Storage] = field(default_factory=dict)
    devices: list[Device] = field(default_factory=list)

    def all_devices(self) -> Iterator[Device]:
        """Every device including sub-devices, depth first."""
        for device in self.devices:
            yield from device.walk()

    def by_name(self, name: str) -> Optional[Device]:
        target = name.upper()
        for device in self.all_devices():
            if device.name.upper() == target:
                return device
        return None

    def of_type(self, device_type: str) -> list[Device]:
        """Every device of ``device_type``, in platform-reported order."""
        target = device_type.upper()
        return [d for d in self.all_devices() if d.device_type == target]

    def first_of_type(self, device_type: str) -> Optional[Device]:
        devices = self.of_type(device_type)
        return devices[0] if devices else None

    @classmethod
    def from_response(cls, message: xmlmsg.Message) -> "RuntimeEnvironment":
        """Build from an ``<authenticateResponse>`` message."""
        body = message.body
        platform_element = body.find("platformParameter")
        platform = PlatformInfo()
        if platform_element is not None:
            default_algorithm = ""
            available: list[str] = []
            crypt_info = platform_element.find("cryptAlgorithmInfo")
            if crypt_info is not None:
                default_element = crypt_info.find("defaultCryptAlgorithm")
                if default_element is not None:
                    algorithm = default_element.find("cryptAlgorithm")
                    if algorithm is not None:
                        default_algorithm = algorithm.get("name", "") or ""
                available_element = crypt_info.find("availableCryptAlgorithms")
                if available_element is not None:
                    available = [
                        algorithm.get("name", "") or ""
                        for algorithm in available_element.findall("cryptAlgorithm")
                    ]
            ip_support = platform_element.find("ipVersionSupport")
            platform = PlatformInfo(
                vendor=platform_element.get("platformVendor", "") or "",
                version=platform_element.get("platformVersion", "") or "",
                time=platform_element.get("platformTime", "") or "",
                default_crypt_algorithm=default_algorithm,
                available_crypt_algorithms=tuple(a for a in available if a),
                run_ipv4=ip_support.get_bool("runIPv4", True) if ip_support else True,
                run_ipv6=ip_support.get_bool("runIPv6", False) if ip_support else False,
            )

        workstation_element = body.find("workstationParameter")
        workstation = WorkstationInfo()
        if workstation_element is not None:
            vendor_info = workstation_element.find("vendorModelInfo")
            workstation = WorkstationInfo(
                name=workstation_element.get("name", "") or "",
                time=workstation_element.get("wsTime", "") or "",
                location_description=workstation_element.get("locDesc", "") or "",
                default_spooled_printer=workstation_element.get(
                    "defaultSpooledPrinter", ""
                )
                or "",
                vendor=vendor_info.get("vendor", "") or "" if vendor_info else "",
                model=vendor_info.get("model", "") or "" if vendor_info else "",
                misc_info=vendor_info.get("miscInfo", "") or "" if vendor_info else "",
            )

        storage: dict[str, Storage] = {}
        application_element = body.find("applicationParameter")
        if application_element is not None:
            for element in application_element.findall("storage"):
                storage_type = element.get("storageT", "") or ""
                storage[storage_type] = Storage(
                    storage_type=storage_type,
                    path=element.get("path", "") or "",
                    drive_letter=element.get("driveLetter", "") or "",
                )

        devices: list[Device] = []
        device_list = body.find("deviceList")
        if device_list is not None:
            devices = [
                Device.from_element(element)
                for element in device_list.findall("device")
            ]

        return cls(
            device_token=body.get("deviceToken", "") or "",
            platform=platform,
            workstation=workstation,
            storage=storage,
            devices=devices,
        )
