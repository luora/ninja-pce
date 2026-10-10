from __future__ import annotations

from dataclasses import replace
from itertools import repeat
from typing import Any, Iterator

from .loaders import InputError
from .models import CandidatePath, CspfResult, Link, PathHop, PathLink, ServiceDemand, Topology


class CspfNoPath(RuntimeError):
    """Raised only after proving that no feasible simple directed path exists."""


class CspfSearchLimit(RuntimeError):
    """INDETERMINATE: the search budget prevents a proof of feasibility/optimality."""

    status = "SEARCH_LIMIT"

    def __init__(self, budget: int, explored: int, feasible_found: int) -> None:
        self.budget = budget
        self.explored = explored
        self.feasible_found = feasible_found
        super().__init__(f"INDETERMINATE / SEARCH_LIMIT: exhausted {budget} edge expansions; "
                         f"found {feasible_found} feasible candidates but global ranking is unproven")


def _link_allowed(link: Link, demand: ServiceDemand) -> tuple[bool, str | None]:
    if link.id in demand.constraints.avoid_links or link.base_id in demand.constraints.avoid_links:
        return False, "link is listed in avoid_links"
    if not link.admin_up or not link.oper_up:
        return False, "link administrative or operational state is down"
    available = link.available_bps(demand.setup_priority)
    if available < demand.bandwidth_bps:
        return False, (f"insufficient nonpreemptive bandwidth at setup priority {demand.setup_priority}: "
                       f"available {available} bit/s < demand {demand.bandwidth_bps} bit/s")
    groups = set(link.admin_groups)
    exclude = set(demand.affinity.exclude_admin_groups)
    include_any = set(demand.affinity.include_any_admin_groups)
    include_all = set(demand.affinity.include_all_admin_groups)
    if groups & exclude:
        return False, f"excluded admin group match: {sorted(groups & exclude)}"
    if include_any and not (groups & include_any):
        return False, f"missing any required admin group from: {sorted(include_any)}"
    if not include_all <= groups:
        return False, f"missing all required admin groups: {sorted(include_all - groups)}"
    return True, None


def _path_link(link: Link, demand: ServiceDemand) -> PathLink:
    assert link.reserved_bw_bps is not None and link.max_bw_bps is not None
    available = link.available_bps(demand.setup_priority)
    return PathLink(
        id=link.id, source=link.source, target=link.target, metric=link.metric,
        reservable_bw_mbps=link.reservable_bw_mbps, reserved_bw_mbps=link.reserved_bw_mbps,
        available_bw_mbps=available / 1_000_000, admin_groups=link.admin_groups,
        from_ip=link.from_ip, to_ip=link.to_ip, srlg=link.srlg, area=link.area, protection=link.protection,
        available_bw_bps=available, reservable_bw_bps=link.bandwidth_bps,
        reserved_bw_bps=link.reserved_bw_bps, max_bw_bps=link.max_bw_bps,
        unreserved_bw_bps=link.unreserved_bw_bps, pending_bw_bps=link.pending_bw_bps,
        setup_priority=demand.setup_priority, base_id=link.base_id,
        local_interface=link.local_interface, remote_interface=link.remote_interface,
    )


def _build_candidate(nodes: tuple[str, ...], links: tuple[Link, ...], demand: ServiceDemand) -> CandidatePath:
    path_links = tuple(_path_link(link, demand) for link in links)
    bottleneck = min(link.available_bw_bps for link in path_links)
    total_metric = sum(link.metric for link in path_links)
    return CandidatePath(
        rank=0, path_nodes=nodes, path_links=path_links, total_metric=total_metric,
        bottleneck_available_bw_mbps=bottleneck / 1_000_000,
        residual_after_reservation_mbps=(bottleneck - demand.bandwidth_bps) / 1_000_000,
        hop_count=len(path_links),
        # Compatibility display field only; never used to rank paths.
        score=float(total_metric if demand.constraints.prefer_lower_metric else len(path_links)),
    )


