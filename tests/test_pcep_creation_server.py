"""Real loopback/Unix transports and synthetic PCC; no router qualification."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import stat
import struct
from contextlib import asynccontextmanager
from ipaddress import IPv4Address
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from test_pcep_compute import prepare

import ninja_pce.pcep_creation as creation_module
from ninja_pce.pcep import (
    ERROR,
    KEEPALIVE,
    PCINITIATE,
    PCRPT,
    PcepServer,
    PcepServerConfig,
    encode_message,
    encode_object,
    encode_open,
    parse_objects,
    parse_open,
)
from ninja_pce.pcep_creation import CreationSession
from ninja_pce.reservations import ReservationLedger
from ninja_pce.state import load_state
from ninja_pce.tunnel_creation import CreationManager

PEER = "127.0.0.1"
PRINCIPAL = f"local-uid:{os.getuid()}"
INTENT = {"headend": "PE1", "destination": "PE3", "bandwidth_bps": 1_000_000}


def _lab(tmp_path: Path, monkeypatch, **configuration):
    monkeypatch.delenv("NINJA_PCE_PREVIEW_ONLY", raising=False)
    computer = prepare(tmp_path, monkeypatch)
    computer.headends = {PEER: "10.255.0.1"}
    manager = CreationManager(computer, tmp_path / "state", {"PE1": PEER}, ("PE3",))
    creation = CreationSession(manager, tmp_path / "private" / "creation.sock")
    config = PcepServerConfig((PEER,), port=0, handshake_timeout=1, **configuration)
    server = PcepServer(config, path_computer=computer, creation=creation)
    return server, creation, manager, computer


@asynccontextmanager
async def _running(lab):
    server = lab[0]
    await server.start()
    try:
        yield lab
    finally:
        await server.close()


async def _read(reader: asyncio.StreamReader, timeout: float = 1) -> bytes:
    header = await asyncio.wait_for(reader.readexactly(4), timeout)
    size = int.from_bytes(header[2:4], "big")
    return header + await asyncio.wait_for(reader.readexactly(size - 4), timeout)


async def _eventually(predicate, seconds: float = 1) -> None:
    deadline = asyncio.get_running_loop().time() + seconds
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("Expected synthetic PCC/controller state was not observed")


async def _connect(server: PcepServer, *, synchronize: bool = True, initiation: bool = True):
    reader, writer = await asyncio.open_connection(PEER, server.addresses[0][1])
    opened = parse_open((await _read(reader))[4:])
    assert opened["stateful_flags"] == 5
    assert {"type": 34, "value_hex": "0000000100"} in opened["tlvs"]
    writer.write(encode_open(updates=True, initiation=initiation) + encode_message(KEEPALIVE))
    await writer.drain()
    assert await _read(reader) == encode_message(KEEPALIVE)
    await _eventually(lambda: server.snapshot()["peers"][PEER]["state"] == "ESTABLISHED")
    if synchronize:
        eos = encode_object(32, bytes(4)) + encode_object(7, b"")
        writer.write(encode_message(PCRPT, eos))
        await writer.drain()
        await _eventually(lambda: server.snapshot()["peers"][PEER]["synchronized"])
    return reader, writer


def _initiates(server: PcepServer) -> list[dict[str, Any]]:
    return [event for event in server.snapshot()["events"]
            if event["direction"] == "tx" and event["message_type"] == PCINITIATE]


def _held(manager: CreationManager, op: dict[str, Any]) -> dict[str, Any]:
    entry = ReservationLedger(manager.state_dir).entries[op["reservation_id"]]
    assert entry["resources"] == {edge: 1_000_000 for edge in op["path_edges"]}
    return entry


def _tlv(kind: int, value: bytes) -> bytes:
    return struct.pack("!HH", kind, len(value)) + value + bytes(-len(value) % 4)


def _report(op, *, code=2, created=True, delegate=True, plsp=17, srp=None, removed=False,
            source=None, destination=None, name=None, route=None, bandwidth=None, priority=7,
            extra_lsp=b"", extra_attributes=b"") -> bytes:
    source = source or op["source_ip"]
    destination = destination or op["destination_ip"]
    identifiers = (IPv4Address(source).packed + struct.pack("!HH", 9, 1001) +
                   IPv4Address(source).packed + IPv4Address(destination).packed)
    bits = (plsp << 12) | (128 if created else 0) | int(delegate) | 8 | (code << 4) | (4 if removed else 0)
    lsp = encode_object(32, struct.pack("!I", bits) + _tlv(17, (name or op["symbolic_name"]).encode()) +
                        _tlv(18, identifiers) + extra_lsp)
    addresses = ([] if code == 0 else op["ero"]) if route is None else route
    ero = encode_object(7, b"".join(bytes((1, 8)) + IPv4Address(ip).packed + bytes((32, 0))
                                  for ip in addresses))
    attrs = (encode_object(9, bytes(12) + bytes((priority, priority, 0, 0))) +
             encode_object(5, bytes.fromhex(bandwidth or op["bandwidth_wire"])) + extra_attributes)
    request_id = op["send"]["srp_id"] if srp is None else srp
    return encode_message(PCRPT, encode_object(33, struct.pack("!II", int(removed), request_id)) + lsp + ero + attrs)


async def _control(path: Path, request: dict[str, Any] | bytes):
    reader, writer = await asyncio.open_unix_connection(path)
    raw = request if isinstance(request, bytes) else json.dumps(request).encode() + b"\n"
    writer.write(raw)
    await writer.drain()
    response = json.loads(await asyncio.wait_for(reader.readline(), 2))
    writer.close()
    await writer.wait_closed()
    return response


def test_create_persists_exact_send_boundary_and_hold_before_transport_and_deduplicates(
    tmp_path, monkeypatch,
):
    async def scenario():
        async with _running(_lab(tmp_path, monkeypatch)) as (server, creation, manager, _):
            reader, writer = await _connect(server)
            connection = server._connections[PEER]
            original_write = connection.writer.write
            observed: list[bytes] = []

            def checked_write(raw):
                if raw[1] == PCINITIATE:
                    durable = next(iter(load_state(manager.path)["operations"].values()))
                    assert durable["status"] == "PUSHING" and durable["send"]["srp_id"] == 1
                    assert durable["send"]["wire_digest"] == hashlib.sha256(raw).hexdigest()
                    assert durable["send"]["session_id"] == connection.state["session_id"]
                    assert _held(manager, durable)["status"] == "PUSHING"
                    observed.append(raw)
                original_write(raw)

            monkeypatch.setattr(connection.writer, "write", checked_write)
            first, duplicate = await asyncio.gather(
                creation.create(PRINCIPAL, "same-key", INTENT),
                creation.create(PRINCIPAL, "same-key", INTENT),
            )
            raw = await _read(reader)
            assert first["id"] == duplicate["id"] and first["status"] == duplicate["status"] == "PUSHING"
            assert observed == [raw] and len(_initiates(server)) == 1
            objects = parse_objects(raw[4:])
            assert [obj.object_class for obj in objects] == [33, 32, 4, 7, 9, 5]
            assert objects[3].body == b"".join(bytes((1, 8)) + IPv4Address(ip).packed + b"\x20\x00"
                                             for ip in first["ero"])
            with pytest.raises(ValueError, match="conflicts"):
                await creation.create(PRINCIPAL, "same-key", {**INTENT, "bandwidth_bps": 500_000})
            assert len(_initiates(server)) == 1
            writer.close()
            await writer.wait_closed()
    asyncio.run(scenario())


def test_down_then_active_report_remains_unverified_and_owned_cleanup_retains_hold(tmp_path, monkeypatch):
    async def scenario():
        async with _running(_lab(tmp_path, monkeypatch)) as (server, creation, manager, _):
            reader, writer = await _connect(server)
            op = await creation.create(PRINCIPAL, "progress", INTENT)
            assert (await _read(reader))[1] == PCINITIATE
            writer.write(_report(op, code=0))
            await writer.drain()
            await _eventually(lambda: manager.get(op["id"])["status"] == "APPLIED_UNVERIFIED")
            down = manager.get(op["id"])
            assert down["reported"]["operational_state"] == "DOWN" and down["verified"] is False
            writer.write(_report(op))
            await writer.drain()
            await _eventually(lambda: manager.get(op["id"])["reported"]["operational_state"] == "ACTIVE")
            active = manager.get(op["id"])
            assert active["status"] == "APPLIED_UNVERIFIED" and active["verified"] is False
            assert _held(manager, active)["status"] == "APPLIED_UNVERIFIED"

            cleanup = await creation.remove_owned(PRINCIPAL, op["id"])
            deletion = parse_objects((await _read(reader))[4:])
            assert [obj.object_class for obj in deletion] == [33, 32]
            assert int.from_bytes(deletion[0].body[:4], "big") == 1
            assert int.from_bytes(deletion[1].body[:4], "big") >> 12 == 17
            with pytest.raises(ValueError):
                await creation.remove_owned(PRINCIPAL, op["id"])
            assert len(_initiates(server)) == 2
            creation.peer_error(PEER, op["send"]["session_id"], cleanup["cleanup"]["srp_id"],
                                [{"type": 24, "value": 3}])
            assert "owned cleanup" in manager.get(op["id"])["reason"]
            writer.write(_report(op, removed=True, srp=cleanup["cleanup"]["srp_id"]))
            await writer.drain()
            await _eventually(lambda: "report" in manager.get(op["id"])["cleanup"])
            removed = manager.get(op["id"])
            assert removed["status"] == "ROLLBACK_REQUIRED" and removed["verified"] is False
            assert _held(manager, removed)["status"] == "ROLLBACK_REQUIRED"
            with pytest.raises(ValueError, match="independent"):
                manager.confirmed_removal(op["id"], removed["cleanup"]["report"])
            assert len(ReservationLedger(manager.state_dir).entries) == 1
            writer.close()
            await writer.wait_closed()
    asyncio.run(scenario())


@pytest.mark.parametrize("change", ["created", "delegate", "srp", "source", "endpoint", "bandwidth",
                                    "priority", "route", "extension", "attribute"])
def test_spoofed_or_mismatched_creation_report_cannot_complete_operation(tmp_path, monkeypatch, change):
    async def scenario():
        async with _running(_lab(tmp_path, monkeypatch)) as (server, creation, manager, _):
            reader, writer = await _connect(server)
            op = await creation.create(PRINCIPAL, "bad-report", INTENT)
            await _read(reader)
            changes = {
                "created": {"created": False}, "delegate": {"delegate": False}, "srp": {"srp": 999},
                "source": {"source": "10.255.0.2"}, "endpoint": {"destination": "10.255.0.5"},
                "bandwidth": {"bandwidth": struct.pack("!f", 250_000).hex()}, "priority": {"priority": 6},
                "route": {"route": ["10.0.12.2"]}, "extension": {"extra_lsp": _tlv(99, bytes(4))},
                "attribute": {"extra_attributes": encode_object(99, bytes(4))},
            }
            writer.write(_report(op, **changes[change]))
            await writer.drain()
            await _eventually(lambda: manager.get(op["id"])["status"] == "ROLLBACK_REQUIRED")
            failed = manager.get(op["id"])
            assert failed["reported"] is None and failed["verified"] is False
            assert _held(manager, failed)["status"] == "ROLLBACK_REQUIRED"
            assert len(_initiates(server)) == 1
            writer.close()
            await writer.wait_closed()
    asyncio.run(scenario())


def test_changed_allocated_identity_after_down_ack_never_becomes_active_or_cleanup_authority(tmp_path, monkeypatch):
    async def scenario():
        async with _running(_lab(tmp_path, monkeypatch)) as (server, creation, manager, _):
            reader, writer = await _connect(server)
            op = await creation.create(PRINCIPAL, "identity", INTENT)
            await _read(reader)
            writer.write(_report(op, code=0))
            await writer.drain()
            await _eventually(lambda: manager.get(op["id"])["status"] == "APPLIED_UNVERIFIED")
            writer.write(_report(op, plsp=18))
            await writer.drain()
            await _eventually(lambda: manager.get(op["id"])["status"] == "ROLLBACK_REQUIRED")
            assert manager.get(op["id"])["reported"]["identity"]["plsp_id"] == 17
            with pytest.raises(ValueError):
                await creation.remove_owned(PRINCIPAL, op["id"])
            assert len(_initiates(server)) == 1
            _held(manager, op)
            writer.close()
            await writer.wait_closed()
    asyncio.run(scenario())


@pytest.mark.parametrize("gate", ["kill", "synchronization", "stale_session", "capture", "remote_i",
                                  "terminal", "pending_update", "pending_computation", "pending_failure", "srp"])
def test_failed_execution_gate_sends_no_initiate_and_releases_only_unattempted_hold(
    tmp_path, monkeypatch, gate,
):
    async def scenario():
        async with _running(_lab(tmp_path, monkeypatch)) as (server, creation, manager, _):
            _, writer = await _connect(server, synchronize=gate != "synchronization", initiation=gate != "remote_i")
            connection = server._connections[PEER]
            if gate == "kill":
                monkeypatch.setenv("NINJA_PCE_PREVIEW_ONLY", "1")
            elif gate == "stale_session":
                connection.last_rx -= 121
            elif gate == "capture":
                server._capture_error = "synthetic unavailable capture"
            elif gate == "terminal":
                connection.terminal = True
            elif gate == "pending_update":
                connection.state["updates"]["1"] = {"acknowledged": False}
            elif gate == "pending_computation":
                connection.computation = asyncio.create_task(asyncio.Event().wait())
            elif gate == "pending_failure":
                server.failover = SimpleNamespace(snapshot=lambda: {"pending": {"srp_id": 99}}, peer="other")
            elif gate == "srp":
                connection.srp_sequence = 0xFFFFFFFE
            op = await creation.create(PRINCIPAL, "gated", INTENT)
            assert op["status"] == "REJECTED" and op["send"] is None
            assert _initiates(server) == [] and ReservationLedger(manager.state_dir).entries == {}
            writer.close()
            await writer.wait_closed()
    asyncio.run(scenario())


@pytest.mark.parametrize("gate", ["kill", "topology"])
def test_waiting_writer_rechecks_kill_switch_and_fresh_ted_at_actual_write_boundary(tmp_path, monkeypatch, gate):
    async def scenario():
        async with _running(_lab(tmp_path, monkeypatch)) as (server, creation, manager, computer):
            _, writer = await _connect(server)
            lock = server._connections[PEER].send_lock
            await lock.acquire()
            task = asyncio.create_task(creation.create(PRINCIPAL, "boundary", INTENT))
            await _eventually(lambda: len(manager.list()) == 1)
            if gate == "kill":
                monkeypatch.setenv("NINJA_PCE_PREVIEW_ONLY", "1")
            else:
                computer.status_path.unlink()
            lock.release()
            op = await task
            assert op["status"] == "REJECTED" and op["send"] is None
            assert _initiates(server) == [] and ReservationLedger(manager.state_dir).entries == {}
            writer.close()
            await writer.wait_closed()
    asyncio.run(scenario())


def test_no_fresh_ted_fails_before_admission_or_any_initiate(tmp_path, monkeypatch):
    async def scenario():
        async with _running(_lab(tmp_path, monkeypatch)) as (server, creation, manager, computer):
            _, writer = await _connect(server)
            computer.raw_path.unlink()
            with pytest.raises((ValueError, OSError)):
                await creation.create(PRINCIPAL, "no-ted", INTENT)
            assert manager.list() == [] and _initiates(server) == []
            writer.close()
            await writer.wait_closed()
    asyncio.run(scenario())


@pytest.mark.parametrize("loss", ["timeout", "disconnect"])
def test_unknown_delivery_outcome_retains_hold_without_retry(tmp_path, monkeypatch, loss):
    async def scenario():
        async with _running(_lab(tmp_path, monkeypatch, update_ack_timeout=0.01)) as (
            server, creation, manager, _,
        ):
            reader, writer = await _connect(server)
            op = await creation.create(PRINCIPAL, "unknown", INTENT)
            await _read(reader)
            if loss == "disconnect":
                writer.close()
                await writer.wait_closed()
            await _eventually(lambda: manager.get(op["id"])["status"] == "ROLLBACK_REQUIRED", seconds=2)
            uncertain = manager.get(op["id"])
            assert _held(manager, uncertain)["status"] == "ROLLBACK_REQUIRED"
            replay = await creation.create(PRINCIPAL, "unknown", INTENT)
            assert replay["id"] == op["id"] and replay["status"] == "ROLLBACK_REQUIRED"
            assert len(_initiates(server)) == 1
            writer.close()
            await writer.wait_closed()
    asyncio.run(scenario())


def test_restart_marks_attempt_uncertain_and_never_replays_saved_creation(tmp_path, monkeypatch):
    async def scenario():
        lab = _lab(tmp_path, monkeypatch)
        server, creation, manager, computer = lab
        await server.start()
        reader, writer = await _connect(server)
        op = await creation.create(PRINCIPAL, "restart", INTENT)
        await _read(reader)
        # Model loss before disconnect persistence; leave the actual durable send.
        monkeypatch.setattr(creation, "disconnected", lambda peer: None)
        await server.close()
        writer.close()
        await writer.wait_closed()
        assert manager.get(op["id"])["status"] == "PUSHING"
        restarted = CreationSession(manager, creation.path)
        second = PcepServer(server.config, path_computer=computer, creation=restarted)
        await second.start()
        try:
            assert manager.get(op["id"])["status"] == "ROLLBACK_REQUIRED"
            assert _held(manager, op)["status"] == "ROLLBACK_REQUIRED"
            replay = await restarted.create(PRINCIPAL, "restart", INTENT)
            assert replay["id"] == op["id"] and replay["send"] == op["send"]
            assert _initiates(second) == []
        finally:
            await second.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("raw", [b"[]\n", b"{\n", b'{"action":"list","action":"create"}\n',
                                 b'{"action":"create","key":"a","intent":null}\n',
                                 b'{"action":"list","extra":1}\n', b"x" * 4100 + b"\n"])
def test_private_socket_rejects_malformed_bounded_requests_without_network_write(tmp_path, monkeypatch, raw):
    async def scenario():
        async with _running(_lab(tmp_path, monkeypatch)) as (server, creation, manager, _):
            assert stat.S_IMODE(creation.path.stat().st_mode) == 0o600
            assert stat.S_IMODE(creation.path.parent.stat().st_mode) == 0o700
            response = await _control(creation.path, raw)
            assert response["ok"] is False
            assert manager.list() == [] and _initiates(server) == []
    asyncio.run(scenario())


def test_private_socket_checks_peer_uid_and_does_not_trust_request_identity(tmp_path, monkeypatch):
    async def scenario():
        async with _running(_lab(tmp_path, monkeypatch)) as (server, creation, manager, _):
            monkeypatch.setattr(creation_module.os, "getuid", lambda: int(PRINCIPAL.split(":")[1]) + 1)
            response = await _control(creation.path, {"action": "list"})
            assert response["ok"] is False and manager.list() == [] and _initiates(server) == []
    asyncio.run(scenario())


def test_insecure_private_directory_fails_start_and_closes_bound_tcp_listener(tmp_path, monkeypatch):
    async def scenario():
        server, creation, _, _ = _lab(tmp_path, monkeypatch)
        creation.path.parent.mkdir(mode=0o755)
        with pytest.raises(ValueError, match="owner-only"):
            await server.start()
        assert server._server is None and creation.listener is None and not creation.path.exists()
        await server.close()
    asyncio.run(scenario())


def test_creation_pce_error_requires_current_session_and_matching_creation_or_cleanup_srp(tmp_path, monkeypatch):
    async def scenario():
        async with _running(_lab(tmp_path, monkeypatch)) as (server, creation, manager, _):
            reader, writer = await _connect(server)
            op = await creation.create(PRINCIPAL, "error", INTENT)
            await _read(reader)
            creation.peer_error(PEER, "previous-session", op["send"]["srp_id"], [{"type": 24, "value": 1}])
            assert manager.get(op["id"])["status"] == "PUSHING"
            error = encode_object(33, struct.pack("!II", 0, op["send"]["srp_id"])) + (
                encode_object(13, bytes((0, 0, 24, 1)))
            )
            writer.write(encode_message(ERROR, error))
            await writer.drain()
            await _eventually(lambda: manager.get(op["id"])["status"] == "ROLLBACK_REQUIRED")
            assert _held(manager, op)["status"] == "ROLLBACK_REQUIRED" and len(_initiates(server)) == 1
            writer.close()
            await writer.wait_closed()
    asyncio.run(scenario())


def test_private_owner_console_creates_and_returns_only_its_operation_idempotently(tmp_path, monkeypatch):
    async def scenario():
        async with _running(_lab(tmp_path, monkeypatch)) as (server, creation, manager, _):
            reader, writer = await _connect(server)
            response = await _control(creation.path, {"action": "create", "key": "private", "intent": INTENT})
            assert response["ok"] is True
            op = response["result"]
            assert op["principal"] == PRINCIPAL and op["status"] == "PUSHING"
            assert (await _read(reader))[1] == PCINITIATE
            replay = await _control(creation.path, {"action": "create", "key": "private", "intent": INTENT})
            assert replay["result"]["id"] == op["id"] and len(_initiates(server)) == 1
            fetched = await _control(creation.path, {"action": "get", "id": op["id"]})
            listed = await _control(creation.path, {"action": "list"})
            assert fetched["result"]["id"] == listed["result"][0]["id"] == op["id"]
            assert _held(manager, op)["status"] == "PUSHING"
            with pytest.raises(ValueError, match="Unknown"):
                manager.get(op["id"], "different-operator")
            writer.close()
            await writer.wait_closed()
    asyncio.run(scenario())


def test_cancelled_transport_drain_after_durable_boundary_retains_uncertain_hold(tmp_path, monkeypatch):
    async def scenario():
        async with _running(_lab(tmp_path, monkeypatch)) as (server, creation, manager, _):
            reader, writer = await _connect(server)
            connection = server._connections[PEER]
            original_drain = connection.writer.drain
            awaiting_drain = asyncio.Event()

            async def blocked_drain():
                awaiting_drain.set()
                await asyncio.Event().wait()

            monkeypatch.setattr(connection.writer, "drain", blocked_drain)
            task = asyncio.create_task(creation.create(PRINCIPAL, "cancelled", INTENT))
            await asyncio.wait_for(awaiting_drain.wait(), 1)
            raw = await _read(reader)
            assert raw[1] == PCINITIATE
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            monkeypatch.setattr(connection.writer, "drain", original_drain)
            op = manager.list()[0]
            assert op["status"] == "ROLLBACK_REQUIRED" and "cancelled" in op["reason"]
            assert _held(manager, op)["status"] == "ROLLBACK_REQUIRED"
            replay = await creation.create(PRINCIPAL, "cancelled", INTENT)
            assert replay["id"] == op["id"] and connection.srp_sequence == 1
            writer.close()
            await writer.wait_closed()
    asyncio.run(scenario())


def test_monitor_failure_and_hook_journal_error_fail_closed_without_skipping_socket_cleanup(tmp_path, monkeypatch):
    async def scenario():
        async with _running(_lab(tmp_path, monkeypatch)) as (server, creation, manager, _):
            _, writer = await _connect(server)

            def unavailable_journal():
                raise ValueError("synthetic malformed creation journal")

            monkeypatch.setattr(manager, "list", unavailable_journal)
            await _eventually(lambda: creation.monitor.done(), seconds=2)
            assert server._capture_error and "Creation state unavailable" in server._capture_error
            assert creation.owns(PEER, {}) is True  # Never route unknown ownership to another writer.
            creation.observe(PEER, [{"end_of_sync": False}])
            creation.peer_error(PEER, "session", 1, [])
            creation.disconnected(PEER)
            op = await creation.create(PRINCIPAL, "monitor-failed", INTENT)
            assert op["status"] == "REJECTED" and _initiates(server) == []
            writer.close()
            await writer.wait_closed()
            await _eventually(lambda: not server._connections)
    asyncio.run(scenario())
