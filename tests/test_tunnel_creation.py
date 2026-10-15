"""Offline admission, crash/replay and exact ownership tests; no router I/O."""
from __future__ import annotations

import json
import multiprocessing
import struct
from dataclasses import replace
from datetime import datetime, timezone
from ipaddress import IPv4Address
from pathlib import Path
from typing import Any, cast

import pytest
from test_pcep_compute import prepare

from ninja_pce import tunnel_creation
from ninja_pce.cspf import CspfNoPath, compute_cspf
from ninja_pce.models import Link, Node, ServiceDemand, Topology
from ninja_pce.pcep import encode_object, parse_reports
from ninja_pce.pcep_compute import PcepPathComputer
from ninja_pce.pcep_initiation import encoded_bandwidth
from ninja_pce.reservations import ReservationLedger, resource_lock
from ninja_pce.state import digest, write_state
from ninja_pce.tunnel_creation import CreationManager

PEER = "10.255.0.1"


class Computer:
    def __init__(self, capacity: int = 1_000_000):
        self.headends = {PEER: PEER, "10.255.0.3": "10.255.0.3"}
        self.node_names = {PEER: "PE1", "10.255.0.2": "P1", "10.255.0.3": "PE3"}
        self.max_age_seconds = 30.0
        self.revision = 0
        self.error: str | None = None
        self.topology = Topology("synthetic directed lab", {
            "PE1": Node("PE1", PEER), "P1": Node("P1", "10.255.0.2"), "PE3": Node("PE3", "10.255.0.3"),
        }, [
            Link("PE1-P1", "PE1", "P1", 1, capacity / 1_000_000, reservable_bw_bps=capacity,
                 bidirectional=False, from_ip="10.0.12.1", to_ip="10.0.12.2"),
            Link("P1-PE3", "P1", "PE3", 1, capacity / 1_000_000, reservable_bw_bps=capacity,
                 bidirectional=False, from_ip="10.0.23.2", to_ip="10.0.23.3"),
        ], {"source": "bgp-ls", "timestamp": datetime.now(timezone.utc).isoformat(),
            "complete": True, "authoritative": False})

    def validated_snapshot(self) -> tuple[Topology, str]:
        if self.error:
            raise ValueError(self.error)
        return self.topology, digest({"revision": self.revision})


def manager(path: Path, computer: Computer | None = None, **options: Any) -> CreationManager:
    return CreationManager(cast(PcepPathComputer, computer or Computer()), path,
                           {"PE1": PEER}, ("PE3",), **options)


def plan(controller: CreationManager, key: str = "request-1", amount: int = 1_000_000,
         principal: str = "operator") -> dict[str, Any]:
    return controller.plan(principal, key, "PE1", "PE3", amount)


def hold(path: Path, operation: dict[str, Any]) -> dict[str, Any]:
    with resource_lock(path):
        return ReservationLedger(path).entries[operation["reservation_id"]]


def report(operation: dict[str, Any], *, code: int = 2, srp: int | None = None,
           removed: bool = False) -> dict[str, Any]:
    """Synthetic response encoded and parsed through the actual PCRpt codec."""
    def tlv(kind: int, value: bytes) -> bytes:
        return struct.pack("!HH", kind, len(value)) + value + bytes(-len(value) % 4)

    send = operation["send"]
    srp_id = send["srp_id"] if srp is None else srp
    ids = (IPv4Address(operation["source_ip"]).packed + struct.pack("!HH", 2, 1001) +
           IPv4Address(operation["source_ip"]).packed + IPv4Address(operation["destination_ip"]).packed)
    bits = (37 << 12) | 0x80 | 9 | (code << 4) | (4 if removed else 0)
    route = b"".join(bytes((1, 8)) + IPv4Address(address).packed + bytes((32, 0)) for address in operation["ero"])
    body = (encode_object(33, struct.pack("!II", 0, srp_id) + tlv(28, bytes(4))) +
            encode_object(32, struct.pack("!I", bits) + tlv(17, operation["symbolic_name"].encode()) + tlv(18, ids)) +
            encode_object(7, route) + encode_object(9, struct.pack("!IIIBBBB", 0, 0, 0, 7, 7, 0, 0)) +
            encode_object(5, bytes.fromhex(operation["bandwidth_wire"])))
    parsed = parse_reports(body, allow_delegation=True)[0]
    parsed.update(session_id=send["session_id"], received_at=datetime.now(timezone.utc).timestamp(), stale=False)
    return parsed


