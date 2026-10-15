"""Wire and writer-fence tests for independent legacy and owned recovery."""
from __future__ import annotations

import asyncio
import copy
import json
import struct
import time
from datetime import datetime, timezone
from ipaddress import IPv4Address

import pytest
from test_pcep import _eventually, _read
from test_pcep_delegation import connect
from test_pcep_failover import _publish
from test_pcep_failover_server import (
    active_body,
    keep_peer_alive,
    progress_body,
    update_message,
    wait_for_status,
)
from test_pcep_managed_failover import managed_lab

import ninja_pce.pcep_compute as computation
from ninja_pce.pcep import (
    ERROR,
    KEEPALIVE,
    PCRPT,
    PcepServer,
    PcepServerConfig,
    encode_message,
    encode_object,
    encode_open,
    parse_objects,
    parse_open,
    parse_reports,
)
from ninja_pce.pcep_delegation import DelegatedPathComputer
from ninja_pce.pcep_failover import FailureRecoveryPlanner
from ninja_pce.pcep_managed_failover import ManagedFailureRecoveryPlanner
from ninja_pce.pcep_provisioning import JournalProvisioningAuthorization
from ninja_pce.state import load_state, write_state

PEER = "127.0.0.1"


def server_lab(tmp_path, monkeypatch, *, legacy=True):
    managed, computer, _, report = managed_lab(tmp_path, monkeypatch, peer=PEER)
    managed.clock = time.time
    managed.debounce = managed.cooldown = .01
    report["plsp_id"] = 524488
    delegated = DelegatedPathComputer(
        computer, {PEER: (100,)} if legacy else {},
        provisioning_authorizer=JournalProvisioningAuthorization(managed.operation_journal, computer.headends),
    )
    fallback = (FailureRecoveryPlanner(computer, peer=PEER, tunnel_id=100,
                                      journal_path=tmp_path / "legacy" / "recovery.json",
                                      debounce_seconds=.01, cooldown_seconds=.01) if legacy else None)
    server = PcepServer(PcepServerConfig((PEER,), port=0, keepalive=1, deadtimer=5),
                        path_computer=computer, delegated_computer=delegated,
                        failover=fallback, managed_failover=managed)
    return server, managed, fallback, computer, report


def managed_body(report, *, delegate=True, code=2, empty=False, sync=False, srp=0):
    """Encode a real report with the fixture's exact owned identity and constraints."""
    lsp = ((report["plsp_id"] << 12) | 8 | int(delegate) | (2 if sync else 0) | (code << 4)).to_bytes(4, "big")
    for item in report["tlvs"]:
        value = bytes.fromhex(item["value_hex"])
        lsp += struct.pack("!HH", item["type"], len(value)) + value + bytes(-len(value) % 4)
    ero = b"" if empty else b"".join(
        bytes((1, 8)) + IPv4Address(hop["address"]).packed + bytes((32, 0)) for hop in report["ero"])
    attributes = []
    for item in report["attributes"]:
        body = bytes.fromhex(item["body_hex"])
        if (item["class"], item["type"]) == (5, 5) and code == 0:
            body = bytes(4)
        attributes.append(encode_object(item["class"], body, item["type"]))
    return (encode_object(33, bytes(4) + srp.to_bytes(4, "big")) + encode_object(32, lsp) +
            encode_object(7, ero) + b"".join(attributes))


def healthy_legacy_body():
    # Keep legacy Tunnel100 on the unaffected lower path while the managed
    # tunnel's captured upper path is withdrawn. This exercises monitor fairness.
    return active_body()


def withdraw_managed(computer, monkeypatch):
    captured = datetime.fromisoformat(json.loads(computer.raw_path.read_text())["captured_at"])
    clock = [captured.timestamp() + 2]
    _publish(computer, clock)
    monkeypatch.setattr(computation, "_utcnow", lambda: datetime.fromtimestamp(clock[0], timezone.utc))


async def cleanup(server, writer, heartbeat):
    heartbeat.cancel()
    await asyncio.gather(heartbeat, return_exceptions=True)
    await server.close()
    writer.close()
    await writer.wait_closed()


