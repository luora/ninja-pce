"""Guided setup rejects authority and renders the established observer schema."""
from __future__ import annotations

import copy
import json

import pytest

from ninja_pce.admin_model import default_setup, render_setup, validate_setup
from ninja_pce.bgpls import parse_ospf_multi_area_policy
from ninja_pce.controller import component_commands, load_controller_config


def multi_area_setup():
    setup = default_setup()
    setup["routing_domain"] = {
        "protocol_id": 3, "instance_id": 47, "autonomous_system": 64512, "bgpls_identifier": 12,
    }
    setup["nodes"] = [
        {"router_id": "192.0.2.11", "name": "border"},
        {"router_id": "192.0.2.12", "name": "east"},
    ]
    setup["abr_bindings"] = [{
        "node_id": "border", "te_router_id": "192.0.2.11", "igp_router_id": "198.51.100.11",
        "areas": ["0.0.0.17", "0.0.0.0"],
    }]
    return setup


def test_default_is_fresh_observation_example_without_inbound_bgp():
    setup = default_setup()
    assert validate_setup(setup)["operating_mode"] == "observe"
    assert setup["controller"]["bgp_port"] == -1
    assert setup["routing_domain"]["protocol_id"] == 3
    assert setup["abr_bindings"] == []
    setup["bgpls"]["peers"][0]["address"] = "198.51.100.20"
    assert default_setup()["bgpls"]["peers"][0]["address"] == "192.0.2.20"


def test_validation_produces_fresh_canonical_idempotent_document():
    setup = multi_area_setup()
    setup["bgpls"]["peers"] = [
        {"address": "192.0.2.200", "asn": 65000}, {"address": "192.0.2.20", "asn": 64512},
    ]
    setup["nodes"].reverse()
    original = copy.deepcopy(setup)
    canonical = validate_setup(setup)
    assert canonical == validate_setup(canonical)
    assert canonical["bgpls"]["peers"][0]["address"] == "192.0.2.20"
    assert canonical["nodes"][0]["name"] == "border"
    assert canonical["abr_bindings"][0]["areas"] == ["0.0.0.0", "0.0.0.17"]
    canonical["abr_bindings"][0]["areas"].append("0.0.0.18")
    assert setup == original
    empty = default_setup()
    validate_setup(empty)["abr_bindings"].append({})
    assert empty["abr_bindings"] == []


@pytest.mark.parametrize("section", [None, "controller", "bgpls", "routing_domain"])
def test_unknown_and_missing_fields_are_rejected(section):
    setup = default_setup()
    target = setup if section is None else setup[section]
    target["command"] = "/bin/sh"
    with pytest.raises(ValueError, match="unsupported fields"):
        validate_setup(setup)
    del target["command"]
    del target[next(iter(target))]
    with pytest.raises(ValueError, match="unsupported fields"):
        validate_setup(setup)


@pytest.mark.parametrize("field,value", [
    ("version", True), ("version", 2), ("operating_mode", "pcep-control"),
    ("operating_mode", "provision"), ("controller", []), ("bgpls", None),
    ("routing_domain", "3"), ("nodes", {}), ("abr_bindings", {}),
])
def test_top_level_invalid_values(field, value):
    setup = default_setup()
    setup[field] = value
    with pytest.raises(ValueError):
        validate_setup(setup)


@pytest.mark.parametrize("field,value", [
    ("name", "unsafe name"), ("name", "../../etc"), ("name", "$(id)"), ("name", "a" * 129),
    ("router_id", "example.com"), ("router_id", "192.0.2.10/32"), ("router_id", 3221225994),
    ("router_id", "192.000.2.10"), ("router_id", "224.0.0.1"), ("router_id", "0.0.0.0"),
    ("source_address", "::1"), ("source_address", "255.255.255.255"),
    ("local_as", True), ("local_as", 0), ("local_as", 2**32), ("local_as", "64512"),
    ("bgp_port", True), ("bgp_port", 0), ("bgp_port", -2), ("bgp_port", 65536),
])
def test_controller_identity_bounds_and_injection(field, value):
    setup = default_setup()
    setup["controller"][field] = value
    with pytest.raises(ValueError):
        validate_setup(setup)


