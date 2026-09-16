# CUPPS 01.04 — interface library, device handler and agent application

An implementation of the **application side** of the Common Use Passenger
Processing Systems Technical Specification, IATA Recommended Practice 1797 /
A4A RP 30.201 / ACI RP 500A07, document issue **01.04.0004 (02JUN2021)**.

Three things live here:

| Component | What it is |
|---|---|
| `cupps/` | The protocol library: framing, streaming protocol, platform and device sessions, device model, MS decryption, Resolution 792 decoding. Use this to build your own application. |
| `cuppsd/` + `webui/` | A working agent application: a device handler that owns every CUPPS session, plus a touch UI for check-in, boarding, baggage and device management. |
| `simulator/` | A CUPPS platform simulator, so all of the above can be exercised without airport hardware. |

Plus `cuppsit.py`, the informational tool that section 11.2 requires every
CUPPS application package to ship.

---

## Try it in 30 seconds

No platform, no hardware, no configuration:

```bash
python3 -m cuppsd --simulator --airline BA --airline-name "BRITISH AIRWAYS" --open
```

That starts the bundled platform simulator, connects the handler to it,
acquires ten simulated devices and serves the agent UI at
<http://127.0.0.1:8631/>.

To drive a scan into it:

```bash
curl -X POST http://127.0.0.1:8631/api/_sim/scan \
  -H 'Content-Type: application/json' \
  -d '{"device":"SIMCUPPSCKI001BC1",
       "data":"M1SMITH/JOHN MR       EXY7K2Q LHRJFKBA 00117326Y032A00025100"}'
```

The passenger appears on the Boarding screen with a board / do-not-board
verdict. Simulator control endpoints exist **only** when `--simulator` is
passed; a position connected to a real platform has no way to fabricate a
device read.

## Run it against a real platform

A CUPPS platform sets `CUPPSPN` and `CUPPSPP` before launching an
application (section 6.3.15), so there is nothing to configure:

```bash
python3 -m cuppsd
```

## Use the library directly

```python
from cupps import CuppsEnvironment, PlatformSession, DeviceSession, InterfaceMode, Reader

env = CuppsEnvironment.from_os()

with PlatformSession(env.platform_node, env.platform_port,
                     airline=env.airline, event_token="YOURTOKEN0000001",
                     applications=[("MYAPP", "01.00")]) as platform:

    runtime = platform.environment            # devices, storage, workstation
    barcode = runtime.first_of_type("BC")

    with DeviceSession(barcode, device_token=platform.device_token,
                       airline_id=env.airline,
                       mode=InterfaceMode.STANDARD) as session:
        session.lock()
        reader = Reader(session, device_token=platform.device_token)
        if reader.wait_for_data(timeout=30):
            for read in reader.read_barcodes():
                print(read.symbology, read.text)
```

The handshake, interface-level negotiation, `EP` on AEA sessions, the closing
`byeRequest` and `deviceReleaseRequest`, and socket keep-alive are all handled
for you.

---

## What is implemented

**Transport and protocol** — header version 01 framing (26.8–26.9) verified
byte-for-byte against Listings 26.1 and 26.3; the streaming protocol with
overlapped messages bounded by `PltStreamOutMsgs`; `PltStreamAccumTime`
inter-byte timing; TCP keep-alive (27.1.3); byte-by-byte header reading that
stops on the first invalid character; `sessionErrorEvent` on every framing and
parse fault, carrying the offending body in Base64; `illogicalMessageErrorEvent`
handling; the full messageID range and wraparound rules (27.1.5).

**Conversations** — the platform prototype of Figures 26.2–26.3 (interface
levels → authenticate → use → bye) and the device prototype (acquire →
interface mode → use → release), with the token lifecycle of section 26.7:
losing the platform connection invalidates the token and rebuilds every device
session.

**Devices** — BC, MS and OC reads including AES/DES track decryption; PR
Standard Mode printing with per-document results; BP, BT and BG in AEA Mode;
ZL logging; device locking by connection and by device token, lock expiry, and
the Special Mode no-locking rule; asynchronous status notification rather than
polling.

**Application requirements** — the CUPPSIT tool (11.2), permanent on-screen
device status (11.2.5), F1 help and Alt-F4 exit (11.3), all 45 platform
parameters of section 26.11 as named constants.

**Formats** — IATA Resolution 792 boarding pass decoding that honours every
declared field length; Code 39; a dependency-free PDF writer with boarding
pass, bag tag and receipt layouts.

## What you must supply before going live

This is a complete, tested CUPPS *client*. It is not a complete airline
operation. See **[docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)** for the full list;
the three that matter most:

1. **A host/DCS connection.** This application has none. It prints the
   passenger data you hand it and decodes what it scans; it does not know your
   flights, seat maps or passenger records. Wire `cuppsd`'s API to your
   departure control system.
2. **A 2D barcode encoder**, if you print boarding passes through a **PR**
   device. Resolution 792 needs PDF417 or Aztec, which are not built in.
   `cuppsd.barcode.register_renderer()` is the hook. The **BP** AEA path needs
   nothing — the printer's firmware draws the symbol.
3. **Certification.** Part III of the specification requires each application
   to be certified against each platform it runs on. That is a process with
   your platform supplier, and no amount of code substitutes for it.

## Testing

```bash
python3 -m pytest          # 103 tests, ~35s, no hardware required
```

The suite asserts against the specification's own published examples wherever
it publishes them — the framing listings, and the four magnetic-stripe vectors
in Listing 30.30 that pin down the cipher construction.

## Requirements

Python 3.9+. The only third-party dependency is a crypto backend for magnetic
stripe decryption — either `pycryptodome` or `cryptography`. Everything else,
including the PDF writer and the web UI, uses the standard library, because
CUPPS workstation images are locked down (section 4.3.3).

## Licence

The specification itself is IATA copyright and is not redistributed here. This
implementation is your own work product to licence as you see fit.
