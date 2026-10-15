"""Bounded RSVP-only PCInitiate wire encoding (RFC 8281 and RFC 8408).

These pure helpers do not authorize a write, reserve resources, or prove that a
PCC implements RSVP initiation. The caller owns session-scoped increasing SRP
IDs, globally unique managed names at the PCC, durable admission and ownership,
and correlated reports. There is deliberately no wildcard delete or SR fallback.
"""
from __future__ import annotations

import math
import re
import struct
from ipaddress import IPv4Address
from typing import Any

from .pcep import PCINITIATE, PCUPD, encode_message, encode_object, parse_objects

MAX_BANDWIDTH_BPS = 1_000_000_000_000
MAX_ERO_HOPS = 256
MAX_SYMBOLIC_NAME_BYTES = 64
_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,63}\Z")


def _identifier(value: int, maximum: int, label: str) -> None:
    if type(value) is not int or not 1 <= value <= maximum:
        raise ValueError(f"{label} must be a nonzero bounded integer")


def _name(value: str) -> bytes:
    if not isinstance(value, str) or not _NAME.fullmatch(value):
        raise ValueError("Symbolic name must contain 1..64 managed ASCII letters, digits, '_' or '-'")
    return value.encode("ascii")


def _address(value: str) -> IPv4Address:
    if not isinstance(value, str):
        raise ValueError("RSVP initiation requires explicit unicast IPv4 addresses")
    address = IPv4Address(value)
    if (str(address) != value or int(address) >> 24 in (0, 127) or int(address) >> 24 >= 224 or
            address.is_link_local):
        raise ValueError("RSVP initiation requires explicit routed unicast IPv4 addresses")
    return address


def encoded_bandwidth(bandwidth_bps: int) -> tuple[bytes, int]:
    """Return the float32 bytes/s body and actual bps; never under-reserve.

    The 1 Tbps local policy bounds both the requested and encoded amount. Reserve
    and compute using the returned amount, which may exceed the requested bps.
    """
    _identifier(bandwidth_bps, MAX_BANDWIDTH_BPS, "Bandwidth in bits/s")
    body = struct.pack("!f", bandwidth_bps / 8)
    amount = struct.unpack("!f", body)[0]
    if amount * 8 < bandwidth_bps:
        body = (int.from_bytes(body, "big") + 1).to_bytes(4, "big")
        amount = struct.unpack("!f", body)[0]
    actual_bps = math.ceil(amount * 8)
    if not math.isfinite(amount) or amount <= 0 or actual_bps > MAX_BANDWIDTH_BPS:
        raise ValueError("Upward-rounded wire bandwidth exceeds the RSVP initiation policy")
    return body, actual_bps


def encoded_bandwidth_bps(bandwidth_bps: int) -> int:
    """Return the conservative wire amount for computation and admission."""
    return encoded_bandwidth(bandwidth_bps)[1]


def _tlv(kind: int, value: bytes) -> bytes:
    return struct.pack("!HH", kind, len(value)) + value + bytes(-len(value) % 4)


def _srp(srp_id: int, *, remove: bool = False) -> bytes:
    # Leave the final uint32 value unused so the session owner can close before
    # identifier exhaustion; no wrap/reuse is provided by this stateless codec.
    _identifier(srp_id, 0xFFFFFFFE, "SRP identifier")
    return encode_object(33, struct.pack("!II", int(remove), srp_id) + _tlv(28, bytes(4)))


