"""Domain creation admission, concurrency and original policy authority."""
from __future__ import annotations

import asyncio
import base64
import copy
import struct
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest
from test_cli_provisioning import mocks as legacy_mocks
from test_cli_provisioning import result_for as legacy_result_for
from test_tunnel_creation import Computer

from ninja_pce import cli_provisioning
from ninja_pce.cli_provisioning import CliProvisioner
from ninja_pce.models import ManagedDevice
from ninja_pce.pcep_compute import PcepPathComputer
from ninja_pce.pcep_domain_policy import DomainDelegationPolicy
from ninja_pce.provisioning_policy import TransportEnrollmentRegistry
from ninja_pce.reservations import ReservationLedger
from ninja_pce.state import load_state, write_state


def result_for(plan, **kwargs):
    result = legacy_result_for(plan, **kwargs)
    return replace(result, postcheck_output=tuple((command, output.replace("172.16.100.10", "192.0.2.254"))
                                                 for command, output in result.postcheck_output))


def mocks(monkeypatch, **kwargs):
    writes, observations = legacy_mocks(monkeypatch, **kwargs)

    def push(plan, **options):
        options["pre_write_guard"]()
        writes.append((plan, options))
        return result_for(plan, **{key: value for key, value in kwargs.items() if key in {"status", "controlled"}})

    monkeypatch.setattr(cli_provisioning, "push_plan", push)
    return writes, observations


def key(value: int) -> str:
    algorithm = b"ssh-ed25519"
    raw = struct.pack("!I", len(algorithm)) + algorithm + struct.pack("!I", 32) + bytes([value]) * 32
    return base64.b64encode(raw).decode()


def domain_controller(tmp_path, monkeypatch, *, computer=None, devices=None, **kwargs):
    computer = computer or Computer(capacity=10_000_000)
    policy = DomainDelegationPolicy.from_document({
        "version": 1, "domain": {"protocol_id": 3, "instance_id": 0,
                                  "autonomous_system": 65000, "bgpls_identifier": 0},
        "pcc_prefixes": ["10.255.0.0/24"], "destination_prefixes": ["10.255.0.0/24"],
        "max_bandwidth_bps": 10_000_000, "max_live_lsps": 32,
    })
    computer.topology.raw_metadata["bgpls_objects"] = {
        node.id: {"identity": node.id, "protocol_id": 3, "instance_id": 0,
                  "descriptors": {"512": "0000fde8", "513": "00000000"}}
        for node in computer.topology.nodes.values()
    }
    computer.topology.raw_metadata["bgpls_objects"].update({
        link.id: {"protocol_id": 3, "instance_id": 0} for link in computer.topology.links
    })
    computer.domain_policy = policy
    # No per-router PCC registrations. Management hosts differ from TE peers.
    computer.headends = {}
    if devices is None:
        devices = {name: ManagedDevice(name, name, f"192.0.2.{index}", "cisco_ios", "ssh-user",
                                        password_env="NINJA_TEST_TRANSPORT")
                   for index, name in enumerate(computer.topology.nodes, 1)}
    pins = tmp_path / "known_hosts"
    if not pins.exists():
        pins.write_text("".join(f"{device.host} ssh-ed25519 {key(index)}\n"
                                for index, device in enumerate(devices.values(), 1)))
    monkeypatch.setenv("NINJA_TEST_TRANSPORT", "synthetic-secret")
    registry = TransportEnrollmentRegistry(devices, pins)
    result = CliProvisioner(cast(PcepPathComputer, computer), tmp_path / "state", devices, (),
                            "192.0.2.254", tmp_path / "capture.json", qualified=True,
                            max_outstanding=kwargs.pop("max_outstanding", 4),
                            domain_policy=policy, transport_registry=registry, **kwargs)
    write_state(result.pcep_capture, {
        "listener_running": True, "generated_at": time.time(),
        "provisioning_policy_digest": result.authorization.policy_digest,
        "capabilities": {"configuration_provisioning": True},
        "peers": {node.loopback: {"state": "ESTABLISHED", "fresh": True, "synchronized": True,
                                   "valid_until": time.time() + 120, "reports": {}}
                  for node in computer.topology.nodes.values()},
    })
    return result


