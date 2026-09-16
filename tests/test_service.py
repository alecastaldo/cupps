"""End-to-end tests of the device handler and its local API."""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request

import pytest

from cupps import CuppsEnvironment
from cuppsd.httpapi import ApiServer
from cuppsd.service import AppState, CuppsService, ServiceConfig

#: Built to exact Resolution 792 field widths.
BOARDING_PASS = (
    "M1" + "SMITH/JOHN MR".ljust(20) + "E" + "XY7K2Q ".ljust(7)
    + "LHRJFKBA " + "00117" + "326" + "Y" + "032A" + "00025" + "1" + "00"
)


def wait_until(predicate, timeout=20.0, interval=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


@pytest.fixture
def service(simulator):
    environment = CuppsEnvironment.from_os(simulator.environment_overrides)
    config = ServiceConfig.from_environment(
        environment, application_name="TESTAPP", airline_name="TEST AIR"
    )
    handler = CuppsService(config)
    handler.start()
    assert wait_until(lambda: handler.state is AppState.STD), "never reached aStd"
    try:
        yield handler
    finally:
        handler.stop(timeout=5.0)


@pytest.fixture
def api(service, simulator):
    server = ApiServer(service, port=0, simulator=simulator).start()
    try:
        yield server
    finally:
        server.stop()


def call(server, path, body=None):
    url = server.url.rstrip("/") + path
    if body is None:
        request = urllib.request.Request(url)
    else:
        request = urllib.request.Request(
            url, data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
        )
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.loads(response.read().decode())


def test_handler_acquires_the_expected_devices(service):
    types = {handle.device_type for handle in service.devices.values()}
    assert {"BC", "MS", "OC", "PR", "BP", "BT", "BG", "ZL"} <= types


def test_devices_are_opened_in_the_mode_table_30_1_requires(service):
    expected = {"BC": "standard", "MS": "standard", "OC": "standard",
                "PR": "standard", "BP": "aea", "BT": "aea", "ZL": "special"}
    for handle in service.devices.values():
        want = expected.get(handle.device_type)
        if want:
            assert handle.mode.value == want, handle.name


def test_scanning_a_boarding_pass_publishes_a_decoded_event(service, simulator):
    channel = service.subscribe()
    barcode_device = service.first_of_type("BC")
    simulator.scan_barcode(barcode_device.name, BOARDING_PASS)

    deadline = time.monotonic() + 15
    event = None
    while time.monotonic() < deadline:
        try:
            candidate = channel.get(timeout=1.0)
        except Exception:
            continue
        if candidate.kind == "boardingPassScanned":
            event = candidate
            break
    service.unsubscribe(channel)

    assert event is not None, "no boardingPassScanned event arrived"
    decoded = event.payload["boardingPass"]
    assert decoded["name"] == "JOHN MR SMITH"
    assert decoded["flight"] == "BA117"
    assert decoded["origin"] == "LHR" and decoded["destination"] == "JFK"
    assert decoded["seat"] == "032A"
    assert decoded["checkedIn"] is True


def test_card_reads_do_not_put_track_content_on_the_event_bus(service, simulator):
    """Section 6.3.12: an application owns what it exposes under PCI DSS."""
    channel = service.subscribe()
    magnetic = service.first_of_type("MS")
    simulator.swipe_card(magnetic.name, {1: "4000123412341234"})

    deadline = time.monotonic() + 15
    event = None
    while time.monotonic() < deadline:
        try:
            candidate = channel.get(timeout=1.0)
        except Exception:
            continue
        if candidate.kind == "cardRead":
            event = candidate
            break
    service.unsubscribe(channel)

    assert event is not None
    serialised = json.dumps(event.to_dict())
    assert "4000123412341234" not in serialised
    assert event.payload["tracks"][0]["length"] == 16


def test_printing_takes_and_releases_the_lock(service):
    printer = service.first_of_type("PR")
    assert not printer.session.locked
    results = service.print_documents(
        printer.name,
        [__import__("cupps").PrintDocument(1, "BP", text="test")],
    )
    assert results[0]["ok"]
    assert not printer.session.locked, "the lock was not released after printing"


def test_activity_is_written_to_the_zl_log(service):
    service.log_activity("conformance test entry")
    entries = service.read_log()
    assert any("conformance test entry" in entry for entry in entries)


def test_api_state_and_health(api):
    state = call(api, "/api/state")
    assert state["state"] == "aStd" and state["connected"] is True
    assert state["airlineName"] == "TEST AIR"
    assert len(state["devices"]) >= 8
    assert call(api, "/api/health")["ok"] is True


def test_api_serves_the_agent_ui(api):
    with urllib.request.urlopen(api.url, timeout=10) as response:
        body = response.read().decode()
    assert "<title>CUPPS Agent</title>" in body
    assert "/api/events" in body


def test_api_refuses_path_traversal(api):
    with pytest.raises(urllib.error.HTTPError) as caught:
        urllib.request.urlopen(api.url + "../../etc/passwd", timeout=10)
    assert caught.value.code == 404


def test_api_prints_a_boarding_pass(api):
    result = call(api, "/api/print/boardingpass", {
        "name": "SMITH/JOHN MR", "pnr": "XY7K2Q", "carrier": "BA",
        "flightNumber": "0117", "origin": "LHR", "destination": "JFK",
        "seat": "32A", "gate": "B32",
    })
    assert result["results"][0]["ok"] is True
    # No 2D encoder is registered by default, so the caller is told.
    assert result["barcodeRendered"] is False


def test_api_prints_a_bag_tag(api):
    result = call(api, "/api/print/bagtag", {
        "name": "SMITH/JOHN MR", "licencePlate": "0125123456",
        "origin": "LHR", "destination": "JFK",
    })
    assert result["results"][0]["ok"] is True
    assert result["licencePlate"] == "0125123456"


def test_api_rejects_a_bag_tag_without_a_licence_plate(api):
    with pytest.raises(urllib.error.HTTPError) as caught:
        call(api, "/api/print/bagtag", {"name": "SMITH/JOHN MR"})
    assert caught.value.code == 400


def test_api_device_test_and_locking(api, service):
    name = service.first_of_type("PR").name
    assert call(api, f"/api/device/{name}/test", {})["result"] == "OK"
    assert call(api, f"/api/device/{name}/lock", {})["result"].startswith("OK")
    assert call(api, f"/api/device/{name}/unlock", {})["result"] == "OK"


def test_api_refuses_to_lock_a_special_mode_device(api, service):
    name = service.first_of_type("ZL").name
    with pytest.raises(urllib.error.HTTPError) as caught:
        call(api, f"/api/device/{name}/lock", {})
    assert caught.value.code == 409
    # The session must still work afterwards.
    assert call(api, f"/api/device/{name}/status", {})["usable"] is True


def test_simulator_controls_are_absent_without_a_simulator(service):
    """A deployed position must have no way to fabricate a device read."""
    server = ApiServer(service, port=0, simulator=None).start()
    try:
        with pytest.raises(urllib.error.HTTPError) as caught:
            call(server, "/api/_sim/scan",
                 {"device": "X", "data": BOARDING_PASS})
        assert caught.value.code == 404
    finally:
        server.stop()


def test_platform_loss_is_detected_and_recovered(service, simulator):
    """Section 26.7: the token dies with the connection, so everything rebuilds."""
    original_token = service.runtime.device_token
    service.platform._connection.close()

    assert wait_until(lambda: service.state is not AppState.STD, timeout=15), \
        "the handler did not notice the platform had gone"
    assert wait_until(lambda: service.state is AppState.STD, timeout=40), \
        "the handler did not reconnect"
    assert service.runtime.device_token != original_token
    assert service.devices, "devices were not re-acquired"


def test_platform_directed_stop_is_answered_and_honoured(service, simulator):
    """Section 29.2: the platform may direct an application to terminate."""
    assert simulator.request_application_stop() >= 1
    assert wait_until(lambda: simulator.stop_responses, timeout=15), \
        "the application never answered the stop command"
    assert simulator.stop_responses[-1] == "OK"
    # Having agreed to stop, it must actually leave steady state.
    assert wait_until(lambda: service.state is not AppState.STD, timeout=20)


def test_stop_is_deferred_while_a_device_is_locked(service, simulator):
    """An application may defer up to MaxSpgDeferTimes to finish a transaction."""
    from cupps import params

    printer = service.first_of_type("PR")
    printer.session.lock()
    try:
        simulator.stop_responses.clear()
        assert simulator.request_application_stop() >= 1
        assert wait_until(lambda: simulator.stop_responses, timeout=15)
        assert simulator.stop_responses[-1] == "defer"
    finally:
        printer.session.unlock()

    # Past the deferral allowance the application must give in.
    for _ in range(params.MAX_SPG_DEFER_TIMES + 1):
        simulator.request_application_stop()
        time.sleep(0.3)
    assert wait_until(lambda: simulator.stop_responses[-1] == "OK", timeout=15)


def test_each_device_resolves_its_own_interface_mode(simulator):
    """One device falling back must not change the mode used for the next.

    A BC advertising only AEA is placed ahead of a BC advertising *both*
    modes. The preferred mode for a BC is Standard (Table 30.1). If the
    resolved mode leaked across the acquisition loop, the second reader would
    be opened in AEA -- which it does support, so nothing would fail; it would
    simply be in the wrong mode. That silence is why this is asserted.
    """
    from cupps import InterfaceMode
    from simulator import SimulatedDevice

    aea_only = SimulatedDevice(
        name="TSTCUPPSCKI001BC8", device_type="BC", index="20",
        modes=(InterfaceMode.AEA,),
    )
    both_modes = SimulatedDevice(
        name="TSTCUPPSCKI001BC9", device_type="BC", index="21",
        modes=(InterfaceMode.AEA, InterfaceMode.STANDARD),
    )
    # Order matters: the AEA-only device is acquired first.
    simulator.devices.insert(0, both_modes)
    simulator.devices.insert(0, aea_only)

    environment = CuppsEnvironment.from_os(simulator.environment_overrides)
    config = ServiceConfig.from_environment(environment, application_name="MODETEST")
    handler = CuppsService(config)
    handler.start()
    try:
        assert wait_until(lambda: handler.state is AppState.STD, timeout=25)
        modes = {h.name: h.mode for h in handler.devices.values()}
        assert modes["TSTCUPPSCKI001BC8"] is InterfaceMode.AEA, \
            "a device offering only AEA must be opened in AEA"
        assert modes["TSTCUPPSCKI001BC9"] is InterfaceMode.STANDARD, \
            "a device offering both modes must get the preferred Standard Mode"
    finally:
        handler.stop(timeout=5.0)
