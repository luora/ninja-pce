"""Shared read-only reports; unknown evidence never becomes authority or health."""
from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import math
import os
import re
import shutil
import stat
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .controller import (
    ControllerConfig,
    _components,
    _runtime_status,
    effective_operating_mode,
    load_controller_config,
)
from .dashboard import DashboardConfig, DashboardStore
from .diagnostics_host import _run, artifact_findings, host_findings, service_findings
from .diagnostics_protocol import protocol_findings
from .listeners import inspect_listener

_CODE = re.compile(r"[A-Z][A-Z0-9_]{0,95}\Z")
_SENSITIVE = re.compile(r"password|secret|token|cookie|credential|private.key|raw.hex|raw.payload|stdout|stderr|command|environment", re.I)
_SEVERITIES = ("blocker", "warning", "unknown", "info", "ok")


def finding(code: str, severity: str, category: str, message: str, remediation: str, **evidence: Any) -> dict[str, Any]:
    return {"code": code, "severity": severity, "category": category, "message": message,
            "remediation": remediation, "evidence": evidence}


def _safe(value: Any, depth: int = 0) -> Any:
    if value is None or type(value) is bool:
        return value
    if type(value) is int:
        return value if abs(value) <= 2**64 else None
    if type(value) is float:
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        return "".join(char for char in value[:512] if ord(char) >= 32 or char in "\n\t")
    if depth >= 3:
        return "Further detail omitted"
    if isinstance(value, list):
        return [_safe(item, depth + 1) for item in value[:16]]
    if isinstance(value, dict):
        return {key[:80]: _safe(item, depth + 1) for key, item in list(value.items())[:24]
                if isinstance(key, str) and not _SENSITIVE.search(key)}
    return None


def report(findings: list[dict[str, Any]], *, mode: str = "observe", live: bool = False,
           limitations: list[str] | None = None) -> dict[str, Any]:
    now = datetime.now(timezone.utc).isoformat()
    normalized = []
    seen = set()
    for item in findings[:256]:
        code = item.get("code")
        if not isinstance(code, str) or not _CODE.fullmatch(code):
            continue
        severity = item.get("severity") if item.get("severity") in _SEVERITIES else "unknown"
        evidence = _safe(item.get("evidence", {}))
        identity = (code, json.dumps(evidence, sort_keys=True))
        if identity in seen:
            continue
        seen.add(identity)
        normalized.append({"code": code, "severity": severity, "category": str(item.get("category", "runtime"))[:32],
                           "message": str(item.get("message", "Observation is unknown."))[:1024],
                           "remediation": str(item.get("remediation", "Review current evidence before changing services."))[:2048],
                           "evidence": evidence, "observed_at": now})
    if len(findings) > 256:
        normalized.append({**finding("DIAGNOSTICS_LIMIT_REACHED", "unknown", "runtime", "Report findings were capped.",
                                    "Inspect the selected scope in smaller requests."), "observed_at": now})
    normalized.sort(key=lambda item: (_SEVERITIES.index(item["severity"]), item["category"], item["code"]))
    summary = {severity: sum(item["severity"] == severity for item in normalized) for severity in _SEVERITIES}
    overall = "blocked" if summary["blocker"] else "warning" if summary["warning"] else "unknown" if summary["unknown"] or not normalized else "ok"
    coverage = ["Findings describe this process's namespaces and permissions.",
                "Diagnostics never grant control authority or verify forwarding.",
                "No service, process, configuration, reservation or recovery state is changed."]
    coverage.extend(limitations or [])
    if not live:
        coverage.append("Live RPC, address assignment, time synchronization, service state and native hashes were not probed.")
    return {"schema_version": 1, "generated_at": now, "scope": "live" if live else "snapshot",
            "operating_mode": mode, "overall": overall, "summary": summary, "findings": normalized,
            "limitations": list(dict.fromkeys(coverage))[:20], "read_only": True, "complete": bool(normalized) and not summary["unknown"],
            "control_authority_verified": False, "forwarding_verified": False}


def _dashboard_config(config: ControllerConfig) -> DashboardConfig:
    from .live import _ospf_multi_area_policy

    data = config.document
    return DashboardConfig(operating_mode=effective_operating_mode(config),
        topology_path=Path(data["files"]["topology"]), bgpls_raw_path=Path(data["files"]["bgpls_raw"]),
        bgpls_status_path=Path(data["files"]["bgpls_status"]), pcep_path=Path(data["files"]["pcep_capture"]),
        state_dir=Path(data["state_dir"]), host=data["dashboard"]["listen_host"], port=data["dashboard"]["port"],
        max_age_seconds=data["bgpls"]["max_age_seconds"], required_bgpls_peers=tuple(data["bgpls"]["required_peers"]),
        ospf_multi_area=_ospf_multi_area_policy(data["bgpls"].get("ospf_multi_area_policy")))