def sent(controller: CreationManager) -> dict[str, Any]:
    operation = plan(controller)
    return controller.mark_sending(operation["id"], "session-one", 5, "a" * 64)


def test_plan_persists_directed_hold_intent_and_unique_owned_name(tmp_path: Path) -> None:
    controller = manager(tmp_path)
    operation = plan(controller)
    assert operation["status"] == "PLANNED" and operation["verified"] is False
    assert operation["symbolic_name"].startswith("NINJA_CREATE_") and "Tunnel100" not in operation["symbolic_name"]
    assert operation["ero"] == ["10.0.12.2", "10.0.23.3"]
    assert operation["path_nodes"] == ["PE1", "P1", "PE3"]
    assert hold(tmp_path, operation)["resources"] == {"PE1-P1": 1_000_000, "P1-PE3": 1_000_000}
    restored = manager(tmp_path).get(operation["id"], "operator")
    assert restored == operation
    operation["ero"].clear()
    assert controller.get(operation["id"])["ero"]


def test_scoped_idempotency_survives_restart_and_conflicting_intent_is_rejected(tmp_path: Path) -> None:
    operation = plan(manager(tmp_path))
    assert plan(manager(tmp_path)) == operation
    with pytest.raises(ValueError, match="conflicts"):
        plan(manager(tmp_path), amount=500_000)
    with pytest.raises(ValueError, match="held or unresolved"):
        plan(manager(tmp_path), principal="another-operator")
    with pytest.raises(ValueError, match="Unknown"):
        manager(tmp_path).get(operation["id"], "another-operator")
    assert manager(tmp_path).list("another-operator") == []


@pytest.mark.parametrize("amount", [0, -1, True, 1.0, "1000000", 1_000_001])
def test_invalid_or_excessive_bandwidth_never_creates_a_hold(tmp_path: Path, amount: Any) -> None:
    with pytest.raises(ValueError):
        plan(manager(tmp_path), amount=amount)
    assert not (tmp_path / ".ninja-pce-reservations.json").exists()


def test_encoded_bandwidth_is_charged_conservatively_and_policy_covers_rounding(tmp_path: Path) -> None:
    requested = 100_000_001
    _, actual = encoded_bandwidth(requested)
    assert actual > requested
    with pytest.raises(ValueError, match="Encoded"):
        plan(manager(tmp_path, Computer(actual), max_bandwidth_bps=requested), amount=requested)
    operation = plan(manager(tmp_path, Computer(actual), max_bandwidth_bps=actual), amount=requested)
    assert operation["intent"]["bandwidth_bps"] == requested and operation["bandwidth_bps"] == actual
    assert set(hold(tmp_path, operation)["resources"].values()) == {actual}


def test_prior_holds_are_charged_without_unknown_or_owned_credit(tmp_path: Path) -> None:
    computer = Computer()
    with resource_lock(tmp_path):
        ledger = ReservationLedger(tmp_path)
        result = compute_cspf(computer.topology, ServiceDemand("existing", "PE1", "PE3", 0.75))
        ledger.reserve("external", "other-owner", result, "existing-plan")
    with pytest.raises(CspfNoPath):
        plan(manager(tmp_path, computer), amount=500_000)
    assert set(ReservationLedger(tmp_path).entries) == {"external"}


def test_one_way_graph_cannot_admit_reverse_creation(tmp_path: Path) -> None:
    computer = Computer()
    controller = CreationManager(cast(PcepPathComputer, computer), tmp_path,
                                 {"PE3": "10.255.0.3"}, ("PE1",))
    with pytest.raises(CspfNoPath):
        controller.plan("operator", "reverse", "PE3", "PE1", 1_000_000)


