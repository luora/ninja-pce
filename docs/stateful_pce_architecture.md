# Stateful PCE architecture and protocol boundaries

## Present boundary

The computation/reconciliation engine now has a read-only live BGP-LS adapter
and a PCEP listener with explicitly enabled computation profiles. GoBGP receives
BGP-LS; `bgpls.py` reads its binary RIB through a restricted local gRPC schema
and normalizes directional
TE attributes. `pcep.py` observes LSP reports by default. Opt-in responders
answer a bounded IPv4 PCReq profile or provide an initial PCUpd for an authorized,
delegated DOWN/empty-ERO RSVP path after synchronization. `live.py` exposes these
profiles. See [computation scope](pcep_computation.md) and
[live qualification](live_pcep_computation_validation.md).
General active-LSP reoptimization, PCInitiate, durable delegated ownership and
integration with the reservation ledger remain future work. Protocol acceptance
remains distinct from independently verified RSVP forwarding.

The existing modules have separable responsibilities: loaders supply immutable
TED snapshots; CSPF selects directional edge paths; a reservation ledger accounts
for planned/pending/observed resources; the state repository persists desired,
pending and last verified state; render/southbound prepare and deliver commands;
operational verification evaluates evidence; reconciliation coordinates them.
New adapters should satisfy these boundaries instead of bypassing admission or
execution authorization. See `interfaces.py` for the small structural interfaces.

## State ownership

The TopologyProvider owns source selection, transport and snapshot completeness.
The TrafficEngineeringDatabase owns directed link identity, TE data provenance,
withdrawals, freshness and atomic snapshot revisions. BGP-LS NLRI identity must
retain protocol/domain and local/remote node/link descriptors, not just router
pairs. Parallel links and per-direction attributes must survive every update.
Missing attributes remain unknown; a collector must not invent bandwidth or
addresses. Graceful restart cannot turn retained stale routes into current TED.

The PathComputationEngine consumes a single immutable revision, returns selected
edge IDs and ERO objects, and distinguishes proven no-path from exhausted search.
The ReservationManager atomically admits requests and maintains local holds until
authoritative per-LSP evidence accounts for them. Aggregate bandwidth reductions
alone cannot prove which pending LSP was reflected. LspStateRepository stores
desired, observed, pending and verified revisions separately with durable
transition history. SouthboundAdapter changes configuration only within a
specific authorized transaction. OperationalVerifier decides what is proven by
device evidence. ReconciliationController checks freshness, ownership and plan
identity before coordinating those operations.

Filesystem locks coordinate processes sharing the same local state directory.
They do not establish distributed controller leadership. Multiple hosts require
a transactional shared store, fencing epochs and a single authoritative writer
per resource. A router or independent controller may consume bandwidth between
snapshots; final RSVP admission remains the router's responsibility.

## PCEP subsystem and remaining extensions

The passive listener implements bounded framing, OPEN/KEEPALIVE timers, CLOSE,
errors, IP peer allowlists and read-only PCRpt synchronization. It advertises
the stateful capability with the update/initiation flags clear by default. These are
implementation and test claims, not complete protocol conformance certification.
Baseline protocol behavior comes from [RFC 5440](https://www.rfc-editor.org/info/rfc5440/).
Read-only PCRpt synchronization must complete before enabling stateful updates;
the initial delegated-path profile enables updates only for explicitly bound
PCC/tunnel IDs, with both peers' U flags set. It sends no active-path rerouting
or PCInitiate, and returns unsupported delegations to PCC local policy.
Delegation/revocation and synchronization belong to the session/state owner;
computation does not confer control authority. [RFC 8231](https://www.rfc-editor.org/info/rfc8231/)
defines these stateful capabilities. PCE-initiated creation/removal is a separate
negotiated stage under [RFC 8281](https://www.rfc-editor.org/info/rfc8281/).

Maintain PLSP-ID scoped to PCC/session identity, symbolic LSP identity, SRP
correlation, reported operational state, actual routes and delegation ownership.
Handle session loss, sync restart and duplicate reports idempotently. No update
is permitted before synchronization and delegation are established. PCC reports
must reconcile ledger holds without interpreting command acceptance as a report.

## Standards review, 2026-10-09

The RFC Editor status and errata pages were checked during remediation. Recheck
them before implementation; this is an implementation plan, not a compliance claim.

| Base | Follow-up review required |
|---|---|
| 5440 | Updates 7896, 9488, 8253, 8356, 9756 and its verified errata |
| 8231 | Updates 8786, 9353, 9753, 9756, 9862 and verified errata |
| 8281 | Current status, stateful capability compatibility and inline errata |
| 9552 | Replaces 7752 and 9029; verified errata 7789 and the current errata set |

Sources: [5440 status](https://www.rfc-editor.org/info/rfc5440/),
[8231 status](https://www.rfc-editor.org/info/rfc8231/),
[8231 errata](https://www.rfc-editor.org/errata/rfc8231),
[8281 status](https://www.rfc-editor.org/info/rfc8281/),
[8281 inline errata](https://www.rfc-editor.org/rfc/inline-errata/rfc8281.html),
[9552 status](https://www.rfc-editor.org/info/rfc9552/),
[9552 errata](https://www.rfc-editor.org/errata/rfc9552).
Updates can change flags, errors and synchronization behavior. Build conformance
vectors from verified errata and applicable negotiated capabilities before code.
Unsupported optional extensions must remain disabled.

## Security and deployment stages

Use authenticated protected management transport, explicit peer allowlists and
least privilege. Evaluate PCEPS/TLS under [RFC 8253](https://www.rfc-editor.org/info/rfc8253/)
and current security policy; certificate identity must bind the expected PCC.
Treat topology sources and PCC reports as untrusted input. Bound message sizes,
timers, queues and computation budgets, isolate collectors from writers, redact
secrets, and audit state/approval revisions.

1. Harden file snapshots, edge-aware computation and offline verification tests.
2. Execute isolated licensed-image GNS3 RSVP tests, capture actual outputs, and
   qualify platform/release parsers before autonomous CLI control.
3. Add a read-only BGP-LS collector with withdrawals, provenance, replay and stale
   tests. Compare its normalized snapshots against router TED state.
4. Add stateless PCEP interoperability tests, then passive stateful synchronization.
5. The isolated initial-delegation profile has capability, restart and bounded
   worker tests plus live forwarding qualification. General delegated updates
   still require durable ownership, authentication and recovery qualification.
6. Enable initiated LSPs and closed-loop changes after ownership/fencing and
   multi-controller recovery tests prove safe operation.

FRR may support collector experiments; it is not an interchangeable IOS XR
RSVP-TE implementation. Licensed images and measured interoperability determine
platform support, not the presence of a renderer.