def _listener(address: str, port: int, component: str, expected: str | None) -> list[dict[str, Any]]:
    evidence = inspect_listener(address, port, expected)
    code = evidence["code"]
    details = {key: evidence[key] for key in ("address", "port", "owner_visibility", "owners", "expected_service")}
    if code == "PORT_AVAILABLE":
        return [finding(f"{component}_LISTENER_ABSENT", "warning", "runtime", "No overlapping TCP listener was observed.",
                        "Review the intended service and address. This snapshot does not reserve the port.", **details)]
    if code == "PORT_INSPECTION_UNAVAILABLE":
        return [finding(f"{component}_LISTENER_UNKNOWN", "unknown", "runtime", "Listener presence could not be established.",
                        "Inspect kernel listener metadata in the intended network namespace with sufficient read permissions.", **details)]
    return [finding(f"{component}_LISTENER_OBSERVED", "info", "runtime", "A TCP listener was observed; an occupied port alone does not identify a conflict.",
                    "Compare the reported owner with the intended service; use normal service controls after verification.", **details),
            finding(f"{component}_OWNERSHIP_VERIFIED" if code == "PORT_OWNED_BY_SERVICE" else f"{component}_OWNERSHIP_UNKNOWN",
                    "ok" if code == "PORT_OWNED_BY_SERVICE" else "unknown", "runtime",
                    "Listener cgroup ownership matches the expected unit." if code == "PORT_OWNED_BY_SERVICE" else
                    "Exact expected service ownership is not established by this listener snapshot.",
                    "Do not kill an arbitrary PID. Review namespace, cgroup and process identities before resolving a duplicate.", **details)]


def live_gobgp(config: ControllerConfig) -> list[dict[str, Any]]:
    """Two capped read-only RPCs to the validated loopback endpoint, never routers."""
    from .bgpls import _AF, _peers, decode_json_stream

    data = config.document["bgpls"]
    executable = shutil.which(data["grpcurl"])
    if executable is None:
        return [finding("BGPLS_RPC_CLIENT_UNAVAILABLE", "blocker", "bgpls", "The configured RPC client is unavailable.",
                        "Install the verified grpcurl client before retrying the live check.")]
    proto = Path(data["proto_path"])
    prefix = [executable, "-plaintext", "-emit-defaults", "-max-time", "1.5", "-import-path", str(proto.parent), "-proto", proto.name]
    findings = []
    peers_before = None
    for method, request in (("ListPeer", {}), ("ListPath", {"tableType": "GLOBAL", "family": _AF, "enableOnlyBinary": True})):
        output, error = _run([*prefix, "-d", json.dumps(request), data["endpoint"], f"apipb.GobgpApi/{method}"],
                             timeout=2, maximum=2 * 1024 * 1024)
        if error:
            findings.append(finding("BGPLS_RPC_UNAVAILABLE" if error == "command_failed" else "BGPLS_RPC_CHECK_INCOMPLETE",
                            "blocker" if error == "command_failed" else "unknown", "bgpls",
                            f"The live read-only {method} RPC did not complete successfully.",
                            "Review local GoBGP availability, the API contract and configured peers; no router changes were attempted.",
                            method=method, endpoint=data["endpoint"], result=error))
            break
        try:
            decoded = decode_json_stream(output.decode("utf-8"))
        except (ValueError, UnicodeError, RecursionError):
            findings.append(finding("BGPLS_RPC_RESPONSE_INVALID", "blocker", "bgpls", "Live RPC returned an invalid bounded JSON stream.",
                                    "Verify the qualified grpcurl and GoBGP API versions.", method=method))
            break
        findings.append(finding("BGPLS_RPC_RESPONDED", "ok", "bgpls", f"Live read-only {method} RPC responded.",
                                "A responding API alone does not establish a usable topology.", method=method, response_count=len(decoded)))
        if method == "ListPeer":
            try:
                peers_before = _peers(decoded, tuple(data["required_peers"]))
                findings.append(finding("BGPLS_LIVE_PEERS_SYNCHRONIZED", "ok", "bgpls", "Required peers are Established/Up with BGP-LS enabled and End-of-RIB received.",
                                        "Continue collection and require a consistent current graph before computing paths.", peer_count=len(peers_before)))
            except (ValueError, TypeError, AttributeError, KeyError, OverflowError, RecursionError) as exc:
                code = getattr(exc, "code", "BGPLS_INPUT_INVALID")
                if code not in {"BGPLS_SESSION_DOWN", "BGPLS_FAMILY_UNAVAILABLE", "BGPLS_EOR_INCOMPLETE", "BGPLS_PEER_MISSING", "BGPLS_SOURCE_RESTARTING", "BGPLS_PEER_IDENTITY_INVALID"}:
                    code = "BGPLS_LIVE_PEER_METADATA_INVALID"
                findings.append(finding(code, "blocker", "bgpls", "Live required-peer metadata failed its session, family or synchronization check.",
                                        "Review the exact required BGP-LS neighbor, address family, restart state and End-of-RIB.", method=method))
                break
    findings.append(finding("LIVE_TOPOLOGY_QUALIFICATION_NOT_RUN", "unknown", "topology", "Live RPC responses were not installed or substituted for the collector's consistent three-phase snapshot.",
                            "Use the collector's validated before/RIB/after observation and graph consistency checks."))
    return findings


