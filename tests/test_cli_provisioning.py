"""Offline exact-plan provisioning tests with synthetic transport/evidence."""
from __future__ import annotations

import asyncio
import copy
import time
from dataclasses import replace
from datetime import datetime, timezone
from ipaddress import IPv4Address
from pathlib import Path
from typing import Any, cast

import pytest
from test_pcep_provisioning import report_for
from test_tunnel_creation import PEER, Computer

from ninja_pce import cli_provisioning
from ninja_pce.cli_provisioning import CliProvisioner
from ninja_pce.cspf import CspfNoPath
from ninja_pce.models import ManagedDevice, ServiceDemand
from ninja_pce.pcep_compute import PcepPathComputer
from ninja_pce.reservations import ReservationLedger, resource_lock
from ninja_pce.southbound import PushResult, SouthboundError
from ninja_pce.state import digest, load_state, write_state

INTENT = {"headend": "PE1", "destination": "PE3", "bandwidth_bps": 1_000_000}


def controller(tmp_path: Path, computer: Computer | None = None, **kwargs: Any) -> CliProvisioner:
    result = CliProvisioner(
        cast(PcepPathComputer, computer or Computer()), tmp_path / "state",
        {"PE1": ManagedDevice("PE1", "PE1", PEER, "cisco_ios", "operator",
                              password_env="NINJA_PCE_TEST_PASSWORD")},
        ("PE3",), "172.16.100.10", tmp_path / "capture.json", **kwargs,
    )
    write_state(result.pcep_capture, {
        "listener_running": True,
        "generated_at": time.time(),
        "provisioning_policy_digest": result.authorization.policy_digest,
        "capabilities": {"configuration_provisioning": True},
        "peers": {PEER: {"state": "ESTABLISHED", "fresh": True, "synchronized": True,
                          "valid_until": time.time() + 120}},
    })
    return result


def preview(provisioner: CliProvisioner, principal: str = "operator", intent: dict[str, Any] | None = None):
    return asyncio.run(provisioner.preview(principal, copy.deepcopy(INTENT if intent is None else intent)))


def test_preview_is_durable_but_never_contacts_device_or_reserves_bandwidth(tmp_path: Path):
    provisioner = controller(tmp_path, connect_handler=lambda **kwargs: pytest.fail("must not connect"))
    plan = preview(provisioner)
    assert plan["headend"] == "PE1" and plan["destination"] == "PE3"
    assert plan["bandwidth_bps"] == 1_000_000
    assert plan["tunnel_id"] == 1000
    assert plan["ero"] == ["10.0.12.2", "10.0.23.3"]
    assert "tunnel mpls traffic-eng bandwidth 1000" in plan["rendered_config"]
    assert "path-option 1 dynamic pce address 172.16.100.10" in plan["rendered_config"]
    assert "path-option 10 explicit name EP_NINJA_" in plan["rendered_config"]
    assert not ReservationLedger(provisioner.state_dir).entries
    saved = load_state(provisioner.path)
    assert saved["previews"][plan["plan_id"]] == plan
    plan["ero"].clear()
    assert load_state(provisioner.path)["previews"][plan["plan_id"]]["ero"]


def test_same_principal_preview_replaces_only_its_old_plan(tmp_path: Path):
    provisioner = controller(tmp_path)
    first = preview(provisioner)
    other = preview(provisioner, "another-operator")
    replacement = preview(provisioner)
    assert replacement["plan_id"] != first["plan_id"]
    assert replacement["tunnel_id"] == first["tunnel_id"] == 1000
    assert other["tunnel_id"] == 1001
    assert set(load_state(provisioner.path)["previews"]) == {replacement["plan_id"], other["plan_id"]}


@pytest.mark.parametrize("intent", [
    {**INTENT, "bandwidth_bps": 0}, {**INTENT, "bandwidth_bps": True},
    {**INTENT, "bandwidth_bps": 1_000_001}, {**INTENT, "headend": "P1"},
    {**INTENT, "destination": "PE1"}, {**INTENT, "tunnel_id": 100},
])
def test_preview_rejects_intent_outside_the_explicit_policy_without_hold(tmp_path: Path, intent):
    provisioner = controller(tmp_path)
    with pytest.raises(ValueError, match="authorized provisioning policy"):
        preview(provisioner, intent=intent)
    assert not ReservationLedger(provisioner.state_dir).entries


