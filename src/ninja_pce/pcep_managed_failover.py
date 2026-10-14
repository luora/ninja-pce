"""Bounded recovery of one independently verified configured tunnel.

The immutable creation operation remains the authority for identity and
constraints. Recovery retains old and new local holds; it never refunds them.
"""
from __future__ import annotations

import copy
import re
import struct
import time
from dataclasses import replace
from datetime import datetime
from ipaddress import IPv4Address
from pathlib import Path
from typing import Any, Callable

from .pcep_compute import PathReply, PcepPathComputer
from .pcep_failover import FailureRecoveryPlanner, _mapped_edges, _report
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
                 ack_timeout_seconds: float = 30, clock: Callable[[], float] = time.time):
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
        if (outcome.get("status") != "FAILURE_PATH" or outcome.get("sender") != auth["source_ip"] or
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
