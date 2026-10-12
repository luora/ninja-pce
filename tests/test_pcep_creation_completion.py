"""Guarded private completion experiments
synthetic evidence is not qualification."""
from __future__ import annotations

import asyncio
import copy
import hashlib
import time
from pathlib import Path
from uuid import UUID

import pytest
from test_pcep_creation_server import (
    INTENT,
    PEER,
    PRINCIPAL,
    _connect,
    _eventually,
    _held,
    _lab,
    _read,
    _report,
    _running,
)
from test_tunnel_creation import hold, manager, report, sent

from ninja_pce.pcep import parse_objects, parse_reports
from ninja_pce.pcep_initiation import encode_delete, encode_initiate, encode_owned_update
from ninja_pce.reservations import ReservationLedger
from ninja_pce.state import load_state, write_state


def test_mismatched_attributes_bind_only_ownership_and_allow_bounded_cleanup(tmp_path: Path):
    controller = manager(tmp_path)
    op = sent(controller)
    wrong = report(op)
    wrong["attributes"][1]["body_hex"] = "00000000"
    bound = controller.bind_owned_candidate(op["id"], wrong)
    assert bound["status"] == "PUSHING" and bound["reported"] is None and bound["verified"] is False
    with pytest.raises(ValueError, match="bandwidth"):
        controller.observe(op["id"], wrong)
    controller.uncertain(op["id"], "attributes ignored")
    cleanup = controller.mark_removing(op["id"], "session-one", 6, "b" * 64)
    assert cleanup["status"] == "ROLLBACK_REQUIRED" and hold(tmp_path, op)
    with pytest.raises(ValueError, match="unreplayed"):
        controller.mark_removing(op["id"], "session-one", 7, "b" * 64)


def test_completion_is_once_only_and_current_capacity_is_required(tmp_path: Path):
    controller = manager(tmp_path)
    op = sent(controller)
    controller.bind_owned_candidate(op["id"], report(op))
    controller.uncertain(op["id"], "attributes ignored")
    completion = controller.mark_completion(op["id"], "session-one", 6, "b" * 64)
    assert completion["completion"]["srp_id"] == 6 and hold(tmp_path, op)
    with pytest.raises(ValueError, match="unreplayed"):
        controller.mark_completion(op["id"], "session-one", 7, "b" * 64)
    with pytest.raises(ValueError, match="correlated"):
        controller.observe(op["id"], report(op, srp=0))
    applied = controller.observe(op["id"], report(op, srp=6))
    assert applied["status"] == "APPLIED_UNVERIFIED" and applied["verified"] is False
    assert applied["completion"]["acknowledged"] is True


def test_completion_rejects_withdrawn_or_exhausted_path(tmp_path: Path):
    controller = manager(tmp_path)
    op = sent(controller)
    controller.bind_owned_candidate(op["id"], report(op))
    controller.uncertain(op["id"], "attributes ignored")
    controller.computer.error = "collector unavailable"
    with pytest.raises(ValueError, match="collector"):
        controller.mark_completion(op["id"], "session-one", 6, "b" * 64)
    assert controller.get(op["id"]).get("completion") is None and hold(tmp_path, op)


def test_new_incarnation_requires_exact_completion_srp_and_same_tunnel(tmp_path: Path):
    controller = manager(tmp_path)
    op = sent(controller)
    controller.bind_owned_candidate(op["id"], report(op))
    controller.uncertain(op["id"], "attributes ignored")
    controller.mark_completion(op["id"], "session-one", 6, "b" * 64)
    fresh = report(op, srp=6)
    ids = fresh["identifiers"]
    raw = bytearray.fromhex(ids["value_hex"])
    raw[4:6] = b"\x00\x03"
    ids.update(lsp_id=3, value_hex=raw.hex())
    next(t for t in fresh["tlvs"] if t["type"] == 18)["value_hex"] = raw.hex()
    wrong = copy.deepcopy(fresh)
    wrong["srp_id"] = 0
    with pytest.raises(ValueError, match="identity"):
        controller.bind_owned_candidate(op["id"], wrong)
    controller.bind_owned_candidate(op["id"], fresh)
    applied = controller.observe(op["id"], fresh)
    assert applied["reported"]["identity"]["identifiers"]["lsp_id"] == 3
    assert len(applied["identity_history"]) == 1 and hold(tmp_path, op)


