"""Application onboarding must isolate new work and reuse qualified runtimes."""
from __future__ import annotations

import json
import socket
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from ninja_pce import application, controller, dashboard
from ninja_pce.cli import build_parser, main


def _files(directory: Path) -> dict[str, bytes]:
    return {str(path.relative_to(directory)): path.read_bytes()
            for path in directory.rglob("*") if path.is_file()}


def _workspace(tmp_path: Path) -> Path:
    return application.initialize_workspace(tmp_path / "new workspace")


def _grpcurl(config_path: Path, executable: str) -> None:
    document = json.loads(config_path.read_text())
    document["bgpls"]["grpcurl"] = executable
    config_path.write_text(json.dumps(document))


def test_init_creates_private_portable_workspace_without_importing_existing_state(tmp_path, monkeypatch):
    old_state = tmp_path / "build/private/cli-provisioning.json"
    old_state.parent.mkdir(parents=True)
    old_state.write_bytes(b"Existing ownership and reservation authority.\n")
    monkeypatch.chdir(tmp_path)
    assert main(["app", "init"]) == 0
    root = tmp_path / application.DEFAULT_WORKSPACE
    config_path = root / "config/controller.json"
    assert old_state.read_bytes() == b"Existing ownership and reservation authority.\n"
    assert set(_files(root)) == {"config/controller.json", "config/domain-policy.json"}
    for path in (root, *root.rglob("*")):
        assert stat.S_IMODE(path.stat().st_mode) == (0o700 if path.is_dir() else 0o600)
    assert set((root / "data").iterdir()) == {root / "data/live", root / "data/state", root / "data/run"}
    # Loading the generated configuration uses package-owned data and works from
    # another working directory. Moving the workspace preserves relative paths.
    moved = tmp_path / "moved workspace"
    root.rename(moved)
    monkeypatch.chdir(tmp_path.parent)
    config = controller.load_controller_config(moved / config_path.relative_to(root))
    data = config.document
    assert data["operating_mode"] == "observe"
    assert list(controller.component_commands(config)) == ["bgpls", "dashboard"]
    assert data["state_dir"] == str(moved / "data/state")
    assert data["runtime_dir"] == str(moved / "data/run")
    assert all(Path(value).is_relative_to(moved / "data/live") for value in data["files"].values())
    assert not data["pcep"]["headends"] and "provisioning_state" not in data["pcep"]
    assert "managed_recovery" not in data["pcep"] and "failover" not in data["pcep"]
    assert data["pcep"]["listen_host"] == "192.0.2.10"


@pytest.mark.parametrize("contents", ["empty", "partial", "owned"])
def test_init_refuses_every_existing_workspace_preserving_exact_authority(tmp_path, contents):
    target = tmp_path / "workspace"
    target.mkdir()
    if contents == "partial":
        (target / "config").mkdir()
        (target / "config/controller.json").write_bytes(b"unfinished operator configuration")
    elif contents == "owned":
        (target / "data/state").mkdir(parents=True)
        (target / "data/state/.ninja-pce-reservations.json").write_bytes(b"held ownership\n")
        (target / "data/state/cli-provisioning.json").write_bytes(b"operation and recovery history\n")
    before = _files(target)
    with pytest.raises(ValueError, match="already exists"):
        application.initialize_workspace(target)
    assert _files(target) == before


@pytest.mark.parametrize("position", ["target", "parent", "dangling_parent", "dangling_target"])
def test_init_refuses_symlink_targets_and_ancestors_without_changing_destination(tmp_path, position):
    actual = tmp_path / "actual"
    actual.mkdir()
    (actual / "important").write_bytes(b"keep")
    alias = tmp_path / "alias"
    alias.symlink_to(tmp_path / "absent" if position.startswith("dangling") else actual, target_is_directory=True)
    target = alias if position.endswith("target") else alias / "workspace"
    before = _files(actual)
    with pytest.raises(ValueError, match="symbolic link"):
        application.initialize_workspace(target)
    assert _files(actual) == before
    assert alias.is_symlink()
    assert not (tmp_path / "absent").exists()


def test_init_refuses_file_parent_and_parent_traversal(tmp_path):
    parent = tmp_path / "operator-file"
    parent.write_bytes(b"preserve")
    with pytest.raises(ValueError, match="not a directory"):
        application.initialize_workspace(parent / "workspace")
    with pytest.raises(ValueError, match="without '..'"):
        application.initialize_workspace(tmp_path / "unused/../workspace")
    assert parent.read_bytes() == b"preserve"
    assert not (tmp_path / "unused").exists()


def test_init_preserves_existing_parent_permissions(tmp_path):
    parent = tmp_path / "shared-parent"
    parent.mkdir(mode=0o750)
    before = stat.S_IMODE(parent.stat().st_mode)
    application.initialize_workspace(parent / "private/workspace")
    assert stat.S_IMODE(parent.stat().st_mode) == before
    assert stat.S_IMODE((parent / "private").stat().st_mode) == 0o700


def test_doctor_is_read_only_and_checks_local_executable_without_execution(tmp_path, monkeypatch, capsys):
    config_path = _workspace(tmp_path)
    _grpcurl(config_path, str(Path(sys.executable).resolve()))
    before = _files(tmp_path)
    monkeypatch.setattr(socket, "socket", lambda *a, **kw: pytest.fail("Doctor opened a socket"))
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: pytest.fail("Doctor executed a command"))
    assert main(["app", "doctor", "--config", str(config_path)]) == 0
    output = capsys.readouterr().out
    assert "Configuration valid" in output and "grpcurl available" in output
    assert "GoBGP must be configured separately" in output
    assert "Operating mode: observe" in output
    assert "PCEP is disabled" in output and "no PCC session is required" in output
    assert "PCC connectivity" not in output
    assert _files(tmp_path) == before
    assert not list((config_path.parent.parent / "data").rglob("*.*"))


