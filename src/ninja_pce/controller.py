"""A validated deployment configuration and supervisor for the existing controller.

This layer assembles the established collectors, protocol engine and read-only
dashboard. It grants no new protocol authority and never initializes or replays
provisioning or recovery state during configuration validation.
"""
from __future__ import annotations

import argparse
import asyncio
import fcntl
import hashlib
import ipaddress
import json
import math
import os
import re
import shutil
import signal
import stat
import sys
import time
import uuid
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import Any, BinaryIO

from .startup import (
    PERMANENT_EXIT,
    PermanentStartupError,
    permanent_os_error,
    require_available_listener,
    startup_finding,
)
from .state import digest, write_state

COMPONENTS = ("bgpls", "pcep", "dashboard")
OPERATING_MODES = ("observe", "pcep-control")
_SHUTDOWN_SECONDS = 10.0
_MAX_STATIC_BYTES = 1_048_576


def _strict_document(path: Path) -> Any:
    from .pcep_headend_policy import _load_document

    # The shared reader bounds size, rejects duplicate keys/nonfinite numbers,
    # and refuses nonregular files without blocking on a FIFO.
    try:
        return _load_document(path)
    except (OSError, RecursionError) as exc:
        raise ValueError(f"Cannot read bounded regular JSON input: {path}") from exc


def _object(value: Any, label: str, required: set[str], optional: set[str] | None = None) -> dict[str, Any]:
    if not isinstance(value, dict) or not required <= value.keys() or value.keys() - required - (optional or set()):
        raise ValueError(f"{label} has missing or unsupported configuration fields")
    return dict(value)


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 4096 or any(ord(char) < 32 for char in value):
        raise ValueError(f"{label} must be a nonempty bounded string without control characters")
    return value