def test_preview_accounts_for_existing_shared_directional_holds(tmp_path: Path):
    computer = Computer()
    provisioner = controller(tmp_path, computer)
    with resource_lock(provisioner.state_dir):
        ledger = ReservationLedger(provisioner.state_dir)
        result = cli_provisioning.compute_cspf(computer.topology, ServiceDemand("existing", "PE1", "PE3", .5))
        ledger.reserve("other-tunnel", "another-owner", result, "other-plan")
    with pytest.raises(CspfNoPath):
        preview(provisioner)
    assert set(ReservationLedger(provisioner.state_dir).entries) == {"other-tunnel"}


@pytest.mark.parametrize("problem", ["unqualified", "preview_only", "expired_peer", "not_synced",
                                     "policy_changed", "pending_recovery", "collector_failed"])
def test_readiness_fails_closed_for_qualification_and_stale_controller_state(tmp_path: Path, monkeypatch, problem):
    computer = Computer()
    provisioner = controller(tmp_path, computer, qualified=problem != "unqualified")
    capture = load_state(provisioner.pcep_capture)
    if problem == "preview_only":
        monkeypatch.setenv("NINJA_PCE_PREVIEW_ONLY", "true")
    elif problem == "expired_peer":
        capture["peers"][PEER]["valid_until"] = time.time() - 1
    elif problem == "not_synced":
        capture["peers"][PEER]["synchronized"] = False
    elif problem == "policy_changed":
        capture["provisioning_policy_digest"] = "different"
    elif problem == "pending_recovery":
        capture["failure_recovery"] = {"pending": {"id": "pending-change"}}
    elif problem == "collector_failed":
        computer.error = "collector unavailable"
    write_state(provisioner.pcep_capture, capture)
    ready, reason = provisioner.readiness("PE1")
    assert ready is False and reason
    assert not ReservationLedger(provisioner.state_dir).entries


def test_ready_qualified_fresh_exact_pcc_profile(tmp_path: Path):
    assert controller(tmp_path, qualified=True).readiness("PE1") == (True, "")


@pytest.mark.parametrize("generated", ["stale", "future", "missing", "nan", "infinite", "boolean"])
def test_readiness_requires_finite_recent_listener_capture(tmp_path: Path, monkeypatch, generated):
    provisioner = controller(tmp_path, qualified=True)
    timestamp = time.time()
    capture = load_state(provisioner.pcep_capture)
    capture["generated_at"] = {
        "stale": timestamp - 5.001, "future": timestamp + 1.001, "missing": None,
        "nan": float("nan"), "infinite": float("inf"), "boolean": True,
    }[generated]
    original_load = cli_provisioning.load_state
    # JSON state checks already reject NaN/Infinity at the storage boundary.
    # Isolate the additional readiness guard against a malformed loaded value.
    monkeypatch.setattr(cli_provisioning, "load_state", lambda path: capture if path == provisioner.pcep_capture
                        else original_load(path))
    monkeypatch.setattr(cli_provisioning.time, "time", lambda: timestamp)
    ready, reason = provisioner.readiness("PE1")
    assert ready is False and "capture" in reason


@pytest.mark.parametrize("offset", [-5, 1])
def test_capture_freshness_accepts_only_the_inclusive_observed_flush_window(tmp_path: Path, monkeypatch, offset):
    provisioner = controller(tmp_path, qualified=True)
    timestamp = time.time()
    capture = load_state(provisioner.pcep_capture)
    capture["generated_at"] = timestamp + offset
    write_state(provisioner.pcep_capture, capture)
    monkeypatch.setattr(cli_provisioning.time, "time", lambda: timestamp)
    assert provisioner.readiness("PE1") == (True, "")


def test_restart_retains_preview_without_reconstructing_different_plan(tmp_path: Path):
    provisioner = controller(tmp_path)
    plan = preview(provisioner)
    restored = controller(tmp_path)
    assert load_state(restored.path)["previews"][plan["plan_id"]] == plan
    assert not ReservationLedger(restored.state_dir).entries


