"""Opt-in managed startup restoration keeps failure and session budgets distinct."""
from __future__ import annotations

import copy
import struct

import pytest
from test_pcep_failover import _observe, _progress, _publish
from test_pcep_managed_failover import hold, managed_lab

from ninja_pce.pcep_failover import RecoveryDeferred
from ninja_pce.pcep_managed_failover import ManagedFailureRecoveryPlanner
from ninja_pce.state import load_state, write_state


def startup_lab(tmp_path, monkeypatch):
    seed, computer, clock, report = managed_lab(tmp_path, monkeypatch)
    # Existing link-failure history must survive arbitrarily many later boots.
    seed._data.update(attempts=2, last_attempt_at=clock[0] - 60)
    seed._save()
    planner = ManagedFailureRecoveryPlanner(
        computer, operation_journal=seed.operation_journal, operation_id=seed.operation_id,
        journal_path=seed.path, startup_recovery=True, clock=lambda: clock[0])
    report.update(ero=[], operational_code=0, operational_state="DOWN", srp_id=0)
    next(a for a in report["attributes"] if (a["class"], a["type"]) == (5, 5))["body_hex"] = bytes(4).hex()
    return planner, computer, clock, report


def startup_reply(lab, *, srp=17):
    planner, _, clock, report = lab
    _observe(planner, report)
    assert planner.compute(planner.peer, report, srp) is None
    assert planner.snapshot()["status"] == "STARTUP_DEBOUNCING"
    clock[0] += 6
    _observe(planner, report)
    reply = planner.compute(planner.peer, report, srp)
    assert reply is not None and reply.outcomes[0]["status"] == "STARTUP_PATH"
    return reply


def complete(lab, reply):
    planner, _, clock, report = lab
    planner.prepare_send(planner.peer, report, reply)
    clock[0] += .1
    active = _progress(report, reply, clock[0])
    next(a for a in active["attributes"] if (a["class"], a["type"]) == (5, 5))["body_hex"] = struct.pack("!f", 125000).hex()
    _observe(planner, active)
    assert planner.snapshot()["status"] == "RECOVERED"
    assert planner.snapshot()["pending"] is None
    assert planner.snapshot()["attempts"] == 2


def next_session(lab, index, *, elapsed=31):
    planner, computer, clock, report = lab
    planner.failed("session_unavailable_or_stale")
    clock[0] += elapsed
    _publish(computer, clock, "bgpls-healthy-raw.json")
    report.update(session_id=f"startup-session-{index}", plsp_id=report["plsp_id"] + 1,
                  received_at=clock[0])


def test_default_has_no_automatic_down_empty_ero_authority(tmp_path, monkeypatch):
    planner, _, clock, report = managed_lab(tmp_path, monkeypatch)
    report.update(ero=[], operational_code=0, operational_state="DOWN", srp_id=0)
    next(a for a in report["attributes"] if (a["class"], a["type"]) == (5, 5))["body_hex"] = bytes(4).hex()
    before = hold(planner)
    for _ in range(3):
        _observe(planner, report)
        assert planner.compute(planner.peer, report, 17) is None
        clock[0] += 6
    assert planner.snapshot()["attempts"] == 0
    assert planner.snapshot()["baseline"] is planner.snapshot()["pending"] is None
    assert hold(planner) == before


