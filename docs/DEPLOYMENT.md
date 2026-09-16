# Deploying to an airport

This document is the honest version: what works today, what you must supply,
and what the specification requires of you as an application supplier before a
position goes live with passengers in front of it.

---

## 1. What this software is, and is not

**It is** a complete and tested implementation of the application side of the
CUPPS 01.04 interface, with a working agent application on top of it. The
protocol layer is exercised by 103 automated tests, including assertions
against the specification's own published examples.

**It is not** an airline system. Specifically, it has:

- **no host or DCS connection** — no reservations, no seat map, no passenger
  list, no through-check, no weight and balance, no API/APIS, no payment;
- **no fare, ancillary or excess baggage pricing**;
- **no baggage reconciliation (BRS) or BSM messaging** — it prints a tag with
  the licence plate you give it, and does not generate or transmit the BSM;
- **no user or role management of its own** — it trusts the CUPPS platform's
  end-user session (section 8).

An airline deploys this as the *device-facing half* of a check-in or boarding
application and wires the other half to its own systems.

---

## 2. Before anything else: certification

Part III of the specification (chapters 12–18) governs certification and
deployment. In summary:

- Applications are certified **per platform**. A build certified on one
  supplier's platform is not automatically acceptable on another's.
- The platform operator schedules the certification; you supply the
  application package, its documentation and its CUPPSIT tool.
- Chapter 25 defines the test cases and framework you will be run against.
- Chapter 21 governs change management: a certified application cannot be
  quietly updated in place.

Nothing in this repository shortens that process. Budget for it.

---

## 3. What you must supply

### 3.1 A host connection — mandatory

`cuppsd` exposes a local JSON API on the loopback adapter. Wire your DCS to
it. The endpoints you will drive:

| Endpoint | Purpose |
|---|---|
| `GET /api/state` | Current position: devices, status, platform, workstation |
| `GET /api/events` | Server-Sent Events: scans, card reads, status changes, faults |
| `POST /api/print/boardingpass` | Render and print a pass on a PR device |
| `POST /api/print/bagtag` | Render and print a Resolution 740 bag tag |
| `POST /api/print/receipt` | Itinerary or excess-baggage receipt |
| `POST /api/aea` | Pass a host-generated AEA stream to a BP, BT or BG device |
| `POST /api/device/<name>/{lock,unlock,status,test}` | Device control |
| `GET /api/log` | Read back this application's ZL log entries |

The event stream is the important one: a barcode scan arrives as a
`boardingPassScanned` event with the Resolution 792 record already decoded.

### 3.2 A 2D barcode encoder — only if you print passes through a PR device

Resolution 792 requires PDF417 (paper) or Aztec/QR (mobile). Neither is built
in: both need large error-correction tables that are better taken from a
maintained library than reimplemented here.

```python
from cuppsd import barcode

def pdf417(payload: str) -> barcode.Symbol:
    ...  # your encoder, returning alternating (is_bar, width) modules

barcode.register_renderer("pdf417", pdf417)
```

Until you do, `POST /api/print/boardingpass` still succeeds but returns
`"barcodeRendered": false` with an explanatory note, and the printed pass
carries a Code 39 sequence reference clearly marked *reference only*. The
agent UI raises this to the operator rather than letting an unscannable pass
reach a passenger. **Decide deliberately whether that fallback is acceptable
at your stations, or gate printing on it.**

If you print boarding passes through a **BP** device in AEA Mode — which is
what CUPPS 01.04 expects, since Table 3.3 note 5 makes BP and BT AEA-only —
you need no encoder at all. The printer's firmware draws the symbol.

### 3.3 AEA command streams

CUPPS carries AEA traffic but does not define it; the AEA specification is
referenced, not reproduced. This library therefore **does not compose AEA
print commands**, because it cannot verify syntax it has not been given.

Take the AEA/ATB stream your host produces and pass it to `POST /api/aea` or
`DeviceSession.aea()`. Splitting it into `<aeaText>` and `<aeaBinary>` per
Table 30.4 is handled for you.

Note the AEA version floors: **AEA 2009** generally, **AEA 2012** minimum for
BD baggage drop and BG e-gate self-boarding, and **ITPS 2019** recommended for
RFID bag tag encoding.

---

## 4. Points to confirm against your actual platform

These are correct against the specification and against the simulator. Confirm
each during platform integration testing, because platforms differ.

