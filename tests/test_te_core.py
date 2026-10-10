from dataclasses import replace
from itertools import permutations, product

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from ninja_pce.cspf import CspfNoPath, CspfSearchLimit, compute_cspf
from ninja_pce.loaders import InputError
from ninja_pce.models import (
    Affinity,
    Link,
    Node,
    PathConstraints,
    ServiceDemand,
    Topology,
    normalize_links,
)


def topology(edges, node_ids=("A", "B", "C", "D")):
    nodes = {name: Node(name, f"192.0.2.{index}") for index, name in enumerate(node_ids, 1)}
    return Topology("test", nodes, list(edges))


def edge(link_id, source="A", target="D", metric=10, capacity=100, **kwargs):
    return Link(link_id, source, target, metric, capacity, bidirectional=False, **kwargs)


def demand(**kwargs):
    return ServiceDemand("test", "A", "D", 50, **kwargs)


def ids(result):
    return tuple(link.id for link in result.path_links)


def test_parallel_links_keep_exact_identity_and_order_independence():
    links = [edge("high-metric", metric=20), edge("low-metric", metric=5)]
    for ordering in (links, list(reversed(links))):
        result = compute_cspf(topology(ordering), demand())
        assert ids(result) == ("low-metric",)
        assert [c.path_links[0].id for c in result.candidate_paths] == ["low-metric", "high-metric"]
        assert result.to_summary_dict()["links"][0]["id"] == "low-metric"


def test_parallel_bandwidth_state_and_direction_are_independent():
    links = [edge("narrow", capacity=20, metric=1), edge("wide", metric=5),
             edge("failed", metric=0, capacity=1000, oper_up=False),
             edge("opposite", "D", "A", metric=2, capacity=1000)]
    result = compute_cspf(topology(links), demand())
    assert ids(result) == ("wide",)
    assert {item["id"] for item in result.pruned_links} == {"failed", "narrow"}
    reverse = replace(demand(), source="D", destination="A")
    assert ids(compute_cspf(topology(links), reverse)) == ("opposite",)


def test_one_way_and_failed_member():
    links = [edge("a", oper_up=False), edge("b")]
    assert ids(compute_cspf(topology(links), demand())) == ("b",)
    with pytest.raises(CspfNoPath):
        compute_cspf(topology(links), replace(demand(), source="D", destination="A"))


def test_normalization_respects_explicit_asymmetric_reverse():
    forward = replace(edge("wire", from_ip="10.0.0.1", to_ip="10.0.0.2"), bidirectional=True)
    reverse = edge("wire:reverse", "D", "A", metric=40, capacity=20)
    links = normalize_links([forward, reverse])
    assert len(links) == 2
    assert links[1].metric == 40 and links[1].available_bps(7) == 20_000_000
    assert normalize_links(links) == links
    with pytest.raises(CspfNoPath):
        compute_cspf(topology(links), replace(demand(), source="D", destination="A"))


def test_metric_is_lexicographically_prior_to_100g_capacity():
    result = compute_cspf(topology([edge("100G", metric=11, capacity=100000), edge("100M", metric=10)]), demand())
    assert ids(result) == ("100M",)


@pytest.mark.parametrize(("prefer_metric", "prefer_bandwidth", "expected"), [
    (True, True, "metric"), (True, False, "metric"),
    (False, True, "capacity"), (False, False, "capacity"),
])
def test_disabled_preferences_and_stable_link_id_tie_break(prefer_metric, prefer_bandwidth, expected):
    constraints = PathConstraints(prefer_lower_metric=prefer_metric, prefer_higher_bottleneck=prefer_bandwidth)
    links = [edge("metric", metric=1, capacity=100), edge("capacity", metric=9, capacity=200)]
    assert ids(compute_cspf(topology(links), demand(constraints=constraints))) == (expected,)


def test_equal_metric_prefers_capacity_then_hops_then_identity():
    links = [edge("direct-z", capacity=200), edge("direct-a", capacity=200),
             edge("via-1", "A", "B", metric=5, capacity=200),
             edge("via-2", "B", "D", metric=5, capacity=200), edge("narrow", capacity=100)]
    result = compute_cspf(topology(links), demand())
    assert [tuple(link.id for link in candidate.path_links) for candidate in result.candidate_paths] == [
        ("direct-a",), ("direct-z",), ("via-1", "via-2")]


def test_required_path_after_more_than_30_cheaper_invalid_alternatives():
    node_ids = ("A", "D", "W", *(f"X{i}" for i in range(40)))
    links = [edge("way-in", "A", "W", metric=100), edge("way-out", "W", "D", metric=100)]
    for i in range(40):
        links.extend([edge(f"in-{i}", "A", f"X{i}", metric=1), edge(f"out-{i}", f"X{i}", "D", metric=1)])
    result = compute_cspf(topology(links, node_ids), demand(constraints=PathConstraints(include_nodes=("W",))))
    assert result.path_nodes == ("A", "W", "D")


