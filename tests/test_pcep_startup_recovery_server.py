"""Automatic router-session restoration through the actual PCEP writer fence."""
from __future__ import annotations

import asyncio
import copy
import json
import struct
import time
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from test_pcep import _eventually, _read
from test_pcep_delegation import connect
from test_pcep_failover import _publish
from test_pcep_failover_server import keep_peer_alive, progress_body, update_message, wait_for_status
from test_pcep_managed_recovery_server import PEER, cleanup, managed_body, server_lab, withdraw_managed
from test_pcep_reinstatement_server import latest_report, updates_sent

import ninja_pce.pcep as protocol
import ninja_pce.pcep_compute as computation
from ninja_pce.pcep import PCRPT, PcepServer, encode_message, parse_reports
from ninja_pce.pcep_managed_failover import ManagedFailureRecoveryPlanner
from ninja_pce.reservations import ReservationLedger
from ninja_pce.state import load_state, write_state


def startup_server_lab(tmp_path, monkeypatch, *, debounce=.01, startup=True):
    seed_server, seed, legacy, computer, report = server_lab(tmp_path, monkeypatch, legacy=False)
    seed._data.update(attempts=2, last_attempt_at=time.time() - 60)
    seed._save()
    offset = [0.0]
    # Advance wall time consistently for planner intent and received-PCRpt
    # timestamps, while the actual socket liveness clock remains monotonic.
    def wall_clock():
        return time.time() + offset[0]
    monkeypatch.setattr(protocol, "time", SimpleNamespace(time=wall_clock, monotonic=time.monotonic))
    managed = ManagedFailureRecoveryPlanner(
        computer, operation_journal=seed.operation_journal, operation_id=seed.operation_id,
        journal_path=seed.path, startup_recovery=startup, debounce_seconds=debounce,
        clock=wall_clock)
    server = PcepServer(seed_server.config, path_computer=computer,
                        delegated_computer=seed_server.delegated_computer,
                        failover=legacy, managed_failover=managed)
    return server, managed, computer, report, offset


async def publish_down(server, managed, writer, fixture_report):
    reports = server.snapshot()["peers"][PEER]["reports"]
    previous = latest_report(server, managed)["received_at"] if reports else 0
    writer.write(encode_message(PCRPT, managed_body(fixture_report, code=0, empty=True)))
    await writer.drain()
    await _eventually(lambda: bool(server.snapshot()["peers"][PEER]["reports"]) and
                     latest_report(server, managed)["received_at"] > previous and
                     latest_report(server, managed)["plsp_id"] == fixture_report["plsp_id"])
    report = latest_report(server, managed)
    assert report["operational_code"] == 0 and report["ero"] == []
    assert report["stale"] is False and report["sync"] is False
    return report


async def confirm_active(writer, managed, update):
    writer.write(encode_message(PCRPT, progress_body(update, 2)))
    await writer.drain()
    await _eventually(lambda: managed.snapshot()["status"] == "RECOVERED")
    assert managed.snapshot()["pending"] is None
    assert managed.snapshot()["attempts"] == 2


async def startup_update(reader):
    # The shared update helper bounds ordinary tests to six seconds. This test
    # also includes the production five-second debounce and one-second watcher.
    async def read_update():
        while True:
            message = await _read(reader)
            if message[1] == 11:
                return message
    return await asyncio.wait_for(read_update(), 9)


def test_default_five_second_debounce_then_ack_then_active_restores_without_request_file(tmp_path, monkeypatch):
    server, managed, _, fixture_report, _ = startup_server_lab(tmp_path, monkeypatch, debounce=5)
    operation_before = managed.operation_journal.read_bytes()

    async def run():
        await server.start()
        reader, writer = await connect(server)
        heartbeat = asyncio.create_task(keep_peer_alive(writer))
        try:
            report = await publish_down(server, managed, writer, fixture_report)
            started = time.monotonic()
            assert managed.snapshot()["baseline"] is None
            assert managed.reinstatement_path is None
            update = await startup_update(reader)
            elapsed = time.monotonic() - started
            assert 5 <= elapsed < 9
            sent = parse_reports(update[4:], allow_delegation=True)[0]
            pending = managed.snapshot()["pending"]
            assert sent["identifiers"] == report["identifiers"]
            assert sent["plsp_id"] == report["plsp_id"]
            assert pending["intent_mode"] == "startup"
            assert pending["outcome"]["status"] == "STARTUP_PATH"
            assert managed.snapshot()["attempts"] == 2
            assert managed.snapshot()["startup_recovery"]["total_attempts"] == 1
            assert len(updates_sent(server)) == 1
            writer.write(encode_message(PCRPT, progress_body(update, 0)))
            await writer.drain()
            await _eventually(lambda: server.snapshot()["peers"][PEER]["updates"][str(sent["srp_id"])]["acknowledged"])
            assert managed.snapshot()["pending"] is not None
            assert managed.snapshot()["baseline"] is None
            await confirm_active(writer, managed, update)
            assert managed.snapshot()["baseline"]["ero"] == pending["ero"]
            assert managed.operation_journal.read_bytes() == operation_before
            assert server.delegated_computer.allowed_tunnels == {}
            await asyncio.sleep(1.2)
            assert len(updates_sent(server)) == 1
        finally:
            await cleanup(server, writer, heartbeat)

    asyncio.run(run())