def _candidate_sort_key(candidate: CandidatePath, demand: ServiceDemand) -> tuple[Any, ...]:
    constraints = demand.constraints
    residual_bps = min(link.available_bw_bps for link in candidate.path_links) - demand.bandwidth_bps
    return (
        candidate.total_metric if constraints.prefer_lower_metric else 0,
        -residual_bps if constraints.prefer_higher_bottleneck else 0,
        candidate.hop_count,
        tuple(link.id for link in candidate.path_links),
        candidate.path_nodes,
    )


def _validate(topology: Topology, demand: ServiceDemand, links: list[Link]) -> None:
    """Validate cross-object constraints, including demands built through the public API."""
    nodes = set(topology.nodes)
    if demand.source not in nodes or demand.destination not in nodes:
        raise InputError(f"Unknown source/destination node: {demand.source}->{demand.destination}")
    if demand.source == demand.destination:
        raise InputError("Source and destination must be distinct")
    constraints = demand.constraints
    if {demand.source, demand.destination} & set(constraints.avoid_nodes):
        raise InputError("Source/destination cannot also be avoided")
    required = set(constraints.include_nodes) | set(constraints.ordered_waypoints)
    unknown = (required | set(constraints.avoid_nodes)) - nodes
    if unknown:
        raise InputError(f"Constraints reference unknown nodes: {sorted(unknown)}")
    if required & set(constraints.avoid_nodes):
        raise InputError("Required nodes/waypoints cannot also be avoided")
    if len(set(constraints.ordered_waypoints)) != len(constraints.ordered_waypoints):
        raise InputError("ordered_waypoints cannot repeat nodes in a simple path")
    known_links = {link.id for link in links} | {link.base_id for link in links}
    if set(constraints.avoid_links) - known_links:
        raise InputError(f"avoid_links references unknown links: {sorted(set(constraints.avoid_links) - known_links)}")
    for label, value, minimum in (("max_candidates", constraints.max_candidates, 1),
                                  ("search_budget", constraints.search_budget, 1),
                                  ("max_hops", constraints.max_hops, 1),
                                  ("max_metric", constraints.max_metric, 0)):
        if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < minimum):
            raise InputError(f"{label} must be an integer >= {minimum}")
    if not isinstance(constraints.prefer_lower_metric, bool) or not isinstance(constraints.prefer_higher_bottleneck, bool):
        raise InputError("Path preferences must be boolean")
    capabilities = set(demand.required_capabilities)
    for node_id in nodes - set(constraints.avoid_nodes):
        if node_id in (demand.source, demand.destination) and capabilities - set(topology.nodes[node_id].capabilities):
            raise InputError(f"Node {node_id} lacks required protocol capabilities: {sorted(capabilities)}")
    for link in links:
        if link.source not in nodes or link.target not in nodes:
            raise InputError(f"Link {link.id} references unknown nodes")


