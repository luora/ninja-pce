from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

from .models import (
    Affinity,
    DemandOutput,
    Inventory,
    Link,
    ManagedDevice,
    Node,
    PathConstraints,
    ServiceDemand,
    Topology,
)


class InputError(ValueError):
    """Raised when a topology or demand file is invalid."""


def _load_mapping(path: str | Path) -> dict[str, Any]:
    p = Path(path)
    if not p.exists():
        raise InputError(f"Input file not found: {p}")

    text = p.read_text(encoding="utf-8")
    if p.suffix.lower() in {".yaml", ".yml"}:
        data = yaml.safe_load(text)
    elif p.suffix.lower() == ".json":
        data = json.loads(text)
    else:
        raise InputError(f"Unsupported input type for {p}. Use .yml, .yaml, or .json")

    if not isinstance(data, dict):
        raise InputError(f"Input file must contain a mapping/object: {p}")
    return data


def _tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    return tuple(str(item) for item in value)


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    return int(value)


def load_topology(path: str | Path) -> Topology:
    data = _load_mapping(path)
    metadata = data.get("metadata", {}) or {}
    node_items = data.get("nodes", [])
    link_items = data.get("links", [])

    if not node_items:
        raise InputError("Topology must contain at least one node")
    if not link_items:
        raise InputError("Topology must contain at least one link")

    nodes: dict[str, Node] = {}
    for item in node_items:
        try:
            node = Node(
                id=str(item["id"]),
                loopback=str(item["loopback"]),
                role=str(item.get("role", "P")),
                area=str(item["area"]) if item.get("area") is not None else None,
                tags=_tuple(item.get("tags", [])),
            )
        except KeyError as exc:
            raise InputError(f"Node is missing required field: {exc}") from exc
        if node.id in nodes:
            raise InputError(f"Duplicate node id: {node.id}")
        nodes[node.id] = node

    links: list[Link] = []
    seen_links: set[str] = set()
    for item in link_items:
        try:
            source = str(item["from"])
            target = str(item["to"])
            link_id = str(item["id"])
            if link_id in seen_links:
                raise InputError(f"Duplicate link id: {link_id}")
            seen_links.add(link_id)
            if source not in nodes:
                raise InputError(f"Link {item.get('id', '<unnamed>')} references unknown source node {source}")
            if target not in nodes:
                raise InputError(f"Link {item.get('id', '<unnamed>')} references unknown target node {target}")

            link = Link(
                id=link_id,
                source=source,
                target=target,
                from_ip=str(item["from_ip"]) if item.get("from_ip") is not None else None,
                to_ip=str(item["to_ip"]) if item.get("to_ip") is not None else None,
                metric=int(item.get("metric", 1)),
                reservable_bw_mbps=float(item.get("reservable_bw_mbps", item.get("max_bw_mbps", 0))),
                reserved_bw_mbps=float(item.get("reserved_bw_mbps", 0)),
                admin_groups=_tuple(item.get("admin_groups", [])),
                bidirectional=bool(item.get("bidirectional", True)),
                srlg=_tuple(item.get("srlg", [])),
                area=str(item["area"]) if item.get("area") is not None else None,
                protection=str(item["protection"]) if item.get("protection") is not None else None,
            )
        except KeyError as exc:
            raise InputError(f"Link is missing required field: {exc}") from exc
        if link.metric < 0:
            raise InputError(f"Link {link.id} has negative metric")
        if link.reservable_bw_mbps <= 0:
            raise InputError(f"Link {link.id} has no positive reservable bandwidth")
        if link.reserved_bw_mbps < 0:
            raise InputError(f"Link {link.id} has negative reserved bandwidth")
        links.append(link)

    return Topology(
        name=str(metadata.get("name", Path(path).stem)),
        nodes=nodes,
        links=links,
        raw_metadata=metadata,
    )


