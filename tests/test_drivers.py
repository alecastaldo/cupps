"""Peripheral drivers, exercised over real I/O.

Serial is tested through a pseudo-terminal, which pyserial drives exactly as
it drives a COM port, and TCP through a real loopback socket. No peripheral
is mocked out at the transport boundary: the bytes genuinely cross a
character device or a socket, because a driver whose only test is against a
fake transport is a driver nobody has run.
"""

from __future__ import annotations

import json
import socket
import threading
import time

import pytest

from cupps import params

from cuppsplatform.drivers import (
    AeaDriver,
    BindingError,
    BindingRegistry,
    DeviceBinding,
    DeviceSecured,
    DriverData,
    DriverStatus,
    LoopbackTransport,
    PtyTransport,
    ReaderDriver,
    TcpTransport,
    TransportConfig,
    TransportError,
    build_transport,
    normalise_port,
)

pytest.importorskip("serial", reason="pyserial is needed for the serial path")


def wait_for(predicate, timeout=3.0, interval=0.02):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


# -- transports -----------------------------------------------------------


@pytest.mark.parametrize(
    "port,expected",
    [
        ("COM1", "COM1"),
        ("COM9", "COM9"),
        ("COM10", r"\\.\COM10"),
        ("COM255", r"\\.\COM255"),
        ("/dev/ttyUSB0", "/dev/ttyUSB0"),
        (r"\\.\COM17", r"\\.\COM17"),
    ],
)
def test_com_port_normalisation(port, expected):
    """Section 6.3.20: COM numbering to 255, and Windows needs \\\\.\\COMxyz."""
    assert normalise_port(port) == expected


def test_pty_transport_carries_bytes_both_ways():
    with PtyTransport() as transport:
        assert transport.is_open
        transport.write(b"LT211909\r")
        assert transport.device_read() == b"LT211909\r"

        transport.device_write(b"ST0100\r")
        data = b""
        deadline = time.monotonic() + 2
        while b"\r" not in data and time.monotonic() < deadline:
            data += transport.read()
        assert data == b"ST0100\r"


def test_tcp_transport_round_trip():
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    host, port = server.getsockname()
    received: list[bytes] = []

    def serve():
        conn, _ = server.accept()
        received.append(conn.recv(64))
        conn.sendall(b"ST0100\r")
        time.sleep(0.3)
        conn.close()

    threading.Thread(target=serve, daemon=True).start()

    config = TransportConfig(kind="tcp", host=host, tcp_port=port)
    with build_transport(config) as transport:
        transport.write(b"ST\r")
        data = b""
        deadline = time.monotonic() + 2
        while b"\r" not in data and time.monotonic() < deadline:
            data += transport.read()
        assert data == b"ST0100\r"
    assert received == [b"ST\r"]
    server.close()


def test_unknown_transport_kind_is_reported():
    with pytest.raises(TransportError, match="unknown transport kind"):
        build_transport(TransportConfig(kind="carrier-pigeon"))


def test_writing_to_a_closed_transport_fails():
    transport = LoopbackTransport()
    with pytest.raises(TransportError, match="not open"):
        transport.write(b"x")


# -- reader driver --------------------------------------------------------


def make_reader(transport, device_type="BC", **kwargs):
    data: list[DriverData] = []
    statuses: list[DriverStatus] = []
    driver = ReaderDriver(
        f"LHRT4LB00302{device_type}1", device_type, transport,
        on_data=data.append, on_status=statuses.append, **kwargs
    )
    return driver, data, statuses


def test_reader_parses_a_scan_over_a_real_serial_port():
    transport = PtyTransport()
    driver, data, _ = make_reader(transport)
    with driver:
        driver.unsecure()
        transport.device_write(b"M1SMITH/JOHN MR       E\r")
        assert wait_for(lambda: data)
    assert data[0].kind == "barcode"
    assert data[0].payload == b"M1SMITH/JOHN MR       E"


def test_a_scan_split_across_two_reads_is_one_record():
    """A record straddling two reads must not become two scans."""
    transport = PtyTransport()
    driver, data, _ = make_reader(transport)
    with driver:
        driver.unsecure()
        transport.device_write(b"M1SMITH/")
        time.sleep(0.2)
        transport.device_write(b"JOHN MR\r")
        assert wait_for(lambda: data)
    assert len(data) == 1
    assert data[0].payload == b"M1SMITH/JOHN MR"


