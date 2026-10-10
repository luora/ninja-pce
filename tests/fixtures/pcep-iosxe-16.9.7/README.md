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
