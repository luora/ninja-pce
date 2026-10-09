from copy import deepcopy
from dataclasses import replace
from decimal import Decimal, localcontext

import pytest
import yaml
from hypothesis import given, settings
from hypothesis import strategies as st

from ninja_pce.cspf import compute_cspf
from ninja_pce.loaders import InputError, load_demand, load_inventory, load_topology
from ninja_pce.models import DemandOutput, Link, PathConstraints, ServiceDemand, mbps_to_bps


def topo_data():
    return {
        "metadata": {"units": "Mbps"},
        "nodes": [{"id": "A", "loopback": "192.0.2.1"}, {"id": "B", "loopback": "192.0.2.2"}],
        "links": [{"id": "AB", "from": "A", "to": "B", "from_ip": "10.0.0.1", "to_ip": "10.0.0.2",
                   "reservable_bw_mbps": 100, "reserved_bw_mbps": 20}],
    }


def demand_data():
    return {"service": {"name": "test", "source": "A", "destination": "B", "bandwidth_mbps": 50}}


def write(tmp_path, data):
    path = tmp_path / "input.yml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


@pytest.mark.parametrize(("field", "value"), [
    ("reservable_bw_mbps", -1), ("reservable_bw_mbps", 0), ("reservable_bw_mbps", float("nan")),
    ("reservable_bw_mbps", float("inf")), ("reservable_bw_mbps", "100"), ("reservable_bw_mbps", True),
    ("reserved_bw_mbps", -1), ("reserved_bw_mbps", 101), ("reserved_bw_mbps", float("nan")),
    ("metric", -1), ("metric", 1.5), ("metric", "5"), ("metric", True),
    ("bidirectional", "false"), ("bidirectional", 1), ("admin_up", "true"), ("oper_up", 0),
    ("from_ip", "10.0.0.999"), ("to_ip", "bad"), ("id", "AB\nshutdown"),
    ("local_interface", "Gig0;shutdown"), ("srlg", [False]), ("from", "UNKNOWN"),
    ("unreserved_bw_mbps", [10] * 7), ("unreserved_bw_mbps", [10] * 9),
    ("unreserved_bw_mbps", [10] * 7 + [11]), ("unreserved_bw_mbps", [101] * 8),
    ("unreserved_bw_mbps", [float("nan")] * 8), ("unreserved_bw_bps", [1.5] * 8),
    ("unknown", 1), ("max_bw_bps", 0), ("max_bw_mbps", 0), ("metric", 4_294_967_296),
    ("to_ip", "fe80::1%scope;commit"),
])
def test_reject_invalid_link_fields(tmp_path, field, value):
    data = topo_data()
    data["links"][0][field] = value
    with pytest.raises(InputError):
        load_topology(write(tmp_path, data))


@pytest.mark.parametrize(("field", "value"), [
    ("bandwidth_mbps", -1), ("bandwidth_mbps", 0), ("bandwidth_mbps", float("nan")),
    ("bandwidth_mbps", float("inf")), ("bandwidth_mbps", "50"), ("bandwidth_mbps", True),
    ("setup_priority", -1), ("setup_priority", 8), ("setup_priority", "7"), ("setup_priority", True),
    ("hold_priority", -1), ("hold_priority", 8), ("name", "bad;shutdown"),
    ("source", "A\nshutdown"), ("name", "x/../../escape"), ("description", "hello\nshutdown"), ("description", 'bad";shutdown'),
    ("path", []), ("constraints", None), ("affinity", "green"), ("output", False), ("unknown", 1),
])
def test_reject_invalid_demand_fields(tmp_path, field, value):
    data = demand_data()
    data["service"][field] = value
    with pytest.raises(InputError):
        load_demand(write(tmp_path, data))


@pytest.mark.parametrize(("field", "value"), [
    ("prefer_lower_metric", "false"), ("prefer_higher_bottleneck", 0), ("max_hops", -1),
    ("max_hops", 0), ("max_metric", -1), ("max_candidates", 0), ("max_candidates", True),
    ("search_budget", 0), ("search_budget", "100"), ("include_nodes", {"B": True}),
    ("ordered_waypoints", ["A", "A"]), ("unknown", "x"),
])
def test_reject_invalid_constraints(tmp_path, field, value):
    data = demand_data()
    data["service"]["constraints"] = {field: value}
    with pytest.raises(InputError):
        load_demand(write(tmp_path, data))


