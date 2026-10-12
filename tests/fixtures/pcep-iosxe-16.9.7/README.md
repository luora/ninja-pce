# Historical IOS XE RSVP-TE PCRpt

`tunnel100-pcrpt.hex` is the exact 156-byte PCRpt frame received from PE1
(`10.255.0.1`), a Cisco CSR1000v running IOS XE 16.9.7, at
2026-10-10 19:02:00.922943 UTC. It was extracted without modifying any wire
bytes from receive event 120 of
`build/live-lab-evidence/first-tunnel/pcep-created.json`. The fixture contains
the original hexadecimal frame followed by one newline, not a synthetic
encoding of its parsed fields.

Source capture SHA-256:

```text
bb4ef97cbaf2c4a014fd94a28cb4fa84dfed88e93664cd478fcff67781bfcb55
```

Decoded wire-frame SHA-256 (enforced by the replay tests):

```text
b871d486e941beeea35690402c7677daa83abc56c4d43c56369f27cde45ad797
```

PE1 had just established the manually configured RSVP-TE Tunnel100 to
`10.255.0.6`, reserving 50 Mbps at setup/holding priorities 7/7 along strict
remote interface hops `10.0.13.3`, `10.0.34.4`, and `10.0.46.6`. No autoroute
was configured. The actual report carries PLSP-ID 524388, symbolic name
`Tunnel100_w`, tunnel ID 100, LSP ID 1, administrative-up and operational
`ACTIVE` flags, and no delegation. It also carries TLV 31 and BANDWIDTH
object type 5; the passive parser retains these extension bytes without
inventing their semantics.

This frame is historical observation evidence. It contains the intended ERO,
with no RRO; it is not independent proof of present-day forwarding or an
authoritative reservation inventory. The replay test uses an explicitly
simulated, synchronized in-memory session to exercise observation storage,
then checks that losing that connection marks the report stale. It opens no
sockets and grants no delegation or execution authority.

Run offline from the repository root:

```bash
.venv/bin/pytest -q tests/test_pcep_live_capture.py
```

## Initial delegated computation qualification

These fixtures are unchanged frames from receive events 26/29 and transmit
event 27 of `build/live-lab-evidence/pce-compute/pcep-T100-primary.json`, source
SHA-256 `62f128286e2c704dba3447032cd4bc7aedd60c262a3ce12e1d175826ab4a6250`.
They belong to PE1's same live session, PLSP-ID 524388, tunnel 100, RSVP LSP
ID 224. Each file contains the original hexadecimal bytes and a newline.

| Fixture | UTC timestamp, 2026-10-10 | Bytes | SHA-256 of decoded frame |
|---|---|---:|---|
| `tunnel100-initial-delegation.hex` | 20:12:09.803614 | 132 | `0884b706110e3329893be514740686e6d1646219d08420bbda99bc2cacb0ecf5` |
| `tunnel100-initial-pcupd.hex` | 20:12:09.974315 | 128 | `30c1a89b0b074e2a2a3f29732afbb758371c607bd85d29cf54fb6eae94f3a8d8` |
| `tunnel100-computed-active.hex` | 20:12:10.059688 | 156 | `2b5cd90e1fef9710abebe15672e46ee7e3e3d19b83d71accc62414f417604657` |

The initial PCRpt has D=1, A=1, O=DOWN, an empty ERO and SRP-ID 0. It requests
50 Mbps at priorities 7/7. This IOS image supplies a nonzero RSVP LSP ID even
for that initial unsignalled report; this is observed compatibility, not a
claim of complete RFC conformance. Ninja-PCE's PCUpd has SRP-ID 3, D=1 and
strict ERO `10.0.12.2`, `10.0.25.5`, `10.0.56.6`. The same-session PCRpt
acknowledges SRP-ID 3 and reports ACTIVE with that ERO and bandwidth.

The exact BGP-LS snapshot used by this computation is
`../bgpls-iosxe-16.9.7/bgpls-before-pce-compute-raw.json`. Replay uses its
historical clock and constructs simulated collector-health files; it does
not make historical evidence fresh or confer current authority.
`test_pcep_live_computation.py` checks exact response reproduction and the
separate directional reservation change. Independent router/RSVP/MPLS OAM
evidence is described in
[the live qualification report](../../../docs/live_pcep_computation_validation.md).

## Creation attribute mismatch regression

The 2026-10-11 UTC private PCInitiate experiment allocated an RSVP auto-tunnel
but ignored the requested 1 Mbps BANDWIDTH and changed LSPA exclude-any to
`0x0000ffff`. The controller rejected attribute application and retained its
hold. This is a negative qualification result, despite the ACTIVE report.

