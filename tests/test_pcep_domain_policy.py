"""Domain discovery is independent of configured router names and tunnel IDs."""
from __future__ import annotations

import copy
import json
import os
import struct
from dataclasses import FrozenInstanceError, replace
from ipaddress import IPv4Address

import pytest
import test_bgpls_multi_area as multi_area
from test_pcep_compute import PEER, prepare
from test_pcep_delegation import initial_report

from ninja_pce.pcep_domain_policy import DomainDelegationPolicy


def document():
    return {"version": 1,
            "domain": {"protocol_id": 3, "instance_id": 0,
                       "autonomous_system": 65000, "bgpls_identifier": 0},
            "pcc_prefixes": ["10.255.0.0/24"], "destination_prefixes": ["10.255.0.0/24"],
            "max_bandwidth_bps": 100_000_000, "max_live_lsps": 32}


def report(*, source=PEER, endpoint="10.255.0.6", tunnel=10030):
    value = initial_report()
    identifiers = value["identifiers"]
    identifiers.update(sender=source, endpoint=endpoint, tunnel_id=tunnel,
                       extended_tunnel_id_hex=IPv4Address(source).packed.hex())
    wire = (IPv4Address(source).packed + struct.pack("!HH", identifiers["lsp_id"], tunnel) +
            IPv4Address(source).packed + IPv4Address(endpoint).packed).hex()
    identifiers["value_hex"] = wire
    name = f"Tunnel{tunnel}_w".encode().hex()
    value["symbolic_name_hex"] = name
    for item in value["tlvs"]:
        if item["type"] == 18:
            item["value_hex"] = wire
        elif item["type"] == 17:
            item["value_hex"] = name
    return value


@pytest.fixture
def topology(tmp_path, monkeypatch):
    return prepare(tmp_path, monkeypatch)._snapshot()[0]


@pytest.fixture
def policy():
    return DomainDelegationPolicy.from_document(document())


@pytest.mark.parametrize("source", [PEER, "10.255.0.2", "10.255.0.4", "10.255.0.5"])
@pytest.mark.parametrize("tunnel", [1, 1003, 10030, 65535])
def test_new_ids_and_unregistered_headends_are_discovered_from_domain(policy, topology, source, tunnel):
    delegated = report(source=source, tunnel=tunnel)
    before = copy.deepcopy(topology.raw_metadata)
    assert policy.source(source, topology) == source
    assert policy.allows(source, delegated, topology)
    assert policy.max_live_lsps(source) == 32
    assert topology.raw_metadata == before


def test_endpoint_must_be_current_unique_domain_member(policy, topology):
    assert policy.destination("10.255.0.6", topology)
    assert not policy.destination("10.255.0.99", topology)
    assert not policy.allows(PEER, report(endpoint="10.255.0.99"), topology)
    node = next(node for node in topology.nodes.values() if node.loopback == "10.255.0.6")
    topology.nodes["duplicate"] = replace(node, id="duplicate")
    assert not policy.destination("10.255.0.6", topology)
    assert not policy.allows(PEER, report(), topology)


def test_transport_sender_and_advertised_loopback_must_match(policy, topology):
    assert policy.source("10.255.0.99", topology) is None
    assert not policy.allows("10.255.0.4", report(), topology)
    node = next(node for node in topology.nodes.values() if node.loopback == PEER)
    topology.nodes["duplicate"] = replace(node, id="duplicate")
    assert policy.source(PEER, topology) is None
    assert not policy.allows(PEER, report(), topology)


@pytest.mark.parametrize("key,value", [("protocol_id", 2), ("instance_id", 8)])
def test_foreign_protocol_and_instance_never_grant_authority(policy, topology, key, value):
    node = next(node for node in topology.nodes.values() if node.loopback == PEER)
    topology.raw_metadata["bgpls_objects"][node.id][key] = value
    assert policy.source(PEER, topology) is None


@pytest.mark.parametrize("key,value", [("512", "0000fde9"), ("513", "00000001")])
def test_foreign_asn_and_bgpls_identifier_never_grant_authority(policy, topology, key, value):
    node = next(node for node in topology.nodes.values() if node.loopback == PEER)
    topology.raw_metadata["bgpls_objects"][node.id]["descriptors"][key] = value
    assert policy.source(PEER, topology) is None


