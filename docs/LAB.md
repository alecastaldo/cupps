# Bench testing with real peripherals

How to get a peripheral from its box to "yes, it works with the platform".

Nothing here needs the rest of the platform running. The driver layer stands
alone deliberately, so a device can be proven before it is wired into a
position.

---

## 1. Rehearse with no hardware first

Pseudo-terminals present a real character device, so pyserial drives them
exactly as it drives a COM port. The whole path can be exercised before
anything is plugged in:

```bash
python3 tools/labbench.py --pty --list
python3 tools/labbench.py --pty --probe
```

This is also what CI runs, which is why the serial path is tested rather
than merely written.

## 2. Bind the real device

Create a JSON binding (see `cuppsplatform/bindings/README.md`):

```json
{
  "device": "LABBC1",
  "deviceType": "BC",
  "transport": {"kind": "serial", "port": "COM3", "baudrate": 9600}
}
```

COM ports above COM9 may be written plainly; `normalise_port` converts them
to the `\\.\COM17` form the operating system requires (§6.3.20).

```bash
python3 tools/labbench.py --bindings ./bench --list
python3 tools/labbench.py --bindings ./bench --probe
```

`--probe` opens every device and reports its status flags. A device that
comes back `NOT READY` has been reached but is unhappy; one that comes back
`FAILED` was not reached at all — wrong port, wrong baud rate, cable.

## 3. Prove each device type

**Readers (BC, MS, OC, SD)** — watch what the device actually sends:

```bash
python3 tools/labbench.py --bindings ./bench --watch LABBC1 --seconds 60
```

Scan something. You should see the decoded record. If you see two records
for one scan, the terminator is wrong for that device; if you see nothing,
check that the reader is configured to transmit a terminator at all.

**AEA devices (BP, BT, BG, SN)** — talk to them directly:

```bash
python3 tools/labbench.py --bindings ./bench --send LABBP1 "ST"
```

`ST` asks for status and is the safest first command. The driver sends `EP`
automatically when it opens the session, because §30.1 requires it.

**Printers (PR)** — print a test page:

```bash
python3 tools/labbench.py --bindings ./bench --test LABPR1
```

Start with the `file` backend to see exactly what the platform would send,
then switch to `cups` or `spooler` once the bytes look right.

## 4. What the driver layer enforces

Two behaviours that will look like bugs if you are not expecting them.

**A device that nothing holds ignores input.** §10.4.1 requires a platform to
logically secure an unheld device: a barcode reader "must be secured such
that it cannot be used (not even for using configuration barcodes) or that
any barcodes read are ignored". The reason is specific — a scanned
configuration barcode can reprogram the reader. So scanning at an idle
position produces nothing, by design. `labbench --watch` unsecures the device
explicitly for the duration of the run.

If you suspect this is biting you, `driver.discarded_while_secured` counts
what was dropped. A non-zero count on an idle reader is worth knowing about
for its own sake.

**A printer out of paper is still ready.** §30.2 requires paper status to be
reported independently of online status: "when pulling out paper without
lifting the print head, the generated notify will include paperOut=true and
must also still report ready=true if the printer is still online". A driver
that clears `ready` on paper-out makes a routine paper change look like a
dead device.

## 5. Recording what you learn

A peripheral that needed a non-obvious setting belongs in a **device
profile**, not in code:

```bash
python3 tools/profiles.py --list
python3 tools/profiles.py --match PR "Acme" "FastPrint 900"
```

Set `"verified": true` only once you have actually run the device test
against the hardware. `--list` reports the verified/unverified split, and an
unverified profile in production is then a known risk rather than a surprise.

This matters beyond tidiness: a profile is data, so adding a peripheral does
not touch `cupps/` and does not trigger the re-certification that §17.1.2
attaches to any change to the platform's software interface. Check it:

```bash
python3 tools/interface_gate.py --check
```

## 6. Backends for PR

| Backend | Use |
|---|---|
| `file` | CI, and a bench where the printer has not arrived. Writes each document to a directory. |
| `cups` | A real printer on a Linux bench, through `lp`. |
| `spooler` | A CUPPS workstation. Table 3.3 note 6 requires the Windows spooler; needs pywin32. |

## 7. Installing

```bash
pip install -e ".[serial]"            # serial peripherals
pip install -e ".[serial,spooler]"    # plus the Windows spooler
```

`pyserial` is imported lazily, so the rest of the platform runs without it
and a missing install produces a clear message rather than an import error at
startup.

## 8. What is not here yet

- **HID / keyboard-wedge readers.** Many USB scanners present as keyboards
  rather than serial ports. Those need a different capture path.
- **RW (raw) devices.** §10.4.2 says the platform's only duty is securing the
  I/O port; that is not implemented.
- **Unsolicited AEA status decoding beyond paper and jam.** Vendor status
  words carry more, and guessing at them would be worse than leaving it to a
  device profile.
- **Automatic device discovery.** Bindings are written by hand.
