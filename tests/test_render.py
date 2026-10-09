from dataclasses import replace
from pathlib import Path

import pytest

from ninja_pce.cspf import compute_cspf
from ninja_pce.loaders import load_demand, load_topology
from ninja_pce.render import render_config

ROOT = Path(__file__).resolve().parents[1]


def inputs():
    topology = load_topology(ROOT / "examples/topologies/simple_multiarea.yml")
    demand = load_demand(ROOT / "examples/demands/pe1_to_pe3_50m.yml")
    return topology, demand


@pytest.mark.parametrize("fmt", ["generic", "iosxr", "iosxe", "junos"])
def test_platform_config_matches_golden_numbered_strict_ero(fmt):
    topology, demand = inputs()
    result = compute_cspf(topology, demand)
    rendered = render_config(result, topology, fmt)
    assert rendered == (ROOT / f"tests/fixtures/render/{fmt}_50m.golden.txt").read_text()
    for hop in result.explicit_hops:
        assert hop.address in rendered
        assert hop.address != hop.loopback


@pytest.mark.parametrize("fmt", ["generic", "iosxr", "iosxe", "junos"])
def test_unnumbered_strict_hop_is_not_invented_or_changed_to_loopback(fmt):
    topology, demand = inputs()
    result = compute_cspf(topology, demand)
    link = replace(result.path_links[0], to_ip=None)
    hop = replace(result.explicit_hops[0], address=None)
    result = replace(
        result, path_links=(link,) + result.path_links[1:], explicit_hops=(hop,) + result.explicit_hops[1:]
    )
    with pytest.raises(ValueError, match="unnumbered"):
        render_config(result, topology, fmt)


@pytest.mark.parametrize("fmt", ["generic", "iosxr", "iosxe", "junos"])
def test_loose_hop_uses_router_loopback_and_explicit_loose_keyword(fmt):
    topology, demand = inputs()
    result = compute_cspf(topology, replace(demand, hop_type="loose"))
    rendered = render_config(result, topology, fmt)
    assert all(hop.address == hop.loopback for hop in result.explicit_hops)
    assert "loose" in rendered


@pytest.mark.parametrize("fmt", ["iosxr", "iosxe"])
def test_cisco_fractional_kbps_rounds_up(fmt):
    topology, demand = inputs()
    result = compute_cspf(topology, replace(demand, reservation_bw_bps=1001))
    rendered = render_config(result, topology, fmt)
    assert ("signalled-bandwidth 2" if fmt == "iosxr" else "tunnel mpls traffic-eng bandwidth 2") in rendered


@pytest.mark.parametrize("fmt", ["iosxr", "iosxe"])
def test_rounded_signaling_reservation_must_still_fit(fmt):
    topology, demand = inputs()
    result = compute_cspf(topology, replace(demand, reservation_bw_bps=1001))
    result = replace(
        result, path_links=tuple(replace(link, available_bw_bps=1001) for link in result.path_links)
    )
    with pytest.raises(ValueError, match="Rounded"):
        render_config(result, topology, fmt)


def test_junos_integer_bps_is_exact():
    topology, demand = inputs()
    result = compute_cspf(topology, replace(demand, reservation_bw_bps=1001))
    assert "bandwidth 1001\n" in render_config(result, topology, "junos")


def test_ero_link_identity_cannot_be_lost():
    topology, demand = inputs()
    result = compute_cspf(topology, demand)
    result = replace(
        result,
        explicit_hops=(replace(result.explicit_hops[0], link_id="wrong-link"),) + result.explicit_hops[1:],
    )
    with pytest.raises(ValueError, match="identity"):
        render_config(result, topology, "iosxr")


def test_parallel_strict_address_must_uniquely_identify_one_adjacency():
    topology, demand = inputs()
    result = compute_cspf(topology, demand)
    edge = next(link for link in topology.links if link.id == "PE1-P3")
    topology = replace(topology, links=topology.links + [replace(edge, id="parallel", metric=26)])
    with pytest.raises(ValueError, match="uniquely"):
        render_config(result, topology, "iosxr")