def result_for(plan, *, status="VERIFIED", controlled=True):
    # Synthetic result from the independently tested southbound verifier. This
    # layer checks the observed IOS XE PCE selection/delegation presentation.
    text = (
        "path option 1, (PCE address 172.16.100.10) type dynamic (Basis for Setup, path weight 2)\n"
        "State: dynamic path option 1 is active\n"
        "Delegation state: Working: yes   Protect: no\n"
        "Delegation peer: 172.16.100.10\n"
        "Created via PCUpd message from PCE server: 172.16.100.10\n"
    ) if controlled else "State: explicit path option 10 is active\n"
    return PushResult(plan.device.name, plan.device.host, len(plan.commands), "configuration accepted",
                      postcheck_output=((plan.postcheck_commands[0], text),),
                      status=status, plan_id=plan.plan_id)


def mocks(monkeypatch, *, status="VERIFIED", controlled=True, completed=True):
    writes, observations = [], []

    def push(plan, **kwargs):
        kwargs["pre_write_guard"]()
        writes.append((plan, kwargs))
        return result_for(plan, status=status, controlled=controlled)

    def verify(plan, **kwargs):
        observations.append((plan, kwargs))
        return result_for(plan, status=status, controlled=controlled)

    monkeypatch.setattr(cli_provisioning, "push_plan", push)
    monkeypatch.setattr(cli_provisioning, "verify_plan", verify)
    # Exact current PCRpt/update correlation is exercised with encoded reports
    # in test_pcep_provisioning. This suite isolates the conjunction of that
    # independent completion proof with the CLI evidence and write lifecycle.
    monkeypatch.setattr(CliProvisioner, "_completed_path", lambda self, op_id: completed)
    monkeypatch.setattr(cli_provisioning.time, "sleep", lambda _: None)
    return writes, observations


def test_exact_plan_create_is_idempotent_and_hold_precedes_a_single_write(tmp_path: Path, monkeypatch):
    writes, observations = mocks(monkeypatch)
    provisioner = controller(tmp_path, qualified=True)

    async def run():
        plan = await provisioner.preview("operator", dict(INTENT))
        first = await provisioner.create("operator", "request-1", dict(INTENT), approved_plan_id=plan["plan_id"])
        assert first["status"] == "PLANNED"
        hold = ReservationLedger(provisioner.state_dir).entries[first["reservation_id"]]
        assert hold["status"] == "PLANNED"
        assert hold["resources"] == {"PE1-P1": 1_000_000, "P1-PE3": 1_000_000}
        assert hold["plan_id"] == plan["plan_id"]
        repeated = await provisioner.create("operator", "request-1", dict(INTENT), approved_plan_id=plan["plan_id"])
        assert repeated["id"] == first["id"]
        await provisioner.close()
        return plan, first

    plan, first = asyncio.run(run())
    assert len(writes) == 1 and not observations
    assert writes[0][1]["approved_plan_id"] == plan["plan_id"]
    assert provisioner.get(first["id"])["status"] == "VERIFIED"
    assert provisioner.get(first["id"])["verification"]["pcep_control"] is True
    assert ReservationLedger(provisioner.state_dir).entries[first["reservation_id"]]["status"] == "VERIFIED"
    assert load_state(provisioner.state_dir / ("cli-evidence-" + first["id"] + ".json"))["result"]["status"] == "VERIFIED"

    restored = controller(tmp_path, qualified=True)
    repeated = asyncio.run(restored.create("operator", "request-1", dict(INTENT), approved_plan_id=plan["plan_id"]))
    assert repeated["id"] == first["id"]
    assert len(writes) == 1
    assert restored.list("another-operator") == []
    with pytest.raises(ValueError, match="Unknown"):
        restored.get(first["id"], "another-operator")


@pytest.mark.parametrize("change", ["qualification", "expired", "changed_route", "changed_capacity",
                                    "another_principal", "intent", "plan_id", "restart"])
