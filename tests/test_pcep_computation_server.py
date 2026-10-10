"""Real loopback tests for bounded PCReq orchestration; no router connections."""
from __future__ import annotations

import asyncio
import ipaddress
import struct
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ninja_pce.pcep import (
    CLOSE,
    ERROR,
    KEEPALIVE,
    OPEN,
    PCREP,
    PCREQ,
    PcepProtocolError,
    PcepServer,
    PcepServerConfig,
    encode_close,
    encode_message,
    encode_object,
    encode_open,
    parse_objects,
    parse_open,
)
from ninja_pce.pcep_compute import PathReply


def _body(request_id: int = 1, *, bandwidth: float = 1_250_000.0) -> bytes:
    return (
        encode_object(2, struct.pack("!II", 0, request_id), flags=2)
        + encode_object(4, ipaddress.IPv4Address("10.255.0.1").packed
                        + ipaddress.IPv4Address("10.255.0.6").packed, flags=2)
        + encode_object(5, struct.pack("!f", bandwidth), flags=2)
    )


def _ids(raw: bytes) -> list[int]:
    return [int.from_bytes(obj.body[4:8], "big") for obj in parse_objects(raw[4:])
            if obj.object_class == 2]


class _Computer:
    def __init__(self, *, blocked: bool = False, valid: bool = True):
        self.release = threading.Event()
        if not blocked:
            self.release.set()
        self.calls: list[tuple[str, bytes]] = []
        self.validation_threads: list[int] = []
        self.valid = valid

    def __call__(self, peer: str, body: bytes) -> PathReply:
        self.calls.append((peer, body))
        if not self.release.wait(8):
            raise RuntimeError("Test worker latch was not released")
        ids = [int.from_bytes(obj.body[4:8], "big") for obj in parse_objects(body) if obj.object_class == 2]
        ero = encode_object(7, bytes((1, 8)) + ipaddress.IPv4Address("10.0.13.3").packed + bytes((32, 0)))
        return PathReply(
            tuple(encode_message(PCREP, encode_object(2, struct.pack("!II", 0, request_id), flags=2) + ero)
                  for request_id in ids),
            tuple({"request_id": request_id, "status": "PATH", "reservation_created": False} for request_id in ids),
        )

    def validate_reply(self, _reply: PathReply) -> bool:
        self.validation_threads.append(threading.get_ident())
        return self.valid


async def _eventually(predicate: Callable[[], bool], *, timeout: float = 2) -> None:
    async def poll() -> None:
        while not predicate():
            await asyncio.sleep(0.01)
    await asyncio.wait_for(poll(), timeout)


async def _read(reader: asyncio.StreamReader, *, timeout: float = 3) -> bytes:
    async def frame() -> bytes:
        header = await reader.readexactly(4)
        length = int.from_bytes(header[2:4], "big")
        return header + await reader.readexactly(length - 4)
    return await asyncio.wait_for(frame(), timeout)


async def _connect(server: PcepServer, *, stateful: bool = True,
                   keepalive: int = 30, deadtimer: int = 120
                   ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter, bytes]:
    reader, writer = await asyncio.open_connection("127.0.0.1", server.addresses[0][1])
    local_open = await _read(reader)
    assert local_open[1] == OPEN
    writer.write(encode_open(keepalive, deadtimer, stateful=stateful) + encode_message(KEEPALIVE))
    await writer.drain()
    assert await _read(reader) == encode_message(KEEPALIVE)
    await _eventually(lambda: server.snapshot()["peers"]["127.0.0.1"]["state"] == "ESTABLISHED")
    return reader, writer, local_open


async def _disconnect(server: PcepServer, writer: asyncio.StreamWriter) -> None:
    writer.close()
    await writer.wait_closed()
    await _eventually(lambda: "127.0.0.1" not in server._connections)


