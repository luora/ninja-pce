#!/usr/bin/env bash
set -euo pipefail

mkdir -p /lab /outputs

# Seed the GNS3 persistent volume the first time the container starts.
if [ ! -f /lab/.ninja-pce-lab-seeded ]; then
  cp -a /opt/ninja-pce/gns3/lab-data/. /lab/
  touch /lab/.ninja-pce-lab-seeded
fi

cat <<'BANNER'

ninja-pce GNS3 lab container
============================
Useful commands:
  ninja-pce validate --topology /lab/topologies/gns3_multiarea.yml --demand /lab/demands/customer-a-50m.yml
  ninja-pce compute  --topology /lab/topologies/gns3_multiarea.yml --demand /lab/demands/customer-a-50m.yml --output-dir /outputs --show-candidates --show-pruned
  ninja-pce inventory-validate --topology /lab/topologies/gns3_multiarea.yml --inventory /lab/inventory/gns3_inventory.yml
  ninja-pce reconcile --topology /lab/topologies/gns3_multiarea.yml --demand /lab/demands/customer-a-50m.yml --inventory /lab/inventory/gns3_inventory.yml --output-dir /outputs/reconcile --watch
  bash /opt/ninja-pce/gns3/scripts/demo_run.sh

The default mode is preview-only. Nothing is pushed unless you explicitly use --execute --approve.

BANNER

exec /bin/bash
