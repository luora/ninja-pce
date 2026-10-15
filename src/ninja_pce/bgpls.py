"""Read-only GoBGP RIB ingestion. No BGP speaker or router configuration lives here.

The supported transport is GoBGP v3.37.0 ListPeer/ListPath through grpcurl.
Binary API fields retain RFC 9552 identity and optional TLV presence, unlike
GoBGP's display-oriented CLI JSON and scalar protobuf TE attributes.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import math
import struct
import subprocess
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from ipaddress import IPv4Address, ip_address
from pathlib import Path
from typing import Any, Callable, Mapping

from .models import Link, Node, Topology, validate_identifier

_MAX_OUTPUT = 32 * 1024 * 1024
_AF = {"afi": "AFI_LS", "safi": "SAFI_LS"}


class BgplsError(ValueError):
    """A poll cannot establish a usable, current topology snapshot."""


@dataclass(frozen=True)
class OspfAbrBinding:
    """Trusted physical-router identity, independently supplied by an operator."""

    node_id: str
    te_router_id: str
    igp_router_id: str
    areas: tuple[str, ...]


@dataclass(frozen=True)
class OspfMultiAreaPolicy:
    """An explicit, single-domain OSPFv2 area-identity projection policy.

    Matching loopbacks alone never establish router equivalence. Each allowed
    ABR must also match the complete trusted domain, IGP router ID, and area
    membership. This policy grants graph equivalence, never delivery authority.
    Use :func:`parse_ospf_multi_area_policy` to validate external configuration.
    """

    protocol_id: int
    instance_id: int
    autonomous_system: int
    bgpls_identifier: int
    bindings: tuple[OspfAbrBinding, ...]

    def document(self) -> dict[str, Any]:
        return {
            "schema_version": 1, "protocol_id": self.protocol_id,
            "instance_id": self.instance_id, "autonomous_system": self.autonomous_system,
            "bgpls_identifier": self.bgpls_identifier,
            "bindings": [{"node_id": binding.node_id, "te_router_id": binding.te_router_id,
                          "igp_router_id": binding.igp_router_id, "areas": list(binding.areas)}
                         for binding in self.bindings],
        }

    @property
    def digest(self) -> str:
        return hashlib.sha256(json.dumps(self.document(), sort_keys=True,
                                         separators=(",", ":")).encode()).hexdigest()


def parse_ospf_multi_area_policy(value: Any) -> OspfMultiAreaPolicy:
    """Validate opt-in identity bindings without inferring names or memberships."""
    data = _object(value, "OSPF multi-area policy")
    fields = {"schema_version", "protocol_id", "instance_id", "autonomous_system",
              "bgpls_identifier", "bindings"}
    if set(data) != fields or type(data["schema_version"]) is not int or data["schema_version"] != 1:
        raise BgplsError("OSPF multi-area policy requires exactly schema version 1 fields")
    for name, maximum in (("protocol_id", 255), ("instance_id", 2**64 - 1),
                          ("autonomous_system", 2**32 - 1), ("bgpls_identifier", 2**32 - 1)):
        if type(data[name]) is not int or not 0 <= data[name] <= maximum:
            raise BgplsError(f"OSPF multi-area policy {name} must be an unsigned integer")
    if data["protocol_id"] != 3:
        raise BgplsError("OSPF multi-area projection supports only protocol ID 3 (OSPFv2)")
    entries = _list(data["bindings"], "OSPF ABR bindings")
    if not entries or len(entries) > 256:
        raise BgplsError("OSPF multi-area policy requires between 1 and 256 explicit ABR bindings")
    bindings = []
    for entry in entries:
        item = _object(entry, "OSPF ABR binding")
        if set(item) != {"node_id", "te_router_id", "igp_router_id", "areas"}:
            raise BgplsError("OSPF ABR binding has missing or unknown fields")
        try:
            node_id = validate_identifier(item["node_id"], "OSPF ABR node id")
            te_router_id = _policy_ipv4(item["te_router_id"], "TE router ID")
            igp_router_id = _policy_ipv4(item["igp_router_id"], "IGP router ID")
            areas = tuple(_policy_ipv4(area, "OSPF area")
                          for area in _list(item["areas"], "OSPF ABR areas"))
        except ValueError as exc:
            raise BgplsError(str(exc)) from exc
        if not 2 <= len(areas) <= 256 or len(set(areas)) != len(areas):
            raise BgplsError("OSPF ABR areas must contain between 2 and 256 unique area IDs")
        bindings.append(OspfAbrBinding(node_id, te_router_id, igp_router_id,
                                       tuple(sorted(areas, key=IPv4Address))))
    for label in ("node_id", "te_router_id", "igp_router_id"):
        if len({getattr(binding, label) for binding in bindings}) != len(bindings):
            raise BgplsError(f"OSPF ABR bindings contain overlapping {label} identities")
    return OspfMultiAreaPolicy(data["protocol_id"], data["instance_id"], data["autonomous_system"],
                               data["bgpls_identifier"], tuple(sorted(bindings, key=lambda b: b.node_id)))


def _policy_ipv4(value: Any, label: str) -> str:
    if not isinstance(value, str) or "/" in value or "%" in value:
        raise BgplsError(f"{label} must be an IPv4 address string")
    try:
        return str(IPv4Address(value))
    except ValueError as exc:
        raise BgplsError(f"{label} must be an IPv4 address string") from exc


def _object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise BgplsError(f"{label} must be an object")
    return value


def _list(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise BgplsError(f"{label} must be a list")
    return value


def _time(value: Any, label: str) -> datetime:
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.tzinfo is None:
            raise ValueError("timezone required")
        return result.astimezone(timezone.utc)
    except (AttributeError, TypeError, ValueError) as exc:
        raise BgplsError(f"Invalid {label}") from exc


def _duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise BgplsError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def decode_json_stream(value: str) -> list[dict[str, Any]]:
    """Decode grpcurl's sequence of JSON response objects, rejecting truncation."""
    if len(value.encode()) > _MAX_OUTPUT:
        raise BgplsError("GoBGP output exceeds the size limit")
    decoder = json.JSONDecoder(object_pairs_hook=_duplicates)
    result = []
    offset = 0
    while offset < len(value):
        while offset < len(value) and value[offset].isspace():
            offset += 1
        if offset == len(value):
            break
        try:
            item, offset = decoder.raw_decode(value, offset)
        except ValueError as exc:
            raise BgplsError("Invalid/truncated GoBGP JSON stream") from exc
        result.append(_object(item, "RPC response"))
    return result