@pytest.mark.parametrize("problem", ["unnumbered", "parallel_ambiguous"])
def test_ambiguous_or_unnumbered_ero_cannot_reserve(tmp_path: Path, problem: str) -> None:
    computer = Computer()
    first = computer.topology.links[0]
    if problem == "unnumbered":
        computer.topology = replace(computer.topology, links=[replace(first, to_ip=None), computer.topology.links[1]])
    else:
        computer.topology = replace(computer.topology, links=[*computer.topology.links, replace(first, id="parallel")])
    with pytest.raises(ValueError, match="numbered|unique directed"):
        plan(manager(tmp_path, computer))
    assert not ReservationLedger(tmp_path).entries


@pytest.mark.parametrize("failure", ["changed", "collector_failed", "expired", "policy_changed"])
def test_writer_rechecks_fresh_ted_policy_and_expiry_before_durable_send(
    tmp_path: Path, monkeypatch, failure: str,
) -> None:
    computer = Computer()
    controller = manager(tmp_path, computer)
    operation = plan(controller)
    if failure == "changed":
        computer.revision += 1
    elif failure == "collector_failed":
        computer.error = "collector unavailable"
    elif failure == "expired":
        monkeypatch.setattr(tunnel_creation, "_now", lambda: datetime.fromisoformat(operation["expires_at"]))
    else:
        controller = manager(tmp_path, computer, max_bandwidth_bps=2_000_000)
    with pytest.raises(ValueError):
        controller.mark_sending(operation["id"], "session-one", 5, "a" * 64)
    assert manager(tmp_path, computer).get(operation["id"])["send"] is None
    assert hold(tmp_path, operation)["status"] == "PLANNED"


def test_send_boundary_is_durable_and_never_replayed_after_timeout_or_restart(tmp_path: Path) -> None:
    operation = sent(manager(tmp_path))
    assert manager(tmp_path).get(operation["id"])["send"] == operation["send"]
    with pytest.raises(ValueError, match="never replay"):
        manager(tmp_path).mark_sending(operation["id"], "session-two", 1, "b" * 64)
    uncertain = manager(tmp_path).uncertain(operation["id"], "acknowledgment timed out")
    assert uncertain["status"] == "ROLLBACK_REQUIRED"
    assert hold(tmp_path, operation)["resources"] == {"PE1-P1": 1_000_000, "P1-PE3": 1_000_000}
    with pytest.raises(ValueError, match="prewrite rejection"):
        manager(tmp_path).reject_before_send(operation["id"], "browser disconnected")


def test_down_ack_then_active_zero_srp_stays_unverified_and_held(tmp_path: Path) -> None:
    controller = manager(tmp_path)
    operation = sent(controller)
    down = report(operation, code=0)
    down["ero"] = []
    acknowledged = controller.observe(operation["id"], down)
    assert acknowledged["status"] == "APPLIED_UNVERIFIED" and acknowledged["reported"]["operational_state"] == "DOWN"
    active = controller.observe(operation["id"], report(operation, srp=0))
    assert active["reported"]["operational_state"] == "ACTIVE" and active["verified"] is False
    assert active["status"] == hold(tmp_path, operation)["status"] == "APPLIED_UNVERIFIED"


@pytest.mark.parametrize("changed", ["session", "srp", "created", "delegate", "sync", "stale", "old",
                                     "name", "identity", "endpoint", "ero", "bandwidth", "priority", "pst",
                                     "unknown_tlv", "error_tlv", "duplicate_tlv", "used_bandwidth"])
