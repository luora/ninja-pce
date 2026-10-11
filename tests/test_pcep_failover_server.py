"""Synthetic wire-level recovery with real historical topology encodings."""
from __future__ import annotations

import asyncio
import base64
import json
from datetime import datetime, timedelta
from ipaddress import IPv4Address
from pathlib import Path

import pytest
from test_pcep import _eventually, _read
from test_pcep_compute import EXPORTER, NAMES
from test_pcep_delegation import adapter, connect

from ninja_pce import pcep_compute
from ninja_pce.bgpls import parse_gobgp_snapshot, topology_document
from ninja_pce.pcep import (
    KEEPALIVE,
    PCRPT,
    PcepServer,
    PcepServerConfig,
    encode_message,
    encode_object,
    parse_objects,
)
from ninja_pce.pcep_failover import FailureRecoveryPlanner
from ninja_pce.state import load_state, write_state

PEER = "127.0.0.1"
ACTIVE = Path(__file__).parent / "fixtures/pcep-iosxe-16.9.7/tunnel100-computed-active.hex"


def active_body(*, delegate: bool = True) -> bytes:
    result = b""
    for obj in parse_objects(bytes.fromhex(ACTIVE.read_text())[4:]):
        body = obj.body
        if obj.object_class == 33:
            body = bytes(8)
        if obj.object_class == 32:
            body = ((int.from_bytes(body[:4], "big") & ~0x7F) | 0x28 | int(delegate)).to_bytes(4, "big") + body[4:]
        # Use the fixture's P3/P4 ERO as our explicitly synthetic baseline.
        if obj.object_class == 7:
            body = b"".join(bytes((1, 8)) + IPv4Address(address).packed + bytes((32, 0))
                            for address in ("10.0.13.3", "10.0.34.4", "10.0.46.6"))
        result += encode_object(obj.object_class, body, obj.object_type)
    return result


def withdraw(computer, monkeypatch) -> None:
    capture = json.loads(computer.raw_path.read_text())
    descriptor = bytes.fromhex("01040004") + IPv4Address("10.0.34.4").packed
    before = len(capture["rib"])
    capture["rib"] = [item for item in capture["rib"] if not any(
        descriptor in base64.b64decode(path["nlriBinary"])
        for path in item["destination"]["paths"])]
    assert len(capture["rib"]) == before - 1
    observed = datetime.fromisoformat(capture["captured_at"]) + timedelta(seconds=2)
    capture["captured_at"] = observed.isoformat()
    monkeypatch.setattr(pcep_compute, "_utcnow", lambda: observed)
    topology = parse_gobgp_snapshot(capture, required_peers=(EXPORTER,), node_names=NAMES,
                                   now=datetime.fromisoformat(capture["captured_at"]))
    computer.raw_path.write_text(json.dumps(capture))
    computer.topology_path.write_text(json.dumps(topology_document(topology)))
    write_state(computer.status_path, {"status": "ok", "observed_at":
                datetime.fromisoformat(capture["captured_at"]).timestamp(),
                "metadata": topology.raw_metadata, "nodes": len(topology.nodes),
                "directed_links": len(topology.links)})


def progress_body(update: bytes, operational_code: int) -> bytes:
    result, bandwidth = b"", b""
    for obj in parse_objects(update[4:]):
        body = obj.body
        if obj.object_class == 32:
            body = ((int.from_bytes(body[:4], "big") & ~0x70) | operational_code << 4).to_bytes(4, "big") + body[4:]
        if obj.object_class == 5:
            bandwidth = body
        result += encode_object(obj.object_class, body, obj.object_type)
    return result + encode_object(5, bandwidth, 5)


async def keep_peer_alive(writer):
    while True:
        await asyncio.sleep(.2)
        writer.write(encode_message(KEEPALIVE))
        await writer.drain()


async def wait_for_status(planner, status):
    while planner.snapshot()["status"] != status:
        await asyncio.sleep(.01)


async def update_message(reader):
    async def read_update():
        while True:
            message = await _read(reader)
            if message[1] == 11:
                return message
    return await asyncio.wait_for(read_update(), 6)


