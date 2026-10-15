"""Exact journal ownership and writer fencing for CLI-provisioned tunnels."""
from __future__ import annotations

import asyncio
import copy
import json
import struct
import threading
from datetime import datetime, timedelta, timezone
from ipaddress import IPv4Address

import pytest
from test_pcep import _eventually, _read
from test_pcep_compute import PEER, prepare, request
from test_pcep_delegation import connect, initial_report

from ninja_pce.pcep import (
    PCRPT,
    PcepServer,
    PcepServerConfig,
    encode_message,
    encode_object,
    parse_objects,
    parse_reports,
)
from ninja_pce.pcep_delegation import DelegatedPathComputer, ownership_key
from ninja_pce.pcep_provisioning import JournalProvisioningAuthorization, configured_initial_path_observed
from ninja_pce.reservations import resource_lock
from ninja_pce.state import digest, load_state, write_state


def report_for(tunnel_id=20000):
    report = initial_report()
    name = f"Tunnel{tunnel_id}_w"
    ids = report["identifiers"]
    ids["tunnel_id"] = tunnel_id
    ids["value_hex"] = (IPv4Address(ids["sender"]).packed + struct.pack("!HH", ids["lsp_id"], tunnel_id) +
                        bytes.fromhex(ids["extended_tunnel_id_hex"]) + IPv4Address(ids["endpoint"]).packed).hex()
    report.update(symbolic_name=name, symbolic_name_hex=name.encode().hex())
    for item in report["tlvs"]:
        if item["type"] == 18:
            item["value_hex"] = ids["value_hex"]
        elif item["type"] == 17:
            item["value_hex"] = name.encode().hex()
    for item in report["attributes"]:
        if item["class"] == 5:
            item["body_hex"] = struct.pack("!f", 125000).hex()
        elif item["class"] == 9:
            item["body_hex"] = (bytes(12) + bytes((7, 7, 0, 0))).hex()
    return report


def report_body(report):
    # Build a real initial PCRpt with the exact identity/name/attributes above.
    body = ((report["plsp_id"] << 12) | 9).to_bytes(4, "big")
    for tlv in report["tlvs"]:
        value = bytes.fromhex(tlv["value_hex"])
        body += struct.pack("!HH", tlv["type"], len(value)) + value + bytes(-len(value) % 4)
    return (encode_object(32, body) + encode_object(7, b"") +
            b"".join(encode_object(attr["class"], bytes.fromhex(attr["body_hex"]), attr["type"])
                     for attr in report["attributes"]))


def setup(tmp_path, monkeypatch, peer=PEER):
    computer = prepare(tmp_path, monkeypatch)
    computer.headends = {peer: PEER}
    report = report_for()
    computed = computer(peer, request(bandwidth=125000, lspa=bytes(12) + bytes((7, 7, 0, 0))))
    assert computed.outcomes[0]["status"] == "PATH"
    authorization = {
        "plan_id": "a" * 64, "peer": peer, "source_ip": PEER, "destination_ip": "10.255.0.6",
        "tunnel_id": 20000, "symbolic_name": "Tunnel20000_w", "bandwidth_bps": 1_000_000,
        "setup_priority": 7, "hold_priority": 7,
        "expires_at": (datetime.now(timezone.utc) + timedelta(seconds=60)).isoformat(),
        "ero": computed.outcomes[0]["ero"], "path_edges": computed.outcomes[0]["path_link_ids"],
    }
    journal = {"kind": "cli_tunnel_provisioning", "operations": {
        "operation-1": {"id": "operation-1", "status": "PUSHING", "authorization": authorization,
                        "authorization_digest": digest(authorization), "owner": "cli-provision:operation-1",
                        "headend": "PE1", "reservation_id": "PE1:iosxe:Tunnel20000",
                        **{key: copy.deepcopy(authorization[key])
                           for key in ("plan_id", "tunnel_id", "bandwidth_bps", "ero", "path_edges")}},
    }}
    path = tmp_path / "cli-provisioning.json"
    with resource_lock(tmp_path):
        write_state(tmp_path / ".ninja-pce-reservations.json", {"entries": {
            "PE1:iosxe:Tunnel20000": {"owner": "cli-provision:operation-1", "plan_id": authorization["plan_id"],
                                     "status": "PUSHING", "current_bandwidth_bps": 1_000_000,
                                     "current_edges": authorization["path_edges"],
                                     "resources": {edge: 1_000_000 for edge in authorization["path_edges"]}},
        }})
        write_state(path, journal)
    authorizer = JournalProvisioningAuthorization(path, computer.headends)
    delegated = DelegatedPathComputer(computer, {}, provisioning_authorizer=authorizer)
    return computer, report, journal, authorizer, delegated


