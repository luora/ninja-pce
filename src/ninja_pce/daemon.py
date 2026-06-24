from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock
from typing import Any, Callable, Mapping

import yaml

from .cspf import CspfNoPath
from .loaders import InputError
from .reconcile import ReconcileOutcome, reconcile_once


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
                if result.error:
                    self._metrics["demands_failed"] += 1
                elif result.action == "applied":
                    self._metrics["demands_applied"] += 1
                elif result.action == "preview":
                    self._metrics["demands_preview"] += 1
                elif result.action == "unchanged":
                    self._metrics["demands_unchanged"] += 1
                elif result.action == "no_config_change":
                    self._metrics["demands_no_config_change"] += 1
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
        return (value,)
    return tuple(str(item) for item in value)


def _path(value: Any, *, base: Path | None = None) -> Path:
    if value is None:
        raise InputError("Expected a filesystem path")
    path = Path(str(value)).expanduser()
    if not path.is_absolute() and base is not None:
        path = base / path
    return path


def _safe_stem(path: Path) -> str:
    stem = path.stem
    return "".join(character if character.isalnum() or character in {"-", "_"} else "_" for character in stem)


def _load_mapping(path: str | Path) -> dict[str, Any]:
    config_path = Path(path)
    if not config_path.exists():
        raise InputError(f"Daemon config not found: {config_path}")
    text = config_path.read_text(encoding="utf-8")
    if config_path.suffix.lower() in {".yml", ".yaml"}:
        data = yaml.safe_load(text)
    elif config_path.suffix.lower() == ".json":
        data = json.loads(text)
    else:
        raise InputError("Daemon config must be .yml, .yaml, or .json")
    if not isinstance(data, dict):
        raise InputError("Daemon config must contain a mapping/object")
    return data


def load_daemon_config(path: str | Path) -> DaemonConfig:
    config_path = Path(path)
    base = config_path.resolve().parent
    data = _load_mapping(config_path)

    topology_data = data.get("topology_feed", {}) or {}
    demands_data = data.get("demands", {}) or {}
    policy_data = data.get("approval_policy", {}) or {}
    health_data = data.get("health", {}) or {}

    topology_type = str(topology_data.get("type", "file"))
    if topology_type != "file":
        raise InputError("Only topology_feed.type=file is supported")
    demands_type = str(demands_data.get("type", "directory"))
    if demands_type not in {"directory", "file"}:
        raise InputError("demands.type must be directory or file")

    mode = str(policy_data.get("mode", "manual"))
    if mode not in POLICY_MODES:
        raise InputError(f"approval_policy.mode must be one of {sorted(POLICY_MODES)}")

    custom_prefixes = policy_data.get("allowed_command_prefixes") or {}
    if custom_prefixes and not isinstance(custom_prefixes, dict):
        raise InputError("approval_policy.allowed_command_prefixes must be a mapping/object")
    allowed_prefixes = dict(DEFAULT_ALLOWED_PREFIXES)
    for output_format, prefixes in custom_prefixes.items():
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
        max_commands=int(policy_data.get("max_commands", 50)),
        min_residual_bw_mbps=float(policy_data.get("min_residual_bw_mbps", 0.0)),
        allow_warnings=bool(policy_data.get("allow_warnings", False)),
        allowed_output_formats=_tuple(policy_data.get("allowed_output_formats", ("iosxr", "iosxe", "junos"))),
        allowed_demand_names=_tuple(policy_data.get("allowed_demand_names", ())),
        allowed_device_tags=_tuple(policy_data.get("allowed_device_tags", ())),
        allowed_command_prefixes=allowed_prefixes,
    )
    if policy.max_commands < 1:
        raise InputError("approval_policy.max_commands must be at least 1")
    if policy.min_residual_bw_mbps < 0:
        raise InputError("approval_policy.min_residual_bw_mbps cannot be negative")

    poll_interval = float(data.get("poll_interval", 5.0))
    if poll_interval <= 0:
        raise InputError("poll_interval must be positive")

    return DaemonConfig(
        topology_feed=TopologyFeedConfig(
            type=topology_type,
            path=_path(topology_data.get("path"), base=base),
            max_age_seconds=(
                float(topology_data["max_age_seconds"])
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
            port=int(health_data.get("port", 8080)),
            enabled=bool(health_data.get("enabled", True)),
        ),
        poll_interval=poll_interval,
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
    return max(time.time() - path.stat().st_mtime, 0.0)


def topology_is_fresh(config: DaemonConfig) -> tuple[bool, float | None]:
    age = topology_age_seconds(config)
    max_age = config.topology_feed.max_age_seconds
    if max_age is None:
        return True, age
    return age <= max_age, age


def demand_state_path(config: DaemonConfig, demand_path: Path) -> Path:
    return config.state_dir / f".ninja-pce-{_safe_stem(demand_path)}-state.json"


def demand_output_dir(config: DaemonConfig, demand_path: Path) -> Path:
    return config.output_dir / _safe_stem(demand_path)


def _truthy(value: str | None) -> bool:
    return bool(value and value.strip().lower() in {"1", "true", "yes", "on"})


def _command_allowed(command: str, output_format: str, policy: ApprovalPolicyConfig) -> bool:
    prefixes = policy.allowed_command_prefixes.get(output_format, ())
    return any(command == prefix or command.startswith(prefix) for prefix in prefixes)


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
    if outcome.action != "preview":
        reasons.append(f"reconcile action is {outcome.action}, not preview")
    if not outcome.diff:
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

    fresh, age = topology_is_fresh(config)
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
                f"{config.topology_feed.max_age_seconds:g}s"
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

    for demand_path in demand_files:
        state_path = demand_state_path(config, demand_path)
        output_dir = demand_output_dir(config, demand_path)
        try:
            preview = reconcile_once(
                topology_path=config.topology_feed.path,
                demand_path=demand_path,
                inventory_path=config.inventory_path,
                output_dir=output_dir,
                state_path=state_path,
                execute=False,
                approved=False,
            )
            if preview.action == "preview":
                decision = evaluate_auto_policy(
                    preview,
                    config.approval_policy,
                    environ=environ,
                )
            else:
                decision = PolicyDecision(allowed=False)
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
                        connect_handler=connect_handler,
                        environ=dict(environ) if environ is not None else None,
                    )
                    auto_approved = final.action == "applied"
                except Exception as exc:
                    error = str(exc)

            demand_results.append(
                DemandRunResult(
                    demand_path=demand_path,
                    demand_name=preview.result.demand.name,
                    action=final.action if error is None else "failed",
                    auto_approved=auto_approved,
                    policy_reasons=() if decision.allowed else decision.reasons,
                    error=error,
                    path_nodes=final.result.path_nodes,
                    device=final.plan.device.name,
                    state_path=state_path,
                )
            )
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

    status = "ok"
    if any(result.error for result in demand_results):
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
                self.send_response(200)
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
