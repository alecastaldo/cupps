"""The platform and device conversations of sections 26.6, 29 and 30."""

from __future__ import annotations

import pytest

from cupps import (
    DeviceSession,
    InterfaceMode,
    LockMethod,
    LogDevice,
    LogSeverity,
    PlatformSession,
    PrintDocument,
    Printer,
    Reader,
    RequestFailed,
    TokenInvalidated,
)
from cupps.errors import CuppsError


def test_authentication_yields_a_token_and_the_runtime_picture(platform):
    runtime = platform.environment
    assert len(runtime.device_token) == 16
    assert runtime.workstation.name == "TSTCUPPSCKI001"
    assert runtime.platform.vendor == "SIM"
    assert platform.interface_level == "01.04"
    # Listing 29.6 returns all three storage areas.
    assert set(runtime.storage) == {"persistentLocal", "persistentGlobal", "transient"}


def test_device_list_includes_sub_devices_of_a_macro_device(platform):
    """Listing 29.6 nests a BC and an MS inside the BG device element."""
    runtime = platform.environment
    boarding_gate = runtime.first_of_type("BG")
    assert boarding_gate is not None
    assert boarding_gate.is_macro
    assert {d.device_type for d in boarding_gate.sub_devices} == {"BC", "MS"}
    # ...and all_devices() walks into them.
    assert len(runtime.of_type("BC")) == 2


def test_device_types_come_from_the_parameter_type(platform):
    for device in platform.environment.all_devices():
        assert device.device_type in device.parameter_type.upper()


def test_device_query_filters_by_type_and_name(platform):
    printers = platform.query_devices(device_type="PR")
    assert [d.device_type for d in printers] == ["PR"]
    named = platform.query_devices(device_name=printers[0].name)
    assert len(named) == 1 and named[0].name == printers[0].name


def test_bye_invalidates_the_device_token(simulator):
    """Section 26.7: the token dies with the platform connection."""
    session = PlatformSession(
        simulator.host, simulator.platform_port,
        airline="ZZ", event_token="TESTTOKEN0000001",
    )
    runtime = session.open()
    device = runtime.first_of_type("BC")
    session.close()

    with pytest.raises(TokenInvalidated):
        _ = session.device_token

    # A device session opened with the dead token must be refused.
    with pytest.raises(TokenInvalidated):
        DeviceSession(
            device, device_token=runtime.device_token, airline_id="ZZ",
            mode=InterfaceMode.STANDARD,
        ).open()


def test_acquire_rejects_an_unknown_device(platform):
    from cupps.model import Device

    bogus = Device(name="NOSUCHDEVICE", index="1",
                   parameter_type="bcDeviceParameter")
    bogus.host_name = platform.environment.first_of_type("BC").host_name
    bogus.port = platform.environment.first_of_type("BC").port
    with pytest.raises(RequestFailed, match="invalidDevice"):
        DeviceSession(bogus, device_token=platform.device_token,
                      airline_id="ZZ", mode=InterfaceMode.STANDARD).open()


def test_interface_mode_is_refused_when_unsupported(platform):
    """Table 30.1: a BC supports Standard Mode only."""
    barcode = platform.environment.first_of_type("BC")
    with pytest.raises(RequestFailed, match="modeNotSupportedForThisDevice"):
        DeviceSession(barcode, device_token=platform.device_token,
                      airline_id="ZZ", mode=InterfaceMode.AEA).open()


def test_aea_session_sends_ep_first(platform, simulator):
    """Section 30.1 CRITICAL: an AEA session opens with the EP command."""
    printer = platform.environment.first_of_type("BP")
    simulator.aea_commands.clear()
    with DeviceSession(printer, device_token=platform.device_token,
                       airline_id="ZZ", mode=InterfaceMode.AEA):
        assert simulator.aea_commands[0] == "EP"


def test_aea_is_refused_on_a_standard_mode_session(platform):
    """Section 30.15: an aeaRequest in Standard Mode is an illogical message."""
    barcode = platform.environment.first_of_type("BC")
    with DeviceSession(barcode, device_token=platform.device_token,
                       airline_id="ZZ", mode=InterfaceMode.STANDARD) as session:
        with pytest.raises(CuppsError, match="illogical"):
            session.aea("LT0A1000")