def test_restart_reconciliation_preserves_send_audit_and_cannot_replay_creation(tmp_path: Path):
    controller = manager(tmp_path)
    op = sent(controller)
    controller.bind_owned_candidate(op["id"], report(op))
    controller.uncertain(op["id"], "restart")
    synced = report(op)
    synced.update(session_id="session-two", sync=True)
    reconciled = controller.reconcile_owned(op["id"], synced)
    assert reconciled["owner_session"] == "session-two" and reconciled["send"] == op["send"]
    assert reconciled["status"] == "ROLLBACK_REQUIRED" and hold(tmp_path, op)
    with pytest.raises(ValueError, match="boundary"):
        controller.mark_sending(op["id"], "session-two", 1, "c" * 64)
    completion = controller.mark_completion(op["id"], "session-two", 1, "c" * 64)
    assert completion["completion"]["session_id"] == "session-two"


def test_reconciled_cleanup_survives_reload_with_session_scoped_srp_and_retains_hold(tmp_path: Path):
    controller = manager(tmp_path)
    op = sent(controller)
    controller.bind_owned_candidate(op["id"], report(op))
    controller.uncertain(op["id"], "restart")
    synced = report(op, srp=0)
    synced.update(session_id="session-two", sync=True)
    manager(tmp_path).reconcile_owned(op["id"], synced)
    # The new session starts below the original creation's SRP 5.
    cleanup = manager(tmp_path).mark_removing(op["id"], "session-two", 1, "b" * 64)
    restored = manager(tmp_path)
    assert restored.get(op["id"]) == cleanup
    assert restored.list()[0]["send"] == op["send"]
    assert hold(tmp_path, op)["status"] == "ROLLBACK_REQUIRED"
    with pytest.raises(ValueError, match="unreplayed"):
        restored.mark_removing(op["id"], "session-two", 2, "c" * 64)
    with pytest.raises(ValueError, match="held or unresolved"):
        restored.plan("operator", "another", "PE1", "PE3", 1_000_000)
    removed = report(op, srp=1, removed=True)
    removed["session_id"] = "session-two"
    restored.record_removal(op["id"], removed)
    assert manager(tmp_path).get(op["id"])["cleanup"]["report_digest"]
    assert hold(tmp_path, op)["status"] == "ROLLBACK_REQUIRED"


def test_reconciled_cleanup_journal_rejects_unknown_session_without_refunding_hold(tmp_path: Path):
    controller = manager(tmp_path)
    op = sent(controller)
    controller.bind_owned_candidate(op["id"], report(op))
    controller.uncertain(op["id"], "restart")
    synced = report(op, srp=0)
    synced.update(session_id="session-two", sync=True)
    controller.reconcile_owned(op["id"], synced)
    controller.mark_removing(op["id"], "session-two", 1, "b" * 64)
    journal = load_state(controller.path)
    journal["operations"][op["id"]]["cleanup"]["session_id"] = "unrelated-session"
    write_state(controller.path, journal)
    with pytest.raises(ValueError, match="cleanup boundary"):
        manager(tmp_path).get(op["id"])
    assert hold(tmp_path, op)["status"] == "ROLLBACK_REQUIRED"


def test_absence_requires_independent_proofs_and_fresh_synchronized_complete_pcc(tmp_path: Path):
    controller = manager(tmp_path)
    op = sent(controller)
    controller.uncertain(op["id"], "lab restarted")
    pcc = {"peer_ip": op["peer"], "state": "ESTABLISHED", "fresh": True, "synchronized": True,
           "valid_until": time.time() + 60, "reports": {}}
    evidence = {"router_reservations": {node: "a" * 64 for node in op["path_nodes"]},
                "headend_configuration": "b" * 64, "observed_at": time.time()}
    with pytest.raises(ValueError, match="independent"):
        controller.confirmed_absence(op["id"], pcc, evidence)
    with pytest.raises(ValueError, match="independent"):
        controller.confirmed_absence(op["id"], {**pcc, "synchronized": False}, evidence, resources_released=True)
    with pytest.raises(ValueError, match="independent"):
        controller.confirmed_absence(op["id"], pcc, {**evidence, "router_reservations": {}}, resources_released=True)
    done = controller.confirmed_absence(op["id"], pcc, evidence, resources_released=True)
    assert done["status"] == "ROLLED_BACK" and done["absence"]["resources_released"] is True
    assert not controller.list()[0]["verified"]


