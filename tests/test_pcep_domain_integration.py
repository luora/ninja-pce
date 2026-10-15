"""Domain discovery uses real BGP-LS evidence without router/tunnel registries."""
from __future__ import annotations

import asyncio
import copy
import json
from dataclasses import replace

import pytest
from test_pcep_compute import PEER, prepare, request
from test_pcep_delegation import initial_body
from test_pcep_policy_server import _body

from ninja_pce import pcep
from ninja_pce.cli import main
from ninja_pce.pcep import parse_reports
from ninja_pce.pcep_compute import PcepPathComputer
from ninja_pce.pcep_delegation import DelegatedPathComputer
from ninja_pce.pcep_domain_policy import DomainDelegationPolicy


def policy_document():
    return {"version": 1, "domain": {"protocol_id": 3, "instance_id": 0,
            "autonomous_system": 65000, "bgpls_identifier": 0},
            "pcc_prefixes": ["10.255.0.0/24"], "destination_prefixes": ["10.255.0.0/24"],
            "max_bandwidth_bps": 100_000_000, "max_live_lsps": 32}


def discovered(tmp_path, monkeypatch):
    old = prepare(tmp_path, monkeypatch)
    return PcepPathComputer(raw_path=old.raw_path, status_path=old.status_path,
                            topology_path=old.topology_path, required_peers=old.required_peers,
                            node_names={}, headends={},
                            domain_policy=DomainDelegationPolicy.from_document(policy_document()))


def test_new_router_without_headend_or_name_registration_computes(tmp_path, monkeypatch):
    computer = discovered(tmp_path, monkeypatch)
    peer = "10.255.0.5"  # Transit router, no role exemption or static registration.
    assert computer.validated_peer(peer)
    result = computer(peer, request(source=peer, destination=PEER))
    assert result.outcomes[0]["status"] == "PATH"
    assert result.outcomes[0]["source"] == peer
    assert computer.validate_reply(result)
    assert computer.headends == {} and computer.node_names == {}


@pytest.mark.parametrize("tunnel", [7, 10030, 65535])
def test_any_new_tunnel_id_gets_exact_domain_bound_initial_path(tmp_path, monkeypatch, tunnel):
    computer = discovered(tmp_path, monkeypatch)
    delegated = DelegatedPathComputer(computer, {})
    report = parse_reports(_body(tunnel), allow_delegation=True)[0]
    result = delegated(PEER, [report], (81,))
    outcome = result.outcomes[0]
    assert outcome["status"] == "INITIAL_PATH"
    assert outcome["tunnel_id"] == tunnel and outcome["domain_policy_digest"]
    assert delegated.validate_authorization(PEER, report, outcome)
    assert delegated.allowed_tunnels == {} and computer.headends == {}
    topology, evidence = computer.validated_snapshot()
    node = next(key for key, node in topology.nodes.items() if node.loopback == PEER)
    withdrawn = replace(topology, nodes={key: value for key, value in topology.nodes.items() if key != node})
    monkeypatch.setattr(computer, "validated_snapshot", lambda: (withdrawn, evidence))
    assert not delegated.validate_authorization(PEER, report, outcome)


def test_stale_ted_and_wrong_session_source_cannot_grant_domain_authority(tmp_path, monkeypatch):
    computer = discovered(tmp_path, monkeypatch)
    delegated = DelegatedPathComputer(computer, {})
    report = parse_reports(initial_body(), allow_delegation=True)[0]
    assert not delegated.policy_allows("10.255.0.5", report)
    assert delegated("10.255.0.5", [report], (1,)).outcomes[0]["status"] == "DELEGATION_RETURNED"
    computer.topology_path.unlink()
    assert not delegated.policy_allows(PEER, report)
    assert not delegated.adopt_active(PEER, report)


def test_static_legacy_mapping_does_not_exempt_new_tunnels_from_domain_graph(tmp_path, monkeypatch):
    computer = discovered(tmp_path, monkeypatch)
    computer.headends[PEER] = PEER
    topology, evidence = computer.validated_snapshot()
    altered = copy.deepcopy(topology)
    # The cheapest old route goes through .3; foreign-domain metadata excludes
    # that transit node, even though the source has an explicit legacy binding.
    node = next(key for key, value in altered.nodes.items() if value.loopback == "10.255.0.3")
    altered.raw_metadata["bgpls_objects"][node]["descriptors"]["512"] = "0000fde9"
    monkeypatch.setattr(computer, "_snapshot", lambda: (altered, evidence))
    result = computer(PEER, request())
    assert result.outcomes[0]["status"] == "PATH"
    assert node not in result.outcomes[0]["path_nodes"]


def test_static_binding_does_not_override_domain_membership_or_bandwidth(tmp_path, monkeypatch):
    computer = discovered(tmp_path, monkeypatch)
    computer.headends[PEER] = PEER
    assert computer(PEER, request(bandwidth=20_000_000)).outcomes[0]["status"] == "REJECTED"
    topology, evidence = computer.validated_snapshot()
    altered = copy.deepcopy(topology)
    node = next(key for key, value in altered.nodes.items() if value.loopback == PEER)
    altered.raw_metadata["bgpls_objects"][node]["descriptors"]["512"] = "0000fde9"
    monkeypatch.setattr(computer, "_snapshot", lambda: (altered, evidence))
    assert computer(PEER, request()).outcomes[0]["status"] == "REJECTED"


def test_domain_only_cli_has_no_static_router_or_name_options(tmp_path, monkeypatch):
    policy = tmp_path / "domain.json"
    policy.write_text(json.dumps(policy_document()))
    observed = {}

    class Server:
        addresses = [("127.0.0.1", 4189)]

        def __init__(self, config, **options):
            observed.update(config=config, options=options)

        async def start(self):
            pass

        async def serve_forever(self):
            raise asyncio.CancelledError

        async def close(self):
            pass

    monkeypatch.setattr(pcep, "PcepServer", Server)
    args = ["pcep-listen", "--capture", str(tmp_path / "pcep.json"),
            "--compute-domain-policy", str(policy),
            "--compute-bgpls-raw", str(tmp_path / "raw.json"),
            "--compute-bgpls-status", str(tmp_path / "status.json"),
            "--compute-topology", str(tmp_path / "ted.json"),
            "--compute-bgpls-peer", "192.0.2.1"]
    assert main(args) == 0
    assert observed["config"].allowed_peers == ()
    assert observed["config"].allowed_peer_networks == ("10.255.0.0/24",)
    delegated = observed["options"]["delegated_computer"]
    assert delegated.domain_enabled and delegated.allowed_tunnels == {}
    assert delegated.computer.headends == {} and delegated.computer.node_names == {}
    # A policy input cannot also be a listener output or another state file.
    assert main([*args, "--capture", str(policy)]) == 1


def test_domain_cli_requires_live_computation_and_keeps_passive_scope_explicit(tmp_path):
    policy = tmp_path / "domain.json"
    policy.write_text(json.dumps(policy_document()))
    assert main(["pcep-listen", "--capture", str(tmp_path / "capture.json"),
                 "--compute-domain-policy", str(policy)]) == 1
    assert main(["pcep-listen", "--capture", str(tmp_path / "capture.json")]) == 1