def _binary(value: Any) -> bytes:
    try:
        if not isinstance(value, str):
            raise ValueError("base64 string required")
        return base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise BgplsError("Invalid binary GoBGP field") from exc


def _tlvs(data: bytes) -> dict[int, bytes]:
    result = {}
    while data:
        if len(data) < 4:
            raise BgplsError("Truncated BGP-LS TLV header")
        kind, size = struct.unpack("!HH", data[:4])
        if len(data) < 4 + size or kind in result:
            raise BgplsError("Truncated or duplicate BGP-LS TLV")
        result[kind] = data[4:4 + size]
        data = data[4 + size:]
    return result


def _required(values: dict[int, bytes], kind: int, size: int | None = None) -> bytes:
    if kind not in values:
        raise BgplsError(f"Missing TLV {kind}")
    value = values[kind]
    if size is not None and len(value) != size:
        raise BgplsError(f"Incorrect length for TLV {kind}")
    return value


def _address(values: dict[int, bytes], kind: int) -> str:
    return str(ip_address(_required(values, kind, 4)))


def _bandwidth(data: bytes) -> int:
    if len(data) != 4:
        raise BgplsError("Incorrect bandwidth TLV length")
    value = struct.unpack("!f", data)[0]
    if not math.isfinite(value) or value < 0:
        raise BgplsError("Invalid BGP-LS bandwidth")
    # IEEE float32 bytes/s -> integer bits/s, conservatively floor capacity.
    return math.floor(value * 8)


def _canonical(values: dict[int, bytes]) -> bytes:
    return b"".join(struct.pack("!HH", kind, len(value)) + value
                    for kind, value in sorted(values.items()))