def test_blocking_computer_preserves_keepalive_reader_and_sender() -> None:
    async def scenario() -> None:
        computer = _Computer(blocked=True)
        server = PcepServer(PcepServerConfig(("127.0.0.1",), port=0, keepalive=1, deadtimer=4,
                                              computation_timeout=6), path_computer=computer)
        await server.start()
        heartbeat: asyncio.Task[None] | None = None
        try:
            reader, writer, _ = await _connect(server, keepalive=1, deadtimer=2)
            writer.write(encode_message(PCREQ, _body()))
            await writer.drain()
            await _eventually(lambda: len(computer.calls) == 1)

            async def keepalive() -> None:
                while True:
                    writer.write(encode_message(KEEPALIVE))
                    await writer.drain()
                    await asyncio.sleep(0.2)

            heartbeat = asyncio.create_task(keepalive())
            assert await _read(reader) == encode_message(KEEPALIVE)
            assert await _read(reader) == encode_message(KEEPALIVE)
            await asyncio.sleep(0.2)  # Total blocked interval exceeds the peer DeadTimer.
            assert server.snapshot()["peers"]["127.0.0.1"]["state"] == "ESTABLISHED"
            computer.release.set()
            reply = await _read(reader)
            assert reply[1] == PCREP and _ids(reply) == [1]
            assert len(computer.validation_threads) == 1
            assert computer.validation_threads[0] != threading.get_ident()
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
            heartbeat = None
            await _disconnect(server, writer)
        finally:
            computer.release.set()
            if heartbeat is not None:
                heartbeat.cancel()
                await asyncio.gather(heartbeat, return_exceptions=True)
            await server.close()
    asyncio.run(scenario())


def test_duplicate_pending_request_runs_computer_once_and_no_cache_replays() -> None:
    async def scenario() -> None:
        computer = _Computer(blocked=True)
        server = PcepServer(PcepServerConfig(("127.0.0.1",), port=0), path_computer=computer)
        await server.start()
        try:
            reader, writer, _ = await _connect(server)
            wire = encode_message(PCREQ, _body())
            writer.write(wire * 3)
            await writer.drain()
            await _eventually(lambda: len(computer.calls) == 1)
            await asyncio.sleep(0.05)
            assert len(computer.calls) == 1
            computer.release.set()
            assert (await _read(reader))[1] == PCREP
            await _eventually(lambda: server._connections["127.0.0.1"].computation.done())
            writer.write(wire)
            await writer.drain()
            assert (await _read(reader))[1] == PCREP
            assert len(computer.calls) == 2
            await _disconnect(server, writer)
        finally:
            computer.release.set()
            await server.close()
    asyncio.run(scenario())


def test_conflicting_pending_request_id_error_cancels_late_success() -> None:
    async def scenario() -> None:
        computer = _Computer(blocked=True)
        server = PcepServer(PcepServerConfig(("127.0.0.1",), port=0), path_computer=computer)
        await server.start()
        try:
            reader, writer, _ = await _connect(server)
            writer.write(encode_message(PCREQ, _body()))
            await writer.drain()
            await _eventually(lambda: len(computer.calls) == 1)
            writer.write(encode_message(PCREQ, _body(bandwidth=2_500_000.0)))
            await writer.drain()
            error = await _read(reader)
            assert error[1] == ERROR and _ids(error) == [1]
            computer.release.set()
            await _eventually(lambda: not server._workers)
            await asyncio.sleep(0.05)
            assert not any(event["direction"] == "tx" and event["message_type"] == PCREP
                           for event in server.snapshot()["events"])
            assert len(computer.calls) == 1
            await _disconnect(server, writer)
        finally:
            computer.release.set()
            await server.close()
    asyncio.run(scenario())


def test_conflicting_id_is_cancelled_before_awaiting_error_delivery() -> None:
    async def scenario() -> None:
        computer = _Computer(blocked=True)
        server = PcepServer(PcepServerConfig(("127.0.0.1",), port=0), path_computer=computer)
        original_send = server._send

        async def delayed_error_send(peer: str, connection: Any, raw: bytes) -> None:
            if raw[1] == ERROR and _ids(raw) == [1]:
                # Emulate a slow drain/send-lock acquisition while the old
                # computation becomes ready to deliver its result.
                computer.release.set()
                await _eventually(lambda: not server._workers)
                await asyncio.sleep(0.02)
            await original_send(peer, connection, raw)

        server._send = delayed_error_send
        await server.start()
        try:
            reader, writer, _ = await _connect(server)
            writer.write(encode_message(PCREQ, _body()))
            await writer.drain()
            await _eventually(lambda: len(computer.calls) == 1)
            writer.write(encode_message(PCREQ, _body(bandwidth=2_500_000.0)))
            await writer.drain()
            error = await _read(reader)
            assert error[1] == ERROR and _ids(error) == [1]
            assert not any(event["direction"] == "tx" and event["message_type"] == PCREP
                           for event in server.snapshot()["events"])
            await _disconnect(server, writer)
        finally:
            computer.release.set()
            await server.close()
    asyncio.run(scenario())


