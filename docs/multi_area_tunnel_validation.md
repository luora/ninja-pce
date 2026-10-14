# Live inter-area provisioning and recovery — IOS XE 16.9.7

On 2026-10-14 UTC, the eight-router GNS3 lab created **Tunnel1002 from PE1 to
PE4 at 1 Mbps**, verified its MPLS forwarding, and recovered it automatically
after the P4–PE4 link failed. The controller computed both paths from the live
two-area BGP-LS feed. Configuration delivery used the dashboard's exact-plan
operator API and IOS XE SSH adapter; PCEP supplied the initial and replacement
paths. Native PCInitiate remains separately unqualified.

This extends the [topology and computation result](multi_area_lab.md) and the
earlier [single-area configuration trial](dashboard_configuration_validation.md).
It qualifies this image, bandwidth and single managed tunnel profile.

## Profile and initial result

| Property | Observation |
|---|---|
| Platform | CSR1000v IOS XE 16.09.07 / IOS Software 16.9.7 |
| Areas / ABRs | `0.0.0.0`, `0.0.0.1` / P2, P4 |
| TED | Ten Node NLRIs projected to eight physical routers; 22 directed TE links |
| Headend / destination | PE1 `10.255.0.1` → PE4 `10.255.0.8` |
| Managed router object | Tunnel1002 |
| Requested and signaled bandwidth | 1,000,000 bit/s; RSVP `1000 kbps` |
| Priorities / affinity | 7/7; affinity `0x0`, mask `0x0` |
| Initial path / metric | PE1 → P3 → P4 → PE4 / 30 |
| Initial strict ERO | `10.0.13.3`, `10.0.34.4`, `10.0.48.8` |
| Active option / configured fallback | Dynamic PCE option 1 / generated explicit option 10 |

The PCEP listener, dashboard and provisioning backend each explicitly loaded
the same validated OSPF multi-area identity policy. The dashboard displayed
eight routers and 22 links. The original P1 collector remained separate; the
active consumers used the qualified P2 export through the isolated collector.
No router names or addresses were added to the generic controller logic.

The exact reviewed API plan created the configuration once. PE1 reported the
selected dynamic PCE option, working delegation and a path created through
PCUpd. Controller evidence correlated that original update with an ACTIVE
report for the authorized identity, strict ERO, bandwidth and priorities.
Independent RSVP tables from all eight routers placed the initial 1 Mbps
reservation on PE1/P3/P4/PE4. MPLS echo passed **5/5** and MPLS trace followed
the three strict ERO hops.

## Controlled failure and restoration

Only P4 GigabitEthernet5 toward PE4 was temporarily shut. The fresh TED
eventually withdrew both directions of that link, reducing the graph to
eight routers and 20 directed links. After the five-second failure debounce,
the managed profile sent **one `FAILURE_PATH` PCUpd**:

| Property | Replacement |
|---|---|
| Path / TE metric | PE1 → P1 → P2 → P5 → PE4 / 40 |
| Strict ERO | `10.0.12.2`, `10.0.25.5`, `10.0.57.7`, `10.0.78.8` |
| Identity and constraints | Same source, destination and Tunnel1002; 1 Mbps, priorities 7/7 |
| Completion | Correlated ACTIVE PCRpt; pending state cleared; one persisted attempt |

With the link still shut, PE1 showed dynamic PCE option 1 active, RSVP
signaling connected and the replacement strict route. MPLS echo again passed
**5/5**, and trace followed P1/P2/P5/PE4. After restoration, independent
sender/reservation tables confirmed the same 1 Mbps LSP along that route,
including termination at PE4. P3/P4 no longer carried its RSVP reservation.

The test restored P4 GigabitEthernet5 in its cleanup block without saving the
temporary shutdown. Its PE4 OSPF adjacency returned to FULL, and the graph
returned to 22 links. MPLS echo passed **5/5** after restoration. The final
capture retained the replacement route and one update; no automatic healthy
path switchback occurred through the final capture, 434.83 seconds after the
ACTIVE acknowledgment.

The first recorded PCC DOWN report preceded the first captured TED withdrawal
by 38.71 seconds. PCUpd followed that capture by 6.84 seconds; the matching
ACTIVE PCRpt arrived on the wire 0.141 seconds after PCUpd. Failure discovery
included a substantial BGP-LS advertisement/collection delay in this trial.
These control-plane observations establish eventual recovery, not fast reroute,
a forwarding outage measurement or a recovery-time guarantee.

## Ownership, deployment and corrections

The original provisioning operation stayed `VERIFIED`. Recovery added the
replacement's four directed edges to its original three held edges under the
same reservation owner. The ledger retained that seven-edge union at 1 Mbps
per edge; it did not refund the old path or allocate a second owner. This is
conservative controller accounting, distinct from current RSVP reservations.
The managed journal recorded one of its three permitted recovery sends.
Tunnel100's separate journal and existing attempt count were preserved.

Tunnel100's configuration matched its pre-deployment snapshot exactly. Its
50 Mbps reservation remained operational throughout the controlled link trial.
Tunnel1002 was saved to PE1 startup configuration after qualification. The
controller services were active and enabled at the final check.

Qualification initially used a private Unix socket. After passing, the backend
returned to the existing authenticated operator proxy. The API reported the
operation `VERIFIED`; anonymous HTTPS returned 401 and mutation without a
current session/CSRF token returned 403. Authentication, firewall rules and
Nginx configuration were unchanged. The profile still permits at most one
held managed tunnel and caps requests at 1 Mbps; further plans are refused
while this demonstration tunnel remains held.

Two live findings produced regression fixes:

- IOS XE omits the local `path weight` suffix from the selected inter-area PCE
  option. Its otherwise successful initial verification was reconciled using
  fresh read-only router checks and the original acknowledged PCUpd evidence.
  The configuration was not replayed and the reservation was not released.
- On listener restart, IOS first sent a historical synchronization report with
  delegation clear, then a live delegated report. Managed recovery now waits
  for the live report. The zero-send blocked adoption journal was preserved;
  explicit review confirmed unchanged ownership and holds before a new scoped
  journal adopted the path. Synchronization never grants recovery write authority.

## Evidence and limits

Private captures remain under the Git-ignored
`build/live-lab-evidence/inter-area-001/` directory. The controller archive's
SHA-256 is
`41ddb4ed2217a346196df8d7be7fa4903e92ef48763119a13d933fcf68a63222`;
all 102 archived artifact hashes passed verification. Console captures are
retained separately. All 18 retained raw BGP-LS captures exactly reproduced
their saved normalized graphs at their recorded timestamps with the pinned
area policy. The deployed source bundle's SHA-256 is
`603a7355ad9ae2ffc657adadda4f510aeff1b0ea569f6947471c4df4fef99207`,
and its installed source files matched the pinned manifest. Public addresses,
credentials, MAC addresses and live operation/session identifiers are omitted
from this published result.

The complete regression suite passed **1,523 tests**; Ruff and mypy passed.
Tests cover exact ownership, conservative holds before durable send intent,
writer-lock authority checks, synchronization, pending restart without replay,
and the separation of legacy and managed recovery profiles. The
[deployment guide](dashboard_provisioning_deployment.md#recovery-of-one-provisioned-tunnel)
documents the explicit opt-in arguments and reconciliation limits.

The trial did not qualify throughput, customer service steering, multiple
simultaneous managed tunnels, repeated/concurrent failures, reboot recovery
after this final deployment, healthy-path optimization, other vendors,
NETCONF provisioning or inter-AS operation. Autoroute stayed disabled; MPLS
OAM verified the selected LSP rather than customer traffic placement.
