# Live PCEP initial path validation

On **2026-10-10 at 20:12:09 UTC / 23:12:09 Africa/Nairobi**, Ninja-PCE
provided a computed RSVP-TE route to the existing PE1 Tunnel100 using
**PCRpt/PCUpd**. PE1 installed the PCE dynamic path as its primary path.
Independent router state, transit RSVP reservations, MPLS OAM and subsequent
BGP-LS advertisements corroborated **PE1 → P1 → P2 → PE3**, 50 Mbps at
priorities 7/7, with TE metric 30.

This validates one explicitly authorized initial-path provision. It does not
qualify general active rerouting, controller-initiated tunnels, an
authoritative reservation ledger or 50 Mbps traffic throughput. The
[computation guide](pcep_computation.md) describes the implemented profiles;
the [structured record](live_pcep_computation_validation_results.json)
contains exact identities, timestamps, evidence hashes and limitations.

Implementation `dbea66c5423cc3954205d74cd4a3c4982699d4c8` passed 583 tests
(85% coverage), its final affected run passed 196 tests, and Ruff/mypy passed
for 19 source files. That exact checkout was deployed and requalified with a
new PCC initial request, which selected **PE1 → P3 → P4 → PE3**. The
historical SRP 3 proof below and the final deployed qualification are separate
observations; the latter is the current route at the recorded final check.

## Scope and retained configuration

The lab uses six Cisco CSR1000v routers running IOS XE 16.9.7 and the Ubuntu
controller at `172.16.100.10`. P1 `172.16.100.102` supplies live BGP-LS.
PE1 connects to PCEP from `10.255.0.1`; its destination is PE3 `10.255.0.6`.
The earlier [manual Tunnel100 validation](live_tunnel_validation.md) used
PE1 → P3 → P4 → PE3 and remains a separate historical result.

Tunnel100 was retained throughout this stage. Only the PCE primary path
option was reapplied and an operator EXEC reoptimization triggered the
qualification. The existing interface settings and explicit fallback option
10, `EP_CUSTOMER_A_PE1_PE3`, remained intact. No Tunnel100 deletion or
recreation occurred. A separate temporary Tunnel101 used to qualify the
initial exchange was removed before final configuration was saved.

The final additive router fragment is
[PE1-pce-tunnel100.cfg](../gns3/router-configs/ios-xe/16.9.7-six-router/underlay/PE1-pce-tunnel100.cfg).
It requires the existing underlay and
[explicit Tunnel100 baseline](../gns3/router-configs/ios-xe/16.9.7-six-router/underlay/PE1-tunnel100.cfg).
The resulting retained configuration includes:

```text
mpls traffic-eng pcc peer 172.16.100.10 source 10.255.0.1 delegate-only
mpls traffic-eng pcc report-all
interface Tunnel100
 tunnel mpls traffic-eng path-option 1 dynamic pce address 172.16.100.10
 tunnel mpls traffic-eng path-option 10 explicit name EP_CUSTOMER_A_PE1_PE3
```

The saved interface also retains destination `10.255.0.6`, bandwidth
`50000` kbps and priorities `7 7`. Autoroute remains disabled. The final
console capture contains `write memory` followed by `[OK]`, and its tunnel
summary contains one head-end tunnel. The controller's persistent initial
provision allowlist is PE1 Tunnel100 only; Tunnel101 is not retained there.

## Actual exchange and computed route

The exact computation input was `bgpls-before-final.json`, captured at
**20:12:07.093159 UTC**. That timestamp equals the SRP 3 computation outcome's
`topology_timestamp`; the later reservation snapshot is not substituted as
the input. The graph contained six nodes and 16 directed links.

PE1's initial report identified Tunnel100, PLSP-ID **524388 / `0x80064`**,
RSVP LSP-ID **224**, and symbolic name `Tunnel100_w`. It requested
administrative UP while reporting operational DOWN, delegated the LSP
(`D=1`), supplied an empty ERO and used unsolicited SRP zero. Requested
bandwidth was 6,250,000 bytes/s, or 50,000,000 bits/s, with explicit LSPA
priorities 7/7.

The actual controller capture contains this sequence for that incarnation:

