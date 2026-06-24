# 25-minute operator demo runbook

## Goal

Show that `ninja-pce` can help an operator compute a feasible RSVP-TE explicit path when the deployed routers lack full PCEP controller support.

## 0:00-3:00 — Problem

Many regional operators already have MPLS, RSVP-TE, VPLS/VPWS/L3VPN, and OSPF/IS-IS. The issue is not that MPLS is missing. The issue is that operational path computation is often manual when platforms lack BGP-LS/PCEP/SR-TE controller support.

## 3:00-6:00 — Lab topology

Show the GNS3 topology:

```text
PE1 -- P1 -- P2 -- PE3
 |     |      |     |
 +---- P3 -- P4 ----+
```

Explain:

- shortest path exists
- not all shortest paths have enough reservable bandwidth
- RSVP-TE tunnel needs an explicit feasible path
- customer policing remains separate from RSVP-TE reservation

## 6:00-10:00 — Demand input

Open:

```bash
cat /lab/demands/customer-a-50m.yml
```

Key fields:

- source PE
- destination PE
- bandwidth
- constraints
- output format

## 10:00-15:00 — CSPF compute

Run:

```bash
ninja-pce compute \
  --topology /lab/topologies/gns3_multiarea.yml \
  --demand /lab/demands/customer-a-50m.yml \
  --output-dir /outputs/base \
  --show-candidates \
  --show-pruned
```

Explain:

- P1-P2 is pruned because available bandwidth is below 50 Mbps
- PE1-P3-P4-PE3 is selected
- candidate paths are ranked, not guessed

## 15:00-18:00 — Config output

Show:

```bash
cat /outputs/base/TUN_CUSTOMER_A_PE1_PE3_iosxr.txt
cat /outputs/base/TUN_CUSTOMER_A_PE1_PE3_junos.txt
```

Explain that the generated config is a proposal. Validate the required
host/OS inventory and preview the source-device commands:

```bash
ninja-pce push \
  --topology /lab/topologies/gns3_multiarea.yml \
  --demand /lab/demands/customer-a-50m.yml \
  --inventory /lab/inventory/gns3_inventory.yml \
  --output-dir /outputs/base
```

No connection is made unless both `--execute --approve` are supplied.

## 18:00-21:00 — Verification and rollback

Show:

```bash
cat /outputs/base/TUN_CUSTOMER_A_PE1_PE3_change_plan.md
cat /outputs/base/TUN_CUSTOMER_A_PE1_PE3_verification_commands.txt
```

Explain what an NOC engineer checks before and after the change:

- tunnel state
- selected path
- reserved bandwidth
- service binding
- interface utilization
- rollback commands

## 21:00-24:00 — Failure/maintenance scenario

Run:

```bash
ninja-pce compute \
  --topology /lab/topologies/gns3_multiarea.yml \
  --demand /lab/demands/customer-a-50m-avoid-pe1-p3.yml \
  --output-dir /outputs/avoid-pe1-p3 \
  --show-candidates \
  --show-pruned
```

Explain that this models an operator avoiding a link during maintenance or after a failure.

## 24:00-25:00 — Roadmap

Close with the staged path:

1. Manual YAML TED.
2. FRR topology listener.
3. BGP-LS/parsed topology ingestion.
4. Dry-run templates for more platforms.
5. Topology-triggered reconciliation with approved Netmiko delivery.
6. Smarter reservation and service-aware recommendations.