@pytest.mark.parametrize("status", ["PUSHING", "APPLIED_UNVERIFIED", "VERIFIED"])
def test_exact_recent_owned_configuration_gets_only_its_approved_initial_path(tmp_path, monkeypatch, status):
    _, report, journal, authorizer, delegated = setup(tmp_path, monkeypatch)
    journal["operations"]["operation-1"]["status"] = status
    write_state(authorizer.path, journal)
    before = authorizer.path.read_bytes()
    reply = delegated(PEER, [report], (3,))
    assert reply.outcomes[0]["status"] == "INITIAL_PATH"
    assert reply.outcomes[0]["provisioning_plan_id"] == "a" * 64
    assert delegated.validate_authorization(PEER, report, reply.outcomes[0])
    parsed = parse_reports(reply.messages[0][4:], allow_delegation=True)[0]
    assert parsed["identifiers"] == report["identifiers"]
    assert [hop["address"] for hop in parsed["ero"]] == journal["operations"]["operation-1"]["authorization"]["ero"]
    assert parsed["attributes"][1]["bits_per_second"] == 1_000_000
    assert authorizer.path.read_bytes() == before


@pytest.mark.parametrize("change", ["created", "source", "destination", "tunnel", "name", "name_tlv_missing",
                                    "identity_tlv_missing", "extended_id", "raw_identity", "bandwidth",
                                    "priority", "affinity", "duplicate_bw"])
def test_unowned_or_changed_report_cannot_gain_authority(tmp_path, monkeypatch, change):
    _, report, _, authorizer, delegated = setup(tmp_path, monkeypatch)
    if change == "created":
        report["created"] = True
    elif change in {"source", "destination", "tunnel", "extended_id", "raw_identity"}:
        field = {"source": "sender", "destination": "endpoint", "tunnel": "tunnel_id",
                 "extended_id": "extended_tunnel_id_hex", "raw_identity": "value_hex"}[change]
        report["identifiers"][field] = 20001 if change == "tunnel" else "10.255.0.99"
    elif change == "name":
        report["symbolic_name_hex"] = b"Tunnel20001_w".hex()
    elif change in {"name_tlv_missing", "identity_tlv_missing"}:
        kind = 17 if change == "name_tlv_missing" else 18
        report["tlvs"] = [item for item in report["tlvs"] if item["type"] != kind]
    elif change == "duplicate_bw":
        report["attributes"].append(next(copy.deepcopy(item) for item in report["attributes"]
                                         if (item["class"], item["type"]) == (5, 1)))
    else:
        for item in report["attributes"]:
            if change == "bandwidth" and item["class"] == 5:
                item["body_hex"] = struct.pack("!f", 0).hex()
            elif change in {"priority", "affinity"} and item["class"] == 9:
                body = bytearray.fromhex(item["body_hex"])
                body[12 if change == "priority" else 0] = 1
                item["body_hex"] = body.hex()
    assert authorizer(PEER, report) is None
    reply = delegated(PEER, [report], (1,))
    assert reply.outcomes[0]["status"] == "DELEGATION_RETURNED"
    assert next(obj.body for obj in parse_objects(reply.messages[0][4:]) if obj.object_class == 7) == b""


@pytest.mark.parametrize("field,value", [("operational_code", 2), ("ero", [{"address": "10.0.13.3"}]),
                                        ("srp_id", 1), ("sync", True), ("delegate", False),
                                        ("administrative_up", False), ("removed", True)])
def test_authorizer_itself_only_accepts_unsolicited_live_initial_delegation(tmp_path, monkeypatch, field, value):
    _, report, _, authorizer, _ = setup(tmp_path, monkeypatch)
    report[field] = value
    assert authorizer(PEER, report) is None


@pytest.mark.parametrize("change", ["missing", "checksum", "authorization_digest", "wrong_kind", "expired",
                                    "far_future", "naive_expiry", "rollback", "planned", "duplicate", "oversize",
                                    "malformed", "wrong_peer", "wrong_name", "extra_field"])
