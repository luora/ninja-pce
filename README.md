# Ninja-PCE

Ninja-PCE is a topology-driven PCEP application for IPv4 RSVP-TE networks. It
collects a live BGP-LS traffic-engineering database, discovers eligible PCCs
within a configured routing domain, computes constrained paths, and delivers
initial paths to delegated router-owned LSPs. A web dashboard shows topology,
protocol peers, tunnels, reservations and events.

## Run the application

On Linux or macOS, install Python 3.10 or later with `venv` and `pip` support,
then clone and launch:

```bash
git clone https://github.com/luora/ninja-pce.git
cd ninja-pce
./ninja-pce
```

Open <http://127.0.0.1:8088>. The launcher prepares its own Python environment
and starts a labelled sample dashboard. Explore the topology, select a tunnel,
and inspect its path and bandwidth. Stop the application with Ctrl-C.
The first launch downloads pinned dependencies; later launches reuse the local
installation. Application files and your local configuration live in the ignored
`.ninja-pce/` directory.

To connect your network, generate a workspace and edit its configuration:

```bash
./ninja-pce init
./ninja-pce doctor
./ninja-pce start
```

The sample uses synthetic data. New live workspaces use
[observation mode](docs/observation_mode.md), with no PCC required. Live topology
requires GoBGP, `grpcurl` and routed exporter reachability. The [getting-started guide](docs/getting_started.md)
walks through the sample, live configuration and updates. The [deployment guide](docs/deployment.md)
covers a managed installation and authenticated dashboard provisioning.

## Application capabilities

Version **0.7.0** adds the clone-and-run application launcher and network
workspace commands. Version **0.6.0** introduced domain-wide dashboard provisioning for authenticated
users, separate SSH transport enrollment, concurrent operation ownership, and
an explicit migration that preserves existing authority and recovery history.
See [domain provisioning](docs/domain_tunnel_provisioning.md) and its
[validation record](docs/domain_tunnel_provisioning_validation.md).
One configuration selects observation or existing PCEP control. The dashboard
stays available after a protocol failure, and readiness reflects the selected
mode. Start with the
[user guide](docs/user_guide.md) and [deployment guide](docs/deployment.md).
The deployment package includes a wheel, source archive, offline dependency
wheelhouse, checksums, configuration examples and a systemd installer.
The optional [verified offline bundle](docs/offline_bundles.md) also packages
patched GoBGP and grpcurl with provenance and a signed manifest for fresh
observation deployments.
Managed services report [listener ownership and startup faults](docs/runtime_startup.md)
and bound retries while preserving the dashboard after protocol failures.
New observers can use [authenticated guided setup](docs/authenticated_setup.md)
to review and activate generated configurations in a separate private workspace.
The [diagnostics guide](docs/diagnostics.md) describes shared CLI and GUI findings,
explicit read-only live checks and manual repair guidance.
See the [application validation record](docs/application_deployment_validation.md)
for packaging, CI and deployment evidence.

```bash
ninja-pce controller validate --config /etc/ninja-pce/controller.json
ninja-pce controller plan --config /etc/ninja-pce/controller.json
ninja-pce controller run --config /etc/ninja-pce/controller.json
ninja-pce controller status --config /etc/ninja-pce/controller.json
```

Use `run` in the foreground or install the supplied systemd service. Configure
GoBGP and routed controller reachability as described in the deployment guide.
Ordinary PCCs and tunnel IDs require no individual controller registration;
each router needs its global PCC configuration once.

| Workflow | Authority and behavior |
|---|---|
| Router-owned tunnel | Domain policy, fresh synchronized delegation and supported constraints permit initial PCUpd; the router signals RSVP |
| Dashboard tunnel provisioning | A separately qualified SSH adapter delivers an exact reviewed configuration and records ownership and reservation holds |
| Managed recovery | The exact owned operation and its persistent recovery journal authorize bounded failure/startup recovery |
| Offline planning | YAML/JSON inputs produce CSPF paths, platform configuration previews and verification commands |

