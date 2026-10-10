"""Historical IOS XE initial delegation, exact PCUpd, and RSVP/TED replay.

Only the TED replay clock is frozen to the capture time. Decoding an archived
ACTIVE acknowledgment does not establish a current session or fresh LSP state.
"""
from __future__ import annotations

import hashlib
import json
import struct
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from test_pcep_compute import EXPORTER, NAMES, PEER, prepare

from ninja_pce.bgpls import parse_gobgp_snapshot
from ninja_pce.cspf import compute_cspf
from ninja_pce.models import ServiceDemand, Topology
from ninja_pce.pcep import PCRPT, PcepProtocolError, parse_objects, parse_reports
from ninja_pce.pcep_delegation import DelegatedPathComputer, ownership_key

FIXTURES = Path(__file__).parent / "fixtures"
PCEP = FIXTURES / "pcep-iosxe-16.9.7"
BGPLS = FIXTURES / "bgpls-iosxe-16.9.7"
INITIAL = "tunnel100-initial-delegation.hex"
UPDATE = "tunnel100-initial-pcupd.hex"
ACTIVE = "tunnel100-computed-active.hex"
BEFORE = "bgpls-before-pce-compute-raw.json"
AFTER = "bgpls-pce-computed-raw.json"
FRAME_DIGESTS = {
    INITIAL: (132, "0884b706110e3329893be514740686e6d1646219d08420bbda99bc2cacb0ecf5"),
    UPDATE: (128, "30c1a89b0b074e2a2a3f29732afbb758371c607bd85d29cf54fb6eae94f3a8d8"),
    ACTIVE: (156, "2b5cd90e1fef9710abebe15672e46ee7e3e3d19b83d71accc62414f417604657"),
}
RAW_DIGESTS = {
    BEFORE: "0b5ac0abbd343da6244b8b40953511d87b0ceb3f115cce36ec7049b866fc27c8",
    AFTER: "78305ce5a6715c55077bcb5ef140fb7af8337555742c16ebd1ce053cde387468",
}
NEW_ERO = ["10.0.12.2", "10.0.25.5", "10.0.56.6"]
OLD_PATH = {("PE1", "P3"), ("P3", "P4"), ("P4", "PE3")}
NEW_PATH = {("PE1", "P1"), ("P1", "P2"), ("P2", "PE3")}


def wire(filename: str) -> bytes:
    return bytes.fromhex((PCEP / filename).read_text())


def report(filename: str) -> dict[str, Any]:
    frame = wire(filename)
    assert frame[1] == PCRPT
    reports = parse_reports(frame[4:], allow_delegation=True)
    assert len(reports) == 1
    return reports[0]


def topology(filename: str) -> Topology:
    capture = json.loads((BGPLS / filename).read_text())
    return parse_gobgp_snapshot(capture, required_peers=(EXPORTER,), node_names=NAMES,
                                now=datetime.fromisoformat(capture["captured_at"]))


@pytest.mark.parametrize("filename", [INITIAL, UPDATE, ACTIVE])
def test_actual_wire_frames_preserve_exact_captured_bytes(filename: str) -> None:
    frame = wire(filename)
    length, expected = FRAME_DIGESTS[filename]
    assert len(frame) == length
    assert frame[0] == 0x20 and int.from_bytes(frame[2:4], "big") == length
    assert hashlib.sha256(frame).hexdigest() == expected


@pytest.mark.parametrize("filename", [BEFORE, AFTER])
def test_actual_raw_ted_captures_preserve_exact_observations(filename: str) -> None:
    raw = (BGPLS / filename).read_bytes()
    assert hashlib.sha256(raw).hexdigest() == RAW_DIGESTS[filename]
    capture = json.loads(raw)
    assert capture["captured_at"] == (
        "2026-10-10T20:12:07.093159+00:00" if filename == BEFORE else "2026-10-10T20:12:38.817300+00:00"
    )
    assert capture["source"] == "gobgp-v3.37.0-grpc"
    assert all(path["nlriBinary"] and path["pattrsBinary"] and path["neighborIp"] == EXPORTER
               for response in capture["rib"] for path in response["destination"]["paths"])