- `creation-request-sanitized.hex` reproduces the transmitted request with
  its 45-byte generated symbolic name replaced by a same-length placeholder.
  All endpoints, ERO, priorities, SRP/PST and bandwidth bytes match the observed
  request. It is not an unchanged raw wire capture.
- `creation-zero-bandwidth-reconstructed.hex` is **re-encoded from the parsed
  live ACTIVE report**, including the observed identity, flags, ERO, LSPA,
  BANDWIDTH, used bandwidth, TE metric and Binding SID. It is explicitly a
  reconstructed regression fixture, not evidence of original receive-byte
  ordering or SRP reserved bits. Its symbolic name uses the same placeholder.

No public addresses, credentials, actual generated name or session UUID are
included. Independent PE1 output showed tunnel62336/RSVP LSP1 at zero bandwidth
and the unchanged Tunnel100 at50Mbps.

The 2026-10-12 UTC repeat added two original wire frames with only the
same-length symbolic-name substitution described above:

- `creation-update-sanitized.hex`: the one owned PCUpd, SRP 2, requesting
  1 Mbps for the original allocated identity and strict upper ERO.
- `creation-update-removal-sanitized.hex`: PE1's correlated SRP 2 PCRpt,
  C=1/D=1/R=1, for that same identity. It contains no bandwidth attributes.

Independent headend inspection found the experimental auto-tunnel down with
zero configured bandwidth, no reservation for it, and Tunnel100 still up at
50 Mbps. These frames record a failed completion experiment. The R flag is not
an application acknowledgment or proof of owned cleanup/resource release.
Owned cleanup remains a separate qualification step.

## Diagnostic creation and exact-owned deletion

The 2026-10-12 UTC diagnostic supplied four additional original wire frames.
Only the 45-byte generated symbolic name was replaced with the same-length
placeholder `NINJA_CREATE_00000000000000000000000000000004`; all other bytes,
including object ordering, padding, SRP IDs, flags, identities and attributes,
are unchanged. These are sanitized captures, not reconstructed reports.

The create and ACTIVE frames came from zero-based events 7 and 9 of
`build/live-lab-evidence/creation-diagnostic-004/controller/created-pcep.json`,
source SHA-256
`4e6c90176f8e2fe73915a59feadd1d702d72554b6d984a77f490255a2940ac4c`.
The delete and removal frames came from zero-based events 10 and 11 of
`build/live-lab-evidence/creation-diagnostic-004/controller/cleanup-pcep.json`,
source SHA-256
`e51f9f2cb9d897d61ba735bca22baf98a0f1f502924762a122b1465f767cda73`.

| Fixture | UTC timestamp, 2026-10-12 | Bytes | SHA-256 of sanitized decoded frame |
|---|---|---:|---|
| `diagnostic-004-create-sanitized.hex` | 20:41:59.536655 | 152 | `c1968d000e6ca9ce81e56406f3bbcfd9a209fa32f0ed0c71762e8cb0241024ba` |
| `diagnostic-004-active-zero-bandwidth-sanitized.hex` | 20:41:59.687485 | 192 | `c9635ecf22ca68823f959ded794d76dbc2e08019761bcb3ce6764a57fa815892` |
| `diagnostic-004-delete-sanitized.hex` | 20:42:09.167281 | 84 | `f1d7f5e950d0347116025f099d936adb6396bba99cda6290ef5ce1d1ea432b4b` |
| `diagnostic-004-removal-sanitized.hex` | 20:42:09.190486 | 144 | `c3314bc120e5b4d2c385ee2eabfd6b88244d1b0b2b33f6d116f2476f35fac440` |

The creation PCInitiate has SRP-ID 1, a 1 Mbps BANDWIDTH object, priorities
7/7 with no affinity constraint, and the strict lower ERO `10.0.13.3`,
`10.0.34.4`, `10.0.46.6`. The ACTIVE report echoes SRP-ID 1 and the allocated
PLSP-ID 586624 / tunnel 62336 / RSVP LSP 1, but reports requested and used
bandwidth zero and LSPA exclude-any `0x0000ffff`.

The exact-owned delete PCInitiate carries SRP-ID **2**, SRP R=1 and that same
allocated PLSP-ID/name. PE1's following removal PCRpt carries SRP-ID **1**,
C=1/D=0/R=1, O=DOWN and the same full RSVP identity, with no bandwidth
attributes. It is not correlated to the cleanup SRP boundary. The controller
therefore retains the reservation hold; R alone does not prove resource
release. Independent router absence checks remain necessary.

`test_diagnostic_owned_delete_removal_has_old_srp_and_cannot_release_hold`
checks the captured facts and replays the frames against the manager with
explicitly simulated current session/timestamps. It preserves the wire SRP
IDs and identity, opens no sockets, and does not confer current authority on
historical evidence. No original generated name, live session UUID,
credentials or public addresses are included in these fixtures.