def test_every_abr_identity_must_match_and_membership_order_is_irrelevant(policy, topology):
    node = next(node for node in topology.nodes.values() if node.loopback == PEER)
    member = topology.raw_metadata["bgpls_objects"][node.id]
    second = copy.deepcopy(member)
    second["identity"] += "-area1"
    second["descriptors"]["514"] = "00000001"
    topology.raw_metadata["bgpls_objects"][node.id] = {
        "projection": "explicit-ospf-abr", "identities": [member, second]}
    before = policy.proof(PEER, report(), topology)
    assert before is not None
    topology.raw_metadata["bgpls_objects"][node.id]["identities"].reverse()
    assert policy.proof(PEER, report(), topology) == before
    second["descriptors"]["512"] = "0000fde9"
    assert policy.source(PEER, topology) is None


def test_real_projected_multi_area_abr_is_discovered_without_headend_registration():
    topology = multi_area.parse(multi_area.capture())
    raw = document()
    raw["domain"] = {"protocol_id": 3, "instance_id": 47,
                     "autonomous_system": 64512, "bgpls_identifier": 12}
    raw["pcc_prefixes"] = ["192.0.2.0/24"]
    raw["destination_prefixes"] = ["192.0.2.0/24"]
    policy = DomainDelegationPolicy.from_document(raw)
    delegated = report(source="192.0.2.11", endpoint="192.0.2.12")
    assert policy.source("192.0.2.11", topology) == "192.0.2.11"
    assert policy.allows("192.0.2.11", delegated, topology)
    scoped = policy.graph(topology)
    assert len(scoped.nodes) == 3 and len(scoped.links) == 2
    assert len(scoped.raw_metadata["bgpls_objects"]["border"]["identities"]) == 2


@pytest.mark.parametrize("members", [[], [None], [True], "invalid"])
def test_malformed_abr_membership_never_grants_authority(policy, topology, members):
    node = next(node for node in topology.nodes.values() if node.loopback == PEER)
    topology.raw_metadata["bgpls_objects"][node.id] = {"identities": members}
    assert policy.source(PEER, topology) is None


def test_graph_excludes_foreign_nodes_and_foreign_links_without_mutation(policy, topology):
    original = copy.deepcopy(topology)
    foreign = next(node for node in topology.nodes.values() if node.loopback == "10.255.0.2")
    topology.raw_metadata["bgpls_objects"][foreign.id]["instance_id"] = 1
    remaining = next(link for link in topology.links if foreign.id not in (link.source, link.target))
    topology.raw_metadata["bgpls_objects"][remaining.id]["protocol_id"] = 2
    before = copy.deepcopy(topology)
    scoped = policy.graph(topology)
    assert foreign.id not in scoped.nodes
    assert remaining.id not in {link.id for link in scoped.links}
    assert all(link.source in scoped.nodes and link.target in scoped.nodes for link in scoped.links)
    assert topology == before
    assert len(scoped.nodes) == len(original.nodes) - 1


def test_graph_keeps_domain_transit_outside_pcc_and_destination_prefixes(topology):
    raw = document()
    raw["pcc_prefixes"] = [PEER + "/32"]
    raw["destination_prefixes"] = ["10.255.0.6/32"]
    scoped = DomainDelegationPolicy.from_document(raw)
    assert scoped.source("10.255.0.4", topology) is None
    assert not scoped.destination("10.255.0.4", topology)
    assert len(scoped.graph(topology).nodes) == len(topology.nodes)
    assert len(scoped.graph(topology).links) == len(topology.links)


def test_proof_is_stable_across_poll_and_display_name_changes(policy, topology):
    delegated = report()
    before = policy.proof(PEER, delegated, topology)
    assert before is not None
    topology.raw_metadata["timestamp"] = "2099-01-01T00:00:00+00:00"
    for item in topology.raw_metadata["bgpls_objects"].values():
        item["advertised_name"] = "new-label"
    old_nodes = topology.nodes
    renamed = {key: "renamed-" + key for key in old_nodes}
    topology = replace(topology,
                       nodes={renamed[key]: replace(node, id=renamed[key]) for key, node in old_nodes.items()},
                       links=[replace(link, source=renamed[link.source], target=renamed[link.target])
                              for link in topology.links])
    topology.raw_metadata["bgpls_objects"] = {
        renamed.get(key, key): value for key, value in topology.raw_metadata["bgpls_objects"].items()}
    assert policy.proof(PEER, delegated, topology) == before


