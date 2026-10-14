"""Explicit CLI boundaries for one owned managed-tunnel recovery policy."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_pcep_compute_cli import PEER, base_args, compute_options, flatten
from test_pcep_compute_cli import offline_server as offline_server
from test_pcep_managed_failover import managed_lab

from ninja_pce.cli import main


def managed_args(planner, *, state: Path | None = None) -> list[str]:
    return ["--provisioning-state", str(planner.operation_journal),
            "--managed-failover-operation", planner.operation_id,
            "--managed-failover-state", str(state or planner.path)]


def qualified_options(tmp_path, planner):
    options = compute_options(tmp_path)
    Path(options[4][1]).write_text(json.dumps(planner.computer.node_names))
    return flatten(options)


@pytest.mark.parametrize("option,value", [
    ("--managed-failover-operation", "a" * 32),
    ("--managed-failover-state", "managed/recovery.json"),
])
def test_managed_recovery_requires_both_explicit_flags(tmp_path, offline_server, option, value):
    assert main([*base_args(tmp_path), option, value]) == 1
    assert not offline_server
    assert not (tmp_path / "pcep.json").exists()


@pytest.mark.parametrize("computation,provisioning", [(False, False), (False, True), (True, False)])
def test_managed_recovery_requires_computation_and_existing_provisioning_authority(
    tmp_path, monkeypatch, offline_server, computation, provisioning,
):
    planner, _, _, _ = managed_lab(tmp_path, monkeypatch)
    flags = managed_args(planner)
    if not provisioning:
        flags = flags[2:]
    assert main([*base_args(tmp_path),
                 *(qualified_options(tmp_path, planner) if computation else []), *flags]) == 1
    assert not offline_server


def test_managed_recovery_never_adds_numeric_tunnel_authorization(tmp_path, monkeypatch, offline_server):
    planner, _, _, _ = managed_lab(tmp_path, monkeypatch)
    before = planner.path.read_bytes()
    assert main([*base_args(tmp_path), *qualified_options(tmp_path, planner), *managed_args(planner)]) == 0
    options = offline_server["options"]
    configured = options["managed_failover"]
    assert configured.operation_id == planner.operation_id
    assert configured.path.resolve() == planner.path.resolve()
    assert configured.operation_journal.resolve() == planner.operation_journal.resolve()
    assert options["delegated_computer"].allowed_tunnels == {}
    assert options["delegated_computer"].provisioning_authorizer.path.resolve() == planner.operation_journal.resolve()
    assert "failover" not in options
    assert offline_server["started"] and offline_server["closed"]
    assert configured.path.read_bytes() == before


def test_managed_and_legacy_cli_profiles_keep_separate_exact_authority(tmp_path, monkeypatch, offline_server):
    planner, _, _, _ = managed_lab(tmp_path, monkeypatch)
    legacy = tmp_path / "legacy-recovery" / "state.json"
    assert main([*base_args(tmp_path), *qualified_options(tmp_path, planner), *managed_args(planner),
                 "--compute-delegated-tunnel", f"{PEER}=100",
                 "--failover-tunnel", f"{PEER}=100", "--failover-state", str(legacy)]) == 0
    options = offline_server["options"]
    assert options["failover"].tunnel_id == 100
    assert options["failover"].path.resolve() == legacy.resolve()
    assert options["managed_failover"].tunnel_id == planner.tunnel_id
    assert options["delegated_computer"].allowed_tunnels == {PEER: (100,)}


@pytest.mark.parametrize("filename", ["pcep.json", "raw.json", "status.json", "topology.json", "nodes.json"])
def test_managed_recovery_journal_cannot_replace_observation_files(
    tmp_path, monkeypatch, offline_server, filename,
):
    planner, _, _, _ = managed_lab(tmp_path, monkeypatch)
    target = tmp_path / filename
    options = qualified_options(tmp_path, planner)
    before = target.read_bytes() if target.exists() else None
    assert main([*base_args(tmp_path), *options, *managed_args(planner, state=target)]) == 1
    assert not offline_server
    if before is not None:
        assert target.read_bytes() == before


@pytest.mark.parametrize("alias", ["same-file", "symlink", "same-parent"])
def test_managed_recovery_cannot_share_configuration_journal_or_resource_directory(
    tmp_path, monkeypatch, offline_server, alias,
):
    planner, _, _, _ = managed_lab(tmp_path, monkeypatch)
    before = planner.operation_journal.read_bytes()
    if alias == "same-file":
        target = planner.operation_journal
    elif alias == "same-parent":
        target = planner.operation_journal.parent / "different-recovery.json"
    else:
        target = tmp_path / "managed-alias.json"
        target.symlink_to(planner.operation_journal)
    assert main([*base_args(tmp_path), *qualified_options(tmp_path, planner),
                 *managed_args(planner, state=target)]) == 1
    assert not offline_server
    assert planner.operation_journal.read_bytes() == before


@pytest.mark.parametrize("alias", ["same-file", "symlink-parent"])
def test_legacy_and_managed_recovery_cannot_alias_same_journal(
    tmp_path, monkeypatch, offline_server, alias,
):
    planner, _, _, _ = managed_lab(tmp_path, monkeypatch)
    if alias == "same-file":
        legacy = planner.path
    else:
        planner.path.parent.mkdir(parents=True, exist_ok=True)
        directory = tmp_path / "recovery-alias"
        directory.symlink_to(planner.path.parent, target_is_directory=True)
        legacy = directory / planner.path.name
    assert main([*base_args(tmp_path), *qualified_options(tmp_path, planner), *managed_args(planner),
                 "--compute-delegated-tunnel", f"{PEER}=100",
                 "--failover-tunnel", f"{PEER}=100", "--failover-state", str(legacy)]) == 1
    assert not offline_server


def test_distinct_legacy_and_managed_journals_can_share_recovery_directory(
    tmp_path, monkeypatch, offline_server,
):
    planner, _, _, _ = managed_lab(tmp_path, monkeypatch)
    assert main([*base_args(tmp_path), *qualified_options(tmp_path, planner), *managed_args(planner),
                 "--compute-delegated-tunnel", f"{PEER}=100",
                 "--failover-tunnel", f"{PEER}=100",
                 "--failover-state", str(planner.path.parent / "legacy.json")]) == 0
    assert offline_server["options"]["delegated_computer"].allowed_tunnels == {PEER: (100,)}


def test_cli_refuses_two_recovery_profiles_for_same_tunnel(tmp_path, monkeypatch, offline_server):
    planner, _, _, _ = managed_lab(tmp_path, monkeypatch)
    assert main([*base_args(tmp_path), *qualified_options(tmp_path, planner), *managed_args(planner),
                 "--compute-delegated-tunnel", f"{PEER}={planner.tunnel_id}",
                 "--failover-tunnel", f"{PEER}={planner.tunnel_id}",
                 "--failover-state", str(tmp_path / "legacy" / "state.json")]) == 1
    assert not offline_server


def test_reinstatement_flag_requires_managed_profile(tmp_path, offline_server):
    assert main([*base_args(tmp_path), "--managed-reinstate-request", str(tmp_path / "request.json")]) == 1
    assert not offline_server


@pytest.mark.parametrize("target", ["pcep.json", "raw.json", "journal"])
def test_reinstatement_request_cannot_alias_controller_inputs(tmp_path, monkeypatch, offline_server, target):
    planner, _, _, _ = managed_lab(tmp_path, monkeypatch)
    request = planner.path if target == "journal" else tmp_path / target
    assert main([*base_args(tmp_path), *qualified_options(tmp_path, planner), *managed_args(planner),
                 "--managed-reinstate-request", str(request)]) == 1
    assert not offline_server