def test_lock_renewal_and_method_switching(platform):
    """Section 30.7: renewing is allowed, switching methods is not."""
    printer = platform.environment.first_of_type("PR")
    with DeviceSession(printer, device_token=platform.device_token,
                       airline_id="ZZ", mode=InterfaceMode.STANDARD) as session:
        assert session.lock(LockMethod.BY_CONNECTION) == "OK"
        assert session.lock(LockMethod.BY_CONNECTION) == "OK-deviceAlreadyLocked"
        with pytest.raises(CuppsError, match="switching"):
            session.lock(LockMethod.BY_DEVICE_TOKEN)
        session.unlock()
        assert not session.locked


def test_a_second_session_cannot_take_a_held_lock(platform):
    printer = platform.environment.first_of_type("PR")
    from cupps import DeviceLocked

    with DeviceSession(printer, device_token=platform.device_token,
                       airline_id="ZZ", mode=InterfaceMode.STANDARD) as first:
        first.lock()
        with DeviceSession(printer, device_token=platform.device_token,
                           airline_id="ZZ", mode=InterfaceMode.STANDARD) as second:
            with pytest.raises(DeviceLocked) as caught:
                second.lock()
            # Section 30.7 requires <deviceLockerInfo> on a refusal.
            assert caught.value.locker


def test_special_mode_devices_refuse_locking(platform):
    """Section 30.20 note: locking a ZL is an illogical message."""
    log_device = platform.environment.first_of_type("ZL")
    with DeviceSession(log_device, device_token=platform.device_token,
                       airline_id="ZZ", mode=InterfaceMode.SPECIAL) as session:
        with pytest.raises(CuppsError, match="Special Mode"):
            session.lock()
        # The session must survive the refusal, because it never reached the wire.
        assert session.connected
        assert session.request_status().ready


def test_reader_data_available_notification_and_read(platform, simulator):
    """Section 30.9: wait for dataAvailable rather than polling."""
    barcode = platform.environment.first_of_type("BC")
    with DeviceSession(barcode, device_token=platform.device_token,
                       airline_id="ZZ", mode=InterfaceMode.STANDARD) as session:
        reader = Reader(session, device_token=platform.device_token)
        simulator.scan_barcode(barcode.name, "BA1234567890", type_code="6")
        assert reader.wait_for_data(timeout=5.0)
        reads = reader.read_barcodes()
        assert [r.text for r in reads] == ["BA1234567890"]
        assert reads[0].symbology == "2D PDF417"
        # Section 26.13.1 step 8: the read clears the platform's pending data.
        assert reader.read_barcodes() == []


def test_magnetic_track_data_is_decrypted_with_the_session_token(platform, simulator):
    magnetic = platform.environment.first_of_type("MS")
    with DeviceSession(magnetic, device_token=platform.device_token,
                       airline_id="ZZ", mode=InterfaceMode.STANDARD) as session:
        reader = Reader(session, device_token=platform.device_token)
        simulator.swipe_card(magnetic.name, {1: "JOHN SMITH", 2: "4000123400001234"})
        assert reader.wait_for_data(timeout=5.0)
        tracks = {t.track_id: t.text for t in reader.read_tracks()}
        assert tracks == {1: "JOHN SMITH", 2: "4000123400001234"}


def test_status_notifications_update_the_session(platform, simulator):
    """Section 30.2: platforms notify, applications do not poll."""
    printer = platform.environment.first_of_type("PR")
    with DeviceSession(printer, device_token=platform.device_token,
                       airline_id="ZZ", mode=InterfaceMode.STANDARD) as session:
        assert session.status.ready
        simulator.set_status(printer.name, paper_out=True)
        for _ in range(50):
            if session.status.paper_out:
                break
            import time; time.sleep(0.05)
        assert session.status.paper_out
        # Section 30.2 CRITICAL: paper status is independent of online status.
        assert session.status.ready


def test_print_reports_per_document_results(platform, simulator):
    """Listing 30.42: a partial failure is oneOrMoreIssues plus per-document results."""
    printer_device = platform.environment.first_of_type("PR")
    with DeviceSession(printer_device, device_token=platform.device_token,
                       airline_id="ZZ", mode=InterfaceMode.STANDARD) as session:
        session.lock()
        printer = Printer(session)
        outcomes = printer.print_documents([
            PrintDocument(1, "BP", text="one"),
            PrintDocument(2, "A4", text="two"),
        ])
        assert [(o.document_id, o.result) for o in outcomes] == [(1, "OK"), (2, "OK")]

        simulator.set_status(printer_device.name, paper_out=True)
        outcomes = printer.print_documents([PrintDocument(3, "BP", text="three")])
        assert outcomes[0].result == "paperOut" and not outcomes[0].ok