def test_startup_intent_is_self_contained_and_processing_ack_is_not_recovery(tmp_path, monkeypatch):
    lab = startup_lab(tmp_path, monkeypatch)
    planner, _, clock, report = lab
    operation = planner.operation_journal.read_bytes()
    initial_hold = copy.deepcopy(hold(planner))
    reply = startup_reply(lab)
    assert planner.snapshot()["attempts"] == 2
    assert planner.snapshot()["startup_recovery"]["total_attempts"] == 0
    assert planner.snapshot()["baseline"] is None and hold(planner) == initial_hold
    planner.prepare_send(planner.peer, report, reply)
    pending = planner.snapshot()["pending"]
    startup = planner.snapshot()["startup_recovery"]
    assert pending["intent_mode"] == "startup" and pending["startup_attempt"] == 1
    assert pending["startup_policy_digest"] == startup["policy_digest"]
    assert pending["bandwidth_wire"] == struct.pack("!f", 125000).hex()
    assert pending["symbolic_name_hex"] == report["symbolic_name_hex"]
    assert pending["ownership_key"] == [18, "10.255.0.1", "10.255.0.6", 1000, "0aff0001"]
    assert startup["total_attempts"] == 1 and startup["last_session_id"] == report["session_id"]
    assert startup["records"][0]["report_token"] == planner._token(report)
    assert set(hold(planner)["resources"]) >= set(initial_hold["resources"])
    assert planner.snapshot()["attempts"] == 2
    clock[0] += .1
    acknowledged = _progress(report, reply, clock[0], active=False)
    _observe(planner, acknowledged)
    assert planner.snapshot()["pending"]["ack_seen"] is True
    assert planner.snapshot()["baseline"] is None
    assert planner.compute(planner.peer, report, 18) is None
    clock[0] += .1
    active = _progress(report, reply, clock[0])
    next(a for a in active["attributes"] if (a["class"], a["type"]) == (5, 5))["body_hex"] = struct.pack("!f", 125000).hex()
    _observe(planner, active)
    assert planner.snapshot()["pending"] is None
    assert planner.snapshot()["status"] == "RECOVERED"
    assert planner.snapshot()["baseline"]["ero"] == pending["ero"]
    assert planner.snapshot()["attempts"] == 2
    assert planner.operation_journal.read_bytes() == operation


def test_consumed_session_cannot_retry_but_new_session_can_restore(tmp_path, monkeypatch):
    lab = startup_lab(tmp_path, monkeypatch)
    planner, _, clock, report = lab
    complete(lab, startup_reply(lab))
    planner.failed("session_unavailable_or_stale")
    clock[0] += 31
    _observe(planner, report)
    assert planner.compute(planner.peer, report, 18) is None
    assert planner.snapshot()["status"] == "STARTUP_SESSION_CONSUMED"
    assert planner.snapshot()["startup_recovery"]["total_attempts"] == 1
    next_session(lab, 2)
    complete(lab, startup_reply(lab, srp=19))
    assert planner.snapshot()["startup_recovery"]["total_attempts"] == 2


def test_three_per_hour_limit_expires_without_resetting_total_or_failure_history(tmp_path, monkeypatch):
    lab = startup_lab(tmp_path, monkeypatch)
    planner, _, clock, report = lab
    for index in range(3):
        if index:
            next_session(lab, index)
        complete(lab, startup_reply(lab, srp=17 + index))
    next_session(lab, 3)
    _observe(planner, report)
    assert planner.compute(planner.peer, report, 20) is None
    assert planner.snapshot()["status"] == "STARTUP_RATE_LIMITED"
    assert planner.snapshot()["reason"] == "startup_window_budget_exhausted"
    before = copy.deepcopy(hold(planner))
    clock[0] += 3601
    _publish(lab[1], clock, "bgpls-healthy-raw.json")
    complete(lab, startup_reply(lab, srp=21))
    startup = planner.snapshot()["startup_recovery"]
    assert startup["total_attempts"] == 4
    assert len(startup["attempt_times"]) == 1
    assert planner.snapshot()["attempts"] == 2
    assert hold(planner) == before


def test_startup_cooldown_is_independent_of_existing_failure_cooldown(tmp_path, monkeypatch):
    lab = startup_lab(tmp_path, monkeypatch)
    planner, _, _, report = lab
    complete(lab, startup_reply(lab))
    next_session(lab, 2, elapsed=1)
    _observe(planner, report)
    assert planner.compute(planner.peer, report, 18) is None
    assert planner.snapshot()["reason"] == "startup_cooldown"
    assert planner.snapshot()["startup_recovery"]["total_attempts"] == 1
    assert planner.snapshot()["attempts"] == 2


