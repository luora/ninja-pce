# Live BGP-LS and passive PCEP lab setup

This stage uses the operator's six CSR1000v IOS XE 16.9.7 nodes and Ubuntu
24.04 controller. Ubuntu ens3 retains Internet DHCP; ens4 is 172.16.100.10/24
through P1 Gi3, 172.16.100.102/24. The router loopbacks are 10.255.0.1 through
.6, with PE1/P1/P3/P4/P2/PE3 respectively. P1 exports topology; PE1 is the
tunnel headend. No changes to the GNS3 server host are required.

## Implemented scope

`ninja-pce bgpls-collect` reads a real GoBGP BGP-LS RIB through read-only local
gRPC calls. Binary NLRI and attributes retain protocol/domain identity,
direction, parallel resources, exact interface addresses, metric and all eight
bandwidth priorities. Successful complete polls replace the prior graph;
known failed polls and orderly shutdown remove the published topology.
Missing required TE attributes exclude a resource with a diagnostic. See the
[GoBGP guide](../gns3/controller/gobgp/README.md) for schema and limitations.

`ninja-pce pcep-listen` is a bounded passive stateful TCP listener: OPEN,
KEEPALIVE, errors, CLOSE, PCRpt parsing, synchronization and durable captures.
It advertises the stateful capability with update/initiation flags clear.
It does **not** answer PCReq with computed paths, send PCUpd/PCInitiate,
accept delegation, or modify tunnels. This is the live observation stage, not
complete stateful PCE control or a full standards conformance claim.

These sources cannot authorize automatic delivery: BGP-LS alone cannot prove
physical interface state or complete router export, and PCEP captures do not
feed reservation accounting. The existing exact-plan approval/version checks
remain in place. IOS XE 16.9.7 is still unqualified for autonomous CLI LSP writes.

## Underlay before protocol peers

Use the six [underlay candidates](../gns3/router-configs/ios-xe/16.9.7-six-router/underlay/README.md)
after reviewing the base map. Bring up OSPF first and verify all eight core
adjacencies and controller return routes; then enable MPLS/LDP and RSVP-TE.
The files choose OSPF 100 area 0, passive by default, RSVP 100 Mbps pools and TE
weight 10. They do not establish measured 100 Mbps forwarding capacity.

Observed through the supplied device consoles on 2026-10-10:

- All six base interface maps matched the intended cables and IPs.
- OSPF counts were PE1=2, P1=3, P2=3, P3=3, P4=3, PE3=2, all FULL.
- Every non-P1 router learned 172.16.100.0/24 through OSPF; CEF was enabled.
- All sixteen directed LDP peer observations were operational.
- Router TEDs contained six nodes and sixteen directed links; P1 flooded
  three TE interfaces with metric 10, reservable 100000 kbps and eight matching
  unreserved-priority values. No LSPs had been created by this stage.
- P1 accepted `distribute link-state`, reported LS distribution Ready Active
  with zero unresolved links, and originated 45 BGP-LS NLRIs. These facts do
  not alone demonstrate the controller's BGP adjacency or ingestion.

Raw before/apply/verification transcripts are local artifacts under
`build/live-lab-evidence/`; they are not committed router configurations.
The console backups precede the changes. Running router configuration has not
been saved to startup configuration by this procedure.

## Controller installation

Install the reviewed branch in `/home/ubuntu/ninja-pce` with a Python virtual
environment, as in [initial setup](initial_controller_setup.md). The live
adapters use the normal Python dependencies; Netmiko is not needed to establish
their protocol sessions.

```bash
cd ~/ninja-pce
python3 -m venv .venv
.venv/bin/python -m pip install -e .
bash gns3/controller/ubuntu-24.04/install-live-tools.sh
```

The tool installer downloads pinned GoBGP 3.37.0 and grpcurl 1.9.3 Linux amd64
release binaries over HTTPS and verifies upstream release SHA256 checksums.
It does not start services. Review AS 65000 and the two lab addresses in
[gobgpd.toml](../gns3/controller/gobgp/gobgpd.toml) before starting the speaker.

