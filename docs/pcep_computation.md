# PCEP computation, initial provision and bounded recovery

Ninja-PCE has separate PCEP profiles: default passive observation,
opt-in PCReq/PCRep path advice, and a further opt-in for an initial PCUpd to
an explicitly authorized PCC and tunnel. Another opt-in profile provides
[one-tunnel withdrawal recovery](pcep_failure_recovery.md), with unit/socket
coverage and [one qualified IOS XE 16.9.7 lab trial](pcep_failure_recovery_validation.md).
The PCC performs RSVP signalling and admission. A computed route, a TCP write
and a protocol acknowledgment
establish different facts; none alone proves reservation or forwarding.

One live initial delegated PCRpt/PCUpd provision to PE1 Tunnel100 succeeded
on 2026-10-10: PE1 → P1 → P2 → PE3, 50 Mbps RSVP admission, metric 30,
corroborated by router state, transit reservations and MPLS OAM. The
[live computation validation record](live_pcep_computation_validation.md)
contains evidence and the historical capture caveat, plus local checks and
successful exact-checkout requalification. That later new initial request
selected PE1 → P3 → P4 → PE3 against its fresh TED; the record preserves both
observations. Active reoptimization PCReq remains unsupported.
The earlier [protocol observation](live_protocol_setup.md) and
[manually provisioned Tunnel100](live_tunnel_validation.md) establish separate
results and do not validate these new computation profiles.

## Default observation

Without computation flags, `pcep-listen` maintains allowlisted sessions and
captures LSP reports. It advertises stateful capability with update capability
clear (`U=0`) and initiation capability clear. It sends no path updates or
initiation messages. P1 remains an observation peer in this lab.

The IP allowlist identifies the permitted TCP source; it is not cryptographic
authentication. Bind to the isolated lab NIC. Authenticated PCEPS remains
future work.

## PCReq/PCRep profile

Computation requires an authorized PCC-IP to head-end-loopback binding and
fresh, healthy BGP-LS inputs. An allowed reporting peer without that binding
cannot request a path for another source. Each request describes one
unidirectional, numbered IPv4 RSVP-TE path.

| Object | Implemented local profile |
|---|---|
| RP | Required processing (`P=1`); nonzero request ID unique within the batch. Reoptimization, bidirectional requests, assigned unsupported flags and RP TLVs are rejected. The O flag permits loose paths; the reply remains strict. |
| END-POINTS | Required processing, IPv4 source and destination. Source must equal the TCP peer's authorized head-end loopback; endpoints must identify unique TED nodes. |
| BANDWIDTH | Required class 5/type 1, finite positive bytes/s, rounded upward after conversion to integer bits/s. Missing or zero bandwidth is rejected by local policy. |
| LSPA | Optional exclude/include-any/include-all masks and setup/hold priorities 0–7. Without LSPA, priorities are 0/0. Setup must be numerically at least hold. Protection requests and LSPA TLVs are unsupported. |
| METRIC | TE metric type 2 only; default objective is minimum additive TE cost. Upper bounds round downward to integer metric units. The first metric for each objective/bound form applies. The reply includes computed TE cost. |
| OF | Minimum-cost-path objective code 1 only. Explicit OF or RP supply-objective requests produce an OF in the reply. OF TLVs are unsupported. |
| LSP | Optional identification context with symbolic-name/IPv4-identifiers TLVs only; no delegation, ownership or existing-reservation credit. |

Unsupported classes/types and extensions are rejected by this narrow local
profile, including unsupported optional objects. This is a local restriction,
not a claim that the base protocol requires rejection of every optional
extension. Unassigned reserved flag bits are ignored where required.

Affinities use advertised administrative-group positions, represented as
`bit-0` through `bit-31`. Admission uses the full bandwidth-priority vector
with nonpreemptive availability. The ERO contains each selected directional
link's remote interface address as a strict IPv4/32 hop. It does not invent
addresses or append the destination loopback; endpoints identify destination.

IPv6, unnumbered hops, segment-routing paths, IRO/RRO constraints, synchronized
or disjoint requests and load balancing are unsupported. Positive PCReps are
independent advice and do not reserve capacity. Only exhaustive CSPF
infeasibility produces NO-PATH; unavailable topology, unsupported constraints
and search-budget exhaustion produce errors.

The running IOS XE 16.9.7 has emitted PCReq after delegation was returned for
an active tunnel. That captured request includes reoptimization, RRO,
a PATH-SETUP-TYPE RP TLV and bandwidth type 2. It is outside this profile and
is rejected. Earlier absence of PCReq does not establish that the router
cannot emit it.

## Opt-in initial delegated path

`--compute-delegated-tunnel PCC-IP=TUNNEL-ID` enables a separate, bounded
initial-path adapter. It requires all computation inputs and an authorized
head-end binding. Only those PCCs receive update capability `U=1`; both sides
must advertise it before delegation is accepted. Initiation capability stays
clear. The original single-area qualification authorized PE1 Tunnel100 only.
The later [manual inter-area tunnel trial](manual_tunnel_delegation.md)
separately authorized Tunnel1003. Temporary Tunnel101 used during the original
qualification is not a persistent authorization.