def snapshot_report(snapshot: dict[str, Any], *, config: ControllerConfig | None = None,
                    live: bool = False, native: str | None = None, authenticated: bool | None = None,
                    runtime: dict[str, Any] | None = None, transaction: dict[str, Any] | None = None,
                    paths: list[Path] | None = None, services: tuple[str, ...] = (), include_host: bool = True,
                    source_addresses: list[str] | None = None) -> dict[str, Any]:
    mode = config.document.get("operating_mode", "pcep-control") if config is not None else snapshot.get("operating_mode", "observe")
    if snapshot.get("mode") == "demo":
        return report([finding("DIAGNOSTICS_DEMO_DATA", "unknown", "topology", "Synthetic demo data cannot establish deployment health.",
                               "Use a configured live workspace for diagnostic evidence.")], mode=mode)
    processes = snapshot.get("processes") if isinstance(snapshot.get("processes"), list) else None
    findings = protocol_findings(snapshot, mode, processes=processes)
    data = config.document if config is not None else None
    addresses = list(source_addresses or [])
    limitations = []
    if data is not None:
        assert config is not None
        findings.append(finding("CONFIGURATION_VALID", "ok", "persistence", "Selected controller configuration and static policy inputs validate.",
                                "Retain the selected revision and preserve operational journals during changes.", config_digest=config.config_digest))
        endpoint = data["bgpls"]["endpoint"]
        expected = "ninja-admin.service" if runtime is not None and native is not None else "ninja-gobgpd.service"
        findings.extend(_listener("127.0.0.1", int(endpoint.rsplit(":", 1)[1]), "GOBGP_API", expected))
        for component in ("dashboard", "pcep"):
            if component == "pcep" and mode == "observe" or component == "dashboard" and not data["dashboard"]["enabled"]:
                continue
            settings = data[component]
            addresses.append(settings["listen_host"])
            findings.extend(_listener(settings["listen_host"], settings["port"], component.upper(), "ninja-controller.service"))
            if ipaddress.ip_address(settings["listen_host"]).is_unspecified:
                findings.append(finding("LISTENER_WILDCARD_CONFIGURED", "warning", "security", "A configured listener uses a wildcard address.",
                                        "Review interface exposure and restrict incoming traffic to intended management clients or PCCs.", component=component, address=settings["listen_host"]))
        if paths is None:
            paths = [config.path.parent, Path(data["state_dir"]), Path(data["runtime_dir"]), Path(data["files"]["topology"]).parent]
        release = Path(native).absolute().parent.parent if native is not None else None
        findings.extend(artifact_findings(grpcurl=data["bgpls"]["grpcurl"], gobgpd=native if live else None,
                                           release_dir=release if live else None))
        if live:
            findings.extend(live_gobgp(config))
    elif live:
        findings.append(finding("LIVE_CONFIGURATION_REQUIRED", "unknown", "persistence", "Live checks require a valid selected controller configuration.",
                                "Save or repair a configuration before requesting live RPC checks."))
    if include_host:
        findings.extend(host_findings(paths=paths or [], addresses=addresses, live=live))
        findings.extend(service_findings(services, live=live))
    if authenticated is True:
        findings.append(finding("AUTHENTICATED_ADMIN_REQUEST", "ok", "security", "This diagnostic request passed the administration session boundary.",
                                "Keep management on loopback and use the documented SSH access path."))
    else:
        findings.append(finding("ADMIN_INTERFACE_AUTHENTICATION_UNKNOWN", "unknown", "security", "This scope does not establish authentication of a running administration interface.",
                                "Use the authenticated setup interface or verify the existing protected reverse proxy separately."))
    if runtime is not None:
        state = runtime.get("state", "unknown")
        findings.append(finding("ADMIN_RUNTIME_" + (state.upper() if state in {"running", "stopped", "failed", "uncertain", "starting"} else "UNKNOWN"),
                                "blocker" if state in {"failed", "uncertain"} else "warning" if state == "stopped" else "info" if state == "running" else "unknown",
                                "runtime", "Independent administration reports its owned observer runtime state.",
                                "Review original startup findings and reconcile uncertain ownership through the local CLI.", state=state))
        for name in ("controller", "gobgpd"):
            child = runtime.get(name, {})
            if isinstance(child, dict) and child.get("state") == "failed":
                findings.append(finding("ADMIN_CHILD_FAILED", "blocker", "runtime", "An owned observer child exited; administration remains available.",
                                        "Review the bounded child log locally. Repair the cause before a deliberate restart.", component=name, returncode=child.get("returncode")))
                log = child.get("log")
                if isinstance(log, str):
                    messages = {"GOBGP_PORT_IN_USE": "An occupied local port blocked managed GoBGP startup.",
                                "GOBGP_CONFIG_INVALID": "Native GoBGP configuration validation failed.",
                                "GOBGP_BINARY_INVALID": "The managed GoBGP binary could not be launched.",
                                "GOBGP_UNEXPECTED_EXIT": "The managed GoBGP native child exited unexpectedly.",
                                "BGPLS_ALREADY_COLLECTING": "Another collector owns the observation writer lock."}
                    for line in log[-16384:].splitlines()[-16:]:
                        try:
                            original = json.loads(line)
                        except (ValueError, RecursionError):
                            continue
                        if not isinstance(original, dict) or not isinstance(original.get("code"), str) or original["code"] not in messages:
                            continue
                        details: dict[str, Any] = {"component": name}
                        for key in ("port", "errno"):
                            if type(original.get(key)) is int and 0 <= original[key] <= 65535:
                                details[key] = original[key]
                        owners = original.get("owners")
                        if isinstance(owners, list):
                            details["owners"] = [{"pid": item["pid"]} for item in owners[:16] if isinstance(item, dict)
                                                 and type(item.get("pid")) is int and 0 < item["pid"] <= 2**31 - 1]
                        try:
                            address = original.get("address")
                            if isinstance(address, str):
                                details["address"] = str(ipaddress.ip_address(address))
                        except (ValueError, TypeError):
                            pass
                        findings.append(finding(original["code"], "blocker", "runtime", messages[original["code"]],
                                        "Review the detected owner, native validation and installed dependency; repair through normal managed controls.", **details))
    if transaction and transaction.get("status") in {"APPLYING", "UNCERTAIN", "FAILED", "ROLLED_BACK"}:
        state = transaction["status"]
        findings.append(finding("ADMIN_TRANSACTION_" + state, "blocker" if state in {"UNCERTAIN", "FAILED"} else "warning", "persistence",
                                "Administration recorded this configuration transaction outcome.",
                                "Reconcile interrupted ownership locally; preserve all journals and review the prior and target revisions.", state=state))
    limitations.append("Listener inspection is a Linux metadata snapshot, not a reservation or authorization to stop a PID.")
    if live:
        limitations.append("Live RPCs contact only the configured loopback GoBGP API. PCC transport and forwarding are not actively probed.")
    return report(findings, mode=mode, live=live, limitations=limitations)


