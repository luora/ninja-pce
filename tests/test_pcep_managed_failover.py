"""Owned managed recovery, shared capacity and durable uncertainty boundaries."""
from __future__ import annotations

import copy
import json
import struct
from datetime import datetime, timezone
from ipaddress import IPv4Address

import pytest
from test_pcep_compute import PEER, prepare
from test_pcep_failover import PCEP, _adopt, _observe, _progress, _publish

import ninja_pce.pcep_compute as computation
from ninja_pce.pcep import parse_reports
from ninja_pce.pcep_failover import FailureRecoveryPlanner, _mapped_edges
from ninja_pce.pcep_managed_failover import ManagedFailureRecoveryPlanner
from ninja_pce.reservations import ReservationLedger, resource_lock
from ninja_pce.state import digest, load_state, write_state

OPERATION_ID = "a" * 32


def managed_lab(tmp_path, monkeypatch, *, peer=PEER):
    computer = prepare(tmp_path, monkeypatch)
    computer.headends = {peer: PEER}
    clock = [datetime.fromisoformat(json.loads(computer.raw_path.read_text())["captured_at"]).timestamp()]
    monkeypatch.setattr(computation, "_utcnow", lambda: datetime.fromtimestamp(clock[0], timezone.utc))
    report = parse_reports(bytes.fromhex((PCEP / "tunnel100-computed-active.hex").read_text())[4:],
                           allow_delegation=True)[0]
    identifiers = report["identifiers"]
    identifiers["tunnel_id"] = 1000
    identifiers["value_hex"] = (IPv4Address(PEER).packed +
                                struct.pack("!HH", identifiers["lsp_id"], 1000) +
                                IPv4Address(PEER).packed + IPv4Address(identifiers["endpoint"]).packed).hex()
    name = "Tunnel1000_w"
    report.update(symbolic_name=name, symbolic_name_hex=name.encode().hex(),
                  session_id="managed-session", received_at=clock[0], stale=False)
    for item in report["tlvs"]:
        if item["type"] == 18:
            item["value_hex"] = identifiers["value_hex"]
        elif item["type"] == 17:
            item["value_hex"] = name.encode().hex()
    for item in report["attributes"]:
        if item["class"] == 5:
            item["body_hex"] = struct.pack("!f", 125000).hex()
        elif item["class"] == 9:
            item["body_hex"] = (bytes(12) + bytes((7, 7, 0, 0))).hex()
    topology, _ = computer.validated_snapshot()
    ero = [hop["address"] for hop in report["ero"]]
    edges = _mapped_edges(topology, ero, PEER, identifiers["endpoint"])
    authorization = {"plan_id": "b" * 64, "peer": peer, "source_ip": PEER,
                     "destination_ip": identifiers["endpoint"], "tunnel_id": 1000,
                     "symbolic_name": name, "bandwidth_bps": 1_000_000,
                     "setup_priority": 7, "hold_priority": 7,
                     "expires_at": "2020-01-01T00:00:00+00:00", "ero": ero, "path_edges": edges}
    operation = {"id": OPERATION_ID, "status": "VERIFIED", "authorization": authorization,
                 "created_at": "2019-12-31T23:59:00+00:00",
                 "authorization_digest": digest(authorization), "owner": "cli-provision:" + OPERATION_ID,
                 "headend": "PE1", "destination": "PE3", "reservation_id": "PE1:iosxe:Tunnel1000",
                 "verification": {"independent": True, "scope": "headend-rsvp", "pcep_control": True,
                                  "checks": ["identity", "destination", "bandwidth", "ero", "signaling"]},
                 **{key: copy.deepcopy(authorization[key])
                    for key in ("plan_id", "tunnel_id", "bandwidth_bps", "ero", "path_edges")}}
    state_dir = tmp_path / "creation"
    path = state_dir / "cli-provisioning.json"
    with resource_lock(state_dir):
        write_state(state_dir / ".ninja-pce-reservations.json", {"entries": {
            operation["reservation_id"]: {"owner": operation["owner"], "plan_id": authorization["plan_id"],
                                           "status": "VERIFIED", "current_bandwidth_bps": 1_000_000,
                                           "current_edges": edges,
                                           "resources": {edge: 1_000_000 for edge in edges}},
        }})
        write_state(path, {"kind": "cli_tunnel_provisioning", "policy_digest": "c" * 64,
                           "operations": {OPERATION_ID: operation}})
    planner = ManagedFailureRecoveryPlanner(computer, operation_journal=path, operation_id=OPERATION_ID,
                                            journal_path=tmp_path / "recovery" / "managed.json",
                                            clock=lambda: clock[0])
    return planner, computer, clock, report


