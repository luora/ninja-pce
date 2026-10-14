from __future__ import annotations

import copy
from pathlib import Path

import pytest
import yaml

from ninja_pce.bgpls import parse_ospf_multi_area_policy
from ninja_pce.lab_profile import (
    lab_ospf_multi_area_policy,
    lab_profile_summary,
    load_lab_profile,
    parse_lab_profile,
    render_lab_configs,
)
from ninja_pce.loaders import InputError

ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "gns3/lab-data/iosxe-16.9.7/multi-area-profile.yml"


@pytest.fixture
def manifest():
    return yaml.safe_load(SAMPLE.read_text())


def test_sample_preserves_backbone_and_renders_only_extension():
    profile = load_lab_profile(SAMPLE)
    configs = render_lab_configs(profile)
    summary = lab_profile_summary(profile)
    assert set(configs) == {"P2", "P4", "P5", "PE4"}
    assert summary["abrs"] == ["P2", "P4"]
    assert summary["node_areas"]["PE4"] == ["0.0.0.1"]
    assert summary["authoritative"] is False
    assert summary["observation_status"] == "unobserved"
    assert summary["dry_run"] is True
    assert len(summary["baseline_links"]) == 8
    for abr, remote, address in (("P2", "P5", "10.0.57.5"), ("P4", "PE4", "10.0.48.4")):
        config = configs[abr]
        assert "hostname" not in config
        assert "interface Loopback" not in config
        assert f"description TO_{remote}_GigabitEthernet5" in config
        assert f"ip address {address} 255.255.255.0" in config
        assert "router ospf 100" in config
        assert "no passive-interface GigabitEthernet5" in config
        assert "mpls traffic-eng area 0.0.0.0" in config
        assert "mpls traffic-eng area 0.0.0.1" in config
        assert "interface GigabitEthernet1\n" not in config
        assert "interface GigabitEthernet2\n" not in config
        assert "interface GigabitEthernet4\n" not in config
    for node in ("P5", "PE4"):
        config = configs[node]
        assert f"hostname {node}" in config
        # CSR IOS XE 16.9.7 rejects bare `ip cef` as incomplete. Verify CEF
        # operationally instead of emitting an unsupported bootstrap command.
        assert "\nip cef\n" not in config
        assert "ip rsvp bandwidth 100000 100000" in config
        assert "mpls traffic-eng area 0.0.0.0" not in config
        assert config.index("interface Loopback0") < config.index("mpls ldp router-id Loopback0")
    for config in configs.values():
        for forbidden in ("router bgp", "neighbor ", "username ", "password ", "interface Tunnel", "ip explicit-path"):
            assert forbidden not in config


def test_intent_is_deterministic_across_manifest_order(manifest):
    first = parse_lab_profile(manifest)
    manifest["nodes"].reverse()
    manifest["links"].reverse()
    for link in manifest["links"]:
        link["endpoints"].reverse()
    second = parse_lab_profile(manifest)
    assert render_lab_configs(first) == render_lab_configs(second)
    assert lab_profile_summary(first) == lab_profile_summary(second)


def test_sample_baseline_prefixes_match_observed_underlay_without_changing_extension_prefixes():
    profile = load_lab_profile(SAMPLE)
    baseline = [link for link in profile.links if not link.configure]
    extension = [link for link in profile.links if link.configure]
    assert len(baseline) == 8
    assert {endpoint.address.network.prefixlen for link in baseline for endpoint in link.endpoints} == {29}
    assert len(extension) == 3
    assert {endpoint.address.network.prefixlen for link in extension for endpoint in link.endpoints} == {24}
    crosslink = next(link for link in baseline if link.id == "P2-P4")
    assert {str(endpoint.address) for endpoint in crosslink.endpoints} == {"10.0.54.5/29", "10.0.54.4/29"}