def _integer(value: Any, label: str, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{label} must be an integer from {minimum} through {maximum}")
    return value


def _seconds(value: Any, label: str, maximum: float = 3600) -> float:
    try:
        valid = type(value) in (int, float) and math.isfinite(value) and 0 < value <= maximum
    except OverflowError:
        valid = False
    if not valid:
        raise ValueError(f"{label} must be finite and positive, at most {maximum:g} seconds")
    return float(value)


def _ipv4(value: Any, label: str) -> str:
    value = _text(value, label)
    address = ipaddress.IPv4Address(value)
    if str(address) != value or address.is_multicast or address.is_unspecified or int(address) == 2**32 - 1:
        raise ValueError(f"{label} must be a canonical unicast IPv4 address")
    return value


def _addresses(value: Any, label: str, *, required: bool = False) -> list[str]:
    if not isinstance(value, list) or len(value) > 256 or required and not value:
        raise ValueError(f"{label} requires {'1..256' if required else '0..256'} IPv4 addresses")
    result = [_ipv4(item, label) for item in value]
    if len(result) != len(set(result)):
        raise ValueError(f"{label} contains duplicate addresses")
    return result


def _path(value: Any, base: Path, label: str, *, exists: bool = False) -> str:
    candidate = Path(_text(value, label)).expanduser()
    candidate = candidate if candidate.is_absolute() else base / candidate
    if candidate.is_symlink():
        raise ValueError(f"{label} must not name a symbolic link")
    result = candidate.resolve()
    if exists and not result.is_file():
        raise ValueError(f"{label} requires an existing regular file: {result}")
    if result.exists() and not (result.is_file() if exists else result.is_file() or result.is_dir()):
        raise ValueError(f"{label} cannot name a special file")
    if any(parent.exists() and not parent.is_dir() for parent in result.parents):
        raise ValueError(f"{label} has a parent that is not a directory")
    return str(result)


@dataclass(frozen=True)
class ControllerConfig:
    """Canonical config stored as immutable JSON; each caller gets a fresh view."""

    path: Path
    _json: str
    config_digest: str

    @property
    def document(self) -> dict[str, Any]:
        return json.loads(self._json)


def effective_operating_mode(config: ControllerConfig) -> str:
    """Omitted mode preserves the existing version-1 PCEP configuration."""
    return str(config.document.get("operating_mode", "pcep-control"))


def load_controller_config(path: str | Path) -> ControllerConfig:
    """Validate static inputs without creating directories, journals or sockets."""
    try:
        return _load_controller_config(path)
    except (OSError, RecursionError) as exc:
        raise ValueError(f"Cannot read controller configuration or static inputs: {path}") from exc


def _load_controller_config(path: str | Path) -> ControllerConfig:
    from .bgpls import GoBgpTopologyProvider
    from .live import _node_names, _ospf_multi_area_policy
    from .pcep import PcepServerConfig
    from .pcep_domain_policy import DomainDelegationPolicy

    candidate = Path(path).expanduser()
    if candidate.is_symlink():
        raise ValueError("Controller configuration must not name a symbolic link")
    source = candidate.resolve()
    document = _strict_document(source)
    data = _object(document, "Controller", {"version", "files", "state_dir", "bgpls", "dashboard"},
                   {"runtime_dir", "operating_mode", "pcep"})
    if type(data["version"]) is not int or data["version"] != 1:
        raise ValueError("Controller configuration requires version 1")
    mode = data.get("operating_mode", "pcep-control")
    if not isinstance(mode, str) or mode not in OPERATING_MODES:
        raise ValueError("operating_mode must be observe or pcep-control; other modes are not implemented")
    if mode != "observe" and "pcep" not in data:
        raise ValueError("PCEP control requires its existing pcep configuration and domain policy")
    base = source.parent
    outputs = _object(data["files"], "files", {"bgpls_raw", "bgpls_status", "topology", "pcep_capture"})
    data["files"] = {key: _path(value, base, f"files.{key}") for key, value in outputs.items()}
    for value in data["files"].values():
        if Path(value).is_dir():
            raise ValueError("Controller observation output must name a file, not a directory")
    data["state_dir"] = _path(data["state_dir"], base, "state_dir")
    if Path(data["state_dir"]).exists() and not Path(data["state_dir"]).is_dir():
        raise ValueError("state_dir must name a directory")
    data["runtime_dir"] = _path(data.get("runtime_dir", str(Path(data["files"]["pcep_capture"]).parent /
                                                        ".controller")), base, "runtime_dir")
    if Path(data["runtime_dir"]).exists() and not Path(data["runtime_dir"]).is_dir():
        raise ValueError("runtime_dir must name a directory")
    if (Path(data["runtime_dir"]).is_relative_to(Path(data["state_dir"])) or
            Path(data["state_dir"]).is_relative_to(Path(data["runtime_dir"]))):
        raise ValueError("runtime_dir must be separate from the shared reservation state_dir")
    bgpls = _object(data["bgpls"], "bgpls", {"endpoint", "required_peers"},
                    {"grpcurl", "proto_path", "node_map", "ospf_multi_area_policy", "interval_seconds",
                     "timeout_seconds", "max_age_seconds"})
    bgpls["endpoint"] = _text(bgpls["endpoint"], "bgpls.endpoint")
    bgpls["required_peers"] = _addresses(bgpls["required_peers"], "bgpls.required_peers", required=True)
    executable = _text(bgpls.get("grpcurl", "grpcurl"), "bgpls.grpcurl")
    if executable.startswith("-"):
        raise ValueError("bgpls.grpcurl must name an executable, not an option")
    bgpls["grpcurl"] = _path(executable, base, "bgpls.grpcurl", exists=True) if "/" in executable else executable
    default_proto = str(files("ninja_pce").joinpath("data/gobgp-readonly.proto"))
    bgpls["proto_path"] = _path(bgpls.get("proto_path", default_proto), base, "bgpls.proto_path", exists=True)
    for key in ("node_map", "ospf_multi_area_policy"):
        if key in bgpls:
            bgpls[key] = _path(bgpls[key], base, f"bgpls.{key}", exists=True)
    for key, default, maximum in (("interval_seconds", 5, 3600), ("timeout_seconds", 10, 30),
                                   ("max_age_seconds", 30, 30)):
        bgpls[key] = _seconds(bgpls.get(key, default), f"bgpls.{key}", maximum)
    names = _node_names(bgpls.get("node_map"))
    for address, name in names.items():
        _ipv4(address, "node_map router ID")
        _text(name, "node_map name")
    area_policy = _ospf_multi_area_policy(bgpls.get("ospf_multi_area_policy"))
    # Constructor validation performs no RPC or mutation.
    GoBgpTopologyProvider(proto_path=bgpls["proto_path"], endpoint=bgpls["endpoint"],
                         required_peers=tuple(bgpls["required_peers"]), grpcurl=bgpls["grpcurl"],
                         timeout_seconds=bgpls["timeout_seconds"], max_age_seconds=bgpls["max_age_seconds"],
                         node_names=names, ospf_multi_area=area_policy)
    data["bgpls"] = bgpls
    pcep = _object(data.get("pcep", {}), "pcep", {"listen_host", "domain_policy"} if mode != "observe" else set(),
                   {"port", "keepalive_seconds", "deadtimer_seconds", "allowed_peers", "headends",
                    "delegated_tunnels", "failover", "provisioning_state", "managed_recovery", "listen_host", "domain_policy"})
    if mode == "observe" and {"failover", "provisioning_state", "managed_recovery"} & pcep.keys():
        raise ValueError("Observation configuration must omit active provisioning and recovery journal settings")
    pcep["listen_host"] = str(ipaddress.ip_address(_text(pcep.get("listen_host", "127.0.0.1"), "pcep.listen_host")))
    domain = None
    if "domain_policy" in pcep:
        pcep["domain_policy"] = _path(pcep["domain_policy"], base, "pcep.domain_policy", exists=mode != "observe")
    if mode != "observe":
        domain = DomainDelegationPolicy.from_path(Path(pcep["domain_policy"]))
    pcep["port"] = _integer(pcep.get("port", 4189), "pcep.port", 1, 65535)
    pcep["keepalive_seconds"] = _integer(pcep.get("keepalive_seconds", 30), "pcep.keepalive_seconds", 0, 255)
    pcep["deadtimer_seconds"] = _integer(pcep.get("deadtimer_seconds", 120), "pcep.deadtimer_seconds", 0, 255)
    pcep["allowed_peers"] = _addresses(pcep.get("allowed_peers", []), "pcep.allowed_peers")
    heads = pcep.get("headends", {})
    if not isinstance(heads, dict) or len(heads) > 16:
        raise ValueError("Legacy explicit headends are limited to 16; use domain discovery for ordinary PCCs")
    pcep["headends"] = {_ipv4(peer, "headend peer"): _ipv4(sender, "headend source")
                       for peer, sender in heads.items()}
    if not pcep["headends"].keys() <= set(pcep["allowed_peers"]):
        raise ValueError("Legacy explicit headends must also occur in pcep.allowed_peers")
    tunnels = pcep.get("delegated_tunnels", {})
    if not isinstance(tunnels, dict) or not tunnels.keys() <= pcep["headends"].keys():
        raise ValueError("Legacy delegated_tunnels must map explicit headend peers to tunnel ID lists")
    for peer, identifiers in tunnels.items():
        if not isinstance(identifiers, list) or not 1 <= len(identifiers) <= 16:
            raise ValueError("Legacy delegated tunnel lists require 1..16 IDs")
        for identifier in identifiers:
            _integer(identifier, "Legacy delegated tunnel ID", 1, 65535)
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("Duplicate legacy delegated tunnel ID")
    pcep["delegated_tunnels"] = tunnels
    if domain is not None:
        PcepServerConfig(listen_host=pcep["listen_host"], port=pcep["port"],
                         allowed_peers=tuple(pcep["allowed_peers"]),
                         allowed_peer_networks=tuple(str(network) for network in domain.pcc_prefixes),
                         keepalive=pcep["keepalive_seconds"], deadtimer=pcep["deadtimer_seconds"])
        from .pcep_compute import PcepPathComputer

        PcepPathComputer(raw_path=Path(data["files"]["bgpls_raw"]), status_path=Path(data["files"]["bgpls_status"]),
                         topology_path=Path(data["files"]["topology"]), required_peers=tuple(bgpls["required_peers"]),
                         node_names=names, headends=pcep["headends"], domain_policy=domain, ospf_multi_area=area_policy,
                         max_age_seconds=bgpls["max_age_seconds"])
    if "provisioning_state" in pcep:
        pcep["provisioning_state"] = _path(pcep["provisioning_state"], base, "pcep.provisioning_state", exists=True)
        if not heads:
            raise ValueError("Existing provisioning state requires explicit bound headends")
        from .dashboard import _read

        journal = _read(Path(pcep["provisioning_state"]), checksummed=True)
        if journal.get("kind") != "cli_tunnel_provisioning" or not isinstance(journal.get("operations"), dict):
            raise ValueError("provisioning_state must be the existing checksummed configuration journal")
    if "failover" in pcep:
        failure = _object(pcep["failover"], "pcep.failover", {"peer", "tunnel_id", "state_path"})
        failure["peer"] = _ipv4(failure["peer"], "failover.peer")
        _integer(failure["tunnel_id"], "failover.tunnel_id", 1, 65535)
        if failure["tunnel_id"] not in tunnels.get(failure["peer"], []):
            raise ValueError("Legacy failover needs the same explicit delegated tunnel binding")
        failure["state_path"] = _path(failure["state_path"], base, "failover.state_path")
        _validate_existing_state(failure["state_path"])
        pcep["failover"] = failure
    if "managed_recovery" in pcep:
        managed = _object(pcep["managed_recovery"], "pcep.managed_recovery",
                          {"operation_id", "journal_path", "startup_recovery"}, {"reinstatement_path"})
        if not isinstance(managed["operation_id"], str) or not re.fullmatch("[0-9a-f]{32}", managed["operation_id"]):
            raise ValueError("managed_recovery.operation_id requires an exact existing operation ID")
        if type(managed["startup_recovery"]) is not bool:
            raise ValueError("managed_recovery.startup_recovery must be a boolean")
        if "provisioning_state" not in pcep:
            raise ValueError("Managed recovery requires the existing provisioning_state")
        if managed["operation_id"] not in journal["operations"]:
            raise ValueError("Managed recovery operation is absent from the provisioning journal")
        operation = journal["operations"][managed["operation_id"]]
        if not isinstance(operation, dict) or operation.get("status") != "VERIFIED":
            raise ValueError("Managed recovery requires an existing VERIFIED provisioning operation")
        managed["journal_path"] = _path(managed["journal_path"], base, "managed_recovery.journal_path")
        _validate_existing_state(managed["journal_path"])
        if Path(managed["journal_path"]).parent == Path(pcep["provisioning_state"]).parent:
            raise ValueError("Managed recovery journal requires a separate directory from provisioning state")
        if "reinstatement_path" in managed:
            managed["reinstatement_path"] = _path(managed["reinstatement_path"], base,
                                                 "managed_recovery.reinstatement_path", exists=True)
        pcep["managed_recovery"] = managed
    data["pcep"] = pcep
    dashboard = _object(data["dashboard"], "dashboard", set(), {"enabled", "listen_host", "port", "refresh_seconds"})
    dashboard["enabled"] = dashboard.get("enabled", True)
    if type(dashboard["enabled"]) is not bool:
        raise ValueError("dashboard.enabled must be a boolean")
    dashboard["listen_host"] = _text(dashboard.get("listen_host", "127.0.0.1"), "dashboard.listen_host")
    dashboard_address = ipaddress.IPv4Address(dashboard["listen_host"])
    if not dashboard_address.is_loopback:
        raise ValueError("The unauthenticated dashboard must bind IPv4 loopback; use an authenticated reverse proxy")
    dashboard["port"] = _integer(dashboard.get("port", 8088), "dashboard.port", 1, 65535)
    dashboard["refresh_seconds"] = _seconds(dashboard.get("refresh_seconds", 5), "dashboard.refresh_seconds")
    if (mode != "observe" and dashboard["enabled"] and dashboard["port"] == pcep["port"] and
            (pcep["listen_host"] in {"0.0.0.0", "::", dashboard["listen_host"]})):
        raise ValueError("PCEP and dashboard listener addresses overlap")
    data["dashboard"] = dashboard
    _validate_paths(data, source)
    canonical = json.dumps(data, sort_keys=True, separators=(",", ":"), allow_nan=False)
    static_digests = {"domain_policy": domain.policy_digest if domain is not None else None, "node_map": digest(names),
                      "ospf_multi_area_policy": area_policy.digest if area_policy is not None else None,
                      "proto_sha256": _static_digest(Path(bgpls["proto_path"]))}
    return ControllerConfig(source, canonical, digest({"configuration": data, "static_inputs": static_digests}))


def _static_digest(path: Path) -> str:
    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_STATIC_BYTES:
            raise ValueError("Static protocol definition must be a bounded regular file")
        raw = handle.read(_MAX_STATIC_BYTES + 1)
    if len(raw) > _MAX_STATIC_BYTES:
        raise ValueError("Static protocol definition exceeds the input size limit")
    return hashlib.sha256(raw).hexdigest()


def _validate_existing_state(path: str) -> None:
    if Path(path).exists():
        from .dashboard import _read

        try:
            _read(Path(path), checksummed=True)
        except OSError as exc:
            raise ValueError(f"Cannot read existing checksummed state: {path}") from exc


def _validate_paths(data: dict[str, Any], source: Path) -> None:
    inputs = [str(source), data["bgpls"]["proto_path"]]
    if "domain_policy" in data["pcep"]:
        inputs.append(data["pcep"]["domain_policy"])
    inputs.extend(data["bgpls"][key] for key in ("node_map", "ospf_multi_area_policy") if key in data["bgpls"])
    if "/" in data["bgpls"]["grpcurl"]:
        inputs.append(data["bgpls"]["grpcurl"])
    static_inputs = list(inputs)
    outputs = list(data["files"].values())
    pcep = data["pcep"]
    if "provisioning_state" in pcep:
        inputs.append(pcep["provisioning_state"])
        inputs.append(str(Path(pcep["provisioning_state"]).parent / ".ninja-pce-reservations.json"))
    if "failover" in pcep:
        outputs.append(pcep["failover"]["state_path"])
    if "managed_recovery" in pcep:
        outputs.append(pcep["managed_recovery"]["journal_path"])
        if "reinstatement_path" in pcep["managed_recovery"]:
            inputs.append(pcep["managed_recovery"]["reinstatement_path"])
    state_dir = Path(data["state_dir"])
    runtime_dir = Path(data["runtime_dir"])
    if any(Path(value).is_relative_to(directory) for value in static_inputs
           for directory in (state_dir, runtime_dir)):
        raise ValueError("Static controller inputs must be outside writable state and runtime directories")
    if any(Path(value).is_relative_to(state_dir) for value in data["files"].values()):
        raise ValueError("Live observation files must be outside the shared reservation state_dir")
    if any(Path(value).is_relative_to(runtime_dir) for value in inputs + outputs):
        raise ValueError("Protocol inputs and outputs must be outside the wrapper runtime_dir")
    reserved = [str(runtime_dir / f".ninja-pce-controller-{component}{suffix}")
                for component in COMPONENTS for suffix in (".json", ".lock", "-startup.json")]
    all_paths = inputs + outputs
    if len(all_paths) != len(set(all_paths)) or set(all_paths) & set(reserved):
        raise ValueError("Controller input, output, journal and runtime paths must be distinct")
    if str(state_dir / ".ninja-pce-reservations.json") in outputs:
        raise ValueError("Controller outputs must be distinct from the shared reservation ledger")
    if str(state_dir) in all_paths or str(runtime_dir) in all_paths or any(Path(value).is_dir() for value in outputs):
        raise ValueError("Controller state directory and file paths must not overlap")


def _components(config: ControllerConfig, requested: list[str] | tuple[str, ...] | None) -> tuple[str, ...]:
    enabled = tuple(component for component in COMPONENTS
                    if (component != "dashboard" or config.document["dashboard"]["enabled"]) and
                    (component != "pcep" or effective_operating_mode(config) != "observe"))
    if requested is None:
        return enabled
    if not requested or len(requested) != len(set(requested)) or not set(requested) <= set(enabled):
        raise ValueError("Select distinct enabled controller components; PCEP is prohibited in observe mode")
    return tuple(component for component in COMPONENTS if component in requested)


def component_commands(config: ControllerConfig, components: list[str] | tuple[str, ...] | None = None
                       ) -> dict[str, list[str]]:
    """Produce only known argv options; configuration cannot inject shell text."""
    data = config.document
    outputs, bgpls, pcep, dashboard = (data[key] for key in ("files", "bgpls", "pcep", "dashboard"))
    prefix = [sys.executable, "-m", "ninja_pce.cli"]
    collector = [*prefix, "bgpls-collect", "--endpoint", bgpls["endpoint"], "--grpcurl", bgpls["grpcurl"],
                 "--proto", bgpls["proto_path"], "--output", outputs["topology"], "--status", outputs["bgpls_status"],
                 "--raw", outputs["bgpls_raw"], "--interval", str(bgpls["interval_seconds"]),
                 "--timeout", str(bgpls["timeout_seconds"]), "--max-age", str(bgpls["max_age_seconds"])]
    listener = [*prefix, "pcep-listen", "--listen-host", pcep["listen_host"], "--port", str(pcep["port"]),
                "--capture", outputs["pcep_capture"], "--keepalive", str(pcep["keepalive_seconds"]),
                "--deadtimer", str(pcep["deadtimer_seconds"]), "--compute-bgpls-raw", outputs["bgpls_raw"],
                "--compute-bgpls-status", outputs["bgpls_status"], "--compute-topology", outputs["topology"],
                "--compute-domain-policy", pcep.get("domain_policy", ""),
                "--compute-max-age", str(bgpls["max_age_seconds"])]
    web = [*prefix, "dashboard", "--host", dashboard["listen_host"], "--port", str(dashboard["port"]),
           "--topology", outputs["topology"], "--bgpls-status", outputs["bgpls_status"],
           "--bgpls-raw", outputs["bgpls_raw"], "--pcep", outputs["pcep_capture"], "--state-dir", data["state_dir"],
           "--max-age", str(bgpls["max_age_seconds"]), "--refresh", str(dashboard["refresh_seconds"])]
    web.extend(("--operating-mode", effective_operating_mode(config), "--controller-config", str(config.path),
                "--controller-digest", config.config_digest))
    for peer in bgpls["required_peers"]:
        collector.extend(("--peer", peer))
        listener.extend(("--compute-bgpls-peer", peer))
        web.extend(("--bgpls-peer", peer))
    for key, collector_flag, listener_flag in (
            ("node_map", "--node-map", "--compute-node-map"),
            ("ospf_multi_area_policy", "--ospf-multi-area-policy", "--compute-ospf-multi-area-policy")):
        if key in bgpls:
            collector.extend((collector_flag, bgpls[key]))
            listener.extend((listener_flag, bgpls[key]))
            if key == "ospf_multi_area_policy":
                web.extend((collector_flag, bgpls[key]))
    for peer in pcep["allowed_peers"]:
        listener.extend(("--allow-peer", peer))
    for peer, source in sorted(pcep["headends"].items()):
        listener.extend(("--compute-headend", f"{peer}={source}"))
    for peer, identifiers in sorted(pcep["delegated_tunnels"].items()):
        for identifier in identifiers:
            listener.extend(("--compute-delegated-tunnel", f"{peer}={identifier}"))
    if "provisioning_state" in pcep:
        listener.extend(("--provisioning-state", pcep["provisioning_state"]))
    if "failover" in pcep:
        failure = pcep["failover"]
        listener.extend(("--failover-tunnel", f"{failure['peer']}={failure['tunnel_id']}",
                         "--failover-state", failure["state_path"]))
    if "managed_recovery" in pcep:
        managed = pcep["managed_recovery"]
        listener.extend(("--managed-failover-operation", managed["operation_id"],
                         "--managed-failover-state", managed["journal_path"]))
        if managed["startup_recovery"]:
            listener.append("--managed-startup-recovery")
        if "reinstatement_path" in managed:
            listener.extend(("--managed-reinstate-request", managed["reinstatement_path"]))
    commands = {"bgpls": collector, "pcep": listener, "dashboard": web}
    for command in commands.values():
        command.append("--managed-startup")
    return {key: commands[key] for key in _components(config, components)}


def controller_plan(config: ControllerConfig, components: list[str] | tuple[str, ...] | None = None
                    ) -> dict[str, Any]:
    data = config.document
    return {"kind": "controller_plan", "config": str(config.path), "config_digest": config.config_digest,
            "operating_mode": effective_operating_mode(config),
            "state_dir": data["state_dir"], "runtime_dir": data["runtime_dir"], "files": data["files"],
            "components": component_commands(config, components), "writes_network_state": False,
            "run_behavior": ("Observation mode disables PCEP and permits no controller network writes."
                             if effective_operating_mode(config) == "observe" else
                             "The PCEP component may send policy-authorized paths to delegated LSPs.")}


def _process_identity(pid: int) -> str | None:
    """Linux start ticks fence PID reuse when checking a saved runtime record."""
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        if fields[0] == "Z":
            return None
        return fields[19]
    except (OSError, ValueError, IndexError):
        return None


def _runtime_path(config: ControllerConfig, component: str) -> Path:
    return Path(config.document["runtime_dir"]) / f".ninja-pce-controller-{component}.json"


def _startup_fault_path(config: ControllerConfig, component: str) -> Path:
    return Path(config.document["runtime_dir"]) / f".ninja-pce-controller-{component}-startup.json"


def _runtime_status(config: ControllerConfig, component: str) -> dict[str, Any]:
    from .dashboard import _read

    try:
        fault = _read(_startup_fault_path(config, component), checksummed=True)
        supervisor = fault.get("supervisor", {})
        if (fault.get("kind") == "controller_startup_fault" and fault.get("component") == component and
                fault.get("config_digest") == config.config_digest and isinstance(supervisor, dict) and
                type(supervisor.get("pid")) is int and isinstance(supervisor.get("start_ticks"), str) and
                _process_identity(supervisor["pid"]) == supervisor["start_ticks"] and
                isinstance(fault.get("finding"), dict)):
            return {"component": component, "state": "failed", "returncode": PERMANENT_EXIT,
                    "updated_at": fault.get("updated_at"), "finding": fault["finding"],
                    "error": str(fault["finding"].get("message", "Startup ownership conflict"))[:512]}
    except (OSError, ValueError, TypeError, RecursionError):
        pass
    try:
        record = _read(_runtime_path(config, component), checksummed=True)
    except (OSError, ValueError, TypeError, RecursionError):
        return {"component": component, "state": "unobserved"}
    if (record.get("kind") != "controller_component" or record.get("component") != component or
            record.get("config_digest") != config.config_digest):
        return {"component": component, "state": "config_mismatch"}
    running = record.get("state") == "running"
    for field in ("supervisor", "child"):
        identity = record.get(field)
        if (not isinstance(identity, dict) or type(identity.get("pid")) is not int or
                not isinstance(identity.get("start_ticks"), str) or
                _process_identity(identity["pid"]) != identity["start_ticks"]):
            running = False
    return {"component": component,
            "state": "running" if running else "failed" if record.get("state") == "failed" else "stopped",
            "updated_at": record.get("updated_at"), "returncode": record.get("returncode"),
            "error": record["error"][:512] if isinstance(record.get("error"), str) else None,
            "finding": record.get("finding") if isinstance(record.get("finding"), dict) else None}


def controller_status(config: ControllerConfig) -> tuple[dict[str, Any], int]:
    from .dashboard import DashboardConfig, DashboardStore
    from .live import _ospf_multi_area_policy
    from .readiness import evaluate_readiness

    data = config.document
    outputs, bgpls = data["files"], data["bgpls"]
    snapshot = DashboardStore(DashboardConfig(
        topology_path=Path(outputs["topology"]), bgpls_status_path=Path(outputs["bgpls_status"]),
        bgpls_raw_path=Path(outputs["bgpls_raw"]), pcep_path=Path(outputs["pcep_capture"]),
        state_dir=Path(data["state_dir"]), max_age_seconds=bgpls["max_age_seconds"],
        ospf_multi_area=_ospf_multi_area_policy(bgpls.get("ospf_multi_area_policy")),
        operating_mode=effective_operating_mode(config), required_bgpls_peers=tuple(bgpls["required_peers"]),
    )).snapshot()
    sources = {source["id"]: source for source in snapshot["sources"]}
    peers = [peer for peer in snapshot["peers"] if peer["protocol"] == "PCEP"]
    synchronized = sum(peer["fresh"] and peer["synchronized"] for peer in peers)
    processes = [_runtime_status(config, component) for component in _components(config, None)]
    readiness = evaluate_readiness(snapshot, effective_operating_mode(config), processes)
    status = "healthy" if readiness["ready"] else "degraded"
    result = {"kind": "controller_status", "config_digest": config.config_digest,
              "generated_at": snapshot["generated_at"], "status": status,
              "operating_mode": effective_operating_mode(config), "readiness": readiness,
              "processes": processes, "sources": list(sources.values()), "counts": snapshot["summary"],
              "protocol": {"observed_pccs": len(peers), "fresh_synchronized_pccs": synchronized},
              "forwarding_verified": False}
    result["message"] = readiness["message"]
    return result, 0 if status == "healthy" else 1


async def _stop_children(processes: dict[str, asyncio.subprocess.Process]) -> None:
    for process in processes.values():
        if process.returncode is None:
            try:
                process.terminate()
            except ProcessLookupError:
                pass
    if not processes:
        return
    waits = [asyncio.create_task(process.wait()) for process in processes.values()]
    _, pending = await asyncio.wait(waits, timeout=_SHUTDOWN_SECONDS)
    if pending:
        for process in processes.values():
            if process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
    await asyncio.gather(*waits, return_exceptions=True)


async def _supervise(config: ControllerConfig, commands: dict[str, list[str]], *, preflight: bool = False) -> int:
    _components(config, tuple(commands))
    processes: dict[str, asyncio.subprocess.Process] = {}
    locks: list[BinaryIO] = []
    records: dict[str, dict[str, Any]] = {}
    waiting: list[asyncio.Task[Any]] = []
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    handlers: list[signal.Signals] = []
    previous_handlers: dict[signal.Signals, Any] = {}
    token = uuid.uuid4().hex
    keep_management = "dashboard" in commands
    ordered = (["dashboard"] if keep_management else []) + [key for key in commands if key != "dashboard"]
    protocol_failed = False
    runtime_dir = Path(config.document["runtime_dir"])
    _recheck_configuration(config)
    runtime_dir.mkdir(parents=True, exist_ok=True)

    def lock_component(component: str) -> None:
        descriptor = os.open(runtime_dir / f".ninja-pce-controller-{component}.lock",
                             os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        handle = os.fdopen(descriptor, "r+b")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            handle.close()
            raise PermanentStartupError(startup_finding(
                "COMPONENT_ALREADY_SUPERVISED", component, f"Controller component already supervised: {component}",
                "Use the existing component's managed stop/restart workflow; do not launch another owner.")) from exc
        locks.append(handle)

    async def stop_protocols() -> None:
        await _stop_children({key: child for key, child in processes.items() if key != "dashboard"})
        for key, child in processes.items():
            if key == "dashboard" or records[key].get("state") == "failed":
                continue
            records[key].update(state="stopped", updated_at=time.time(), returncode=child.returncode,
                                error="Protocol group stopped after a sibling failure; inspect the original fault.")
            try:
                write_state(_runtime_path(config, key), records[key])
            except OSError as exc:
                print(f"Cannot persist protocol stop for {key}: {exc}", file=sys.stderr, flush=True)

    try:
        for signum in (signal.SIGTERM, signal.SIGINT):
            previous_handlers[signum] = signal.getsignal(signum)
            loop.add_signal_handler(signum, stop.set)
            handlers.append(signum)
        for component in ordered:
            if stop.is_set():
                break
            if protocol_failed and component != "dashboard":
                continue
            _recheck_configuration(config)
            record = {"kind": "controller_component", "component": component, "config_digest": config.config_digest,
                      "run_token": token, "state": "starting", "updated_at": time.time(),
                      "supervisor": {"pid": os.getpid(), "start_ticks": _process_identity(os.getpid())}, "child": {}}
            locked = False
            try:
                lock_component(component)
                locked = True
                if preflight and component in {"dashboard", "pcep"}:
                    listener = config.document[component]
                    require_available_listener(listener["listen_host"], listener["port"], component=component)
                if preflight and component == "bgpls" and shutil.which(config.document["bgpls"]["grpcurl"]) is None:
                    raise PermanentStartupError(startup_finding(
                        "BGPLS_CLIENT_MISSING", component,
                        "The configured grpcurl executable is missing or not executable.",
                        "Install the verified client or correct bgpls.grpcurl before restarting the collector."))
                process = await asyncio.create_subprocess_exec(*commands[component])
                processes[component] = process
                record.update(state="running", child={"pid": process.pid, "start_ticks": _process_identity(process.pid)})
                records[component] = record
                write_state(_runtime_path(config, component), record)
                print(json.dumps({"component": component, "state": "started", "pid": process.pid}), flush=True)
            except (OSError, PermanentStartupError) as exc:
                if not keep_management or component == "dashboard":
                    raise
                protocol_failed = True
                record.update(state="failed", error=f"Component startup failed: {exc}", updated_at=time.time())
                if isinstance(exc, PermanentStartupError):
                    record.update(finding=exc.finding, returncode=PERMANENT_EXIT)
                elif permanent_os_error(exc):
                    record.update(finding=startup_finding("STARTUP_LOCAL_RESOURCE_ERROR", component, str(exc),
                                                         errno=exc.errno), returncode=PERMANENT_EXIT)
                try:
                    if locked:
                        records[component] = record
                        write_state(_runtime_path(config, component), record)
                    else:
                        fault = {**record, "kind": "controller_startup_fault"}
                        write_state(_startup_fault_path(config, component), fault)
                except OSError:
                    pass
                print(json.dumps({"component": component, "state": "startup_failed", "error": str(exc)}),
                      file=sys.stderr, flush=True)
                await stop_protocols()
        stop_task = asyncio.create_task(stop.wait())
        waiting.append(stop_task)
        child_tasks = {asyncio.create_task(process.wait()): component for component, process in processes.items()
                       if component == "dashboard" or not protocol_failed}
        waiting.extend(child_tasks)
        while True:
            done, _ = await asyncio.wait([stop_task, *child_tasks], return_when=asyncio.FIRST_COMPLETED)
            if stop_task in done:
                return 0
            exited = [child_tasks[task] for task in done if task in child_tasks]
            if "dashboard" in exited or not keep_management:
                component = "dashboard" if "dashboard" in exited else exited[0]
                print(json.dumps({"component": component, "state": "unexpected_exit",
                                  "returncode": processes[component].returncode}), file=sys.stderr, flush=True)
                return PERMANENT_EXIT if processes[component].returncode == PERMANENT_EXIT else 1
            for component in exited:
                record = records[component]
                record.update(state="failed", updated_at=time.time(), returncode=processes[component].returncode,
                              error="Protocol component exited unexpectedly; management remains available.")
                if processes[component].returncode == PERMANENT_EXIT:
                    record["finding"] = startup_finding(
                        "COMPONENT_PERMANENT_STARTUP_FAILURE", component,
                        "Component rejected startup with exit status 78; inspect its original structured error.")
                try:
                    write_state(_runtime_path(config, component), record)
                except OSError as exc:
                    print(f"Cannot persist component fault for {component}: {exc}", file=sys.stderr, flush=True)
                print(json.dumps({"component": component, "state": "unexpected_exit",
                                  "returncode": processes[component].returncode}), file=sys.stderr, flush=True)
            await stop_protocols()
            # A failed protocol group is never automatically restarted or allowed
            # to leave another writer running against uncertain sibling state.
            for task, component in tuple(child_tasks.items()):
                if component != "dashboard":
                    await task
                    del child_tasks[task]
    finally:
        await _stop_children(processes)
        for task in waiting:
            if not task.done():
                task.cancel()
        if waiting:
            await asyncio.gather(*waiting, return_exceptions=True)
        for component, record in records.items():
            record.update(state="failed" if record.get("state") == "failed" else "stopped", updated_at=time.time())
            if component in processes:
                record["returncode"] = processes[component].returncode
            try:
                write_state(_runtime_path(config, component), record)
            except OSError as exc:
                print(f"Cannot persist stopped controller status for {component}: {exc}", file=sys.stderr, flush=True)
        for signum in handlers:
            loop.remove_signal_handler(signum)
            signal.signal(signum, previous_handlers[signum])
        for lock_file in locks:
            lock_file.close()


def _recheck_configuration(config: ControllerConfig) -> None:
    if load_controller_config(config.path).config_digest != config.config_digest:
        raise PermanentStartupError(startup_finding(
            "CONTROLLER_CONFIGURATION_CHANGED", "controller",
            "Controller configuration or static policy inputs changed before component startup"))


def run_controller(config: ControllerConfig, components: list[str] | tuple[str, ...] | None = None) -> int:
    try:
        return asyncio.run(_supervise(config, component_commands(config, components), preflight=True))
    except KeyboardInterrupt:
        return 0
    except OSError as exc:
        if permanent_os_error(exc):
            raise PermanentStartupError(startup_finding("STARTUP_LOCAL_RESOURCE_ERROR", "controller", str(exc),
                                                       errno=exc.errno)) from exc
        raise


def add_controller_commands(subparsers: Any) -> None:
    controller = subparsers.add_parser("controller", help="Validate, run and inspect one generic controller application")
    actions = controller.add_subparsers(dest="controller_action", required=True)
    for action in ("validate", "plan", "run", "status"):
        command = actions.add_parser(action, help=f"{action.capitalize()} the configured controller")
        command.add_argument("--config", required=True, help="Version 1 controller JSON configuration")
        if action == "run":
            command.add_argument("--component", action="append", choices=COMPONENTS,
                                 help="Run only these enabled components; repeatable, defaults to all enabled")


def controller_command(args: argparse.Namespace) -> int:
    try:
        config = load_controller_config(args.config)
    except ValueError as exc:
        if args.controller_action == "run":
            raise PermanentStartupError(startup_finding("CONTROLLER_CONFIGURATION_INVALID", "controller", str(exc))) from exc
        raise
    if args.controller_action == "run":
        try:
            return run_controller(config, args.component)
        except PermanentStartupError:
            raise
        except ValueError as exc:
            raise PermanentStartupError(startup_finding("CONTROLLER_CONFIGURATION_INVALID", "controller", str(exc))) from exc
    if args.controller_action == "status":
        document, code = controller_status(config)
    elif args.controller_action == "plan":
        document, code = controller_plan(config), 0
    else:
        document, code = {"kind": "controller_configuration", "valid": True,
                          "config": str(config.path), "config_digest": config.config_digest,
                          "operating_mode": effective_operating_mode(config),
                          "components": list(component_commands(config)),
                          "live_health_checked": False}, 0
    print(json.dumps(document, sort_keys=True, indent=2, allow_nan=False))
    return code