def encode_initiate(srp_id: int, symbolic_name: str, source: str, destination: str,
                    ero: tuple[str, ...], bandwidth_bps: int,
                    setup_priority: int = 7, hold_priority: int = 7) -> bytes:
    """Encode one new, administratively enabled numbered IPv4 RSVP LSP.

    PLSP-ID is zero. C/D are PCC report flags, not asserted creation or ownership.
    Every ERO item is encoded as a strict IPv4 /32 remote interface address.
    """
    name = _name(symbolic_name)
    sender, endpoint = _address(source), _address(destination)
    if sender == endpoint:
        raise ValueError("Initiated RSVP endpoints must differ")
    if not isinstance(ero, tuple) or not 1 <= len(ero) <= MAX_ERO_HOPS:
        raise ValueError("RSVP initiation requires 1..256 explicit strict IPv4 hops")
    addresses = tuple(_address(item) for item in ero)
    if len(set(addresses)) != len(addresses):
        raise ValueError("Initiated RSVP path must not repeat a hop address")
    for value in (setup_priority, hold_priority):
        if type(value) is not int or not 0 <= value <= 7:
            raise ValueError("RSVP setup and hold priorities must be integers 0..7")
    if setup_priority < hold_priority:
        raise ValueError("Setup priority must not exceed hold priority in precedence")
    bandwidth, _ = encoded_bandwidth(bandwidth_bps)
    route = b"".join(bytes((1, 8)) + address.packed + bytes((32, 0)) for address in addresses)
    lsp = encode_object(32, struct.pack("!I", 8) + _tlv(17, name))
    endpoints = encode_object(4, sender.packed + endpoint.packed)
    lspa = encode_object(9, bytes(12) + bytes((setup_priority, hold_priority, 0, 0)))
    return encode_message(PCINITIATE, _srp(srp_id) + lsp + endpoints + encode_object(7, route) +
                          lspa + encode_object(5, bandwidth))


def encode_delete(srp_id: int, plsp_id: int, symbolic_name: str) -> bytes:
    """Encode deletion of one caller-proven owned, created and delegated LSP.

    PLSP-ID zero is a destructive wildcard and 0xfffff is reserved: both reject.
    RFC 8281 erratum 6301 preserves the SRP,LSP-only deletion grammar. The remove
    bit belongs to SRP, not LSP. No reclamation or bulk deletion API is exposed.
    """
    _identifier(plsp_id, 0xFFFFE, "Deletion PLSP identifier")
    name = _name(symbolic_name)
    return encode_message(PCINITIATE, _srp(srp_id, remove=True) +
                          encode_object(32, struct.pack("!I", plsp_id << 12) + _tlv(17, name)))


def encode_owned_update(srp_id: int, report: dict[str, Any], ero: tuple[str, ...],
                        bandwidth_bps: int, setup_priority: int = 7, hold_priority: int = 7) -> bytes:
    """Encode a private qualification update of one proven owned RSVP LSP.

    Durable ownership, current session and admission are separate writer gates.
    This does not silently turn failed initiation into a public creation profile.
    """
    if (not isinstance(report, dict) or report.get("created") is not True or report.get("delegate") is not True or
            report.get("removed") is not False or report.get("administrative_up") is not True or
            type(report.get("sync")) is not bool or report.get("end_of_sync") is not False):
        raise ValueError("An owned created, delegated and enabled RSVP report is required")
    plsp, ids = report.get("plsp_id"), report.get("identifiers")
    if type(plsp) is not int:
        raise ValueError("An allocated owned PLSP identifier is required")
    _identifier(plsp, 0xFFFFE, "Owned update PLSP identifier")
    if not isinstance(ids, dict) or ids.get("type") != 18:
        raise ValueError("An owned full IPv4 RSVP identity is required")
    try:
        identity = bytes.fromhex(ids["value_hex"])
        source, destination = ids["sender"], ids["endpoint"]
        if (len(identity) != 16 or _address(source).packed != identity[:4] or
                _address(destination).packed != identity[12:] or
                ids["lsp_id"] != int.from_bytes(identity[4:6], "big") or
                ids["tunnel_id"] != int.from_bytes(identity[6:8], "big") or not ids["tunnel_id"] or
                ids["extended_tunnel_id_hex"] != identity[8:12].hex() or ids.get("all_zero") is not False):
            raise ValueError("Owned RSVP identity bytes differ")
        name = _name(report["symbolic_name"])
        if report.get("symbolic_name_hex") != name.hex():
            raise ValueError("Owned symbolic name bytes differ")
    except (KeyError, TypeError) as exc:
        raise ValueError("Malformed owned RSVP identity") from exc
    if report.get("srp_tlvs", []) not in ([], [{"type": 28, "value_hex": "00000000"}]):
        raise ValueError("Owned update requires RSVP setup type")
    objects = parse_objects(encode_initiate(srp_id, name.decode(), source, destination, ero,
                                            bandwidth_bps, setup_priority, hold_priority)[4:])
    lsp = encode_object(32, struct.pack("!I", (plsp << 12) | 9) + _tlv(17, name) + _tlv(18, identity))
    return encode_message(PCUPD, _srp(srp_id) + lsp + b"".join(
        encode_object(obj.object_class, obj.body, obj.object_type) for obj in objects if obj.object_class in (7, 9, 5)
    ) + encode_object(6, bytes((0, 0, 0, 2)) + bytes(4)))


