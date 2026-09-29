# Demo run-of-show

A 20-minute demonstration of the CUPPS platform engine. It shows a real
airline application using peripherals through the engine, the security
default, the self-certification tool, and the release gate. You need one
Windows, Linux or macOS machine with Python 3.10+ and a browser. No
hardware is required. Act 7 covers real hardware if you have some.

Commands are written for Windows PowerShell. On Linux or macOS they are
the same, except that you type `python3` where `python` is not found and
set environment variables as shown in Act 2.

## Before the audience arrives (5 minutes)

```powershell
git clone <repo> cupps
cd cupps
python -m venv .venv
.venv\Scripts\Activate.ps1          # Linux/macOS: source .venv/bin/activate
python -m pip install -e ".[test]"
python -m pytest -q                 # all pass, about 90 s
python -m cuppsplatform --demo --agent
```

If PowerShell refuses to run `Activate.ps1`, run
`Set-ExecutionPolicy -Scope Process Bypass` in that window first. When
Windows Defender Firewall asks about Python, **Cancel** is fine: the demo
listens on 127.0.0.1 only.

The last command prints:

```
CUPPSPN / CUPPSPP ... 127.0.0.1 / 7535
platform console .... http://127.0.0.1:8640/
airline agent app ... http://127.0.0.1:8631/
printed documents ... demo-output/prints
CUPPSPLT001BC1  BC  socketpair:bench     (pty:/dev/pts/0 on Linux/macOS)
...
```

Open both URLs side by side: the **platform console** on the left and the
**agent app** on the right.

Each bench device is a real operating-system channel: a connected socket
pair on Windows, a pseudo-terminal on Linux and macOS. The bench buttons
write bytes into it, and the same drivers that would read a scanner read
those bytes. No step of the demo bypasses the
driver. For a cleaner story, start without `--agent` and bring the
application in during Act 2.

## Act 1: the platform owns the peripherals (2 minutes)

*Console only. Start without `--agent`.*

- The header shows the platform in `pStd` and the workstation in `wStd`,
  which are the Part II state machines from chapters 6 and 7. Each device
  card shows its chapter 10 state.
- Every device is marked **secured**. No application holds them.
- Click **Scan config barcode** on BC1. The discard counter increases and
  nothing reaches any application. This is §10.4.1. On most CUPPS
  platforms today, a configuration barcode scanned at an unattended desk
  can reprogram the scanner.

## Act 2: an airline application arrives (3 minutes)

In a second terminal, start the application using the standard CUPPS
environment variables:

```powershell
$env:CUPPSPN="127.0.0.1"; $env:CUPPSPP="7535"; $env:CUPPSAL2="BA"
python -m cuppsd --airline-name "BRITISH AIRWAYS"
```

On Linux or macOS:
`CUPPSPN=127.0.0.1 CUPPSPP=7535 CUPPSAL2=BA python3 -m cuppsd --airline-name "BRITISH AIRWAYS"`

- The console shows `CUPPSAGENT aStd`, and the event log shows
  `aStdEnteredEvent` followed by the device `acquire` events.
- Devices change to **held by 1 session**. Their drivers were unsecured
  only because an application acquired them.
- The agent app shows **Connected** and a ready tile for each device.

## Act 3: the boarding gate (3 minutes)

- Click **Scan boarding pass** on the console. The agent shows the
  passenger (JOHN MR SMITH, BA117 LHR→JFK, seat 32A) and a
  **BOARD** verdict.
- In the console event log, point out `driver:barcode`, which is the raw
  scan the driver read, followed by the protocol traffic.
- Click **Swipe card** on MS1. The tracks are encrypted to the application's
  device token (AES-128, §30.30) and decrypted by the application.

## Act 4: a fault at the printer (2 minutes)

- Click **Paper out** on BP1. The driver receives the ATB status word
  `ST01P`, and the agent's BP tile changes to *Ready (out of paper)*.
  Paper-out is reported independently of ready, as §30.2 requires. Many
  platforms get this wrong and report the printer offline.
- Click **Paper in**. The printer recovers.

## Act 5: print (2 minutes)

- In the agent, open **Check-in**, fill in a passenger and press
  **Print boarding pass**.
- The console shows the lock, busy, print, unlock sequence, and a PDF
  appears in `demo-output/prints/`. Open it.
- The PR device uses the file backend here. Changing one line in the
  binding sends the same job to a CUPS queue or the Windows spooler.

## Act 6: self-certification (4 minutes)

In a third terminal:

```powershell
python -m conformance --engine --launch "python -m cuppsd --airline BA --port 8799"
```

- The harness starts the real engine, launches the application,
  exercises it, forces a connection drop and a directed stop, and grades
  22 rules. Each rule cites the specification paragraph it enforces.
- Open `conformance-record.html`. The result is **22 passed, 0 failed**.
  An airline developer gets this report by running the same command
  against their own application.
- Show a failure. `tests/fixtures/nonconforming_app.py` is an application
  that breaks rules deliberately:
  `python -m conformance --engine --launch "python tests/fixtures/nonconforming_app.py"`
  gives 7 failed and the verdict **NOT READY for compliance testing**, and
  each failure names the rule and the message that broke it.

Then run the release gate:

```powershell
python tools/interface_gate.py --check
```

The output `Interface unchanged ... does not require re-certification
under section 17.1.2` is the evidence for a §21.3.4 Standard Change: this
release can go to 500 airports without a change advisory board.

## Act 7 (optional): real hardware

Plug in a USB-serial barcode scanner or ATB printer and write a binding
(see `cuppsplatform/bindings/README.md`). Then run:

```powershell
python tools/labbench.py --bindings .\bench --probe
python -m cuppsplatform --bindings .\bench --agent
```

On Windows a USB-serial device appears as a COM port. Find it in Device
Manager under *Ports (COM & LPT)* and put it in the binding, for example
`"transport": {"kind": "serial", "port": "COM17"}`. Ports above COM9 are
handled for you.

The code path is identical. The console hides the bench buttons for real
hardware and refuses bench requests for it, because a simulated read of
a real device would be a fabricated read. `docs/LAB.md` covers
bring-up.

## If something goes wrong

| Symptom | Fix |
|---|---|
| `Address already in use`, or on Windows `WinError 10048` | Pass `--platform-port`, `--device-port`, `--console-port` or `--agent-port` with free ports. |
| Agent shows *Disconnected* | Check that the platform terminal is still running and that the agent's `CUPPSPP` matches `--platform-port`. |
| Scans do nothing | Expected when no application holds the reader (Act 1). Start the agent. |
| No PDF | Check the path printed on the `printed documents` line. |
| Windows: `python` opens the Microsoft Store | Use `py` instead of `python` in every command, or install Python from python.org with *Add to PATH* ticked. |
| Windows: `pip install` fails building `pycryptodome` | Upgrade pip first: `python -m pip install -U pip`. A wheel then installs with no compiler. |

## What not to claim

Be precise about these points in front of an audience:

- This is **not certified**. Platform compliance must be tested by a CTE
  (§13.1.2). The harness reports conformance evidence, and its record
  says it is not a certificate.
- XML schema validation, which §27.1.4 makes mandatory for platforms, is
  not implemented yet. Chapter 20 security, persistence, metrics, HID
  and RFID readers are also missing.
- It runs on Windows from Python, but there is no installer and no
  Windows service wrapper yet.