@pytest.mark.parametrize("field,value", [
    ("interval_seconds", 0), ("interval_seconds", 3601), ("interval_seconds", True),
    ("interval_seconds", 10**1000), ("timeout_seconds", float("nan")), ("timeout_seconds", 31),
    ("timeout_seconds", "10"), ("max_age_seconds", float("inf")), ("max_age_seconds", -1),
    ("max_age_seconds", 31),
])
def test_freshness_bounds_are_preserved(field, value):
    setup = default_setup()
    setup["bgpls"][field] = value
    with pytest.raises(ValueError):
        validate_setup(setup)


@pytest.mark.parametrize("peers", [
    [], [{"address": "192.0.2.20", "asn": 64512}] * 257,
    [{"address": "192.0.2.20", "asn": 64512}] * 2,
    [{"address": "192.0.2.10", "asn": 64512}],
    [{"address": "192.0.2.20", "asn": False}],
    [{"address": "192.0.2.20", "asn": 0}],
    [{"address": "192.0.2.20", "asn": 2**32}],
    [{"address": "192.0.2.20", "asn": 64512, "password": "secret"}],
    [{"address": "peer.example", "asn": 64512}],
])
def test_peers_are_explicit_unique_bounded_identities(peers):
    setup = default_setup()
    setup["bgpls"]["peers"] = peers
    with pytest.raises(ValueError):
        validate_setup(setup)


@pytest.mark.parametrize("field,value", [
    ("protocol_id", 2), ("protocol_id", True), ("instance_id", -1), ("instance_id", 2**64),
    ("instance_id", "47"), ("autonomous_system", 0), ("autonomous_system", 2**32),
    ("bgpls_identifier", -1), ("bgpls_identifier", 2**32),
])
def test_trusted_domain_tuple_has_typed_bounds(field, value):
    setup = default_setup()
    setup["routing_domain"][field] = value
    with pytest.raises(ValueError):
        validate_setup(setup)


@pytest.mark.parametrize("nodes", [
    [{"router_id": "192.0.2.11", "name": "border"}] * 2,
    [{"router_id": "192.0.2.11", "name": "border"}, {"router_id": "192.0.2.12", "name": "border"}],
    [{"router_id": "192.0.2.11", "name": "border", "file": "/etc/passwd"}],
    [{"router_id": "192.0.2.11", "name": "bad\nname"}],
    [{"router_id": "224.0.0.11", "name": "border"}],
])
def test_node_labels_are_explicit_and_unique(nodes):
    setup = default_setup()
    setup["nodes"] = nodes
    with pytest.raises(ValueError):
        validate_setup(setup)


@pytest.mark.parametrize("field,value", [
    ("node_id", "other"), ("te_router_id", "192.0.2.12"), ("igp_router_id", "0.0.0.0"),
    ("areas", ["0.0.0.0"]), ("areas", ["0.0.0.0", "0.0.0.0"]),
    ("areas", [0, 17]), ("areas", ["0.0.0.0", "0.0.0.17/32"]),
])
def test_abr_projection_requires_explicit_matching_typed_identity(field, value):
    setup = multi_area_setup()
    setup["abr_bindings"][0][field] = value
    with pytest.raises(ValueError):
        validate_setup(setup)


def test_abr_policy_cannot_override_domain_or_claim_delivery_authority():
    setup = multi_area_setup()
    setup["abr_bindings"][0]["autonomous_system"] = 65000
    with pytest.raises(ValueError, match="unsupported fields"):
        validate_setup(setup)
    setup = multi_area_setup()
    setup["pcep"] = {"domain_policy": "/etc/ninja-pce/domain-policy.json"}
    with pytest.raises(ValueError, match="unsupported fields"):
        validate_setup(setup)


