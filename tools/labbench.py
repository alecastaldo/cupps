#!/usr/bin/env python3
"""Drive real peripherals from a bench, without the rest of the platform.

When a printer arrives and nobody knows whether it works, this is the
shortest path from cardboard box to "yes, it prints":

    python3 tools/labbench.py --list
    python3 tools/labbench.py --probe                      # open everything
    python3 tools/labbench.py --watch LHRT4LB00302BC1      # scan something
    python3 tools/labbench.py --send LHRT4LB00302BP1 "ST"  # talk AEA
    python3 tools/labbench.py --test LHRT4LB00302PR1       # print a test page

Bindings come from ``--bindings``, which may be given more than once; a later
directory overrides an earlier one, so a bench can point one device at a
pseudo-terminal while leaving site configuration alone.

``--pty`` runs against pseudo-terminals instead of hardware, which is how the
whole path can be rehearsed before any peripheral is plugged in.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from cuppsplatform.drivers import (  # noqa: E402
    DEFAULT_BENCH_KIND,
    BindingError,
    BindingRegistry,
    DeviceBinding,
    DriverData,
    DriverStatus,
)
from cuppsplatform.drivers.registry import DEFAULT_DRIVER_BY_TYPE  # noqa: E402


def _status_line(status: DriverStatus) -> str:
    flags = [
        name
        for name, value in (
            ("ready", status.ready), ("unknown", status.unknown),
            ("init", status.init), ("powerOff", status.power_off),
            ("paperOut", status.paper_out), ("paperJam", status.paper_jam),
            ("diskError", status.disk_error),
        )
        if value
    ]
    return f"{', '.join(flags) or 'no flags'}  {status.description}".strip()


def load_registry(paths: list[Path]) -> BindingRegistry:
    if not paths:
        default = REPO_ROOT / "cuppsplatform" / "bindings"
        paths = [default] if default.is_dir() else []
    if not paths:
        print(
            "labbench: no binding directories. Pass --bindings <dir>, or use\n"
            "          --pty to rehearse against bench devices.",
            file=sys.stderr,
        )
        raise SystemExit(2)
    return BindingRegistry.load(*paths)


def pty_registry() -> BindingRegistry:
    """Bench devices, for a rehearsal with no hardware.

    Pseudo-terminals where the OS has them, socket pairs on Windows.
    """
    registry = BindingRegistry()
    for name, device_type, options in (
        ("BENCHBC1", "BC", {}),
        ("BENCHMS1", "MS", {}),
        ("BENCHBP1", "BP", {}),
        ("BENCHPR1", "PR", {"backend": "file"}),
    ):
        registry.add(
            DeviceBinding.from_dict({
                "device": name,
                "deviceType": device_type,
                "driver": DEFAULT_DRIVER_BY_TYPE[device_type],
                "transport": {"kind": DEFAULT_BENCH_KIND},
                "options": options,
            })
        )
    return registry


def command_list(registry: BindingRegistry) -> int:
    print()
    print(f"{'DEVICE':<26}{'TYPE':<6}{'DRIVER':<10}TRANSPORT")
    print("-" * 78)
    for binding in registry:
        transport = binding.transport
        where = transport.port or f"{transport.host}:{transport.tcp_port}"
        print(
            f"{binding.device_name:<26}{binding.device_type:<6}"
            f"{binding.driver:<10}{transport.kind}:{where or '-'}"
        )
    print(f"\n{len(registry)} binding(s).\n")
    return 0


def command_probe(registry: BindingRegistry, timeout: float) -> int:
    """Open every bound device and report what came back."""
    print()
    failures = 0
    for binding in registry:
        print(f"{binding.device_name} ({binding.device_type}) ... ", end="",
              flush=True)
        driver = binding.build()
        try:
            driver.start()
            time.sleep(min(timeout, 1.0))
            status = driver.status
            verdict = "OK" if status.usable else "NOT READY"
            if not status.usable:
                failures += 1
            print(f"{verdict}  [{_status_line(status)}]")
        except Exception as exc:
            failures += 1
            print(f"FAILED  {type(exc).__name__}: {exc}")
        finally:
            try:
                driver.stop()
            except Exception:
                pass
    print(f"\n{len(registry) - failures}/{len(registry)} device(s) usable.\n")
    return 1 if failures else 0


def command_watch(
    registry: BindingRegistry, device_name: str, seconds: float
) -> int:
    """Unsecure a reader and print whatever it reads."""
    binding = registry.require(device_name)
    received: list[DriverData] = []

    def on_data(data: DriverData) -> None:
        received.append(data)
        try:
            text = data.payload.decode("utf-8")
        except UnicodeDecodeError:
            text = repr(data.payload)
        print(f"  [{data.kind}] {text}")

    driver = binding.build(on_data=on_data,
                           on_status=lambda s: print(f"  status: {_status_line(s)}"))
    print(f"\nWatching {binding.describe()}")
    print(f"Present something to the device. Ctrl-C to stop "
          f"(auto-stops after {seconds:.0f}s).\n")
    try:
        driver.start()
        # Section 10.4.1: a device is secured until something holds it. The
        # bench is that something, explicitly and only for this run.
        driver.unsecure()
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            time.sleep(0.1)
    except KeyboardInterrupt:
        pass
    finally:
        driver.stop()
    print(f"\n{len(received)} read(s).\n")
    return 0 if received else 1


def command_send(
    registry: BindingRegistry, device_name: str, payload: str, wait: float
) -> int:
    """Send a command to an AEA device and show what comes back."""
    binding = registry.require(device_name)
    replies: list[bytes] = []
    driver = binding.build(on_data=lambda d: replies.append(d.payload))
    print(f"\nSending to {binding.describe()}")
    try:
        driver.start()
        driver.unsecure()
        stream = payload.encode("latin-1").decode("unicode_escape").encode("latin-1")
        sender = getattr(driver, "send_stream", None) or driver.write
        sender(stream if stream.endswith(b"\r") else stream + b"\r")
        print(f"  sent {len(stream)} byte(s)")
        time.sleep(wait)
        for reply in replies:
            print(f"  <- {reply!r}")
        print(f"  status: {_status_line(driver.status)}")
    finally:
        driver.stop()
    print()
    return 0


def command_test(registry: BindingRegistry, device_name: str) -> int:
    """Run the device's own test (section 11.2.1)."""
    binding = registry.require(device_name)
    driver = binding.build()
    print(f"\nTesting {binding.describe()}")
    try:
        driver.start()
        status = driver.test()
        print(f"  result: {_status_line(status)}")
        usable = status.usable
    finally:
        driver.stop()
    print()
    return 0 if usable else 1


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="labbench",
        description="Drive real CUPPS peripherals from a bench.",
    )
    parser.add_argument("--bindings", action="append", default=[], type=Path,
                        help="a directory of device bindings; repeatable, "
                             "later directories override earlier ones")
    parser.add_argument("--pty", action="store_true",
                        help="use pseudo-terminal devices instead of hardware")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--list", action="store_true")
    group.add_argument("--probe", action="store_true",
                       help="open every bound device and report its status")
    group.add_argument("--watch", metavar="DEVICE",
                       help="show what a reader reads")
    group.add_argument("--test", metavar="DEVICE",
                       help="run the device's own test")
    parser.add_argument("--send", nargs=2, metavar=("DEVICE", "PAYLOAD"),
                        help="send a command to an AEA device")
    parser.add_argument("--seconds", type=float, default=30.0,
                        help="how long --watch listens (default 30)")
    parser.add_argument("--wait", type=float, default=2.0,
                        help="how long --send waits for a reply (default 2)")
    parser.add_argument("--log-level", default="WARNING",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = parser.parse_args(argv)

    import logging
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(levelname)-7s %(name)s  %(message)s",
    )

    try:
        registry = pty_registry() if args.pty else load_registry(args.bindings)
        if args.list:
            return command_list(registry)
        if args.probe:
            return command_probe(registry, args.wait)
        if args.watch:
            return command_watch(registry, args.watch, args.seconds)
        if args.test:
            return command_test(registry, args.test)
        if args.send:
            return command_send(registry, args.send[0], args.send[1], args.wait)
    except BindingError as exc:
        print(f"\nlabbench: {exc}\n", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