Live qualification covers CSR1000v IOS XE 16.9.7. The
[eight-router domain trial](docs/topology_domain_validation.md) verified initial
inter-area paths, RSVP reservations, MPLS forwarding and healthy listener
restart. Rendering and mock parsers cover additional platform dialects;
[platform support](docs/platform_support.md) records their qualification scope.
General active optimization, PCReq reoptimization, native bandwidth-aware
PCInitiate and authenticated PCEPS remain outside the qualified application
profile. GoBGP is the external BGP speaker; Ninja-PCE reads its protected local
API.

Historical evidence and engineering details remain available through
[input schema](docs/input_schema.md), [reconciliation](docs/reconciliation.md),
[architecture](docs/architecture.md) and [engineering audit](docs/engineering_audit.md).

## What problem this solves

Traffic engineering depends on an accurate view of topology, available bandwidth,
path constraints, and network failures. Ninja-PCE uses that information to compute
and validate paths, with policy-controlled provisioning and recovery for qualified
deployments.

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

- Done: manual YAML/JSON topology input
- Done: bandwidth-aware CSPF
- Done: affinity/admin-group constraints
- Done: avoid nodes and links
- Done: include required transit nodes
- Done: candidate path ranking
- Done: dry-run config rendering
- Done: generic, IOS XR-style, IOS/IOS XE-style, and Junos-style templates
- Done: change-plan markdown output
- Done: verification command output
- Done: summary JSON output
- Done: mandatory host/OS inventory for southbound workflows
- Done: IOS XR, IOS/IOS XE, and Junos device rendering
- Done: preview-first Netmiko config delivery
- Done: exact-plan approval with `--execute --approve --approved-plan-id`
- Done: topology watcher with debounce and semantic no-op suppression
- Done: persistent reconciliation state and JSONL audit log
- Done: multi-demand daemon with guarded autonomous delivery policy
- Done: read-only live BGP-LS ingestion through GoBGP
- Added: data-driven IOS XE 16.9.7 underlay rendering and opt-in OSPFv2 ABR
  identity projection for an isolated multi-area collector
- Done: passive stateful PCEP, bounded PCReq/PCRep and opt-in initial PCUpd
- Added: [topology-domain PCC discovery](docs/topology_domain_delegation.md)
  and bounded initial delegation without registering individual routers or
  tunnel IDs; exact managed recovery authority remains separate
- Done: opt-in one-tunnel withdrawal recovery, qualified for one IOS XE 16.9.7 lab scenario
- Done: read-only local controller dashboard, with explicitly labelled demo mode
- Done: gated dashboard configuration previews, exact-plan delivery, shared
  reservation holds and independently checked initial PCEP control; one private
  PE1 → PE3 1 Mbps configuration-provisioning trial passed on IOS XE 16.9.7
- Experimental: native PCInitiate, disabled for the tested RSVP bandwidth profile
- Not implemented: general active optimization, PCReq reoptimization or durable
  delegated ownership across controller restarts

Rendering and mock-tested operational parsers cover IOS XR 7.9.x, IOS XE
17.12.x and Junos 23.4R candidate dialects. Actual live qualification is currently
limited to CSR1000v IOS XE 16.9.7 BGP-LS/PCEP, separate RSVP forwarding
observations, and one bounded
[configuration-provisioning trial](docs/dashboard_configuration_validation.md).
That new-tunnel trial proves RSVP reservation and initial PCEP control, with
no payload-forwarding or throughput claim. See [platform support](docs/platform_support.md)
and the [disabled deployment examples](docs/dashboard_provisioning_deployment.md).

## Technical overview

`ninja-pce` is implemented as a Python 3.10+ CLI package with a small library
core under `src/ninja_pce`. The runtime path is intentionally explicit:
operator-authored topology, demand, and inventory files are loaded into typed
dataclasses; the CSPF engine computes and ranks feasible paths; Jinja2 renders
platform-specific RSVP-TE configuration; and the optional southbound layer
previews or applies the resulting command set through Netmiko. Live collectors
publish snapshots for the same computation model. The dashboard configuration
profile binds one new tunnel's shared reservation hold to exact initial PCEP
authority. General PCEP computation and withdrawal recovery remain separate
opt-in paths without global reservation integration.

For the rendered pictorial view, see [docs/architecture.md](docs/architecture.md).