def test_owned_update_codec_requires_full_owned_identity_and_requested_attributes(tmp_path: Path):
    controller = manager(tmp_path)
    op = sent(controller)
    owned = report(op)
    wire = encode_owned_update(6, owned, tuple(op["ero"]), op["bandwidth_bps"])
    assert wire[1] == 11
    assert [o.object_class for o in parse_objects(wire[4:])] == [33, 32, 7, 9, 5, 6]
    decoded = parse_reports(wire[4:], allow_delegation=True)[0]
    assert decoded["plsp_id"] == owned["plsp_id"] and decoded["identifiers"] == owned["identifiers"]
    assert decoded["delegate"] and not decoded["created"] and decoded["srp_id"] == 6
    assert decoded["attributes"][1]["body_hex"] == op["bandwidth_wire"]
    for field in ["created", "delegate", "administrative_up"]:
        with pytest.raises(ValueError):
            encode_owned_update(6, {**owned, field: False}, tuple(op["ero"]), op["bandwidth_bps"])


def test_private_completion_persists_before_write_and_never_auto_runs_or_repeats(tmp_path, monkeypatch):
    async def scenario():
        async with _running(_lab(tmp_path, monkeypatch)) as (server, creation, controller, _):
            reader, writer = await _connect(server)
            op = await creation.create(PRINCIPAL, "completion", INTENT)
            assert (await _read(reader))[1] == 12
            writer.write(_report(op, bandwidth="00000000"))
            await writer.drain()
            await _eventually(lambda: controller.get(op["id"])["status"] == "ROLLBACK_REQUIRED")
            assert controller.get(op["id"])["owned_candidate"] and controller.get(op["id"])["reported"] is None
            connection = server._connections[PEER]
            original = connection.writer.write
            def checked(raw):
                if raw[1] == 11:
                    assert controller.get(op["id"])["completion"]["srp_id"] == 2
                    assert _held(controller, op)
                original(raw)
            monkeypatch.setattr(connection.writer, "write", checked)
            updated = await creation.complete_owned(PRINCIPAL, op["id"])
            assert updated["completion"]["srp_id"] == 2 and (await _read(reader))[1] == 11
            with pytest.raises(ValueError, match="unreplayed"):
                await creation.complete_owned(PRINCIPAL, op["id"])
            writer.write(_report(op, srp=2))
            await writer.drain()
            await _eventually(lambda: controller.get(op["id"])["status"] == "APPLIED_UNVERIFIED")
            assert not controller.get(op["id"])["verified"] and _held(controller, op)
            writer.close()
            await writer.wait_closed()
    asyncio.run(scenario())


def test_iosxe_observed_creation_parameters_do_not_qualify_bandwidth_creation():
    fixtures = Path(__file__).parent / "fixtures/pcep-iosxe-16.9.7"
    requested = parse_objects(bytes.fromhex((fixtures / "creation-request-sanitized.hex").read_text())[4:])
    observed = parse_reports(bytes.fromhex((fixtures / "creation-zero-bandwidth-reconstructed.hex").read_text())[4:],
                             allow_delegation=True)[0]
    assert observed["created"] and observed["delegate"] and observed["srp_id"] == 1
    assert observed["identifiers"]["tunnel_id"] == 62336 and observed["operational_state"] == "ACTIVE"
    sent_bw = next(obj.body for obj in requested if obj.object_class == 5)
    observed_bw = next(attr["body_hex"] for attr in observed["attributes"] if attr["class"] == 5)
    assert sent_bw.hex() == "47f42400" and observed_bw == "00000000"
    observed_lspa = next(attr["body_hex"] for attr in observed["attributes"] if attr["class"] == 9)
    assert observed_lspa.startswith("0000ffff")
    assert observed_lspa != next(obj.body.hex() for obj in requested if obj.object_class == 9)


