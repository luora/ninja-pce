from __future__ import annotations

from itertools import islice
from typing import Any, Iterable

import networkx as nx

from .models import CandidatePath, CspfResult, Link, PathHop, PathLink, ServiceDemand, Topology


class CspfNoPath(RuntimeError):
    """Raised when CSPF cannot find a feasible path."""


def _link_allowed(link: Link, demand: ServiceDemand) -> tuple[bool, str | None]:
    if link.id in demand.constraints.avoid_links:
        return False, "link is listed in avoid_links"

    available = link.available_bw_mbps
    if available < demand.bandwidth_mbps:
        return False, f"insufficient bandwidth: available {available:g} Mbps < demand {demand.bandwidth_mbps:g} Mbps"

    groups = set(link.admin_groups)
    exclude = set(demand.affinity.exclude_admin_groups)
    include_any = set(demand.affinity.include_any_admin_groups)
    include_all = set(demand.affinity.include_all_admin_groups)

    blocked = groups & exclude
    if blocked:
        return False, f"excluded admin group match: {sorted(blocked)}"

    if include_any and not (groups & include_any):
        return False, f"missing any required admin group from: {sorted(include_any)}"

    if include_all and not include_all.issubset(groups):
        return False, f"missing all required admin groups: {sorted(include_all - groups)}"

    return True, None


def _add_edge(graph: nx.DiGraph, link: Link, source: str, target: str, link_id: str, from_ip: str | None, to_ip: str | None) -> None:
    graph.add_edge(
        source,
        target,
        id=link_id,
        base_id=link.id,
        metric=link.metric,
        reservable_bw_mbps=link.reservable_bw_mbps,
        reserved_bw_mbps=link.reserved_bw_mbps,
        available_bw_mbps=link.available_bw_mbps,
        admin_groups=link.admin_groups,
        from_ip=from_ip,
        to_ip=to_ip,
        srlg=link.srlg,
        area=link.area,
        protection=link.protection,
    )


def _edge_to_path_link(source: str, target: str, edge: dict[str, Any]) -> PathLink:
    return PathLink(
        id=str(edge["id"]),
        source=source,
        target=target,
        metric=int(edge["metric"]),
        reservable_bw_mbps=float(edge["reservable_bw_mbps"]),
        reserved_bw_mbps=float(edge["reserved_bw_mbps"]),
        available_bw_mbps=float(edge["available_bw_mbps"]),
        admin_groups=tuple(edge["admin_groups"]),
        from_ip=edge.get("from_ip"),
        to_ip=edge.get("to_ip"),
        srlg=tuple(edge.get("srlg", ())),
        area=edge.get("area"),
        protection=edge.get("protection"),
    )


def _path_links(graph: nx.DiGraph, path_nodes: list[str]) -> tuple[PathLink, ...]:
    links: list[PathLink] = []
    for source, target in zip(path_nodes, path_nodes[1:]):
        links.append(_edge_to_path_link(source, target, graph[source][target]))
    return tuple(links)


def _metric(path_links: Iterable[PathLink]) -> int:
    return sum(link.metric for link in path_links)


def _bottleneck(path_links: Iterable[PathLink]) -> float:
    vals = [link.available_bw_mbps for link in path_links]
    return min(vals) if vals else 0.0


def _candidate_allowed(path_nodes: list[str], path_links: tuple[PathLink, ...], demand: ServiceDemand) -> tuple[bool, str | None]:
    constraints = demand.constraints

    if constraints.max_hops is not None and (len(path_nodes) - 1) > constraints.max_hops:
        return False, f"hop count {len(path_nodes) - 1} exceeds max_hops {constraints.max_hops}"

    total_metric = _metric(path_links)
    if constraints.max_metric is not None and total_metric > constraints.max_metric:
        return False, f"metric {total_metric} exceeds max_metric {constraints.max_metric}"

    missing_required_nodes = set(constraints.include_nodes) - set(path_nodes)
    if missing_required_nodes:
        return False, f"missing include_nodes: {sorted(missing_required_nodes)}"

    return True, None


def _score(total_metric: int, bottleneck: float, demand: ServiceDemand) -> float:
    # Lower score wins. Metric dominates. Bottleneck is a tie-breaker when enabled.
    score = float(total_metric if demand.constraints.prefer_lower_metric else 0)
    if demand.constraints.prefer_higher_bottleneck:
        score -= bottleneck / 10_000.0
    return score


def _build_candidate(rank: int, path_nodes: list[str], path_links: tuple[PathLink, ...], demand: ServiceDemand, notes: tuple[str, ...] = ()) -> CandidatePath:
    bottleneck = _bottleneck(path_links)
    residual = max(bottleneck - demand.bandwidth_mbps, 0.0)
    total_metric = _metric(path_links)
    return CandidatePath(
        rank=rank,
        path_nodes=tuple(path_nodes),
        path_links=path_links,
        total_metric=total_metric,
        bottleneck_available_bw_mbps=bottleneck,
        residual_after_reservation_mbps=residual,
        hop_count=max(len(path_nodes) - 1, 0),
        score=_score(total_metric, bottleneck, demand),
        notes=notes,
    )


def _candidate_sort_key(candidate: CandidatePath) -> tuple[float, int, int, tuple[str, ...]]:
    return (candidate.score, candidate.total_metric, candidate.hop_count, candidate.path_nodes)