```text
YAML/JSON topology  YAML/JSON demand  YAML/JSON inventory
       |                  |                   |
       v                  v                   v
  loaders.py  ---->  models.py domain objects  <---- inventory.py
                         |
                         v
                    cspf.py CSPF
                         |
                         v
        render.py Jinja2 outputs + summary JSON
                         |
               +---------+----------+
               |                    |
               v                    v
        reconcile.py state     southbound.py Netmiko
               |
               v
        daemon.py multi-demand cycles,
        approval policy, health, metrics
```

### Core package layout

- `models.py` defines immutable dataclasses for the traffic-engineering domain:
  `Topology`, `Node`, `Link`, `ServiceDemand`, `Affinity`,
  `PathConstraints`, `ManagedDevice`, `CandidatePath`, and `CspfResult`.
- `loaders.py` accepts `.yml`, `.yaml`, and `.json` inputs and validates the
  external file formats before the rest of the system sees them.
- `cspf.py` contains a bounded constrained search over NetworkX directed
  multigraphs, retaining exact selected edge identities.
- `render.py` wraps the Jinja2 template environment and writes rendered
  configuration, change plans, verification commands, and summary JSON.
- `inventory.py` normalizes supported platform aliases and enforces the mapping
  between topology node IDs and managed devices.
- `southbound.py` builds push plans, extracts only configuration commands from
  rendered templates, runs pre/post checks, and applies approved changes with
  Netmiko.
- `reconcile.py` adds desired-state comparison, semantic topology
  fingerprinting, config diffs, file/device locks, persistent state, and JSONL
  audit events.
- `daemon.py` runs reconciliation across one or more demand files, checks
  topology freshness, evaluates approval policy, and exposes `/healthz` and
  `/metrics`.
- `bgpls.py` and `live.py` collect directional BGP-LS data from GoBGP, preserving
  withdrawals, source health and unknown attributes.
- `lab_profile.py` validates topology intent and renders scoped underlay changes;
  `lab-render` produces configuration hashes, a node map and optional explicit
  ABR bindings without connecting to devices.
- `pcep.py`, `pcep_compute.py` and `pcep_delegation.py` implement observation and
  the explicitly enabled computation profiles described in [PCEP scope](docs/pcep_computation.md).
  `pcep_failover.py` adds separately enabled
  [one-tunnel withdrawal recovery](docs/pcep_failure_recovery.md).
- `dashboard.py` and `web/` serve a read-only view of snapshots, reports and events.
- `cli.py` is the command surface for `compute`, `validate`,
  `inventory-validate`, `push`, `reconcile`, `topology-summary`, `gns3-demo`,
  `daemon`, `lab-render`, `bgpls-collect`, `pcep-listen`, `dashboard`, `admin` and `diagnostics`.

### Input and domain model

The topology file is the Traffic Engineering Database snapshot. It carries
nodes and links, including TE metric, reservable bandwidth, already reserved
bandwidth, admin groups/colors, SRLG labels, area, protection metadata, link
directionality, and per-side IP addresses. Canonical accounting uses integer
bits/s and all eight unreserved-bandwidth priorities. Admission takes the
conservative minimum of setup-priority availability, priority-7 availability
and aggregate free capacity, then subtracts local pending holds. Preemption
is not enabled. Strict vendor EROs require numbered receiving-interface addresses.

The demand file describes one requested RSVP-TE LSP: source, destination,
bandwidth, RSVP setup/hold priorities, affinity rules, path constraints, and
output/rendering preferences. Path constraints include avoided nodes, avoided
links, required transit nodes, maximum hops, maximum metric, candidate count,
and ranking preferences.

The inventory file is required for CLI configuration delivery. Each
managed device maps exactly one `topology_node_id` to a management host,
Netmiko `device_type`, username, SSH port, tags, and a `password_env` secret
reference. Inline passwords are intentionally unsupported.

### CSPF algorithm

The CSPF implementation is deliberately reviewable by operators:

1. Validate endpoints, constraints and directional resources.
2. Normalize explicitly bidirectional input and preserve parallel edges.
3. Prune unavailable edges and bandwidth, affinity or avoidance violations.
4. Search loop-free edge paths while enforcing ordered waypoints, hop and
   metric limits.