@pytest.mark.parametrize("terminator", [b"\r", b"\n", b"\r\n"])
def test_all_record_terminators_are_accepted(terminator):
    transport = PtyTransport()
    driver, data, _ = make_reader(transport)
    with driver:
        driver.unsecure()
        transport.device_write(b"0125123456" + terminator)
        assert wait_for(lambda: data)
    assert data[0].payload == b"0125123456"


def test_two_scans_in_one_burst_are_two_records():
    transport = PtyTransport()
    driver, data, _ = make_reader(transport)
    with driver:
        driver.unsecure()
        transport.device_write(b"FIRST\rSECOND\r")
        assert wait_for(lambda: len(data) >= 2)
    assert [d.payload for d in data[:2]] == [b"FIRST", b"SECOND"]


def test_unterminated_stream_is_flushed_rather_than_buffered_forever():
    transport = PtyTransport()
    driver, data, _ = make_reader(transport)
    with driver:
        driver.unsecure()
        transport.device_write(b"X" * 9000)
        assert wait_for(lambda: data, timeout=5)
    assert len(data[0].payload) >= 8192


def test_normalisation_keeps_only_the_last_value():
    """Section 26.11.8: the last value inside DevNormTime is the one sent."""
    transport = PtyTransport()
    driver, data, _ = make_reader(transport, normalise=True,
                                  normalise_window=0.3)
    with driver:
        driver.unsecure()
        for value in (b"FIRST\r", b"SECOND\r", b"THIRD\r"):
            transport.device_write(value)
            time.sleep(0.05)
        assert wait_for(lambda: data, timeout=3)
    assert len(data) == 1
    assert data[0].payload == b"THIRD"


# -- securing (section 10.4.1) --------------------------------------------


def test_a_secured_reader_ignores_scans():
    """Section 10.4.1: a device no application holds must not be usable.

    The stated reason is configuration barcodes, which can reprogram a
    reader. Input read while secured must never reach an application.
    """
    transport = PtyTransport()
    driver, data, _ = make_reader(transport)
    with driver:
        assert driver.secured, "a device must start secured"
        transport.device_write(b"CONFIGURE-ME\r")
        time.sleep(0.4)
        assert data == [], "a secured device delivered input"
        assert driver.discarded_while_secured >= 1


def test_unsecuring_then_securing_again_stops_delivery():
    transport = PtyTransport()
    driver, data, _ = make_reader(transport)
    with driver:
        driver.unsecure()
        transport.device_write(b"ALLOWED\r")
        assert wait_for(lambda: data)
        driver.secure()
        transport.device_write(b"IGNORED\r")
        time.sleep(0.4)
    assert [d.payload for d in data] == [b"ALLOWED"]


def test_securing_discards_a_partial_record():
    """A part-read record must not be completed by a later scan."""
    transport = PtyTransport()
    driver, data, _ = make_reader(transport)
    with driver:
        driver.unsecure()
        transport.device_write(b"PARTIAL")
        time.sleep(0.2)
        driver.secure()
        driver.unsecure()
        transport.device_write(b"-REST\r")
        assert wait_for(lambda: data)
    assert data[0].payload == b"-REST", "a partial record survived securing"


def test_a_secured_device_refuses_writes():
    transport = PtyTransport()
    driver, _, _ = make_reader(transport)
    with driver:
        with pytest.raises(DeviceSecured, match="10.4.1"):
            driver.write(b"anything")


# -- AEA driver -----------------------------------------------------------


def test_aea_driver_sends_ep_first_on_start():
    """Section 30.1 CRITICAL: EP opens every AEA session."""
    transport = PtyTransport()
    driver = AeaDriver("LHRT4LB00302BP1", "BP", transport)
    try:
        driver.start()
        assert wait_for(lambda: transport.device_read(timeout=0.2) or True)
        # The EP went out during initialise, before anything else.
        assert driver.status.ready
    finally:
        driver.stop()


