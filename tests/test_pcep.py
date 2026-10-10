"""Wire and real loopback TCP tests; no routers or external addresses contacted."""
from __future__ import annotations

import asyncio
import ipaddress
import json
import struct
from pathlib import Path
from typing import Any

import pytest

from ninja_pce.pcep import (
    CLOSE,
    ERROR,
    KEEPALIVE,
    OPEN,
    PCRPT,
    PcepProtocolError,
    PcepServer,
    PcepServerConfig,
    encode_close,
    encode_message,
    encode_object,
    encode_open,
    parse_objects,
    parse_open,
    parse_reports,
    parse_tlvs,
)


def _tlv(kind: int, value: bytes) -> bytes:
    return struct.pack("!HH", kind, len(value)) + value + bytes(-len(value) % 4)


def _report(plsp: int = 1, *, sync: bool = True, name: bytes | None = b"lab-lsp",
            flags: int = 0, lsp_id: int = 7, empty: bool = False) -> bytes:
    identifiers = (ipaddress.IPv4Address("10.255.0.1").packed + struct.pack("!HH", lsp_id, 1001) +
                   ipaddress.IPv4Address("10.255.0.1").packed + ipaddress.IPv4Address("10.255.0.6").packed)
    body = struct.pack("!I", (plsp << 12) | (2 if sync else 0) | flags | (1 << 4))
    body += _tlv(18, identifiers) if plsp else _tlv(18, bytes(16))
    if name is not None and plsp:
        body += _tlv(17, name)
    ero = b"" if empty or not plsp else bytes((1, 8)) + ipaddress.IPv4Address("10.0.13.3").packed + b"\x20\x00"
    return encode_object(32, body) + encode_object(7, ero)


async def _read(reader: asyncio.StreamReader, timeout: float = 2.0) -> bytes:
    header = await asyncio.wait_for(reader.readexactly(4), timeout)
    length = struct.unpack("!H", header[2:])[0]
    return header + await asyncio.wait_for(reader.readexactly(length - 4), timeout)


