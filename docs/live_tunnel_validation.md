# First live RSVP-TE tunnel validation

On 2026-10-10, PE1 Tunnel100 was created and verified in the isolated
six-router CSR1000v IOS XE 16.9.7 lab. This follows the earlier
[BGP-LS/passive-PCEP observation stage](live_protocol_setup.md), which did not
create a tunnel. The [machine-readable results](live_tunnel_validation_results.json)
record the exact observed scope.

The subsequent [PCE computation qualification](live_pcep_computation_validation.md)
records computed primary paths through P1/P2 and then P3/P4 after reviewed-build
deployment. The explicit path in this first-stage report remains configured
as fallback.

Ninja-PCE computed the current live path; the resulting configuration was
applied through PE1's supplied lab console. PE1 signalled RSVP-TE, and the
passive PCEP listener received its actual LSP state report. This is manual
lab provisioning. At that stage, PCEP computation replies and PCUpd were
unimplemented; PCInitiate and reservation-ledger integration remain future work.
The existing IOS XE 16.9.7
automated CLI delivery restriction remains in place.

## Configuration and path

The freshly computed 50 Mbps demand selected PE1–P3–P4–PE3 at TE metric 30,
with strict remote-interface hops 10.0.13.3, 10.0.34.4, 10.0.46.6. Tunnel100 has
source 10.255.0.1, destination 10.255.0.6, bandwidth 50000 kbps, setup/hold priorities
7/7, and explicit path EP_CUSTOMER_A_PE1_PE3. Autoroute is disabled.

The [applied console candidate](../gns3/router-configs/ios-xe/16.9.7-six-router/underlay/PE1-tunnel100.cfg)
contains the complete commands. Tunnel100 and that path were absent from the
pre-change running configuration. Both running and startup configuration
backups are retained locally. PE1 returned `[OK]` to `write memory` after
verification, so the tunnel configuration persists across restart.

## Observed verification

PE1 reported `Admin: up`, `Oper: up`, `Path: valid`, and
`Signalling: connected`, with explicit path weight 30 and 50000 kbps bandwidth.
The RSVP session had tunnel ID 100 and LSP instance 1. The configured three-hop
ERO matched the computed route; the router's signalling display additionally
appended the destination loopback 10.255.0.6.

P3, P4 and PE3 all showed the same signalled LSP with the expected incoming and
outgoing interfaces. RSVP reserved 50 Mbps on PE1 Gi2, P3 Gi3 and P4 Gi2.

`ping mpls traffic-eng Tunnel100` succeeded 5/5. MPLS traceroute returned
10.0.13.3, 10.0.34.4, 10.0.46.6, confirming the forwarding path. These small OAM
probes demonstrate reachability through the LSP, not measured 50 Mbps throughput;
the CSR's observed license throughput limit remains 1000 kbps.

PE1 sent an actual PCRpt with symbolic name Tunnel100_w, PLSP-ID 524388
(0x80064), tunnel 100/LSP 1, state ACTIVE, administrative-up and delegation clear.
The controller retained the exact three strict IPv4/32 ERO subobjects and
50000000 bits/s bandwidth. The peer remained established, fresh and synchronized.
Optional IOS extension TLV 31 and BANDWIDTH type 5 were retained as raw fields;
they are not assigned invented semantics. Actual/intended bandwidth role
separation is still absent from the parser's normalized model.

BGP-LS subsequently advertised a 50 Mbps reservation on exactly the three
selected forward edges. Their priorities 0–6 remained 100 Mbps unreserved;
priority 7 became 50 Mbps. Reverse directions and other links remained 100 Mbps
unreserved, with all sixteen link identities preserved. The source remains
non-authoritative because physical state and reservation ownership are unknown.
A subsequent calculation for an additional demand can therefore prefer the
other equal-metric path; it does not automatically update Tunnel100.

This executes T01's baseline computation/path/operational-LSP comparison using
manual lab provisioning. The remaining T02–T15 scenario matrix has not been
executed end to end.

The final local regression suite passed 441 tests in 17.41 seconds at 84% aggregate
coverage; Ruff and mypy passed. Tests include replay of the actual non-empty
PCRpt and the subsequent directional priority-aware reservation advertisements.
Historical replay is distinct from the live router and MPLS OAM observations.

## Operational commands

On PE1:

```text
show mpls traffic-eng tunnels Tunnel100
show interfaces Tunnel100
show pce client lsp detail
show pce client peer detail
show ip rsvp interface
show ip rsvp reservation
ping mpls traffic-eng Tunnel100
traceroute mpls traffic-eng Tunnel100
```

On P3, P4 and PE3:

```text
show mpls traffic-eng tunnels
show ip rsvp reservation
```

On Ubuntu:

```bash
cd ~/ninja-pce
.venv/bin/ninja-pce topology-summary --topology build/live/topology.json
python3 -m json.tool build/live/pcep.json
```

Raw before/apply/router/OAM/controller captures are local artifacts under
`build/live-lab-evidence/first-tunnel/`. Historical PCRpt and BGP-LS reservation
fixtures are kept separately under `tests/fixtures/` for offline regression.

To remove only these lab resources after confirming Tunnel100's description
and path ownership against the pre-change backup:

```text
configure terminal
interface Tunnel100
 shutdown
 exit
no interface Tunnel100
no ip explicit-path name EP_CUSTOMER_A_PE1_PE3
end
```

That is a manual cleanup procedure, not an automatic rollback command. Save
only after verifying the intended final running configuration.

Syntax references: [Cisco IOS XE16.9 TE tunnel configuration examples](https://www.cisco.com/c/en/us/td/docs/ios-xml/ios/mp_te_diffserv/configuration/xe-16-9/mp-te-diffserv-xe-16-9-book.pdf),
[RFC 8231 PCRpt semantics](https://www.rfc-editor.org/rfc/rfc8231.html#section-6.1).
Actual command acceptance and operational outputs above were observed on the
running CSR1000v 16.9.7 image; this does not qualify other releases or platforms.
