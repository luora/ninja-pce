from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field, replace
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, InvalidOperation, localcontext
from ipaddress import ip_address
from typing import Any

_IDENTIFIER = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.:-]{0,127}\Z")
_INTERFACE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.:/-]{0,127}\Z")


def validate_identifier(value: str, label: str = "identifier", *, allow_slash: bool = False) -> str:
    pattern = _INTERFACE if allow_slash else _IDENTIFIER
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise ValueError(f"{label} must be a safe CLI identifier (letters, digits, _, ., :, -)")
    return value


def mbps_to_bps(value: Any, *, round_up: bool = True) -> int:
    """Convert decimal Mbps conservatively: ceil reservations, floor capacities."""
    if isinstance(value, (bool, str)) or not isinstance(value, (int, float, Decimal)):
        raise ValueError("bandwidth must be a finite number, not a string or boolean")
    try:
        number = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError("bandwidth must be a finite number") from exc
    if not number.is_finite() or number < 0:
        raise ValueError("bandwidth must be finite and nonnegative")
    # Decimal's ambient context defaults to 28 digits; multiplying in that
    # context could discard the fractional bit before applying ceiling.
    with localcontext() as context:
        context.prec = max(len(number.as_tuple().digits) + 6, 28)
        result = int((number * 1_000_000).to_integral_value(
            rounding=ROUND_CEILING if round_up else ROUND_FLOOR))
    return _bps(result, "bandwidth")


def _bps(value: int, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} must be a nonnegative integer bit/s value")
    try:
        value / 1_000_000  # Public legacy Mbps views must remain representable.
    except OverflowError as exc:
        raise ValueError(f"{label} exceeds the supported numeric range") from exc
    return value