def test_recent_history_is_bounded_while_total_remains_monotonic(tmp_path, monkeypatch):
    lab = startup_lab(tmp_path, monkeypatch)
    planner = lab[0]
    for index in range(18):
        if index:
            next_session(lab, index, elapsed=3601)
        complete(lab, startup_reply(lab, srp=17 + index))
    startup = planner.snapshot()["startup_recovery"]
    assert startup["total_attempts"] == 18
    assert len(startup["records"]) == 16
    assert [item["attempt"] for item in startup["records"]] == list(range(3, 19))
    assert len(startup["attempt_times"]) == 1
    assert planner.snapshot()["attempts"] == 2


@pytest.mark.parametrize("change", ["bandwidth", "admin-down", "delegate", "sync", "removed", "stale",
                                    "blocked", "operation", "owner", "resources"])
def test_startup_never_bypasses_profile_session_or_durable_authority(tmp_path, monkeypatch, change):
    lab = startup_lab(tmp_path, monkeypatch)
    planner, _, clock, report = lab
    if change == "bandwidth":
        next(a for a in report["attributes"] if (a["class"], a["type"]) == (5, 1))["body_hex"] = struct.pack("!f", 125).hex()
    elif change in {"admin-down", "delegate", "sync", "removed", "stale"}:
        report[{"admin-down": "administrative_up", "delegate": "delegate", "sync": "sync",
                "removed": "removed", "stale": "stale"}[change]] = change in {"sync", "removed", "stale"}
    elif change == "blocked":
        planner._deny("explicit_reconciliation_required")
    elif change == "operation":
        journal = load_state(planner.operation_journal)
        journal["operations"][planner.operation_id]["status"] = "RECOVERY_REQUIRED"
        write_state(planner.operation_journal, journal)
    else:
        path = planner.operation_journal.parent / ".ninja-pce-reservations.json"
        ledger = load_state(path)
        entry = ledger["entries"]["PE1:iosxe:Tunnel1000"]
        if change == "owner":
            entry["owner"] = "another-owner"
        else:
            entry["resources"]["untracked-edge"] = 1_000_000
        write_state(path, ledger)
    before = copy.deepcopy(hold(planner))
    for _ in range(2):
        planner.observe(planner.peer, report, fresh=change != "stale", synchronized=True)
        assert planner.compute(planner.peer, report, 17) is None
        clock[0] += 6
    assert planner.snapshot()["pending"] is planner.snapshot()["baseline"] is None
    assert planner.snapshot()["attempts"] == 2
    assert planner.snapshot()["startup_recovery"]["total_attempts"] == 0
    assert hold(planner) == before


def test_historical_sync_d0_grants_nothing_until_post_sync_live_d1(tmp_path, monkeypatch):
    lab = startup_lab(tmp_path, monkeypatch)
    planner, _, clock, report = lab
    historical = copy.deepcopy(report)
    historical.update(sync=True, delegate=False)
    _observe(planner, historical)
    assert planner.compute(planner.peer, historical, 17) is None
    clock[0] += 6
    _observe(planner, historical)
    assert planner.compute(planner.peer, historical, 17) is None
    assert not planner.snapshot()["blocked"]
    assert planner.snapshot()["startup_recovery"]["total_attempts"] == 0
    complete(lab, startup_reply(lab))


@pytest.mark.parametrize("withdrawal", ["delegate", "admin-down", "removed"])
def test_withdrawn_report_waits_without_poisoning_later_exact_delegation(tmp_path, monkeypatch, withdrawal):
    lab = startup_lab(tmp_path, monkeypatch)
    planner, _, clock, report = lab
    withdrawn = copy.deepcopy(report)
    withdrawn[{"delegate": "delegate", "admin-down": "administrative_up", "removed": "removed"}[withdrawal]] = (
        withdrawal == "removed")
    before = copy.deepcopy(hold(planner))
    for _ in range(3):
        _observe(planner, withdrawn)
        assert planner.compute(planner.peer, withdrawn, 17) is None
        clock[0] += 2
    assert not planner.snapshot()["blocked"]
    assert planner.snapshot()["pending"] is planner.snapshot()["baseline"] is None
    assert planner.snapshot()["startup_recovery"]["total_attempts"] == 0
    assert hold(planner) == before
    complete(lab, startup_reply(lab))