def replacement(lab):
    planner, computer, clock, report = lab
    _adopt(lab)
    clock[0] += 1
    _publish(computer, clock)
    _observe(planner, report)
    assert planner.compute(planner.peer, report, 17) is None
    assert planner.snapshot()["status"] == "DEBOUNCING"
    clock[0] += 6
    _observe(planner, report)
    return planner.compute(planner.peer, report, 17)


def hold(planner):
    return ReservationLedger(planner.operation_journal.parent).entries["PE1:iosxe:Tunnel1000"]


def test_verified_expired_creation_authority_adopts_only_healthy_exact_path(tmp_path, monkeypatch):
    lab = managed_lab(tmp_path, monkeypatch)
    planner, _, _, _ = lab
    before = hold(planner)
    _adopt(lab)
    assert planner.validate_authority()
    assert planner.snapshot()["attempts"] == 0
    assert hold(planner) == before
    assert planner.snapshot()["baseline"]["bandwidth_bps"] == 1_000_000


def test_recovery_retains_union_before_intent_and_preserves_immutable_creation(tmp_path, monkeypatch):
    lab = managed_lab(tmp_path, monkeypatch)
    planner, _, _, report = lab
    original_operation = planner.operation_journal.read_bytes()
    original_hold = hold(planner)
    reply = replacement(lab)
    assert reply is not None
    assert hold(planner) == original_hold
    original_prepare = FailureRecoveryPlanner.prepare_send
    seen = []

    def checking_prepare(self, peer, observed, candidate):
        saved = hold(planner)
        assert set(saved["resources"]) == set(original_hold["resources"]) | set(candidate.outcomes[0]["path_link_ids"])
        assert saved["current_edges"] == original_hold["current_edges"]
        assert saved["current_bandwidth_bps"] == original_hold["current_bandwidth_bps"]
        assert saved["plan_id"] == original_hold["plan_id"]
        assert planner.snapshot()["pending"] is None
        seen.append(True)
        original_prepare(self, peer, observed, candidate)

    monkeypatch.setattr(FailureRecoveryPlanner, "prepare_send", checking_prepare)
    planner.prepare_send(planner.peer, report, reply)
    assert seen and planner.snapshot()["pending"] is not None
    assert planner.operation_journal.read_bytes() == original_operation
    assert planner.validate_authority()


@pytest.mark.parametrize("changed", ["status", "operation", "verification", "policy", "resources",
                                     "hold_owner", "hold_plan", "hold_path", "hold_bandwidth", "topology_policy"])
def test_changed_authority_stops_before_any_recovery(tmp_path, monkeypatch, changed):
    planner, computer, _, report = managed_lab(tmp_path, monkeypatch)
    journal = load_state(planner.operation_journal)
    operation = journal["operations"][OPERATION_ID]
    ledger = ReservationLedger(planner.operation_journal.parent)
    entry = ledger.data["entries"][operation["reservation_id"]]
    if changed == "status":
        operation["status"] = "ROLLBACK_REQUIRED"
    elif changed == "operation":
        operation["reason"] = "externally changed"
    elif changed == "verification":
        operation["verification"]["independent"] = False
    elif changed == "policy":
        journal["policy_digest"] = "d" * 64
    elif changed == "resources":
        entry["resources"]["untracked-edge"] = 1_000_000
    elif changed == "hold_owner":
        entry["owner"] = "another-owner"
    elif changed == "hold_plan":
        entry["plan_id"] = "d" * 64
    elif changed == "hold_path":
        entry["current_edges"] = entry["current_edges"][:-1]
    elif changed == "hold_bandwidth":
        entry["current_bandwidth_bps"] = 500000
    else:
        computer.node_names["10.255.0.9"] = "another-node"
    write_state(planner.operation_journal, journal)
    ledger.save()
    before = ledger.path.read_bytes()
    assert not planner.validate_authority()
    planner.observe(planner.peer, report, fresh=True, synchronized=True)
    assert planner.snapshot()["blocked"] and planner.snapshot()["attempts"] == 0
    assert ledger.path.read_bytes() == before


