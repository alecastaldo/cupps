"""Run the CUPPS device handler and agent UI.

    python -m cuppsd                 # against the platform in the environment
    python -m cuppsd --simulator     # against the bundled platform simulator

The handler reads its platform address from ``CUPPSPN``/``CUPPSPP``, which a
CUPPS platform sets before launching an application (section 6.3.15).  The
``--simulator`` switch starts the bundled simulator and points the handler at
it, for bench testing and for verifying a build before an airport cutover.
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
import webbrowser
from typing import Optional

from cupps import CuppsEnvironment
from cupps.env import EnvironmentError_

from . import APPLICATION_NAME, __version__
from .httpapi import ApiServer
from .service import CuppsService, ServiceConfig

log = logging.getLogger("cuppsd")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cuppsd",
        description="CUPPS 01.04 device handler and agent application",
    )
    parser.add_argument("--host", default="127.0.0.1",
                        help="address for the local UI (default: loopback only)")
    parser.add_argument("--port", type=int, default=8631,
                        help="port for the local UI (default: 8631)")
    parser.add_argument("--airline", default="",
                        help="IATA airline code; defaults to CUPPSAL2")
    parser.add_argument("--airline-name", default="",
                        help="airline name shown on screen and on documents")
    parser.add_argument("--application-name", default=APPLICATION_NAME,
                        help="name reported in <authenticateRequest>")
    parser.add_argument("--platform-node", default="",
                        help="override CUPPSPN")
    parser.add_argument("--platform-port", type=int, default=0,
                        help="override CUPPSPP")
    parser.add_argument("--simulator", action="store_true",
                        help="start the bundled platform simulator and use it")
    parser.add_argument("--print-dir", default="",
                        help="with --simulator, write printed documents here")
    parser.add_argument("--open", action="store_true",
                        help="open the agent UI in a browser once started")
    parser.add_argument("--log-level", default="INFO",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    parser.add_argument("--version", action="version",
                        version=f"cuppsd {__version__}")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)-7s %(name)s  %(message)s",
    )

    simulator = None
    overrides: dict[str, str] = {}
    if args.simulator:
        from simulator import PlatformSimulator

        simulator = PlatformSimulator(
            print_sink=args.print_dir or None
        ).start()
        overrides = simulator.environment_overrides
        log.info(
            "simulator started: platform %s:%s",
            simulator.host,
            simulator.platform_port,
        )

    environment = CuppsEnvironment.from_os(overrides)
    try:
        config = ServiceConfig.from_environment(
            environment,
            application_name=args.application_name,
            application_version=__version__,
            airline_name=args.airline_name,
        )
    except EnvironmentError_ as exc:
        print(f"cuppsd: {exc}", file=sys.stderr)
        print(
            "\nRun with --simulator to start against the bundled platform "
            "simulator instead.",
            file=sys.stderr,
        )
        return 2

    if args.airline:
        config.airline = args.airline
    if args.platform_node:
        config.platform_node = args.platform_node
    if args.platform_port:
        config.platform_port = args.platform_port
    if not config.airline:
        print(
            "cuppsd: no airline code. Set CUPPSAL2 or pass --airline.",
            file=sys.stderr,
        )
        return 2

    service = CuppsService(config)
    api = ApiServer(service, host=args.host, port=args.port, simulator=simulator)

    stopping = threading.Event()

    def shutdown(signum, frame):  # noqa: ANN001 - signal handler signature
        if stopping.is_set():
            return
        stopping.set()
        log.info("shutting down")

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, shutdown)
        except (ValueError, OSError):
            # Not the main thread, or a platform without that signal.
            pass

    service.start()
    api.start()
    print(f"\n  CUPPS agent ready:  {api.url}")
    print(f"  Airline {config.airline}  ->  platform "
          f"{config.platform_node}:{config.platform_port}\n")
    if args.open:
        try:
            webbrowser.open(api.url)
        except Exception:  # pragma: no cover - no browser on the image
            log.debug("could not open a browser", exc_info=True)

    try:
        while not stopping.wait(0.5):
            pass
    except KeyboardInterrupt:
        pass
    finally:
        api.stop()
        service.stop()
        if simulator is not None:
            simulator.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