def test_unordered_includes_distinct_from_ordered_waypoints():
    links = [edge("ab", "A", "B", metric=1), edge("bc", "B", "C", metric=1),
             edge("cd", "C", "D", metric=1), edge("ac", "A", "C", metric=2),
             edge("cb", "C", "B", metric=2), edge("bd", "B", "D", metric=2)]
    graph = topology(links)
    unordered = demand(constraints=PathConstraints(include_nodes=("C", "B")))
    ordered = demand(constraints=PathConstraints(ordered_waypoints=("C", "B")))
    assert compute_cspf(graph, unordered).path_nodes == ("A", "B", "C", "D")
    assert compute_cspf(graph, ordered).path_nodes == ("A", "C", "B", "D")


def test_hop_metric_affinity_and_node_link_constraints():
    links = [edge("ab", "A", "B", metric=1, admin_groups=("green",)),
             edge("bd", "B", "D", metric=1, admin_groups=("green",)),
             edge("ac", "A", "C", metric=3, admin_groups=("blue",)),
             edge("cd", "C", "D", metric=3, admin_groups=("blue",))]
    graph = topology(links)
    constraints = PathConstraints(avoid_nodes=("B",), max_hops=2, max_metric=6)
    assert ids(compute_cspf(graph, demand(constraints=constraints))) == ("ac", "cd")
    assert ids(compute_cspf(graph, demand(affinity=Affinity(include_all_admin_groups=("green",))))) == ("ab", "bd")
    for constraints in (PathConstraints(max_hops=1), PathConstraints(max_metric=1),
                        PathConstraints(avoid_links=("ab", "ac"))):
        with pytest.raises(CspfNoPath):
            compute_cspf(graph, demand(constraints=constraints))


def test_search_limit_distinct_from_no_path_even_after_feasible_path_found():
    graph = topology([edge("a-feasible"), edge("z-other", "A", "B")])
    with pytest.raises(CspfSearchLimit) as exc:
        compute_cspf(graph, demand(constraints=PathConstraints(search_budget=1)))
    assert exc.value.feasible_found == 1 and exc.value.status == "SEARCH_LIMIT"
    assert not isinstance(exc.value, CspfNoPath)
    assert ids(compute_cspf(topology([edge("only")]), demand(constraints=PathConstraints(search_budget=1)))) == ("only",)


@pytest.mark.parametrize("priority", range(8))
def test_all_setup_priorities_apply_nonpreemptive_admission(priority):
    vector = tuple((100 - 10 * i) * 1_000_000 for i in range(8))
    wire = edge("wire", unreserved_bw_bps=vector)
    assert wire.observed_unreserved_bps(priority) == vector[priority]
    assert wire.available_bps(priority) == 30_000_000
    feasible = ServiceDemand("test", "A", "D", 30, setup_priority=priority, hold_priority=priority)
    assert ids(compute_cspf(topology([wire]), feasible)) == ("wire",)
    with pytest.raises(CspfNoPath):
        compute_cspf(topology([wire]), replace(feasible, reservation_bw_bps=30_000_001))


def test_pending_reservations_and_preemption_deficit_are_separate():
    wire = edge("wire", unreserved_bw_bps=(100_000_000,) * 7 + (20_000_000,), pending_bw_bps=5_000_000)
    assert wire.available_bps(0) == 15_000_000
    assert wire.preemption_required_bps(0, 50_000_000) == 35_000_000
    with pytest.raises(CspfNoPath):
        compute_cspf(topology([wire]), demand(setup_priority=0, hold_priority=0))


def test_strict_remote_interface_and_loose_loopback_addresses():
    graph = topology([edge("wire", from_ip="10.0.0.1", to_ip="10.0.0.2")])
    strict = compute_cspf(graph, demand())
    assert strict.explicit_hops[0].address == "10.0.0.2"
    assert strict.explicit_hops[0].link_id == "wire"
    assert strict.explicit_hops[0].local_address == "10.0.0.1"
    loose = compute_cspf(graph, demand(hop_type="loose"))
    assert loose.explicit_hops[0].address == graph.nodes["D"].loopback
    assert "does not enforce" in loose.warnings[-1]
    unnumbered = compute_cspf(topology([edge("unnumbered")]), demand())
    assert unnumbered.explicit_hops[0].address is None


def test_unknown_nodes_links_and_required_capabilities_fail_validation():
    graph = topology([edge("wire")])
    for constraints in (PathConstraints(avoid_nodes=("UNKNOWN",)), PathConstraints(avoid_links=("UNKNOWN",)),
                        PathConstraints(include_nodes=("UNKNOWN",))):
        with pytest.raises(InputError):
            compute_cspf(graph, demand(constraints=constraints))
    with pytest.raises(InputError, match="capabilities"):
        compute_cspf(graph, demand(required_capabilities=("rsvp-te",)))


