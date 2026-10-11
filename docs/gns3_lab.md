# ninja-pce GNS3 lab guide

> This guide describes the optional Docker wrapper and synthetic sample inputs.
> For the qualified Ubuntu/six-router lab, follow [live protocol setup](live_protocol_setup.md)
> and [initial PCEP computation results](live_pcep_computation_validation.md).
> The [T01–T15 runner](gns3_validation.md) distinguishes offline assertions from
> actual device evidence; T02–T15 remain unexecuted end to end.

This guide turns `ninja-pce` from an offline CSPF script into a usable GNS3 demo appliance.

The Docker wrapper retains these boundaries:

- no automatic push by default
- no RSVP-TE signaling from FRR
- dry-run outputs first
- exact-plan approval before manual Netmiko delivery
- guarded-auto mode is opt-in and needs authoritative evidence
- the supplied static multi-area topology cannot authorize guarded writes

The separate GoBGP/PCEP services implement live observation and bounded initial
paths; they are not an autonomous failover integration in this wrapper. NETCONF
delivery is not implemented. Docker image execution and the mixed XR/Junos sample
are not established by the Cisco IOS XE 16.9.7 live qualification.

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
| PE1 | Separately qualified IOS XR, IOS XE or Junos image | RSVP-TE head-end |
| P1 | router | core transit |
| P2 | router | core transit |
| P3 | router | core transit / alternate path |
| P4 | router | core transit / alternate path |
| PE3 | router | RSVP-TE tail-end |
| ninja-pce | Docker container | computes CSPF and renders configs |
| FRR-LISTENER | optional experimental Docker/FRR node | Future collector experiments |

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
It also seeds `/lab/daemon/guarded-auto.yml`, an optional policy example. Existing
seeded volumes receive that file only if missing; operator edits are preserved.

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
  --output-dir /outputs/base \
  --state-dir /outputs/daemon-state
```

This is preview-only. Delivery additionally requires the exact reviewed
`--approved-plan-id`, external credentials and qualified platform checks.
Use `--state-dir /outputs/daemon-state` for every delivery command sharing this
lab, matching the daemon's persistent ledger directory. Addresses and usernames
in the inventory are private lab examples, not reachable operator endpoints.

To reconcile whenever the topology YAML changes:

```bash
ninja-pce reconcile \
  --topology /lab/topologies/gns3_multiarea.yml \
  --demand /lab/demands/customer-a-50m.yml \
  --inventory /lab/inventory/gns3_inventory.yml \
  --output-dir /outputs/reconcile \
  --state-dir /outputs/daemon-state \
  --watch
```

## Optional guarded daemon policy

The daemon polls topology and demand files and maintains reconciliation state.
Guarded delivery needs a fresh, complete authoritative TED plus every approval,
command, credential and release gate. The supplied static topology lacks that
evidence; the current live BGP-LS feed is also non-authoritative for automatic
CLI delivery. Do not invent authority metadata or update a file timestamp to
bypass that boundary. This example does not enable PCEP service recovery.

The seeded daemon config is:

```text
/lab/daemon/guarded-auto.yml
```

The legacy mixed XR/Junos inventory marks `PE1` with an optional `auto-apply`
tag. The policy requires that tag; a tag is not platform qualification or source
authority. Actual IOS XE 16.9.7 automatic CLI writes remain blocked.

Inspect the policy with writes explicitly disabled:

```bash
NINJA_PCE_PREVIEW_ONLY=1 ninja-pce daemon --config /lab/daemon/guarded-auto.yml
```

For a container that starts the daemon automatically on boot, set this
environment variable in the GNS3 Docker template:

```bash
NINJA_PCE_GNS3_AUTORUN=1
```

The kill switch is:

```bash
NINJA_PCE_PREVIEW_ONLY=1
```

The kill switch refuses router writes. Valid fresh input can still produce
previews and audit state; stale input stops before planning. Automatic startup
does not make the static sample authoritative. Source age is bounded to 30
seconds in this policy example. The health listener binds only to loopback.

Useful endpoints in continuous mode:

```bash
curl http://127.0.0.1:8080/healthz
curl http://127.0.0.1:8080/metrics
```

Autonomous mode reads topology from:

```text
/lab/topologies/gns3_multiarea.yml
```

It reads demands from:

```text
/lab/demands/
```

It writes rendered configs, JSON summaries, persistent reservation state and
audit events under:

```text
/outputs/daemon
/outputs/daemon-state
```

Interactive `push`/`reconcile` commands must use `--state-dir /outputs/daemon-state`.
If you choose another directory, update the daemon YAML too; daemon state is
selected by its configuration. Dashboard ledger inspection must use the same
directory. Keep this volume across restarts rather than deleting uncertain holds.

## Optional FRR listener

`gns3/frr-listener` is an experimental example, not the implemented live ingestion
path. Current live collection uses [GoBGP](../gns3/controller/gobgp/README.md).

The FRR listener should:

- form an IGP adjacency for visibility
- avoid becoming a transit router
- export or expose LSDB/topology data later
- not become the RSVP-TE tunnel head-end

Any future collector must retain directional identity, freshness and unknown
attributes. It must not turn missing evidence into authorization for delivery.

## Operator demo flow

1. Show current topology YAML as the TED.
2. Show demand YAML for a 50 Mbps service.
3. Run `ninja-pce compute`.
4. Point out that the shortest path is pruned due to insufficient bandwidth.
5. Show selected feasible path.
6. Open generated IOS XR/Junos config.
7. Open generated verification commands.
8. Explain exact-plan approval and demonstrate why the static sample cannot authorize guarded writes.
9. Optional: simulate failure/maintenance using `customer-a-50m-avoid-pe1-p3.yml`.
10. Compare the synthetic sample with recorded GoBGP/PCEP live evidence; label each scope explicitly.

## Practical reminder

RSVP-TE reserves bandwidth for LSP admission/signaling. It does not police the customer. Customer CIR enforcement remains on PE policers, shapers, queues, and QoS policies.