def config_report(path: str | Path, *, live: bool = False, native: str | None = None) -> dict[str, Any]:
    try:
        config = load_controller_config(path)
        snapshot = DashboardStore(_dashboard_config(config)).snapshot()
        snapshot["processes"] = [_runtime_status(config, item) for item in _components(config, None)]
    except (OSError, ValueError, TypeError, RecursionError):
        return report([finding("CONFIGURATION_INVALID", "blocker", "persistence", "The selected controller configuration could not be validated.",
                               "Run controller validate locally, repair the static input and preserve all journals. No network probe was attempted.")])
    try:
        return snapshot_report(snapshot, config=config, live=live, native=native,
                               services=("ninja-controller.service", "ninja-gobgpd.service"))
    except (OSError, ValueError, TypeError, RecursionError):
        return report([finding("DIAGNOSTICS_CHECK_INCOMPLETE", "unknown", "runtime", "Diagnostic evidence could not be fully inspected.",
                               "Review local permissions and retry. Some requested read-only checks may have completed.")],
                      mode=effective_operating_mode(config), live=live)


def admin_report(store: Any, *, live: bool = False, authenticated: bool = False) -> dict[str, Any]:
    config = None
    try:
        if not store._mutex.acquire(blocking=False):
            return report([finding("DIAGNOSTICS_CONFIGURATION_BUSY", "unknown", "persistence", "Configuration is changing; diagnostic evidence was not sampled.",
                                   "Refresh after the current administration transaction completes.")])
        try:
            with store.locked():
                revision = store.current_revision()
                source_addresses = []
                if revision is not None:
                    store._revision_files(revision)
                    config = load_controller_config(store.root / "revisions" / revision / "controller.json")
                    source_addresses = [store.configuration()["setup"]["controller"]["source_address"]]
                snapshot = store.snapshot()
                status = store.status()
                if config is not None:
                    snapshot["processes"] = [_runtime_status(config, "bgpls")]
        finally:
            store._mutex.release()
        result = snapshot_report(snapshot, config=config, live=live, native=store.gobgpd, authenticated=authenticated,
                                 runtime=status.get("runtime"), transaction=status.get("transaction"),
                                 paths=[store.admin, store.root / "revisions", store.root / "data/state", store.root / "data/run"],
                                 services=("ninja-admin.service",), source_addresses=source_addresses)
        if revision is not None:
            result = report([*result["findings"], finding("ADMIN_REVISION_SNAPSHOT_VALID", "ok", "persistence",
                            "The selected generated revision and its private inventory validate.",
                            "Retain immutable revisions and preserve separate operational journals.", revision=revision)],
                            mode="observe", live=live, limitations=result["limitations"])
        if store.current_revision() != revision:
            result = report([finding("DIAGNOSTIC_REVISION_CHANGED", "unknown", "persistence", "Configuration changed during diagnostics; the report was discarded.",
                                     "Refresh against the new revision.")], live=False)
        return result
    except (OSError, ValueError, TypeError, RecursionError):
        return report([finding("ADMIN_CONFIGURATION_UNAVAILABLE", "blocker", "persistence", "Administration state could not be inspected safely.",
                               "Review private state permissions, inventory and revision consistency locally; management liveness remains separate.")])