def _priority(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 7:
        raise ValueError("RSVP priority must be an integer from 0 through 7")
    return value


@dataclass(frozen=True)
class Node:
    id: str
    loopback: str
    role: str = "P"
    area: str | None = None
    tags: tuple[str, ...] = field(default_factory=tuple)
    capabilities: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        validate_identifier(self.id, "node id")
        if not isinstance(self.loopback, str) or "%" in self.loopback:
            raise ValueError("Loopback must be an unscoped IP address")
        ip_address(self.loopback)


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

    max_bw_bps: int | None = None
    reservable_bw_bps: int | None = None
    reserved_bw_bps: int | None = None
    unreserved_bw_bps: tuple[int, ...] = field(default_factory=tuple)
    pending_bw_bps: int = 0
    admin_up: bool = True
    oper_up: bool = True
    provenance_source: str | None = None
    timestamp: str | None = None
    local_interface: str | None = None
    remote_interface: str | None = None
    base_id: str | None = None

    def __post_init__(self) -> None:
        for label, value in (("link id", self.id), ("local node", self.source), ("remote node", self.target)):
            validate_identifier(value, label)
        if self.source == self.target:
            raise ValueError("TE links must connect distinct routers")
        if isinstance(self.metric, bool) or not isinstance(self.metric, int) or not 0 <= self.metric <= 4_294_967_295:
            raise ValueError("TE metric must be an unsigned 32-bit integer")
        for flag in (self.bidirectional, self.admin_up, self.oper_up):
            if not isinstance(flag, bool):
                raise ValueError("link states and bidirectional must be booleans")
        for address in (self.from_ip, self.to_ip):
            if address is not None:
                if not isinstance(address, str) or "%" in address:
                    raise ValueError("Interface IP must be an unscoped IP address")
                ip_address(address)
        for interface in (self.local_interface, self.remote_interface):
            if interface is not None:
                validate_identifier(interface, "interface", allow_slash=True)
        reservable = (mbps_to_bps(self.reservable_bw_mbps, round_up=False)
                      if self.reservable_bw_bps is None else _bps(self.reservable_bw_bps, "reservable bandwidth"))
        reserved = (mbps_to_bps(self.reserved_bw_mbps)
                    if self.reserved_bw_bps is None else _bps(self.reserved_bw_bps, "reserved bandwidth"))
        maximum = reservable if self.max_bw_bps is None else _bps(self.max_bw_bps, "maximum bandwidth")
        if reservable <= 0 or reserved > reservable or reservable > maximum:
            raise ValueError("require 0 <= reserved bandwidth <= positive reservable bandwidth <= maximum bandwidth")
        vector = self.unreserved_bw_bps or (reservable - reserved,) * 8
        if len(vector) != 8:
            raise ValueError("unreserved bandwidth must contain exactly eight priorities (0-7)")
        for amount in vector:
            _bps(amount, "unreserved bandwidth")
            if amount > reservable:
                raise ValueError("unreserved bandwidth exceeds reservable bandwidth")
        if any(vector[i] < vector[i + 1] for i in range(7)):
            raise ValueError("unreserved bandwidth must be nonincreasing from priority 0 to 7")
        _bps(self.pending_bw_bps, "pending bandwidth")
        object.__setattr__(self, "reservable_bw_bps", reservable)
        object.__setattr__(self, "reserved_bw_bps", reserved)
        object.__setattr__(self, "max_bw_bps", maximum)
        object.__setattr__(self, "unreserved_bw_bps", tuple(vector))
        object.__setattr__(self, "reservable_bw_mbps", reservable / 1_000_000)
        object.__setattr__(self, "reserved_bw_mbps", reserved / 1_000_000)

    @property
    def bandwidth_bps(self) -> int:
        assert self.reservable_bw_bps is not None
        return self.reservable_bw_bps

    def observed_unreserved_bps(self, priority: int) -> int:
        return self.unreserved_bw_bps[_priority(priority)]

    def available_bps(self, priority: int = 7) -> int:
        """Admission without permission to preempt any existing reservation."""
        assert self.reserved_bw_bps is not None
        return max(min(self.observed_unreserved_bps(priority), self.unreserved_bw_bps[7],
                       self.bandwidth_bps - self.reserved_bw_bps) - self.pending_bw_bps, 0)

    def preemption_required_bps(self, priority: int, requested_bps: int) -> int:
        """Theoretical deficit; callers must never treat this as preemption authority."""
        _bps(requested_bps, "requested bandwidth")
        if requested_bps > max(self.observed_unreserved_bps(priority) - self.pending_bw_bps, 0):
            return requested_bps
        return max(requested_bps - self.available_bps(priority), 0)

    @property
    def available_bw_mbps(self) -> float:
        return self.available_bps(7) / 1_000_000


def normalize_links(links: list[Link]) -> list[Link]:
    """Expand legacy shorthand, preserving explicit directional reverse attributes."""
    explicit: dict[str, Link] = {}
    for link in links:
        if link.id in explicit:
            raise ValueError(f"Duplicate link id: {link.id}")
        explicit[link.id] = link
    normalized = {key: replace(link, bidirectional=False, base_id=link.base_id or link.id)
                  for key, link in explicit.items()}
    for link in links:
        if not link.bidirectional:
            continue
        reverse_id = f"{link.id}:reverse"
        if reverse_id in explicit:
            reverse = explicit[reverse_id]
            if (reverse.source, reverse.target) != (link.target, link.source) or reverse.bidirectional:
                raise ValueError(f"Explicit reverse link {reverse_id} has contradictory endpoints or bidirectionality")
            normalized[reverse_id] = replace(reverse, base_id=link.base_id or link.id)
        else:
            normalized[reverse_id] = replace(
                link, id=reverse_id, source=link.target, target=link.source,
                from_ip=link.to_ip, to_ip=link.from_ip, local_interface=link.remote_interface,
                remote_interface=link.local_interface, bidirectional=False, base_id=link.base_id or link.id,
            )
    return [normalized[key] for key in sorted(normalized)]


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
    ordered_waypoints: tuple[str, ...] = field(default_factory=tuple)
    search_budget: int = 100_000

    def __post_init__(self) -> None:
        for label in ("max_candidates", "search_budget", "max_hops", "max_metric"):
            value = getattr(self, label)
            minimum = 0 if label == "max_metric" else 1
            if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < minimum):
                raise ValueError(f"{label} must be an integer >= {minimum}")
        if not isinstance(self.prefer_lower_metric, bool) or not isinstance(self.prefer_higher_bottleneck, bool):
            raise ValueError("Path preferences must be boolean")
        for label in ("avoid_nodes", "avoid_links", "include_nodes", "ordered_waypoints"):
            values = getattr(self, label)
            if isinstance(values, str) or len(set(values)) != len(values):
                raise ValueError(f"{label} must contain unique identifiers")
            for value in values:
                validate_identifier(value, label)
        if set(self.avoid_nodes) & (set(self.include_nodes) | set(self.ordered_waypoints)):
            raise ValueError("Required nodes and waypoints cannot also be avoided")


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

    def __post_init__(self) -> None:
        for label in ("dry_run", "write_plan", "write_verification"):
            if not isinstance(getattr(self, label), bool):
                raise ValueError(f"{label} must be a boolean")
        for label in ("tunnel_name", "explicit_path_name", "iosxr_interface", "junos_lsp_name", "junos_primary_path_name"):
            validate_identifier(getattr(self, label), label)
        if isinstance(self.tunnel_id, bool) or not isinstance(self.tunnel_id, int) or not 1 <= self.tunnel_id <= 65535:
            raise ValueError("tunnel_id must be an integer from 1 to 65535")
        if not re.fullmatch(r"tunnel-te[0-9]+", self.iosxr_interface):
            raise ValueError("iosxr_interface must be tunnel-te followed by a numeric identifier")
        if not self.formats or isinstance(self.formats, str) or set(self.formats) - {"generic", "iosxr", "iosxe", "junos"}:
            raise ValueError("Unsupported output formats")


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
    reservation_bw_bps: int | None = None
    required_capabilities: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        for value in (self.name, self.source, self.destination):
            validate_identifier(value)
        _priority(self.setup_priority)
        _priority(self.hold_priority)
        # Conservative product policy following RFC 3209 section 4.7.3 SHOULD.
        if self.setup_priority < self.hold_priority:
            raise ValueError("setup_priority must be numerically >= hold_priority")
        if self.hop_type not in {"strict", "loose"}:
            raise ValueError("hop_type must be strict or loose")
        amount = (mbps_to_bps(self.bandwidth_mbps) if self.reservation_bw_bps is None
                  else _bps(self.reservation_bw_bps, "reservation bandwidth"))
        if amount <= 0:
            raise ValueError("Demand bandwidth must be positive")
        object.__setattr__(self, "reservation_bw_bps", amount)
        object.__setattr__(self, "bandwidth_mbps", amount / 1_000_000)
        if self.description is not None and (not isinstance(self.description, str) or
                any(ord(c) < 32 or ord(c) == 127 or c in ';\\"' for c in self.description)):
            raise ValueError("description contains unsafe CLI characters")

    @property
    def bandwidth_bps(self) -> int:
        assert self.reservation_bw_bps is not None
        return self.reservation_bw_bps


