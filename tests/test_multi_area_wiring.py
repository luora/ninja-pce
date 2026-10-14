"""Offline collector/computation policy wiring over synthetic OSPFv2 wire data."""
from __future__ import annotations

import copy
import json
import struct
from datetime import datetime, timedelta, timezone
from ipaddress import IPv4Address
from pathlib import Path
from types import SimpleNamespace

import pytest
import test_pcep_compute_cli as computation_cli
from test_bgpls import capture, descriptor, node, route, te_attrs, tlv
from test_live import collector_args
from test_pcep_compute import PEER, request
from test_pcep_compute_cli import base_args, compute_options, flatten

import ninja_pce.pcep_compute as computation
from ninja_pce import bgpls
from ninja_pce.bgpls import BgplsError, parse_gobgp_snapshot, parse_ospf_multi_area_policy, topology_document
from ninja_pce.cli import main
from ninja_pce.pcep_compute import PcepPathComputer
from ninja_pce.state import load_state, write_state

offline_server = computation_cli.offline_server

EXPORTER = "10.255.0.5"
DESTINATION = "10.255.0.8"
NAMES = {PEER: "PE1", EXPORTER: "P2", DESTINATION: "PE4"}


def policy_document() -> dict:
    return {
        "schema_version": 1, "protocol_id": 3, "instance_id": 0,
        "autonomous_system": 65000, "bgpls_identifier": 0,
        "bindings": [{"node_id": "P2", "te_router_id": EXPORTER, "igp_router_id": EXPORTER,
                      "areas": ["0.0.0.0", "0.0.0.1"]}],
    }


def policy_file(tmp_path: Path) -> Path:
    path = tmp_path / "ospf-policy.json"
    path.write_text(json.dumps(policy_document()))
    return path


def area_link(source: str, target: str, local: str, remote: str, *, area: int) -> bytes:
    payload = (bytes([3]) + bytes(8) + tlv(256, descriptor(source, area=area))
               + tlv(257, descriptor(target, area=area)) + tlv(259, IPv4Address(local).packed)
               + tlv(260, IPv4Address(remote).packed))
    return struct.pack("!HH", 2, len(payload)) + payload


def multi_area_capture() -> dict:
    value = capture()
    now = datetime.now(timezone.utc)
    value["captured_at"] = now.isoformat()
    value["poll_started_at"] = (now - timedelta(seconds=1)).isoformat()
    for key in ("peers_before", "peers_after"):
        value[key][0]["peer"]["state"]["neighborAddress"] = EXPORTER
    value["rib"] = [
        route(node(router, area=area), {1028: IPv4Address(router).packed})
        for router, area in ((PEER, 0), (EXPORTER, 0), (EXPORTER, 1), (DESTINATION, 1))
    ] + [
        route(area_link(PEER, EXPORTER, "10.0.15.1", "10.0.15.5", area=0), te_attrs()),
        route(area_link(EXPORTER, DESTINATION, "10.0.58.5", "10.0.58.8", area=1), te_attrs()),
    ]
    for item in value["rib"]:
        item["destination"]["paths"][0]["neighborIp"] = EXPORTER
    return value


def prepare(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, document: dict | None = None,
            value: dict | None = None) -> PcepPathComputer:
    policy = parse_ospf_multi_area_policy(document or policy_document())
    value = value or multi_area_capture()
    now = datetime.fromisoformat(value["captured_at"])
    monkeypatch.setattr(computation, "_utcnow", lambda: now)
    topology = parse_gobgp_snapshot(value, node_names=NAMES, required_peers=(EXPORTER,),
                                    now=now, ospf_multi_area=policy)
    (tmp_path / "raw.json").write_text(json.dumps(value))
    (tmp_path / "topology.json").write_text(json.dumps(topology_document(topology)))
    write_state(tmp_path / "status.json", {
        "status": "ok", "observed_at": now.timestamp(), "nodes": len(topology.nodes),
        "directed_links": len(topology.links), "metadata": topology.raw_metadata,
    })
    return PcepPathComputer(raw_path=tmp_path / "raw.json", status_path=tmp_path / "status.json",
                            topology_path=tmp_path / "topology.json", required_peers=(EXPORTER,),
                            node_names=NAMES, headends={PEER: PEER}, ospf_multi_area=policy)


def test_explicit_policy_computes_numbered_inter_area_path(tmp_path, monkeypatch):
    computer = prepare(tmp_path, monkeypatch)
    reply = computer(PEER, request(destination=DESTINATION, bandwidth=125_000))
    assert reply.outcomes[0]["status"] == "PATH"
    assert reply.outcomes[0]["path_nodes"] == ["PE1", "P2", "PE4"]
    assert reply.outcomes[0]["ero"] == ["10.0.15.5", "10.0.58.8"]
    assert computer.validate_reply(reply)
    topology, _ = computer.validated_snapshot()
    assert topology.nodes["P2"].area is None
    assert {edge.source: edge.area for edge in topology.links} == {"PE1": "0.0.0.0", "P2": "0.0.0.1"}
    assert topology.raw_metadata["execution_eligible"] is False


@pytest.mark.parametrize("filename", ["status.json", "topology.json"])
@pytest.mark.parametrize("mutation", ["missing", "digest", "document"])
def test_computation_rejects_missing_or_mismatched_policy_provenance(tmp_path, monkeypatch, filename, mutation):
    computer = prepare(tmp_path, monkeypatch)
    path = tmp_path / filename
    value = load_state(path) if filename == "status.json" else json.loads(path.read_text())
    metadata = value["metadata"]
    if mutation == "missing":
        metadata.pop("ospf_multi_area")
    elif mutation == "digest":
        metadata["ospf_multi_area_digest"] = "0" * 64
    else:
        metadata["ospf_multi_area"]["instance_id"] = 1
    if filename == "status.json":
        write_state(path, value)
    else:
        path.write_text(json.dumps(value))
    with pytest.raises(BgplsError, match="OSPF area policy"):
        computer.validated_snapshot()
    assert computer(PEER, request(destination=DESTINATION)).outcomes[0]["status"] == "TOPOLOGY_UNAVAILABLE"


