"""Private two-phase reinstatement review, freshness, and exclusive publication."""
from __future__ import annotations

import copy
import json
import os
import stat
import struct
from pathlib import Path

import pytest
from test_pcep_managed_failover import managed_lab

import ninja_pce.pcep_reinstate as cli
from ninja_pce.state import load_state, write_state


def private_lab(tmp_path, monkeypatch):
    planner, computer, clock, report = managed_lab(tmp_path, monkeypatch)
    monkeypatch.setattr(cli.time, "time", lambda: clock[0])
    monkeypatch.delenv("NINJA_PCE_PREVIEW_ONLY", raising=False)
    report.update(ero=[], rro=[], operational_code=0, operational_state="DOWN", srp_id=0)
    report["attributes"] = [item for item in report["attributes"] if item["class"] != 8]
    for item in report["attributes"]:
        if (item["class"], item["type"]) == (5, 5):
            item["body_hex"] = bytes(4).hex()
    planner._data.update(attempts=1, last_attempt_at=clock[0] - 40)
    planner._deny("managed_operation_or_report_authority_changed")
    capture = tmp_path / "capture.json"
    document = {
        "kind": "passive_pcep_capture", "listener_running": True, "capture_error": None,
        "generated_at": clock[0], "capabilities": {"stateful": True, "lsp_update": True,
                                                    "path_computation": True},
        "managed_failure_recovery": planner.snapshot(),
        "peers": {planner.peer: {
            "state": "ESTABLISHED", "fresh": True, "synchronized": True,
            "valid_until": clock[0] + 30, "session_id": report["session_id"],
            "reports": {"current": copy.deepcopy(report)},
        }},
    }
    write_state(capture, document)
    config = tmp_path / "runtime.json"
    config.write_text(json.dumps({
        "state_dir": str(planner.operation_journal.parent), "pcep_capture": str(capture),
        "computer": {"raw_path": str(computer.raw_path), "status_path": str(computer.status_path),
                     "topology_path": str(computer.topology_path), "required_peers": list(computer.required_peers),
                     "node_names": computer.node_names, "headends": computer.headends,
                     "max_age_seconds": computer.max_age_seconds},
    }))
    plan = tmp_path / "private" / "plan.json"
    request = tmp_path / "private" / "request.json"
    args = ["--config", str(config), "--operation", planner.operation_id,
            "--recovery-state", str(planner.path), "--capture", str(capture),
            "--request", str(request), "--plan", str(plan)]
    return planner, computer, clock, capture, plan, request, args


def mutate_capture(path: Path, change) -> None:
    document = load_state(path)
    change(document)
    write_state(path, document)


def current_report(document):
    return next(iter(next(iter(document["peers"].values()))["reports"].values()))


def test_plan_is_read_only_and_authorization_preserves_exact_private_artifact(tmp_path, monkeypatch):
    planner, computer, _, capture, plan, request, args = private_lab(tmp_path, monkeypatch)
    protected = [planner.path, planner.operation_journal,
                 planner.operation_journal.parent / ".ninja-pce-reservations.json", capture,
                 computer.raw_path, computer.status_path, computer.topology_path]
    before = {path: path.read_bytes() for path in protected}
    assert cli.main(["plan", *args]) == 0
    assert not request.exists()
    reviewed = load_state(plan)
    assert reviewed["kind"] == "managed_pcep_reinstatement"
    assert reviewed["expires_at"] - reviewed["created_at"] == 60
    assert stat.S_IMODE(plan.stat().st_mode) == 0o600
    assert {path: path.read_bytes() for path in protected} == before
    assert cli.main(["authorize", *args]) == 0
    assert load_state(request) == reviewed
    assert stat.S_IMODE(request.stat().st_mode) == 0o600
    assert {path: path.read_bytes() for path in protected} == before


