"""Offline PCReq/PCRep computation using actual historical BGP-LS graph data."""
from __future__ import annotations

import hashlib
import json
import struct
from datetime import datetime, timedelta
from ipaddress import IPv4Address
from pathlib import Path
from typing import Any

import pytest

import ninja_pce.pcep_compute as computation
from ninja_pce.bgpls import parse_gobgp_snapshot, topology_document
from ninja_pce.cspf import CspfSearchLimit
from ninja_pce.models import Node, Topology
from ninja_pce.pcep import ERROR, PCREP, encode_object, parse_objects
from ninja_pce.pcep_compute import PathReply, PcepPathComputer, request_error
from ninja_pce.state import load_state, write_state

PEER = "10.255.0.1"
EXPORTER = "172.16.100.102"
NAMES = {
    "10.255.0.1": "PE1", "10.255.0.2": "P1", "10.255.0.3": "P3",
    "10.255.0.4": "P4", "10.255.0.5": "P2", "10.255.0.6": "PE3",
}
FIXTURES = Path(__file__).parent / "fixtures/bgpls-iosxe-16.9.7"


def prepare(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
            filename: str = "bgpls-healthy-raw.json") -> PcepPathComputer:
    raw = tmp_path / "raw.json"
    raw.write_bytes((FIXTURES / filename).read_bytes())
    capture = json.loads(raw.read_text())
    now = datetime.fromisoformat(capture["captured_at"])
    monkeypatch.setattr(computation, "_utcnow", lambda: now)
    topology = parse_gobgp_snapshot(capture, required_peers=(EXPORTER,), node_names=NAMES, now=now)
    feed = tmp_path / "topology.json"
    feed.write_text(json.dumps(topology_document(topology)))
    status = tmp_path / "status.json"
    write_state(status, {"status": "ok", "observed_at": now.timestamp(),
                         "metadata": topology.raw_metadata, "nodes": len(topology.nodes),
                         "directed_links": len(topology.links)})
    return PcepPathComputer(raw_path=raw, status_path=status, topology_path=feed,
                            required_peers=(EXPORTER,), node_names=NAMES, headends={PEER: PEER})


@pytest.fixture
def computer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> PcepPathComputer:
    return prepare(tmp_path, monkeypatch)


def request(request_id: int = 1, *, bandwidth: float = 6_250_000, rp_flags: int = 0,
            source: str = PEER, destination: str = "10.255.0.6", extra: bytes = b"",
            lspa: bytes = b"", include_bandwidth: bool = True) -> bytes:
    return (
        encode_object(2, struct.pack("!II", rp_flags, request_id), flags=2)
        + encode_object(4, IPv4Address(source).packed + IPv4Address(destination).packed, flags=2)
        + (encode_object(9, lspa, flags=2) if lspa else b"")
        + (encode_object(5, struct.pack("!f", bandwidth), flags=2) if include_bandwidth else b"")
        + extra
    )


def metric(value: float, *, kind: int = 2, flags: int = 0) -> bytes:
    return encode_object(6, bytes((0, 0, flags, kind)) + struct.pack("!f", value), flags=2)


def assert_error(reply: PathReply, request_id: int | None = 1) -> None:
    assert reply.messages and all(message[1] == ERROR for message in reply.messages)
    for message in reply.messages:
        assert int.from_bytes(message[2:4], "big") == len(message)
        objects = parse_objects(message[4:])
        assert objects[-1].object_class == 13
        if request_id is not None:
            assert objects[0].object_class == 2 and objects[0].flags == 0
            assert int.from_bytes(objects[0].body[4:8], "big") == request_id


def test_actual_graph_positive_wire_reply_and_read_only_repeated_computation(computer: PcepPathComputer) -> None:
    paths = (computer.raw_path, computer.status_path, computer.topology_path)
    before = [path.read_bytes() for path in paths]
    reply = computer(PEER, request(rp_flags=7))
    assert reply.messages[0][1] == PCREP
    assert reply.topology_digest == hashlib.sha256(before[0]).hexdigest()
    assert computer.validate_reply(reply)
    outcome = reply.outcomes[0]
    assert outcome["status"] == "PATH"
    assert outcome["path_nodes"] == ["PE1", "P3", "P4", "PE3"]
    assert outcome["total_metric"] == 30 and outcome["bandwidth_bps"] == 50_000_000
    # RP request scheduling priority 7 must not become RSVP priority 7.
    assert outcome["setup_priority"] == outcome["hold_priority"] == 0
    assert outcome["reservation_created"] is False and outcome["authoritative"] is False
    objects = parse_objects(reply.messages[0][4:])
    assert [obj.object_class for obj in objects] == [2, 7, 5, 6]
    assert objects[0].flags == 2 and objects[0].body == struct.pack("!II", 0, 1)
    assert objects[1].body == bytes.fromhex("01080a000d03200001080a002204200001080a002e062000")
    assert struct.unpack("!f", objects[2].body)[0] == 6_250_000
    assert objects[3].body == bytes((0, 0, 0, 2)) + struct.pack("!f", 30)
    assert computer(PEER, request()).outcomes[0]["bandwidth_bps"] == 50_000_000
    assert [path.read_bytes() for path in paths] == before