def test_doctor_preserves_legacy_active_configuration_and_pcc_guidance(tmp_path, monkeypatch, capsys):
    config_path = _workspace(tmp_path)
    document = json.loads(config_path.read_text())
    del document["operating_mode"]
    config_path.write_text(json.dumps(document))
    _grpcurl(config_path, str(Path(sys.executable).resolve()))
    before = _files(tmp_path)
    monkeypatch.setattr(socket, "socket", lambda *a, **kw: pytest.fail("Doctor opened a socket"))
    assert main(["app", "doctor", "--config", str(config_path)]) == 0
    output = capsys.readouterr().out
    assert "Operating mode: pcep-control" in output and "PCC connectivity" in output
    assert "PCEP is disabled" not in output
    assert "pcep" in controller.component_commands(controller.load_controller_config(config_path))
    assert _files(tmp_path) == before


def test_doctor_reports_missing_or_nonexecutable_grpcurl(tmp_path, monkeypatch, capsys):
    config_path = _workspace(tmp_path)
    monkeypatch.setenv("PATH", str(tmp_path / "no-programs"))
    assert main(["app", "doctor", "--config", str(config_path)]) == 1
    assert "Missing required executable: grpcurl" in capsys.readouterr().out
    tool = tmp_path / "grpcurl"
    tool.write_text("must not run")
    tool.chmod(0o600)
    _grpcurl(config_path, str(tool))
    assert main(["app", "doctor", "--config", str(config_path)]) == 1
    assert "Missing required executable" in capsys.readouterr().out


def test_doctor_rejects_invalid_configuration_before_tool_lookup(tmp_path, monkeypatch):
    config_path = _workspace(tmp_path)
    document = json.loads(config_path.read_text())
    document["bgpls"]["required_peers"] = []
    config_path.write_text(json.dumps(document))
    monkeypatch.setattr(application.shutil, "which", lambda *a: pytest.fail("Looked up tools for invalid config"))
    with pytest.raises(ValueError):
        application.doctor(config_path)


@pytest.mark.parametrize("action", ["doctor", "start", "status"])
def test_missing_config_reports_workspace_guidance(action, tmp_path, capsys):
    absent = tmp_path / "not-created/controller.json"
    assert main(["app", action, "--config", str(absent)]) == 1
    error = capsys.readouterr().err
    assert "ninja-pce app init" in error and "--config" in error
    assert not absent.parent.exists()


def test_start_reuses_existing_controller_validation_and_runtime(tmp_path, monkeypatch):
    config_path = _workspace(tmp_path)
    captured = []

    def run(config, components):
        captured.append((config, components))
        return 7

    monkeypatch.setattr(controller, "run_controller", run)
    assert main(["app", "start", "--config", str(config_path)]) == 7
    assert len(captured) == 1 and captured[0][0].path == config_path
    assert captured[0][1] is None
    document = json.loads(config_path.read_text())
    document["pcep"]["port"] = 0
    config_path.write_text(json.dumps(document))
    assert main(["app", "start", "--config", str(config_path)]) == 1
    assert len(captured) == 1


def test_start_preserves_existing_legacy_pcep_intent_without_rewriting_configuration(tmp_path, monkeypatch):
    config_path = _workspace(tmp_path)
    document = json.loads(config_path.read_text())
    del document["operating_mode"]
    config_path.write_text(json.dumps(document))
    before = _files(tmp_path)
    components = []

    def run(config, requested):
        assert requested is None
        components.extend(controller.component_commands(config))
        return 0

    monkeypatch.setattr(controller, "run_controller", run)
    assert main(["app", "start", "--config", str(config_path)]) == 0
    assert components == ["bgpls", "pcep", "dashboard"]
    assert _files(tmp_path) == before


def test_status_uses_existing_health_reporting_without_initializing_state(tmp_path, capsys):
    config_path = _workspace(tmp_path)
    before = _files(tmp_path)
    assert main(["app", "status", "--config", str(config_path)]) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["kind"] == "controller_status" and result["status"] == "degraded"
    assert _files(tmp_path) == before


def test_demo_reuses_dashboard_runtime_with_loopback_and_synthetic_data(monkeypatch, capsys):
    captured = []

    class Server:
        server_address = ("127.0.0.1", 49238)

        def serve_forever(self, **kwargs):
            raise KeyboardInterrupt

        def server_close(self):
            captured.append("closed")

    def create(config):
        captured.append(config)
        return Server()

    monkeypatch.setattr(dashboard, "create_dashboard_server", create)
    assert main(["app", "demo", "--port", "0"]) == 0
    assert captured[0].host == "127.0.0.1" and captured[0].port == 0 and captured[0].demo is True
    assert captured[-1] == "closed"
    assert "Synthetic demo data" in capsys.readouterr().out
    snapshot = dashboard.DashboardStore(captured[0]).snapshot()
    assert snapshot["mode"] == "demo" and snapshot["read_only"] is True


def test_application_cli_defaults_and_demo_cannot_bind_a_public_interface():
    parser = build_parser()
    assert parser.parse_args(["app", "demo"]).port == 8088
    assert parser.parse_args(["app", "init"]).directory == str(application.DEFAULT_WORKSPACE)
    assert parser.parse_args(["app", "start"]).config == str(application.DEFAULT_CONFIG)
    with pytest.raises(SystemExit):
        parser.parse_args(["app", "demo", "--host", "0.0.0.0"])
