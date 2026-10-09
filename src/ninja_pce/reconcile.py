from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

from .cspf import compute_cspf
from .inventory import device_for_node, render_format_for_device, validate_inventory
from .loaders import load_demand, load_inventory, load_topology
from .models import CspfResult, Inventory, ServiceDemand, Topology
from .render import write_outputs
from .reservations import ReservationLedger, authoritative_snapshot, lsp_identity, resource_lock
from .southbound import PushPlan, PushResult, build_push_plan, config_diff, push_plan, verify_plan
from .state import digest, load_state, write_state


@dataclass(frozen=True)
class ReconcileOutcome:
    action: str
    topology_changed: bool
    desired_config_changed: bool
    result: CspfResult
    plan: PushPlan
    diff: str
    state_path: Path
    written_outputs: tuple[Path, ...] = ()
    push_result: PushResult | None = None
    plan_id: str = ""


def _digest(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def topology_fingerprint(topology: Topology) -> str:
    nodes = [
        {
            "id": node.id,
            "loopback": node.loopback,
            "role": node.role,
            "area": node.area,
            "tags": sorted(node.tags),
        }
        for node in topology.nodes.values()
    ]
    links = [asdict(link) for link in topology.directional_links]
    return _digest(
        {
            "nodes": sorted(nodes, key=lambda item: item["id"]),
            "links": sorted(links, key=lambda item: item["id"]),
            "metadata": topology.raw_metadata,
        }
    )


def demand_fingerprint(demand: ServiceDemand) -> str:
    return _digest(asdict(demand))


def inventory_fingerprint(inventory: Inventory) -> str:
    devices = [
        {
            "name": device.name,
            "topology_node_id": device.topology_node_id,
            "host": device.host,
            "port": device.port,
            "device_type": device.device_type,
            "username": device.username,
            "password_env": device.password_env,
            "role": device.role,
            "tags": sorted(device.tags),
        }
        for device in inventory.devices.values()
    ]
    return _digest(sorted(devices, key=lambda item: item["name"]))


def file_fingerprint(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _load_state(path: Path) -> dict[str, Any]:
    return load_state(path, legacy=True)


def _write_json_atomic(path: Path, data: dict[str, Any]) -> None:
    write_state(path, data)


def _append_audit(path: Path, event: dict[str, Any]) -> None:
    audit_path = path.with_suffix(".audit.jsonl")
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    with audit_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


@contextmanager
def _state_lock(state_path: Path) -> Iterator[None]:
    lock_path = state_path.with_suffix(state_path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def _device_lock(state_dir: Path, device_name: str) -> Iterator[None]:
    safe_name = "".join(
        character if character.isalnum() or character in {"-", "_"} else "_"
        for character in device_name
    )
    lock_path = state_dir / f".ninja-pce-device-{safe_name}.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def reconcile_once(
    *,
    topology_path: str | Path,
    demand_path: str | Path,
    inventory_path: str | Path,
    output_dir: str | Path,
    state_path: str | Path,
    execute: bool = False,
    approved: bool = False,
    force: bool = False,
    approved_plan_id: str | None = None,
    approval_expires_at: str | None = None,
    require_authoritative: bool = False,
    max_topology_age_seconds: float = 60.0,
    connect_handler: Callable[..., Any] | None = None,
    environ: dict[str, str] | None = None,
    verifier: Any = None,
) -> ReconcileOutcome:
    for name, value in (("execute", execute), ("approved", approved), ("force", force),
                        ("require_authoritative", require_authoritative)):
        if not isinstance(value, bool):
            raise ValueError(f"{name} must be a boolean")
    if (isinstance(max_topology_age_seconds, bool) or not isinstance(max_topology_age_seconds, (int, float))
            or not math.isfinite(max_topology_age_seconds) or max_topology_age_seconds <= 0):
        raise ValueError("max_topology_age_seconds must be finite and positive")
    if execute and approval_expires_at is not None:
        try:
            expiry = datetime.fromisoformat(approval_expires_at.replace("Z", "+00:00"))
            if expiry.tzinfo is None or expiry <= datetime.now(timezone.utc):
                raise ValueError("expired or timezone missing")
        except (AttributeError, TypeError, ValueError) as exc:
            raise ValueError("Approval is expired or invalid") from exc
    if execute and not approved:
        raise ValueError("Refusing reconciliation push without --approve")
    if approved and not execute:
        raise ValueError("--approve requires --execute")
    env = os.environ if environ is None else environ
    if execute and env.get("NINJA_PCE_PREVIEW_ONLY", "").strip().lower() in {"1", "true", "yes", "on"}:
        raise ValueError("NINJA_PCE_PREVIEW_ONLY prohibits execution")
    state_file = Path(state_path).resolve()
    owner = str(state_file)
    paths = (Path(topology_path), Path(demand_path), Path(inventory_path))

    # Admission and delivery share one lock across processes on this filesystem.
    with resource_lock(state_file.parent), _state_lock(state_file):
        input_hashes = tuple(file_fingerprint(path) for path in paths)
        topology = load_topology(topology_path)
        demand = load_demand(demand_path)
        inventory = load_inventory(inventory_path)
        if input_hashes != tuple(file_fingerprint(path) for path in paths):
            raise ValueError("Inputs changed during loading; retry with a stable snapshot")
        validate_inventory(inventory, topology, require_all_nodes=True)
        if execute and require_authoritative and not authoritative_snapshot(topology, max_topology_age_seconds):
            raise ValueError("Execution requires a fresh, complete authoritative TED snapshot")
        state = _load_state(state_file)
        if execute and state.get("migrated_from") == 1 and state.get("last_applied_config_hash"):
            raise ValueError("Legacy applied state has no reservation ownership; manual recovery required")
        ledger = ReservationLedger(state_file.parent)
        device = device_for_node(inventory, demand.source)
        output_format = render_format_for_device(device)
        identity = lsp_identity(demand, output_format)
        ledger.assert_owner(identity, owner)
        effective = ledger.effective_topology(topology, owner)
        signaling_demand = demand
        if output_format in {"iosxr", "iosxe"}:
            signaling_bps = ((demand.bandwidth_bps + 999) // 1000) * 1000
            signaling_demand = replace(demand, reservation_bw_bps=signaling_bps)
        result = compute_cspf(effective, signaling_demand)
        plan = build_push_plan(result, effective, device)
        assert plan.expectation is not None
        if plan.expectation.bandwidth_bps > demand.bandwidth_bps:
            # Router signaling units round upward; admission must cover that amount too.
            result = compute_cspf(effective, replace(demand, reservation_bw_bps=plan.expectation.bandwidth_bps))
            plan = build_push_plan(result, effective, device)
            assert plan.expectation is not None
        assert plan.expectation is not None
        topology_hash = topology_fingerprint(topology)
        demand_hash = demand_fingerprint(demand)
        inventory_hash = inventory_fingerprint(inventory)
        desired_hash = digest(plan.command_text)
        topology_changed = state.get("topology_hash") != topology_hash
        inputs_changed = (
            topology_changed
            or state.get("demand_hash") != demand_hash
            or state.get("inventory_hash") != inventory_hash
            or state.get("last_planned_plan_id") != plan.plan_id
        )
        desired_changed = state.get("last_planned_config_hash") != desired_hash
        pending_apply = state.get("last_applied_config_hash") != desired_hash
        diff = config_diff(state.get("last_applied_config"), plan.command_text)
        timestamp = datetime.now(timezone.utc).isoformat()
        written: tuple[Path, ...] = ()
        push_result: PushResult | None = None
        action = "preview"
        if not force and not inputs_changed and not execute:
            return ReconcileOutcome(
                "unchanged", False, False, result, plan, "", state_file, plan_id=plan.plan_id
            )
        if execute:
            approval = approved_plan_id or state.get("last_planned_plan_id")
            if approval != plan.plan_id:
                raise ValueError("Approved plan is missing or stale; review the current preview plan ID")
            if state.get("pending") and state["pending"].get("plan_id") != plan.plan_id:
                raise ValueError("A prior delivery is unresolved; manual recovery is required before changing its plan")
            if input_hashes != tuple(file_fingerprint(path) for path in paths):
                raise ValueError("Inputs changed after approval; refusing execution")

        written = tuple(write_outputs(result, effective, output_dir))
        event: dict[str, Any] = {
            "timestamp": timestamp, "topology_hash": topology_hash,
            "demand_hash": demand_hash, "inventory_hash": inventory_hash,
            "desired_config_hash": desired_hash, "plan_id": plan.plan_id,
            "device": device.name, "lsp_id": identity,
            "path_nodes": list(result.path_nodes),
            "path_edges": [edge.id for edge in result.path_links],
            "approved": approved, "execute": execute,
        }
        state.update({
            "version": 2, "updated_at": timestamp, "status": state.get("status", "PLANNED"),
            "topology_hash": topology_hash, "demand_hash": demand_hash,
            "inventory_hash": inventory_hash, "reservation_id": identity,
            "last_planned_plan_id": plan.plan_id,
            "last_planned_config_hash": desired_hash,
            "last_planned_config": plan.command_text,
            "desired": {"plan_id": plan.plan_id, "edges": event["path_edges"],
                        "bandwidth_bps": plan.expectation.bandwidth_bps},
            "device": device.name, "topology_node_id": device.topology_node_id,
            "device_type": device.device_type, "path_nodes": list(result.path_nodes),
            "path_edges": event["path_edges"], "output_format": plan.output_format,
            "approval": {"approved": approved, "execute": execute},
            "verification_commands": {"pre": list(plan.precheck_commands), "post": list(plan.postcheck_commands)},
        })
        # Ledger first: a crash between durable writes leaks capacity safely.
        if inputs_changed or identity not in ledger.entries:
            ledger.reserve(identity, owner, result, plan.plan_id, plan.expectation.bandwidth_bps)
        _write_json_atomic(state_file, state)

        if execute:
            try:
                with _device_lock(state_file.parent, device.name):
                    def execution_guard() -> None:
                        if input_hashes != tuple(file_fingerprint(path) for path in paths):
                            raise ValueError("Inputs changed before application; refusing execution")
                        if require_authoritative and not authoritative_snapshot(topology, max_topology_age_seconds):
                            raise ValueError("TED became stale before execution")

                    execution_guard()
                    if not pending_apply or state.get("pending"):
                        push_result = verify_plan(
                            plan, environ=environ, connect_handler=connect_handler, verifier=verifier
                        )
                        action = "revalidated" if push_result.verified else "verification_failed"
                        if push_result.verified:
                            state["last_applied_config_hash"] = desired_hash
                            state["last_applied_config"] = plan.command_text
                    else:
                        state["status"] = "PUSHING"
                        state["pending"] = {"plan_id": plan.plan_id, "started_at": timestamp}
                        ledger.transition(identity, "PUSHING")
                        _write_json_atomic(state_file, state)
                        push_result = push_plan(
                            plan, execute=True, approved=True, approved_plan_id=plan.plan_id,
                            approval_expires_at=approval_expires_at,
                            pre_write_guard=execution_guard,
                            environ=environ, connect_handler=connect_handler, verifier=verifier,
                        )
                        action = "applied" if push_result.verified else "verification_failed"
                        state["last_applied_config_hash"] = desired_hash
                        state["last_applied_config"] = plan.command_text
                        state["last_applied_at"] = timestamp
                    state["status"] = push_result.status
                    state["observed"] = {"status": push_result.status, "observed_at": datetime.now(timezone.utc).isoformat()}
                    if push_result.verified:
                        state["last_verified"] = {"plan_id": plan.plan_id, "verified_at": timestamp}
                        state.pop("pending", None)
                    ledger.transition(identity, push_result.status)
                    event["verification_status"] = push_result.status
                    event["verification_errors"] = list(push_result.verification_errors)
            except Exception:
                # Never release capacity after SSH loss, timeout, failed commit, or crash.
                state["status"] = "ROLLBACK_REQUIRED"
                ledger.transition(identity, "ROLLBACK_REQUIRED")
                event["action"] = "failed"
                event["error"] = "Delivery or verification failed; inspect retained pending reservation"
                _write_json_atomic(state_file, state)
                _append_audit(state_file, event)
                raise
        elif not desired_changed and not pending_apply:
            action = "no_config_change"
        event["action"] = action
        _write_json_atomic(state_file, state)
        _append_audit(state_file, event)
        return ReconcileOutcome(
            action, topology_changed, desired_changed, result, plan, diff, state_file,
            written_outputs=written, push_result=push_result, plan_id=plan.plan_id,
        )
