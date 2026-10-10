"""Opt-in initial RSVP path provision for explicitly delegated lab tunnels.

Only a DOWN, administratively enabled, empty-ERO delegation requests a path.
Active LSPs are never rerouted. The PCC performs RSVP admission/signaling.
"""
from __future__ import annotations

import math
import struct
from ipaddress import IPv4Address
from typing import Any

from .pcep import PCREP, encode_message, encode_object, parse_objects
from .pcep_compute import PathReply, PcepPathComputer

PCUPD = 11


def report_key(report: dict[str, Any]) -> str:
    identifiers = report["identifiers"]
    return f"{report['plsp_id']}:{identifiers['type']}:{identifiers['value_hex']}"


def ownership_key(report: dict[str, Any]) -> tuple[Any, ...]:
    """Bind delegation to the tunnel, allowing RSVP LSP-ID to change."""
    identifiers = report["identifiers"]
    return (identifiers.get("type"), identifiers["sender"], identifiers["endpoint"],
            identifiers["tunnel_id"], identifiers.get("extended_tunnel_id_hex"))


def report_fingerprint(report: dict[str, Any]) -> tuple[Any, ...]:
    """Fields that must remain unchanged while an initial path is computed."""
    return (report_key(report), report["delegate"], report["administrative_up"],
            report["removed"], report["operational_code"], repr(report["ero"]),
            repr(report["attributes"]), report.get("symbolic_name_hex"),
            report["srp_id"], repr(report.get("srp_tlvs", [])), repr(report["tlvs"]))


def _lsp(report: dict[str, Any], delegate: bool) -> bytes:
    bits = (report["plsp_id"] << 12) | (8 if report["administrative_up"] else 0) | int(delegate)
    tlvs = b""
    for item in report["tlvs"]:
        if item["type"] in (17, 18):
            value = bytes.fromhex(item["value_hex"])
            tlvs += struct.pack("!HH", item["type"], len(value)) + value + bytes(-len(value) % 4)
    return encode_object(32, bits.to_bytes(4, "big") + tlvs)


def return_delegation(report: dict[str, Any], srp_id: int) -> bytes:
    """RFC 8231 empty update D=0: return authority; PCC applies local policy."""
    return encode_message(PCUPD, encode_object(33, struct.pack("!II", 0, srp_id)) +
                          _lsp(report, False) + encode_object(7, b""))