def _node_key(protocol: int, instance: bytes, value: bytes) -> str:
    descriptors = _tlvs(value)
    _required(descriptors, 512, 4)
    router = _required(descriptors, 515)
    if protocol in {1, 2} and len(router) != 6:
        raise BgplsError("IS-IS pseudonodes and unsupported router identifiers are excluded")
    if protocol == 3 and len(router) != 4:
        raise BgplsError("OSPF pseudonodes and unsupported router identifiers are excluded")
    if protocol == 3:
        _required(descriptors, 514, 4)
    for kind in (513, 514):
        if kind in descriptors and len(descriptors[kind]) != 4:
            raise BgplsError("Incorrect node descriptor length")
    key = bytes([protocol]) + instance + _canonical(descriptors)
    return "bgpls-node-" + hashlib.sha256(key).hexdigest()[:32]


def _ls_attributes(path: dict[str, Any]) -> dict[int, bytes]:
    found = []
    for encoded in _list(path.get("pattrsBinary"), "binary attributes"):
        value = _binary(encoded)
        if len(value) < 3:
            raise BgplsError("Truncated BGP path attribute")
        extended = bool(value[0] & 0x10)
        header = 4 if extended else 3
        if len(value) < header:
            raise BgplsError("Truncated BGP attribute length")
        size = int.from_bytes(value[2:header], "big")
        if len(value) != header + size:
            raise BgplsError("Incorrect BGP attribute length")
        if value[1] == 29:
            found.append(_tlvs(value[header:]))
    if len(found) != 1:
        raise BgplsError("Exactly one BGP-LS attribute is required")
    return found[0]


def _peers(responses: Any, required: tuple[str, ...]) -> dict[str, tuple[Any, ...]]:
    result = {}
    for response in _list(responses, "peer responses"):
        peer = _object(_object(response, "peer response").get("peer"), "peer")
        state = _object(peer.get("state"), "peer state")
        address = state.get("neighborAddress")
        families = _list(peer.get("afiSafis", []), "peer families")
        ls = [family for family in families if _object(family, "family").get("state", {}).get("family") == _AF]
        if address not in required:
            continue
        if address in result or state.get("sessionState") != "ESTABLISHED" or state.get("adminState") != "UP":
            raise BgplsError("Required BGP-LS peer is duplicate or not Established/Up")
        if len(ls) != 1 or ls[0].get("state", {}).get("enabled") is not True:
            raise BgplsError("Required BGP-LS address family is not enabled")
        if ls[0].get("mpGracefulRestart", {}).get("state", {}).get("endOfRibReceived") is not True:
            raise BgplsError("BGP-LS End-of-RIB has not been received")
        restart = _object(peer.get("gracefulRestart", {}), "graceful restart")
        if restart.get("peerRestarting") or restart.get("localRestarting"):
            raise BgplsError("BGP-LS restart/stale state prohibits ingestion")
        uptime = peer.get("timers", {}).get("state", {}).get("uptime")
        _time(uptime, "peer uptime")
        received = state.get("messages", {}).get("received", {})
        result[address] = (uptime, state.get("flops", 0), received.get("update", "0"),
                           received.get("withdrawUpdate", "0"), received.get("withdrawPrefix", "0"),
                           ls[0].get("state", {}).get("accepted", "0"))
    if set(result) != set(required):
        raise BgplsError("A required BGP-LS peer is missing")
    return result


