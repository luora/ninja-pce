# Deploy Ninja-PCE

This procedure installs the reusable controller application on an Ubuntu host.
One versioned configuration starts the BGP-LS collector, generic domain-policy
PCEP listener and read-only dashboard under one supervisor. GoBGP supplies the
BGP-LS RIB as a separate service. Qualified SSH tunnel provisioning and HTTPS
authentication remain separate, explicitly configured services.

The [user guide](user_guide.md) covers daily operation and router-owned tunnels.
The [topology-domain runbook](topology_domain_delegation.md) defines the trust
policy and supported PCEP profile.

## Requirements and layout

The clean-host procedure targets Ubuntu 24.04 with systemd and Python 3.12.
The Python package requires Python 3.10 or later. Use a host with routed access
to the BGP-LS exporter and intended PCC loopbacks, synchronized time, and a
protected control-network interface. No nested virtualization is required for
the controller process.

| Location | Purpose |
|---|---|
| `/opt/ninja-pce/releases/0.5.0/venv` | Versioned application virtual environment |
| `/etc/ninja-pce/controller.json` | Controller configuration |
| `/etc/ninja-pce/domain-policy.json` | Reviewed topology-domain policy |
| `/var/lib/ninja-pce/live` | Current collector and PCEP observations |
| `/var/lib/ninja-pce/state` | Persistent ownership journals and reservation holds |
| `/var/lib/ninja-pce/run` | Supervisor PID records and component locks |
| `ninja-controller.service` | Supervisor for collector, PCEP and dashboard |

The installer uses the dedicated unprivileged `ninja-pce` account. The PCEP port
4189 and loopback dashboard port 8088 do not require root. GoBGP's TCP/179 bind
and any external firewall or proxy changes belong to their own deployment.

Live observations are replaceable evidence; ownership journals and reservation
holds are durable authority. Keep policy files outside mutable state. The
application does not contain a built-in BGP speaker, PCEPS/TLS endpoint or
dashboard authentication provider.

## Prepare the host and release artifacts

On the controller:

```bash
sudo apt-get update
sudo apt-get install -y python3-venv python3-pip ca-certificates
python3 --version
timedatectl status
ip -br address
ip -4 route
```

Retain the host's working Internet/default-route configuration. Add control
network addressing and routes through the host's normal network configuration
process. Confirm source and return routing before changing transport access
rules.

Use the wheel and deployment files from the same reviewed release. Build and
download dependencies on a compatible trusted build host if the controller has
no Internet access. A wheelhouse makes the installation independent of package
index availability; retain its exact wheel files and checksums with the release.
The application's dependency ranges do not by themselves form a fully locked
set. The deployment bundle uses the reviewed
[core constraints](../deploy/constraints.txt); retain the actual wheelhouse and
manifest as the installation record. Build/download dependencies for the
controller's Python version and platform.

On a build host with the reviewed source checkout, build the distribution and
download its runtime dependencies explicitly:

```bash
python3 -m venv .build-venv
.build-venv/bin/python -m pip install build setuptools wheel
.build-venv/bin/python tools/build_distribution.py \
  --output-dir dist --download-dependencies
```

The build emits a wheel, source distribution, wheelhouse, checksums and release
manifest. The dependency download requires Internet access; building without
`--download-dependencies` does not request that download. If you already have
the reviewed wheel and need only the runtime wheelhouse:

```bash
python3 -m pip download --only-binary=:all: -c deploy/constraints.txt \
  --dest dist/wheelhouse \
  dist/ninja_pce-0.5.0-py3-none-any.whl
```

Inspect the release version and supplied artifacts before installation. Do not
mix deployment scripts from one revision with a wheel from another revision.
The build output must be absent or empty; select a new directory for another
build. The loose bundle contains the installer, generic examples and these
guides. Extract the accompanying source archive when following historical lab
runbooks that refer to additional repository fixtures or router candidates.

Set the bundle path according to where you are running the installation steps:

```bash
# In the source checkout after the build above:
ninja_release_dir="$PWD/dist"
```

If you transferred the generated bundle and are already inside it, use this
instead:

```bash
ninja_release_dir="$PWD"
```

Verify the generated artifact checksums from that directory:

```bash
cd "$ninja_release_dir"
sha256sum --check SHA256SUMS
```

