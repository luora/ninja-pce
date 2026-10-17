"""Deployment must be reviewable, isolated and preserve existing controller state."""
from __future__ import annotations

import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import zipfile
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest


def load_tool(name: str, relative: str):
    path = Path(__file__).resolve().parents[1] / relative
    specification = importlib.util.spec_from_file_location(name, path)
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    sys.modules[name] = module
    specification.loader.exec_module(module)
    return module


installer = load_tool("ninja_test_installer", "deploy/install.py")
distribution = load_tool("ninja_test_distribution", "tools/build_distribution.py")


@pytest.fixture
def options(tmp_path):
    wheel = tmp_path / "ninja_pce-0.5.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("ninja_pce-0.5.0.dist-info/METADATA", "Name: ninja-pce\nVersion: 0.5.0\n")
    config_dir = tmp_path / "configuration"
    data_dir = tmp_path / "data"
    controller = {
        "version": 1,
        "files": {"bgpls_raw": str(data_dir / "live/raw.json"),
                  "bgpls_status": str(data_dir / "live/status.json"),
                  "topology": str(data_dir / "live/topology.json"),
                  "pcep_capture": str(data_dir / "live/pcep.json")},
        "state_dir": str(data_dir / "state"), "runtime_dir": str(data_dir / "run"),
        "bgpls": {"endpoint": "127.0.0.1:50051", "required_peers": ["10.201.0.1"]},
        "pcep": {"listen_host": "10.201.0.10", "domain_policy": str(config_dir / "domain-policy.json")},
        "dashboard": {"enabled": True, "listen_host": "127.0.0.1", "port": 8088},
    }
    config = tmp_path / "source-controller.json"
    config.write_text(json.dumps(controller))
    policy = tmp_path / "source-domain-policy.json"
    policy.write_text('{"version": 1}')
    return SimpleNamespace(wheel=str(wheel), config=str(config), domain_policy=str(policy),
                           prefix=str(tmp_path / "application"), config_dir=str(config_dir),
                           data_dir=str(data_dir), unit_dir=str(tmp_path / "systemd"),
                           service_user="ninja-pce", service_group=None, unit_name="ninja-controller")


def test_render_is_repeatable_and_performs_no_system_changes(options, tmp_path, monkeypatch):
    monkeypatch.setattr(installer.subprocess, "run", lambda *a, **kw: pytest.fail("Renderer ran a command"))
    plan = installer.deployment(options)
    output = tmp_path / "review"
    installer.render(plan, output)
    first = {path.name: path.read_bytes() for path in output.iterdir()}
    installer.render(plan, output)
    assert {path.name: path.read_bytes() for path in output.iterdir()} == first
    assert set(first) == {"controller.json", "domain-policy.json", "ninja-controller.service",
                          "deployment-manifest.json"}
    for destination in (plan.prefix, plan.config_dir, plan.data_dir, plan.unit_dir):
        assert not destination.exists()
    unit = first["ninja-controller.service"].decode()
    assert f'ExecStart="{plan.python}" -I -m ninja_pce.cli controller run --config "{plan.config_path}"' in unit
    for setting in ("User=ninja-pce", "NoNewPrivileges=true", "PrivateTmp=true", "ProtectSystem=strict",
                    "ProtectHome=true", f'ReadWritePaths="{plan.data_dir}"'):
        assert setting in unit
    assert "ExecStartPre=" not in unit
    assert f"WorkingDirectory={plan.data_dir}\n" in unit
    assert "UnsetEnvironment=PYTHONPATH PYTHONHOME" in unit


@pytest.mark.parametrize(("field", "value"), [
    ("service_user", "root"), ("service_user", "ninja\nExecStart=/bin/sh"),
    ("service_group", "root"), ("unit_name", "../other"), ("unit_name", "ninja.service"),
    ("prefix", "/opt/%u"), ("prefix", "/opt/$HOME"), ("prefix", "/opt/../etc"),
])
def test_rejects_privileged_accounts_and_unit_injection(options, field, value):
    setattr(options, field, value)
    with pytest.raises(ValueError):
        installer.deployment(options)