@pytest.mark.parametrize("change", [
    lambda data: data.update(listener_running=False),
    lambda data: data.update(capture_error="capture failed"),
    lambda data: data.update(generated_at=data["generated_at"] - 6),
    lambda data: data.update(generated_at=data["generated_at"] + 1),
    lambda data: next(iter(data["peers"].values())).update(fresh=False),
    lambda data: next(iter(data["peers"].values())).update(synchronized=False),
    lambda data: next(iter(data["peers"].values())).update(valid_until=data["generated_at"]),
    lambda data: next(iter(data["peers"].values())).update(state="DISCONNECTED"),
    lambda data: current_report(data).update(sync=True),
    lambda data: current_report(data).update(stale=True),
    lambda data: current_report(data).update(removed=True),
    lambda data: current_report(data).update(session_id="another-session"),
])
def test_plan_refuses_stale_or_historical_listener_evidence(tmp_path, monkeypatch, change):
    _, _, _, capture, plan, request, args = private_lab(tmp_path, monkeypatch)
    mutate_capture(capture, change)
    assert cli.main(["plan", *args]) == 1
    assert not plan.exists() and not request.exists()


def test_latest_removed_report_cannot_be_hidden_by_older_healthy_report(tmp_path, monkeypatch):
    _, _, _, capture, plan, _, args = private_lab(tmp_path, monkeypatch)

    def add_removed(data):
        reports = next(iter(data["peers"].values()))["reports"]
        removed = copy.deepcopy(reports["current"])
        removed.update(received_at=removed["received_at"] + .1, removed=True)
        reports["newer"] = removed

    mutate_capture(capture, add_removed)
    assert cli.main(["plan", *args]) == 1 and not plan.exists()


@pytest.mark.parametrize("phase", ["plan", "authorize"])
def test_preview_only_never_publishes_authority(tmp_path, monkeypatch, phase):
    _, _, _, _, plan, request, args = private_lab(tmp_path, monkeypatch)
    if phase == "authorize":
        assert cli.main(["plan", *args]) == 0
    monkeypatch.setenv("NINJA_PCE_PREVIEW_ONLY", "1")
    assert cli.main([phase, *args]) == 1
    assert not request.exists()
    if phase == "plan":
        assert not plan.exists()


@pytest.mark.parametrize("change", ["expiry", "report", "journal", "bandwidth"])
def test_authorize_rechecks_reviewed_evidence_and_interval(tmp_path, monkeypatch, change):
    planner, _, clock, capture, plan, request, args = private_lab(tmp_path, monkeypatch)
    assert cli.main(["plan", *args]) == 0
    if change == "expiry":
        clock[0] += 61
    elif change == "report":
        mutate_capture(capture, lambda data: current_report(data).update(plsp_id=12345))
    elif change == "journal":
        document = load_state(planner.path)
        document["reason"] = "another explicit review is required"
        write_state(planner.path, document)
    else:
        def bandwidth(data):
            item = next(attr for attr in current_report(data)["attributes"]
                        if (attr["class"], attr["type"]) == (5, 1))
            item["body_hex"] = struct.pack("!f", 125).hex()
        mutate_capture(capture, bandwidth)
    original_plan = plan.read_bytes()
    assert cli.main(["authorize", *args]) == 1
    assert not request.exists() and plan.read_bytes() == original_plan


@pytest.mark.parametrize("phase", ["plan", "authorize"])
def test_existing_artifact_is_never_overwritten(tmp_path, monkeypatch, phase):
    _, _, _, _, plan, request, args = private_lab(tmp_path, monkeypatch)
    if phase == "authorize":
        assert cli.main(["plan", *args]) == 0
    target = plan if phase == "plan" else request
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("existing evidence")
    assert cli.main([phase, *args]) == 1
    assert target.read_text() == "existing evidence"


@pytest.mark.parametrize("artifact", ["--plan", "--request"])
def test_output_cannot_alias_a_runtime_input(tmp_path, monkeypatch, artifact):
    planner, _, _, _, _, _, args = private_lab(tmp_path, monkeypatch)
    before = planner.path.read_bytes()
    args[args.index(artifact) + 1] = str(planner.path)
    assert cli.main(["plan", *args]) == 1
    assert planner.path.read_bytes() == before


def test_exclusive_publication_loses_race_without_replacing_competing_request(tmp_path, monkeypatch):
    target = tmp_path / "request.json"
    original = os.link

    def racing_link(source, destination, **kwargs):
        Path(destination).write_text("competing request")
        return original(source, destination, **kwargs)

    monkeypatch.setattr(cli.os, "link", racing_link)
    with pytest.raises(FileExistsError):
        cli._create_private_state(target, {"kind": "test"})
    assert target.read_text() == "competing request"
    assert list(tmp_path.glob(".ninja-reinstatement-*")) == []
