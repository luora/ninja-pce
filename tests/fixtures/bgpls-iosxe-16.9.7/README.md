# Historical IOS XE / GoBGP interoperability captures

The later PCE computation stage adds two byte-for-byte captures from
`build/live-lab-evidence/pce-compute/`. Both contain 45 RIB objects, six nodes,
16 directed links and 23 excluded prefix NLRIs:

| Fixture | UTC capture time, 2026-10-10 | SHA-256 |
|---|---|---|
| `bgpls-before-pce-compute-raw.json` | 20:12:07.093159 | `0b5ac0abbd343da6244b8b40953511d87b0ceb3f115cce36ec7049b866fc27c8` |
| `bgpls-pce-computed-raw.json` | 20:12:38.817300 | `78305ce5a6715c55077bcb5ef140fb7af8337555742c16ebd1ce053cde387468` |

The first is the exact snapshot named by the live SRP-ID 3 computation.
Priority-7 unreserved bandwidth is 50 Mbps on PE1→P3→P4→PE3 and 100 Mbps
elsewhere. In the second, it is 50 Mbps on PE1→P1→P2→PE3 and 100 Mbps
elsewhere, including all reverse links and the old path. Priorities 0–6
remain 100 Mbps. These aggregate observations corroborate independent RSVP
evidence; they do not identify reservation ownership by themselves. See
`tests/test_pcep_live_computation.py` and
[the qualification report](../../../docs/live_pcep_computation_validation.md).

These are actual read-only GoBGP gRPC captures observed on 2026-10-10 from a
six-router Cisco CSR1000v IOS XE 16.9.7 lab. P1 (`10.255.0.2`) exported OSPF
BGP-LS to GoBGP 3.37.0 from peer `172.16.100.102`. The three earlier files below were copied
byte-for-byte from `build/live-lab-evidence/controller/` (healthy/withdrawn)
and `build/live-lab-evidence/first-tunnel/` (reserved); no NLRI, path
attribute, peer statistic, or timestamp was synthesized or rewritten.

| Fixture | Captured at (UTC) | RIB objects | Nodes / directed links | Excluded prefix NLRIs |
| --- | --- | --- | --- | --- |
| `bgpls-withdrawn-raw.json` | 2026-10-10 18:36:00.658203 | 42 | 6 / 14 | 22 |
| `bgpls-healthy-raw.json` | 2026-10-10 18:36:53.222648 | 45 | 6 / 16 | 23 |
| `bgpls-reserved-raw.json` | 2026-10-10 19:03:44.954198 | 45 | 6 / 16 | 23 |

The withdrawn capture was collected after P1 `GigabitEthernet2` was shut down
and the control plane converged. Both directions of P1–P2 (`10.0.25.2` ↔
`10.0.25.5`) disappeared. The healthy capture followed restoration and
convergence, restoring all 16 directed links. These captures show the exported
control-plane graph; they do not independently prove data-plane forwarding.

The reserved capture is the third stage, after PE1's manually configured
50 Mbps RSVP-TE Tunnel100 was established to PE3. All 16 directed link
identities remain unchanged. Exactly PE1→P3, P3→P4, and P4→PE3 advertise
50 Mbps at unreserved priority 7 and 100 Mbps at priorities 0–6. Reverse
directions and all other links retain 100 Mbps at every priority. Maximum
physical bandwidth (1 Gbps) and reservable bandwidth (100 Mbps) stay unchanged.
The parser infers 50 Mbps aggregate reserved capacity on those three forward
edges from reservable minus priority-7 unreserved; the advertisement does not
identify reservation ownership. Admission remains conservative at every setup
priority and does not automatically preempt the existing reservation.

Each JSON retains GoBGP's `nlriBinary` and `pattrsBinary` fields as base64-encoded
wire bytes, along with the before/after peer observations and original poll
timestamps. Tests use all six loopback-to-node mappings and require the actual
exporting peer. The observed TE attributes are 1 Gbps maximum physical bandwidth,
100 Mbps maximum reservable bandwidth, and metric 10 on every retained edge.
Healthy/withdrawn edges have 100 Mbps unreserved at each of eight priorities;
the reserved capture has the three priority-7 changes described above.

The timestamps are historical, not live freshness evidence. Offline replay
explicitly supplies `now=captured_at`; replay at `captured_at + 31 seconds`
must fail the default freshness check. Prefix NLRIs are retained in the raw
files but excluded from the TE graph, so all three parsed snapshots have
`complete: false`. The BGP-LS adapter always keeps `authoritative: false` and
`execution_eligible: false`. A computed 50 Mbps strict ERO is a computation
result, not an installed or verified LSP.

SHA-256 of the original bytes (also enforced by the replay tests):

```text
52bd12841798adf238541e549efde7968e73ad463ccc6ce2dd4d7f68c193bd10  bgpls-healthy-raw.json
a60cfc23a16a6c21b9a59488110cf665568d1633c30a5528b1425dae94897c3f  bgpls-withdrawn-raw.json
0f754bb000ff05fdee536716be18db3407fee683b66f7911e13c247399b5ce79  bgpls-reserved-raw.json
```

Run offline from the repository root:

```bash
.venv/bin/pytest -q tests/test_bgpls_live_capture.py
```
