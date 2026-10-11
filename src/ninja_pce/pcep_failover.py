"""One-tunnel, opt-in failure recovery; no optimization or global admission.

Only a previously mapped strict path can establish failure evidence. Preparing
a write durably records uncertainty first. A restart with a pending operation
requires manual recovery; an archived UP report cannot authorize a replay.
"""
from __future__ import annotations

import copy
import math
import struct
import threading
import time
from datetime import datetime
from ipaddress import IPv4Address
from pathlib import Path
from typing import Any, Callable

from .models import Topology
from .pcep import encode_object, parse_reports
from .pcep_compute import PathReply, PcepPathComputer
from .pcep_delegation import DelegatedPathComputer, ownership_key, report_fingerprint
from .reservations import resource_lock
from .state import digest, load_state, write_state

_KIND = "bounded_pcep_failure_recovery"
_OPERATIONS = {0: "DOWN", 1: "UP", 2: "ACTIVE", 3: "GOING_DOWN", 4: "GOING_UP"}


def _positive(value: Any, label: str, maximum: float = 3600) -> float:
    try:
        valid = (not isinstance(value, bool) and isinstance(value, (float, int)) and
                 math.isfinite(value) and 0 < value <= maximum)
    except OverflowError:
        valid = False
    if not valid:
        raise ValueError(f"{label} must be positive and at most {maximum}")
    return float(value)


def _hex(value: Any, maximum: int = 4096) -> bytes:
    if not isinstance(value, str) or len(value) > maximum * 2:
        raise ValueError("Missing or oversized PCEP bytes")
    return bytes.fromhex(value)


def _strict_route(route: Any, *, recorded: bool = False) -> list[str]:
    if not isinstance(route, list) or len(route) > 256:
        raise ValueError("Invalid route list")
    addresses = []
    for hop in route:
        if (not isinstance(hop, dict) or type(hop.get("type")) is not int or hop["type"] != 1 or
                type(hop.get("prefix_length")) is not int or hop["prefix_length"] != 32 or
                (not recorded and hop.get("loose") is not False)):
            raise ValueError("Only strict numbered IPv4/32 routes are recoverable")
        address = str(IPv4Address(hop.get("address")))
        expected = bytes((1, 8)) + IPv4Address(address).packed + bytes((32, 0))
        if "raw_hex" in hop and _hex(hop["raw_hex"]) != expected:
            raise ValueError("Route bytes have unsupported flags or differ from decoded fields")
        addresses.append(address)
    return addresses


