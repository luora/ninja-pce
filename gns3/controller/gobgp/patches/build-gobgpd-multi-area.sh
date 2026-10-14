#!/usr/bin/env bash
# Build the isolated collector; never installs or starts a service.
set -euo pipefail
umask 077

PATCH_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
BUILD_ROOT="${1:-/tmp/ninja-gobgp-build}"
OUTPUT="${2:-/tmp/ninja-gobgpd-multi-area}"
COMMIT=9bc8e113e4de6b5542cfafe0c64403b129f4e544
SOURCE_SHA256=354f4b46f94d08c6f5f064db319a4b029b60e410955d89e96e8ea5552d7aa58a
GO_VERSION=1.24.1
GO_SHA256=cb2396bae64183cdccf81a9a6df0aea3bce9511fc21469fb89a0c00470088073

if [[ "$(uname -s)" != Linux || "$(uname -m)" != x86_64 ]]; then
  printf 'This pinned toolchain requires a Linux x86_64 build host.\n' >&2
  exit 1
fi
for command_name in curl tar sha256sum patch install; do
  command -v "$command_name" >/dev/null || { printf 'Missing required command: %s\n' "$command_name" >&2; exit 1; }
done
mkdir -p -- "$BUILD_ROOT/downloads" "$BUILD_ROOT/cache" "$BUILD_ROOT/gopath" "$BUILD_ROOT/tmp" "$BUILD_ROOT/logs"
BUILD_ROOT="$(cd -- "$BUILD_ROOT" && pwd)"
mkdir -p -- "$(dirname -- "$OUTPUT")"
OUTPUT="$(cd -- "$(dirname -- "$OUTPUT")" && pwd)/$(basename -- "$OUTPUT")"

download() {
  local filename="$1" url="$2" expected="$3"
  if [[ -f "$filename" ]] && printf '%s  %s\n' "$expected" "$filename" | sha256sum --check --status; then
    return
  fi
  curl --fail --location --show-error --retry 3 --connect-timeout 30 \
    --output "$filename.part" "$url"
  printf '%s  %s\n' "$expected" "$filename.part" | sha256sum --check
  mv -- "$filename.part" "$filename"
}

SOURCE_ARCHIVE="$BUILD_ROOT/downloads/gobgp-$COMMIT.tar.gz"
GO_ARCHIVE="$BUILD_ROOT/downloads/go$GO_VERSION.linux-amd64.tar.gz"
download "$SOURCE_ARCHIVE" "https://codeload.github.com/osrg/gobgp/tar.gz/$COMMIT" "$SOURCE_SHA256"
download "$GO_ARCHIVE" "https://go.dev/dl/go$GO_VERSION.linux-amd64.tar.gz" "$GO_SHA256"
TOOLCHAIN_ROOT="$(mktemp -d "$BUILD_ROOT/toolchain.XXXXXX")"
tar -xzf "$GO_ARCHIVE" -C "$TOOLCHAIN_ROOT"
SOURCE_ROOT="$(mktemp -d "$BUILD_ROOT/source.XXXXXX")"
tar -xzf "$SOURCE_ARCHIVE" -C "$SOURCE_ROOT" --strip-components=1
cp -- "$SOURCE_ROOT/pkg/packet/bgp/bgp.go" "$SOURCE_ROOT/pkg/packet/bgp/bgp.go.unpatched"
patch --batch --forward -d "$SOURCE_ROOT" -p1 < "$PATCH_DIR/0001-bgpls-node-canonical-identity.patch"

export GOTOOLCHAIN=local GOENV=off CGO_ENABLED=0 GOOS=linux GOARCH=amd64
export GOPATH="$BUILD_ROOT/gopath" GOMODCACHE="$BUILD_ROOT/gopath/pkg/mod" GOCACHE="$BUILD_ROOT/cache" TMPDIR="$BUILD_ROOT/tmp"
export GOROOT="$TOOLCHAIN_ROOT/go" GO111MODULE=on GOPRIVATE= GONOSUMDB= GONOPROXY=
export GOPROXY=https://proxy.golang.org GOSUMDB=sum.golang.org
export GOMAXPROCS=2 GOMEMLIMIT=1GiB GOFLAGS='-mod=readonly -p=2'
GO="$TOOLCHAIN_ROOT/go/bin/go"
cd -- "$SOURCE_ROOT"
"$GO" version

