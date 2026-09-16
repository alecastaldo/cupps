# Conformance against CUPPS TS 01.04.0004

What is implemented, where, and how it is verified. Section numbers refer to
the Technical Specification, issue 01.04.0004 of 02JUN2021.

Legend: **✅** implemented and tested · **◑** partial, see note · **—** out of
scope for an application (platform obligation) · **✗** not implemented

---

## Part II — Platform functionality

These chapters bind the *platform*. An application's obligation is to behave
correctly against them, which is what the "application side" column records.

| § | Subject | Application side | Status |
|---|---|---|---|
| 6.3.13 | Token management | Token held for the platform connection only; invalidated on drop or `byeRequest`; every device session rebuilt | ✅ |
| 6.3.15 | Standard environment variables | All of Table 6.6 in `cupps/env.py`, including `CUPPSxxn` device assignments | ✅ |
| 6.3.12 | Logging | ZL device used for application activity; track data deliberately excluded | ✅ |
| 6.3.10 | Storage | Three storage areas read from `<authenticateResponse>` and the environment | ✅ |
| 9.1 | Application states | `aAth`/`aCts`/`aStd`/`aSpg`/`aStp` observed and published | ◑ ¹ |
| 9.3 | Application architectures | Architecture (b) of Figure 9.4: separate device handler | ✅ |
| 7, 8 | Workstation and user management | — | — |

¹ `aZom` is a state the *platform* assigns to an application that failed to
stop; the application cannot observe its own zombie state.

## Part V — Interfaces

### Chapter 26 — Interface standards

| § | Subject | Status | Where / verified by |
|---|---|---|---|
| 26.2–26.3 | Schema versions | ✅ | Negotiates 01.04 / 01.03 / 01.01 |
| 26.4 | Interface versions, one port | ✅ | `session._handshake` |
| 26.5 | Hostname and port from `CUPPSPN`/`CUPPSPP`, RFC 6335 range enforced | ✅ | `env.platform_port` |
| 26.6 | Conversation prototype | ✅ | `tests/test_conversation.py` |
| 26.7 | Token and device session invalidation | ✅ | `test_bye_invalidates_the_device_token` |
| 26.8 | Header versions; uppercase hex length; `invalidVersionNumberInHeader` | ✅ | Matches Listings 26.1 and 26.3 byte-for-byte |
| 26.9 | Streaming protocol; byte-by-byte header read; `PltStreamAccumTime` | ✅ | `tests/test_framing.py` |
| 26.10 | Base64 for binary, whitespace tolerated | ✅ | `test_base64_may_contain_whitespace` |
| 26.11 | All 45 platform parameters | ✅ | `cupps/params.py`, each with its section number |
| 26.12 | Message format; element order significant | ✅ | `test_element_order_is_preserved` |
| 26.13 | Locking `byConnection` / `byDeviceToken`; expiry; `deviceLockerInfo` | ✅ | `tests/test_conversation.py` |
| 26.13.3 | Sub-device (macro) locking | ◑ | Sub-devices are modelled and the platform enforces the rule; the client does not pre-empt it locally |
| 26.14 | Standard stock coordinate system | ✅ | `cuppsd/documents.py`, millimetres |

### Chapter 27 — Interface overview

| § | Subject | Status |
|---|---|---|
| 27.1.1 | Platform-originated notifications | ✅ |
| 27.1.2 | Illogical message processing | ✅ handled inbound; raised outbound by the simulator |
| 27.1.3 | TCP keep-alive on every socket | ✅ plus shortened probe intervals |
| 27.1.4 | Stream and parse errors → `sessionErrorEvent`, socket closed | ✅ with the offending body in Base64 |
| 27.1.5 | messageID ranges, wraparound, `MsgIDListLen` | ✅ |

### Chapters 28–30 — Use cases

| Message | Status | Note |
|---|---|---|
| `interfaceLevelsAvailableRequest` / `Response` | ✅ | |
| `interfaceLevelRequest` / `Response` | ✅ | |
| `authenticateRequest` / `Response` | ✅ | Full descriptor parsing incl. sub-devices |
| `deviceQueryRequest` / `Response` | ✅ | All three criteria of Table 29.1 are sent |
| `byeRequest` / `Response` | ✅ | |
| `applicationStopCommandRequest` / `Response` | ✅ | Including deferral up to `MaxSpgDeferTimes` |
| `deviceAcquireRequest` / `Response` | ✅ | Incl. `msFoidMasking`, all four result codes |
| `deviceReleaseRequest` / `Response` | ✅ | |
| `interfaceModeRequest` / `Response` | ✅ | Mode checked against Table 30.1 before sending |
| `deviceStatusRequest` / `Response` | ✅ | Incl. `pollingTooFast` |
| `deviceLockRequest` / `Response` | ✅ | All four results; Special Mode refused locally |
| `deviceUnlockRequest` / `Response` | ✅ | |
| `deviceParameterRequest` / `Response` | ◑ | Descriptor parsed at acquire; no separate request |
| `setCryptAlgorithmRequest` / `Response` | ✅ | |
| `readerReadRequest` / `Response` | ✅ | BC, MS and OC |
| `aeaRequest` / `Response` | ✅ | Text/binary segmentation per Table 30.4 |
| `printRequest` / `Response` | ✅ | PDF and simple text; per-document results |
| `printCancelRequest` / `Response` | ✅ | |
| `logOpen` / `Write` / `Retrieve` / `Close` | ✅ | Platform scope refused per 30.20.1 |
| `notify` (dataAvailable, status) | ✅ | |
| `downloadObject*` | ✗ | Platform-side print object cache (30.4) |
| `simpleHostPrint*` | ✗ | Spooler-driven host print (30.14) |
| `setStockNameAlias*` | ✗ | |
| `iataMessageRequest` / `Response` (ZI) | ✗ | ZI is acquired but Type B messaging is not implemented |
| `snTrigger*` / `snParameter*` (SN) | ✗ | |
| `setDeviceCommsAcks*` | ✗ | |
| `subscriptionAdd` / `List` / `Remove` (ch. 31) | ✗ | Event subscription; notifications work without it |