def _report(report: dict[str, Any], now: float) -> dict[str, Any]:
    """Validate the supported report completely before transforming observations.

    RRO is observational, not a request constraint. Only an unprotected IPv4
    record consistent with the strict ERO is understood; all other extensions
    are refused. LSPA and requested/used bandwidth remain exact wire values.
    """
    for flag in ("delegate", "administrative_up", "removed", "sync", "end_of_sync"):
        if type(report.get(flag)) is not bool:
            raise ValueError("Missing report flags")
    if (not report["delegate"] or not report["administrative_up"] or report["removed"] or
            report["end_of_sync"] or report.get("stale") is True):
        raise ValueError("Fresh live delegation and administrative UP are required")
    plsp = report.get("plsp_id")
    code = report.get("operational_code")
    if type(plsp) is not int or not 0 < plsp < 0xFFFFF:
        raise ValueError("Invalid scoped PLSP-ID")
    if type(code) is not int or code not in _OPERATIONS or report.get("operational_state") != _OPERATIONS[code]:
        raise ValueError("Unsupported or inconsistent operational state")
    session = report.get("session_id")
    received = report.get("received_at")
    if not isinstance(session, str) or not session or len(session) > 128:
        raise ValueError("Current session identity is required")
    if (isinstance(received, bool) or not isinstance(received, (int, float)) or
            not math.isfinite(received) or received > now + 1):
        raise ValueError("Valid report observation time is required")
    srp = report.get("srp_id")
    if type(srp) is not int or not 0 <= srp <= 0xFFFFFFFF:
        raise ValueError("Invalid report SRP-ID")
    if report.get("srp_tlvs", []) not in ([], [{"type": 28, "value_hex": "00000000"}]):
        raise ValueError("Unsupported SRP constraints")
    tlvs = report.get("tlvs")
    if not isinstance(tlvs, list) or len(tlvs) > 3:
        raise ValueError("Unsupported LSP extensions")
    encoded_tlvs = b""
    seen: set[int] = set()
    for item in tlvs:
        if not isinstance(item, dict) or item.get("type") not in {17, 18, 31} or item["type"] in seen:
            raise ValueError("Unsupported, duplicate or error-bearing LSP extension")
        kind = item["type"]
        seen.add(kind)
        value = _hex(item.get("value_hex"))
        if kind == 31 and (len(value) != 6 or value[:2] != bytes(2) or int.from_bytes(value[2:], "big") & 0xFFF):
            raise ValueError("Unsupported binding SID")
        encoded_tlvs += struct.pack("!HH", kind, len(value)) + value + bytes(-len(value) % 4)
    if 17 not in seen:
        # The session owner validates the initial name and may inherit it for
        # subsequent reports that legitimately omit TLV17 on the wire.
        name = _hex(report.get("symbolic_name_hex"))
        if not name:
            raise ValueError("Validated symbolic name is required")
        encoded_tlvs += struct.pack("!HH", 17, len(name)) + name + bytes(-len(name) % 4)
    if 18 not in seen:
        raise ValueError("Explicit IPv4 identifiers are required")
    ero = _strict_route(report.get("ero"))
    rro = _strict_route(report.get("rro", []), recorded=True)
    attributes = report.get("attributes")
    if not isinstance(attributes, list) or len(attributes) > 5:
        raise ValueError("Unsupported LSP attributes")
    attrs: dict[tuple[int, int], bytes] = {}
    for attr in attributes:
        if not isinstance(attr, dict):
            raise ValueError("Malformed LSP attribute")
        key = (attr.get("class"), attr.get("type"))
        if key not in {(5, 1), (5, 5), (9, 1), (6, 1), (8, 1)} or key in attrs:
            raise ValueError("Unsupported or duplicate LSP constraint")
        attrs[key] = _hex(attr.get("body_hex"))
        if key == (8, 1) and attrs[key] != b"".join(
                bytes((1, 8)) + IPv4Address(address).packed + bytes((32, 0)) for address in rro):
            raise ValueError("RRO bytes differ from the understood record")
    bandwidth = attrs.get((5, 1), b"")
    lspa = attrs.get((9, 1), b"")
    used = attrs.get((5, 5), b"")
    zero_used_down = code == 0 and len(used) == 4 and struct.unpack("!f", used)[0] == 0
    if len(bandwidth) != 4 or (used != bandwidth and not zero_used_down) or len(lspa) != 16:
        raise ValueError("Identical intended/used BANDWIDTH (or zero used while DOWN) and exact LSPA are required")
    _, _, _, setup, hold, flags, _ = struct.unpack("!IIIBBBB", lspa)
    if setup > 7 or hold > 7 or setup < hold or flags & 3:
        raise ValueError("Only valid priorities without protection constraints are supported")
    amount = struct.unpack("!f", bandwidth)[0]
    if not math.isfinite(amount) or amount <= 0:
        raise ValueError("Positive finite bandwidth is required")
    if rro and (8, 1) not in attrs:
        raise ValueError("RRO lacks its recorded-route object")
    # Reparse the identity bytes instead of trusting inconsistent decoded IDs.
    bits = (plsp << 12) | 9 | (code << 4)
    body = encode_object(32, bits.to_bytes(4, "big") + encoded_tlvs) + encode_object(7, b"")
    canonical = parse_reports(body, allow_delegation=True)[0]
    if canonical["identifiers"] != report.get("identifiers") or (
            canonical.get("symbolic_name_hex") != report.get("symbolic_name_hex")):
        raise ValueError("Decoded tunnel identity differs from its wire identity")
    ids = canonical["identifiers"]
    if ids["all_zero"] or ids["type"] != 18 or ids["sender"] == ids["endpoint"]:
        raise ValueError("Distinct, nonzero IPv4 tunnel endpoints are required")
    if rro and rro not in (ero, [ids["sender"]] + ero, ero + [ids["endpoint"]],
                           [ids["sender"]] + ero + [ids["endpoint"]]):
        raise ValueError("Recorded route does not match the understood strict path")
    transformed = copy.deepcopy(report)
    transformed.update(operational_code=0, operational_state="DOWN", ero=[], rro=[], srp_id=0)
    if 17 not in seen:
        transformed["tlvs"].append({"type": 17, "value_hex": report["symbolic_name_hex"]})
    transformed["attributes"] = [attr for attr in transformed["attributes"] if attr["class"] != 8]
    if zero_used_down:
        # Used bandwidth is an observation, not permission to refund capacity.
        # Compute the full unchanged positive requested bandwidth, without
        # fabricating a positive used-bandwidth value for an unsignalled path.
        transformed["attributes"] = [attr for attr in transformed["attributes"]
                                     if (attr["class"], attr["type"]) != (5, 5)]
    # Delegate the remaining constraint validation to the established adapter;
    # this also rejects unsupported protection and metric-bound semantics.
    return {"report": transformed, "ero": ero, "bandwidth_wire": bandwidth.hex(),
            "bandwidth_bps": math.ceil(amount * 8), "lspa_wire": lspa.hex(),
            "ownership_key": list(ownership_key(report)), "plsp_id": plsp,
            "symbolic_name_hex": report["symbolic_name_hex"], "session_id": session}


