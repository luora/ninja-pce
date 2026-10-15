"""Session isolation and bounded initial delegation under headend policy."""
from __future__ import annotations

import asyncio
import copy
import struct
import time
from types import SimpleNamespace

import pytest
from test_pcep import _eventually, _read
from test_pcep_compute import PEER, prepare
from test_pcep_delegation import adapter, connect, initial_body, initial_report

from ninja_pce.pcep import (
    PCRPT,
    PcepServer,
    PcepServerConfig,
    encode_message,
    encode_object,
    encode_open,
    parse_objects,
    parse_reports,
    parse_tlvs,
)
from ninja_pce.pcep_delegation import DelegatedPathComputer, ownership_key
from ninja_pce.pcep_headend_policy import HeadendDelegationPolicy


def _body(tunnel: int, *, plsp: int | None = None, incarnation: int = 1, sync: bool = False) -> bytes:
    result = b""
    for obj in parse_objects(initial_body()):
        body = obj.body
        if obj.object_class == 32:
            body = struct.pack("!I", ((plsp or tunnel) << 12) | 9 | (2 if sync else 0))
            for kind, raw in parse_tlvs(obj.body[4:]):
                if kind == 17:
                    raw = f"Tunnel{tunnel}_w".encode()
                elif kind == 18:
                    raw = raw[:4] + struct.pack("!HH", incarnation, tunnel) + raw[8:]
                body += struct.pack("!HH", kind, len(raw)) + raw + bytes(-len(raw) % 4)
        result += encode_object(obj.object_class, body, obj.object_type)
    return result


def _active(body: bytes) -> bytes:
    result = b""
    for obj in parse_objects(body):
        raw = obj.body
        if obj.object_class == 32:
            raw = (int.from_bytes(raw[:4], "big") | 0x20).to_bytes(4, "big") + raw[4:]
        result += encode_object(obj.object_class, raw, obj.object_type)
    return result


def _removed(body: bytes) -> bytes:
    result = b""
    for obj in parse_objects(body):
        raw = obj.body
        if obj.object_class == 32:
            raw = ((int.from_bytes(raw[:4], "big") & ~1) | 4).to_bytes(4, "big") + raw[4:]
        result += encode_object(obj.object_class, raw, obj.object_type)
    return result


async def _next_update(reader):
    deadline = time.monotonic() + 5
    while True:
        frame = await _read(reader, max(0.01, deadline - time.monotonic()))
        if frame[1] == 11:
            return frame


class _LoopbackPolicy:
    """Transport alias keeps production policy's unicast-peer check intact."""
    def __init__(self, delegated):
        self.delegated = delegated
        self.allowed_tunnels = {}
        self.authorized_peers = frozenset({"127.0.0.1"})
        self.headend_policy = delegated.headend_policy

    def __call__(self, peer, reports, srps):
        assert peer == "127.0.0.1"
        return self.delegated(PEER, reports, srps)

    def validate_reply(self, reply):
        return self.delegated.validate_reply(reply)

    def validate_authorization(self, peer, report, outcome):
        return self.delegated.validate_authorization(PEER, report, outcome)

    def concurrent_limit(self, peer):
        return self.delegated.concurrent_limit(PEER)

    def adopt_active(self, peer, report):
        return self.delegated.adopt_active(PEER, report)


def _policy_adapter(tmp_path, monkeypatch, *, limit=32):
    computer = prepare(tmp_path, monkeypatch)
    computer.headends["127.0.0.1"] = PEER
    policy = HeadendDelegationPolicy.from_document({"version": 1, "headends": {
        PEER: {"source": PEER, "destinations": ["10.255.0.6"],
               "max_bandwidth_bps": 100_000_000, "max_live_lsps": limit},
    }}, computer.headends)
    delegated = DelegatedPathComputer(computer, {}, headend_policy=policy)
    return computer, _LoopbackPolicy(delegated)