def intent(headend="PE1", bandwidth=1_000_000):
    return {"headend": headend, "destination": "PE3", "bandwidth_bps": bandwidth}


def test_domain_transit_headend_uses_separate_management_endpoint(tmp_path, monkeypatch):
    backend = domain_controller(tmp_path, monkeypatch)
    assert backend.headends == {"P1": "10.255.0.2", "PE1": "10.255.0.1", "PE3": "10.255.0.3"}
    assert backend.allowed_destinations == ("P1", "PE1", "PE3")
    plan = asyncio.run(backend.preview("new-user-with-no-registration", intent("P1")))
    assert plan["path_nodes"] == ["P1", "PE3"]
    assert backend._plans[plan["plan_id"]].device.host == "192.0.2.2"
    assert backend.readiness("P1") == (True, "")


def test_unenrolled_domain_nodes_remain_visible_and_explain_readiness(tmp_path, monkeypatch):
    device = ManagedDevice("PE1", "PE1", "192.0.2.1", "cisco_ios", "ssh-user",
                           password_env="NINJA_TEST_TRANSPORT")
    backend = domain_controller(tmp_path, monkeypatch, devices={"PE1": device})
    assert set(backend.headends) == {"PE1", "P1", "PE3"}
    readiness = backend.node_readiness()
    assert readiness["P1"]["transport_enrolled"] is False
    assert readiness["P1"]["pcc_ready"] is True
    assert "transport" in readiness["P1"]["reason"]
    assert backend.readiness() == (True, "")
    with pytest.raises(ValueError, match="enrolled"):
        asyncio.run(backend.preview("user", intent("P1")))


def test_concurrent_distinct_headends_and_principals_share_key_but_not_ownership(tmp_path, monkeypatch):
    backend = domain_controller(tmp_path, monkeypatch)
    mocks(monkeypatch)
    reached = threading.Barrier(2)
    delivered = []

    def push(plan, **kwargs):
        kwargs["pre_write_guard"]()
        reached.wait(timeout=3)
        delivered.append(plan.device.topology_node_id)
        return result_for(plan)

    monkeypatch.setattr(cli_provisioning, "push_plan", push)

    async def run():
        plans = await asyncio.gather(backend.preview("alice", intent()), backend.preview("bob", intent("P1")))
        assert [plan["tunnel_id"] for plan in plans] == [1000, 1000]
        operations = await asyncio.gather(*(
            backend.create(principal, "same-key", intent(headend), approved_plan_id=plan["plan_id"])
            for principal, headend, plan in zip(("alice", "bob"), ("PE1", "P1"), plans)
        ))
        repeated = await backend.create("alice", "same-key", intent(), approved_plan_id=plans[0]["plan_id"])
        assert repeated["id"] == operations[0]["id"]
        await backend.close()
        return operations

    operations = asyncio.run(run())
    assert sorted(delivered) == ["P1", "PE1"]
    assert {op["status"] for op in backend.list()} == {"VERIFIED"}
    assert len({op["owner"] for op in operations}) == 2
    assert len(ReservationLedger(backend.state_dir).entries) == 2
    assert backend.list("alice")[0]["id"] != backend.list("bob")[0]["id"]


def test_concurrent_same_headend_previews_allocate_distinct_ids_and_admit_capacity(tmp_path, monkeypatch):
    backend = domain_controller(tmp_path, monkeypatch)
    mocks(monkeypatch)

    async def run():
        plans = await asyncio.gather(*(backend.preview(principal, intent()) for principal in ("alice", "bob")))
        assert {p["tunnel_id"] for p in plans} == {1000, 1001}
        ops = await asyncio.gather(*(backend.create(principal, "key", intent(), approved_plan_id=p["plan_id"])
                                    for principal, p in zip(("alice", "bob"), plans)))
        await backend.close()
        return ops

    operations = asyncio.run(run())
    assert len({op["reservation_id"] for op in operations}) == 2
    assert all(backend.get(op["id"])["status"] == "VERIFIED" for op in operations)
    assert sum(entry["resources"].get("PE1-P1", 0)
               for entry in ReservationLedger(backend.state_dir).entries.values()) == 2_000_000