def test_create_rechecks_exact_plan_policy_and_current_resources_before_holding_or_writing(
    tmp_path: Path, monkeypatch, change,
):
    writes, _ = mocks(monkeypatch)
    computer = Computer()
    provisioner = controller(tmp_path, computer, qualified=True)
    plan = preview(provisioner)
    intent, principal, approval = dict(INTENT), "operator", plan["plan_id"]
    if change == "qualification":
        provisioner.qualified = False
    elif change == "expired":
        monkeypatch.setattr(cli_provisioning, "_now", lambda: datetime.fromisoformat(plan["expires_at"]))
    elif change == "changed_route":
        computer.topology = replace(computer.topology, links=[
            replace(computer.topology.links[0], to_ip="10.0.12.22"), computer.topology.links[1],
        ])
    elif change == "changed_capacity":
        computer.topology = replace(computer.topology, links=[
            replace(link, pending_bw_bps=1) for link in computer.topology.links
        ])
    elif change == "another_principal":
        principal = "another-operator"
    elif change == "intent":
        intent["bandwidth_bps"] = 500_000
    elif change == "plan_id":
        approval = "0" * 64
    else:
        provisioner = controller(tmp_path, computer, qualified=True)

    async def run():
        rejected = await provisioner.create(principal, "request-1", intent, approved_plan_id=approval)
        assert rejected["status"] == "REJECTED" and rejected["authorization"] is None
        await provisioner.close()

    asyncio.run(run())
    assert not writes
    assert not ReservationLedger(provisioner.state_dir).entries
    assert len(provisioner.list()) == 1 and provisioner.list()[0]["status"] == "REJECTED"


@pytest.mark.parametrize("controlled", [False, True])
def test_only_independent_rsvp_and_pce_control_evidence_can_mark_verified(tmp_path: Path, monkeypatch, controlled):
    writes, observations = mocks(monkeypatch, status="VERIFIED" if not controlled else "VERIFICATION_FAILED",
                                 controlled=controlled)
    provisioner = controller(tmp_path, qualified=True)

    async def run():
        plan = await provisioner.preview("operator", dict(INTENT))
        operation = await provisioner.create("operator", "request-1", dict(INTENT), approved_plan_id=plan["plan_id"])
        await provisioner.close()
        return operation

    operation = asyncio.run(run())
    observed = provisioner.get(operation["id"])
    assert observed["status"] == "VERIFICATION_FAILED"
    assert "verification" not in observed
    assert len(writes) == 1 and len(observations) == 4
    assert ReservationLedger(provisioner.state_dir).entries[operation["reservation_id"]]["resources"]


def test_positive_cli_proof_without_exact_pcep_completion_stays_unverified_and_held(tmp_path: Path, monkeypatch):
    writes, observations = mocks(monkeypatch, completed=False)
    provisioner = controller(tmp_path, qualified=True)

    async def run():
        plan = await provisioner.preview("operator", dict(INTENT))
        operation = await provisioner.create("operator", "request-1", dict(INTENT), approved_plan_id=plan["plan_id"])
        await provisioner.close()
        return operation

    operation = asyncio.run(run())
    assert provisioner.get(operation["id"])["status"] == "VERIFICATION_FAILED"
    assert "verification" not in provisioner.get(operation["id"])
    assert len(writes) == 1 and len(observations) == 4
    assert ReservationLedger(provisioner.state_dir).entries[operation["reservation_id"]]["resources"]


def test_postwrite_readonly_connection_failure_retains_hold_and_never_becomes_prewrite_rejection(
    tmp_path: Path, monkeypatch,
):
    writes, _ = mocks(monkeypatch, status="VERIFICATION_FAILED")

    def fail(*args, **kwargs):
        raise SouthboundError("Read-only verification connection failed")

    monkeypatch.setattr(cli_provisioning, "verify_plan", fail)
    provisioner = controller(tmp_path, qualified=True)

    async def run():
        plan = await provisioner.preview("operator", dict(INTENT))
        operation = await provisioner.create("operator", "request-1", dict(INTENT), approved_plan_id=plan["plan_id"])
        await provisioner.close()
        return operation

    operation = asyncio.run(run())
    assert len(writes) == 1
    assert provisioner.get(operation["id"])["status"] == "ROLLBACK_REQUIRED"
    assert ReservationLedger(provisioner.state_dir).entries[operation["reservation_id"]]["resources"]
    evidence = load_state(provisioner.state_dir / ("cli-evidence-" + operation["id"] + ".json"))
    assert evidence["failure"] == "Read-only verification connection failed"
    assert evidence["stage"] == "postwrite-observation"
    assert evidence["failure"] not in provisioner.get(operation["id"])["reason"]