def test_uncorrelated_or_conflicting_report_never_refunds_or_claims_applied(
    tmp_path: Path, changed: str,
) -> None:
    controller = manager(tmp_path)
    operation = sent(controller)
    observed = report(operation)
    if changed == "session":
        observed["session_id"] = "old-session"
    elif changed == "srp":
        observed["srp_id"] = 0
    elif changed in {"created", "delegate"}:
        observed[changed] = False
    elif changed in {"sync", "stale"}:
        observed[changed] = True
    elif changed == "old":
        observed["received_at"] -= 60
    elif changed == "name":
        observed["symbolic_name_hex"] = b"Tunnel100".hex()
    elif changed == "identity":
        observed["identifiers"]["tunnel_id"] = 100
    elif changed == "endpoint":
        observed["identifiers"]["endpoint"] = "10.255.0.2"
    elif changed == "ero":
        observed["ero"][0]["address"] = "10.0.99.2"
    elif changed == "bandwidth":
        next(attr for attr in observed["attributes"] if attr["class"] == 5)["body_hex"] = struct.pack("!f", 0).hex()
    elif changed == "priority":
        next(attr for attr in observed["attributes"] if attr["class"] == 9)["body_hex"] = bytes(16).hex()
    elif changed == "unknown_tlv":
        observed["tlvs"].append({"type": 999, "value_hex": ""})
    elif changed == "error_tlv":
        observed["tlvs"].append({"type": 20, "value_hex": bytes(4).hex()})
    elif changed == "duplicate_tlv":
        observed["tlvs"].append(dict(observed["tlvs"][0]))
    elif changed == "used_bandwidth":
        observed["attributes"].append({"class": 5, "type": 5, "body_hex": bytes(4).hex()})
    else:
        observed["srp_tlvs"] = [{"type": 28, "value_hex": "00000001"}]
    with pytest.raises(ValueError):
        controller.observe(operation["id"], observed)
    assert controller.get(operation["id"])["status"] == "PUSHING"
    assert hold(tmp_path, operation)["current_bandwidth_bps"] == 1_000_000


def test_owned_cleanup_requires_persisted_boundary_and_independent_release(tmp_path: Path) -> None:
    controller = manager(tmp_path)
    operation = sent(controller)
    controller.observe(operation["id"], report(operation))
    removed = report(operation, removed=True, srp=6)
    with pytest.raises(ValueError, match="correlated"):
        controller.record_removal(operation["id"], removed)
    removing = controller.mark_removing(operation["id"], "session-one", 6, "c" * 64)
    assert removing["status"] == "ROLLBACK_REQUIRED"
    with pytest.raises(ValueError, match="unreplayed"):
        manager(tmp_path).mark_removing(operation["id"], "session-one", 7, "d" * 64)
    removed = report(operation, removed=True, srp=6)
    recorded = controller.record_removal(operation["id"], removed)
    assert recorded["cleanup"]["report_digest"] == digest(removed)
    assert hold(tmp_path, operation)["current_bandwidth_bps"] == 1_000_000
    with pytest.raises(ValueError, match="independent"):
        controller.confirmed_removal(operation["id"], removed)
    finished = controller.confirmed_removal(operation["id"], removed, resources_released=True)
    assert finished["status"] == "ROLLED_BACK"
    assert not ReservationLedger(tmp_path).entries


@pytest.mark.parametrize("changed", ["session", "srp", "identity", "removed"])
def test_owned_cleanup_never_accepts_unrelated_removal(tmp_path: Path, changed: str) -> None:
    controller = manager(tmp_path)
    operation = sent(controller)
    controller.observe(operation["id"], report(operation))
    controller.mark_removing(operation["id"], "session-one", 6, "c" * 64)
    observed = report(operation, removed=True, srp=6)
    if changed == "session":
        observed["session_id"] = "old-session"
    elif changed == "srp":
        observed["srp_id"] = 5
    elif changed == "identity":
        observed["plsp_id"] += 1
    else:
        observed["removed"] = False
    with pytest.raises(ValueError):
        controller.record_removal(operation["id"], observed)
    assert hold(tmp_path, operation)["current_bandwidth_bps"] == 1_000_000