def test_existing_observed_ids_and_other_ledger_owners_are_not_allocated(tmp_path, monkeypatch):
    backend = domain_controller(tmp_path, monkeypatch)
    capture = load_state(backend.pcep_capture)
    capture["peers"]["10.255.0.1"]["reports"] = {"report": {"identifiers": {"tunnel_id": 1000, "lsp_id": 90}}}
    write_state(backend.pcep_capture, capture)
    plan = asyncio.run(backend.preview("alice", intent()))
    assert plan["tunnel_id"] == 1001
    capture["peers"]["10.255.0.1"]["reports"]["report"]["identifiers"].update(tunnel_id=1001, lsp_id=91)
    write_state(backend.pcep_capture, capture)
    rejected = asyncio.run(backend.create("alice", "key", intent(), approved_plan_id=plan["plan_id"]))
    assert rejected["status"] == "REJECTED" and "already in use" in rejected["reason"]
    assert not ReservationLedger(backend.state_dir).entries


@pytest.mark.parametrize("problem", ["changed", "missing", "credential"])
def test_transport_changes_after_preview_refuse_without_holds(tmp_path, monkeypatch, problem):
    backend = domain_controller(tmp_path, monkeypatch)
    plan = asyncio.run(backend.preview("alice", intent()))
    if problem == "credential":
        monkeypatch.delenv("NINJA_TEST_TRANSPORT")
    elif problem == "missing":
        (tmp_path / "known_hosts").unlink()
    else:
        path = tmp_path / "known_hosts"
        path.write_text(path.read_text().replace(key(1), key(99)))
    refused = asyncio.run(backend.create("alice", "key", intent(), approved_plan_id=plan["plan_id"]))
    assert refused["status"] == "REJECTED"
    assert not ReservationLedger(backend.state_dir).entries


def test_policy_revision_and_scoped_retry_survive_restart_and_removed_membership(tmp_path, monkeypatch):
    backend = domain_controller(tmp_path, monkeypatch)
    mocks(monkeypatch, status="ROLLBACK_REQUIRED")

    async def run():
        plan = await backend.preview("alice", intent())
        operation = await backend.create("alice", "key", intent(), approved_plan_id=plan["plan_id"])
        await backend.close()
        return operation

    original = asyncio.run(run())
    before = copy.deepcopy(backend.get(original["id"]))
    ledger = (backend.state_dir / ".ninja-pce-reservations.json").read_bytes()
    restored = domain_controller(tmp_path, monkeypatch, computer=backend.computer, max_bandwidth_bps=500_000)
    restored.computer.topology = replace(restored.computer.topology, nodes={})
    repeated = asyncio.run(restored.create("alice", "key", intent(), approved_plan_id=original["plan_id"]))
    assert repeated == before
    assert repeated["policy_revision"] != restored.policy_digest
    assert len(load_state(restored.path)["policies"]) == 2
    assert (restored.state_dir / ".ninja-pce-reservations.json").read_bytes() == ledger


def test_new_plan_cannot_reuse_already_consumed_plan_id_under_another_key(tmp_path, monkeypatch):
    backend = domain_controller(tmp_path, monkeypatch)
    mocks(monkeypatch)

    async def run():
        plan = await backend.preview("alice", intent())
        first = await backend.create("alice", "first", intent(), approved_plan_id=plan["plan_id"])
        second = await backend.create("alice", "second", intent(), approved_plan_id=plan["plan_id"])
        await backend.close()
        return first, second

    first, second = asyncio.run(run())
    assert first["id"] != second["id"] and second["status"] == "REJECTED"
    assert len(ReservationLedger(backend.state_dir).entries) == 1