def test_restart_after_durable_send_boundary_never_replays_or_releases_hold(tmp_path: Path, monkeypatch):
    writes, _ = mocks(monkeypatch, status="ROLLBACK_REQUIRED")
    provisioner = controller(tmp_path, qualified=True)

    async def run():
        plan = await provisioner.preview("operator", dict(INTENT))
        operation = await provisioner.create("operator", "request-1", dict(INTENT), approved_plan_id=plan["plan_id"])
        await provisioner.close()
        return plan, operation

    plan, operation = asyncio.run(run())
    # Simulate a process terminating after the already-durable PUSHING record.
    journal = load_state(provisioner.path)
    journal["operations"][operation["id"]]["status"] = "PUSHING"
    write_state(provisioner.path, journal)
    with resource_lock(provisioner.state_dir):
        ledger = ReservationLedger(provisioner.state_dir)
        ledger.data["entries"][operation["reservation_id"]]["status"] = "PUSHING"
        ledger.save()
    restored = controller(tmp_path, qualified=True)
    assert restored.get(operation["id"])["status"] == "ROLLBACK_REQUIRED"
    repeated = asyncio.run(restored.create("operator", "request-1", dict(INTENT), approved_plan_id=plan["plan_id"]))
    assert repeated["id"] == operation["id"] and repeated["status"] == "ROLLBACK_REQUIRED"
    assert len(writes) == 1
    hold = ReservationLedger(restored.state_dir).entries[operation["reservation_id"]]
    assert hold["status"] == "ROLLBACK_REQUIRED" and hold["resources"]


def test_same_idempotency_key_cannot_change_intent_or_approved_plan_after_restart(tmp_path: Path, monkeypatch):
    writes, _ = mocks(monkeypatch, status="ROLLBACK_REQUIRED")
    provisioner = controller(tmp_path, qualified=True)

    async def run():
        plan = await provisioner.preview("operator", dict(INTENT))
        operation = await provisioner.create("operator", "request-1", dict(INTENT), approved_plan_id=plan["plan_id"])
        await provisioner.close()
        return plan, operation

    plan, operation = asyncio.run(run())
    restored = controller(tmp_path, qualified=True)
    for intent, approval in (({**INTENT, "bandwidth_bps": 500_000}, plan["plan_id"]), (dict(INTENT), "0" * 64)):
        with pytest.raises(ValueError, match="Idempotency key"):
            asyncio.run(restored.create("operator", "request-1", intent, approved_plan_id=approval))
    assert len(writes) == 1
    assert restored.get(operation["id"])["status"] == "ROLLBACK_REQUIRED"


def test_proven_prewrite_rejection_releases_only_its_hold_and_survives_restart(tmp_path: Path, monkeypatch):
    def reject(*args, **kwargs):
        raise SouthboundError("Pre-existing resource; no configuration was sent")

    monkeypatch.setattr(cli_provisioning, "push_plan", reject)
    provisioner = controller(tmp_path, qualified=True)

    async def run():
        plan = await provisioner.preview("operator", dict(INTENT))
        operation = await provisioner.create("operator", "request-1", dict(INTENT), approved_plan_id=plan["plan_id"])
        await provisioner.close()
        return operation

    operation = asyncio.run(run())
    assert provisioner.get(operation["id"])["status"] == "REJECTED"
    assert not ReservationLedger(provisioner.state_dir).entries
    evidence = load_state(provisioner.state_dir / ("cli-evidence-" + operation["id"] + ".json"))
    assert evidence["failure"] == "Pre-existing resource; no configuration was sent"
    assert evidence["stage"] == "prewrite"
    assert evidence["failure"] not in provisioner.get(operation["id"])["reason"]
    restored = controller(tmp_path, qualified=True)
    assert restored.get(operation["id"])["status"] == "REJECTED"


def test_delayed_pce_setup_is_observed_without_repeating_configuration(tmp_path: Path, monkeypatch):
    writes, observations = mocks(monkeypatch, status="VERIFICATION_FAILED")

    def verify(plan, **kwargs):
        observations.append((plan, kwargs))
        return result_for(plan)

    monkeypatch.setattr(cli_provisioning, "verify_plan", verify)
    provisioner = controller(tmp_path, qualified=True)

    async def run():
        plan = await provisioner.preview("operator", dict(INTENT))
        operation = await provisioner.create("operator", "request-1", dict(INTENT), approved_plan_id=plan["plan_id"])
        await provisioner.close()
        return operation

    operation = asyncio.run(run())
    assert provisioner.get(operation["id"])["status"] == "VERIFIED"
    assert len(writes) == len(observations) == 1