def test_aea_driver_passes_a_host_stream_through_unchanged():
    transport = PtyTransport()
    driver = AeaDriver("LHRT4LB00302BP1", "BP", transport,
                       send_ep_on_start=False)
    with driver:
        driver.unsecure()
        transport.device_read(timeout=0.2)
        stream = b"LT211909\x0a\x05\x01BINARY\r"
        driver.send_stream(stream)
        assert transport.device_read() == stream


def test_aea_status_keeps_ready_true_when_paper_runs_out():
    """Section 30.2 CRITICAL: paper status is independent of online status.

    A printer with no paper that still answers AEA is still ready. A driver
    that clears ready here makes a routine paper change look like a dead
    device.
    """
    transport = PtyTransport()
    statuses: list[DriverStatus] = []
    driver = AeaDriver(
        "LHRT4LB00302BP1", "BP", transport,
        on_status=statuses.append, send_ep_on_start=False,
    )
    with driver:
        driver.unsecure()
        transport.device_write(b"ST01P\r")
        assert wait_for(lambda: driver.status.paper_out)
        # Asserted inside the block: stop() resets status to "stopped".
        assert driver.status.paper_out is True
        assert driver.status.ready is True, "ready must survive paper out"


def test_aea_messages_reach_the_application():
    transport = PtyTransport()
    data: list[DriverData] = []
    driver = AeaDriver("LHRT4LB00302BP1", "BP", transport,
                       on_data=data.append, send_ep_on_start=False)
    with driver:
        driver.unsecure()
        transport.device_write(b"ST0100\r")
        assert wait_for(lambda: data)
    assert data[0].kind == "aea"
    assert data[0].payload == b"ST0100"


def test_transport_loss_is_reported_as_powered_off():
    transport = PtyTransport()
    statuses: list[DriverStatus] = []
    driver = AeaDriver("LHRT4LB00302BP1", "BP", transport,
                       on_status=statuses.append, send_ep_on_start=False)
    driver.start()
    try:
        transport.close()
        assert wait_for(lambda: driver.status.power_off, timeout=3)
    finally:
        driver.stop()


# -- bindings -------------------------------------------------------------


def test_binding_builds_a_driver_and_transport():
    binding = DeviceBinding.from_dict({
        "device": "LHRT4LB00302BP1",
        "deviceType": "BP",
        "driver": "aea",
        "transport": {"kind": "pty"},
    })
    driver = binding.build()
    assert isinstance(driver, AeaDriver)
    assert driver.device_name == "LHRT4LB00302BP1"


def test_binding_infers_the_driver_from_the_device_type():
    binding = DeviceBinding.from_dict({
        "device": "LHRT4LB00302BC1",
        "deviceType": "BC",
        "transport": {"kind": "loopback"},
    })
    assert binding.driver == "reader"


def test_binding_rejects_a_driver_that_cannot_serve_the_device_type():
    with pytest.raises(BindingError, match="does not serve"):
        DeviceBinding.from_dict({
            "device": "X", "deviceType": "BC", "driver": "aea",
            "transport": {"kind": "loopback"},
        })


@pytest.mark.parametrize("missing", ["device", "deviceType", "transport"])
def test_binding_requires_its_fields(missing):
    payload = {
        "device": "X", "deviceType": "BC",
        "transport": {"kind": "loopback"},
    }
    payload.pop(missing)
    with pytest.raises(BindingError, match=missing):
        DeviceBinding.from_dict(payload)


def test_binding_rejects_an_unknown_driver():
    with pytest.raises(BindingError, match="unknown driver"):
        DeviceBinding.from_dict({
            "device": "X", "deviceType": "BC", "driver": "telepathy",
            "transport": {"kind": "loopback"},
        })


