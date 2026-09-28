"""Device profiles: peripheral behaviour as data rather than code.

The specification is candid that identical device types behave differently in
the field.  Section 30.2 says so outright:

    "Some printers are capable of reporting paperJam independently of
     paperOut; some are not. Some printers keep the ERR5 determination after
     a power-cycle; some do not..."

Left in code, each of those differences is a release -- and, worse, a release
that touches the wire-facing layer is a release that triggers Application
Compliance Testing all over again (section 17.1.2).  Onboarding a new printer
model should not cost a certification cycle.

So a profile is a JSON document that describes one family of peripherals:
which devices it matches, how to read its status, what to send it when a
session opens, what its stocks are called, and how long it takes.  Adding
support for a new peripheral is adding a file.

Profiles live entirely in ``cuppsd`` and are consumed above the interface.
Nothing here can change a byte on the wire that ``cupps`` did not already
send, which is what keeps the interface signature -- and the certification --
untouched when the catalogue grows.

**Verified and unverified.** A profile carries ``verified``, meaning someone
has run the device test against real hardware and confirmed it. An unverified
profile is a starting point written from a datasheet, and the tooling reports
it as such.  "We support any peripheral" is only honest if you can say which
ones you have actually put paper through.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Optional

log = logging.getLogger("cuppsd.profiles")

#: Where the shipped catalogue lives. A site adds its own directory alongside.
PROFILE_DIR = Path(__file__).resolve().parent / "profiles"

#: Profile documents must declare the schema they were written against, so a
#: future incompatible change can be detected rather than silently misread.
SCHEMA_VERSION = 1


class ProfileError(ValueError):
    """A profile document is malformed."""


@dataclass(frozen=True)
class StatusBehaviour:
    """How to read this device's status flags (section 30.2).

    Defaults describe a device that behaves exactly as the specification
    describes, so an absent profile and a fully conforming device agree.
    """

    #: False when the device cannot tell a jam from an empty paper path, so a
    #: paperJam report must not be presented to an agent as a distinct fault.
    reports_paper_jam_independently: bool = True
    #: True when an error survives a power cycle and must be cleared by hand
    #: rather than waiting for the device to recover on its own.
    retains_error_across_power_cycle: bool = False
    #: The specification requires paper status to be reported independently of
    #: online status. A device that drops ready when paper runs out is not
    #: conforming, but it exists, and an application still has to cope.
    paper_out_clears_ready: bool = False
    #: Seconds of unknown status to tolerate before treating a device as down.
    unknown_grace_seconds: float = 5.0


@dataclass(frozen=True)
class AeaBehaviour:
    """AEA Mode specifics for BP, BT, BG, SD and SN devices.

    ``EP`` is not listed here: section 30.1 makes it mandatory for every AEA
    session and :mod:`cupps.session` always sends it.  These are the commands
    a particular device needs *in addition*, after EP.
    """

    #: Extra commands sent after the mandatory EP, in order.
    opening_commands: tuple[str, ...] = ()
    #: Named command templates with ``{placeholder}`` fields, so a host
    #: integration can name an operation rather than hard-code a byte string.
    templates: dict[str, str] = field(default_factory=dict)
    #: Minimum AEA revision the device needs. Section 30 sets 2009 generally,
    #: 2012 for BD and for BG e-gate self-boarding, and recommends ITPS 2019
    #: for RFID bag tag encoding.
    minimum_revision: str = "2009"

    def render(self, template: str, **values: Any) -> str:
        """Fill a named template.

        Raises :class:`KeyError` naming the template, rather than the missing
        placeholder, because that is the useful message at a support desk.
        """
        try:
            body = self.templates[template]
        except KeyError:
            raise KeyError(
                f"no AEA template {template!r} in this profile; it has "
                f"{sorted(self.templates) or 'none'}"
            ) from None
        try:
            return body.format(**values)
        except KeyError as exc:
            raise KeyError(
                f"AEA template {template!r} needs a value for {exc}"
            ) from None


@dataclass(frozen=True)
class StockBehaviour:
    """Stock naming and geometry (sections 30.15.3, 26.14).

    Stock names are site configuration, not a standard: one platform calls a
    boarding pass ``BP`` and another ``ATB2``.  Aliases map the name the
    application uses onto the name the platform gave.
    """

    #: application stock name -> platform stock name
    aliases: dict[str, str] = field(default_factory=dict)
    #: stock name -> (width_mm, height_mm), overriding the device descriptor
    #: only where the platform reports nothing usable.
    dimensions: dict[str, tuple[float, float]] = field(default_factory=dict)

    def resolve(self, stock_name: str) -> str:
        return self.aliases.get(stock_name, stock_name)


@dataclass(frozen=True)
class Timing:
    """Per-device timing, within the ceilings section 26.11 sets."""

    #: How long to allow a print to complete. Bounded by PRMaxResponseTime.
    print_timeout_seconds: float = 120.0
    #: Settling time after acquiring, for devices that need a moment.
    warmup_seconds: float = 0.0


@dataclass(frozen=True)
class Match:
    """Which devices a profile applies to."""

    device_type: str
    vendor: Optional[re.Pattern] = None
    model: Optional[re.Pattern] = None
    misc_info: Optional[re.Pattern] = None

    def specificity(self) -> int:
        """How narrow this match is; the narrowest wins."""
        return sum(1 for p in (self.vendor, self.model, self.misc_info) if p)

    def matches(self, device) -> bool:
        if device.device_type.upper() != self.device_type.upper():
            return False
        for pattern, value in (
            (self.vendor, device.vendor),
            (self.model, device.model),
            (self.misc_info, device.misc_info),
        ):
            if pattern is not None and not pattern.search(value or ""):
                return False
        return True


@dataclass(frozen=True)
class DeviceProfile:
    """One peripheral family."""

    profile_id: str
    description: str
    match: Match
    status: StatusBehaviour = field(default_factory=StatusBehaviour)
    aea: AeaBehaviour = field(default_factory=AeaBehaviour)
    stocks: StockBehaviour = field(default_factory=StockBehaviour)
    timing: Timing = field(default_factory=Timing)
    #: True only when the device test has been run against real hardware.
    verified: bool = False
    #: Free text for whoever is holding a radio at three in the morning.
    notes: str = ""
    source: Optional[Path] = None

    @property
    def device_type(self) -> str:
        return self.match.device_type.upper()

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.profile_id,
            "description": self.description,
            "deviceType": self.device_type,
            "verified": self.verified,
            "notes": self.notes,
            "source": str(self.source) if self.source else None,
        }

    @classmethod
    def from_dict(
        cls, payload: dict[str, Any], *, source: Optional[Path] = None
    ) -> "DeviceProfile":
        where = f" in {source}" if source else ""

        version = payload.get("schemaVersion", SCHEMA_VERSION)
        if version != SCHEMA_VERSION:
            raise ProfileError(
                f"profile schema version {version} is not supported"
                f" (this build reads version {SCHEMA_VERSION}){where}"
            )

        for required in ("id", "match"):
            if required not in payload:
                raise ProfileError(f"profile is missing {required!r}{where}")

        raw_match = payload["match"]
        if "deviceType" not in raw_match:
            raise ProfileError(f"profile match is missing deviceType{where}")

        def compile_pattern(name: str) -> Optional[re.Pattern]:
            value = raw_match.get(name)
            if not value:
                return None
            try:
                return re.compile(value)
            except re.error as exc:
                raise ProfileError(
                    f"match.{name} is not a valid regular expression: "
                    f"{exc}{where}"
                ) from exc

        match = Match(
            device_type=str(raw_match["deviceType"]).upper(),
            vendor=compile_pattern("vendor"),
            model=compile_pattern("model"),
            misc_info=compile_pattern("miscInfo"),
        )

        status_payload = payload.get("status", {})
        status = StatusBehaviour(
            reports_paper_jam_independently=bool(
                status_payload.get("reportsPaperJamIndependently", True)
            ),
            retains_error_across_power_cycle=bool(
                status_payload.get("retainsErrorAcrossPowerCycle", False)
            ),
            paper_out_clears_ready=bool(
                status_payload.get("paperOutClearsReady", False)
            ),
            unknown_grace_seconds=float(
                status_payload.get("unknownGraceSeconds", 5.0)
            ),
        )

        aea_payload = payload.get("aea", {})
        opening = tuple(str(c) for c in aea_payload.get("openingCommands", []))
        if any(command.strip().upper().startswith("EP") for command in opening):
            # Section 30.1 already guarantees EP as the first AEA command;
            # repeating it would reset parameters the profile just set.
            raise ProfileError(
                f"aea.openingCommands must not include EP: it is sent "
                f"automatically as the first command of every AEA session "
                f"(section 30.1){where}"
            )
        aea = AeaBehaviour(
            opening_commands=opening,
            templates={
                str(k): str(v) for k, v in aea_payload.get("templates", {}).items()
            },
            minimum_revision=str(aea_payload.get("minimumRevision", "2009")),
        )

        stock_payload = payload.get("stocks", {})
        dimensions: dict[str, tuple[float, float]] = {}
        for name, size in stock_payload.get("dimensions", {}).items():
            if not isinstance(size, (list, tuple)) or len(size) != 2:
                raise ProfileError(
                    f"stocks.dimensions[{name!r}] must be [width_mm, "
                    f"height_mm]{where}"
                )
            dimensions[str(name)] = (float(size[0]), float(size[1]))
        stocks = StockBehaviour(
            aliases={
                str(k): str(v) for k, v in stock_payload.get("aliases", {}).items()
            },
            dimensions=dimensions,
        )

        timing_payload = payload.get("timing", {})
        timing = Timing(
            print_timeout_seconds=float(
                timing_payload.get("printTimeoutSeconds", 120.0)
            ),
            warmup_seconds=float(timing_payload.get("warmupSeconds", 0.0)),
        )

        return cls(
            profile_id=str(payload["id"]),
            description=str(payload.get("description", "")),
            match=match,
            status=status,
            aea=aea,
            stocks=stocks,
            timing=timing,
            verified=bool(payload.get("verified", False)),
            notes=str(payload.get("notes", "")),
            source=source,
        )


#: The profile used when nothing matches: a device that behaves exactly as the
#: specification describes.
def default_profile(device_type: str) -> DeviceProfile:
    return DeviceProfile(
        profile_id=f"default-{device_type.lower()}",
        description=(
            f"Specification default for a {device_type.upper()} device; no "
            f"profile matched"
        ),
        match=Match(device_type=device_type.upper()),
        verified=False,
        notes=(
            "No profile matched this device, so specification behaviour is "
            "assumed. If the device has quirks, add a profile rather than "
            "special-casing it in code."
        ),
    )


class ProfileRegistry:
    """The loaded catalogue, and the matching rules over it."""

    def __init__(self, profiles: Optional[list[DeviceProfile]] = None) -> None:
        self._profiles: list[DeviceProfile] = list(profiles or [])

    def __len__(self) -> int:
        return len(self._profiles)

    def __iter__(self) -> Iterator[DeviceProfile]:
        return iter(sorted(self._profiles, key=lambda p: (p.device_type, p.profile_id)))

    @classmethod
    def load(
        cls, *directories: Path, include_shipped: bool = True
    ) -> "ProfileRegistry":
        """Load profiles from the shipped catalogue and any extra directories.

        A site drops its own directory on the end; a later profile with a
        narrower match wins, so local overrides need no edit to shipped files.
        """
        search: list[Path] = []
        if include_shipped:
            search.append(PROFILE_DIR)
        search.extend(Path(directory) for directory in directories)

        profiles: list[DeviceProfile] = []
        seen: dict[str, Path] = {}
        for directory in search:
            if not directory.is_dir():
                log.debug("profile directory %s does not exist", directory)
                continue
            for path in sorted(directory.glob("*.json")):
                try:
                    payload = json.loads(path.read_text(encoding="utf-8"))
                except json.JSONDecodeError as exc:
                    raise ProfileError(f"{path} is not valid JSON: {exc}") from exc
                profile = DeviceProfile.from_dict(payload, source=path)
                if profile.profile_id in seen:
                    raise ProfileError(
                        f"duplicate profile id {profile.profile_id!r} in "
                        f"{path} and {seen[profile.profile_id]}"
                    )
                seen[profile.profile_id] = path
                profiles.append(profile)
        log.info("loaded %d device profile(s)", len(profiles))
        return cls(profiles)

    def resolve(self, device) -> DeviceProfile:
        """The best profile for ``device``, or the specification default.

        The most specific match wins; ties are broken by profile id so the
        result never depends on filesystem ordering.
        """
        candidates = [p for p in self._profiles if p.match.matches(device)]
        if not candidates:
            return default_profile(device.device_type or "??")
        return sorted(
            candidates,
            key=lambda p: (-p.match.specificity(), p.profile_id),
        )[0]

    def for_type(self, device_type: str) -> list[DeviceProfile]:
        return [p for p in self if p.device_type == device_type.upper()]

    @property
    def unverified(self) -> list[DeviceProfile]:
        """Profiles nobody has confirmed against real hardware."""
        return [p for p in self if not p.verified]
