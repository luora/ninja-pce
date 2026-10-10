#!/usr/bin/env python3
"""Offline scenario checks or evidence-backed GNS3 comparisons. No configuration calls."""
from __future__ import annotations

import argparse
import hashlib
import json
import tempfile
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ninja_pce.cspf import CspfNoPath, CspfSearchLimit, compute_cspf
from ninja_pce.loaders import load_demand, load_topology
from ninja_pce.models import ManagedDevice
from ninja_pce.render import render_config
from ninja_pce.reservations import ReservationLedger, authoritative_snapshot, resource_lock
from ninja_pce.southbound import build_push_plan
from ninja_pce.verification import CliOperationalVerifier


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _within(root: Path, name: str) -> Path:
    path = (root / name).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError("Case paths must remain inside the manifest directory")
    return path


def compute_case(case: dict[str, Any], root: Path) -> tuple[dict[str, Any], Any]:
    topology = load_topology(_within(root, case["topology"]))
    demand = load_demand(_within(root, case["demand"]))
    result = None
    try:
        result = compute_cspf(topology, demand)
    except CspfNoPath:
        if case["kind"] != "no_path":
            raise
        return {"outcome": "NO_PATH", "links": []}, None
    if case["kind"] == "no_path":
        raise AssertionError("Expected proven NO_PATH but a path was found")
    ids = [link.id for link in result.path_links]
    if ids != case["expected_links"]:
        raise AssertionError(f"Expected links {case['expected_links']}, computed {ids}")
    if case["kind"] == "render_refusal":
        try:
            render_config(result, topology, "iosxr")
        except ValueError:
            return {"outcome": "UNSUPPORTED_ERO_REFUSED", "links": ids}, None
        raise AssertionError("Unsupported strict encoding rendered successfully")
    if case["kind"] == "stale" and authoritative_snapshot(topology):
        raise AssertionError("Stale topology accepted")
    if case["kind"] in {"competing", "restart"}:
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            with resource_lock(state):
                ledger = ReservationLedger(state)
                ledger.reserve("PE1:iosxr:tunnel-te100", "A", result, "test-plan")
                ledger.transition("PE1:iosxr:tunnel-te100", "PUSHING")
            with resource_lock(state):
                ledger = ReservationLedger(state)
                try:
                    compute_cspf(ledger.effective_topology(topology, "B"), demand)
                except CspfNoPath:
                    pass
                else:
                    raise AssertionError("A80M and B80M admitted on100M")
    if case["kind"] == "repeat":
        repeated = compute_cspf(topology, demand)
        if repeated.to_summary_dict() != result.to_summary_dict():
            raise AssertionError("Repeated identical snapshot changed computation")
    plan = build_push_plan(result, topology,
                           ManagedDevice("lab-headend", demand.source, "192.0.2.1", "cisco_xr", "lab"))
    return {"outcome": "COMPUTED", "links": ids, "summary": result.to_summary_dict()}, plan