def test_absent_stale_ambiguous_or_corrupt_journal_fails_closed(tmp_path, monkeypatch, change):
    _, report, journal, authorizer, _ = setup(tmp_path, monkeypatch)
    operation = journal["operations"]["operation-1"]
    authorization = operation["authorization"]
    if change == "missing":
        authorizer.path.unlink()
    elif change == "checksum":
        document = json.loads(authorizer.path.read_text())
        document["checksum"] = "invalid"
        authorizer.path.write_text(json.dumps(document))
    elif change == "oversize":
        authorizer.path.write_text(" " * 1_048_577)
    else:
        if change == "authorization_digest":
            operation["authorization_digest"] = "invalid"
        elif change == "wrong_kind":
            journal["kind"] = "lab_pcep_creations"
        elif change in {"expired", "far_future", "naive_expiry"}:
            offset = -1 if change == "expired" else 300 if change == "far_future" else 60
            expiry = datetime.now(timezone.utc) + timedelta(seconds=offset)
            authorization["expires_at"] = (expiry.replace(tzinfo=None) if change == "naive_expiry" else expiry).isoformat()
        elif change in {"rollback", "planned"}:
            operation["status"] = "ROLLBACK_REQUIRED" if change == "rollback" else "PLANNED"
        elif change == "duplicate":
            journal["operations"]["operation-2"] = {**copy.deepcopy(operation), "id": "operation-2"}
        elif change == "malformed":
            authorization["expires_at"] = None
        elif change == "wrong_peer":
            authorization["peer"] = "10.255.0.99"
        elif change == "wrong_name":
            authorization["symbolic_name"] = "Tunnel20001_w"
        elif change == "extra_field":
            authorization["ignored"] = True
        if change != "authorization_digest":
            operation["authorization_digest"] = digest(authorization)
        write_state(authorizer.path, journal)
    assert authorizer(PEER, report) is None


def test_changed_approved_path_refuses_update_and_static_tunnel100_remains_independent(tmp_path, monkeypatch):
    computer, report, journal, authorizer, delegated = setup(tmp_path, monkeypatch)
    operation = journal["operations"]["operation-1"]
    operation["authorization"]["ero"] = ["10.0.12.2", "10.0.25.5", "10.0.56.6"]
    operation["ero"] = operation["authorization"]["ero"]
    operation["authorization_digest"] = digest(operation["authorization"])
    write_state(authorizer.path, journal)
    assert authorizer(PEER, report) is not None
    assert delegated(PEER, [report], (1,)).outcomes[0]["status"] == "DELEGATION_RETURNED"
    authorizer.path.write_text("corrupt state")
    static = DelegatedPathComputer(computer, {PEER: (100,)}, provisioning_authorizer=authorizer)
    assert static(PEER, [initial_report()], (2,)).outcomes[0]["status"] == "INITIAL_PATH"
    assert static.allowed_tunnels == {PEER: (100,)}


@pytest.mark.parametrize("change", ["missing_lock", "missing_ledger", "corrupt_ledger", "hold_owner", "hold_plan",
                                    "hold_status", "hold_bandwidth", "hold_edges", "hold_resources", "op_owner",
                                    "op_reservation", "op_plan", "op_bandwidth", "op_edges", "op_ero"])
def test_journal_without_exact_live_owned_hold_grants_no_path_authority(tmp_path, monkeypatch, change):
    _, report, journal, authorizer, _ = setup(tmp_path, monkeypatch)
    ledger_path = tmp_path / ".ninja-pce-reservations.json"
    ledger = load_state(ledger_path)
    hold = ledger["entries"]["PE1:iosxe:Tunnel20000"]
    operation = journal["operations"]["operation-1"]
    if change == "missing_lock":
        (tmp_path / ".ninja-pce-resources.lock").unlink()
    elif change == "missing_ledger":
        ledger_path.unlink()
    elif change == "corrupt_ledger":
        ledger_path.write_text("corrupt")
    elif change.startswith("hold_"):
        field = {"hold_owner": "owner", "hold_plan": "plan_id", "hold_status": "status",
                 "hold_bandwidth": "current_bandwidth_bps", "hold_edges": "current_edges",
                 "hold_resources": "resources"}[change]
        hold[field] = 0 if change == "hold_bandwidth" else [] if change == "hold_edges" else (
            {} if change == "hold_resources" else "different")
        write_state(ledger_path, ledger)
    else:
        field = {"op_owner": "owner", "op_reservation": "reservation_id", "op_plan": "plan_id",
                 "op_bandwidth": "bandwidth_bps", "op_edges": "path_edges", "op_ero": "ero"}[change]
        operation[field] = 0 if change == "op_bandwidth" else [] if change in {"op_edges", "op_ero"} else "different"
        write_state(authorizer.path, journal)
    before = {path.name: path.read_bytes() for path in tmp_path.iterdir() if path.is_file()}
    assert authorizer(PEER, report) is None
    assert before == {path.name: path.read_bytes() for path in tmp_path.iterdir() if path.is_file()}


