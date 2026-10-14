"""Explicit dashboard identity authority over synthetic multi-area wire captures.

The three-node fixture uses distinct IGP and TE router IDs and area 17, so the
dashboard cannot succeed through lab-specific names, addresses, or area IDs.
"""
from __future__ import annotations

import json
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest
from test_bgpls_multi_area import NAMES, NOW, PEER, capture, policy_document

from ninja_pce import dashboard
from ninja_pce.bgpls import parse_gobgp_snapshot, parse_ospf_multi_area_policy, topology_document
from ninja_pce.cli import main
from ninja_pce.dashboard import DashboardConfig, DashboardStore
from ninja_pce.state import load_state, write_state


def publish(directory: Path) -> DashboardConfig:
    policy = parse_ospf_multi_area_policy(policy_document())
    config = DashboardConfig(
        topology_path=directory / "topology.json", bgpls_status_path=directory / "status.json",
        bgpls_raw_path=directory / "raw.json", pcep_path=directory / "pcep.json",
        state_dir=directory / "state", ospf_multi_area=policy,
    )
    value = capture()
    topology = parse_gobgp_snapshot(value, required_peers=(PEER,), node_names=NAMES, now=NOW,
                                    ospf_multi_area=policy)
    config.bgpls_raw_path.write_text(json.dumps(value))
    config.topology_path.write_text(json.dumps(topology_document(topology)))
    write_state(config.bgpls_status_path, {
        "status": "ok", "observed_at": NOW.timestamp(), "nodes": len(topology.nodes),
        "directed_links": len(topology.links), "metadata": topology.raw_metadata,
    })
    return config


def source(snapshot: dict, identifier: str) -> dict:
    return next(item for item in snapshot["sources"] if item["id"] == identifier)


def test_explicit_policy_revalidates_physical_abr_against_actual_wire_descriptors(tmp_path):
    config = publish(tmp_path)
    snapshot = DashboardStore(config).snapshot(now=NOW)
    assert source(snapshot, "topology")["state"] == source(snapshot, "bgpls")["state"] == "fresh"
    assert snapshot["summary"]["nodes"] == 3 and snapshot["summary"]["directed_links"] == 2
    assert {item["id"] for item in snapshot["topology"]["nodes"]} == {"west", "border", "east"}
    links = snapshot["topology"]["links"]
    assert {(item["source"], item["target"]) for item in links} == {("west", "border"), ("border", "east")}
    assert {item["remote_address"] for item in links} == {"203.0.113.2", "203.0.113.6"}
    assert next(item for item in snapshot["peers"] if item["protocol"] == "BGP-LS")["fresh"] is True
    assert snapshot["read_only"] is True


@pytest.mark.parametrize("age", [0, 60])
def test_default_dashboard_rejects_multi_area_metadata_even_when_retained(tmp_path, age):
    config = replace(publish(tmp_path), ospf_multi_area=None)
    snapshot = DashboardStore(config).snapshot(now=NOW + timedelta(seconds=age))
    assert source(snapshot, "topology")["state"] == source(snapshot, "bgpls")["state"] == "invalid"
    assert snapshot["topology"]["nodes"] == snapshot["topology"]["links"] == []
    assert not any(item["fresh"] for item in snapshot["peers"])


@pytest.mark.parametrize("filename", ["topology.json", "status.json", "both"])
@pytest.mark.parametrize("change", ["missing_document", "missing_digest", "digest", "domain", "areas"])
def test_feed_and_status_policy_must_both_equal_explicit_configuration(tmp_path, filename, change):
    config = publish(tmp_path)
    targets = [config.topology_path, config.bgpls_status_path] if filename == "both" else [tmp_path / filename]
    for path in targets:
        document = load_state(path) if path == config.bgpls_status_path else json.loads(path.read_text())
        metadata = document["metadata"]
        if change == "missing_document":
            metadata.pop("ospf_multi_area")
        elif change == "missing_digest":
            metadata.pop("ospf_multi_area_digest")
        elif change == "digest":
            metadata["ospf_multi_area_digest"] = "0" * 64
        elif change == "domain":
            metadata["ospf_multi_area"]["instance_id"] += 1
        else:
            metadata["ospf_multi_area"]["bindings"][0]["areas"] = ["0.0.0.0", "0.0.0.18"]
        if path == config.bgpls_status_path:
            write_state(path, document)
        else:
            path.write_text(json.dumps(document))
    snapshot = DashboardStore(config).snapshot(now=NOW)
    assert source(snapshot, "bgpls")["state"] == source(snapshot, "topology")["state"] == "invalid"
    assert snapshot["topology"]["nodes"] == snapshot["topology"]["links"] == []