def test_fresh_collector_timestamp_without_path_or_config_change_keeps_exact_approval(tmp_path: Path, monkeypatch):
    writes, _ = mocks(monkeypatch)
    computer = Computer()
    provisioner = controller(tmp_path, computer, qualified=True)
    plan = preview(provisioner)
    computer.revision += 1
    computer.topology = replace(computer.topology, raw_metadata={
        **computer.topology.raw_metadata, "timestamp": datetime.now(timezone.utc).isoformat(),
    })

    async def run():
        operation = await provisioner.create("operator", "request-1", dict(INTENT), approved_plan_id=plan["plan_id"])
        await provisioner.close()
        return operation

    operation = asyncio.run(run())
    assert provisioner.get(operation["id"])["status"] == "VERIFIED"
    assert len(writes) == 1 and writes[0][0].plan_id == plan["plan_id"]


def test_shared_reservation_gained_after_preview_blocks_delivery_and_preserves_other_owner(tmp_path: Path, monkeypatch):
    writes, _ = mocks(monkeypatch)
    computer = Computer()
    provisioner = controller(tmp_path, computer, qualified=True)
    plan = preview(provisioner)
    with resource_lock(provisioner.state_dir):
        ledger = ReservationLedger(provisioner.state_dir)
        result = cli_provisioning.compute_cspf(computer.topology, ServiceDemand("other", "PE1", "PE3", .5))
        ledger.reserve("other-tunnel", "other-owner", result, "other-plan")
    rejected = asyncio.run(provisioner.create("operator", "request-1", dict(INTENT), approved_plan_id=plan["plan_id"]))
    assert rejected["status"] == "REJECTED"
    assert not writes and len(provisioner.list()) == 1
    assert set(ReservationLedger(provisioner.state_dir).entries) == {"other-tunnel"}


def test_initial_pcep_authority_activates_only_after_absence_and_reachability_prechecks(tmp_path: Path, monkeypatch):
    provisioner = controller(tmp_path, qualified=True)
    report = report_for(tunnel_id=1000)
    ids = report["identifiers"]
    ids["endpoint"] = "10.255.0.3"
    ids["value_hex"] = (bytes.fromhex(ids["value_hex"])[:-4] + IPv4Address(ids["endpoint"]).packed).hex()
    for item in report["tlvs"]:
        if item["type"] == 18:
            item["value_hex"] = ids["value_hex"]
    checks = []

    def push(plan, **kwargs):
        operation = provisioner.list()[0]
        assert operation["status"] == "PLANNED"
        assert provisioner.authorization(PEER, report) is None
        assert ReservationLedger(provisioner.state_dir).entries[operation["reservation_id"]]["status"] == "PLANNED"
        # push_plan invokes this only after successful scoped resource-absence,
        # version, reachability and exact approval checks.
        kwargs["pre_write_guard"]()
        assert provisioner.get(operation["id"])["status"] == "PUSHING"
        assert provisioner.authorization(PEER, report) == plan.plan_id
        checks.append(True)
        return result_for(plan)

    monkeypatch.setattr(cli_provisioning, "push_plan", push)
    monkeypatch.setattr(CliProvisioner, "_completed_path", lambda self, op_id: True)

    async def run():
        plan = await provisioner.preview("operator", dict(INTENT))
        operation = await provisioner.create("operator", "request-1", dict(INTENT), approved_plan_id=plan["plan_id"])
        assert provisioner.authorization(PEER, report) is None
        await provisioner.close()
        return operation

    operation = asyncio.run(run())
    assert checks == [True]
    assert provisioner.get(operation["id"])["status"] == "VERIFIED"