5. Rank candidates lexicographically by enabled metric/headroom preferences,
   hop count and stable edge identity. The legacy score is display-only.
6. Return proven feasible candidates, or `SEARCH_LIMIT` if the bounded search
   cannot establish its result; budget exhaustion is never reported as no-path.
7. Render strict ERO hops from the selected edges' remote interface addresses.

The result records exact path edges, alternate candidates, pruning reasons,
metric, bandwidth headroom and warnings. Search is exponential in the general
case; the default budget is 100,000 edge expansions, not a scalability claim.

### Rendering and generated artifacts

Rendering is template-driven. The supported output formats are:

- `generic` for human-readable explicit-path output
- `iosxr` for Cisco IOS XR RSVP-TE configuration
- `iosxe` for Cisco IOS/IOS XE-style RSVP-TE configuration
- `junos` for Junos RSVP/MPLS LSP configuration

For each demand, `write_outputs` produces the requested config snippets plus a
change plan, verification command list, and machine-readable summary JSON. The
render context includes the topology, source/destination nodes, selected
`CspfResult`, demand output settings, bandwidth converted to kbps/bps/Junos
syntax, and a display path string.

### Push and reconciliation model

`push` is a preview-first delivery path. It computes CSPF, validates inventory,
chooses a renderer based on the source/head-end device type, extracts only the
configuration commands from the rendered template, and prints the exact command
set. Configuration delivery requires `--execute`, `--approve` and the exact
reviewed `--approved-plan-id`, followed by fresh input and device prechecks.

`reconcile` adds persistent desired-state behavior. It fingerprints topology,
demand, and inventory semantically, so formatting or list-order-only topology
changes do not trigger a configuration write. It persists desired, pending,
observed and last-verified revisions, preserves uncertain resource holds, uses
shared resource/device locks and appends JSONL audit events. Unchanged executed
plans still run read-only operational verification; a no-op is not proof of health.

### Daemon and guarded automation

The daemon runs the same reconciliation engine across a file or directory of
demands. In `manual` mode it stops at preview/audit state. In `guarded-auto`
mode it first creates the preview, then applies only if every policy check
passes. Built-in checks include:

- fresh, complete authoritative topology evidence
- output format allowlist
- command count limit
- exact command grammar plus platform allowlists
- minimum residual bandwidth
- CSPF warning policy
- optional demand-name allowlist
- optional device-tag requirements
- required `password_env` secret presence
- global `NINJA_PCE_PREVIEW_ONLY` kill switch

Long-running daemon mode exposes a JSON health endpoint at `/healthz` and
Prometheus-style counters at `/metrics`.

The supplied manual topology and current live BGP-LS feed are non-authoritative
for guarded automatic CLI delivery. Tags, credentials and a fresh file mtime
do not establish network authority. Existing TE object replacement and uncertain
rollback/release require ownership-aware manual recovery.

### Safety boundaries

Routers perform RSVP signaling and final admission. The initial PCEP profile
provides paths only for explicitly authorized delegated DOWN/empty-ERO LSPs.
Separately enabled [withdrawal recovery](docs/pcep_failure_recovery.md) can
replace one authorized path after a mapped healthy baseline and stable failure
evidence; healthy-path optimization and automatic switchback are excluded.
One [live IOS XE 16.9.7 withdrawal trial](docs/pcep_failure_recovery_validation.md)
passed with independent RSVP/OAM evidence; this does not qualify broader autonomy.
Its journal preserves uncertain intent but is not a shared reservation ledger
or durable delegated ownership. General optimization, PCReq reoptimization,
native bandwidth-aware PCInitiate, general PCEP-to-ledger integration and
authenticated PCEPS remain future work. Customer policing, shaping and queuing remain PE service
policy responsibilities. Reported ACTIVE state and advertised bandwidth are
distinct from independently verified forwarding and measured throughput.

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

For legacy scalar inputs without pending holds or a supplied priority vector,
available bandwidth reduces to:

```text
available_bw_mbps = max(reservable_bw_mbps - reserved_bw_mbps, 0)
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

For a separately qualified isolated lab, review the preview and supply its exact
plan ID. Set router credentials through your environment or secret provider:

```bash
read -rp 'Reviewed plan ID: ' NINJA_PCE_REVIEWED_PLAN_ID

