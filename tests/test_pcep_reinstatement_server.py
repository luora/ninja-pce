"""Exact, operator-approved reinstatement through the real PCEP writer fence."""
from __future__ import annotations

import asyncio
import copy
import json
import struct

import pytest
from test_pcep import _eventually
from test_pcep_delegation import connect
from test_pcep_failover_server import keep_peer_alive, progress_body, update_message, wait_for_status
from test_pcep_managed_recovery_server import PEER, cleanup, managed_body, server_lab

from ninja_pce.pcep import PCRPT, encode_message, parse_reports
from ninja_pce.state import load_state, write_state


def latest_report(server, managed):
    reports = server.snapshot()["peers"][PEER]["reports"].values()
    return max((report for report in reports if managed.matches(PEER, report)),
               key=lambda report: report["received_at"])


def updates_sent(server):
    return [event for event in server.snapshot()["events"]
            if event["direction"] == "tx" and event["message_type"] == 11]


async def publish_down(server, managed, writer, fixture_report):
    writer.write(encode_message(PCRPT, managed_body(fixture_report, code=0, empty=True)))
    await writer.drain()
    await _eventually(lambda: bool(server.snapshot()["peers"][PEER]["reports"]))
    report = latest_report(server, managed)
    assert report["operational_code"] == 0 and report["ero"] == []
    assert report["session_id"] != fixture_report["session_id"]
    assert report["stale"] is False and report["sync"] is False
    return report


def test_exact_reinstatement_sends_once_and_finishes_only_after_correlated_active_report(tmp_path, monkeypatch):
    server, managed, _, _, fixture_report = server_lab(tmp_path, monkeypatch, legacy=False)
    managed.reinstatement_path = tmp_path / "reinstatement.json"
    operation_before = managed.operation_journal.read_bytes()

    async def run():
        await server.start()
        reader, writer = await connect(server)
        heartbeat = asyncio.create_task(keep_peer_alive(writer))
        try:
            report = await publish_down(server, managed, writer, fixture_report)
            assert managed.snapshot()["baseline"] is None
            assert not updates_sent(server)
            # The maintenance CLI consumes the checksummed capture, whose JSON
            # serializer sorts dictionary keys, unlike the live wire parser.
            captured = json.loads(json.dumps(report, sort_keys=True))
            plan = managed.reinstatement_plan(PEER, captured, ttl_seconds=60)
            write_state(managed.reinstatement_path, plan)
            update = await asyncio.wait_for(update_message(reader), 5)
            sent = parse_reports(update[4:], allow_delegation=True)[0]
            pending = managed.snapshot()["pending"]
            assert sent["identifiers"]["tunnel_id"] == managed.tunnel_id
            assert sent["plsp_id"] == report["plsp_id"]
            assert pending is not None and managed.snapshot()["attempts"] == 1
            assert pending["ero"] == [hop["address"] for hop in sent["ero"]]
            assert pending["outcome"]["status"] == "REINSTATEMENT_PATH"
            assert pending["edges"] == pending["outcome"]["path_link_ids"]
            assert len(updates_sent(server)) == 1
            # Processing acknowledgement alone does not establish an RSVP LSP.
            writer.write(encode_message(PCRPT, progress_body(update, 0)))
            await writer.drain()
            await _eventually(lambda: server.snapshot()["peers"][PEER]["updates"][
                str(sent["srp_id"])]["acknowledged"])
            assert managed.snapshot()["pending"] is not None
            writer.write(encode_message(PCRPT, progress_body(update, 2)))
            await writer.drain()
            await _eventually(lambda: managed.snapshot()["status"] == "RECOVERED")
            completed = managed.snapshot()
            assert completed["pending"] is None and completed["attempts"] == 1
            assert completed["baseline"]["ero"] == pending["ero"]
            assert managed.operation_journal.read_bytes() == operation_before
            # Leaving the exact request on disk cannot replay an acknowledged send.
            await asyncio.sleep(1.2)
            assert managed.snapshot()["attempts"] == 1
            assert len(updates_sent(server)) == 1
            assert len(server.snapshot()["peers"][PEER]["updates"]) == 1
            assert server.delegated_computer.allowed_tunnels == {}
        finally:
            await cleanup(server, writer, heartbeat)

    asyncio.run(run())


def test_preview_only_refuses_reinstatement_without_consuming_budget_or_holds(tmp_path, monkeypatch):
    server, managed, _, _, fixture_report = server_lab(tmp_path, monkeypatch, legacy=False)
    managed.reinstatement_path = tmp_path / "reinstatement.json"
    ledger_path = managed.operation_journal.parent / ".ninja-pce-reservations.json"
    ledger_before = ledger_path.read_bytes()
    operation_before = managed.operation_journal.read_bytes()
    monkeypatch.setenv("NINJA_PCE_PREVIEW_ONLY", "1")

    async def run():
        await server.start()
        _, writer = await connect(server)
        heartbeat = asyncio.create_task(keep_peer_alive(writer))
        try:
            report = await publish_down(server, managed, writer, fixture_report)
            write_state(managed.reinstatement_path,
                        managed.reinstatement_plan(PEER, report, ttl_seconds=60))
            await asyncio.sleep(1.3)
            assert managed.snapshot()["attempts"] == 0
            assert managed.snapshot()["pending"] is None
            assert not server.snapshot()["peers"][PEER]["updates"]
            assert not updates_sent(server)
            assert ledger_path.read_bytes() == ledger_before
            assert managed.operation_journal.read_bytes() == operation_before
        finally:
            await cleanup(server, writer, heartbeat)

    asyncio.run(run())


@pytest.mark.parametrize("change", ["request-removed", "report-bandwidth", "operation-journal"])
def test_reinstatement_revalidates_authority_after_waiting_for_writer_lock(tmp_path, monkeypatch, change):
    server, managed, _, _, fixture_report = server_lab(tmp_path, monkeypatch, legacy=False)
    managed.reinstatement_path = tmp_path / "reinstatement.json"
    ledger_path = managed.operation_journal.parent / ".ninja-pce-reservations.json"
    ledger_before = ledger_path.read_bytes()

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
            write_state(managed.reinstatement_path,
                        managed.reinstatement_plan(PEER, report, ttl_seconds=60))
            await asyncio.wait_for(wait_for_status(managed, "READY"), 4)
            if change == "request-removed":
                managed.reinstatement_path.unlink()
            elif change == "report-bandwidth":
                changed = copy.deepcopy(fixture_report)
                for item in changed["attributes"]:
                    if (item["class"], item["type"]) == (5, 1):
                        item["body_hex"] = struct.pack("!f", 250000).hex()
                writer.write(encode_message(PCRPT, managed_body(changed, code=0, empty=True)))
                await writer.drain()
                await _eventually(lambda: latest_report(server, managed)["received_at"] > report["received_at"])
            else:
                journal = load_state(managed.operation_journal)
                journal["operations"][managed.operation_id]["status"] = "RECOVERY_REQUIRED"
                write_state(managed.operation_journal, journal)
            connection.send_lock.release()
            held = False
            await asyncio.sleep(.2)
            assert managed.snapshot()["attempts"] == 0
            assert managed.snapshot()["pending"] is None
            assert not connection.state["updates"]
            assert not updates_sent(server)
            assert ledger_path.read_bytes() == ledger_before
        finally:
            if held:
                connection.send_lock.release()
            await cleanup(server, writer, heartbeat)

    asyncio.run(run())