def test_default_and_legacy_capture_contract_remains_unchanged(tmp_path, monkeypatch):
    server, managed, legacy, computer, _ = server_lab(tmp_path, monkeypatch)
    default = PcepServer(PcepServerConfig((PEER,), port=0))
    assert default.snapshot()["failure_recovery"] is None
    assert "managed_failure_recovery" not in default.snapshot()
    previous = PcepServer(server.config, path_computer=computer,
                          delegated_computer=server.delegated_computer, failover=legacy)
    assert previous.snapshot()["failure_recovery"] == legacy.snapshot()
    assert "managed_failure_recovery" not in previous.snapshot()
    assert server.snapshot()["managed_failure_recovery"] == managed.snapshot()
    assert server.delegated_computer.allowed_tunnels == {PEER: (100,)}


def test_restart_preserves_legacy_scope_and_marks_all_loaded_reports_historical(tmp_path, monkeypatch):
    server, managed, legacy, computer, report = server_lab(tmp_path, monkeypatch)
    capture = server.snapshot()
    legacy_report = parse_reports(healthy_legacy_body(), allow_delegation=True)[0]
    capture["peers"][PEER] = {
        "state": "ESTABLISHED", "fresh": True, "synchronized": True,
        "last_received_at": time.time(), "reports": {"legacy": legacy_report, "managed": report},
        "updates": {},
    }
    capture_path = tmp_path / "pcep-capture.json"
    write_state(capture_path, capture)
    restarted_managed = ManagedFailureRecoveryPlanner(
        computer, operation_journal=managed.operation_journal, operation_id=managed.operation_id,
        journal_path=managed.path)
    restarted_legacy = FailureRecoveryPlanner(computer, peer=PEER, tunnel_id=100, journal_path=legacy.path)
    restarted = PcepServer(server.config, capture_path=capture_path, path_computer=computer,
                            delegated_computer=server.delegated_computer,
                            failover=restarted_legacy, managed_failover=restarted_managed)
    assert restarted.failover.tunnel_id == 100
    assert restarted.managed_failover.tunnel_id == managed.tunnel_id
    assert restarted.delegated_computer.allowed_tunnels == {PEER: (100,)}
    peer = restarted.snapshot()["peers"][PEER]
    assert peer["state"] == "DISCONNECTED"
    assert peer["fresh"] is False and peer["synchronized"] is False
    assert peer["stale_reason"] == "process_restart"
    assert all(item["stale"] is True for item in peer["reports"].values())
    assert restarted.failover.snapshot()["baseline"] is None
    assert restarted.managed_failover.snapshot()["baseline"] is None
    assert not restarted._connections


@pytest.mark.parametrize("change", ["computer", "journal", "missing-authorizer", "same-target", "same-state"])
def test_server_requires_exact_managed_authority_and_distinct_profiles(tmp_path, monkeypatch, change):
    server, managed, legacy, computer, _ = server_lab(tmp_path, monkeypatch)
    delegated = server.delegated_computer
    if change == "computer":
        computer = copy.copy(computer)
    elif change in {"journal", "missing-authorizer"}:
        authorizer = (JournalProvisioningAuthorization(tmp_path / "other.json", computer.headends)
                      if change == "journal" else None)
        delegated = DelegatedPathComputer(computer, {PEER: (100,)}, provisioning_authorizer=authorizer)
    elif change == "same-target":
        delegated = DelegatedPathComputer(
            computer, {PEER: (managed.tunnel_id,)},
            provisioning_authorizer=delegated.provisioning_authorizer)
        legacy = FailureRecoveryPlanner(computer, peer=PEER, tunnel_id=managed.tunnel_id,
                                         journal_path=tmp_path / "duplicate" / "recovery.json")
    else:
        legacy.path = managed.path
    with pytest.raises(ValueError):
        PcepServer(server.config, path_computer=computer, delegated_computer=delegated,
                   failover=legacy, managed_failover=managed)


def test_managed_empty_ero_never_enters_initial_delegation_scheduler(tmp_path, monkeypatch):
    server, managed, _, _, report = server_lab(tmp_path, monkeypatch, legacy=False)

    async def run():
        await server.start()
        _, writer = await connect(server)
        heartbeat = asyncio.create_task(keep_peer_alive(writer))
        try:
            writer.write(encode_message(PCRPT, managed_body(report, code=0, empty=True)))
            await writer.drain()
            await _eventually(lambda: bool(server.snapshot()["peers"][PEER]["reports"]))
            await asyncio.sleep(1.1)
            connection = server._connections[PEER]
            assert connection.delegation_attempts == set()
            assert connection.state["updates"] == {}
            assert managed.snapshot()["attempts"] == 0
            assert server.delegated_computer.allowed_tunnels == {}
            assert not any(event["direction"] == "tx" and event["message_type"] == 11
                           for event in server.snapshot()["events"])
        finally:
            await cleanup(server, writer, heartbeat)

    asyncio.run(run())