@pytest.mark.parametrize(("field", "value"), [
    ("dry_run", "false"), ("write_plan", 1), ("write_verification", "true"), ("tunnel_id", 0),
    ("tunnel_id", 65536), ("tunnel_id", True), ("tunnel_name", "bad\nshutdown"),
    ("explicit_path_name", "bad;commit"), ("iosxr_interface", "GigabitEthernet0/0"),
    ("junos_lsp_name", 'name"'), ("formats", ["unsupported"]), ("formats", []), ("unknown", 1),
])
def test_reject_invalid_output(tmp_path, field, value):
    data = demand_data()
    data["service"]["output"] = {field: value}
    with pytest.raises(InputError):
        load_demand(write(tmp_path, data))


@pytest.mark.parametrize("data", [None, [], "text", {"nodes": [], "links": []},
    {"nodes": ["bad"], "links": [{}]}, {"nodes": {}, "links": []}, {"nodes": [{}], "links": [{}]},
    {"nodes": topo_data()["nodes"], "links": [None]}, {**topo_data(), "metadata": []},
    {**topo_data(), "typo": True},
])
def test_malformed_topology_fails_actionably(tmp_path, data):
    with pytest.raises(InputError):
        load_topology(write(tmp_path, data))


@pytest.mark.parametrize("text", [
    "nodes: []\nnodes: []\nlinks: []\n", "nodes: [", "{\"nodes\": [], \"nodes\": [], \"links\": []}",
    "nodes:\n - id: A\n   id: B\n   loopback: 192.0.2.1\nlinks: []", "1: a",
])
def test_duplicate_mapping_keys_and_parse_errors(tmp_path, text):
    path = tmp_path / ("input.json" if text.startswith("{") else "input.yml")
    path.write_text(text, encoding="utf-8")
    with pytest.raises(InputError):
        load_topology(path)


def test_duplicate_topology_ids_and_reverse_collision(tmp_path):
    data = topo_data()
    data["nodes"].append(deepcopy(data["nodes"][0]))
    with pytest.raises(InputError, match="Duplicate node"):
        load_topology(write(tmp_path, data))
    data = topo_data()
    data["links"].append(deepcopy(data["links"][0]))
    with pytest.raises(InputError, match="Duplicate link"):
        load_topology(write(tmp_path, data))
    data = topo_data()
    data["links"].append({**data["links"][0], "id": "AB:reverse", "bidirectional": False})
    with pytest.raises(InputError, match="contradictory"):
        load_topology(write(tmp_path, data))


def test_explicit_reverse_attributes_and_ipv6_are_preserved(tmp_path):
    data = topo_data()
    data["nodes"][0]["loopback"] = "2001:db8::1"
    data["links"][0]["reverse"] = {"metric": 55, "reservable_bw_mbps": 30}
    result = load_topology(write(tmp_path, data))
    assert result.nodes["A"].loopback == "2001:db8::1"
    forward, reverse = result.links
    assert forward.id == "AB" and reverse.id == "AB:reverse"
    assert forward.metric == 1 and reverse.metric == 55
    assert reverse.reservable_bw_bps == 30_000_000
    assert reverse.from_ip == "10.0.0.2" and reverse.to_ip == "10.0.0.1"
    assert not forward.bidirectional and not reverse.bidirectional


def test_units_and_double_declared_bandwidth_fail(tmp_path):
    data = topo_data()
    data["metadata"]["units"] = "kbps"
    with pytest.raises(InputError, match="units"):
        load_topology(write(tmp_path, data))
    data = topo_data()
    data["links"][0]["reservable_bw_bps"] = 100_000_000
    with pytest.raises(InputError, match="only one"):
        load_topology(write(tmp_path, data))
    data = demand_data()
    data["service"]["bandwidth_bps"] = 50_000_000
    with pytest.raises(InputError, match="only one"):
        load_demand(write(tmp_path, data))


def test_canonical_bps_legacy_conversion_and_ceil_reservations(tmp_path):
    data = topo_data()
    del data["links"][0]["reservable_bw_mbps"]
    del data["links"][0]["reserved_bw_mbps"]
    data["links"][0].update(reservable_bw_bps=100_000_001, reserved_bw_bps=1,
                            unreserved_bw_bps=[100_000_000] * 8)
    graph = load_topology(write(tmp_path, data))
    wire = graph.links[0]
    assert wire.available_bps(7) == 100_000_000
    data = demand_data()
    data["service"]["bandwidth_mbps"] = 0.0000011
    request = load_demand(write(tmp_path, data))
    assert request.bandwidth_bps == 2
    assert compute_cspf(graph, request).path_links[0].available_bw_bps == 100_000_000
    data = topo_data()
    legacy = load_topology(write(tmp_path, data)).links[0]
    assert legacy.unreserved_bw_bps == (80_000_000,) * 8


