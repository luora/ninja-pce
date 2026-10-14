"""Immutable, bounded initial RSVP delegation authority for approved PCCs.

A headend policy authorizes a supported report profile, not a tunnel-number
list. It does not allocate tunnels, reserve bandwidth, or authorize changing
an existing path. Session freshness and the concurrent ownership limit belong
to the PCEP session owner.
"""
from __future__ import annotations

import json
import math
import os
import stat
import struct
from dataclasses import dataclass
from ipaddress import IPv4Address, IPv4Network
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from .state import digest

_MAX_FILE_BYTES = 1_048_576
_PROOF_KEYS = ("headend_policy_digest", "headend_report_digest")


def _load_document(path: Path) -> Any:
    """Read bounded strict JSON without following symlinks or blocking on FIFOs."""
    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate delegation policy JSON key")
            result[key] = value
        return result

    def constant(value: str) -> Any:
        raise ValueError("Nonfinite delegation policy JSON constant")

    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_FILE_BYTES:
            raise ValueError("Delegation policy must be a bounded regular JSON file")
        raw = handle.read(_MAX_FILE_BYTES + 1)
    if len(raw) > _MAX_FILE_BYTES:
        raise ValueError("Delegation policy exceeds 1 MiB")
    return json.loads(raw, object_pairs_hook=unique, parse_constant=constant)


def _address(value: Any) -> IPv4Address:
    if not isinstance(value, str):
        raise ValueError("Headend policy addresses must be IPv4 strings")
    address = IPv4Address(value)
    if (str(address) != value or address.is_unspecified or address.is_multicast or
            address.is_loopback or address == IPv4Address("255.255.255.255")):
        raise ValueError("Headend policy requires canonical unicast IPv4 addresses")
    return address


def _hex(value: Any, limit: int = 512) -> bytes:
    if not isinstance(value, str) or len(value) > limit * 2:
        raise ValueError("Malformed or oversized delegated wire value")
    raw = bytes.fromhex(value)
    if raw.hex() != value:
        raise ValueError("Delegated wire values must use canonical hexadecimal")
    return raw


def _route(value: Any, *, recorded: bool = False) -> bytes:
    if not isinstance(value, list) or len(value) > 64:
        raise ValueError("Only bounded IPv4 routes are supported")
    body = b""
    for item in value:
        if (not isinstance(item, dict) or item.get("type") != 1 or item.get("prefix_length") != 32 or
                not recorded and item.get("loose") is not False):
            raise ValueError("Only strict IPv4 /32 route subobjects are supported")
        address = _address(item.get("address"))
        raw = bytes((1, 8)) + address.packed + bytes((32, 0))
        if _hex(item.get("raw_hex"), 8) != raw:
            raise ValueError("Route fields differ from the reported wire value")
        body += raw
    return body