def test_new_pcep_session_restores_again_without_consuming_failure_budget(tmp_path, monkeypatch):
    server, managed, _, fixture_report, offset = startup_server_lab(tmp_path, monkeypatch)

    async def run():
        await server.start()
        reader, writer = await connect(server)
        heartbeat = asyncio.create_task(keep_peer_alive(writer))
        try:
            first = await publish_down(server, managed, writer, fixture_report)
            update = await update_message(reader)
            await confirm_active(writer, managed, update)
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
            writer.close()
            await writer.wait_closed()
            await _eventually(lambda: server.snapshot()["peers"][PEER]["state"] == "DISCONNECTED")
            offset[0] += 31
            reader, writer = await connect(server)
            heartbeat = asyncio.create_task(keep_peer_alive(writer))
            fixture_report["plsp_id"] += 1
            second = await publish_down(server, managed, writer, fixture_report)
            assert first["session_id"] != second["session_id"]
            update = await update_message(reader)
            await confirm_active(writer, managed, update)
            startup = managed.snapshot()["startup_recovery"]
            assert startup["total_attempts"] == 2
            assert [item["session_id"] for item in startup["records"]] == [first["session_id"], second["session_id"]]
            assert managed.snapshot()["attempts"] == 2
            assert len(updates_sent(server)) == 2
        finally:
            await cleanup(server, writer, heartbeat)

    asyncio.run(run())


def test_post_sync_undelegated_report_survives_watcher_until_pcc_redelegates(tmp_path, monkeypatch):
    server, managed, _, fixture_report, _ = startup_server_lab(tmp_path, monkeypatch)

    async def run():
        await server.start()
        reader, writer = await connect(server)
        heartbeat = asyncio.create_task(keep_peer_alive(writer))
        try:
            writer.write(encode_message(PCRPT, managed_body(fixture_report, delegate=False, code=0, empty=True)))
            await writer.drain()
            await _eventually(lambda: bool(server.snapshot()["peers"][PEER]["reports"]))
            await asyncio.sleep(2.2)
            assert not managed.snapshot()["blocked"]
            assert managed.snapshot()["baseline"] is managed.snapshot()["pending"] is None
            assert managed.snapshot()["startup_recovery"]["total_attempts"] == 0
            assert not updates_sent(server)
            await publish_down(server, managed, writer, fixture_report)
            await confirm_active(writer, managed, await update_message(reader))
            assert managed.snapshot()["startup_recovery"]["total_attempts"] == 1
            assert len(updates_sent(server)) == 1
        finally:
            await cleanup(server, writer, heartbeat)

    asyncio.run(run())