def test_renderer_uses_manifest_identities_process_interfaces_and_directional_capacity(manifest):
    renames = {node["id"]: f"R{index}" for index, node in enumerate(manifest["nodes"], 1)}
    for node in manifest["nodes"]:
        node["id"] = renames[node["id"]]
        node["ospf_process"] = 23
        if node.get("configure_loopback"):
            node["loopback_interface"] = "Lo123"
    for link in manifest["links"]:
        for endpoint in link["endpoints"]:
            endpoint["node"] = renames[endpoint["node"]]
    extension = next(link for link in manifest["links"] if link["id"] == "P2-P5")
    extension["endpoints"][0].update(metric=42, te_metric=55, reservable_kbps=50000, max_reservation_kbps=10000)
    configs = render_lab_configs(parse_lab_profile(manifest))
    abr = configs[renames["P2"]]
    transit = configs[renames["P5"]]
    assert "router ospf 23" in abr and "ip ospf 23 area 0.0.0.1" in abr
    assert "ip ospf cost 42" in abr and "administrative-weight 55" in abr
    assert "ip rsvp bandwidth 50000 10000" in abr
    assert "ip rsvp bandwidth 100000 100000" in transit
    assert "interface Loopback123" in transit
    assert "mpls traffic-eng router-id Loopback123" in transit
    assert "TO_P5" not in abr


def test_ospf_process_defaults_are_explicitly_optional(manifest):
    for node in manifest["nodes"]:
        del node["ospf_process"]
    assert "router ospf 1\n" in render_lab_configs(parse_lab_profile(manifest))["P2"]


def test_baseline_only_profile_generates_no_device_config(manifest):
    for link in manifest["links"]:
        link["configure"] = False
    for node in manifest["nodes"]:
        node["configure_loopback"] = False
    assert render_lab_configs(parse_lab_profile(manifest)) == {}


@pytest.mark.parametrize("field,value", [
    ("schema_version", True), ("schema_version", 2),
    ("platform_profile", "iosxe"), ("platform_profile", "junos"),
    ("name", "../../private"), ("name", "example\nhostname injected"),
])
def test_rejects_ambiguous_or_unsupported_profiles(manifest, field, value):
    manifest[field] = value
    with pytest.raises(InputError):
        parse_lab_profile(manifest)


@pytest.mark.parametrize("field,value", [
    ("hostname", "router\nusername injected"), ("id", "router; injected"),
    ("loopback", "10.255.0.7/24"), ("loopback", "::1"),
    ("loopback", "127.0.0.1"), ("loopback", "224.1.1.1"),
    ("loopback_area", "-1"), ("loopback_area", 2**32), ("loopback_area", True),
    ("ospf_process", 0), ("ospf_process", 65536), ("ospf_process", True),
    ("configure_loopback", "true"), ("loopback_interface", "Lo0\nshutdown"),
    ("reserved_interfaces", ["Gi3", "GigabitEthernet03"]),
])
def test_rejects_invalid_node_intent(manifest, field, value):
    manifest["nodes"][-2][field] = value
    with pytest.raises(InputError):
        parse_lab_profile(manifest)


@pytest.mark.parametrize("field,value", [
    ("metric", 0), ("metric", 65536), ("metric", True),
    ("te_metric", -1), ("te_metric", 2**32),
    ("capacity_kbps", 0), ("capacity_kbps", 1.5),
    ("reservable_kbps", 1000001), ("reservable_kbps", 0),
    ("max_reservation_kbps", 100001), ("max_reservation_kbps", 0),
    ("configure", "false"), ("area", "0.0.0.256"),
])
def test_rejects_invalid_link_capacity_metrics_and_area(manifest, field, value):
    manifest["links"][-1][field] = value
    with pytest.raises(InputError):
        parse_lab_profile(manifest)


@pytest.mark.parametrize("field,value", [
    ("node", "unknown"), ("interface", "Gi5\nshutdown"), ("interface", "Loopback0"),
    ("address", "10.0.48.4"), ("address", "10.0.48.4/32"),
    ("address", "10.0.48.0/24"), ("address", "10.0.48.255/24"),
    ("address", "127.0.0.4/24"), ("address", "224.1.1.4/24"),
    ("address", "2001:db8::4/64"), ("address", "10.0.47.4/24"),
    ("address", "10.0.48.4/25"),
])
def test_rejects_invalid_endpoint_intent(manifest, field, value):
    manifest["links"][-1]["endpoints"][0][field] = value
    with pytest.raises(InputError):
        parse_lab_profile(manifest)