def test_proof_binds_bandwidth_lspa_and_wire_identity(policy, topology):
    delegated = report()
    before = policy.proof(PEER, delegated, topology)
    assert before is not None
    for attr in delegated["attributes"]:
        if attr["class"] == 5:
            attr["body_hex"] = struct.pack("!f", 125000).hex()
    changed = policy.proof(PEER, delegated, topology)
    assert changed is not None and changed != before
    lspa = next(attr for attr in delegated["attributes"] if attr["class"] == 9)
    lspa["body_hex"] = bytes(12).hex() + bytes((6, 5, 0, 0)).hex()
    assert policy.proof(PEER, delegated, topology) not in (None, changed, before)
    delegated["identifiers"]["tunnel_id"] += 1
    assert policy.proof(PEER, delegated, topology) is None


@pytest.mark.parametrize("state", [1, 2])
def test_current_active_path_with_recorded_route_can_be_retained_without_initial_write(policy, topology, state):
    delegated = report()
    delegated.update(operational_code=state, operational_state={1: "UP", 2: "ACTIVE"}[state], srp_id=31)
    raw = bytes((1, 8)) + IPv4Address("10.0.13.3").packed + bytes((32, 0))
    delegated["ero"] = [{"type": 1, "raw_hex": raw.hex(), "address": "10.0.13.3",
                         "prefix_length": 32, "loose": False}]
    delegated["rro"] = [{"type": 1, "raw_hex": raw.hex(), "address": "10.0.13.3", "prefix_length": 32}]
    delegated["attributes"].append({"class": 8, "type": 1, "body_hex": raw.hex()})
    before = copy.deepcopy(delegated)
    assert policy.allows(PEER, delegated, topology)
    assert delegated == before
    delegated["rro"][0]["address"] = "10.0.34.4"
    assert not policy.allows(PEER, delegated, topology)


@pytest.mark.parametrize("change", ["loose", "protection", "unknown", "duplicate", "missing_lspa"])
def test_unsupported_active_profile_cannot_be_adopted(policy, topology, change):
    delegated = report()
    delegated.update(operational_code=2, operational_state="ACTIVE")
    raw = bytes((1, 8)) + IPv4Address("10.0.13.3").packed + bytes((32, 0))
    delegated["ero"] = [{"type": 1, "raw_hex": raw.hex(), "address": "10.0.13.3",
                         "prefix_length": 32, "loose": False}]
    if change == "loose":
        delegated["ero"][0]["loose"] = True
    elif change == "protection":
        lspa = next(attr for attr in delegated["attributes"] if attr["class"] == 9)
        lspa["body_hex"] = bytes(12).hex() + bytes((7, 7, 1, 0)).hex()
    elif change == "unknown":
        delegated["attributes"].append({"class": 99, "type": 1, "body_hex": ""})
    elif change == "duplicate":
        delegated["tlvs"].append(copy.deepcopy(delegated["tlvs"][0]))
    elif change == "missing_lspa":
        delegated["attributes"] = [attr for attr in delegated["attributes"] if attr["class"] != 9]
    assert not policy.allows(PEER, delegated, topology)


@pytest.mark.parametrize("field,value", [
    ("sync", True), ("delegate", False), ("removed", True), ("administrative_up", False),
    ("created", True), ("end_of_sync", True), ("stale", True), ("srp_id", 1),
    ("plsp_id", 0), ("plsp_id", 0xFFFFF), ("operational_code", 4),
])
def test_historical_and_unsupported_reports_never_authorize(policy, topology, field, value):
    delegated = report()
    delegated[field] = value
    assert not policy.allows(PEER, delegated, topology)


@pytest.mark.parametrize("amount", [0, -1, float("nan"), float("inf"), 100_000_001])
def test_invalid_or_excessive_bandwidth_never_authorizes(policy, topology, amount):
    delegated = report()
    for attr in delegated["attributes"]:
        if attr["class"] == 5:
            attr["body_hex"] = struct.pack("!f", amount).hex()
    assert not policy.allows(PEER, delegated, topology)