| UTC on 2026-10-10 | Direction | Message | SRP | State / result |
|---|---|---|---:|---|
| 20:12:09.803614 | PE1 → PCE | PCRpt | 0 | DOWN, delegated, empty ERO |
| 20:12:09.974315 | PCE → PE1 | PCUpd | 3 | Delegation confirmed; strict computed ERO |
| 20:12:09.997983 | PE1 → PCE | PCRpt | 3 | DOWN; update processing acknowledged |
| 20:12:10.059688 | PE1 → PCE | PCRpt | 3 | ACTIVE with the computed ERO |
| 20:12:13.000138 | PE1 → PCE | PCRpt | 3 | ACTIVE repeated with the same SRP |

The PCUpd supplied these strict IPv4/32 receiving-interface hops:

| Directed link | Strict hop | Directional link ID |
|---|---|---|
| PE1 → P1 | `10.0.12.2` | `bgpls-link-e5e84bf629fb8dbedc0d27f8b45cc9ba` |
| P1 → P2 | `10.0.25.5` | `bgpls-link-f236c6993d24c8e9bd58d6ebfdb7390d` |
| P2 → PE3 | `10.0.56.6` | `bgpls-link-de9490d87876a12654bbf88e016d7f82` |

The initial DOWN acknowledgment did not prove admission. The subsequent
same-incarnation ACTIVE reports and independent router observations establish
the stronger result. IOS XE repeated SRP 3 across the progress reports;
code must not assume that only the first report carries the operation ID.
The initial unsignalled report already carried nonzero RSVP LSP-ID 224.
These are captured image-specific behaviors, not a general conformance claim.

## Independent operational checks

`PE1-T100-primary-verified.txt` records Admin UP, Oper UP, Path valid and
Signalling connected. Dynamic **path option 1 is active**, is the **Basis
for Setup**, and has path weight 30. Its PCEP information explicitly says
the working path was created through PCUpd from `172.16.100.10`, SRP 3.
The explicit fallback option 10 is still listed.

P1's reservation is 50M on `GigabitEthernet2` toward `10.0.25.5`; P2's is
50M on `GigabitEthernet4` toward `10.0.56.6`. PE3 records the matching 50M
tail reservation. Each reservation identifies source `10.255.0.1`,
destination `10.255.0.6`, tunnel 100 and LSP 224.

PE1's MPLS echo succeeded **5/5**. MPLS traceroute independently followed
`10.0.12.2 → 10.0.25.5 → 10.0.56.6`, matching the intended ERO.
The reports have no RRO; an intended route alone is not treated as forwarding
proof. The router's RSVP display appends destination loopback `10.255.0.6`
to the signalling route, while the PCUpd ERO contains the three interface
hops above.

At **20:12:38.817300 UTC**, `bgpls-T100-primary.json` showed the corresponding
directional reservation change:

| Forward path | Priority-7 unreserved before → after |
|---|---|
| PE1 → P3 → P4 → PE3 | 50 Mbps → 100 Mbps on each of three links |
| PE1 → P1 → P2 → PE3 | 100 Mbps → 50 Mbps on each of three links |

Priorities 0–6 remained 100 Mbps. Reverse directions and every other link
remained 100 Mbps at every priority; all 16 link identities were retained.
This is aggregate control-plane corroboration, not reservation ownership
attribution by BGP-LS.

## Final deployed checkout qualification

The exact reviewed implementation was deployed to Ubuntu and the listener
restarted. After delegation return, PE1 supplied a new DOWN/empty-ERO initial
request for LSP **225**. The controller sent PCUpd **SRP 2** in the new
session, selecting strict hops
`10.0.13.3 → 10.0.34.4 → 10.0.46.6` through P3 and P4.

`PE1-reviewed-build.txt` independently records dynamic option 1 active and
Basis for Setup, Admin/Oper UP, Path valid, Signalling connected, metric 30,
50,000 kbps and priorities 7/7. Fallback explicit option 10 remains present.
MPLS echo passed **5/5** and MPLS traceroute matched the three P3/P4 hops.

`pcep-reviewed-build.json` records both allowed PCC sessions ESTABLISHED,
fresh and synchronized. SRP 2 is acknowledged for the complete LSP 225
identity; `reported_state` and `latest_reported_state` both remain ACTIVE
after the old LSP 224 removal. It records the used source timestamp
**20:18:36.146957 UTC** and raw topology digest:

```text
fc9c05bf77d6326ae4eacf387b47890d898d5ef3b232720e80ad17011865b345
```

Final transit captures identify tunnel 100/LSP 225 reserving 50M on P3
`GigabitEthernet3` and P4 `GigabitEthernet2`. P1 has no RSVP reservations and
zero allocation on its RSVP interfaces. At **20:19:39.085352 UTC**,
`bgpls-reviewed-build.json` independently advertises 50 Mbps unreserved at
priority 7 on PE1→P3, P3→P4 and P4→PE3. Priorities 0–6, all reverse links
and every other edge remain 100 Mbps. Its SHA-256 is:

```text
e9b869e99420ff4702d5609099db62428c680c86b67d2600e64698d18e73416b
```

At this new computation, the P1/P2 route already advertised a 50 Mbps
reservation and received no existing-reservation credit. P3/P4 had greater
available bandwidth. This fresh selection followed a new initial delegation;
it is not evidence of automatic active-path rerouting. Its PCEP capture
SHA-256 is:

```text
65812c3df5334a14d79737cac3e51767b9524334a246643c3219fd1c3765e861
```

The final PE1 check shows one head-end tunnel, the PCEP peer UP and a
successful `write memory`. The deployed Ubuntu checkout was verified clean
at the exact implementation commit, with `gobgpd`, `ninja-bgpls` and
`ninja-pcep` active. Repository and installed computation overrides matched
SHA-256 `9cacf5890b94b8afcbd431ef36b0263774fb950babd11afb0566db0f9cd850e0`.

## Historical capture caveat and bounded behavior

The historical capture has an inconsistent summary: SRP 3 `reported_state`
is ACTIVE, but `latest_reported_state` became DOWN when the previous
make-before-break incarnation, LSP 223, was removed. Its raw LSP 224 PCRpt
remains ACTIVE, and the independent router, RSVP and OAM captures corroborate
that incarnation. Final source hardening separates old-incarnation removal
from the live tunnel's state and ownership. Final source also records the raw
topology digest; this older outcome identifies its input by capture timestamp.

The earlier SRP 3 PE1 show reports delegation clear (`D=0`) while the PCE-provided
dynamic route stays active. This record proves delivery and operation of the
initial path; it does not claim uninterrupted retained delegation. IOS can
also repeat historical SRP IDs across reconnection and after synchronization.
Synchronization IDs and unknown operation IDs remain diagnostic context.
A known current-session ID must match its pending operation's tunnel identity
to acknowledge processing; acknowledgment alone never grants authority.

PE1 did emit active reoptimization PCReq after delegation was returned.
That request contains an RP PATH-SETUP-TYPE TLV, reoptimization flag, RRO and
bandwidth type 2. The constrained PCReq profile rejects those unsupported
semantics. This validation therefore concerns initial delegated PCRpt/PCUpd,
not successful active reoptimization through PCReq/PCRep.

There is no PCInitiate, active-path update policy, durable ownership recovery,
authoritative reservation ledger, self-reservation credit or preemption.
The 50 Mbps value is RSVP admission, not measured packet throughput; the
earlier CSR license evidence reported a 1000 kbps throughput limit. IOS XE
16.9.7 autonomous CLI delivery is still a separate unqualified profile.

## Evidence provenance

Large original artifacts remain local ignored files under
`build/live-lab-evidence/pce-compute/`. Their exact hashes, raw exchange and
directional bandwidth vectors are retained in the structured record.
The three central JSON hashes were recomputed from the original bytes:

```text
62f128286e2c704dba3447032cd4bc7aedd60c262a3ce12e1d175826ab4a6250  pcep-T100-primary.json
0b5ac0abbd343da6244b8b40953511d87b0ceb3f115cce36ec7049b866fc27c8  bgpls-before-final.json
78305ce5a6715c55077bcb5ef140fb7af8337555742c16ebd1ce053cde387468  bgpls-T100-primary.json
```

Tracked historical fixtures under `tests/fixtures/pcep-iosxe-16.9.7/` and
`tests/fixtures/bgpls-iosxe-16.9.7/` preserve the actual initial delegation,
PCUpd, ACTIVE report and before/after topology bytes for offline replay.
Historical replay is not evidence of present-day source freshness. The
structured record separates passed local checks, the historical P1/P2 proof
and the successful exact-checkout P3/P4 requalification.