@pytest.mark.parametrize("withdrawal", ["delegate", "admin-down", "removed"])
def test_withdrawal_after_intent_keeps_uncertainty_blocked(tmp_path, monkeypatch, withdrawal):
    lab = startup_lab(tmp_path, monkeypatch)
    planner, _, _, report = lab
    planner.prepare_send(planner.peer, report, startup_reply(lab))
    pending = copy.deepcopy(planner.snapshot()["pending"])
    withdrawn = copy.deepcopy(report)
    withdrawn[{"delegate": "delegate", "admin-down": "administrative_up", "removed": "removed"}[withdrawal]] = (
        withdrawal == "removed")
    _observe(planner, withdrawn)
    assert planner.snapshot()["blocked"]
    assert planner.snapshot()["pending"] == pending
    _observe(planner, report)
    assert planner.compute(planner.peer, report, 18) is None
    assert planner.snapshot()["startup_recovery"]["total_attempts"] == 1


def test_reasserted_delegation_still_rejects_profile_drift_after_withdrawal(tmp_path, monkeypatch):
    planner, _, _, report = startup_lab(tmp_path, monkeypatch)
    withdrawn = {**report, "delegate": False}
    _observe(planner, withdrawn)
    changed = copy.deepcopy(report)
    next(a for a in changed["attributes"] if (a["class"], a["type"]) == (5, 1))["body_hex"] = struct.pack("!f", 125).hex()
    _observe(planner, changed)
    assert planner.snapshot()["blocked"]
    assert planner.snapshot()["startup_recovery"]["total_attempts"] == 0


@pytest.mark.parametrize("drift", ["bandwidth", "name", "priorities", "owner"])
def test_undelegated_report_still_checks_profile_and_immutable_ownership(tmp_path, monkeypatch, drift):
    planner, _, _, report = startup_lab(tmp_path, monkeypatch)
    report["delegate"] = False
    if drift == "bandwidth":
        for attr in report["attributes"]:
            if attr["class"] == 5:
                attr["body_hex"] = struct.pack("!f", 250000).hex()
    elif drift == "name":
        report["symbolic_name_hex"] = "changed".encode().hex()
    elif drift == "priorities":
        next(a for a in report["attributes"] if a["class"] == 9)["body_hex"] = bytes(16).hex()
    else:
        ledger_path = planner.operation_journal.parent / ".ninja-pce-reservations.json"
        ledger = load_state(ledger_path)
        ledger["entries"]["PE1:iosxe:Tunnel1000"]["owner"] = "another-owner"
        write_state(ledger_path, ledger)
    _observe(planner, report)
    assert planner.snapshot()["blocked"]
    assert planner.compute(planner.peer, report, 17) is None
    assert planner.snapshot()["startup_recovery"]["total_attempts"] == 0


def test_sparse_removal_invalidates_baseline_without_granting_write_authority(tmp_path, monkeypatch):
    lab = startup_lab(tmp_path, monkeypatch)
    planner, _, _, report = lab
    removed = {"identifiers": report["identifiers"], "removed": True}
    _observe(planner, removed)
    assert not planner.snapshot()["blocked"]
    assert planner.compute(planner.peer, removed, 17) is None
    assert planner.snapshot()["startup_recovery"]["total_attempts"] == 0
    complete(lab, startup_reply(lab))


