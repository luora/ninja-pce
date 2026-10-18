"""Typed, observation-only onboarding and deterministic configuration rendering.

The setup document contains network identities, never paths, command lines,
credentials, or PCEP delivery authority. Rendering is pure; the transaction
layer writes and validates the generated files before publishing a revision.
"""
from __future__ import annotations

import json
import math
from ipaddress import IPv4Address
from pathlib import Path
from typing import Any

from .bgpls import parse_ospf_multi_area_policy
from .models import validate_identifier

_MAX_AS = 2**32 - 1


def default_setup() -> dict[str, Any]:
    """Return a fresh example that requires deliberate operator review."""
    return {
        "version": 1,
        "operating_mode": "observe",
        "controller": {
            "name": "NINJA-PCE", "router_id": "192.0.2.10", "local_as": 64512,
            "source_address": "192.0.2.10", "bgp_port": -1,
        },
        "bgpls": {
            "peers": [{"address": "192.0.2.20", "asn": 64512}],
            "interval_seconds": 5, "timeout_seconds": 10, "max_age_seconds": 30,
        },
        "routing_domain": {
            "protocol_id": 3, "instance_id": 0, "autonomous_system": 64512, "bgpls_identifier": 0,
        },
        "nodes": [],
        "abr_bindings": [],
    }


