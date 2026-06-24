from pathlib import Path

import pytest

from ninja_pce.cspf import CspfNoPath, compute_cspf
from ninja_pce.loaders import load_demand, load_topology

ROOT = Path(__file__).resolve().parents[1]
TOPOLOGY = ROOT / "examples" / "topologies" / "simple_multiarea.yml"


def test_cspf_selects_bandwidth_feasible_green_path():
    topology = load_topology(TOPOLOGY)
    demand = load_demand(ROOT / "examples" / "demands" / "pe1_to_pe3_50m.yml")

    result = compute_cspf(topology, demand)

    assert result.path_nodes == ("PE1", "P3", "P4", "PE3")
    assert result.total_metric == 50
    assert result.bottleneck_available_bw_mbps == 170
    assert result.residual_after_reservation_mbps == 120
    assert len(result.candidate_paths) >= 2
    assert any(item["id"] == "P1-P2" for item in result.pruned_links)


def test_avoid_link_forces_alternate_path():
    topology = load_topology(TOPOLOGY)
    demand = load_demand(ROOT / "examples" / "demands" / "pe1_to_pe3_50m_avoid_pe1_p3.yml")

    result = compute_cspf(topology, demand)

    assert result.path_nodes == ("PE1", "P1", "P3", "P4", "PE3")
    assert all(link.id != "PE1-P3" for link in result.path_links)
    assert result.bottleneck_available_bw_mbps >= demand.bandwidth_mbps


def test_include_green_large_demand_uses_high_capacity_path():
    topology = load_topology(TOPOLOGY)
    demand = load_demand(ROOT / "examples" / "demands" / "pe1_to_pe3_120m.yml")

    result = compute_cspf(topology, demand)

    assert result.path_nodes == ("PE1", "P3", "P4", "PE3")
    assert all("GREEN" in link.admin_groups for link in result.path_links)
    assert result.residual_after_reservation_mbps == 50


def test_no_path_when_demand_is_too_large(tmp_path):
    topology = load_topology(TOPOLOGY)
    demand_file = tmp_path / "too_large.yml"
    demand_file.write_text(
        """
service:
  name: TOO_LARGE
  source: PE1
  destination: PE3
  bandwidth_mbps: 1000
""".strip(),
        encoding="utf-8",
    )
    demand = load_demand(demand_file)

    with pytest.raises(CspfNoPath):
        compute_cspf(topology, demand)