def test_default_computer_cannot_consume_policy_normalized_feed(tmp_path, monkeypatch):
    configured = prepare(tmp_path, monkeypatch)
    computer = PcepPathComputer(raw_path=configured.raw_path, status_path=configured.status_path,
                                topology_path=configured.topology_path, required_peers=(EXPORTER,),
                                node_names=NAMES, headends={PEER: PEER})
    with pytest.raises(BgplsError, match="OSPF area policy"):
        computer.validated_snapshot()


def test_topology_digest_binds_identity_policy_as_well_as_raw_capture(tmp_path, monkeypatch):
    value = multi_area_capture()
    first = prepare(tmp_path, monkeypatch, value=value)
    reply = first(PEER, request(destination=DESTINATION, bandwidth=125_000))
    changed = policy_document()
    changed["bindings"][0]["areas"].append("0.0.0.2")
    second = prepare(tmp_path, monkeypatch, document=changed, value=value)
    second_reply = second(PEER, request(destination=DESTINATION, bandwidth=125_000))
    assert reply.outcomes[0]["status"] == second_reply.outcomes[0]["status"] == "PATH"
    assert reply.topology_digest != second_reply.topology_digest
    assert not second.validate_reply(reply)
    assert not first.validate_reply(reply)


def test_computation_policy_must_match_explicit_node_mapping(tmp_path):
    with pytest.raises(ValueError, match="node map"):
        PcepPathComputer(raw_path=tmp_path / "raw", status_path=tmp_path / "status",
                         topology_path=tmp_path / "topology", required_peers=(EXPORTER,),
                         node_names={PEER: "PE1", EXPORTER: "OTHER"}, headends={PEER: PEER},
                         ospf_multi_area=parse_ospf_multi_area_policy(policy_document()))


def test_collector_cli_passes_policy_to_transport_and_publishes_provenance(tmp_path, monkeypatch):
    value = multi_area_capture()
    seen = {}

    def provider(**kwargs):
        seen.update(kwargs)
        return SimpleNamespace(capture=lambda: copy.deepcopy(value))

    monkeypatch.setattr(bgpls, "GoBgpTopologyProvider", provider)
    names = tmp_path / "names.json"
    names.write_text(json.dumps(NAMES))
    args = collector_args(tmp_path)
    args[args.index("--peer") + 1] = EXPORTER
    assert main([*args, "--node-map", str(names), "--ospf-multi-area-policy", str(policy_file(tmp_path)),
                 "--endpoint", "127.0.0.1:50052", "--once"]) == 0
    policy = seen["ospf_multi_area"]
    assert seen["endpoint"] == "127.0.0.1:50052"
    status = load_state(tmp_path / "status.json")
    feed = json.loads((tmp_path / "ted.json").read_text())
    assert status["nodes"] == 3 and status["directed_links"] == 2
    assert status["metadata"]["ospf_multi_area_digest"] == policy.digest
    assert feed["metadata"]["ospf_multi_area"] == policy.document()


@pytest.mark.parametrize("output", ["--output", "--status", "--raw"])
def test_collector_policy_input_cannot_be_overwritten(tmp_path, monkeypatch, output):
    monkeypatch.setattr(bgpls, "GoBgpTopologyProvider", lambda **kwargs: SimpleNamespace(capture=multi_area_capture))
    policy = policy_file(tmp_path)
    before = policy.read_bytes()
    assert main([*collector_args(tmp_path), "--ospf-multi-area-policy", str(policy), output, str(policy),
                 "--once"]) == 1
    assert policy.read_bytes() == before


def test_cli_policy_requires_full_computation_options(tmp_path, offline_server):
    assert main([*base_args(tmp_path), "--compute-ospf-multi-area-policy", str(policy_file(tmp_path))]) == 1
    assert not offline_server


def test_pcep_cli_passes_policy_without_granting_update_authority(tmp_path, offline_server):
    options = compute_options(tmp_path)
    Path(options[4][1]).write_text(json.dumps(NAMES))
    assert main([*base_args(tmp_path), *flatten(options), "--compute-ospf-multi-area-policy",
                 str(policy_file(tmp_path))]) == 0
    computer = offline_server["options"]["path_computer"]
    assert computer.ospf_multi_area.document() == policy_document()
    assert "delegated_computer" not in offline_server["options"]
    capture = offline_server["server"].snapshot()
    assert capture["capabilities"]["lsp_update"] is False
    assert capture["capabilities"]["lsp_initiation"] is False
    assert capture["topology_policy"] == {"ospf_multi_area": computer.ospf_multi_area.document(),
                                          "ospf_multi_area_digest": computer.ospf_multi_area.digest}


def test_default_computation_capture_has_no_multi_area_policy(tmp_path, offline_server):
    assert main([*base_args(tmp_path), *flatten(compute_options(tmp_path))]) == 0
    assert "topology_policy" not in offline_server["server"].snapshot()


@pytest.mark.parametrize("input_index", [None, 0, 1, 2, 4])
def test_pcep_policy_cannot_alias_capture_or_live_inputs(tmp_path, offline_server, input_index):
    options = compute_options(tmp_path)
    path = tmp_path / "pcep.json" if input_index is None else Path(options[input_index][1])
    assert main([*base_args(tmp_path), *flatten(options), "--compute-ospf-multi-area-policy", str(path)]) == 1
    assert not offline_server