@pytest.mark.parametrize("changed", ["bandwidth", "priorities", "name", "endpoint", "created"])
def test_exact_profile_rejects_other_report_constraints(tmp_path, monkeypatch, changed):
    planner, _, _, report = managed_lab(tmp_path, monkeypatch)
    if changed == "bandwidth":
        for attr in report["attributes"]:
            if attr["class"] == 5:
                attr["body_hex"] = struct.pack("!f", 250000).hex()
    elif changed == "priorities":
        next(attr for attr in report["attributes"] if attr["class"] == 9)["body_hex"] = bytes(16).hex()
    elif changed == "name":
        report["symbolic_name_hex"] = "another-name".encode().hex()
    elif changed == "endpoint":
        report["identifiers"]["endpoint"] = "10.255.0.5"
    else:
        report["created"] = True
    planner.observe(planner.peer, report, fresh=True, synchronized=True)
    assert planner.snapshot()["blocked"]
    assert planner.snapshot()["baseline"] is None


def test_other_owners_holds_block_computed_replacement_without_new_hold(tmp_path, monkeypatch):
    lab = managed_lab(tmp_path, monkeypatch)
    planner = lab[0]
    reply = replacement(lab)
    assert reply is not None
    initial = hold(planner)
    ledger = ReservationLedger(planner.operation_journal.parent)
    edges = reply.outcomes[0]["path_link_ids"]
    ledger.data["entries"]["another-tunnel"] = {
        "owner": "another-owner", "status": "PLANNED", "current_edges": edges,
        "current_bandwidth_bps": 1_000_000_000,
        "resources": {edge: 1_000_000_000 for edge in edges},
    }
    ledger.save()
    assert planner.compute(planner.peer, lab[3], 18) is None
    assert planner.snapshot()["status"] == "NO_REPLACEMENT"
    assert hold(planner) == initial
    with pytest.raises(ValueError, match="capacity"):
        planner.prepare_send(planner.peer, lab[3], reply)
    assert hold(planner) == initial
    assert planner.snapshot()["blocked"]


def test_uncertain_intent_failure_retains_both_holds_and_blocks_restart(tmp_path, monkeypatch):
    lab = managed_lab(tmp_path, monkeypatch)
    planner, computer, clock, report = lab
    reply = replacement(lab)
    assert reply is not None
    initial = hold(planner)

    def fail(*args):
        raise OSError("simulated journal persistence failure")

    monkeypatch.setattr(FailureRecoveryPlanner, "prepare_send", fail)
    with pytest.raises(OSError, match="simulated"):
        planner.prepare_send(planner.peer, report, reply)
    assert set(hold(planner)["resources"]) > set(initial["resources"])
    assert planner.snapshot()["blocked"]
    restarted = ManagedFailureRecoveryPlanner(computer, operation_journal=planner.operation_journal,
                                               operation_id=OPERATION_ID, journal_path=planner.path,
                                               clock=lambda: clock[0])
    assert restarted.snapshot()["blocked"]
    assert restarted.compute(restarted.peer, report, 18) is None


def test_pending_restart_cannot_replay_or_release_union(tmp_path, monkeypatch):
    lab = managed_lab(tmp_path, monkeypatch)
    planner, computer, clock, report = lab
    reply = replacement(lab)
    assert reply is not None
    planner.prepare_send(planner.peer, report, reply)
    retained = hold(planner)
    restarted = ManagedFailureRecoveryPlanner(computer, operation_journal=planner.operation_journal,
                                               operation_id=OPERATION_ID, journal_path=planner.path,
                                               clock=lambda: clock[0])
    assert restarted.snapshot()["blocked"] and restarted.snapshot()["attempts"] == 1
    assert restarted.compute(restarted.peer, report, 18) is None
    assert hold(planner) == retained


def test_managed_journal_requires_distinct_parent_and_cannot_adopt_foreign_journal(tmp_path, monkeypatch):
    planner, computer, _, _ = managed_lab(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="separate parent"):
        ManagedFailureRecoveryPlanner(computer, operation_journal=planner.operation_journal,
                                      operation_id=OPERATION_ID,
                                      journal_path=planner.operation_journal.parent / "recovery.json")
    saved = load_state(planner.path)
    saved.pop("managed_authority")
    write_state(planner.path, saved)
    with pytest.raises(ValueError, match="Untracked"):
        ManagedFailureRecoveryPlanner(computer, operation_journal=planner.operation_journal,
                                      operation_id=OPERATION_ID, journal_path=planner.path)


def test_down_empty_report_never_grants_initial_recovery_authority(tmp_path, monkeypatch):
    planner, _, _, report = managed_lab(tmp_path, monkeypatch)
    report.update(ero=[], operational_code=0, operational_state="DOWN")
    next(attr for attr in report["attributes"] if (attr["class"], attr["type"]) == (5, 5))[
        "body_hex"] = struct.pack("!f", 0).hex()
    planner.observe(planner.peer, report, fresh=True, synchronized=True)
    assert planner.compute(planner.peer, report, 1) is None
    assert planner.snapshot()["baseline"] is None
    assert planner.snapshot()["attempts"] == 0 and not planner.snapshot()["blocked"]