def test_writer_lock_revocation_has_no_side_effects_and_redelegation_can_restore(tmp_path, monkeypatch):
    server, managed, _, fixture_report, _ = startup_server_lab(tmp_path, monkeypatch)
    ledger_path = managed.operation_journal.parent / ".ninja-pce-reservations.json"
    ledger_before = ledger_path.read_bytes()
    operation_before = managed.operation_journal.read_bytes()

    async def run():
        await server.start()
        reader, writer = await connect(server)
        heartbeat = asyncio.create_task(keep_peer_alive(writer))
        connection = server._connections[PEER]
        held = False
        try:
            original = await publish_down(server, managed, writer, fixture_report)
            await connection.send_lock.acquire()
            held = True
            await asyncio.wait_for(wait_for_status(managed, "STARTUP_READY"), 4)
            waiting_delivery = connection.computation
            assert waiting_delivery is not None and not waiting_delivery.done()
            writer.write(encode_message(PCRPT, managed_body(fixture_report, delegate=False,
                                                           code=0, empty=True)))
            await writer.drain()
            await _eventually(lambda: latest_report(server, managed)["received_at"] > original["received_at"]
                             and latest_report(server, managed)["delegate"] is False)
            await _eventually(lambda: managed.snapshot()["status"] == "WAITING_FRESH_REPORT")
            connection.send_lock.release()
            held = False
            await asyncio.wait_for(asyncio.shield(waiting_delivery), 3)
            # The watcher must also tolerate the retained revoked report after
            # the old delivery has been rejected by the actual writer fence.
            await asyncio.sleep(1.2)
            assert not managed.snapshot()["blocked"]
            assert managed.snapshot()["baseline"] is managed.snapshot()["pending"] is None
            assert managed.snapshot()["attempts"] == 2
            assert managed.snapshot()["startup_recovery"]["total_attempts"] == 0
            assert connection.state["updates"] == {} and not updates_sent(server)
            assert ledger_path.read_bytes() == ledger_before
            assert managed.operation_journal.read_bytes() == operation_before

            await publish_down(server, managed, writer, fixture_report)
            await confirm_active(writer, managed, await update_message(reader))
            assert managed.snapshot()["startup_recovery"]["total_attempts"] == 1
            assert len(updates_sent(server)) == 1
            assert managed.operation_journal.read_bytes() == operation_before
        finally:
            if held:
                connection.send_lock.release()
            await cleanup(server, writer, heartbeat)

    asyncio.run(run())


def test_disabled_startup_recovery_cannot_create_path_after_live_redelegation(tmp_path, monkeypatch):
    server, managed, _, fixture_report, _ = startup_server_lab(tmp_path, monkeypatch, startup=False)
    ledger_path = managed.operation_journal.parent / ".ninja-pce-reservations.json"
    ledger_before = ledger_path.read_bytes()
    operation_before = managed.operation_journal.read_bytes()

    async def run():
        await server.start()
        _, writer = await connect(server)
        heartbeat = asyncio.create_task(keep_peer_alive(writer))
        try:
            writer.write(encode_message(PCRPT, managed_body(fixture_report, delegate=False,
                                                           code=0, empty=True)))
            await writer.drain()
            await _eventually(lambda: bool(server.snapshot()["peers"][PEER]["reports"]))
            await asyncio.sleep(1.2)
            assert not managed.snapshot()["blocked"]
            await publish_down(server, managed, writer, fixture_report)
            await asyncio.sleep(2.2)
            snapshot = managed.snapshot()
            assert snapshot["startup_recovery_enabled"] is False
            assert "startup_recovery" not in snapshot
            assert not snapshot["blocked"]
            assert snapshot["baseline"] is snapshot["pending"] is None
            assert snapshot["attempts"] == 2
            assert not server.snapshot()["peers"][PEER]["updates"]
            assert not updates_sent(server)
            assert ledger_path.read_bytes() == ledger_before
            assert managed.operation_journal.read_bytes() == operation_before
            assert server.delegated_computer.allowed_tunnels == {}
        finally:
            await cleanup(server, writer, heartbeat)

    asyncio.run(run())


def test_same_session_empty_delegation_cannot_repeat_startup_write(tmp_path, monkeypatch):
    server, managed, _, fixture_report, offset = startup_server_lab(tmp_path, monkeypatch)

    async def run():
        await server.start()
        reader, writer = await connect(server)
        heartbeat = asyncio.create_task(keep_peer_alive(writer))
        try:
            await publish_down(server, managed, writer, fixture_report)
            await confirm_active(writer, managed, await update_message(reader))
            managed.failed("session_unavailable_or_stale")
            offset[0] += 31
            await publish_down(server, managed, writer, fixture_report)
            await asyncio.wait_for(wait_for_status(managed, "STARTUP_SESSION_CONSUMED"), 4)
            await asyncio.sleep(1.1)
            assert managed.snapshot()["startup_recovery"]["total_attempts"] == 1
            assert managed.snapshot()["attempts"] == 2
            assert managed.snapshot()["pending"] is None
            assert len(updates_sent(server)) == 1
        finally:
            await cleanup(server, writer, heartbeat)

    asyncio.run(run())


