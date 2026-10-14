"""Review and authorize one exact managed-tunnel PCEP reinstatement locally."""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Sequence, TypeGuard

from .cli_provisioning import _runtime_computer
from .loaders import _load_mapping
from .pcep_managed_failover import ManagedFailureRecoveryPlanner
from .state import digest, load_state, write_state


def _regular(path: Path, maximum: int = 1_048_576) -> None:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > maximum:
        raise ValueError("Reinstatement inputs must be existing bounded regular files")


def _finite(value: Any) -> TypeGuard[int | float]:
    return bool(not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value))


def _distinct(paths: list[Path]) -> None:
    for index, path in enumerate(paths):
        for other in paths[:index]:
            if path.resolve() == other.resolve() or (
                    path.exists() and other.exists() and os.path.samefile(path, other)):
                raise ValueError("Reinstatement artifacts must be distinct from each other and runtime files")


def _create_private_state(path: Path, payload: dict[str, Any]) -> None:
    """Publish a complete mode0600 document without replacing any existing path."""
    if path.exists() or path.is_symlink():
        raise ValueError("Reinstatement artifact already exists; inspect it before continuing")
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".ninja-reinstatement-", dir=path.parent)
    os.close(descriptor)
    staged = Path(temporary)
    try:
        write_state(staged, payload)
        # A same-filesystem hard link publishes the fsynced document atomically
        # and refuses a competing creator, including a dangling symlink.
        os.link(staged, path, follow_symlinks=False)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        staged.unlink(missing_ok=True)


def _live_report(capture_path: Path, planner: ManagedFailureRecoveryPlanner, now: float) -> dict[str, Any]:
    _regular(capture_path, 4_194_304)
    capture = load_state(capture_path)
    generated = capture.get("generated_at")
    if (capture.get("kind") != "passive_pcep_capture" or capture.get("listener_running") is not True or
            capture.get("capture_error") is not None or not _finite(generated) or not 0 <= now - generated <= 5):
        raise ValueError("Reinstatement requires a healthy listener capture no more than five seconds old")
    capabilities = capture.get("capabilities", {})
    if (not isinstance(capabilities, dict) or capabilities.get("stateful") is not True or
            capabilities.get("lsp_update") is not True or capabilities.get("path_computation") is not True):
        raise ValueError("Reinstatement requires the configured stateful PCEP computation listener")
    peers = capture.get("peers")
    peer = peers.get(planner.peer) if isinstance(peers, dict) else None
    if (not isinstance(peer, dict) or peer.get("state") != "ESTABLISHED" or peer.get("fresh") is not True or
            peer.get("synchronized") is not True or not _finite(peer.get("valid_until")) or
            peer["valid_until"] <= now):
        raise ValueError("Reinstatement requires the exact fresh synchronized PCC session")
    reports = peer.get("reports")
    matching = [item for item in reports.values() if isinstance(item, dict) and planner.matches(planner.peer, item)] \
        if isinstance(reports, dict) else []
    if not matching or any(not _finite(item.get("received_at")) for item in matching):
        raise ValueError("Reinstatement requires a current report for the exact owned tunnel")
    report = max(matching, key=lambda item: item["received_at"])
    if (report.get("removed") is not False or report.get("stale") is not False or report.get("sync") is not False or
            report.get("session_id") != peer.get("session_id") or report["received_at"] > now + 1):
        raise ValueError("The latest owned tunnel report is historical, removed, or from another session")
    return report


