"""Validate and render intended lab extensions without contacting any device.

This manifest is configuration intent, not a discovered or authoritative TED.
Baseline links participate in validation but are never rendered. Platform profiles
are explicit; adding another release requires a separate syntax qualification.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from ipaddress import IPv4Address, IPv4Interface, IPv4Network
from pathlib import Path
from typing import Any, Mapping

from .bgpls import BgplsError, parse_ospf_multi_area_policy
from .loaders import InputError, _load_mapping

PLATFORM_PROFILE = "iosxe-16.9.7"


@dataclass(frozen=True)
class LabNode:
    id: str
    hostname: str
    loopback: IPv4Address
    igp_router_id: IPv4Address
    loopback_area: str
    ospf_process: int
    configure_loopback: bool
    loopback_interface: str
    reserved_interfaces: tuple[str, ...]


@dataclass(frozen=True)
class LabEndpoint:
    node: str
    interface: str
    address: IPv4Interface
    metric: int
    te_metric: int
    capacity_kbps: int
    reservable_kbps: int
    max_reservation_kbps: int


@dataclass(frozen=True)
class LabLink:
    id: str
    area: str
    configure: bool
    endpoints: tuple[LabEndpoint, LabEndpoint]


@dataclass(frozen=True)
class LabProfile:
    name: str
    platform_profile: str
    nodes: tuple[LabNode, ...]
    links: tuple[LabLink, ...]

    def node_areas(self, node_id: str) -> tuple[str, ...]:
        node = next((item for item in self.nodes if item.id == node_id), None)
        if node is None:
            raise InputError(f"Unknown lab node {node_id}")
        areas = {node.loopback_area}
        areas.update(link.area for link in self.links if any(e.node == node_id for e in link.endpoints))
        return tuple(sorted(areas, key=lambda area: int(IPv4Address(area))))


def _mapping(value: Any, label: str, allowed: set[str]) -> Mapping[str, Any]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise InputError(f"{label} must be an object with string keys")
    unknown = set(value) - allowed
    if unknown:
        raise InputError(f"Unknown {label} fields: {sorted(unknown)}")
    return value


def _list(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise InputError(f"{label} must be a list")
    return value


def _integer(value: Any, label: str, maximum: int, minimum: int = 1) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise InputError(f"{label} must be an integer from {minimum} to {maximum}")
    return value


def _boolean(value: Any, label: str) -> bool:
    if type(value) is not bool:
        raise InputError(f"{label} must be true or false")
    return value


def _identifier(value: Any, label: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}", value):
        raise InputError(f"{label} must be a safe identifier of at most 64 characters")
    return value


def _area(value: Any, label: str) -> str:
    try:
        if type(value) is int:
            return str(IPv4Address(value))
        if isinstance(value, str):
            return str(IPv4Address(int(value) if value.isdecimal() else value))
    except ValueError:
        pass
    raise InputError(f"{label} must be an OSPF area ID from 0 to 4294967295 or dotted IPv4")


def _interface(value: Any, label: str, *, loopback: bool = False) -> str:
    pattern = r"(?:Loopback|Lo)(\d+)" if loopback else r"(?:GigabitEthernet|Gi)(\d+(?:/\d+)*)"
    match = re.fullmatch(pattern, value, re.IGNORECASE) if isinstance(value, str) else None
    if match is None:
        kind = "Loopback" if loopback else "GigabitEthernet"
        raise InputError(f"{label} must be a {kind} interface supported by {PLATFORM_PROFILE}")
    numbers = [int(part) for part in match.group(1).split("/")]
    if any(number > 2**31 - 1 for number in numbers):
        raise InputError(f"{label} interface index is too large")
    return ("Loopback" if loopback else "GigabitEthernet") + "/".join(map(str, numbers))


def _unicast(address: IPv4Address, label: str) -> None:
    if int(address) < 2**24 or int(address) >= 0xE0000000 or address.is_loopback:
        raise InputError(f"{label} must be a usable unicast IPv4 address")


def _loopback(value: Any, label: str) -> IPv4Address:
    if not isinstance(value, str):
        raise InputError(f"{label} must be an IPv4 address or /32")
    try:
        interface = IPv4Interface(value)
    except ValueError as exc:
        raise InputError(f"Invalid {label}: {value}") from exc
    if interface.network.prefixlen != 32:
        raise InputError(f"{label} must use a /32 prefix")
    _unicast(interface.ip, label)
    return interface.ip


def _node(raw: Any) -> LabNode:
    item = _mapping(raw, "node", {
        "id", "hostname", "loopback", "loopback_area", "ospf_process", "configure_loopback",
        "loopback_interface", "reserved_interfaces", "igp_router_id",
    })
    node_id = _identifier(item.get("id"), "node.id")
    hostname = item.get("hostname", node_id)
    if not isinstance(hostname, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9-]{0,62}", hostname):
        raise InputError(f"Node {node_id}: invalid IOS XE hostname")
    reserved = tuple(_interface(value, f"{node_id}.reserved_interfaces")
                     for value in _list(item.get("reserved_interfaces", []), "reserved_interfaces"))
    if len(set(reserved)) != len(reserved):
        raise InputError(f"Node {node_id}: duplicate reserved interface")
    loopback = _loopback(item.get("loopback"), f"{node_id}.loopback")
    return LabNode(
        node_id, hostname, loopback,
        _loopback(item.get("igp_router_id", str(loopback)), f"{node_id}.igp_router_id"),
        _area(item.get("loopback_area"), f"{node_id}.loopback_area"),
        _integer(item.get("ospf_process", 1), f"{node_id}.ospf_process", 65535),
        _boolean(item.get("configure_loopback", False), f"{node_id}.configure_loopback"),
        _interface(item.get("loopback_interface", "Loopback0"), f"{node_id}.loopback_interface", loopback=True),
        reserved,
    )


_CAPACITY_FIELDS = {"metric", "te_metric", "capacity_kbps", "reservable_kbps", "max_reservation_kbps"}


def _endpoint(raw: Any, defaults: Mapping[str, Any], link_id: str) -> LabEndpoint:
    item = _mapping(raw, "endpoint", {"node", "interface", "address"} | _CAPACITY_FIELDS)
    node = _identifier(item.get("node"), f"{link_id}.endpoint.node")
    label = f"{link_id}.{node}"
    try:
        address = IPv4Interface(item.get("address"))
    except (ValueError, TypeError) as exc:
        raise InputError(f"{label}.address must be a numbered IPv4 interface prefix") from exc
    if not isinstance(item.get("address"), str) or "/" not in item["address"]:
        raise InputError(f"{label}.address requires an explicit prefix length")
    _unicast(address.ip, f"{label}.address")
    if address.network.prefixlen == 32 or (address.network.prefixlen < 31 and address.ip in (
        address.network.network_address, address.network.broadcast_address,
    )):
        raise InputError(f"{label}.address cannot be a /32, network or broadcast address")
    merged = dict(defaults) | dict(item)
    metric = _integer(merged.get("metric"), f"{label}.metric", 65535)
    te_metric = _integer(merged.get("te_metric", metric), f"{label}.te_metric", 2**32 - 1)
    capacity = _integer(merged.get("capacity_kbps"), f"{label}.capacity_kbps", 2**31 - 1)
    reservable = _integer(merged.get("reservable_kbps"), f"{label}.reservable_kbps", capacity)
    maximum = _integer(merged.get("max_reservation_kbps", reservable), f"{label}.max_reservation_kbps", reservable)
    return LabEndpoint(node, _interface(item.get("interface"), f"{label}.interface"), address,
                       metric, te_metric, capacity, reservable, maximum)


def parse_lab_profile(data: Mapping[str, Any]) -> LabProfile:
    """Load strict manifest intent, including all baseline collision constraints."""
    manifest = _mapping(data, "lab profile", {"schema_version", "name", "platform_profile", "nodes", "links"})
    if type(manifest.get("schema_version")) is not int or manifest["schema_version"] != 1:
        raise InputError("lab profile schema_version must be 1")
    if manifest.get("platform_profile") != PLATFORM_PROFILE:
        raise InputError(f"Only explicit platform_profile {PLATFORM_PROFILE!r} is supported")
    nodes = tuple(_node(item) for item in _list(manifest.get("nodes"), "nodes"))
    if not nodes:
        raise InputError("Lab profile needs at least one node")
    for key in ("id", "hostname", "loopback", "igp_router_id"):
        values = [str(getattr(node, key)).casefold() for node in nodes]
        if len(set(values)) != len(values):
            raise InputError(f"Duplicate node {key}")
    known_nodes = {node.id: node for node in nodes}
    used_interfaces = {(node.id, interface) for node in nodes for interface in node.reserved_interfaces}
    used_addresses = {node.loopback for node in nodes}
    networks: list[IPv4Network] = []
    links: list[LabLink] = []
    link_ids: set[str] = set()
    for raw in _list(manifest.get("links"), "links"):
        item = _mapping(raw, "link", {"id", "area", "configure", "endpoints"} | _CAPACITY_FIELDS)
        link_id = _identifier(item.get("id"), "link.id")
        if link_id in link_ids:
            raise InputError(f"Duplicate link id {link_id}")
        link_ids.add(link_id)
        raw_endpoints = _list(item.get("endpoints"), f"{link_id}.endpoints")
        if len(raw_endpoints) != 2:
            raise InputError(f"{link_id} must have exactly two endpoints")
        endpoint_a, endpoint_b = (_endpoint(value, item, link_id) for value in raw_endpoints)
        if endpoint_a.node == endpoint_b.node:
            raise InputError(f"{link_id} cannot connect a node to itself")
        if endpoint_a.address.network != endpoint_b.address.network:
            raise InputError(f"{link_id} endpoints must share the same subnet and prefix")
        network = endpoint_a.address.network
        if any(network.overlaps(previous) for previous in networks):
            raise InputError(f"{link_id} subnet overlaps another link subnet")
        if any(node.loopback in network for node in nodes):
            raise InputError(f"{link_id} subnet overlaps a node loopback")
        networks.append(network)
        for endpoint in (endpoint_a, endpoint_b):
            if endpoint.node not in known_nodes:
                raise InputError(f"{link_id} references unknown node {endpoint.node}")
            interface_key = (endpoint.node, endpoint.interface)
            if interface_key in used_interfaces:
                raise InputError(f"Interface conflict at {endpoint.node} {endpoint.interface}")
            used_interfaces.add(interface_key)
            if endpoint.address.ip in used_addresses:
                raise InputError(f"Duplicate interface address {endpoint.address.ip}")
            used_addresses.add(endpoint.address.ip)
        links.append(LabLink(link_id, _area(item.get("area"), f"{link_id}.area"),
                             _boolean(item.get("configure"), f"{link_id}.configure"),
                             (endpoint_a, endpoint_b)))
    profile = LabProfile(_identifier(manifest.get("name"), "name"), PLATFORM_PROFILE,
                         tuple(sorted(nodes, key=lambda node: node.id)), tuple(sorted(links, key=lambda link: link.id)))
    for node in profile.nodes:
        areas = profile.node_areas(node.id)
        if len(areas) > 1 and "0.0.0.0" not in areas:
            raise InputError(f"Node {node.id} spans multiple areas without a backbone area; virtual links are unsupported")
        if node.configure_loopback and any(not link.configure and any(e.node == node.id for e in link.endpoints)
                                           for link in profile.links):
            raise InputError(f"Node {node.id} has baseline links; configure_loopback is restricted to new nodes")
        if node.configure_loopback and not any(link.configure and any(e.node == node.id for e in link.endpoints)
                                               for link in profile.links):
            raise InputError(f"New node {node.id} needs a configured link")
    return profile


def load_lab_profile(path: str | Path) -> LabProfile:
    return parse_lab_profile(_load_mapping(path))


def render_lab_configs(profile: LabProfile) -> dict[str, str]:
    """Render additive configuration intent only; existing link stanzas are omitted."""
    rendered: dict[str, str] = {}
    for node in profile.nodes:
        changes = [(link, endpoint, next(e for e in link.endpoints if e.node != node.id))
                   for link in profile.links if link.configure
                   for endpoint in link.endpoints if endpoint.node == node.id]
        if not changes and not node.configure_loopback:
            continue
        lines = [f"! ninja-pce lab extension: {profile.name}",
                 f"! Platform syntax profile: {profile.platform_profile}; live qualification is separate.",
                 "! Dry-run configuration intent. Review existing device state before applying.", "!"]
        if node.configure_loopback:
            lines += [f"hostname {node.hostname}", "mpls label protocol ldp", "!"]
        lines += ["mpls traffic-eng tunnels", "!"]
        if node.configure_loopback:
            lines += [f"interface {node.loopback_interface}", f" description ROUTER_ID_{node.id}",
                      f" ip address {node.loopback} 255.255.255.255",
                      f" ip ospf {node.ospf_process} area {node.loopback_area}", "!"]
            lines += [f"mpls ldp router-id {node.loopback_interface}", "!"]
        for link, endpoint, remote in changes:
            lines += [f"interface {endpoint.interface}",
                      f" description TO_{remote.node}_{remote.interface}_AREA_{link.area}",
                      f" bandwidth {endpoint.capacity_kbps}",
                      f" ip address {endpoint.address.ip} {endpoint.address.netmask}",
                      " ip ospf network point-to-point", f" ip ospf {node.ospf_process} area {link.area}",
                      f" ip ospf cost {endpoint.metric}", " mpls ip", " mpls traffic-eng tunnels",
                      f" mpls traffic-eng administrative-weight {endpoint.te_metric}",
                      f" ip rsvp bandwidth {endpoint.reservable_kbps} {endpoint.max_reservation_kbps}",
                      " no shutdown", "!"]
        lines += [f"router ospf {node.ospf_process}"]
        if node.configure_loopback:
            lines += [f" router-id {node.igp_router_id}", f" passive-interface {node.loopback_interface}",
                      f" mpls traffic-eng router-id {node.loopback_interface}"]
        # Existing routers may use passive-interface default. Enable adjacency
        # only on interfaces explicitly selected for this extension.
        lines.extend(f" no passive-interface {endpoint.interface}" for _, endpoint, _ in changes)
        lines.extend(f" mpls traffic-eng area {area}" for area in profile.node_areas(node.id))
        lines += ["!", "! Collect evidence after applying; these commands are not configuration:",
                  "! show ip ospf neighbor", "! show ip ospf database opaque-area",
                  "! show mpls traffic-eng topology", "! show ip rsvp interface", "!"]
        rendered[node.id] = "\n".join(lines) + "\n"
    return rendered


def lab_profile_summary(profile: LabProfile) -> dict[str, Any]:
    """Describe intent for review without claiming observed topology or capability."""
    configured_nodes = sorted(render_lab_configs(profile))
    return {
        "schema_version": 1, "name": profile.name, "platform_profile": profile.platform_profile,
        "dry_run": True, "observation_status": "unobserved", "authoritative": False,
        "configured_nodes": configured_nodes,
        "new_loopbacks": [node.id for node in profile.nodes if node.configure_loopback],
        "configured_links": [link.id for link in profile.links if link.configure],
        "baseline_links": [link.id for link in profile.links if not link.configure],
        "node_areas": {node.id: list(profile.node_areas(node.id)) for node in profile.nodes},
        "node_router_ids": {node.id: {"te_router_id": str(node.loopback), "igp_router_id": str(node.igp_router_id)}
                            for node in profile.nodes},
        "abrs": [node.id for node in profile.nodes if len(profile.node_areas(node.id)) > 1],
    }


def lab_ospf_multi_area_policy(
    profile: LabProfile,
    *,
    autonomous_system: int,
    instance_id: int,
    bgpls_identifier: int,
) -> dict[str, Any]:
    """Bind manifest ABRs to an operator-supplied, independently observed domain.

    Membership derives from declared link and loopback areas. This function
    cannot establish that the exporter actually advertises those identities;
    compare router IDs and domain fields with fresh device/BGP-LS evidence.
    """
    document = {
        "schema_version": 1, "protocol_id": 3, "instance_id": instance_id,
        "autonomous_system": autonomous_system, "bgpls_identifier": bgpls_identifier,
        "bindings": [
            {"node_id": node.id, "te_router_id": str(node.loopback), "igp_router_id": str(node.igp_router_id),
             "areas": list(profile.node_areas(node.id))}
            for node in profile.nodes if len(profile.node_areas(node.id)) > 1
        ],
    }
    try:
        return parse_ospf_multi_area_policy(document).document()
    except BgplsError as exc:
        raise InputError(f"Cannot generate OSPF multi-area policy: {exc}") from exc