def test_sync_undelegated_history_then_live_delegation_adopts_without_recovery_write(tmp_path, monkeypatch):
    server, managed, _, _, report = server_lab(tmp_path, monkeypatch, legacy=False)
    original_ledger = (managed.operation_journal.parent / ".ninja-pce-reservations.json").read_bytes()
    observed = asyncio.Event()
    original_observe = managed.observe

    def observe(peer, item, *, fresh, synchronized):
        original_observe(peer, item, fresh=fresh, synchronized=synchronized)
        if item.get("sync") is True and fresh and synchronized:
            observed.set()

    monkeypatch.setattr(managed, "observe", observe)

    async def run():
        await server.start()
        reader, writer = await asyncio.open_connection(PEER, server.addresses[0][1])
        heartbeat = asyncio.create_task(keep_peer_alive(writer))
        try:
            assert parse_open((await _read(reader))[4:])["stateful_flags"] == 1
            writer.write(encode_open(1, 5, updates=True) + encode_message(KEEPALIVE))
            await writer.drain()
            assert await _read(reader) == encode_message(KEEPALIVE)
            # IOS advertises historical ACTIVE D=0 S=1 with its previous SRP,
            # then ends synchronization before reasserting live D=1 S=0.
            writer.write(encode_message(PCRPT, managed_body(report, delegate=False, sync=True, srp=2)))
            writer.write(encode_message(PCRPT, bytes.fromhex(
                "2010001c00000000001200100000000000000000000000000000000007100004")))
            await writer.drain()
            await asyncio.wait_for(observed.wait(), 4)
            waiting = managed.snapshot()
            assert not waiting["blocked"] and waiting["baseline"] is None
            assert waiting["pending"] is None and waiting["attempts"] == 0
            assert not server.snapshot()["peers"][PEER]["updates"]
            writer.write(encode_message(PCRPT, managed_body(report)))
            await writer.drain()
            await asyncio.wait_for(wait_for_status(managed, "MONITORING"), 4)
            assert managed.snapshot()["attempts"] == 0
            assert (managed.operation_journal.parent / ".ninja-pce-reservations.json").read_bytes() == original_ledger
            assert server.delegated_computer.allowed_tunnels == {}
            assert not any(event["direction"] == "tx" and event["message_type"] == 11
                           for event in server.snapshot()["events"])
        finally:
            await cleanup(server, writer, heartbeat)

    asyncio.run(run())


def test_healthy_legacy_cannot_starve_managed_recovery_or_consume_its_reports(tmp_path, monkeypatch):
    server, managed, legacy, computer, report = server_lab(tmp_path, monkeypatch)

    async def run():
        await server.start()
        reader, writer = await connect(server)
        heartbeat = asyncio.create_task(keep_peer_alive(writer))
        try:
            writer.write(encode_message(PCRPT, healthy_legacy_body() + managed_body(report)))
            await writer.drain()
            await asyncio.wait_for(wait_for_status(legacy, "MONITORING"), 5)
            await asyncio.wait_for(wait_for_status(managed, "MONITORING"), 5)
            assert not server.snapshot()["peers"][PEER]["updates"]
            withdraw_managed(computer, monkeypatch)
            update = await update_message(reader)
            actual = parse_reports(update[4:], allow_delegation=True)[0]
            assert actual["identifiers"]["tunnel_id"] == managed.tunnel_id
            assert managed.snapshot()["pending"] is not None
            assert legacy.snapshot()["attempts"] == 0
            writer.write(encode_message(PCRPT, progress_body(update, 2)))
            await writer.drain()
            await _eventually(lambda: managed.snapshot()["status"] == "RECOVERED")
            assert legacy.snapshot()["status"] == "MONITORING"
            assert legacy.snapshot()["pending"] is None
            assert server.delegated_computer.allowed_tunnels == {PEER: (100,)}
        finally:
            await cleanup(server, writer, heartbeat)

    asyncio.run(run())


