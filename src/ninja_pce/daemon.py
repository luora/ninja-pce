from __future__ import annotations

import json
import math
import os
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock
from typing import Any, Callable, Mapping

from .cspf import CspfNoPath, CspfSearchLimit
from .loaders import InputError, load_topology
from .loaders import _load_mapping as load_input_mapping
from .reconcile import ReconcileOutcome, file_fingerprint, reconcile_once
from .reservations import authoritative_snapshot, resource_lock
from .state import digest, load_state, write_state

DEMAND_SUFFIXES = {".yml", ".yaml", ".json"}
POLICY_MODES = {"manual", "guarded-auto"}
PREVIEW_ONLY_ENV = "NINJA_PCE_PREVIEW_ONLY"

DEFAULT_ALLOWED_PREFIXES: dict[str, tuple[str, ...]] = {
    "iosxr": (
        "explicit-path name ",
        "index ",
        "interface ",
        "description ninja-pce ",
        "ipv4 unnumbered ",
        "destination ",
        "signalled-bandwidth ",
        "priority ",
        "path-option ",
        "tunnel mode mpls traffic-eng",
        "tunnel destination ",
        "tunnel mpls traffic-eng bandwidth ",
        "tunnel mpls traffic-eng priority ",
        "tunnel mpls traffic-eng path-option ",
        "exit",
    ),
    "iosxe": (
        "ip explicit-path name ",
        "next-address ",
        "interface Tunnel",
        "description ninja-pce ",
        "ip unnumbered ",
        "tunnel mode mpls traffic-eng",
        "tunnel destination ",
        "tunnel mpls traffic-eng bandwidth ",
        "tunnel mpls traffic-eng priority ",
        "tunnel mpls traffic-eng path-option ",
        "exit",
    ),
    "junos": (
        "set protocols mpls path ",
        "set protocols mpls label-switched-path ",
    ),
}


@dataclass(frozen=True)
class TopologyFeedConfig:
    type: str
    path: Path
    max_age_seconds: float | None = None


@dataclass(frozen=True)
class DemandsConfig:
    type: str
    path: Path


@dataclass(frozen=True)
class ApprovalPolicyConfig:
    mode: str = "manual"
    max_commands: int = 50
    min_residual_bw_mbps: float = 0.0
    allow_warnings: bool = False
    allowed_output_formats: tuple[str, ...] = ("iosxr", "iosxe", "junos")
    allowed_demand_names: tuple[str, ...] = ()
    allowed_device_tags: tuple[str, ...] = ()
    allowed_command_prefixes: dict[str, tuple[str, ...]] = field(
        default_factory=lambda: dict(DEFAULT_ALLOWED_PREFIXES)
    )


@dataclass(frozen=True)
class HealthConfig:
    host: str = "127.0.0.1"
    port: int = 8080
    enabled: bool = True


@dataclass(frozen=True)
class DaemonConfig:
    topology_feed: TopologyFeedConfig
    demands: DemandsConfig
    inventory_path: Path
    output_dir: Path
    state_dir: Path
    approval_policy: ApprovalPolicyConfig = field(default_factory=ApprovalPolicyConfig)
    health: HealthConfig = field(default_factory=HealthConfig)
    poll_interval: float = 5.0
    retry_attempts: int = 3
    retry_backoff_seconds: float = 5.0
    retry_max_backoff_seconds: float = 60.0
    debounce_seconds: float = 1.0
    revalidation_interval_seconds: float = 30.0


@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class DemandRunResult:
    demand_path: Path
    demand_name: str | None
    action: str
    auto_approved: bool = False
    policy_reasons: tuple[str, ...] = ()
    error: str | None = None
    path_nodes: tuple[str, ...] = ()
    device: str | None = None
    state_path: Path | None = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["demand_path"] = str(self.demand_path)
        data["state_path"] = str(self.state_path) if self.state_path else None
        return data


@dataclass(frozen=True)
class DaemonCycleResult:
    status: str
    started_at: str
    finished_at: str
    topology_path: Path
    topology_fresh: bool
    topology_age_seconds: float | None
    demand_results: tuple[DemandRunResult, ...]
    message: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "topology_path": str(self.topology_path),
            "topology_fresh": self.topology_fresh,
            "topology_age_seconds": self.topology_age_seconds,
            "message": self.message,
            "demand_results": [result.to_dict() for result in self.demand_results],
        }


