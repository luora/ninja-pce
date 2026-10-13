# Dashboard tunnel creation design

Status: experimental implementation; the configuration backend passed
[one private IOS XE 16.9.7 qualification](dashboard_configuration_validation.md).
Shipped examples and the default server keep creation disabled. Native
PCInitiate bandwidth creation remains unqualified. The [dashboard](dashboard.md)
has a Create tunnel form,
and its default read-only server reports the qualification gate through
`GET /api/tunnel-options`. It continues to reject mutation methods. The separate
operator HTTP service uses a permissioned Unix socket, verified proxy UID and
principal, bounded sessions, same-origin checks and CSRF protection; it defaults
to unqualified and requires a separately qualified operator deployment.

The private lab session can send guarded PCInitiate with durable holds and
idempotency, correlate owned reports, and attempt one explicit owned PCUpd or
cleanup. Initiation remains clear unless the creation options are configured.
Offline tests do not establish device support. See the live result below.

## Configuration-based provisioning profile

The dashboard also supports a separately qualified **router configuration +
PCEP path control** backend. This does not enable or qualify native PCInitiate.
`CliProvisioner` admits a bounded managed tunnel, delivers generated IOS XE
configuration through the existing southbound adapter, and authorizes the
existing PCEP writer for that exact newly configured tunnel. Both the backend
and operator API default to unqualified. The private qualification of this
profile is recorded in the
[configuration qualification report](dashboard_configuration_validation.md),
separately from the negative initiation results below.

After the operator chooses headend, destination and bandwidth, the page
automatically requests a configuration preview. The preview displays the
computed route, actual RSVP bandwidth, exact generated router configuration
and expiry. IOS XE bandwidth rounds upward to whole kilobits/s; the preview
discloses any difference from the requested bits/s. Previewing creates neither
a tunnel nor a bandwidth hold and sends no router commands.

One explicit **Create tunnel** click approves the displayed plan's SHA256
identifier together with the original intent and idempotency key. Changing a
field invalidates the preview immediately. An expired preview is refreshed,
and Create remains disabled until a current plan is displayed. The service
revalidates admission, policy, topology and ownership before delivering the
exact approved plan; an altered or expired plan cannot authorize a new write.
An uncertain browser result retains the original key, intent and approved plan
across reload. Reload never resubmits automatically, and retry must recover
the existing operation rather than allocate another tunnel.
An authenticated retry with the same intent, key and approved plan can recover
an existing configuration operation even while new delivery is disabled or its
PCC/TED is unavailable. This lookup performs no backend creation or router
write. A changed intent or plan conflicts; an unknown key still requires full
qualification and readiness. The page permits only this explicit pending
request recovery while new creation is unavailable.
Definitive admission rejection returns a durable `REJECTED` operation bound to
that exact key, intent and approved plan. The form retains the rejection reason,
unlocks the fields and obtains a fresh preview; a new Create click uses a new
key. A late retry of the rejected key cannot deliver configuration. Transport
errors and uncertain HTTP failures retain the original pending request rather
than being treated as evidence that nothing was sent.

Generated configuration contains RSVP bandwidth and priorities, a dynamic PCE
path option, and the computed strict explicit path as fallback. It does not
enable customer traffic steering. Tunnel100 and its explicit fallback remain
outside the new operation's ownership. The initial profile admits one managed
tunnel at a time; no generic command, public deletion or arbitrary tunnel-ID
endpoint is exposed.

The configuration backend reaches `VERIFIED` only after independent headend
checks of identity, destination, bandwidth, ordered ERO and RSVP signaling,
plus evidence that the controller's initial PCEP path control completed. The
API requires the full explicit proof with scope `headend-rsvp`; the page labels
this **RSVP verified on the headend**. This does not prove payload forwarding,
end-to-end OAM, transit reservations or throughput. A PCRpt acknowledgment alone
cannot establish this state. Newly configured tunnels receive bounded
authorization for initial PCEP path control only. General automatic failure
recovery and reoptimization of dashboard-created tunnels require a separate
implementation and qualification; Tunnel100's existing recovery remains a
distinct workflow.

Sanitized private runtime policy, strict SSH host-key handling, systemd and
authenticated Unix proxy examples are in the
[configuration provisioning deployment guide](dashboard_provisioning_deployment.md).

## First live result — CSR1000v 16.9.7

The operator-authorized 2026-10-11 UTC test negotiated both initiation flags,
enabled PE1's P2P auto-tunnel allocator and sent one strict RSVP PCInitiate for
PE1 to PE3 at **1 Mbps**, priorities 7/7, with no affinity constraint. The route
was PE1 → P1 → P2 → PE3. Tunnel100 and its explicit fallback were preserved.

