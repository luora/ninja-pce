"""Finite, exact delegation authority for router-configured dashboard tunnels.

The configuration service owns this journal. Reading an allocated numeric range
is never sufficient authority: each initial update must match an immutable,
checksummed operation and its approved strict path. This reader performs no
network or state mutations and fails closed when state is unavailable.
"""
from __future__ import annotations

import fcntl
import math
import re
import struct
import time
from datetime import datetime, timezone
from ipaddress import IPv4Address
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .reservations import ReservationLedger
from .state import digest, load_state

if TYPE_CHECKING:
    from .pcep_domain_policy import DomainDelegationPolicy

_PLAN_ID = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")
_FIELDS = {"plan_id", "peer", "source_ip", "destination_ip", "tunnel_id", "symbolic_name",
           "bandwidth_bps", "setup_priority", "hold_priority", "expires_at", "ero", "path_edges"}
_AUTHORIZED_STATES = {"PUSHING", "APPLIED_UNVERIFIED", "VERIFIED"}
_JOURNAL_MAX_BYTES = 16 * 1_048_576
_JOURNAL_MAX_OPERATIONS = 4096


def operation_policy_revision(journal: dict[str, Any], operation: dict[str, Any]) -> str:
    """Read immutable operation policy without rebinding it to current policy.

    Legacy operations bind the journal's sole policy. The explicit v2 migration
    adds only ``policy_revision`` to each operation and retains that policy's
    complete document, so existing recovery bindings continue to identify the
    same original authorization.
    """
    schema = journal.get("provisioning_schema", 1)
    if type(schema) is not int or schema not in (1, 2):
        raise ValueError("Unsupported provisioning journal schema")
    revision = operation.get("policy_revision") if schema == 2 else journal.get("policy_digest")
    if not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{64}", revision):
        raise ValueError("Provisioning operation requires its original policy revision")
    if schema == 2:
        policies = journal.get("policies")
        if (not isinstance(policies, dict) or not 1 <= len(policies) <= _JOURNAL_MAX_OPERATIONS or
                not isinstance(policies.get(revision), dict) or digest(policies[revision]) != revision):
            raise ValueError("Provisioning operation policy revision is missing or changed")
    return revision


def provisioning_operation_digest(operation: dict[str, Any]) -> str:
    """Keep the original managed binding stable across policy-only migration."""
    return digest({key: value for key, value in operation.items() if key != "policy_revision"})