| Area | What to confirm | Why |
|---|---|---|
| **MS encryption** | That track data decrypts correctly | The construction is AES-128-ECB / PKCS#7 keyed by the device token, derived from the four vectors in Listing 30.30 and asserted in `tests/test_crypto.py`. The prose defers to the CUPPS SDK. If your platform differs, `tests/test_crypto.py` fails loudly rather than returning garbage. |
| **XSD validation** | Whether your platform is strict | Section 27.1.4 requires *platforms* to validate every message and says applications *should*. This client validates structurally, not against the XSD the platform publishes at `CUPPSXSDD`. Adding full validation is the single highest-value hardening step — see §6. |
| **Interface level** | Which levels your platform offers | The client negotiates the best of 01.04 / 01.03 / 01.01. Check `GET /api/state` → `interfaceLevel` after connecting. |
| **Device names and stocks** | That the position's devices match your expectation | `python3 cuppsit.py -d` lists them. Stock names such as `BP`, `BT`, `A4` are site configuration, not standard. |
| **Locking policy** | `byConnection` vs `byDeviceToken` | The handler takes a `byConnection` lock for the duration of a print and releases it. If you run multiple threads against one device, switch to `byDeviceToken` — and note all connections must agree (section 26.13.1). |

---

## 5. Installing on a CUPPS workstation

### 5.1 Environment

Section 4.3 requires 64-bit Windows 10 Professional or later (or Server 2016 /
2019), English as the default language, and a touch screen at a minimum of
1024×768. The agent UI is built for that: 48-pixel minimum touch targets, no
horizontal scrolling at 1024 wide, and a light/dark theme that follows the OS.

### 5.2 Packaging

Sections 11.6 and 17 govern application packaging. Requirements that bear on
this code:

- **`CUPPSIT` must sit in the application relative-root folder** and be
  runnable as the command `CUPPSIT`. On Windows, ship a one-line
  `CUPPSIT.cmd`:
  ```bat
  @echo off
  "%~dp0python\python.exe" "%~dp0cuppsit.py" %*
  ```
- **Registry use is restricted** (section 11.5). This application writes
  nothing to the registry.
- **CPU affinity** (section 11.1): the application must behave on a multi-core
  CPU. CPython does; if you embed a runtime that does not, use
  `SetProcessAffinityMask` or `CMD /AFFINITY`.
- **Storage**: write only to the areas the platform gave you —
  `CUPPSPLSD` (persistent local), `CUPPSPGSD` (persistent global, backed up)
  and `CUPPSTSD` (transient, erased when you exit). Quotas are 1 GB each
  (Table 6.5).

Ship an embedded Python runtime with the package rather than depending on a
system interpreter; the platform operator will not install one for you.

### 5.3 Launching

The platform launches your application from its menu and must see it start
within `AppStgTime` (30 s). `cuppsd` connects and acquires devices in well
under a second against a responsive platform.

Have the platform menu entry launch `cuppsd` and then open the workstation
browser at `http://127.0.0.1:8631/` in kiosk mode.

### 5.4 Network posture

The handler binds **127.0.0.1 only** by default. Keep it that way: an agent
position should expose no new listening surface to the airport network.
`--host` exists for development and should not be used at a station.

---

## 6. Hardening worth doing

In rough order of value:

1. **Full XSD validation** of inbound and outbound messages against the
   schemas the platform publishes at `CUPPSXSDD`. `lxml` can do this; wire it
   into `cupps.xmlmsg.parse`. This turns a class of silent
   misinterpretation into a loud failure.
2. **Persist the event stream** to `CUPPSPLSD` so a position that is restarted
   mid-shift keeps its audit trail independently of the platform's ZL log.
3. **Cap the reconnect loop** with backoff and raise an alert after N failures,
   rather than retrying every 5 seconds indefinitely.
4. **Review what reaches the ZL log.** Track content is deliberately excluded
   today (section 6.3.12 makes what you log your responsibility under PCI
   DSS). Keep it that way as you add host integration.

---

## 7. Acceptance checklist

Run through this at the station before handing over.

```bash
# 1. The package identifies itself (section 11.2)
python3 cuppsit.py -v
python3 cuppsit.py -i

# 2. The platform is reachable and the position's devices are as expected
python3 cuppsit.py -d

# 3. Every device passes its test, printers actually print (section 11.2.5)
python3 cuppsit.py -t <EACH-DEVICE-NAME>

# 4. The protocol suite is green on this machine
python3 -m pytest

# 5. The application starts and reaches steady state
python3 -m cuppsd &
curl -s http://127.0.0.1:8631/api/health     # {"ok": true, "state": "aStd"}
```

Then, on the position itself, with the operator watching:

- [ ] Every device shows **Ready** in the status strip.
- [ ] Pulling paper from the printer turns its tile amber **within a second**,
      without polling — this proves the notification path works.
- [ ] Reloading paper clears it.
- [ ] Scanning a live boarding pass shows the right passenger, flight and seat.
- [ ] Scanning a pass for a passenger who is not checked in shows the red
      do-not-board verdict.
- [ ] A printed bag tag scans on the baggage system's own reader.
- [ ] A printed boarding pass scans at the gate reader.
- [ ] Closing the application returns every device; the platform shows no
      orphaned locks.
- [ ] Pulling the network cable and restoring it recovers the position
      without operator action.