def test_timeout_and_reconnect_retain_global_slot_until_actual_worker_finishes() -> None:
    async def scenario() -> None:
        computer = _Computer(blocked=True)
        server = PcepServer(PcepServerConfig(("127.0.0.1",), port=0, max_computations=1,
                                              computation_timeout=0.1), path_computer=computer)
        await server.start()
        try:
            reader, writer, _ = await _connect(server)
            writer.write(encode_message(PCREQ, _body()))
            await writer.drain()
            error = await _read(reader)
            assert error[1] == ERROR and _ids(error) == [1]
            assert server.snapshot()["peers"]["127.0.0.1"]["last_computation"]["outcomes"][0]["reason"] == \
                "computation_timeout"
            assert len(server._workers) == 1 and server._compute_slots.locked()
            await _disconnect(server, writer)
            reader, writer, _ = await _connect(server)
            writer.write(encode_message(PCREQ, _body(2)))
            await writer.drain()
            error = await _read(reader)
            assert error[1] == ERROR and _ids(error) == [2]
            assert server.snapshot()["peers"]["127.0.0.1"]["last_computation"]["outcomes"][0]["reason"] == \
                "computation_capacity_exceeded"
            assert len(computer.calls) == 1 and len(server._workers) == 1
            computer.release.set()
            await _eventually(lambda: not server._workers)
            writer.write(encode_message(PCREQ, _body(3)))
            await writer.drain()
            reply = await _read(reader)
            assert reply[1] == PCREP and _ids(reply) == [3]
            assert len(computer.calls) == 2
            await _disconnect(server, writer)
        finally:
            computer.release.set()
            await server.close()
    asyncio.run(scenario())


def test_stale_reply_validation_rejects_instead_of_sending_ero() -> None:
    async def scenario() -> None:
        computer = _Computer(valid=False)
        server = PcepServer(PcepServerConfig(("127.0.0.1",), port=0), path_computer=computer)
        await server.start()
        try:
            reader, writer, _ = await _connect(server)
            writer.write(encode_message(PCREQ, _body(17)))
            await writer.drain()
            reply = await _read(reader)
            assert reply[1] == ERROR and _ids(reply) == [17]
            assert not any(obj.object_class == 7 for obj in parse_objects(reply[4:]))
            outcome = server.snapshot()["peers"]["127.0.0.1"]["last_computation"]["outcomes"][0]
            assert outcome["reason"] == "topology_changed_or_unavailable"
            await _disconnect(server, writer)
        finally:
            await server.close()
    asyncio.run(scenario())


def test_peer_close_cancels_delivery_and_server_close_with_active_client_is_bounded(tmp_path: Path) -> None:
    async def scenario() -> None:
        computer = _Computer(blocked=True)
        server = PcepServer(PcepServerConfig(("127.0.0.1",), port=0), path_computer=computer,
                            capture_path=tmp_path / "pcep.json")
        await server.start()
        try:
            reader, writer, _ = await _connect(server)
            writer.write(encode_message(PCREQ, _body()))
            await writer.drain()
            await _eventually(lambda: len(computer.calls) == 1)
            writer.write(encode_close())
            await writer.drain()
            assert await asyncio.wait_for(reader.read(), 2) == b""
            await _eventually(lambda: "127.0.0.1" not in server._connections)
            computer.release.set()
            await _eventually(lambda: not server._workers)
            assert not any(event["direction"] == "tx" and event["message_type"] == PCREP
                           for event in server.snapshot()["events"])
            writer.close()
            await writer.wait_closed()
            reader, writer, _ = await _connect(server)
            await asyncio.wait_for(server.close(), 2)
            assert (await _read(reader))[1] == CLOSE
            assert await asyncio.wait_for(reader.read(), 2) == b""
            peer = server.snapshot()["peers"]["127.0.0.1"]
            assert peer["state"] == "DISCONNECTED" and not peer["fresh"]
            writer.close()
            await writer.wait_closed()
        finally:
            computer.release.set()
            await server.close()
    asyncio.run(scenario())