def test_pcc_error_is_routed_only_to_owning_recovery_profile(tmp_path, monkeypatch):
    server, managed, legacy, computer, report = server_lab(tmp_path, monkeypatch)

    async def run():
        await server.start()
        reader, writer = await connect(server)
        heartbeat = asyncio.create_task(keep_peer_alive(writer))
        try:
            writer.write(encode_message(PCRPT, healthy_legacy_body() + managed_body(report)))
            await writer.drain()
            await asyncio.wait_for(wait_for_status(legacy, "MONITORING"), 5)
            await asyncio.wait_for(wait_for_status(managed, "MONITORING"), 5)
            withdraw_managed(computer, monkeypatch)
            update = await update_message(reader)
            srp = next(obj.body for obj in parse_objects(update[4:]) if obj.object_class == 33)
            writer.write(encode_message(ERROR, encode_object(33, srp) + encode_object(13, bytes((0, 0, 19, 1)))))
            await writer.drain()
            await _eventually(lambda: managed.snapshot()["reason"] == "pcc_error")
            assert managed.snapshot()["blocked"] is True
            assert legacy.snapshot()["status"] == "MONITORING"
            assert legacy.snapshot()["attempts"] == 0
        finally:
            await cleanup(server, writer, heartbeat)

    asyncio.run(run())


@pytest.mark.parametrize("change", ["journal", "delegation", "kill-switch"])
def test_managed_authority_is_rechecked_under_writer_lock(tmp_path, monkeypatch, change):
    server, managed, _, computer, report = server_lab(tmp_path, monkeypatch, legacy=False)
    ledger_path = managed.operation_journal.parent / ".ninja-pce-reservations.json"
    original_hold = ledger_path.read_bytes()

    async def run():
        await server.start()
        _, writer = await connect(server)
        heartbeat = asyncio.create_task(keep_peer_alive(writer))
        held = False
        connection = server._connections[PEER]
        try:
            writer.write(encode_message(PCRPT, managed_body(report)))
            await writer.drain()
            await asyncio.wait_for(wait_for_status(managed, "MONITORING"), 4)
            await connection.send_lock.acquire()
            held = True
            withdraw_managed(computer, monkeypatch)
            await asyncio.wait_for(wait_for_status(managed, "READY"), 4)
            if change == "journal":
                journal = load_state(managed.operation_journal)
                journal["operations"][managed.operation_id]["status"] = "RECOVERY_REQUIRED"
                write_state(managed.operation_journal, journal)
            elif change == "delegation":
                writer.write(encode_message(PCRPT, managed_body(report, delegate=False)))
                await writer.drain()
                await _eventually(lambda: managed.snapshot()["baseline"] is None)
            else:
                monkeypatch.setenv("NINJA_PCE_PREVIEW_ONLY", "1")
            connection.send_lock.release()
            held = False
            await asyncio.sleep(.2)
            assert managed.snapshot()["attempts"] == 0
            assert managed.snapshot()["pending"] is None
            assert connection.state["updates"] == {}
            assert ledger_path.read_bytes() == original_hold
            assert not any(event["direction"] == "tx" and event["message_type"] == 11
                           for event in server.snapshot()["events"])
        finally:
            if held:
                connection.send_lock.release()
            await cleanup(server, writer, heartbeat)

    asyncio.run(run())


def test_disconnect_invalidates_both_recovery_profiles_without_removing_legacy_scope(tmp_path, monkeypatch):
    server, managed, legacy, _, report = server_lab(tmp_path, monkeypatch)

    async def run():
        await server.start()
        _, writer = await connect(server)
        heartbeat = asyncio.create_task(keep_peer_alive(writer))
        try:
            writer.write(encode_message(PCRPT, healthy_legacy_body() + managed_body(report)))
            await writer.drain()
            await asyncio.wait_for(wait_for_status(legacy, "MONITORING"), 5)
            await asyncio.wait_for(wait_for_status(managed, "MONITORING"), 5)
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
            writer.close()
            await writer.wait_closed()
            await _eventually(lambda: server.snapshot()["peers"][PEER]["state"] == "DISCONNECTED")
            assert legacy.snapshot()["reason"] == "pcep_session_disconnected"
            assert managed.snapshot()["reason"] == "pcep_session_disconnected"
            assert legacy.snapshot()["baseline"] is None
            assert managed.snapshot()["baseline"] is None
            assert server.delegated_computer.allowed_tunnels == {PEER: (100,)}
        finally:
            await cleanup(server, writer, heartbeat)

    asyncio.run(run())
