"""Review, archive and migrate synthetic durable state without router writes."""
from __future__ import annotations

import asyncio
import copy
import json
import os
from dataclasses import asdict

import pytest
from test_cli_provisioning import INTENT, controller, mocks
from test_pcep_domain_integration import policy_document

from ninja_pce import provisioning_migration
from ninja_pce.provisioning_migration import apply_migration, legacy_policy_document, main, plan_migration
from ninja_pce.state import digest, load_state, write_state


def deployment(tmp_path, monkeypatch):
    mocks(monkeypatch)
    provisioner = controller(tmp_path, qualified=True)

    async def create():
        preview = await provisioner.preview("operator", dict(INTENT))
        operation = await provisioner.create("operator", "retained-key", dict(INTENT),
                                             approved_plan_id=preview["plan_id"])
        await provisioner.close()
        return provisioner.get(operation["id"])

    operation = asyncio.run(create())
    assert operation["status"] == "VERIFIED"
    inventory = tmp_path / "inventory.json"
    inventory.write_text(json.dumps({"devices": [asdict(device) for device in provisioner.devices.values()]}))
    legacy = {"inventory": str(inventory), "state_dir": str(provisioner.state_dir),
              "headends": ["PE1"], "destinations": ["PE3"], "pce_address": provisioner.pce_address,
              "max_bandwidth_bps": provisioner.max_bandwidth_bps,
              "computer": {"raw_path": str(tmp_path / "raw.json"), "status_path": str(tmp_path / "status.json"),
                           "topology_path": str(tmp_path / "topology.json"), "required_peers": ["192.0.2.1"],
                           "node_names": provisioner.computer.node_names,
                           "headends": provisioner.computer.headends}}
    domain_path = tmp_path / "domain.json"
    domain_path.write_text(json.dumps(policy_document()))
    known_hosts = tmp_path / "known_hosts"
    known_hosts.write_text("")  # An unavailable pin remains a visible transport readiness refusal.
    current = {**legacy, "domain_policy": str(domain_path), "principal_policy": "authenticated", "max_outstanding": 8,
               "known_hosts": str(known_hosts), "origin": "https://controller.example.test", "proxy_uid": 33,
               "socket_path": str(tmp_path / "runtime" / "provisioning.sock")}
    del current["headends"], current["destinations"]
    legacy_path, current_path = tmp_path / "legacy.json", tmp_path / "current.json"
    legacy_path.write_text(json.dumps(legacy))
    current_path.write_text(json.dumps(current))
    recovery = tmp_path / "recovery.json"
    write_state(recovery, {"kind": "synthetic_preserved_recovery", "attempts": 2,
                           "startup_recovery": {"total_attempts": 9, "records": ["retained"]}})
    journal = load_state(provisioner.path)
    journal["previews"]["outstanding-preview"] = {"principal": "another-user"}
    write_state(provisioner.path, journal)
    return provisioner, operation, legacy_path, current_path, recovery


def test_plan_is_readonly_and_exact_migration_keeps_authority_holds_and_history(tmp_path, monkeypatch):
    provisioner, operation, legacy, current, recovery = deployment(tmp_path, monkeypatch)
    original_paths = [provisioner.path, provisioner.state_dir / ".ninja-pce-reservations.json", recovery]
    originals = {path: path.read_bytes() for path in original_paths}
    plan = plan_migration(legacy, current, preserve=(recovery,))
    assert all(path.read_bytes() == content for path, content in originals.items())
    assert plan["operation_count"] == 1 and plan["invalidated_preview_count"] == 2
    assert plan["original_policy_digest"] == provisioner.policy_digest
    assert digest(legacy_policy_document(json.loads(legacy.read_text()), config_path=legacy)) == provisioner.policy_digest
    backup_dir = tmp_path / "backup"
    result = apply_migration(plan, backup_dir=backup_dir)
    assert result["migrated"]
    journal = load_state(provisioner.path)
    assert journal["provisioning_schema"] == 2 and journal["previews"] == {}
    assert journal["operations"][operation["id"]] == {**operation, "policy_revision": provisioner.policy_digest}
    assert journal["policies"][provisioner.policy_digest] == json.loads(json.dumps(provisioner.policy_document))
    assert all(path.read_bytes() == content for path, content in originals.items() if path != provisioner.path)
    manifest = load_state(backup_dir / "manifest.json")
    for path, content in originals.items():
        record = manifest["files"][str(path)]
        assert (backup_dir / record["archive"]).read_bytes() == content
    assert backup_dir.stat().st_mode & 0o777 == 0o700
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in backup_dir.iterdir())