class DelegatedPathComputer:
    def __init__(self, computer: PcepPathComputer, allowed_tunnels: dict[str, tuple[int, ...]]):
        if not allowed_tunnels or any(peer not in computer.headends for peer in allowed_tunnels):
            raise ValueError("Delegation requires explicitly authorized headends")
        for tunnels in allowed_tunnels.values():
            if (not tunnels or len(tunnels) > 16 or len(set(tunnels)) != len(tunnels) or
                    any(type(value) is not int or not 1 <= value <= 65535 for value in tunnels)):
                raise ValueError("Authorize 1..16 unique tunnel IDs per PCC")
        self.computer = computer
        self.allowed_tunnels = dict(allowed_tunnels)

    def validate_reply(self, reply: PathReply) -> bool:
        # A refusal does not depend on a TED. An initial path does.
        if reply.topology_digest is None:
            return all(item.get("status") == "DELEGATION_RETURNED" for item in reply.outcomes)
        return self.computer.validate_reply(reply)

    def _request(self, peer: str, report: dict[str, Any], srp_id: int) -> bytes:
        ids = report["identifiers"]
        if (not report["delegate"] or report["removed"] or not report["administrative_up"] or
                report["operational_code"] != 0 or report["ero"] or report["srp_id"] or
                ids["type"] != 18 or ids["all_zero"] or
                ids["sender"] != self.computer.headends.get(peer) or
                ids["tunnel_id"] not in self.allowed_tunnels.get(peer, ())):
            raise ValueError("Only an authorized initial empty-ERO DOWN delegation can request a path")
        if report.get("rro") or report.get("srp_tlvs", []) not in (
                [], [{"type": 28, "value_hex": "00000000"}]):
            raise ValueError("Recorded routes and SRP extensions are unsupported for initial provision")
        for item in report["tlvs"]:
            if item["type"] not in (17, 18, 31):
                raise ValueError("Unsupported delegated LSP extension")
            if item["type"] == 31:
                # Captured IOS XE RSVP Binding SID: opaque existing label;
                # not an instruction to allocate or change it.
                value = bytes.fromhex(item["value_hex"])
                if len(value) != 6 or value[:2] != bytes(2) or int.from_bytes(value[2:], "big") & 0xFFF:
                    raise ValueError("Unsupported Binding SID representation")
        attrs: dict[tuple[int, int], bytes] = {}
        for attr in report["attributes"]:
            key = (attr["class"], attr["type"])
            if key in attrs or key not in {(5, 1), (5, 5), (9, 1), (6, 1)}:
                raise ValueError("Unsupported or duplicate delegated path attribute")
            attrs[key] = bytes.fromhex(attr["body_hex"])
        bandwidth = attrs.get((5, 1))
        lspa = attrs.get((9, 1))
        if bandwidth is None or len(bandwidth) != 4 or lspa is None or len(lspa) != 16:
            raise ValueError("Delegated path requires explicit BANDWIDTH and LSPA")
        amount = struct.unpack("!f", bandwidth)[0]
        if not math.isfinite(amount) or amount <= 0:
            raise ValueError("Delegated bandwidth must be finite and positive")
        if (5, 5) in attrs and attrs[(5, 5)] != bandwidth:
            raise ValueError("Different requested/used bandwidth needs unsupported reoptimization accounting")
        metric = attrs.get((6, 1), bytes((0, 0, 0, 2)) + bytes(4))
        if len(metric) != 8 or metric[3] != 2 or metric[2] & 1:
            raise ValueError("Only unconstrained TE-cost objective is supported for initial delegation")
        return (encode_object(2, struct.pack("!II", 0, srp_id), flags=2) +
                encode_object(4, IPv4Address(ids["sender"]).packed + IPv4Address(ids["endpoint"]).packed, flags=2) +
                encode_object(9, lspa) + encode_object(5, bandwidth) + encode_object(6, metric))

    def __call__(self, peer: str, reports: list[dict[str, Any]], srp_ids: tuple[int, ...]) -> PathReply:
        if (not reports or len(reports) > 8 or len(reports) != len(srp_ids) or
                len(set(srp_ids)) != len(srp_ids) or
                any(type(value) is not int or not 1 <= value < 0xFFFFFFFF for value in srp_ids)):
            raise ValueError("Delegation batch needs 1..8 distinct valid SRP identifiers")
        if any(not report["delegate"] or report["end_of_sync"] or not report["plsp_id"] for report in reports):
            raise ValueError("A live nonzero D=1 delegation is required before any PCUpd")
        messages: list[bytes] = []
        outcomes: list[dict[str, Any]] = []
        topology_digest: str | None = None
        for report, srp_id in zip(reports, srp_ids):
            ids = report["identifiers"]
            details = {"plsp_id": report["plsp_id"], "tunnel_id": ids["tunnel_id"],
                       "sender": ids["sender"], "endpoint": ids["endpoint"], "srp_id": srp_id,
                       "report_key": report_key(report), "authoritative": False, "reservation_created": False}
            try:
                request = self._request(peer, report, srp_id)
                computed = self.computer(peer, request)
                if len(computed.messages) != 1 or computed.outcomes[0]["status"] != "PATH":
                    raise ValueError(computed.outcomes[0]["reason"])
                if topology_digest is not None and topology_digest != computed.topology_digest:
                    raise ValueError("Topology changed between delegated requests")
                if not self.computer.validate_reply(computed):
                    raise ValueError("Topology changed or became unavailable during computation")
                objects = parse_objects(computed.messages[0][4:])
                if computed.messages[0][1] != PCREP:
                    raise ValueError("Initial path computation did not produce PCRep")
                ero = next(obj for obj in objects if obj.object_class == 7)
                bandwidth = next(obj for obj in objects if obj.object_class == 5)
                metric = next(obj for obj in objects if obj.object_class == 6)
                lspa = next(attr for attr in report["attributes"] if attr["class"] == 9)
                message = encode_message(PCUPD, encode_object(33, struct.pack("!II", 0, srp_id)) +
                                         _lsp(report, True) + encode_object(7, ero.body) +
                                         encode_object(9, bytes.fromhex(lspa["body_hex"])) +
                                         encode_object(5, bandwidth.body) + encode_object(6, metric.body))
                topology_digest = computed.topology_digest
                messages.append(message)
                outcomes.append({**computed.outcomes[0], **details, "status": "INITIAL_PATH",
                                 "reason": "Initial delegated path; awaiting PCC RSVP signaling"})
            except (ValueError, RuntimeError, OSError) as exc:
                messages.append(return_delegation(report, srp_id))
                outcomes.append({**details, "status": "DELEGATION_RETURNED", "reason": str(exc)})
        return PathReply(tuple(messages), tuple(outcomes), topology_digest)