@pytest.mark.parametrize("field,value", [
    ("max_live_lsps", 0), ("max_live_lsps", 257), ("max_live_lsps", True),
    ("max_bandwidth_bps", 0), ("max_bandwidth_bps", 10**12 + 1),
    ("max_bandwidth_bps", float("nan")), ("max_bandwidth_bps", True), ("version", True),
    ("pcc_prefixes", []), ("pcc_prefixes", ["0.0.0.0/0"]),
    ("pcc_prefixes", ["10.255.0.1/24"]), ("pcc_prefixes", ["10.255.0.1"]),
    ("pcc_prefixes", ["224.0.0.0/4"]), ("destination_prefixes", ["::/64"]),
    ("pcc_prefixes", ["10.255.0.0/24", "10.255.0.0/24"]),
])
def test_invalid_policy_bounds_are_rejected(field, value):
    raw = document()
    raw[field] = value
    with pytest.raises(ValueError):
        DomainDelegationPolicy.from_document(raw)


@pytest.mark.parametrize("field,value", [
    ("protocol_id", 0), ("protocol_id", 4), ("protocol_id", True),
    ("instance_id", -1), ("instance_id", 2**64), ("instance_id", False),
    ("autonomous_system", 0), ("autonomous_system", 2**32),
    ("bgpls_identifier", -1), ("bgpls_identifier", 2**32),
])
def test_invalid_domain_identity_is_rejected(field, value):
    raw = document()
    raw["domain"][field] = value
    with pytest.raises(ValueError):
        DomainDelegationPolicy.from_document(raw)


def test_policy_is_copied_immutable_and_canonical(topology):
    raw = document()
    policy = DomainDelegationPolicy.from_document(raw)
    proof = policy.proof(PEER, report(), topology)
    raw["domain"]["autonomous_system"] = 1
    raw["pcc_prefixes"].clear()
    assert policy.proof(PEER, report(), topology) == proof
    with pytest.raises(FrozenInstanceError):
        policy.max_bandwidth_bps = 1
    assert policy.pcc_prefixes[0].with_prefixlen == "10.255.0.0/24"
    # IPv4Network permits reassignment internally; the exposed view is a copy.
    policy.pcc_prefixes[0].network_address = IPv4Address("192.0.2.0")
    assert policy.pcc_prefixes[0].with_prefixlen == "10.255.0.0/24"
    assert policy.proof(PEER, report(), topology) == proof
    assert policy.max_live_lsps("192.0.2.1") is None
    assert policy.max_live_lsps("bad-address") is None


def test_unknown_policy_fields_missing_fields_and_duplicate_json_are_rejected(tmp_path):
    raw = document()
    raw["extra"] = True
    with pytest.raises(ValueError):
        DomainDelegationPolicy.from_document(raw)
    raw = document()
    del raw["domain"]["instance_id"]
    with pytest.raises(ValueError):
        DomainDelegationPolicy.from_document(raw)
    path = tmp_path / "policy.json"
    path.write_text('{"version":1,"version":1}')
    with pytest.raises(ValueError, match="Duplicate"):
        DomainDelegationPolicy.from_path(path)
    path.write_text('{"version":NaN}')
    with pytest.raises(ValueError, match="Nonfinite"):
        DomainDelegationPolicy.from_path(path)


def test_policy_file_is_bounded_regular_and_not_a_symlink(tmp_path):
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(document()))
    assert DomainDelegationPolicy.from_path(path).max_live_lsps(PEER) == 32
    link = tmp_path / "policy-link.json"
    link.symlink_to(path)
    with pytest.raises(OSError):
        DomainDelegationPolicy.from_path(link)
    fifo = tmp_path / "policy-fifo.json"
    os.mkfifo(fifo)
    with pytest.raises(ValueError, match="regular"):
        DomainDelegationPolicy.from_path(fifo)
    path.write_bytes(b" " * 1_048_577)
    with pytest.raises(ValueError, match="bounded"):
        DomainDelegationPolicy.from_path(path)
