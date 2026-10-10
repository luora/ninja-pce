"""Initial delegation wire/admission tests; synthetic requests use captured fields."""

from __future__ import annotations

import asyncio
import copy
import struct
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_pcep import _eventually, _read
from test_pcep_compute import PEER, prepare

from ninja_pce.pcep import (
    CLOSE,
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
from ninja_pce.pcep_delegation import (
    DelegatedPathComputer,
    ownership_key,
    report_fingerprint,
    report_key,
)

FRAME = Path(__file__).parent / "fixtures/pcep-iosxe-16.9.7/tunnel100-pcrpt.hex"


def initial_body():
    # Deliberately synthetic initial delegation, derived from captured field
    # encodings. This is not represented as a live D=1 capture.
    objects = parse_objects(bytes.fromhex(FRAME.read_text())[4:])
    body = b""
    for obj in objects:
        raw = obj.body
        if obj.object_class == 32:
            raw = struct.pack("!I", (int.from_bytes(raw[:4], "big") & ~0x7F) | 9) + raw[4:]
        elif obj.object_class == 7:
            raw = b""
        elif obj.object_class == 6:
            raw = bytes((0, 0, 0, 2)) + bytes(4)
        body += encode_object(obj.object_class, raw, obj.object_type)
    return body


def initial_report():
    return parse_reports(initial_body(), allow_delegation=True)[0]


def adapter(tmp_path, monkeypatch, peer=PEER):
    computer = prepare(tmp_path, monkeypatch)
    computer.headends[peer] = PEER
    return computer, DelegatedPathComputer(computer, {peer: (100,)})


def test_initial_delegation_strict_path_and_complete_update_fields(tmp_path, monkeypatch):
    computer, delegated = adapter(tmp_path, monkeypatch)
    report = initial_report()
    before = computer.raw_path.read_bytes()
    reply = delegated(PEER, [report], (17,))
    assert delegated.validate_reply(reply)
    assert reply.outcomes[0]["status"] == "INITIAL_PATH"
    assert reply.outcomes[0]["bandwidth_bps"] == 50_000_000
    assert reply.outcomes[0]["reservation_created"] is False
    wire = reply.messages[0]
    assert wire[1] == 11
    assert [obj.object_class for obj in parse_objects(wire[4:])] == [33, 32, 7, 9, 5, 6]
    result = parse_reports(wire[4:], allow_delegation=True)[0]
    assert result["srp_id"] == 17 and result["delegate"] and result["administrative_up"]
    assert result["identifiers"] == report["identifiers"]
    assert [item["address"] for item in result["ero"]] == ["10.0.13.3", "10.0.34.4", "10.0.46.6"]
    assert computer.raw_path.read_bytes() == before


@pytest.mark.parametrize(
    "change",
    [
        "active",
        "ero",
        "source",
        "tunnel",
        "protection",
        "igp",
        "different_bandwidth",
        "unknown",
        "tlv",
    ],
)
def test_unsupported_delegation_returns_authority_without_path_change(tmp_path, monkeypatch, change):
    _, delegated = adapter(tmp_path, monkeypatch)
    report = initial_report()
    if change == "active":
        report["operational_code"] = 2
    elif change == "ero":
        report["ero"] = [{"address": "10.0.13.3"}]
    elif change == "source":
        report["identifiers"]["sender"] = "10.255.0.2"
    elif change == "tunnel":
        report["identifiers"]["tunnel_id"] = 999
    elif change == "tlv":
        report["tlvs"].append({"type": 999, "value_hex": ""})
    elif change == "unknown":
        report["attributes"].append({"class": 99, "type": 1, "body_hex": ""})
    else:
        for attr in report["attributes"]:
            if change == "protection" and attr["class"] == 9:
                value = bytearray.fromhex(attr["body_hex"])
                value[14] = 1
                attr["body_hex"] = value.hex()
            if change == "igp" and attr["class"] == 6:
                value = bytearray.fromhex(attr["body_hex"])
                value[3] = 1
                attr["body_hex"] = value.hex()
            if change == "different_bandwidth" and (attr["class"], attr["type"]) == (5, 5):
                attr["body_hex"] = struct.pack("!f", 1).hex()
    reply = delegated(PEER, [report], (1,))
    assert reply.outcomes[0]["status"] == "DELEGATION_RETURNED"
    returned = parse_reports(reply.messages[0][4:])[0]
    assert not returned["delegate"] and returned["administrative_up"]
    assert returned["ero"] == [] and returned["attributes"] == []


def test_missing_authority_and_reserved_srp_never_send_update(tmp_path, monkeypatch):
    _, delegated = adapter(tmp_path, monkeypatch)
    report = initial_report()
    for number in (0, 0xFFFFFFFF, -1):
        with pytest.raises(ValueError):
            delegated(PEER, [report], (number,))
    report["delegate"] = False
    with pytest.raises(ValueError):
        delegated(PEER, [report], (1,))
    with pytest.raises(ValueError):
        encode_open(stateful=False, updates=True)


def test_collector_failure_returns_delegation_and_stale_path_fails_validation(tmp_path, monkeypatch):
    computer, delegated = adapter(tmp_path, monkeypatch)
    good = delegated(PEER, [initial_report()], (1,))
    computer.topology_path.unlink()
    assert not delegated.validate_reply(good)
    refused = delegated(PEER, [initial_report()], (2,))
    assert refused.outcomes[0]["status"] == "DELEGATION_RETURNED"
    assert delegated.validate_reply(refused)


async def connect(server):
    reader, writer = await asyncio.open_connection("127.0.0.1", server.addresses[0][1])
    assert parse_open((await _read(reader))[4:])["stateful_flags"] == 1
    writer.write(encode_open(1, 5, updates=True) + encode_message(KEEPALIVE))
    await writer.drain()
    assert await _read(reader) == encode_message(KEEPALIVE)
    eos = bytes.fromhex("2010001c00000000001200100000000000000000000000000000000007100004")
    writer.write(encode_message(PCRPT, eos))
    await writer.drain()
    await _eventually(lambda: server.snapshot()["peers"]["127.0.0.1"]["synchronized"])
    return reader, writer


def test_socket_delegation_reply_acknowledgement_and_no_active_rerouting(tmp_path, monkeypatch):
    computer, delegated = adapter(tmp_path, monkeypatch, "127.0.0.1")

    async def run():
        server = PcepServer(
            PcepServerConfig(("127.0.0.1",), port=0, keepalive=1, deadtimer=5),
            path_computer=computer,
            delegated_computer=delegated,
        )
        await server.start()
        reader, writer = await connect(server)
        try:
            writer.write(encode_message(PCRPT, initial_body()))
            await writer.drain()
            response = await _read(reader)
            assert response[1] == 11
            objects = parse_objects(response[4:])
            ack = b""
            for obj in objects:
                body = obj.body
                if obj.object_class == 32:
                    body = (int.from_bytes(body[:4], "big") | 0x20).to_bytes(4, "big") + body[4:]
                ack += encode_object(obj.object_class, body, obj.object_type)
            writer.write(encode_message(PCRPT, ack))
            await writer.drain()
            await _eventually(lambda: server.snapshot()["peers"]["127.0.0.1"]["updates"]["1"]["acknowledged"])
            state = server.snapshot()["peers"]["127.0.0.1"]
            assert state["updates"]["1"]["reported_state"] == "ACTIVE"
            assert len(state["updates"]) == 1
            assert all(report["delegation_accepted"] for report in state["reports"].values())
            # Active repeat report cannot cause another update/reroute.
            writer.write(encode_message(PCRPT, ack))
            await writer.drain()
            await asyncio.sleep(0.05)
            assert len(server.snapshot()["peers"]["127.0.0.1"]["updates"]) == 1
        finally:
            await asyncio.wait_for(server.close(), 2)
            writer.close()
            await writer.wait_closed()

    asyncio.run(run())


def test_socket_missing_update_ack_closes_session(tmp_path, monkeypatch):
    computer, delegated = adapter(tmp_path, monkeypatch, "127.0.0.1")

    async def run():
        server = PcepServer(
            PcepServerConfig(("127.0.0.1",), port=0, keepalive=1, deadtimer=5, update_ack_timeout=0.1),
            path_computer=computer,
            delegated_computer=delegated,
        )
        await server.start()
        reader, writer = await connect(server)
        try:
            writer.write(encode_message(PCRPT, initial_body()))
            await writer.drain()
            assert (await _read(reader))[1] == 11
            assert (await _read(reader, 2))[1] == CLOSE
            await _eventually(lambda: server.snapshot()["peers"]["127.0.0.1"]["state"] == "DISCONNECTED")
            assert server.snapshot()["peers"]["127.0.0.1"]["updates"]["1"]["status"] == "ACK_TIMEOUT"
        finally:
            await server.close()
            writer.close()
            await writer.wait_closed()

    asyncio.run(run())


@pytest.mark.parametrize(
    "field,value,accepted",
    [("sender", "10.255.0.99", False), ("endpoint", "10.255.0.99", False),
     ("tunnel_id", 101, False), ("type", 19, False),
     ("extended_tunnel_id_hex", "0aff0063", False), ("lsp_id", 2, True)],
)
def test_scheduler_binds_owned_plsp_to_tunnel_identity(tmp_path, monkeypatch, field, value, accepted):
    computer, delegated = adapter(tmp_path, monkeypatch)

    async def run():
        server = PcepServer(PcepServerConfig((PEER,), port=0),
                            path_computer=computer, delegated_computer=delegated)
        original = initial_report()
        original["ero"] = [{"address": "10.0.13.3"}]
        changed = copy.deepcopy(original)
        # Isolate the scheduler's decoded-identity comparison from wire parsing.
        changed["identifiers"][field] = value
        changed["delegation_accepted"] = False
        connection = SimpleNamespace(
            state={"synchronized": True, "reports": {report_key(changed): changed}, "updates": {}},
            terminal=False, computation=None, owned_plsps={original["plsp_id"]: ownership_key(original)},
            delegation_attempts=set(), srp_sequence=0, request_body=None,
        )
        computed = []

        async def compute(_peer, _connection, reports, _srps):
            computed.extend(reports)

        monkeypatch.setattr(server, "_compute_delegations", compute)
        server._schedule_delegations(PEER, connection)
        if connection.computation is not None:
            await connection.computation
        assert changed["delegation_accepted"] is accepted
        assert bool(computed) is not accepted
        assert connection.owned_plsps == {original["plsp_id"]: ownership_key(original)}

    asyncio.run(run())


class _BlockedDelegatedComputer:
    def __init__(self, delegate):
        self.delegate = delegate
        self.allowed_tunnels = delegate.allowed_tunnels
        self.started = threading.Event()
        self.release = threading.Event()

    def __call__(self, peer, reports, srps):
        self.started.set()
        if not self.release.wait(5):
            raise RuntimeError("Test worker latch was not released")
        return self.delegate(peer, reports, srps)

    def validate_reply(self, reply):
        return self.delegate.validate_reply(reply)


def _changed_pending_body(change):
    result = b""
    for obj in parse_objects(initial_body()):
        body = obj.body
        if obj.object_class == 33:
            if change == "srp_id":
                body = body[:4] + struct.pack("!I", 123) + body[8:]
            elif change == "pst":
                body = body[:8] + struct.pack("!HHI", 28, 4, 1)
        elif obj.object_class == 32 and change == "lsp_tlv":
            body += struct.pack("!HH", 65000, 0)
        result += encode_object(obj.object_class, body, obj.object_type)
    return result


@pytest.mark.parametrize("change", ["srp_id", "pst", "lsp_tlv"])
def test_changed_report_while_worker_pending_cannot_deliver_old_update(tmp_path, monkeypatch, change):
    computer, delegated = adapter(tmp_path, monkeypatch, "127.0.0.1")
    blocked = _BlockedDelegatedComputer(delegated)

    async def run():
        server = PcepServer(PcepServerConfig(("127.0.0.1",), port=0, keepalive=1, deadtimer=5),
                            path_computer=computer, delegated_computer=blocked)
        await server.start()
        reader, writer = await connect(server)
        try:
            writer.write(encode_message(PCRPT, initial_body()))
            await writer.drain()
            await _eventually(blocked.started.is_set)
            connection = server._connections["127.0.0.1"]
            key = report_key(initial_report())
            before = report_fingerprint(connection.state["reports"][key])
            writer.write(encode_message(PCRPT, _changed_pending_body(change)))
            await writer.drain()
            await _eventually(lambda: report_fingerprint(connection.state["reports"][key]) != before)
            blocked.release.set()
            await _eventually(lambda: not server._workers and connection.computation.done())
            assert not connection.state["updates"] and not connection.owned_plsps
            assert not any(event["direction"] == "tx" and event["message_type"] == 11
                           for event in server.snapshot()["events"])
            assert server.snapshot()["peers"]["127.0.0.1"]["state"] == "ESTABLISHED"
        finally:
            blocked.release.set()
            await server.close()
            writer.close()
            await writer.wait_closed()

    asyncio.run(run())


def test_returned_delegation_without_ack_does_not_expire_positive_path_timer(tmp_path, monkeypatch):
    computer, delegated = adapter(tmp_path, monkeypatch, "127.0.0.1")

    async def run():
        server = PcepServer(
            PcepServerConfig(("127.0.0.1",), port=0, keepalive=1, deadtimer=5, update_ack_timeout=0.1),
            path_computer=computer, delegated_computer=delegated,
        )
        await server.start()
        reader, writer = await connect(server)
        try:
            active = b""
            for obj in parse_objects(initial_body()):
                body = obj.body
                if obj.object_class == 32:
                    body = (int.from_bytes(body[:4], "big") | 0x20).to_bytes(4, "big") + body[4:]
                active += encode_object(obj.object_class, body, obj.object_type)
            writer.write(encode_message(PCRPT, active))
            await writer.drain()
            returned = await _read(reader)
            assert returned[1] == 11
            assert not parse_reports(returned[4:])[0]["delegate"]
            # No acknowledgment is sent: after the timer deadline the next
            # server frame must remain a keepalive, rather than CLOSE.
            assert await _read(reader, 2) == encode_message(KEEPALIVE)
            peer = server.snapshot()["peers"]["127.0.0.1"]
            assert peer["state"] == "ESTABLISHED"
            assert peer["updates"]["1"]["status"] == "DELEGATION_RETURNED"
            assert not peer["updates"]["1"]["acknowledged"]
            assert not server._connections["127.0.0.1"].owned_plsps
        finally:
            await server.close()
            writer.close()
            await writer.wait_closed()

    asyncio.run(run())
