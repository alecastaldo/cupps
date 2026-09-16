#!/usr/bin/env python3
"""CUPPSIT -- the informational tool required of every CUPPS application.

Section 11.2 requires an application package to ship a tool named ``CUPPSIT``
in the application relative-root folder, supporting:

    -h              help information                      (11.2.3)
    -v              version of the executable and its dependencies (11.2.2)
    -i              supported interface levels            (11.2.4)
    -d              devices used by the application       (11.2.5)
    -t <device>     test the named device                 (11.2.5)

The output formats follow Listings 11.1 through 11.5.  ``-d`` and ``-t`` need
a live platform, which the tool reaches through ``CUPPSPN``/``CUPPSPP`` the
same way the application does; ``--simulator`` runs them against the bundled
simulator so a package can be smoke-tested before it is installed.
"""

from __future__ import annotations

import argparse
import sys
from typing import Optional

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))

from cupps import (  # noqa: E402
    SUPPORTED_INTERFACE_LEVELS,
    CUPPS_TS_VERSION,
    CuppsEnvironment,
    CuppsError,
)
from cupps import __version__ as library_version  # noqa: E402
from cupps.crypto import backend_name  # noqa: E402
from cupps.env import EnvironmentError_  # noqa: E402
from cuppsd import APPLICATION_NAME, __version__ as app_version  # noqa: E402

APPLICATION_EXECUTABLE = "cuppsd"

DESCRIPTION = (
    "Common-use check-in and boarding application. The following command "
    "line\nparameters are supported."
)


def _banner() -> str:
    """The two-line header every CUPPSIT response opens with (Listing 11.1)."""
    return f"Application: {APPLICATION_EXECUTABLE}\nVersion....: {app_version}\n"


def show_help() -> int:
    """``-h`` (section 11.2.3, Listing 11.2)."""
    print()
    print(_banner())
    print(DESCRIPTION)
    print()
    print("  -h               display this help information")
    print("  -v               display version information for the package")
    print("  -i               display the supported CUPPS interface levels")
    print("  -d               display the devices used by the application")
    print("  -t [devicename]  test the named device")
    print()
    print("  --simulator      run -d and -t against the bundled platform")
    print("                   simulator instead of the installed platform")
    print()
    return 0


def show_version() -> int:
    """``-v`` (section 11.2.2, Listing 11.1)."""
    print()
    print(_banner())
    for name, version in _dependencies():
        print(f"Dependency.: {name}")
        print(f"Version....: {version}")
        print()
    return 0


def _dependencies() -> list[tuple[str, str]]:
    """Dependencies to report; section 11.2.2 leaves the choice to the app."""
    import platform

    found = [
        ("cupps (CUPPS 01.04 interface library)", library_version),
        ("python", platform.python_version()),
    ]
    crypto = backend_name()
    found.append(
        ("cryptography backend (MS track decryption)", crypto or "NOT INSTALLED")
    )
    return found


def show_interfaces() -> int:
    """``-i`` (section 11.2.4, Listing 11.3)."""
    print()
    print(_banner())
    print(f"IATA-CUPPS versions: {', '.join(SUPPORTED_INTERFACE_LEVELS)}")
    print()
    print(f"Specification......: {CUPPS_TS_VERSION}")
    print()
    return 0


def _connect(use_simulator: bool):
    """Open a platform session, returning ``(service, simulator)``."""
    from cuppsd.service import CuppsService, ServiceConfig

    simulator = None
    overrides: dict[str, str] = {}
    if use_simulator:
        from simulator import PlatformSimulator

        simulator = PlatformSimulator().start()
        overrides = simulator.environment_overrides

    environment = CuppsEnvironment.from_os(overrides)
    config = ServiceConfig.from_environment(
        environment,
        application_name=APPLICATION_NAME,
        application_version=app_version,
    )
    if not config.airline:
        config.airline = "ZZ"

    service = CuppsService(config)
    service.start()

    # Wait for the handler to reach steady state before reporting devices.
    import time

    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        if service.state.value == "aStd":
            break
        if service.last_error:
            break
        time.sleep(0.1)
    return service, simulator


def show_devices(use_simulator: bool) -> int:
    """``-d`` (section 11.2.5, Listing 11.4)."""
    print()
    print(_banner())
    service, simulator = _connect(use_simulator)
    try:
        if not service.devices:
            print(f"No devices available. {service.last_error}".rstrip())
            return 1
        for handle in service.devices.values():
            print(f"{handle.device_type:<4}{handle.name}")
        print()
        return 0
    finally:
        service.stop()
        if simulator:
            simulator.stop()


def test_device(device_name: str, use_simulator: bool) -> int:
    """``-t [devicename]`` (section 11.2.5, Listing 11.5).

    The printed ``Result`` is the ``result`` attribute of the platform's API
    response, which is what the spec's example shows.
    """
    print()
    print(_banner())
    service, simulator = _connect(use_simulator)
    try:
        try:
            handle = service.device(device_name)
        except KeyError:
            print(f"Testing...: {device_name}")
            print("Result....: invalidDevice")
            print()
            return 1

        print(f"Testing...: {handle.device_type} {handle.name}")
        try:
            outcome = service.test_device(device_name)
            result = outcome["result"]
        except CuppsError as exc:
            result = getattr(exc, "result", "error")
            print(f"Result....: {result}")
            print(f"Detail....: {exc}")
            print()
            return 1

        print(f"Result....: {result}")
        if outcome.get("detail"):
            print(f"Detail....: {outcome['detail']}")
        print()
        return 0 if str(result).startswith("OK") else 1
    finally:
        service.stop()
        if simulator:
            simulator.stop()


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("-h", action="store_true", dest="help")
    parser.add_argument("-v", action="store_true", dest="version")
    parser.add_argument("-i", action="store_true", dest="interfaces")
    parser.add_argument("-d", action="store_true", dest="devices")
    parser.add_argument("-t", dest="test", metavar="devicename")
    parser.add_argument("--simulator", action="store_true")
    args, unknown = parser.parse_known_args(argv)

    if unknown:
        print(f"cuppsit: unrecognised argument {unknown[0]!r}", file=sys.stderr)
        return show_help() or 2

    try:
        if args.version:
            return show_version()
        if args.interfaces:
            return show_interfaces()
        if args.devices:
            return show_devices(args.simulator)
        if args.test:
            return test_device(args.test, args.simulator)
        return show_help()
    except EnvironmentError_ as exc:
        print(f"\ncuppsit: {exc}\n", file=sys.stderr)
        print("Run with --simulator to test without a platform.", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
