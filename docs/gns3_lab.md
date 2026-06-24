# ninja-pce GNS3 lab guide

This guide turns `ninja-pce` from an offline CSPF script into a usable GNS3 demo appliance.

The first GNS3 version is still deliberately safe:

- no PCEP controller behavior
- no automatic push by default
- no RSVP-TE signaling from FRR
- dry-run outputs first
- human approval before any Netmiko/NETCONF action

## Lab story

The demo shows why an external path computation helper is useful for operators that run RSVP-TE but do not have native PCEP/BGP-LS controller support everywhere.

The network has a short IGP path and a longer feasible TE path:

```text
                low available BW
           +--------- P1 --------- P2 --------+
           |          |           |           |
           |          |           |           |
PE1 -------+          |           |           +------ PE3
           |          |           |           |
           +--------- P3 --------- P4 --------+
                    higher available BW
```

For a 50 Mbps service from PE1 to PE3, the shortest IGP path may be unusable because one link has only 20 Mbps available. `ninja-pce` prunes that link and generates an explicit RSVP-TE path over the feasible path.

## GNS3 nodes

Minimum useful lab:

| Node | Type | Purpose |
|---|---|---|
| PE1 | IOS XRv / CSR / vMX / RouterOS capable router | RSVP-TE head-end |
| P1 | router | core transit |
| P2 | router | core transit |
| P3 | router | core transit / alternate path |
| P4 | router | core transit / alternate path |
| PE3 | router | RSVP-TE tail-end |
| ninja-pce | Docker container | computes CSPF and renders configs |
| FRR-LISTENER | optional Docker/FRR node | Phase 2 topology listener |

## Build the ninja-pce Docker image

From the repository root on the GNS3 VM or on a Linux GNS3 server:

```bash
bash gns3/scripts/build_ninja_pce_image.sh
```

This builds:

```text
ninja-pce:gns3
```

## Add the Docker node in GNS3

In GNS3:

1. Go to **Edit > Preferences > Docker containers > New**.
2. Select the GNS3 server where the image was built.
3. Choose **Existing image**.
4. Select `ninja-pce:gns3`.
5. Name it `ninja-pce`.
6. Use one adapter for management-only mode, or two adapters if you want separate management and lab networks.
7. Start command can stay default.
8. Console type: Telnet.

When the node starts, it opens a shell and seeds `/lab` with sample topology, demands, and inventory.

## Inside the ninja-pce node

Validate the default files:

```bash
ninja-pce validate \
  --topology /lab/topologies/gns3_multiarea.yml \
  --demand /lab/demands/customer-a-50m.yml
```

Run the base demo:

```bash
ninja-pce compute \
  --topology /lab/topologies/gns3_multiarea.yml \
  --demand /lab/demands/customer-a-50m.yml \
  --output-dir /outputs/base \
  --show-candidates \
  --show-pruned
```

Run all canned scenarios:

```bash
bash /opt/ninja-pce/gns3/scripts/demo_run.sh
```

Expected base result:

```text
Path: PE1 -> P3 -> P4 -> PE3
```

Expected pruned link:

```text
P1-P2: insufficient bandwidth: available 20 Mbps < demand 50 Mbps
```

## Output files

Outputs are written under `/outputs`:

```text
/outputs/base/TUN_CUSTOMER_A_PE1_PE3_generic.txt
/outputs/base/TUN_CUSTOMER_A_PE1_PE3_iosxr.txt
/outputs/base/TUN_CUSTOMER_A_PE1_PE3_junos.txt
/outputs/base/TUN_CUSTOMER_A_PE1_PE3_change_plan.md
/outputs/base/TUN_CUSTOMER_A_PE1_PE3_verification_commands.txt
/outputs/base/TUN_CUSTOMER_A_PE1_PE3_summary.json
```

Use the generated change plan as your operator handoff artifact.

## Management reachability

Edit:

```text
/lab/inventory/gns3_inventory.yml
```

Set each router management IP, platform, username, and password environment variable name.

Check TCP/SSH reachability:

```bash
python /opt/ninja-pce/tools/ssh_precheck.py \
  --inventory /lab/inventory/gns3_inventory.yml
```

Validate the inventory and preview Netmiko commands without pushing:

```bash
ninja-pce inventory-validate \
  --topology /lab/topologies/gns3_multiarea.yml \
  --inventory /lab/inventory/gns3_inventory.yml

ninja-pce push \
  --topology /lab/topologies/gns3_multiarea.yml \
  --demand /lab/demands/customer-a-50m.yml \
  --inventory /lab/inventory/gns3_inventory.yml \
  --output-dir /outputs/base
```

This is preview-only unless you add both `--execute --approve`.

To reconcile whenever the topology YAML changes:

```bash
ninja-pce reconcile \
  --topology /lab/topologies/gns3_multiarea.yml \
  --demand /lab/demands/customer-a-50m.yml \
  --inventory /lab/inventory/gns3_inventory.yml \
  --output-dir /outputs/reconcile \
  --watch
```

## Optional FRR listener

Use `gns3/frr-listener` when moving toward Phase 2.

The FRR listener should:

- form an IGP adjacency for visibility
- avoid becoming a transit router
- export or expose LSDB/topology data later
- not become the RSVP-TE tunnel head-end

Start with manual YAML first. Add FRR only after the demo path computation is stable.

## Operator demo flow

1. Show current topology YAML as the TED.
2. Show demand YAML for a 50 Mbps service.
3. Run `ninja-pce compute`.
4. Point out that the shortest path is pruned due to insufficient bandwidth.
5. Show selected feasible path.
6. Open generated IOS XR/Junos config.
7. Open generated verification commands.
8. Explain that the tool is still dry-run and approval-based.
9. Optional: simulate failure/maintenance using `customer-a-50m-avoid-pe1-p3.yml`.
10. Optional: show FRR listener as the future BGP-LS/topology ingestion step.

## Practical reminder

RSVP-TE reserves bandwidth for LSP admission/signaling. It does not police the customer. Customer CIR enforcement remains on PE policers, shapers, queues, and QoS policies.