The adapter considers a synchronized, live PCRpt only when:

1. The PCC delegates a nonzero PLSP-ID (`D=1`), requests administrative UP,
   reports operational DOWN, supplies an empty intended ERO and has not
   removed the LSP. The report is unsolicited (`SRP-ID=0`).
2. IPv4 LSP identifiers match the authorized source and tunnel ID, with a
   destination known to the current TED. No recorded route is present.
3. Explicit positive requested BANDWIDTH and LSPA supply the constraints.
   Used bandwidth, if reported, must match requested bandwidth exactly;
   it supplies no reservation credit.
4. The objective is unconstrained TE cost. Duplicate/unsupported attributes
   and extension forms refuse provision. Only narrowly checked extension
   forms captured in this IOS XE lab are accepted; opaque label data does
   not authorize label changes.
5. Report identity, flags, route and attributes remain current after
   computation, and the BGP-LS source still passes validation.

The PCUpd contains a fresh SRP ID, the existing PLSP-ID, `D=1`, a strict
numbered ERO and requested LSPA/bandwidth/TE attributes. It supplies an initial
route to an already configured PCC tunnel. It does not create a tunnel or
update an active route. With only this initial-path profile enabled, later
reports are observed without repeatedly recomputing or rerouting that LSP.

A refused delegation receives an empty PCUpd with `D=0`, including SRP, LSP
and an empty ERO object. This returns control to the PCC. It is not rollback
or a guarantee that the path remains unchanged: PCC fallback policy controls
subsequent behavior. A `D=1` update with an empty ERO has different semantics
and is not a generic refusal. Delegation returns are not pending positive
initial-path updates for timeout purposes.

