#!/usr/bin/env python3
"""Explicit read-only collection from an isolated GNS3 head-end; never sends config."""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from run import _within, sha256

from ninja_pce.cspf import compute_cspf
from ninja_pce.inventory import device_for_node, validate_inventory
from ninja_pce.loaders import load_demand, load_inventory, load_topology
from ninja_pce.southbound import build_push_plan, verify_plan


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--case", required=True)
    parser.add_argument("--inventory", required=True, type=Path)
    parser.add_argument("--isolated-lab-id", required=True, help="Must match manifest lab_id")
    parser.add_argument("--condition-evidence", required=True, help="Describe actual failure/action and collected supporting evidence")
    parser.add_argument("--output-dir", type=Path, default=Path("build/gns3-evidence"))
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_text())
    if args.isolated_lab_id != manifest["lab_id"]:
        parser.error("Lab identity must match the manifest")
    cases = [case for case in manifest["cases"] if case["id"] == args.case]
    if len(cases) != 1:
        parser.error("Select one case ID from the manifest")
    case = cases[0]
    root = args.manifest.parent
    topology_path, demand_path = _within(root, case["topology"]), _within(root, case["demand"])
    topology, demand = load_topology(topology_path), load_demand(demand_path)
    inventory = load_inventory(args.inventory)
    validate_inventory(inventory, topology, require_all_nodes=True)
    plan = build_push_plan(compute_cspf(topology, demand), topology, device_for_node(inventory, demand.source))
    result = verify_plan(plan, verification_attempts=1)
    versions = dict(result.precheck_output)
    evidence = {"scope": "integration", "lab_id": args.isolated_lab_id,
                "captured_at": datetime.now(timezone.utc).isoformat(),
                "condition_evidence": args.condition_evidence,
                "topology_sha256": sha256(topology_path), "demand_sha256": sha256(demand_path),
                "version_output": versions.get("show version", ""),
                "command_outputs": dict(result.postcheck_output), "status": result.status,
                "output_format": plan.output_format,
                "errors": list(result.verification_errors), "plan_id": plan.plan_id,
                "device": result.device, "commands_sent": result.commands_sent}
    args.output_dir.mkdir(parents=True, exist_ok=True)
    destination = args.output_dir / f"{args.case}.json"
    destination.write_text(json.dumps(evidence, indent=2) + "\n")
    print(destination)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