def _profile(report: dict[str, Any]) -> tuple[str, str, int, dict[str, Any]]:
    """Validate the understood IPv4 RSVP profile and return its wire proof."""
    if (report.get("delegate") is not True or report.get("administrative_up") is not True or
            report.get("sync") is not False or report.get("removed") is not False or
            report.get("created") is not False or report.get("end_of_sync") is not False or
            report.get("stale") is True):
        raise ValueError("A current live router-owned delegation is required")
    plsp = report.get("plsp_id")
    state = report.get("operational_code")
    if (type(plsp) is not int or not 0 < plsp < 0xFFFFF or type(state) is not int or state not in (0, 1, 2) or
            report.get("operational_state") != {0: "DOWN", 1: "UP", 2: "ACTIVE"}[state]):
        raise ValueError("Unsupported delegated LSP identity or state")
    srp = report.get("srp_id")
    if type(srp) is not int or not 0 <= srp < 0xFFFFFFFF or state == 0 and srp != 0:
        raise ValueError("Unsupported delegated SRP identity")
    srp_tlvs = report.get("srp_tlvs", [])
    if srp_tlvs not in ([], [{"type": 28, "value_hex": "00000000"}]):
        raise ValueError("Unsupported delegated SRP extension")
    identifiers = report.get("identifiers")
    if (not isinstance(identifiers, dict) or identifiers.get("type") != 18 or
            identifiers.get("all_zero") is not False):
        raise ValueError("Explicit IPv4 RSVP LSP identifiers are required")
    source = _address(identifiers.get("sender"))
    destination = _address(identifiers.get("endpoint"))
    if source == destination:
        raise ValueError("Delegated source and destination must differ")
    tunnel = identifiers.get("tunnel_id")
    instance = identifiers.get("lsp_id")
    if (type(tunnel) is not int or not 1 <= tunnel <= 65535 or
            type(instance) is not int or not 0 <= instance <= 65535):
        raise ValueError("Invalid RSVP tunnel or LSP identifier")
    extended = _hex(identifiers.get("extended_tunnel_id_hex"), 4)
    if len(extended) != 4:
        raise ValueError("Invalid extended RSVP tunnel identifier")
    identity = source.packed + struct.pack("!HH", instance, tunnel) + extended + destination.packed
    if _hex(identifiers.get("value_hex"), 16) != identity:
        raise ValueError("RSVP identity fields differ from the reported wire value")
    tlvs = report.get("tlvs")
    if not isinstance(tlvs, list) or not 1 <= len(tlvs) <= 3:
        raise ValueError("Unsupported delegated LSP extensions")
    decoded: dict[int, bytes] = {}
    for item in tlvs:
        if not isinstance(item, dict):
            raise ValueError("Malformed delegated LSP extension")
        kind = item.get("type")
        if type(kind) is not int or kind not in (17, 18, 31) or kind in decoded:
            raise ValueError("Unsupported or duplicate delegated LSP extension")
        raw = _hex(item.get("value_hex"))
        if kind == 17 and not raw:
            raise ValueError("A symbolic name cannot be empty")
        if kind == 31 and (len(raw) != 6 or raw[:2] != bytes(2) or int.from_bytes(raw[2:], "big") & 0xFFF):
            raise ValueError("Unsupported RSVP Binding SID")
        decoded[kind] = raw
    if decoded.get(18) != identity:
        raise ValueError("IPv4 identifiers TLV does not match the RSVP identity")
    if 17 in decoded and report.get("symbolic_name_hex") != decoded[17].hex():
        raise ValueError("Symbolic name differs from the reported wire value")
    ero = _route(report.get("ero"))
    rro = _route(report.get("rro", []), recorded=True)
    if state == 0 and (ero or rro) or state in (1, 2) and not ero:
        raise ValueError("Only initial empty paths or existing active paths are supported")
    attributes = report.get("attributes")
    if not isinstance(attributes, list) or not 2 <= len(attributes) <= 5:
        raise ValueError("Explicit bounded RSVP attributes are required")
    attrs: dict[tuple[int, int], bytes] = {}
    for attr in attributes:
        if not isinstance(attr, dict):
            raise ValueError("Malformed delegated attribute")
        key = (attr.get("class"), attr.get("type"))
        if key not in {(5, 1), (5, 5), (9, 1), (6, 1), (8, 1)} or key in attrs:
            raise ValueError("Unsupported or duplicate delegated attribute")
        attrs[key] = _hex(attr.get("body_hex"))
    bandwidth = attrs.get((5, 1), b"")
    lspa = attrs.get((9, 1), b"")
    if len(bandwidth) != 4 or len(lspa) != 16:
        raise ValueError("Explicit BANDWIDTH and exact LSPA are required")
    amount = struct.unpack("!f", bandwidth)[0]
    if not math.isfinite(amount) or amount <= 0:
        raise ValueError("Positive finite RSVP bandwidth is required")
    if (5, 5) in attrs and attrs[(5, 5)] != bandwidth:
        raise ValueError("Different intended/used bandwidth needs reoptimization accounting")
    _, _, _, setup, hold, flags, _ = struct.unpack("!IIIBBBB", lspa)
    if setup > 7 or hold > 7 or setup < hold or flags & 3:
        raise ValueError("Unsupported priorities or RSVP protection constraints")
    metric = attrs.get((6, 1), bytes((0, 0, 0, 2)) + bytes(4))
    if len(metric) != 8 or metric[3] != 2 or metric[2] & 1:
        raise ValueError("Only unconstrained TE metric is supported")
    cost = struct.unpack("!f", metric[4:])[0]
    if not math.isfinite(cost) or cost < 0:
        raise ValueError("TE metric must be finite and nonnegative")
    if (8, 1) in attrs and (state == 0 or attrs[(8, 1)] != rro) or rro and (8, 1) not in attrs:
        raise ValueError("Recorded route attributes differ from the reported route")
    proof = {"plsp_id": plsp, "state": state, "srp_id": srp, "srp_tlvs": srp_tlvs,
             "identity": identity.hex(), "symbolic_name_hex": report.get("symbolic_name_hex"),
             "tlvs": [(kind, raw.hex()) for kind, raw in decoded.items()],
             "ero": ero.hex(), "rro": rro.hex(),
             "attributes": [(kind, raw.hex()) for kind, raw in attrs.items()]}
    return str(source), str(destination), math.ceil(amount * 8), proof


