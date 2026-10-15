"""Discover bounded PCC authority from a trusted, fresh BGP-LS domain.

The caller supplies its validated topology snapshot. Transport peer, advertised
loopback, and RSVP sender must be the same unique router; display names and
router roles grant no authority. This module neither reads live collectors nor
mutates computation headend mappings.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass
from ipaddress import IPv4Network
from pathlib import Path
from typing import Any

from .models import Topology
from .pcep_headend_policy import _address, _hex, _load_document, _profile
from .state import digest


def _prefixes(value: Any) -> tuple[IPv4Network, ...]:
    if not isinstance(value, list) or not 1 <= len(value) <= 64:
        raise ValueError("Delegation domain requires 1..64 explicit IPv4 prefixes per scope")
    networks = []
    for item in value:
        if not isinstance(item, str) or "/" not in item:
            raise ValueError("Delegation domain scope must contain canonical IPv4 CIDRs")
        network = IPv4Network(item, strict=True)
        if str(network) != item or network.prefixlen == 0 or network.is_multicast:
            raise ValueError("Delegation domain requires explicit canonical unicast IPv4 networks")
        if network in networks:
            raise ValueError("Delegation domain prefixes must be distinct")
        networks.append(network)
    return tuple(sorted(networks, key=lambda item: (int(item.network_address), item.prefixlen)))


@dataclass(frozen=True)
class DomainDelegationPolicy:
    """An immutable domain scope; arbitrary new tunnel IDs need no registration."""

    protocol_id: int
    instance_id: int
    autonomous_system: int
    bgpls_identifier: int
    _pcc_prefixes: tuple[str, ...]
    _destination_prefixes: tuple[str, ...]
    max_bandwidth_bps: int
    _max_live_lsps: int
    policy_digest: str

    @classmethod
    def from_document(cls, document: Any) -> DomainDelegationPolicy:
        fields = {"version", "domain", "pcc_prefixes", "destination_prefixes",
                  "max_bandwidth_bps", "max_live_lsps"}
        if (not isinstance(document, dict) or set(document) != fields or
                type(document.get("version")) is not int or document["version"] != 1):
            raise ValueError("Delegation domain policy requires exactly the version 1 scope fields")
        domain = document["domain"]
        domain_fields = {"protocol_id", "instance_id", "autonomous_system", "bgpls_identifier"}
        if not isinstance(domain, dict) or set(domain) != domain_fields:
            raise ValueError("Delegation domain identity has missing or unsupported fields")
        for field, minimum, maximum in (("protocol_id", 1, 3), ("instance_id", 0, 2**64 - 1),
                                        ("autonomous_system", 1, 2**32 - 1),
                                        ("bgpls_identifier", 0, 2**32 - 1)):
            value = domain[field]
            if type(value) is not int or not minimum <= value <= maximum:
                raise ValueError(f"Delegation domain {field} is outside its supported integer range")
        pcc_prefixes = _prefixes(document["pcc_prefixes"])
        destination_prefixes = _prefixes(document["destination_prefixes"])
        bandwidth = document["max_bandwidth_bps"]
        count = document["max_live_lsps"]
        if type(bandwidth) is not int or not 0 < bandwidth <= 10**12:
            raise ValueError("Domain maximum bandwidth must be a positive integer at most 10^12 bps")
        if type(count) is not int or not 1 <= count <= 256:
            raise ValueError("Domain concurrent LSP limit must be 1..256")
        canonical = {"version": 1, "domain": dict(domain),
                     "pcc_prefixes": [str(value) for value in pcc_prefixes],
                     "destination_prefixes": [str(value) for value in destination_prefixes],
                     "max_bandwidth_bps": bandwidth, "max_live_lsps": count}
        return cls(domain["protocol_id"], domain["instance_id"], domain["autonomous_system"],
                   domain["bgpls_identifier"], tuple(str(value) for value in pcc_prefixes),
                   tuple(str(value) for value in destination_prefixes), bandwidth, count,
                   digest(canonical))

    @classmethod
    def from_path(cls, path: Path) -> DomainDelegationPolicy:
        return cls.from_document(_load_document(path))

    @property
    def pcc_prefixes(self) -> tuple[IPv4Network, ...]:
        # IPv4Network itself is mutable; callers receive fresh views.
        return tuple(IPv4Network(value) for value in self._pcc_prefixes)

    @property
    def destination_prefixes(self) -> tuple[IPv4Network, ...]:
        return tuple(IPv4Network(value) for value in self._destination_prefixes)

    def _member(self, address: str, topology: Topology) -> dict[str, Any] | None:
        """Prove one physical router and every advertised ABR area identity."""
        matches = [node for node in topology.nodes.values() if node.loopback == address]
        if len(matches) != 1:
            return None
        details = topology.raw_metadata.get("bgpls_objects")
        if not isinstance(details, dict):
            return None
        detail = details.get(matches[0].id)
        if not isinstance(detail, dict):
            return None
        members = detail.get("identities") if "identities" in detail else [detail]
        if not isinstance(members, list) or not 1 <= len(members) <= 64:
            return None
        identities: list[dict[str, Any]] = []
        seen: set[str] = set()
        for member in members:
            if (not isinstance(member, dict) or type(member.get("protocol_id")) is not int or
                    member["protocol_id"] != self.protocol_id or type(member.get("instance_id")) is not int or
                    member["instance_id"] != self.instance_id):
                return None
            descriptors = member.get("descriptors")
            identity = member.get("identity")
            if (not isinstance(descriptors, dict) or not isinstance(identity, str) or
                    not 1 <= len(identity) <= 128 or identity in seen or
                    descriptors.get("512") != self.autonomous_system.to_bytes(4, "big").hex() or
                    descriptors.get("513") != self.bgpls_identifier.to_bytes(4, "big").hex()):
                return None
            seen.add(identity)
            if (not all(isinstance(key, str) and key.isascii() and key.isdecimal() for key in descriptors) or
                    len(descriptors) > 64):
                return None
            decoded = {key: _hex(value).hex() for key, value in descriptors.items()}
            identities.append({"identity": identity, "protocol_id": self.protocol_id,
                               "instance_id": self.instance_id, "descriptors": decoded})
        identities.sort(key=lambda item: item["identity"])
        return {"loopback": address, "identities": identities}

    def source(self, peer: str, topology: Topology) -> str | None:
        try:
            address = _address(peer)
            if (not any(address in network for network in self.pcc_prefixes) or
                    self._member(str(address), topology) is None):
                return None
            return str(address)
        except (ValueError, KeyError, TypeError, AttributeError, OverflowError):
            return None

    def max_live_lsps(self, peer: str) -> int | None:
        try:
            address = _address(peer)
            return self._max_live_lsps if any(address in network for network in self.pcc_prefixes) else None
        except (ValueError, TypeError):
            return None

    def destination(self, address: str, topology: Topology) -> bool:
        try:
            destination = _address(address)
            return (any(destination in network for network in self.destination_prefixes) and
                    self._member(str(destination), topology) is not None)
        except (ValueError, KeyError, TypeError, AttributeError, OverflowError):
            return False

    def graph(self, topology: Topology) -> Topology:
        """Keep domain transit nodes and edges; endpoint prefixes remain separate."""
        nodes = {}
        for key, node in topology.nodes.items():
            try:
                if self._member(str(_address(node.loopback)), topology) is not None:
                    nodes[key] = node
            except (ValueError, KeyError, TypeError, AttributeError, OverflowError):
                continue
        details = topology.raw_metadata.get("bgpls_objects", {})
        links = []
        for link in topology.links:
            if link.source not in nodes or link.target not in nodes:
                continue
            detail = details.get(link.id) if isinstance(details, dict) else None
            if (not isinstance(detail, dict) or type(detail.get("protocol_id")) is not int or
                    detail["protocol_id"] != self.protocol_id or type(detail.get("instance_id")) is not int or
                    detail["instance_id"] != self.instance_id):
                continue
            links.append(link)
        metadata = dict(topology.raw_metadata)
        if isinstance(details, dict):
            keys = set(nodes) | {link.id for link in links}
            metadata["bgpls_objects"] = {key: value for key, value in details.items() if key in keys}
        return Topology(name=topology.name, nodes=nodes, links=links, raw_metadata=metadata)

    def proof(self, peer: str, report: dict[str, Any], topology: Topology) -> dict[str, str] | None:
        try:
            source, destination, bandwidth, profile = _profile(report)
            if (self.source(peer, topology) != source or bandwidth > self.max_bandwidth_bps or
                    not self.destination(destination, topology)):
                return None
            source_member = self._member(source, topology)
            destination_member = self._member(destination, topology)
            if source_member is None or destination_member is None:
                return None
            return {"domain_policy_digest": self.policy_digest, "domain_report_digest": digest(profile),
                    "domain_source_digest": digest({"source": source_member, "destination": destination_member})}
        except (ValueError, KeyError, TypeError, AttributeError, OverflowError, struct.error):
            return None

    def allows(self, peer: str, report: dict[str, Any], topology: Topology) -> bool:
        return self.proof(peer, report, topology) is not None
