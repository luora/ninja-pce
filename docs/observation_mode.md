# Observation mode and readiness

New application workspaces and fresh deployment examples explicitly set
"operating_mode": "observe". Start the application and open the loopback
dashboard before configuring a network source:

~~~bash
./ninja-pce init
./ninja-pce start
~~~

The dashboard starts first. Missing GoBGP connectivity or grpcurl leaves it
available with an explanation. Configure the BGP-LS source and trusted router
identity policy before expecting a usable topology.

## Mode boundaries

| Configuration | Started components | PCEP authority |
|---|---|---|
| operating_mode: observe | BGP-LS collector and enabled dashboard | No PCEP process or listener |
| operating_mode: pcep-control | BGP-LS collector, PCEP and enabled dashboard | Existing validated PCEP policy |
| operating_mode omitted | Same behavior as the previous version | Existing validated PCEP policy |

Existing version-1 configurations keep their canonical configuration and
digest when the mode is omitted. They are not rewritten or silently migrated.
The mode selects services; it does not grant authority to provision a tunnel.
Passive monitoring and provisioning modes are not implemented and are rejected
rather than treated as active control.

Observe mode rejects --component pcep and mixed selections containing PCEP.
It accepts a configuration without a pcep section or domain delegation policy.
If an inactive domain-policy path is supplied, the controller still protects
it against overlap with collector outputs, without reading its contents.
Observation configuration must omit provisioning_state, failover and
managed_recovery settings; use a separate observer configuration and retain
the active controller's journals.

The version-1 files section still includes a pcep_capture path. It is inert in
observe mode: neither the listener nor the dashboard reads or writes that
capture. Reservation ledgers are displayed read-only if present.

An example observer configuration is:

~~~json
{
  "version": 1,
  "operating_mode": "observe",
  "files": {
    "bgpls_raw": "../data/live/bgpls-raw.json",
    "bgpls_status": "../data/live/bgpls-status.json",
    "topology": "../data/live/topology.json",
    "pcep_capture": "../data/live/pcep.json"
  },
  "state_dir": "../data/state",
  "runtime_dir": "../data/run",
  "bgpls": {
    "endpoint": "127.0.0.1:50051",
    "required_peers": ["192.0.2.20"]
  },
  "dashboard": {
    "listen_host": "127.0.0.1",
    "port": 8088
  }
}
~~~

Replace the documentation peer address with the intended exporter. Multi-area
topology still requires the independently reviewed ospf_multi_area_policy;
matching loopbacks alone never authorize ABR projection.

## Liveness and readiness

/healthz remains a process-liveness endpoint. It returns HTTP 200 while the
dashboard can respond, including when the managed network is unavailable.

/readyz evaluates the current validated observations and returns HTTP 200
only when ready; otherwise it returns HTTP 503. /api/controller includes the
same readiness object plus operating_mode and, for a managed dashboard,
component process records. The CLI exposes it through:

~~~bash
ninja-pce controller status --config /path/to/controller.json
~~~

The CLI retains healthy/degraded status for compatibility and returns zero
only when readiness succeeds. An observer with a fresh complete graph and
zero PCC sessions reports READY_OBSERVING.

| Readiness state | Meaning |
|---|---|
| READY_OBSERVING | Valid current topology; PCEP intentionally disabled |
| READY_PCEP | Valid topology and a fresh synchronized PCC under existing policy |
| WAITING_FOR_BGPLS | A valid session, End-of-RIB and consistent topology are not yet available |
| TOPOLOGY_STALE | Retained BGP-LS/topology evidence exceeded its freshness bound |
| TOPOLOGY_INCOMPLETE | Invalid, empty, inconsistent or incomplete Node/Link evidence |
| BGPLS_UNAVAILABLE | Managed collector is stopped or failed |
| MANAGEMENT_UNAVAILABLE | Configured dashboard process is stopped or unobserved |
| PCEP_UNAVAILABLE | Required PCEP process or current capture is unavailable |
| WAITING_FOR_PCC | Current listener has no fresh synchronized PCC |
| CONFIGURATION_MISMATCH | Runtime or dashboard configuration differs from the expected revision |

Readiness reuses the binary BGP-LS parser: configured exporters must match the
capture, both peer observations must satisfy session and End-of-RIB checks,
and raw/status/topology files must agree. Excluded Node/Link advertisements
block readiness. Optional IPv4/IPv6 Prefix NLRI exclusions retain the existing
computation policy and do not alone make the TE graph incomplete.

When the controller starts the dashboard, it supplies the configured exporter
list and configuration digest. That dashboard checks component process
identities and rejects changed configuration. A separately launched dashboard
can use --operating-mode observe and repeated --bgpls-peer options; without
managed configuration context its readiness scope is observations and does
not establish process supervision.

READY_PCEP establishes session readiness, not controller ownership or verified
forwarding. control_authority_verified and forwarding_verified remain false.
Cross-controller ownership fencing and authenticated provisioning qualification
are separate work.

The browser displays operating mode, readiness and intentionally disabled
PCEP. When polling fails or is paused, it stops presenting the retained
snapshot as current readiness.

## Protocol failures and shutdown

With the dashboard selected, supervision starts it before protocol processes.
A collector or PCEP process crash, including a protocol executable startup
failure, stops and reaps the protocol group while keeping the dashboard
available. The original fault remains in the component runtime record;
intentional sibling shutdowns are recorded separately. Protocol processes
are not automatically restarted against uncertain state.

Repair the cause, review the retained records and restart the controller when
ready. A dashboard exit is fatal and reaps protocol children. Headless
component selections retain fail-fast behavior. Operator SIGINT/SIGTERM
shuts down and reaps every child with a bounded wait.

Read-only collection can recover from an unavailable source within its normal
polling loop. That is distinct from restarting a crashed protocol process.
No recovery journal or reservation ownership is cleared during these changes.

[Managed startup checks](runtime_startup.md) now report occupied ports, missing
clients and duplicate component ownership as permanent faults. Readiness also
includes startup_findings, rendered in the dashboard banner. Ordinary source
connectivity failures remain recoverable within the collector's polling loop.

## Upgrade order and validation

This change follows the verified offline-bundle PR. Merge that PR first.
Fresh bundle installation now records observation explicitly. Existing
installed configurations and units remain protected from overwrite; migrate
their reviewed intent separately and restart against the resulting revision.

Tests exercise real local HTTP availability after collector exit, spawn
failure and missing grpcurl, absence of a configured PCEP listener, retained
journal bytes, readiness freshness/identity checks, legacy mode behavior,
and browser handling of disconnected snapshots. They use local processes and
captured/synthetic protocol inputs. Independent live router evidence remains
necessary for release acceptance.

Authenticated bootstrap/setup and
provisioning capability integration remain later PRs. The management dashboard
continues to require loopback access or the existing authenticated reverse
proxy.