def test_refusal_churn_from_rsvp_instances_does_not_consume_shared_session(tmp_path, monkeypatch):
    computer, delegated = adapter(tmp_path, monkeypatch, "127.0.0.1")

    async def run():
        server = PcepServer(PcepServerConfig(("127.0.0.1",), port=0, keepalive=1, deadtimer=5),
                            path_computer=computer, delegated_computer=delegated)
        await server.start()
        reader, writer = await connect(server)
        try:
            writer.write(encode_message(PCRPT, _body(999)))
            await writer.drain()
            assert not parse_reports((await _read(reader))[4:])[0]["delegate"]
            for incarnation in range(2, 41):
                writer.write(encode_message(PCRPT, _body(999, incarnation=incarnation)))
            await writer.drain()
            await _eventually(lambda: len(server.snapshot()["peers"]["127.0.0.1"]["reports"]) == 40)
            connection = server._connections["127.0.0.1"]
            assert len(connection.delegation_attempts) == 1
            assert len(connection.state["updates"]) == 1
            assert connection.state["state"] == "ESTABLISHED"
            # An unrelated authorized tunnel still receives its initial path.
            writer.write(encode_message(PCRPT, _body(100)))
            await writer.drain()
            response = parse_reports((await _read(reader))[4:], allow_delegation=True)[0]
            assert response["delegate"] and response["identifiers"]["tunnel_id"] == 100
        finally:
            await server.close()
            writer.close()
            await writer.wait_closed()

    asyncio.run(run())


def test_many_refusals_archive_terminal_history_without_resetting_srp(tmp_path, monkeypatch):
    computer, delegated = adapter(tmp_path, monkeypatch, "127.0.0.1")

    async def run():
        server = PcepServer(PcepServerConfig(("127.0.0.1",), port=0, keepalive=1, deadtimer=5),
                            path_computer=computer, delegated_computer=delegated)
        await server.start()
        reader, writer = await connect(server)
        try:
            for index in range(70):
                writer.write(encode_message(PCRPT, _body(200 + index)))
                await writer.drain()
                response = parse_reports((await _read(reader))[4:])[0]
                assert response["srp_id"] == index + 1 and not response["delegate"]
            state = server.snapshot()["peers"]["127.0.0.1"]
            assert state["state"] == "ESTABLISHED"
            assert len(state["updates"]) == len(state["update_archive"]) == 32
            assert list(state["updates"]) == [str(value) for value in range(39, 71)]
            assert [item["srp_id"] for item in state["update_archive"]] == list(range(7, 39))
            writer.write(encode_message(PCRPT, _body(100)))
            await writer.drain()
            response = parse_reports((await _read(reader))[4:], allow_delegation=True)[0]
            assert response["delegate"] and response["srp_id"] == 71
        finally:
            await server.close()
            writer.close()
            await writer.wait_closed()

    asyncio.run(run())


@pytest.mark.parametrize("status,acknowledged,latest,plan,age,removable", [
    ("INITIAL_PATH", False, "ACTIVE", None, 1, False),
    ("INITIAL_PATH", True, "DOWN", None, 1, False),
    ("ACK_TIMEOUT", True, "ACTIVE", None, 1, False),
    ("INITIAL_PATH", True, "ACTIVE", "pending-plan", 1, False),
    ("INITIAL_PATH", True, "ACTIVE", "expired-plan", 121, True),
    ("INITIAL_PATH", True, "ACTIVE", None, 1, True),
    ("INITIAL_PATH", True, "REMOVED", None, 1, True),
    ("PCC_ERROR", True, None, None, 1, True),
    ("DELEGATION_RETURNED", False, None, None, 1, True),
])
def test_update_room_preserves_uncertainty_and_provisioning_evidence(status, acknowledged, latest, plan, age, removable):
    record = {"status": status, "acknowledged": acknowledged, "latest_reported_state": latest,
              "provisioning_plan_id": plan, "sent_at": time.time() - age}
    connection = SimpleNamespace(state={"updates": {str(i): copy.deepcopy(record) for i in range(1, 33)}})
    before = copy.deepcopy(connection.state)
    assert PcepServer._make_update_room(connection) is removable
    if removable:
        assert len(connection.state["updates"]) == 31
        assert connection.state["update_archive"][0]["srp_id"] == 1
    else:
        assert connection.state == before


def test_attempt_identity_ignores_rsvp_id_but_changes_with_profile():
    original = initial_report()
    changed = copy.deepcopy(original)
    changed["identifiers"]["lsp_id"] += 1
    changed["identifiers"]["value_hex"] = "different-instance"
    assert PcepServer._delegation_attempt_key(original) == PcepServer._delegation_attempt_key(changed)
    changed["attributes"][0]["body_hex"] += "00"
    assert PcepServer._delegation_attempt_key(original) != PcepServer._delegation_attempt_key(changed)


