# Verified offline controller bundles

The optional offline bundle profile extends the existing distribution builder
and installer. It packages a reviewed application wheel, pinned runtime wheels,
the qualified multi-area GoBGP daemon, grpcurl 1.9.3, provenance, deployment
scripts and documentation. Installation needs no Go toolchain, source builds
or package index. The existing external-tool installation path remains
available in [deployment.md](deployment.md).

The supported installation target is Ubuntu 24.04, Linux amd64, CPython 3.12.
The host must already have Python's venv/ensurepip support, OpenSSL, systemd,
and the normal system-account utilities. This bundle does not include OS
packages. Other platforms require separately qualified artifacts.

## Trust and release preparation

Run the builder and installer from a reviewed, trusted checkout. A script
inside a downloaded bundle cannot establish its own authenticity by verifying
that same bundle. Before running a bundled installer, independently
authenticate its code, or use the installer from a trusted checkout.

The release verification public key must reach the operator through a separate
trusted channel, with its fingerprint independently checked. The installer
rejects a trust key located inside the bundle. The signing private key stays
outside the checkout and output directory and is never copied into artifacts.
The manifest uses an OpenSSL SHA-256 detached signature; RSA 3072 is a suitable
release-key choice. Key custody, distribution, rotation and revocation remain
release-operator responsibilities. Tests generate disposable keys solely for
local regression checks.

Supply these release inputs from a trusted build environment:

- The GoBGP binary produced by the
  [qualified multi-area build](../gns3/controller/gobgp/patches/README.md)
  and its adjacent provenance JSON. The bundle validates the upstream
  commit, source/patch/toolchain hashes, version, target, binary checksum,
  disabled CGO, and passing negative-control, packet, table and module tests.
- The grpcurl 1.9.3 Linux x86-64 release binary. Verify its archive against the
  upstream release checksums before extracting it, then record the archive
  and binary checksums in provenance.
- A complete CPython 3.12/Linux amd64 runtime wheelhouse, selected with
  [deploy/constraints.txt](../deploy/constraints.txt).
- The release signing key.

The builder inspects native binaries as data; it never executes or compiles
them. Provenance is an assertion by the release signer about the build
evidence. It does not independently rerun the upstream Go tests.

grpcurl provenance has this shape; replace both checksum placeholders with
actual lowercase SHA-256 digests:

~~~json
{
  "schema_version": 1,
  "component": "grpcurl",
  "version": "1.9.3",
  "target_os": "linux",
  "target_arch": "amd64",
  "binary_sha256": "<sha256 of extracted grpcurl>",
  "archive_sha256": "<sha256 of verified upstream archive>",
  "source_url": "https://github.com/fullstorydev/grpcurl/releases/download/v1.9.3/grpcurl_1.9.3_linux_x86_64.tar.gz"
}
~~~

On a Linux amd64 / Python 3.12 build host with build/setuptools/wheel already
installed, assemble the release using local inputs:

~~~bash
python3 tools/build_distribution.py \
  --output-dir /tmp/ninja-pce-offline-candidate \
  --wheelhouse /srv/release-inputs/python312-wheels \
  --gobgpd /srv/release-inputs/gobgpd-multi-area \
  --gobgpd-provenance /srv/release-inputs/gobgpd-multi-area.provenance.json \
  --grpcurl /srv/release-inputs/grpcurl \
  --grpcurl-provenance /srv/release-inputs/grpcurl.provenance.json \
  --signing-key /srv/release-keys/ninja-pce-private.pem
~~~

The output directory must be absent or empty. The application version comes
from the built wheel; this PR does not declare v0.8.0 released. For a production
release, use a reviewed clean checkout and inspect the manifest's
source_revision and source_dirty fields before publishing.

The local wheelhouse path copies only ordinary .whl files. Dependency
resolution uses the staged wheelhouse with indexes, source packages, pip
configuration and pip environment overrides disabled. URL requirements and
unsafe constraints are rejected. A missing dependency aborts publication.

Alternatively, replace --wheelhouse with --download-dependencies to explicitly
download pinned wheels on the build host. No missing binary triggers a
download or compilation fallback.

## Inspect and install

The output is an extracted directory, following the existing distribution
format. --bundle accepts that directory; compressed-archive installation and
the proposed ninja-pce install shortcut are not implemented.

