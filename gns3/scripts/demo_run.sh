#!/usr/bin/env bash
set -euo pipefail

LAB_ROOT="${NINJA_PCE_LAB:-/lab}"
OUTPUT_ROOT="${NINJA_PCE_OUTPUTS:-/outputs}"
TOPOLOGY="$LAB_ROOT/topologies/gns3_multiarea.yml"

mkdir -p "$OUTPUT_ROOT"

echo "[1/3] Base service demand: 50 Mbps PE1 -> PE3"
ninja-pce compute \
  --topology "$TOPOLOGY" \
  --demand "$LAB_ROOT/demands/customer-a-50m.yml" \
  --output-dir "$OUTPUT_ROOT/base" \
  --show-candidates \
  --show-pruned

echo
echo "[2/3] Simulated maintenance/failure: avoid PE1-P3"
ninja-pce compute \
  --topology "$TOPOLOGY" \
  --demand "$LAB_ROOT/demands/customer-a-50m-avoid-pe1-p3.yml" \
  --output-dir "$OUTPUT_ROOT/avoid-pe1-p3" \
  --show-candidates \
  --show-pruned

echo
echo "[3/3] Oversized demand: 120 Mbps PE1 -> PE3"
if ninja-pce compute \
  --topology "$TOPOLOGY" \
  --demand "$LAB_ROOT/demands/customer-b-120m.yml" \
  --output-dir "$OUTPUT_ROOT/customer-b-120m" \
  --show-candidates \
  --show-pruned; then
  echo "120 Mbps demand found a path. Review output under $OUTPUT_ROOT/customer-b-120m"
else
  echo "120 Mbps demand failed as expected in some lab states. This is useful for explaining constraints."
fi

echo
echo "Generated files:"
find "$OUTPUT_ROOT" -maxdepth 3 -type f | sort