def _capabilities(peer_open: dict[str, Any]) -> dict[int, list[bytes]] | None:
    values: dict[int, list[bytes]] = {}
    tlvs = peer_open.get("tlvs")
    if not isinstance(tlvs, list) or len(tlvs) > 64:
        return None
    for item in tlvs:
        if not isinstance(item, dict):
            return None
        kind, value = item.get("type"), item.get("value_hex")
        if (type(kind) is not int or not 1 <= kind <= 65535 or not isinstance(value, str) or
                len(value) > 131070 or not re.fullmatch(r"(?:[0-9a-fA-F]{2})*", value)):
            return None
        values.setdefault(kind, []).append(bytes.fromhex(value))
    return values


def supports_rsvp_initiation(peer_open: dict[str, Any]) -> bool:
    """Validate advertised U/I and RSVP setup compatibility, not qualification.

    Modern PST capability takes precedence over legacy SR capability. Unknown
    modern setup sub-TLVs are outside this bounded profile and fail closed.
    Session authorization and the local peer's negotiated I flag remain caller
    responsibilities. An OPEN without setup extensions uses base RSVP.
    """
    if not isinstance(peer_open, dict) or peer_open.get("stateful") is not True:
        return False
    flags = peer_open.get("stateful_flags")
    if type(flags) is not int or not 0 <= flags <= 0xFFFFFFFF or flags & 5 != 5:
        return False
    values = _capabilities(peer_open)
    if values is None or values.get(16) != [struct.pack("!I", flags)]:
        return False
    modern, legacy = values.get(34, []), values.get(26, [])
    if modern:
        # RFC 8408: use only the first modern capability; legacy top-level
        # capability is ignored, including when both generations are present.
        value = modern[0]
        if len(value) < 5 or not value[3] or len(value) < 4 + value[3]:
            return False
        count = value[3]
        psts = value[4:4 + count]
        if 0 not in psts:
            return False
        # With no sub-TLVs, Length excludes final list padding (RFC 8408 §3).
        if len(value) == 4 + count:
            return 1 not in psts
        sub_start = 4 + count + (-count % 4)
        # Only the defined SR capability sub-TLV is understood here. RFC 8664
        # requires it when PST 1 is advertised. No unknown setup constraint is
        # silently accepted and no SR capability ever substitutes for RSVP.
        return (1 in psts and len(value) == sub_start + 8 and
                value[sub_start:sub_start + 4] == struct.pack("!HH", 26, 4) and
                _legacy_sr_capability(value[sub_start + 4:]))
    if legacy:
        # RFC 8664 Appendix A: legacy SR capability also advertises base RSVP.
        return _legacy_sr_capability(legacy[0])
    return True


def _legacy_sr_capability(value: bytes) -> bool:
    # N=bit1 concerns NAI; X=bit0 means MSD is ignored. Reserved bits/bytes must
    # be ignored on receipt. X=0 with zero MSD is invalid (RFC 8664 §5.1).
    return len(value) == 4 and (bool(value[2] & 1) or value[3] != 0)