def test_concurrent_admission_capacity_is_rechecked_and_denial_is_idempotent(tmp_path, monkeypatch):
    backend = domain_controller(tmp_path, monkeypatch, max_outstanding=1)
    mocks(monkeypatch)

    async def run():
        plans = [await backend.preview(p, intent()) for p in ("alice", "bob")]
        operations = await asyncio.gather(*(backend.create(p, "key", intent(), approved_plan_id=plan["plan_id"])
                                           for p, plan in zip(("alice", "bob"), plans)))
        assert [op["status"] for op in operations] == ["PLANNED", "REJECTED"]
        retry = await backend.create("bob", "key", intent(), approved_plan_id=plans[1]["plan_id"])
        assert retry["id"] == operations[1]["id"]
        await backend.close()

    asyncio.run(run())
    assert len(ReservationLedger(backend.state_dir).entries) == 1


def test_two_controller_instances_coordinate_preview_namespace_under_file_lock(tmp_path, monkeypatch):
    first = domain_controller(tmp_path, monkeypatch)
    second = domain_controller(tmp_path, monkeypatch, computer=first.computer)

    def preview(backend, principal):
        return asyncio.run(backend.preview(principal, intent()))

    async def run():
        return await asyncio.gather(asyncio.to_thread(preview, first, "alice"),
                                    asyncio.to_thread(preview, second, "bob"))

    plans = asyncio.run(run())
    assert {plan["tunnel_id"] for plan in plans} == {1000, 1001}


def test_same_endpoint_delivery_is_serialized_across_initialized_controller_instances(tmp_path: Path, monkeypatch):
    first = domain_controller(tmp_path, monkeypatch)
    second = domain_controller(tmp_path, monkeypatch, computer=first.computer)
    mocks(monkeypatch)
    monkeypatch.setattr(CliProvisioner, "_pce_control", lambda self, outputs: True)
    first_entered, second_attempted, second_entered, release_first = (threading.Event() for _ in range(4))
    order = []
    first_plan_id = None
    original_execute = CliProvisioner._execute

    def execute(backend, op_id, plan):
        if plan.plan_id != first_plan_id:
            second_attempted.set()
        return original_execute(backend, op_id, plan)

    def push(plan, **kwargs):
        kwargs["pre_write_guard"]()
        if plan.plan_id == first_plan_id:
            order.append("first-started")
            first_entered.set()
            assert release_first.wait(timeout=3), "First delivery was never released"
            order.append("first-finished")
        else:
            order.append("second-started")
            second_entered.set()
            order.append("second-finished")
        return result_for(plan)

    monkeypatch.setattr(CliProvisioner, "_execute", execute)
    monkeypatch.setattr(cli_provisioning, "push_plan", push)

    async def run():
        nonlocal first_plan_id
        plans = [await first.preview("alice", intent()), await second.preview("bob", intent())]
        first_plan_id = plans[0]["plan_id"]
        try:
            first_operation = await first.create("alice", "key", intent(), approved_plan_id=first_plan_id)
            assert await asyncio.to_thread(first_entered.wait, 2), "First delivery did not start"
            second_operation = await second.create("bob", "key", intent(), approved_plan_id=plans[1]["plan_id"])
            assert await asyncio.to_thread(second_attempted.wait, 2), "Second worker was never scheduled"
            assert not await asyncio.to_thread(second_entered.wait, .1), "Two CLI deliveries overlapped"
            assert second.get(second_operation["id"])["status"] == "PLANNED"
            return first_operation, second_operation
        finally:
            release_first.set()
            await asyncio.gather(first.close(), second.close())

    operations = asyncio.run(run())
    assert second_entered.is_set()
    assert order == ["first-started", "first-finished", "second-started", "second-finished"]
    assert all(first.get(op["id"])["status"] == "VERIFIED" for op in operations)
    assert len(ReservationLedger(first.state_dir).entries) == 2