def _object(value: Any, label: str, fields: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError(f"{label} has missing or unsupported fields")
    return value


def _integer(value: Any, label: str, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{label} must be an integer from {minimum} through {maximum}")
    return value


def _address(value: Any, label: str) -> str:
    if not isinstance(value, str) or len(value) > 15:
        raise ValueError(f"{label} must be a canonical unicast IPv4 address")
    try:
        address = IPv4Address(value)
    except ValueError as exc:
        raise ValueError(f"{label} must be a canonical unicast IPv4 address") from exc
    if str(address) != value or address.is_multicast or address.is_unspecified or int(address) == _MAX_AS:
        raise ValueError(f"{label} must be a canonical unicast IPv4 address")
    return str(address)


def _seconds(value: Any, label: str, maximum: int) -> float:
    try:
        valid = type(value) in (int, float) and math.isfinite(value) and 0 < value <= maximum
    except OverflowError:
        valid = False
    if not valid:
        raise ValueError(f"{label} must be finite and positive, at most {maximum} seconds")
    return float(value)


def _rows(value: Any, label: str, minimum: int, maximum: int) -> list[Any]:
    if not isinstance(value, list) or not minimum <= len(value) <= maximum:
        raise ValueError(f"{label} requires {minimum}..{maximum} rows")
    return value


def _area_document(domain: dict[str, Any], bindings: list[dict[str, Any]]) -> dict[str, Any]:
    return {"schema_version": 1, **domain, "bindings": bindings}


def validate_setup(document: Any) -> dict[str, Any]:
    """Return canonical fresh data, rejecting unsupported mode and authority."""
    setup = _object(document, "Setup", {
        "version", "operating_mode", "controller", "bgpls", "routing_domain", "nodes", "abr_bindings",
    })
    if type(setup["version"]) is not int or setup["version"] != 1:
        raise ValueError("Setup requires version 1")
    if setup["operating_mode"] != "observe":
        raise ValueError("Guided setup supports only observe mode")

    controller = _object(setup["controller"], "controller", {
        "name", "router_id", "local_as", "source_address", "bgp_port",
    })
    port = controller["bgp_port"]
    if type(port) is not int or port != -1 and not 1 <= port <= 65535:
        raise ValueError("controller.bgp_port must be -1 (disabled) or an integer from 1 through 65535")
    controller = {
        "name": validate_identifier(controller["name"], "controller.name"),
        "router_id": _address(controller["router_id"], "controller.router_id"),
        "local_as": _integer(controller["local_as"], "controller.local_as", 1, _MAX_AS),
        "source_address": _address(controller["source_address"], "controller.source_address"),
        "bgp_port": port,
    }

    bgpls = _object(setup["bgpls"], "bgpls", {
        "peers", "interval_seconds", "timeout_seconds", "max_age_seconds",
    })
    peers = []
    for entry in _rows(bgpls["peers"], "bgpls.peers", 1, 256):
        peer = _object(entry, "BGP-LS peer", {"address", "asn"})
        peers.append({"address": _address(peer["address"], "peer.address"),
                      "asn": _integer(peer["asn"], "peer.asn", 1, _MAX_AS)})
    if len({peer["address"] for peer in peers}) != len(peers):
        raise ValueError("bgpls.peers contains duplicate addresses")
    if any(peer["address"] == controller["source_address"] for peer in peers):
        raise ValueError("A BGP-LS peer cannot equal the controller source address")
    canonical_bgpls = {
        "peers": sorted(peers, key=lambda peer: IPv4Address(peer["address"])),
        **{key: _seconds(bgpls[key], f"bgpls.{key}", maximum)
           for key, maximum in (("interval_seconds", 3600), ("timeout_seconds", 30), ("max_age_seconds", 30))},
    }

    domain = _object(setup["routing_domain"], "routing_domain", {
        "protocol_id", "instance_id", "autonomous_system", "bgpls_identifier",
    })
    domain = {
        "protocol_id": _integer(domain["protocol_id"], "routing_domain.protocol_id", 3, 3),
        "instance_id": _integer(domain["instance_id"], "routing_domain.instance_id", 0, 2**64 - 1),
        "autonomous_system": _integer(domain["autonomous_system"], "routing_domain.autonomous_system", 1, _MAX_AS),
        "bgpls_identifier": _integer(domain["bgpls_identifier"], "routing_domain.bgpls_identifier", 0, _MAX_AS),
    }

    nodes = []
    for entry in _rows(setup["nodes"], "nodes", 0, 4096):
        node = _object(entry, "Node label", {"router_id", "name"})
        nodes.append({"router_id": _address(node["router_id"], "node.router_id"),
                      "name": validate_identifier(node["name"], "node.name")})
    for key in ("router_id", "name"):
        if len({node[key] for node in nodes}) != len(nodes):
            raise ValueError(f"nodes contains duplicate {key} values")
    nodes.sort(key=lambda node: IPv4Address(node["router_id"]))

    bindings = []
    for entry in _rows(setup["abr_bindings"], "abr_bindings", 0, 256):
        binding = _object(entry, "ABR binding", {"node_id", "te_router_id", "igp_router_id", "areas"})
        bindings.append({
            "node_id": validate_identifier(binding["node_id"], "ABR node_id"),
            "te_router_id": _address(binding["te_router_id"], "ABR te_router_id"),
            "igp_router_id": _address(binding["igp_router_id"], "ABR igp_router_id"),
            "areas": list(_rows(binding["areas"], "ABR areas", 2, 256)),
        })
    if bindings:
        policy = parse_ospf_multi_area_policy(_area_document(domain, bindings))
        bindings = policy.document()["bindings"]
        names = {node["router_id"]: node["name"] for node in nodes}
        for binding in bindings:
            if names.get(binding["te_router_id"]) != binding["node_id"]:
                raise ValueError("Every ABR binding must match an explicit node router_id and name")
    return {
        "version": 1, "operating_mode": "observe", "controller": controller,
        "bgpls": canonical_bgpls, "routing_domain": domain, "nodes": nodes, "abr_bindings": bindings,
    }


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n").encode("utf-8")


def _gobgp_toml(setup: dict[str, Any]) -> bytes:
    controller = setup["controller"]
    quote = json.dumps
    lines = [
        "# Generated observation-only BGP-LS collector configuration.",
        "# The managed launcher binds the gRPC API to 127.0.0.1:50051.",
        "[global.config]", f"  as = {controller['local_as']}",
        f"  router-id = {quote(controller['router_id'])}", f"  port = {controller['bgp_port']}",
        f"  local-address-list = [{quote(controller['source_address'])}]", "",
        "[global.apply-policy.config]", '  default-import-policy = "accept-route"',
        '  default-export-policy = "reject-route"',
    ]
    for peer in setup["bgpls"]["peers"]:
        lines.extend([
            "", "[[neighbors]]", "  [neighbors.config]",
            f"    neighbor-address = {quote(peer['address'])}", f"    peer-as = {peer['asn']}",
            '    description = "Explicit observation BGP-LS source"',
            "  [neighbors.transport.config]", f"    local-address = {quote(controller['source_address'])}",
            "  [neighbors.timers.config]", "    hold-time = 30", "    keepalive-interval = 10",
            "  [neighbors.graceful-restart.config]", "    enabled = true", "    helper-only = true",
            "  [[neighbors.afi-safis]]", "    [neighbors.afi-safis.config]", '      afi-safi-name = "ls"',
            "    [neighbors.afi-safis.mp-graceful-restart.config]", "      enabled = true",
            "  [neighbors.apply-policy.config]", '    default-import-policy = "accept-route"',
            '    default-export-policy = "reject-route"',
        ])
    return ("\n".join(lines) + "\n").encode("utf-8")


def render_setup(document: Any, workspace: Path, revision_dir: Path, *, grpcurl: str = "grpcurl"
                 ) -> dict[str, bytes]:
    """Render files using only trusted local paths supplied by the application."""
    setup = validate_setup(document)
    if not isinstance(grpcurl, str) or not grpcurl or grpcurl.startswith("-") or len(grpcurl) > 4096 or any(
        ord(char) < 32 for char in grpcurl
    ):
        raise ValueError("Trusted grpcurl must name a bounded executable")
    workspace = Path(workspace).resolve()
    revision_dir = Path(revision_dir).resolve()
    live = workspace / "data/live"
    bgpls = setup["bgpls"]
    controller: dict[str, Any] = {
        "version": 1, "operating_mode": "observe",
        "files": {"bgpls_raw": str(live / "bgpls-raw.json"), "bgpls_status": str(live / "bgpls-status.json"),
                  "topology": str(live / "topology.json"), "pcep_capture": str(live / "pcep.json")},
        "state_dir": str(workspace / "data/state"), "runtime_dir": str(workspace / "data/run"),
        "bgpls": {
            "endpoint": "127.0.0.1:50051", "grpcurl": grpcurl,
            "required_peers": [peer["address"] for peer in bgpls["peers"]],
            "node_map": str(revision_dir / "node-map.json"),
            **{key: bgpls[key] for key in ("interval_seconds", "timeout_seconds", "max_age_seconds")},
        },
        "dashboard": {"enabled": False},
    }
    output = {
        "setup.json": _json_bytes(setup), "gobgpd.toml": _gobgp_toml(setup),
        "node-map.json": _json_bytes({node["router_id"]: node["name"] for node in setup["nodes"]}),
    }
    if setup["abr_bindings"]:
        output["ospf-multi-area-policy.json"] = _json_bytes(_area_document(
            setup["routing_domain"], setup["abr_bindings"],
        ))
        controller["bgpls"]["ospf_multi_area_policy"] = str(revision_dir / "ospf-multi-area-policy.json")
    output["controller.json"] = _json_bytes(controller)
    return output
