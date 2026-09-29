"""Run the CUPPS platform.

    python -m cuppsplatform --demo --agent
        Pseudo-terminal peripherals, the console, and the reference airline
        application connected over CUPPS. Everything a demonstration needs.

    python -m cuppsplatform --bindings ./lab
        Real peripherals from a binding directory (see docs/LAB.md).

The platform listens on CUPPSPN/CUPPSPP -- 127.0.0.1:7535 by default -- so any
CUPPS application, in any language, can connect to it.
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
from pathlib import Path

from .console import Console
from .drivers import BindingRegistry
from .server import CuppsPlatform, bench_bindings

log = logging.getLogger("cuppsplatform")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="cuppsplatform",
                                     description="CUPPS 01.04 platform")
    parser.add_argument("--demo", action="store_true",
                        help="bench peripherals (pseudo-terminals, or socket pairs on Windows); no hardware needed")
    parser.add_argument("--bench", choices=["pty", "socketpair"],
                        help="bench transport for --demo (default: pty, or socketpair on Windows)")
    parser.add_argument("--bindings", action="append", default=[], type=Path,
                        help="directory of device bindings (repeatable)")
    parser.add_argument("--agent", action="store_true",
                        help="also run the reference airline application")
    parser.add_argument("--airline", default="BA")
    parser.add_argument("--airline-name", default="BRITISH AIRWAYS")
    parser.add_argument("--workstation", default="CUPPSPLT001")
    parser.add_argument("--platform-port", type=int, default=7535)
    parser.add_argument("--device-port", type=int, default=7536)
    parser.add_argument("--console-port", type=int, default=8640)
    parser.add_argument("--agent-port", type=int, default=8631)
    parser.add_argument("--output", type=Path, default=Path("demo-output"),
                        help="where the file print backend writes documents")
    parser.add_argument("--log-level", default="WARNING",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = parser.parse_args(argv)

    logging.basicConfig(level=getattr(logging, args.log_level),
                        format="%(asctime)s %(levelname)-7s %(name)s  %(message)s")

    if args.demo:
        prints = (args.output / "prints").resolve()
        prints.mkdir(parents=True, exist_ok=True)
        bindings = bench_bindings(prints, args.bench) if args.bench else bench_bindings(prints)
    elif args.bindings:
        bindings = BindingRegistry.load(*args.bindings)
    else:
        print("cuppsplatform: pass --demo, or --bindings <dir> for real hardware.",
              file=sys.stderr)
        return 2

    platform = CuppsPlatform(
        bindings=bindings,
        workstation=args.workstation,
        platform_port=args.platform_port,
        device_port=args.device_port,
    ).start()
    console = Console(platform, port=args.console_port).start()

    agent = api = None
    if args.agent:
        from cuppsd.httpapi import ApiServer
        from cuppsd.service import CuppsService, ServiceConfig

        config = ServiceConfig(
            airline=args.airline,
            airline_name=args.airline_name,
            application_name="CUPPSAGENT",
            application_version="01.00.0001",
            event_token="AGENTTOKEN000001",
            log_security_token="AGENTTOKEN000001",
            platform_node=platform.host,
            platform_port=platform.platform_port,
        )
        agent = CuppsService(config)
        agent.start()
        api = ApiServer(agent, port=args.agent_port).start()

    print()
    print("  CUPPS platform running")
    print(f"    CUPPSPN / CUPPSPP ... {platform.host} / {platform.platform_port}")
    print(f"    platform console .... {console.url}")
    if api is not None:
        print(f"    airline agent app ... {api.url}")
    if args.demo:
        print(f"    printed documents ... {(args.output / 'prints').resolve()}")
    for binding in bindings:
        driver = platform.drivers.get(binding.device_name.upper())
        where = driver.transport.describe() if driver else "-"
        print(f"    {binding.device_name:<18} {binding.device_type}  {where}")
    print("\n  Ctrl-C to stop.\n")

    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, lambda *_: stop.set())
        except (ValueError, OSError):
            pass
    try:
        while not stop.wait(0.5):
            pass
    finally:
        if api is not None:
            api.stop()
        if agent is not None:
            agent.stop(timeout=10)
        console.stop()
        platform.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