def test_iosxe_completion_removal_is_not_a_successful_bandwidth_acknowledgment():
    fixtures = Path(__file__).parent / "fixtures/pcep-iosxe-16.9.7"
    update_wire = bytes.fromhex((fixtures / "creation-update-sanitized.hex").read_text())
    removal_wire = bytes.fromhex((fixtures / "creation-update-removal-sanitized.hex").read_text())
    assert update_wire[1] == 11 and removal_wire[1] == 10
    objects = parse_objects(update_wire[4:])
    srp = next(obj for obj in objects if obj.object_class == 33)
    assert int.from_bytes(srp.body[:4], "big") & 1 == 0  # No removal was requested.
    update = parse_reports(update_wire[4:], allow_delegation=True)[0]
    removal = parse_reports(removal_wire[4:], allow_delegation=True)[0]
    assert update["srp_id"] == removal["srp_id"] == 2
    assert update["plsp_id"] == removal["plsp_id"] == 586624
    assert update["identifiers"] == removal["identifiers"]
    assert update["symbolic_name_hex"] == removal["symbolic_name_hex"]
    assert not update["removed"]
    assert next(attr["body_hex"] for attr in update["attributes"] if attr["class"] == 5) == "47f42400"
    assert removal["created"] and removal["delegate"] and removal["removed"]
    assert removal["operational_state"] == "DOWN" and removal["attributes"] == []


def test_completion_correlated_removal_cannot_apply_or_refund_without_owned_cleanup(tmp_path: Path):
    controller = manager(tmp_path)
    op = sent(controller)
    controller.bind_owned_candidate(op["id"], report(op))
    controller.uncertain(op["id"], "creation ignored bandwidth")
    controller.mark_completion(op["id"], "session-one", 6, "b" * 64)
    removed = report(op, code=0, srp=6, removed=True)
    removed["attributes"] = []
    with pytest.raises(ValueError, match="owned creation report"):
        controller.observe(op["id"], removed)
    with pytest.raises(ValueError, match="cleanup boundary"):
        controller.record_removal(op["id"], removed)
    with pytest.raises(ValueError, match="independent resource-release"):
        controller.confirmed_removal(op["id"], removed, resources_released=True)
    restored = manager(tmp_path).get(op["id"])
    assert restored["status"] == "ROLLBACK_REQUIRED"
    assert restored["reported"] is None and restored["cleanup"] is None and restored["verified"] is False
    assert not restored["completion"].get("acknowledged")
    assert hold(tmp_path, op)["status"] == "ROLLBACK_REQUIRED"


def _released_unacknowledged_completion(controller):
    """Persist the recovered lab history without pretending it was acknowledged."""
    op = controller.plan(PRINCIPAL, "historical-completion", **INTENT)
    op = controller.mark_sending(op["id"], "historical-session", 1, "a" * 64)
    controller.bind_owned_candidate(op["id"], report(op))
    controller.uncertain(op["id"], "bandwidth was not applied")
    controller.mark_completion(op["id"], "historical-session", 2, "b" * 64)
    pcc = {"peer_ip": op["peer"], "state": "ESTABLISHED", "fresh": True, "synchronized": True,
           "valid_until": time.time() + 60, "reports": {}}
    evidence = {"router_reservations": {node: "c" * 64 for node in op["path_nodes"]},
                "headend_configuration": "d" * 64, "observed_at": time.time()}
    return controller.confirmed_absence(op["id"], pcc, evidence, resources_released=True)