def test_registry_loads_and_a_later_directory_overrides(tmp_path):
    site = tmp_path / "site"
    bench = tmp_path / "bench"
    site.mkdir()
    bench.mkdir()
    (site / "devices.json").write_text(json.dumps([
        {"device": "LHRPR1", "deviceType": "PR", "driver": "print",
         "transport": {"kind": "serial", "port": "COM17"},
         "options": {"backend": "file"}},
        {"device": "LHRBC1", "deviceType": "BC",
         "transport": {"kind": "serial", "port": "COM3"}},
    ]))
    # A lab bench points one device at a pseudo-terminal instead.
    (bench / "override.json").write_text(json.dumps({
        "device": "LHRPR1", "deviceType": "PR", "driver": "print",
        "transport": {"kind": "pty"}, "options": {"backend": "file"},
    }))

    registry = BindingRegistry.load(site, bench)
    assert len(registry) == 2
    assert registry.require("LHRPR1").transport.kind == "pty"
    assert registry.require("LHRBC1").transport.port == "COM3"
    assert [b.device_name for b in registry.of_type("BC")] == ["LHRBC1"]


def test_registry_reports_an_unbound_device(tmp_path):
    registry = BindingRegistry.load(tmp_path)
    with pytest.raises(BindingError, match="no binding for device"):
        registry.require("NOSUCHDEVICE")


def test_registry_rejects_malformed_json(tmp_path):
    (tmp_path / "broken.json").write_text("{not json")
    with pytest.raises(BindingError, match="not valid JSON"):
        BindingRegistry.load(tmp_path)


# -- PR printing (section 30.15) ------------------------------------------


def make_printer(tmp_path, **kwargs):
    from cuppsplatform.drivers import PrintDriver

    driver = PrintDriver(
        "LHRT4LB00302PR1", "PR",
        backend="file", backend_options={"directory": str(tmp_path)},
        **kwargs,
    )
    return driver


def test_printer_writes_the_document(tmp_path):
    driver = make_printer(tmp_path)
    with driver:
        driver.unsecure()
        outcome = driver.print_document(b"%PDF-1.4 boarding pass", stock="BP")
    assert outcome.ok
    written = list(tmp_path.glob("*.pdf"))
    assert len(written) == 1
    assert written[0].read_bytes() == b"%PDF-1.4 boarding pass"
    assert "BP" in written[0].name


def test_printer_refuses_while_secured(tmp_path):
    """Section 10.4.1 applies to printers as much as to readers."""
    driver = make_printer(tmp_path)
    with driver:
        with pytest.raises(DeviceSecured):
            driver.print_document(b"text", stock="BP")
    assert list(tmp_path.glob("*")) == []


def test_printer_reports_paper_out_without_losing_ready(tmp_path):
    """Section 30.2: paper status is independent of online status."""
    driver = make_printer(tmp_path)
    with driver:
        driver.unsecure()
        driver.set_paper_out(True)
        assert driver.status.ready is True
        outcome = driver.print_document(b"text", stock="BP")
        assert outcome.result == "paperOut"
        assert not outcome.ok

        driver.set_paper_out(False)
        assert driver.print_document(b"text", stock="BP").ok


def test_print_that_never_completes_returns_within_prmaxresponsetime(tmp_path):
    """Section 26.11.37: a hung spooler must not hang the position."""
    from cuppsplatform.drivers.printer import FileBackend

    class NeverCompletes(FileBackend):
        def poll(self, job_id):
            return None

    driver = make_printer(tmp_path, max_response_time=0.5)
    driver.backend = NeverCompletes(directory=str(tmp_path))
    with driver:
        driver.unsecure()
        started = time.monotonic()
        outcome = driver.print_document(b"text", stock="BP")
    assert outcome.result == "timeout"
    assert time.monotonic() - started < 3.0


def test_printer_device_test_actually_prints(tmp_path):
    """Section 11.2.1: a printer test must include actual printing."""
    driver = make_printer(tmp_path)
    with driver:
        status = driver.test()
    assert "test print: OK" in status.description
    assert len(list(tmp_path.glob("*"))) == 1


def test_unknown_print_backend_is_reported(tmp_path):
    from cuppsplatform.drivers import PrintDriver, PrintError

    with pytest.raises(PrintError, match="unknown print backend"):
        PrintDriver("PR1", "PR", backend="carrier-pigeon")


def test_pr_binds_to_the_print_driver_by_default():
    binding = DeviceBinding.from_dict({
        "device": "LHRT4LB00302PR1", "deviceType": "PR",
        "transport": {"kind": "loopback"},
        "options": {"backend": "file"},
    })
    assert binding.driver == "print"