def compare_evidence(case: dict[str, Any], root: Path, evidence_dir: Path,
                     plan: Any, manifest: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
    evidence_path = evidence_dir / f"{case['id']}.json"
    if not evidence_path.exists():
        return "SKIP", "NOT EXECUTED: no device evidence supplied", {}
    evidence = json.loads(evidence_path.read_text())
    if evidence.get("scope") != "integration" or evidence.get("lab_id") != manifest["lab_id"]:
        return "INCONCLUSIVE", "Evidence scope/lab identity missing or mismatched", {}
    for key in ("topology", "demand"):
        if evidence.get(f"{key}_sha256") != sha256(_within(root, case[key])):
            return "INCONCLUSIVE", "Evidence input revision differs from computed case", {}
    if not evidence.get("condition_evidence") or not evidence.get("captured_at"):
        return "INCONCLUSIVE", "Missing dated evidence of required lab condition", {}
    captured = datetime.fromisoformat(evidence["captured_at"].replace("Z", "+00:00"))
    if not captured.tzinfo or not 0 <= (datetime.now(timezone.utc) - captured).total_seconds() <= 300:
        return "INCONCLUSIVE", "Evidence is stale or has no timezone", {}
    outputs = evidence.get("command_outputs")
    if not isinstance(outputs, dict) or not outputs:
        return "INCONCLUSIVE", "No raw operational command evidence", {}
    lifecycle = case["kind"] in {"competing", "restart", "repeat", "stale", "no_path", "render_refusal"}
    if lifecycle:
        # These claims require real audit evidence, not an operator-supplied boolean.
        return "INCONCLUSIVE", "Attach/review lifecycle audit and before/after router state; automated qualification pending", evidence
    if plan is None:
        return "INCONCLUSIVE", "No expected operational plan", evidence
    output_format = evidence.get("output_format", "iosxr")
    platforms = {"iosxr": "cisco_xr", "iosxe": "cisco_ios", "junos": "juniper_junos"}
    if output_format not in platforms:
        return "INCONCLUSIVE", "Unsupported evidence platform", evidence
    if output_format != plan.output_format:
        topology = load_topology(_within(root, case["topology"]))
        demand = load_demand(_within(root, case["demand"]))
        plan = build_push_plan(compute_cspf(topology, demand), topology,
                               ManagedDevice("lab-headend", demand.source, "192.0.2.1",
                                             platforms[output_format], "lab"))
    verification = CliOperationalVerifier().verify(
        output_format, evidence.get("version_output", ""), outputs, plan.expectation
    )
    if case["kind"] == "verification_down":
        text = "\n".join(outputs.values()).lower()
        passed = not verification.verified and "oper: down" in text and all(
            "state" in error.lower() for error in verification.errors
        )
    elif case["kind"] == "verification_mismatch":
        passed = not verification.verified and bool(verification.hops) and verification.errors == (
            "Missing or mismatched current explicit route",
        )
    else:
        passed = verification.verified
    if passed and case["id"] in {"T03", "T04", "T05", "T06"}:
        return "INCONCLUSIVE", "Head-end LSP matches; raw link/priority/failure condition evidence needs independent qualification", {
            "evidence_sha256": sha256(evidence_path), "verification": asdict(verification),
            "raw_evidence": evidence,
        }
    return ("PASS" if passed else "FAIL"), "; ".join(verification.errors) or "Matched operational LSP evidence", {
        "evidence_sha256": sha256(evidence_path), "verification": asdict(verification),
        "raw_evidence": evidence,
    }


def run(manifest_path: Path, scope: str, evidence_dir: Path | None = None,
        selected: str | None = None) -> dict[str, Any]:
    manifest = json.loads(manifest_path.read_text())
    if manifest.get("schema_version") != 1:
        raise ValueError("Unsupported manifest schema")
    if scope not in {"offline", "integration"}:
        raise ValueError("Scope must be offline or integration")
    cases = manifest.get("cases")
    expected_ids = {f"T{n:02}" for n in range(1, 16)}
    if not isinstance(cases, list) or len(cases) != 15 or {
        case.get("id") for case in cases if isinstance(case, dict)
    } != expected_ids:
        raise ValueError("Manifest must contain exactly one case for each ID T01-T15")
    if selected is not None and selected not in expected_ids:
        raise ValueError("Selected case is absent from the manifest")
    rows = []
    for case in cases:
        if selected and case["id"] != selected:
            continue
        row: dict[str, Any] = {"id": case["id"], "name": case["name"], "scope": scope,
                               "expected_links": case["expected_links"]}
        try:
            computed, plan = compute_case(case, manifest_path.parent)
            row["computed"] = computed
            if scope == "integration":
                status, reason, evidence = compare_evidence(case, manifest_path.parent,
                                                          evidence_dir or Path("/nonexistent"), plan, manifest)
                row.update(status=status, reason=reason, evidence=evidence)
            elif case["kind"] in {"verification_down", "verification_mismatch"}:
                row.update(status="SKIP", reason="Device operational behavior requires integration evidence; see mock pytest fixtures")
            else:
                row.update(status="PASS", reason="Offline computation/accounting assertion only; no router validation")
        except CspfSearchLimit as exc:
            row.update(status="INCONCLUSIVE", reason=f"SEARCH_LIMIT: {exc}")
        except (ValueError, RuntimeError, AssertionError, KeyError, TypeError) as exc:
            row.update(status="FAIL", reason=str(exc))
        rows.append(row)
    return {"schema_version": 1, "scope": scope, "lab_id": manifest["lab_id"],
            "timestamp": datetime.now(timezone.utc).isoformat(), "results": rows}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--scope", choices=["offline", "integration"], default="offline")
    parser.add_argument("--evidence-dir", type=Path)
    parser.add_argument("--case", choices=[f"T{n:02}" for n in range(1, 16)])
    parser.add_argument("--output", type=Path, default=Path("build/gns3-results.json"))
    args = parser.parse_args()
    report = run(args.manifest, args.scope, args.evidence_dir, args.case)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    for row in report["results"]:
        print(f"{row['id']} {row['scope']:11} {row['status']:12} {row['reason']}")
    return 1 if any(row["status"] == "FAIL" for row in report["results"]) else 0


if __name__ == "__main__":
    raise SystemExit(main())