def _mapped_edges(topology: Topology, addresses: list[str], source: str, destination: str) -> list[str]:
    nodes = {node.loopback: node.id for node in topology.nodes.values()}
    if len(nodes) != len(topology.nodes) or source not in nodes or destination not in nodes or not addresses:
        raise ValueError("Strict path endpoints are not unique known nodes")
    current = nodes[source]
    visited = {current}
    edges = []
    for address in addresses:
        matches = [link for link in topology.directional_links if link.source == current and link.to_ip == address]
        if len(matches) != 1 or not matches[0].admin_up or not matches[0].oper_up:
            raise ValueError("Strict ERO does not identify one usable adjacent directional edge")
        link = matches[0]
        if link.target in visited:
            raise ValueError("Looping strict path is unsupported")
        visited.add(link.target)
        edges.append(link.id)
        current = link.target
    if current != nodes[destination]:
        raise ValueError("Strict ERO does not reach the authorized destination")
    return edges


def _reply_hash(reply: PathReply) -> str:
    return digest({"messages": [message.hex() for message in reply.messages],
                   "outcomes": reply.outcomes, "topology_digest": reply.topology_digest})


def _saved_path(value: dict[str, Any], policy: dict[str, Any]) -> None:
    owner = value.get("ownership_key")
    if (not isinstance(owner, list) or len(owner) != 5 or type(owner[0]) is not int or owner[0] != 18 or
            owner[1] != policy["source"] or type(owner[3]) is not int or owner[3] != policy["tunnel_id"] or
            len(_hex(owner[4])) != 4):
        raise ValueError("Malformed journal tunnel binding")
    IPv4Address(owner[2])
    wire, lspa = _hex(value.get("bandwidth_wire")), _hex(value.get("lspa_wire"))
    if len(wire) != 4 or len(lspa) != 16 or not _hex(value.get("symbolic_name_hex")):
        raise ValueError("Malformed journal constraints")
    bandwidth = struct.unpack("!f", wire)[0]
    if not math.isfinite(bandwidth) or bandwidth <= 0 or value.get("bandwidth_bps") != math.ceil(bandwidth * 8):
        raise ValueError("Malformed journal bandwidth")
    _, _, _, setup, hold, flags, _ = struct.unpack("!IIIBBBB", lspa)
    if setup > 7 or hold > 7 or setup < hold or flags & 3:
        raise ValueError("Unsupported saved LSPA")
    if (type(value.get("plsp_id")) is not int or not 0 < value["plsp_id"] < 0xFFFFF or
            not isinstance(value.get("session_id"), str) or not value["session_id"]):
        raise ValueError("Malformed journal session binding")
    for field in ("ero", "edges"):
        items = value.get(field)
        if (not isinstance(items, list) or not 0 < len(items) <= 256 or
                not all(isinstance(item, str) and 0 < len(item) <= 128 for item in items) or
                len(set(items)) != len(items)):
            raise ValueError("Malformed journal path")
    if len(value["ero"]) != len(value["edges"]):
        raise ValueError("Journal path and edge counts differ")
    for address in value["ero"]:
        IPv4Address(address)
    stamp = value.get("topology_timestamp")
    if not isinstance(stamp, str) or datetime.fromisoformat(stamp.replace("Z", "+00:00")).tzinfo is None:
        raise ValueError("Malformed journal topology time")
    if len(_hex(value.get("topology_digest"))) != 32:
        raise ValueError("Malformed journal topology digest")