@pytest.mark.parametrize("delegate", [False, True])
def test_sync_observation_waits_for_live_delegation_without_baseline_or_write(tmp_path, monkeypatch, delegate):
    planner, _, _, report = managed_lab(tmp_path, monkeypatch)
    original_hold = hold(planner)
    synchronized = copy.deepcopy(report)
    synchronized.update(sync=True, delegate=delegate, srp_id=2)
    planner.observe(planner.peer, synchronized, fresh=True, synchronized=True)
    assert planner.compute(planner.peer, synchronized, 3) is None
    waiting = planner.snapshot()
    assert waiting["status"] == "WAITING_FRESH_REPORT" and not waiting["blocked"]
    assert waiting["baseline"] is None and waiting["pending"] is None and waiting["attempts"] == 0
    assert hold(planner) == original_hold
    planner.observe(planner.peer, report, fresh=True, synchronized=True)
    assert planner.compute(planner.peer, report, 4) is None
    assert planner.snapshot()["status"] == "MONITORING"
    assert planner.snapshot()["attempts"] == 0 and hold(planner) == original_hold


@pytest.mark.parametrize("method", ["observe", "compute"])
def test_sync_observation_still_blocks_changed_durable_authority(tmp_path, monkeypatch, method):
    planner, _, _, report = managed_lab(tmp_path, monkeypatch)
    journal = load_state(planner.operation_journal)
    journal["operations"][OPERATION_ID]["reason"] = "externally changed"
    write_state(planner.operation_journal, journal)
    report.update(sync=True, delegate=False)
    if method == "observe":
        planner.observe(planner.peer, report, fresh=True, synchronized=True)
    else:
        assert planner.compute(planner.peer, report, 1) is None
    assert planner.snapshot()["blocked"] and planner.snapshot()["status"] == "RECOVERY_REQUIRED"
    assert planner.snapshot()["attempts"] == 0


def test_sync_observation_does_not_clear_existing_recovery_block(tmp_path, monkeypatch):
    planner, _, _, report = managed_lab(tmp_path, monkeypatch)
    planner._deny("explicit_reconciliation_required")
    report.update(sync=True, delegate=False)
    planner.observe(planner.peer, report, fresh=True, synchronized=True)
    assert planner.compute(planner.peer, report, 1) is None
    assert planner.snapshot()["blocked"] and planner.snapshot()["status"] == "RECOVERY_REQUIRED"
    assert planner.snapshot()["reason"] == "explicit_reconciliation_required"


def test_stale_session_and_unrelated_peer_cannot_block_exact_managed_profile(tmp_path, monkeypatch):
    planner, _, _, report = managed_lab(tmp_path, monkeypatch)
    stale = copy.deepcopy(report)
    stale["stale"] = True
    planner.observe(planner.peer, stale, fresh=False, synchronized=False)
    assert planner.snapshot()["status"] == "WAITING_FRESH_REPORT"
    assert not planner.snapshot()["blocked"]
    assert planner.compute("10.255.0.9", report, 1) is None
    assert not planner.snapshot()["blocked"]


def test_correlated_active_recovery_keeps_initial_and_replacement_holds(tmp_path, monkeypatch):
    lab = managed_lab(tmp_path, monkeypatch)
    planner, _, clock, report = lab
    reply = replacement(lab)
    assert reply is not None
    planner.prepare_send(planner.peer, report, reply)
    retained = hold(planner)
    clock[0] += 1
    active = _progress(report, reply, clock[0])
    planner.observe(planner.peer, active, fresh=True, synchronized=True)
    assert planner.snapshot()["status"] == "RECOVERED"
    assert planner.snapshot()["pending"] is None and planner.snapshot()["attempts"] == 1
    assert hold(planner) == retained
    assert planner.validate_authority()


def test_changed_operation_between_computation_and_prewrite_is_refused(tmp_path, monkeypatch):
    lab = managed_lab(tmp_path, monkeypatch)
    planner, _, _, report = lab
    reply = replacement(lab)
    assert reply is not None
    initial = hold(planner)
    journal = load_state(planner.operation_journal)
    journal["operations"][OPERATION_ID]["status"] = "ROLLED_BACK"
    write_state(planner.operation_journal, journal)
    with pytest.raises(ValueError, match="authority"):
        planner.prepare_send(planner.peer, report, reply)
    assert hold(planner) == initial
    assert planner.snapshot()["attempts"] == 0 and planner.snapshot()["blocked"]