def test_actual_initial_delegation_is_explicit_down_empty_path_with_constraints() -> None:
    initial = report(INITIAL)
    assert initial["plsp_id"] == 524388 and initial["symbolic_name"] == "Tunnel100_w"
    assert initial["delegate"] and initial["administrative_up"]
    assert not initial["sync"] and not initial["removed"] and not initial["end_of_sync"]
    assert initial["operational_state"] == "DOWN" and initial["operational_code"] == 0
    assert initial["ero"] == [] and initial["srp_id"] == 0
    assert initial["srp_tlvs"] == [{"type": 28, "value_hex": "00000000"}]
    ids = initial["identifiers"]
    assert (ids["type"], ids["sender"], ids["endpoint"], ids["tunnel_id"], ids["lsp_id"]) == (
        18, PEER, "10.255.0.6", 100, 224,
    )
    assert ids["extended_tunnel_id_hex"] == "0aff0001" and not ids["all_zero"]
    attrs = {(attr["class"], attr["type"]): attr for attr in initial["attributes"]}
    assert attrs[(5, 1)]["bits_per_second"] == 50_000_000
    assert attrs[(5, 1)]["body_hex"] == attrs[(5, 5)]["body_hex"] == "4abebc20"
    assert bytes.fromhex(attrs[(9, 1)]["body_hex"])[12:16] == bytes((7, 7, 0, 0))
    assert attrs[(6, 1)]["body_hex"] == "0000000200000000"
    assert {item["type"]: item["value_hex"] for item in initial["tlvs"]}[31] == "00000001c000"
    # An observed D=1 must not silently enable updates on the default observer.
    with pytest.raises(PcepProtocolError, match="Delegation not negotiated"):
        parse_reports(wire(INITIAL)[4:])


def test_exact_observed_initial_computation_reproduces_actual_pcupd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    computer = prepare(tmp_path, monkeypatch, filename=BEFORE)
    paths = (computer.raw_path, computer.status_path, computer.topology_path)
    original_bytes = tuple(path.read_bytes() for path in paths)
    delegated = DelegatedPathComputer(computer, {PEER: (100,)})
    reply = delegated(PEER, [report(INITIAL)], (3,))
    assert reply.messages == (wire(UPDATE),)
    assert reply.topology_digest == RAW_DIGESTS[BEFORE]
    assert delegated.validate_reply(reply)
    outcome, = reply.outcomes
    assert outcome["status"] == "INITIAL_PATH" and outcome["srp_id"] == 3
    assert outcome["plsp_id"] == 524388 and outcome["tunnel_id"] == 100
    assert outcome["sender"] == PEER and outcome["endpoint"] == "10.255.0.6"
    assert outcome["bandwidth_bps"] == 50_000_000
    assert outcome["setup_priority"] == outcome["hold_priority"] == 7
    assert outcome["path_nodes"] == ["PE1", "P1", "P2", "PE3"]
    assert outcome["ero"] == NEW_ERO and outcome["total_metric"] == 30
    assert outcome["topology_timestamp"] == "2026-10-10T20:12:07.093159+00:00"
    assert outcome["reservation_created"] is False and outcome["authoritative"] is False
    assert tuple(path.read_bytes() for path in paths) == original_bytes


