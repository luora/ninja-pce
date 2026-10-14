# RSVP-TE rendering and operational evidence

These are documentation-checked configuration candidates and conservative CLI
parsers, **not a platform certification or a CLI provisioning qualification**. All files
under `tests/fixtures/operational/*.mock.txt` are synthetic mocks. The golden
render fixtures describe intended configuration text; they are not router output.
Those initial renderer/parser checks did not contact a router. Subsequent
[live GNS3 qualification](live_pcep_computation_validation.md) exercised
BGP-LS and initial delegated PCEP paths on CSR1000v IOS XE 16.9.7. That
protocol qualification is separate from CLI provisioning. Captured IOS XE
16.9.7 operational output now supports the narrow evidence dialect described below.
One subsequent [configuration-provisioning trial](dashboard_configuration_validation.md)
created a 1 Mbps PE1-to-PE3 tunnel with initial PCEP control and independently
observed RSVP reservations. This result applies only to that bounded lab profile.

The separate [OSPF multi-area lab profile](multi_area_lab.md) passed underlay,
isolated BGP-LS ingestion and local PE1-to-PE4 path computation checks on IOS XE
16.9.7. It requires the documented GoBGP identity correction and explicit ABR
bindings. That result does not extend tunnel delivery, signaling or forwarding
qualification across areas.

| Adapter | Documentation / candidate release scope | Numbered IPv4 strict ERO | Operational evidence dialect |
|---|---|---|---|
| `iosxr` | ASR 9000 IOS XR 7.9.x; XRv/XRv9000 requires independent image validation | `index N next-address strict ipv4 unicast REMOTE_INTERFACE_IP` | Version 7.9.x, one tunnel identity/destination, admin/oper up, valid path, connected signaling, bandwidth, current LSP ERO/Tspec and Resv Fspec |
| `iosxe` | IOS XE 17.12.x and exact observed 16.09.07 / 16.9.7 | `next-address strict REMOTE_INTERFACE_IP` | One tunnel identity/destination, admin/oper up, valid path, connected signaling, bandwidth, current RSVP Path ERO/Tspec and Resv Fspec |
| `junos` | Junos MPLS documented syntax; parser restricted to 23.4R | `set protocols mpls path NAME REMOTE_INTERFACE_IP strict` | Version 23.4R, one LSP identity/destination, LSP up, active primary up, bandwidth, computed ERO and received RRO |
| `generic` | Descriptive output, no delivery support | Explicit selected link, address and hop type | None |

IOS XR uses interface `destination`, `signalled-bandwidth`, `priority` and
`path-option`, rather than IOS XE's `tunnel mpls traffic-eng ...` hierarchy.
Junos `primary NAME` references the MPLS path named NAME directly. The legacy
`junos_primary_path_name` input remains accepted for compatibility, but the
explicit-path object's name is the effective primary path reference.

Strict hops use the selected directed edge's remote numbered IPv4 interface
address. The renderer checks hop/link identity, and refuses missing addresses,
unsupported IPv6/unnumbered strict encodings and ambiguous parallel adjacencies
that share the same remote address from one source. It never substitutes a
loopback. Loose hops use router loopbacks and preserve the loose keyword; they
cannot prove the computed directed physical links, so this adapter does not
verify loose paths for automatic reservation confirmation. All selected links
must lie in the head-end's usable TE visibility/signaling domain. Multi-area
reachability cannot be inferred from a YAML graph alone.

The canonical demand is integer bit/s. Cisco reservations round upward to whole
kbps; rendering rejects a rounded value exceeding any selected link's effective
available bandwidth. The shared ledger reserves the actual signaled amount.
Junos emits an integer bit/s value. Customer CIR and service binding remain
separate operator functions.

## IOS XE 16.9.7 candidate supplied for GNS3 preparation

