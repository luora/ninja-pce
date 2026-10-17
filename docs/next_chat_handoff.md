# Ninja-PCE next-chat handoff

Testing branch decision, 2026-10-17 (Africa/Nairobi): use **`main`** for
ongoing application testing. The integration includes domain provisioning and
the 0.7.0 local launcher. Continue from updated `main`; retain the distinction
between current source and the qualified live 0.6.0 artifact described below.

Updated 2026-10-16 (Africa/Nairobi). The current source also includes the **0.7.0**
clone-and-run application launcher. Start with `./ninja-pce` for a labelled
loopback-only sample, then `init`, edit the generated network configuration,
`doctor`, and `start` for the existing live controller runtime. See
[getting started](getting_started.md). The launcher owns an ignored isolated
`.ninja-pce/venv` and a separate workspace, preserves existing files, reuses an
unchanged installation offline, and refuses package replacement while a locally
launched application is running. Optional SSH dependencies use
`./ninja-pce setup --southbound`; authenticated provisioning still requires the
separate enrollment/proxy service setup.

The application-packaging regression checkpoint passed **2,074 tests in
199.53 seconds, 89% coverage**; Ruff and mypy passed. Fresh-checkout installation
and offline sample reopens, assets, mutation refusal, signals, duplicate-init
preservation and degraded status were exercised from a checkout containing
spaces and an unrelated working directory. Three later installer parser/path
checks passed in the focused suite (138 focused checks after the 0.7.0 version
update). A real running sample also refused package replacement without changing
its installation record. CI now exercises the launcher on Python
3.10 and 3.12; those remote runs have not been claimed locally.

This packaging work did **not** redeploy or alter the running application. The
live domain provisioning deployment remains the qualified **0.6.0** wheel from
`31c998d`, with the preserved authority and qualification state recorded below.
Do not confuse the new source/package version with that deployed artifact.

Domain deployment checkpoint, 2026-10-15 (Africa/Nairobi). Domain-wide, multi-user dashboard tunnel
provisioning is implemented, deployed and qualified on
`feat/domain-tunnel-provisioning` within the scope below. Concurrent authenticated
live creation, transport refusal, forwarding, service restart, final read-only
audit and temporary-account cleanup passed while preserving the original live
application and durable authority.

## Current milestone: live domain provisioning complete

The new implementation discovers every eligible domain headend, including
transit routers; separates SSH transport enrollment from topology eligibility;
authorizes authenticated proxy principals without an ordinary username list;
and supports bounded concurrent operations, headend-scoped tunnel allocation,
principal-scoped retries, per-operation policy revisions, endpoint-serialized
delivery, and principal-scoped dashboard operation history. An explicit
review/apply migration preserves original authority, held resources, managed
recovery bindings, uncertainty, and attempt histories.

Complete regression at the final offline checkpoint: **2,038 passed in
206.70 seconds, 88% coverage**. Ruff and mypy passed. The clean **0.6.0** release
was built from source `31c998d`, with `source_dirty=false`; fresh wheel-install
smoke checks passed.
Later documentation/comment commits record qualification evidence; the deployed
release wheel's source revision remains `31c998d`.
See [domain provisioning validation](domain_tunnel_provisioning_validation.md)
and [domain provisioning configuration](domain_tunnel_provisioning.md).

The initial read-only live baseline captured before rollout contained eight nodes,
22 directed links, eight synchronized PCCs, six ACTIVE LSPs, one original hold,
two failure attempts, nine startup attempts, and three original creation
operations (one VERIFIED, two ROLLED_BACK). These observations are historical
preservation evidence. The activation, creation, restart and final audit below
record the subsequent live deployment separately.

The subsequent activation preservation check verified installed 0.6.0 modules,
eight synchronized PCCs and six ACTIVE original LSPs. The original three
creation records, reservation-ledger bytes, managed recovery binding and history
were retained; counters stayed at two failure attempts and nine startup
attempts. Unrelated process IDs were unchanged and activation sent no PCUpd.
An inherited `PYTHONPATH` selecting old source was corrected with isolated
installed interpreters and `UnsetEnvironment=PYTHONPATH PYTHONHOME`.