def test_disconnect_before_timeout_keeps_worker_slot_and_cannot_reach_replacement_session() -> None:
    async def scenario() -> None:
        computer = _Computer(blocked=True)
        server = PcepServer(PcepServerConfig(("127.0.0.1",), port=0, max_computations=1,
                                              computation_timeout=6), path_computer=computer)
        await server.start()
        try:
            _, writer, _ = await _connect(server)
            writer.write(encode_message(PCREQ, _body()))
            await writer.drain()
            await _eventually(lambda: len(computer.calls) == 1)
            await _disconnect(server, writer)
            assert len(server._workers) == 1 and server._compute_slots.locked()
            reader, writer, _ = await _connect(server)
            writer.write(encode_message(PCREQ, _body(2)))
            await writer.drain()
            error = await _read(reader)
            assert error[1] == ERROR and _ids(error) == [2]
            assert len(computer.calls) == 1
            computer.release.set()
            await _eventually(lambda: not server._workers)
            await asyncio.sleep(0.05)
            assert not any(event["direction"] == "tx" and event["message_type"] == PCREP
                           for event in server.snapshot()["events"])
            await asyncio.wait_for(server.close(), 2)
            assert (await _read(reader))[1] == CLOSE
            writer.close()
            await writer.wait_closed()
        finally:
            computer.release.set()
            await server.close()
    asyncio.run(scenario())


def test_default_observer_rejects_pcreq_and_stateless_profile_omits_capabilities() -> None:
    async def scenario() -> None:
        observer = PcepServer(PcepServerConfig(("127.0.0.1",), port=0))
        await observer.start()
        try:
            reader, writer, local_open = await _connect(observer)
            assert parse_open(local_open[4:])["stateful_flags"] == 0
            writer.write(encode_message(PCREQ, _body()))
            await writer.drain()
            assert (await _read(reader))[1] == ERROR
            assert observer.snapshot()["capabilities"]["path_computation"] is False
            await _disconnect(observer, writer)
        finally:
            await observer.close()
        computer = _Computer()
        stateless = PcepServer(PcepServerConfig(("127.0.0.1",), port=0,
                                                 stateless_peers=("127.0.0.1",)), path_computer=computer)
        await stateless.start()
        try:
            reader, writer, local_open = await _connect(stateless, stateful=False)
            assert parse_open(local_open[4:])["stateful"] is False
            writer.write(encode_message(PCREQ, _body(22)))
            await writer.drain()
            reply = await _read(reader)
            assert reply[1] == PCREP and _ids(reply) == [22]
            assert stateless.snapshot()["capabilities"]["lsp_update"] is False
            assert not any(event["message_type"] in (11, 12) for event in stateless.snapshot()["events"])
            await _disconnect(stateless, writer)
        finally:
            await stateless.close()
    asyncio.run(scenario())


def test_cancel_serve_forever_then_close_with_active_client_is_bounded() -> None:
    """SIGTERM cancellation must reach CLI cleanup before clients disconnect."""
    async def scenario() -> None:
        server = PcepServer(PcepServerConfig(("127.0.0.1",), port=0))
        await server.start()
        serving = asyncio.create_task(server.serve_forever())
        writer: asyncio.StreamWriter | None = None
        try:
            reader, writer, _ = await _connect(server)
            serving.cancel()
            done, _ = await asyncio.wait({serving}, timeout=2)
            assert serving in done, "Cancelled serve_forever blocked before CLI shutdown cleanup"
            try:
                await serving
            except asyncio.CancelledError:
                pass
            else:
                raise AssertionError("serve_forever swallowed SIGTERM cancellation")
            await asyncio.wait_for(server.close(), 2)
            assert (await _read(reader))[1] == CLOSE
            assert await asyncio.wait_for(reader.read(), 2) == b""
            assert server.snapshot()["peers"]["127.0.0.1"]["state"] == "DISCONNECTED"
        finally:
            await server.close()
            await asyncio.gather(serving, return_exceptions=True)
            if writer is not None:
                writer.close()
                await writer.wait_closed()
    asyncio.run(scenario())