@pytest.mark.parametrize("field", ["id", "hostname", "loopback"])
def test_duplicate_node_identity_is_rejected(manifest, field):
    # hostname defaults to id, so inject it explicitly to check that identity.
    if field == "hostname":
        manifest["nodes"][0][field] = "shared"
        manifest["nodes"][1][field] = "SHARED"
    else:
        manifest["nodes"][1][field] = manifest["nodes"][0][field]
    with pytest.raises(InputError, match=f"Duplicate node {field}"):
        parse_lab_profile(manifest)


def test_existing_interface_collision_is_rejected_even_when_baseline_is_not_configured(manifest):
    manifest["links"][-3]["endpoints"][0]["interface"] = "GigabitEthernet02"
    with pytest.raises(InputError, match="Interface conflict at P2 GigabitEthernet2"):
        parse_lab_profile(manifest)


def test_reserved_non_te_interface_cannot_be_reused(manifest):
    manifest["nodes"][4]["reserved_interfaces"] = ["Gi5"]
    with pytest.raises(InputError, match="Interface conflict at P2 GigabitEthernet5"):
        parse_lab_profile(manifest)


def test_overlap_with_existing_subnet_is_rejected(manifest):
    link = manifest["links"][-1]
    link["endpoints"][0]["address"] = "10.0.54.40/24"
    link["endpoints"][1]["address"] = "10.0.54.80/24"
    with pytest.raises(InputError, match="subnet overlaps another link"):
        parse_lab_profile(manifest)


def test_loopback_subnet_overlap_is_rejected(manifest):
    link = manifest["links"][-1]
    link["endpoints"][0]["address"] = "10.255.0.40/24"
    link["endpoints"][1]["address"] = "10.255.0.80/24"
    with pytest.raises(InputError, match="subnet overlaps a node loopback"):
        parse_lab_profile(manifest)


def test_duplicate_endpoint_address_is_rejected(manifest):
    endpoint = manifest["links"][-1]["endpoints"]
    endpoint[1]["address"] = endpoint[0]["address"]
    with pytest.raises(InputError, match="Duplicate interface address"):
        parse_lab_profile(manifest)


def test_multi_area_router_without_backbone_is_rejected(manifest):
    manifest["nodes"][-2]["loopback_area"] = 2
    with pytest.raises(InputError, match="without a backbone"):
        parse_lab_profile(manifest)


def test_new_isolated_loopback_is_rejected(manifest):
    manifest["links"] = []
    with pytest.raises(InputError, match="needs a configured link"):
        parse_lab_profile(manifest)


def test_existing_node_cannot_be_declared_fresh_to_overwrite_global_identity(manifest):
    manifest["nodes"][4]["configure_loopback"] = True
    with pytest.raises(InputError, match="has baseline links"):
        parse_lab_profile(manifest)


def test_new_node_baseline_links_require_no_global_bootstrap(manifest):
    for link in manifest["links"]:
        link["configure"] = False
    with pytest.raises(InputError, match="has baseline links"):
        parse_lab_profile(manifest)


@pytest.mark.parametrize("scope", ["manifest", "node", "link", "endpoint"])
def test_unknown_fields_fail_closed(manifest, scope):
    item = {"manifest": manifest, "node": manifest["nodes"][0], "link": manifest["links"][0],
            "endpoint": manifest["links"][0]["endpoints"][0]}[scope]
    item["autopush"] = True
    with pytest.raises(InputError, match="Unknown"):
        parse_lab_profile(manifest)


def test_duplicate_manifest_mapping_keys_are_rejected(tmp_path):
    path = tmp_path / "bad.yml"
    path.write_text("schema_version: 1\nschema_version: 2\n")
    with pytest.raises(InputError, match="Duplicate mapping key"):
        load_lab_profile(path)