### Device coverage (Table 3.3)

| Device | Mode | Status |
|---|---|---|
| BC — barcode reader | Standard | ✅ read, Resolution 792 decoding |
| MS — magnetic stripe | Standard | ✅ read with AES/DES decryption |
| OC — optical reader | Standard | ✅ read |
| PR — printer | Standard | ✅ PDF and text printing |
| BP — boarding pass printer | AEA | ✅ acquire, `EP`, AEA passthrough |
| BT — bag tag printer | AEA | ✅ acquire, `EP`, AEA passthrough |
| BG — boarding gate reader | AEA / Standard | ◑ acquired and locked; e-gate flow not implemented |
| ZL — logging | Special | ✅ full log conversation |
| ZI — IATA messaging | Special | ◑ acquired; Type B messaging not implemented |
| BD — baggage drop | AEA | ✗ |
| SD — scale | AEA / Standard | ✗ |
| SN — snapshot / document reader | AEA / Standard | ✗ |
| EP — electronic payment | Standard | ✗ (Defined, Not Required in 01.04) |
| RW — raw | Raw | ✗ |

## Part III–IV — Certification, deployment, infrastructure

| Chapter | Status |
|---|---|
| 11.1 CPU affinity | ✅ CPython is multi-core safe |
| 11.2 CUPPSIT tool (`-h -v -i -d -t`) | ✅ formats match Listings 11.1–11.5 |
| 11.3 UI standards (F1, Alt-F4, Help/About) | ✅ |
| 11.4 Application logging | ✅ local plus ZL |
| 11.5 Registry restrictions | ✅ nothing written to the registry |
| 11.6 / ch. 17 Application packaging | ◑ see `docs/DEPLOYMENT.md` §5.2 |
| ch. 12–18 Certification | — process, not code |
| ch. 20 Security | ◑ loopback-only binding, DOCTYPE refused, no track data logged |
| ch. 25 Test cases and framework | ◑ 115 own tests plus a 22-check self-assessment harness (`conformance/`); the official CUPPS Application Test Cases are run by a platform supplier or the CTE |
| ch. 17.1.2 Compliance testing evidence | ✅ `python3 -m conformance` produces a Compliance Testing Record |
| ch. 17.3.1 Release notes | ✅ generated by `tools/release_notes.py` |
| ch. 21 Change management | ◑ `tools/interface_gate.py` decides whether a release is an interface change |

---

## Verified against the specification's own examples

Where the specification publishes a worked example, it is asserted rather than
paraphrased:

| Example | What it fixes | Test |
|---|---|---|
| Listing 26.3 | `0100000013<CUPPS>body</CUPPS>` | `test_sample_message_matches_listing_26_3` |
| Listing 26.1 | `01FF00003F` + 63-byte body | `test_invalid_version_reply_matches_listing_26_1` |
| Listing 30.30 | Device token `K39FM6AK2P10MG4K` and four track plaintext/ciphertext pairs | `tests/test_crypto.py` |
| Listing 28.3 | `<interfaceLevelRequest level="01.04"/>` | `test_interface_level_request_matches_listing_28_3` |
| Listings 28.1–28.4 | Handshake messages carry no default namespace | `test_handshake_messages_carry_no_default_namespace` |
| Listing 29.6 | BG macro device nesting BC and MS sub-devices | `test_device_list_includes_sub_devices_of_a_macro_device` |
| Listing 30.42 | `oneOrMoreIssues` with per-document results | `test_print_reports_per_document_results` |
| Table 30.4 | AEA text/binary reassembly order | `tests/test_conversation.py`, `cuppsd/aea.py` |
| Listing 11.1–11.5 | CUPPSIT output format | Manual, see `docs/DEPLOYMENT.md` §7 |

### A note on Listing 30.30

Section 30.3 names the algorithms `aes-strong` and `des-weak` but defers the
cipher construction to the CUPPS SDK sample package, which is not part of the
specification text. Listing 30.30 resolves the ambiguity: it publishes a
device token alongside four tracks' plaintext and ciphertext.

Those four vectors are satisfied by **AES-128 in ECB mode with PKCS#7 padding,
keyed by the 16-byte device token**, and by nothing else tried. In particular
CBC with a zero IV reproduces the three single-block vectors but *not* the
21-byte "Palm Springs, Florida" track, which is what discriminates the two.

ECB is a weak construction. It is not a choice made here; it is what the
published vectors require for interoperability.