def test_te_bound_computed_metric_and_of_request(computer: PcepPathComputer) -> None:
    of = encode_object(21, struct.pack("!HH", 1, 0), flags=2)
    reply = computer(PEER, request(rp_flags=0xA0, extra=metric(0, flags=2) + metric(30.9, flags=1) + of))
    assert reply.outcomes[0]["status"] == "PATH"
    objects = parse_objects(reply.messages[0][4:])
    # Loose is acceptable to this PCC, but the returned path is strict.
    assert int.from_bytes(objects[0].body[:4], "big") == 0x80
    assert [obj.object_class for obj in objects] == [2, 21, 7, 5, 6]
    assert objects[1].body == struct.pack("!HH", 1, 0)
    no_path = computer(PEER, request(extra=metric(29.9, flags=1)))
    assert no_path.outcomes[0]["status"] == "NO_PATH"
    objects = parse_objects(no_path.messages[0][4:])
    assert [obj.object_class for obj in objects] == [2, 3]
    assert objects[1].body == bytes(4)


def test_duplicate_metric_uses_first_bound_per_rfc(computer: PcepPathComputer) -> None:
    reply = computer(PEER, request(extra=metric(30, flags=1) + metric(0, flags=1)))
    assert reply.outcomes[0]["status"] == "PATH"


def test_lspa_priority_affinity_and_no_automatic_preemption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    computer = prepare(tmp_path, monkeypatch, "bgpls-reserved-raw.json")
    lspa = struct.pack("!IIIBBBB", 0xFFFF, 0, 0, 0, 0, 0, 0)
    reply = computer(PEER, request(bandwidth=7_500_000, lspa=lspa))
    assert reply.outcomes[0]["path_nodes"] == ["PE1", "P1", "P2", "PE3"]
    assert reply.outcomes[0]["bandwidth_bps"] == 60_000_000
    include_all = struct.pack("!IIIBBBB", 0, 0, 1, 7, 7, 0, 0)
    assert computer(PEER, request(lspa=include_all)).outcomes[0]["status"] == "NO_PATH"


def test_fractional_float32_bit_request_is_rounded_up(computer: PcepPathComputer) -> None:
    value = struct.unpack("!f", struct.pack("!f", 0.1))[0]
    reply = computer(PEER, request(bandwidth=value))
    assert reply.outcomes[0]["status"] == "PATH"
    assert reply.outcomes[0]["bandwidth_bps"] == 1


@pytest.mark.parametrize("amount", [float("nan"), float("inf"), -1.0, 0.0])
def test_nonfinite_nonpositive_bandwidth_rejected(computer: PcepPathComputer, amount: float) -> None:
    assert_error(computer(PEER, request(bandwidth=amount)))


@pytest.mark.parametrize("extra", [
    encode_object(99, bytes(4), flags=2), encode_object(99, bytes(4)),
    encode_object(5, struct.pack("!f", 1), object_type=2),
    encode_object(5, struct.pack("!f", 1), object_type=5),
    encode_object(8, b""), encode_object(10, b""), encode_object(11, bytes(8)),
    encode_object(14, bytes(8)), metric(0, kind=1), metric(0, kind=3),
    metric(float("nan")), metric(-1),
    encode_object(21, struct.pack("!HH", 2, 0), flags=2),
])
def test_unsupported_constraints_never_get_a_positive_reply(computer: PcepPathComputer, extra: bytes) -> None:
    assert_error(computer(PEER, request(extra=extra)))


@pytest.mark.parametrize("flags", [8, 16])
def test_reoptimization_and_bidirectional_rejected(computer: PcepPathComputer, flags: int) -> None:
    assert_error(computer(PEER, request(rp_flags=flags)))


@pytest.mark.parametrize("setup,hold,flags", [(8, 7, 0), (6, 7, 0), (7, 7, 1), (7, 7, 2)])
def test_invalid_priority_and_unknown_protection_rejected(
    computer: PcepPathComputer, setup: int, hold: int, flags: int,
) -> None:
    assert_error(computer(PEER, request(lspa=struct.pack("!IIIBBBB", 0, 0, 0, setup, hold, flags, 0))))


