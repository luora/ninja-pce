"""One-use managed reinstatement preserves ownership, budget and uncertainty."""
from __future__ import annotations

import copy
import struct

import pytest
from test_pcep_failover import _observe, _progress
from test_pcep_managed_failover import hold, managed_lab

from ninja_pce.pcep_managed_failover import ManagedFailureRecoveryPlanner
from ninja_pce.state import load_state, write_state


def down_lab(tmp_path, monkeypatch):
    planner, computer, clock, report = managed_lab(tmp_path, monkeypatch)
    report.update(ero=[], operational_code=0, operational_state="DOWN", srp_id=0)
    next(a for a in report["attributes"] if (a["class"], a["type"]) == (5, 5))["body_hex"] = bytes(4).hex()
    planner._data["attempts"] = 1
    planner._data["last_attempt_at"] = clock[0] - 60
    planner._deny("managed_operation_or_report_authority_changed")
    planner.reinstatement_path = tmp_path / "request.json"
    _observe(planner, report)
    return planner, computer, clock, report


def approve(planner, report):
    plan = planner.reinstatement_plan(planner.peer, report)
    write_state(planner.reinstatement_path, plan)
    return plan


def test_review_does_not_write_and_send_consumes_existing_budget_with_no_fabricated_baseline(tmp_path, monkeypatch):
    planner, computer, clock, report = down_lab(tmp_path, monkeypatch)
    original = planner.operation_journal.read_bytes()
    saved = planner.path.read_bytes()
    before = hold(planner)
    plan = planner.reinstatement_plan(planner.peer, report)
    assert planner.path.read_bytes() == saved and hold(planner) == before
    assert planner.compute(planner.peer, report, 17) is None
    write_state(planner.reinstatement_path, plan)
    reply = planner.compute(planner.peer, report, 17)
    assert reply is not None and reply.outcomes[0]["status"] == "REINSTATEMENT_PATH"
    assert planner.snapshot()["attempts"] == 1
    planner.prepare_send(planner.peer, report, reply)
    pending = planner.snapshot()["pending"]
    assert planner.snapshot()["baseline"] is None
    assert planner.snapshot()["attempts"] == 2
    assert pending["intent_mode"] == "reinstatement"
    assert pending["reinstatement_id"] == plan["request_id"]
    assert set(hold(planner)["resources"]) >= set(before["resources"])
    assert planner.operation_journal.read_bytes() == original
    restarted = ManagedFailureRecoveryPlanner(
        computer, operation_journal=planner.operation_journal, operation_id=planner.operation_id,
        journal_path=planner.path, reinstatement_path=planner.reinstatement_path, clock=lambda: clock[0])
    assert restarted.snapshot()["blocked"] and restarted.snapshot()["attempts"] == 2
    assert restarted.compute(planner.peer, report, 18) is None
    active = _progress(report, reply, clock[0])
    next(a for a in active["attributes"] if (a["class"], a["type"]) == (5, 5))["body_hex"] = struct.pack("!f", 125000).hex()
    planner.observe(planner.peer, active, fresh=True, synchronized=True)
    assert planner.snapshot()["status"] == "RECOVERED" and planner.snapshot()["pending"] is None
    assert planner.snapshot()["baseline"]["ero"] == pending["ero"]
    with pytest.raises(ValueError):
        planner.reinstatement_plan(planner.peer, report, request_id=plan["request_id"])
    assert planner.operation_journal.read_bytes() == original


@pytest.mark.parametrize("changed", ["bandwidth", "sync", "delegate", "stale", "active", "old-report",
                                     "pending", "budget", "cooldown", "uncertain-block", "other-owner"])
def test_review_refuses_unsupported_or_uncertain_authority(tmp_path, monkeypatch, changed):
    planner, _, clock, report = down_lab(tmp_path, monkeypatch)
    before = hold(planner)
    if changed == "bandwidth":
        next(a for a in report["attributes"] if (a["class"], a["type"]) == (5, 1))["body_hex"] = struct.pack("!f", 125).hex()
    elif changed == "sync":
        report["sync"] = True
    elif changed == "delegate":
        report["delegate"] = False
    elif changed == "stale":
        report["stale"] = True
    elif changed == "active":
        report.update(operational_code=2, operational_state="ACTIVE")
    elif changed == "old-report":
        report["received_at"] = clock[0] - 31
    elif changed == "pending":
        planner._data["pending"] = {"uncertain": True}
    elif changed == "budget":
        planner._data["attempts"] = 3
    elif changed == "cooldown":
        planner._data["last_attempt_at"] = clock[0]
    elif changed == "uncertain-block":
        planner._deny("pending_operation_on_restart")
    else:
        ledger = load_state(planner.operation_journal.parent / ".ninja-pce-reservations.json")
        ledger["entries"]["PE1:iosxe:Tunnel1000"]["owner"] = "another-owner"
        write_state(planner.operation_journal.parent / ".ninja-pce-reservations.json", ledger)
    with pytest.raises(ValueError):
        planner.reinstatement_plan(planner.peer, report)
    if changed != "other-owner":
        assert hold(planner) == before