def test_close_drain_timeout_still_closes_writer_and_cancels_reply_delivery() -> None:
    async def scenario() -> None:
        computer = _Computer(blocked=True)
        server = PcepServer(PcepServerConfig(("127.0.0.1",), port=0, handshake_timeout=0.2),
                            path_computer=computer)
        await server.start()
        writer: asyncio.StreamWriter | None = None
        try:
            reader, writer, _ = await _connect(server)
            writer.write(encode_message(PCREQ, _body()))
            await writer.drain()
            await _eventually(lambda: len(computer.calls) == 1)
            connection = server._connections["127.0.0.1"]
            drain_started = asyncio.Event()

            async def stalled_drain() -> None:
                drain_started.set()
                await asyncio.Event().wait()

            connection.writer.drain = stalled_drain
            await asyncio.wait_for(server.close(), 2)
            assert drain_started.is_set()
            assert connection.terminal and connection.writer.is_closing()
            assert connection.computation is not None and connection.computation.cancelled()
            assert not server._connections and not server._tasks
            assert (await _read(reader))[1] == CLOSE
            assert await asyncio.wait_for(reader.read(), 2) == b""
            peer = server.snapshot()["peers"]["127.0.0.1"]
            assert peer["state"] == "DISCONNECTED" and not peer["fresh"]
            computer.release.set()
            await _eventually(lambda: not server._workers)
            assert not any(event["direction"] == "tx" and event["message_type"] == PCREP
                           for event in server.snapshot()["events"])
        finally:
            computer.release.set()
            await server.close()
            if writer is not None:
                writer.close()
                await writer.wait_closed()
    asyncio.run(scenario())


def test_historical_and_uncorrelated_srp_do_not_acknowledge_current_session_updates() -> None:
    async def scenario() -> None:
        server = PcepServer(PcepServerConfig(("127.0.0.1",), port=0))
        await server.start()
        writer: asyncio.StreamWriter | None = None
        try:
            _, writer, _ = await _connect(server)
            connection = server._connections["127.0.0.1"]
            report = {"end_of_sync": False, "sync": True, "delegate": True, "removed": False,
                      "plsp_id": 1, "srp_id": 2,
                      "identifiers": {"sender": "10.255.0.1", "endpoint": "10.255.0.6", "tunnel_id": 101},
                      "operational_state": "UP", "ero": []}
            server._acknowledge_updates(connection, [report])
            assert connection.state["updates"] == {} and not connection.owned_plsps

            # SRP IDs restart per session; a historical ID can also collide with
            # an outstanding current update and must not acknowledge that update.
            connection.state["updates"]["2"] = {"plsp_id": 1, "acknowledged": False}
            server._acknowledge_updates(connection, [report])
            assert connection.state["updates"]["2"] == {"plsp_id": 1, "acknowledged": False}
            assert not connection.owned_plsps

            uncorrelated = {**report, "sync": False, "srp_id": 3}
            server._acknowledge_updates(connection, [uncorrelated])
            assert uncorrelated["uncorrelated_srp_id"] == 3
            assert connection.state["updates"]["2"] == {"plsp_id": 1, "acknowledged": False}
            assert not connection.owned_plsps
        finally:
            await server.close()
            if writer is not None:
                writer.close()
                await writer.wait_closed()
    asyncio.run(scenario())


def test_known_srp_with_changed_lsp_identity_remains_an_error() -> None:
    async def scenario() -> None:
        server = PcepServer(PcepServerConfig(("127.0.0.1",), port=0))
        await server.start()
        writer: asyncio.StreamWriter | None = None
        try:
            _, writer, _ = await _connect(server)
            connection = server._connections["127.0.0.1"]
            identity = _lsp_identifiers(224)
            pending = {"plsp_id": 1, "acknowledged": False,
                       "sender": identity["sender"], "endpoint": identity["endpoint"],
                       "tunnel_id": identity["tunnel_id"],
                       "ownership_key": [18, "10.255.0.1", "10.255.0.6", 101, "0aff0001"]}
            connection.state["updates"]["2"] = pending.copy()
            report = {"end_of_sync": False, "sync": False, "delegate": True, "removed": False,
                      "plsp_id": 1, "srp_id": 2, "identifiers": identity,
                      "operational_state": "UP", "ero": []}
            changed_reports = [
                {**report, "identifiers": {**identity, "sender": "10.255.0.99"}},
                {**report, "identifiers": {**identity, "endpoint": "10.255.0.99"}},
                {**report, "identifiers": {**identity, "tunnel_id": 102}},
                {**report, "identifiers": {**identity, "type": 19}},
                {**report, "identifiers": {**identity, "extended_tunnel_id_hex": "0aff0063"}},
                {**report, "plsp_id": 2},
            ]
            for changed in changed_reports:
                try:
                    server._acknowledge_updates(connection, [changed])
                except PcepProtocolError as exc:
                    assert (exc.error_type, exc.error_value) == (19, 1)
                else:
                    raise AssertionError("A known SRP ID acknowledged a different LSP identity")
                assert connection.state["updates"]["2"] == pending
                assert not connection.owned_plsps
        finally:
            await server.close()
            if writer is not None:
                writer.close()
                await writer.wait_closed()
    asyncio.run(scenario())