@pytest.mark.parametrize("flags", [0x40, *(1 << bit for bit in range(8, 17))])
def test_assigned_unsupported_rp_flags_are_rejected(computer: PcepPathComputer, flags: int) -> None:
    assert_error(computer(PEER, request(rp_flags=flags)))


def test_reserved_rp_and_lspa_flags_do_not_acquire_meaning(computer: PcepPathComputer) -> None:
    lspa = struct.pack("!IIIBBBB", 0, 0, 0, 7, 7, 0x80, 0)
    reply = computer(PEER, request(rp_flags=0x80000000, lspa=lspa))
    assert reply.outcomes[0]["status"] == "PATH"
    assert parse_objects(reply.messages[0][4:])[0].body[:4] == bytes(4)


@pytest.mark.parametrize("kind", ["rp", "lspa", "of", "lsp"])
def test_extension_tlvs_are_not_silently_dropped(computer: PcepPathComputer, kind: str) -> None:
    tlv = struct.pack("!HHI", 60000, 4, 1)
    if kind == "rp":
        body = request()
        body = encode_object(2, struct.pack("!II", 0, 1) + tlv, flags=2) + body[12:]
    elif kind == "lspa":
        body = request(lspa=struct.pack("!IIIBBBB", 0, 0, 0, 7, 7, 0, 0) + tlv)
    elif kind == "of":
        body = request(extra=encode_object(21, struct.pack("!HH", 1, 0) + tlv))
    else:
        body = request(extra=encode_object(32, struct.pack("!I", 524388 << 12) + tlv))
    assert_error(computer(PEER, body))


def test_error_codes_distinguish_unsupported_object_of_and_policy(computer: PcepPathComputer) -> None:
    cases = [(request(extra=encode_object(99, bytes(4))), (4, 1)),
             (request(extra=encode_object(5, bytes(4), object_type=2)), (4, 2)),
             (request(extra=encode_object(21, struct.pack("!HH", 2, 0))), (4, 4)),
             (request(rp_flags=8), (5, 0))]
    for body, expected in cases:
        assert parse_objects(computer(PEER, body).messages[0][4:])[-1].body[2:] == bytes(expected)


def test_optional_request_lsp_identification_does_not_require_report_tlvs(computer: PcepPathComputer) -> None:
    request_lsp = encode_object(32, struct.pack("!I", 524388 << 12))
    reply = computer(PEER, request(extra=request_lsp))
    assert reply.outcomes[0]["status"] == "PATH"
    objects = parse_objects(reply.messages[0][4:])
    assert [obj.object_class for obj in objects][:3] == [2, 32, 7]
    assert objects[1].body == struct.pack("!I", 524388 << 12)
    assert reply.outcomes[0]["reservation_created"] is False


def test_endpoint_authorization_and_missing_bandwidth(computer: PcepPathComputer) -> None:
    assert_error(computer("10.255.0.2", request()))
    assert_error(computer(PEER, request(source="10.255.0.2")))
    assert_error(computer(PEER, request(destination="10.255.0.99")))
    assert_error(computer(PEER, request(include_bandwidth=False)))


def test_proved_insufficient_capacity_is_no_path(computer: PcepPathComputer) -> None:
    reply = computer(PEER, request(bandwidth=12_500_001))
    assert reply.messages[0][1] == PCREP
    assert reply.outcomes[0]["status"] == "NO_PATH"
    assert parse_objects(reply.messages[0][4:])[1].object_class == 3
    assert computer.validate_reply(reply)


def test_search_limit_is_error_not_no_path(computer: PcepPathComputer, monkeypatch: pytest.MonkeyPatch) -> None:
    def exhausted(topology: Topology, demand: Any) -> Any:
        assert demand.constraints.search_budget <= 100_000
        assert demand.constraints.max_candidates == 1
        raise CspfSearchLimit(100_000, 100_000, 1)
    monkeypatch.setattr(computation, "compute_cspf", exhausted)
    reply = computer(PEER, request())
    assert_error(reply)
    assert reply.outcomes[0]["status"] == "SEARCH_LIMIT"


