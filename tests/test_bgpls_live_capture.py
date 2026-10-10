"""Offline replay of actual CSR1000v 16.9.7 / GoBGP 3.37.0 captures."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from ninja_pce.bgpls import BgplsError, parse_gobgp_snapshot
from ninja_pce.cspf import compute_cspf
from ninja_pce.models import ServiceDemand, Topology

FIXTURES = Path(__file__).parent / "fixtures/bgpls-iosxe-16.9.7"
PEER = "172.16.100.102"
NODE_NAMES = {
    "10.255.0.1": "PE1",
    "10.255.0.2": "P1",
    "10.255.0.3": "P3",
    "10.255.0.4": "P4",
    "10.255.0.5": "P2",
    "10.255.0.6": "PE3",
}
HEALTHY = "bgpls-healthy-raw.json"
WITHDRAWN = "bgpls-withdrawn-raw.json"
SHA256 = {
    HEALTHY: "52bd12841798adf238541e549efde7968e73ad463ccc6ce2dd4d7f68c193bd10",
    WITHDRAWN: "a60cfc23a16a6c21b9a59488110cf665568d1633c30a5528b1425dae94897c3f",
}
CORE_LINKS = {
    ("PE1", "P1", "10.0.12.1", "10.0.12.2"),
    ("PE1", "P3", "10.0.13.1", "10.0.13.3"),
    ("P1", "P2", "10.0.25.2", "10.0.25.5"),
    ("P1", "P3", "10.0.23.2", "10.0.23.3"),
    ("P2", "P4", "10.0.54.5", "10.0.54.4"),
    ("P2", "PE3", "10.0.56.5", "10.0.56.6"),
    ("P3", "P4", "10.0.34.3", "10.0.34.4"),
    ("P4", "PE3", "10.0.46.4", "10.0.46.6"),
}
DIRECTED_LINKS = CORE_LINKS | {
    (target, source, remote, local) for source, target, local, remote in CORE_LINKS
}
WITHDRAWN_LINKS = {
    ("P1", "P2", "10.0.25.2", "10.0.25.5"),
    ("P2", "P1", "10.0.25.5", "10.0.25.2"),
}


def read_capture(filename: str) -> dict[str, Any]:
    return json.loads((FIXTURES / filename).read_text())


def parse_capture(filename: str) -> Topology:
    capture = read_capture(filename)
    return parse_gobgp_snapshot(
        capture, required_peers=(PEER,), node_names=NODE_NAMES,
        now=datetime.fromisoformat(capture["captured_at"]),
    )


@pytest.mark.parametrize("filename", [HEALTHY, WITHDRAWN])
def test_captures_preserve_actual_raw_wire_data(filename: str) -> None:
    assert hashlib.sha256((FIXTURES / filename).read_bytes()).hexdigest() == SHA256[filename]
    capture = read_capture(filename)
    assert capture["source"] == "gobgp-v3.37.0-grpc"
    assert capture["schema_version"] == 1
    for response in capture["rib"]:
        for path in response["destination"]["paths"]:
            assert path["nlriBinary"]
            assert path["pattrsBinary"]
            assert path["neighborIp"] == PEER


@pytest.mark.parametrize("filename,edge_count,prefix_count", [(HEALTHY, 16, 23), (WITHDRAWN, 14, 22)])
def test_actual_six_router_graph_and_conservative_provenance(
    filename: str, edge_count: int, prefix_count: int,
) -> None:
    topology = parse_capture(filename)
    assert {node.loopback: node.id for node in topology.nodes.values()} == NODE_NAMES
    assert len(topology.links) == len(topology.directional_links) == edge_count
    expected = DIRECTED_LINKS if filename == HEALTHY else DIRECTED_LINKS - WITHDRAWN_LINKS
    assert {(edge.source, edge.target, edge.from_ip, edge.to_ip) for edge in topology.links} == expected
    assert len({edge.id for edge in topology.links}) == edge_count
    assert all(edge.bidirectional is False for edge in topology.links)
    metadata = topology.raw_metadata
    assert metadata["session_state"] == "ESTABLISHED"
    assert metadata["required_peers"] == [PEER]
    assert metadata["timestamp"] == read_capture(filename)["captured_at"]
    assert metadata["complete"] is False
    assert metadata["authoritative"] is False
    assert metadata["execution_eligible"] is False
    assert metadata["excluded_objects"] == [
        {"nlri_type": 3, "reason": "prefix/extension NLRI is not a TE graph edge"}
    ] * prefix_count


@pytest.mark.parametrize("filename", [HEALTHY, WITHDRAWN])
def test_actual_te_attributes_decode_to_exact_integer_bits_per_second(filename: str) -> None:
    for edge in parse_capture(filename).links:
        assert type(edge.max_bw_bps) is int and edge.max_bw_bps == 1_000_000_000
        assert type(edge.reservable_bw_bps) is int and edge.reservable_bw_bps == 100_000_000
        assert type(edge.bandwidth_bps) is int and edge.bandwidth_bps == 100_000_000
        assert type(edge.reserved_bw_bps) is int and edge.reserved_bw_bps == 0
        assert edge.unreserved_bw_bps == (100_000_000,) * 8
        assert all(type(value) is int for value in edge.unreserved_bw_bps)
        assert all(edge.available_bps(priority) == 100_000_000 for priority in range(8))
        assert type(edge.metric) is int and edge.metric == 10


@pytest.mark.parametrize("filename", [HEALTHY, WITHDRAWN])
def test_actual_graph_computes_50mbps_three_hop_strict_ero(filename: str) -> None:
    result = compute_cspf(
        parse_capture(filename),
        ServiceDemand(name="IOSXE_CAPTURE_50M", source="PE1", destination="PE3", bandwidth_mbps=50),
    )
    assert result.path_nodes == ("PE1", "P3", "P4", "PE3")
    assert result.total_metric == 30
    assert len(result.path_links) == len(result.explicit_hops) == 3
    assert result.demand.bandwidth_bps == 50_000_000
    assert result.bottleneck_available_bw_mbps == 100
    assert result.residual_after_reservation_mbps == 50
    assert [(hop.node_id, hop.address, hop.local_address, hop.hop_type) for hop in result.explicit_hops] == [
        ("P3", "10.0.13.3", "10.0.13.1", "strict"),
        ("P4", "10.0.34.4", "10.0.34.3", "strict"),
        ("PE3", "10.0.46.6", "10.0.46.4", "strict"),
    ]
    for hop, edge in zip(result.explicit_hops, result.path_links, strict=True):
        assert hop.link_id == edge.id
        assert hop.address == edge.to_ip
        assert hop.local_address == edge.from_ip
        assert edge.available_bw_bps >= result.demand.bandwidth_bps
    if filename == HEALTHY:
        assert {candidate.path_nodes for candidate in result.candidate_paths[:2]} == {
            ("PE1", "P3", "P4", "PE3"), ("PE1", "P1", "P2", "PE3"),
        }
        assert all(candidate.total_metric == 30 and candidate.hop_count == 3
                   for candidate in result.candidate_paths[:2])


def test_actual_withdrawal_removes_both_directions_and_preserves_other_identities() -> None:
    healthy = parse_capture(HEALTHY)
    withdrawn = parse_capture(WITHDRAWN)
    healthy_ids = {
        (edge.source, edge.target, edge.from_ip, edge.to_ip): edge.id for edge in healthy.links
    }
    withdrawn_ids = {
        (edge.source, edge.target, edge.from_ip, edge.to_ip): edge.id for edge in withdrawn.links
    }
    assert healthy_ids.keys() - withdrawn_ids.keys() == WITHDRAWN_LINKS
    assert withdrawn_ids == {key: value for key, value in healthy_ids.items() if key not in WITHDRAWN_LINKS}


@pytest.mark.parametrize("filename", [HEALTHY, WITHDRAWN])
def test_historical_capture_is_rejected_after_freshness_budget(filename: str) -> None:
    capture = read_capture(filename)
    with pytest.raises(BgplsError, match="expired"):
        parse_gobgp_snapshot(
            capture, required_peers=(PEER,), node_names=NODE_NAMES,
            now=datetime.fromisoformat(capture["captured_at"]) + timedelta(seconds=31),
        )