ninja-pce push \
  --topology examples/topologies/simple_multiarea.yml \
  --demand examples/demands/pe1_to_pe3_50m.yml \
  --inventory examples/inventory/network.yml \
  --output-dir build/outputs \
  --execute --approve --approved-plan-id "$NINJA_PCE_REVIEWED_PLAN_ID"
```

Example inventory addresses are documentation placeholders. A real push requires
operator-managed reachable devices and a qualified release. The driver checks
exact configuration grammar and positive operational evidence, uses confirmed
commits for XR/Junos, and preserves uncertainty rather than claiming success.

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
when the actual commands are unchanged. These watcher examples are preview-only.
A changed plan needs a new exact approval; one approval does not authorize future
topology revisions. Use [reconciliation safety](docs/reconciliation.md) for details.

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
device tags, authoritative source evidence and the global `NINJA_PCE_PREVIEW_ONLY`
kill switch. All commands for the same network must share one persistent state
directory: set interactive `--state-dir` to the daemon config's `state_dir`.
The bundled static examples cannot enable guarded automatic writes.

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

- Complete independent live T02–T15 failure, contention, restart and recovery
  evidence; qualify XR/Juniper headends and mixed-vendor paths on exact releases.
- Integrate PCEP/RSVP observations with durable ownership and reservation
  accounting, including old-path holds and safe release.
- Extend the qualified one-tunnel withdrawal scenario to additional failures,
  no-path, contention and restart cases, then address general active optimization
  and the PCReq reoptimization extensions emitted by actual PCCs.
  Initial PCUpd alone does not enable recovery.
- Add authenticated transport/PCEPS, distributed fencing and qualified recovery
  before widening control authority. PCInitiate remains a separate extension.
- Extend the [qualified configuration-provisioning profile](docs/dashboard_configuration_validation.md)
  beyond one headend, destination and held tunnel only after independent
  reservation, ownership, failure/recovery and security qualification.
- Qualify IPv6/unnumbered, inter-area/inter-AS and diversity/protection behavior;
  investigate scalability and additional NETCONF/vendor adapters separately.
- Choose a project license and reproducible dependency pins before wider reuse.

See the [tester guide](docs/tester_guide.md) for practical assignments and the
[stateful architecture](docs/stateful_pce_architecture.md) for control boundaries.

## Repository principle

Do not vendor-copy FRR, ExaBGP, Netmiko, ncclient, Nornir, scrapli, NAPALM, Jinja2, or PyEZ into this repository. Study them as references and use them as dependencies or optional plugins when needed.

## GNS3 lab mode

The version `0.4.0` Docker wrapper seeds offline topology, demand, inventory and
an optional guarded-auto policy example. It defaults to an interactive shell.
These static multi-area examples are for computation/preview and cannot establish
authority for automatic delivery. The separately qualified Ubuntu live setup is
documented in [live protocol setup](docs/live_protocol_setup.md).

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
|-- docker/ninja-pce/       # Dockerfile and container startup
|-- frr-listener/           # optional Phase 2 FRR listener examples
|-- lab-data/               # topology, demands, inventory, daemon policy
|-- router-configs/         # starter IOS XR/Junos notes
`-- scripts/                # build and demo helpers
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
  --state-dir /outputs/daemon-state \
  --watch
```

This command is preview-only. Actual delivery needs an exact reviewed plan ID,
credentials and the release/source checks described above.

To inspect the optional guarded policy with router writes explicitly disabled:

```bash
NINJA_PCE_PREVIEW_ONLY=1 ninja-pce daemon --config /lab/daemon/guarded-auto.yml
```

Setting `NINJA_PCE_GNS3_AUTORUN=1` starts that daemon on boot; it does not make the
sample authoritative or enable PCEP failover. Stale input stops planning. Valid
fresh input can produce previews, but guarded writes additionally require fresh,
complete authoritative evidence and every delivery gate. The current live BGP-LS
source does not meet that authority requirement. Keep interactive delivery and
the daemon on `/outputs/daemon-state`; use that same directory for dashboard
ledger inspection. No Docker-image or autonomous multi-vendor qualification is
claimed by these examples.