@pytest.mark.parametrize("changed", ["journal", "ledger", "history", "legacy", "current", "inventory", "domain"])
def test_apply_refuses_any_change_since_review_before_creating_backup(tmp_path, monkeypatch, changed):
    provisioner, _, legacy, current, recovery = deployment(tmp_path, monkeypatch)
    plan = plan_migration(legacy, current, preserve=(recovery,))
    paths = {"journal": provisioner.path, "ledger": provisioner.state_dir / ".ninja-pce-reservations.json",
             "history": recovery, "legacy": legacy, "current": current,
             "inventory": tmp_path / "inventory.json", "domain": tmp_path / "domain.json"}
    with paths[changed].open("ab") as handle:
        handle.write(b"\n")
    journal_before = provisioner.path.read_bytes()
    backup = tmp_path / "backup"
    with pytest.raises(ValueError, match="plan differs|input changed"):
        apply_migration(plan, backup_dir=backup)
    assert provisioner.path.read_bytes() == journal_before
    assert not backup.exists()


def test_migration_retains_uncertain_delivery_status_without_restarting_it(tmp_path, monkeypatch):
    provisioner, operation, legacy, current, recovery = deployment(tmp_path, monkeypatch)
    journal = load_state(provisioner.path)
    journal["operations"][operation["id"]].update(status="PUSHING", reason="Original uncertain delivery")
    write_state(provisioner.path, journal)
    apply_migration(plan_migration(legacy, current, preserve=(recovery,)), backup_dir=tmp_path / "backup")
    migrated = load_state(provisioner.path)["operations"][operation["id"]]
    assert migrated["status"] == "PUSHING" and migrated["reason"] == "Original uncertain delivery"
    assert migrated["authorization_digest"] == operation["authorization_digest"]


def test_failed_atomic_journal_write_keeps_originals_and_private_archive(tmp_path, monkeypatch):
    provisioner, _, legacy, current, recovery = deployment(tmp_path, monkeypatch)
    plan = plan_migration(legacy, current, preserve=(recovery,))
    original = provisioner.path.read_bytes()
    write = provisioning_migration.write_state

    def failed(path, data):
        if path == provisioner.path:
            raise OSError("Synthetic persistence failure")
        write(path, data)

    monkeypatch.setattr(provisioning_migration, "write_state", failed)
    with pytest.raises(OSError, match="persistence"):
        apply_migration(plan, backup_dir=tmp_path / "backup")
    assert provisioner.path.read_bytes() == original
    assert (tmp_path / "backup" / "manifest.json").is_file()


def test_cli_plan_is_private_checksums_enforced_and_exact_apply_is_single_use(tmp_path, monkeypatch):
    provisioner, _, legacy, current, recovery = deployment(tmp_path, monkeypatch)
    plan_path, backup = tmp_path / "plan.json", tmp_path / "backup"
    assert main(["plan", "--legacy-config", str(legacy), "--config", str(current),
                 "--plan", str(plan_path), "--preserve", str(recovery)]) == 0
    assert plan_path.stat().st_mode & 0o777 == 0o600
    original = plan_path.read_bytes()
    tampered = json.loads(original)
    tampered["operation_count"] = 0
    plan_path.write_text(json.dumps(tampered))
    assert main(["apply", "--plan", str(plan_path), "--backup-dir", str(backup)]) == 1
    assert not backup.exists()
    plan_path.write_bytes(original)
    assert main(["apply", "--plan", str(plan_path), "--backup-dir", str(backup)]) == 0
    migrated = provisioner.path.read_bytes()
    assert main(["apply", "--plan", str(plan_path), "--backup-dir", str(tmp_path / "another-backup")]) == 1
    assert provisioner.path.read_bytes() == migrated