def test_prewrite_rejection_releases_only_unsent_hold_and_history_is_bounded(tmp_path: Path) -> None:
    controller = manager(tmp_path, max_operations=2)
    first = plan(controller)
    rejected = controller.reject_before_send(first["id"], "writer disabled before send")
    assert rejected["status"] == "REJECTED" and not ReservationLedger(tmp_path).entries
    assert plan(controller) == rejected
    second = plan(controller, key="second")
    controller.reject_before_send(second["id"], "writer disabled before send")
    with pytest.raises(ValueError, match="history is full"):
        plan(controller, key="third")


def test_crash_after_hold_before_intent_blocks_duplicate_creation(tmp_path: Path, monkeypatch) -> None:
    controller = manager(tmp_path)

    def fail_save(_journal):
        raise OSError("simulated intent fsync failure")

    monkeypatch.setattr(controller, "_save", fail_save)
    with pytest.raises(OSError):
        plan(controller)
    assert ReservationLedger(tmp_path).entries
    with pytest.raises(ValueError, match="Orphan"):
        plan(manager(tmp_path))


def test_crash_during_send_persistence_retains_hold_and_fences_replay(tmp_path: Path, monkeypatch) -> None:
    controller = manager(tmp_path)
    operation = plan(controller)

    def fail_save(_journal):
        raise OSError("simulated send-boundary fsync failure")

    monkeypatch.setattr(controller, "_save", fail_save)
    with pytest.raises(OSError):
        controller.mark_sending(operation["id"], "session-one", 5, "a" * 64)
    with pytest.raises(ValueError, match="unresolved send"):
        manager(tmp_path).mark_sending(operation["id"], "session-one", 6, "b" * 64)
    assert hold(tmp_path, operation)["status"] == "PUSHING"


@pytest.mark.parametrize("lost", ["ledger", "journal", "checksum"])
def test_missing_or_corrupt_durable_evidence_blocks_new_creation(tmp_path: Path, lost: str) -> None:
    controller = manager(tmp_path)
    operation = plan(controller)
    if lost == "ledger":
        (tmp_path / ".ninja-pce-reservations.json").unlink()
    elif lost == "journal":
        controller.path.unlink()
    else:
        document = json.loads(controller.path.read_text())
        document["operations"][operation["id"]]["status"] = "REJECTED"
        controller.path.write_text(json.dumps(document))
    with pytest.raises(ValueError):
        plan(manager(tmp_path), key="new")


def _compete(path: str, name: str, barrier, queue) -> None:
    barrier.wait()
    try:
        plan(manager(Path(path), Computer(2_000_000)), key=name)
        queue.put("admitted")
    except ValueError:
        queue.put("blocked")


def test_two_processes_cannot_cross_single_outstanding_admission(tmp_path: Path) -> None:
    context = multiprocessing.get_context("spawn")
    barrier, queue = context.Barrier(2), context.Queue()
    processes = [context.Process(target=_compete, args=(str(tmp_path), name, barrier, queue)) for name in ("a", "b")]
    for process in processes:
        process.start()
    outcomes = [queue.get(timeout=20) for _ in processes]
    for process in processes:
        process.join(timeout=20)
        assert process.exitcode == 0
    assert sorted(outcomes) == ["admitted", "blocked"]


def test_actual_captured_ted_admits_and_collector_failure_blocks_send(tmp_path: Path, monkeypatch) -> None:
    inputs, state = tmp_path / "inputs", tmp_path / "state"
    inputs.mkdir()
    computer = prepare(inputs, monkeypatch)
    controller = CreationManager(computer, state, {"PE1": PEER}, ("PE3",))
    operation = plan(controller)
    assert operation["path_nodes"][0] == "PE1" and operation["path_nodes"][-1] == "PE3"
    write_state(computer.status_path, {"status": "unavailable", "observed_at": 0})
    with pytest.raises(ValueError, match="unavailable"):
        controller.mark_sending(operation["id"], "session-one", 5, "a" * 64)
    assert hold(state, operation)["status"] == "PLANNED"