def test_log_conversation(platform):
    """Section 30.20: open, write, retrieve with the same security token."""
    log_device = platform.environment.first_of_type("ZL")
    with DeviceSession(log_device, device_token=platform.device_token,
                       airline_id="ZZ", mode=InterfaceMode.SPECIAL) as session:
        zl = LogDevice(session)
        zl.open("1", "A5H2J3JXF2GHN9G5")
        zl.write_one("1", "Host login failed.", LogSeverity.CRITICAL)
        zl.write_one("1", "Gate reader ready")
        entries = zl.retrieve("1", "A5H2J3JXF2GHN9G5")
        assert entries == [
            "[critical] Host login failed.",
            "[normal] Gate reader ready",
        ]
        # The wrong token must not return another application's entries.
        assert zl.retrieve("1", "WRONGTOKEN000000") == []
        zl.close_all()


def test_applications_may_not_open_a_platform_scope_log(platform):
    """Section 30.20.1 CRITICAL."""
    from cupps import LogScope

    log_device = platform.environment.first_of_type("ZL")
    with DeviceSession(log_device, device_token=platform.device_token,
                       airline_id="ZZ", mode=InterfaceMode.SPECIAL) as session:
        with pytest.raises(CuppsError, match="platform-scope"):
            LogDevice(session).open("1", "TOKEN", LogScope.PLATFORM)


def test_print_cancel(platform, simulator):
    """Section 30.15.5: a queued document may be cancelled."""
    from cupps import Printer

    printer_device = platform.environment.first_of_type("PR")
    with DeviceSession(printer_device, device_token=platform.device_token,
                       airline_id="ZZ", mode=InterfaceMode.STANDARD) as session:
        session.lock()
        assert Printer(session).cancel(7) == "OK"
        assert simulator.cancelled_documents[-1] == "7"


def test_crypt_algorithm_selection(platform, simulator):
    """Section 30.3: the application may choose the algorithm for a session."""
    from cupps import CryptAlgorithm

    magnetic = platform.environment.first_of_type("MS")
    # The platform advertises its default and what else it offers.
    assert platform.environment.platform.default_crypt_algorithm == "aes-strong"
    assert set(platform.environment.platform.available_crypt_algorithms) == {
        "aes-strong", "des-weak",
    }

    with DeviceSession(magnetic, device_token=platform.device_token,
                       airline_id="ZZ", mode=InterfaceMode.STANDARD) as session:
        reader = Reader(session, device_token=platform.device_token)
        assert reader.crypt_algorithm == CryptAlgorithm.AES_STRONG.value
        reader.set_crypt_algorithm(CryptAlgorithm.DES_WEAK.value)
        assert reader.crypt_algorithm == CryptAlgorithm.DES_WEAK.value

        # ...and a swipe now decrypts under the newly chosen algorithm.
        simulator.crypt_algorithm = CryptAlgorithm.DES_WEAK.value
        try:
            simulator.swipe_card(magnetic.name, {1: "DES TRACK DATA"})
            assert reader.wait_for_data(timeout=5.0)
            assert reader.read_tracks()[0].text == "DES TRACK DATA"
        finally:
            simulator.crypt_algorithm = CryptAlgorithm.AES_STRONG.value


def test_device_query_proximity_criterion_is_sent(platform):
    """Table 29.1 defines DeviceType, Proximity and DeviceName."""
    devices = platform.query_devices(device_type="PR", proximity="local")
    assert all(d.device_type == "PR" for d in devices)


def test_empty_barcode_payload_does_not_raise(platform, simulator):
    """An empty <bcData> must read as no data, not a Base64 error."""
    from cupps import xmlmsg

    barcode = platform.environment.first_of_type("BC")
    device = simulator.device(barcode.name)
    element = xmlmsg.Element("readerData")
    element.add(xmlmsg.Element("bcData", {"bcTypeCode": "u", "readStatus": "OK"},
                               text=""))
    with device._lock:
        device.pending_reads.append(element)

    with DeviceSession(barcode, device_token=platform.device_token,
                       airline_id="ZZ", mode=InterfaceMode.STANDARD) as session:
        reads = Reader(session, device_token=platform.device_token).read_barcodes()
        assert [r.data for r in reads] == [b""]