def test_startup_graph_unavailable_before_holds_defers_without_spending_authority(tmp_path, monkeypatch):
    lab = startup_lab(tmp_path, monkeypatch)
    planner, computer, _, report = lab
    reply = startup_reply(lab)
    before = copy.deepcopy(hold(planner))
    monkeypatch.setattr(computer, "validate_reply", lambda reply: False)
    with pytest.raises(RecoveryDeferred):
        planner.prepare_send(planner.peer, report, reply)
    assert planner.snapshot()["status"] == "STARTUP_TOPOLOGY_UNAVAILABLE"
    assert not planner.snapshot()["blocked"]
    assert planner.snapshot()["pending"] is None
    assert planner.snapshot()["startup_recovery"]["total_attempts"] == 0
    assert planner.snapshot()["attempts"] == 2
    assert hold(planner) == before
    assert planner._proposal is None


def test_expired_manual_request_does_not_prevent_automatic_current_session_restoration(tmp_path, monkeypatch):
    lab = startup_lab(tmp_path, monkeypatch)
    planner, _, _, report = lab
    _observe(planner, report)
    planner.reinstatement_path = tmp_path / "expired-request.json"
    request = planner.reinstatement_plan(planner.peer, report)
    request["created_at"] -= 121
    request["expires_at"] -= 121
    write_state(planner.reinstatement_path, request)
    complete(lab, startup_reply(lab))
    assert planner.reinstatement_path.exists()
    assert planner.snapshot()["startup_recovery"]["total_attempts"] == 1


def test_current_live_session_can_restore_old_report_when_ted_settles_later(tmp_path, monkeypatch):
    lab = startup_lab(tmp_path, monkeypatch)
    planner, computer, clock, report = lab
    received = report["received_at"]
    clock[0] += computer.max_age_seconds + 60
    _publish(computer, clock, "bgpls-healthy-raw.json")
    assert report["received_at"] == received
    # observe(fresh=True) is the session owner's keepalive/sync proof. A report
    # captured on disk alone never supplies this current live authority.
    assert planner.compute(planner.peer, report, 17) is None
    complete(lab, startup_reply(lab))


def test_pending_process_restart_stays_blocked_without_replaying_or_releasing_holds(tmp_path, monkeypatch):
    lab = startup_lab(tmp_path, monkeypatch)
    planner, computer, clock, report = lab
    reply = startup_reply(lab)
    planner.prepare_send(planner.peer, report, reply)
    retained = copy.deepcopy(hold(planner))
    restarted = ManagedFailureRecoveryPlanner(
        computer, operation_journal=planner.operation_journal, operation_id=planner.operation_id,
        journal_path=planner.path, startup_recovery=True, clock=lambda: clock[0])
    assert restarted.snapshot()["blocked"]
    assert restarted.snapshot()["reason"] == "pending_operation_on_restart"
    _observe(restarted, report)
    assert restarted.compute(restarted.peer, report, 18) is None
    assert restarted.snapshot()["startup_recovery"]["total_attempts"] == 1
    assert restarted.snapshot()["attempts"] == 2 and hold(restarted) == retained


@pytest.mark.parametrize("change", ["bandwidth_wire", "startup_attempt", "startup_policy_digest",
                                    "outcome", "intent_mode"])
def test_pending_startup_requires_exact_self_contained_identity_on_reload(tmp_path, monkeypatch, change):
    lab = startup_lab(tmp_path, monkeypatch)
    planner, computer, clock, report = lab
    planner.prepare_send(planner.peer, report, startup_reply(lab))
    saved = load_state(planner.path)
    saved["pending"][change] = (9 if change == "startup_attempt" else [1] if change == "outcome"
                                 else "unknown" if change == "intent_mode" else "0" * 64)
    write_state(planner.path, saved)
    with pytest.raises(ValueError):
        ManagedFailureRecoveryPlanner(
            computer, operation_journal=planner.operation_journal, operation_id=planner.operation_id,
            journal_path=planner.path, startup_recovery=True, clock=lambda: clock[0])