def test_keepalives_allow_old_live_report_to_wait_for_late_ted_without_new_pcrpt(tmp_path, monkeypatch):
    server, managed, computer, fixture_report, offset = startup_server_lab(tmp_path, monkeypatch)
    topology = computer.topology_path.read_bytes()
    computer.topology_path.unlink()

    async def run():
        await server.start()
        reader, writer = await connect(server)
        heartbeat = asyncio.create_task(keep_peer_alive(writer))
        try:
            report = await publish_down(server, managed, writer, fixture_report)
            await asyncio.sleep(1.2)
            assert not updates_sent(server)
            offset[0] += computer.max_age_seconds + 60
            assert managed.clock() - report["received_at"] > computer.max_age_seconds
            computer.topology_path.write_bytes(topology)
            update = await update_message(reader)
            assert latest_report(server, managed)["received_at"] == report["received_at"]
            # The peer remains live through keepalives; no synthetic PCRpt was
            # injected to refresh historical state into new authority.
            assert server.snapshot()["peers"][PEER]["state"] == "ESTABLISHED"
            assert managed.snapshot()["startup_recovery"]["total_attempts"] == 1
            await confirm_active(writer, managed, update)
        finally:
            await cleanup(server, writer, heartbeat)

    asyncio.run(run())


@pytest.mark.parametrize("change", ["delegate", "report-bandwidth", "operation", "hold-owner", "preview"])
def test_startup_revalidates_authority_after_waiting_for_writer_lock(tmp_path, monkeypatch, change):
    server, managed, _, fixture_report, _ = startup_server_lab(tmp_path, monkeypatch)
    ledger_path = managed.operation_journal.parent / ".ninja-pce-reservations.json"

    async def run():
        await server.start()
        _, writer = await connect(server)
        heartbeat = asyncio.create_task(keep_peer_alive(writer))
        connection = server._connections[PEER]
        held = False
        try:
            report = await publish_down(server, managed, writer, fixture_report)
            await connection.send_lock.acquire()
            held = True
            await asyncio.wait_for(wait_for_status(managed, "STARTUP_READY"), 4)
            if change in {"delegate", "report-bandwidth"}:
                changed = copy.deepcopy(fixture_report)
                if change == "report-bandwidth":
                    next(a for a in changed["attributes"] if (a["class"], a["type"]) == (5, 1))["body_hex"] = struct.pack("!f", 125).hex()
                writer.write(encode_message(PCRPT, managed_body(changed, code=0, empty=True,
                                                               delegate=change != "delegate")))
                await writer.drain()
                await _eventually(lambda: latest_report(server, managed)["received_at"] > report["received_at"])
            elif change == "operation":
                journal = load_state(managed.operation_journal)
                journal["operations"][managed.operation_id]["status"] = "RECOVERY_REQUIRED"
                write_state(managed.operation_journal, journal)
            elif change == "hold-owner":
                ledger = load_state(ledger_path)
                ledger["entries"]["PE1:iosxe:Tunnel1000"]["owner"] = "another-owner"
                write_state(ledger_path, ledger)
            else:
                monkeypatch.setenv("NINJA_PCE_PREVIEW_ONLY", "1")
            ledger_after_change = ledger_path.read_bytes()
            connection.send_lock.release()
            held = False
            await asyncio.sleep(.3)
            assert not connection.state["updates"]
            assert not updates_sent(server)
            assert managed.snapshot()["pending"] is None
            assert managed.snapshot()["attempts"] == 2
            assert managed.snapshot()["startup_recovery"]["total_attempts"] == 0
            assert ledger_path.read_bytes() == ledger_after_change
        finally:
            if held:
                connection.send_lock.release()
            await cleanup(server, writer, heartbeat)

    asyncio.run(run())


def test_preview_mode_has_no_intent_or_reservation_side_effect(tmp_path, monkeypatch):
    server, managed, _, fixture_report, _ = startup_server_lab(tmp_path, monkeypatch)
    monkeypatch.setenv("NINJA_PCE_PREVIEW_ONLY", "1")
    ledger = managed.operation_journal.parent / ".ninja-pce-reservations.json"
    before = ledger.read_bytes()

    async def run():
        await server.start()
        _, writer = await connect(server)
        heartbeat = asyncio.create_task(keep_peer_alive(writer))
        try:
            await publish_down(server, managed, writer, fixture_report)
            await asyncio.wait_for(wait_for_status(managed, "STARTUP_READY"), 4)
            assert not updates_sent(server)
            assert managed.snapshot()["pending"] is None
            assert managed.snapshot()["attempts"] == 2
            assert managed.snapshot()["startup_recovery"]["total_attempts"] == 0
            assert ledger.read_bytes() == before
        finally:
            await cleanup(server, writer, heartbeat)

    asyncio.run(run())


