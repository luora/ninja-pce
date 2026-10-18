"""Explicitly qualified, exact-plan RSVP provisioning behind the operator API.

Configuration creates the router object; the existing PCEP writer computes its
initial path. A durable send boundary forbids replay after uncertain delivery.
"""
from __future__ import annotations

import asyncio
import copy
import json
import math
import os
import re
import stat
import time
import uuid
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, cast

from .bgpls import parse_ospf_multi_area_policy
from .cspf import CspfNoPath, CspfSearchLimit, compute_cspf
from .models import DemandOutput, ManagedDevice, PathConstraints, ServiceDemand
from .pcep_compute import PcepPathComputer
from .reservations import ReservationLedger, resource_lock
from .southbound import PushPlan, SouthboundError, build_pce_push_plan, push_plan, verify_plan
from .state import SCHEMA_VERSION, digest, load_state, write_state

if TYPE_CHECKING:
    from .pcep_domain_policy import DomainDelegationPolicy
    from .provisioning_policy import TransportEnrollmentRegistry
    from .tunnel_api import ProvisioningManager


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _expiry(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("An aware plan expiry is required")
    return parsed


class _AdmissionRejected(ValueError):
    """A known refusal before this request acquires resources or write authority."""


_CAPABILITY_TEXT = {
    "backend_qualified": (("The configured backend is explicitly qualified.",
                           "The configured backend has not been qualified.",
                           "Backend qualification is unknown."),
                          "Complete the separate device-profile qualification before enabling provisioning."),
    "writes_enabled": (("The controller's existing write policy is enabled.",
                        "The controller's preview-only policy disables writes.",
                        "The controller's write policy is unknown."),
                       "Review the controller's preview-only policy with the authorized operator."),
    "topology_ready": (("A fresh, consistent topology snapshot is available.",
                        "The topology is not ready for provisioning.",
                        "A fresh, consistent topology snapshot could not be established."),
                       "Restore BGP-LS synchronization and validate the trusted topology policy."),
    "pcep_listener_ready": (("The current PCEP capture reports a running listener without a capture error.",
                             "The current PCEP capture reports a stopped listener or a capture error.",
                             "Current PCEP listener readiness could not be established."),
                            "Inspect the PCEP service and restore a fresh, valid listener capture."),
    "pcep_authorized": (("The current PCEP listener reports the exact provisioning authorization policy.",
                         "The current PCEP listener does not authorize this provisioning policy.",
                         "Current PCEP provisioning authorization could not be established."),
                        "Reconcile the listener's provisioning capability and trusted authorization policy."),
    "recovery_clear": (("The current listener capture reports no recovery barrier.",
                       "The current listener capture reports recovery requiring reconciliation.",
                       "Current recovery status could not be established."),
                      "Review retained operations and resolve recovery through the existing owned recovery procedure."),
    "topology_policy_matches": (("The listener and provisioning backend use the same OSPF area policy.",
                                "The listener and provisioning backend have different OSPF area policies.",
                                "The listener's current OSPF area policy could not be established."),
                               "Reconcile the listener and provisioning backend's trusted OSPF area policy."),
    "domain_eligible": (("The headend belongs to the current trusted provisioning scope.",
                         "The headend is outside the current trusted provisioning scope.",
                         "Current trusted headend membership could not be established."),
                        "Validate the headend's unique topology identity and trusted domain policy."),
    "transport_enrolled": (("The headend has an enrolled configuration transport.",
                            "The headend has no enrolled configuration transport.",
                            "Transport enrollment could not be established."),
                           "Enroll the headend's management transport using the existing trusted enrollment procedure."),
    "transport_ready": (("The enrolled transport meets the backend's current readiness policy.",
                         "The enrolled transport does not meet the backend's current readiness policy.",
                         "Current transport readiness could not be established."),
                        "Review the supported device profile, credential availability and independently pinned SSH identity."),
    "synchronized_headend": (("The selected PCC is fresh, established and synchronized.",
                              "The selected PCC is disconnected, stale or not synchronized.",
                              "A fresh, synchronized session for the selected PCC could not be established."),
                             "Restore the selected PCC session and wait for synchronization before requesting a new plan."),
}


def _capability_check(name: str, result: bool | None) -> dict[str, str]:
    """Fixed messages keep local errors, credentials and captures out of reports."""
    state = "unknown" if result is None else "pass" if result else "fail"
    messages, action = _CAPABILITY_TEXT[name]
    return {"state": state, "code": f"{name.upper()}_{state.upper()}",
            "message": messages[0 if result is True else 1 if result is False else 2],
            "next_action": action}


def _capability_number(value: Any) -> float | None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _capability_capture(path: Path) -> dict[str, Any]:
    """Read one bounded ordinary capture without touching locks or local state."""
    maximum = 16_777_216
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    except FileNotFoundError:
        return {}
    with os.fdopen(descriptor, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > maximum:
            raise ValueError("PCEP capability capture must be a bounded ordinary file")
        raw = handle.read(maximum + 1)
    if len(raw) > maximum:
        raise ValueError("PCEP capability capture exceeds its bound")

    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        document: dict[str, Any] = {}
        for key, value in pairs:
            if key in document:
                raise ValueError("Duplicate capability capture field")
            document[key] = value
        return document

    data = json.loads(raw, object_pairs_hook=unique)
    if not isinstance(data, dict) or type(data.get("version")) is not int or data["version"] != SCHEMA_VERSION:
        raise ValueError("Unsupported capability capture schema")
    checksum = data.pop("checksum", None)
    if checksum != digest(data):
        raise ValueError("Capability capture checksum mismatch")
    return data


class CliProvisioner:
    requires_plan = True
    backend_name = "router-config"
    independent_verification = True

    def __init__(self, computer: PcepPathComputer, state_dir: Path, devices: dict[str, ManagedDevice],
                 destinations: tuple[str, ...], pce_address: str, pcep_capture: Path, *,
                 qualified: bool = False, max_bandwidth_bps: int = 1_000_000,
                 connect_handler: Callable[..., Any] | None = None,
                 domain_policy: DomainDelegationPolicy | None = None,
                 transport_registry: TransportEnrollmentRegistry | None = None,
                 max_outstanding: int = 1, tunnel_id_min: int = 1000, tunnel_id_max: int | None = None,
                 max_operations: int | None = None, max_previews: int | None = None,
                 max_journal_bytes: int | None = None):
        self.domain_policy = domain_policy or getattr(computer, "domain_policy", None)
        computer_domain = getattr(computer, "domain_policy", None)
        if self.domain_policy is not None and (computer_domain is None or
                computer_domain.policy_digest != self.domain_policy.policy_digest):
            raise ValueError("Provisioning and path computation must use the same trusted domain policy")
        self.transport_registry = transport_registry
        if ((not devices and self.domain_policy is None) or len(devices) > 256 or
                any(device.topology_node_id != name for name, device in devices.items())):
            raise ValueError("Provisioning requires distinct configured headend devices")
        if type(qualified) is not bool or type(max_bandwidth_bps) is not int or not 0 < max_bandwidth_bps <= 10**9:
            raise ValueError("Invalid qualification or bandwidth policy")
        if self.domain_policy is None and (not destinations or len(set(destinations)) != len(destinations)):
            raise ValueError("Provisioning requires unique authorized destinations")
        from .pcep_provisioning import JournalProvisioningAuthorization

        self.computer, self.state_dir = computer, Path(state_dir)
        self.path = self.state_dir / "cli-provisioning.json"
        self.devices, self._allowed_destinations = dict(devices), destinations
        if transport_registry is not None and transport_registry.devices != self.devices:
            raise ValueError("Enrolled transport identities must match the executable device profiles")
        self._headends = {name: device.host for name, device in devices.items()}
        if self.domain_policy is None and any(computer.headends.get(peer) is None for peer in self._headends.values()):
            raise ValueError("Configured SSH headends must also be authorized PCC loopbacks")
        self.pce_address, self.pcep_capture = pce_address, Path(pcep_capture)
        self.qualified, self.max_bandwidth_bps = qualified, max_bandwidth_bps
        self.max_outstanding = max_outstanding
        self.tunnel_id_min = tunnel_id_min
        self.tunnel_id_max = tunnel_id_max if tunnel_id_max is not None else (1999 if self.domain_policy else 1015)
        self.max_operations = max_operations if max_operations is not None else (4096 if self.domain_policy else 16)
        self.max_previews = max_previews if max_previews is not None else (256 if self.domain_policy else 16)
        self.max_journal_bytes = max_journal_bytes if max_journal_bytes is not None else (
            16_777_216 if self.domain_policy else 1_048_576)
        for value, minimum, maximum in ((max_outstanding, 1, 256), (self.max_operations, 1, 4096),
                                         (self.max_previews, 1, 256),
                                         (self.max_journal_bytes, 1024, 16_777_216),
                                         (tunnel_id_min, 1, 65535), (self.tunnel_id_max, tunnel_id_min, 65535)):
            if type(value) is not int or not minimum <= value <= maximum:
                raise ValueError("Invalid bounded provisioning capacity policy")
        if max_outstanding > self.max_operations:
            raise ValueError("Outstanding operation limit exceeds retained operation history")
        self.manager: ProvisioningManager = self
        self.handler = connect_handler
        self.authorization = JournalProvisioningAuthorization(self.path, computer.headends,
                                                              domain_policy=self.domain_policy)
        policy = {"devices": {k: asdict(v) for k, v in devices.items()},
                  "destinations": destinations, "max_bandwidth_bps": max_bandwidth_bps,
                  "pce_address": pce_address, "max_outstanding": self.max_outstanding,
                  "qualified_versions": ["16.09.07", "16.9.7"]}
        identity_policy = getattr(computer, "ospf_multi_area", None)
        if identity_policy is not None:
            policy["ospf_multi_area_digest"] = identity_policy.digest
        if self.domain_policy is not None:
            policy.update(domain_policy_digest=self.domain_policy.policy_digest,
                          domain_policy=self.domain_policy.document(),
                          tunnel_id_min=self.tunnel_id_min, tunnel_id_max=self.tunnel_id_max,
                          max_operations=self.max_operations, max_previews=self.max_previews,
                          max_journal_bytes=self.max_journal_bytes,
                          transport_enrollment=(transport_registry.snapshot() if transport_registry else {}))
        self.policy_document = policy
        self.policy_digest = digest(policy)
        self._lock = asyncio.Lock()
        self._workers: set[asyncio.Task[None]] = set()
        self._plans: dict[str, PushPlan] = {}
        with resource_lock(self.state_dir):
            journal, ledger = self._load()
            if not ledger.path.exists():
                ledger.save()
            for op in journal["operations"].values():
                if op["status"] in {"PLANNED", "PUSHING"}:
                    op.update(status="ROLLBACK_REQUIRED", reason="Restart after an uncertain configuration attempt")
                    ledger.transition(op["reservation_id"], "ROLLBACK_REQUIRED")
            self._save_journal(journal)

    @property
    def headends(self) -> dict[str, str]:
        if self.domain_policy is None:
            return dict(self._headends)
        from .provisioning_policy import domain_headends

        topology, _ = self.computer.validated_snapshot()
        return domain_headends(topology, self.domain_policy)

    @property
    def allowed_destinations(self) -> tuple[str, ...]:
        if self.domain_policy is None:
            return self._allowed_destinations
        from .provisioning_policy import domain_destinations

        topology, _ = self.computer.validated_snapshot()
        return domain_destinations(topology, self.domain_policy)

    def _load(self) -> tuple[dict[str, Any], ReservationLedger]:
        if self.domain_policy is not None and self.transport_registry is not None:
            self.policy_document["transport_enrollment"] = self.transport_registry.snapshot()
            self.policy_digest = digest(self.policy_document)
        if self.path.exists() and self.path.stat().st_size > self.max_journal_bytes:
            raise ValueError("Provisioning journal exceeds the bounded history size")
        state = load_state(self.path)
        if not state:
            state = {"kind": "cli_tunnel_provisioning", "operations": {}, "previews": {},
                     "policy_digest": self.policy_digest, "reservation_id": "cli-provisioning-journal"}
            if self.domain_policy is not None:
                state.update(provisioning_schema=2, policies={self.policy_digest: self.policy_document})
        schema = state.get("provisioning_schema", 1)
        if type(schema) is not int or schema not in (1, 2):
            raise ValueError("Unsupported provisioning journal schema")
        versioned = schema == 2
        if (state.get("kind") != "cli_tunnel_provisioning" or
                not versioned and state.get("policy_digest") != self.policy_digest or
                not isinstance(state.get("operations"), dict) or not isinstance(state.get("previews"), dict) or
                len(state["operations"]) > 4096 or len(state["previews"]) > 256):
            raise ValueError("Provisioning journal policy or schema differs; reconcile it explicitly")
        if versioned:
            policies = state.get("policies")
            if (not isinstance(policies, dict) or not 1 <= len(policies) <= 4096 or
                    any(not isinstance(value, dict) or digest(value) != key for key, value in policies.items()) or
                    state.get("policy_digest") not in policies):
                raise ValueError("Invalid versioned provisioning policy history")
            # A policy revision applies only to new operations. Existing
            # authority and retries keep their original immutable revision.
            if self.policy_digest not in policies and len(policies) >= 4096:
                raise ValueError("Provisioning policy history is full; explicit archival is required")
            state["policies"][self.policy_digest] = copy.deepcopy(self.policy_document)
            state["policy_digest"] = self.policy_digest
        elif "provisioning_schema" in state:
            raise ValueError("Unsupported provisioning journal schema")
        ledger = ReservationLedger(self.state_dir)
        held_identities: set[str] = set()
        scoped_keys: set[tuple[str, str]] = set()
        for identifier, op in state["operations"].items():
            if (not isinstance(op, dict) or op.get("id") != identifier or
                    op.get("authorization_digest") != digest(op.get("authorization")) or
                    op.get("status") not in {"PLANNED", "PUSHING", "APPLIED_UNVERIFIED", "VERIFIED", "VERIFICATION_FAILED",
                                             "ROLLBACK_REQUIRED", "REJECTED", "ROLLED_BACK"}):
                raise ValueError("Invalid immutable provisioning operation")
            self._principal(op["principal"])
            scoped_key = (op["principal"], op.get("idempotency_key"))
            if (not isinstance(scoped_key[1], str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", scoped_key[1]) or
                    scoped_key in scoped_keys):
                raise ValueError("Duplicate or invalid principal-scoped operation key")
            scoped_keys.add(cast(tuple[str, str], scoped_key))
            op_policy: Any = (state["policies"].get(op.get("policy_revision")) if versioned else self.policy_document)
            if not isinstance(op_policy, dict):
                raise ValueError("Operation lost its original policy revision")
            authorization = op["authorization"]
            if authorization is None:
                self._validate_rejection(identifier, op, ledger)
                continue
            if (not isinstance(authorization, dict) or op.get("owner") != "cli-provision:" + identifier or
                    ("domain_policy_digest" not in op_policy and authorization.get("peer") !=
                     op_policy.get("devices", {}).get(str(op.get("headend")), {}).get("host")) or
                    any(authorization.get(field) != op.get(field)
                        for field in ("plan_id", "tunnel_id", "bandwidth_bps", "ero", "path_edges"))):
                raise ValueError("PCEP authority differs from the exact held provisioning operation")
            if op["status"] not in {"REJECTED", "ROLLED_BACK"}:
                if op["reservation_id"] in held_identities:
                    raise ValueError("Multiple operations claim the same headend tunnel identity")
                held_identities.add(op["reservation_id"])
            hold = ledger.entries.get(op["reservation_id"])
            if op["status"] not in {"REJECTED", "ROLLED_BACK"} and (
                    not hold or hold["owner"] != op["owner"] or hold.get("plan_id") != op["plan_id"] or
                    hold["current_bandwidth_bps"] != op["bandwidth_bps"] or
                    hold["current_edges"] != op["path_edges"]):
                raise ValueError("Unresolved provisioning operation lost its exact reservation hold")
            if op["status"] in {"REJECTED", "ROLLED_BACK"} and hold:
                raise ValueError("Released operation still holds resources; reconcile the durable journal")
        known_owners = {op["owner"] for op in state["operations"].values()}
        if any(entry["owner"].startswith("cli-provision:") and entry["owner"] not in known_owners
               for entry in ledger.entries.values()):
            raise ValueError("Orphan provisioning reservation requires explicit recovery")
        return state, ledger

    def _check_journal_size(self, state: dict[str, Any]) -> None:
        if len(json.dumps(state, indent=2, sort_keys=True, allow_nan=False).encode()) + 128 > self.max_journal_bytes:
            raise ValueError("Provisioning journal exceeds the bounded history size")

    def _save_journal(self, state: dict[str, Any]) -> None:
        self._check_journal_size(state)
        write_state(self.path, state)

    def _validate_rejection(self, identifier: str, op: dict[str, Any], ledger: ReservationLedger) -> None:
        fields = {"id", "principal", "idempotency_key", "intent", "plan_id", "authorization",
                  "authorization_digest", "owner", "reservation_id", "tunnel_id", "status",
                  "created_at", "reason", "headend", "destination", "bandwidth_bps"}
        if "policy_revision" in op:
            fields.add("policy_revision")
        if (set(op) != fields or not isinstance(identifier, str) or not re.fullmatch(r"[0-9a-f]{32}", identifier) or
                op["status"] != "REJECTED" or op["reservation_id"] is not None or op["tunnel_id"] is not None or
                op["owner"] != "cli-provision:" + identifier or
                not isinstance(op["plan_id"], str) or not re.fullmatch(r"[0-9a-f]{64}", op["plan_id"]) or
                not isinstance(op["idempotency_key"], str) or
                not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", op["idempotency_key"]) or
                not isinstance(op["reason"], str) or not 1 <= len(op["reason"]) <= 256 or
                not isinstance(op["created_at"], str) or
                any(entry["owner"] == op["owner"] for entry in ledger.entries.values())):
            raise ValueError("Invalid released admission rejection")
        self._principal(op["principal"])
        self._intent_shape(op["intent"])
        _expiry(op["created_at"])
        if any(op[field] != op["intent"][field] for field in ("headend", "destination", "bandwidth_bps")):
            raise ValueError("Released admission rejection differs from its exact intent")

    @staticmethod
    def _principal(principal: str) -> None:
        if not isinstance(principal, str) or not re.fullmatch(r"[A-Za-z0-9_.:@-]{1,128}", principal):
            raise ValueError("Invalid operator principal")

    def list(self, principal: str | None = None) -> list[dict[str, Any]]:
        if principal is not None:
            self._principal(principal)
        with resource_lock(self.state_dir):
            state, _ = self._load()
            return copy.deepcopy([op for op in state["operations"].values()
                                  if principal is None or op["principal"] == principal])

    def get(self, op_id: str, principal: str | None = None) -> dict[str, Any]:
        matches = [op for op in self.list(principal) if op["id"] == op_id]
        if len(matches) != 1:
            raise ValueError("Unknown operation")
        return matches[0]

    def capability_observations(self) -> dict[str, Any]:
        """Informational prerequisites, sampled once without admission or writes.

        These observations never establish exclusive ownership, perform router
        I/O, inspect reservations or replace the durable submission guards.
        Legacy optional recovery and area-policy fields retain their existing
        readiness meaning. Missing required evidence remains unknown.
        """
        names = ("backend_qualified", "writes_enabled", "topology_ready", "pcep_listener_ready",
                 "pcep_authorized", "recovery_clear", "topology_policy_matches")
        checks = {name: _capability_check(name, None) for name in names}
        checks["backend_qualified"] = _capability_check("backend_qualified", self.qualified)
        checks["writes_enabled"] = _capability_check(
            "writes_enabled", os.environ.get("NINJA_PCE_PREVIEW_ONLY", "").lower() not in {"1", "true", "yes", "on"})
        headends: dict[str, str] = {}
        destinations: tuple[str, ...] = ()
        try:
            topology, _ = self.computer.validated_snapshot()
            if self.domain_policy is None:
                headends, destinations = dict(self._headends), self._allowed_destinations
            else:
                from .provisioning_policy import domain_destinations, domain_headends

                headends = domain_headends(topology, self.domain_policy)
                destinations = domain_destinations(topology, self.domain_policy)
            checks["topology_ready"] = _capability_check("topology_ready", True)
        except (ValueError, OSError, RuntimeError, KeyError, TypeError, RecursionError):
            # Do not retain previously selectable endpoints after a failed TED.
            headends, destinations = {}, ()

        now = time.time()
        try:
            capture = _capability_capture(self.pcep_capture)
        except (ValueError, OSError, UnicodeError, TypeError, RecursionError, OverflowError):
            capture = {}
        generated = _capability_number(capture.get("generated_at"))
        current = generated is not None and -1 <= now - generated <= 5
        peers: Any = None
        if current:
            running = capture.get("listener_running")
            listener = False if capture.get("capture_error") else running if type(running) is bool else None
            checks["pcep_listener_ready"] = _capability_check("pcep_listener_ready", listener)
            capabilities = capture.get("capabilities")
            peers = capture.get("peers")
            policy_digest = capture.get("provisioning_policy_digest")
            capability = capabilities.get("configuration_provisioning") if isinstance(capabilities, dict) else None
            authorized: bool | None = None
            if capability is False or isinstance(policy_digest, str) and policy_digest != self.authorization.policy_digest:
                authorized = False
            elif capability is True and policy_digest == self.authorization.policy_digest and isinstance(peers, dict):
                authorized = True
            checks["pcep_authorized"] = _capability_check("pcep_authorized", authorized)

            recovery = capture.get("failure_recovery")
            recovery_clear: bool | None = (True if recovery is None else not bool(recovery.get("pending"))
                                           if isinstance(recovery, dict) else None)
            if "managed_failure_recovery" in capture:
                managed = capture["managed_failure_recovery"]
                managed_clear = (not bool(managed.get("pending") or managed.get("blocked") or
                                          managed.get("status") == "RECOVERY_REQUIRED")
                                 if isinstance(managed, dict) else None)
                recovery_clear = (False if recovery_clear is False or managed_clear is False else
                                  None if recovery_clear is None or managed_clear is None else True)
            checks["recovery_clear"] = _capability_check("recovery_clear", recovery_clear)

            identity_policy = getattr(self.computer, "ospf_multi_area", None)
            expected_policy = (None if identity_policy is None else {
                "ospf_multi_area": identity_policy.document(), "ospf_multi_area_digest": identity_policy.digest})
            actual_policy = capture.get("topology_policy")
            matching_policy: bool | None = None
            if expected_policy is None:
                matching_policy = "topology_policy" not in capture
            elif isinstance(actual_policy, dict):
                matching_policy = actual_policy == expected_policy
            checks["topology_policy_matches"] = _capability_check("topology_policy_matches", matching_policy)

        details: dict[str, Any] = {}
        for name, address in headends.items():
            enrolled = name in self.devices
            transport: bool | None = enrolled
            if enrolled and self.transport_registry is not None:
                try:
                    observed, _ = self.transport_registry.readiness(name)
                    transport = observed if type(observed) is bool else None
                except (ValueError, OSError, UnicodeError, RuntimeError, TypeError):
                    transport = None
            synchronized: bool | None = None
            peer = peers.get(address) if isinstance(peers, dict) else None
            if isinstance(peer, dict):
                state, fresh, synced, until = (peer.get("state"), peer.get("fresh"),
                                               peer.get("synchronized"), peer.get("valid_until"))
                until_number = _capability_number(until)
                if (isinstance(state, str) and state != "ESTABLISHED" or fresh is False or synced is False or
                        until_number is not None and until_number <= now):
                    synchronized = False
                elif state == "ESTABLISHED" and fresh is True and synced is True and until_number is not None:
                    synchronized = until_number > now
            details[name] = {"checks": {
                "domain_eligible": _capability_check("domain_eligible", True),
                "transport_enrolled": _capability_check("transport_enrolled", enrolled),
                "transport_ready": _capability_check("transport_ready", transport),
                "synchronized_headend": _capability_check("synchronized_headend", synchronized),
            }}
        return {"checks": checks, "headends": details, "headend_addresses": dict(headends),
                "destinations": list(destinations)}

    def readiness(self, headend: str | None = None) -> tuple[bool, str]:
        try:
            if not self.qualified:
                raise ValueError("Configuration provisioning has not been qualified for this device profile.")
            if os.environ.get("NINJA_PCE_PREVIEW_ONLY", "").lower() in {"1", "true", "yes", "on"}:
                raise ValueError("Controller writes are disabled.")
            self.computer.validated_snapshot()
            headends = self.headends
            if headend is None:
                results = [self.readiness(name) for name in headends]
                return (True, "") if any(ready for ready, _ in results) else (
                    False, "No managed headend is ready for provisioning.")
            if headend not in headends:
                raise ValueError("The selected headend is outside the current trusted domain.")
            if headend not in self.devices:
                raise ValueError("The selected managed headend has no enrolled provisioning transport.")
            if self.transport_registry is not None:
                ready, reason = self.transport_registry.readiness(headend)
                if not ready:
                    raise ValueError(reason)
            capture = load_state(self.pcep_capture)
            generated = capture.get("generated_at")
            if (isinstance(generated, bool) or not isinstance(generated, (int, float)) or not math.isfinite(generated) or
                    not -1 <= time.time() - generated <= 5):
                raise ValueError("PCEP listener capture is unavailable or stale.")
            capabilities, recovery, peers = (capture.get("capabilities"), capture.get("failure_recovery"),
                                               capture.get("peers"))
            if (capture.get("capture_error") or not capture.get("listener_running") or
                    capture.get("provisioning_policy_digest") != self.authorization.policy_digest or
                    not isinstance(capabilities, dict) or capabilities.get("configuration_provisioning") is not True or
                    not isinstance(peers, dict) or
                    recovery is not None and (not isinstance(recovery, dict) or recovery.get("pending"))):
                raise ValueError("PCEP provisioning authorization is unavailable or recovery is pending.")
            if "managed_failure_recovery" in capture:
                managed_recovery = capture["managed_failure_recovery"]
                if (not isinstance(managed_recovery, dict) or managed_recovery.get("pending") or
                        managed_recovery.get("blocked") or managed_recovery.get("status") == "RECOVERY_REQUIRED"):
                    raise ValueError("Managed tunnel recovery is pending or requires reconciliation.")
            identity_policy = getattr(self.computer, "ospf_multi_area", None)
            expected_policy = (None if identity_policy is None else {
                "ospf_multi_area": identity_policy.document(), "ospf_multi_area_digest": identity_policy.digest,
            })
            if (capture.get("topology_policy") != expected_policy or
                    expected_policy is None and "topology_policy" in capture):
                raise ValueError("PCEP listener OSPF area policy differs from provisioning policy.")
            for name in [headend]:
                peer = peers.get(headends.get(name), {})
                if (not isinstance(peer, dict) or peer.get("state") != "ESTABLISHED" or peer.get("fresh") is not True or
                        peer.get("synchronized") is not True or type(peer.get("valid_until")) not in (int, float) or
                        peer["valid_until"] <= time.time()):
                    raise ValueError("The selected PCC is not fresh and synchronized.")
            return True, ""
        except (ValueError, OSError, KeyError, TypeError) as exc:
            return False, str(exc)[:256]

    def node_readiness(self) -> dict[str, dict[str, Any]]:
        headends = self.headends
        try:
            capture = load_state(self.pcep_capture)
            peers = capture.get("peers", {})
        except (ValueError, OSError):
            peers = {}
        result = {}
        for name, peer_address in headends.items():
            ready, reason = self.readiness(name)
            peer = peers.get(peer_address, {}) if isinstance(peers, dict) else {}
            pcc_ready = bool(isinstance(peer, dict) and peer.get("state") == "ESTABLISHED" and
                             peer.get("fresh") is True and peer.get("synchronized") is True and
                             type(peer.get("valid_until")) in (int, float) and peer["valid_until"] > time.time())
            transport_ready = (self.transport_registry.readiness(name)[0] if self.transport_registry else
                               name in self.devices)
            result[name] = {"enabled": ready, "reason": reason, "transport_enrolled": name in self.devices,
                            "transport_ready": transport_ready, "pcc_ready": pcc_ready}
        return result

    def _transport_revision(self, node: str) -> str:
        if self.transport_registry is not None:
            return self.transport_registry.revision(node)
        if node not in self.devices:
            raise ValueError("The selected managed headend has no enrolled provisioning transport")
        return digest(asdict(self.devices[node]))

    def _used_tunnel_ids(self, state: dict[str, Any], ledger: ReservationLedger, headend: str,
                         principal: str | None = None) -> set[int]:
        peer_address = self.headends[headend]
        used = {op["tunnel_id"] for op in state["operations"].values()
                if (op["headend"] == headend or isinstance(op.get("authorization"), dict) and
                    op["authorization"].get("peer") == peer_address) and
                op["status"] not in {"REJECTED", "ROLLED_BACK"}}
        used.update(p["tunnel_id"] for p in state["previews"].values()
                    if (p["headend"] == headend or p.get("headend_peer") == peer_address) and
                    _expiry(p["expires_at"]) > _now() and
                    p["principal"] != principal)
        prefix = headend + ":iosxe:Tunnel"
        used.update(int(identity[len(prefix):]) for identity in ledger.entries
                    if identity.startswith(prefix) and identity[len(prefix):].isdecimal())
        capture = load_state(self.pcep_capture)
        peer = capture.get("peers", {}).get(self.headends.get(headend), {})
        reports = peer.get("reports", {})
        if not isinstance(reports, dict) or len(reports) > 4096:
            raise ValueError("PCC resource inventory is malformed or exceeds its bound")
        used.update(ids["tunnel_id"] for report in reports.values() if isinstance(report, dict)
                    for ids in [report.get("identifiers")] if isinstance(ids, dict) and
                    type(ids.get("tunnel_id")) is int)
        return used

    def _intent(self, intent: dict[str, Any]) -> None:
        self._intent_shape(intent)
        if (intent["headend"] not in self.headends or intent["destination"] not in self.allowed_destinations or
                intent["bandwidth_bps"] > self.max_bandwidth_bps or self.domain_policy is not None and
                intent["bandwidth_bps"] > self.domain_policy.max_bandwidth_bps):
            raise ValueError("Intent exceeds the authorized provisioning policy")

    @staticmethod
    def _intent_shape(intent: dict[str, Any]) -> None:
        if (not isinstance(intent, dict) or set(intent) != {"headend", "destination", "bandwidth_bps"} or
                any(not isinstance(intent[field], str) or not 1 <= len(intent[field]) <= 128
                    for field in ("headend", "destination")) or
                intent["headend"] == intent["destination"] or type(intent["bandwidth_bps"]) is not int or
                not 0 < intent["bandwidth_bps"] <= 10**12):
            raise ValueError("Intent exceeds the authorized provisioning policy")

    def _compute(self, intent: dict[str, Any], op_id: str, tunnel_id: int,
                 ledger: ReservationLedger) -> tuple[Any, PushPlan, str]:
        if intent["headend"] not in self.devices:
            raise ValueError("The selected managed headend has no enrolled provisioning transport")
        topology, revision = self.computer.validated_snapshot()
        if self.domain_policy is not None:
            topology = self.domain_policy.graph(topology)
        conservative = replace(topology, raw_metadata={**topology.raw_metadata, "authoritative": False})
        effective = ledger.effective_topology(conservative, "cli-provision:" + op_id)
        bps = ((intent["bandwidth_bps"] + 999) // 1000) * 1000
        if bps > self.max_bandwidth_bps or self.domain_policy is not None and bps > self.domain_policy.max_bandwidth_bps:
            raise ValueError("Rounded Cisco bandwidth exceeds policy")
        demand = ServiceDemand("NINJA_CFG_" + op_id, intent["headend"], intent["destination"], bps / 1e6,
                               reservation_bw_bps=bps, constraints=PathConstraints(max_candidates=1),
                               output=DemandOutput(tunnel_id=tunnel_id, tunnel_name="NINJA_CFG_" + op_id,
                                                   explicit_path_name="EP_NINJA_" + op_id, formats=("iosxe",)))
        result = compute_cspf(effective, demand)
        plan = build_pce_push_plan(result, effective, self.devices[intent["headend"]], self.pce_address)
        return result, plan, revision

    async def preview(self, principal: str, intent: dict[str, Any]) -> dict[str, Any]:
        self._principal(principal)
        self._intent(intent)
        async with self._lock:
            with resource_lock(self.state_dir):
                state, ledger = self._load()
                if len(state["operations"]) >= self.max_operations or sum(
                        op["status"] not in {"REJECTED", "ROLLED_BACK"}
                        for op in state["operations"].values()) >= self.max_outstanding:
                    raise ValueError("Provisioning capacity or retained operation history is full")
                # Previews allocate no bandwidth and never contact a device.
                used = self._used_tunnel_ids(state, ledger, intent["headend"], principal)
                tunnel_id = next((number for number in range(self.tunnel_id_min, self.tunnel_id_max + 1)
                                  if number not in used), None)
                if tunnel_id is None:
                    raise ValueError("Managed tunnel ID allocation is exhausted")
                op_id = uuid.uuid4().hex
                result, plan, revision = self._compute(intent, op_id, tunnel_id, ledger)
                state["previews"] = {k: v for k, v in state["previews"].items()
                                     if (v["op_id"] in state["operations"] and
                                         state["operations"][v["op_id"]]["status"] == "PLANNED") or
                                     (_expiry(v["expires_at"]) > _now() and
                                      (v["principal"] != principal or v["headend"] != intent["headend"]))}
                if len(state["previews"]) >= self.max_previews:
                    raise ValueError("Too many active previews")
                preview = {"plan_id": plan.plan_id, "principal": principal, "op_id": op_id,
                           "intent": intent, "tunnel_id": tunnel_id, "topology_digest": revision,
                           "expires_at": (_now() + timedelta(seconds=60)).isoformat(),
                           "rendered_config": plan.rendered_config, "path_nodes": list(result.path_nodes),
                           "path_edges": [edge.id for edge in result.path_links],
                           "ero": [hop.address for hop in result.explicit_hops],
                           "bandwidth_bps": result.demand.bandwidth_bps,
                           "headend": intent["headend"], "destination": intent["destination"]}
                if state.get("provisioning_schema") == 2:
                    preview.update(policy_revision=self.policy_digest,
                                   headend_peer=self.headends[intent["headend"]],
                                   transport_revision=self._transport_revision(intent["headend"]))
                state["previews"][plan.plan_id] = preview
                self._save_journal(state)
                self._plans = {key: value for key, value in self._plans.items() if key in state["previews"]}
                self._plans[plan.plan_id] = plan
                return copy.deepcopy(preview)

    @staticmethod
    def _same_plan(left: PushPlan, right: PushPlan) -> bool:
        # Collector timestamps change without a graph change. Recompute against
        # fresh resources, then compare every executable and verification field.
        return all(getattr(left, name) == getattr(right, name) for name in (
            "device", "output_format", "rendered_config", "commands", "precheck_commands",
            "postcheck_commands", "demand_revision", "device_revision", "config_revision", "expectation"))

    def _check_preview(self, state: dict[str, Any], ledger: ReservationLedger, principal: str,
                       intent: dict[str, Any], plan_id: str) -> tuple[dict[str, Any], Any, PushPlan]:
        preview = state["previews"].get(plan_id)
        plan = self._plans.get(plan_id)
        if (not isinstance(preview, dict) or plan is None or preview["principal"] != principal or
                preview["intent"] != intent or _expiry(preview["expires_at"]) <= _now()):
            raise _AdmissionRejected("The exact reviewed plan expired or changed; request a new preview")
        if state.get("provisioning_schema") == 2 and (
                preview.get("policy_revision") != self.policy_digest or
                preview.get("headend_peer") != self.headends.get(intent["headend"]) or
                preview.get("transport_revision") != self._transport_revision(intent["headend"])):
            raise _AdmissionRejected("Provisioning policy or pinned transport identity changed after preview")
        result, current, _ = self._compute(intent, preview["op_id"], preview["tunnel_id"], ledger)
        if not self._same_plan(plan, current) or preview["path_edges"] != [e.id for e in result.path_links]:
            raise _AdmissionRejected("Fresh topology or resources changed the approved plan")
        return preview, result, plan

    def _reject_admission(self, state: dict[str, Any], principal: str, key: str,
                          intent: dict[str, Any], plan_id: str, reason: str) -> dict[str, Any]:
        """Bind an exact request to a durable no-write result, without a reservation."""
        identifier = uuid.uuid4().hex
        operation = {"id": identifier, "principal": principal, "idempotency_key": key,
                     "intent": copy.deepcopy(intent), "plan_id": plan_id, "authorization": None,
                     "authorization_digest": digest(None), "owner": "cli-provision:" + identifier,
                     "reservation_id": None, "tunnel_id": None, "status": "REJECTED",
                     "created_at": _now().isoformat(), "reason": "Admission rejected before configuration: " + reason[:192],
                     "headend": intent["headend"], "destination": intent["destination"],
                     "bandwidth_bps": intent["bandwidth_bps"]}
        if state.get("provisioning_schema") == 2:
            operation["policy_revision"] = self.policy_digest
        state["operations"][identifier] = operation
        self._save_journal(state)
        return copy.deepcopy(operation)

    async def create(self, principal: str, key: str, intent: dict[str, Any], *,
                     approved_plan_id: str) -> dict[str, Any]:
        self._principal(principal)
        self._intent_shape(intent)
        if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", key):
            raise ValueError("Invalid idempotency key")
        if not isinstance(approved_plan_id, str) or not re.fullmatch(r"[0-9a-f]{64}", approved_plan_id):
            raise ValueError("An exact reviewed plan is required")
        async with self._lock:
            with resource_lock(self.state_dir):
                state, ledger = self._load()
                for old in state["operations"].values():
                    if old["principal"] == principal and old["idempotency_key"] == key:
                        if old["intent"] != intent or old["plan_id"] != approved_plan_id:
                            raise ValueError("Idempotency key is already bound to another exact intent and plan")
                        return copy.deepcopy(old)
                if len(state["operations"]) >= self.max_operations:
                    raise ValueError("Provisioning operation history is full; explicit recovery is required")
                # Do not turn corrupt capture persistence into an ordinary
                # readiness rejection. Journal/ledger loading and all writes
                # likewise remain outside the known-admission refusal catch.
                load_state(self.pcep_capture)
                try:
                    try:
                        self._intent(intent)
                    except ValueError as exc:
                        raise _AdmissionRejected(str(exc)) from exc
                    ready, reason = self.readiness(intent["headend"])
                    if not ready:
                        raise _AdmissionRejected(reason)
                    if sum(op["status"] not in {"REJECTED", "ROLLED_BACK"}
                           for op in state["operations"].values()) >= self.max_outstanding:
                        raise _AdmissionRejected("Provisioning concurrent service capacity is full")
                    preview, result, plan = self._check_preview(state, ledger, principal, intent, approved_plan_id)
                    if preview["op_id"] in state["operations"]:
                        raise _AdmissionRejected("This exact plan already belongs to a recorded operation")
                    # Exclude only this preview; every current held/observed
                    # resource and every other principal's lease must agree.
                    if preview["tunnel_id"] in self._used_tunnel_ids(state, ledger, intent["headend"], principal):
                        raise _AdmissionRejected("The approved headend tunnel identity is already in use")
                except (_AdmissionRejected, CspfNoPath, CspfSearchLimit) as exc:
                    return self._reject_admission(state, principal, key, intent, approved_plan_id, str(exc))
                op_id, tunnel_id = preview["op_id"], preview["tunnel_id"]
                owner = "cli-provision:" + op_id
                identity = f"{intent['headend']}:iosxe:Tunnel{tunnel_id}"
                topology, _ = self.computer.validated_snapshot()
                source = topology.nodes[intent["headend"]].loopback
                if (self.domain_policy is None and
                        self.computer.headends[self.headends[intent["headend"]]] != source or
                        self.domain_policy is not None and
                        (preview["headend_peer"] != source or
                         self.domain_policy.source(self.headends[intent["headend"]], topology) != source)):
                    raise ValueError("PCC identity does not match the selected topology headend")
                authorization = {"plan_id": plan.plan_id, "peer": self.headends[intent["headend"]],
                                 "source_ip": source, "destination_ip": topology.nodes[intent["destination"]].loopback,
                                 "tunnel_id": tunnel_id, "symbolic_name": f"Tunnel{tunnel_id}_w",
                                 "bandwidth_bps": preview["bandwidth_bps"], "setup_priority": 7, "hold_priority": 7,
                                 "expires_at": (_now() + timedelta(seconds=119)).isoformat(),
                                 "ero": preview["ero"], "path_edges": preview["path_edges"]}
                operation = {**preview, "id": op_id, "idempotency_key": key, "owner": owner,
                             "reservation_id": identity, "status": "PLANNED", "created_at": _now().isoformat(),
                             "authorization": authorization, "authorization_digest": digest(authorization),
                             "reason": "Delivering the exact reviewed router configuration"}
                state["operations"][op_id] = operation
                self._check_journal_size(state)
                ledger.reserve(identity, owner, result, plan.plan_id)
                self._save_journal(state)
            task = asyncio.create_task(asyncio.to_thread(self._execute, op_id, plan))
            self._workers.add(task)
            task.add_done_callback(self._workers.discard)
            return copy.deepcopy(operation)

    def _prewrite(self, op_id: str, plan: PushPlan) -> None:
        with resource_lock(self.state_dir):
            state, ledger = self._load()
            op = state["operations"][op_id]
            ready, reason = self.readiness(op["headend"])
            if not ready or op["status"] != "PLANNED":
                raise ValueError(reason or "Provisioning operation no longer owns write authority")
            self._check_preview(state, ledger, op["principal"], op["intent"], plan.plan_id)
            if _expiry(op["authorization"]["expires_at"]) <= _now():
                raise ValueError("Initial PCEP authorization expired before delivery")
            # Only after independent SSH absence/reachability prechecks may
            # this identity obtain initial-path PCEP authority.
            op["status"] = "PUSHING"
            ledger.transition(op["reservation_id"], "PUSHING")
            self._save_journal(state)

    def _pce_control(self, outputs: tuple[tuple[str, str], ...]) -> bool:
        text = "\n".join(value for _, value in outputs)
        # Require the observed active option, remote peer, and delegation. The
        # generic verifier independently establishes this exact tunnel's RSVP.
        return bool(re.search(r"(?im)^\s*path option 1, \(PCE address " + re.escape(self.pce_address) +
                              r"\) type dynamic \(Basis for Setup(?:, path weight \d+)?\)\s*$", text) and
                    re.search(r"(?im)^\s*Delegation state:\s*Working:\s*yes\s+Protect:\s*no\s*$", text) and
                    re.search(r"(?im)^\s*Delegation peer:\s*" + re.escape(self.pce_address) + r"\s*$", text) and
                    re.search(r"(?im)^\s*Created via PCUpd message from PCE server:\s*" +
                              re.escape(self.pce_address) + r"\s*$", text))

    def _completed_path(self, op_id: str) -> bool:
        from .pcep_provisioning import configured_initial_path_observed

        return configured_initial_path_observed(load_state(self.pcep_capture), self.get(op_id),
                                                policy_digest=self.authorization.policy_digest)

    def _execute(self, op_id: str, plan: PushPlan) -> None:
        # Separate per-endpoint locks serialize router CLI sessions across
        # threads/processes sharing this state directory. Other headends retain
        # parallel delivery; the global admission lock never spans network I/O.
        endpoint = digest({"host": plan.device.host, "port": plan.device.port})
        with resource_lock(self.state_dir / "delivery-locks" / endpoint):
            self._execute_locked(op_id, plan)

    def _execute_locked(self, op_id: str, plan: PushPlan) -> None:
        push_returned = False
        try:
            operation = self.get(op_id)
            result = push_plan(plan, execute=True, approved=True, approved_plan_id=plan.plan_id,
                               approval_expires_at=operation["expires_at"], connect_handler=self.handler,
                               qualified_versions=("16.09.07", "16.9.7"),
                               verification_attempts=3, read_timeout=15,
                               pre_write_guard=lambda: self._prewrite(op_id, plan))
            push_returned = True
            # Initial path setup can finish after the configuration response.
            # These additional observations never retry a configuration write.
            if result.status != "ROLLBACK_REQUIRED":
                for _ in range(4):
                    if (result.verified and self._pce_control(result.postcheck_output) and
                            self._completed_path(op_id)):
                        break
                    time.sleep(2)
                    result = verify_plan(plan, connect_handler=self.handler, verification_attempts=1, read_timeout=15)
            status = result.status
            if status == "VERIFIED" and not (self._pce_control(result.postcheck_output) and
                                            self._completed_path(op_id)):
                status = "VERIFICATION_FAILED"
            with resource_lock(self.state_dir):
                state, ledger = self._load()
                op = state["operations"][op_id]
                # Evidence is private and durable before exposing success.
                evidence_path = self.state_dir / ("cli-evidence-" + op_id + ".json")
                write_state(evidence_path, {"kind": "cli_provisioning_evidence", "result": asdict(result)})
                op.update(status=status, observed_at=_now().isoformat(),
                          reason=("Headend RSVP and initial PCEP path control verified" if status == "VERIFIED"
                                  else "Inspect retained configuration and reservations; no automatic resend or deletion"))
                if status == "VERIFIED":
                    op["verification"] = {"independent": True, "scope": "headend-rsvp", "pcep_control": True,
                                          "checks": ["identity", "destination", "bandwidth", "ero", "signaling"]}
                ledger.transition(op["reservation_id"], status)
                self._save_journal(state)
        except SouthboundError as exc:
            # push_plan raises only before its first configuration operation;
            # post-write failures return a retained ROLLBACK_REQUIRED result.
            with resource_lock(self.state_dir):
                state, ledger = self._load()
                op = state["operations"][op_id]
                write_state(self.state_dir / ("cli-evidence-" + op_id + ".json"), {
                    "kind": "cli_provisioning_evidence", "failure": str(exc), "plan_id": plan.plan_id,
                    "stage": "postwrite-observation" if push_returned else "prewrite",
                })
                if push_returned:
                    op.update(status="ROLLBACK_REQUIRED", reason="Post-write observation failed; reservation retained")
                    ledger.transition(op["reservation_id"], "ROLLBACK_REQUIRED")
                else:
                    op.update(status="REJECTED", reason="Delivery rejected before configuration was sent")
                    # Persist rejection first: interruption can retain an extra
                    # hold, but must never forget an unresolved delivery.
                    self._save_journal(state)
                    ledger.assert_owner(op["reservation_id"], op["owner"])
                    del ledger.data["entries"][op["reservation_id"]]
                    ledger.save()
                self._save_journal(state)
        except Exception:
            with resource_lock(self.state_dir):
                state, ledger = self._load()
                op = state["operations"][op_id]
                op.update(status="ROLLBACK_REQUIRED", reason="Uncertain delivery; retained reservation requires recovery")
                ledger.transition(op["reservation_id"], "ROLLBACK_REQUIRED")
                self._save_journal(state)

    async def close(self) -> None:
        if self._workers:
            await asyncio.gather(*tuple(self._workers), return_exceptions=True)


def _runtime_policy_path(value: Any, label: str, config: dict[str, Any],
                         config_path: Path | None) -> Path:
    """Keep authorization documents separate from mutable observation and state."""
    if not isinstance(value, str) or not value or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError(f"{label} must be an explicit JSON file path")
    policy_path = Path(value)
    if policy_path.suffix.lower() != ".json":
        raise ValueError(f"{label} must be an explicit JSON file")
    other_paths = [Path(config["computer"][key]) for key in ("raw_path", "status_path", "topology_path")]
    other_paths.extend(Path(config[key]) for key in
                       ("inventory", "known_hosts", "pcep_capture", "socket_path") if key in config)
    other_policy = (config.get("domain_policy") if label == "OSPF multi-area policy" else
                    config["computer"].get("ospf_multi_area_policy"))
    if isinstance(other_policy, str):
        other_paths.append(Path(other_policy))
    if config_path is not None:
        other_paths.append(Path(config_path))
    resolved = policy_path.resolve()
    for path in other_paths:
        same_file = resolved == path.resolve()
        if not same_file and policy_path.exists() and path.exists():
            same_file = os.path.samefile(policy_path, path)
        if same_file:
            raise ValueError(f"{label} must be distinct from runtime inputs and outputs")
    if "state_dir" in config and resolved.is_relative_to(Path(config["state_dir"]).resolve()):
        raise ValueError(f"{label} must be outside the mutable provisioning state directory")
    return policy_path


def _runtime_computer(config: dict[str, Any], *, config_path: Path | None = None) -> PcepPathComputer:
    """Load explicit domain and area identity independently of runtime outputs."""
    from .loaders import _load_mapping
    from .pcep_domain_policy import DomainDelegationPolicy

    computer_config = dict(config["computer"])
    allowed = {"raw_path", "status_path", "topology_path", "required_peers", "node_names", "headends",
               "max_age_seconds", "ospf_multi_area_policy"}
    if set(computer_config) - allowed:
        raise ValueError("Unknown provisioning computer fields")
    for key in ("raw_path", "status_path", "topology_path"):
        computer_config[key] = Path(computer_config[key])
    computer_config["required_peers"] = tuple(computer_config["required_peers"])
    if "ospf_multi_area_policy" in computer_config:
        policy_path = _runtime_policy_path(computer_config.pop("ospf_multi_area_policy"),
                                           "OSPF multi-area policy", config, config_path)
        computer_config["ospf_multi_area"] = parse_ospf_multi_area_policy(_load_mapping(policy_path))
    if "domain_policy" in config:
        policy_path = _runtime_policy_path(config["domain_policy"], "Provisioning domain policy", config, config_path)
        computer_config["domain_policy"] = DomainDelegationPolicy.from_path(policy_path)
        computer_config.setdefault("node_names", {})
        computer_config.setdefault("headends", {})
    return PcepPathComputer(**computer_config)


async def serve(config: dict[str, Any], *, config_path: Path | None = None) -> None:
    """Run only the configured Unix API; the authenticated reverse proxy owns TLS."""
    import signal

    from netmiko import ConnectHandler

    from .loaders import load_inventory
    from .provisioning_policy import TransportEnrollmentRegistry
    from .tunnel_api import TunnelAPI

    computer = _runtime_computer(config, config_path=config_path)
    inventory = load_inventory(config["inventory"]) if "inventory" in config else None
    domain_policy = computer.domain_policy
    if domain_policy is not None:
        if config.get("principal_policy", "authenticated") != "authenticated" or config.get("operators"):
            raise ValueError("Domain provisioning requires authenticated principals without an operator allowlist")
        if "headends" in config or "destinations" in config:
            raise ValueError("Domain provisioning discovers headends and destinations from the trusted topology")
        devices = {device.topology_node_id: device for device in inventory.devices.values()} if inventory else {}
    else:
        if inventory is None:
            raise ValueError("Legacy provisioning requires a transport inventory")
        devices = {name: inventory.device_for_node(name) for name in config["headends"]}
    known_hosts = Path(config["known_hosts"]) if "known_hosts" in config else None
    if devices and (known_hosts is None or not known_hosts.is_file() or known_hosts.is_symlink()):
        raise ValueError("An independently pinned SSH host-key file is required")
    transports = TransportEnrollmentRegistry(devices, known_hosts)

    def connect(**kwargs: Any) -> Any:
        return ConnectHandler(**kwargs, ssh_strict=True, system_host_keys=False,
                              alt_host_keys=True, alt_key_file=str(known_hosts))

    limits = {field: config[field] for field in ("max_outstanding", "tunnel_id_min", "tunnel_id_max",
                                               "max_operations", "max_previews", "max_journal_bytes")
              if field in config}
    backend = CliProvisioner(computer, Path(config["state_dir"]), devices, tuple(config.get("destinations", ())),
                             config["pce_address"], Path(config["pcep_capture"]), qualified=config.get("qualified", False),
                             max_bandwidth_bps=config.get("max_bandwidth_bps", 1_000_000), connect_handler=connect,
                             domain_policy=domain_policy, transport_registry=transports, **limits)
    api = TunnelAPI(backend, Path(config["socket_path"]), config["proxy_uid"], config["origin"],
                    tuple(config.get("operators", ())), qualified=config.get("qualified", False),
                    principal_policy=config.get("principal_policy", "authenticated" if domain_policy else "allowlist"))
    stopped = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stopped.set)
    await api.start()
    try:
        await stopped.wait()
    finally:
        await api.close()
        await backend.close()


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path, help="Private provisioning policy JSON")
    args = parser.parse_args()
    from .loaders import _load_mapping

    if args.config.suffix.lower() != ".json":
        raise ValueError("Provisioning runtime policy must be a JSON file")
    config = _load_mapping(args.config)
    asyncio.run(serve(config, config_path=args.config))


if __name__ == "__main__":
    main()
