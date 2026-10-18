# Listener ownership and managed startup

Managed services check local dependencies and listener availability before
starting protocol work. A blocked protocol component leaves the selected
dashboard available. Its readiness banner, /api/controller and controller
status include structured startup_findings with a code, cause and remediation.
The supervisor preserves an existing component owner's runtime record when
its lock is held and records the new invocation's fault separately. It never
stops the other owner or clears captures, provisioning or recovery journals.

## Inspect a listener

Use the installed CLI to inspect TCP listener metadata without binding a
socket, contacting a service, or changing a process:

~~~bash
ninja-pce runtime inspect-listener \
  --address 127.0.0.1 --port 50051 --service ninja-gobgpd.service
~~~

The JSON includes address, port, expected_service, owners (PID, executable and
systemd service where visible), owner_visibility and operator guidance.

| Code | Meaning | Exit |
|---|---|---|
| PORT_AVAILABLE | No overlapping listener was found in the inspected namespace | 0 |
| PORT_OWNED_BY_SERVICE | Every visible owner matches the exact expected service, with complete attribution | 0 |
| PORT_CONFLICT | An overlapping listener exists; ownership may be incomplete | 1 |
| PORT_INSPECTION_UNAVAILABLE | The listener tables could not establish availability | 1 |

Inspection uses bounded Linux /proc reads in the current network and PID
namespaces. Restricted permissions, exited processes or inspection bounds can
limit attribution. It checks process identities again to reduce stale PID
attribution. It does not read process arguments or environment variables.
IPv4, IPv6 and wildcard overlaps are conservative because /proc does not
expose each socket's IPV6_V6ONLY setting. An inspection snapshot is evidence
for diagnosis, not a reservation or proof of controller write authority.

Startup separately probes bind eligibility with a temporary socket that never
listens or connects. It closes that socket before launching the real listener;
the real bind still handles a concurrent claimant. Even an expected service's
existing listener blocks a second launch. Address/port checks do not replace
the separately reviewed multi-controller ownership and fencing requirements.

## Managed GoBGP

The verified offline installer renders ninja-gobgpd.service with the installed
Python launcher as its main process:

~~~bash
/opt/ninja-pce/releases/0.7.0/venv/bin/python -I -m ninja_pce.cli runtime gobgp \
  --binary /opt/ninja-pce/releases/0.7.0/bin/gobgpd \
  --config /etc/ninja-pce/gobgpd.toml \
  --api-address 127.0.0.1 --api-port 50051 --service ninja-gobgpd.service
~~~

The launcher requires Python 3.11 or newer; the offline target uses 3.12.
It checks the API and configured BGP ports before native execution, runs the
daemon's strict configuration dry-run with a 30-second timeout, verifies that
the TOML bytes have not changed, then checks the ports again before starting
one daemon. Profiling and metrics listeners are disabled during both phases.
It forwards SIGINT/SIGTERM to that child, allows ten seconds for termination,
then kills and reaps only its own child if necessary. It performs no retries.

Enabled BGP requires an explicit numeric global.config.local-address-list and
a port from 1 to 65535. port = -1 disables the BGP listener. Hostnames, scoped
IPv6 addresses and overlapping configured endpoints are rejected. In
particular, an IPv6 wildcard and IPv4 address sharing a port are rejected
conservatively; use separate explicit addresses. The bootstrap remains
peer-free with BGP disabled and the API bound to loopback.

Native validation or unexpected-exit findings retain the last 16 KiB of each
output stream, with truncation flags. Values in TOML password, secret, token
and private-key fields are redacted, including JSON-escaped forms. This is a
bounded error tail rather than a continuous native log stream. Runtime
validation uses the already installed binary; signature and provenance
authentication remain the installer's responsibility.

## Exit codes and restart policy

| Outcome | Managed exit and handling |
|---|---|
| Invalid configuration, missing executable, permission/address fault or occupied local port | 78, with a structured original cause; systemd does not retry |
| Ordinary unexpected runtime exit | 1; systemd retries within its start limit |
| Unavailable BGP-LS RPC/source | Collector retains its existing polling behavior; --once returns 1 |
| Operator stop | 0 after owned children are stopped and reaped |

controller run uses this permanent-error contract. Standalone dashboard,
bgpls-collect and pcep-listen opt into configuration/dependency classification
with --managed-startup; generated controller commands and Python unit examples
include it. Ordinary standalone validation keeps its previous exit status 1.
An actual dashboard or PCEP bind conflict reports 78 directly. PCEP cleanup
does not replace a capture if the listener never acquired its socket.

Managed units use Restart=on-failure, RestartSec=5,
StartLimitIntervalSec=60, StartLimitBurst=3 and RestartPreventExitStatus=78.
The launcher is ExecStart rather than ExecStartPre so systemd can apply the
exit-78 exemption to its main process. The historical raw GoBGP example has
the same start limit but no Python exit-code exemption.

With a dashboard selected, a protocol startup failure stops the owned protocol
group and leaves management running without automatic protocol restarts.
Inspect its finding, repair the cause and deliberately restart the controller.
A dashboard failure or a headless selection exits the supervisor; permanent
status 78 is preserved. Existing files and foreign processes are preserved.

After fixing a failed managed GoBGP service, an operator can reset its start
limit and restart it through normal service controls:

~~~bash
journalctl -u ninja-gobgpd --no-pager -n 80
sudo systemctl reset-failed ninja-gobgpd
sudo systemctl restart ninja-gobgpd
~~~

Do not kill a PID merely because a snapshot names it. Confirm the intended
service and reconfigure or stop a conflicting instance through its own normal
controls. Repair its ownership arrangement before launching another instance.

## Validation scope

Regression tests use synthetic /proc trees, real disposable localhost
listeners, component locks and child processes. They cover owner uncertainty,
native validation and log tails, signals, exit classifications, retained GUI
availability and byte-preserved captures and owner records. Unit rendering is
checked against the restart contract and the host systemd parser. Offline
package smoke checks exercise the installed CLI outside the source checkout.
Live router behavior and fresh Ubuntu VM service activation remain separate
release acceptance work.