@pytest.mark.parametrize("field", ["plan_id", "tunnel_id", "bandwidth_bps", "ero", "path_edges", "peer", "owner"])
def test_rechecksummed_pcep_authority_cannot_differ_from_held_approved_operation(tmp_path: Path, monkeypatch, field):
    mocks(monkeypatch, status="ROLLBACK_REQUIRED")
    provisioner = controller(tmp_path, qualified=True)

    async def run():
        plan = await provisioner.preview("operator", dict(INTENT))
        operation = await provisioner.create("operator", "request-1", dict(INTENT), approved_plan_id=plan["plan_id"])
        await provisioner.close()
        return operation

    operation = asyncio.run(run())
    journal = load_state(provisioner.path)
    saved = journal["operations"][operation["id"]]
    if field == "owner":
        saved["owner"] = "cli-provision:someone-else"
    else:
        saved["authorization"][field] = {
            "plan_id": "0" * 64, "tunnel_id": 1001, "bandwidth_bps": 500_000,
            "ero": ["10.0.12.22", "10.0.23.3"], "path_edges": ["other", "P1-PE3"], "peer": "10.255.0.99",
        }[field]
    saved["authorization_digest"] = digest(saved["authorization"])
    write_state(provisioner.path, journal)
    with pytest.raises(ValueError, match="authority differs"):
        controller(tmp_path, qualified=True)
    assert ReservationLedger(provisioner.state_dir).entries[operation["reservation_id"]]["resources"]


def test_orphan_provisioning_hold_blocks_startup_without_refund_or_replay(tmp_path: Path):
    provisioner = controller(tmp_path)
    with resource_lock(provisioner.state_dir):
        ledger = ReservationLedger(provisioner.state_dir)
        result = cli_provisioning.compute_cspf(provisioner.computer.topology, ServiceDemand("orphan", "PE1", "PE3", .5))
        ledger.reserve("PE1:iosxe:Tunnel1000", "cli-provision:lost-operation", result, "lost-plan")
    with pytest.raises(ValueError, match="Orphan provisioning reservation"):
        controller(tmp_path)
    assert ReservationLedger(provisioner.state_dir).entries["PE1:iosxe:Tunnel1000"]["resources"]


def test_expired_admission_is_durably_rejected_and_old_request_cannot_deliver_after_restart(tmp_path: Path, monkeypatch):
    writes, _ = mocks(monkeypatch)
    provisioner = controller(tmp_path, qualified=True)
    old_plan = preview(provisioner)
    actual_now = cli_provisioning._now
    monkeypatch.setattr(cli_provisioning, "_now", lambda: datetime.fromisoformat(old_plan["expires_at"]))
    rejected = asyncio.run(provisioner.create("operator", "old-key", dict(INTENT),
                                             approved_plan_id=old_plan["plan_id"]))
    assert rejected["status"] == "REJECTED"
    assert rejected["authorization"] is None
    assert rejected["reservation_id"] is rejected["tunnel_id"] is None
    assert "expired or changed" in rejected["reason"]
    assert not writes and not ReservationLedger(provisioner.state_dir).entries
    monkeypatch.setattr(cli_provisioning, "_now", actual_now)

    restored = controller(tmp_path, qualified=True)
    restored.qualified = False
    retry = asyncio.run(restored.create("operator", "old-key", dict(INTENT), approved_plan_id=old_plan["plan_id"]))
    assert retry == rejected
    restored.qualified = True
    fresh = preview(restored)
    assert fresh["plan_id"] != old_plan["plan_id"] and fresh["tunnel_id"] == 1000

    async def run():
        late = await restored.create("operator", "old-key", dict(INTENT), approved_plan_id=old_plan["plan_id"])
        assert late == rejected and not writes
        with pytest.raises(ValueError, match="Idempotency key"):
            await restored.create("operator", "old-key", dict(INTENT), approved_plan_id=fresh["plan_id"])
        operation = await restored.create("operator", "new-key", dict(INTENT), approved_plan_id=fresh["plan_id"])
        await restored.close()
        return operation

    operation = asyncio.run(run())
    assert len(writes) == 1
    assert restored.get(operation["id"])["status"] == "VERIFIED"
    assert restored.get(rejected["id"])["status"] == "REJECTED"


def test_managed_tunnel_admission_rejection_never_changes_the_existing_hold(tmp_path: Path, monkeypatch):
    writes, _ = mocks(monkeypatch, status="ROLLBACK_REQUIRED")
    provisioner = controller(tmp_path, qualified=True)

    async def run():
        approved = await provisioner.preview("operator", dict(INTENT))
        first = await provisioner.create("operator", "first-key", dict(INTENT), approved_plan_id=approved["plan_id"])
        await provisioner.close()
        rejected = await provisioner.create("operator", "another-key", dict(INTENT), approved_plan_id=approved["plan_id"])
        return first, rejected

    first, rejected = asyncio.run(run())
    assert len(writes) == 1 and rejected["status"] == "REJECTED"
    assert rejected["authorization"] is None and rejected["reservation_id"] is None
    holds = ReservationLedger(provisioner.state_dir).entries
    assert set(holds) == {first["reservation_id"]}
    assert holds[first["reservation_id"]]["status"] == "ROLLBACK_REQUIRED"