PE1 allocated auto-tunnel **62336**, PLSP-ID **586624**, RSVP LSP-ID **1**, and
echoed the creation SRP with C=1/D=1, PST 0 and the requested strict ERO. The
new object became operationally ACTIVE. However, requested and used bandwidth
were both **zero**, and LSPA exclude-any became `0x0000ffff` instead of the
requested zero. Independent `show ip rsvp reservation` also showed a zero-bit
reservation for this object, while Tunnel100 retained its 50 Mbps reservation.
The manager correctly retained its local hold and marked `ROLLBACK_REQUIRED`.
This result **does not qualify bandwidth-aware dashboard creation**.

The lab subsequently restarted. PE1 showed only Tunnel100, with the P2P
allocator disabled, and Ubuntu returned to its login prompt. On 2026-10-12 UTC,
controller access was recovered. Fresh synchronized PCC absence plus independent
headend and all five other routers' reservation/configuration checks confirmed
the old object and its reservations were gone. Only then was its saved hold
released. Restart alone was not treated as release evidence.

A second 1 Mbps PCInitiate reproduced the zero-bandwidth result, with the same
strict upper route and a separately persisted owned identity. One explicit
owned PCUpd requested the original bandwidth, priorities and affinity. PE1
returned a correlated SRP 2 PCRpt with C/D/R set for the old incarnation;
the experimental tunnel became down without a 1 Mbps RSVP reservation. The
controller kept `ROLLBACK_REQUIRED` and its hold. An R report caused by completion
is neither an applied creation nor acknowledgment of a requested cleanup.
Tunnel100 remained up at 50 Mbps with its configuration and explicit fallback
unchanged. This second result also fails qualification; no public delivery
profile is enabled, and no automatic update retry is introduced.

The second experimental object was allowed to expire during a bounded 75-second
PCEP pause with automatic service restart. PE1 then showed zero auto-tunnels;
its allocator was restored to disabled. Fresh synchronized PCC state and
independent checks on all six routers confirmed only Tunnel100 and its 50 Mbps
reservations remained. The second saved hold was released only after those
checks, and both operations are `ROLLED_BACK`. This is observed orphan expiry,
not qualification of the private PCInitiate deletion path. The original
non-initiation failover listener was restored; its PCEP session is fresh and
synchronized. The read-only dashboard form is deployed with creation disabled.

Controller recovery corrected a private control socket left in a persistent
home directory, which had blocked startup after reboot. Private qualification
uses a systemd-managed `/run` directory. Missing GoBGP/grpcurl executables were
restored from the existing pinned, checksum-verified archives. A separately
approved lab-interface firewall rule restored PE1 PCEP; the BGP-LS peer returned
with 45 routes. These repairs did not change Ubuntu account credentials.

The bounded private completion experiment now has offline tests: it requires
the exact persisted owned identity, fresh synchronized session and current
directed capacity, records a new session-scoped SRP before writing, and sends
only one PCUpd. Attribute failure can bind ownership for safe cleanup without
claiming application. A new RSVP incarnation is accepted only on the exact
outstanding completion SRP for the same PLSP/tunnel/name/endpoints. Restart
reconciliation records a new session binding without replaying creation.
This is not automatically invoked or enabled through the public API.

See [fixture provenance](../tests/fixtures/pcep-iosxe-16.9.7/README.md) for the
sanitized request and explicitly reconstructed response regression fixture.

## Captured diagnostic repeat — 2026-10-12 UTC

A preliminary request was rejected before transmission. Loading the recovered
history exposed a controller bug: the timeout monitor tried to mark a fully
`ROLLED_BACK` operation's old, unacknowledged completion uncertain. The manager
correctly refused to reopen a released operation, but the resulting monitor
error closed the creation write gate. Released operations are now skipped by
the monitor and report observer. Historical ownership suppression remains in
place, and unresolved operations still retain their holds and timeout handling.
Regression tests cover restart history, delayed ACTIVE/removal reports and
current-operation timeouts. This fix does not resolve the bandwidth mismatch.

After that fix, one new 1 Mbps PCInitiate was captured with PE1's supported
`debug pce client`, `error`, `obj`, `lsp` and `pak` categories, P2P auto-tunnel
events/state, and detailed TE tunnel errors. Debugging lasted about 26 seconds;
the original logging and debug settings were restored. The computed strict
route was PE1 → P3 → P4 → PE3, using remote addresses `10.0.13.3`, `10.0.34.4`
and `10.0.46.6`. Tunnel100 stayed on the upper route at 50 Mbps.