Its schema-v2 distribution-manifest.json binds every payload file to its
SHA-256, application version, target platform and native component contract.
distribution-manifest.sig authenticates the exact manifest bytes. SHA256SUMS
remains a convenience for transport integrity; it is not the trust anchor.

From the trusted checkout, render a deployment without root or service
changes:

~~~bash
python3 deploy/install.py \
  --bundle /srv/releases/ninja-pce-offline-candidate \
  --trusted-key /srv/release-trust/ninja-pce-public.pem \
  --output-dir /tmp/ninja-pce-deployment-review
~~~

Rendering authenticates the signature, checks the complete file inventory,
rejects symlinks/special files and unsafe paths, validates native provenance
and offline wheel metadata, and snapshots the verified inputs privately.
Render previews may run on other hosts; actual installation enforces the
qualified OS, architecture and interpreter.
Keep --output-dir outside the signed bundle; adding review files inside it
would invalidate its inventory, so the installer refuses that destination.

Review controller.json, domain-policy.json, gobgpd.toml,
ninja-controller.service, ninja-gobgpd.service and deployment-manifest.json.
The bootstrap configuration has no BGP peers, disables the BGP listener with
port = -1, and binds the GoBGP gRPC API to 127.0.0.1:50051. GoBGP creates a
BGP listener only for a positive port in the
[pinned server implementation](https://github.com/osrg/gobgp/blob/v3.37.0/pkg/server/server.go#L2324).
The controller service selects only bgpls and dashboard components, leaving
PCEP inactive. The dashboard binds to loopback.

After review, install with the same arguments plus --install:

~~~bash
sudo python3 deploy/install.py \
  --bundle /srv/releases/ninja-pce-offline-candidate \
  --trusted-key /srv/release-trust/ninja-pce-public.pem \
  --output-dir /tmp/ninja-pce-deployment-review \
  --install
~~~

Installation authenticates a new snapshot, resolves and installs wheels into a
temporary environment, checks the GoBGP configuration with the authenticated
daemon, then validates the application before persistent deployment changes.
Native tools go under /opt/ninja-pce/releases/<version>/bin, and their
provenance plus the signed manifest/signature stay under that release's
provenance directory. Runtime configurations live in /etc/ninja-pce.

The installer refuses differing existing configurations, service units and
release directories. Existing journals and reservation ledgers are preserved.
Upgrades and migration of an existing active PCEP deployment require separate
review; this fresh-install path does not overwrite them.

No service is enabled or started by installation. Activation remains an
explicit operational step after reviewing ports, addressing and control
network policy. The GoBGP unit has bounded restart attempts and binds its API
only to loopback. Profiling/metrics listeners are disabled, including during
configuration validation. The controller reads GoBGP through the bundled
grpcurl and packaged read-only protobuf schema.

For a configured topology source, supply all three reviewed inputs together:

~~~bash
python3 deploy/install.py \
  --bundle /srv/releases/ninja-pce-offline-candidate \
  --trusted-key /srv/release-trust/ninja-pce-public.pem \
  --config /srv/candidates/controller.json \
  --domain-policy /srv/candidates/domain-policy.json \
  --gobgp-config /srv/candidates/gobgpd.toml \
  --output-dir /tmp/ninja-pce-configured-review
~~~

Custom TOML rendering requires Python 3.11 or later; the install target already
requires 3.12. The managed source requires bgpls.endpoint to be
127.0.0.1:50051. Additional domain/ABR mapping inputs still follow the existing
deployment rules. This path always renders observation component selection;
it does not authorize PCEP or router writes.

## Acceptance scope

Regression tests cover real detached signing and verification, private
snapshots, tampering and invalid provenance, offline dependency resolution
failures, unsupported hosts, service rendering, refusal to overwrite existing
state, and manual activation. Native binaries in unit tests are inert ELF
fixtures; they do not certify live network behavior.

A release operator must still assemble a signed candidate using the real
qualified native artifacts and perform the disconnected fresh-Ubuntu-24.04 VM
installation test. No production signing key or native release binary is
created by this change.

Mode-aware readiness, an authenticated setup GUI, independent management
availability, live process ownership diagnostics and provisioning integration
belong to subsequent PRs. Until mode-aware readiness lands, the legacy
controller status can still report degraded with zero PCCs. This packaging
change makes no claim that the full v0.8.0 acceptance checklist is complete.
