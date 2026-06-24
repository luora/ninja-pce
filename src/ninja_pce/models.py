from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Node:
    id: str
    loopback: str
    role: str = "P"
    area: str | None = None
    tags: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class Link:
    id: str
    source: str
    target: str
    metric: int
    reservable_bw_mbps: float
    reserved_bw_mbps: float = 0.0
    admin_groups: tuple[str, ...] = field(default_factory=tuple)
    bidirectional: bool = True
    from_ip: str | None = None
    to_ip: str | None = None
    srlg: tuple[str, ...] = field(default_factory=tuple)
    area: str | None = None
    protection: str | None = None

    @property
    def available_bw_mbps(self) -> float:
        return max(self.reservable_bw_mbps - self.reserved_bw_mbps, 0.0)


@dataclass(frozen=True)
class Affinity:
    exclude_admin_groups: tuple[str, ...] = field(default_factory=tuple)
    include_any_admin_groups: tuple[str, ...] = field(default_factory=tuple)
    include_all_admin_groups: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class PathConstraints:
    avoid_nodes: tuple[str, ...] = field(default_factory=tuple)
    avoid_links: tuple[str, ...] = field(default_factory=tuple)
    include_nodes: tuple[str, ...] = field(default_factory=tuple)
    max_hops: int | None = None
    max_metric: int | None = None
    max_candidates: int = 3
    prefer_lower_metric: bool = True
    prefer_higher_bottleneck: bool = True


@dataclass(frozen=True)
class DemandOutput:
    dry_run: bool = True
    tunnel_id: int = 100
    tunnel_name: str = "TUN_NINJA_PCE"
    explicit_path_name: str = "EP_NINJA_PCE"
    iosxr_interface: str = "tunnel-te100"
    junos_lsp_name: str = "lsp-ninja-pce"
    junos_primary_path_name: str = "primary-ninja-pce"
    formats: tuple[str, ...] = ("generic", "iosxr", "junos")
    write_plan: bool = True
    write_verification: bool = True


@dataclass(frozen=True)
class ServiceDemand:
    name: str
    source: str
    destination: str
    bandwidth_mbps: float
    setup_priority: int = 7
    hold_priority: int = 7
    description: str | None = None
    affinity: Affinity = field(default_factory=Affinity)
    constraints: PathConstraints = field(default_factory=PathConstraints)
    hop_type: str = "strict"
    output: DemandOutput = field(default_factory=DemandOutput)


@dataclass(frozen=True)
class Topology:
    name: str
    nodes: dict[str, Node]
    links: list[Link]
    raw_metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ManagedDevice:
    name: str
    topology_node_id: str
    host: str
    device_type: str
    username: str
    port: int = 22
    password_env: str | None = None
    role: str | None = None
    tags: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class Inventory:
    devices: dict[str, ManagedDevice]

    def device_for_node(self, node_id: str) -> ManagedDevice:
        matches = [device for device in self.devices.values() if device.topology_node_id == node_id]
        if not matches:
            raise KeyError(node_id)
        if len(matches) > 1:
            raise ValueError(f"Topology node {node_id} maps to multiple inventory devices")
        return matches[0]


@dataclass(frozen=True)
class PathHop:
    index: int
    node_id: str
    loopback: str
    role: str
    area: str | None
    hop_type: str = "strict"


@dataclass(frozen=True)
class PathLink:
    id: str
    source: str
    target: str
    metric: int
    reservable_bw_mbps: float
    reserved_bw_mbps: float
    available_bw_mbps: float
    admin_groups: tuple[str, ...]
    from_ip: str | None = None
    to_ip: str | None = None
    srlg: tuple[str, ...] = field(default_factory=tuple)
    area: str | None = None
    protection: str | None = None


@dataclass(frozen=True)
class CandidatePath:
    rank: int
    path_nodes: tuple[str, ...]
    path_links: tuple[PathLink, ...]
    total_metric: int
    bottleneck_available_bw_mbps: float
    residual_after_reservation_mbps: float
    hop_count: int
    score: float
    notes: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {
            "rank": self.rank,
            "path_nodes": list(self.path_nodes),
            "total_metric": self.total_metric,
            "hop_count": self.hop_count,
            "bottleneck_available_bw_mbps": self.bottleneck_available_bw_mbps,
            "residual_after_reservation_mbps": self.residual_after_reservation_mbps,
            "score": self.score,
            "notes": list(self.notes),
            "links": [
                {
                    "id": link.id,
                    "source": link.source,
                    "target": link.target,
                    "metric": link.metric,
                    "available_bw_mbps": link.available_bw_mbps,
                    "reservable_bw_mbps": link.reservable_bw_mbps,
                    "reserved_bw_mbps": link.reserved_bw_mbps,
                    "admin_groups": list(link.admin_groups),
                    "srlg": list(link.srlg),
                    "area": link.area,
                    "protection": link.protection,
                }
                for link in self.path_links
            ],
        }


@dataclass(frozen=True)
class CspfResult:
    demand: ServiceDemand
    path_nodes: tuple[str, ...]
    explicit_hops: tuple[PathHop, ...]
    path_links: tuple[PathLink, ...]
    total_metric: int
    bottleneck_available_bw_mbps: float
    residual_after_reservation_mbps: float
    pruned_links: tuple[dict[str, Any], ...]
    candidate_paths: tuple[CandidatePath, ...]
    selected_rank: int = 1
    warnings: tuple[str, ...] = field(default_factory=tuple)

    def to_summary_dict(self) -> dict[str, Any]:
        return {
            "service": self.demand.name,
            "source": self.demand.source,
            "destination": self.demand.destination,
            "bandwidth_mbps": self.demand.bandwidth_mbps,
            "path_nodes": list(self.path_nodes),
            "selected_rank": self.selected_rank,
            "explicit_hops": [
                {
                    "index": hop.index,
                    "node": hop.node_id,
                    "loopback": hop.loopback,
                    "role": hop.role,
                    "area": hop.area,
                    "hop_type": hop.hop_type,
                }
                for hop in self.explicit_hops
            ],
            "links": [
                {
                    "id": link.id,
                    "source": link.source,
                    "target": link.target,
                    "metric": link.metric,
                    "available_bw_mbps": link.available_bw_mbps,
                    "reservable_bw_mbps": link.reservable_bw_mbps,
                    "reserved_bw_mbps": link.reserved_bw_mbps,
                    "admin_groups": list(link.admin_groups),
                    "srlg": list(link.srlg),
                    "area": link.area,
                    "protection": link.protection,
                }
                for link in self.path_links
            ],
            "total_metric": self.total_metric,
            "bottleneck_available_bw_mbps": self.bottleneck_available_bw_mbps,
            "residual_after_reservation_mbps": self.residual_after_reservation_mbps,
            "candidate_paths": [candidate.to_dict() for candidate in self.candidate_paths],
            "pruned_links": list(self.pruned_links),
            "warnings": list(self.warnings),
            "dry_run": self.demand.output.dry_run,
        }
