"""Synthetic multi-area RFC9552 fixtures; no device names or topology assumptions."""
from __future__ import annotations

import base64
import copy
import json
import struct
from datetime import datetime, timedelta, timezone
from ipaddress import IPv4Address
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from ninja_pce.bgpls import (
    BgplsError,
    GoBgpTopologyProvider,
    parse_gobgp_snapshot,
    parse_ospf_multi_area_policy,
    topology_document,
)
from ninja_pce.cspf import CspfNoPath, compute_cspf
from ninja_pce.loaders import load_topology
from ninja_pce.models import ServiceDemand

NOW = datetime(2026, 10, 12, 20, tzinfo=timezone.utc)
PEER = "192.0.2.200"
AF = {"afi": "AFI_LS", "safi": "SAFI_LS"}
NAMES = {"192.0.2.10": "west", "192.0.2.11": "border", "192.0.2.12": "east"}
PROTO = Path(__file__).resolve().parents[1] / "gns3/controller/gobgp/gobgp-readonly.proto"


def tlv(kind, value):
    return struct.pack("!HH", kind, len(value)) + value


def descriptor(igp, area, *, autonomous_system=64512, bgpls_identifier=12, extra=b""):
    return (tlv(512, struct.pack("!I", autonomous_system))
            + tlv(513, struct.pack("!I", bgpls_identifier))
            + tlv(514, IPv4Address(area).packed) + tlv(515, IPv4Address(igp).packed) + extra)


def nlri(kind, values, *, protocol=3, instance=47):
    payload = bytes([protocol]) + instance.to_bytes(8, "big") + values
    return struct.pack("!HH", kind, len(payload)) + payload


def node(igp, area, *, protocol=3, instance=47, **kwargs):
    return nlri(1, tlv(256, descriptor(igp, area, **kwargs)), protocol=protocol, instance=instance)


def link(source, target, area, local, remote, *, target_area=None, **kwargs):
    return nlri(2, tlv(256, descriptor(source, area))
                + tlv(257, descriptor(target, target_area or area))
                + tlv(259, IPv4Address(local).packed) + tlv(260, IPv4Address(remote).packed), **kwargs)


def attrs(*, metric=11, capacity=10_000_000):
    return {1088: bytes(4), 1089: struct.pack("!f", capacity),
            1090: struct.pack("!f", capacity), 1091: struct.pack("!8f", *([capacity] * 8)),
            1092: struct.pack("!I", metric)}


def route(encoded, attributes):
    value = b"".join(tlv(kind, value) for kind, value in sorted(attributes.items()))
    attribute = bytes([0x90, 29]) + struct.pack("!H", len(value)) + value
    return {"destination": {"paths": [{"best": True, "family": AF.copy(), "neighborIp": PEER,
                                      "nlriBinary": base64.b64encode(encoded).decode(),
                                      "pattrsBinary": [base64.b64encode(attribute).decode()]}]}}


def peer():
    return {"peer": {"state": {"neighborAddress": PEER, "sessionState": "ESTABLISHED", "adminState": "UP"},
                     "timers": {"state": {"uptime": "2026-10-12T19:00:00Z"}},
                     "afiSafis": [{"state": {"family": AF.copy(), "enabled": True},
                                   "mpGracefulRestart": {"state": {"endOfRibReceived": True}}}]}}


def capture():
    return {"schema_version": 1, "source": "gobgp-v3.37.0-grpc",
            "poll_started_at": (NOW - timedelta(seconds=1)).isoformat(), "captured_at": NOW.isoformat(),
            "peers_before": [peer()], "peers_after": [peer()],
            "rib": [route(node("198.51.100.10", "0.0.0.0"), {1028: IPv4Address("192.0.2.10").packed}),
                    route(node("198.51.100.11", "0.0.0.0"), {1028: IPv4Address("192.0.2.11").packed}),
                    route(node("198.51.100.11", "0.0.0.17"), {1028: IPv4Address("192.0.2.11").packed}),
                    route(node("198.51.100.12", "0.0.0.17"), {1028: IPv4Address("192.0.2.12").packed}),
                    route(link("198.51.100.10", "198.51.100.11", "0.0.0.0",
                               "203.0.113.1", "203.0.113.2"), attrs(metric=11)),
                    route(link("198.51.100.11", "198.51.100.12", "0.0.0.17",
                               "203.0.113.5", "203.0.113.6"), attrs(metric=19))]}


