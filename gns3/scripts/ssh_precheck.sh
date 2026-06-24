#!/usr/bin/env bash
set -euo pipefail

INVENTORY="${1:-/lab/inventory/gns3_inventory.yml}"
python /opt/ninja-pce/tools/ssh_precheck.py --inventory "$INVENTORY"
