#!/usr/bin/env bash
set -euo pipefail
OUTPUT_ROOT="${NINJA_PCE_OUTPUTS:-/outputs}"
find "$OUTPUT_ROOT" -type f | sort
