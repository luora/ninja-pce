# Get started from a clone

Ninja-PCE includes a local application launcher for Linux and macOS. It creates
an isolated Python environment in the checkout, so you can use the application
without activating a virtual environment or installing the package globally.
You need Git and Python 3.10 or later with `venv` and `pip` support. On Debian or
Ubuntu, the Python virtual-environment support comes from `python3-venv`.

## Launch the sample

```bash
git clone https://github.com/luora/ninja-pce.git
cd ninja-pce
./ninja-pce
```

The first launch downloads pinned core dependencies and installs the application
in `.ninja-pce/venv`. The launcher prints its progress and dashboard URL. Open
<http://127.0.0.1:8088> in your browser. Nothing else needs to be configured to
explore the sample.

The **Sample workspace** banner identifies the synthetic six-router network.
Select a tunnel to inspect its explicit path, open Topology to examine directed
links and advertised bandwidth, and view the sample peers and computation
outcomes. The sample serves only the dashboard and has no router connection or
tunnel-creation access. It does not read live controller observations.

Stop the application with Ctrl-C. To use another local HTTP port:

```bash
./ninja-pce demo --port 8090
```

The dashboard binds to `127.0.0.1` by default. The launcher does not install an
operating-system service or open a browser automatically. Repeated launches
reuse the prepared environment and work offline when the checkout and dependency
metadata and dependency constraints have not changed. To install the application without starting it,
run `./ninja-pce setup`.

## Create a live workspace

```bash
./ninja-pce init
```

This creates `.ninja-pce/workspace` with a controller configuration and routing
domain policy under `config/`, plus separate directories for observations,
reservation state and runtime status. These local files are ignored by Git.
`init` prepares configuration; it does not connect to routers or start services.
Use `./ninja-pce init --directory /path/to/new/workspace` to choose a different
location. Initialization refuses an existing directory, including a partially
created workspace, so it cannot overwrite configuration or controller state.

Edit `.ninja-pce/workspace/config/controller.json` and its domain policy for your
network. The generated addresses and domain identifiers are examples that need
to be replaced. Configure:

- The protected local GoBGP API endpoint and expected BGP-LS peers.
- The actual routing protocol, instance, AS and BGP-LS identifiers, with the PCC
  and destination address prefixes permitted in the routing domain.
- The controller's routed PCEP listening address and the dashboard binding.
- The observation, reservation and runtime paths you intend this instance to own.

GoBGP supplies the BGP speaker, and `grpcurl` reads its API. Install and configure
those external tools separately using the [deployment guide](deployment.md).
The network needs a complete BGP-LS TE topology, RSVP/MPLS TE on the intended
paths, routed controller-to-PCC reachability, and global PCC configuration on
each router. See [network preparation and PCC bootstrap](user_guide.md#prepare-the-network).

Check the configuration and prerequisites before starting:

```bash
./ninja-pce doctor
./ninja-pce controller plan --config .ninja-pce/workspace/config/controller.json
./ninja-pce start
```

`doctor` validates local configuration and checks for `grpcurl` without opening
protocol listeners or contacting routers. `plan` shows the configured component commands.
`start` runs the live BGP-LS collector, PCEP listener and dashboard in the
foreground. It uses your configuration and does not fall back to sample data.
Stop it with Ctrl-C.

In a second terminal, inspect the running application:

```bash
./ninja-pce status
```

Status includes process and observation health. A fresh installation is degraded
until the topology is available and a PCC has connected and synchronized. A
reported ACTIVE tunnel describes the control plane; verify forwarding separately.

The `doctor`, `start` and `status` shortcuts use the workspace inside this
checkout by default. For a workspace elsewhere, pass its configuration explicitly:

```bash
./ninja-pce doctor --config /path/to/workspace/config/controller.json
./ninja-pce start --config /path/to/workspace/config/controller.json
./ninja-pce status --config /path/to/workspace/config/controller.json
```

Give each controller instance its own listener addresses and state paths. When
continuing an existing installation, use its reviewed configuration and preserve
its journals and reservation ledger. Run one supervisor for each configured set
of services and files; do not start another alongside its systemd service.

## Enable authenticated provisioning

The local sample and controller dashboard provide read-only observations.
Domain-wide tunnel provisioning uses a separately configured authenticated
HTTPS proxy and provisioning service, with explicit domain policy, independently
verified SSH host keys and router transport credentials.

Install the optional SSH adapter dependencies when preparing that service:

```bash
./ninja-pce setup --southbound
```

Follow [domain tunnel provisioning](domain_tunnel_provisioning.md) and the
[provisioning deployment guide](dashboard_provisioning_deployment.md) for
enrollment, access and migration of existing state. Installing the adapter does
not enroll routers or enable provisioning.

## Update and use the CLI

Stop a locally running application before updating its checkout:

```bash
git pull
./ninja-pce
```

The launcher updates the installed application when its packaged source changes.
It reuses dependencies when package metadata and dependency constraints are
unchanged; changes to either may require Internet access. Your workspace and state remain in
`.ninja-pce/`. Keep backups of live configuration and state when upgrading a
deployment; the [deployment guide](deployment.md) covers managed release changes.

All existing commands are available through the same launcher:

```bash
./ninja-pce --help
./ninja-pce --version
./ninja-pce compute \
  --topology examples/topologies/simple_multiarea.yml \
  --demand examples/demands/pe1_to_pe3_50m.yml \
  --output-dir examples/outputs \
  --show-candidates
```

This computation uses the supplied files and writes a local plan. The
[user guide](user_guide.md) explains live operation, while the
[input schema](input_schema.md) documents offline planning inputs.