def compute_cspf(topology: Topology, demand: ServiceDemand) -> CspfResult:
    """Compute a bandwidth-aware CSPF path and alternate candidates.

    The prototype CSPF logic remains intentionally operator-readable:
    1. Prune links that fail bandwidth, affinity, or avoid-link constraints.
    2. Remove avoid-nodes from the graph, except source/destination.
    3. Generate weighted shortest simple-path candidates.
    4. Filter candidates by max_hops, max_metric, and include_nodes.
    5. Select the best candidate and render dry-run configuration.
    """
    if demand.source not in topology.nodes:
        raise CspfNoPath(f"Unknown source node: {demand.source}")
    if demand.destination not in topology.nodes:
        raise CspfNoPath(f"Unknown destination node: {demand.destination}")

    graph = nx.DiGraph()
    for node_id in topology.nodes:
        graph.add_node(node_id)

    pruned: list[dict[str, Any]] = []
    avoid_nodes = set(demand.constraints.avoid_nodes) - {demand.source, demand.destination}

    for link in topology.links:
        if link.source in avoid_nodes or link.target in avoid_nodes:
            pruned.append({"id": link.id, "from": link.source, "to": link.target, "reason": "touches avoid_nodes"})
            continue

        allowed, reason = _link_allowed(link, demand)
        if not allowed:
            pruned.append({"id": link.id, "from": link.source, "to": link.target, "reason": reason})
            continue

        _add_edge(graph, link, link.source, link.target, link.id, link.from_ip, link.to_ip)
        if link.bidirectional:
            _add_edge(graph, link, link.target, link.source, f"{link.id}:reverse", link.to_ip, link.from_ip)

    graph.remove_nodes_from(avoid_nodes)

    raw_candidates: list[CandidatePath] = []
    rejected_candidates: list[dict[str, Any]] = []
    requested = demand.constraints.max_candidates
    # Generate more than requested so filtering does not leave us with only one option.
    generation_limit = max(requested * 10, requested + 5)

    try:
        paths_iter = nx.shortest_simple_paths(graph, demand.source, demand.destination, weight="metric")
        for raw_path in islice(paths_iter, generation_limit):
            links = _path_links(graph, raw_path)
            allowed, reason = _candidate_allowed(raw_path, links, demand)
            if not allowed:
                rejected_candidates.append({"path": raw_path, "reason": reason})
                continue
            candidate = _build_candidate(0, raw_path, links, demand)
            raw_candidates.append(candidate)
            if len(raw_candidates) >= requested:
                break
    except (nx.NetworkXNoPath, nx.NodeNotFound) as exc:
        raise CspfNoPath(
            f"No feasible CSPF path from {demand.source} to {demand.destination} for {demand.bandwidth_mbps:g} Mbps"
        ) from exc

    if not raw_candidates:
        hint = ""
        if rejected_candidates:
            sample = rejected_candidates[:3]
            hint = f"; rejected candidate examples: {sample}"
        raise CspfNoPath(
            f"No candidate path passed all constraints from {demand.source} to {demand.destination}{hint}"
        )

    sorted_candidates = sorted(raw_candidates, key=_candidate_sort_key)
    candidates: list[CandidatePath] = []
    for rank, candidate in enumerate(sorted_candidates[:requested], start=1):
        candidates.append(
            CandidatePath(
                rank=rank,
                path_nodes=candidate.path_nodes,
                path_links=candidate.path_links,
                total_metric=candidate.total_metric,
                bottleneck_available_bw_mbps=candidate.bottleneck_available_bw_mbps,
                residual_after_reservation_mbps=candidate.residual_after_reservation_mbps,
                hop_count=candidate.hop_count,
                score=candidate.score,
                notes=candidate.notes,
            )
        )

    selected = candidates[0]

    explicit_hops = []
    for idx, node_id in enumerate(selected.path_nodes[1:], start=1):
        node = topology.nodes[node_id]
        explicit_hops.append(
            PathHop(
                index=idx,
                node_id=node.id,
                loopback=node.loopback,
                role=node.role,
                area=node.area,
                hop_type=demand.hop_type,
            )
        )

    warnings: list[str] = []
    if selected.residual_after_reservation_mbps < demand.bandwidth_mbps * 0.10:
        warnings.append(
            "Selected path has less than 10% headroom after reservation; consider a wider path or lower reservation."
        )
    if demand.constraints.avoid_nodes:
        unknown = set(demand.constraints.avoid_nodes) - set(topology.nodes)
        if unknown:
            warnings.append(f"avoid_nodes contains unknown node ids: {sorted(unknown)}")
    if demand.constraints.include_nodes:
        unknown = set(demand.constraints.include_nodes) - set(topology.nodes)
        if unknown:
            warnings.append(f"include_nodes contains unknown node ids: {sorted(unknown)}")

    return CspfResult(
        demand=demand,
        path_nodes=selected.path_nodes,
        explicit_hops=tuple(explicit_hops),
        path_links=selected.path_links,
        total_metric=selected.total_metric,
        bottleneck_available_bw_mbps=selected.bottleneck_available_bw_mbps,
        residual_after_reservation_mbps=selected.residual_after_reservation_mbps,
        pruned_links=tuple(pruned),
        candidate_paths=tuple(candidates),
        selected_rank=selected.rank,
        warnings=tuple(warnings),
    )