def test_snapshot_identifies_exact_provisioning_policy_and_rejects_stateless_peer(tmp_path, monkeypatch):
    computer, _, _, authorizer, delegated = setup(tmp_path, monkeypatch)
    server = PcepServer(PcepServerConfig((PEER,), port=0), path_computer=computer, delegated_computer=delegated)
    snapshot = server.snapshot()
    assert snapshot["capabilities"]["configuration_provisioning"] is True
    assert snapshot["capabilities"]["lsp_initiation"] is False
    assert snapshot["provisioning_policy_digest"] == digest({"journal_path": str(authorizer.path.resolve()),
                                                           "headends": {PEER: PEER}})
    with pytest.raises(ValueError, match="stateful"):
        PcepServer(PcepServerConfig((PEER,), port=0, stateless_peers=(PEER,)),
                   path_computer=computer, delegated_computer=delegated)


def test_socket_owned_configuration_receives_same_writer_initial_update(tmp_path, monkeypatch):
    computer, report, journal, authorizer, delegated = setup(tmp_path, monkeypatch, "127.0.0.1")
    operation = journal["operations"]["operation-1"]
    operation["created_at"] = datetime.now(timezone.utc).isoformat()

    async def run():
        server = PcepServer(PcepServerConfig(("127.0.0.1",), port=0, keepalive=1, deadtimer=5),
                            path_computer=computer, delegated_computer=delegated)
        await server.start()
        reader, writer = await connect(server)
        try:
            writer.write(encode_message(PCRPT, report_body(report)))
            await writer.drain()
            wire = await _read(reader)
            update = parse_reports(wire[4:], allow_delegation=True)[0]
            assert update["delegate"] and update["identifiers"]["tunnel_id"] == 20000
            assert update["attributes"][1]["bits_per_second"] == 1_000_000
            assert len(server._connections["127.0.0.1"].owned_plsps) == 1
            assert server.snapshot()["peers"]["127.0.0.1"]["updates"]["1"]["provisioning_plan_id"] == "a" * 64
            assert not configured_initial_path_observed(server.snapshot(), operation,
                                                        policy_digest=authorizer.policy_digest)
            acknowledgment = b""
            for obj in parse_objects(wire[4:]):
                body = obj.body
                if obj.object_class == 32:
                    body = (int.from_bytes(body[:4], "big") | 0x20).to_bytes(4, "big") + body[4:]
                acknowledgment += encode_object(obj.object_class, body, obj.object_type)
            writer.write(encode_message(PCRPT, acknowledgment))
            await writer.drain()
            await _eventually(lambda: server.snapshot()["peers"]["127.0.0.1"]["updates"]["1"]["acknowledged"])
            assert configured_initial_path_observed(server.snapshot(), operation,
                                                    policy_digest=authorizer.policy_digest)
        finally:
            await server.close()
            writer.close()
            await writer.wait_closed()

    asyncio.run(run())


def completion_proof(tmp_path, monkeypatch):
    _, report, journal, authorizer, delegated = setup(tmp_path, monkeypatch)
    reply = delegated(PEER, [report], (17,))
    current = parse_reports(reply.messages[0][4:], allow_delegation=True)[0]
    timestamp = datetime.now(timezone.utc).timestamp()
    current.update(operational_code=2, operational_state="ACTIVE", stale=False,
                   received_at=timestamp - .1, session_id="current-session")
    operation = journal["operations"]["operation-1"]
    operation["created_at"] = datetime.fromtimestamp(timestamp - 1, timezone.utc).isoformat()
    update = {**reply.outcomes[0], "sent_at": timestamp - .5, "acknowledged": True,
              "acknowledged_at": timestamp - .25, "latest_reported_state": "ACTIVE",
              "acknowledged_identifiers": copy.deepcopy(current["identifiers"]),
              "ownership_key": list(ownership_key(current))}
    capture = {"listener_running": True, "capture_error": None, "generated_at": timestamp,
               "capabilities": {"configuration_provisioning": True},
               "provisioning_policy_digest": authorizer.policy_digest,
               "peers": {PEER: {"state": "ESTABLISHED", "fresh": True, "synchronized": True,
                                "session_id": "current-session", "valid_until": timestamp + 60,
                                "reports": {"owned": current}, "updates": {"17": update}}}}
    return capture, operation, authorizer.policy_digest, timestamp