class HealthState:
    def __init__(self) -> None:
        self._lock = Lock()
        self._last_cycle: dict[str, Any] | None = None
        self._metrics: dict[str, int] = {
            "cycles_total": 0,
            "cycles_ok": 0,
            "cycles_degraded": 0,
            "cycles_stale_topology": 0,
            "demands_total": 0,
            "demands_preview": 0,
            "demands_applied": 0,
            "demands_unchanged": 0,
            "demands_no_config_change": 0,
            "demands_auto_denied": 0,
            "demands_failed": 0,
            "demands_revalidated": 0,
            "demands_verification_failed": 0,
            "demands_recovery_required": 0,
            "demands_backoff": 0,
            "demands_debounced": 0,
        }

    def record(self, cycle: DaemonCycleResult) -> None:
        with self._lock:
            self._last_cycle = cycle.to_dict()
            self._metrics["cycles_total"] += 1
            if cycle.status == "ok":
                self._metrics["cycles_ok"] += 1
            elif cycle.status == "stale_topology":
                self._metrics["cycles_stale_topology"] += 1
            else:
                self._metrics["cycles_degraded"] += 1

            for result in cycle.demand_results:
                self._metrics["demands_total"] += 1
                if result.action in {"verification_failed", "recovery_required"}:
                    self._metrics[f"demands_{result.action}"] += 1
                    self._metrics["demands_failed"] += 1
                elif result.error:
                    self._metrics["demands_failed"] += 1
                elif result.action == "applied":
                    self._metrics["demands_applied"] += 1
                elif result.action == "preview":
                    self._metrics["demands_preview"] += 1
                elif result.action == "unchanged":
                    self._metrics["demands_unchanged"] += 1
                elif result.action == "no_config_change":
                    self._metrics["demands_no_config_change"] += 1
                elif result.action in {"revalidated", "backoff", "debounced"}:
                    self._metrics[f"demands_{result.action}"] += 1
                if result.policy_reasons:
                    self._metrics["demands_auto_denied"] += 1

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "last_cycle": self._last_cycle,
                "metrics": dict(self._metrics),
            }


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        value = (value,)
    if not isinstance(value, (list, tuple)) or not all(isinstance(item, str) for item in value):
        raise InputError("Expected a string or list of strings")
    if any(not item or any(ord(character) < 32 for character in item) for item in value):
        raise InputError("String lists cannot contain empty entries or control characters")
    return tuple(value)