def test_batch_structural_errors_are_atomic_and_semantic_errors_correlated(computer: PcepPathComputer) -> None:
    duplicate = computer(PEER, request() + request())
    assert_error(duplicate)
    assert all(item["status"] == "REJECTED" for item in duplicate.outcomes)
    assert_error(computer(PEER, request() + bytes.fromhex("02100020")), None)
    assert_error(computer(PEER, b""), None)
    assert_error(computer(PEER, request(request_id=0)), None)
    assert_error(computer(PEER, b"".join(request(index) for index in range(1, 10))), None)
    mixed = computer(PEER, request(11) + request(12, extra=metric(0, kind=1)))
    assert [item["request_id"] for item in mixed.outcomes] == [11, 12]
    assert [item["status"] for item in mixed.outcomes] == ["PATH", "REJECTED"]
    assert mixed.messages[0][1] == PCREP and mixed.messages[1][1] == ERROR


@pytest.mark.parametrize("changed", ["stale", "failed", "missing_feed", "status_timestamp", "feed_timestamp", "bool_time", "huge_time"])
def test_known_collector_failures_never_use_last_good_raw(
    computer: PcepPathComputer, monkeypatch: pytest.MonkeyPatch, changed: str,
) -> None:
    status = load_state(computer.status_path)
    if changed == "stale":
        now = computation._utcnow() + timedelta(seconds=31)
        monkeypatch.setattr(computation, "_utcnow", lambda: now)
    elif changed == "failed":
        status["status"] = "unavailable"
        write_state(computer.status_path, status)
    elif changed == "missing_feed":
        computer.topology_path.unlink()
    elif changed == "status_timestamp":
        status["metadata"]["timestamp"] = "2026-10-10T18:00:00+00:00"
        write_state(computer.status_path, status)
    elif changed == "feed_timestamp":
        feed = json.loads(computer.topology_path.read_text())
        feed["metadata"]["timestamp"] = "2026-10-10T18:00:00+00:00"
        computer.topology_path.write_text(json.dumps(feed))
    elif changed == "bool_time":
        status["observed_at"] = True
        write_state(computer.status_path, status)
    else:
        status["observed_at"] = 10 ** 309
        write_state(computer.status_path, status)
    reply = computer(PEER, request())
    assert_error(reply)
    assert reply.outcomes[0]["status"] == "TOPOLOGY_UNAVAILABLE"


@pytest.mark.parametrize("changed", ["raw_bytes", "status_failed", "feed_removed", "expired"])
def test_presend_revalidation_rejects_changed_or_unavailable_observation(
    computer: PcepPathComputer, monkeypatch: pytest.MonkeyPatch, changed: str,
) -> None:
    reply = computer(PEER, request())
    assert computer.validate_reply(reply)
    if changed == "raw_bytes":
        computer.raw_path.write_bytes(computer.raw_path.read_bytes() + b"\n")
    elif changed == "status_failed":
        status = load_state(computer.status_path)
        status["status"] = "unavailable"
        write_state(computer.status_path, status)
    elif changed == "feed_removed":
        computer.topology_path.unlink()
    else:
        now = computation._utcnow() + timedelta(seconds=31)
        monkeypatch.setattr(computation, "_utcnow", lambda: now)
    assert not computer.validate_reply(reply)


def test_oversized_input_and_fifo_are_rejected_without_blocking(computer: PcepPathComputer) -> None:
    computer.raw_path.write_bytes(b" " * (1_048_576 + 1))
    assert_error(computer(PEER, request()))
    computer.raw_path.unlink()
    import os
    os.mkfifo(computer.raw_path)
    assert_error(computer(PEER, request()))


def test_graph_bound_is_enforced_before_search(computer: PcepPathComputer, monkeypatch: pytest.MonkeyPatch) -> None:
    topology, _ = computer._snapshot()
    nodes = {f"N{index}": Node(f"N{index}", str(IPv4Address(0x0A020001 + index))) for index in range(257)}
    large = Topology(topology.name, nodes, topology.links, topology.raw_metadata)
    status = load_state(computer.status_path)
    status["nodes"] = 257
    write_state(computer.status_path, status)
    monkeypatch.setattr(computation, "parse_gobgp_snapshot", lambda *args, **kwargs: large)
    reply = computer(PEER, request())
    assert_error(reply)
    assert "bounds" in reply.outcomes[0]["reason"]


def test_error_helper_has_only_real_correlation_and_needs_no_ted(computer: PcepPathComputer) -> None:
    computer.raw_path.unlink()
    reply = request_error(request(71) + request(72), reason="worker timeout")
    assert [item["request_id"] for item in reply.outcomes] == [71, 72]
    assert_error(PathReply((reply.messages[0],), (reply.outcomes[0],)), 71)
    assert computer.validate_reply(reply)
    assert request_error(bytes.fromhex("02100020")).outcomes[0]["request_id"] is None
