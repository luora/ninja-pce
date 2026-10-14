"""Bounded recovery of one independently verified configured tunnel.

The immutable creation operation remains the authority for identity and
constraints. Recovery retains old and new local holds; it never refunds them.
"""
from __future__ import annotations

import copy
import math
import os
import re
import struct
import time
import uuid
from dataclasses import replace
from datetime import datetime
from ipaddress import IPv4Address
from pathlib import Path
from typing import Any, Callable

from .pcep_compute import PathReply, PcepPathComputer
from .pcep_failover import FailureRecoveryPlanner, _mapped_edges, _reply_hash, _report
from .pcep_provisioning import _FIELDS
from .reservations import ReservationLedger, resource_lock
from .state import digest, load_state


def _regular(path: Path, maximum: int = 1_048_576) -> None:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > maximum:
        raise ValueError("Managed recovery requires existing bounded regular state files")


class ManagedFailureRecoveryPlanner(FailureRecoveryPlanner):
    """One exact VERIFIED creation operation, with conservative shared holds.

    The separate recovery journal must be outside the provisioning state
    directory: the inherited writer locks its own parent independently. An
    interrupted ledger/journal transition retains capacity and requires review.
    """

    def __init__(self, computer: PcepPathComputer, *, operation_journal: Path,
                 operation_id: str, journal_path: Path, debounce_seconds: float = 5,
                 cooldown_seconds: float = 30, max_attempts: int = 3,
                 ack_timeout_seconds: float = 30, clock: Callable[[], float] = time.time,
                 reinstatement_path: Path | None = None):
        self.operation_journal = Path(operation_journal).resolve()
        self.operation_id = operation_id
        if not isinstance(operation_id, str) or not re.fullmatch(r"[0-9a-f]{32}", operation_id):
            raise ValueError("Managed recovery requires one exact provisioning operation ID")
        recovery_path = Path(journal_path).resolve()
        if (Path(operation_journal).is_symlink() or Path(journal_path).is_symlink() or
                recovery_path.parent == self.operation_journal.parent or
                recovery_path in {computer.raw_path.resolve(), computer.status_path.resolve(),
                                  computer.topology_path.resolve()}):
            raise ValueError("Managed recovery journal needs a separate parent from shared provisioning state")
        self._configured_computer = computer
        self._topology_policy = self._computer_policy()
        existed = recovery_path.exists()
        self._state_files()
        with resource_lock(self.operation_journal.parent):
            journal, ledger, operation, hold = self._owned_state()
        authorization = operation["authorization"]
        self._authorization = copy.deepcopy(authorization)
        self._binding = {
            "operation_journal": str(self.operation_journal), "operation_id": operation_id,
            "operation_digest": digest(operation), "authorization_digest": operation["authorization_digest"],
            "provisioning_policy_digest": journal["policy_digest"],
            "hold_digest": digest({key: value for key, value in hold.items() if key != "resources"}),
            "topology_policy": self._topology_policy,
        }
        self._expected_resources = copy.deepcopy(hold["resources"])
        self.reinstatement_path = Path(reinstatement_path) if reinstatement_path is not None else None
        if self.reinstatement_path is not None and self.reinstatement_path.resolve() in {
                self.operation_journal, recovery_path, computer.raw_path.resolve(),
                computer.status_path.resolve(), computer.topology_path.resolve(),
                self.operation_journal.parent / ".ninja-pce-reservations.json"}:
            raise ValueError("Reinstatement request must be separate from controller inputs and state")
        super().__init__(computer, peer=authorization["peer"], tunnel_id=authorization["tunnel_id"],
                         journal_path=recovery_path, debounce_seconds=debounce_seconds,
                         cooldown_seconds=cooldown_seconds, max_attempts=max_attempts,
                         ack_timeout_seconds=ack_timeout_seconds, clock=clock)
        saved = self._data.get("managed_authority")
        if saved is None:
            initial = {edge: authorization["bandwidth_bps"] for edge in authorization["path_edges"]}
            if existed or hold["resources"] != initial or self._data.get("baseline") or self._data.get("pending"):
                raise ValueError("Untracked managed recovery resources require explicit reconciliation")
            self._data["managed_authority"] = {"binding": self._binding, "resources": initial}
            self._save()
        elif (not isinstance(saved, dict) or saved.get("binding") != self._binding or
              saved.get("resources") != hold["resources"]):
            raise ValueError("Managed recovery operation or held resources changed; reconcile explicitly")
        if not self.validate_authority():
            raise ValueError("Managed recovery authority changed during initialization")

    def _pending_basis(self, pending: dict[str, Any]) -> dict[str, Any]:
        if pending.get("intent_mode") != "reinstatement":
            return super()._pending_basis(pending)
        records = self._data.get("reinstatements", [])
        auth = self._authorization
        if (self._data.get("baseline") is not None or not isinstance(records, list) or len(records) > 3 or
                not any(isinstance(item, dict) and item.get("request_id") == pending.get("reinstatement_id") and
                        item.get("attempt") == self._data["attempts"] for item in records) or
                (pending.get("outcome") or {}).get("status") != "REINSTATEMENT_PATH" or
                pending.get("ownership_key") != [18, auth["source_ip"], auth["destination_ip"],
                                                 auth["tunnel_id"], IPv4Address(auth["source_ip"]).packed.hex()] or
                pending.get("bandwidth_wire") != struct.pack("!f", auth["bandwidth_bps"] / 8).hex() or
                pending.get("lspa_wire") != (bytes(12) + bytes((7, 7, 0, 0))).hex() or
                pending.get("symbolic_name_hex") != auth["symbolic_name"].encode().hex()):
            raise ValueError("Malformed pending managed reinstatement authority")
        return pending

    def _reinstatement_candidate(self, peer: str, report: dict[str, Any], srp_id: int) -> PathReply:
        if (type(srp_id) is not int or not 0 < srp_id < 0xFFFFFFFF or
                not self._report_authorized(peer, report) or report.get("sync") is not False or
                report.get("operational_code") != 0 or report.get("ero") != [] or report.get("srp_id") != 0):
            raise ValueError("Reinstatement requires an exact live DOWN empty-ERO delegation")
        validated = _report(report, self._now())
        reply = self.delegated(peer, [validated["report"]], (srp_id,))
        if (len(reply.messages) != 1 or len(reply.outcomes) != 1 or
                reply.outcomes[0].get("status") != "INITIAL_PATH" or not self.computer.validate_reply(reply)):
            raise ValueError("No fresh admissible reinstatement path")
        outcome = {**reply.outcomes[0], "status": "REINSTATEMENT_PATH",
                   "reason": "Explicit one-use managed tunnel reinstatement",
                   "topology_digest": reply.topology_digest}
        result = PathReply(reply.messages, (outcome,), reply.topology_digest)
        with resource_lock(self.operation_journal.parent):
            journal, ledger, operation, hold = self._owned_state()
            self._check_binding(journal, operation, hold)
            self._candidate(result, ledger)
        return result

    def reinstatement_plan(self, peer: str, report: dict[str, Any], *, ttl_seconds: float = 60,
                           request_id: str | None = None, created_at: float | None = None,
                           _reply: PathReply | None = None) -> dict[str, Any]:
        """Reviewable local authority; does not change state or send a message."""
        with self._lock:
            now = self._now()
            created = now if created_at is None else created_at
            identifier = uuid.uuid4().hex if request_id is None else request_id
            if (isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, (int, float)) or
                    not math.isfinite(ttl_seconds) or not 0 < ttl_seconds <= 120 or
                    isinstance(created, bool) or not isinstance(created, (int, float)) or
                    not math.isfinite(created) or not created <= now < created + ttl_seconds or
                    not isinstance(identifier, str) or not re.fullmatch(r"[0-9a-f]{32}", identifier)):
                raise ValueError("Reinstatement needs a fresh bounded request identity and expiry")
            saved = load_state(self.path)
            if digest(saved) != self._disk_revision:
                raise ValueError("Reinstatement recovery journal changed")
            if (self._data.get("pending") or self._data.get("baseline") or
                    self._data["attempts"] >= self.max_attempts or
                    self._data["blocked"] and saved.get("reason") not in {
                        "managed_operation_or_report_authority_changed", "tunnel_identity_or_constraints_changed"}):
                raise ValueError("Unresolved intent, active baseline or recovery policy prevents reinstatement")
            last = self._data.get("last_attempt_at")
            if last is not None and now - last < self.cooldown:
                raise ValueError("Reinstatement recovery cooldown has not elapsed")
            if any(item.get("request_id") == identifier for item in self._data.get("reinstatements", [])):
                raise ValueError("Reinstatement request was already consumed")
            received = report.get("received_at")
            if (isinstance(received, bool) or not isinstance(received, (int, float)) or
                    not math.isfinite(received) or not 0 <= now - received <= self.computer.max_age_seconds):
                raise ValueError("Reinstatement requires a newly received report")
            revision, epoch = self._disk_revision, self._epoch
            attempts, resources = self._data["attempts"], digest(self._expected_resources)
        # Graph search runs outside the planner lock, so the session reader can
        # observe revocation while the bounded computation is in progress.
        reply = _reply if _reply is not None else self._reinstatement_candidate(peer, report, 1)
        if _reply is not None and not self._report_authorized(peer, report):
            raise ValueError("Reinstatement report authority changed")
        with self._lock:
            if (self._disk_revision != revision or self._epoch != epoch or self._data.get("pending") or
                    self._data.get("baseline") or self._data["attempts"] != attempts or
                    digest(self._expected_resources) != resources):
                raise ValueError("Reinstatement authority changed during computation")
            if not created <= self._now() < created + ttl_seconds:
                raise ValueError("Reinstatement review interval expired during computation")
            return {"version": 2, "kind": "managed_pcep_reinstatement", "request_id": identifier,
                    "created_at": created, "expires_at": created + ttl_seconds,
                    "binding": copy.deepcopy(self._binding), "recovery_revision": self._disk_revision,
                    "resources_digest": digest(self._expected_resources), "attempts": self._data["attempts"],
                    "report_token": self._token(report), "session_id": report["session_id"],
                    "plsp_id": report["plsp_id"], "ero": reply.outcomes[0]["ero"],
                    "edges": reply.outcomes[0]["path_link_ids"],
                    "prior_status": saved["status"], "prior_reason": saved.get("reason")}

    def _checked_reinstatement(self, peer: str, report: dict[str, Any], *,
                               reply: PathReply | None = None) -> dict[str, Any]:
        path = self.reinstatement_path
        if path is None:
            raise ValueError("No explicit reinstatement request configured")
        _regular(path, 65536)
        stat = path.stat()
        if stat.st_uid != os.geteuid() or stat.st_mode & 0o077:
            raise ValueError("Reinstatement request must be owned privately by the listener user")
        request = load_state(path)
        expected = self.reinstatement_plan(peer, report, request_id=request.get("request_id"),
                                           created_at=request.get("created_at"),
                                           ttl_seconds=request["expires_at"] - request["created_at"], _reply=reply)
        if request != expected:
            raise ValueError("Reinstatement request differs from current reviewed authority")
        return request

    def _compute_reinstatement(self, peer: str, report: dict[str, Any], srp_id: int) -> PathReply | None:
        with self._lock:
            if (not self._live or self._computing or self._data.get("pending") or self._data.get("baseline") or
                    self._token(report) != self._token(self._live["raw"])):
                return None
            self._computing = True
            epoch = self._epoch
        try:
            reply = self._reinstatement_candidate(peer, report, srp_id)
            request = self._checked_reinstatement(peer, report, reply=reply)
            with self._lock:
                if (self._epoch != epoch or not self._live or self._data.get("pending") or
                        self._data.get("baseline") or self._token(report) != self._token(self._live["raw"])):
                    return None
                if reply.outcomes[0]["ero"] != request["ero"] or reply.outcomes[0]["path_link_ids"] != request["edges"]:
                    return None
                self._proposal = {"hash": _reply_hash(reply), "token": self._token(report),
                                  "epoch": self._epoch, "reply": reply, "reinstatement": request}
                # READY is observational only; the request remains bound to the
                # durable journal revision and its prior status/reason.
                self._data["status"] = "READY"
                return reply
        except (ValueError, OSError, KeyError, TypeError, OverflowError):
            return None
        finally:
            with self._lock:
                self._computing = False

    def _computer_policy(self) -> dict[str, Any]:
        computer = self._configured_computer
        return {"headends": dict(computer.headends), "node_names": dict(computer.node_names),
                "required_peers": list(computer.required_peers),
                "raw_path": str(computer.raw_path.resolve()), "status_path": str(computer.status_path.resolve()),
                "topology_path": str(computer.topology_path.resolve()), "max_age_seconds": computer.max_age_seconds,
                "ospf_multi_area_digest": computer.ospf_multi_area.digest if computer.ospf_multi_area else None}

    def _owned_state(self) -> tuple[dict[str, Any], ReservationLedger, dict[str, Any], dict[str, Any]]:
        _regular(self.operation_journal)
        _regular(self.operation_journal.parent / ".ninja-pce-reservations.json")
        journal = load_state(self.operation_journal)
        operations = journal.get("operations")
        if (journal.get("kind") != "cli_tunnel_provisioning" or
                not isinstance(journal.get("policy_digest"), str) or
                not isinstance(operations, dict) or not 1 <= len(operations) <= 16):
            raise ValueError("Managed recovery requires the original bounded provisioning journal")
        operation = operations.get(self.operation_id)
        if (not isinstance(operation, dict) or operation.get("id") != self.operation_id or
                operation.get("status") != "VERIFIED"):
            raise ValueError("Managed recovery requires one VERIFIED provisioning operation")
        for identifier, other in operations.items():
            if (not isinstance(other, dict) or other.get("id") != identifier or
                    identifier != self.operation_id and other.get("status") not in {"REJECTED", "ROLLED_BACK"}):
                raise ValueError("Another unresolved provisioning operation prevents managed recovery")
        authorization = operation.get("authorization")
        if (not isinstance(authorization, dict) or set(authorization) != _FIELDS or
                operation.get("authorization_digest") != digest(authorization) or
                operation.get("owner") != "cli-provision:" + self.operation_id or
                not isinstance(operation.get("headend"), str) or
                not isinstance(operation.get("destination"), str) or
                operation.get("reservation_id") !=
                f"{operation['headend']}:iosxe:Tunnel{authorization.get('tunnel_id')}" or
                any(operation.get(field) != authorization[field]
                    for field in ("plan_id", "tunnel_id", "bandwidth_bps", "ero", "path_edges"))):
            raise ValueError("Managed recovery operation differs from its immutable authorization")
        computer = self._configured_computer
        bandwidth, tunnel = authorization["bandwidth_bps"], authorization["tunnel_id"]
        source, destination, peer = (authorization["source_ip"], authorization["destination_ip"],
                                     authorization["peer"])
        if (type(bandwidth) is not int or not 0 < bandwidth <= 10**9 or
                type(tunnel) is not int or not 1 <= tunnel <= 65535 or
                not isinstance(authorization["plan_id"], str) or
                not re.fullmatch(r"[0-9a-f]{64}", authorization["plan_id"]) or
                computer.headends.get(peer) != source or
                computer.node_names.get(source) != operation["headend"] or
                computer.node_names.get(destination) != operation["destination"] or source == destination or
                authorization["symbolic_name"] != f"Tunnel{tunnel}_w" or
                type(authorization["setup_priority"]) is not int or authorization["setup_priority"] != 7 or
                type(authorization["hold_priority"]) is not int or authorization["hold_priority"] != 7):
            raise ValueError("Managed recovery identity or constraints exceed the configured profile")
        created, expiry = operation.get("created_at"), authorization["expires_at"]
        if not isinstance(created, str) or not isinstance(expiry, str):
            raise ValueError("Managed recovery requires an immutable initial authorization interval")
        created_time = datetime.fromisoformat(created.replace("Z", "+00:00"))
        expiry_time = datetime.fromisoformat(expiry.replace("Z", "+00:00"))
        if (created_time.tzinfo is None or expiry_time.tzinfo is None or
                not 0 < (expiry_time - created_time).total_seconds() <= 120):
            raise ValueError("Managed recovery initial authorization interval was not bounded")
        verification = operation.get("verification")
        if (not isinstance(verification, dict) or verification.get("independent") is not True or
                verification.get("scope") != "headend-rsvp" or verification.get("pcep_control") is not True or
                verification.get("checks") != ["identity", "destination", "bandwidth", "ero", "signaling"]):
            raise ValueError("Managed recovery lacks independent initial RSVP/PCEP verification")
        edges, ero = authorization["path_edges"], authorization["ero"]
        if (not isinstance(edges, list) or not 1 <= len(edges) <= 64 or
                any(not isinstance(edge, str) or not 1 <= len(edge) <= 256 for edge in edges) or
                len(set(edges)) != len(edges) or not isinstance(ero, list) or len(ero) != len(edges) or
                any(not isinstance(address, str) or str(IPv4Address(address)) != address for address in ero)):
            raise ValueError("Managed recovery initial strict path is malformed")
        ledger = ReservationLedger(self.operation_journal.parent)
        hold = ledger.entries.get(operation["reservation_id"])
        if (not hold or hold.get("owner") != operation["owner"] or hold.get("status") != "VERIFIED" or
                hold.get("plan_id") != authorization["plan_id"] or
                hold.get("current_edges") != edges or hold.get("current_bandwidth_bps") != bandwidth):
            raise ValueError("Managed recovery lost its exact original reservation hold")
        return journal, ledger, operation, hold

    def _state_files(self) -> None:
        for path in (self.operation_journal, self.operation_journal.parent / ".ninja-pce-reservations.json",
                     self.operation_journal.parent / ".ninja-pce-resources.lock"):
            _regular(path)

    def _check_binding(self, journal: dict[str, Any], operation: dict[str, Any], hold: dict[str, Any]) -> None:
        if (self._computer_policy() != self._topology_policy or
                digest(operation) != self._binding["operation_digest"] or
                journal["policy_digest"] != self._binding["provisioning_policy_digest"] or
                digest({key: value for key, value in hold.items() if key != "resources"}) !=
                self._binding["hold_digest"] or hold["resources"] != self._expected_resources):
            raise ValueError("Managed recovery immutable authority or held resources changed")

    def validate_authority(self) -> bool:
        """Reread immutable ownership; expiry applies only to initial creation."""
        try:
            self._state_files()
            with resource_lock(self.operation_journal.parent):
                journal, _, operation, hold = self._owned_state()
                self._check_binding(journal, operation, hold)
            return True
        except (ValueError, OSError, KeyError, TypeError, OverflowError):
            return False

    def _report_authorized(self, peer: str, report: dict[str, Any]) -> bool:
        try:
            if not self.validate_authority() or peer != self.peer or report.get("created") is not False:
                return False
            validated = _report(report, self._now())
            auth = self._authorization
            return bool(validated["ownership_key"] == [18, auth["source_ip"], auth["destination_ip"],
                         auth["tunnel_id"], IPv4Address(auth["source_ip"]).packed.hex()] and
                        validated["symbolic_name_hex"] == auth["symbolic_name"].encode().hex() and
                        validated["bandwidth_wire"] == struct.pack("!f", auth["bandwidth_bps"] / 8).hex() and
                        validated["lspa_wire"] == (bytes(12) + bytes((7, 7, 0, 0))).hex())
        except (ValueError, KeyError, TypeError, OverflowError):
            return False

    def _deny(self, reason: str) -> None:
        with self._lock:
            self._live, self._proposal = None, None
            self._data.update(blocked=True, status="RECOVERY_REQUIRED", reason=reason)
            self._save()

    def observe(self, peer: str, report: dict[str, Any], *, fresh: bool, synchronized: bool) -> None:
        if not self.matches(peer, report):
            return
        if report.get("sync") is True:
            # Synchronization can report historical D=0 state before the PCC
            # reasserts live delegation. It grants no baseline or write, and
            # must not be mistaken for a change to the owned operation.
            if not self.validate_authority():
                self._deny("managed_operation_or_report_authority_changed")
                return
            super().observe(peer, report, fresh=False, synchronized=False)
            return
        if fresh is not True or synchronized is not True:
            super().observe(peer, report, fresh=fresh, synchronized=synchronized)
            return
        if not self._report_authorized(peer, report):
            self._deny("managed_operation_or_report_authority_changed")
            return
        super().observe(peer, report, fresh=fresh, synchronized=synchronized)

    def _candidate(self, reply: PathReply, ledger: ReservationLedger) -> list[str]:
        topology, revision = self.computer.validated_snapshot()
        if reply.topology_digest != revision or len(reply.outcomes) != 1:
            raise ValueError("Managed recovery candidate requires the exact fresh topology")
        outcome, auth = reply.outcomes[0], self._authorization
        if (outcome.get("status") not in {"FAILURE_PATH", "REINSTATEMENT_PATH"} or
                outcome.get("sender") != auth["source_ip"] or
                outcome.get("endpoint") != auth["destination_ip"] or outcome.get("tunnel_id") != self.tunnel_id or
                outcome.get("bandwidth_bps") != auth["bandwidth_bps"] or
                outcome.get("setup_priority") != 7 or outcome.get("hold_priority") != 7):
            raise ValueError("Managed recovery candidate changes its authorized identity or constraints")
        selected = _mapped_edges(topology, outcome["ero"], auth["source_ip"], auth["destination_ip"])
        if selected != outcome.get("path_link_ids"):
            raise ValueError("Managed recovery candidate edges differ from the strict ERO")
        conservative = replace(topology, raw_metadata={**topology.raw_metadata, "authoritative": False})
        effective = ledger.effective_topology(conservative, "cli-provision:" + self.operation_id)
        links = {link.id: link for link in effective.directional_links}
        if any(links[edge].available_bps(7) < auth["bandwidth_bps"] for edge in selected):
            raise ValueError("Managed recovery candidate exceeds capacity after other owners' holds")
        return selected

    def compute(self, peer: str, report: dict[str, Any], srp_id: int) -> PathReply | None:
        if not self.matches(peer, report):
            return None
        if report.get("sync") is True:
            if not self.validate_authority():
                self._deny("managed_operation_or_report_authority_changed")
            return None
        if not self._report_authorized(peer, report):
            self._deny("managed_operation_or_report_authority_changed")
            return None
        if self.reinstatement_path is not None and self.reinstatement_path.exists():
            reinstated = self._compute_reinstatement(peer, report, srp_id)
            if reinstated is not None:
                return reinstated
        reply = super().compute(peer, report, srp_id)
        if reply is not None:
            try:
                with resource_lock(self.operation_journal.parent):
                    journal, ledger, operation, hold = self._owned_state()
                    self._check_binding(journal, operation, hold)
                    self._candidate(reply, ledger)
            except (ValueError, OSError, KeyError, TypeError, OverflowError):
                with self._lock:
                    self._proposal = None
                    self._data.update(status="NO_REPLACEMENT", reason="managed_capacity_or_authority_unavailable")
                return None
        return reply

    def prepare_send(self, peer: str, report: dict[str, Any], reply: PathReply) -> None:
        if not self._report_authorized(peer, report):
            self._deny("managed_operation_or_report_authority_changed")
            raise ValueError("Managed recovery authority is no longer current")
        if reply.outcomes and reply.outcomes[0].get("status") == "REINSTATEMENT_PATH":
            self._prepare_reinstatement(peer, report, reply)
            return
        try:
            with resource_lock(self.operation_journal.parent):
                journal, ledger, operation, hold = self._owned_state()
                self._check_binding(journal, operation, hold)
                selected = self._candidate(reply, ledger)
                resources = dict(hold["resources"])
                for edge in selected:
                    resources[edge] = max(resources.get(edge, 0), self._authorization["bandwidth_bps"])
                ledger.data["entries"][operation["reservation_id"]]["resources"] = resources
                ledger.save()
                self._expected_resources = resources
            # The shared directory lock is released before the inherited
            # recovery journal acquires its distinct parent lock.
            with self._lock:
                self._data["managed_authority"]["resources"] = copy.deepcopy(resources)
            super().prepare_send(peer, report, reply)
            if not self.validate_authority():
                raise ValueError("Managed recovery authority changed during intent persistence")
        except (ValueError, OSError, KeyError, TypeError, OverflowError):
            self._deny("managed_recovery_prewrite_failed_holds_retained")
            raise

    def _prepare_reinstatement(self, peer: str, report: dict[str, Any], reply: PathReply) -> None:
        try:
            with self._lock:
                request = self._checked_reinstatement(peer, report, reply=reply)
                if (not self._live or not self._proposal or self._proposal.get("reinstatement") != request or
                        self._proposal["hash"] != _reply_hash(reply) or self._proposal["epoch"] != self._epoch or
                        self._proposal["token"] != self._token(report) or
                        self._token(self._live["raw"]) != self._token(report) or
                        not 0 <= self._now() - self._observed_at <= self.computer.max_age_seconds or
                        not self.computer.validate_reply(reply)):
                    raise ValueError("Reinstatement proposal no longer matches live authority")
                with resource_lock(self.operation_journal.parent):
                    journal, ledger, operation, hold = self._owned_state()
                    self._check_binding(journal, operation, hold)
                    selected = self._candidate(reply, ledger)
                    if not request["created_at"] <= self._now() < request["expires_at"]:
                        raise ValueError("Reinstatement expired before resource preparation")
                    resources = dict(hold["resources"])
                    for edge in selected:
                        resources[edge] = max(resources.get(edge, 0), self._authorization["bandwidth_bps"])
                    ledger.data["entries"][operation["reservation_id"]]["resources"] = resources
                    ledger.save()
                    self._expected_resources = resources
                self._data["managed_authority"]["resources"] = copy.deepcopy(resources)
                self._data.setdefault("reinstatements", []).append({
                    "request_id": request["request_id"], "request_digest": digest(request),
                    "consumed_at": self._now(), "attempt": self._data["attempts"] + 1,
                    "prior_status": request["prior_status"], "prior_reason": request["prior_reason"],
                    "recovery_revision": request["recovery_revision"],
                })
                validated = _report(report, self._now())
                if not request["created_at"] <= self._now() < request["expires_at"]:
                    raise ValueError("Reinstatement expired before intent persistence")
                self._data["blocked"] = False
                self._persist_intent(peer, report, reply, extra={
                    "intent_mode": "reinstatement", "reinstatement_id": request["request_id"],
                    **{key: validated[key] for key in ("bandwidth_wire", "lspa_wire", "symbolic_name_hex")},
                })
                if not self.validate_authority():
                    raise ValueError("Reinstatement authority changed during intent persistence")
        except (ValueError, OSError, KeyError, TypeError, OverflowError):
            self._deny("managed_reinstatement_prewrite_failed_holds_retained")
            raise