The router's own incoming packet decode shows BANDWIDTH class 5/type 1 and
`Requested BW: 125000` bytes/s: exactly **1 Mbps**, matching the controller's
wire bytes `47f42400`. Immediately afterward, PE1's local auto-tunnel/PCC path
construction already supplies bandwidth type 1 and type 5 as zero. Its tunnel
configuration, RSVP TSpec/FSpec, sender table and reservation table all show
zero. The new LSP was ACTIVE with the requested route, but failed bandwidth
qualification. No completion PCUpd was sent for this experimental LSP.

This localizes the loss to this router's handling of the supplied attributes
between PCEP decoding and tunnel setup. It rules out a transport or bytes/s
conversion error, and a merely incorrect dashboard/PCRpt display, for this
captured request. It does not distinguish an image limitation, implementation
bug or configuration-dependent behavior, and does not establish that native
RSVP initiation is impossible on other images or configurations.

One exact-owned PCInitiate delete used SRP 2 and the allocated PLSP-ID. PE1
removed the auto-tunnel, but its matching-identity removal report reused the
original creation SRP 1. The controller therefore did not accept it as a
correlated cleanup acknowledgment and retained the hold. Fresh synchronized
PCC absence and independent reservation/configuration checks on all six routers
then confirmed that only Tunnel100 remained; only then was the hold released
and the operation marked `ROLLED_BACK`. This is observed deletion with an
uncorrelated report, not qualification of the correlated deletion profile.

The allocator was restored to disabled and the original non-initiation failover
listener restored. BGP-LS, PCEP, dashboard and Nginx services were healthy.
Public dashboard creation remains disabled. Raw console logs and controller
snapshots are private, ignored artifacts; sanitized wire fixtures and their
provenance are recorded alongside the existing regression evidence.

## Operator flow

The operator selects **headend**, **destination** and **bandwidth in Mbps**,
reviews the generated configuration preview when using the configuration
backend, then clicks **Create** once. Headends come from a server-side, qualified PCC
allowlist; destinations come from known topology nodes. For example, PE1
`10.255.0.1` to PE3 `10.255.0.6` with `1` Mbps. Free-form router addresses,
commands and arbitrary filesystem paths are not accepted.

The form displays the enforced policy: numbered IPv4 RSVP-TE, strict hops,
minimum TE metric, no affinity constraint, and configured setup/hold priorities
(initial proposal: 7/7). These defaults need no additional fields. Bandwidth
uses decimal Mbps with up to six decimal places, converted exactly to integer
bits/s. The configuration backend rounds the RSVP reservation upward to whole
kilobits/s within policy and displays the actual value before approval. The
controller allocates a unique managed name. The configuration backend assigns
a bounded unused tunnel ID; native initiation asks the PCC to allocate it.

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

## API and execution boundary

The separate `TunnelAPI` implements these endpoints behind an explicitly
configured Unix listener. The default dashboard serves only disabled options;
the current proxy permits only read methods. Public operator routing and an
exact qualified device profile remain deployment gates.

| Endpoint | Behavior |
|---|---|
| `GET /api/tunnel-options` | Authorized headends, destinations, policy defaults, qualification and source-health status. |
| `POST /api/tunnel-plans` | Configuration backend only: validate intent and return a generated expiring configuration/path preview without router delivery or capacity reservation. Uses the same operator session, Origin and CSRF boundary as creation. |
| `POST /api/tunnels` | Validate intent and, for configuration provisioning, its exact `approved_plan_id`; create or recover an idempotent operation, then admit/send through the selected backend's guarded execution boundary. |
| `GET /api/tunnel-operations/{id}` | Authorized operation status, requested/computed/reported route, evidence and failure reason. |

Example intent, with a session-bound CSRF token and an `Idempotency-Key` header:

```json
{"headend":"PE1","destination":"PE3","bandwidth_bps":1000000}
```

This is the preview schema and native initiation schema. Configuration
creation adds exactly one field, `"approved_plan_id"`, containing the 64 lower
case hexadecimal characters returned by the preview. Caller-supplied rendered
configuration, command lists or other extra fields are rejected. Options expose
`backend`, `requires_plan`, qualification, maximum bandwidth and the configured
outstanding-operation limit. Native initiation does not gain a preview route.