class FailureRecoveryPlanner:
    """One explicitly configured tunnel, one pending write, at most three writes.

    observe() requires fresh, synchronized session evidence from the session
    owner. compute() performs graph I/O and belongs in the bounded worker pool.
    These observations provide no authoritative global reservation accounting.
    """

    def __init__(self, computer: PcepPathComputer, *, peer: str, tunnel_id: int, journal_path: Path,
                 debounce_seconds: float = 5, cooldown_seconds: float = 30, max_attempts: int = 3,
                 ack_timeout_seconds: float = 30, clock: Callable[[], float] = time.time):
        self.peer = str(IPv4Address(peer))
        if self.peer not in computer.headends or type(tunnel_id) is not int or not 1 <= tunnel_id <= 65535:
            raise ValueError("One authorized peer and tunnel ID are required")
        if type(max_attempts) is not int or not 1 <= max_attempts <= 3:
            raise ValueError("Failure recovery permits at most three attempts")
        self.debounce = _positive(debounce_seconds, "debounce_seconds")
        self.cooldown = _positive(cooldown_seconds, "cooldown_seconds")
        self.ack_timeout = _positive(ack_timeout_seconds, "ack_timeout_seconds", 300)
        self.max_attempts, self.clock, self.computer = max_attempts, clock, computer
        self.tunnel_id, self.path = tunnel_id, Path(journal_path)
        self.delegated = DelegatedPathComputer(computer, {self.peer: (tunnel_id,)})
        self._lock = threading.RLock()
        self._live: dict[str, Any] | None = None
        self._proposal: dict[str, Any] | None = None
        self._computing = False
        self._epoch = 0
        self._observed_at = 0.0
        policy = {"peer": self.peer, "tunnel_id": tunnel_id, "source": computer.headends[self.peer]}
        if self.path.exists() and (not self.path.is_file() or self.path.stat().st_size > 65536):
            raise ValueError("Failure journal must be a bounded regular file")
        loaded = load_state(self.path)
        self._disk_revision = digest(loaded) if loaded else None
        self._data: dict[str, Any] = loaded or {
            "version": 2, "kind": _KIND, "policy": policy, "status": "WAITING_BASELINE",
            "baseline": None, "pending": None, "attempts": 0, "last_attempt_at": None,
            "failure_since": None, "failure_signature": None, "blocked": False,
        }
        if (self._data.get("kind") != _KIND or self._data.get("policy") != policy or
                type(self._data.get("attempts")) is not int or not 0 <= self._data["attempts"] <= 3 or
                type(self._data.get("blocked")) is not bool):
            raise ValueError("Failure journal does not match this bounded policy")
        for field in ("baseline", "pending"):
            if self._data.get(field) is not None and not isinstance(self._data[field], dict):
                raise ValueError("Malformed failure journal state")
        if self._data.get("baseline") is not None:
            _saved_path(self._data["baseline"], policy)
        pending = self._data.get("pending")
        if pending is not None:
            if not self._data.get("baseline"):
                raise ValueError("Pending recovery lacks an adopted baseline")
            if (type(pending.get("ack_seen")) is not bool or
                    type(pending.get("srp_id")) is not int or not 0 < pending["srp_id"] < 0xFFFFFFFF or
                    pending.get("peer") != self.peer or not isinstance(pending.get("session_id"), str)):
                raise ValueError("Malformed pending recovery binding")
            for field in ("created_at", "deadline"):
                value = pending.get(field)
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                    raise ValueError("Malformed pending recovery time")
            if not 0 < pending["deadline"] - pending["created_at"] <= 300:
                raise ValueError("Unbounded pending recovery deadline")
            outcome = pending.get("outcome")
            if not isinstance(outcome, dict) or outcome.get("status") != "FAILURE_PATH":
                raise ValueError("Malformed pending recovery outcome")
            candidate = {**self._data["baseline"], **pending,
                         "topology_timestamp": outcome.get("topology_timestamp"),
                         "topology_digest": outcome.get("topology_digest")}
            _saved_path(candidate, policy)
            message = _hex(pending.get("message_hex"), 65532)
            if (len(message) < 8 or message[1] != 11 or int.from_bytes(message[2:4], "big") != len(message) or
                    pending.get("reply_hash") != _reply_hash(PathReply((message,), (outcome,), outcome["topology_digest"]))):
                raise ValueError("Pending wire intent is inconsistent")
        if self._data.get("pending") is not None:
            self._data.update(blocked=True, status="RECOVERY_REQUIRED", reason="pending_operation_on_restart")
        else:
            # A persisted path is historical evidence, never a new session's
            # healthy baseline. Adoption must map fresh UP/ACTIVE state again.
            self._data.update(baseline=None, failure_since=None, failure_signature=None)
            if not self._data["blocked"]:
                self._data["status"] = "WAITING_BASELINE"

    def _now(self) -> float:
        result = self.clock()
        if isinstance(result, bool) or not isinstance(result, (int, float)) or not math.isfinite(result):
            raise ValueError("Recovery clock must be finite")
        return float(result)

    def _save(self) -> None:
        with resource_lock(self.path.parent):
            current = load_state(self.path)
            if (digest(current) if current else None) != self._disk_revision:
                self._data.update(blocked=True, status="RECOVERY_REQUIRED", reason="journal_changed_by_another_writer")
                raise ValueError("Failure journal changed; recovery is required")
            write_state(self.path, self._data)
            self._disk_revision = digest(self._data)

    def _timeout(self, now: float) -> None:
        pending = self._data.get("pending")
        if pending and not self._data["blocked"] and (now < pending["created_at"] or now >= pending["deadline"]):
            self._data.update(blocked=True, status="RECOVERY_REQUIRED", reason="pending_report_timeout_or_clock_change")
            self._save()

    def matches(self, peer: str, report: dict[str, Any]) -> bool:
        ids = report.get("identifiers")
        return peer == self.peer and isinstance(ids, dict) and ids.get("tunnel_id") == self.tunnel_id

    def observe(self, peer: str, report: dict[str, Any], *, fresh: bool, synchronized: bool) -> None:
        with self._lock:
            now = self._now()
            self._timeout(now)
            if not self.matches(peer, report):
                return
            if fresh is not True or synchronized is not True:
                self._live = None
                self._data.update(failure_since=None, failure_signature=None)
                if not self._data["blocked"] and not self._data.get("pending"):
                    self._data["status"] = "WAITING_FRESH_REPORT"
                return
            try:
                validated = _report(report, now)
                self.delegated._request(peer, validated["report"], 1)
                if validated["ownership_key"][1] != self.computer.headends[self.peer]:
                    raise ValueError("Report sender is not the authorized headend")
            except (ValueError, TypeError, KeyError, OverflowError) as exc:
                self._live = None
                if self._data.get("pending") or self._data.get("baseline"):
                    self._data.update(blocked=True, status="RECOVERY_REQUIRED", reason=str(exc)[:256])
                    self._save()
                else:
                    self._data.update(status="UNSUPPORTED_REPORT", reason=str(exc)[:256])
                return
            if self._live is None or self._token(report) != self._token(self._live["raw"]):
                self._epoch += 1
            self._live = {"raw": copy.deepcopy(report), "epoch": self._epoch, **validated}
            self._observed_at = now
            baseline, pending = self._data.get("baseline"), self._data.get("pending")
            if baseline and not pending and baseline["session_id"] != validated["session_id"]:
                self._data.update(baseline=None, failure_since=None, failure_signature=None,
                                  status="WAITING_BASELINE")
                baseline = None
                self._save()
            if baseline and any(validated[key] != baseline[key] for key in (
                    "ownership_key", "bandwidth_wire", "lspa_wire", "symbolic_name_hex")):
                self._data.update(blocked=True, status="RECOVERY_REQUIRED", reason="tunnel_identity_or_constraints_changed")
                self._save()
                return
            if pending and not self._data["blocked"]:
                if (report["session_id"] != pending["session_id"] or report["plsp_id"] != pending["plsp_id"]):
                    self.failed("pending_session_or_plsp_changed")
                    return
                if (not report["sync"] and report["received_at"] >= pending["created_at"] and
                        report["srp_id"] == pending["srp_id"] and not pending["ack_seen"]):
                    pending["ack_seen"] = True
                    self._save()
                if report["operational_code"] in {1, 2} and not report["sync"] and pending["ack_seen"] and (
                        report["received_at"] >= pending["created_at"] and report["srp_id"] in {0, pending["srp_id"]}):
                    if validated["ero"] != pending["ero"]:
                        if report["srp_id"] == pending["srp_id"]:
                            self.failed("terminal_report_has_wrong_ero")
                    else:
                        self._data.update(baseline={**validated, "report": None, "edges": pending["edges"]},
                                          pending=None, failure_since=None, failure_signature=None,
                                          status="RECOVERED", reason="matching_pcc_up_report")
                        self._data["baseline"].update(
                            topology_timestamp=pending["outcome"]["topology_timestamp"],
                            topology_digest=pending["outcome"]["topology_digest"],
                        )
                        self._proposal = None
                        self._save()

    def compute(self, peer: str, report: dict[str, Any], srp_id: int) -> PathReply | None:
        if type(srp_id) is not int or not 1 <= srp_id < 0xFFFFFFFF:
            raise ValueError("A nonzero usable SRP-ID is required")
        with self._lock:
            now = self._now()
            self._timeout(now)
            if (self._data["blocked"] or self._data.get("pending") or self._computing or
                    not self._live or not self.matches(peer, report) or
                    now - self._observed_at < 0 or now - self._observed_at > self.computer.max_age_seconds or
                    self._token(report) != self._token(self._live["raw"])):
                return None
            live, baseline = copy.deepcopy(self._live), copy.deepcopy(self._data.get("baseline"))
            self._computing = True
        try:
            topology, topology_digest = self.computer.validated_snapshot()
            timestamp = topology.raw_metadata["timestamp"]
            observed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            with self._lock:
                previous_stamp = self._data.get("last_graph_timestamp")
                previous_time = datetime.fromisoformat(previous_stamp.replace("Z", "+00:00")) if previous_stamp else None
                if previous_time and (observed < previous_time or (observed == previous_time and
                        topology_digest != self._data["last_graph_digest"])):
                    self._data.update(status="TOPOLOGY_REPLAY", failure_since=None, failure_signature=None)
                    return None
                self._data.update(last_graph_timestamp=timestamp, last_graph_digest=topology_digest)
            if baseline is None:
                if report["operational_code"] not in {1, 2}:
                    return None
                edges = _mapped_edges(topology, live["ero"], live["ownership_key"][1], live["ownership_key"][2])
                with self._lock:
                    if self._unchanged(live):
                        self._data.update(baseline={**live, "raw": None, "report": None, "edges": edges},
                                          status="MONITORING", reason="explicit_existing_tunnel_adoption")
                        self._data["baseline"].update(topology_timestamp=timestamp, topology_digest=topology_digest)
                        self._save()
                return None
            if live["ero"] != baseline["ero"] and not (report["operational_code"] == 0 and not live["ero"]):
                with self._lock:
                    self._data.update(blocked=True, status="RECOVERY_REQUIRED", reason="observed_path_changed_outside_recovery")
                    self._save()
                return None
            links = {link.id: link for link in topology.directional_links}
            failed = [edge for edge in baseline["edges"] if edge not in links or
                      not links[edge].admin_up or not links[edge].oper_up]
            with self._lock:
                if not self._unchanged(live):
                    return None
                if not failed:
                    if self._data.get("failure_since") is not None or timestamp != baseline["topology_timestamp"]:
                        self._data["baseline"].update(topology_timestamp=timestamp, topology_digest=topology_digest)
                        self._data.update(failure_since=None, failure_signature=None, status="MONITORING")
                        self._save()
                    return None
                if observed <= datetime.fromisoformat(baseline["topology_timestamp"].replace("Z", "+00:00")):
                    self._data.update(status="TOPOLOGY_REPLAY", failure_since=None, failure_signature=None)
                    return None
                signature = digest(failed)
                if self._data.get("failure_signature") != signature:
                    self._data.update(failure_since=now, failure_signature=signature, status="DEBOUNCING")
                    self._save()
                    return None
                if now - self._data["failure_since"] < self.debounce:
                    return None
                if self._data["attempts"] >= self.max_attempts:
                    self._data["status"] = "BUDGET_EXHAUSTED"
                    return None
                last = self._data.get("last_attempt_at")
                if last is not None and not self.cooldown <= now - last:
                    self._data["status"] = "COOLDOWN"
                    return None
            reply = self.delegated(peer, [live["report"]], (srp_id,))
            if (len(reply.messages) != 1 or len(reply.outcomes) != 1 or
                    reply.outcomes[0].get("status") != "INITIAL_PATH" or
                    reply.topology_digest != topology_digest or not self.computer.validate_reply(reply)):
                with self._lock:
                    self._data.update(status="NO_REPLACEMENT", reason="no_fresh_admissible_replacement")
                return None
            selected = _mapped_edges(topology, reply.outcomes[0]["ero"],
                                     live["ownership_key"][1], live["ownership_key"][2])
            if selected != reply.outcomes[0].get("path_link_ids") or set(selected) & set(failed):
                return None
            outcome = {**reply.outcomes[0], "status": "FAILURE_PATH", "reason": "Stable failure of adopted path",
                       "failed_edge_ids": failed, "previous_edge_ids": baseline["edges"],
                       "topology_digest": reply.topology_digest}
            result = PathReply(reply.messages, (outcome,), reply.topology_digest)
            with self._lock:
                if not self._unchanged(live):
                    return None
                self._proposal = {"hash": _reply_hash(result), "token": self._token(report),
                                  "epoch": self._epoch, "reply": result}
                self._data["status"] = "READY"
                return result
        except (ValueError, OSError, RuntimeError, RecursionError):
            with self._lock:
                if not self._data["blocked"]:
                    self._data.update(status="TOPOLOGY_UNAVAILABLE", failure_since=None, failure_signature=None)
            return None
        finally:
            with self._lock:
                self._computing = False

    @staticmethod
    def _token(report: dict[str, Any]) -> str:
        return digest([report.get("session_id"), report_fingerprint(report)])

    def _unchanged(self, live: dict[str, Any]) -> bool:
        return bool(self._live and not self._data["blocked"] and not self._data.get("pending") and
                    self._live["epoch"] == live["epoch"] and
                    self._token(self._live["raw"]) == self._token(live["raw"]))

    def prepare_send(self, peer: str, report: dict[str, Any], reply: PathReply) -> None:
        with self._lock:
            now = self._now()
            self._timeout(now)
            if (not self.matches(peer, report) or not self._live or not self._proposal or
                    self._data["blocked"] or self._data.get("pending") or
                    self._data["attempts"] >= self.max_attempts or
                    not 0 <= now - self._observed_at <= self.computer.max_age_seconds or
                    self._token(report) != self._token(self._live["raw"]) or
                    self._proposal["token"] != self._token(report) or
                    self._proposal["epoch"] != self._epoch or
                    self._proposal["hash"] != _reply_hash(reply)):
                raise ValueError("Recovery proposal no longer matches live authority")
        if not self.computer.validate_reply(reply):
            raise ValueError("Recovery topology changed before delivery")
        with self._lock:
            now = self._now()
            if (not self._live or not self._proposal or
                    self._proposal["hash"] != _reply_hash(reply) or self._proposal["epoch"] != self._epoch or
                    self._token(report) != self._token(self._live["raw"]) or
                    not 0 <= now - self._observed_at <= self.computer.max_age_seconds or
                    self._data["attempts"] >= self.max_attempts or
                    self._data["blocked"] or self._data.get("pending")):
                raise ValueError("Recovery authority changed before persistence")
            outcome = reply.outcomes[0]
            self._data["pending"] = {
                "created_at": now, "deadline": now + self.ack_timeout, "peer": peer,
                "session_id": report["session_id"], "plsp_id": report["plsp_id"],
                "ownership_key": list(ownership_key(report)), "srp_id": outcome["srp_id"],
                "ack_seen": False,
                "ero": outcome["ero"], "edges": outcome["path_link_ids"],
                "bandwidth_bps": outcome["bandwidth_bps"], "reply_hash": _reply_hash(reply),
                "outcome": copy.deepcopy(outcome), "message_hex": reply.messages[0].hex(),
            }
            self._data.update(attempts=self._data["attempts"] + 1, last_attempt_at=now,
                              status="AWAITING_REPORT", reason="durable_intent_before_network_write")
            self._proposal = None
            try:
                self._save()
            except (ValueError, OSError):
                self._data.update(blocked=True, status="RECOVERY_REQUIRED", reason="pending_persistence_failed")
                raise

    def failed(self, reason: str) -> None:
        with self._lock:
            before = digest(self._data)
            self._live, self._proposal = None, None
            self._epoch += 1
            self._data.update(failure_since=None, failure_signature=None, reason=str(reason)[:256])
            if self._data.get("pending"):
                self._data.update(blocked=True, status="RECOVERY_REQUIRED")
                if digest(self._data) != before:
                    self._save()
            elif not self._data["blocked"]:
                self._data.update(status="WAITING_FRESH_REPORT", baseline=None)
                if digest(self._data) != before:
                    self._save()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            self._timeout(self._now())
            return copy.deepcopy({**self._data, "computing": self._computing,
                                  "reservation_created": False, "authoritative": False})