def policy_document():
    return {"schema_version": 1, "protocol_id": 3, "instance_id": 47,
            "autonomous_system": 64512, "bgpls_identifier": 12,
            "bindings": [{"node_id": "border", "te_router_id": "192.0.2.11",
                          "igp_router_id": "198.51.100.11", "areas": ["0.0.0.0", "0.0.0.17"]}]}


def parse(value, *, policy=None, names=NAMES):
    return parse_gobgp_snapshot(value, required_peers=(PEER,), node_names=names, now=NOW,
                                ospf_multi_area=policy or parse_ospf_multi_area_policy(policy_document()))


def demand():
    return ServiceDemand(name="cross_area", source="west", destination="east", bandwidth_mbps=5)


def test_explicit_domain_binding_joins_only_physical_router_and_keeps_real_edges():
    raw = parse_gobgp_snapshot(capture(), required_peers=(PEER,), now=NOW)
    topology = parse(capture())
    assert len(raw.nodes) == 4 and len(topology.nodes) == 3
    assert len(raw.links) == len(topology.links) == 2
    assert {edge.id for edge in raw.links} == {edge.id for edge in topology.links}
    assert topology.nodes["border"].area is None
    assert {edge.area for edge in topology.links} == {"0.0.0.0", "0.0.0.17"}
    result = compute_cspf(topology, demand())
    assert result.path_nodes == ("west", "border", "east")
    assert result.total_metric == 30
    assert [hop.address for hop in result.explicit_hops] == ["203.0.113.2", "203.0.113.6"]
    assert all(edge.available_bps() == 80_000_000 for edge in topology.links)
    assert topology.raw_metadata["complete"] is True
    assert topology.raw_metadata["execution_eligible"] is False


def test_projection_is_explicit_and_default_mapping_still_refuses_abr_collision():
    topology = parse_gobgp_snapshot(capture(), required_peers=(PEER,), node_names=NAMES, now=NOW)
    assert topology.raw_metadata["complete"] is False
    assert len(topology.links) == 1
    assert "ospf_multi_area" not in topology.raw_metadata
    with pytest.raises(CspfNoPath):
        compute_cspf(topology, demand())


def test_metadata_preserves_every_area_identity_and_exact_endpoint_provenance():
    policy = parse_ospf_multi_area_policy(policy_document())
    topology = parse(capture(), policy=policy)
    details = topology.raw_metadata["bgpls_objects"]
    abr = details["border"]
    assert abr["projection"] == "explicit-ospf-abr"
    assert abr["areas"] == abr["expected_areas"] == ["0.0.0.0", "0.0.0.17"]
    assert len({item["identity"] for item in abr["identities"]}) == 2
    assert all(item["protocol_id"] == 3 and item["instance_id"] == 47 for item in abr["identities"])
    assert all(item["descriptors"]["512"] == "0000fc00" for item in abr["identities"])
    assert all(item["descriptors"]["513"] == "0000000c" for item in abr["identities"])
    assert topology.raw_metadata["ospf_multi_area"] == policy.document()
    assert topology.raw_metadata["ospf_multi_area_digest"] == policy.digest
    for edge in topology.links:
        item = details[edge.id]
        assert item["source_identity"].startswith("bgpls-node-")
        assert item["target_identity"].startswith("bgpls-node-")
        assert item["area"] == edge.area
        assert item["source_node_descriptors"]["514"] == IPv4Address(edge.area).packed.hex()
        assert item["target_node_descriptors"]["514"] == IPv4Address(edge.area).packed.hex()