@pytest.mark.parametrize("service_update", [
    {"source": "B"}, {"path": {"hop_type": "invalid"}},
    {"setup_priority": 0, "hold_priority": 7},
    {"constraints": {"avoid_nodes": ["A"]}},
    {"constraints": {"include_nodes": ["B"], "avoid_nodes": ["B"]}},
    {"path": {"max_hops": 2}, "constraints": {"max_hops": 3}},
    {"affinity": {"include_all_admin_groups": ["green"], "exclude_admin_groups": ["green"]}},
    {"affinity": {"include_any_admin_groups": ["green"], "exclude_admin_groups": ["green"]}},
])
def test_contradictory_demand_rejected(tmp_path, service_update):
    data = demand_data()
    data["service"].update(service_update)
    with pytest.raises(InputError):
        load_demand(write(tmp_path, data))


def test_metadata_timestamp_is_string_and_boolean_is_strict(tmp_path):
    data = topo_data()
    text = yaml.safe_dump(data) + ""
    path = tmp_path / "input.yml"
    path.write_text(text.replace("units: Mbps", "units: Mbps\n  timestamp: 2026-10-09T00:00:00Z"), encoding="utf-8")
    assert load_topology(path).raw_metadata["timestamp"] == "2026-10-09T00:00:00Z"
    data["metadata"]["authoritative"] = "false"
    with pytest.raises(InputError):
        load_topology(write(tmp_path, data))


def test_inventory_rejects_unsafe_types_and_mappings(tmp_path):
    base = {"name": "router", "host": "192.0.2.1", "device_type": "iosxr", "username": "admin"}
    for field, value in [("port", True), ("host", "a\nshutdown"), ("host", "a;shutdown"),
                         ("username", "user;shutdown"), ("password_env", "PASS;SECRET"), ("name", "../x\n")]:
        with pytest.raises(InputError):
            load_inventory(write(tmp_path, {"devices": [{**base, field: value}]}))
    with pytest.raises(InputError, match="Contradictory"):
        load_inventory(write(tmp_path, {"devices": {"router": {**base, "name": "other"}}}))


def test_domain_constructors_do_not_allow_unsafe_bypass():
    for create in (lambda: DemandOutput(tunnel_name="a\nshutdown"), lambda: DemandOutput(dry_run="false"),
                   lambda: PathConstraints(prefer_lower_metric="false"),
                   lambda: Link("x", "A", "B", 1, float("inf")),
                   lambda: ServiceDemand("x", "A", "B", True)):
        with pytest.raises(ValueError):
            create()
    original = ServiceDemand("x", "A", "B", 50)
    assert replace(original, reservation_bw_bps=50_000_001).bandwidth_bps == 50_000_001


@given(st.decimals(min_value="0", max_value="1000000", places=9, allow_nan=False, allow_infinity=False))
@settings(max_examples=100, derandomize=True)
def test_decimal_unit_conversion_never_under_reserves_or_over_advertises(mbps):
    actual_bps = mbps * 1_000_000
    assert mbps_to_bps(mbps) >= actual_bps
    assert mbps_to_bps(mbps, round_up=False) <= actual_bps
    assert mbps_to_bps(mbps) - mbps_to_bps(mbps, round_up=False) <= 1


def test_decimal_conversion_preserves_sub_bit_ceiling_beyond_ambient_precision():
    amount = Decimal("12345678901234567890123.0000001")
    reserved = mbps_to_bps(amount)
    capacity = mbps_to_bps(amount, round_up=False)
    with localcontext() as context:
        context.prec = 70
        exact = amount * 1_000_000
        assert reserved >= exact
        assert capacity <= exact
    assert reserved - capacity == 1


def test_unrepresentable_integer_bandwidth_fails_actionably(tmp_path):
    data = demand_data()
    del data["service"]["bandwidth_mbps"]
    data["service"]["bandwidth_bps"] = 10 ** 400
    with pytest.raises(InputError, match="numeric range"):
        load_demand(write(tmp_path, data))
