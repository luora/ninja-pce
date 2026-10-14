# Repeatable OSPF multi-area lab extension

The eight-router profile extends the existing six-router area 0 with area 1.
P2 and P4 become ABRs; P5 and PE4 are new area 1 routers. The controller stays
attached through P1. BGP-LS export and inter-area tunnel qualification are
separate steps from configuring this underlay.

The reusable input is
[`multi-area-profile.yml`](../gns3/lab-data/iosxe-16.9.7/multi-area-profile.yml).
It contains topology intent and explicit configuration scope. It does not
contain console endpoints, credentials, public addresses, or authoritative TED
metadata. Node names and addresses are data; the renderer does not special-case
the routers in this example.

```text
Existing area 0                 New area 1

PE1──P1──P2 (ABR)──────────────P5
 │    │   │                    │
 P3──────P4 (ABR)──────────────PE4
          │
         PE3 (area 0, linked to both P2 and P4)
```

The diagram shows area boundaries; the manifest lists every physical link.

| New link | Local endpoint | Remote endpoint | Subnet | Area |
|---|---|---|---|---|
| P2–P5 | P2 GigabitEthernet5 / 10.0.57.5 | P5 GigabitEthernet5 / 10.0.57.7 | 10.0.57.0/24 | 1 |
| P5–PE4 | P5 GigabitEthernet1 / 10.0.78.7 | PE4 GigabitEthernet1 / 10.0.78.8 | 10.0.78.0/24 | 1 |
| P4–PE4 | P4 GigabitEthernet5 / 10.0.48.4 | PE4 GigabitEthernet5 / 10.0.48.8 | 10.0.48.0/24 | 1 |

P5 uses Loopback0 `10.255.0.7/32`; PE4 uses `10.255.0.8/32`. Both loopbacks are in
area 1. The sample explicitly uses OSPF process **100** on every router. Each new
direction has OSPF cost 10, TE metric 10, an interface bandwidth reference of
1,000,000 kbps, and RSVP total/single-flow limits of 100,000 kbps. These settings
are configuration intent, not a measurement of forwarding capacity.

## Render and review

```bash
ninja-pce lab-render \
  --profile gns3/lab-data/iosxe-16.9.7/multi-area-profile.yml \
  --output-dir build/multi-area
```

This command validates the entire manifest and writes dry-run configuration for
P2, P4, P5 and PE4 plus `plan.json` and `node-map.json`. The plan binds the input
file hash, configuration hashes and node-map digest to a deterministic plan ID.
It performs no network connection or
configuration delivery. It does not create a tunnel or enable BGP, PCEP, SSH,
NETCONF, or dashboard provisioning.

The `iosxe-16.9.7` platform profile is explicit. Its renderer is separate from
the live RSVP tunnel provisioning adapter and does not inherit that adapter's
qualification. Check the exact device image and existing configuration before
applying the generated underlay configuration through an authorized console.

Baseline links have `configure: false`. They participate in interface, address,
subnet and area validation, and they are omitted from the configuration output.
All eight existing backbone links use `/29` prefixes. The existing P2–P4 subnet
is `10.0.54.0/29`, with P2 `.5` and P4 `.4`. The three new area 1 links use `/24`.

New links have `configure: true`. Their endpoint stanzas set descriptions,
addresses, OSPF membership and metrics, MPLS and RSVP settings, and `no shutdown`.
OSPF `no passive-interface` applies only to these selected interfaces, allowing
adjacencies when an existing router uses `passive-interface default`. P2 and P4
receive TE area configuration for their declared area 0/1 membership; their
existing loopbacks and router IDs are omitted from the changes.

Only nodes with `configure_loopback: true` receive a hostname, a new loopback,
OSPF router ID, LDP router ID and LDP configuration. In this example those
nodes are P5 and PE4. The profile is additive intent, not a safe replacement for
arbitrary running configuration: confirm the selected new interfaces and
loopbacks are unused before applying it. Preserve a pre-change snapshot for
manual rollback.

## Adapt the profile to another topology