## Prepare BGP-LS ingestion

The supported adapter reads the GoBGP 3.37.0 API using grpcurl 1.9.3. The
read-only protobuf schema is packaged with the application. Install and manage
the external speaker and grpcurl separately; see the
[pinned tool procedure](../gns3/controller/gobgp/README.md#install-pinned-tools-on-ubuntu-x86-64).
Use the matching official architecture assets and retain their integrity checks.
The supplied [generic speaker example](../deploy/examples/gobgpd.toml) uses the
same invented addresses as the controller example below; replace them together.

Configure GoBGP to receive only the intended BGP-LS address family from trusted
exporters. Bind the BGP listener to the control interface and the management
gRPC API to loopback, such as `127.0.0.1:50051`. The API has write methods even
though Ninja-PCE's collector uses only reads; do not expose that API as a public
unauthenticated endpoint.

The exporter must establish BGP-LS and send End-of-RIB. Confirm the speaker's
neighbor and LS RIB before activating the controller:

```bash
gobgp -u 127.0.0.1 neighbor
gobgp -u 127.0.0.1 global rib -a ls
```

The stock GoBGP 3.37.0 node-key behavior loses distinct area advertisements in
the qualified OSPF multi-area profile. That profile requires the
[documented identity correction](../gns3/controller/gobgp/patches/README.md),
its reviewed binary/provenance, a reconstructed RIB and an explicit ABR identity
policy. Installing the Python application does not patch or replace GoBGP.

## Configure the application

Use the supplied [controller](../deploy/examples/controller.json) and
[domain-policy](../deploy/examples/domain-policy.json) examples as templates, replacing
all example addresses and domain identities. The following is a minimal
controller configuration for an invented private network:

```json
{
  "version": 1,
  "files": {
    "bgpls_raw": "/var/lib/ninja-pce/live/bgpls-raw.json",
    "bgpls_status": "/var/lib/ninja-pce/live/bgpls-status.json",
    "topology": "/var/lib/ninja-pce/live/topology.json",
    "pcep_capture": "/var/lib/ninja-pce/live/pcep.json"
  },
  "state_dir": "/var/lib/ninja-pce/state",
  "runtime_dir": "/var/lib/ninja-pce/run",
  "bgpls": {
    "endpoint": "127.0.0.1:50051",
    "required_peers": ["10.201.0.1"]
  },
  "pcep": {
    "listen_host": "10.201.0.10",
    "domain_policy": "/etc/ninja-pce/domain-policy.json"
  },
  "dashboard": {
    "listen_host": "127.0.0.1",
    "port": 8088
  }
}
```

The matching example domain policy is:

```json
{
  "version": 1,
  "domain": {
    "protocol_id": 3,
    "instance_id": 0,
    "autonomous_system": 65000,
    "bgpls_identifier": 0
  },
  "pcc_prefixes": ["10.200.0.0/16"],
  "destination_prefixes": ["10.200.0.0/16"],
  "max_bandwidth_bps": 100000000,
  "max_live_lsps": 32
}
```

The four domain fields must match actual binary BGP-LS descriptors. Protocol
ID `3` selects OSPFv2; it is not the OSPF area number. A prefix alone does not
authenticate a PCC or make an absent node eligible. `max_bandwidth_bps` is a
per-LSP policy cap, while actual path feasibility uses current advertised TE
resources and the routers' RSVP admission.

For multiple OSPF areas, set `bgpls.ospf_multi_area_policy` to the absolute path
of the independently reviewed identity policy. The wrapper uses that same
policy for its collector, PCEP computation and dashboard. An optional
`bgpls.node_map` supplies readable node labels; labels grant no authority.
Install these additional policy/label files separately with service-readable
permissions and preserve them in backups.

The examples use absolute runtime paths, as the packaged installer requires.
Additional static inputs must be outside `/home`, `/root`, `/tmp` and `/var/tmp`
because the supplied unit uses `ProtectHome` and `PrivateTmp`. The lower-level
application also accepts relative paths,
resolved against the controller JSON file's directory, for interactive/custom
deployments.

Unknown fields, malformed values, overlapping input/output paths and invalid
policy documents fail application validation. Live observation files, shared
ownership state and supervisor runtime files have separate paths; static policy,
map and schema inputs stay outside the mutable directories. Direct symbolic-link
configurations and static references are rejected.

The collector, computation and dashboard use the same freshness bound, at most
30 seconds. The application configuration digest also binds the semantic
domain/ABR policies, node map and protobuf content. Editing those inputs requires
a supervised restart; `status` reports a configuration mismatch until the running
processes use the new configuration. New routers and tunnel IDs within unchanged
scope do not require controller configuration changes.

## Render, review and install

With `ninja_release_dir` set above, render the installation bundle without
changing the host. This works from either the source checkout or the transferred
bundle:

```bash
python3 "$ninja_release_dir/deploy/install.py" \
  --wheel "$ninja_release_dir/ninja_pce-0.5.0-py3-none-any.whl" \
  --config /path/to/controller.json \
  --domain-policy /path/to/domain-policy.json \
  --output-dir build/deployment
```

Inspect the rendered `controller.json`, `domain-policy.json`,
`ninja-controller.service` and `deployment-manifest.json`. Check
the control address, exporter, policy scope, state paths and service account.
Rendering checks the deployment document shape, paths and artifact metadata; it
does not run the complete application/policy validator. The explicit installation
below installs dependencies and validates the configuration with its supplied
domain policy in an isolated temporary environment before writing persistent
deployment files. Prepare the dependency wheelhouse before applying the bundle.

```bash
sudo python3 "$ninja_release_dir/deploy/install.py" \
  --wheel "$ninja_release_dir/ninja_pce-0.5.0-py3-none-any.whl" \
  --wheelhouse "$ninja_release_dir/wheelhouse" \
  --config /path/to/controller.json \
  --domain-policy /path/to/domain-policy.json \
  --output-dir build/deployment \
  --install
```

Installation creates the missing account/directories, installs the supplied
wheel and dependencies into the versioned virtual environment, writes the
reviewed configuration and service unit, and reloads systemd. It does not start
or enable the service, configure routers, change network addressing, open a
firewall, install GoBGP or activate SSH provisioning.

Existing configuration/unit files must match the reviewed content; a different
existing file is refused. Existing release directories are never overwritten.
The installer does not delete or reinitialize journals. Review and reconcile an
existing installation before selecting its paths. A rerun for an already
installed release is refused even when its version matches.

## Validate and activate

Check the installed policy and service permissions, then validate and review
the planned commands as the service account:

```bash
sudo -u ninja-pce /opt/ninja-pce/releases/0.5.0/venv/bin/ninja-pce controller validate \
  --config /etc/ninja-pce/controller.json
sudo -u ninja-pce /opt/ninja-pce/releases/0.5.0/venv/bin/ninja-pce controller plan \
  --config /etc/ninja-pce/controller.json
systemctl cat ninja-controller
```

Validation checks configuration and policy; it does not prove that GoBGP is
reachable or that a PCC and path are ready. The expected control-network
address must already exist on the host. Restrict inbound TCP/4189 to intended
PCC source ranges through that interface. Allow BGP TCP/179 only for the actual
exporter and keep gRPC/dashboard loopback listeners private.

When the network and external GoBGP service are ready:

```bash
sudo systemctl enable --now ninja-controller
systemctl status ninja-controller --no-pager
journalctl -u ninja-controller --no-pager -n 80
sudo -u ninja-pce /opt/ninja-pce/releases/0.5.0/venv/bin/ninja-pce controller status \
  --config /etc/ninja-pce/controller.json
curl --fail http://127.0.0.1:8088/api/controller
```

Confirm a fresh consistent BGP-LS raw/status/topology set, the intended domain
nodes and directional TE links, and the expected PCEP listen address. Configure
routers' global PCC peers once, then confirm each session synchronizes before
trying a tunnel. Follow the [user guide](user_guide.md#configure-a-router-as-a-pcc-once).

The supervisor owns its component processes. Do not run the old standalone
collector/PCEP/dashboard units or another foreground instance against the same
ports and files. If any selected child exits unexpectedly, the supervisor stops
and reaps the others, then exits with a failure status; the installed systemd
unit restarts it after five seconds. An operator stop shuts down the children
gracefully, with a bounded wait before forced termination. Inspect the service
logs and recorded source health if the supervisor repeatedly restarts.

## Dashboard access and optional provisioning

For private read-only access, forward the loopback dashboard over an existing
SSH connection from a workstation:

```bash
ssh -L 8088:127.0.0.1:8088 <HOST_ACCOUNT>@<CONTROLLER_ADDRESS>
```

Open <http://127.0.0.1:8088> on that workstation. For shared access, use an HTTPS
reverse proxy with authentication; the dashboard HTTP server does not supply
either. The [dashboard guide](dashboard.md) describes the proxy boundary.

To enable dashboard tunnel creation, independently configure and qualify the
SSH transport enrollment, pinned router host keys, credential references,
principal policy, permissioned Unix socket and authenticated HTTPS origin.
The opt-in [domain provisioning profile](domain_tunnel_provisioning.md) selects
every eligible router from the fresh trusted domain, including transit routers,
and allows every authenticated proxy principal to create within deployment
policy. Its explicit limits support concurrent independent operations without
router, username or tunnel-number allowlists. Transport readiness remains
separate from topology eligibility.

Use [configuration provisioning deployment](dashboard_provisioning_deployment.md)
for setup and migration of existing state. The application's installation alone
does not change the separate backend's policy or qualification gate, and native
RSVP PCInitiate creation remains unqualified.

## Migrate an existing controller

Use a maintenance window for the listener/session transition. Before stopping
anything, record the current component commands, service identities, addresses,
source hashes, observation paths and all ownership journals. Back up the
configuration and entire state set together.

Create the application configuration using the existing absolute file paths.
Preserve exact provisioning-state, legacy recovery and managed-recovery settings
and their original journals. Keep any approved environment controls. Select the
existing service account and permissions deliberately; the clean-host installer
is not an account/journal migration tool.

Plan and validate the full replacement command before stopping the old
collector, PCEP and dashboard processes. Disable their autostart only when the
new service is ready to own those ports/files. Keep external GoBGP, the SSH
provisioning backend, authentication proxy, credential files and ownership state
unchanged unless their separate migration has been reviewed.

After activation, require fresh exporter health, synchronized PCC sessions,
matching active LSP identities/EROs and unchanged operation/hold history. Verify
the dashboard through its authenticated URL and independently verify the test
tunnels' RSVP and forwarding. A healthy process or a reconnect alone is not a
complete migration check.

## Backup, update and rollback

Keep versioned copies of the installed wheel/wheelhouse, configuration, domain
and ABR policies, node map, external GoBGP configuration/binary provenance,
service units and the entire persistent state directory. Back up any separate
provisioning inventory, private credentials, host-key file and proxy material
through the deployment's secret-storage process.

For a consistent backup, stop every process that can mutate the shared
provisioning/reservation/recovery state, then copy the whole state set together
with permissions and checksums. Start the unchanged services again after the
backup. Do not copy only a creation journal while omitting its reservation
ledger or managed recovery history.

Before an update, review release notes and schema compatibility, build a new
immutable virtual environment from the reviewed wheelhouse, and validate its
configuration. Preserve the previous environment, unit and complete state
backup. The clean-host installer does not overwrite an old unit to perform an
upgrade: a new version changes its executable path and requires explicit review
and replacement of the unit. Render and compare the new files, back up the old
unit and deliberately install the reviewed replacement. Switch the service only
after that review, then verify fresh topology,
synchronization, ownership and forwarding as in migration.

To roll back code, stop the new supervisor before restoring the previous
reviewed unit/environment and restart only one set of components. Restore
matching configuration and state only after reconciling router changes made
since the backup. A code rollback does not undo a PCUpd or SSH write. Never
replay uncertain operations, reset attempt counters or discard retained holds
to make an older version start.

If state schema/policy changes prevent startup, retain the failed files and
diagnostic evidence. Compare the configuration with the last reviewed release
and reconcile the ownership mismatch explicitly rather than creating an empty
state directory.

## Qualification boundary

This packaging consolidates the existing bounded implementation; it does not
certify every network size or vendor. Current live evidence covers the
[documented IOS XE 16.9.7 domain-discovery lab](topology_domain_validation.md).
The domain membership checks assume trusted BGP-LS exporters and protected
PCEP transport. They are authorization inputs, not cryptographic authentication.
Production deployment needs a separately designed transport/authentication
boundary, qualified device profiles and operational ownership/recovery rules.