def parse_gobgp_snapshot(
    capture: Mapping[str, Any], *, node_names: Mapping[str, str] | None = None,
    required_peers: tuple[str, ...], now: datetime | None = None, max_age_seconds: float = 30,
    ospf_multi_area: OspfMultiAreaPolicy | None = None,
) -> Topology:
    """Replace the entire topology from one bounded, quiescent GoBGP RIB poll.

    Unknown/incomplete TE objects are preserved in metadata, excluded from the
    computable graph. RFC 9552 does not prove physical admin/oper state; this
    adapter therefore never grants authoritative execution status.
    """
    if not required_peers or len(set(required_peers)) != len(required_peers):
        raise BgplsError("Supply unique trusted required_peers")
    if isinstance(max_age_seconds, bool) or not math.isfinite(max_age_seconds) or max_age_seconds <= 0:
        raise BgplsError("max_age_seconds must be finite and positive")
    if ospf_multi_area is not None:
        if not isinstance(ospf_multi_area, OspfMultiAreaPolicy):
            raise BgplsError("OSPF multi-area projection requires a validated explicit policy")
        # Frozen dataclasses can still be instantiated directly; do not let a
        # caller bypass the external policy parser's strict identity checks.
        ospf_multi_area = parse_ospf_multi_area_policy(ospf_multi_area.document())
    bindings = {binding.te_router_id: binding for binding in ospf_multi_area.bindings} if ospf_multi_area else {}
    if capture.get("schema_version") != 1 or capture.get("source") != "gobgp-v3.37.0-grpc":
        raise BgplsError("Unsupported GoBGP capture schema/source")
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise BgplsError("Current time must include timezone")
    observed = _time(capture.get("captured_at"), "capture timestamp")
    started = _time(capture.get("poll_started_at"), "poll start")
    if not 0 <= (current - observed).total_seconds() <= max_age_seconds:
        raise BgplsError("GoBGP capture is expired or from the future")
    if not 0 <= (observed - started).total_seconds() <= max_age_seconds:
        raise BgplsError("GoBGP poll duration exceeds freshness budget")
    before = _peers(capture.get("peers_before"), required_peers)
    after = _peers(capture.get("peers_after"), required_peers)
    if before != after:
        raise BgplsError("BGP-LS session/RIB changed during poll; retry a full poll")
    records: dict[str, tuple[int, int, bytes, dict[int, bytes], dict[int, bytes]]] = {}
    diagnostics: list[dict[str, Any]] = []
    for response in _list(capture.get("rib"), "RIB responses"):
        destination = _object(_object(response, "RIB response").get("destination"), "destination")
        paths = _list(destination.get("paths"), "paths")
        selected = [path for path in paths if _object(path, "path").get("best") is True]
        if len(selected) != 1:
            raise BgplsError("Each destination must have exactly one best path")
        path = selected[0]
        if path.get("family") != _AF or path.get("neighborIp") not in before:
            raise BgplsError("RIB path is not LS from a trusted active peer")
        for flag in ("stale", "isWithdraw", "filtered", "isNexthopInvalid"):
            if path.get(flag, False) is not False:
                raise BgplsError(f"Unsafe BGP-LS path flag: {flag}")
        raw = _binary(path.get("nlriBinary"))
        if len(raw) < 13:
            raise BgplsError("Truncated BGP-LS NLRI")
        kind, size = struct.unpack("!HH", raw[:4])
        if len(raw) != size + 4:
            raise BgplsError("Incorrect BGP-LS NLRI length")
        protocol, instance = raw[4], raw[5:13]
        if kind not in {1, 2}:
            diagnostics.append({"nlri_type": kind, "reason": "prefix/extension NLRI is not a TE graph edge"})
            continue
        if protocol not in {1, 2, 3}:
            diagnostics.append({"nlri_type": kind, "reason": "unsupported protocol; only IS-IS L1/L2 and OSPFv2"})
            continue
        values = _tlvs(raw[13:])
        nlri_identity = bytes([protocol]) + instance + _canonical(values)
        key = "bgpls-link-" + hashlib.sha256(nlri_identity).hexdigest()[:32]
        if key in records:
            raise BgplsError("Duplicate canonical BGP-LS NLRI")
        records[key] = (kind, protocol, instance, values, _ls_attributes(path))
    nodes: dict[str, Node] = {}
    identities: dict[str, str] = {}
    details: dict[str, Any] = {}
    for key, (kind, protocol, instance, values, attrs) in records.items():
        if kind != 1:
            continue
        try:
            descriptor = _required(values, 256)
            identity = _node_key(protocol, instance, descriptor)
            router = _address(attrs, 1028)
            descriptor_values = _tlvs(descriptor)
            area = str(ip_address(descriptor_values[514])) if 514 in descriptor_values else None
            binding = bindings.get(router)
            if binding is None and ospf_multi_area is not None:
                # A bound IGP identity with a different advertised TE router ID
                # is inconsistent evidence, not a new independently routable
                # router. Do not silently create a second physical identity.
                for expected_binding in ospf_multi_area.bindings:
                    if (protocol == ospf_multi_area.protocol_id
                            and int.from_bytes(instance, "big") == ospf_multi_area.instance_id
                            and descriptor_values.get(512) == ospf_multi_area.autonomous_system.to_bytes(4, "big")
                            and descriptor_values.get(513) == ospf_multi_area.bgpls_identifier.to_bytes(4, "big")
                            and descriptor_values.get(515) == IPv4Address(expected_binding.igp_router_id).packed):
                        raise BgplsError("OSPF ABR advertised TE router ID conflicts with its trusted binding")
            if binding is not None:
                assert ospf_multi_area is not None
                _validate_ospf_binding(ospf_multi_area, binding, protocol, instance, descriptor_values, area)
                node_id = binding.node_id
                mapped_name = (node_names or {}).get(router)
                if mapped_name is not None and mapped_name != node_id:
                    raise BgplsError("OSPF ABR binding conflicts with the trusted node name mapping")
            else:
                node_id = (node_names or {}).get(router, identity)
            validate_identifier(node_id, "BGP-LS node mapping")
            if identity in identities or (node_id in nodes and binding is None):
                raise BgplsError("Node mapping merges distinct BGP-LS identities")
            if node_id in nodes and (nodes[node_id].loopback != router
                                     or details[node_id].get("projection") != "explicit-ospf-abr"):
                raise BgplsError("OSPF ABR binding collides with another physical router")
            nodes[node_id] = Node(id=node_id, loopback=router, area=None if binding else area)
            identities[identity] = node_id
            detail = {"identity": identity, "protocol_id": protocol,
                      "instance_id": int.from_bytes(instance, "big"),
                      "descriptors": {str(k): v.hex() for k, v in descriptor_values.items()},
                      "advertised_name": attrs.get(1026, b"").decode("utf-8", errors="replace"),
                      "unknown_attributes": ["role", "capabilities"]}
            if binding is None:
                details[node_id] = detail
            else:
                assert ospf_multi_area is not None
                previous = details.get(node_id, {}).get("identities", [])
                memberships = sorted([*previous, {**detail, "area": area}], key=lambda item: item["identity"])
                details[node_id] = {"projection": "explicit-ospf-abr",
                                    "policy_digest": ospf_multi_area.digest,
                                    "identities": memberships,
                                    "areas": sorted(item["area"] for item in memberships),
                                    "expected_areas": list(binding.areas),
                                    "unknown_attributes": ["role", "capabilities"]}
        except BgplsError as exc:
            diagnostics.append({"identity": key, "reason": str(exc)})
    links = []
    for key, (kind, protocol, instance, values, attrs) in records.items():
        if kind != 2:
            continue
        try:
            source_identity = _node_key(protocol, instance, _required(values, 256))
            target_identity = _node_key(protocol, instance, _required(values, 257))
            source, target = identities[source_identity], identities[target_identity]
            source_area = nodes[source].area
            if ospf_multi_area is not None and protocol == 3:
                local_descriptor, remote_descriptor = _tlvs(values[256]), _tlvs(values[257])
                if any(local_descriptor.get(kind) != remote_descriptor.get(kind) for kind in (512, 513, 514)):
                    raise BgplsError("OSPF physical link endpoints disagree on AS, domain identifier, or area")
                source_area = str(ip_address(_required(local_descriptor, 514, 4)))
            if 261 in values or 262 in values or 263 in values:
                raise BgplsError("IPv6 and multi-topology link descriptors require a separate adapter")
            local, remote = _address(values, 259), _address(values, 260)
            if 258 in values:
                _required(values, 258, 8)
            metric = int.from_bytes(_required(attrs, 1092, 4), "big")
            maximum = _bandwidth(_required(attrs, 1089, 4))
            reservable = _bandwidth(_required(attrs, 1090, 4))
            raw_vector = _required(attrs, 1091, 32)
            vector = tuple(_bandwidth(raw_vector[i:i + 4]) for i in range(0, 32, 4))
            # Priority 7 gives a conservative nonpreemptive occupied amount;
            # it is an inference, not a per-LSP reservation inventory.
            reserved = reservable - vector[7]
            group = int.from_bytes(_required(attrs, 1088, 4), "big")
            srlgs = attrs.get(1096)
            if srlgs is not None and len(srlgs) % 4:
                raise BgplsError("Incorrect SRLG TLV length")
            link = Link(
                id=key, source=source, target=target, metric=metric, bidirectional=False,
                from_ip=local, to_ip=remote, reservable_bw_mbps=reservable / 1_000_000,
                max_bw_bps=maximum, reservable_bw_bps=reservable, reserved_bw_bps=reserved,
                unreserved_bw_bps=vector, admin_up=True, oper_up=True,
                admin_groups=tuple(f"bit-{bit}" for bit in range(32) if group & (1 << bit)),
                srlg=tuple(str(int.from_bytes(srlgs[i:i + 4], "big"))
                           for i in range(0, len(srlgs), 4)) if srlgs is not None else (),
                area=source_area, provenance_source="bgp-ls", timestamp=observed.isoformat(),
            )
            links.append(link)
            details[key] = {"protocol_id": protocol, "instance_id": int.from_bytes(instance, "big"),
                            "descriptors": {str(k): v.hex() for k, v in values.items()},
                            "reserved_bw_basis": "reservable_minus_priority7_unreserved",
                            "state_basis": "IGP_LINK_ADVERTISEMENT",
                            "unknown_attributes": ["physical_admin_state", "physical_oper_state",
                                                   "local_interface", "remote_interface", "protection"]
                            + (["srlg"] if srlgs is None else [])}
            if ospf_multi_area is not None:
                details[key].update(source_identity=source_identity, target_identity=target_identity,
                                    area=source_area,
                                    source_node_descriptors={str(k): v.hex() for k, v in _tlvs(values[256]).items()},
                                    target_node_descriptors={str(k): v.hex() for k, v in _tlvs(values[257]).items()})
        except (BgplsError, KeyError, ValueError) as exc:
            diagnostics.append({"identity": key, "reason": str(exc)})
    metadata = {
        "name": "gobgp-live-ted", "source": "bgp-ls", "source_type": "bgp-ls",
        "timestamp": observed.isoformat(), "complete": not diagnostics, "authoritative": False,
        "session_state": "ESTABLISHED", "required_peers": sorted(required_peers),
        "state_basis": "IGP_LINK_ADVERTISEMENT", "execution_eligible": False,
        "limitations": ["physical interface state is unknown", "poll is not an atomic IGP database transaction",
                        "no per-LSP reservation ownership", "no PCEP or RSVP signaling"],
        "excluded_objects": diagnostics, "bgpls_objects": details,
    }
    if ospf_multi_area is not None:
        metadata.update(ospf_multi_area=ospf_multi_area.document(),
                        ospf_multi_area_digest=ospf_multi_area.digest)
    return Topology(name="gobgp-live-ted", nodes=dict(sorted(nodes.items())),
                    links=sorted(links, key=lambda link: link.id), raw_metadata=metadata)