class JournalProvisioningAuthorization:
    """Resolve a recent owned configuration to one initial PCEP path update."""

    def __init__(self, journal_path: Path, headends: dict[str, str], *,
                 domain_policy: DomainDelegationPolicy | None = None):
        if domain_policy is None and (not headends or len(headends) > 16):
            raise ValueError("Configuration provisioning requires bounded explicit headends")
        self.path = Path(journal_path)
        self.headends = {str(IPv4Address(peer)): str(IPv4Address(source)) for peer, source in headends.items()}
        if len(self.headends) != len(headends):
            raise ValueError("Configuration provisioning headends must be distinct")
        self.domain_policy = domain_policy
        authority = ({"domain_policy_digest": domain_policy.policy_digest} if domain_policy is not None else
                     {"headends": self.headends})
        self.policy_digest = digest({"journal_path": str(self.path.resolve()), **authority})

    def _resolve(self, peer: str, report: dict[str, Any]) -> dict[str, Any] | None:
        try:
            size = self.path.stat().st_size
            if ((self.domain_policy is None and peer not in self.headends) or size > _JOURNAL_MAX_BYTES):
                return None
            # The configuration writer holds this lock only for short local
            # state transitions, never for SSH I/O. Do not create missing
            # locks/state: an incomplete deployment grants no authority.
            with (self.path.parent / ".ninja-pce-resources.lock").open("rb") as handle:
                fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
                try:
                    journal = load_state(self.path)
                    ledger_path = self.path.parent / ".ninja-pce-reservations.json"
                    if ledger_path.stat().st_size > _JOURNAL_MAX_BYTES:
                        return None
                    ledger = load_state(ledger_path)
                finally:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            operations = journal.get("operations")
            entries = ledger.get("entries")
            schema = journal.get("provisioning_schema", 1)
            limit = _JOURNAL_MAX_OPERATIONS if schema == 2 else 64
            if (journal.get("kind") != "cli_tunnel_provisioning" or not isinstance(operations, dict) or
                    type(schema) is not int or schema not in (1, 2) or
                    schema == 1 and size > 1_048_576 or
                    not 1 <= len(operations) <= limit or not isinstance(entries, dict)):
                return None
            matches = []
            for op_id, operation in operations.items():
                if not isinstance(operation, dict) or operation.get("id") != op_id:
                    return None
                if schema == 2:
                    operation_policy_revision(journal, operation)
                authorization = operation.get("authorization")
                if authorization is None:
                    continue  # Planning/rejected operations grant no authority.
                if (not isinstance(authorization, dict) or set(authorization) != _FIELDS or
                        operation.get("authorization_digest") != digest(authorization)):
                    return None
                if operation.get("status") not in _AUTHORIZED_STATES:
                    continue
                if not self._owned_hold(operation, authorization, entries):
                    return None
                if self._matches(peer, report, authorization, immutable_source=schema == 2):
                    matches.append(authorization)
            return matches[0] if len(matches) == 1 else None
        except (ValueError, OSError, KeyError, TypeError, OverflowError):
            return None

    @staticmethod
    def _owned_hold(operation: dict[str, Any], authorization: dict[str, Any], entries: dict[str, Any]) -> bool:
        if (operation.get("owner") != "cli-provision:" + operation["id"] or
                operation.get("reservation_id") != f"{operation['headend']}:iosxe:Tunnel{authorization['tunnel_id']}" or
                any(operation.get(field) != authorization[field]
                    for field in ("plan_id", "tunnel_id", "bandwidth_bps", "ero", "path_edges"))):
            return False
        hold = entries.get(operation["reservation_id"])
        if (not isinstance(hold, dict) or hold.get("owner") != operation["owner"] or
                hold.get("plan_id") != authorization["plan_id"] or hold.get("status") not in _AUTHORIZED_STATES or
                hold.get("current_edges") != authorization["path_edges"] or
                hold.get("current_bandwidth_bps") != authorization["bandwidth_bps"] or
                not isinstance(hold.get("resources"), dict)):
            return False
        return all(type(hold["resources"].get(edge)) is int and
                   hold["resources"][edge] >= authorization["bandwidth_bps"] for edge in authorization["path_edges"])

    def _matches(self, peer: str, report: dict[str, Any], authorization: dict[str, Any], *,
                 immutable_source: bool = False) -> bool:
        if not isinstance(authorization["expires_at"], str):
            return False
        expiry = datetime.fromisoformat(authorization["expires_at"].replace("Z", "+00:00"))
        if expiry.tzinfo is None or not 0 < (expiry - datetime.now(timezone.utc)).total_seconds() <= 120:
            return False
        plan_id = authorization["plan_id"]
        tunnel_id = authorization["tunnel_id"]
        bandwidth = authorization["bandwidth_bps"]
        if any(not isinstance(authorization[field], str) or
               str(IPv4Address(authorization[field])) != authorization[field]
               for field in ("peer", "source_ip", "destination_ip")):
            return False
        source = (str(IPv4Address(authorization["source_ip"])) if immutable_source else self.headends[peer])
        if self.domain_policy is not None:
            # The computation path independently verifies fresh, unique TED
            # membership. This check ensures a stored operation cannot escape
            # the current domain's transport scope before computation.
            address = IPv4Address(peer)
            if peer != source or not any(address in network for network in self.domain_policy.pcc_prefixes):
                return False
        elif self.headends.get(peer) != source:
            return False
        destination = str(IPv4Address(authorization["destination_ip"]))
        name = f"Tunnel{tunnel_id}_w"
        ero, edges = authorization["ero"], authorization["path_edges"]
        if (not isinstance(plan_id, str) or not _PLAN_ID.fullmatch(plan_id) or
                type(tunnel_id) is not int or not 1 <= tunnel_id <= 65535 or
                type(bandwidth) is not int or not 0 < bandwidth <= 10**12 or
                authorization["peer"] != peer or authorization["source_ip"] != source or
                destination == source or authorization["symbolic_name"] != name or
                type(authorization["setup_priority"]) is not int or authorization["setup_priority"] != 7 or
                type(authorization["hold_priority"]) is not int or authorization["hold_priority"] != 7 or
                not isinstance(ero, list) or not 1 <= len(ero) <= 64 or
                any(not isinstance(address, str) or str(IPv4Address(address)) != address for address in ero) or
                not isinstance(edges, list) or len(edges) != len(ero) or len(set(edges)) != len(edges) or
                any(not isinstance(edge, str) or not 1 <= len(edge) <= 256 for edge in edges)):
            return False
        identifiers = report.get("identifiers")
        if (report.get("created") is not False or report.get("delegate") is not True or
                report.get("administrative_up") is not True or report.get("removed") is not False or
                report.get("operational_code") != 0 or report.get("ero") != [] or report.get("srp_id") != 0 or
                report.get("sync") is not False or
                report.get("end_of_sync") is not False or not isinstance(identifiers, dict) or
                identifiers.get("type") != 18 or identifiers.get("sender") != source or
                identifiers.get("endpoint") != destination or identifiers.get("tunnel_id") != tunnel_id or
                identifiers.get("extended_tunnel_id_hex") != IPv4Address(source).packed.hex() or
                report.get("symbolic_name_hex") != name.encode().hex()):
            return False
        lsp_id = identifiers.get("lsp_id")
        if type(lsp_id) is not int or not 0 <= lsp_id <= 65535:
            return False
        identity = (IPv4Address(source).packed + struct.pack("!HH", lsp_id, tunnel_id) +
                    IPv4Address(source).packed + IPv4Address(destination).packed).hex()
        if identifiers.get("value_hex") != identity:
            return False
        tlvs = report.get("tlvs", [])
        attributes = report.get("attributes", [])
        if (not isinstance(tlvs, list) or not all(isinstance(item, dict) for item in tlvs) or
                not isinstance(attributes, list) or not all(isinstance(item, dict) for item in attributes)):
            return False
        if ([item.get("value_hex") for item in tlvs if item.get("type") == 17] != [name.encode().hex()] or
                [item.get("value_hex") for item in tlvs if item.get("type") == 18] != [identity]):
            return False
        requested = [item.get("body_hex") for item in attributes
                     if (item.get("class"), item.get("type")) == (5, 1)]
        lspa = [item.get("body_hex") for item in attributes
                if (item.get("class"), item.get("type")) == (9, 1)]
        return (requested == [struct.pack("!f", bandwidth / 8).hex()] and
                lspa == [(bytes(12) + bytes((7, 7, 0, 0))).hex()])

    def __call__(self, peer: str, report: dict[str, Any]) -> str | None:
        authorization = self._resolve(peer, report)
        return authorization["plan_id"] if authorization is not None else None

    def owns(self, peer: str, report: dict[str, Any]) -> bool:
        """Fence configured identities from fresh ordinary domain authority.

        Expiry, rollback uncertainty and a refused exact authorization never
        turn a configured tunnel into an unowned router request. If durable
        ownership cannot be read, fail closed for new ordinary initial writes.
        A proven released operation no longer fences its ID when the same
        locked ledger confirms both its owner and reservation have no hold.
        """
        try:
            if self.path.stat().st_size > _JOURNAL_MAX_BYTES:
                return True
            with (self.path.parent / ".ninja-pce-resources.lock").open("rb") as handle:
                fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
                try:
                    journal = load_state(self.path)
                    ledger_path = self.path.parent / ".ninja-pce-reservations.json"
                    if ledger_path.stat().st_size > _JOURNAL_MAX_BYTES:
                        return True
                    ledger = ReservationLedger(self.path.parent).data
                finally:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            operations = journal.get("operations")
            entries = ledger.get("entries")
            schema = journal.get("provisioning_schema", 1)
            if (journal.get("kind") != "cli_tunnel_provisioning" or
                    type(schema) is not int or schema not in (1, 2) or not isinstance(operations, dict) or
                    len(operations) > _JOURNAL_MAX_OPERATIONS or not isinstance(entries, dict) or
                    any(not isinstance(hold, dict) for hold in entries.values())):
                return True
            identifiers = report["identifiers"]
            for identifier, operation in operations.items():
                if not isinstance(operation, dict) or operation.get("id") != identifier:
                    return True
                if operation.get("status") not in {
                        "PLANNED", "PUSHING", "APPLIED_UNVERIFIED", "VERIFIED", "VERIFICATION_FAILED",
                        "ROLLBACK_REQUIRED", "REJECTED", "ROLLED_BACK"}:
                    return True
                if schema == 2:
                    operation_policy_revision(journal, operation)
                authorization = operation.get("authorization")
                if operation.get("authorization_digest") != digest(authorization):
                    return True
                if operation.get("status") in {"REJECTED", "ROLLED_BACK"}:
                    owner = operation.get("owner")
                    if (owner != "cli-provision:" + identifier or operation.get("reservation_id") in entries or
                            any(hold.get("owner") == owner for hold in entries.values())):
                        return True
                    continue
                if authorization is None:
                    continue
                if (not isinstance(authorization, dict) or set(authorization) != _FIELDS or
                        operation.get("authorization_digest") != digest(authorization)):
                    return True
                if (authorization["peer"] == peer and authorization["source_ip"] == identifiers.get("sender") and
                        authorization["tunnel_id"] == identifiers.get("tunnel_id")):
                    return True
            return False
        except (ValueError, OSError, KeyError, TypeError, OverflowError):
            return True

    def validate_path(self, peer: str, report: dict[str, Any], plan_id: str, ero: Any) -> bool:
        """Reread current authority; the chosen ERO must remain the approved one."""
        authorization = self._resolve(peer, report)
        return bool(authorization is not None and authorization["plan_id"] == plan_id and
                    authorization["ero"] == ero)