@pytest.mark.parametrize("failure", ["policy", "missing_ledger", "missing_lock", "symlink_history", "unknown_owner"])
def test_plan_never_initializes_or_repairs_invalid_legacy_state(tmp_path, monkeypatch, failure):
    provisioner, _, legacy, current, recovery = deployment(tmp_path, monkeypatch)
    journal_before = provisioner.path.read_bytes()
    if failure == "policy":
        config = json.loads(legacy.read_text())
        config["max_bandwidth_bps"] = 500_000
        legacy.write_text(json.dumps(config))
    elif failure == "missing_ledger":
        (provisioner.state_dir / ".ninja-pce-reservations.json").unlink()
    elif failure == "missing_lock":
        (provisioner.state_dir / ".ninja-pce-resources.lock").unlink()
    elif failure == "symlink_history":
        target = tmp_path / "history-target.json"
        recovery.rename(target)
        recovery.symlink_to(target)
    else:
        ledger = load_state(provisioner.state_dir / ".ninja-pce-reservations.json")
        entry = copy.deepcopy(next(iter(ledger["entries"].values())))
        entry["owner"] = "cli-provision:" + "f" * 32
        ledger["entries"]["another-tunnel"] = entry
        write_state(provisioner.state_dir / ".ninja-pce-reservations.json", ledger)
    with pytest.raises((ValueError, OSError)):
        plan_migration(legacy, current, preserve=(recovery,))
    assert provisioner.path.read_bytes() == journal_before
    if failure == "missing_lock":
        assert not (provisioner.state_dir / ".ninja-pce-resources.lock").exists()


@pytest.mark.parametrize("change", ["capacity", "namespace", "journal_bound", "origin", "proxy_uid", "enrollment"])
def test_unusable_target_policy_is_refused_without_state_changes(tmp_path, monkeypatch, change):
    provisioner, _, legacy, current, recovery = deployment(tmp_path, monkeypatch)
    original = provisioner.path.read_bytes()
    config = json.loads(current.read_text())
    if change == "capacity":
        config["max_outstanding"] = 4097
    elif change == "namespace":
        config.update(tunnel_id_min=3000, tunnel_id_max=2000)
    elif change == "journal_bound":
        config["max_journal_bytes"] = 1024
    elif change == "origin":
        config["origin"] = "http://controller.example.test"
    elif change == "proxy_uid":
        config["proxy_uid"] = True
    else:
        inventory_path = tmp_path / "inventory.json"
        inventory = json.loads(inventory_path.read_text())
        duplicate = {**inventory["devices"][0], "name": "P1", "topology_node_id": "P1"}
        inventory["devices"].append(duplicate)
        inventory_path.write_text(json.dumps(inventory))
    current.write_text(json.dumps(config))
    with pytest.raises(ValueError):
        plan_migration(legacy, current, preserve=(recovery,))
    assert provisioner.path.read_bytes() == original


def test_preserved_history_cannot_alias_the_ledger_by_hardlink_or_parent_path(tmp_path, monkeypatch):
    provisioner, _, legacy, current, recovery = deployment(tmp_path, monkeypatch)
    ledger = provisioner.state_dir / ".ninja-pce-reservations.json"
    hardlink = tmp_path / "ledger-alias.json"
    os.link(ledger, hardlink)
    original = ledger.read_bytes()
    with pytest.raises(ValueError, match="alias"):
        plan_migration(legacy, current, preserve=(recovery, hardlink))
    subdirectory = provisioner.state_dir / "existing"
    subdirectory.mkdir()
    with pytest.raises(ValueError, match="alias"):
        plan_migration(legacy, current, preserve=(subdirectory / ".." / ledger.name,))
    assert ledger.read_bytes() == original


@pytest.mark.parametrize("alias", ["symlink", "hardlink", "broken_symlink"])
def test_plan_output_aliases_never_overwrite_retained_files(tmp_path, monkeypatch, alias):
    provisioner, _, legacy, current, recovery = deployment(tmp_path, monkeypatch)
    original = provisioner.path.read_bytes()
    output = tmp_path / "plan-alias.json"
    if alias == "hardlink":
        os.link(provisioner.path, output)
    else:
        output.symlink_to(provisioner.path if alias == "symlink" else tmp_path / "missing-state.json")
    assert main(["plan", "--legacy-config", str(legacy), "--config", str(current),
                 "--plan", str(output), "--preserve", str(recovery)]) == 1
    assert provisioner.path.read_bytes() == original
    assert not (tmp_path / "missing-state.json").exists()


def test_backup_parent_symlink_cannot_put_archives_inside_live_state(tmp_path, monkeypatch):
    provisioner, _, legacy, current, recovery = deployment(tmp_path, monkeypatch)
    plan = plan_migration(legacy, current, preserve=(recovery,))
    original = provisioner.path.read_bytes()
    alias = tmp_path / "state-alias"
    alias.symlink_to(provisioner.state_dir, target_is_directory=True)
    with pytest.raises(ValueError, match="outside mutable"):
        apply_migration(plan, backup_dir=alias / "new-backup")
    assert provisioner.path.read_bytes() == original
    assert not (provisioner.state_dir / "new-backup").exists()
