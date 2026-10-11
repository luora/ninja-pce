#!/usr/bin/env bash
set -euo pipefail

mkdir -p /lab /outputs

# Seed the GNS3 persistent volume the first time the container starts.
if [ ! -f /lab/.ninja-pce-lab-seeded ]; then
  cp -a /opt/ninja-pce/gns3/lab-data/. /lab/
  touch /lab/.ninja-pce-lab-seeded
fi

# Add the optional policy to older seeded volumes without overwriting edits.
if [ ! -f /lab/daemon/guarded-auto.yml ]; then
  mkdir -p /lab/daemon
  cp /opt/ninja-pce/gns3/lab-data/daemon/guarded-auto.yml /lab/daemon/guarded-auto.yml
fi

cat <<'BANNER'

ninja-pce GNS3 lab container
============================
Useful commands:
  ninja-pce validate --topology /lab/topologies/gns3_multiarea.yml --demand /lab/demands/customer-a-50m.yml
  ninja-pce compute  --topology /lab/topologies/gns3_multiarea.yml --demand /lab/demands/customer-a-50m.yml --output-dir /outputs --show-candidates --show-pruned
  ninja-pce inventory-validate --topology /lab/topologies/gns3_multiarea.yml --inventory /lab/inventory/gns3_inventory.yml
  ninja-pce reconcile --topology /lab/topologies/gns3_multiarea.yml --demand /lab/demands/customer-a-50m.yml --inventory /lab/inventory/gns3_inventory.yml --output-dir /outputs/reconcile --state-dir /outputs/daemon-state --watch
  NINJA_PCE_PREVIEW_ONLY=1 ninja-pce daemon --config /lab/daemon/guarded-auto.yml
  bash /opt/ninja-pce/gns3/scripts/demo_run.sh

The default mode is interactive. Set NINJA_PCE_GNS3_AUTORUN=1 to start
the guarded-auto daemon on boot. Set NINJA_PCE_PREVIEW_ONLY=1 as a kill switch.
The static sample TED cannot authorize guarded writes; no automatic PCEP
failover is enabled. Use /outputs/daemon-state for every CLI delivery workflow
sharing this lab, matching state_dir in the daemon configuration.

BANNER

if [ "${NINJA_PCE_GNS3_AUTORUN:-}" = "1" ]; then
  exec ninja-pce daemon --config /lab/daemon/guarded-auto.yml
fi

exec /bin/bash
