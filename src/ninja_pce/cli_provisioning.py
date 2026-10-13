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
import time
import uuid
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from .cspf import compute_cspf
from .models import DemandOutput, ManagedDevice, PathConstraints, ServiceDemand
from .pcep_compute import PcepPathComputer
from .reservations import ReservationLedger, resource_lock
from .southbound import PushPlan, SouthboundError, build_pce_push_plan, push_plan, verify_plan
from .state import digest, load_state, write_state

if TYPE_CHECKING:
    from .tunnel_api import ProvisioningManager


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _expiry(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("An aware plan expiry is required")
    return parsed


class CliProvisioner:
    requires_plan = True
    backend_name = "router-config"
    independent_verification = True

    def __init__(self, computer: PcepPathComputer, state_dir: Path, devices: dict[str, ManagedDevice],
                 destinations: tuple[str, ...], pce_address: str, pcep_capture: Path, *,
                 qualified: bool = False, max_bandwidth_bps: int = 1_000_000,
                 connect_handler: Callable[..., Any] | None = None):
        if not devices or len(devices) > 16 or any(device.topology_node_id != name for name, device in devices.items()):
            raise ValueError("Provisioning requires distinct configured headend devices")
        if type(qualified) is not bool or type(max_bandwidth_bps) is not int or not 0 < max_bandwidth_bps <= 10**9:
            raise ValueError("Invalid qualification or bandwidth policy")
        if not destinations or len(set(destinations)) != len(destinations):
            raise ValueError("Provisioning requires unique authorized destinations")
        from .pcep_provisioning import JournalProvisioningAuthorization

        self.computer, self.state_dir = computer, Path(state_dir)
        self.path = self.state_dir / "cli-provisioning.json"
        self.devices, self.allowed_destinations = dict(devices), destinations
        self.headends = {name: device.host for name, device in devices.items()}
        if any(computer.headends.get(peer) is None for peer in self.headends.values()):
            raise ValueError("Configured SSH headends must also be authorized PCC loopbacks")
        self.pce_address, self.pcep_capture = pce_address, Path(pcep_capture)
        self.qualified, self.max_bandwidth_bps = qualified, max_bandwidth_bps
        self.max_outstanding = 1
        self.manager: ProvisioningManager = self
        self.handler = connect_handler
        self.authorization = JournalProvisioningAuthorization(self.path, computer.headends)
        self.policy_digest = digest({"devices": {k: asdict(v) for k, v in devices.items()},
                                     "destinations": destinations, "max_bandwidth_bps": max_bandwidth_bps,
                                     "pce_address": pce_address, "max_outstanding": self.max_outstanding,
                                     "qualified_versions": ["16.09.07", "16.9.7"]})
        self._lock = asyncio.Lock()
        self._workers: set[asyncio.Task[None]] = set()
        self._plans: dict[str, PushPlan] = {}
        with resource_lock(self.state_dir):
            journal, ledger = self._load()
            ledger.save()
            for op in journal["operations"].values():
                if op["status"] in {"PLANNED", "PUSHING"}:
                    op.update(status="ROLLBACK_REQUIRED", reason="Restart after an uncertain configuration attempt")
                    ledger.transition(op["reservation_id"], "ROLLBACK_REQUIRED")
            write_state(self.path, journal)

    def _load(self) -> tuple[dict[str, Any], ReservationLedger]:
        if self.path.exists() and self.path.stat().st_size > 1_048_576:
            raise ValueError("Provisioning journal exceeds the bounded history size")
        state = load_state(self.path)
        if not state:
            state = {"kind": "cli_tunnel_provisioning", "operations": {}, "previews": {},
                     "policy_digest": self.policy_digest, "reservation_id": "cli-provisioning-journal"}
        if (state.get("kind") != "cli_tunnel_provisioning" or state.get("policy_digest") != self.policy_digest or
                not isinstance(state.get("operations"), dict) or not isinstance(state.get("previews"), dict) or
                len(state["operations"]) > 16 or len(state["previews"]) > 16):
            raise ValueError("Provisioning journal policy or schema differs; reconcile it explicitly")
        ledger = ReservationLedger(self.state_dir)
        for identifier, op in state["operations"].items():
            if (not isinstance(op, dict) or op.get("id") != identifier or
                    op.get("authorization_digest") != digest(op.get("authorization")) or
                    op.get("status") not in {"PLANNED", "PUSHING", "APPLIED_UNVERIFIED", "VERIFIED", "VERIFICATION_FAILED",
                                             "ROLLBACK_REQUIRED", "REJECTED", "ROLLED_BACK"}):
                raise ValueError("Invalid immutable provisioning operation")
            authorization = op["authorization"]
            if (not isinstance(authorization, dict) or op.get("owner") != "cli-provision:" + identifier or
                    authorization.get("peer") != self.headends.get(str(op.get("headend"))) or
                    any(authorization.get(field) != op.get(field)
                        for field in ("plan_id", "tunnel_id", "bandwidth_bps", "ero", "path_edges"))):
                raise ValueError("PCEP authority differs from the exact held provisioning operation")
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

    def readiness(self, headend: str | None = None) -> tuple[bool, str]:
        try:
            if not self.qualified:
                raise ValueError("Configuration provisioning has not been qualified for this device profile.")
            if os.environ.get("NINJA_PCE_PREVIEW_ONLY", "").lower() in {"1", "true", "yes", "on"}:
                raise ValueError("Controller writes are disabled.")
            self.computer.validated_snapshot()
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
            for name in ([headend] if headend is not None else self.headends):
                peer = peers.get(self.headends.get(name), {})
                if (not isinstance(peer, dict) or peer.get("state") != "ESTABLISHED" or peer.get("fresh") is not True or
                        peer.get("synchronized") is not True or type(peer.get("valid_until")) not in (int, float) or
                        peer["valid_until"] <= time.time()):
                    raise ValueError("The selected PCC is not fresh and synchronized.")
            return True, ""
        except (ValueError, OSError, KeyError, TypeError) as exc:
            return False, str(exc)[:256]

    def _intent(self, intent: dict[str, Any]) -> None:
        if (not isinstance(intent, dict) or set(intent) != {"headend", "destination", "bandwidth_bps"} or
                intent["headend"] not in self.headends or intent["destination"] not in self.allowed_destinations or
                intent["headend"] == intent["destination"] or type(intent["bandwidth_bps"]) is not int or
                not 0 < intent["bandwidth_bps"] <= self.max_bandwidth_bps):
            raise ValueError("Intent exceeds the authorized provisioning policy")

    def _compute(self, intent: dict[str, Any], op_id: str, tunnel_id: int,
                 ledger: ReservationLedger) -> tuple[Any, PushPlan, str]:
        topology, revision = self.computer.validated_snapshot()
        conservative = replace(topology, raw_metadata={**topology.raw_metadata, "authoritative": False})
        effective = ledger.effective_topology(conservative, "cli-provision:" + op_id)
        bps = ((intent["bandwidth_bps"] + 999) // 1000) * 1000
        if bps > self.max_bandwidth_bps:
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
                if len(state["operations"]) >= 16 or any(
                        op["status"] not in {"REJECTED", "ROLLED_BACK"} for op in state["operations"].values()):
                    raise ValueError("Another provisioned tunnel is held; this profile permits one managed tunnel")
                # Previews allocate no bandwidth and never contact a device.
                used = {op["tunnel_id"] for op in state["operations"].values()}
                used.update(p["tunnel_id"] for p in state["previews"].values()
                            if _expiry(p["expires_at"]) > _now() and p["principal"] != principal)
                tunnel_id = next((number for number in range(1000, 1016) if number not in used), None)
                if tunnel_id is None:
                    raise ValueError("Managed tunnel ID allocation is exhausted")
                op_id = uuid.uuid4().hex
                result, plan, revision = self._compute(intent, op_id, tunnel_id, ledger)
                state["previews"] = {k: v for k, v in state["previews"].items()
                                     if _expiry(v["expires_at"]) > _now() and v["principal"] != principal}
                if len(state["previews"]) >= 16:
                    raise ValueError("Too many active previews")
                preview = {"plan_id": plan.plan_id, "principal": principal, "op_id": op_id,
                           "intent": intent, "tunnel_id": tunnel_id, "topology_digest": revision,
                           "expires_at": (_now() + timedelta(seconds=60)).isoformat(),
                           "rendered_config": plan.rendered_config, "path_nodes": list(result.path_nodes),
                           "path_edges": [edge.id for edge in result.path_links],
                           "ero": [hop.address for hop in result.explicit_hops],
                           "bandwidth_bps": result.demand.bandwidth_bps,
                           "headend": intent["headend"], "destination": intent["destination"]}
                state["previews"][plan.plan_id] = preview
                write_state(self.path, state)
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
            raise ValueError("The exact reviewed plan expired or changed; request a new preview")
        result, current, _ = self._compute(intent, preview["op_id"], preview["tunnel_id"], ledger)
        if not self._same_plan(plan, current) or preview["path_edges"] != [e.id for e in result.path_links]:
            raise ValueError("Fresh topology or resources changed the approved plan")
        return preview, result, plan

    async def create(self, principal: str, key: str, intent: dict[str, Any], *,
                     approved_plan_id: str) -> dict[str, Any]:
        self._principal(principal)
        self._intent(intent)
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
                ready, reason = self.readiness(intent["headend"])
                if not ready:
                    raise ValueError(reason)
                if len(state["operations"]) >= 16 or any(op["status"] not in {"REJECTED", "ROLLED_BACK"}
                                                          for op in state["operations"].values()):
                    raise ValueError("Another managed tunnel requires observation or recovery")
                preview, result, plan = self._check_preview(state, ledger, principal, intent, approved_plan_id)
                op_id, tunnel_id = preview["op_id"], preview["tunnel_id"]
                owner = "cli-provision:" + op_id
                identity = f"{intent['headend']}:iosxe:Tunnel{tunnel_id}"
                topology, _ = self.computer.validated_snapshot()
                source = topology.nodes[intent["headend"]].loopback
                if self.computer.headends[self.headends[intent["headend"]]] != source:
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
                ledger.reserve(identity, owner, result, plan.plan_id)
                state["operations"][op_id] = operation
                write_state(self.path, state)
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
            write_state(self.path, state)

    def _pce_control(self, outputs: tuple[tuple[str, str], ...]) -> bool:
        text = "\n".join(value for _, value in outputs)
        # Require the observed active option, remote peer, and delegation. The
        # generic verifier independently establishes this exact tunnel's RSVP.
        return bool(re.search(r"(?im)^\s*path option 1, \(PCE address " + re.escape(self.pce_address) +
                              r"\) type dynamic \(Basis for Setup, path weight \d+\)\s*$", text) and
                    re.search(r"(?im)^\s*Delegation state:\s*Working:\s*yes\s+Protect:\s*no\s*$", text) and
                    re.search(r"(?im)^\s*Delegation peer:\s*" + re.escape(self.pce_address) + r"\s*$", text) and
                    re.search(r"(?im)^\s*Created via PCUpd message from PCE server:\s*" +
                              re.escape(self.pce_address) + r"\s*$", text))

    def _completed_path(self, op_id: str) -> bool:
        from .pcep_provisioning import configured_initial_path_observed

        return configured_initial_path_observed(load_state(self.pcep_capture), self.get(op_id),
                                                policy_digest=self.authorization.policy_digest)

    def _execute(self, op_id: str, plan: PushPlan) -> None:
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
                write_state(self.path, state)
        except SouthboundError:
            # push_plan raises only before its first configuration operation;
            # post-write failures return a retained ROLLBACK_REQUIRED result.
            with resource_lock(self.state_dir):
                state, ledger = self._load()
                op = state["operations"][op_id]
                if push_returned:
                    op.update(status="ROLLBACK_REQUIRED", reason="Post-write observation failed; reservation retained")
                    ledger.transition(op["reservation_id"], "ROLLBACK_REQUIRED")
                else:
                    op.update(status="REJECTED", reason="Delivery rejected before configuration was sent")
                    # Persist rejection first: interruption can retain an extra
                    # hold, but must never forget an unresolved delivery.
                    write_state(self.path, state)
                    ledger.assert_owner(op["reservation_id"], op["owner"])
                    del ledger.data["entries"][op["reservation_id"]]
                    ledger.save()
                write_state(self.path, state)
        except Exception:
            with resource_lock(self.state_dir):
                state, ledger = self._load()
                op = state["operations"][op_id]
                op.update(status="ROLLBACK_REQUIRED", reason="Uncertain delivery; retained reservation requires recovery")
                ledger.transition(op["reservation_id"], "ROLLBACK_REQUIRED")
                write_state(self.path, state)

    async def close(self) -> None:
        if self._workers:
            await asyncio.gather(*tuple(self._workers), return_exceptions=True)


async def serve(config: dict[str, Any]) -> None:
    """Run only the configured Unix API; the authenticated reverse proxy owns TLS."""
    import signal

    from netmiko import ConnectHandler

    from .loaders import load_inventory
    from .tunnel_api import TunnelAPI

    computer_config = dict(config["computer"])
    for key in ("raw_path", "status_path", "topology_path"):
        computer_config[key] = Path(computer_config[key])
    computer_config["required_peers"] = tuple(computer_config["required_peers"])
    computer = PcepPathComputer(**computer_config)
    inventory = load_inventory(config["inventory"])
    devices = {name: inventory.device_for_node(name) for name in config["headends"]}
    known_hosts = Path(config["known_hosts"])
    if not known_hosts.is_file() or known_hosts.is_symlink():
        raise ValueError("An independently pinned SSH host-key file is required")

    def connect(**kwargs: Any) -> Any:
        return ConnectHandler(**kwargs, ssh_strict=True, system_host_keys=False,
                              alt_host_keys=True, alt_key_file=str(known_hosts))

    backend = CliProvisioner(computer, Path(config["state_dir"]), devices, tuple(config["destinations"]),
                             config["pce_address"], Path(config["pcep_capture"]), qualified=config.get("qualified", False),
                             max_bandwidth_bps=config.get("max_bandwidth_bps", 1_000_000), connect_handler=connect)
    api = TunnelAPI(backend, Path(config["socket_path"]), config["proxy_uid"], config["origin"],
                    tuple(config["operators"]), qualified=config.get("qualified", False))
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
    config = json.loads(args.config.read_text(encoding="utf-8"))
    asyncio.run(serve(config))


if __name__ == "__main__":
    main()
