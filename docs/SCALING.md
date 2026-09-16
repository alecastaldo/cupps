# Scaling to thousands of workstations and hundreds of airports

How to run this application across a large estate without the certification
and change-management overhead growing with it.

---

## 1. The thing to internalise first: you do not deploy

Section 17.2.1 is explicit about who does what:

> The Application Supplier shall either send the release by e-mail or make it
> available in an agreed software repository as the hand-over process to the
> CUPPS Platform Supplier/Operator... **The Platform Supplier/Operator shall
> then raise the appropriate Work Order(s) and distribute the CUPPS
> Application to all the target sites by means of their own internal
> process.**

There is no fleet-update system for you to build. No agent, no MDM, no
rollout orchestration, no workstation inventory. Your unit of work is a
*package hand-over*, and your efficiency problem is:

1. make each hand-over cheap and hard to reject, and
2. avoid re-certifying on releases that do not need it.

### The cost matrix is not 500

| Axis | Count | Source |
|---|---|---|
| Application Compliance Test | 1 per OS per TS major version | §17.1.2 |
| Integration Test | one per **platform supplier** (~10–15 exist) | §17.1.3 |
| Beta Test | 1 operational site | §17.1.4 |
| Airports | **0 additional certification** | §17.1.2 |

Put the last row in your contracts verbatim, because sites will ask:

> **No site shall place requirements on an Application beyond those of CUPPS
> Compliance Testing. The Compliance Testing process shall preclude site
> specific CUPPS certification testing.** — §17.1.2

Budget for about a dozen integrations, not five hundred campaigns.

---

## 2. Firewall the interface layer

Section 17.1.2 lists four triggers for repeating Application Compliance
Testing. Three are occasional and obvious. The fourth fires by accident:

> Any modification to a CUPPS Compliant Application which changes the
> **software interface** to the CUPPS Compliant Platform.

Business logic, the agent UI, DCS adapters, document layouts, pricing, bag
rules — none of it triggers re-certification. Only the wire-facing layer does.

That is why `cupps/` is a separate package from `cuppsd/` with a strict
one-way dependency, and why the boundary is enforced mechanically rather than
by convention:

```bash
python3 tools/interface_gate.py --check     # CI gate; fails on an undeclared change
python3 tools/interface_gate.py --show      # the current surface
python3 tools/interface_gate.py --update --note "why the wire changed"
```

The gate signatures the public surface of `cupps/` — names, signatures, and
the values of protocol constants in `params.py`, `header.py` and
`results.py`. Refactoring a method body does not trip it; changing what goes
on the wire does.

Run it as a required CI job (`.github/workflows/ci.yml` has it). When it
fails, that is not automatically a mistake — it is the build telling you this
release costs a certification cycle, in time to decide whether you meant it.

**Two release trains follow from this:**

| Train | Contents | Cadence | Certification |
|---|---|---|---|
| Interface | `cupps/` | rare, deliberate | full re-compliance + all integrations |
| Application | `cuppsd/`, `webui/`, DCS adapters | as often as you like | none |

Get this right and the large majority of releases never see a CTE.

---

## 3. Make your updates a Standard Change — once per operator

Section 21.3.4:

> Upon agreement, a recurring change may be permanently categorized as
> standard. **Once categorized as such, no approval from a CAB or Change
> Manager is required.**

Negotiate once with each platform operator: *an application release whose
CUPPS interface signature is unchanged is a Standard Change*. An RfC is still
submitted so stakeholders know it happened (§21.3.4 requires that), but no
Change Advisory Board has to sit.

This converts a per-release CAB submission at every operator into a one-time
negotiation. The interface gate is what makes it credible — the release notes
carry the signature and state plainly whether it moved.

---

## 4. Use the clock the specification gives you

Section 17.2.3 provides two contractual numbers and one piece of leverage:

- **15 working days** — Work Order to activation at *all* targeted sites
- **5 working days** — minor and configuration changes (§17.2.4)
- and: *"there cannot be any unreasonable limitation (for example, arbitrary
  calendar, or date-related delays) imposed upon technical updates... bug
  fixes, modifications to address security-related changes"*

Fifteen working days is room for a **staged Work Order**: beta site → one
airport → one region → all. You do not control distribution, but you can
specify the sequence when you request it. Do it on every release — it is your
only canary.

Section 17.3.5 makes fallback/rollback documentation optional ("may"). At this
scale treat it as mandatory for yourself, and hand over the previous package
alongside the new one.

---

## 5. Generate the paperwork