def _lsp_identifiers(lsp_id: int) -> dict[str, Any]:
    sender = ipaddress.IPv4Address("10.255.0.1").packed
    endpoint = ipaddress.IPv4Address("10.255.0.6").packed
    value = sender + struct.pack("!HH", lsp_id, 101) + sender + endpoint
    return {"type": 18, "sender": "10.255.0.1", "endpoint": "10.255.0.6", "tunnel_id": 101,
            "extended_tunnel_id_hex": sender.hex(), "lsp_id": lsp_id,
            "all_zero": False, "value_hex": value.hex()}


def test_old_mbb_lsp_removal_cannot_change_latest_state_of_acknowledged_new_lsp() -> None:
    async def scenario() -> None:
        server = PcepServer(PcepServerConfig(("127.0.0.1",), port=0))
        await server.start()
        writer: asyncio.StreamWriter | None = None
        try:
            _, writer, _ = await _connect(server)
            connection = server._connections["127.0.0.1"]
            pending = {"plsp_id": 1, "acknowledged": False,
                       "sender": "10.255.0.1", "endpoint": "10.255.0.6", "tunnel_id": 101,
                       "ownership_key": [18, "10.255.0.1", "10.255.0.6", 101, "0aff0001"]}
            connection.state["updates"]["2"] = pending
            identity = _lsp_identifiers(224)
            report = {"end_of_sync": False, "sync": False, "delegate": True, "removed": False,
                      "plsp_id": 1, "srp_id": 2, "identifiers": identity,
                      "operational_state": "ACTIVE", "ero": []}
            server._acknowledge_updates(connection, [report])
            assert pending["acknowledged"]
            assert pending["acknowledged_identifiers"] == identity
            assert pending["acknowledged_identifiers"] is not identity
            assert pending["reported_state"] == pending["latest_reported_state"] == "ACTIVE"
            owner = (18, "10.255.0.1", "10.255.0.6", 101, "0aff0001")
            connection.owned_plsps[1] = owner
            connection.state["reports"] = {f"1:18:{identity['value_hex']}": report}

            old_removed = {**report, "srp_id": 0, "identifiers": _lsp_identifiers(223),
                           "delegate": False, "removed": True, "operational_state": "DOWN"}
            server._acknowledge_updates(connection, [old_removed])
            assert pending["latest_reported_state"] == "ACTIVE"
            assert pending["acknowledged_identifiers"]["lsp_id"] == 224
            assert connection.owned_plsps == {1: owner}

            # Reports for the acknowledged instance still update its diagnostic
            # state, including an explicit removal of that same instance.
            current = {**report, "srp_id": 0, "operational_state": "UP"}
            server._acknowledge_updates(connection, [current])
            assert pending["latest_reported_state"] == "UP"
            server._acknowledge_updates(connection, [{**current, "delegate": False}])
            assert not connection.owned_plsps
            connection.owned_plsps[1] = owner
            # _apply_reports removes the current entry before acknowledgment
            # handling. There is now no other delegated instance to preserve.
            connection.state["reports"] = {}
            server._acknowledge_updates(connection, [{**current, "removed": True}])
            assert pending["latest_reported_state"] == "REMOVED"
            assert pending["reported_state"] == "ACTIVE"
            assert not connection.owned_plsps
        finally:
            await server.close()
            if writer is not None:
                writer.close()
                await writer.wait_closed()
    asyncio.run(scenario())