def _boolean(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise InputError(f"{name} must be a boolean")
    return value


def _number(value: Any, name: str, *, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value):
        raise InputError(f"{name} must be a finite number")
    if value < 0 or (positive and value == 0):
        raise InputError(f"{name} must be {'positive' if positive else 'nonnegative'}")
    return float(value)


def _integer(value: Any, name: str, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise InputError(f"{name} must be an integer in {low}..{high}")
    return value


def _path(value: Any, *, base: Path | None = None) -> Path:
    if not isinstance(value, str) or not value or any(ord(character) < 32 for character in value):
        raise InputError("Expected a filesystem path")
    path = Path(str(value)).expanduser()
    if not path.is_absolute() and base is not None:
        path = base / path
    return path


def _safe_stem(path: Path) -> str:
    stem = path.stem
    return "".join(character if character.isalnum() or character in {"-", "_"} else "_" for character in stem)


def _load_mapping(path: str | Path) -> dict[str, Any]:
    return load_input_mapping(path)


def load_daemon_config(path: str | Path) -> DaemonConfig:
    config_path = Path(path)
    base = config_path.resolve().parent
    data = _load_mapping(config_path)
    allowed = {"topology_feed", "demands", "approval_policy", "health", "inventory", "output_dir",
               "state_dir", "state", "poll_interval", "retry_attempts", "retry_backoff_seconds",
               "retry_max_backoff_seconds", "debounce_seconds", "revalidation_interval_seconds"}
    if set(data) - allowed:
        raise InputError(f"Unknown daemon configuration fields: {sorted(set(data) - allowed)}")
    allowed_sections = {
        "topology_feed": {"type", "path", "max_age_seconds"},
        "demands": {"type", "path"},
        "health": {"host", "port", "enabled"},
        "approval_policy": {"mode", "max_commands", "min_residual_bw_mbps", "allow_warnings",
                            "allowed_output_formats", "allowed_demand_names", "allowed_device_tags",
                            "allowed_command_prefixes"},
    }

    sections: dict[str, dict[str, Any]] = {}
    for name in ("topology_feed", "demands", "approval_policy", "health"):
        section = data.get(name, {})
        if not isinstance(section, dict):
            raise InputError(f"{name} must be a mapping/object")
        if set(section) - allowed_sections[name]:
            raise InputError(f"Unknown {name} fields: {sorted(set(section) - allowed_sections[name])}")
        sections[name] = section
    topology_data, demands_data = sections["topology_feed"], sections["demands"]
    policy_data, health_data = sections["approval_policy"], sections["health"]

    topology_type = str(topology_data.get("type", "file"))
    if topology_type != "file":
        raise InputError("Only topology_feed.type=file is supported")
    demands_type = str(demands_data.get("type", "directory"))
    if demands_type not in {"directory", "file"}:
        raise InputError("demands.type must be directory or file")

    mode = str(policy_data.get("mode", "manual"))
    if mode not in POLICY_MODES:
        raise InputError(f"approval_policy.mode must be one of {sorted(POLICY_MODES)}")

    custom_prefixes = policy_data.get("allowed_command_prefixes", {})
    if not isinstance(custom_prefixes, dict):
        raise InputError("approval_policy.allowed_command_prefixes must be a mapping/object")
    allowed_prefixes = dict(DEFAULT_ALLOWED_PREFIXES)
    for output_format, prefixes in custom_prefixes.items():
        if output_format not in DEFAULT_ALLOWED_PREFIXES:
            raise InputError(f"Unsupported command prefix format {output_format}")
        allowed_prefixes[str(output_format)] = _tuple(prefixes)

    output_dir = _path(data.get("output_dir", "build/daemon/outputs"), base=base)
    if "state_dir" in data:
        state_dir = _path(data["state_dir"], base=base)
    else:
        state_dir = _path(data.get("state", "build/daemon/state"), base=base)
    if "state_dir" not in data and state_dir.suffix:
        state_dir = state_dir.parent / state_dir.stem

    policy = ApprovalPolicyConfig(
        mode=mode,
        max_commands=_integer(policy_data.get("max_commands", 50), "max_commands", 1, 10000),
        min_residual_bw_mbps=_number(policy_data.get("min_residual_bw_mbps", 0.0), "min_residual_bw_mbps"),
        allow_warnings=_boolean(policy_data.get("allow_warnings", False), "allow_warnings"),
        allowed_output_formats=_tuple(policy_data.get("allowed_output_formats", ("iosxr", "iosxe", "junos"))),
        allowed_demand_names=_tuple(policy_data.get("allowed_demand_names", ())),
        allowed_device_tags=_tuple(policy_data.get("allowed_device_tags", ())),
        allowed_command_prefixes=allowed_prefixes,
    )
    if policy.max_commands < 1:
        raise InputError("approval_policy.max_commands must be at least 1")
    if policy.min_residual_bw_mbps < 0:
        raise InputError("approval_policy.min_residual_bw_mbps cannot be negative")
    if not policy.allowed_output_formats or set(policy.allowed_output_formats) - set(DEFAULT_ALLOWED_PREFIXES):
        raise InputError("allowed_output_formats must contain supported vendor formats")

    poll_interval = _number(data.get("poll_interval", 5.0), "poll_interval", positive=True)

    return DaemonConfig(
        topology_feed=TopologyFeedConfig(
            type=topology_type,
            path=_path(topology_data.get("path"), base=base),
            max_age_seconds=(
                _number(topology_data["max_age_seconds"], "max_age_seconds", positive=True)
                if topology_data.get("max_age_seconds") is not None
                else None
            ),
        ),
        demands=DemandsConfig(
            type=demands_type,
            path=_path(demands_data.get("path"), base=base),
        ),
        inventory_path=_path(data.get("inventory"), base=base),
        output_dir=output_dir,
        state_dir=state_dir,
        approval_policy=policy,
        health=HealthConfig(
            host=str(health_data.get("host", "127.0.0.1")),
            port=_integer(health_data.get("port", 8080), "health.port", 1, 65535),
            enabled=_boolean(health_data.get("enabled", True), "health.enabled"),
        ),
        poll_interval=poll_interval,
        retry_attempts=_integer(data.get("retry_attempts", 3), "retry_attempts", 1, 20),
        retry_backoff_seconds=_number(data.get("retry_backoff_seconds", 5.0), "retry_backoff_seconds", positive=True),
        retry_max_backoff_seconds=_number(data.get("retry_max_backoff_seconds", 60.0), "retry_max_backoff_seconds", positive=True),
        debounce_seconds=_number(data.get("debounce_seconds", 1.0), "debounce_seconds"),
        revalidation_interval_seconds=_number(data.get("revalidation_interval_seconds", 30.0), "revalidation_interval_seconds", positive=True),
    )


def discover_demand_files(config: DaemonConfig) -> tuple[Path, ...]:
    path = config.demands.path
    if config.demands.type == "file":
        if not path.exists():
            raise InputError(f"Demand file not found: {path}")
        if path.suffix.lower() not in DEMAND_SUFFIXES:
            raise InputError(f"Unsupported demand file type: {path}")
        return (path,)

    if not path.exists():
        raise InputError(f"Demand directory not found: {path}")
    if not path.is_dir():
        raise InputError(f"Demand path is not a directory: {path}")
    return tuple(
        sorted(
            item
            for item in path.iterdir()
            if item.is_file() and item.suffix.lower() in DEMAND_SUFFIXES
        )
    )


def topology_age_seconds(config: DaemonConfig) -> float | None:
    path = config.topology_feed.path
    if not path.exists():
        raise InputError(f"Topology feed file not found: {path}")
    topology = load_topology(path)
    timestamp = topology.raw_metadata.get("timestamp")
    if timestamp is not None:
        try:
            observed = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
            if observed.tzinfo is None:
                raise ValueError("Timezone missing")
            age = (datetime.now(timezone.utc) - observed).total_seconds()
            if age < 0:
                raise ValueError("Future topology timestamp")
            return age
        except ValueError as exc:
            raise InputError("Topology timestamp must be a valid past timezone-aware timestamp") from exc
    return max(time.time() - path.stat().st_mtime, 0.0)


def topology_is_fresh(config: DaemonConfig) -> tuple[bool, float | None]:
    age = topology_age_seconds(config)
    max_age = config.topology_feed.max_age_seconds
    if max_age is None and config.approval_policy.mode == "guarded-auto":
        max_age = 60.0
    if max_age is None:
        return True, age
    return age is not None and age <= max_age, age


def demand_state_path(config: DaemonConfig, demand_path: Path) -> Path:
    identity = digest(str(demand_path.resolve()))[:12]
    return config.state_dir / f".ninja-pce-{_safe_stem(demand_path)}-{identity}-state.json"


def demand_output_dir(config: DaemonConfig, demand_path: Path) -> Path:
    return config.output_dir / _safe_stem(demand_path)


def _truthy(value: str | None) -> bool:
    return bool(value and value.strip().lower() in {"1", "true", "yes", "on"})


def _topology_event_revision(topology: Any) -> str:
    """Coalesce reordered snapshots and timestamp-only updates, retain TE changes."""
    links = []
    for link in topology.directional_links:
        item = asdict(link)
        item.pop("provenance_timestamp", None)
        item.pop("timestamp", None)
        links.append(item)
    metadata = dict(topology.raw_metadata)
    metadata.pop("timestamp", None)
    if isinstance(metadata.get("reservations"), list):
        metadata["reservations"] = [
            {key: value for key, value in item.items() if key != "observed_at"}
            if isinstance(item, dict) else item for item in metadata["reservations"]
        ]
    return digest({"nodes": sorted((asdict(node) for node in topology.nodes.values()), key=lambda item: item["id"]),
                   "links": sorted(links, key=lambda item: item["id"]), "metadata": metadata})


def _command_allowed(command: str, output_format: str, policy: ApprovalPolicyConfig) -> bool:
    prefixes = policy.allowed_command_prefixes.get(output_format, ())
    if not any(command == prefix or command.startswith(prefix) for prefix in prefixes):
        return False
    token = r"[A-Za-z0-9_.:/-]+"
    address = r"[0-9]+(?:\.[0-9]+){3}"
    common = (
        rf"description ninja-pce {token} {token} to {token}",
        r"exit",
    )
    patterns: dict[str, tuple[str, ...]] = {
        "iosxr": common + (
            rf"explicit-path name {token}",
            rf"index [0-9]+ next-address (?:strict|loose) ipv4 unicast {address}",
            r"interface tunnel-te[0-9]+", r"ipv4 unnumbered Loopback[0-9]+",
            rf"destination {address}", r"signalled-bandwidth [0-9]+",
            r"priority [0-7] [0-7]", rf"path-option [0-9]+ explicit name {token}",
        ),
        "iosxe": common + (
            rf"ip explicit-path name {token} enable", rf"next-address (?:strict|loose) {address}",
            r"interface Tunnel[0-9]+", r"ip unnumbered Loopback[0-9]+",
            r"tunnel mode mpls traffic-eng", rf"tunnel destination {address}",
            r"tunnel mpls traffic-eng bandwidth [0-9]+", r"tunnel mpls traffic-eng priority [0-7] [0-7]",
            rf"tunnel mpls traffic-eng path-option [0-9]+ explicit name {token}",
        ),
        "junos": (
            rf"set protocols mpls path {token} {address} (?:strict|loose)",
            rf"set protocols mpls label-switched-path {token} (?:to {address}|bandwidth [0-9]+(?:[kmg])?|primary {token}|priority [0-7] [0-7])",
        ),
    }
    return any(re.fullmatch(pattern, command) for pattern in patterns.get(output_format, ()))


def evaluate_auto_policy(
    outcome: ReconcileOutcome,
    policy: ApprovalPolicyConfig,
    *,
    environ: Mapping[str, str] | None = None,
) -> PolicyDecision:
    env = os.environ if environ is None else environ
    reasons: list[str] = []
    plan = outcome.plan
    result = outcome.result

    if policy.mode != "guarded-auto":
        return PolicyDecision(
            allowed=False,
            reasons=("approval_policy.mode is manual",),
        )
    if _truthy(env.get(PREVIEW_ONLY_ENV)):
        reasons.append(f"{PREVIEW_ONLY_ENV} is set")
    if outcome.action not in {"preview", "no_config_change"}:
        reasons.append(f"reconcile action is {outcome.action}, not preview")
    if not outcome.diff and outcome.action != "no_config_change":
        reasons.append("no config diff is pending")
    if plan.output_format not in policy.allowed_output_formats:
        reasons.append(f"output format {plan.output_format} is not auto-approved")
    if len(plan.commands) > policy.max_commands:
        reasons.append(
            f"command count {len(plan.commands)} exceeds max_commands {policy.max_commands}"
        )
    if result.residual_after_reservation_mbps < policy.min_residual_bw_mbps:
        reasons.append(
            "residual bandwidth "
            f"{result.residual_after_reservation_mbps:g} Mbps is below "
            f"{policy.min_residual_bw_mbps:g} Mbps"
        )
    if result.warnings and not policy.allow_warnings:
        reasons.append("CSPF result has warnings")
    if policy.allowed_demand_names and result.demand.name not in policy.allowed_demand_names:
        reasons.append(f"demand {result.demand.name} is not in allowed_demand_names")
    if policy.allowed_device_tags:
        device_tags = set(plan.device.tags)
        required_tags = set(policy.allowed_device_tags)
        if not required_tags.issubset(device_tags):
            reasons.append(
                "device tags do not include required auto-approval tags: "
                f"{sorted(required_tags - device_tags)}"
            )
    if not plan.device.password_env:
        reasons.append(f"device {plan.device.name} has no password_env")
    elif not env.get(plan.device.password_env):
        reasons.append(f"required secret {plan.device.password_env} is not set")

    blocked_commands = [
        command
        for command in plan.commands
        if not _command_allowed(command, plan.output_format, policy)
    ]
    if blocked_commands:
        reasons.append(
            "commands outside auto-approval allowlist: "
            + ", ".join(blocked_commands[:3])
            + (" ..." if len(blocked_commands) > 3 else "")
        )

    return PolicyDecision(allowed=not reasons, reasons=tuple(reasons))


def run_daemon_cycle(
    config: DaemonConfig,
    *,
    connect_handler: Callable[..., Any] | None = None,
    environ: Mapping[str, str] | None = None,
) -> DaemonCycleResult:
    started = _utc_now()
    demand_results: list[DemandRunResult] = []
    try:
        fresh, age = topology_is_fresh(config)
        topology = load_topology(config.topology_feed.path)
    except Exception as exc:
        return DaemonCycleResult(
            "invalid_topology", started, _utc_now(), config.topology_feed.path,
            False, None, (), f"Topology unavailable or invalid: {exc}",
        )
    if not fresh:
        finished = _utc_now()
        return DaemonCycleResult(
            status="stale_topology",
            started_at=started,
            finished_at=finished,
            topology_path=config.topology_feed.path,
            topology_fresh=False,
            topology_age_seconds=age,
            demand_results=(),
            message=(
                f"Topology feed is stale: age {age:g}s exceeds "
                f"{config.topology_feed.max_age_seconds or 60.0:g}s"
            ),
        )

    try:
        demand_files = discover_demand_files(config)
    except Exception as exc:
        finished = _utc_now()
        return DaemonCycleResult(
            status="degraded",
            started_at=started,
            finished_at=finished,
            topology_path=config.topology_feed.path,
            topology_fresh=True,
            topology_age_seconds=age,
            demand_results=(
                DemandRunResult(
                    demand_path=config.demands.path,
                    demand_name=None,
                    action="failed",
                    error=str(exc),
                ),
            ),
        )

    runtime_path = config.state_dir / ".ninja-pce-daemon-runtime.json"
    try:
        with resource_lock(config.state_dir):
            runtime = load_state(runtime_path) or {"version": 2, "demands": {}}
    except ValueError as exc:
        return DaemonCycleResult(
            "degraded", started, _utc_now(), config.topology_feed.path,
            True, age, (), str(exc),
        )
    authoritative = authoritative_snapshot(topology, config.topology_feed.max_age_seconds or 60.0)
    for demand_path in demand_files:
        state_path = demand_state_path(config, demand_path)
        output_dir = demand_output_dir(config, demand_path)
        try:
            now = time.time()
            key = str(demand_path.resolve())
            revision = digest([
                _topology_event_revision(topology), file_fingerprint(demand_path),
                file_fingerprint(config.inventory_path),
            ])
            record = runtime["demands"].get(key, {})
            if record.get("revision") != revision:
                record = {"revision": revision, "first_seen": now, "attempts": 0, "next_attempt": 0.0}
            runtime["demands"][key] = record
            automatic = config.approval_policy.mode == "guarded-auto"
            if automatic and (record["attempts"] >= config.retry_attempts or now < record["next_attempt"]):
                demand_results.append(DemandRunResult(
                    demand_path, None, "backoff", state_path=state_path,
                    error="Automatic attempts are bounded; change inputs or inspect and reset retry state",
                ))
                continue
            preview = reconcile_once(
                topology_path=config.topology_feed.path,
                demand_path=demand_path,
                inventory_path=config.inventory_path,
                output_dir=output_dir,
                state_path=state_path,
                execute=False,
                approved=False,
                force=automatic,
            )
            if preview.action in {"preview", "no_config_change"}:
                decision = evaluate_auto_policy(
                    preview,
                    config.approval_policy,
                    environ=environ,
                )
            else:
                decision = PolicyDecision(allowed=False)
            if automatic and not authoritative:
                decision = PolicyDecision(False, decision.reasons + ("TED snapshot is not fresh, complete and authoritative",))
            if decision.allowed and now - record["first_seen"] < config.debounce_seconds:
                demand_results.append(DemandRunResult(
                    demand_path, preview.result.demand.name, "debounced", state_path=state_path,
                    path_nodes=preview.result.path_nodes, device=preview.plan.device.name,
                ))
                continue
            if (decision.allowed and preview.action == "no_config_change"
                    and now - record.get("last_verified", 0.0) < config.revalidation_interval_seconds):
                demand_results.append(DemandRunResult(
                    demand_path, preview.result.demand.name, "unchanged", state_path=state_path,
                    path_nodes=preview.result.path_nodes, device=preview.plan.device.name,
                ))
                continue
            final = preview
            auto_approved = False
            error: str | None = None
            if decision.allowed:
                try:
                    final = reconcile_once(
                        topology_path=config.topology_feed.path,
                        demand_path=demand_path,
                        inventory_path=config.inventory_path,
                        output_dir=output_dir,
                        state_path=state_path,
                        execute=True,
                        approved=True,
                        approved_plan_id=preview.plan.plan_id,
                        require_authoritative=True,
                        max_topology_age_seconds=config.topology_feed.max_age_seconds or 60.0,
                        connect_handler=connect_handler,
                        environ=dict(environ) if environ is not None else None,
                    )
                    auto_approved = final.action == "applied"
                    if final.action == "verification_failed":
                        error = "Operational LSP verification failed; reservation retained"
                    elif final.action == "recovery_required":
                        error = (
                            final.push_result.recovery_note if final.push_result else None
                        ) or "Interrupted delivery requires manual permanent commit confirmation"
                    else:
                        record["last_verified"] = now
                        record["attempts"] = 0
                except Exception as exc:
                    error = str(exc)
                if error:
                    record["attempts"] += 1
                    record["next_attempt"] = now + min(
                        config.retry_backoff_seconds * 2 ** (record["attempts"] - 1),
                        config.retry_max_backoff_seconds,
                    )

            demand_results.append(
                DemandRunResult(
                    demand_path=demand_path,
                    demand_name=preview.result.demand.name,
                    action=final.action if error is None or final.action in {"verification_failed", "recovery_required"} else "failed",
                    auto_approved=auto_approved,
                    policy_reasons=() if decision.allowed else decision.reasons,
                    error=error,
                    path_nodes=final.result.path_nodes,
                    device=final.plan.device.name,
                    state_path=state_path,
                )
            )
        except CspfSearchLimit as exc:
            demand_results.append(DemandRunResult(
                demand_path, None, "indeterminate", error=str(exc), state_path=state_path,
            ))
        except (InputError, CspfNoPath, ValueError) as exc:
            demand_results.append(
                DemandRunResult(
                    demand_path=demand_path,
                    demand_name=None,
                    action="failed",
                    error=str(exc),
                    state_path=state_path,
                )
            )
        except Exception as exc:
            demand_results.append(
                DemandRunResult(
                    demand_path=demand_path,
                    demand_name=None,
                    action="failed",
                    error=f"Unexpected daemon error: {exc}",
                    state_path=state_path,
                )
            )

    with resource_lock(config.state_dir):
        write_state(runtime_path, runtime)
    status = "ok"
    if any(result.error for result in demand_results):
        status = "degraded"
    if config.approval_policy.mode == "guarded-auto" and not authoritative:
        status = "degraded"
    finished = _utc_now()
    return DaemonCycleResult(
        status=status,
        started_at=started,
        finished_at=finished,
        topology_path=config.topology_feed.path,
        topology_fresh=True,
        topology_age_seconds=age,
        demand_results=tuple(demand_results),
    )


def start_health_server(
    health: HealthConfig,
    state: HealthState,
) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            return

        def do_GET(self) -> None:  # noqa: N802 - stdlib callback name
            snapshot = state.snapshot()
            if self.path in {"/healthz", "/"}:
                body = json.dumps(snapshot, indent=2, sort_keys=True).encode("utf-8")
                healthy = snapshot["last_cycle"] is not None and snapshot["last_cycle"]["status"] == "ok"
                self.send_response(200 if healthy else 503)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if self.path == "/metrics":
                lines = [
                    f"ninja_pce_{key} {value}"
                    for key, value in sorted(snapshot["metrics"].items())
                ]
                body = ("\n".join(lines) + "\n").encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; version=0.0.4")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            self.send_response(404)
            self.end_headers()

    server = ThreadingHTTPServer((health.host, health.port), Handler)
    return server
