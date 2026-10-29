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
The [capability-driven tunnel workflow](docs/tunnel_capabilities.md) explains
missing provisioning prerequisites and preserves exact-plan approval and recovery
for qualified deployments.
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