def _planner(args: argparse.Namespace) -> ManagedFailureRecoveryPlanner:
    _regular(args.config)
    if args.config.suffix.lower() != ".json":
        raise ValueError("Reinstatement runtime policy must be an explicit JSON file")
    config = _load_mapping(args.config)
    computer = _runtime_computer(config, config_path=args.config)
    operation_journal = Path(config["state_dir"]) / "cli-provisioning.json"
    if args.capture.resolve() != Path(config["pcep_capture"]).resolve():
        raise ValueError("Reinstatement capture must match the private runtime policy")
    protected = [args.config, args.recovery_state, args.capture, operation_journal,
                 operation_journal.parent / ".ninja-pce-reservations.json",
                 operation_journal.parent / ".ninja-pce-resources.lock",
                 computer.raw_path, computer.status_path, computer.topology_path]
    protected.extend(Path(config[key]) for key in ("inventory", "known_hosts", "socket_path") if key in config)
    policy = config["computer"].get("ospf_multi_area_policy")
    if policy is not None:
        protected.append(Path(policy))
    _distinct([*protected, args.request, args.plan])
    for path in (operation_journal, args.recovery_state,
                 operation_journal.parent / ".ninja-pce-reservations.json",
                 operation_journal.parent / ".ninja-pce-resources.lock"):
        _regular(path)
    recovery = load_state(args.recovery_state)
    if not isinstance(recovery.get("managed_authority"), dict):
        raise ValueError("Reinstatement requires an existing owned managed-recovery journal")
    if args.request.exists() or args.request.is_symlink():
        raise ValueError("Reinstatement request already exists; inspect its disposition before continuing")
    return ManagedFailureRecoveryPlanner(
        computer, operation_journal=operation_journal, operation_id=args.operation,
        journal_path=args.recovery_state, reinstatement_path=args.request, clock=time.time,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    phases = parser.add_subparsers(dest="phase", required=True)
    for phase in ("plan", "authorize"):
        command = phases.add_parser(phase)
        command.add_argument("--config", required=True, type=Path, help="Private provisioning runtime JSON")
        command.add_argument("--operation", required=True, help="Exact VERIFIED provisioning operation ID")
        command.add_argument("--recovery-state", required=True, type=Path, help="Existing managed recovery journal")
        command.add_argument("--capture", required=True, type=Path, help="Current PCEP listener capture")
        command.add_argument("--request", required=True, type=Path, help="Exact request path configured in the listener")
        command.add_argument("--plan", required=True, type=Path, help="Private review artifact")
        if phase == "plan":
            command.add_argument("--ttl-seconds", type=float, default=60,
                                 help="Bounded review interval, in seconds (default60)")
    args = parser.parse_args(argv)
    try:
        if os.environ.get("NINJA_PCE_PREVIEW_ONLY", "").strip().lower() in {"1", "true", "yes", "on"}:
            raise ValueError("Preview-only mode forbids reinstatement authorization")
        planner = _planner(args)
        report = _live_report(args.capture, planner, time.time())
        if args.phase == "plan":
            payload = planner.reinstatement_plan(planner.peer, report, ttl_seconds=args.ttl_seconds)
            target = args.plan
        else:
            _regular(args.plan, 65_536)
            reviewed = load_state(args.plan)
            if reviewed.get("kind") != "managed_pcep_reinstatement":
                raise ValueError("The review artifact is not a managed-tunnel reinstatement plan")
            created, expiry = reviewed.get("created_at"), reviewed.get("expires_at")
            if not _finite(created) or not _finite(expiry) or not created <= time.time() < expiry:
                raise ValueError("The reinstatement review interval has expired or the clock changed")
            payload = planner.reinstatement_plan(
                planner.peer, report, ttl_seconds=expiry - created,
                request_id=reviewed.get("request_id"), created_at=created,
            )
            if digest(payload) != digest(reviewed):
                raise ValueError("Reinstatement authority or path changed; create and review a new plan")
            target = args.request
        _create_private_state(target, payload)
        print(json.dumps({"status": "PLANNED" if args.phase == "plan" else "AUTHORIZED",
                          "artifact": str(target), "request_id": payload["request_id"],
                          "expires_at": payload["expires_at"]}, sort_keys=True))
        return 0
    except (ValueError, OSError, KeyError, TypeError, OverflowError) as exc:
        print(f"Reinstatement refused: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
