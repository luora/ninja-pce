#!/usr/bin/env python3
"""Prepare offline case snapshots. Never connects to routers."""
from __future__ import annotations

import argparse
import copy
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
BASE = ROOT / "gns3/lab-data/topologies/gns3_multiarea.yml"
DEMAND = ROOT / "gns3/lab-data/demands/customer-a-50m.yml"
NAMES = ["Baseline 50M", "20M short path", "50M bottleneck pruning", "Parallel member",
         "Asymmetric reverse", "Link shutdown", "Router failure", "Priority preemption denied",
         "Competing demands", "Operationally DOWN", "Unnumbered strict refusal", "Stale TED",
         "Restart pending ledger", "Repeated topology", "ERO mismatch"]
LONG = ["PE1-P3", "P3-P4", "P4-PE3"]
SHORT = ["PE1-P1", "P1-P2", "P2-PE3"]


def prepare(destination: Path) -> Path:
    destination.mkdir(parents=True, exist_ok=True)
    base = yaml.safe_load(BASE.read_text())
    # Initial qualification uses one TE-visible backbone, avoiding inter-area assumptions.
    for node in base["nodes"]:
        node["area"] = "0.0.0.0"
    for link in base["links"]:
        link["area"] = "0.0.0.0"
    demand = yaml.safe_load(DEMAND.read_text())
    cases = []
    for number, name in enumerate(NAMES, 1):
        case_id = f"T{number:02}"
        topology, service = copy.deepcopy(base), copy.deepcopy(demand)
        kind, expected = "compute", LONG.copy()
        action = "Restore baseline; establish the 50M LSP, then collect operational evidence."
        if number == 2:
            service["service"]["bandwidth_mbps"] = 20
            expected = SHORT.copy()
            action = "Establish 20M with no color restriction; confirm the 20M P1-P2 boundary."
        elif number == 4:
            parallel = copy.deepcopy(topology["links"][3])
            parallel.update(id="PE1-P3-parallel", from_ip="10.0.113.1", to_ip="10.0.113.3", metric=20)
            topology["links"].append(parallel)
            expected[0] = parallel["id"]
            action = "Cable/address a second PE1-P3 link (10.0.113.0/29), TE metric20, 200M reservable."
        elif number == 5:
            service["service"].update(source="PE3", destination="PE1")
            topology["links"][5]["reverse"] = {"reservable_bw_mbps": 40, "reserved_bw_mbps": 0}
            expected = ["P2-PE3:reverse", "P2-P4", "P3-P4:reverse", "PE1-P3:reverse"]
            action = "Set PE3→P4 reservable40M; keep P4→PE3 unchanged. Signal reverse 50M."
        elif number == 6:
            topology["links"][3]["oper_up"] = False
            expected = ["PE1-P1", "P1-P3", "P3-P4", "P4-PE3"]
            action = "In the isolated lab shut PE1-P3, refresh TED, then reconcile."
        elif number == 7:
            for link in topology["links"]:
                if "P3" in (link["from"], link["to"]):
                    link["oper_up"] = False
            expected = ["PE1-P1", "P1-P2", "P2-PE3"]
            kind = "no_path"
            action = "Stop isolated P3; 50M has no alternative because P1-P2 has20M. Do not push."
        elif number == 8:
            service["service"].update(setup_priority=0, hold_priority=0)
            for link in topology["links"]:
                link["unreserved_bw_mbps"] = [90, 90, 90, 90, 90, 90, 20, 20]
                link["reserved_bw_mbps"] = min(link["reserved_bw_mbps"], 10)
            kind = "no_path"
            action = "Create lower-holding-priority reservations leaving20M nonpreemptive capacity. No auto preemption."
        elif number in {9, 13}:
            topology["links"] = [copy.deepcopy(topology["links"][3])]
            topology["links"][0].update(reservable_bw_mbps=100, reserved_bw_mbps=0)
            service["service"].update(destination="P3", bandwidth_mbps=80)
            expected = ["PE1-P3"]
            kind = "restart" if number == 13 else "competing"
            action = "Admit A80M on sole100M link; try distinct B80M. For T13 restart the controller between requests."
        elif number in {10, 15}:
            kind = "verification_down" if number == 10 else "verification_mismatch"
            action = "Keep desired TED; cause LSP DOWN." if number == 10 else "Alter router ERO to a different adjacency; retain desired TED."
        elif number == 11:
            topology["links"][3].pop("from_ip", None)
            topology["links"][3].pop("to_ip", None)
            kind = "render_refusal"
            action = "Use an unnumbered PE1-P3 TED entry; confirm rendering refuses without changing router."
        elif number == 12:
            topology["metadata"].update(authoritative=True, complete=True, source="isolated-gns3",
                                         timestamp=(datetime.now(timezone.utc) - timedelta(hours=1)).isoformat())
            kind = "stale"
            action = "Stop authoritative collector >60s; record daemon denial and zero push events."
        elif number == 14:
            kind = "repeat"
            action = "Repeat identical topology events then flap inside debounce window; record audit and push count."
        folder = destination / case_id
        if kind == "no_path":
            expected = []
        folder.mkdir(exist_ok=True)
        (folder / "topology.yml").write_text(yaml.safe_dump(topology, sort_keys=False))
        (folder / "demand.yml").write_text(yaml.safe_dump(service, sort_keys=False))
        cases.append({"id": case_id, "name": name, "kind": kind, "topology": f"{case_id}/topology.yml",
                      "demand": f"{case_id}/demand.yml", "expected_links": expected,
                      "operator_condition": action})
    manifest = destination / "manifest.json"
    manifest.write_text(json.dumps({"schema_version": 1, "lab_id": "ninja-pce-six-router-isolated",
                                    "cases": cases}, indent=2) + "\n")
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "build/gns3-validation")
    args = parser.parse_args()
    print(prepare(args.output))
    print("Prepared snapshots only. Configure/cable isolated licensed routers using docs/gns3_validation.md.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
