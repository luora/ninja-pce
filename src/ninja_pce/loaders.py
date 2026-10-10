from __future__ import annotations

import json
import re
from ipaddress import ip_address
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
    mbps_to_bps,
    normalize_links,
    validate_identifier,
)


class InputError(ValueError):
    """Raised when a topology, demand or inventory file is invalid."""


class _UniqueLoader(yaml.SafeLoader):
    pass


# Preserve timestamps as strings; callers validate freshness against their clock.
_UniqueLoader.yaml_implicit_resolvers = {
    key: [(tag, pattern) for tag, pattern in values if tag != "tag:yaml.org,2002:timestamp"]
    for key, values in yaml.SafeLoader.yaml_implicit_resolvers.items()
}


def _unique_pairs(pairs: list[tuple[Any, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if not isinstance(key, str):
            raise InputError("Mapping keys must be strings")
        if key in result:
            raise InputError(f"Duplicate mapping key: {key}")
        result[key] = value
    return result


def _yaml_mapping(loader: _UniqueLoader, node: yaml.MappingNode) -> dict[str, Any]:
    return _unique_pairs([(loader.construct_object(key), loader.construct_object(value))
                          for key, value in node.value])


_UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _yaml_mapping)


def _load_mapping(path: str | Path) -> dict[str, Any]:
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
        if p.suffix.lower() in {".yaml", ".yml"}:
            data = yaml.load(text, Loader=_UniqueLoader)
        elif p.suffix.lower() == ".json":
            data = json.loads(text, object_pairs_hook=_unique_pairs)
        else:
            raise InputError(f"Unsupported input type for {p}. Use .yml, .yaml, or .json")
    except (OSError, UnicodeError, yaml.YAMLError, json.JSONDecodeError) as exc:
        raise InputError(f"Cannot read input {p}: {exc}") from exc
    return _mapping(data, f"Input file {p}")


def _mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise InputError(f"{label} must be a mapping/object")
    return value


def _keys(data: dict[str, Any], allowed: set[str], label: str) -> None:
    unknown = set(data) - allowed
    if unknown:
        raise InputError(f"Unknown {label} fields: {sorted(unknown)}")


def _required(data: dict[str, Any], key: str) -> Any:
    if key not in data:
        raise InputError(f"Missing required field: {key}")
    return data[key]


def _text(value: Any, label: str, *, identifier: bool = False) -> str:
    if not isinstance(value, str) or not value or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise InputError(f"{label} must be a nonempty string without control characters")
    if identifier:
        try:
            validate_identifier(value, label)
        except ValueError as exc:
            raise InputError(str(exc)) from exc
    return value


def _optional_text(value: Any, label: str, *, identifier: bool = False) -> str | None:
    return None if value is None else _text(value, label, identifier=identifier)


def _interface(value: Any, label: str) -> str | None:
    if value is None:
        return None
    text = _text(value, label)
    try:
        return validate_identifier(text, label, allow_slash=True)
    except ValueError as exc:
        raise InputError(str(exc)) from exc


def _tuple(value: Any, label: str = "list") -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        value = [value]  # Retain the unambiguous legacy singleton shorthand.
    if not isinstance(value, list):
        raise InputError(f"{label} must be a list of strings")
    items = tuple(_text(item, label, identifier=True) for item in value)
    if len(set(items)) != len(items):
        raise InputError(f"{label} contains duplicates")
    return items


def _boolean(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        raise InputError(f"{label} must be a boolean true/false, not a quoted string")
    return value


def _integer(value: Any, label: str, minimum: int = 0, maximum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum or (maximum is not None and value > maximum):
        raise InputError(f"{label} must be an integer >= {minimum}" + (f" and <= {maximum}" if maximum is not None else ""))
    return value


def _optional_int(value: Any, label: str = "constraint", minimum: int = 0) -> int | None:
    return None if value is None else _integer(value, label, minimum)


def _address(value: Any, label: str) -> str:
    text = _text(value, label)
    try:
        if "%" in text:
            raise ValueError("Scoped IP addresses are unsupported")
        return str(ip_address(text))
    except ValueError as exc:
        raise InputError(f"Invalid {label}: {text!r}") from exc


def _bandwidth(data: dict[str, Any], stem: str, *, default: int | None = None, reservation: bool = False) -> int:
    bps_key, mbps_key = f"{stem}_bps", f"{stem}_mbps"
    if bps_key in data and mbps_key in data:
        raise InputError(f"Supply only one of {bps_key} and {mbps_key}")
    if bps_key in data:
        amount = _integer(data[bps_key], bps_key)
        try:
            amount / 1_000_000
        except OverflowError as exc:
            raise InputError(f"{bps_key} exceeds the supported numeric range") from exc
        return amount
    if mbps_key in data:
        try:
            return mbps_to_bps(data[mbps_key], round_up=reservation)
        except ValueError as exc:
            raise InputError(f"Invalid {mbps_key}: {exc}") from exc
    if default is None:
        raise InputError(f"Missing bandwidth: supply {bps_key} or {mbps_key}")
    return default


_LINK_KEYS = {
    "id", "from", "to", "from_ip", "to_ip", "metric", "max_bw_mbps", "max_bw_bps",
    "reservable_bw_mbps", "reservable_bw_bps", "reserved_bw_mbps", "reserved_bw_bps",
    "unreserved_bw_mbps", "unreserved_bw_bps", "pending_bw_bps", "admin_groups", "bidirectional",
    "srlg", "area", "protection", "admin_up", "oper_up", "source", "timestamp",
    "local_interface", "remote_interface", "reverse",
}


def _link(item: dict[str, Any], nodes: dict[str, Node], metadata: dict[str, Any]) -> Link:
    _keys(item, _LINK_KEYS, "link")
    link_id = _text(_required(item, "id"), "link id", identifier=True)
    source = _text(_required(item, "from"), "link from", identifier=True)
    target = _text(_required(item, "to"), "link to", identifier=True)
    if source not in nodes or target not in nodes:
        raise InputError(f"Link {link_id} references unknown source/destination node {source}->{target}")
    maximum = _bandwidth(item, "max_bw", default=0)
    reservable = _bandwidth(item, "reservable_bw", default=maximum)
    if not ({"max_bw_mbps", "max_bw_bps"} & set(item)):
        maximum = reservable
    reserved = _bandwidth(item, "reserved_bw", default=0, reservation=True)
    if "unreserved_bw_bps" in item and "unreserved_bw_mbps" in item:
        raise InputError("Supply only one unreserved bandwidth vector and declare its units")
    vector: tuple[int, ...] = ()
    vector_key = "unreserved_bw_bps" if "unreserved_bw_bps" in item else "unreserved_bw_mbps"
    if vector_key in item:
        values = item[vector_key]
        if not isinstance(values, list) or len(values) != 8:
            raise InputError("unreserved bandwidth must contain exactly eight priorities (0-7)")
        try:
            vector = tuple(_integer(v, vector_key) if vector_key.endswith("_bps")
                           else mbps_to_bps(v, round_up=False) for v in values)
        except ValueError as exc:
            raise InputError(f"Invalid {vector_key}: {exc}") from exc
    try:
        return Link(
            id=link_id, source=source, target=target,
            metric=_integer(item.get("metric", 1), "metric", 0, 4_294_967_295),
            reservable_bw_mbps=reservable / 1_000_000, reserved_bw_mbps=reserved / 1_000_000,
            reservable_bw_bps=reservable, reserved_bw_bps=reserved, max_bw_bps=maximum,
            unreserved_bw_bps=vector,
            pending_bw_bps=_integer(item.get("pending_bw_bps", 0), "pending_bw_bps"),
            from_ip=None if item.get("from_ip") is None else _address(item["from_ip"], "from_ip"),
            to_ip=None if item.get("to_ip") is None else _address(item["to_ip"], "to_ip"),
            admin_groups=_tuple(item.get("admin_groups", []), "admin_groups"),
            bidirectional=_boolean(item.get("bidirectional", True), "bidirectional"),
            srlg=_tuple(item.get("srlg", []), "srlg"),
            area=_optional_text(item.get("area"), "area"),
            protection=_optional_text(item.get("protection"), "protection"),
            admin_up=_boolean(item.get("admin_up", True), "admin_up"),
            oper_up=_boolean(item.get("oper_up", True), "oper_up"),
            provenance_source=_optional_text(item.get("source", metadata.get("source", metadata.get("source_type"))), "source"),
            timestamp=_optional_text(item.get("timestamp", metadata.get("timestamp")), "timestamp"),
            local_interface=_interface(item.get("local_interface"), "local_interface"),
            remote_interface=_interface(item.get("remote_interface"), "remote_interface"),
        )
    except (TypeError, ValueError) as exc:
        raise InputError(f"Invalid link {link_id}: {exc}") from exc


def load_topology(path: str | Path) -> Topology:
    data = _load_mapping(path)
    _keys(data, {"metadata", "nodes", "links"}, "topology")
    metadata = _mapping(data.get("metadata", {}), "metadata")
    if "units" in metadata and metadata["units"] != "Mbps":
        raise InputError("Legacy metadata.units must be Mbps; use explicit *_bps fields for bit/s")
    for key in ("complete", "authoritative"):
        if key in metadata:
            _boolean(metadata[key], f"metadata.{key}")
    nodes: dict[str, Node] = {}
    node_items = _required(data, "nodes")
    link_items = _required(data, "links")
    if not isinstance(node_items, list) or not node_items:
        raise InputError("Topology nodes must be a nonempty list")
    if not isinstance(link_items, list) or not link_items:
        raise InputError("Topology links must be a nonempty list")
    for raw in node_items:
        item = _mapping(raw, "node")
        _keys(item, {"id", "loopback", "role", "area", "tags", "capabilities"}, "node")
        node = Node(
            id=_text(_required(item, "id"), "node id", identifier=True),
            loopback=_address(_required(item, "loopback"), "loopback"),
            role=_text(item.get("role", "P"), "role", identifier=True),
            area=_optional_text(item.get("area"), "area"),
            tags=_tuple(item.get("tags", []), "tags"),
            capabilities=_tuple(item.get("capabilities", []), "capabilities"),
        )
        if node.id in nodes:
            raise InputError(f"Duplicate node id: {node.id}")
        nodes[node.id] = node
    links: list[Link] = []
    for raw in link_items:
        item = _mapping(raw, "link")
        forward = _link(item, nodes, metadata)
        reverse = item.get("reverse")
        if reverse is not None:
            if not forward.bidirectional:
                raise InputError("reverse attributes require bidirectional: true")
            reverse = _mapping(reverse, "reverse link")
            _keys(reverse, _LINK_KEYS - {"reverse", "from", "to", "bidirectional"}, "reverse link")
            inherited = {k: v for k, v in item.items() if k != "reverse"}
            inherited.update(id=f"{forward.id}:reverse", **{"from": forward.target, "to": forward.source},
                             from_ip=forward.to_ip, to_ip=forward.from_ip, bidirectional=False,
                             local_interface=forward.remote_interface, remote_interface=forward.local_interface)
            # Explicit overrides with different units replace the corresponding inherited field.
            for key in reverse:
                if key.endswith("_bps"):
                    inherited.pop(key[:-4] + "_mbps", None)
                elif key.endswith("_mbps"):
                    inherited.pop(key[:-5] + "_bps", None)
            inherited.update(reverse)
            reverse_link = _link(inherited, nodes, metadata)
            forward = Link(**{**forward.__dict__, "bidirectional": False, "base_id": forward.id})
            reverse_link = Link(**{**reverse_link.__dict__, "base_id": forward.id})
            links.extend((forward, reverse_link))
        else:
            links.append(forward)
    try:
        links = normalize_links(links)
    except ValueError as exc:
        raise InputError(str(exc)) from exc
    return Topology(name=_text(metadata.get("name", Path(path).stem), "topology name"),
                    nodes=nodes, links=links, raw_metadata=metadata)


_CONSTRAINT_KEYS = {
    "avoid_nodes", "avoid_links", "include_nodes", "ordered_waypoints", "max_hops", "max_metric",
    "max_candidates", "prefer_lower_metric", "prefer_higher_bottleneck", "search_budget",
}


def _load_demand(path: str | Path) -> ServiceDemand:
    data = _load_mapping(path)
    if "service" in data:
        _keys(data, {"service"}, "demand wrapper")
    service = _mapping(data.get("service", data), "service")
    _keys(service, {"name", "source", "destination", "bandwidth_mbps", "bandwidth_bps", "setup_priority",
                    "hold_priority", "description", "affinity", "output", "path", "constraints", "required_capabilities"}, "service")
    affinity_data = _mapping(service.get("affinity", {}), "affinity")
    output_data = _mapping(service.get("output", {}), "output")
    path_data = _mapping(service.get("path", {}), "path")
    constraints_data = _mapping(service.get("constraints", {}), "constraints")
    _keys(affinity_data, {"exclude_admin_groups", "include_any_admin_groups", "include_all_admin_groups"}, "affinity")
    _keys(path_data, _CONSTRAINT_KEYS | {"hop_type"}, "path")
    _keys(constraints_data, _CONSTRAINT_KEYS, "constraints")
    for key in set(path_data) & set(constraints_data):
        if path_data[key] != constraints_data[key]:
            raise InputError(f"Contradictory constraint {key} in path and constraints")
    merged = {**path_data, **constraints_data}
    affinity = Affinity(**{key: _tuple(affinity_data.get(key, []), key) for key in
                         ("exclude_admin_groups", "include_any_admin_groups", "include_all_admin_groups")})
    excluded = set(affinity.exclude_admin_groups)
    if excluded & set(affinity.include_all_admin_groups) or (affinity.include_any_admin_groups and
            set(affinity.include_any_admin_groups) <= excluded):
        raise InputError("Contradictory administrative affinity constraints")
    constraints = PathConstraints(
        avoid_nodes=_tuple(merged.get("avoid_nodes", []), "avoid_nodes"),
        avoid_links=_tuple(merged.get("avoid_links", []), "avoid_links"),
        include_nodes=_tuple(merged.get("include_nodes", []), "include_nodes"),
        ordered_waypoints=_tuple(merged.get("ordered_waypoints", []), "ordered_waypoints"),
        max_hops=_optional_int(merged.get("max_hops"), "max_hops", 1),
        max_metric=_optional_int(merged.get("max_metric"), "max_metric"),
        max_candidates=_integer(merged.get("max_candidates", 3), "max_candidates", 1),
        search_budget=_integer(merged.get("search_budget", 100_000), "search_budget", 1),
        prefer_lower_metric=_boolean(merged.get("prefer_lower_metric", True), "prefer_lower_metric"),
        prefer_higher_bottleneck=_boolean(merged.get("prefer_higher_bottleneck", True), "prefer_higher_bottleneck"),
    )
    if set(constraints.avoid_nodes) & (set(constraints.include_nodes) | set(constraints.ordered_waypoints)):
        raise InputError("A required node/waypoint cannot also be avoided")
    allowed_output = set(DemandOutput.__dataclass_fields__)
    _keys(output_data, allowed_output, "output")
    output_values: dict[str, Any] = {}
    for key, value in output_data.items():
        if key in {"dry_run", "write_plan", "write_verification"}:
            output_values[key] = _boolean(value, key)
        elif key == "tunnel_id":
            output_values[key] = _integer(value, key, 1, 65535)
        elif key == "formats":
            formats = _tuple(value, key)
            if not formats or set(formats) - {"generic", "iosxr", "iosxe", "junos"}:
                raise InputError("formats must contain supported generic, iosxr, iosxe or junos formats")
            output_values[key] = formats
        else:
            output_values[key] = _text(value, key, identifier=True)
    output = DemandOutput(**output_values)
    if not re.fullmatch(r"tunnel-te[0-9]+", output.iosxr_interface):
        raise InputError("iosxr_interface must be tunnel-te followed by a numeric identifier")
    amount = _bandwidth(service, "bandwidth", reservation=True)
    try:
        demand = ServiceDemand(
            name=_text(_required(service, "name"), "service name", identifier=True),
            source=_text(_required(service, "source"), "source", identifier=True),
            destination=_text(_required(service, "destination"), "destination", identifier=True),
            bandwidth_mbps=amount / 1_000_000, reservation_bw_bps=amount,
            setup_priority=_integer(service.get("setup_priority", 7), "setup_priority", 0, 7),
            hold_priority=_integer(service.get("hold_priority", 7), "hold_priority", 0, 7),
            description=_optional_text(service.get("description"), "description"),
            affinity=affinity, constraints=constraints,
            hop_type=_text(path_data.get("hop_type", "strict"), "hop_type"), output=output,
            required_capabilities=_tuple(service.get("required_capabilities", []), "required_capabilities"),
        )
    except ValueError as exc:
        raise InputError(f"Invalid demand: {exc}") from exc
    if demand.source == demand.destination:
        raise InputError("source and destination must be distinct")
    if {demand.source, demand.destination} & set(constraints.avoid_nodes):
        raise InputError("Source/destination cannot be in avoid_nodes")
    return demand


def load_demand(path: str | Path) -> ServiceDemand:
    try:
        return _load_demand(path)
    except InputError:
        raise
    except (TypeError, ValueError) as exc:
        raise InputError(f"Invalid demand: {exc}") from exc


def load_inventory(path: str | Path) -> Inventory:
    """Load canonical lists or the legacy GNS3 device mapping without coercion."""
    data = _load_mapping(path)
    _keys(data, {"devices"}, "inventory")
    raw = _required(data, "devices")
    if isinstance(raw, dict):
        items = []
        for name, item in raw.items():
            item = _mapping(item, f"Inventory device {name}")
            if "name" in item and item["name"] != name:
                raise InputError(f"Contradictory inventory name for {name}")
            items.append({**item, "name": name})
    elif isinstance(raw, list):
        items = raw
    else:
        raise InputError("Inventory devices must be a list or mapping/object")
    if not items:
        raise InputError("Inventory must contain at least one device")
    devices: dict[str, ManagedDevice] = {}
    mappings: dict[str, str] = {}
    for raw_item in items:
        item = _mapping(raw_item, "inventory device")
        _keys(item, {"name", "topology_node_id", "host", "management_ip", "device_type", "platform", "username",
                     "port", "ssh_port", "password_env", "role", "tags", "loopback", "notes"}, "inventory device")
        for canonical, legacy in (("host", "management_ip"), ("device_type", "platform"), ("port", "ssh_port")):
            if canonical in item and legacy in item and item[canonical] != item[legacy]:
                raise InputError(f"Contradictory inventory fields: {canonical}/{legacy}")
        name = _text(_required(item, "name"), "device name", identifier=True)
        node_id = _text(item.get("topology_node_id", name), "topology_node_id", identifier=True)
        if name in devices:
            raise InputError(f"Duplicate inventory device name: {name}")
        if node_id in mappings:
            raise InputError(f"Topology node {node_id} is mapped by both {mappings[node_id]} and {name}")
        host = _text(item.get("host", item.get("management_ip", "")), "host")
        try:
            ip_address(host)
        except ValueError:
            if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", host) or len(host) > 253:
                raise InputError(f"Invalid host: {host!r}") from None
        password_env = _optional_text(item.get("password_env"), "password_env", identifier=True)
        if password_env is not None and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", password_env):
            raise InputError("password_env must be an environment variable name")
        devices[name] = ManagedDevice(
            name=name, topology_node_id=node_id, host=host,
            device_type=_text(item.get("device_type", item.get("platform", "")), "device_type", identifier=True),
            username=_text(item.get("username", ""), "username", identifier=True),
            port=_integer(item.get("port", item.get("ssh_port", 22)), "port", 1, 65535),
            password_env=password_env, role=_optional_text(item.get("role"), "role"),
            tags=_tuple(item.get("tags", []), "tags"),
        )
        mappings[node_id] = name
    return Inventory(devices=devices)