Keep the schema and replace the node/link data. Include existing links that must
constrain address/interface allocation, marking them `configure: false`.
Declare controller-facing or otherwise occupied GigabitEthernet interfaces in
the node's `reserved_interfaces` list when they are not TE links. Set
`configure: true` only for reviewed new links. Set `configure_loopback: true`
only for reviewed new nodes; this flag also enables that node's initial global
underlay settings. A node incident to any baseline `configure: false` link
cannot use this flag, preventing an existing router's global identity from
being treated as fresh configuration.

Each node requires a unique `id`, a loopback IPv4 address or `/32`, and
`loopback_area`. `hostname` defaults to `id`, `loopback_interface` defaults to
`Loopback0`, `igp_router_id` defaults to the loopback address, and `ospf_process`
defaults to 1. Set `igp_router_id` explicitly when the OSPF router ID differs
from the TE loopback; the new-router renderer uses this value for OSPF, while
the TE router ID remains the declared loopback. Existing router IDs are never
changed by the renderer. Compare every existing router's actual OSPF ID with
the manifest before applying any derived identity policy. Always set the OSPF
process explicitly when extending an existing network. Supported physical interface names are
`GigabitEthernet` and its `Gi` abbreviation, including slotted interface indices;
aliases normalize before collision checks. Other interface families require an
explicit platform-profile extension.

Each link requires a unique `id`, an `area`, a boolean `configure`, and exactly
two `endpoints`. Each endpoint supplies `node`, `interface`, and a numbered IPv4
`address/prefix`. Link-level `metric`, `te_metric`, `capacity_kbps`,
`reservable_kbps` and `max_reservation_kbps` set defaults for both directions;
endpoint fields can override those defaults for asymmetric links. `te_metric`
defaults to OSPF `metric`; the maximum single-flow reservation defaults to the
total reservable bandwidth. The units are decimal **kbps** throughout.

The validator rejects unknown fields, unsupported platform profiles, duplicate
mapping keys or identities, reused/reserved interfaces, overlapping link
subnets, loopback/subnet conflicts, mismatched endpoint prefixes, invalid
metrics, and RSVP limits above the declared interface reference or total
reservation limit. Numbered `/31` links are supported. Virtual links,
unnumbered links and non-backbone multi-area routers are outside this profile.

## Establish underlay and topology evidence

After applying the reviewed changes, collect fresh independent output from all
four changed routers:

```text
show version
show ip ospf neighbor
show ip ospf interface brief
show ip ospf database opaque-area
show mpls traffic-eng topology
show mpls ldp neighbor
show ip rsvp interface
show ip cef summary
```

Check all three new adjacencies reach FULL, the new loopbacks are reachable from
the backbone, P2/P4 each have both area databases, and every new direction has
the intended TE metric and RSVP capacity. The summary's `node_areas` describes
intent only; these checks provide evidence about the actual routers.

Configure a BGP-LS source with visibility into both areas, then independently
confirm that the received feed contains both areas' detailed links, metrics and
bandwidth attributes. The controller can reach that exporter through its
existing P1 attachment; direct controller-to-ABR cabling is unnecessary. Do not
assume that ordinary OSPF summary routes provide a remote area's detailed TED.

The ABR identity policy must explicitly bind each ABR's per-area BGP-LS node
identities to its physical router and match the observed protocol, instance,
autonomous system and BGP-LS identifier. The lab renderer does not manufacture
those observed domain identifiers. Collect them before configuring an
inter-area computation policy.

After independently checking those values, the same manifest can generate a
validated ABR policy:

```bash
ninja-pce lab-render \
  --profile gns3/lab-data/iosxe-16.9.7/multi-area-profile.yml \
  --output-dir build/multi-area \
  --bgpls-asn 65000 --bgpls-instance-id 0 --bgpls-identifier 0
```

The three domain flags must be supplied together. Omitting them produces only
underlay intent; supplying them also produces `ospf-multi-area-policy.json`.
ABR membership derives from each node's declared link/loopback areas, not from
router naming conventions. The generated policy includes the TE and IGP router
IDs separately and passes the same strict validation used by BGP-LS ingestion.
The numbers shown here belong to this lab's explicit domain profile; use actual
captured values for another topology.

The separate
[outbound-only GoBGP collector candidate](../gns3/controller/multi-area/README.md)
and [P2 exporter fragment](../gns3/router-configs/ios-xe/16.9.7-multi-area/P2-bgpls.cfg)
keep the two-area feed isolated from existing P1 collection and PCEP inputs.
They contain no authentication or firewall changes and are applied separately
from the generated underlay configuration.

