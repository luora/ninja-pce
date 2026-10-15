"""Review and apply a journal-only migration without starting any controller.

Run this module's ``plan`` and ``apply`` commands during a provisioning/recovery
maintenance window. Plans and backups contain private deployment paths and
policy documents; keep them outside source control. No transport is contacted.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import stat
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .cli_provisioning import CliProvisioner, _runtime_computer
from .loaders import _load_mapping, load_inventory
from .pcep_compute import PcepPathComputer
from .provisioning_policy import TransportEnrollmentRegistry
from .reservations import resource_lock
from .state import digest, load_state, write_state
from .tunnel_api import TunnelAPI

_KIND = "cli_provisioning_policy_migration"
_MAX_BYTES = 16_777_216


def _bytes(path: Path) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_BYTES:
            raise ValueError("Migration inputs must be existing bounded regular files")
        result = handle.read(_MAX_BYTES + 1)
    if len(result) > _MAX_BYTES:
        raise ValueError("Migration input exceeds 16 MiB")
    return result


def _hash(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def legacy_policy_document(config: dict[str, Any], *, config_path: Path | None = None) -> dict[str, Any]:
    """Reconstruct exactly the pre-domain provisioning policy, including area identity."""
    if "domain_policy" in config or config.get("max_outstanding", 1) != 1:
        raise ValueError("Legacy migration source must retain its original single-operation policy")
    inventory = load_inventory(config["inventory"])
    headends = config["headends"]
    destinations = config["destinations"]
    if (not isinstance(headends, list) or not 1 <= len(headends) <= 16 or len(set(headends)) != len(headends) or
            not isinstance(destinations, list) or not destinations or len(set(destinations)) != len(destinations)):
        raise ValueError("Migration requires the original distinct headends and destinations")
    devices = {name: inventory.device_for_node(name) for name in headends}
    policy = {"devices": {name: asdict(device) for name, device in devices.items()},
              "destinations": destinations, "max_bandwidth_bps": config.get("max_bandwidth_bps", 1_000_000),
              "pce_address": config["pce_address"], "max_outstanding": 1,
              "qualified_versions": ["16.09.07", "16.9.7"]}
    computer = _runtime_computer(config, config_path=config_path)
    if computer.ospf_multi_area is not None:
        policy["ospf_multi_area_digest"] = computer.ospf_multi_area.digest
    return policy


def _reader(config: dict[str, Any], config_path: Path, policy: dict[str, Any]) -> CliProvisioner:
    # Bypass __init__: it persists restart transitions. _load is the same
    # journal/hold validator used by the service and performs no writes.
    reader = object.__new__(CliProvisioner)
    reader.state_dir = Path(config["state_dir"]).resolve()
    reader.path = reader.state_dir / "cli-provisioning.json"
    reader.domain_policy = None
    reader.transport_registry = None
    reader.computer = _runtime_computer(config, config_path=config_path)
    reader.policy_document, reader.policy_digest = policy, digest(policy)
    reader.max_journal_bytes = 1_048_576
    return reader


def _inputs(path: Path, config: dict[str, Any]) -> set[Path]:
    paths = {path}
    paths.update(Path(config[key]) for key in ("inventory", "known_hosts", "domain_policy") if key in config)
    if "ospf_multi_area_policy" in config["computer"]:
        paths.add(Path(config["computer"]["ospf_multi_area_policy"]))
    return paths


def _target_policy(config: dict[str, Any], computer: PcepPathComputer, reader: CliProvisioner) -> dict[str, Any]:
    """Validate service construction policy without starting it or reading credentials."""
    bandwidth = config.get("max_bandwidth_bps", 1_000_000)
    if type(config.get("qualified", False)) is not bool or type(bandwidth) is not int or not 0 < bandwidth <= 10**9:
        raise ValueError("Target qualification and bandwidth policy are invalid")
    limits = {"max_outstanding": config.get("max_outstanding", 1), "tunnel_id_min": config.get("tunnel_id_min", 1000),
              "tunnel_id_max": config.get("tunnel_id_max", 1999), "max_operations": config.get("max_operations", 4096),
              "max_previews": config.get("max_previews", 256),
              "max_journal_bytes": config.get("max_journal_bytes", 16_777_216)}
    for field, minimum, maximum in (("max_outstanding", 1, 256), ("max_operations", 1, 4096),
                                     ("max_previews", 1, 256), ("max_journal_bytes", 1024, _MAX_BYTES),
                                     ("tunnel_id_min", 1, 65535)):
        if type(limits[field]) is not int or not minimum <= limits[field] <= maximum:
            raise ValueError("Target bounded provisioning limits are invalid")
    if (type(limits["tunnel_id_max"]) is not int or
            not limits["tunnel_id_min"] <= limits["tunnel_id_max"] <= 65535 or
            limits["max_outstanding"] > limits["max_operations"]):
        raise ValueError("Target operation limits or tunnel namespace are invalid")
    inventory = load_inventory(config["inventory"]) if "inventory" in config else None
    devices = {device.topology_node_id: device for device in inventory.devices.values()} if inventory else {}
    known_hosts = Path(config["known_hosts"]) if "known_hosts" in config else None
    if devices and known_hosts is None:
        raise ValueError("Target enrolled transports require an independently pinned host-key file")
    if known_hosts is not None:
        _bytes(known_hosts)
    registry = TransportEnrollmentRegistry(devices, known_hosts)
    # Construction validates proxy identity, exact HTTPS origin, principal
    # policy and absolute socket path. Only start() creates a socket or session.
    TunnelAPI(reader, Path(config["socket_path"]), config["proxy_uid"], config["origin"],
              qualified=config.get("qualified", False), principal_policy="authenticated")
    assert computer.domain_policy is not None
    policy = {"devices": {name: asdict(device) for name, device in devices.items()}, "destinations": (),
              "max_bandwidth_bps": bandwidth, "pce_address": config["pce_address"],
              "qualified_versions": ["16.09.07", "16.9.7"], **limits,
              "domain_policy_digest": computer.domain_policy.policy_digest,
              "domain_policy": computer.domain_policy.document(),
              "transport_enrollment": registry.snapshot()}
    if computer.ospf_multi_area is not None:
        policy["ospf_multi_area_digest"] = computer.ospf_multi_area.digest
    return policy


def _context(legacy_path: Path, config_path: Path) -> tuple[CliProvisioner, dict[str, Any], set[Path]]:
    legacy, current = _load_mapping(legacy_path), _load_mapping(config_path)
    if Path(legacy["state_dir"]).resolve() != Path(current["state_dir"]).resolve():
        raise ValueError("Migration must preserve the existing provisioning state directory")
    target = _runtime_computer(current, config_path=config_path)
    if (target.domain_policy is None or "headends" in current or "destinations" in current or
            current.get("principal_policy", "authenticated") != "authenticated" or current.get("operators")):
        raise ValueError("Target configuration must use discovered domain members and authenticated principals")
    policy = legacy_policy_document(legacy, config_path=legacy_path)
    reader = _reader(legacy, legacy_path, policy)
    target_policy = _target_policy(current, target, reader)
    return reader, target_policy, _inputs(legacy_path, legacy) | _inputs(config_path, current)


def _migrated(journal: dict[str, Any], policy: dict[str, Any]) -> dict[str, Any]:
    if "provisioning_schema" in journal or "policies" in journal:
        raise ValueError("Only an unmigrated legacy journal may be migrated")
    revision = digest(policy)
    if journal.get("policy_digest") != revision:
        raise ValueError("Original provisioning policy does not match the retained journal")
    result = copy.deepcopy(journal)
    result.update(provisioning_schema=2, policies={revision: copy.deepcopy(policy)}, previews={})
    for operation in result["operations"].values():
        if "policy_revision" in operation:
            raise ValueError("Legacy operation already has an unexpected policy revision")
        operation["policy_revision"] = revision
    return result


def plan_migration(legacy_config: Path, config: Path, *, preserve: tuple[Path, ...] = ()) -> dict[str, Any]:
    """Return a reviewable checksummed-plan payload without changing live state."""
    legacy_path, config_path = Path(legacy_config).absolute(), Path(config).absolute()
    reader, target_policy, inputs = _context(legacy_path, config_path)
    lock = reader.state_dir / ".ninja-pce-resources.lock"
    _bytes(lock)  # Missing deployments must never be initialized by migration.
    preserved = tuple(Path(path).absolute() for path in preserve)
    if len(preserved) > 32 or len(set(preserved)) != len(preserved):
        raise ValueError("Migration requires at most 32 distinct preserved recovery files")
    journal_path, ledger_path = reader.path, reader.state_dir / ".ninja-pce-reservations.json"
    protected = {journal_path, ledger_path, *preserved}
    if len(protected) != 2 + len(preserved):
        raise ValueError("Preserved recovery files must be distinct from provisioning journal and ledger")
    identities = {(path.stat().st_dev, path.stat().st_ino) for path in protected}
    if len(identities) != len(protected):
        raise ValueError("Preserved files must not alias the journal, ledger or each other")
    paths = protected | inputs
    with resource_lock(reader.state_dir):
        journal, _ = reader._load()
        migrated = _migrated(journal, reader.policy_document)
        if len(migrated["operations"]) > target_policy["max_operations"]:
            raise ValueError("Target history bound cannot retain every original operation")
        on_start = copy.deepcopy(migrated)
        on_start["policies"][digest(target_policy)] = target_policy
        on_start["policy_digest"] = digest(target_policy)
        if len(json.dumps(on_start, indent=2, sort_keys=True, allow_nan=False).encode()) + 128 > target_policy[
                "max_journal_bytes"]:
            raise ValueError("Target journal bound cannot retain original state and both policy revisions")
        for path in protected:
            _bytes(path)
            load_state(path)
        snapshots = {}
        for path in sorted(paths):
            raw = _bytes(path)
            snapshots[str(path.absolute())] = {"sha256": _hash(raw), "size": len(raw)}
    return {"kind": _KIND, "migration_version": 1, "created_at": datetime.now(timezone.utc).isoformat(),
            "legacy_config": str(legacy_path), "config": str(config_path), "journal": str(journal_path),
            "ledger": str(ledger_path), "preserve": [str(path) for path in preserved], "files": snapshots,
            "source_journal_digest": digest(journal), "result_journal_digest": digest(migrated),
            "original_policy_digest": reader.policy_digest,
            "operation_count": len(journal["operations"]), "invalidated_preview_count": len(journal["previews"]),
            "changes": ["Add provisioning_schema=2 and the complete original immutable policy document",
                        "Add only policy_revision to each existing operation; retain original authorization and status",
                        "Invalidate outstanding previews; fresh preview and approval are required",
                        "Keep the reservation ledger and explicitly preserved recovery files byte-for-byte"]}


def _validate_files(plan: dict[str, Any]) -> dict[Path, bytes]:
    records = plan.get("files")
    if not isinstance(records, dict) or not 4 <= len(records) <= 64:
        raise ValueError("Migration plan has invalid bounded input bindings")
    files = {}
    for name, record in records.items():
        path = Path(name)
        if not path.is_absolute() or not isinstance(record, dict):
            raise ValueError("Migration plan requires absolute input paths and exact file hashes")
        raw = _bytes(path)
        if record != {"sha256": _hash(raw), "size": len(raw)}:
            raise ValueError("Migration input changed after review; prepare a new plan")
        files[path] = raw
    return files


def apply_migration(plan: dict[str, Any], *, backup_dir: Path) -> dict[str, Any]:
    """Archive exact originals before one atomic journal write; retain all holds/history."""
    if plan.get("kind") != _KIND or plan.get("migration_version") != 1:
        raise ValueError("Unsupported provisioning migration plan")
    reader, _, _ = _context(Path(plan["legacy_config"]), Path(plan["config"]))
    if str(reader.path) != plan["journal"] or str(reader.state_dir / ".ninja-pce-reservations.json") != plan["ledger"]:
        raise ValueError("Migration plan no longer identifies the original state")
    destination = Path(backup_dir).resolve()
    if destination.is_relative_to(reader.state_dir) or destination.exists():
        raise ValueError("Migration backup directory must be new and outside mutable provisioning state")
    _bytes(reader.state_dir / ".ninja-pce-resources.lock")
    expected = plan_migration(Path(plan["legacy_config"]), Path(plan["config"]),
                              preserve=tuple(Path(path) for path in plan["preserve"]))
    if any(plan.get(key) != value for key, value in expected.items() if key != "created_at"):
        raise ValueError("Migration plan differs from the validated exact transformation")
    with resource_lock(reader.state_dir):
        files = _validate_files(plan)
        journal, _ = reader._load()
        migrated = _migrated(journal, reader.policy_document)
        destination.mkdir(mode=0o700)
        archived = {}
        for index, (path, raw) in enumerate(sorted(files.items())):
            archive = destination / f"{index:02d}-{path.name}"
            descriptor = os.open(archive, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            archived[str(path)] = {"archive": archive.name, "sha256": _hash(raw)}
        write_state(destination / "manifest.json", {"kind": _KIND, "plan_digest": digest(plan), "files": archived})
        _validate_files(plan)
        write_state(reader.path, migrated)
        unchanged = {path for path in files if path != reader.path}
        if any(_bytes(path) != files[path] for path in unchanged):
            raise ValueError("Preserved state changed concurrently; retain backups and inspect the maintenance window")
    return {"migrated": True, "operation_count": plan["operation_count"],
            "invalidated_preview_count": plan["invalidated_preview_count"], "backup_dir": str(destination)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    planning = commands.add_parser("plan")
    planning.add_argument("--legacy-config", required=True, type=Path)
    planning.add_argument("--config", required=True, type=Path)
    planning.add_argument("--plan", required=True, type=Path)
    planning.add_argument("--preserve", action="append", default=[], type=Path)
    applying = commands.add_parser("apply")
    applying.add_argument("--plan", required=True, type=Path)
    applying.add_argument("--backup-dir", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "plan":
            plan = plan_migration(args.legacy_config, args.config, preserve=tuple(args.preserve))
            if (args.plan.exists() or args.plan.is_symlink() or
                    args.plan.resolve().is_relative_to(Path(plan["journal"]).parent) or
                    str(args.plan.absolute()) in plan["files"]):
                raise ValueError("Migration plan output must be new and distinct from every retained input")
            write_state(args.plan, plan)
            print(f"Prepared migration for {plan['operation_count']} operations; "
                  f"invalidates {plan['invalidated_preview_count']} previews.")
        else:
            _bytes(args.plan)
            result = apply_migration(load_state(args.plan), backup_dir=args.backup_dir)
            print(f"Migrated {result['operation_count']} operations; exact originals archived.")
        return 0
    except (ValueError, OSError, KeyError, TypeError) as exc:
        print(f"Migration refused: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
