# GoBGP 3.37.0 BGP-LS node identity correction

This patch is for the separate multi-area collector. It builds a private
binary and does not install software, start GoBGP, or change the stock
single-area collector.

## Diagnosis and scope

In upstream v3.37.0, `Table.tableKey()` falls back to `nlri.String()` for
BGP-LS (`internal/pkg/table/table.go:442–469`). `LsAddrPrefix.String()` wraps
`LsNodeNLRI.String()`, whose display omits the OSPF area. Two Node NLRIs with
the same router ID, AS, BGP-LS ID, protocol and instance therefore share a RIB
destination even when their area descriptors differ. A later same-peer path
implicitly withdraws the first. Adj-RIB and several other destination maps
also use these strings, so changing only the global RIB key would leave other
collisions.

The patch preserves the readable node text and appends hexadecimal serialized
node NLRI bytes: protocol, instance identifier and the complete decoded local
node descriptor. This also distinguishes an absent descriptor from an explicit
zero, and preserves BGP router and confederation descriptors. Received node
sub-TLVs must already be unique and sorted; upstream rejects invalid ordering
and sorts descriptors created through its constructor. The string operation
does not reorder or modify the NLRI. Wire serialization, JSON field structure,
link/prefix NLRIs, and BGP path identifiers are unchanged. All protocols receive
the suffix, including IS-IS; consumers comparing exact node display strings
must account for it. Binary BGP-LS ingestion is unchanged.

Upstream already discards unknown node descriptor sub-TLVs when decoding.
This focused patch retains that behavior and keys every descriptor retained by
the supported decoder; it does not add support for unknown descriptor types.
It does not relax Ninja-PCE's missing-node exclusion.

If a malformed descriptor accepted by upstream cannot serialize, the node
string retains protocol, instance and deterministic JSON of its ordered
supported sub-TLVs, including each descriptor's type, value and presence. This
avoids a shared error key for distinct nodes. The invalid status remains
explicit. Decoder acceptance rules are unchanged. A malformed JSON identity
remains distinct from a valid hexadecimal identity even if their supported
fields match; this change does not normalize malformed routes into valid ones.

The private lab evidence on 2026-10-13 contained ten producer Node NLRIs
(including both area memberships of two ABRs), but the unpatched consumer RIB
contained eight, with only the area-one ABR nodes remaining. Evidence captures
are deliberately not copied into this patch directory.

## Pinned sources

| Input | Immutable pin | SHA-256 |
| --- | --- | --- |
| GoBGP v3.37.0 source | `9bc8e113e4de6b5542cfafe0c64403b129f4e544` | `354f4b46f94d08c6f5f064db319a4b029b60e410955d89e96e8ea5552d7aa58a` |
| Go Linux amd64 toolchain | `go1.24.1.linux-amd64.tar.gz` | `cb2396bae64183cdccf81a9a6df0aea3bce9511fc21469fb89a0c00470088073` |

The GoBGP tag was resolved with `git ls-remote` against the official repository;
the source archive is fetched by commit, then checked against the pinned hash.
Go 1.24.1 matches upstream's `toolchain` directive. Its hash was verified
against the official [Go release metadata](https://go.dev/dl/?mode=json&include=all).
Dependencies are fixed by upstream `go.mod`/`go.sum`, fetched through the Go
module proxy with checksum database verification, and checked with
`go mod verify`. The build disables automatic toolchain selection and module
file changes.

Primary source: [node string and decoder](https://github.com/osrg/gobgp/blob/v3.37.0/pkg/packet/bgp/bgp.go),
[RIB key](https://github.com/osrg/gobgp/blob/v3.37.0/internal/pkg/table/table.go),
[Adj-RIB](https://github.com/osrg/gobgp/blob/v3.37.0/internal/pkg/table/adj.go),
[toolchain directive](https://github.com/osrg/gobgp/blob/v3.37.0/go.mod).

## Build without global installation

On a Linux x86-64 host (including Ubuntu 24.04), run as the ordinary user.
The host needs `bash`, `curl`, CA certificates, `tar`, `patch`, and standard
coreutils. No existing Go installation or root package installation is used.
Allow roughly 80 MB for the toolchain download, plus dependency/build storage.

```bash
bash gns3/controller/gobgp/patches/build-gobgpd-multi-area.sh \
  /tmp/ninja-gobgp-build /tmp/ninja-gobgpd-multi-area
```

For a controller build, copy this directory intact to a private location, then
choose a private build directory and output path. Background execution with a
log is convenient on a slow console:

```bash
mkdir -p "$HOME/ninja-multi-area-build"
nohup bash ./patches/build-gobgpd-multi-area.sh \
  "$HOME/ninja-multi-area-build/work" \
  "$HOME/ninja-multi-area-build/gobgpd" \
  > "$HOME/ninja-multi-area-build/build.log" 2>&1 < /dev/null &
```

The script limits compilation to two processes and a 1 GiB Go memory budget,
uses private module/build caches, and builds `CGO_ENABLED=0`, Linux amd64 with
`-trimpath`, `-buildvcs=false`, and stripped symbols. The version suffix is
`ninja-node-identity.1` with the upstream commit recorded. It never starts the
output binary except for `--version`.

Before the patched full packet/RIB package suites run, an automated negative
control runs the new regressions against the original upstream node string.
It must fail with both the identity collision and one-destination result.
Patched tests cover two-area coexistence, same-area replacement, independently
decoded identical withdrawal, optional-zero/absent descriptors, BGP router and
confederation identity, protocol/instance isolation, IS-IS wire round-trips,
and destination independence from BGP path IDs. Raw UPDATE regressions also
exercise upstream's acceptance of descriptors with 1–3 trailing bytes,
deterministic fallback identities, same-area replacement, and an independently
decoded malformed withdrawal that leaves the other area intact. Logs and the output binary's
SHA-256 are saved under the private build directory. A provenance manifest is
written beside the binary as `<output>.provenance.json` and copied into
`logs/provenance.json`; it records source, toolchain, patch and binary hashes,
version, build settings, and successful validation steps.

The local Linux amd64 validation on 2026-10-14 passed the complete
`pkg/packet/bgp` and `internal/pkg/table` suites. In addition to the automated
upstream negative control, a local source-overlay negative control reproduces
the malformed-node collision with the earlier shared-error fallback. The
original build is retained separately as an undeployed binary-delta base.

The final stripped static Linux amd64 binary is 20,279,480 bytes with SHA-256
`3b65786c4927d1fd9c1682a3dba6ec7e6f9faceb29af3bb92c0fd268c4d576ca`.
An independent rebuild from a different source/toolchain extraction path was
byte-identical. Its patch SHA-256 is
`ff833d21abe13098a87be92a960ec3b018359748b686691527b70af5e696ce10`.
The final provenance manifest SHA-256 is
`283f5570360a2b968b9808d47b8b2c11da9f0dc35b026cf71f08f8804947e3c8`.

Deploy the output only through the separately reviewed multi-area service
configuration. Re-establish that collector's session to reconstruct its RIB;
changing an executable cannot restore NLRIs already overwritten in memory.
The stock GoBGP executable and single-area service remain separate.