def test_released_completion_history_does_not_kill_monitor_or_block_next_creation(tmp_path, monkeypatch):
    async def scenario():
        lab = _lab(tmp_path, monkeypatch, update_ack_timeout=0.01)
        server, creation, controller, _ = lab
        historical = _released_unacknowledged_completion(controller)
        rejected = controller.plan(PRINCIPAL, "historical-rejection", **INTENT)
        rejected = controller.reject_before_send(rejected["id"], "rejected before delivery")
        assert historical["status"] == "ROLLED_BACK" and not historical["completion"].get("acknowledged")
        assert ReservationLedger(controller.state_dir).entries == {}

        async with _running(lab):
            reader, writer = await _connect(server)
            original_list = controller.list
            monitor_passes = 0

            def counted_list():
                nonlocal monitor_passes
                monitor_passes += 1
                return original_list()

            monkeypatch.setattr(controller, "list", counted_list)
            await _eventually(lambda: monitor_passes > 0, seconds=2)
            assert server._capture_error is None and not creation.monitor.done()
            assert controller.get(historical["id"]) == historical
            assert controller.get(rejected["id"]) == rejected
            assert ReservationLedger(controller.state_dir).entries == {}

            fresh = await creation.create(PRINCIPAL, "after-recovery", INTENT)
            assert fresh["status"] == "PUSHING" and (await _read(reader))[1] == 12
            # Skipping released history must not disable timeouts or refund a
            # current unacknowledged operation's reservation.
            await _eventually(lambda: controller.get(fresh["id"])["status"] == "ROLLBACK_REQUIRED", seconds=2)
            assert controller.get(fresh["id"])["reason"] == "acknowledgment_timeout_reconciliation_required"
            assert _held(controller, fresh)["status"] == "ROLLBACK_REQUIRED"
            assert server._capture_error is None and not creation.monitor.done()
            writer.close()
            await writer.wait_closed()
    asyncio.run(scenario())


@pytest.mark.parametrize("removed", [False, True])
def test_delayed_report_for_released_completion_cannot_reopen_history_or_poison_writer(
    tmp_path, monkeypatch, removed,
):
    server, creation, controller, _ = _lab(tmp_path, monkeypatch)
    creation.server = server
    historical = _released_unacknowledged_completion(controller)
    delayed = report(historical, srp=2, removed=removed)
    fresh = controller.plan(PRINCIPAL, "after-recovery", **INTENT)
    fresh = controller.mark_sending(fresh["id"], "current-session", 1, "e" * 64)

    # Retain ownership suppression for old identities, so a late report cannot
    # reach the separate preconfigured-tunnel writer.
    assert creation.owns(PEER, delayed)
    creation.observe(PEER, [delayed])
    assert server._capture_error is None
    assert controller.get(historical["id"]) == historical
    assert historical["reservation_id"] not in ReservationLedger(controller.state_dir).entries
    assert controller.get(fresh["id"]) == fresh and _held(controller, fresh)["status"] == "PUSHING"


def test_unacknowledged_current_completion_still_times_out_and_retains_hold(tmp_path, monkeypatch):
    async def scenario():
        async with _running(_lab(tmp_path, monkeypatch, update_ack_timeout=0.01)) as lab:
            server, creation, controller, _ = lab
            reader, writer = await _connect(server)
            op = await creation.create(PRINCIPAL, "pending-completion", INTENT)
            await _read(reader)
            writer.write(_report(op))
            await writer.drain()
            await _eventually(lambda: controller.get(op["id"])["status"] == "APPLIED_UNVERIFIED")
            pending = await creation.complete_owned(PRINCIPAL, op["id"])
            assert pending["status"] == "APPLIED_UNVERIFIED" and (await _read(reader))[1] == 11
            await _eventually(lambda: controller.get(op["id"])["status"] == "ROLLBACK_REQUIRED", seconds=2)
            timed_out = controller.get(op["id"])
            assert timed_out["reason"] == "owned_completion_acknowledgment_timeout"
            assert not timed_out["completion"].get("acknowledged")
            assert _held(controller, op)["status"] == "ROLLBACK_REQUIRED"
            assert server._capture_error is None and not creation.monitor.done()
            writer.close()
            await writer.wait_closed()
    asyncio.run(scenario())