def test_policy_accepts_arbitrary_tunnel_numbers_and_enforces_shared_peer_cap(tmp_path, monkeypatch):
    computer, delegated = _policy_adapter(tmp_path, monkeypatch, limit=2)

    async def run():
        server = PcepServer(PcepServerConfig(("127.0.0.1",), port=0, keepalive=1, deadtimer=5),
                            path_computer=computer, delegated_computer=delegated)
        await server.start()
        reader, writer = await connect(server)
        try:
            writer.write(encode_message(PCRPT, _body(1003) + _body(10030) + _body(59999)))
            await writer.drain()
            responses = [parse_reports((await _next_update(reader))[4:], allow_delegation=True)[0] for _ in range(2)]
            assert [item["identifiers"]["tunnel_id"] for item in responses] == [1003, 10030]
            assert all(item["delegate"] for item in responses)
            state = server.snapshot()["peers"]["127.0.0.1"]
            assert len(state["updates"]) == 2
            assert state["state"] == "ESTABLISHED"
            # Capacity exhaustion is temporary. Removing another ownership
            # admits the same waiting tunnel without a session restart.
            writer.write(encode_message(PCRPT, _removed(_body(1003))))
            await writer.drain()
            resumed = parse_reports((await _next_update(reader))[4:], allow_delegation=True)[0]
            assert resumed["delegate"] and resumed["identifiers"]["tunnel_id"] == 59999
            assert server.snapshot()["peers"]["127.0.0.1"]["state"] == "ESTABLISHED"
        finally:
            await server.close()
            writer.close()
            await writer.wait_closed()

    asyncio.run(run())


def test_active_policy_report_is_adopted_without_an_update(tmp_path, monkeypatch):
    computer, delegated = _policy_adapter(tmp_path, monkeypatch)
    initial = parse_reports(_body(10030), allow_delegation=True)[0]
    computed = delegated("127.0.0.1", [initial], (19,))
    active = _active(computed.messages[0][4:])

    async def run():
        server = PcepServer(PcepServerConfig(("127.0.0.1",), port=0, keepalive=1, deadtimer=5),
                            path_computer=computer, delegated_computer=delegated)
        await server.start()
        reader, writer = await connect(server)
        try:
            writer.write(encode_message(PCRPT, active))
            await writer.drain()
            await _eventually(lambda: any(item["delegation_accepted"] for item in
                                         server._connections["127.0.0.1"].state["reports"].values()))
            connection = server._connections["127.0.0.1"]
            assert not connection.state["updates"]
            assert not connection.delegation_attempts and connection.srp_sequence == 0
            assert all(item["delegation_accepted"] for item in connection.state["reports"].values())
            assert not connection.owned_plsps
        finally:
            await server.close()
            writer.close()
            await writer.wait_closed()

    asyncio.run(run())


def test_historical_sync_active_report_cannot_establish_policy_ownership(tmp_path, monkeypatch):
    computer, delegated = _policy_adapter(tmp_path, monkeypatch)
    original = parse_reports(_body(10030), allow_delegation=True)[0]
    active = _active(delegated("127.0.0.1", [original], (19,)).messages[0][4:])
    historical = b""
    for obj in parse_objects(active):
        raw = obj.body
        if obj.object_class == 32:
            raw = (int.from_bytes(raw[:4], "big") | 2).to_bytes(4, "big") + raw[4:]
        historical += encode_object(obj.object_class, raw, obj.object_type)

    async def run():
        server = PcepServer(PcepServerConfig(("127.0.0.1",), port=0, keepalive=1, deadtimer=5),
                            path_computer=computer, delegated_computer=delegated)
        await server.start()
        reader, writer = await asyncio.open_connection("127.0.0.1", server.addresses[0][1])
        try:
            await _read(reader)
            writer.write(encode_open(1, 5, updates=True) + encode_message(2))
            await writer.drain()
            await _read(reader)
            eos = bytes.fromhex("2010001c00000000001200100000000000000000000000000000000007100004")
            writer.write(encode_message(PCRPT, historical + eos))
            await writer.drain()
            await _eventually(lambda: server._connections["127.0.0.1"].state["synchronized"])
            connection = server._connections["127.0.0.1"]
            assert not connection.owned_plsps and not connection.state["updates"]
            assert not connection.delegation_attempts
            writer.write(encode_message(PCRPT, active))
            await writer.drain()
            await _eventually(lambda: any(item["delegation_accepted"] for item in connection.state["reports"].values()))
            assert not connection.state["updates"]
            assert not connection.owned_plsps
        finally:
            await server.close()
            writer.close()
            await writer.wait_closed()

    asyncio.run(run())


