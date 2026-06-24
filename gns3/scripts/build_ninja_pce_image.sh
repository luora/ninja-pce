#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"

docker build -t ninja-pce:gns3 -f gns3/docker/ninja-pce/Dockerfile .

echo
echo "Built Docker image: ninja-pce:gns3"
echo "In GNS3: Edit > Preferences > Docker containers > New > Existing image > ninja-pce:gns3"
