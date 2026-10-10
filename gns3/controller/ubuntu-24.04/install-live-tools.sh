#!/usr/bin/env bash
# Install pinned upstream tools; does not start a service or change router state.
set -euo pipefail
test "$(uname -m)" = x86_64 || { echo 'This installer requires x86_64.' >&2; exit 1; }
ninja_source_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../../.." && pwd)"
ninja_tools_root="$ninja_source_root/build/controller-tools"
mkdir -p "$ninja_tools_root/gobgp-3.37.0" "$ninja_tools_root/grpcurl-1.9.3"

cd "$ninja_tools_root/gobgp-3.37.0"
curl --fail --location --retry 3 --output gobgp_3.37.0_linux_amd64.tar.gz \
  https://github.com/osrg/gobgp/releases/download/v3.37.0/gobgp_3.37.0_linux_amd64.tar.gz
curl --fail --location --retry 3 --output gobgp_3.37.0_checksums.txt \
  https://github.com/osrg/gobgp/releases/download/v3.37.0/gobgp_3.37.0_checksums.txt
sha256sum --check --ignore-missing gobgp_3.37.0_checksums.txt
tar -xzf gobgp_3.37.0_linux_amd64.tar.gz gobgp gobgpd
sudo install -m 755 gobgp gobgpd /usr/local/bin/

cd "$ninja_tools_root/grpcurl-1.9.3"
curl --fail --location --retry 3 --output grpcurl_1.9.3_linux_x86_64.tar.gz \
  https://github.com/fullstorydev/grpcurl/releases/download/v1.9.3/grpcurl_1.9.3_linux_x86_64.tar.gz
curl --fail --location --retry 3 --output grpcurl_1.9.3_checksums.txt \
  https://github.com/fullstorydev/grpcurl/releases/download/v1.9.3/grpcurl_1.9.3_checksums.txt
sha256sum --check --ignore-missing grpcurl_1.9.3_checksums.txt
tar -xzf grpcurl_1.9.3_linux_x86_64.tar.gz grpcurl
sudo install -m 755 grpcurl /usr/local/bin/
gobgp --version
grpcurl -version
