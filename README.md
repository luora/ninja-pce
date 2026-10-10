# ninja-pce

> **Current lab preview:** start with the [tester guide](docs/tester_guide.md).
> Live BGP-LS ingestion and bounded PCEP initial-path provision have been
> [validated in GNS3](docs/live_pcep_computation_validation.md). The older status,
> roadmap and execution examples below are historical; use the current guides
> for supported behavior and approval requirements.

> Reliability remediation: current behavior and migration rules are documented in
> [input schema](docs/input_schema.md), [reconciliation safety](docs/reconciliation.md),
> [platform support](docs/platform_support.md), and the [engineering audit](docs/engineering_audit.md).
> These supersede the earlier implementation/status descriptions below. Parallel
> links now retain directional IDs, CSPF proves its ranking or returns SEARCH_LIMIT,
> strict EROs use numbered interface addresses, and CLI acceptance never implies
> a verified LSP. Execution uses an exact reviewed plan and shared local reservations;
> unsupported output and uncertain recovery fail closed. See the
> [GNS3 validation runner](docs/gns3_validation.md) for honest offline/device results.

`ninja-pce` is a practical external path computation and topology-reconciliation helper for RSVP-TE networks where the deployed routers may not support native PCEP, BGP-LS, or SR-TE controller workflows.

It is intentionally **not** a full PCEP controller. It takes a topology/TED file, a service-demand file, and an operator-maintained host/OS inventory; computes feasible bandwidth-aware RSVP-TE paths; renders platform configuration; and can deliver an explicitly approved update with Netmiko.

## What problem this solves

Many regional and NREN-style operators already run MPLS, RSVP-TE, VPLS/VPWS/L3VPN, and OSPF/IS-IS. The harder part is operational: platforms may lack BGP-LS/PCEP/SR-TE controller support, so engineers manually inspect TE topology, bandwidth, and failures before editing RSVP-TE explicit paths.

`ninja-pce` gives you a safe first step:

- ingest a simple Traffic Engineering Database from YAML/JSON
- prune links that cannot carry the requested reservation
- honor admin-group/color constraints
- honor avoid/include node/link constraints
- rank candidate paths
- generate explicit RSVP-TE config snippets
- generate change plans, verification commands, and rollback reminders
- validate that every topology node exists in the management inventory
- reconcile meaningful topology changes while suppressing no-op pushes
- preview every Netmiko change and require explicit approval before execution

## Important boundary

RSVP-TE is for LSP signaling and bandwidth reservation. It is **not** customer policing.

Customer CIR enforcement still belongs on the PE using policers, shapers, queues, hierarchical QoS, or service policies. `ninja-pce` only helps compute and document the transport LSP path.

## Current status

This repository is now beyond the first MVP, but still intentionally safe:

- ✅ manual YAML/JSON topology input
- ✅ bandwidth-aware CSPF
- ✅ affinity/admin-group constraints
- ✅ avoid nodes and links
- ✅ include required transit nodes
- ✅ candidate path ranking
- ✅ dry-run config rendering
- ✅ generic, IOS XR-style, IOS/IOS XE-style, and Junos-style templates
- ✅ change-plan markdown output
- ✅ verification command output
- ✅ summary JSON output
- ✅ mandatory host/OS inventory for southbound workflows
- ✅ IOS XR, IOS/IOS XE, and Junos device rendering
- ✅ preview-first Netmiko config delivery
- ✅ explicit `--execute --approve` safety gate
- ✅ topology watcher with debounce and semantic no-op suppression
- ✅ persistent reconciliation state and JSONL audit log
- ❌ no PCEP controller behavior yet
- ❌ no live BGP-LS ingestion yet

## Install locally

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

For Netmiko delivery:

```bash
pip install -e '.[southbound]'
```

For tests:

```bash
pip install -e '.[dev]'
pytest
```

## Run the main demo

```bash
ninja-pce compute \
  --topology examples/topologies/simple_multiarea.yml \
  --demand examples/demands/pe1_to_pe3_50m.yml \
  --output-dir examples/outputs \
  --show-candidates \
  --show-pruned
```

Expected selected path:

```text
PE1 -> P3 -> P4 -> PE3
```

The shorter path is not selected because `P1-P2` has only 20 Mbps available, while the service asks for 50 Mbps.

## Other demo demands

Avoid-link maintenance/failure example:

```bash
ninja-pce compute \
  --topology examples/topologies/simple_multiarea.yml \
  --demand examples/demands/pe1_to_pe3_50m_avoid_pe1_p3.yml \
  --output-dir examples/outputs \
  --show-candidates \
  --show-pruned
```

Large-demand example:

```bash
ninja-pce compute \
  --topology examples/topologies/simple_multiarea.yml \
  --demand examples/demands/pe1_to_pe3_120m.yml \
  --output-dir examples/outputs \
  --show-candidates \
  --show-pruned
```

## Input model

### Topology/TED file

A topology file contains nodes and links. Each link can carry TE attributes:

```yaml
links:
  - id: PE1-P3
    from: PE1
    to: P3
    from_ip: 10.0.13.1
    to_ip: 10.0.13.3
    metric: 25
    reservable_bw_mbps: 200
    reserved_bw_mbps: 20
    admin_groups: [GREEN]
    srlg: [ring-east]
    area: 0.0.0.1
    protection: link-protected
    bidirectional: true
```

Available bandwidth is calculated as:

```text
available_bw_mbps = reservable_bw_mbps - reserved_bw_mbps
```

### Service demand file

```yaml
service:
  name: CUSTOMER_A_PE1_PE3_50M
  source: PE1
  destination: PE3
  bandwidth_mbps: 50
  setup_priority: 7
  hold_priority: 7

  affinity:
    exclude_admin_groups: []
    include_any_admin_groups: []
    include_all_admin_groups: []

  path:
    hop_type: strict
    max_candidates: 4
    avoid_nodes: []
    avoid_links: []
    include_nodes: []
    max_hops:
    max_metric:

  output:
    dry_run: true
    tunnel_id: 100
    tunnel_name: TUN_CUSTOMER_A_PE1_PE3
    explicit_path_name: EP_CUSTOMER_A_PE1_PE3
    iosxr_interface: tunnel-te100
    junos_lsp_name: lsp-customer-a-pe1-pe3
    formats: [generic, iosxr, junos]
```

### Operator inventory

Every topology node must map to exactly one management host and supported
network OS before `push` or `reconcile` can run:

```yaml
devices:
  - name: pe1
    topology_node_id: PE1
    host: 192.0.2.11
    port: 22
    device_type: cisco_xr
    username: automation
    password_env: NINJA_PCE_PE1_PASSWORD
```

Use environment variables or a secret manager for credentials. Plaintext
password fields are not accepted. A complete example is available at
`examples/inventory/network.yml`.

Validate the topology-to-inventory mapping:

```bash
ninja-pce inventory-validate \
  --topology examples/topologies/simple_multiarea.yml \
  --inventory examples/inventory/network.yml
```

## Preview and apply configuration

Preview the exact commands selected for the source/head-end platform:

```bash
ninja-pce push \
  --topology examples/topologies/simple_multiarea.yml \
  --demand examples/demands/pe1_to_pe3_50m.yml \
  --inventory examples/inventory/network.yml \
  --output-dir build/outputs
```

After review, execution requires both flags:

```bash
export NINJA_PCE_PE1_PASSWORD='use-a-secret-provider-in-production'

ninja-pce push \
  --topology examples/topologies/simple_multiarea.yml \
  --demand examples/demands/pe1_to_pe3_50m.yml \
  --inventory examples/inventory/network.yml \
  --execute --approve
```

The push runs read-only pre-checks, sends only extracted configuration
commands, commits Junos changes, and records post-check output.

## Topology-triggered reconciliation

Run one reconciliation cycle:

```bash
ninja-pce reconcile \
  --topology examples/topologies/simple_multiarea.yml \
  --demand examples/demands/pe1_to_pe3_50m.yml \
  --inventory examples/inventory/network.yml \
  --output-dir build/reconcile
```

Keep watching the topology file:

```bash
ninja-pce reconcile \
  --topology examples/topologies/simple_multiarea.yml \
  --demand examples/demands/pe1_to_pe3_50m.yml \
  --inventory examples/inventory/network.yml \
  --output-dir build/reconcile \
  --watch
```

Formatting and list-order changes are ignored. Meaningful topology changes
rerun CSPF, compare the desired device config with state, and suppress a push
when the actual commands are unchanged. Add `--execute --approve` to a watcher
only when unattended operation under that explicit policy is intended.

## Multi-demand daemon

For a networked deployment, run the daemon against a config file. Manual mode
is the default: every demand is reconciled into preview/audit state, but no
router config is pushed.

```bash
ninja-pce daemon --config examples/daemon/manual.yml --once
```

Continuous mode also exposes health and metrics:

```bash
ninja-pce daemon --config examples/daemon/manual.yml

curl http://127.0.0.1:8080/healthz
curl http://127.0.0.1:8080/metrics
```

The daemon supports two approval modes:

- `manual`: preview only, no autonomous pushes
- `guarded-auto`: first previews the desired state, then applies only if all
  built-in safety gates pass