def test_actual_active_acknowledgment_correlates_srp_identity_path_and_bandwidth() -> None:
    initial = report(INITIAL)
    active = report(ACTIVE)
    assert active["srp_id"] == 3 and active["plsp_id"] == 524388
    assert active["delegate"] and active["administrative_up"]
    assert active["operational_state"] == "ACTIVE" and active["operational_code"] == 2
    assert not active["sync"] and not active["removed"] and not active["end_of_sync"]
    assert active["identifiers"] == initial["identifiers"]
    assert ownership_key(active) == ownership_key(initial)
    assert active["symbolic_name"] == "Tunnel100_w"
    assert [(hop["address"], hop["prefix_length"], hop["loose"]) for hop in active["ero"]] == [
        (address, 32, False) for address in NEW_ERO
    ]
    attrs = {(attr["class"], attr["type"]): attr for attr in active["attributes"]}
    assert attrs[(5, 1)]["bits_per_second"] == 50_000_000
    assert attrs[(5, 5)]["body_hex"] == "4abebc20"
    assert bytes.fromhex(attrs[(9, 1)]["body_hex"])[12:16] == bytes((7, 7, 0, 0))
    assert struct.unpack("!f", bytes.fromhex(attrs[(6, 1)]["body_hex"])[4:])[0] == 30
    update_objects = parse_objects(wire(UPDATE)[4:])
    assert [obj.object_class for obj in update_objects] == [33, 32, 7, 9, 5, 6]
    assert int.from_bytes(update_objects[0].body[4:8], "big") == active["srp_id"]
    assert all(obj.flags == 0 for obj in update_objects)
    assert update_objects[2].body == b"".join(bytes.fromhex(hop["raw_hex"]) for hop in active["ero"])
    # Archived wire data has no current session freshness or verification claim.
    assert "received_at" not in active and "delegation_accepted" not in active and "stale" not in active


def test_actual_reservation_moves_only_forward_path_and_changes_admission() -> None:
    before, after = topology(BEFORE), topology(AFTER)
    before_edges = {(edge.source, edge.target): edge for edge in before.links}
    after_edges = {(edge.source, edge.target): edge for edge in after.links}
    assert len(before.nodes) == len(after.nodes) == 6
    assert len(before_edges) == len(after_edges) == 16
    assert {edge.id for edge in before.links} == {edge.id for edge in after.links}
    for edges, reserved_path in ((before_edges, OLD_PATH), (after_edges, NEW_PATH)):
        assert {pair for pair, edge in edges.items() if edge.reserved_bw_bps} == reserved_path
        for pair, edge in edges.items():
            assert edge.max_bw_bps == 1_000_000_000 and edge.reservable_bw_bps == 100_000_000
            assert edge.bandwidth_bps == 100_000_000 and edge.metric == 10
            expected_reserved = 50_000_000 if pair in reserved_path else 0
            assert type(edge.reserved_bw_bps) is int and edge.reserved_bw_bps == expected_reserved
            assert edge.unreserved_bw_bps == (100_000_000,) * 7 + (100_000_000 - expected_reserved,)
            assert all(type(value) is int for value in edge.unreserved_bw_bps)
            assert all(edge.available_bps(priority) == 100_000_000 - expected_reserved for priority in range(8))
    assert all(replace(before_edges[pair], timestamp=after_edges[pair].timestamp)
               == after_edges[pair] for pair in before_edges if pair not in OLD_PATH | NEW_PATH)
    for observed, expected_path in ((before, ("PE1", "P1", "P2", "PE3")),
                                    (after, ("PE1", "P3", "P4", "PE3"))):
        # 100 Mbps cannot fit the observed 50 Mbps residual at any priority;
        # selecting the other path gives neither self-credit nor preemption.
        result = compute_cspf(observed, ServiceDemand(name="CAPTURE_ADMISSION", source="PE1", destination="PE3",
                                                      bandwidth_mbps=100, setup_priority=0, hold_priority=0))
        assert result.path_nodes == expected_path and result.total_metric == 30
        assert all(edge.available_bw_bps == 100_000_000 for edge in result.path_links)
        assert observed.raw_metadata["authoritative"] is False
        assert observed.raw_metadata["complete"] is False
        assert observed.raw_metadata["execution_eligible"] is False