def test_uncertain_ledger_persistence_keeps_union_and_requires_explicit_reconciliation(tmp_path, monkeypatch):
    lab = managed_lab(tmp_path, monkeypatch)
    planner, computer, _, report = lab
    reply = replacement(lab)
    assert reply is not None
    initial = hold(planner)
    original_save = ReservationLedger.save

    def uncertain_save(ledger):
        original_save(ledger)
        raise OSError("simulated post-replace ledger uncertainty")

    monkeypatch.setattr(ReservationLedger, "save", uncertain_save)
    with pytest.raises(OSError, match="post-replace"):
        planner.prepare_send(planner.peer, report, reply)
    assert set(hold(planner)["resources"]) > set(initial["resources"])
    assert planner.snapshot()["blocked"] and planner.snapshot()["attempts"] == 0
    with pytest.raises(ValueError, match="held resources changed"):
        ManagedFailureRecoveryPlanner(computer, operation_journal=planner.operation_journal,
                                      operation_id=OPERATION_ID, journal_path=planner.path)


def test_missing_shared_lock_never_recreates_authority(tmp_path, monkeypatch):
    planner, _, _, _ = managed_lab(tmp_path, monkeypatch)
    lock = planner.operation_journal.parent / ".ninja-pce-resources.lock"
    lock.unlink()
    assert not planner.validate_authority()
    assert not lock.exists()


def test_verified_inter_area_tunnel_requires_unchanged_explicit_area_policy(tmp_path, monkeypatch):
    from test_multi_area_wiring import DESTINATION
    from test_multi_area_wiring import prepare as prepare_multi_area
    from test_pcep_compute import request

    seed_dir, multi_dir = tmp_path / "seed", tmp_path / "multi"
    seed_dir.mkdir()
    multi_dir.mkdir()
    seed, _, _, report = managed_lab(seed_dir, monkeypatch)
    computer = prepare_multi_area(multi_dir, monkeypatch)
    result = computer(PEER, request(destination=DESTINATION, bandwidth=125000,
                                    lspa=bytes(12) + bytes((7, 7, 0, 0)))).outcomes[0]
    assert result["status"] == "PATH"
    operation = load_state(seed.operation_journal)["operations"][OPERATION_ID]
    operation["destination"] = "PE4"
    operation["authorization"]["destination_ip"] = DESTINATION
    for field, value in (("ero", result["ero"]), ("path_edges", result["path_link_ids"])):
        operation[field] = copy.deepcopy(value)
        operation["authorization"][field] = copy.deepcopy(value)
    operation["authorization_digest"] = digest(operation["authorization"])
    state_dir = multi_dir / "creation"
    journal_path = state_dir / "cli-provisioning.json"
    initial = hold(seed)
    initial.update(current_edges=result["path_link_ids"],
                   resources={edge: 1_000_000 for edge in result["path_link_ids"]})
    with resource_lock(state_dir):
        write_state(state_dir / ".ninja-pce-reservations.json",
                    {"entries": {operation["reservation_id"]: initial}})
        write_state(journal_path, {"kind": "cli_tunnel_provisioning", "policy_digest": "c" * 64,
                                   "operations": {OPERATION_ID: operation}})
    ids = report["identifiers"]
    ids["endpoint"] = DESTINATION
    ids["value_hex"] = (IPv4Address(PEER).packed + struct.pack("!HH", ids["lsp_id"], 1000) +
                        IPv4Address(PEER).packed + IPv4Address(DESTINATION).packed).hex()
    next(tlv for tlv in report["tlvs"] if tlv["type"] == 18)["value_hex"] = ids["value_hex"]
    report["ero"] = [{"type": 1, "prefix_length": 32, "loose": False, "address": address}
                     for address in result["ero"]]
    now = datetime.fromisoformat(json.loads(computer.raw_path.read_text())["captured_at"]).timestamp()
    report["received_at"] = now
    planner = ManagedFailureRecoveryPlanner(computer, operation_journal=journal_path,
                                            operation_id=OPERATION_ID,
                                            journal_path=multi_dir / "recovery" / "managed.json",
                                            clock=lambda: now)
    planner.observe(PEER, report, fresh=True, synchronized=True)
    assert planner.compute(PEER, report, 1) is None
    assert planner.snapshot()["status"] == "MONITORING"
    assert planner.snapshot()["baseline"]["edges"] == result["path_link_ids"]
    assert planner.validate_authority()
    computer.ospf_multi_area = None
    assert not planner.validate_authority()