@dataclass(frozen=True)
class Topology:
    name: str
    nodes: dict[str, Node]
    links: list[Link]
    raw_metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def directional_links(self) -> list[Link]:
        return normalize_links(self.links)


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
    address: str | None = None
    link_id: str | None = None
    local_address: str | None = None


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
    available_bw_bps: int = 0
    reservable_bw_bps: int = 0
    reserved_bw_bps: int = 0
    max_bw_bps: int = 0
    unreserved_bw_bps: tuple[int, ...] = field(default_factory=tuple)
    pending_bw_bps: int = 0
    setup_priority: int = 7
    base_id: str | None = None
    local_interface: str | None = None
    remote_interface: str | None = None

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["admin_groups"] = list(self.admin_groups)
        value["srlg"] = list(self.srlg)
        value["unreserved_bw_bps"] = list(self.unreserved_bw_bps)
        return value


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
            "links": [link.to_dict() for link in self.path_links],
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
            "bandwidth_bps": self.demand.bandwidth_bps,
            "setup_priority": self.demand.setup_priority,
            "hold_priority": self.demand.hold_priority,
            "optimality_proven": True,
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
                    "address": hop.address,
                    "link_id": hop.link_id,
                    "local_address": hop.local_address,
                }
                for hop in self.explicit_hops
            ],
            "links": [link.to_dict() for link in self.path_links],
            "total_metric": self.total_metric,
            "bottleneck_available_bw_mbps": self.bottleneck_available_bw_mbps,
            "residual_after_reservation_mbps": self.residual_after_reservation_mbps,
            "candidate_paths": [candidate.to_dict() for candidate in self.candidate_paths],
            "pruned_links": list(self.pruned_links),
            "warnings": list(self.warnings),
            "dry_run": self.demand.output.dry_run,
        }
