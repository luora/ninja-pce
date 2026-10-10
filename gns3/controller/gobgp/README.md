# GoBGP read-only BGP-LS topology ingestion

GoBGP is the BGP speaker; Ninja-PCE reads its RIB using the supported
`apipb.GobgpApi/ListPeer` and `ListPath` RPCs. No BGP stack or router writes are
implemented here. The adapter is pinned to the GoBGP **v3.37.0 API** and grpcurl
**v1.9.3**. Later GoBGP major versions need separate qualification.

The Python parser and transport are tested with synthetic binary NLRIs and
mock RPC responses. Real GoBGP/peer/router interoperability has **not** been
executed by these tests. P1's actual IOS XE image must support BGP-LS production;
the earlier partial `show version` output does not establish that capability.

## Install pinned tools on Ubuntu x86-64

Run from the reviewed checkout on the controller, after Internet/DNS work.
These commands install software locally; starting the service later opens a
BGP session to the explicitly configured peer. Confirm `uname -m` reports
`x86_64`; use the corresponding official assets for another architecture.

```bash
sudo apt-get update
sudo apt-get install -y curl ca-certificates
uname -m
mkdir -p build/controller-tools/gobgp-3.37.0
cd build/controller-tools/gobgp-3.37.0
curl --fail --location --output gobgp_3.37.0_linux_amd64.tar.gz \
  https://github.com/osrg/gobgp/releases/download/v3.37.0/gobgp_3.37.0_linux_amd64.tar.gz
curl --fail --location --output gobgp_3.37.0_checksums.txt \
  https://github.com/osrg/gobgp/releases/download/v3.37.0/gobgp_3.37.0_checksums.txt
sha256sum --check --ignore-missing gobgp_3.37.0_checksums.txt
tar -xzf gobgp_3.37.0_linux_amd64.tar.gz gobgp gobgpd
sudo install -m 755 gobgp gobgpd /usr/local/bin/
cd ../../..
mkdir -p build/controller-tools/grpcurl-1.9.3
cd build/controller-tools/grpcurl-1.9.3
curl --fail --location --output grpcurl_1.9.3_linux_x86_64.tar.gz \
  https://github.com/fullstorydev/grpcurl/releases/download/v1.9.3/grpcurl_1.9.3_linux_x86_64.tar.gz
curl --fail --location --output grpcurl_1.9.3_checksums.txt \
  https://github.com/fullstorydev/grpcurl/releases/download/v1.9.3/grpcurl_1.9.3_checksums.txt
sha256sum --check --ignore-missing grpcurl_1.9.3_checksums.txt
tar -xzf grpcurl_1.9.3_linux_x86_64.tar.gz grpcurl
sudo install -m 755 grpcurl /usr/local/bin/
cd ../../..
gobgp --version
grpcurl -version
```

Checksums come from the upstream release, providing transport/integrity checks;
they are not an independently authenticated package signature.

## Receive-only candidate configuration

Review [gobgpd.toml](gobgpd.toml): controller `172.16.100.10`, producer P1
`172.16.100.102`, candidate AS 65000 on both sides. The ASN is a deliberate
lab choice, not an observed router setting. The sole address family is `ls`;
default export rejects all routes at global and neighbor scope. There are no
locally originated routes or Linux kernel/FIB integration. P1 needs matching
BGP-LS export configuration, independently qualified for its platform.

The candidate negotiates graceful restart to obtain End-of-RIB evidence.
Ninja-PCE rejects restarting peers, stale best paths, missing End-of-RIB and
changes in session epoch/update counters during a poll. A peer that does not
send BGP-LS End-of-RIB remains unqualified; elapsed time cannot substitute for
that evidence.

Install/start only after the candidate peer/AS and controller address are ready:

```bash
getent passwd gobgp >/dev/null || sudo useradd --system --user-group --no-create-home --shell /usr/sbin/nologin gobgp
sudo install -d -m 755 /etc/gobgp
sudo install -m 644 gns3/controller/gobgp/gobgpd.toml /etc/gobgp/gobgpd.toml
sudo install -m 644 gns3/controller/gobgp/gobgpd.service /etc/systemd/system/gobgpd.service
sudo /usr/local/bin/gobgpd --dry-run -f /etc/gobgp/gobgpd.toml
sudo systemctl daemon-reload
sudo systemctl enable --now gobgpd
systemctl status gobgpd --no-pager
gobgp neighbor
```

