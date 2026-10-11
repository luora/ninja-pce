# Dashboard tunnel creation design

Status: proposed; tunnel creation is not implemented. The current
[dashboard](dashboard.md) is read-only, rejects mutation methods and has no
Create action. Ninja-PCE advertises initiation capability clear and sends no
PCInitiate. This document defines the next feature and its qualification gate;
it does not authorize a router change.

## Operator flow

The operator selects **headend**, **destination** and **bandwidth in Mbps**,
then clicks **Create** once. Headends come from a server-side, qualified PCC
allowlist; destinations come from known topology nodes. For example, PE1
`10.255.0.1` to PE3 `10.255.0.6` with `1` Mbps. Free-form router addresses,
commands and arbitrary filesystem paths are not accepted.

The form displays the enforced policy: numbered IPv4 RSVP-TE, strict hops,
minimum TE metric, no affinity constraint, and configured setup/hold priorities
(initial proposal: 7/7). These defaults need no additional fields. Bandwidth
uses decimal Mbps, validated and rounded upward to integer bits/s. The
controller allocates a unique managed name; the PCC assigns its own LSP IDs.

Create starts computation, admission and delivery as one tracked operation.
The page immediately shows its operation ID and progress, and remains usable
while it polls. Acknowledged, reported ACTIVE and independently verified are
different labels. An unavailable verifier leaves an acknowledged operation
unverified. Creating a tunnel does not enable autoroute or bind customer traffic;
service steering requires a separately authorized feature.

## What the current image proves

The [live validation](live_pcep_computation_validation.md) proves initial
PCRpt/PCUpd provisioning of an already configured PE1 Tunnel100, with RSVP
reservation and MPLS OAM evidence. That is distinct from creating a new tunnel.
PE1's observed OPEN has `stateful_flags=5` (`U=1`, `I=1`) and top-level TLV 26
value `00000010`. These are capability observations, not a completed creation
test. The controller's own initiation capability remains clear.