@pytest.mark.parametrize("failure", ["journal_corrupt", "capture_corrupt", "unknown_compute", "persistence"])
def test_unknown_or_corrupt_admission_outcomes_are_not_changed_into_rejections(tmp_path: Path, monkeypatch, failure):
    writes, _ = mocks(monkeypatch)
    provisioner = controller(tmp_path, qualified=True)
    approved = preview(provisioner)
    before = provisioner.path.read_bytes()
    if failure == "journal_corrupt":
        provisioner.path.write_text("{broken json")
    elif failure == "capture_corrupt":
        provisioner.pcep_capture.write_text("{broken json")
    elif failure == "unknown_compute":
        monkeypatch.setattr(provisioner, "_compute", lambda *args: (_ for _ in ()).throw(ValueError("Unexpected invariant")))
    else:
        provisioner.qualified = False

        def fail_save(*args, **kwargs):
            raise OSError("Journal storage unavailable")

        monkeypatch.setattr(cli_provisioning, "write_state", fail_save)
    with pytest.raises((ValueError, OSError)):
        asyncio.run(provisioner.create("operator", "request-1", dict(INTENT), approved_plan_id=approved["plan_id"]))
    assert not writes and not ReservationLedger(provisioner.state_dir).entries
    if failure != "journal_corrupt":
        assert provisioner.path.read_bytes() == before


def test_rejection_history_stops_at_bound_without_unrecorded_delivery_or_growth(tmp_path: Path):
    provisioner = controller(tmp_path, qualified=False)
    for index in range(16):
        operation = asyncio.run(provisioner.create("operator", f"rejected-{index}", dict(INTENT),
                                                   approved_plan_id="0" * 64))
        assert operation["status"] == "REJECTED"
    saved = provisioner.path.read_bytes()
    with pytest.raises(ValueError, match="history is full"):
        asyncio.run(provisioner.create("operator", "one-too-many", dict(INTENT), approved_plan_id="0" * 64))
    assert len(provisioner.list()) == 16 and provisioner.path.read_bytes() == saved
    repeated = asyncio.run(provisioner.create("operator", "rejected-0", dict(INTENT), approved_plan_id="0" * 64))
    assert repeated["idempotency_key"] == "rejected-0" and repeated["status"] == "REJECTED"
    assert not ReservationLedger(provisioner.state_dir).entries


def test_rejected_tombstone_cannot_conceal_a_reservation_with_its_owner(tmp_path: Path):
    provisioner = controller(tmp_path, qualified=False)
    rejected = asyncio.run(provisioner.create("operator", "rejected-key", dict(INTENT), approved_plan_id="0" * 64))
    with resource_lock(provisioner.state_dir):
        ledger = ReservationLedger(provisioner.state_dir)
        result = cli_provisioning.compute_cspf(provisioner.computer.topology, ServiceDemand("unexpected", "PE1", "PE3", .5))
        ledger.reserve("PE1:iosxe:Tunnel1000", rejected["owner"], result, rejected["plan_id"])
    with pytest.raises(ValueError, match="Invalid released admission rejection"):
        controller(tmp_path)
    assert ReservationLedger(provisioner.state_dir).entries["PE1:iosxe:Tunnel1000"]["resources"]


def test_live_configured_tunnel_pce_control_proof_requires_the_selected_option_peer_and_update(tmp_path: Path):
    provisioner = controller(tmp_path)
    fixture = Path(__file__).parent / "fixtures/operational/iosxe_16.9.7_configured_tunnel1000_up.live.txt"
    text = fixture.read_text()
    command = "show mpls traffic-eng tunnels tunnel 1000"
    assert provisioner._pce_control(((command, text),))
    for changed in (
        text.replace("(Basis for Setup, path weight 30)", ""),
        text.replace("Working: yes", "Working: no"),
        text.replace("Delegation peer: 172.16.100.10", "Delegation peer: 172.16.100.11"),
        text.replace("Created via PCUpd message", "Created via PCInitiate message"),
    ):
        assert not provisioner._pce_control(((command, changed),))