def test_policy_quota_counts_unique_ownerships_and_protects_managed_tunnels(tmp_path, monkeypatch):
    computer, delegated = _policy_adapter(tmp_path, monkeypatch, limit=2)
    server = PcepServer(PcepServerConfig(("127.0.0.1",), port=0),
                        path_computer=computer, delegated_computer=delegated)
    reports = [parse_reports(_body(number, incarnation=instance), allow_delegation=True)[0]
               for number, instance in ((10, 1), (10, 2), (1002, 1), (10030, 1))]
    reports[0]["received_at"] = 1
    reports[1]["received_at"] = 2
    connection = SimpleNamespace(state={"reports": dict(enumerate(reports))}, owned_plsps={})
    planner = SimpleNamespace(matches=lambda peer, report: report["identifiers"]["tunnel_id"] == 1002)
    monkeypatch.setattr(server, "_recovery_planners", lambda: (planner,))
    selected = server._policy_selection("127.0.0.1", connection)
    assert selected == {ownership_key(reports[1]), ownership_key(reports[2])}


@pytest.mark.parametrize("networks", [(), ("0.0.0.0/0",), ("127.0.0.1",), ("10.0.0.1/24",),
                                        ("::1/128",), ("224.0.0.0/4",), ("10.0.0.0/8", "10.0.0.0/8")])
def test_dynamic_transport_scope_requires_bounded_canonical_ipv4_networks(networks):
    with pytest.raises(ValueError):
        PcepServerConfig((), allowed_peer_networks=networks)


def test_network_transport_scope_requires_domain_authority():
    with pytest.raises(ValueError, match="topology-domain authority"):
        PcepServer(PcepServerConfig((), allowed_peer_networks=("10.0.0.0/8",)))


@pytest.mark.parametrize("approved,scope", [(True, "127.0.0.1/32"), (False, "127.0.0.1/32"), (True, "10.0.0.0/8")])
def test_cidr_candidate_must_pass_current_domain_validation_before_open(approved, scope):
    calls = []

    def validate(peer):
        calls.append(peer)
        return approved

    computer = SimpleNamespace(domain_policy=object(), validated_peer=validate)

    async def run():
        server = PcepServer(PcepServerConfig((), port=0, allowed_peer_networks=(scope,)), path_computer=computer)
        await server.start()
        reader, writer = await asyncio.open_connection("127.0.0.1", server.addresses[0][1])
        try:
            if approved and scope.startswith("127."):
                assert (await _read(reader))[1] == 1
                assert calls == ["127.0.0.1"]
            else:
                assert await asyncio.wait_for(reader.read(), 2) == b""
                assert calls == (["127.0.0.1"] if scope.startswith("127.") else [])
                assert server.snapshot()["rejected_connections"] == 1
        finally:
            writer.close()
            await writer.wait_closed()
            await server.close()

    asyncio.run(run())


def test_initial_path_waits_for_current_domain_then_retries_without_reconnect(tmp_path, monkeypatch):
    computer, delegated = _policy_adapter(tmp_path, monkeypatch)
    available = {"value": False}
    delegated.domain_enabled = True
    delegated.computer = SimpleNamespace(validated_peer=lambda peer: available["value"])

    async def run():
        server = PcepServer(PcepServerConfig(("127.0.0.1",), port=0, keepalive=1, deadtimer=5),
                            path_computer=computer, delegated_computer=delegated)
        await server.start()
        reader, writer = await connect(server)
        try:
            writer.write(encode_message(PCRPT, _body(10030)))
            await writer.drain()
            connection = server._connections["127.0.0.1"]
            await _eventually(lambda: bool(connection.delegation_deferred))
            assert not connection.state["updates"] and not connection.delegation_attempts
            available["value"] = True
            resumed = parse_reports((await _next_update(reader))[4:], allow_delegation=True)[0]
            assert resumed["delegate"] and resumed["srp_id"] == 2
            assert connection.state["state"] == "ESTABLISHED"
        finally:
            await server.close()
            writer.close()
            await writer.wait_closed()

    asyncio.run(run())


