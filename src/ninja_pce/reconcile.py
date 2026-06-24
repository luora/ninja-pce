from __future__ import annotations

import fcntl
import hashlib
import json
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

from .cspf import compute_cspf
from .inventory import device_for_node, validate_inventory
from .loaders import load_demand, load_inventory, load_topology
from .models import CspfResult, Inventory, ServiceDemand, Topology
from .render import write_outputs
from .southbound import PushPlan, PushResult, build_push_plan, config_diff, push_plan


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
    links = [
        {
            "id": link.id,
            "source": link.source,
            "target": link.target,
            "from_ip": link.from_ip,
            "to_ip": link.to_ip,
            "metric": link.metric,
            "reservable_bw_mbps": link.reservable_bw_mbps,
            "reserved_bw_mbps": link.reserved_bw_mbps,
            "admin_groups": sorted(link.admin_groups),
            "bidirectional": link.bidirectional,
            "srlg": sorted(link.srlg),
            "area": link.area,
            "protection": link.protection,
        }
        for link in topology.links
    ]
    return _digest(
        {
            "nodes": sorted(nodes, key=lambda item: item["id"]),
            "links": sorted(links, key=lambda item: item["id"]),
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
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"Reconcile state must contain an object: {path}")
    return data


def _write_json_atomic(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def _append_audit(path: Path, event: dict[str, Any]) -> None:
    audit_path = path.with_suffix(".audit.jsonl")
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    with audit_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, sort_keys=True) + "\n")


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
    connect_handler: Callable[..., Any] | None = None,
    environ: dict[str, str] | None = None,
) -> ReconcileOutcome:
    topology = load_topology(topology_path)
    demand = load_demand(demand_path)
    inventory = load_inventory(inventory_path)
    validate_inventory(inventory, topology, require_all_nodes=True)

    result = compute_cspf(topology, demand)
    device = device_for_node(inventory, demand.source)
    plan = build_push_plan(result, topology, device)

    topology_hash = topology_fingerprint(topology)
    demand_hash = demand_fingerprint(demand)
    inventory_hash = inventory_fingerprint(inventory)
    desired_hash = _digest(plan.command_text)
    state_file = Path(state_path)

    with _state_lock(state_file):
        state = _load_state(state_file)
        topology_changed = state.get("topology_hash") != topology_hash
        inputs_changed = (
            topology_changed
            or state.get("demand_hash") != demand_hash
            or state.get("inventory_hash") != inventory_hash
        )
        desired_changed = state.get("last_planned_config_hash") != desired_hash
        pending_apply = state.get("last_applied_config_hash") != desired_hash
        previous_config = state.get("last_applied_config")
        diff = config_diff(previous_config, plan.command_text)

        if not force and not inputs_changed and not (execute and pending_apply):
            return ReconcileOutcome(
                action="unchanged",
                topology_changed=False,
                desired_config_changed=False,
                result=result,
                plan=plan,
                diff="",
                state_path=state_file,
            )

        written = tuple(write_outputs(result, topology, output_dir))
        timestamp = datetime.now(timezone.utc).isoformat()
        action = "preview"
        push_result: PushResult | None = None

        event: dict[str, Any] = {
            "timestamp": timestamp,
            "topology_hash": topology_hash,
            "demand_hash": demand_hash,
            "inventory_hash": inventory_hash,
            "desired_config_hash": desired_hash,
            "topology_changed": topology_changed,
            "desired_config_changed": desired_changed,
            "device": device.name,
            "topology_node_id": device.topology_node_id,
            "path_nodes": list(result.path_nodes),
            "approved": approved,
            "execute": execute,
        }

        if not desired_changed and not pending_apply:
            action = "no_config_change"
        elif execute:
            if not approved:
                raise ValueError("Refusing reconciliation push without --approve")
            try:
                with _device_lock(state_file.parent, device.name):
                    push_result = push_plan(
                        plan,
                        environ=environ,
                        connect_handler=connect_handler,
                    )
            except Exception as exc:
                event["action"] = "failed"
                event["error"] = str(exc)
                _append_audit(state_file, event)
                raise
            action = "applied"
            state["last_applied_config_hash"] = desired_hash
            state["last_applied_config"] = plan.command_text
            state["last_applied_at"] = timestamp
            event["push_result"] = asdict(push_result)

        state.update(
            {
                "version": 1,
                "updated_at": timestamp,
                "status": action,
                "topology_hash": topology_hash,
                "demand_hash": demand_hash,
                "inventory_hash": inventory_hash,
                "last_planned_config_hash": desired_hash,
                "last_planned_config": plan.command_text,
                "device": device.name,
                "topology_node_id": device.topology_node_id,
                "device_type": device.device_type,
                "path_nodes": list(result.path_nodes),
                "output_format": plan.output_format,
                "approval": {"approved": approved, "execute": execute},
                "verification_commands": {
                    "pre": list(plan.precheck_commands),
                    "post": list(plan.postcheck_commands),
                },
            }
        )
        event["action"] = action
        _write_json_atomic(state_file, state)
        _append_audit(state_file, event)

        return ReconcileOutcome(
            action=action,
            topology_changed=topology_changed,
            desired_config_changed=desired_changed,
            result=result,
            plan=plan,
            diff=diff,
            state_path=state_file,
            written_outputs=written,
            push_result=push_result,
        )