@pytest.mark.parametrize("multi_area", [False, True])
def test_rendered_files_validate_with_established_controller_and_area_policy(tmp_path, multi_area):
    workspace = tmp_path / "private workspace"
    revision = workspace / "config/revisions/example"
    setup = multi_area_setup() if multi_area else default_setup()
    generated = render_setup(setup, workspace, revision)
    assert not workspace.exists()  # Rendering cannot create even staging paths.
    revision.mkdir(parents=True)
    for name, value in generated.items():
        assert isinstance(value, bytes)
        (revision / name).write_bytes(value)
    config = load_controller_config(revision / "controller.json")
    assert set(component_commands(config)) == {"bgpls"}
    assert config.document["dashboard"]["enabled"] is False
    assert config.document["state_dir"] == str(workspace / "data/state")
    assert config.document["runtime_dir"] == str(workspace / "data/run")
    assert config.document["bgpls"]["endpoint"] == "127.0.0.1:50051"
    document = json.loads(generated["controller.json"])
    assert "pcep" not in document
    assert not (workspace / "data").exists()
    if multi_area:
        policy = parse_ospf_multi_area_policy(json.loads(generated["ospf-multi-area-policy.json"]))
        assert policy.instance_id == setup["routing_domain"]["instance_id"]
        assert policy.autonomous_system == setup["routing_domain"]["autonomous_system"]
        assert policy.bgpls_identifier == setup["routing_domain"]["bgpls_identifier"]
        assert policy.bindings[0].node_id == "border"
    else:
        assert "ospf-multi-area-policy.json" not in generated
        assert "ospf_multi_area_policy" not in document["bgpls"]
        assert json.loads(generated["setup.json"])["routing_domain"] == setup["routing_domain"]


def test_rendered_gobgp_limits_families_and_exports_for_every_peer(tmp_path):
    tomllib = pytest.importorskip("tomllib")
    setup = default_setup()
    setup["controller"]["bgp_port"] = 179
    setup["bgpls"]["peers"].append({"address": "198.51.100.20", "asn": 65000})
    generated = render_setup(setup, tmp_path, tmp_path / "revision")
    config = tomllib.loads(generated["gobgpd.toml"].decode())
    assert config["global"]["config"]["local-address-list"] == ["192.0.2.10"]
    assert config["global"]["config"]["port"] == 179
    assert config["global"]["apply-policy"]["config"]["default-export-policy"] == "reject-route"
    assert len(config["neighbors"]) == 2
    for peer in config["neighbors"]:
        assert peer["transport"]["config"]["local-address"] == "192.0.2.10"
        assert peer["apply-policy"]["config"]["default-export-policy"] == "reject-route"
        assert [item["config"]["afi-safi-name"] for item in peer["afi-safis"]] == ["ls"]


def test_rendering_is_deterministic_and_does_not_mutate_inputs(tmp_path):
    setup = multi_area_setup()
    original = copy.deepcopy(setup)
    first = render_setup(setup, tmp_path, tmp_path / "revision", grpcurl="trusted-grpcurl")
    assert setup == original
    setup["nodes"].reverse()
    setup["abr_bindings"][0]["areas"].reverse()
    assert first == render_setup(setup, tmp_path, tmp_path / "revision", grpcurl="trusted-grpcurl")
    assert original == multi_area_setup()
    controller = json.loads(first["controller.json"])
    assert controller["bgpls"]["grpcurl"] == "trusted-grpcurl"


@pytest.mark.parametrize("grpcurl", ["", "--help", "grpcurl\n/bin/sh", None, "a" * 4097])
def test_render_rejects_malformed_trusted_executable(tmp_path, grpcurl):
    with pytest.raises(ValueError):
        render_setup(default_setup(), tmp_path, tmp_path / "revision", grpcurl=grpcurl)