def configured_initial_path_observed(capture: dict[str, Any], operation: dict[str, Any], *,
                                     policy_digest: str, now: float | None = None) -> bool:
    """Prove exact owned initial PCUpd completion, separately from RSVP checks.

    The caller reads the capture with ``load_state``. A current-session
    correlated ACTIVE report proves PCEP application, never packet forwarding.
    Expired initial write authority may still have valid completion evidence.
    """
    try:
        observed_now = time.time() if now is None else now
        authorization = operation["authorization"]
        if (not isinstance(authorization, dict) or set(authorization) != _FIELDS or
                operation.get("authorization_digest") != digest(authorization) or
                operation.get("status") not in _AUTHORIZED_STATES or
                type(authorization["setup_priority"]) is not int or authorization["setup_priority"] != 7 or
                type(authorization["hold_priority"]) is not int or authorization["hold_priority"] != 7 or
                type(authorization["bandwidth_bps"]) is not int or
                not 0 < authorization["bandwidth_bps"] <= 10**12 or
                authorization["symbolic_name"] != f"Tunnel{authorization['tunnel_id']}_w" or
                capture.get("capture_error") or capture.get("listener_running") is not True or
                capture.get("provisioning_policy_digest") != policy_digest or
                capture.get("capabilities", {}).get("configuration_provisioning") is not True):
            return False
        if "checksum" in capture and capture["checksum"] != digest(
                {key: value for key, value in capture.items() if key != "checksum"}):
            return False
        generated = capture.get("generated_at")
        if (isinstance(generated, bool) or not isinstance(generated, (int, float)) or
                not math.isfinite(generated) or not 0 <= observed_now - generated <= 5):
            return False
        peer = capture["peers"][authorization["peer"]]
        valid_until = peer.get("valid_until")
        if (peer.get("state") != "ESTABLISHED" or peer.get("fresh") is not True or
                peer.get("synchronized") is not True or isinstance(valid_until, bool) or
                not isinstance(valid_until, (int, float)) or not math.isfinite(valid_until) or
                valid_until <= observed_now):
            return False
        created = datetime.fromisoformat(operation["created_at"].replace("Z", "+00:00"))
        expiry = datetime.fromisoformat(authorization["expires_at"].replace("Z", "+00:00"))
        if (created.tzinfo is None or expiry.tzinfo is None or
                not 0 < (expiry - created).total_seconds() <= 120):
            return False
        source, destination = authorization["source_ip"], authorization["destination_ip"]
        tunnel_id = authorization["tunnel_id"]
        expected_name = f"Tunnel{tunnel_id}_w".encode().hex()
        expected_bandwidth = struct.pack("!f", authorization["bandwidth_bps"] / 8).hex()
        expected_lspa = (bytes(12) + bytes((7, 7, 0, 0))).hex()
        expected_ownership = [18, source, destination, tunnel_id, IPv4Address(source).packed.hex()]
        reports, updates = peer["reports"], peer["updates"]
        if (not isinstance(reports, dict) or len(reports) > 4096 or
                not isinstance(updates, dict) or len(updates) > 32):
            return False
        matches = []
        for report in reports.values():
            ids = report.get("identifiers") or {}
            if (report.get("created") is not False or report.get("administrative_up") is not True or
                    report.get("removed") is not False or report.get("sync") is not False or
                    report.get("stale") is not False or report.get("operational_code") != 2 or
                    not isinstance(peer.get("session_id"), str) or
                    report.get("session_id") != peer["session_id"] or
                    ids.get("type") != 18 or ids.get("sender") != source or ids.get("endpoint") != destination or
                    ids.get("tunnel_id") != tunnel_id or ids.get("extended_tunnel_id_hex") != expected_ownership[4] or
                    report.get("symbolic_name_hex") != expected_name):
                continue
            ero = report.get("ero", [])
            if (not isinstance(ero, list) or [hop.get("address") for hop in ero] != authorization["ero"] or
                    any(hop.get("type") != 1 or hop.get("prefix_length") != 32 or hop.get("loose") is not False
                        for hop in ero)):
                continue
            attrs = report.get("attributes", [])
            if ([attr.get("body_hex") for attr in attrs
                 if (attr.get("class"), attr.get("type")) == (5, 1)] != [expected_bandwidth] or
                    [attr.get("body_hex") for attr in attrs
                     if (attr.get("class"), attr.get("type")) == (9, 1)] != [expected_lspa] or
                    any(attr.get("body_hex") != expected_bandwidth for attr in attrs
                        if (attr.get("class"), attr.get("type")) == (5, 5))):
                continue
            for key, update in updates.items():
                sent, acknowledged = update.get("sent_at"), update.get("acknowledged_at")
                received = report.get("received_at")
                if (any(isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)
                        for value in (sent, acknowledged, received)) or
                        not created.timestamp() <= sent <= expiry.timestamp() or
                        not sent <= acknowledged <= observed_now or not sent <= received <= observed_now):
                    continue
                if (type(update.get("srp_id")) is int and 0 < update["srp_id"] < 0xFFFFFFFF and
                        str(update["srp_id"]) == key and update.get("status") == "INITIAL_PATH" and
                        update.get("provisioning_plan_id") == authorization["plan_id"] and
                        update.get("acknowledged") is True and update.get("latest_reported_state") == "ACTIVE" and
                        update.get("acknowledged_identifiers") == ids and
                        update.get("ownership_key") == expected_ownership and
                        update.get("plsp_id") == report.get("plsp_id") and
                        update.get("sender") == source and update.get("endpoint") == destination and
                        update.get("tunnel_id") == tunnel_id and
                        update.get("bandwidth_bps") == authorization["bandwidth_bps"] and
                        update.get("setup_priority") == update.get("hold_priority") == 7 and
                        update.get("ero") == authorization["ero"] and
                        update.get("path_link_ids") == authorization["path_edges"]):
                    matches.append(update)
        return len(matches) == 1
    except (AttributeError, ValueError, OSError, KeyError, TypeError, OverflowError):
        return False
