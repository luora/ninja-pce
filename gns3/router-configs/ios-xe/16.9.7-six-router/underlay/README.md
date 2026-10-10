# Six-router IOS XE 16.9.7 underlay candidates

Apply each `HOST.cfg` to the matching router after its [base addressing candidate](../README.md) has been reviewed and applied. These are additive lab candidates for the agreed eight-link topology. Each router must provide the named GigabitEthernet1–4 adapters. The optional PE1 Gi4–P3 Gi1 parallel link remains outside this stage.

The candidates enable OSPF process 100, area 0, with the Loopback0 address as router-ID. Interfaces are passive by default; only the eight core link pairs send OSPF hellos. P1 Gi3 advertises 172.16.100.0/24 passively so the other routers have a return route to Ubuntu 172.16.100.10. Ubuntu remains an IP host and needs a route to the core/loopback prefixes through 172.16.100.102.

Core interfaces enable LDP, MPLS TE, and RSVP. The 100000-kbps aggregate pool and 100000-kbps single-flow ceiling are a chosen 100 Mbps lab reservation policy; they do not establish measured capacity or override the CSR license throughput limit. TE administrative weight is 10. IGP cost retains its existing/default setting.

The six HOST.cfg files contain no tunnel, PCC, BGP, authentication, save, or reload commands. The separately named P1-bgpls.cfg, P1-pcep.cfg and PE1-pcep.cfg enable the later live protocol stage. They preserve base IP addresses and unused-port state. The LDP router-ID preference uses Loopback0 without `force`; inspect the operational LDP ID because an existing LDP process may retain its current ID until its normal next selection.

## Expected adjacencies

Each row lists the complete core interface set and expected directly connected OSPF/LDP peers after all six candidates converge.

| Router | Router-ID / LDP ID prefix | Active core interfaces | Peer router-IDs | Count |
| --- | --- | --- | --- | --- |
| PE1 | 10.255.0.1 | Gi1, Gi2 | 10.255.0.2, 10.255.0.3 | 2 |
| P1 | 10.255.0.2 | Gi1, Gi2, Gi4 | 10.255.0.1, 10.255.0.5, 10.255.0.3 | 3 |
| P3 | 10.255.0.3 | Gi2, Gi3, Gi4 | 10.255.0.1, 10.255.0.4, 10.255.0.2 | 3 |
| P4 | 10.255.0.4 | Gi1, Gi2, Gi3 | 10.255.0.5, 10.255.0.6, 10.255.0.3 | 3 |
| P2 | 10.255.0.5 | Gi1, Gi2, Gi4 | 10.255.0.4, 10.255.0.2, 10.255.0.6 | 3 |
| PE3 | 10.255.0.6 | Gi2, Gi4 | 10.255.0.4, 10.255.0.5 | 2 |

The exact physical pairs remain PE1 Gi1–P1 Gi1, PE1 Gi2–P3 Gi2, P1 Gi2–P2 Gi2, P1 Gi4–P3 Gi4, P2 Gi1–P4 Gi1, P2 Gi4–PE3 Gi4, P3 Gi3–P4 Gi3, and P4 Gi2–PE3 Gi2.

## Read-only verification

Run on each router and retain the raw output:

```text
show ip interface brief
show ip cef summary
show ip ospf
show ip ospf interface brief
show ip ospf neighbor
show ip route ospf
show ip route 172.16.100.0
show mpls interfaces
show mpls ldp discovery
show mpls ldp neighbor
show mpls forwarding-table
show ip rsvp interface
show ip ospf database opaque-area
show mpls traffic-eng link-management interfaces
show mpls traffic-eng link-management advertisements
show mpls traffic-eng topology
show mpls traffic-eng topology brief
show platform hardware throughput level
```

Confirm OSPF router-ID/process 100/area 0, a FULL adjacency on each listed core interface, and the peer set/count above. Confirm LDP sessions are operational with those peer IDs and local LDP ID `10.255.0.N:0`. Check loopback routes and that every non-P1 router learns 172.16.100.0/24 through OSPF. On P1, Gi3 must be passive with no OSPF neighbor, MPLS TE, LDP, or RSVP enabled.

The TE database should identify all six router-IDs and both directions of all eight core links, including the correct local/remote numbered addresses. Check each local core interface is MPLS TE on, RSVP on, admin-up, and flooded in OSPF 100 area 0. Confirm TE metric 10 and max-reservable global bandwidth 100000 kbps. With no existing RSVP reservations, all eight priority entries should show 100000 kbps unreserved; retain observed values if prior reservations exist. The controller subnet and reserved/unused ports must not become TE links.

These checks qualify the underlay only. They do not demonstrate a BGP-LS export, a PCEP session, or an established RSVP-TE service.

## Official syntax references

- [Cisco IOS XE Fuji 16.9 OSPF configuration](https://www.cisco.com/c/en/us/td/docs/ios-xml/ios/iproute_ospf/configuration/xe-16-9/iro-xe-16-9-book/iro-cfg.html) describes OSPF operation and database/interface/neighbor checks. [The 16.9 OSPF TE chapter](https://www.cisco.com/c/en/us/td/docs/ios-xml/ios/seg_routing/configuration/xe-16-9/segrt-xe-16-9-book/sr-te-ospf.html) shows interface OSPF area/point-to-point and OSPF TE area/router-ID syntax; no segment-routing configuration is used here.
- [Cisco IOS XE Fuji 16.9 LDP](https://www.cisco.com/c/en/us/td/docs/ios-xml/ios/mp_ldp/configuration/xe-16-9/mp-ldp-xe-16-9-book/mp-ldp.html) documents CEF, MPLS/LDP enablement, and the optional `force` router-ID behavior.
- [Cisco IOS XE Fuji 16.9 RSVP guide](https://www.cisco.com/c/en/us/td/docs/ios-xml/ios/qos_rsvp/configuration/xe-16-9/qos-rsvp-xe-16-9-book.pdf) documents `ip rsvp bandwidth interface-kbps single-flow-kbps`.
- [Cisco IOS XE Fuji 16.9 TE interface weight](https://www.cisco.com/c/en/us/td/docs/ios-xml/ios/mp_te_path_protect/configuration/xe-16-9/mp-te-path-protect-xe-16-9-book/mpls-traffic-engineering-inter-as-te.html) documents `mpls traffic-eng administrative-weight weight`; [TE verification](https://www.cisco.com/c/en/us/td/docs/ios-xml/ios/mp_te_path_protect/configuration/xe-16-9/mp-te-path-protect-xe-16-9-book/mpls-traffic-engineering-shared-risk-link-groups.html) describes topology and link-management output.

The guides cover the 16.9 software train; operational output from the running CSR1000v 16.9.7 image is required to confirm acceptance and behavior. CEF is an MPLS prerequisite: verify it with `show ip cef summary`. Generic `ip cef` was rejected as incomplete on the running CSR image, so these candidates rely on its existing CEF setting and do not configure that command.
