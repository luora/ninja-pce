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