On 2026-10-10 the operator supplied partial `show version` output identifying
IOS XE `16.09.07` and IOS Software `16.9.7` (Fuji, Virtual XE,
`X86_64_LINUX_IOSD-UNIVERSALK9-M`). Empty PCE client LSP/peer tables are not
operational RSVP-TE evidence. Model/PID, license, interface mapping, TE
advertisements, reservations and established tunnel state were unqualified
at that first inspection. Later manual and PCEP lab results are recorded in
the [live qualification](live_pcep_computation_validation.md).

The [Fuji 16.9 TE documentation](https://www.cisco.com/c/en/us/td/docs/ios-xml/ios/mp_te_path_protect/configuration/xe-16-9/mp-te-path-protect-xe-16-9-book/mpls-traffic-engineering-bfd-triggered-fast-reroute.html)
provides release-family command references, with platform restrictions; it does
not certify this unidentified virtual appliance. Use the initial setup and
read-only capture procedure in [GNS3 validation](gns3_validation.md). The
separate two-router profile is for offline preview and lab qualification only.

The IOS XE evidence parser accepts **17.12.x and exactly 16.09.07 / 16.9.7**;
other Fuji patches remain rejected. Its 16.9.7 fixtures include a captured
50 Mbps Tunnel100 and a captured zero-bandwidth PCE-created auto-tunnel.
The parser accepts the observed multiword tunnel description and optional
`Ifhandle` header, reads the current RSVP Path and Resv sections, and ignores
the separate unconstrained shortest-path display. This image appends the
destination loopback to the RSVP ERO display; only that exact terminal endpoint
is normalized after matching every selected strict hop. Both Path Tspec and
Resv Fspec must match the intended reservation. Fixture provenance and
sanitization are recorded in
[`tests/fixtures/operational/README.md`](../tests/fixtures/operational/README.md).

Recognizing this operational dialect does not qualify automatic CLI provisioning
or enable dashboard creation. An exact approved CLI plan is still required, and
any dashboard provisioning service must keep its independent qualification gate
closed until its exact delivery workflow passes a controlled live test. The
bounded PE1-to-PE3 1 Mbps profile has
[one such recorded result](dashboard_configuration_validation.md); shipped
configuration examples remain disabled by default.
The separate PCEP listener supports observation, a bounded PCReq profile, and
opt-in initial delegated PCUpd; see [its implemented scope](pcep_computation.md).

## Delivery boundary and recovery

`build_push_plan` hashes the topology's directional resources and provenance,
demand, inventory device and exact rendered configuration/commands. Node/link
input ordering does not change identity. `push_plan` requires `execute=True`,
`approved=True`, and `approved_plan_id=plan.plan_id` (or the `approved_plan_hash`
alias), then rechecks integrity immediately before configuration. Optional
`approval_expires_at` is an aware datetime/ISO timestamp. Reconciliation also
checks current file revisions, because an isolated plan cannot observe a file
change by itself. `NINJA_PCE_PREVIEW_ONLY` blocks every write at this boundary.
An optional `qualified_versions` tuple narrows the supported evidence dialects
to exact releases approved for a delivery profile. It is checked against the
router's current version before configuration, so recognizing another release
does not silently extend that profile after a software upgrade.

`build_pce_push_plan` is an explicit IOS XE variant for strict numbered IPv4
paths without affinity constraints. It adds primary path-option 1 using the
specified PCE address, sets affinity and mask to zero, and retains the selected
explicit path-option 10 as fallback. These commands are part of the exact
reviewed plan identity; ordinary `build_push_plan` output remains explicit-only.
The CLI postcheck proves RSVP state, bandwidth and current route. PCE selection,
delegation and authorization of the new tunnel identity require separate
evidence and controller policy; the configuration helper cannot establish them.

The driver executes bounded read-only version, destination route and scoped
configuration prechecks, captures state and a diff, and rejects unknown releases
before writing. It accepts only complete command grammars and documented Cisco
submodes. Connection and command timeouts are explicit. Writes are not retried;
verification retries are bounded to at most five reads and a total configured
I/O budget of 120 seconds. Retries are immediate and do not impose a convergence
sleep; operators should use read-only revalidation if establishment takes longer.

**Existing TE objects require manual ownership-aware replacement.** The current
adapter refuses a detected pre-existing tunnel/LSP or path before configuration:
a merge can retain obsolete trailing ERO hops, and deleting an object without
ownership/dependency evidence is unsafe. This deliberately limits automated
rerouting of an already configured LSP. An ownership-aware, transactional replace
adapter is future work. Unchanged desired state is still revalidated read-only
by `verify_plan`; a no-op never implies router health.

IOS XR candidates use a 300-second confirmed commit. Junos runs commit-check and
a five-minute commit-confirmed. A second permanent commit occurs only after
positive operational verification. IOS XE has immediate configuration semantics.
Commit/transport uncertainty returns `ROLLBACK_REQUIRED`, preserving pending
reservations. Operational evidence failure returns `VERIFICATION_FAILED` and
never updates last-verified state. The temporary commit may expire; this is not
reported as `ROLLED_BACK` without fresh observation. No blind rollback deletion
is implemented. Manual recovery must review the captured prior state and resource
ownership, then verify actual forwarding before releasing a reservation.

`PushResult` uses `PLANNED`, `PUSHING`, `APPLIED_UNVERIFIED`, `VERIFIED`,
`VERIFICATION_FAILED`, `ROLLBACK_REQUIRED` and `ROLLED_BACK` state vocabulary;
intermediate/persistent transitions are maintained by reconciliation. A successful
Netmiko call alone never yields `VERIFIED`. Unsupported, ambiguous, truncated or
missing fields fail closed. The current CLI dialect intentionally rejects other
valid formatting variants until backed by reviewed fixtures. It does not prove
service traffic steering, QoS/CIR, intermediate-router telemetry, protection
switch behavior or forwarding correctness end to end. Fixtures must be extended
with redacted real lab captures before any platform deployment claim.

Credentials use inventory environment references. Passwords and common sensitive
response fields are redacted from retained driver output and exceptions. No
credential/session log is enabled by the adapter. The pre-change diff is scoped
to TE resources, never the complete device configuration.

## Primary references checked

- [Cisco ASR 9000 IOS XR 7.9.x MPLS TE configuration guide](https://www.cisco.com/c/en/us/td/docs/routers/asr9000/software/asr9k-r7-9/mpls/configuration/guide/b-mpls-cg-asr9000-79x/implementing-mpls-te.html): tunnel interface hierarchy, signaled bandwidth, explicit paths and operational display.
- [Cisco IOS XE 17.x MPLS TE path protection guide](https://www.cisco.com/c/en/us/td/docs/routers/ios/config/17-x/mpls/b-mpls/m_mp-te-path-prot-0.html): IOS XE explicit path and tunnel hierarchy.
- [Cisco MPLS command reference, next-address](https://www.cisco.com/c/en/us/td/docs/ios-xml/ios/mpls/command/mp-cr-book/mp-m4.pdf): strict/loose next-address grammar.
- [Juniper Basic LSP Configuration](https://www.juniper.net/documentation/us/en/software/junos/mpls/topics/topic-map/basic-lsp-configurtion.html): numbered strict/loose paths and named primary path association.
- [Juniper show mpls lsp reference](https://www.juniper.net/documentation/us/en/software/junos/cli-reference/topics/ref/command/show-mpls-lsp.html): operational fields and ERO/RRO evidence.
- [Netmiko IOS XR adapter source documentation](https://ktbyers.github.io/netmiko/docs/netmiko/cisco/cisco_xr.html) and [Junos adapter source documentation](https://ktbyers.github.io/netmiko/docs/netmiko/juniper/juniper.html): timeout-aware commit and confirmed-commit interfaces.

Original adapter references checked on 2026-10-09; IOS XE 16.9 candidate
references checked on 2026-10-10. Release-family syntax does not establish that
a particular image, license, virtual dataplane or hardware supports RSVP-TE.
