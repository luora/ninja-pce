# Capability-driven tunnel workflow

PR 6 adds prerequisites and workflow stages to the existing tunnel dialog.
An additive `capability` report in `GET /api/tunnel-options` explains global and
selected-headend eligibility. The qualified backend still decides every
authorization, admission, delivery and verification outcome.

## Scope and ownership

`scope: "existing-qualified-profile"` concerns the previously qualified IOS XE
configuration workflow. It does not qualify another router image, deployment,
vendor, production controller or native PCInitiate profile. Existing private
API/backend qualification and preview-only policy remain in force. Shipped
examples still disable writes and qualification.

Cross-controller exclusivity is not established by synchronized PCCs, delegation,
qualification, Unix sockets or local resource locks. Every report contains
`production_available: false`, `control_ownership_verified: false`,
`control_authority_verified: false` and `forwarding_verified: false`.
`CONTROL_OWNERSHIP_UNVERIFIED` remains unknown and blocks production scope;
it does not silently disable the existing independently qualified lab workflow.
Enforced scope-bound single active controller ownership with PCC-aware fencing
remains a release blocker for active production control. This PR introduces no
switch that claims to verify ownership or changes retained DEV owners, holds
and recovery budgets.

Authenticated [observation setup](authenticated_setup.md) and the standalone
dashboard always report unavailable provisioning, regardless of healthy network
observations. Admin login does not authorize the separate provisioning service,
and setup accepts no browser request to attach a write backend. Its report
explains missing qualification, backend and authority. Demo data grants none.

## Operator workflow

1. Open **Create tunnel** and review **Creation prerequisites**. Pass, fail and
   unknown checks show stable codes and manual next steps. Global eligibility
   never hides the selected node's missing transport or PCC. **Refresh
   availability** samples current evidence; discovered domain endpoints remain
   visible when a prerequisite is missing.
2. Select a distinct authorized headend/destination and bandwidth. Review the
   computed strict path, effective RSVP bandwidth, exact configuration and expiry.
   Preview reserves no bandwidth and contacts no router. Editing intent or
   expiry invalidates that preview.
3. Choose **Create tunnel** to approve the exact plan. Capability eligibility
   cannot substitute for approval or backend admission.
4. Inspect the durable operation. Provisioning, acknowledgment, PCEP/RSVP
   verification and forwarding are separate stages. Only the existing exact
   independent headend RSVP proof advances `VERIFIED`; forwarding remains
   unverified and needs an independent traffic test.
5. Recover a lost response using **Retry same request**, preserving its original
   principal, intent, approved plan and idempotency key. Capability loss or
   qualification withdrawal does not erase that identity. Existing operation
   status remains readable under its authenticated boundary.

Uncertain delivery retains capacity and requires the existing owned
reconciliation procedure. The dialog never resends an uncertain recorded
operation, deletes a tunnel, refunds holds, resets recovery budgets or saves
router startup configuration. Follow the existing
[deployment guide](dashboard_provisioning_deployment.md) and
[domain provisioning policy](domain_tunnel_provisioning.md).

## Report contract and boundaries

Legacy `enabled`, `reason`, endpoints, per-node readiness, principal, policy and
CSRF fields remain. `capability` adds `schema_version: 1`,
`capability: "tunnel_creation"`, ISO `generated_at`, scope, availability,
`checks`, per-headend checks, `next_action`, `limitations`, `complete`,
`informational: true` and `submission_revalidated: true`.
Each check has `state: "pass" | "fail" | "unknown"`, `required`, `code`,
`message` and `next_action`. Missing evidence is unknown. Mandatory prerequisites
cannot be made optional through a projection. `complete` includes unresolved
ownership coverage and is separate from existing lab eligibility.

| Global check | Evidence |
|---|---|
| `authenticated_operator` | Existing trusted proxy/principal boundary, or the distinct read-only admin session |
| `provisioning_service_ready` | Current request reached its provisioning API |
| `api_qualified`, `backend_qualified`, `backend_supported` | Explicit qualification and supported exact-plan configuration backend |
| `writes_enabled` | Existing preview-only policy permits writes |
| `topology_ready` | One fresh consistent topology sample under trusted domain policy |
| `pcep_listener_ready` | One current checksummed capture without listener/capture failure |
| `pcep_authorized` | Listener configuration-provisioning capability and exact authorization policy digest |
| `recovery_clear` | Existing failure/managed recovery barriers are clear |
| `topology_policy_matches` | Exact OSPF area identity policy agreement |

Per-node checks cover domain eligibility, enrolled transport, transport
profile/credential/pinned identity readiness and a fresh established synchronized
PCC. The API also requires a distinct authorized destination for each eligible
headend. Reports bound endpoints to 256 and limit check counts/text; excessive
scope prevents availability rather than certifying a partial domain.

The backend samples topology and PCEP capture once. Its capture reader requires
a checksummed regular file, refuses symbolic links/FIFOs and caps input at
16 MiB. Malformed, missing or stale evidence becomes unknown. Reporting opens no
router connection, acquires no admission locks, instantiates no new provisioner
and changes no journal/reservation state. Messages use fixed safe guidance,
without credentials, management endpoints, pins, paths, raw captures or errors.

No new route or proxy trust is introduced. Qualified provisioning retains its
permissioned Unix socket, trusted proxy UID, exact HTTPS origin, principal-bound
secure session, CSRF and strict POST intent/approval. Observe administration
keeps its separate loopback session and has no provisioning POST route.

The browser refuses new work for missing, malformed, stale or unsupported
reports and unmet mandatory global/selected-node checks. Reports expire after
thirty seconds; future/unconfirmed timestamps also disable new creation.
Late options responses cannot restore an earlier principal after close/reopen.
Messages are rendered as text. Upgrade matching API/UI assets together: an older
backend without a structured report leaves new creation unconfirmed. Legacy API
admission semantics and exact-request recovery remain unchanged.

Admission rechecks policy, topology, PCC synchronization, transport revision,
exact approval, ownership, capacity and expiry under the durable request lock.
Immediate prewrite checks remain intact. Scoped idempotency recovery precedes
readiness, so reading an old operation needs no authority for a new delivery.

Validation uses synthetic transport/API fixtures, local browser tests and offline
installed-package checks. It covers missing evidence, unsafe/malformed captures
and timestamps, per-node refusal, unchanged state, redaction, exact approval,
principal isolation and recovery during capability loss. It adds no fresh router,
production ownership, reboot or forwarding qualification.