# Prove the regressions detect the upstream collision, then restore the fix.
cp -- pkg/packet/bgp/bgp.go pkg/packet/bgp/bgp.go.patched
cp -- pkg/packet/bgp/bgp.go.unpatched pkg/packet/bgp/bgp.go
if "$GO" test -count=1 -run '^(TestLsNodeCanonicalIdentity|TestLsNodeAreasSurviveIndependentWithdrawal|TestLsNodeMalformedCanonicalIdentity|TestLsNodeMalformedAreasSurviveIndependentWithdrawal)$' \
  ./pkg/packet/bgp ./internal/pkg/table > "$BUILD_ROOT/logs/negative-control.log" 2>&1; then
  printf 'Unpatched negative control unexpectedly passed.\n' >&2
  exit 1
fi
cp -- pkg/packet/bgp/bgp.go.patched pkg/packet/bgp/bgp.go
grep -q 'distinct node identities collide' "$BUILD_ROOT/logs/negative-control.log"
grep -q 'same OSPF router in two areas must retain two RIB destinations; got 1' "$BUILD_ROOT/logs/negative-control.log"
printf 'Upstream negative control reproduced both identity and RIB collisions.\n'
"$GO" test -count=1 ./pkg/packet/bgp ./internal/pkg/table 2>&1 | tee "$BUILD_ROOT/logs/tests.log"
"$GO" build -trimpath -buildvcs=false \
  -ldflags="-s -w -X github.com/osrg/gobgp/v3/internal/pkg/version.COMMIT=$COMMIT -X github.com/osrg/gobgp/v3/internal/pkg/version.IDENTIFIER=ninja-node-identity.1" \
  -o "$BUILD_ROOT/gobgpd-multi-area" ./cmd/gobgpd
"$GO" mod verify 2>&1 | tee "$BUILD_ROOT/logs/modules.log"
install -m 755 -- "$BUILD_ROOT/gobgpd-multi-area" "$OUTPUT"
"$OUTPUT" --version
sha256sum -- "$OUTPUT" | tee "$BUILD_ROOT/logs/binary.sha256"
BINARY_SHA256="$(sha256sum -- "$OUTPUT" | cut -d ' ' -f 1)"
PATCH_SHA256="$(sha256sum -- "$PATCH_DIR/0001-bgpls-node-canonical-identity.patch" | cut -d ' ' -f 1)"
cat > "$OUTPUT.provenance.json" <<EOF
{
  "schema_version": 1,
  "component": "gobgpd",
  "gobgp_version": "3.37.0",
  "gobgp_commit": "$COMMIT",
  "source_archive_sha256": "$SOURCE_SHA256",
  "patch_file": "0001-bgpls-node-canonical-identity.patch",
  "patch_sha256": "$PATCH_SHA256",
  "go_version": "go$GO_VERSION",
  "go_toolchain_sha256": "$GO_SHA256",
  "binary_sha256": "$BINARY_SHA256",
  "binary_version": "3.37.0-ninja-node-identity.1+commit.$COMMIT",
  "target_os": "linux",
  "target_arch": "amd64",
  "cgo_enabled": false,
  "tests": {"negative_control": "passed", "packet": "passed", "table": "passed", "module_verify": "passed"},
  "build_flags": ["-trimpath", "-buildvcs=false", "-ldflags=-s -w", "GOFLAGS=-mod=readonly -p=2", "GOMAXPROCS=2", "GOMEMLIMIT=1GiB"]
}
EOF
cp -- "$OUTPUT.provenance.json" "$BUILD_ROOT/logs/provenance.json"
printf 'Built isolated binary: %s\nBuild source and logs: %s\n' "$OUTPUT" "$BUILD_ROOT"