def test_withdrawn_area_half_does_not_invent_membership_or_connectivity():
    value = capture()
    value["rib"] = [record for index, record in enumerate(value["rib"]) if index not in {2, 5}]
    topology = parse(value)
    assert topology.nodes["border"].area is None
    assert topology.raw_metadata["bgpls_objects"]["border"]["areas"] == ["0.0.0.0"]
    assert len(topology.links) == 1 and topology.raw_metadata["complete"] is True
    with pytest.raises(CspfNoPath):
        compute_cspf(topology, demand())
    value["rib"] = []
    assert parse(value).nodes == {}


def test_link_endpoint_alone_cannot_supply_an_unadvertised_area_membership():
    value = capture()
    # Some exporters omit the ABR Node NLRI in one area while retaining real
    # Link NLRIs in that area. The strict policy must expose the missing
    # canonical endpoint rather than claiming it was independently advertised.
    value["rib"].pop(1)
    topology = parse(value)
    assert topology.raw_metadata["bgpls_objects"]["border"]["areas"] == ["0.0.0.17"]
    assert topology.raw_metadata["complete"] is False
    assert len(topology.links) == 1 and topology.links[0].area == "0.0.0.17"
    with pytest.raises(CspfNoPath):
        compute_cspf(topology, demand())


def test_explicit_binding_alone_cannot_create_an_absent_physical_abr():
    value = capture()
    value["rib"] = [record for index, record in enumerate(value["rib"]) if index not in {1, 2}]
    topology = parse(value)
    assert "border" not in topology.nodes
    assert topology.links == [] and topology.raw_metadata["complete"] is False


@pytest.mark.parametrize("changes", [
    {"protocol": 2}, {"instance": 48}, {"autonomous_system": 64513},
    {"bgpls_identifier": 13}, {"igp": "198.51.100.99"}, {"area": "0.0.0.18"},
    {"extra": tlv(999, b"new-domain-descriptor")},
])
def test_mismatched_domain_router_or_area_never_gets_joined(changes):
    value = capture()
    arguments = {"igp": "198.51.100.11", "area": "0.0.0.17", **changes}
    value["rib"][2] = route(node(**arguments), {1028: IPv4Address("192.0.2.11").packed})
    topology = parse(value)
    assert topology.raw_metadata["complete"] is False
    assert topology.raw_metadata["bgpls_objects"]["border"]["areas"] == ["0.0.0.0"]
    assert len(topology.links) == 1


def test_area_identity_with_changed_te_router_id_is_inconsistent_not_another_router():
    value = capture()
    value["rib"][2] = route(node("198.51.100.11", "0.0.0.17"),
                            {1028: IPv4Address("192.0.2.99").packed})
    topology = parse(value)
    assert topology.raw_metadata["complete"] is False
    assert len(topology.nodes) == 3 and len(topology.links) == 1
    assert any("TE router ID conflicts" in item["reason"] for item in topology.raw_metadata["excluded_objects"])


def test_binding_and_friendly_name_mapping_must_agree():
    topology = parse(capture(), names={**NAMES, "192.0.2.11": "different_border"})
    assert "border" not in topology.nodes and "different_border" not in topology.nodes
    assert not topology.links and topology.raw_metadata["complete"] is False


def test_link_cannot_cross_areas_under_cover_of_a_merged_abr():
    value = capture()
    value["rib"][4] = route(link("198.51.100.10", "198.51.100.11", "0.0.0.0",
                                  "203.0.113.1", "203.0.113.2", target_area="0.0.0.17"), attrs())
    topology = parse(value)
    assert len(topology.links) == 1 and topology.raw_metadata["complete"] is False
    assert any("endpoints disagree" in item["reason"] for item in topology.raw_metadata["excluded_objects"])


def test_reverse_and_parallel_resources_remain_independent_after_projection():
    value = capture()
    value["rib"] += [route(link("198.51.100.11", "198.51.100.10", "0.0.0.0",
                                "203.0.113.2", "203.0.113.1"), attrs(metric=80, capacity=1_000_000)),
                     route(link("198.51.100.11", "198.51.100.12", "0.0.0.17",
                                "203.0.113.9", "203.0.113.10"), attrs(metric=2, capacity=2_000_000))]
    topology = parse(value)
    assert len(topology.links) == 4 and len({edge.id for edge in topology.links}) == 4
    result = compute_cspf(topology, demand())
    assert result.total_metric == 13
    assert [hop.address for hop in result.explicit_hops] == ["203.0.113.2", "203.0.113.10"]
    reverse = next(edge for edge in topology.links if edge.target == "west")
    assert reverse.metric == 80 and reverse.available_bps() == 8_000_000


