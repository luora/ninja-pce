# Domain tunnel provisioning validation

Prepared 2026-10-15 (Africa/Nairobi) for `feat/domain-tunnel-provisioning`.
The domain-wide, multi-user provisioning implementation is deployed and
qualified within the live scope recorded below, with the offline suite passed.
The clean 0.6.0 release was built from source `31c998d`, with
`source_dirty=false`; fresh wheel-install smoke checks passed. Live rollout
and qualification evidence are recorded separately from the offline checkpoint.
Later documentation/comment commits record these outcomes; the deployed
release wheel's source revision remains `31c998d`.

See [domain tunnel provisioning](domain_tunnel_provisioning.md) for configuration
and policy behavior, and
[the provisioning deployment guide](dashboard_provisioning_deployment.md) for
the explicit journal migration procedure.

## Implemented behavior

- Fresh trusted topology determines selectable headends and destinations,
  including transit routers. Transport enrollment and PCC readiness are
  reported separately from domain eligibility.
- Authenticated proxy principals can create within deployment policy without
  an ordinary username allowlist. Unix peer identity, origin, sessions, CSRF,
  ownership, principal-scoped visibility, and audit checks remain enforced.
- Concurrent operations share bounded admission and directional capacity
  accounting. Tunnel IDs belong to each headend; generated ownership remains
  distinct. Delivery is serialized per management endpoint while independent
  endpoints can proceed concurrently.
- Principal-scoped idempotency recovers an existing exact operation before
  applying a later domain or transport policy. Unknown delivery retains its
  resources and is never automatically resent.
- The explicit migration preserves original operation authority, owners,
  reservations, recovery bindings, uncertainty, and attempt history. New
  operations retain their own policy revision. The browser retains separate
  recent operation histories for each authenticated principal.

The qualified transport remains Cisco IOS XE 16.9.7 configuration over SSH,
followed by initial PCEP control and independent headend RSVP verification.
The implementation does not qualify native RSVP creation through PCInitiate,
NETCONF, or another vendor platform.

## Offline verification

| Check | Result | Scope |
| --- | --- | --- |
| Complete regression suite | 2,038 passed in 206.70 seconds; 88% coverage | Implementation and existing application, protocol, provisioning, and browser behavior at that checkpoint |
| Earlier focused regression | 349 passed in 13.15 seconds | Migration and concurrency checks before the final complete-suite checkpoint |
| Ruff | Passed | Source and test static checks |
| mypy | Passed | Application source type checks |
| Clean release | 0.6.0 from `31c998d`; `source_dirty=false` | Release artifact provenance |
| Wheel-install smoke checks | Passed | Fresh isolated installation of the release wheel |

The earlier focused run is separate from the final complete-suite checkpoint.
Offline coverage includes distinct authenticated principals, transit headends,
transport refusal, changed host-key pins, scoped retries, policy revisions,
reservation ownership, uncertain restart, migration integrity, and concurrent
allocation/admission across initialized controller instances. Endpoint
serialization and independent-headend parallelism are exercised with synthetic
delivery workers. Browser tests exercise principal switching, multiple tracked
operations, unavailable headends, exact reviewed-plan approval, and recovery
after an unknown response.

These tests use synthetic topology, transport, and delivery evidence. They do
not establish live RSVP signaling or forwarding outcomes for the new milestone.

## Initial read-only live baseline: historical preservation evidence

| Observation | Result |
| --- | --- |
| Topology | Eight routers and 22 directed TE links |
| PCC state | Eight synchronized sessions |
| LSP state | Six ACTIVE LSPs |
| Reservation ledger | One original held managed operation |
| Recovery history | Two failure attempts and nine startup attempts |
| Existing creation journal | Three operations: one VERIFIED and two ROLLED_BACK |

These observations were captured before rollout and provide a preservation
baseline. They do not establish current enrollment readiness or completed
live migration, new creation, or restart qualification. Previously recorded
RSVP and forwarding checks remain evidence for the historical deployment.

## Live activation preservation checkpoint

The installed 0.6.0 modules were verified during activation. Eight PCC sessions
were synchronized and the six original LSPs remained ACTIVE. All three original
creation records retained their authority; reservation-ledger bytes, the exact
managed recovery binding, and failure/startup history remained unchanged. The
recovery counters stayed at two failure attempts and nine startup attempts.
Unrelated service process IDs were unchanged, and activation sent no PCUpd.