Two authenticated HTTPS principals then concurrently created independent
1 Mbps tunnels from transit headend P1 to PE4 and from PE4 to PE1. Both reached
VERIFIED with independent RSVP/PCEP checks. Same-key retries recovered the
original operations; cross-principal reads returned 404. Anonymous HTTPS,
policy, CSRF and Origin refusal checks passed. Both new headends returned MPLS
echo 5/5 and traces reached their destinations; the original PE1 managed tunnel
also retained MPLS echo 5/5. The creation checkpoint contained five operations
(three VERIFIED, two ROLLED_BACK), three holds and eight ACTIVE LSPs. See the
validation record for exact logical routes and separate evidence scopes.

Strict SSH refused changed host identity before any CLI, and missing pins
failed readiness; active configuration, inventory, pins and state remained
unchanged. The restart preserved all eight paths/delegation flags, all five
operation records, all three hold bytes, the original recovery binding/counters
and unrelated process IDs, with no PCUpd or archived update. The legacy recovery
journal's three attempts also remained unchanged. Reducing the
capacity policy from 16 to 12 retained a third policy revision without rebinding
old operations. Both principals' retries and independent RSVP/forwarding checks
passed again after restart.

The final audit was healthy: eight nodes, 22 directed links, eight fresh PCCs,
eight ACTIVE LSPs, three reservations, five operations (three VERIFIED and two
ROLLED_BACK), and three policy revisions. All eight transports were
SSH-authenticated and version-qualified with pinned identities. Current
`max_outstanding` is 12; three retained held services consume three slots.
Controller and provisioning services were active
and enabled. Temporary HTTPS qualification accounts and transport test units
were removed; original authentication bytes/hash were restored exactly, and
anonymous HTTPS again returned 401. Exact operation/hold bytes, paths, history
and unrelated process IDs remained unchanged after cleanup.

Preserve the completed migration/backups and qualification evidence. The two
new 1 Mbps qualification tunnels remain in running configuration with their
holds. The SSH profile does not save startup configuration. Live checks used
authenticated HTTPS API requests and dashboard-asset inspection; an interactive
browser session, cold controller VM/router reboot and every possible path were
not qualified. Managed recovery remains the exact original-operation profile;
the two new operations were not enrolled automatically. Native
PCInitiate/NETCONF/other vendors remain separate qualification work. No migration
or deployment task remains for this milestone. Keep private access evidence
outside Git.

The remainder retains the original application milestone and starting
requirements as historical context. Its implementation and deployment plan
is complete as recorded above; preserve its live-state guarantees in future
work.

## Historical Git starting point

