# Domain tunnel provisioning validation

Prepared 2026-10-15 (Africa/Nairobi) for `feat/domain-tunnel-provisioning`.
The domain-wide, multi-user provisioning implementation is verified offline.
The existing live application remains on its previously qualified release;
this milestone has not been deployed or qualified on live routers.

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
| Later focused regression | 349 passed in 13.15 seconds | Migration and concurrency changes plus affected provisioning, authority, and dashboard behavior |
| Ruff | Passed | Source and test static checks |
| mypy | Passed | Application source type checks |

The later focused run is separate from the complete-suite checkpoint.
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

## Read-only live baseline

| Observation | Result |
| --- | --- |
| Topology | Eight routers and 22 directed TE links |
| PCC state | Eight synchronized sessions |
| LSP state | Six ACTIVE LSPs |
| Reservation ledger | One original held managed operation |
| Recovery history | Two failure attempts and nine startup attempts |
| Existing creation journal | Three operations: one VERIFIED and two ROLLED_BACK |
| SSH identity preparation | Seven independent router host keys observed; the remaining router console refused access |

These observations provide a preservation baseline. They do not demonstrate
that all eight transports are enrolled and ready. No live deployment, journal
migration, new tunnel creation, or restart qualification was performed for this
milestone. Previously recorded RSVP and forwarding checks remain evidence for
the existing deployment only.

## Pending live qualification

Complete transport enrollment for every managed router using independently
verified host identities. Review the exact migration plan and retained backups
before changing live state; preserve original holds, operation authority,
recovery bindings, failure/startup history, and healthy legacy delegation flags.

After deployment, demonstrate concurrent independent operations from at least
two managed headends and two authenticated principals, safe same-key retries,
policy refusal, missing and changed transport identities, and restart
preservation. Independently check headend RSVP state and bounded forwarding
evidence for the new operations. Record report acknowledgement, ACTIVE state,
RSVP verification, and forwarding checks as separate outcomes.