[RFC 8231](https://www.rfc-editor.org/rfc/rfc8231.html), Sections 5.4, 5.7,
5.8.3, 6.2 and 7.2–7.3, supplies the protocol basis. A delegated LSP uses
PCRpt/PCUpd; it cannot issue PCReq while that delegation remains in effect.
The [Cisco 16.9 Dynamic PCC guide](https://www.cisco.com/c/en/us/td/docs/ios-xml/ios/seg_routing/configuration/xe-16-9/segrt-xe-16-9-book/sr-dynamic-pcc.html)
documents RSVP-TE and SR-TE delegation, but its examples do not establish this
lab's exact interoperability.

## Opt-in withdrawal recovery

The [failure recovery runbook](pcep_failure_recovery.md) defines a separate
`--failover-tunnel`/`--failover-state` profile for one already authorized tunnel.
It first maps a synchronized, delegated UP/ACTIVE strict numbered path, then
requires a later validated withdrawal or explicit down state of a selected
edge. Stable failure evidence can trigger a replacement PCUpd with unchanged
endpoints, bandwidth, priorities and LSPA. No existing-reservation credit or
preemption is granted.

Healthy-path metric/bandwidth changes and restoration do not trigger updates.
A durable journal records intent before sending and retains uncertain pending
operations across restart for manual review; it does not integrate PCEP with
the shared reservation ledger or establish durable delegated ownership.
`RECOVERED` requires correlated processing and matching reported route/state;
it is control-plane convergence, not independent forwarding proof.

On 2026-10-11, a P1 → P2 withdrawal moved PE1 Tunnel100 from
PE1 → P1 → P2 → PE3 to PE1 → P3 → P4 → PE3 through exactly one failure PCUpd.
Requested bandwidth remained 50 Mbps with priorities 7/7. Correlated DOWN then
ACTIVE reports were corroborated by transit RSVP reservations, MPLS echo 5/5
and a matching trace. Restoring the failed interface did not trigger switchback.
The [dated qualification record](pcep_failure_recovery_validation.md) contains
the exact topology revision and evidence. This single trial does not qualify
general autonomous recovery, other images or the remaining failure/restart cases.

The default observer and initial-only configuration retain their existing
behavior. General active optimization, active PCReq reoptimization and
PCInitiate remain unsupported for general operation. Dashboard
[configuration provisioning](dashboard_provisioning_deployment.md) is a
separate implemented profile: its SSH adapter creates an owned router
configuration and grants initial PCEP authority through the exact operation
journal. A manually created interface does not acquire that ownership merely
by using a tunnel number in the dashboard's allocation range.

## Freshness, session scope and limits

Raw capture, checksummed collector status and normalized topology must refer
to the same capture timestamp and required BGP-LS exporter. P1 must be
Established with End-of-RIB received. Status and source data must be no older
than 30 seconds. Failed/stopped collectors cannot authorize positive paths.
Exact raw snapshot digest and health are rechecked after computation; source
changes or failure refuse the pending path.

Bounds are eight requests per message, 512 request objects, 1 MiB per
collector input file, 256 nodes, 2,048 directed edges and 100,000 search
expansions per request. The server permits four workers and one pending batch
per PCC. Its five-second response deadline does not kill a running thread:
the worker retains its slot until it exits. Late results cannot be delivered
through a closed or replaced session.

Delegation attempts and SRP IDs belong to the current session. SRP IDs increase
from nonreserved values without reuse. Retained updates are bounded; the
server closes before exhausting its supported sequence/history. Nonzero SRP
IDs during synchronization, and unknown IDs after it, are diagnostic context
and cannot acknowledge an operation. A known current ID must match the
pending operation's tunnel identity; acknowledgment never grants authority. Revocation,
removal or report changes during computation suppress the pending update.
Restart/session loss discards live ownership authority. Captures are evidence,
not durable authorization to resume control.

The first correlated PCRpt acknowledges PCUpd processing. Later progress may
use SRP zero. Capture records acknowledgment and state separately: an
acknowledgment can report DOWN or GOING-UP and does not prove RSVP admission.
Unacknowledged positive initial updates cross a 60-second threshold checked
by the keepalive task, are marked `ACK_TIMEOUT` and close the session. Timeout
is uncertain, not proof that the router did nothing; inspect current PCC
state before another attempt.

## Persistent lab deployment

Use the reviewed `/home/ubuntu/ninja-pce` checkout. `ninja-bgpls` publishes
matching `build/live/bgpls-raw.json`, `build/live/bgpls-status.json` and
`build/live/topology.json`. Review options, collector health and the repository
drop-in before enabling it:

```bash
cd ~/ninja-pce
.venv/bin/ninja-pce pcep-listen --help
systemctl status ninja-bgpls ninja-pcep --no-pager
python3 -m json.tool build/live/bgpls-status.json
cat gns3/controller/ubuntu-24.04/ninja-pcep-compute.conf
```

The drop-in binds `172.16.100.10:4189`, allows PE1 `10.255.0.1` and P1
`172.16.100.102`, supplies all three collector inputs and the node map, and
includes:

```text
--compute-bgpls-peer 172.16.100.102
--compute-headend 10.255.0.1=10.255.0.1
--compute-delegated-tunnel 10.255.0.1=100
```

`--compute-headend` means **PCC TCP source IP = requested head-end loopback**.
By itself, the tunnel binding authorizes initial PCUpd, not every reported LSP
or withdrawal recovery. The latter requires its separate explicit opt-in.
Do not combine this peer's delegation binding with `--stateless-peer`;
delegation requires stateful capability. P1 has no initial-path authorization.

Install the reviewed repository drop-in:

```bash
sudo install -D -m 0644 \
  gns3/controller/ubuntu-24.04/ninja-pcep-compute.conf \
  /etc/systemd/system/ninja-pcep.service.d/10-compute.conf
sudo systemctl daemon-reload
sudo systemctl restart ninja-pcep
systemctl cat ninja-pcep
systemctl status ninja-pcep --no-pager
journalctl -u ninja-pcep --no-pager -n 60
```

Restart closes existing sessions; PCCs must reconnect and synchronize. Keep
one listener on TCP/4189. The default service without the drop-in remains an
observer. The feed remains non-authoritative for guarded autonomous CLI
delivery; explicitly enabling PCEP computation does not change provenance.

## Qualification evidence

Retain router configuration and current tunnel state before testing. Capture
negotiated OPEN flags and actual PCReq or delegated PCRpt bytes. These
exchanges have different identifiers and success criteria; an IOS log alone
does not establish the wire request.

For each test retain:

1. Fresh raw BGP-LS, collector health and directional bandwidth vectors from
   the revision used for computation.
2. Received bytes and decoded endpoints, bandwidth, priorities, affinities,
   objective and request/LSP identity.
3. PCRep/PCUpd bytes, correlated request/SRP identifier and every strict hop
   checked against the selected directional links.
4. For PCUpd, correlated acknowledgment and subsequent LSP state/error
   reports, recorded separately from operational verification.
5. Router tunnel state, RSVP reservations along the route, ERO/RRO and MPLS
   forwarding/OAM evidence.

Existing and temporary tunnels change advertised available bandwidth. Use the
current TED instead of an earlier empty snapshot or assumed preferred path.
Positive replies do not serialize competing admissions; RSVP handles resource
races.

[RFC 5440](https://www.rfc-editor.org/rfc/rfc5440.html) defines PCReq/PCRep;
[RFC 3209](https://www.rfc-editor.org/rfc/rfc3209.html) defines RSVP-TE admission
and signalling. Label local codec/socket tests separately from live evidence.
General active optimization, PCReq reoptimization, PCInitiate, durable delegated
ownership/recovery, reservation ledger integration, preemption and authenticated
transport remain future work. Additional recovery scenarios still need live
qualification; the recorded withdrawal and historical initial-provision evidence
do not qualify broader recovery.