def test_completed_owned_update_remains_observable_after_write_authority_expires(tmp_path, monkeypatch):
    capture, operation, policy, timestamp = completion_proof(tmp_path, monkeypatch)
    operation["authorization"]["expires_at"] = datetime.fromtimestamp(timestamp - .2, timezone.utc).isoformat()
    operation["authorization_digest"] = digest(operation["authorization"])
    assert configured_initial_path_observed(capture, operation, policy_digest=policy, now=timestamp)


@pytest.mark.parametrize("change", ["disconnected", "stale", "unsynchronized", "policy", "no_updates", "plan",
                                    "pc_rep", "unacknowledged", "old_incarnation", "down", "report_session",
                                    "report_bandwidth", "report_ero", "update_ero", "update_bandwidth", "edges",
                                    "before_send", "after_authority", "old_capture", "stopped_writer",
                                    "expired_peer", "checksum"])
def test_path_advice_or_inexact_completion_cannot_prove_owned_pcupd(tmp_path, monkeypatch, change):
    capture, operation, policy, timestamp = completion_proof(tmp_path, monkeypatch)
    peer = capture["peers"][PEER]
    report, update = peer["reports"]["owned"], peer["updates"]["17"]
    if change == "disconnected":
        peer["state"] = "DISCONNECTED"
    elif change == "stale":
        peer["fresh"] = False
    elif change == "unsynchronized":
        peer["synchronized"] = False
    elif change == "policy":
        capture["provisioning_policy_digest"] = "other"
    elif change == "no_updates":
        peer["updates"] = {}
    elif change == "plan":
        update["provisioning_plan_id"] = "other"
    elif change == "pc_rep":
        update["status"] = "PATH"
    elif change == "unacknowledged":
        update["acknowledged"] = False
    elif change == "old_incarnation":
        update["acknowledged_identifiers"]["lsp_id"] += 1
    elif change == "down":
        report["operational_code"] = 0
    elif change == "report_session":
        report["session_id"] = "old-session"
    elif change == "report_bandwidth":
        report["attributes"][1]["body_hex"] = struct.pack("!f", 0).hex()
    elif change == "report_ero":
        report["ero"][0]["loose"] = True
    elif change == "update_ero":
        update["ero"] = ["10.0.12.2"]
    elif change == "update_bandwidth":
        update["bandwidth_bps"] = 0
    elif change == "edges":
        update["path_link_ids"] = ["different-edge"]
    elif change == "before_send":
        report["received_at"] = update["sent_at"] - 1
    elif change == "after_authority":
        update["sent_at"] = timestamp + 120
    elif change == "old_capture":
        capture["generated_at"] = timestamp - 121
    elif change == "stopped_writer":
        capture["generated_at"] = timestamp - 6
    elif change == "expired_peer":
        peer["valid_until"] = timestamp - 1
    elif change == "checksum":
        capture["checksum"] = "invalid"
    assert not configured_initial_path_observed(capture, operation, policy_digest=policy, now=timestamp)


def test_withdrawn_journal_authority_while_writer_locked_never_sends_old_path(tmp_path, monkeypatch):
    computer, report, journal, authorizer, delegated = setup(tmp_path, monkeypatch, "127.0.0.1")
    computed = threading.Event()
    original_validate = delegated.validate_reply

    def validate(reply):
        valid = original_validate(reply)
        if reply.outcomes[0]["status"] == "INITIAL_PATH":
            computed.set()
        return valid

    monkeypatch.setattr(delegated, "validate_reply", validate)

    async def run():
        server = PcepServer(PcepServerConfig(("127.0.0.1",), port=0, keepalive=1, deadtimer=5),
                            path_computer=computer, delegated_computer=delegated)
        await server.start()
        reader, writer = await connect(server)
        connection = server._connections["127.0.0.1"]
        await connection.send_lock.acquire()
        try:
            writer.write(encode_message(PCRPT, report_body(report)))
            await writer.drain()
            await _eventually(computed.is_set)
            journal["operations"]["operation-1"]["status"] = "ROLLBACK_REQUIRED"
            write_state(authorizer.path, journal)
            connection.send_lock.release()
            await _eventually(lambda: connection.computation.done())
            with pytest.raises(asyncio.TimeoutError):
                await _read(reader, .05)
            assert not connection.owned_plsps and not connection.state["updates"]
            assert load_state(authorizer.path)["operations"]["operation-1"]["status"] == "ROLLBACK_REQUIRED"
        finally:
            if connection.send_lock.locked():
                connection.send_lock.release()
            await server.close()
            writer.close()
            await writer.wait_closed()

    asyncio.run(run())