```bash
getent passwd gobgp >/dev/null || sudo useradd --system --user-group --no-create-home --shell /usr/sbin/nologin gobgp
sudo install -d -m 755 /etc/gobgp
sudo install -m 644 gns3/controller/gobgp/gobgpd.toml /etc/gobgp/gobgpd.toml
sudo install -m 644 gns3/controller/gobgp/gobgpd.service /etc/systemd/system/
sudo /usr/local/bin/gobgpd --dry-run -f /etc/gobgp/gobgpd.toml
sudo install -m 644 gns3/controller/ubuntu-24.04/ninja-pcep.service /etc/systemd/system/
sudo install -m 644 gns3/controller/ubuntu-24.04/ninja-bgpls.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now gobgpd ninja-pcep ninja-bgpls
systemctl status gobgpd ninja-pcep ninja-bgpls --no-pager
```

The unit templates explicitly assume user `ubuntu`, that checkout location,
and the agreed addresses. GoBGP binds TCP/179 to the lab NIC and gRPC to
127.0.0.1:50051. PCEP binds 172.16.100.10:4189 and permits PCC source addresses
172.16.100.102 and 10.255.0.1. Plain TCP/IP allowlists are suitable only for the
isolated lab; authenticated PCEPS/TLS and controller ownership remain future
work. Existing Internet addressing and forwarding settings are unchanged.

## Router peers and live evidence

Apply the separate P1 [BGP-LS candidate](../gns3/router-configs/ios-xe/16.9.7-six-router/underlay/P1-bgpls.cfg)
after the underlay check. It activates only the LS address family toward Ubuntu,
with no IPv4-unicast neighbor activation. Apply the passive PCC candidates
[P1](../gns3/router-configs/ios-xe/16.9.7-six-router/underlay/P1-pcep.cfg) and
[PE1](../gns3/router-configs/ios-xe/16.9.7-six-router/underlay/PE1-pcep.cfg)
after the listener is active and source/return routing is verified.

```text
! P1
show ip ospf ls-distribution
show bgp link-state link-state summary
show bgp link-state link-state neighbors 172.16.100.10 advertised-routes
show pce client peer detail
! PE1
show pce client peer detail
show pce client lsp detail
```

On Ubuntu:

```bash
cd ~/ninja-pce
gobgp neighbor
gobgp global rib -a ls
journalctl -u gobgpd -u ninja-bgpls -u ninja-pcep --no-pager -n 60
.venv/bin/ninja-pce topology-summary --topology build/live/topology.json
.venv/bin/ninja-pce compute --topology build/live/topology.json \
  --demand examples/demands/pe1_to_pe3_50m.yml --output-dir build/live/computed
```

The expected live graph is six routers and sixteen directed core links.
The earlier offline example's reserved bandwidth is simulated and must not be
copied into this live TED. With all live pools initially empty and equal TE
metrics, the computed path may differ from that offline example.

Inspect `build/live/bgpls-status.json`, `bgpls-raw.json` and `pcep.json`.
PCEP ESTABLISHED proves the session only. `synchronized=true` requires the
actual end-of-sync report. An empty report set is expected when no LSP exists;
it is not evidence of an established RSVP service. Honor report freshness and
`valid_until`; process crashes cannot update the persisted capture immediately.

Validation of this implementation: 423 local tests passed, 84% aggregate
coverage, including fragmented/coalesced real loopback PCEP traffic, malformed
frames, timers, allowlists, synchronization, stale/restart and bounded storage;
BGP-LS binary parsing/replay/withdrawal/unknown-attribute/session-failure cases;
and CLI feed publication/invalidation/shutdown contracts. Router observations
above are separate from these synthetic tests.

References: [Cisco 16.9 BGP-LS](https://www.cisco.com/c/en/us/td/docs/ios-xml/ios/iproute_bgp/configuration/xe-16-9/irg-xe-16-9-book/bgp-ls.html),
[Cisco 16.9 Dynamic PCC](https://www.cisco.com/c/en/us/td/docs/ios-xml/ios/seg_routing/configuration/xe-16-9/segrt-xe-16-9-book/sr-dynamic-pcc.html),
[RFC 9552](https://www.rfc-editor.org/rfc/rfc9552.html),
[RFC 5440](https://www.rfc-editor.org/rfc/rfc5440.html),
and [RFC 8231](https://www.rfc-editor.org/rfc/rfc8231.html).