Only after both-area topology ingestion and ABR identity handling pass should
PE1→PE4 path computation, RSVP signaling, forwarding and link-failure recovery
be qualified. The existing single-area tunnel profile remains separate until
that evidence exists.

## Recorded underlay result — 2026-10-13

P2, P4, P5 and PE4 received the reviewed underlay changes and their resulting
underlay configurations were saved. All three new OSPF links reached FULL;
their LDP sessions established and RSVP reported 100 Mbps reservable in each
direction. PE1 reached `10.255.0.7` and `10.255.0.8` using Loopback0 as its source,
with three replies from three probes to each destination. P5 and PE4 each
returned three replies from three probes to PE1's loopback. Tunnel100's
configuration remained unchanged and its operational state remained up.

The P2 BGP-LS exporter was configured separately. Its exporter changes remain
unsaved while the isolated collector is being qualified; the saved underlay
does not imply acceptance of that collector profile.

The exporter advertised ten Node NLRIs for the eight physical routers, including
separate area 0/1 identities for P2 and P4. The stock GoBGP 3.37.0 RIB retained
only eight Node NLRIs because its node key collided across OSPF areas. This is
an incomplete input graph. The new collector units were stopped while an
isolated dependency fix is prepared; the graph is blocked from qualification
and inter-area computation. Established OSPF and a working BGP session do not
resolve this missing-node evidence.

This result qualifies the recorded underlay checks only. It does not qualify
the multi-area TED, inter-area tunnel provisioning, forwarding through an
inter-area RSVP tunnel, or automatic recovery for that tunnel.

## Recorded topology and computation result — 2026-10-14

The isolated collector now uses the
[pinned GoBGP node-identity correction](../gns3/controller/gobgp/patches/README.md).
Its live RIB retained all **ten Node NLRIs and 22 Link NLRIs**. Explicit policy
projection produced **eight physical routers and 22 directed TE links** across
areas `0.0.0.0` and `0.0.0.1`. P2 and P4 each retained independently advertised
identities in both areas; no missing Node NLRI was reconstructed from a link.
This resolves the stock-collector blocker recorded above.

A fresh raw/status/topology consistency check passed through
`PcepPathComputer.validated_snapshot()`. Default computation without the
explicit area policy refused the normalized feed. A read-only CSPF calculation
for 1 Mbps from PE1 to PE4 returned:

| Observation | Result |
|---|---|
| Path | PE1 → P3 → P4 → PE4 |
| Edge areas | 0 → 0 → 1 |
| Total TE metric | 30 |
| Numbered strict ERO | `10.0.13.3`, `10.0.34.4`, `10.0.48.8` |
| Advertised bottleneck availability at capture | 50 Mbps |

The existing P1 collector and PCEP/dashboard service definitions remained
unchanged and active. Tunnel100 remained operational with its 50 Mbps RSVP
reservation on explicit fallback; its configuration was unchanged. PE1 again
reached PE4's loopback with three replies from three probes. The saved underlay
survived the intervening lab reboot. P2's separately unsaved exporter was
restored after that reboot, verified established, and saved after qualification.
The two isolated collector services were enabled on boot only after fresh
qualification passed.

The private evidence archive contains the exact raw capture, status, normalized
graph, explicit policy, node map, report and hash manifest. Its SHA-256 is
`40f2fca0747ec80edc75feafaa3f8e84bd078ce48c730987404ff88a31c8d984`.
Captures and binary artifacts remain outside Git. The final daemon binary
SHA-256 is `3b65786c4927d1fd9c1682a3dba6ec7e6f9faceb29af3bb92c0fd268c4d576ca`;
its packet/RIB package suites, expected-failing upstream controls, module
verification and independent byte-identical rebuild passed. The Python suite
passed **1,391 tests**, with Ruff and mypy checks clean.

This result qualifies live topology ingestion and a local path calculation.
It did not send a PCEP request or update, create an inter-area tunnel, reserve
the previewed 1 Mbps, or test forwarding or failure recovery for that tunnel.
The existing PCEP service and dashboard still consume their original area 0
inputs. Qualifying one PE1→PE4 tunnel and updating those consumers are separate
next steps.