def test_numbered_slash31_extension_is_supported(manifest):
    link = manifest["links"][-1]
    link["endpoints"][0]["address"] = "192.0.2.0/31"
    link["endpoints"][1]["address"] = "192.0.2.1/31"
    configs = render_lab_configs(parse_lab_profile(manifest))
    assert "ip address 192.0.2.0 255.255.255.254" in configs["P4"]


def test_loading_and_rendering_do_not_mutate_manifest(manifest):
    before = copy.deepcopy(manifest)
    profile = parse_lab_profile(manifest)
    render_lab_configs(profile)
    lab_profile_summary(profile)
    assert manifest == before


def test_manifest_generates_explicit_validated_abr_policy():
    profile = load_lab_profile(SAMPLE)
    document = lab_ospf_multi_area_policy(profile, autonomous_system=65000, instance_id=0, bgpls_identifier=0)
    assert document == {
        "schema_version": 1, "protocol_id": 3, "instance_id": 0,
        "autonomous_system": 65000, "bgpls_identifier": 0,
        "bindings": [
            {"node_id": "P2", "te_router_id": "10.255.0.5", "igp_router_id": "10.255.0.5",
             "areas": ["0.0.0.0", "0.0.0.1"]},
            {"node_id": "P4", "te_router_id": "10.255.0.4", "igp_router_id": "10.255.0.4",
             "areas": ["0.0.0.0", "0.0.0.1"]},
        ],
    }
    assert parse_ospf_multi_area_policy(document).document() == document


def test_igp_router_ids_can_differ_from_te_loopbacks(manifest):
    manifest["nodes"][4]["igp_router_id"] = "192.0.2.5"
    manifest["nodes"][-2]["igp_router_id"] = "192.0.2.7"
    profile = parse_lab_profile(manifest)
    document = lab_ospf_multi_area_policy(profile, autonomous_system=64512, instance_id=47, bgpls_identifier=12)
    assert document["bindings"][0]["te_router_id"] == "10.255.0.5"
    assert document["bindings"][0]["igp_router_id"] == "192.0.2.5"
    assert document["autonomous_system"] == 64512
    assert document["instance_id"] == 47
    assert document["bgpls_identifier"] == 12
    assert " router-id 192.0.2.7\n" in render_lab_configs(profile)["P5"]
    assert lab_profile_summary(profile)["node_router_ids"]["P2"] == {
        "te_router_id": "10.255.0.5", "igp_router_id": "192.0.2.5",
    }


def test_duplicate_igp_router_ids_are_rejected(manifest):
    manifest["nodes"][4]["igp_router_id"] = "10.255.0.4"
    with pytest.raises(InputError, match="Duplicate node igp_router_id"):
        parse_lab_profile(manifest)


@pytest.mark.parametrize("domain", [
    {"autonomous_system": -1, "instance_id": 0, "bgpls_identifier": 0},
    {"autonomous_system": 2**32, "instance_id": 0, "bgpls_identifier": 0},
    {"autonomous_system": True, "instance_id": 0, "bgpls_identifier": 0},
    {"autonomous_system": 65000, "instance_id": -1, "bgpls_identifier": 0},
    {"autonomous_system": 65000, "instance_id": 2**64, "bgpls_identifier": 0},
    {"autonomous_system": 65000, "instance_id": 0, "bgpls_identifier": 2**32},
    {"autonomous_system": 65000, "instance_id": 0, "bgpls_identifier": "0"},
])
def test_policy_generator_requires_valid_explicit_domain(domain):
    with pytest.raises(InputError, match="Cannot generate OSPF multi-area policy"):
        lab_ospf_multi_area_policy(load_lab_profile(SAMPLE), **domain)


def test_single_area_profile_cannot_manufacture_an_abr_policy(manifest):
    for node in manifest["nodes"]:
        node["loopback_area"] = 0
    for link in manifest["links"]:
        link["area"] = 0
    with pytest.raises(InputError, match="explicit ABR bindings"):
        lab_ospf_multi_area_policy(parse_lab_profile(manifest), autonomous_system=65000,
                                   instance_id=0, bgpls_identifier=0)