def _validate_ospf_binding(policy: OspfMultiAreaPolicy, binding: OspfAbrBinding,
                           protocol: int, instance: bytes, descriptors: dict[int, bytes],
                           area: str | None) -> None:
    if set(descriptors) != {512, 513, 514, 515}:
        raise BgplsError("OSPF ABR binding requires exactly the trusted AS, domain, area and IGP descriptors")
    expected = {512: policy.autonomous_system.to_bytes(4, "big"),
                513: policy.bgpls_identifier.to_bytes(4, "big"),
                515: IPv4Address(binding.igp_router_id).packed}
    if (protocol != policy.protocol_id or int.from_bytes(instance, "big") != policy.instance_id
            or any(descriptors[kind] != value for kind, value in expected.items())
            or area not in binding.areas):
        raise BgplsError("OSPF ABR advertisement does not match the trusted domain, router ID, or area binding")


def topology_document(topology: Topology) -> dict[str, Any]:
    """Produce the existing strict YAML loader's schema without unit ambiguity."""
    links = []
    for link in topology.links:
        item = asdict(link)
        item["from"], item["to"] = item.pop("source"), item.pop("target")
        item["source"] = item.pop("provenance_source")
        for key in ("base_id", "reservable_bw_mbps", "reserved_bw_mbps"):
            item.pop(key)
        for key in ("unreserved_bw_bps", "admin_groups", "srlg"):
            item[key] = list(item[key])
        links.append(item)
    nodes = []
    for node in topology.nodes.values():
        item = asdict(node)
        item["tags"], item["capabilities"] = list(node.tags), list(node.capabilities)
        nodes.append(item)
    return {"metadata": topology.raw_metadata, "nodes": nodes, "links": links}