Section 17.3.1 enumerates roughly twenty elements release notes *shall*
contain, down to "best time(s) of day to be reached, by GMT offset" and an
explicit "None" where a list is empty. Across a dozen suppliers and many
releases, writing that by hand is toil and a recurring cause of rejected
hand-overs.

```bash
python3 tools/release_notes.py --since v1.0.0 \
    --airline "EXAMPLE AIRLINES" \
    --contact-email ops@example.com --contact-phone "+44 20 0000 0000" \
    --contact-hours "0700-1900 GMT+0" \
    --sites LHR JFK FRA --beta-site LHR \
    --activation 2026-10-01T02:00Z
```

Fixed/added/removed come from the commit log, targeted specification versions
from the interface library, dependencies from the project metadata, device
requirements from the device-mode table — and the interface signature and
whether it moved, which is the field the platform supplier reads first.

---

## 6. Self-service conformance: catch it before integration

Section 17.1.2 contains a trap that is easy to miss:

> If a CUPPS Compliant Application **fails two integration attempts of
> multiple Platform Suppliers** and the failure is linked to the CUPPS
> interface, it would require going through Technical Compliance Testing
> again.

Two bad integrations and you are back at the CTE. Integration attempts are
therefore scarce, and discovering an interface fault during one is expensive.

The harness lets any developer produce a Compliance Testing Record before
asking for lab time:

```bash
# Assess an application the harness launches
python3 -m conformance --launch "python3 -m cuppsd" --name MYAPP --version 01.00

# Or one you start yourself, in any language, on any host
python3 -m conformance --observe
```

It stands up an instrumented platform, records every message, drives the
scenarios the specification calls out — including the platform-session loss
that §17.1.2 singles out — and evaluates 22 checks, each citing its clause.
Output is text, JSON and a self-contained HTML document to attach to an
integration request.

### What it is not

Section 17.1.2 reserves Application Compliance Testing to *a compliant
platform supplier or the IATA approved CTE*. **A developer cannot
self-certify**, and every report says so prominently. What the record does is
turn the formal test from a discovery exercise into a review of evidence —
and that is where the cycle-time saving comes from.

Map the official CUPPS Application Test Cases (`CUPPS-ATC-01.04.*`, on the
IATA MS Teams site) onto the `atc_reference` field in
`conformance/checks.py`, and your record lines up line-for-line with what the
CTE works from.

### Make it a gate, not a ritual

Run it in CI on every build (`.github/workflows/ci.yml` does) and keep the
record as an artifact. A regression in closing handshakes or session recovery
then fails a pull request instead of an integration slot.

---

## 7. Onboarding DCS applications at the same scale

The same principle one layer up: a departure control team must never touch
`cupps/`, and their work must never trigger CUPPS re-certification.

**Give them an environment, not a booking.** A DCS team should not wait for
airport lab time to start:

```bash
docker compose up cupps              # agent UI on http://127.0.0.1:8631/
docker compose run --rm certify      # their own conformance record
```

The container carries the platform simulator, the device handler and the
harness. The simulator's injection endpoints — scans, card swipes, paper-out,
device-off — are the error paths a DCS team most needs to exercise and can
least easily produce at a real airport. Those endpoints exist **only** when
the handler runs against the simulator; a deployed position has no way to
fabricate a device read.

**Version the local API contract separately** from both the CUPPS interface
and the application. That contract is all a DCS adapter sees, and changing it
costs you nothing in certification.

---

## 8. Knowing what is actually out there

You cannot reach the workstations, and you did not distribute the software, so
without deliberate effort you will not know what is running where.

Have the application report, at startup and on reconnection: its own version
and interface signature, the platform vendor and version from
`<authenticateResponse>`, the negotiated interface level, the device
inventory, and its error counts. The platform version is also available from
the Platform Information Tool (§6.5).

Route it wherever your operation already collects telemetry. Two things fall
out of it that are otherwise guesswork: which sites are still on an old
release after a Work Order, and which platform supplier's estate a new fault
correlates with.

---

## 9. What this adds up to

| Activity | Naive | With the above |
|---|---|---|
| Certification per release | every release | only interface releases |
| Integrations per release | ~12 | 0 unless the interface moved |
| CAB submissions per release | one per operator | none, after a one-time agreement |
| Release notes | hand-written per hand-over | generated |
| Interface regressions found | at integration | in CI |
| DCS team onboarding | needs lab time | `docker compose up` |
| Site-specific test campaigns | as demanded | refused, citing §17.1.2 |

The consistent theme is that the specification already contains the machinery
for this. Most of the work is arranging your code and your contracts so you
can actually use it.