def test_projection_roundtrips_strict_topology_schema(tmp_path):
    topology = parse(capture())
    path = tmp_path / "multi-area.yml"
    path.write_text(yaml.safe_dump(topology_document(topology)))
    loaded = load_topology(path)
    assert loaded.nodes == topology.nodes
    assert {edge.id: edge.area for edge in loaded.links} == {edge.id: edge.area for edge in topology.links}
    assert loaded.raw_metadata == topology.raw_metadata


def test_provider_propagates_explicit_policy_into_every_validation():
    value = capture()
    replies = [value["peers_before"], value["rib"], value["peers_after"]]

    def runner(command, **kwargs):
        kwargs["stdout"].write("\n".join(json.dumps(item) for item in replies.pop(0)).encode())
        return SimpleNamespace(returncode=0)

    policy = parse_ospf_multi_area_policy(policy_document())
    provider = GoBgpTopologyProvider(proto_path=PROTO, required_peers=(PEER,), node_names=NAMES,
                                    ospf_multi_area=policy, runner=runner, clock=lambda: NOW)
    topology = provider.snapshot()
    assert topology.raw_metadata["complete"] is True
    assert topology.raw_metadata["ospf_multi_area_digest"] == policy.digest


@pytest.mark.parametrize("field,value", [
    ("schema_version", True), ("schema_version", 2), ("protocol_id", 2), ("protocol_id", True),
    ("instance_id", -1), ("instance_id", 2**64), ("autonomous_system", 2**32),
    ("bgpls_identifier", "12"), ("bindings", []), ("extra", "unknown"),
])
def test_policy_rejects_unknown_fields_protocols_and_ambiguous_domain_values(field, value):
    document = {**policy_document(), field: value}
    with pytest.raises(BgplsError):
        parse_ospf_multi_area_policy(document)


@pytest.mark.parametrize("field,value", [
    ("areas", ["0.0.0.0"]), ("areas", ["0.0.0.0", "0.0.0.0"]),
    ("areas", ["0.0.0.0", 17]), ("areas", ["0.0.0.0", "::1"]),
    ("node_id", "unsafe;command"), ("igp_router_id", "198.51.100.11/32"),
    ("te_router_id", 1234), ("extra", "unknown"),
])
def test_binding_values_require_explicit_safe_identity_and_unique_ipv4_memberships(field, value):
    document = policy_document()
    document["bindings"][0][field] = value
    with pytest.raises(BgplsError):
        parse_ospf_multi_area_policy(document)


@pytest.mark.parametrize("field", ["node_id", "te_router_id", "igp_router_id"])
def test_binding_overlap_cannot_grant_multiple_physical_identities(field):
    document = policy_document()
    original = document["bindings"][0]
    other = {"node_id": "other", "te_router_id": "192.0.2.50", "igp_router_id": "198.51.100.50",
             "areas": ["0.0.0.0", "0.0.0.18"]}
    other[field] = original[field]
    document["bindings"].append(other)
    with pytest.raises(BgplsError, match="overlapping"):
        parse_ospf_multi_area_policy(document)


def test_policy_normalization_and_snapshot_identity_are_order_independent():
    document = policy_document()
    first = parse_ospf_multi_area_policy(document)
    document["bindings"][0]["areas"].reverse()
    second = parse_ospf_multi_area_policy(document)
    assert first.document() == second.document() and first.digest == second.digest
    value = capture()
    expected = topology_document(parse(value, policy=first))
    value["rib"].reverse()
    assert topology_document(parse(value, policy=second)) == expected


def test_unvalidated_mapping_cannot_enable_projection():
    with pytest.raises(BgplsError, match="validated explicit policy"):
        parse_gobgp_snapshot(capture(), required_peers=(PEER,), now=NOW,
                             ospf_multi_area=copy.deepcopy(policy_document()))