class GoBgpTopologyProvider:
    """Poll a local GoBGP gRPC endpoint with only read-only RPCs.

    Each call replaces all objects; a failed call raises and cannot return an
    older cached topology. Consumers must invalidate their own prior snapshot.
    """

    def __init__(self, *, proto_path: str | Path, required_peers: tuple[str, ...],
                 node_names: Mapping[str, str] | None = None, endpoint: str = "127.0.0.1:50051",
                 grpcurl: str = "grpcurl", timeout_seconds: float = 10, max_age_seconds: float = 30,
                 ospf_multi_area: OspfMultiAreaPolicy | None = None,
                 runner: Callable[..., Any] = subprocess.run,
                 clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> None:
        if not required_peers or len(set(required_peers)) != len(required_peers):
            raise BgplsError("Supply unique trusted required_peers")
        for address in required_peers:
            ip_address(address)
        if not endpoint.startswith("127.0.0.1:") or not endpoint.removeprefix("127.0.0.1:").isdigit():
            raise BgplsError("GoBGP plaintext gRPC must use an explicit loopback endpoint")
        if not 1 <= int(endpoint.split(":")[1]) <= 65535:
            raise BgplsError("Invalid GoBGP gRPC port")
        if isinstance(timeout_seconds, bool) or not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 30:
            raise BgplsError("RPC timeout must be finite and between 0 and 30 seconds")
        self.proto_path = Path(proto_path).resolve()
        if not self.proto_path.is_file():
            raise BgplsError("GoBGP read-only protobuf definition is missing")
        self.required_peers = tuple(required_peers)
        self.node_names = dict(node_names or {})
        if ospf_multi_area is not None and not isinstance(ospf_multi_area, OspfMultiAreaPolicy):
            raise BgplsError("OSPF multi-area projection requires a validated explicit policy")
        self.ospf_multi_area = (parse_ospf_multi_area_policy(ospf_multi_area.document())
                                if ospf_multi_area is not None else None)
        self.endpoint, self.grpcurl = endpoint, grpcurl
        self.timeout_seconds, self.max_age_seconds = timeout_seconds, max_age_seconds
        self.runner, self.clock = runner, clock

    def _rpc(self, method: str, request: dict[str, Any]) -> list[dict[str, Any]]:
        if method not in {"ListPeer", "ListPath"}:
            raise BgplsError("Only read-only GoBGP RPCs are allowed")
        command = [self.grpcurl, "-plaintext", "-emit-defaults", "-max-time", str(self.timeout_seconds),
                   "-import-path", str(self.proto_path.parent), "-proto", self.proto_path.name,
                   "-d", json.dumps(request), self.endpoint, f"apipb.GobgpApi/{method}"]
        # Disk spooling bounds in-memory output even if a daemon misbehaves.
        with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
            try:
                result = self.runner(command, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr,
                                     timeout=self.timeout_seconds + 1, check=False)
                if result.returncode:
                    raise BgplsError(f"GoBGP {method} RPC failed (exit {result.returncode})")
                if stdout.tell() > _MAX_OUTPUT:
                    raise BgplsError("GoBGP output exceeds the size limit")
                stdout.seek(0)
                output = stdout.read(_MAX_OUTPUT + 1).decode("utf-8")
            except (OSError, subprocess.TimeoutExpired, UnicodeError) as exc:
                raise BgplsError(f"GoBGP {method} RPC unavailable or timed out") from exc
        return decode_json_stream(output)

    def capture(self) -> dict[str, Any]:
        started = self.clock().isoformat()
        before = self._rpc("ListPeer", {})
        _peers(before, self.required_peers)
        rib = self._rpc("ListPath", {"tableType": "GLOBAL", "family": _AF, "enableOnlyBinary": True})
        after = self._rpc("ListPeer", {})
        capture = {"schema_version": 1, "source": "gobgp-v3.37.0-grpc", "poll_started_at": started,
                   "captured_at": self.clock().isoformat(), "peers_before": before, "rib": rib,
                   "peers_after": after}
        parse_gobgp_snapshot(capture, node_names=self.node_names, required_peers=self.required_peers,
                             now=self.clock(), max_age_seconds=self.max_age_seconds,
                             ospf_multi_area=self.ospf_multi_area)
        return capture

    def snapshot(self) -> Topology:
        return parse_gobgp_snapshot(self.capture(), node_names=self.node_names,
                                    required_peers=self.required_peers, now=self.clock(),
                                    max_age_seconds=self.max_age_seconds, ospf_multi_area=self.ospf_multi_area)