def load_demand(path: str | Path) -> ServiceDemand:
    data = _load_mapping(path)
    service = data.get("service", data)
    if not isinstance(service, dict):
        raise InputError("Demand file must contain a service mapping/object")

    affinity_data = service.get("affinity", {}) or {}
    output_data = service.get("output", {}) or {}
    path_data = service.get("path", {}) or {}
    constraints_data = service.get("constraints", {}) or {}
    # Keep backward compatibility with earlier MVP demand files where constraints
    # lived under path:.
    merged_constraints = {**path_data, **constraints_data}

    try:
        affinity = Affinity(
            exclude_admin_groups=_tuple(affinity_data.get("exclude_admin_groups", [])),
            include_any_admin_groups=_tuple(affinity_data.get("include_any_admin_groups", [])),
            include_all_admin_groups=_tuple(affinity_data.get("include_all_admin_groups", [])),
        )
        constraints = PathConstraints(
            avoid_nodes=_tuple(merged_constraints.get("avoid_nodes", [])),
            avoid_links=_tuple(merged_constraints.get("avoid_links", [])),
            include_nodes=_tuple(merged_constraints.get("include_nodes", [])),
            max_hops=_optional_int(merged_constraints.get("max_hops")),
            max_metric=_optional_int(merged_constraints.get("max_metric")),
            max_candidates=int(merged_constraints.get("max_candidates", 3)),
            prefer_lower_metric=bool(merged_constraints.get("prefer_lower_metric", True)),
            prefer_higher_bottleneck=bool(merged_constraints.get("prefer_higher_bottleneck", True)),
        )
        output = DemandOutput(
            dry_run=bool(output_data.get("dry_run", True)),
            tunnel_id=int(output_data.get("tunnel_id", 100)),
            tunnel_name=str(output_data.get("tunnel_name", "TUN_NINJA_PCE")),
            explicit_path_name=str(output_data.get("explicit_path_name", "EP_NINJA_PCE")),
            iosxr_interface=str(output_data.get("iosxr_interface", "tunnel-te100")),
            junos_lsp_name=str(output_data.get("junos_lsp_name", "lsp-ninja-pce")),
            junos_primary_path_name=str(output_data.get("junos_primary_path_name", "primary-ninja-pce")),
            formats=tuple(output_data.get("formats", ["generic", "iosxr", "junos"])),
            write_plan=bool(output_data.get("write_plan", True)),
            write_verification=bool(output_data.get("write_verification", True)),
        )
        demand = ServiceDemand(
            name=str(service["name"]),
            source=str(service["source"]),
            destination=str(service["destination"]),
            bandwidth_mbps=float(service["bandwidth_mbps"]),
            setup_priority=int(service.get("setup_priority", 7)),
            hold_priority=int(service.get("hold_priority", 7)),
            description=str(service["description"]) if service.get("description") is not None else None,
            affinity=affinity,
            constraints=constraints,
            hop_type=str(path_data.get("hop_type", "strict")),
            output=output,
        )
    except KeyError as exc:
        raise InputError(f"Demand is missing required field: {exc}") from exc

    if demand.bandwidth_mbps <= 0:
        raise InputError("Demand bandwidth must be positive")
    if demand.constraints.max_candidates < 1:
        raise InputError("max_candidates must be at least 1")
    return demand


def load_inventory(path: str | Path) -> Inventory:
    """Load the operator-maintained host/OS inventory.

    The canonical format is a list of device objects. The older GNS3 mapping
    format remains accepted so existing labs continue to work.
    """
    data = _load_mapping(path)
    raw_devices = data.get("devices")
    if not raw_devices:
        raise InputError("Inventory must contain at least one device")

    if isinstance(raw_devices, dict):
        device_items = []
        for name, item in raw_devices.items():
            if not isinstance(item, dict):
                raise InputError(f"Inventory device {name!r} must be a mapping/object")
            device_items.append({"name": name, **item})
    elif isinstance(raw_devices, list):
        device_items = raw_devices
    else:
        raise InputError("Inventory 'devices' must be a list or mapping/object")

    devices: dict[str, ManagedDevice] = {}
    node_mappings: dict[str, str] = {}
    for item in device_items:
        if not isinstance(item, dict):
            raise InputError("Each inventory device must be a mapping/object")
        try:
            name = str(item["name"])
            topology_node_id = str(item.get("topology_node_id", name))
            host = str(item.get("host", item.get("management_ip", ""))).strip()
            device_type = str(item.get("device_type", item.get("platform", ""))).strip()
            username = str(item.get("username", "")).strip()
            port = int(item.get("port", item.get("ssh_port", 22)))
        except KeyError as exc:
            raise InputError(f"Inventory device is missing required field: {exc}") from exc
        except (TypeError, ValueError) as exc:
            raise InputError(f"Invalid inventory device {item.get('name', '<unnamed>')}: {exc}") from exc

        if not name:
            raise InputError("Inventory device name cannot be empty")
        if name in devices:
            raise InputError(f"Duplicate inventory device name: {name}")
        if not topology_node_id:
            raise InputError(f"Inventory device {name} has no topology_node_id")
        if topology_node_id in node_mappings:
            raise InputError(
                f"Topology node {topology_node_id} is mapped by both "
                f"{node_mappings[topology_node_id]} and {name}"
            )
        if not host:
            raise InputError(f"Inventory device {name} has no host/management_ip")
        if not device_type:
            raise InputError(f"Inventory device {name} has no device_type/platform")
        if not username:
            raise InputError(f"Inventory device {name} has no username")
        if not 1 <= port <= 65535:
            raise InputError(f"Inventory device {name} has invalid port: {port}")

        device = ManagedDevice(
            name=name,
            topology_node_id=topology_node_id,
            host=host,
            port=port,
            device_type=device_type,
            username=username,
            password_env=str(item["password_env"]) if item.get("password_env") else None,
            role=str(item["role"]) if item.get("role") is not None else None,
            tags=_tuple(item.get("tags", [])),
        )
        devices[name] = device
        node_mappings[topology_node_id] = name

    return Inventory(devices=devices)