def test_disabling_and_reenabling_startup_keeps_consumed_session_and_total(tmp_path, monkeypatch):
    lab = startup_lab(tmp_path, monkeypatch)
    planner, computer, clock, report = lab
    complete(lab, startup_reply(lab))
    disabled = ManagedFailureRecoveryPlanner(
        computer, operation_journal=planner.operation_journal, operation_id=planner.operation_id,
        journal_path=planner.path, clock=lambda: clock[0])
    assert disabled.snapshot()["startup_recovery"]["total_attempts"] == 1
    next_session((disabled, computer, clock, report), 2)
    _observe(disabled, report)
    assert disabled.compute(disabled.peer, report, 18) is None
    assert disabled.snapshot()["attempts"] == 2
    enabled = ManagedFailureRecoveryPlanner(
        computer, operation_journal=planner.operation_journal, operation_id=planner.operation_id,
        journal_path=planner.path, startup_recovery=True, clock=lambda: clock[0])
    assert enabled.snapshot()["startup_recovery"]["total_attempts"] == 1
    enabled_lab = (enabled, computer, clock, report)
    complete(enabled_lab, startup_reply(enabled_lab, srp=19))
    assert enabled.snapshot()["startup_recovery"]["total_attempts"] == 2


@pytest.mark.parametrize("change", ["null", "policy-bool", "total-bool", "version-bool", "times",
                                    "last-session", "record-attempt", "report-token", "historical-rate",
                                    "last-attempt-null", "last-attempt-bool", "last-attempt-string",
                                    "last-attempt-backdated"])
def test_retained_startup_policy_and_history_cannot_be_reset_or_tampered(tmp_path, monkeypatch, change):
    lab = startup_lab(tmp_path, monkeypatch)
    planner, computer, clock, _ = lab
    complete(lab, startup_reply(lab))
    saved = load_state(planner.path)
    startup = saved["startup_recovery"]
    if change == "null":
        saved["startup_recovery"] = None
    elif change == "policy-bool":
        startup["policy"]["one_per_session"] = 1
    elif change == "total-bool":
        startup["total_attempts"] = True
    elif change == "version-bool":
        startup["version"] = True
    elif change == "times":
        startup["attempt_times"] = []
    elif change == "last-session":
        startup["last_session_id"] = "unconsumed-session"
    elif change == "record-attempt":
        startup["records"][0]["attempt"] += 1
    elif change == "historical-rate":
        original = startup["records"][0]
        times = [original["created_at"] + elapsed for elapsed in (0, 30, 60, 90, 4000)]
        startup.update(total_attempts=5, last_session_id="historical-5", attempt_times=[times[-1]],
                       records=[{**original, "attempt": index, "session_id": f"historical-{index}",
                                 "created_at": stamp} for index, stamp in enumerate(times, 1)])
    elif change.startswith("last-attempt-"):
        saved["last_attempt_at"] = {"last-attempt-null": None, "last-attempt-bool": True,
                                    "last-attempt-string": "recent",
                                    "last-attempt-backdated": startup["records"][-1]["created_at"] - 1}[change]
    else:
        startup["records"][0]["report_token"] = "invalid-token"
    write_state(planner.path, saved)
    with pytest.raises(ValueError):
        ManagedFailureRecoveryPlanner(
            computer, operation_journal=planner.operation_journal, operation_id=planner.operation_id,
            journal_path=planner.path, startup_recovery=True, clock=lambda: clock[0])


def test_exhausted_link_failure_budget_is_preserved_during_independent_startup(tmp_path, monkeypatch):
    lab = startup_lab(tmp_path, monkeypatch)
    planner, _, _, report = lab
    planner._data["attempts"] = planner.max_attempts
    planner._save()
    reply = startup_reply(lab)
    planner.prepare_send(planner.peer, report, reply)
    assert planner.snapshot()["attempts"] == planner.max_attempts == 3
    assert planner.snapshot()["startup_recovery"]["total_attempts"] == 1
    assert planner.snapshot()["pending"]["intent_mode"] == "startup"