async def _connect(server: PcepServer, *, stateful: bool = True, keepalive: int = 30, deadtimer: int = 120
                   ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    reader, writer = await asyncio.open_connection("127.0.0.1", server.addresses[0][1])
    local_open = await _read(reader)
    assert local_open[1] == OPEN
    assert parse_open(local_open[4:])["stateful_flags"] == 0
    writer.write(encode_open(keepalive, deadtimer, 21, stateful=stateful) + encode_message(KEEPALIVE))
    await writer.drain()
    assert await _read(reader) == encode_message(KEEPALIVE)
    await _eventually(lambda: server.snapshot()["peers"]["127.0.0.1"]["state"] == "ESTABLISHED")
    return reader, writer


async def _eventually(predicate: Any) -> None:
    for _ in range(100):
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("Expected server state was not observed")


def test_codec_exact_open_passive_capability_and_tlv_padding() -> None:
    assert encode_open().hex() == "2001001401100010201e78000010000400000000"
    assert parse_open(encode_open()[4:])["stateful_flags"] == 0
    assert parse_tlvs(_tlv(17, b"abc")) == ((17, b"abc"),)
    # Reserved common-object flags and unsupported SRP flags are ignored.
    payload = encode_object(33, struct.pack("!II", 0xFFFFFFFF, 0)) + _report()
    assert parse_reports(payload)[0]["srp_id"] == 0


def test_ios_xe_16_9_captured_open_and_empty_database_sync() -> None:
    # Actual CSR1000v IOS XE 16.9.7 frames captured in the six-router lab.
    peer_open = bytes.fromhex("2001001c01100018202878010010000400000005001a000400000010")
    end_of_sync = bytes.fromhex(
        "200a00242010001c00000000001200100000000000000000000000000000000007100004"
    )
    opened = parse_open(peer_open[4:])
    assert opened["keepalive"] == 40 and opened["deadtimer"] == 120
    assert opened["stateful"] and opened["stateful_flags"] == 5
    assert {"type": 26, "value_hex": "00000010"} in opened["tlvs"]

    reports = parse_reports(end_of_sync[4:])
    assert len(reports) == 1
    assert reports[0]["end_of_sync"] and reports[0]["plsp_id"] == 0
    assert reports[0]["ero"] == [] and reports[0]["identifiers"]["all_zero"]
    state: dict[str, Any] = {"reports": {}, "lsp_names": {}, "synchronized": False}
    server = PcepServer(PcepServerConfig(("127.0.0.1",)))
    server._apply_reports(state, reports)
    assert state["synchronized"] and state["reports"] == {} and state["lsp_names"] == {}


@pytest.mark.parametrize("body", [b"\x01", b"\x01\x10\x00\x00", b"\x01\x10\x00\x06xx",
                                  b"\x01\x10\x00\x0c1234", b"\x01\x00\x00\x04"])
def test_invalid_objects_rejected(body: bytes) -> None:
    with pytest.raises(PcepProtocolError):
        parse_objects(body)


@pytest.mark.parametrize("body", [b"x", b"\x00\x10\x00\x04xx", b"\x00\x11\x00\x03abc"])
def test_invalid_tlvs_rejected(body: bytes) -> None:
    with pytest.raises(PcepProtocolError):
        parse_tlvs(body)


def test_open_validation_ignores_reserved_bits_but_rejects_bad_capability() -> None:
    raw = bytearray(encode_open())
    raw[8] |= 0x1F
    assert parse_open(bytes(raw[4:]))["stateful"]
    with pytest.raises(PcepProtocolError):
        parse_open(encode_object(1, b"\x20\x1e\x78\x00" + _tlv(16, b"bad")))
    with pytest.raises(PcepProtocolError):
        parse_open(encode_open(30, 20)[4:])
    assert parse_open(encode_open(0, 120)[4:])["deadtimer"] == 0


def test_report_fields_bandwidth_and_end_sync() -> None:
    payload = _report() + encode_object(5, struct.pack("!f", 1_250_000.0)) + _report(0, sync=False)
    report, end = parse_reports(payload)
    assert report["plsp_id"] == 1
    assert report["operational_state"] == "UP"
    assert report["symbolic_name"] == "lab-lsp"
    assert report["identifiers"]["tunnel_id"] == 1001
    assert report["ero"][0]["address"] == "10.0.13.3"
    assert report["attributes"][0]["bits_per_second"] == 10_000_000
    assert end["end_of_sync"]


@pytest.mark.parametrize("payload", [b"", encode_object(7, b""), encode_object(32, struct.pack("!I", 4096)),
                                     _report(flags=1), _report(0, sync=True),
                                     _report() + encode_object(5, struct.pack("!f", float("nan")))])
def test_invalid_reports_and_delegation_rejected(payload: bytes) -> None:
    with pytest.raises(PcepProtocolError):
        parse_reports(payload)


@pytest.mark.parametrize("values", [
    {"allowed_peers": ()}, {"allowed_peers": ("0.0.0.0",)}, {"port": True},
    {"keepalive": 30, "deadtimer": 20}, {"handshake_timeout": float("nan")},
    {"max_message_bytes": 21}, {"max_sessions": 0}, {"observation_ttl": False},
])
def test_config_fail_closed(values: dict[str, Any]) -> None:
    arguments: dict[str, Any] = {"allowed_peers": ("127.0.0.1",)}
    arguments.update(values)
    with pytest.raises(ValueError):
        PcepServerConfig(**arguments)


def test_real_socket_fragmented_open_coalesced_reports_capture_and_close(tmp_path: Path) -> None:
    async def scenario() -> None:
        path = tmp_path / "capture.json"
        server = PcepServer(PcepServerConfig(("127.0.0.1",), port=0), capture_path=path)
        await server.start()
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", server.addresses[0][1])
            assert (await _read(reader))[1] == OPEN
            raw = encode_open()
            for byte in raw:
                writer.write(bytes((byte,)))
                await writer.drain()
            assert await _read(reader) == encode_message(KEEPALIVE)
            writer.write(encode_message(KEEPALIVE) + encode_message(PCRPT, _report()) +
                         encode_message(PCRPT, _report(0, sync=False)))
            await writer.drain()
            await _eventually(lambda: server.snapshot()["peers"]["127.0.0.1"]["synchronized"])
            peer = server.snapshot()["peers"]["127.0.0.1"]
            assert peer["fresh"] and peer["valid_until"]
            assert len(peer["reports"]) == 1
            assert not next(iter(peer["reports"].values()))["stale"]
            captured = json.loads(path.read_text())
            assert captured["checksum"]
            assert captured["authoritative"] is False and captured["complete"] is False
            assert any(item["raw_hex"] == encode_message(PCRPT, _report()).hex() for item in captured["events"])
            assert {item["message_type"] for item in captured["events"] if item["direction"] == "tx"} == {1, 2}
            writer.write(encode_close())
            await writer.drain()
            assert await asyncio.wait_for(reader.read(), 2) == b""
            await _eventually(lambda: server.snapshot()["peers"]["127.0.0.1"]["state"] == "DISCONNECTED")
            peer = server.snapshot()["peers"]["127.0.0.1"]
            assert not peer["fresh"] and not peer["synchronized"]
            assert next(iter(peer["reports"].values()))["stale"]
            writer.close()
            await writer.wait_closed()
        finally:
            await server.close()
        reopened = PcepServer(server.config, capture_path=path)
        peer = reopened.snapshot()["peers"]["127.0.0.1"]
        assert peer["stale_reason"] == "process_restart" and not peer["fresh"]
    asyncio.run(scenario())


def test_allowlist_and_duplicate_connection_real_sockets() -> None:
    async def scenario() -> None:
        refused = PcepServer(PcepServerConfig(("192.0.2.1",), port=0))
        await refused.start()
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", refused.addresses[0][1])
            assert await asyncio.wait_for(reader.read(), 2) == b""
            assert not refused.snapshot()["peers"]
            writer.close()
            await writer.wait_closed()
        finally:
            await refused.close()
        server = PcepServer(PcepServerConfig(("127.0.0.1",), port=0))
        await server.start()
        try:
            _, writer = await _connect(server)
            other_reader, other_writer = await asyncio.open_connection("127.0.0.1", server.addresses[0][1])
            assert await _read(other_reader) == bytes.fromhex("2006000c0d10000800000900")
            assert await asyncio.wait_for(other_reader.read(), 2) == b""
            assert server.snapshot()["peers"]["127.0.0.1"]["state"] == "ESTABLISHED"
            other_writer.close()
            writer.close()
            await other_writer.wait_closed()
            await writer.wait_closed()
        finally:
            await server.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("wire", [bytes.fromhex("20020003"), bytes.fromhex("20020008"),
                                  bytes.fromhex("40020004"), bytes.fromhex("20020040")])
def test_bad_framing_or_non_open_rejected_before_establishment(wire: bytes) -> None:
    async def scenario() -> None:
        server = PcepServer(PcepServerConfig(("127.0.0.1",), port=0, max_message_bytes=32,
                                              handshake_timeout=0.1))
        await server.start()
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", server.addresses[0][1])
            await _read(reader)
            writer.write(wire)
            await writer.drain()
            assert (await _read(reader))[1] == ERROR
            assert await asyncio.wait_for(reader.read(), 2) == b""
            writer.close()
            await writer.wait_closed()
        finally:
            await server.close()
    asyncio.run(scenario())


def test_openwait_and_keepwait_timeout_real_sockets() -> None:
    async def scenario() -> None:
        server = PcepServer(PcepServerConfig(("127.0.0.1",), port=0, handshake_timeout=0.1))
        await server.start()
        try:
            for opened, value in ((False, 2), (True, 7)):
                reader, writer = await asyncio.open_connection("127.0.0.1", server.addresses[0][1])
                await _read(reader)
                if opened:
                    writer.write(encode_open())
                    await writer.drain()
                    await _read(reader)
                error = await _read(reader)
                assert error[-2:] == bytes((1, value))
                assert await asyncio.wait_for(reader.read(), 2) == b""
                writer.close()
                await writer.wait_closed()
                await _eventually(lambda: server.snapshot()["peers"]["127.0.0.1"]["state"] == "DISCONNECTED")
        finally:
            await server.close()
    asyncio.run(scenario())


def test_keepalive_and_dead_timer_real_socket() -> None:
    async def scenario() -> None:
        server = PcepServer(PcepServerConfig(("127.0.0.1",), port=0, keepalive=1, deadtimer=4))
        await server.start()
        try:
            reader, writer = await _connect(server, keepalive=1, deadtimer=2)
            assert await _read(reader) == encode_message(KEEPALIVE)
            while True:
                raw = await _read(reader)
                if raw[1] == CLOSE:
                    assert raw[-1] == 2
                    break
            assert await asyncio.wait_for(reader.read(), 2) == b""
            await _eventually(lambda: server.snapshot()["peers"]["127.0.0.1"]["state"] == "DISCONNECTED")
            assert server.snapshot()["peers"]["127.0.0.1"]["stale_reason"] == "deadtimer_expired"
            writer.close()
            await writer.wait_closed()
        finally:
            await server.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("stateful,payload,error", [(False, _report(), (19, 5)),
                                                     (True, _report(flags=1), (19, 1)),
                                                     (True, encode_object(7, b""), (6, 8))])
def test_unnegotiated_reports_delegation_and_missing_lsp_rejected(
        stateful: bool, payload: bytes, error: tuple[int, int]) -> None:
    async def scenario() -> None:
        server = PcepServer(PcepServerConfig(("127.0.0.1",), port=0))
        await server.start()
        try:
            reader, writer = await _connect(server, stateful=stateful)
            writer.write(encode_message(PCRPT, payload))
            await writer.drain()
            assert (await _read(reader))[-2:] == bytes(error)
            assert (await _read(reader))[1] == CLOSE
            assert await asyncio.wait_for(reader.read(), 2) == b""
            assert not server.snapshot()["peers"]["127.0.0.1"]["reports"]
            writer.close()
            await writer.wait_closed()
        finally:
            await server.close()
    asyncio.run(scenario())


def test_updates_removals_and_bounded_unknown_message_handling() -> None:
    async def scenario() -> None:
        server = PcepServer(PcepServerConfig(("127.0.0.1",), port=0, max_unknown_messages=2,
                                              max_capture_events=8))
        await server.start()
        try:
            reader, writer = await _connect(server)
            writer.write(encode_message(PCRPT, _report() + _report(lsp_id=8) + _report(0, sync=False)))
            await writer.drain()
            await _eventually(lambda: len(server.snapshot()["peers"]["127.0.0.1"]["reports"]) == 2)
            writer.write(encode_message(PCRPT, _report(sync=False, name=None, flags=4, lsp_id=7)))
            await writer.drain()
            await _eventually(lambda: len(server.snapshot()["peers"]["127.0.0.1"]["reports"]) == 1)
            for _ in range(2):
                writer.write(encode_message(99))
                await writer.drain()
                assert (await _read(reader))[1] == ERROR
            assert (await _read(reader))[1] == CLOSE
            assert len(server.snapshot()["events"]) <= 8
            writer.close()
            await writer.wait_closed()
        finally:
            await server.close()
    asyncio.run(scenario())


def test_report_message_is_atomic_and_names_survive_path_removal() -> None:
    server = PcepServer(PcepServerConfig(("127.0.0.1",)))
    state: dict[str, Any] = {"reports": {}, "lsp_names": {}, "synchronized": False,
                             "source": "pcep:127.0.0.1", "session_id": "test-session"}
    server._apply_reports(state, parse_reports(_report() + _report(0, sync=False)))
    before = json.dumps(state, sort_keys=True)
    with pytest.raises(PcepProtocolError, match="name changed"):
        server._apply_reports(state, parse_reports(_report(sync=False, lsp_id=8) +
                                                   _report(sync=False, name=b"changed")))
    assert json.dumps(state, sort_keys=True) == before
    server._apply_reports(state, parse_reports(_report(sync=False, flags=4, name=None)))
    assert state["reports"] == {}
    server._apply_reports(state, parse_reports(_report(sync=False, name=None, lsp_id=8)))
    assert next(iter(state["reports"].values()))["symbolic_name"] == "lab-lsp"


def test_report_capacity_and_byte_limit_are_atomic() -> None:
    for config, payload in (
        (PcepServerConfig(("127.0.0.1",), max_reports_per_peer=1), _report() + _report(lsp_id=8)),
        (PcepServerConfig(("127.0.0.1",), max_report_bytes_per_peer=1024),
         _report(name=b"x" * 800)),
    ):
        server = PcepServer(config)
        state: dict[str, Any] = {"reports": {}, "lsp_names": {}, "synchronized": False,
                                 "source": "pcep:127.0.0.1", "session_id": "test-session"}
        with pytest.raises(PcepProtocolError, match="storage limit"):
            server._apply_reports(state, parse_reports(payload))
        assert state["reports"] == {} and state["lsp_names"] == {}


def test_disabled_keepalive_observation_ttl_and_disconnect_reconnect() -> None:
    async def scenario() -> None:
        server = PcepServer(PcepServerConfig(("127.0.0.1",), port=0, keepalive=0, deadtimer=0,
                                              observation_ttl=0.1))
        await server.start()
        try:
            reader, writer = await _connect(server, keepalive=0, deadtimer=0)
            writer.write(encode_message(PCRPT, _report()))
            await writer.drain()
            await _eventually(lambda: bool(server.snapshot()["peers"]["127.0.0.1"]["reports"]))
            assert next(iter(server.snapshot()["peers"]["127.0.0.1"]["reports"].values()))["stale"]
            writer.write(encode_message(PCRPT, _report(0, sync=False)))
            await writer.drain()
            await _eventually(lambda: server.snapshot()["peers"]["127.0.0.1"]["synchronized"])
            await asyncio.sleep(0.15)
            assert not server.snapshot()["peers"]["127.0.0.1"]["fresh"]
            assert not reader._buffer  # Keepalive0 must not generate periodic messages.
            writer.close()
            await writer.wait_closed()
            await _eventually(lambda: server.snapshot()["peers"]["127.0.0.1"]["state"] == "DISCONNECTED")
            _, replacement = await _connect(server, keepalive=0, deadtimer=0)
            peer = server.snapshot()["peers"]["127.0.0.1"]
            assert peer["local_sid"] == 2 and not peer["reports"] and not peer["synchronized"]
            replacement.close()
            await replacement.wait_closed()
        finally:
            await server.close()
    asyncio.run(scenario())


def test_message_rate_limit_and_malformed_keepalive_real_socket() -> None:
    async def scenario() -> None:
        for bad_keepalive in (False, True):
            server = PcepServer(PcepServerConfig(("127.0.0.1",), port=0, max_messages_per_minute=1))
            await server.start()
            try:
                reader, writer = await _connect(server)
                if bad_keepalive:
                    writer.write(encode_message(KEEPALIVE, bytes(4)))
                    await writer.drain()
                    assert (await _read(reader))[1] == ERROR
                else:
                    writer.write(encode_message(KEEPALIVE) * 2)
                    await writer.drain()
                assert (await _read(reader))[1] == CLOSE
                writer.close()
                await writer.wait_closed()
            finally:
                await server.close()
    asyncio.run(scenario())


def test_capture_byte_budget_and_corrupt_restart_fail_closed(tmp_path: Path) -> None:
    server = PcepServer(PcepServerConfig(("127.0.0.1",), max_capture_bytes=1024))
    for _ in range(10):
        server._event("127.0.0.1", "rx", encode_message(99, bytes(400)))
    assert sum(len(event["raw_hex"]) for event in server.snapshot()["events"]) <= 1024
    path = tmp_path / "corrupt.json"
    path.write_text('{"version":2,"checksum":"bad"}')
    with pytest.raises(ValueError, match="checksum"):
        PcepServer(server.config, capture_path=path)