Guarded auto checks command allowlists, command count, output format, residual
bandwidth, CSPF warnings, required secrets, optional demand allowlists, optional
device tags, and the global `NINJA_PCE_PREVIEW_ONLY` kill switch.

## Generated outputs

For a demand named `TUN_CUSTOMER_A_PE1_PE3`, the tool writes:

```text
TUN_CUSTOMER_A_PE1_PE3_generic.txt
TUN_CUSTOMER_A_PE1_PE3_iosxr.txt
TUN_CUSTOMER_A_PE1_PE3_junos.txt
TUN_CUSTOMER_A_PE1_PE3_change_plan.md
TUN_CUSTOMER_A_PE1_PE3_verification_commands.txt
TUN_CUSTOMER_A_PE1_PE3_summary.json
```

## Suggested demo storyline

1. Show the topology and explain that normal IGP shortest path is not enough for RSVP-TE reservation planning.
2. Show `P1-P2` has only 20 Mbps available.
3. Request a 50 Mbps LSP from PE1 to PE3.
4. Run `ninja-pce compute`.
5. Show that the shorter path is pruned and the feasible path is selected.
6. Show candidate paths, generated IOS XR/Junos config, verification commands, and rollback plan.
7. Emphasize that this is an external helper for platforms that lack native PCEP controller support.
8. Show that execution is gated by inventory validation and explicit approval.

## Roadmap

### Phase 1 — complete

Manual topology input, CSPF, dry-run templates.

### Phase 1.5 — complete

Candidate ranking, constraints, operator change plans, verification output, richer TED attributes.

### Phase 2 — current

Host/OS inventory, approved Netmiko delivery, persistent desired state, and
topology-file reconciliation.

### Phase 2.25 — current

Multi-demand daemon mode, manual approval policy, guarded-auto policy gates,
topology freshness checks, and health/metrics endpoints.

### Phase 2.5 — next

Add topology ingestion adapters:

- FRR JSON/CLI parser
- ExaBGP message bridge
- BGP-LS normalized JSON input
- vendor CLI parsers for RSVP/TE topology snapshots

All adapters should convert into the same internal TED model used today.

### Phase 3

Expand templates:

- IOS XR
- Junos
- IOS XE
- MikroTik RouterOS RSVP-TE-style workflows where applicable
- generic CLI template

### Phase 4

Expand southbound adapters after the Netmiko workflow is proven:

- ncclient NETCONF
- PyEZ for Junos
- transactional rollback generation

### Phase 5

Smarter TE recommendations:

- aggregate LSPs per PE pair/class
- dedicated LSPs for high-value services
- reservation update recommendations
- admin-group and SRLG-aware diversity
- multi-area and H-PCE-style modeling
- auto-bandwidth awareness

## Repository principle

Do not vendor-copy FRR, ExaBGP, Netmiko, ncclient, Nornir, scrapli, NAPALM, Jinja2, or PyEZ into this repository. Study them as references and use them as dependencies or optional plugins when needed.

## GNS3 lab mode

The GNS3-ready lab wrapper includes the version `0.4.0` reconciliation flow.

Build the Docker appliance image from the repository root:

```bash
bash gns3/scripts/build_ninja_pce_image.sh
```

Then add `ninja-pce:gns3` as a Docker container in GNS3.

Inside the GNS3 container:

```bash
ninja-pce topology-summary --topology /lab/topologies/gns3_multiarea.yml

ninja-pce compute \
  --topology /lab/topologies/gns3_multiarea.yml \
  --demand /lab/demands/customer-a-50m.yml \
  --output-dir /outputs/base \
  --show-candidates \
  --show-pruned
```

Or run all canned GNS3 scenarios:

```bash
ninja-pce gns3-demo --show-candidates --show-pruned
```

GNS3 assets are under:

```text
gns3/
├── docker/ninja-pce/       # Dockerfile and container startup
├── frr-listener/           # optional Phase 2 FRR listener examples
├── lab-data/               # topology, demands, inventory
├── router-configs/         # starter IOS XR/Junos notes
└── scripts/                # build and demo helpers
```

Detailed guide:

```text
docs/gns3_lab.md
docs/operator_demo_runbook.md
```

The integrated southbound workflow is preview-first:

```bash
ninja-pce reconcile \
  --topology /lab/topologies/gns3_multiarea.yml \
  --demand /lab/demands/customer-a-50m.yml \
  --inventory /lab/inventory/gns3_inventory.yml \
  --output-dir /outputs/reconcile \
  --watch
```

No router changes are made unless you explicitly add `--execute --approve`
after reviewing the change plan and setting the referenced password variable.