def test_topology_watcher_recovers_without_new_pcrpt_and_ignores_restore(tmp_path, monkeypatch):
    computer, delegated = adapter(tmp_path, monkeypatch, PEER)
    original = (computer.raw_path.read_bytes(), computer.status_path.read_bytes(), computer.topology_path.read_bytes())
    planner = FailureRecoveryPlanner(computer, peer=PEER, tunnel_id=100, journal_path=tmp_path / "recovery.json",
                                     debounce_seconds=.01, cooldown_seconds=.01)

    async def run():
        server = PcepServer(PcepServerConfig((PEER,), port=0, keepalive=1, deadtimer=5),
                            path_computer=computer, delegated_computer=delegated, failover=planner)
        await server.start()
        reader, writer = await connect(server)
        heartbeat = asyncio.create_task(keep_peer_alive(writer))
        try:
            writer.write(encode_message(PCRPT, active_body()))
            await writer.drain()
            await asyncio.wait_for(wait_for_status(planner, "MONITORING"), 4)
            assert not server.snapshot()["peers"][PEER]["updates"]
            withdraw(computer, monkeypatch)
            update = await update_message(reader)
            persisted = load_state(planner.path)
            assert persisted["pending"] and persisted["attempts"] == 1
            assert persisted["pending"]["ero"] != ["10.0.13.3", "10.0.34.4", "10.0.46.6"]
            # A DOWN report acknowledges processing but does not confirm recovery.
            writer.write(encode_message(PCRPT, progress_body(update, 0)))
            await writer.drain()
            await _eventually(lambda: server.snapshot()["peers"][PEER]["updates"]["1"]["acknowledged"])
            assert planner.snapshot()["pending"] is not None
            writer.write(encode_message(PCRPT, progress_body(update, 2)))
            await writer.drain()
            await _eventually(lambda: planner.snapshot()["status"] == "RECOVERED")
            assert planner.snapshot()["pending"] is None
            for path, raw in zip((computer.raw_path, computer.status_path, computer.topology_path), original):
                path.write_bytes(raw)
            await asyncio.sleep(1.2)
            assert planner.snapshot()["attempts"] == 1
            assert len(server.snapshot()["peers"][PEER]["updates"]) == 1
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
            await server.close()
            writer.close()
            await writer.wait_closed()

    asyncio.run(run())


def test_revoked_delegation_and_kill_switch_prevent_failure_write(tmp_path, monkeypatch):
    computer, delegated = adapter(tmp_path, monkeypatch, PEER)
    planner = FailureRecoveryPlanner(computer, peer=PEER, tunnel_id=100, journal_path=tmp_path / "recovery.json",
                                     debounce_seconds=.01, cooldown_seconds=.01)

    async def run():
        server = PcepServer(PcepServerConfig((PEER,), port=0, keepalive=1, deadtimer=5),
                            path_computer=computer, delegated_computer=delegated, failover=planner)
        await server.start()
        _, writer = await connect(server)
        heartbeat = asyncio.create_task(keep_peer_alive(writer))
        try:
            writer.write(encode_message(PCRPT, active_body()))
            await writer.drain()
            await asyncio.wait_for(wait_for_status(planner, "MONITORING"), 4)
            monkeypatch.setenv("NINJA_PCE_PREVIEW_ONLY", "1")
            withdraw(computer, monkeypatch)
            await asyncio.sleep(2.2)
            assert planner.snapshot()["attempts"] == 0
            assert not server.snapshot()["peers"][PEER]["updates"]
            writer.write(encode_message(PCRPT, active_body(delegate=False)))
            await writer.drain()
            await _eventually(lambda: planner.snapshot()["baseline"] is None)
            monkeypatch.delenv("NINJA_PCE_PREVIEW_ONLY")
            await asyncio.sleep(1.2)
            assert planner.snapshot()["attempts"] == 0
            assert not server.snapshot()["peers"][PEER]["updates"]
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
            await server.close()
            writer.close()
            await writer.wait_closed()

    asyncio.run(run())


@pytest.mark.parametrize("change", ["revocation", "kill_switch"])
def test_authority_is_rechecked_after_waiting_for_writer_lock(tmp_path, monkeypatch, change):
    computer, delegated = adapter(tmp_path, monkeypatch, PEER)
    planner = FailureRecoveryPlanner(computer, peer=PEER, tunnel_id=100, journal_path=tmp_path / "recovery.json",
                                     debounce_seconds=.01, cooldown_seconds=.01)

    async def run():
        server = PcepServer(PcepServerConfig((PEER,), port=0, keepalive=1, deadtimer=5),
                            path_computer=computer, delegated_computer=delegated, failover=planner)
        await server.start()
        _, writer = await connect(server)
        heartbeat = asyncio.create_task(keep_peer_alive(writer))
        held = False
        connection = server._connections[PEER]
        try:
            writer.write(encode_message(PCRPT, active_body()))
            await writer.drain()
            await asyncio.wait_for(wait_for_status(planner, "MONITORING"), 4)
            await connection.send_lock.acquire()
            held = True
            withdraw(computer, monkeypatch)
            await asyncio.wait_for(wait_for_status(planner, "READY"), 4)
            if change == "revocation":
                writer.write(encode_message(PCRPT, active_body(delegate=False)))
                await writer.drain()
                await _eventually(lambda: planner.snapshot()["baseline"] is None)
            else:
                monkeypatch.setenv("NINJA_PCE_PREVIEW_ONLY", "1")
            connection.send_lock.release()
            held = False
            await asyncio.sleep(.2)
            assert planner.snapshot()["attempts"] == 0
            assert planner.snapshot()["pending"] is None
            assert not connection.state["updates"]
            assert not any(event["direction"] == "tx" and event["message_type"] == 11
                           for event in server.snapshot()["events"])
        finally:
            if held:
                connection.send_lock.release()
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
            await server.close()
            writer.close()
            await writer.wait_closed()

    asyncio.run(run())