def test_new_same_principal_preview_preserves_an_admitted_plan_waiting_for_prechecks(tmp_path, monkeypatch):
    backend = domain_controller(tmp_path, monkeypatch)
    mocks(monkeypatch)
    monkeypatch.setattr(CliProvisioner, "_pce_control", lambda self, outputs: True)
    prechecks_started, finish_prechecks = threading.Event(), threading.Event()

    def push(plan, **kwargs):
        prechecks_started.set()
        assert finish_prechecks.wait(timeout=3), "Synthetic SSH prechecks were never released"
        kwargs["pre_write_guard"]()
        return result_for(plan)

    monkeypatch.setattr(cli_provisioning, "push_plan", push)

    async def run():
        reviewed = await backend.preview("alice", intent())
        try:
            admitted = await backend.create("alice", "first", intent(), approved_plan_id=reviewed["plan_id"])
            assert await asyncio.to_thread(prechecks_started.wait, 2), "Synthetic prechecks did not start"
            assert backend.get(admitted["id"])["status"] == "PLANNED"
            another = await backend.preview("alice", intent())
            previews = load_state(backend.path)["previews"]
            assert previews[reviewed["plan_id"]] == reviewed
            assert another["plan_id"] in previews
            assert another["tunnel_id"] != reviewed["tunnel_id"]
            return admitted
        finally:
            finish_prechecks.set()
            await backend.close()

    admitted = asyncio.run(run())
    assert backend.get(admitted["id"])["status"] == "VERIFIED"
    assert len(ReservationLedger(backend.state_dir).entries) == 1


@pytest.mark.parametrize("capacity", [1, 2])
def test_two_initialized_instances_admit_concurrently_with_shared_ids_capacity_and_retries(
    tmp_path, monkeypatch, capacity,
):
    first = domain_controller(tmp_path, monkeypatch, max_outstanding=capacity)
    second = domain_controller(tmp_path, monkeypatch, computer=first.computer, max_outstanding=capacity)
    mocks(monkeypatch)
    monkeypatch.setattr(CliProvisioner, "_pce_control", lambda self, outputs: True)
    previews_ready = threading.Barrier(2)

    def request(backend, principal):
        async def run():
            plan = await backend.preview(principal, intent())
            await asyncio.to_thread(previews_ready.wait, 3)
            try:
                operation = await backend.create(principal, "same-key", intent(), approved_plan_id=plan["plan_id"])
                retry = await backend.create(principal, "same-key", intent(), approved_plan_id=plan["plan_id"])
                assert retry["id"] == operation["id"]
                return plan, operation
            finally:
                await backend.close()
        return asyncio.run(run())

    async def concurrent_requests():
        return await asyncio.gather(asyncio.to_thread(request, first, "alice"),
                                    asyncio.to_thread(request, second, "bob"))

    results = asyncio.run(concurrent_requests())
    assert {plan["tunnel_id"] for plan, _ in results} == {1000, 1001}
    operations = [operation for _, operation in results]
    assert len({op["id"] for op in operations}) == 2
    current = first.list()
    assert len(current) == 2
    assert sum(op["status"] == "VERIFIED" for op in current) == capacity
    assert sum(op["status"] == "REJECTED" for op in current) == 2 - capacity
    holds = ReservationLedger(first.state_dir).entries
    assert len(holds) == capacity
    assert sum(entry["resources"]["PE1-P1"] for entry in holds.values()) == capacity * 1_000_000
    assert {op["reservation_id"] for op in current if op["status"] == "VERIFIED"} == set(holds)
    assert first.list("alice")[0]["id"] != second.list("bob")[0]["id"]


def test_malformed_or_duplicate_original_policy_authority_is_rejected(tmp_path, monkeypatch):
    backend = domain_controller(tmp_path, monkeypatch)
    operation = asyncio.run(backend.create("alice", "key", intent(), approved_plan_id="0" * 64))
    journal = load_state(backend.path)
    journal["operations"][operation["id"]]["policy_revision"] = "0" * 64
    write_state(backend.path, journal)
    with pytest.raises(ValueError, match="original policy"):
        backend.list()