@dataclass
class DiagnosticService:
    """One live request at a time; rapid retries do not spawn more subprocesses."""

    lock: Any = field(default_factory=threading.Lock)
    last_started: float = -math.inf

    def run(self, store: Any, *, live: bool, authenticated: bool = True) -> dict[str, Any]:
        if not live:
            return admin_report(store, authenticated=authenticated)
        if not self.lock.acquire(blocking=False):
            return report([finding("DIAGNOSTICS_BUSY", "unknown", "runtime", "A live diagnostic request is already in progress.",
                                   "Wait for the current read-only check before retrying.")])
        try:
            if time.monotonic() - self.last_started < 5:
                return report([finding("DIAGNOSTICS_RATE_LIMITED", "unknown", "runtime", "Live checks are limited to one start per five seconds.",
                                       "Wait before requesting another live check.")])
            self.last_started = time.monotonic()
            return admin_report(store, live=True, authenticated=authenticated)
        finally:
            self.lock.release()


def add_diagnostics_commands(subparsers: Any) -> None:
    command = subparsers.add_parser("diagnostics", help="Read structured health findings and manual repair guidance")
    source = command.add_mutually_exclusive_group(required=True)
    source.add_argument("--config", help="Existing controller JSON configuration")
    source.add_argument("--directory", help="Existing authenticated observation workspace (read-only)")
    command.add_argument("--live", action="store_true", help="Explicitly run bounded read-only local RPC and system metadata probes")
    command.add_argument("--gobgpd", help="Trusted native binary path for optional release hash inspection")
    command.add_argument("--format", choices=("json", "text"), default="json")


def diagnostics_command(args: argparse.Namespace) -> int:
    result = config_report(args.config, live=args.live, native=args.gobgpd) if args.config else directory_report(args.directory, live=args.live, native=args.gobgpd)
    if args.format == "json":
        print(json.dumps(result, sort_keys=True, allow_nan=False))
    else:
        print(f"Diagnostics: {result['overall']} ({result['scope']}); generated {result['generated_at']}")
        for item in result["findings"]:
            print(f"[{item['severity'].upper()}] {item['code']}: {item['message']}\n  Next step: {item['remediation']}")
        for limitation in result["limitations"]:
            print(f"Scope: {limitation}")
    return 0 if result["overall"] == "ok" else 1