def test_rejects_writable_paths_outside_service_data(options):
    source = Path(options.config)
    document = json.loads(source.read_text())
    document["files"]["pcep_capture"] = "/etc/important.json"
    source.write_text(json.dumps(document))
    with pytest.raises(ValueError, match="writable"):
        installer.deployment(options)


@pytest.mark.parametrize("value", ["labels.json", "/home/operator/labels.json", "/root/labels.json"])
def test_installed_static_paths_must_survive_config_move_and_service_sandbox(options, value):
    source = Path(options.config)
    document = json.loads(source.read_text())
    document["bgpls"]["node_map"] = value
    source.write_text(json.dumps(document))
    with pytest.raises(ValueError):
        installer.deployment(options)


def test_render_refuses_different_existing_file_before_writing_others(options, tmp_path):
    plan = installer.deployment(options)
    output = tmp_path / "review"
    output.mkdir()
    existing = output / "domain-policy.json"
    existing.write_bytes(b"operator policy\n")
    with pytest.raises(ValueError, match="overwrite"):
        installer.render(plan, output)
    assert list(output.iterdir()) == [existing]
    assert existing.read_bytes() == b"operator policy\n"


def test_install_preflight_rejects_existing_configuration_release_and_symlinks(options, tmp_path):
    plan = installer.deployment(options)
    plan.config_dir.mkdir()
    plan.config_path.write_text("operator configuration")
    with pytest.raises(ValueError, match="overwrite"):
        installer.preflight(plan)
    plan.config_path.unlink()
    plan.release.mkdir(parents=True)
    marker = plan.release / "immutable"
    marker.write_text("old release")
    with pytest.raises(ValueError, match="Release already exists"):
        installer.preflight(plan)
    assert marker.read_text() == "old release"
    marker.unlink()
    plan.release.rmdir()
    target = tmp_path / "actual-data"
    target.mkdir()
    plan.data_dir.symlink_to(target, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        installer.preflight(plan)


def test_install_is_offline_validates_first_preserves_journals_and_never_activates(options, tmp_path, monkeypatch):
    plan = installer.deployment(options)
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    journal_dir = plan.data_dir / "state"
    journal_dir.mkdir(parents=True)
    journal = journal_dir / "managed-recovery.json"
    journal.write_bytes(b"existing checksummed private journal\n")
    commands = []
    ownership_changes = []
    monkeypatch.setattr(installer.os, "geteuid", lambda: 0)
    monkeypatch.setattr(installer, "systemd_access_preflight", lambda p: None)
    monkeypatch.setattr(installer, "account", lambda p: (os.getuid(), os.getgid()))
    monkeypatch.setattr(installer.os, "chown", lambda path, uid, gid: ownership_changes.append(Path(path)))

    class Builder:
        def __init__(self, **kwargs):
            assert kwargs == {"with_pip": True}

        def create(self, path):
            (path / "bin").mkdir(parents=True)

    def command(args, **kwargs):
        commands.append(args)
        assert kwargs == {"check": True}
        if "validate" in args:
            staged = json.loads(Path(args[-1]).read_text())
            assert Path(staged["pcep"]["domain_policy"]).exists()
            if args[0] == "runuser":
                assert plan.config_path.exists() and args[-1] == str(plan.config_path)
                assert args[args.index("--user") + 1] == plan.service_user
            else:
                assert not plan.config_path.exists()
        if "pip" in args:
            assert "--no-index" in args and "--find-links" in args
            assert "--constraint" in args
            assert Path(args[args.index("--constraint") + 1]).read_bytes() == plan.constraints
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(installer.venv, "EnvBuilder", Builder)
    monkeypatch.setattr(installer.subprocess, "run", command)
    installer.install(plan, wheelhouse)
    assert journal.read_bytes() == b"existing checksummed private journal\n"
    assert journal not in ownership_changes and journal_dir not in ownership_changes
    assert plan.config_path.read_bytes() == plan.controller
    assert plan.unit_path.read_bytes() == plan.unit()
    assert [args for args in commands if args[0] == "systemctl"] == [["systemctl", "daemon-reload"]]
    assert sum("validate" in args for args in commands) == 2
    assert sum("pip" in args for args in commands) == 2


def test_installer_requires_explicit_offline_dependencies(options, monkeypatch):
    plan = installer.deployment(options)
    monkeypatch.setattr(installer.os, "geteuid", lambda: 0)
    with pytest.raises(ValueError, match="wheelhouse"):
        installer.install(plan, None)
    assert not plan.release.exists()


def test_existing_directory_ownership_is_never_rewritten(tmp_path, monkeypatch):
    directory = tmp_path / "state"
    directory.mkdir()
    changes = []
    monkeypatch.setattr(installer.os, "chown", lambda *args: changes.append(args))
    with pytest.raises(ValueError, match="ownership"):
        installer.state_directory(directory, os.getuid() + 100, os.getgid())
    assert changes == []


def test_existing_state_directory_needs_service_permissions_without_rewriting(tmp_path, monkeypatch):
    directory = tmp_path / "state"
    directory.mkdir()
    directory.chmod(0o000)
    monkeypatch.setattr(installer.os, "chown", lambda *args: pytest.fail("Existing ownership changed"))
    try:
        with pytest.raises(ValueError, match="read/write/traverse"):
            installer.state_directory(directory, os.getuid(), os.getgid())
        assert directory.stat().st_mode & 0o777 == 0
    finally:
        directory.chmod(0o700)


@pytest.mark.parametrize("root", ["/home/operator", "/root", "/tmp", "/var/tmp"])
def test_install_rejects_service_paths_hidden_by_systemd_before_account_changes(options, root):
    options.prefix = root + "/ninja-pce"
    plan = installer.deployment(options)
    with pytest.raises(ValueError, match="ProtectHome/PrivateTmp"):
        installer.systemd_access_preflight(plan)


@pytest.mark.parametrize("path", ["/tmp/labels.json", "/var/tmp/labels.json"])
def test_render_allows_temporary_static_preview_but_install_rejects_it(options, path):
    source = Path(options.config)
    document = json.loads(source.read_text())
    document["bgpls"]["node_map"] = path
    source.write_text(json.dumps(document))
    plan = installer.deployment(options)
    plan = replace(plan, prefix=Path("/opt/ninja-pce"), config_dir=Path("/etc/ninja-pce"),
                   data_dir=Path("/var/lib/ninja-pce"), unit_dir=Path("/etc/systemd/system"))
    with pytest.raises(ValueError, match=path):
        installer.systemd_access_preflight(plan)


@pytest.mark.parametrize("private", [".env", ".ninja-pce/installation.json"])
def test_distribution_rejects_private_source_members_and_wheel_files(tmp_path, private):
    wheel = tmp_path / "application.whl"
    source = tmp_path / "application.tar.gz"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("ninja_pce/__init__.py", "")
    with tarfile.open(source, "w:gz") as archive:
        content = b"secret"
        member = tarfile.TarInfo("ninja-pce/" + private)
        member.size = len(content)
        archive.addfile(member, io.BytesIO(content))
    with pytest.raises(ValueError, match="source archive"):
        distribution.inspect_artifacts(wheel, source)
    with tarfile.open(source, "w:gz"):
        pass
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("private-config/password.json", "")
    with pytest.raises(ValueError, match="wheel member"):
        distribution.inspect_artifacts(wheel, source)


def test_build_never_overwrites_an_existing_output(tmp_path, monkeypatch):
    output = tmp_path / "dist"
    output.mkdir()
    artifact = output / "reviewed.whl"
    artifact.write_bytes(b"existing release")
    monkeypatch.setattr(distribution.subprocess, "run", lambda *a, **kw: pytest.fail("Build unexpectedly ran"))
    with pytest.raises(ValueError, match="overwritten"):
        distribution.build(output)
    assert artifact.read_bytes() == b"existing release"


@pytest.mark.parametrize("contents", [b'{"version":1,"version":2}', b'{"number":NaN}'])
def test_deployment_json_rejects_ambiguous_authority(tmp_path, contents):
    path = tmp_path / "policy.json"
    path.write_bytes(contents)
    with pytest.raises(ValueError):
        installer.read_json(path)


def test_deployment_json_rejects_fifo_without_waiting_for_writer(tmp_path):
    path = tmp_path / "config.fifo"
    os.mkfifo(path)
    with pytest.raises(ValueError, match="regular file"):
        installer.read_json(path)


@pytest.mark.parametrize("source_change", [None, "untracked", "tracked"])
def test_distribution_provenance_excludes_only_own_build_scratch(tmp_path, monkeypatch, source_change):
    repo = tmp_path / "repository"
    repo.mkdir()
    (repo / "README.md").write_text("Reviewed application source\n")
    (repo / ".gitignore").write_text("dist/\n")
    (repo / "docs").mkdir()
    (repo / "deploy").mkdir()
    (repo / "deploy/install.py").write_text("# reviewed installer\n")
    for arguments in (["init", "--quiet"], ["add", "."],
                      ["-c", "core.hooksPath=/dev/null", "-c", "user.name=Release Test",
                       "-c", "user.email=release-test@example.invalid", "commit", "--quiet", "-m", "Source"]):
        subprocess.run(["git", *arguments], cwd=repo, check=True, capture_output=True)
    if source_change == "untracked":
        (repo / "unreviewed-source.py").write_text("# real source change\n")
    elif source_change == "tracked":
        (repo / "README.md").write_text("Modified source\n")

    run_command = subprocess.run
    scratches = []

    def fake_build(arguments, **kwargs):
        if arguments[:3] != [sys.executable, "-m", "build"]:
            return run_command(arguments, **kwargs)
        stage = Path(arguments[arguments.index("--outdir") + 1])
        scratches.append(stage.parent)
        assert stage.parent.parent == repo
        # The scratch is a real untracked directory during the build. The final
        # provenance must exclude it while preserving unrelated source changes.
        (stage.parent / "scratch-marker").write_text("temporary build content")
        with zipfile.ZipFile(stage / "ninja_pce-0.5.0-py3-none-any.whl", "w") as archive:
            archive.writestr("ninja_pce/__init__.py", "")
        with tarfile.open(stage / "ninja_pce-0.5.0.tar.gz", "w:gz") as archive:
            content = b"reviewed source"
            member = tarfile.TarInfo("ninja_pce-0.5.0/README.md")
            member.size = len(content)
            archive.addfile(member, io.BytesIO(content))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(distribution, "__file__", str(repo / "tools/build_distribution.py"))
    monkeypatch.setattr(distribution.subprocess, "run", fake_build)
    distribution.build(repo / "dist")
    manifest = json.loads((repo / "dist/distribution-manifest.json").read_text())
    assert manifest["source_revision"] is not None
    assert manifest["source_dirty"] is (source_change is not None)
    assert scratches and all(not scratch.exists() for scratch in scratches)


def test_generated_unit_with_spaced_paths_is_accepted_by_systemd(options, tmp_path):
    analyzer = shutil.which("systemd-analyze")
    if analyzer is None:
        pytest.skip("systemd parser is not installed")
    plan = installer.deployment(options)
    plan = replace(plan, prefix=tmp_path / "application with spaces",
                   data_dir=tmp_path / "data with spaces")
    plan.python.parent.mkdir(parents=True)
    plan.python.write_text("#!/bin/sh\nexit 0\n")
    plan.python.chmod(0o755)
    unit = tmp_path / "ninja-parser-test.service"
    unit.write_bytes(plan.unit())
    result = subprocess.run([analyzer, "verify", "--man=no", str(unit)],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert not any("ninja-parser-test.service:" in line for line in result.stderr.splitlines()), result.stderr


@pytest.mark.parametrize("path", ["/opt/application ", "/etc/configuration  "])
def test_systemd_scalar_paths_refuse_trailing_whitespace(path):
    with pytest.raises(ValueError, match="canonical absolute"):
        installer.absolute_path(path)
