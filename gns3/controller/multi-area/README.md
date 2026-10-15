# Isolated multi-area BGP-LS collector candidate

This configuration adds a separate GoBGP v3.37.0 process for the two-area lab.
It leaves the existing P1 collector and its output files separate. The
controller initiates BGP from lab address `172.16.100.10` to P2 Loopback0
`10.255.0.5`; P2 is configured passive for that peer. GoBGP `port = -1` disables
its inbound BGP listener. The per-neighbor `local-address` selects the lab
source address. The upstream
[GoBGP v3.37.0 configuration guide](https://github.com/osrg/gobgp/blob/v3.37.0/docs/sources/configuration.md)
documents both fields.

Stock v3.37.0 loses distinct ABR Node NLRIs across OSPF areas. Build the
[pinned node-identity correction](../gobgp/patches/README.md) for this isolated
process and use that binary's absolute path in its service. An established
session with the stock binary does not qualify a complete multi-area TED.

Use a separate loopback-only gRPC endpoint, `127.0.0.1:50052`, and a separate
`build/live/multi-area` output directory. No additional controller NIC, inbound
BGP listener, firewall rule, authentication setup, or tunnel configuration is
included. The session still requires existing routed reachability from the
controller to P2 and the return route to the controller's lab address.

## Stage and inspect

From the repository root, prepare output without starting the speaker:

```bash
mkdir -p build/live/multi-area build/multi-area
cp gns3/controller/multi-area/gobgpd.toml build/multi-area/gobgpd.toml
/tmp/ninja-gobgpd-multi-area --version
/tmp/ninja-gobgpd-multi-area --dry-run -f build/multi-area/gobgpd.toml --api-hosts=127.0.0.1:50052
```

Review the separate
[P2 exporter fragment](../../router-configs/ios-xe/16.9.7-multi-area/P2-bgpls.cfg).
It uses OSPF process 100, BGP AS 65000, BGP router ID `10.255.0.5`, and
`distribute link-state`, which uses default instance ID 0. IOS XE's explicit
`instance-id` syntax accepts values starting at 32, so this profile omits that
argument rather than configuring explicit 0. The captured domain and generated
identity policy still use 0. Confirm P2's actual OSPF router ID,
Loopback0 address, underlay reachability and existing BGP configuration before
applying it. This fragment assumes there is no conflicting BGP process or peer
policy. Adapt all values together for another deployment; never infer a domain
solely from an equal loopback address. Cisco's
[IOS XE 16.9 BGP-LS guide](https://www.cisco.com/c/en/us/td/docs/ios-xml/ios/iproute_bgp/configuration/xe-16-9/irg-xe-16-9-book/bgp-ls.html)
describes OSPF distribution and the link-state address family.

The actual multi-area controller and exporter must be started only as a
reviewed deployment step. Installing or rendering this example does not start
them. Keep the existing single-area PCEP computation input unchanged until the
new feed passes qualification.

## Read-only collection after explicit deployment

Once the reviewed speaker/exporter are running, inspect their session through
the separate endpoint:

```bash
gobgp -u 127.0.0.1 -p 50052 neighbor
gobgp -u 127.0.0.1 -p 50052 global rib -a ls
```

Render the ABR identity policy using independently checked domain values:

```bash
ninja-pce lab-render \
  --profile gns3/lab-data/iosxe-16.9.7/multi-area-profile.yml \
  --output-dir build/multi-area \
  --bgpls-asn 65000 --bgpls-instance-id 0 --bgpls-identifier 0
```

Review the generated `node-map.json` against the received TE router IDs, then
use a read-only collection command with that map:

```bash
ninja-pce bgpls-collect \
  --endpoint 127.0.0.1:50052 \
  --proto gns3/controller/gobgp/gobgp-readonly.proto \
  --peer 10.255.0.5 \
  --node-map build/multi-area/node-map.json \
  --ospf-multi-area-policy build/multi-area/ospf-multi-area-policy.json \
  --output build/live/multi-area/topology.json \
  --status build/live/multi-area/bgpls-status.json \
  --raw build/live/multi-area/bgpls-raw.json
```

The existing collector uses its own required peer, endpoint and output files;
keep them distinct. Verify the received domain is protocol 3, AS 65000,
instance 0, BGP-LS identifier 0, and that ABR IGP router IDs and both area
memberships match the explicit policy. End-of-RIB and established BGP are
necessary but do not establish full exporter completeness or inter-area RSVP
signaling capability. This collector grants no configuration authority.

An opted-in PCEP computation process must also receive
`--compute-ospf-multi-area-policy build/multi-area/ospf-multi-area-policy.json`
alongside its full computation inputs. The collector and computation policy
documents and digests must agree. Default computation rejects a feed carrying
this projection policy. These options do not enable LSP creation or updates;
keep the existing PCEP service unchanged while qualifying the separate feed.