The service binds BGP TCP/179 only to the lab address and the management gRPC
API only to `127.0.0.1:50051`. The API has write methods even though this
adapter's schema/command allowlist exposes only reads: protect local access and
do not expose that plaintext API on the Internet-facing NIC. Stop the lab
speaker with `sudo systemctl stop gobgpd` if necessary.

## Read-only source API

```python
from ninja_pce.bgpls import GoBgpTopologyProvider, topology_document

provider = GoBgpTopologyProvider(
    proto_path="gns3/controller/gobgp/gobgp-readonly.proto",
    required_peers=("172.16.100.102",),
    node_names={"10.255.0.1": "PE1", "10.255.0.2": "P1", "10.255.0.3": "P3",
                "10.255.0.4": "P4", "10.255.0.5": "P2", "10.255.0.6": "PE3"},
)
capture = provider.capture()  # JSON-compatible raw read-only evidence envelope
topology = provider.snapshot()  # performs a separate fresh full poll
document = topology_document(topology)  # existing loader-compatible YAML mapping
```

For one poll, call `capture()` then
`parse_gobgp_snapshot(capture, required_peers=(...), node_names={...})`.
Never retain a previously healthy graph after a failed poll. Atomically replace
the complete persisted snapshot; never merge it with the old graph. Withdrawal
means an NLRI disappears from the next successful complete RIB. An empty RIB
produces an empty graph, disabling path computation; the current file loader
refuses empty node/link lists, so a caller must report the empty state explicitly.

## Conversion and limitations

- Identity includes Protocol-ID, Instance-ID, every node descriptor and every
  directional link descriptor. Reordering TLVs does not change identity.
  Reverse and parallel links remain separate; no reverse edges are synthesized.
- Only IS-IS L1/L2 and OSPFv2 non-pseudonode, numbered IPv4 links are supported.
  Nodes need an advertised IPv4 TE Router-ID. Prefix/extension NLRIs, pseudonodes,
  unnumbered, IPv6 and multi-topology links are excluded with diagnostics.
- TE metric, administrative group, maximum/reservable bandwidth and the full
  eight-priority unreserved vector must be present. Missing attributes remain
  unknown and exclude the edge. Float32 bytes/second become integer bits/second
  by multiplying by eight and flooring. Capacity is never rounded upward.
- Reserved bandwidth is a conservative inference from reservable bandwidth
  minus priority-7 unreserved bandwidth. It does not identify LSP owners.
  SRLGs are preserved as decimal strings; administrative bits are `bit-0` through
  `bit-31`. Existing named colors require an explicit policy mapping later.
- RFC 9552 does not supply physical interface admin/oper state. Existing model
  booleans indicate the advertised IGP adjacency, with the unknown physical
  states recorded in metadata. Interface names, protection, node roles and
  capabilities are not invented. `authoritative=False` prevents guarded-auto
  admission; manually approved delivery remains subject to the existing exact
  plan approval and operational verification checks.
- Freshness is the successful RIB poll/session observation time, not NLRI age:
  an unchanged route may legitimately be old. Full polls are bounded and require
  stable peer update counters, but are not an atomic transaction across the IGP.
  BGP-LS cannot prove that the producer exported its entire underlying TED.
- This source does not implement PCEP, RSVP signaling, router provisioning,
  LSP delegation or protocol interoperability certification.

## Primary references

- [GoBGP v3.37.0 RPC/API definitions](https://github.com/osrg/gobgp/blob/v3.37.0/api/gobgp.proto):
  `enable_only_binary`, path state, peer state and End-of-RIB fields.
- [GoBGP v3.37.0 CLI JSON conversion](https://github.com/osrg/gobgp/blob/v3.37.0/pkg/apiutil/util.go)
  and [NLRI/TE encoding](https://github.com/osrg/gobgp/blob/v3.37.0/pkg/packet/bgp/bgp.go):
  CLI JSON uses display strings for LS NLRIs; native bandwidth uses bytes/s.
- [GoBGP configuration example](https://github.com/osrg/gobgp/blob/v3.37.0/docs/sources/configuration.md).
- [RFC 9552](https://www.rfc-editor.org/rfc/rfc9552.html), Sections 5.2 and 5.3:
  unique descriptors, directional link information and TE attribute encodings.
- [RFC 5305 bandwidth encodings](https://www.rfc-editor.org/rfc/rfc5305.html#section-3.4)
  referenced by RFC 9552; unreserved bandwidth has priorities 0 through 7.
- [grpcurl](https://github.com/fullstorydev/grpcurl): supplied protobuf definitions
  let the client use a server without requiring gRPC reflection.
