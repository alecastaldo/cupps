# Device profiles

One JSON file per peripheral family. Adding support for a new device is
adding a file here — no code change, no release of `cupps/`, and therefore no
re-certification under TS 01.04.0004 section 17.1.2.

## Writing one

```json
{
  "schemaVersion": 1,
  "id": "vendor-family",
  "description": "What this covers",
  "verified": false,
  "match": { "deviceType": "PR", "vendor": "(?i)vendor", "model": "(?i)model" },
  "status": { "reportsPaperJamIndependently": false },
  "stocks": { "aliases": { "BP": "ATB2" } },
  "timing": { "printTimeoutSeconds": 45 }
}
```

`match.deviceType` is required; `vendor`, `model` and `miscInfo` are regular
expressions tested against the `vendorModelInfo` the platform returns. The
most specific match wins, so a model-specific profile beats a vendor-wide one
without editing either.

## verified

`"verified": true` means **someone has run `cuppsit -t <device>` against the
real hardware and confirmed the behaviour in this file**. Everything else is a
starting point written from a datasheet.

Keep it honest. `python3 tools/profiles.py --list` reports the split, and an
unverified profile in production is a known risk, not a surprise.

## Validating

```bash
python3 tools/profiles.py --validate            # schema and regex checks
python3 tools/profiles.py --explain LHRT4PR1    # which profile a device gets
```

CI runs `--validate`, so a malformed profile fails the build rather than the
gate at 0600.