def test_acknowledged_positive_history_does_not_limit_long_lived_peer(tmp_path, monkeypatch):
    computer, delegated = _policy_adapter(tmp_path, monkeypatch, limit=64)

    async def run():
        server = PcepServer(PcepServerConfig(("127.0.0.1",), port=0, keepalive=1, deadtimer=5),
                            path_computer=computer, delegated_computer=delegated)
        await server.start()
        reader, writer = await connect(server)
        try:
            for index in range(40):
                writer.write(encode_message(PCRPT, _body(1000 + index)))
                await writer.drain()
                reply = await _next_update(reader)
                report = parse_reports(reply[4:], allow_delegation=True)[0]
                assert report["delegate"] and report["srp_id"] == index + 1
                writer.write(encode_message(PCRPT, _active(reply[4:])))
                await writer.drain()
                await _eventually(lambda: server._connections["127.0.0.1"].state["updates"][str(index + 1)]["acknowledged"])
            connection = server._connections["127.0.0.1"]
            assert len(connection.state["updates"]) == 32
            assert len(connection.state["update_archive"]) == 8
            assert len(connection.owned_plsps) == 40
            assert connection.srp_sequence == 40
            assert connection.state["state"] == "ESTABLISHED"
        finally:
            await server.close()
            writer.close()
            await writer.wait_closed()

    asyncio.run(run())


def test_many_tunnel_lifecycles_retire_work_and_name_history_with_small_live_bound(tmp_path, monkeypatch):
    computer, delegated = _policy_adapter(tmp_path, monkeypatch, limit=2)

    async def run():
        server = PcepServer(PcepServerConfig(("127.0.0.1",), port=0, keepalive=1, deadtimer=5,
                                              max_reports_per_peer=2),
                            path_computer=computer, delegated_computer=delegated)
        await server.start()
        reader, writer = await connect(server)
        try:
            connection = server._connections["127.0.0.1"]
            for index in range(40):
                body = _body(1000 + index)
                writer.write(encode_message(PCRPT, body))
                await writer.drain()
                reply = await _next_update(reader)
                assert parse_reports(reply[4:], allow_delegation=True)[0]["delegate"]
                writer.write(encode_message(PCRPT, _active(reply[4:])))
                await writer.drain()
                await _eventually(lambda: connection.state["updates"][str(index + 1)]["acknowledged"])
                writer.write(encode_message(PCRPT, _removed(body)))
                await writer.drain()
                await _eventually(lambda: not connection.state["reports"])
                assert not connection.delegation_attempts and not connection.owned_plsps
                assert len(connection.state["lsp_names"]) <= 2
            assert connection.srp_sequence == 40 and len(connection.state["updates"]) == 32
            assert len(connection.state["update_archive"]) == 8
            assert connection.state["state"] == "ESTABLISHED"
        finally:
            await server.close()
            writer.close()
            await writer.wait_closed()

    asyncio.run(run())


def test_removed_lsp_cannot_retire_uncertain_positive_attempt_or_name(tmp_path, monkeypatch):
    computer, delegated = _policy_adapter(tmp_path, monkeypatch, limit=2)

    async def run():
        server = PcepServer(PcepServerConfig(("127.0.0.1",), port=0, keepalive=1, deadtimer=5,
                                              max_reports_per_peer=2),
                            path_computer=computer, delegated_computer=delegated)
        await server.start()
        reader, writer = await connect(server)
        try:
            body = _body(10030)
            writer.write(encode_message(PCRPT, body))
            await writer.drain()
            assert (await _next_update(reader))[1] == 11
            connection = server._connections["127.0.0.1"]
            pending = copy.deepcopy(connection.state["updates"]["1"])
            writer.write(encode_message(PCRPT, _removed(body)))
            await writer.drain()
            await _eventually(lambda: not connection.state["reports"])
            assert connection.delegation_attempts == {pending["delegation_attempt_key"]}
            # New RSVP incarnation is the same uncertain request, so cannot
            # consume another update or be mistaken for a new authorization.
            writer.write(encode_message(PCRPT, _body(10030, incarnation=2)))
            await writer.drain()
            await _eventually(lambda: bool(connection.state["reports"]))
            assert connection.state["updates"] == {"1": pending}
            assert connection.srp_sequence == 1
            assert "10030" in connection.state["lsp_names"]
        finally:
            await server.close()
            writer.close()
            await writer.wait_closed()

    asyncio.run(run())
