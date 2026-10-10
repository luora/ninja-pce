# Live BGP-LS and passive PCEP lab setup

This stage uses the operator's six CSR1000v IOS XE 16.9.7 nodes and Ubuntu
24.04 controller. Ubuntu ens3 retains its working Internet settings; ens4 is
172.16.100.10/24
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

Ubuntu reached all six loopbacks, and PE1 reached 172.16.100.10 with source
10.255.0.1 (3/3). The later merged Netplan inspection showed ens3 explicitly
configured as 192.0.2.81/24 with gateway 192.0.2.254 and DNS 8.8.8.8/1.1.1.1;
these working operator settings were preserved.

GoBGP 3.37.0 and grpcurl 1.9.3 were installed from pinned upstream release
archives after SHA256 verification. BGP-LS then established between P1 and
Ubuntu: GoBGP accepted all 45 advertisements, and the read-only collector
published six nodes and sixteen directed TE links. Each link had metric 10,
maximum bandwidth 1 Gbps, reservable bandwidth 100 Mbps and eight unreserved
values of 100 Mbps. Twenty-three prefix NLRIs were deliberately excluded from
the TE graph. Physical interface state remains unknown; the source is
non-authoritative and ineligible for automatic delivery. The 50 Mbps dry-run selected PE1–P3–P4–PE3, metric 30, bottleneck
100 Mbps, with strict remote-interface hops 10.0.13.3, 10.0.34.4 and 10.0.46.6.
No tunnel was provisioned by that computation.

A controlled P1 Gi2 shutdown withdrew P1→P2 first; after the remote OSPF
40-second dead timer expired, P2→P1 also disappeared, leaving fourteen directed
links. Re-enabling Gi2 restored sixteen links. The collector preserved the
independent directional advertisements throughout convergence. Stopping GoBGP
removed the published topology and recorded source unavailability while PCEP
remained established; restarting GoBGP restored the live graph.

Live PCEP was then established from P1 (172.16.100.102) and PE1
(10.255.0.1) to the controller on TCP/4189. Both IOS peers reported State up,
Stateful capability, and stable keepalive exchange with zero PCEP errors.
The controller decoded each actual OPEN and PLSP-ID 0 end-of-sync PCRpt;
both peers were fresh and synchronized with empty LSP databases. No tunnel
had been created. The captured IOS 16.9.7 OPEN and empty-database marker are
also retained as a codec regression test.

Raw before/apply/verification transcripts are local artifacts under
`build/live-lab-evidence/`; they are not committed router configurations.
The running-config backups precede the changes, and startup-config backups
precede persistence. All six running configurations were saved after final
OSPF/LDP recovery checks. The controller services are enabled for boot.
The [machine-readable stage results](live_validation_results.json) distinguish
these observed checks from the unexecuted RSVP-LSP validation matrix.

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
gobgp -u 127.0.0.1 neighbor
gobgp -u 127.0.0.1 global rib -a ls
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

Validation after the live captures: 435 local tests passed in 17.30 seconds,
84% aggregate
coverage, including fragmented/coalesced real loopback PCEP traffic, malformed
frames, timers, allowlists, synchronization, stale/restart and bounded storage;
BGP-LS binary parsing/replay/withdrawal/unknown-attribute/session-failure cases;
CLI feed publication/invalidation/shutdown contracts; and actual captured
GoBGP/IOS binary replay and PCEP OPEN/end-of-sync regression cases. Ruff and
mypy also passed. Router observations above remain distinct from offline replay.

```bash
.venv/bin/pytest -q --cov=src/ninja_pce
.venv/bin/ruff check src tests gns3/validation
.venv/bin/mypy
```

References: [Cisco 16.9 BGP-LS](https://www.cisco.com/c/en/us/td/docs/ios-xml/ios/iproute_bgp/configuration/xe-16-9/irg-xe-16-9-book/bgp-ls.html),
[Cisco 16.9 Dynamic PCC](https://www.cisco.com/c/en/us/td/docs/ios-xml/ios/seg_routing/configuration/xe-16-9/segrt-xe-16-9-book/sr-dynamic-pcc.html),
[RFC 9552](https://www.rfc-editor.org/rfc/rfc9552.html),
[RFC 5440](https://www.rfc-editor.org/rfc/rfc5440.html),
and [RFC 8231](https://www.rfc-editor.org/rfc/rfc8231.html).
