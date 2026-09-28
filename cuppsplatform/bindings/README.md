# Device bindings

One JSON file maps logical CUPPS device names onto physical peripherals.
A file may hold a single binding or a list.

```json
[
  {
    "device": "LHRT4LB00302PR1",
    "deviceType": "PR",
    "driver": "print",
    "transport": {"kind": "spooler"},
    "options": {"backend": "spooler", "backend_options": {"printer": "ATB2"}}
  },
  {
    "device": "LHRT4LB00302BC1",
    "deviceType": "BC",
    "transport": {"kind": "serial", "port": "COM3", "baudrate": 9600}
  },
  {
    "device": "LHRT4LB00302BP1",
    "deviceType": "BP",
    "transport": {"kind": "tcp", "host": "10.20.4.31", "tcp_port": 9100}
  }
]
```

`driver` may be omitted when the device type has a default: `reader` for
BC/MS/OC/SD, `aea` for BP/BT/BG/SN, `print` for PR.

`transport.kind` is `serial`, `tcp`, `loopback` or `pty`. COM ports above
COM9 may be written plainly — `COM17` — and are converted to the
`\\.\COM17` form the operating system needs (§6.3.20).

Directories are searched in order and a later one wins for the same device,
so a bench can redirect one device to a pseudo-terminal without editing site
configuration.