@pytest.mark.parametrize("persist_deferral", [True, False])
def test_topology_refresh_after_saved_startup_holds_defers_then_restores_automatically(
        tmp_path, monkeypatch, persist_deferral):
    server, managed, computer, fixture_report, _ = startup_server_lab(tmp_path, monkeypatch)
    withdraw_managed(computer, monkeypatch)
    ledger_path = managed.operation_journal.parent / ".ninja-pce-reservations.json"
    initial_hold = copy.deepcopy(load_state(ledger_path)["entries"]["PE1:iosxe:Tunnel1000"])
    operation_before = managed.operation_journal.read_bytes()
    save_ledger = ReservationLedger.save
    refreshed = []
    persistence_failures = []
    save_recovery = managed._save

    def save_recovery_or_fail():
        if (not persist_deferral and not persistence_failures and
                managed._data.get("reason") == "startup_topology_changed_before_write"):
            persistence_failures.append(True)
            raise OSError("simulated deferral journal persistence failure")
        save_recovery()

    monkeypatch.setattr(managed, "_save", save_recovery_or_fail)

    def save_then_refresh(ledger):
        save_ledger(ledger)
        if ledger.path == ledger_path and not refreshed:
            # Publish a newer valid revision after the candidate's resource
            # union is durable but before its network intent can be recorded.
            stamp = datetime.fromisoformat(json.loads(computer.raw_path.read_text())["captured_at"]).timestamp() + 1
            _publish(computer, [stamp])
            monkeypatch.setattr(computation, "_utcnow", lambda: datetime.fromtimestamp(stamp, timezone.utc))
            refreshed.append(True)

    monkeypatch.setattr(ReservationLedger, "save", save_then_refresh)

    async def run():
        await server.start()
        reader, writer = await connect(server)
        heartbeat = asyncio.create_task(keep_peer_alive(writer))
        connection = server._connections[PEER]
        initial_srp = connection.srp_sequence
        try:
            await publish_down(server, managed, writer, fixture_report)
            if persist_deferral:
                await asyncio.wait_for(wait_for_status(managed, "STARTUP_TOPOLOGY_UNAVAILABLE"), 4)
            else:
                async def monitor_stopped():
                    while server._capture_error is None:
                        await asyncio.sleep(.01)
                await asyncio.wait_for(monitor_stopped(), 4)
            assert refreshed == [True]
            snapshot = managed.snapshot()
            if persist_deferral:
                assert not snapshot["blocked"] and server._capture_error is None
            assert snapshot["baseline"] is snapshot["pending"] is None
            assert snapshot["attempts"] == 2
            assert snapshot["startup_recovery"]["total_attempts"] == 0
            assert connection.srp_sequence == initial_srp
            assert connection.state["updates"] == {} and not updates_sent(server)
            retained = load_state(ledger_path)["entries"]["PE1:iosxe:Tunnel1000"]
            assert set(retained["resources"]) > set(initial_hold["resources"])
            assert retained["current_edges"] == initial_hold["current_edges"]
            assert retained["current_bandwidth_bps"] == initial_hold["current_bandwidth_bps"]
            durable = load_state(managed.path)
            assert durable["managed_authority"]["resources"] == retained["resources"]
            assert durable["pending"] is None
            if persist_deferral:
                assert not durable["blocked"]
            assert durable["startup_recovery"]["total_attempts"] == 0
            assert managed.validate_authority()
            assert managed.operation_journal.read_bytes() == operation_before
            if not persist_deferral:
                # A persistence error is terminal to the current monitor, not
                # a graph-refresh deferral eligible for another network try.
                assert persistence_failures == [True]
                assert server._capture_error is not None
                await asyncio.sleep(2.2)
                assert managed.snapshot()["pending"] is None
                assert managed.snapshot()["startup_recovery"]["total_attempts"] == 0
                assert connection.srp_sequence == initial_srp
                assert connection.state["updates"] == {} and not updates_sent(server)
                assert managed.operation_journal.read_bytes() == operation_before
                return

            # No replacement PCRpt or operator request is needed: the watcher
            # recomputes the same live delegation against the fresh revision.
            update = await update_message(reader)
            assert connection.srp_sequence == initial_srp + 1
            assert managed.snapshot()["startup_recovery"]["total_attempts"] == 1
            writer.write(encode_message(PCRPT, progress_body(update, 0)))
            await writer.drain()
            await _eventually(lambda: managed.snapshot()["pending"] is not None and
                             managed.snapshot()["pending"]["ack_seen"])
            assert managed.snapshot()["baseline"] is None
            await confirm_active(writer, managed, update)
            assert len(updates_sent(server)) == 1
            assert server._capture_error is None
            assert managed.operation_journal.read_bytes() == operation_before
        finally:
            await cleanup(server, writer, heartbeat)

    asyncio.run(run())