@dataclass(frozen=True)
class _Headend:
    source: str
    destinations: tuple[str, ...]
    max_bandwidth_bps: int
    max_live_lsps: int


@dataclass(frozen=True)
class HeadendDelegationPolicy:
    """Loaded once; its canonical digest and immutable rules bind every write."""

    _rules: Mapping[str, _Headend]
    policy_digest: str

    @classmethod
    def from_document(cls, document: Any, headends: dict[str, str]) -> HeadendDelegationPolicy:
        if (not isinstance(document, dict) or set(document) != {"version", "headends"} or
                type(document.get("version")) is not int or document["version"] != 1):
            raise ValueError("Headend delegation policy requires version 1 and explicit headends")
        entries = document["headends"]
        if not isinstance(entries, dict) or not 1 <= len(entries) <= 64:
            raise ValueError("Authorize 1..64 explicit delegation headends")
        rules: dict[str, _Headend] = {}
        canonical: dict[str, Any] = {}
        for peer, item in entries.items():
            address = str(_address(peer))
            if (not isinstance(item, dict) or
                    set(item) - {"source", "destinations", "max_bandwidth_bps", "max_live_lsps"} or
                    not {"source", "destinations", "max_bandwidth_bps"} <= set(item)):
                raise ValueError("Headend delegation rule has missing or unsupported fields")
            source = str(_address(item["source"]))
            if headends.get(address) != source:
                raise ValueError("Delegation policy must match explicitly configured computation headends")
            destinations = item["destinations"]
            if not isinstance(destinations, list) or not 1 <= len(destinations) <= 256:
                raise ValueError("Authorize 1..256 destination addresses or networks per headend")
            networks = []
            for destination in destinations:
                if not isinstance(destination, str):
                    raise ValueError("Delegation destinations must be IPv4 strings")
                if "/" not in destination:
                    network = IPv4Network(str(_address(destination)) + "/32")
                else:
                    network = IPv4Network(destination, strict=True)
                    if str(network) != destination or network.prefixlen == 0 or network.is_multicast:
                        raise ValueError("Delegation destinations require explicit canonical IPv4 networks")
                if network in networks:
                    raise ValueError("Delegation destination networks must be distinct")
                networks.append(network)
            maximum = item["max_bandwidth_bps"]
            count = item.get("max_live_lsps", 32)
            if type(maximum) is not int or not 0 < maximum <= 10**12:
                raise ValueError("Headend maximum bandwidth must be a positive integer at most 10^12 bps")
            if type(count) is not int or not 1 <= count <= 256:
                raise ValueError("Headend concurrent LSP limit must be 1..256")
            rules[address] = _Headend(source, tuple(str(network) for network in networks), maximum, count)
            canonical[address] = {"source": source, "destinations": [str(value) for value in networks],
                                  "max_bandwidth_bps": maximum, "max_live_lsps": count}
        return cls(MappingProxyType(rules), digest({"version": 1, "headends": canonical}))

    @classmethod
    def from_path(cls, path: Path, headends: dict[str, str]) -> HeadendDelegationPolicy:
        return cls.from_document(_load_document(path), headends)

    @property
    def headends(self) -> dict[str, str]:
        return {peer: item.source for peer, item in self._rules.items()}

    def max_live_lsps(self, peer: str) -> int | None:
        rule = self._rules.get(peer)
        return rule.max_live_lsps if rule is not None else None

    def proof(self, peer: str, report: dict[str, Any]) -> dict[str, str] | None:
        rule = self._rules.get(peer)
        if rule is None:
            return None
        try:
            source, destination, bandwidth, profile = _profile(report)
            if (source != rule.source or bandwidth > rule.max_bandwidth_bps or
                    not any(IPv4Address(destination) in IPv4Network(network) for network in rule.destinations)):
                return None
            return dict(zip(_PROOF_KEYS, (self.policy_digest, digest(profile))))
        except (ValueError, KeyError, TypeError, OverflowError, struct.error):
            return None

    def allows(self, peer: str, report: dict[str, Any]) -> bool:
        return self.proof(peer, report) is not None