An inherited `PYTHONPATH` initially selected an older source checkout. Reviewed
service overrides now use the installed interpreter with `-I` and
`UnsetEnvironment=PYTHONPATH PYTHONHOME`; installed-module version 0.6.0 was
verified after correction. The
[deployment guide](deployment.md#validate-and-activate) describes the isolation
check.

This activation check establishes preservation during rollout. The creation
checks below provide separate signaling and forwarding evidence.

## Concurrent authenticated creation checkpoint

Two separate authenticated HTTPS principals concurrently submitted distinct
reviewed 1 Mbps plans. A transit headend used `P1 → P3 → P4 → PE4`; the other
headend used `PE4 → P4 → P3 → PE1`. Both allocated Tunnel1000 in their separate
headend namespaces and reached `VERIFIED`. Each operation retained independent
headend RSVP and correlated PCEP-control evidence, with identity, destination,
bandwidth, ERO and signaling checks complete.

| Check | Recorded outcome |
| --- | --- |
| Domain selector | All eight managed headends exposed |
| Transport readiness | All eight transports SSH-authenticated and version-qualified, with independently pinned identities |
| Principal-scoped same-key retries | Returned the original operation identities |
| Cross-principal operation reads | 404 |
| Anonymous HTTPS HEAD | 401 with `WWW-Authenticate` |
| Over-policy request | 400 |
| Bad CSRF token | 403 |
| Wrong Origin | 403 |
| Headend signaling | Both new tunnels Admin/Oper UP and RSVP connected |
| Forwarding | Both headends returned MPLS echo 5/5; traces reached their respective destinations |
| Transit reservation evidence | P3 and P4 reported matching 1 Mbps RSVP sender/reservation pairs for both services |
| Existing managed tunnel | Original PE1 managed tunnel still returned MPLS echo 5/5 |
| Creation journal | Five operations: three VERIFIED and two ROLLED_BACK |
| Reservation ledger | Three holds, including the original managed hold |
| LSP state after creation | Eight ACTIVE LSPs |

An acknowledgement, correlated PCEP application, RSVP verification and
forwarding were checked as distinct outcomes. These results qualify the two
tested creation paths; exposing eight headends does not establish creation or
forwarding qualification for every possible headend/path combination.

## Transport identity refusal

Strict SSH host-key verification refused a changed host identity before any CLI
command. Missing host-key enrollment separately failed the readiness check.
The active configuration, inventory, pins, journal, ledger and state-file hashes
and filenames remained unchanged through these checks. The temporary test
service was removed afterward.

## Restart and policy revision preservation

The service restart preserved all eight paths and delegation flags, all five
operation records and the bytes of all three holds. It sent no PCUpd and created
no archived update. The original managed recovery binding, two failure attempts,
nine startup attempts, three legacy recovery attempts and unrelated service
process IDs were unchanged.

For restart qualification, the only policy change reduced `max_outstanding`
from 16 to 12,
creating a third retained policy revision. Existing operations kept their
original revisions and authority. After restart, both authenticated principals'
same-key retries recovered the identical VERIFIED operations and cross-principal
reads again returned 404.

Independent console checks were repeated after restart: both new headends
returned MPLS echo 5/5 with traces reaching their destinations, the original
managed tunnel returned MPLS echo 5/5, and P3/P4 retained the exact 1 Mbps RSVP
sender/reservation pairs.

## Retained state and qualification scope

The two 1 Mbps qualification tunnels and their holds are retained. Together
with the original managed hold, the ledger contains three holds; the journal
contains five operations, three VERIFIED and two ROLLED_BACK. The live tests
used authenticated HTTPS API requests and inspected the served dashboard asset
for principal histories and headend readiness. Browser behavior was exercised
by the offline regression suite; a live interactive browser session was not
qualified. Cold controller VM and router reboots were not qualified. The SSH
profile changes running configuration and does not save startup configuration.

Managed failure/startup recovery remains the exact original-operation profile;
the two new tunnels were not automatically enrolled into managed recovery.
The tested transport is Cisco IOS XE 16.9.7 over SSH. Native RSVP creation via
PCInitiate, NETCONF and additional vendor interoperability remain unqualified.

The final read-only audit passed: controller status was healthy with eight
nodes, 22 directed links, eight fresh PCCs, eight ACTIVE LSPs and three
reservations. Five operation records and three policy revisions were retained;
the current `max_outstanding` is 12, with three retained held services consuming
three of those slots. Both controller and provisioning
services were active and enabled. Exact operation/hold bytes, paths and recovery
history remained unchanged after retries and cleanup; unrelated process IDs
were unchanged.

The two temporary HTTPS qualification accounts and transport test units were
removed. The original authentication file bytes/hash were restored exactly,
and anonymous HTTPS again returned 401. The two qualification tunnels remain in
running configuration with their reservation holds. All qualification checks
described in this record are complete within the stated scope.