def test_default_cannot_accept_projection_by_stripping_policy_metadata(tmp_path):
    config = replace(publish(tmp_path), ospf_multi_area=None)
    for path in (config.topology_path, config.bgpls_status_path):
        document = load_state(path) if path == config.bgpls_status_path else json.loads(path.read_text())
        document["metadata"].pop("ospf_multi_area")
        document["metadata"].pop("ospf_multi_area_digest")
        if path == config.bgpls_status_path:
            write_state(path, document)
        else:
            path.write_text(json.dumps(document))
    snapshot = DashboardStore(config).snapshot(now=NOW)
    assert source(snapshot, "bgpls")["state"] == source(snapshot, "topology")["state"] == "invalid"
    assert snapshot["topology"]["nodes"] == []


def test_matching_policy_does_not_hide_changed_raw_topology(tmp_path):
    config = publish(tmp_path)
    value = json.loads(config.bgpls_raw_path.read_text())
    value["rib"].pop(2)  # Remove the independently advertised area-17 ABR identity.
    config.bgpls_raw_path.write_text(json.dumps(value))
    snapshot = DashboardStore(config).snapshot(now=NOW)
    assert source(snapshot, "bgpls")["state"] == source(snapshot, "topology")["state"] == "invalid"
    assert snapshot["topology"]["nodes"] == []


@pytest.mark.parametrize("policy", [{}, policy_document(), "policy.json"])
def test_config_rejects_unvalidated_external_policy(policy):
    with pytest.raises(ValueError, match="validated configuration"):
        DashboardConfig(ospf_multi_area=policy)


def test_config_revalidates_even_dataclass_policy_instances():
    policy = replace(parse_ospf_multi_area_policy(policy_document()), protocol_id=2)
    with pytest.raises(ValueError, match="OSPFv2"):
        DashboardConfig(ospf_multi_area=policy)


def test_cli_loads_policy_before_binding_and_passes_validated_object(tmp_path, monkeypatch):
    observed = {}
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(policy_document()))

    class Server:
        server_address = ("127.0.0.1", 8088)

        def serve_forever(self, **_options):
            raise KeyboardInterrupt

        def server_close(self):
            observed["closed"] = True

    def create(config):
        observed["config"] = config
        return Server()

    monkeypatch.setattr(dashboard, "create_dashboard_server", create)
    assert main(["dashboard", "--ospf-multi-area-policy", str(path)]) == 0
    assert observed["config"].ospf_multi_area.document() == policy_document()
    assert observed["closed"] is True


@pytest.mark.parametrize("value", ["missing", "[]", "{}", "{", '{"protocol_id":NaN}', "duplicate", "yaml"])
def test_cli_rejects_missing_malformed_or_unqualified_policy_before_binding(tmp_path, monkeypatch, value):
    path = tmp_path / ("policy.yml" if value == "yaml" else "policy.json")
    if value == "duplicate":
        valid = json.dumps(policy_document())
        path.write_text('{"schema_version":1,' + valid[1:])
    elif value != "missing":
        path.write_text(json.dumps(policy_document()) if value == "yaml" else value)

    def forbid_binding(_config):
        raise AssertionError("Invalid policy reached HTTP binding")

    monkeypatch.setattr(dashboard, "create_dashboard_server", forbid_binding)
    assert main(["dashboard", "--ospf-multi-area-policy", str(path)]) == 1