def compute_cspf(topology: Topology, demand: ServiceDemand) -> CspfResult:
    """Enumerate constrained simple edge paths, retaining the globally ranked top K.

    Directed parallel resources remain independent throughout. The iterative DFS
    prunes monotone metric/hop constraints and invalid waypoint prefixes. Every
    eligible outgoing edge examined consumes one search_budget unit. Full search
    exhaustion proves ranking/no-path; budget exhaustion always raises the
    distinct CspfSearchLimit, even if a feasible but unproven candidate was found.
    Memory is bounded by graph size, path depth, and max_candidates.
    """
    try:
        links = topology.directional_links
    except ValueError as exc:
        raise InputError(str(exc)) from exc
    _validate(topology, demand, links)
    constraints = demand.constraints
    avoided = set(constraints.avoid_nodes)
    adjacency: dict[str, list[Link]] = {node: [] for node in topology.nodes}
    pruned: list[dict[str, Any]] = []
    for link in links:
        allowed, reason = _link_allowed(link, demand)
        if link.source in avoided or link.target in avoided:
            allowed, reason = False, "touches avoid_nodes"
        if demand.required_capabilities and any(
                not set(demand.required_capabilities) <= set(topology.nodes[node].capabilities)
                for node in (link.source, link.target)):
            allowed, reason = False, "node lacks required protocol capabilities"
        if not allowed:
            pruned.append({"id": link.id, "from": link.source, "to": link.target, "reason": reason})
        else:
            adjacency[link.source].append(link)

    waypoints = constraints.ordered_waypoints
    initial_progress = int(bool(waypoints and waypoints[0] == demand.source))
    # State: node path, exact edge path, accumulated metric, next waypoint index.
    initial: tuple[tuple[str, ...], tuple[Link, ...], int, int] = ((demand.source,), (), 0, initial_progress)
    stack: list[Iterator[tuple[Link | None, tuple[tuple[str, ...], tuple[Link, ...], int, int]]]] = [iter([(None, initial)])]
    retained: list[CandidatePath] = []
    explored = 0
    feasible_found = 0
    while stack:
        try:
            edge, state = next(stack[-1])
        except StopIteration:
            stack.pop()
            continue
        nodes_path, edge_path, metric, progress = state
        if edge is not None:
            if explored >= constraints.search_budget:
                raise CspfSearchLimit(constraints.search_budget, explored, feasible_found)
            explored += 1
            if edge.target in nodes_path:
                continue
            metric += edge.metric
            if constraints.max_metric is not None and metric > constraints.max_metric:
                continue
            if constraints.max_hops is not None and len(edge_path) + 1 > constraints.max_hops:
                continue
            if edge.target in waypoints:
                if progress >= len(waypoints) or edge.target != waypoints[progress]:
                    continue
                progress += 1
            nodes_path = (*nodes_path, edge.target)
            edge_path = (*edge_path, edge)
        if nodes_path[-1] == demand.destination:
            if progress == len(waypoints) and set(constraints.include_nodes) <= set(nodes_path):
                feasible_found += 1
                retained.append(_build_candidate(nodes_path, edge_path, demand))
                retained.sort(key=lambda candidate: _candidate_sort_key(candidate, demand))
                del retained[constraints.max_candidates:]
            continue
        next_state = (nodes_path, edge_path, metric, progress)
        stack.append(iter(zip(adjacency[nodes_path[-1]], repeat(next_state))))
    if not retained:
        raise CspfNoPath(f"No feasible CSPF path from {demand.source} to {demand.destination} "
                         f"for {demand.bandwidth_bps} bit/s after exhaustive constrained search")
    candidates = tuple(replace(candidate, rank=index) for index, candidate in enumerate(retained, 1))
    selected = candidates[0]
    explicit_hops = tuple(
        PathHop(index=index, node_id=node.id, loopback=node.loopback, role=node.role, area=node.area,
                hop_type=demand.hop_type, address=link.to_ip if demand.hop_type == "strict" else node.loopback,
                link_id=link.id, local_address=link.from_ip)
        for index, link in enumerate(selected.path_links, 1)
        for node in (topology.nodes[link.target],)
    )
    warnings = []
    if selected.residual_after_reservation_mbps < demand.bandwidth_mbps * 0.1:
        warnings.append("Selected path has less than 10% headroom after reservation.")
    if any(link.address is None for link in explicit_hops):
        warnings.append("Strict ERO contains an unnumbered link; unsupported renderers must refuse delivery.")
    if demand.hop_type == "loose":
        warnings.append("Loose loopback ERO does not enforce the computed directional link path or its bandwidth feasibility.")
    return CspfResult(
        demand=demand, path_nodes=selected.path_nodes, explicit_hops=explicit_hops,
        path_links=selected.path_links, total_metric=selected.total_metric,
        bottleneck_available_bw_mbps=selected.bottleneck_available_bw_mbps,
        residual_after_reservation_mbps=selected.residual_after_reservation_mbps,
        pruned_links=tuple(pruned), candidate_paths=candidates, warnings=tuple(warnings),
    )