@pytest.mark.parametrize("changed", ["expiry", "session", "plsp", "resources", "journal", "path", "permissions"])
def test_request_is_bound_to_current_report_journal_holds_and_exact_path(tmp_path, monkeypatch, changed):
    planner, _, clock, report = down_lab(tmp_path, monkeypatch)
    plan = approve(planner, report)
    before = hold(planner)
    if changed == "expiry":
        clock[0] += 61
    elif changed == "session":
        report["session_id"] = "another-session"
    elif changed == "plsp":
        report["plsp_id"] += 1
    elif changed == "resources":
        ledger = load_state(planner.operation_journal.parent / ".ninja-pce-reservations.json")
        ledger["entries"]["PE1:iosxe:Tunnel1000"]["resources"]["other-edge"] = 1000000
        write_state(planner.operation_journal.parent / ".ninja-pce-reservations.json", ledger)
    elif changed == "journal":
        saved = load_state(planner.path)
        saved["attempts"] += 1
        write_state(planner.path, saved)
    elif changed == "path":
        plan["ero"][0] = "10.0.0.99"
        write_state(planner.reinstatement_path, plan)
    else:
        planner.reinstatement_path.chmod(0o644)
    _observe(planner, report)
    assert planner.compute(planner.peer, report, 17) is None
    assert planner.snapshot()["attempts"] == 1
    if changed != "resources":
        assert hold(planner) == before


def test_reinstatement_pending_must_have_exact_self_contained_identity_on_restart(tmp_path, monkeypatch):
    planner, computer, clock, report = down_lab(tmp_path, monkeypatch)
    approve(planner, report)
    reply = planner.compute(planner.peer, report, 17)
    planner.prepare_send(planner.peer, report, reply)
    saved = load_state(planner.path)
    saved["pending"]["bandwidth_wire"] = struct.pack("!f", 125).hex()
    write_state(planner.path, saved)
    with pytest.raises(ValueError, match="reinstatement authority"):
        ManagedFailureRecoveryPlanner(computer, operation_journal=planner.operation_journal,
                                      operation_id=planner.operation_id, journal_path=planner.path,
                                      clock=lambda: clock[0])


def test_revoked_request_at_prewrite_retains_budget_and_resources(tmp_path, monkeypatch):
    planner, _, _, report = down_lab(tmp_path, monkeypatch)
    approve(planner, report)
    before = copy.deepcopy(hold(planner))
    reply = planner.compute(planner.peer, report, 17)
    planner.reinstatement_path.unlink()
    with pytest.raises(ValueError):
        planner.prepare_send(planner.peer, report, reply)
    assert planner.snapshot()["attempts"] == 1 and planner.snapshot()["pending"] is None
    assert hold(planner) == before


def test_transient_restart_reason_does_not_replace_reviewed_durable_block(tmp_path, monkeypatch):
    planner, _, _, report = down_lab(tmp_path, monkeypatch)
    planner.failed("session_unavailable_or_stale")
    assert planner.snapshot()["reason"] == "session_unavailable_or_stale"
    _observe(planner, report)
    approve(planner, report)
    reply = planner.compute(planner.peer, report, 17)
    assert reply is not None
    planner.prepare_send(planner.peer, report, reply)
    assert planner.snapshot()["attempts"] == 2


def test_request_expiring_during_prewrite_computation_cannot_send(tmp_path, monkeypatch):
    planner, _, clock, report = down_lab(tmp_path, monkeypatch)
    approve(planner, report)
    reply = planner.compute(planner.peer, report, 17)
    original = planner._candidate
    before = hold(planner)

    def expire(candidate, ledger):
        result = original(candidate, ledger)
        clock[0] += 121
        return result

    monkeypatch.setattr(planner, "_candidate", expire)
    with pytest.raises(ValueError, match="expired"):
        planner.prepare_send(planner.peer, report, reply)
    assert planner.snapshot()["attempts"] == 1 and planner.snapshot()["pending"] is None
    assert hold(planner) == before
