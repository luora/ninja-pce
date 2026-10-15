"""Durable, conservative admission for explicitly enabled lab PCInitiate.

This module never opens a socket, retries a send, or calls PCRpt forwarding
verification. The session owner must fence and authorize the writer immediately
before ``mark_sending`` and send only after that durable boundary succeeds.
"""
from __future__ import annotations

import copy
import math
import re
import struct
import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from ipaddress import IPv4Address
from pathlib import Path
from typing import Any

from .cspf import compute_cspf
from .models import PathConstraints, ServiceDemand, validate_identifier
from .pcep_compute import PcepPathComputer
from .pcep_initiation import encoded_bandwidth
from .reservations import ReservationLedger, resource_lock
from .state import digest, load_state, write_state

_HEX = re.compile(r"[0-9a-f]{64}\Z")
_ID = re.compile(r"[0-9a-f]{32}\Z")
_KEY = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")
_STATES = {"PLANNED", "PUSHING", "APPLIED_UNVERIFIED", "ROLLBACK_REQUIRED", "REJECTED", "ROLLED_BACK"}
_RELEASED = {"REJECTED", "ROLLED_BACK"}
_OPERATIONS = {0: "DOWN", 1: "UP", 2: "ACTIVE", 3: "GOING_DOWN", 4: "GOING_UP"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _stamp(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ValueError("A timezone-aware operation timestamp is required")
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("A timezone-aware operation timestamp is required")
    return result.astimezone(timezone.utc)


def _principal(value: str) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 128 or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError("A bounded authenticated principal is required")
    return value


def _plan_fields(operation: dict[str, Any]) -> dict[str, Any]:
    fields = ("id", "intent", "principal", "idempotency_scope", "policy_digest", "peer", "symbolic_name",
              "source_ip", "destination_ip", "bandwidth_bps", "bandwidth_wire", "setup_priority",
              "hold_priority", "ero", "path_edges", "path_nodes", "topology_digest", "expires_at")
    return {key: operation[key] for key in fields}


class CreationManager:
    """A shared ledger admits at most one nonreleased managed lab creation.

    ``plan`` and lifecycle methods return detached operation dictionaries.
    ``validate_plan`` returns True or raises before a write. An operation with a
    persisted send boundary can never enter that boundary a second time, even
    after process restart. Unknown outcomes retain all directed resources.
    """

    def __init__(self, computer: PcepPathComputer, state_dir: Path, headends: dict[str, str],
                 allowed_destinations: tuple[str, ...], max_bandwidth_bps: int = 1_000_000,
                 max_operations: int = 16):
        if not headends or len(headends) > 16 or len(set(headends.values())) != len(headends):
            raise ValueError("Creation requires bounded, distinct authorized headends")
        self.headends = {validate_identifier(name): str(IPv4Address(peer)) for name, peer in headends.items()}
        names = {name: address for address, name in computer.node_names.items()}
        if any(name not in names or computer.headends.get(peer) != names[name]
               for name, peer in self.headends.items()):
            raise ValueError("Creation headends must match the configured path computer")
        if (not isinstance(allowed_destinations, tuple) or not allowed_destinations or
                len(allowed_destinations) > 16 or len(set(allowed_destinations)) != len(allowed_destinations)):
            raise ValueError("Creation destinations must be a bounded unique tuple")
        for name in allowed_destinations:
            validate_identifier(name)
            if name not in names:
                raise ValueError("Creation destination is not a configured topology node")
        if type(max_bandwidth_bps) is not int or not 0 < max_bandwidth_bps <= 10**12:
            raise ValueError("Creation bandwidth policy must be a positive bounded integer")
        if type(max_operations) is not int or not 1 <= max_operations <= 16:
            raise ValueError("Creation history must be bounded to at most 16 operations")
        self.computer, self.state_dir = computer, Path(state_dir)
        self.allowed_destinations = allowed_destinations
        self.max_bandwidth_bps, self.max_operations = max_bandwidth_bps, max_operations
        self.path = self.state_dir / ".ninja-pce-creations.json"
        self.policy_digest = digest({"headends": self.headends, "destinations": list(allowed_destinations),
                                     "max_bandwidth_bps": max_bandwidth_bps, "max_outstanding": 1})

    def _load(self) -> tuple[dict[str, Any], ReservationLedger]:
        journal = load_state(self.path)
        ledger = ReservationLedger(self.state_dir)
        if not journal:
            journal = {"kind": "lab_pcep_creations", "operations": {}, "reservation_id": "creation-journal"}
        operations = journal.get("operations")
        if (journal.get("kind") != "lab_pcep_creations" or not isinstance(operations, dict) or
                len(operations) > self.max_operations):
            raise ValueError("Malformed or oversized creation journal; explicit recovery is required")
        scopes: set[str] = set()
        owners: set[str] = set()
        for op_id, op in operations.items():
            if (not isinstance(op_id, str) or not _ID.fullmatch(op_id) or not isinstance(op, dict) or
                    op.get("id") != op_id or op.get("status") not in _STATES or
                    op.get("symbolic_name") != f"NINJA_CREATE_{op_id}" or
                    op.get("owner") != f"creation:{op_id}"):
                raise ValueError("Malformed creation operation ownership")
            try:
                _principal(op["principal"])
                scope = op["idempotency_scope"]
                if not isinstance(scope, str) or not _HEX.fullmatch(scope) or scope in scopes:
                    raise ValueError("Malformed creation idempotency scope")
                scopes.add(scope)
                owners.add(op["owner"])
                if op.get("plan_digest") != digest(_plan_fields(op)):
                    raise ValueError("Creation plan digest differs from durable intent")
                if op.get("intent_digest") != digest(op["intent"]):
                    raise ValueError("Creation intent digest is invalid")
                _stamp(op["created_at"])
                _stamp(op["expires_at"])
                entry = ledger.entries.get(op["reservation_id"])
                if op["status"] not in _RELEASED and entry is None:
                    raise ValueError("Creation hold is missing; explicit recovery is required")
                if entry is not None and (entry["owner"] != op["owner"] or entry["plan_id"] != op["plan_digest"] or
                        entry["current_edges"] != op["path_edges"] or
                        entry["current_bandwidth_bps"] != op["bandwidth_bps"]):
                    raise ValueError("Creation hold differs from its durable plan")
                send = op.get("send")
                if send is not None and (not isinstance(send, dict) or not isinstance(send.get("session_id"), str) or
                        not send["session_id"] or type(send.get("srp_id")) is not int or
                        not 0 < send["srp_id"] < 0xFFFFFFFF or
                        not isinstance(send.get("wire_digest"), str) or not _HEX.fullmatch(send["wire_digest"])):
                    raise ValueError("Malformed durable creation send boundary")
                if op["status"] in {"PUSHING", "APPLIED_UNVERIFIED", "ROLLBACK_REQUIRED"} and send is None:
                    raise ValueError("Creation delivery state has no durable send boundary")
                # SRP identifiers are scoped to a PCEP session. Reconciliation
                # preserves the original send audit while binding ownership to
                # a new session, whose sequence may start below the old one.
                owner_session = send["session_id"] if send else None
                owner_sessions = {owner_session} if owner_session is not None else set()
                bindings = op.get("session_bindings", [])
                if not isinstance(bindings, list) or len(bindings) > 4:
                    raise ValueError("Malformed durable ownership session history")
                for binding in bindings:
                    if (not isinstance(binding, dict) or binding.get("previous") != owner_session or
                            not isinstance(binding.get("current"), str) or
                            not 1 <= len(binding["current"]) <= 128 or
                            not isinstance(binding.get("report_digest"), str) or
                            not _HEX.fullmatch(binding["report_digest"])):
                        raise ValueError("Malformed durable ownership session history")
                    _stamp(binding["observed_at"])
                    owner_session = binding["current"]
                    owner_sessions.add(owner_session)
                if op.get("owner_session", send["session_id"] if send else None) != owner_session:
                    raise ValueError("Durable ownership session differs from its reconciliation history")
                cleanup = op.get("cleanup")
                if cleanup is not None and (not isinstance(cleanup, dict) or send is None or
                        cleanup.get("session_id") not in owner_sessions or type(cleanup.get("srp_id")) is not int or
                        not 0 < cleanup["srp_id"] < 0xFFFFFFFF or
                        (cleanup["session_id"] == send["session_id"] and cleanup["srp_id"] <= send["srp_id"]) or
                        (op.get("completion") and op["completion"]["session_id"] == cleanup["session_id"] and
                         cleanup["srp_id"] <= op["completion"]["srp_id"]) or
                        not isinstance(cleanup.get("wire_digest"), str) or not _HEX.fullmatch(cleanup["wire_digest"])):
                    raise ValueError("Malformed durable owned cleanup boundary")
            except (KeyError, TypeError) as exc:
                raise ValueError("Incomplete creation journal; explicit recovery is required") from exc
        if any(entry["owner"].startswith("creation:") and entry["owner"] not in owners
               for entry in ledger.entries.values()):
            raise ValueError("Orphan creation hold; recover the journal before creating another tunnel")
        return journal, ledger

    def _save(self, journal: dict[str, Any]) -> None:
        write_state(self.path, journal)

    @staticmethod
    def _operation(journal: dict[str, Any], op_id: str) -> dict[str, Any]:
        if not isinstance(op_id, str) or not _ID.fullmatch(op_id) or op_id not in journal["operations"]:
            raise ValueError("Unknown creation operation")
        return journal["operations"][op_id]

    def get(self, op_id: str, principal: str | None = None) -> dict[str, Any]:
        with resource_lock(self.state_dir):
            journal, _ = self._load()
            op = self._operation(journal, op_id)
            if principal is not None and op["principal"] != _principal(principal):
                raise ValueError("Unknown creation operation")
            return copy.deepcopy(op)

    def list(self, principal: str | None = None) -> list[dict[str, Any]]:
        if principal is not None:
            _principal(principal)
        with resource_lock(self.state_dir):
            journal, _ = self._load()
            return copy.deepcopy([op for op in journal["operations"].values()
                                  if principal is None or op["principal"] == principal])

    def plan(self, principal: str, key: str, headend: str, destination: str,
             bandwidth_bps: int) -> dict[str, Any]:
        _principal(principal)
        if not isinstance(key, str) or not _KEY.fullmatch(key):
            raise ValueError("A bounded idempotency key is required")
        if headend not in self.headends or destination not in self.allowed_destinations or headend == destination:
            raise ValueError("Creation endpoints are outside the authorized lab policy")
        if type(bandwidth_bps) is not int or not 0 < bandwidth_bps <= self.max_bandwidth_bps:
            raise ValueError("Creation bandwidth exceeds the authorized policy")
        bandwidth_wire, actual_bps = encoded_bandwidth(bandwidth_bps)
        if actual_bps > self.max_bandwidth_bps:
            raise ValueError("Encoded creation bandwidth exceeds the authorized policy")
        intent = {"headend": headend, "destination": destination, "bandwidth_bps": bandwidth_bps}
        scope, intent_digest = digest([principal, key]), digest(intent)
        with resource_lock(self.state_dir):
            journal, ledger = self._load()
            for existing in journal["operations"].values():
                if existing["idempotency_scope"] == scope:
                    if existing["intent_digest"] != intent_digest:
                        raise ValueError("Idempotency key conflicts with a different creation intent")
                    return copy.deepcopy(existing)
            if len(journal["operations"]) >= self.max_operations:
                raise ValueError("Creation operation history is full; explicit archival is required")
            if any(op["status"] not in _RELEASED for op in journal["operations"].values()):
                raise ValueError("Another managed creation remains held or unresolved")
            topology, topology_digest = self.computer.validated_snapshot()
            if headend not in topology.nodes or destination not in topology.nodes:
                raise ValueError("Creation endpoints are absent from the fresh topology")
            source_ip = str(IPv4Address(topology.nodes[headend].loopback))
            destination_ip = str(IPv4Address(topology.nodes[destination].loopback))
            if self.computer.headends[self.headends[headend]] != source_ip:
                raise ValueError("Observed source differs from the authorized PCC")
            if len({node.loopback for node in topology.nodes.values()}) != len(topology.nodes):
                raise ValueError("Creation endpoints are ambiguous in the fresh topology")
            op_id = uuid.uuid4().hex
            symbolic_name, owner = f"NINJA_CREATE_{op_id}", f"creation:{op_id}"
            reservation_id = f"{source_ip}:pcep-initiate:{symbolic_name}"
            # This live BGP-LS feed does not prove owned RSVP debits. Keep every
            # existing hold fully charged, even if a feed claims self-credit.
            conservative = replace(topology, raw_metadata={**topology.raw_metadata, "authoritative": False})
            effective = ledger.effective_topology(conservative, owner)
            demand = ServiceDemand(symbolic_name, headend, destination, actual_bps / 1_000_000,
                                   reservation_bw_bps=actual_bps, constraints=PathConstraints(max_candidates=1))
            result = compute_cspf(effective, demand)
            ero = []
            for edge in result.path_links:
                if edge.from_ip is None or edge.to_ip is None:
                    raise ValueError("Creation requires numbered, strict IPv4 TE links")
                IPv4Address(edge.from_ip)
                address = str(IPv4Address(edge.to_ip))
                if sum(link.source == edge.source and link.to_ip == address
                       for link in topology.directional_links) != 1:
                    raise ValueError("Creation ERO does not identify a unique directed TE resource")
                ero.append(address)
            now = _now()
            op: dict[str, Any] = {"id": op_id, "principal": principal, "idempotency_scope": scope,
                  "intent": intent, "intent_digest": intent_digest, "policy_digest": self.policy_digest,
                  "status": "PLANNED", "peer": self.headends[headend], "symbolic_name": symbolic_name,
                  "source_ip": source_ip, "destination_ip": destination_ip, "owner": owner,
                  "reservation_id": reservation_id, "bandwidth_bps": actual_bps,
                  "bandwidth_wire": bandwidth_wire.hex(), "setup_priority": 7, "hold_priority": 7,
                  "ero": ero, "path_edges": [edge.id for edge in result.path_links],
                  "path_nodes": list(result.path_nodes), "topology_digest": topology_digest,
                  "created_at": now.isoformat(), "updated_at": now.isoformat(),
                  "expires_at": (now + timedelta(seconds=min(self.computer.max_age_seconds, 30))).isoformat(),
                  "send": None, "reported": None, "cleanup": None, "verified": False}
            op["plan_digest"] = digest(_plan_fields(op))
            # A crash between these writes leaves an orphan hold, which blocks
            # further creation instead of losing admission/idempotency evidence.
            ledger.reserve(reservation_id, owner, result, op["plan_digest"], actual_bps)
            journal["operations"][op_id] = op
            self._save(journal)
            return copy.deepcopy(op)

    def _validate_plan(self, op: dict[str, Any], ledger: ReservationLedger) -> None:
        if op["status"] != "PLANNED" or op.get("send") is not None:
            raise ValueError("Creation has already crossed or left the send boundary; never replay it")
        if ledger.entries[op["reservation_id"]]["status"] != "PLANNED":
            raise ValueError("Creation ledger has an unresolved send boundary")
        if op["policy_digest"] != self.policy_digest or _now() >= _stamp(op["expires_at"]):
            raise ValueError("Creation authorization policy changed or the plan expired")
        _, current_digest = self.computer.validated_snapshot()
        if current_digest != op["topology_digest"]:
            raise ValueError("Fresh topology changed; reject and explicitly recompute before sending")

    def validate_plan(self, op_id: str) -> bool:
        with resource_lock(self.state_dir):
            journal, ledger = self._load()
            self._validate_plan(self._operation(journal, op_id), ledger)
            return True

    def mark_sending(self, op_id: str, session_id: str, srp_id: int, wire_digest: str) -> dict[str, Any]:
        if (not isinstance(session_id, str) or not 1 <= len(session_id) <= 128 or
                type(srp_id) is not int or not 0 < srp_id < 0xFFFFFFFF or
                not isinstance(wire_digest, str) or not _HEX.fullmatch(wire_digest)):
            raise ValueError("A current session, nonreserved SRP and exact wire digest are required")
        with resource_lock(self.state_dir):
            journal, ledger = self._load()
            op = self._operation(journal, op_id)
            self._validate_plan(op, ledger)
            timestamp = _now().isoformat()
            ledger.transition(op["reservation_id"], "PUSHING")
            op.update(status="PUSHING", updated_at=timestamp,
                      send={"session_id": session_id, "srp_id": srp_id,
                            "wire_digest": wire_digest, "attempted_at": timestamp})
            self._save(journal)
            return copy.deepcopy(op)

    @staticmethod
    def _owned_report(op: dict[str, Any], report: dict[str, Any], *, removal: bool = False) -> dict[str, Any]:
        send = op.get("send")
        owner_session = op.get("owner_session", send["session_id"] if send else None)
        if not send or not isinstance(report, dict) or report.get("session_id") != owner_session:
            raise ValueError("Creation report belongs to another or unknown session")
        received = report.get("received_at")
        if (isinstance(received, bool) or not isinstance(received, (int, float)) or not math.isfinite(received) or
                not _stamp(send["attempted_at"]).timestamp() <= received <= _now().timestamp() + 1 or
                _now().timestamp() - received > 30 or
                report.get("sync") is not False or report.get("stale") is True or
                report.get("end_of_sync") is not False or report.get("created") is not True or
                report.get("removed") is not removal):
            raise ValueError("A fresh, nonsynchronization owned creation report is required")
        if (report.get("symbolic_name_hex") != op["symbolic_name"].encode().hex() or
                type(report.get("plsp_id")) is not int or not 0 < report["plsp_id"] < 0xFFFFF):
            raise ValueError("Creation report symbolic name or allocated PLSP-ID differs")
        ids = report.get("identifiers")
        if not isinstance(ids, dict) or ids.get("type") != 18:
            raise ValueError("Creation requires a full IPv4 RSVP LSP identity")
        try:
            raw = bytes.fromhex(ids["value_hex"])
            if len(raw) != 16:
                raise ValueError("Wrong RSVP identity length")
            canonical = {"type": 18, "sender": str(IPv4Address(raw[:4])),
                         "lsp_id": int.from_bytes(raw[4:6], "big"), "tunnel_id": int.from_bytes(raw[6:8], "big"),
                         "extended_tunnel_id_hex": raw[8:12].hex(), "endpoint": str(IPv4Address(raw[12:])),
                         "all_zero": not any(raw), "value_hex": raw.hex()}
        except (KeyError, TypeError) as exc:
            raise ValueError("Malformed RSVP identity bytes") from exc
        if (ids != canonical or ids["sender"] != op["source_ip"] or ids["endpoint"] != op["destination_ip"] or
                ids["all_zero"] or not ids["tunnel_id"]):
            raise ValueError("Creation report endpoints or full identity bytes differ")
        tlvs = report.get("tlvs")
        if not isinstance(tlvs, list) or len(tlvs) > 3:
            raise ValueError("Unsupported creation report LSP extensions")
        seen: set[int] = set()
        for item in tlvs:
            if not isinstance(item, dict) or item.get("type") not in {17, 18, 31} or item["type"] in seen:
                raise ValueError("Unsupported, duplicate or error-bearing creation report extension")
            kind = item["type"]
            seen.add(kind)
            value_hex = item.get("value_hex")
            if not isinstance(value_hex, str) or len(value_hex) > 128:
                raise ValueError("Malformed creation report extension value")
            value = bytes.fromhex(value_hex)
            if ((kind == 17 and value != op["symbolic_name"].encode()) or
                    (kind == 18 and value != raw) or
                    (kind == 31 and (len(value) != 6 or value[:2] != bytes(2) or
                                    int.from_bytes(value[2:], "big") & 0xFFF))):
                raise ValueError("Creation report extension contradicts the owned identity or supported profile")
        bound = op.get("owned_candidate") or op.get("reported")
        if 18 not in seen or (bound is None and 17 not in seen):
            raise ValueError("Initial creation report requires explicit owned name and RSVP identifiers")
        identity = {"plsp_id": report["plsp_id"], "identifiers": canonical,
                    "symbolic_name_hex": report["symbolic_name_hex"]}
        if bound is not None and identity != bound["identity"]:
            previous = bound["identity"]
            completion = op.get("completion")
            # RSVP make-before-break may assign a new LSP-ID. Only the exact
            # outstanding completion SRP can bind it to the same owned tunnel.
            same_tunnel = (identity["plsp_id"] == previous["plsp_id"] and
                           identity["symbolic_name_hex"] == previous["symbolic_name_hex"] and
                           all(canonical[key] == previous["identifiers"][key] for key in
                               ("type", "sender", "endpoint", "tunnel_id", "extended_tunnel_id_hex")))
            if (removal or not same_tunnel or not completion or completion.get("acknowledged") or
                    completion["session_id"] != owner_session or report.get("srp_id") != completion["srp_id"] or
                    received < _stamp(completion["attempted_at"]).timestamp() or not canonical["lsp_id"]):
                raise ValueError("Creation report is not the previously bound full LSP identity")
        if not removal:
            allowed_srp = {send["srp_id"]} if bound is None else {0}
            if owner_session == send["session_id"]:
                allowed_srp.add(send["srp_id"])
            completion = op.get("completion")
            if completion and completion["session_id"] == owner_session:
                allowed_srp.add(completion["srp_id"])
            if (report.get("srp_id") not in allowed_srp or report.get("delegate") is not True or
                    report.get("administrative_up") is not True or
                    report.get("srp_tlvs", []) not in ([], [{"type": 28, "value_hex": "00000000"}])):
                raise ValueError("Creation report lacks exact correlation, delegation or RSVP setup type")
        return identity

    def bind_owned_candidate(self, op_id: str, report: dict[str, Any]) -> dict[str, Any]:
        """Record exact ownership separately from successful attribute application.

        A PCC can allocate the requested identity while ignoring bandwidth. That
        permits bounded owned cleanup, never an APPLIED or VERIFIED claim.
        """
        with resource_lock(self.state_dir):
            journal, _ = self._load()
            op = self._operation(journal, op_id)
            if op["status"] not in {"PUSHING", "APPLIED_UNVERIFIED", "ROLLBACK_REQUIRED"}:
                raise ValueError("Creation is not awaiting owned identity reconciliation")
            identity = self._owned_report(op, report)
            previous = op.get("owned_candidate") or op.get("reported")
            if previous and previous["identity"] != identity:
                history = op.setdefault("identity_history", [])
                if len(history) >= 4:
                    raise ValueError("Owned RSVP incarnation history is full")
                history.append(previous["identity"])
            op["owned_candidate"] = {"identity": identity, "report_digest": digest(report),
                                     "received_at": report["received_at"]}
            op["updated_at"] = _now().isoformat()
            self._save(journal)
            return copy.deepcopy(op)

    def confirmed_absence(self, op_id: str, pcc_state: dict[str, Any], evidence: dict[str, Any], *,
                          resources_released: bool = False) -> dict[str, Any]:
        """Private manual recovery after independent headend/transit absence proof.

        No HTTP/control route exposes this method. The operator must check all
        RSVP reservations and supply durable evidence hashes. A missing PCRpt
        or browser node alone is insufficient to release a hold.
        """
        with resource_lock(self.state_dir):
            journal, ledger = self._load()
            op = self._operation(journal, op_id)
            proofs = evidence.get("router_reservations", {})
            reports = pcc_state.get("reports")
            now = _now().timestamp()
            if (resources_released is not True or op.get("send") is None or op["status"] in _RELEASED or
                    pcc_state.get("peer_ip") != op["peer"] or pcc_state.get("state") != "ESTABLISHED" or
                    pcc_state.get("fresh") is not True or pcc_state.get("synchronized") is not True or
                    not isinstance(reports, dict) or not isinstance(pcc_state.get("valid_until"), (int, float)) or
                    pcc_state["valid_until"] <= now or any(
                        r.get("symbolic_name_hex") == op["symbolic_name"].encode().hex() for r in reports.values()) or
                    not isinstance(proofs, dict) or not set(op["path_nodes"]) <= set(proofs) or
                    any(not isinstance(value, str) or not _HEX.fullmatch(value) for value in proofs.values()) or
                    not isinstance(evidence.get("headend_configuration"), str) or
                    not _HEX.fullmatch(evidence["headend_configuration"]) or
                    not isinstance(evidence.get("observed_at"), (int, float)) or
                    not now - 60 <= evidence["observed_at"] <= now + 1):
                raise ValueError("Fresh synchronized PCC absence and independent reservation-release evidence are required")
            op.update(status="ROLLED_BACK", updated_at=_now().isoformat(),
                      absence={"pcc_digest": digest(pcc_state), "evidence": copy.deepcopy(evidence),
                               "resources_released": True})
            self._save(journal)
            ledger.data["entries"].pop(op["reservation_id"], None)
            ledger.save()
            return copy.deepcopy(op)

    def reconcile_owned(self, op_id: str, report: dict[str, Any]) -> dict[str, Any]:
        """Explicit private reconciliation of an unchanged identity after restart.

        The session service separately requires fresh completed synchronization.
        No create, update, reclaim or delete is sent by this method.
        """
        with resource_lock(self.state_dir):
            journal, _ = self._load()
            op = self._operation(journal, op_id)
            bound = op.get("owned_candidate") or op.get("reported")
            identity = {"plsp_id": report.get("plsp_id"), "identifiers": report.get("identifiers"),
                        "symbolic_name_hex": report.get("symbolic_name_hex")}
            session = report.get("session_id")
            received = report.get("received_at")
            if (op["status"] not in {"APPLIED_UNVERIFIED", "ROLLBACK_REQUIRED"} or not bound or
                    identity != bound["identity"] or not isinstance(session, str) or not 1 <= len(session) <= 128 or
                    report.get("created") is not True or report.get("delegate") is not True or
                    report.get("removed") is not False or report.get("end_of_sync") is not False or
                    report.get("stale") is True or not isinstance(received, (float, int)) or
                    not _now().timestamp() - 30 <= received <= _now().timestamp() + 1 or
                    report.get("srp_tlvs", []) not in ([], [{"type": 28, "value_hex": "00000000"}])):
                raise ValueError("Reconciliation requires a fresh unchanged created and delegated owned identity")
            previous = op.get("owner_session", op["send"]["session_id"])
            if previous != session:
                history = op.setdefault("session_bindings", [])
                if len(history) >= 4:
                    raise ValueError("Ownership session recovery limit reached")
                history.append({"previous": previous, "current": session, "report_digest": digest(report),
                                "observed_at": _now().isoformat()})
                op["owner_session"] = session
                op["updated_at"] = _now().isoformat()
                self._save(journal)
            return copy.deepcopy(op)

    def mark_completion(self, op_id: str, session_id: str, srp_id: int, wire_digest: str) -> dict[str, Any]:
        """Persist one private bandwidth-completion experiment, never retry it."""
        if (not isinstance(session_id, str) or not 1 <= len(session_id) <= 128 or
                type(srp_id) is not int or not 0 < srp_id < 0xFFFFFFFF or
                not isinstance(wire_digest, str) or not _HEX.fullmatch(wire_digest)):
            raise ValueError("Completion requires an exact current session, SRP and wire digest")
        with resource_lock(self.state_dir):
            journal, ledger = self._load()
            op = self._operation(journal, op_id)
            if (op["status"] not in {"ROLLBACK_REQUIRED", "APPLIED_UNVERIFIED"} or
                    not (op.get("owned_candidate") or op.get("reported")) or op.get("completion") or
                    op.get("cleanup") or session_id != op.get("owner_session", op["send"]["session_id"]) or
                    (session_id == op["send"]["session_id"] and srp_id <= op["send"]["srp_id"]) or
                    op["policy_digest"] != self.policy_digest):
                raise ValueError("Completion needs an unreplayed owned identity and current authorization")
            topology, topology_digest = self.computer.validated_snapshot()
            topology = replace(topology, raw_metadata={**topology.raw_metadata, "authoritative": False})
            effective = ledger.effective_topology(topology, op["owner"])
            by_id = {edge.id: edge for edge in effective.directional_links}
            for i, edge_id in enumerate(op["path_edges"]):
                edge = by_id.get(edge_id)
                if (edge is None or not edge.admin_up or not edge.oper_up or
                        edge.source != op["path_nodes"][i] or edge.target != op["path_nodes"][i + 1] or
                        edge.to_ip != op["ero"][i] or edge.available_bps(7) < op["bandwidth_bps"]):
                    raise ValueError("The owned path is absent or lacks current directed RSVP capacity")
            op["completion"] = {"session_id": session_id, "srp_id": srp_id, "wire_digest": wire_digest,
                                "attempted_at": _now().isoformat(), "topology_digest": topology_digest}
            op["updated_at"] = _now().isoformat()
            self._save(journal)
            return copy.deepcopy(op)

    def observe(self, op_id: str, report: dict[str, Any]) -> dict[str, Any]:
        with resource_lock(self.state_dir):
            journal, ledger = self._load()
            op = self._operation(journal, op_id)
            if op["status"] not in {"PUSHING", "APPLIED_UNVERIFIED", "ROLLBACK_REQUIRED"}:
                raise ValueError("Creation is not awaiting an owned report")
            identity = self._owned_report(op, report)
            completion = op.get("completion")
            if completion and not completion.get("acknowledged") and (
                    report.get("srp_id") != completion["srp_id"] or
                    report["received_at"] < _stamp(completion["attempted_at"]).timestamp()):
                raise ValueError("Owned completion requires its exact new correlated acknowledgment")
            code = report.get("operational_code")
            if type(code) is not int or _OPERATIONS.get(code) != report.get("operational_state"):
                raise ValueError("Creation report operational state is inconsistent")
            ero = report.get("ero")
            if not isinstance(ero, list) or any(not isinstance(hop, dict) or hop.get("type") != 1 or
                    hop.get("loose") is not False or hop.get("prefix_length") != 32 for hop in ero):
                raise ValueError("Creation report requires a strict numbered IPv4 ERO")
            addresses = [hop.get("address") for hop in ero]
            if addresses != op["ero"] and not (code == 0 and not addresses):
                raise ValueError("Creation report path differs from the admitted directed path")
            attributes = report.get("attributes")
            if not isinstance(attributes, list) or len(attributes) > 8:
                raise ValueError("Creation report attributes are malformed")
            attrs: dict[tuple[int, int], str] = {}
            for item in attributes:
                if not isinstance(item, dict):
                    raise ValueError("Malformed creation report attribute")
                key = (item.get("class"), item.get("type"))
                if key in attrs or key not in {(5, 1), (5, 5), (9, 1), (6, 1), (8, 1)}:
                    raise ValueError("Unsupported or duplicate creation report attribute")
                attrs[key] = item.get("body_hex", "")
            lspa = struct.pack("!IIIBBBB", 0, 0, 0, 7, 7, 0, 0).hex()
            if attrs.get((5, 1)) != op["bandwidth_wire"] or attrs.get((9, 1)) != lspa:
                raise ValueError("Creation report bandwidth or priorities differ from the exact wire plan")
            used = attrs.get((5, 5))
            if used is not None and used != op["bandwidth_wire"] and not (code == 0 and used == bytes(4).hex()):
                raise ValueError("Creation report used bandwidth conflicts with the admitted bandwidth")
            op.update(status="APPLIED_UNVERIFIED", updated_at=_now().isoformat(), verified=False,
                      reported={"identity": identity, "operational_state": report["operational_state"],
                                "ero": addresses, "received_at": report["received_at"],
                                "report_digest": digest(report)})
            if completion:
                completion.update(acknowledged=True, acknowledged_at=_now().isoformat())
            ledger.transition(op["reservation_id"], "APPLIED_UNVERIFIED")
            self._save(journal)
            return copy.deepcopy(op)

    def reject_before_send(self, op_id: str, reason: str) -> dict[str, Any]:
        with resource_lock(self.state_dir):
            journal, ledger = self._load()
            op = self._operation(journal, op_id)
            entry = ledger.entries.get(op["reservation_id"])
            if op.get("send") is not None or op["status"] not in {"PLANNED", "REJECTED"} or (
                    entry is not None and entry["status"] != "PLANNED"):
                raise ValueError("An attempted or uncertain creation cannot release its hold as a prewrite rejection")
            op.update(status="REJECTED", reason=_principal(reason), updated_at=_now().isoformat())
            self._save(journal)
            ledger.data["entries"].pop(op["reservation_id"], None)
            ledger.save()
            return copy.deepcopy(op)

    def uncertain(self, op_id: str, reason: str) -> dict[str, Any]:
        with resource_lock(self.state_dir):
            journal, ledger = self._load()
            op = self._operation(journal, op_id)
            if not op.get("send") or op["status"] in _RELEASED:
                raise ValueError("Only attempted creation delivery can become uncertain")
            op.update(status="ROLLBACK_REQUIRED", reason=_principal(reason), updated_at=_now().isoformat())
            ledger.transition(op["reservation_id"], "ROLLBACK_REQUIRED")
            self._save(journal)
            return copy.deepcopy(op)

    def mark_removing(self, op_id: str, session_id: str, srp_id: int, wire_digest: str) -> dict[str, Any]:
        """Persist one owned qualification cleanup attempt; never replay or refund it."""
        if (not isinstance(session_id, str) or not 1 <= len(session_id) <= 128 or
                type(srp_id) is not int or not 0 < srp_id < 0xFFFFFFFF or
                not isinstance(wire_digest, str) or not _HEX.fullmatch(wire_digest)):
            raise ValueError("Owned cleanup requires an exact current session, SRP and wire digest")
        with resource_lock(self.state_dir):
            journal, ledger = self._load()
            op = self._operation(journal, op_id)
            send = op.get("send")
            if (op["status"] not in {"APPLIED_UNVERIFIED", "ROLLBACK_REQUIRED"} or
                    not (op.get("owned_candidate") or op.get("reported")) or not send or op.get("cleanup") is not None or
                    session_id != op.get("owner_session", send["session_id"]) or
                    (session_id == send["session_id"] and srp_id <= send["srp_id"]) or
                    (op.get("completion") and op["completion"]["session_id"] == session_id and
                     srp_id <= op["completion"]["srp_id"])):
                raise ValueError("Cleanup needs a bound owned identity and a new, unreplayed same-session boundary")
            timestamp = _now().isoformat()
            op.update(status="ROLLBACK_REQUIRED", updated_at=timestamp,
                      cleanup={"session_id": session_id, "srp_id": srp_id,
                               "wire_digest": wire_digest, "attempted_at": timestamp})
            ledger.transition(op["reservation_id"], "ROLLBACK_REQUIRED")
            self._save(journal)
            return copy.deepcopy(op)

    def record_removal(self, op_id: str, report: dict[str, Any]) -> dict[str, Any]:
        """Correlate owned removal acknowledgment while retaining the entire hold."""
        with resource_lock(self.state_dir):
            journal, _ = self._load()
            op = self._operation(journal, op_id)
            cleanup = op.get("cleanup")
            if (op["status"] != "ROLLBACK_REQUIRED" or not (op.get("owned_candidate") or op.get("reported")) or not cleanup or
                    report.get("session_id") != cleanup["session_id"] or report.get("srp_id") != cleanup["srp_id"]):
                raise ValueError("Removal report is not correlated to the owned cleanup boundary")
            self._owned_report(op, report, removal=True)
            if report["received_at"] < _stamp(cleanup["attempted_at"]).timestamp():
                raise ValueError("Removal report predates the owned cleanup attempt")
            cleanup.update(report=copy.deepcopy(report), report_digest=digest(report))
            op["updated_at"] = _now().isoformat()
            self._save(journal)
            return copy.deepcopy(op)

    def confirmed_removal(self, op_id: str, report: dict[str, Any], *,
                          resources_released: bool = False) -> dict[str, Any]:
        """Caller must independently confirm resource release; PCRpt R alone cannot refund it."""
        with resource_lock(self.state_dir):
            journal, ledger = self._load()
            op = self._operation(journal, op_id)
            cleanup = op.get("cleanup") or {}
            if (not (op.get("owned_candidate") or op.get("reported")) or resources_released is not True or op["status"] == "REJECTED" or
                    cleanup.get("report_digest") != digest(report)):
                raise ValueError("Owned identity and independent resource-release evidence are required")
            self._owned_report(op, report, removal=True)
            op.update(status="ROLLED_BACK", updated_at=_now().isoformat(),
                      removal={"report_digest": digest(report), "resources_released": True,
                               "received_at": report["received_at"]})
            self._save(journal)
            ledger.data["entries"].pop(op["reservation_id"], None)
            ledger.save()
            return copy.deepcopy(op)
