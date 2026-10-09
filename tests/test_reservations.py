"""Synthetic TED and process tests; these do not contact network devices."""
import json
import multiprocessing
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from ninja_pce.cspf import CspfNoPath, compute_cspf
from ninja_pce.models import Link, Node, ServiceDemand, Topology
from ninja_pce.reservations import ReservationLedger, resource_lock
from ninja_pce.state import load_state, write_state


def topology(reserved=0, metadata=None):
    return Topology("test", {"A": Node("A", "10.255.0.1"), "B": Node("B", "10.255.0.2")},
                    [Link("A-B", "A", "B", 1, 100, reserved, bidirectional=False,
                          from_ip="10.0.0.1", to_ip="10.0.0.2")], metadata or {})


def reserve(state_dir, owner, amount=80):
    with resource_lock(state_dir):
        ledger = ReservationLedger(state_dir)
        ted = ledger.effective_topology(topology(), owner)
        result = compute_cspf(ted, ServiceDemand(owner, "A", "B", amount))
        ledger.reserve(owner, owner, result, "plan")


def _compete(state_dir, name, barrier, queue):
    barrier.wait()
    try:
        reserve(Path(state_dir), name)
        queue.put("admitted")
    except CspfNoPath:
        queue.put("no_path")


def test_100m_cannot_admit_two_80m_demands_across_restart(tmp_path):
    reserve(tmp_path, "A")
    with pytest.raises(CspfNoPath):
        reserve(tmp_path, "B")
    ledger = ReservationLedger(tmp_path)
    assert ledger.entries["A"]["resources"] == {"A-B": 80_000_000}
    assert "B" not in ledger.entries


def test_two_processes_admit_at_most_one_competing_demand(tmp_path):
    ctx = multiprocessing.get_context("spawn")
    barrier, queue = ctx.Barrier(2), ctx.Queue()
    processes = [ctx.Process(target=_compete, args=(str(tmp_path), name, barrier, queue)) for name in ("A", "B")]
    for process in processes:
        process.start()
    results = [queue.get(timeout=20) for _ in processes]
    for process in processes:
        process.join(timeout=20)
        assert process.exitcode == 0
    assert sorted(results) == ["admitted", "no_path"]


def test_duplicate_identity_in_another_state_file_is_rejected(tmp_path):
    reserve(tmp_path, "A")
    with pytest.raises(ValueError, match="Duplicate LSP identity"):
        ReservationLedger(tmp_path).assert_owner("A", "different-state.json")


def test_reroute_retains_old_resources_and_failed_pending(tmp_path):
    reserve(tmp_path, "A", 40)
    ledger = ReservationLedger(tmp_path)
    alternative = replace(topology(), links=[replace(topology().links[0], id="other")])
    result = compute_cspf(alternative, ServiceDemand("A", "A", "B", 30))
    ledger.reserve("A", "A", result, "new-plan")
    ledger.transition("A", "PUSHING")
    ledger.transition("A", "ROLLBACK_REQUIRED")
    recovered = ReservationLedger(tmp_path).entries["A"]
    assert recovered["resources"] == {"A-B": 40_000_000, "other": 30_000_000}
    assert recovered["status"] == "ROLLBACK_REQUIRED"


def confirmed_ted(ledger):
    attempted = datetime.fromisoformat(ledger.entries["A"]["network_attempted_at"])
    observed = attempted + timedelta(microseconds=1)
    return topology(80, {
        "source": "trusted-collector", "timestamp": observed.isoformat(),
        "complete": True, "authoritative": True,
        "reservations": [{"lsp_id": "A", "owner": "A", "source": "trusted-collector",
                          "observed_at": observed.isoformat(), "status": "VERIFIED",
                          "edges": ["A-B"], "bandwidth_bps": 80_000_000}],
    })


def test_explicit_owned_observation_avoids_double_counting_and_reuses_own_lsp(tmp_path):
    reserve(tmp_path, "A")
    ledger = ReservationLedger(tmp_path)
    ledger.transition("A", "PUSHING")
    ted = confirmed_ted(ledger)
    effective = ledger.effective_topology(ted, "B")
    assert effective.links[0].available_bps(7) == 20_000_000
    assert ledger.effective_topology(ted, "A").links[0].available_bps(7) == 100_000_000
    assert ledger.entries["A"]["resources"]["A-B"] == 80_000_000


@pytest.mark.parametrize("alter", ["timestamp_only", "wrong_owner", "wrong_edge", "wrong_source", "stale", "unreflected"])
def test_timestamp_or_ambiguous_observation_cannot_clear_pending(tmp_path, alter):
    reserve(tmp_path, "A")
    ledger = ReservationLedger(tmp_path)
    ledger.transition("A", "PUSHING")
    ted = confirmed_ted(ledger)
    metadata = ted.raw_metadata
    if alter == "timestamp_only":
        metadata.pop("reservations")
    elif alter == "wrong_owner":
        metadata["reservations"][0]["owner"] = "other"
    elif alter == "wrong_edge":
        metadata["reservations"][0]["edges"] = ["other"]
    elif alter == "wrong_source":
        metadata["reservations"][0]["source"] = "other"
    elif alter == "stale":
        metadata["timestamp"] = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    elif alter == "unreflected":
        ted = replace(ted, links=topology().links)
    assert ledger.effective_topology(ted, "B").links[0].pending_bw_bps == 80_000_000


def test_corrupt_checksum_fails_closed(tmp_path):
    reserve(tmp_path, "A")
    path = tmp_path / ".ninja-pce-reservations.json"
    data = json.loads(path.read_text())
    data["entries"]["A"]["resources"]["A-B"] = 0
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="checksum"):
        ReservationLedger(tmp_path)


def test_missing_ledger_with_existing_state_requires_recovery(tmp_path):
    write_state(tmp_path / "lsp.json", {"reservation_id": "A"})
    with pytest.raises(ValueError, match="ledger is missing"):
        ReservationLedger(tmp_path)


def test_legacy_state_migration_cannot_supply_verified_state(tmp_path):
    path = tmp_path / "old.json"
    path.write_text('{"version": 1, "last_applied_config_hash": "old"}')
    migrated = load_state(path, legacy=True)
    assert migrated["migrated_from"] == 1
    assert "last_verified" not in migrated
    write_state(path, migrated)
    assert load_state(path)["version"] == 2


def test_status_cannot_silently_release_capacity(tmp_path):
    reserve(tmp_path, "A")
    ledger = ReservationLedger(tmp_path)
    with pytest.raises(ValueError, match="automatic release"):
        ledger.transition("A", "RELEASED")
    ledger.transition("A", "ROLLED_BACK")
    assert ledger.effective_topology(topology(), "B").links[0].available_bps(7) == 20_000_000


def test_valid_checksum_does_not_bypass_reservation_schema_validation(tmp_path):
    reserve(tmp_path, "A")
    path = tmp_path / ".ninja-pce-reservations.json"
    data = load_state(path)
    data["entries"]["A"]["resources"]["A-B"] = -80_000_000
    write_state(path, data)
    with pytest.raises(ValueError, match="Malformed directional"):
        ReservationLedger(tmp_path)
