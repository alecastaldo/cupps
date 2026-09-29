"""End-to-end tests of the platform engine served over the wire.

An application library session talks to :class:`CuppsPlatform`, which
drives pseudo-terminal peripherals through the real drivers.  Every read
the application sees started as bytes written into a character device.
"""

import json
import os
import time
import urllib.error
import urllib.request

import pytest

from cupps import (  # noqa: E402
    DeviceLocked,
    DeviceSession,
    InterfaceMode,
    PlatformSession,
    PrintDocument,
    Printer,
    Reader,
)
from cuppsplatform.console import Console, SAMPLE_BCBP  # noqa: E402
from cuppsplatform.server import CuppsPlatform, bench_bindings  # noqa: E402


def _wait(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


# "socketpair" is the Windows bench; it runs everywhere, so CI covers it.
@pytest.fixture(params=["pty", "socketpair"])
def platform(request, tmp_path):
    if request.param == "pty" and os.name != "posix":
        pytest.skip("pseudo-terminals are POSIX only")
    plat = CuppsPlatform(bindings=bench_bindings(tmp_path, request.param)).start()
    yield plat
    plat.stop()


def _session(plat):
    return PlatformSession(
        plat.host, plat.platform_port, airline="BA",
        event_token="TESTTOKEN0000001", applications=[("BAGATE", "01.00")],
    )


def test_start_reaches_started_with_secured_devices(platform):
    snap = platform.snapshot()
    assert snap["platform"]["state"] == "pStd"
    assert snap["platform"]["workstationState"] == "wStd"
    bench = [d for d in snap["devices"] if d["bench"]]
    assert {d["name"] for d in bench} == {
        "CUPPSPLT001BC1", "CUPPSPLT001MS1", "CUPPSPLT001BP1", "CUPPSPLT001PR1",
    }
    assert all(d["state"] == "dStd" and d["secured"] for d in bench)


def test_input_on_an_unheld_reader_is_discarded(platform):
    # §10.4.1: nobody holds the reader, so a configuration barcode goes nowhere.
    platform.scan_barcode("CUPPSPLT001BC1", "CONFIG-BARCODE")
    driver = platform.drivers["CUPPSPLT001BC1"]
    assert _wait(lambda: driver.discarded_while_secured == 1)


def test_application_lifecycle_and_real_byte_reads(platform, tmp_path):
    with _session(platform) as ps:
        apps = platform.snapshot()["applications"]
        assert [a["state"] for a in apps] == ["aStd"]
        env = ps.environment

        bc = env.by_name("CUPPSPLT001BC1")
        with DeviceSession(bc, device_token=ps.device_token, airline_id="BA",
                           mode=InterfaceMode.STANDARD) as s:
            reader = Reader(s, device_token=ps.device_token)
            platform.scan_barcode("CUPPSPLT001BC1", SAMPLE_BCBP)
            assert reader.wait_for_data(3)
            assert reader.read_barcodes()[0].text == SAMPLE_BCBP

            s.lock()
            with DeviceSession(bc, device_token=ps.device_token, airline_id="BA",
                               mode=InterfaceMode.STANDARD) as other:
                with pytest.raises(DeviceLocked):
                    other.lock()
            s.unlock()

        ms = env.by_name("CUPPSPLT001MS1")
        with DeviceSession(ms, device_token=ps.device_token, airline_id="BA",
                           mode=InterfaceMode.STANDARD) as s:
            reader = Reader(s, device_token=ps.device_token)
            platform.swipe_card("CUPPSPLT001MS1", {2: "4111111111111111=2912"})
            assert reader.wait_for_data(3)
            tracks = {str(t.track_id): t.text for t in reader.read_tracks()}
            assert tracks["2"] == "4111111111111111=2912"

        pr = env.by_name("CUPPSPLT001PR1")
        with DeviceSession(pr, device_token=ps.device_token, airline_id="BA",
                           mode=InterfaceMode.STANDARD) as s:
            s.lock()
            results = Printer(s).print_documents(
                [PrintDocument(1, "BP", text="BOARDING PASS\r\nSMITH/JOHN")]
            )
            s.unlock()
        assert [r.result for r in results] == ["OK"]
        assert any(p.is_file() for p in tmp_path.rglob("*"))

    assert _wait(lambda: platform.snapshot()["applications"] == [])
    # Released devices are secured again.
    bench = [d for d in platform.snapshot()["devices"] if d["bench"]]
    assert _wait(lambda: all(
        d["secured"] for d in platform.snapshot()["devices"] if d["bench"]
    )), bench


def test_aea_paper_out_reaches_the_application(platform):
    with _session(platform) as ps:
        bp = ps.environment.by_name("CUPPSPLT001BP1")
        pty = platform.drivers["CUPPSPLT001BP1"].transport
        pty.device_read(timeout=0.2)
        with DeviceSession(bp, device_token=ps.device_token, airline_id="BA",
                           mode=InterfaceMode.AEA) as s:
            assert b"EP" in pty.device_read(timeout=1.0)
            platform.set_status("CUPPSPLT001BP1", paper_out=True)
            assert _wait(lambda: s.status.paper_out)
            # §30.2: paperOut is independent of ready.
            assert s.status.ready


def test_platform_identifies_itself_as_the_engine(platform):
    with _session(platform) as ps:
        assert ps.environment.platform.vendor == "CUPPSPLATFORM"


def test_stop_returns_to_stopped(tmp_path):
    plat = CuppsPlatform(bindings=bench_bindings(tmp_path, "socketpair")).start()
    plat.stop()
    assert plat.machine.state.value == "pStp"


# -- console -----------------------------------------------------------------


@pytest.fixture
def console(platform):
    con = Console(platform, port=0).start()
    yield con
    con.stop()


def _post(con, path, payload):
    req = urllib.request.Request(
        con.url + path.lstrip("/"), data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=3) as resp:
            return resp.status, json.load(resp)
    except urllib.error.HTTPError as exc:
        return exc.code, json.load(exc)


def test_console_serves_page_and_state(console):
    with urllib.request.urlopen(console.url, timeout=3) as resp:
        assert b"CUPPS Platform Console" in resp.read()
    with urllib.request.urlopen(console.url + "api/state", timeout=3) as resp:
        assert json.load(resp)["platform"]["state"] == "pStd"


def test_console_bench_scan_writes_to_the_device(console, platform):
    status, body = _post(console, "/api/bench/scan", {"device": "CUPPSPLT001BC1"})
    assert status == 200 and body["ok"]
    driver = platform.drivers["CUPPSPLT001BC1"]
    assert _wait(lambda: driver.discarded_while_secured == 1)


def test_console_refuses_unknown_and_software_devices(console):
    assert _post(console, "/api/bench/scan", {"device": "NOPE"})[0] == 404
    # Software devices have no character device to write into.
    assert _post(console, "/api/bench/scan", {"device": "CUPPSPLT001ZL1"})[0] == 409


def test_split_tracks_numbers_by_sentinel():
    from cuppsplatform.server import split_tracks

    assert split_tracks(";4111=2912?") == {2: "4111=2912"}
    assert split_tracks("%B41^X^29?;41=29?;015?") == {1: "B41^X^29", 2: "41=29", 3: "015"}
    assert split_tracks(";41=29?+015?") == {2: "41=29", 3: "015"}
    assert split_tracks("RAW") == {1: "RAW"}