def directory_report(directory: str | Path, *, live: bool = False, native: str | None = None) -> dict[str, Any]:
    """Inspect an active admin workspace without acquiring/creating ownership locks."""
    from .admin_model import render_setup, validate_setup
    from .admin_store import private_directory, read_private, strict_json

    root = Path(os.path.abspath(Path(directory).expanduser()))
    try:
        for relative in (".", "admin", "revisions", "data/state", "data/run"):
            private_directory(root / relative)
        if read_private(root / "admin/workspace.json").get("kind") != "ninja_admin_workspace":
            raise ValueError("Not an administration workspace")
        active = root / "admin/active.json"
        if not active.exists() and not active.is_symlink():
            revision = None
        else:
            current = read_private(active)
            if current.get("kind") != "admin_active_revision":
                raise ValueError("Invalid active revision")
            revision = current.get("revision")
        if revision is None:
            return report([finding("ADMIN_SETUP_REQUIRED", "warning", "persistence", "The private administration workspace has no saved configuration.",
                                   "Sign in to guided setup, replace example identities and review a generated revision.")])
        if not isinstance(revision, str) or not re.fullmatch(r"[a-f0-9]{64}", revision):
            raise ValueError("Invalid revision")
        stage = root / "revisions" / revision
        private_directory(stage)
        manifest = read_private(stage / "manifest.json")
        payload = read_private(stage / "setup.json", checksummed=False)
        raw_config = read_private(stage / "controller.json", checksummed=False)
        grpcurl = raw_config.get("bgpls", {}).get("grpcurl")
        if not isinstance(grpcurl, str):
            raise ValueError("Invalid trusted client")
        expected = render_setup(validate_setup(payload), root, stage, grpcurl=grpcurl)
        if (manifest.get("kind") != "admin_revision" or manifest.get("revision") != revision or
                set(manifest.get("files", {})) != set(expected) or
                set(item.name for item in stage.iterdir()) != {*expected, "manifest.json"}):
            raise ValueError("Invalid inventory")
        for name, contents in expected.items():
            actual = read_private(stage / name, checksummed=False) if name.endswith(".json") else None
            if name.endswith(".json") and actual != strict_json(contents):
                raise ValueError("Modified generated JSON")
            # The TOML inventory is checked as bytes without executing a daemon.
            descriptor = os.open(stage / name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(descriptor, "rb") as stream:
                metadata = os.fstat(stream.fileno())
                if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) != 0o600 or metadata.st_nlink != 1:
                    raise ValueError("Unsafe revision payload")
                observed = stream.read(1048577)
            if observed != contents or manifest["files"][name] != hashlib.sha256(observed).hexdigest():
                raise ValueError("Modified revision payload")
        transaction_path = root / "admin/transaction.json"
        transaction = read_private(transaction_path) if transaction_path.exists() or transaction_path.is_symlink() else {}
        result = config_report(stage / "controller.json", live=live, native=native)
        result = report([*result["findings"], finding("ADMIN_REVISION_SNAPSHOT_VALID", "ok", "persistence",
                        "The selected generated revision and its private inventory validate.",
                        "Retain immutable revisions and preserve separate operational journals.", revision=revision)],
                        mode="observe", live=live, limitations=result["limitations"])
        if transaction.get("status") in {"APPLYING", "UNCERTAIN"}:
            extra = finding("ADMIN_RECONCILIATION_REQUIRED", "blocker", "persistence", "The administration transaction requires ownership reconciliation.",
                            "Stop management and reconcile locally after confirming all intended writers stopped.")
            result = report([*result["findings"], extra], mode="observe", live=live, limitations=result["limitations"])
        if read_private(active).get("revision") != revision:
            return report([finding("DIAGNOSTIC_REVISION_CHANGED", "unknown", "persistence", "Configuration changed during diagnostics; the report was discarded.",
                                   "Refresh against the new revision.")])
        return result
    except (OSError, ValueError, TypeError, AttributeError, RecursionError):
        return report([finding("ADMIN_WORKSPACE_INVALID", "blocker", "persistence", "The private workspace or revision could not be safely validated.",
                               "Inspect workspace permissions and the revision inventory locally; do not replace operational journals.")])
