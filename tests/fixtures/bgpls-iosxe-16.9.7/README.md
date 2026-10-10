# Historical IOS XE / GoBGP interoperability captures

These are actual read-only GoBGP gRPC captures observed on 2026-10-10 from a
six-router Cisco CSR1000v IOS XE 16.9.7 lab. P1 (`10.255.0.2`) exported OSPF
BGP-LS to GoBGP 3.37.0 from peer `172.16.100.102`. The files were copied
byte-for-byte from `build/live-lab-evidence/controller/`; no NLRI, path
attribute, peer statistic, or timestamp was synthesized or rewritten.

| Fixture | Captured at (UTC) | RIB objects | Nodes / directed links | Excluded prefix NLRIs |
| --- | --- | --- | --- | --- |
| `bgpls-withdrawn-raw.json` | 2026-10-10 18:36:00.658203 | 42 | 6 / 14 | 22 |
| `bgpls-healthy-raw.json` | 2026-10-10 18:36:53.222648 | 45 | 6 / 16 | 23 |

The withdrawn capture was collected after P1 `GigabitEthernet2` was shut down
and the control plane converged. Both directions of P1–P2 (`10.0.25.2` ↔
`10.0.25.5`) disappeared. The healthy capture followed restoration and
convergence, restoring all 16 directed links. These captures show the exported
control-plane graph; they do not independently prove data-plane forwarding.

Each JSON retains GoBGP's `nlriBinary` and `pattrsBinary` fields as base64-encoded
wire bytes, along with the before/after peer observations and original poll
timestamps. Tests use all six loopback-to-node mappings and require the actual
exporting peer. The observed TE attributes are 1 Gbps maximum physical bandwidth,
100 Mbps maximum reservable bandwidth, 100 Mbps unreserved at each of eight
priorities, and metric 10 on every retained edge.

The timestamps are historical, not live freshness evidence. Offline replay
explicitly supplies `now=captured_at`; replay at `captured_at + 31 seconds`
must fail the default freshness check. Prefix NLRIs are retained in the raw
files but excluded from the TE graph, so both parsed snapshots have
`complete: false`. The BGP-LS adapter always keeps `authoritative: false` and
`execution_eligible: false`. A computed 50 Mbps strict ERO is a computation
result, not an installed or verified LSP.

SHA-256 of the original bytes (also enforced by the replay tests):

```text
52bd12841798adf238541e549efde7968e73ad463ccc6ce2dd4d7f68c193bd10  bgpls-healthy-raw.json
a60cfc23a16a6c21b9a59488110cf665568d1633c30a5528b1425dae94897c3f  bgpls-withdrawn-raw.json
```

Run offline from the repository root:

```bash
.venv/bin/pytest -q tests/test_bgpls_live_capture.py
```