def test_diagnostic_owned_delete_removal_has_old_srp_and_cannot_release_hold(tmp_path, monkeypatch):
    fixtures = Path(__file__).parent / "fixtures/pcep-iosxe-16.9.7"
    hashes = {
        "create": "c1968d000e6ca9ce81e56406f3bbcfd9a209fa32f0ed0c71762e8cb0241024ba",
        "active-zero-bandwidth": "c9635ecf22ca68823f959ded794d76dbc2e08019761bcb3ce6764a57fa815892",
        "delete": "f1d7f5e950d0347116025f099d936adb6396bba99cda6290ef5ce1d1ea432b4b",
        "removal": "c3314bc120e5b4d2c385ee2eabfd6b88244d1b0b2b33f6d116f2476f35fac440",
    }
    frames = {}
    for name, expected in hashes.items():
        frames[name] = bytes.fromhex((fixtures / f"diagnostic-004-{name}-sanitized.hex").read_text())
        assert hashlib.sha256(frames[name]).hexdigest() == expected
    create = parse_objects(frames["create"][4:])
    delete = parse_objects(frames["delete"][4:])
    active = parse_reports(frames["active-zero-bandwidth"][4:], allow_delegation=True)[0]
    removed = parse_reports(frames["removal"][4:], allow_delegation=True)[0]
    assert frames["create"][1] == frames["delete"][1] == 12
    assert frames["active-zero-bandwidth"][1] == frames["removal"][1] == 10
    assert int.from_bytes(create[0].body[4:8], "big") == active["srp_id"] == removed["srp_id"] == 1
    assert int.from_bytes(delete[0].body[:4], "big") == 1
    assert int.from_bytes(delete[0].body[4:8], "big") == 2
    assert active["created"] and active["delegate"] and active["operational_state"] == "ACTIVE"
    assert next(obj.body.hex() for obj in create if obj.object_class == 5) == "47f42400"
    observed_bandwidth = next(attr["body_hex"] for attr in active["attributes"]
                              if attr["class"] == 5 and attr["type"] == 1)
    assert observed_bandwidth == "00000000"
    assert removed["created"] and removed["removed"] and not removed["delegate"]
    assert removed["operational_state"] == "DOWN" and removed["attributes"] == []
    assert active["plsp_id"] == removed["plsp_id"] == 586624
    assert active["identifiers"] == removed["identifiers"]
    assert active["symbolic_name_hex"] == removed["symbolic_name_hex"]

    # Offline replay only: use a simulated current session and receive times,
    # preserving captured SRP IDs, identity, flags and attributes unchanged.
    _, _, controller, _ = _lab(tmp_path, monkeypatch)
    monkeypatch.setattr("ninja_pce.tunnel_creation.uuid.uuid4", lambda: UUID(int=4))
    op = controller.plan(PRINCIPAL, "diagnostic-cleanup-replay", **INTENT)
    assert encode_initiate(1, op["symbolic_name"], op["source_ip"], op["destination_ip"],
                           tuple(op["ero"]), op["bandwidth_bps"]) == frames["create"]
    op = controller.mark_sending(op["id"], "simulated-replay-session", 1, hashes["create"])
    active.update(session_id=op["send"]["session_id"], received_at=time.time(), stale=False)
    controller.bind_owned_candidate(op["id"], active)
    with pytest.raises(ValueError, match="bandwidth"):
        controller.observe(op["id"], active)
    controller.uncertain(op["id"], "captured report ignored requested bandwidth")
    assert encode_delete(2, active["plsp_id"], op["symbolic_name"]) == frames["delete"]
    controller.mark_removing(op["id"], op["send"]["session_id"], 2, hashes["delete"])
    removed.update(session_id=op["send"]["session_id"], received_at=time.time(), stale=False)
    with pytest.raises(ValueError, match="cleanup boundary"):
        controller.record_removal(op["id"], removed)
    with pytest.raises(ValueError, match="independent resource-release"):
        controller.confirmed_removal(op["id"], removed, resources_released=True)
    unresolved = controller.get(op["id"])
    assert unresolved["status"] == "ROLLBACK_REQUIRED" and unresolved["reported"] is None
    assert unresolved["cleanup"]["srp_id"] == 2 and "report_digest" not in unresolved["cleanup"]
    assert _held(controller, op)["status"] == "ROLLBACK_REQUIRED"