[RFC 8281, Sections 4–5](https://www.rfc-editor.org/rfc/rfc8281.html#section-4)
requires initiation capability on both peers. Its creation procedure uses
PCInitiate, a new session-scoped SRP ID, PLSP-ID zero, a symbolic name and an
ERO. The PCC returns its allocated nonzero PLSP-ID and correlated PCRpt with
Create and Delegate flags. RSVP-TE is the base setup method.

[RFC 8408, Sections 3–5](https://www.rfc-editor.org/rfc/rfc8408.html#section-3)
defines OPEN capability TLV 34 and RP/SRP setup-type TLV 28. PST 0 means RSVP-TE;
omitted setup-type information defaults to RSVP. PST 1 means SR under
[RFC 8664](https://www.rfc-editor.org/rfc/rfc8664.html#section-4.1.2).
RFC 8664 Appendix A treats legacy top-level TLV 26 as advertising PSTs 0 and 1;
a present TLV 34 takes precedence. The observed value has SR flags clear and
MSD 16 under that format. This compatibility inference does not establish
RSVP initiation with bandwidth on the actual image.

Cisco's [Fuji 16.9 Dynamic PCC chapter](https://www.cisco.com/c/en/us/td/docs/ios-xml/ios/seg_routing/configuration/xe-16-9/segrt-xe-16-9-book/sr-dynamic-pcc.html)
documents RSVP and SR reporting/delegation/update. Its
[16.9 PCE-initiated LSP chapter](https://www.cisco.com/c/en/us/td/docs/ios-xml/ios/seg_routing/configuration/xe-16-9/segrt-xe-16-9-book/sr-pcep-lsp.html)
describes **SR**, requires auto tunnels and limits support to basic generation
without TE attributes. The example is an SR auto tunnel with zero bandwidth.
The release-family guide does not qualify RSVP bandwidth creation on this
CSR1000v 16.9.7 image. Do not silently substitute SR for RSVP.

Standards review: [RFC 9756](https://www.rfc-editor.org/rfc/rfc9756.html)
updates RFC 8281's registration procedures; RFC 8664 updates RFC 8408 and is itself
updated by RFC 9756 and [RFC 9933](https://www.rfc-editor.org/rfc/rfc9933.html)
(SR-Algorithm extensions). These updates do not certify platform support.
Implementation must also account for verified RFC 8281 erratum 6301 on the
deletion/reclamation grammar; see the
[RFC 8281 errata registry](https://www.rfc-editor.org/errata/rfc8281).

## Proposed API and execution boundary

These endpoints do not exist yet. The current proxy also permits only read
methods and would require an intentional security-reviewed update.

| Endpoint | Proposed behavior |
|---|---|
| `GET /api/tunnel-options` | Authorized headends, destinations, policy defaults, qualification and source-health status. |
| `POST /api/tunnels` | Validate intent, create or return an idempotent operation, then compute/admit/send through the controller's guarded writer. |
| `GET /api/tunnel-operations/{id}` | Authorized operation status, requested/computed/reported route, evidence and failure reason. |

Example intent, with a session-bound CSRF token and an `Idempotency-Key` header:

```json
{"headend":"PE1","destination":"PE3","bandwidth_bps":1000000}
```

Return `202 Accepted` with an operation ID and status URL, never an immediate
"tunnel created" claim. Reject invalid fields, unauthorized endpoints, disabled
creation or unsupported platform profiles before scheduling any write. A
well-formed intent can still fail admission without sending protocol bytes.

One click authorizes this constrained creation intent under the displayed
operator policy. It does not manufacture approval for an unrelated CLI plan.
Persist the authenticated principal, exact intent, effective policy revision,
computed plan digest and its expiry. A shared execution boundary must enforce
that authorization against the exact wire plan immediately before sending.
Future raw CLI execution must retain its existing exact-plan approval rules.

The worker must atomically lock and admit against fresh directed TE data plus
all controller holds, persist the pending reservation and ownership intent,
then recheck topology digest/freshness, PCC session/synchronization, negotiated
setup type, authorization, writer fencing and kill switch inside the per-PCC
writer lock immediately before sending. Recompute/reject if those inputs
change before the write; never send an old
plan. Unknown bandwidth, ambiguous hop identity, incomplete sources or another
writer's unresolved operation block admission. The current live BGP-LS feed is
not an authoritative reservation ledger: connecting it to durable creation
accounting and defining its admission policy remain implementation work.
Use directed advertised bandwidth-priority vectors and conservative holds;
do not infer spare capacity by summing LSP reports or grant unproven self-credit.
The PCC's RSVP admission remains the final network admission decision.

Look up persistent idempotency records by `(authenticated principal, key)`,
then compare the stored intent digest. Replaying an identical intent returns
the same operation; changed intent with that key returns a conflict. Serialize
writes per PCC and bound queue, worker, LSP and bandwidth limits. A browser
timeout/reload must not create another LSP.

## Security and recovery requirements

- Keep the public proxy authenticated over TLS. Add operator/read-only roles
  with explicit headend, destination and bandwidth limits; the current shared
  viewer credential is insufficient mutation authorization. The trusted proxy
  must strip caller-supplied identity headers and pass verified identity over
  a permissioned Unix socket or an independently authenticated backend channel.
  Do not trust identity headers from arbitrary loopback callers or expose a
  route around the proxy.
- Require JSON POST, strict body/schema bounds, a session-bound CSRF token,
  validated Origin/Host, restrictive cookie settings and no permissive CORS.
  Basic authentication alone does not provide CSRF protection. GET remains
  side-effect free, including on loopback. Disable creation entirely in demo.
- Enforce the kill switch and all write permissions in the execution service,
  not only the page. Restrict PCC reachability; the existing IP allowlist is
  not cryptographic PCEP authentication. Authenticated transport remains a
  separate deployment requirement before untrusted-network operation.
- Record intent, policy/TED digests, send boundary, PCC/session identity, SRP,
  symbolic name, allocated full LSP identity and evidence references durably.
  Redact credentials and environment identifiers from exported audit records.
- Persist holds before network I/O. After any uncertain send, retain resources
  and reconcile fresh synchronized PCC state before retry, release or reclaim.
  Never blindly repeat creation after an acknowledgment timeout or restart.
- Do not reuse, delete or overwrite Tunnel100, its explicit fallback option 10,
  preexisting paths or another controller's LSP. A new PCE-created auto tunnel
  has no assumed fallback. Deletion/reclamation needs separate qualified
  ownership handling; no generic teardown button is included in this feature.

## Visible operation states

| State | Meaning and required evidence |
|---|---|
| `PLANNED` | Computed intent passed admission; exact plan and pending hold are durable. |
| `PUSHING` | Write boundary entered; outcome can become uncertain. |
| `APPLIED_UNVERIFIED` | Correlated response identifies the new LSP; acknowledgment may still report DOWN. No green success badge. |
| `VERIFIED` | Fresh independent headend/transit evidence matches identity, RSVP setup, endpoints, requested bandwidth/priorities, ordered route and operational state. |
| `VERIFICATION_FAILED` | Evidence conflicts, is incomplete or misses the deadline; retain holds unless absence/release is proven. |
| `ROLLBACK_REQUIRED` | Delivery/recovery is uncertain and needs controlled reconciliation or operator intervention. |
| `ROLLED_BACK` | An owned-object cleanup was independently confirmed, including resource release. |

Prewrite validation/no-path failures show their reason without implying that
anything reached a router. SRP correlation is scoped to the current session and
full LSP identity; historical/synchronization SRP repeats are not acknowledgments.
Reported ACTIVE alone cannot reach VERIFIED. MPLS OAM and any traffic test appear
as separate dated evidence: RSVP reservation does not prove payload throughput,
and the lab's license limits preclude a 50 Mbps throughput claim.

## Next experiment on CSR1000v 16.9.7

This is a future operator-authorized lab experiment, not a command sequence
implemented by the current listener. First build and test a bounded PCInitiate
codec, response correlation, durable holds and owned-object cleanup/recovery.
Keep initiation clear in normal operation until that handler exists.

1. Snapshot PE1 configuration, OPEN capability bytes, existing LSP names/IDs,
   Tunnel100 and its fallback, and fresh directed BGP-LS/RSVP availability.
   Confirm the exact image/license and any required auto-tunnel configuration
   from image-specific CLI/help; do not copy SR prerequisites into RSVP blindly.
2. Enable initiation only for the authorized PE1 session and one new managed
   symbolic name, for example `NINJA_QUAL_CREATE_001`. Reestablish that session
   deliberately to negotiate both I flags; capture the effect on existing
   Tunnel100 and await synchronization. No global initiation enablement.
3. Send one PCInitiate: fresh nonzero SRP, remove flag clear, PLSP-ID 0, unique
   symbolic name, explicit IPv4 endpoints `10.255.0.1`/`10.255.0.6`, explicit
   PST 0, strict numbered ERO from the fresh TED, 1 Mbps requested bandwidth
   (125000 bytes/s) and priorities 7/7. No SR segments or autoroute changes.
   This is the narrow proposed local profile, not a claim every field is
   mandatory in the base protocol.
4. Capture PCErr or PCRpt without changing the request into SR or a CLI push.
   An unsupported setup/parameter error is a useful negative result. Positive
   qualification requires the new assigned identity, echoed SRP, Create and
   Delegate flags, matching RSVP attributes and subsequent operational proof.
   A first DOWN acknowledgment remains pending; a setup-type mismatch fails.
5. Independently inspect the new headend LSP, every transit/tail RSVP
   reservation and a bounded MPLS OAM probe. Confirm Tunnel100/fallback are
   unchanged. Then test owned cleanup and observed capacity release; failure
   leaves the operation held for manual reconciliation, not blanket deletion.
6. Before enabling dashboard creation, qualify duplicate-click/replay,
   reservation exhaustion, lost reply, session/controller restart and
   ownership recovery without duplicate LSPs or released uncertain holds.
   Add raw, redacted captures and regression fixtures for the exact image.

If RSVP PCInitiate is rejected or bandwidth is ignored, keep creation disabled
for this profile. A qualified newer image/vendor or a separately qualified
configuration-based provisioner is an alternative project milestone; the
existing preconfigured-tunnel PCUpd workflow must retain its distinct label.