def exhaustive_oracle(graph, request):
    """Independent node permutation + parallel-edge Cartesian product oracle."""
    feasible = []
    c = request.constraints
    middle_nodes = sorted(set(graph.nodes) - {request.source, request.destination})
    for length in range(len(middle_nodes) + 1):
        for middle in permutations(middle_nodes, length):
            nodes = (request.source, *middle, request.destination)
            if set(nodes) & set(c.avoid_nodes) or not set(c.include_nodes) <= set(nodes):
                continue
            if tuple(node for node in nodes if node in c.ordered_waypoints) != c.ordered_waypoints:
                continue
            if c.max_hops is not None and len(nodes) - 1 > c.max_hops:
                continue
            options = [[link for link in graph.links if (link.source, link.target) == (src, dst)]
                       for src, dst in zip(nodes, nodes[1:])]
            for path in product(*options):
                if any(not link.oper_up or not link.admin_up or link.id in c.avoid_links or
                       link.available_bw_mbps < request.bandwidth_mbps for link in path):
                    continue
                if any(set(link.admin_groups) & set(request.affinity.exclude_admin_groups) or
                       not set(request.affinity.include_all_admin_groups) <= set(link.admin_groups) or
                       (request.affinity.include_any_admin_groups and not
                        set(request.affinity.include_any_admin_groups) & set(link.admin_groups)) for link in path):
                    continue
                metric = sum(link.metric for link in path)
                if c.max_metric is not None and metric > c.max_metric:
                    continue
                residual = min(link.available_bw_mbps for link in path) - request.bandwidth_mbps
                key = (metric if c.prefer_lower_metric else 0, -residual if c.prefer_higher_bottleneck else 0,
                       len(path), tuple(link.id for link in path), nodes)
                feasible.append((key, nodes, tuple(link.id for link in path)))
    return sorted(feasible)[:c.max_candidates]


@st.composite
def graph_cases(draw):
    count = draw(st.integers(2, 5))
    names = tuple(chr(65 + i) for i in range(count))
    pairs = [(a, b) for a in names for b in names if a != b]
    raw_edges = draw(st.lists(st.tuples(st.sampled_from(pairs), st.integers(0, 20), st.integers(1, 10),
                                      st.booleans(), st.sampled_from([(), ("green",), ("blue",)])), max_size=12))
    links = [edge(f"e{i:02}", src, dst, metric=metric, capacity=bw, oper_up=up, admin_groups=groups)
             for i, ((src, dst), metric, bw, up, groups) in enumerate(raw_edges)]
    includes = draw(st.lists(st.sampled_from(names), unique=True, max_size=count))
    waypoints = draw(st.lists(st.sampled_from(names), unique=True, max_size=count))
    avoidable = sorted(set(names[1:-1]) - set(includes) - set(waypoints))
    avoids = draw(st.lists(st.sampled_from(avoidable), unique=True)) if avoidable else []
    avoided_links = draw(st.lists(st.sampled_from([link.id for link in links]), unique=True)) if links else []
    constraints = PathConstraints(
        include_nodes=tuple(includes), ordered_waypoints=tuple(waypoints), avoid_nodes=tuple(avoids),
        avoid_links=tuple(avoided_links), max_hops=draw(st.integers(1, count)), max_metric=draw(st.integers(0, 60)),
        max_candidates=draw(st.integers(1, 4)), prefer_lower_metric=draw(st.booleans()),
        prefer_higher_bottleneck=draw(st.booleans()),
    )
    affinity = draw(st.sampled_from([Affinity(), Affinity(exclude_admin_groups=("blue",)),
                                    Affinity(include_any_admin_groups=("green",)),
                                    Affinity(include_all_admin_groups=("green",))]))
    request = ServiceDemand("random", names[0], names[-1], draw(st.integers(1, 10)),
                            constraints=constraints, affinity=affinity)
    return topology(links, names), request


@given(graph_cases())
@settings(max_examples=200, deadline=None, derandomize=True)
def test_random_directed_multigraph_matches_independent_exhaustive_oracle(case):
    graph, request = case
    expected = exhaustive_oracle(graph, request)
    if not expected:
        with pytest.raises(CspfNoPath):
            compute_cspf(graph, request)
        return
    result = compute_cspf(graph, request)
    assert [(candidate.path_nodes, tuple(link.id for link in candidate.path_links))
            for candidate in result.candidate_paths] == [(nodes, link_ids) for _, nodes, link_ids in expected]
    for candidate in result.candidate_paths:
        assert len(candidate.path_nodes) == len(set(candidate.path_nodes))
        assert candidate.total_metric <= request.constraints.max_metric
        assert candidate.hop_count <= request.constraints.max_hops
        for src, dst, link in zip(candidate.path_nodes, candidate.path_nodes[1:], candidate.path_links):
            assert (link.source, link.target) == (src, dst)
            assert link.available_bw_bps >= request.bandwidth_bps
    reordered = replace(graph, links=list(reversed(graph.links)))
    assert compute_cspf(reordered, request).to_summary_dict() == result.to_summary_dict()