Return `202 Accepted` with an operation ID and status URL, never an immediate
"tunnel created" claim. Reject invalid fields, unauthorized endpoints, disabled
creation or unsupported platform profiles before scheduling any write. A
well-formed intent can still fail admission without sending protocol bytes.

One click authorizes this constrained creation intent under the displayed
operator policy and, for configuration provisioning, the exact displayed CLI
plan. It does not manufacture approval for an unrelated CLI plan.
Persist the authenticated principal, exact intent, effective policy revision,
computed plan digest and its expiry. A shared execution boundary must enforce
that authorization against the exact wire plan immediately before sending.
The configuration adapter retains the existing exact-plan approval rules; it
does not provide raw CLI execution from the dashboard.

The worker must atomically lock and admit against fresh directed TE data plus
all controller holds, persist the pending reservation and ownership intent,
then recheck topology digest/freshness, PCC session/synchronization, negotiated
setup type, authorization, writer fencing and kill switch inside the per-PCC
writer lock immediately before sending. Recompute/reject if those inputs
change before the write; never send an old
plan. Unknown bandwidth, ambiguous hop identity, incomplete sources or another
writer's unresolved operation block admission. The current live BGP-LS feed is
not an authoritative reservation ledger. `CreationManager` combines its fresh
directed bandwidth-priority vectors with a shared durable ledger, conservatively
subtracting all controller holds. Do not grant unproven self-credit;
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
| `APPLIED_UNVERIFIED` | Delivery was acknowledged; configuration delivery or a correlated PCRpt alone does not establish RSVP verification. A native report may still show DOWN. |
| `VERIFIED` | Configuration profile only: full independent headend identity, destination, bandwidth, ERO and signaling checks passed, with initial PCEP control confirmed. The UI says RSVP verified; payload forwarding remains unverified. |
| `VERIFICATION_FAILED` | Evidence conflicts, is incomplete or misses the deadline; retain holds unless absence/release is proven. |
| `ROLLBACK_REQUIRED` | Delivery/recovery is uncertain and needs controlled reconciliation or operator intervention. |
| `ROLLED_BACK` | An owned-object cleanup was independently confirmed, including resource release. |

Prewrite validation/no-path failures show their reason without implying that
anything reached a router. SRP correlation is scoped to the current session and
full LSP identity; historical/synchronization SRP repeats are not acknowledgments.
Reported ACTIVE alone cannot reach VERIFIED. MPLS OAM and any traffic test appear
as separate dated evidence: RSVP reservation does not prove payload throughput,
and the lab's license limits preclude a 50 Mbps throughput claim.

## Native initiation qualification procedure on CSR1000v 16.9.7

The first creation attempt above exercised the private listener. Complete the
remaining checks before enabling a public native initiation profile. Initiation is
opt-in, scoped to explicitly configured creation headends.

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

If RSVP PCInitiate is rejected or bandwidth is ignored, keep native creation disabled
for this profile. A qualified newer image/vendor or a separately qualified
configuration-based provisioner is an alternative project milestone; the
existing preconfigured-tunnel PCUpd workflow must retain its distinct label.

## Private lab execution options

`pcep-listen` requires all existing live computation options plus these
explicit creation arguments; they are absent from normal service templates:

```text
--creation-state /home/ubuntu/ninja-pce/build/state/creations
--creation-socket /run/ninja-creation/control.sock
--creation-headend PE1=10.255.0.1
--creation-destination PE3
--creation-max-bandwidth 1000000
```

Use a process-owned mode0700 socket directory. A systemd deployment should
prepare it with `RuntimeDirectory=ninja-creation` and
`RuntimeDirectoryMode=0700`, so service shutdown/reboot removes stale sockets.
The listener refuses to overwrite an existing socket. Reconcile any old
process and durable operation before removing its socket or restarting.
Creation state belongs on persistent storage and must survive restarts.

The private mode0600 console accepts one bounded JSON line per connection:
`create`, `get`, `list`, `complete-owned`, `reconcile-owned` and `remove-owned`.
Create requires an idempotency key and exactly the three intent fields above.
The other mutation experiments require the authenticated local principal's
existing operation ID. There is no arbitrary command, wildcard delete, public
cleanup route, automatic completion or automatic creation retry.

`NINJA_PCE_PREVIEW_ONLY=1` blocks creation, completion and cleanup at the
execution boundary. Unknown delivery retains its entire directed hold. Manual
absence recovery requires a fresh complete PCC session and independently
checked headend/configuration and transit reservation-release evidence; neither
browser disappearance nor a removal report alone refunds capacity.