- Main integration: [PR #2](https://github.com/luora/ninja-pce/pull/2) merged
  at `8380f97` after Python 3.10/3.12 and packaging integration checks passed.
  Local and remote `main` contain the verified application milestone.
- Last verified application branch: `feat/generic-controller-application`,
  through `028f384`.
- Working branch prepared for this next task: `feat/domain-tunnel-provisioning`,
  created from updated `main` after that merge. The original handoff commit
  changed documentation only; the domain provisioning behavior is now
  implemented and qualified as recorded above.
- Historical application release: `0.5.0`; its wheel code was source `3886b08`.
  Later `b1e29a1` changes the CI/browser harness, and later commits record
  deployment evidence. Do not confuse the current Git revision with the
  deployed artifact's source revision.

## User decisions: implement both

1. Every managed router in the trusted topology domain must be selectable as a
   dashboard tunnel headend. This includes transit P routers, not just PEs.
   New routers and tunnel IDs must not require ad-hoc controller allowlists.
2. Every **authenticated** dashboard user may create tunnels within the
   deployment's policy. Do not introduce another manually maintained username
   allowlist for ordinary creation. Anonymous requests remain unauthorized.

Authentication does not remove policy, capacity, ownership, transport readiness,
session, same-origin or CSRF checks. It must remain possible to identify and
audit the actual principal responsible for each operation.

## Historical verified application baseline: 0.5.0

At the application milestone below, the enabled `ninja-controller` service
supervised the BGP-LS collector, PCEP listener and read-only dashboard. GoBGP
remained the external BGP speaker;
the separately qualified SSH provisioning service and authenticated Nginx
proxy remained separate. The unified application was deployed and service-restart
qualification was complete for that release.

| Observation | Verified result |
|---|---|
| Topology | Eight routers and 22 directed TE links across two OSPF areas |
| PCCs | Eight fresh synchronized sessions |
| LSPs | Six ACTIVE, with original routes and delegation flags preserved |
| Delegation | Legacy Tunnel100 is healthy with `D=0`; the other five LSPs retain `D=1` |
| Local reservation ledger | Original single held managed operation |
| Activation/restart writes | No PCUpd or archived update; healthy paths were preserved |
| Durable authority | Provisioning journal/hold hashes and managed authority/history unchanged |
| Recovery history | Two failure attempts and nine startup attempts retained; pre-migration operator reboots contributed to that history |
| External services | Unrelated service process IDs unchanged |
| PE1 Tunnel1002 | Admin/Oper UP, RSVP connected, MPLS echo 5/5 |
| PE1 Tunnel10030 | MPLS echo 5/5 after application migration/restart |
| PE4 Tunnel1003 → PE1 | Admin/Oper UP, RSVP connected, MPLS echo 5/5; trace through P4/P3 reached PE1 |
| HTTPS boundary | Anonymous `HEAD /healthz` returned 401 with `WWW-Authenticate` |

An authenticated browser session was not exercised in this final deployment
check. The service is enabled on boot, but a cold controller VM reboot was not
qualified as part of this application migration.

Complete regression: **1,876 tests passed in 199.66 seconds, 88% coverage**.
Ruff and mypy passed. Remote CI passed packaging, Python 3.10 and Python 3.12.
Five remote browser startup timeouts were fixed by explicit native Chrome
selection and startup preflight, retaining all five existing UI assertions;
the focused browser selection run passed seven tests. The focused deployment/
provenance run passed 33 tests.

The clean release manifest records source `3886b08`, `source_dirty=false`,
artifact checksums and wheel/source verification. A fresh offline wheel install
passed version/validate/plan/status checks. Missing sources correctly produce
degraded status/exit 1; static validation creates no live/state/runtime files.

## Read these files first

These links are relative to a handoff stored under `docs/`:

- [User guide](user_guide.md), [deployment guide](deployment.md) and
  [application validation](application_deployment_validation.md).
- [Domain-policy behavior](topology_domain_delegation.md) and
  [domain qualification](topology_domain_validation.md).
- [Dashboard creation](dashboard_tunnel_creation.md),
  [qualified configuration provisioning](dashboard_configuration_validation.md)
  and [provisioning deployment](dashboard_provisioning_deployment.md).
- [Managed startup qualification](managed_startup_recovery_validation.md) and
  [stateful architecture](stateful_pce_architecture.md).

Private live access notes are maintained in the ignored local file
`build/private/session_access.md`. Consult that file locally when needed; it is
not a versioned dependency. Do not copy its contents into Git or this handoff.
Keep live public addresses, credentials, operational UUIDs, private host paths
and console endpoints out of tracked files. Use synthetic fixtures/examples. Do not read dashboard initial-password
files or ask the user to send passwords. Router console access does not authorize
administering the GNS3 server OS.

## Existing behavior to preserve

Topology-domain discovery already handles eligible router-owned initial LSPs
without per-router or per-tunnel registrations. Exact domain identity, current
unique TE node, TCP-source/sender identity, positive bandwidth, fresh synchronized
delegation, supported attributes and current path feasibility remain required.
Healthy eligible delegated LSPs can be passively adopted after reconnect.

This initial authority does not create tunnel interfaces, grant arbitrary
active optimization or replace an existing managed owner's authority. The
exact managed recovery operation retains separate original constraints, journals,
reservation holds and attempt history. Healthy legacy `D=0` state must remain
healthy without forcing delegation.

Dashboard configuration creation currently uses a separately qualified SSH
adapter. Native bandwidth-aware RSVP creation through PCInitiate remains
unqualified on IOS XE 16.9.7. Retain the working configuration-plus-PCEP path
until another mechanism is independently qualified.

## Historical problem statement: completed by this milestone

Before 0.6.0, ordinary initial PCEP paths were topology-driven, but dashboard
provisioning was tied to a small explicitly configured device/destination inventory, an
exact operator-name list, one held managed operation, bounded small journal
history and a global policy digest. These restrictions prevented the domain model
from extending naturally to multiple headends, users and simultaneous services.

The user wants one reusable application that can be deployed across devices and
topologies. Do not solve the next example by adding another router, username,
tunnel ID or fixed path to an allowlist.

## Original implementation and preservation requirements

Keep topology authorization separate from provisioning transport enrollment.
The fresh trusted TED determines managed node identity and eligibility; an
enrolled transport profile supplies the device adapter, SSH/NETCONF endpoint,
credential reference and independently pinned host key. A TE router ID is not
automatically a management endpoint, credential or cryptographic identity.
Show managed nodes consistently and explain transport/PCC readiness without
silently replacing domain eligibility with a static inventory filter.

Retain the qualified SSH backend initially behind a transport interface. A
NETCONF adapter can follow as independently tested work; do not label rendering
support or a shared transport interface as vendor interoperability.

Replace the ordinary username allowlist with authorization of authenticated
proxy principals within the configured domain policy. Preserve trusted proxy
identity, Unix-socket permissions, exact HTTPS origin, secure sessions, CSRF,
principal-scoped operation visibility and audit. Never trust a caller-supplied
username header outside the authenticated proxy boundary.

Support multiple current provisioning operations with explicit bounded
concurrency and capacity checks. Allocate tunnel IDs in the correct headend
namespace, keep ownership distinct from changing RSVP LSP-IDs, reject existing
resource collisions and preserve unique generated configuration ownership.
Principal-scoped idempotency must recover the same uncertain operation rather
than allocate a duplicate. Concurrent previews/creates, retries and policy
changes need meaningful tests.

Version policy per operation. A later domain or transport-policy revision must
not invalidate the complete historical journal or silently grant new authority
to an old operation. Design an explicit migration for the existing provisioning
journal, reservation ledger and managed recovery binding. Preserve original
authorization, owner, exact held resources, uncertainty, failure/startup history
and attempt limits; do not reset/recreate the live managed operation to bypass
the migration.

Preserve preview → exact plan approval → current admission/ownership recheck →
durable intent → configuration write → initial PCEP control → independent
headend verification. A report acknowledgement, ACTIVE state, RSVP verification
and forwarding test remain distinct outcomes. Retain holds when delivery or
cleanup is uncertain; do not release them merely because a GUI line disappears.

## Historical starting plan: migration and deployment completed

This plan is retained as historical context; its migration and deployment steps
have been completed for the 0.6.0 milestone above.

1. Confirm the merge outcome, checkout `feat/domain-tunnel-provisioning` from
   updated `main`, and read repository instructions and the guides above.
2. Review the current provisioning, API, PCEP ownership bridge and reservation
   formats before defining a migration. Relevant code starts in
   [cli_provisioning.py](../src/ninja_pce/cli_provisioning.py),
   [tunnel_api.py](../src/ninja_pce/tunnel_api.py),
   [pcep_provisioning.py](../src/ninja_pce/pcep_provisioning.py),
   [pcep_managed_failover.py](../src/ninja_pce/pcep_managed_failover.py),
   [pcep_domain_policy.py](../src/ninja_pce/pcep_domain_policy.py),
   [reservations.py](../src/ninja_pce/reservations.py) and the dashboard UI.
3. Implement the generic domain/principal/transport policy and a reviewable
   state migration, with offline concurrency/ownership/restart coverage first.
   Parallelize separable work with agents where useful.
4. Capture a fresh live baseline before deployment. Preserve all existing
   tunnels, journals, holds, recovery histories, authentication and unrelated
   service processes. Never assume a console remains logged in indefinitely.
5. Prepare provisioning readiness for all eight existing managed lab routers
   using reusable enrollment. Qualify at least two managed headends and two
   authenticated principals with concurrent independent operations, safe same-key
   retries, policy refusal,
   a missing/changed transport identity, restart preservation and independent
   RSVP/forwarding checks. Update user/deployment guides with the actual scope.

## Future work: two different meanings of FRR

RSVP-TE Fast Reroute is router-local protection. The intended later design is
local repair first, followed by PCE restoration when the topology and ownership
evidence support it. RSVP-TE local backup mechanisms are defined in
[RFC 4090](https://www.rfc-editor.org/rfc/rfc4090.html). Ninja-PCE currently rejects
unsupported protection flags; qualify protection reporting, ownership and
capacity before enabling them. Do not promise a failover time from control-plane
observations alone.

FRRouting is a separate software routing suite. It may provide lightweight
future OSPF/BFD and SR-TE/PCEP test nodes: its
[pathd documentation](https://docs.frrouting.org/en/latest/pathd.html) describes
SR-TE policies and PCEP, while its
[BFD documentation](https://docs.frrouting.org/en/latest/bfd.html) covers BFD
and routing-daemon integration. These are future qualification tracks, not a
drop-in RSVP-TE replacement. Keep the existing qualified GoBGP ingestion while
evaluating them.
