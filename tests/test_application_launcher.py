"""A local launcher must preserve state and install only its isolated environment."""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location("ninja_launcher_test", Path(__file__).parents[1] / "tools/run_application.py")
assert spec is not None and spec.loader is not None
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)


@pytest.fixture
def checkout(tmp_path, monkeypatch):
    root = tmp_path / "checkout with spaces"
    (root / "src/ninja_pce").mkdir(parents=True)
    (root / "deploy").mkdir()
    for name, content in {"README.md": "Application", "pyproject.toml": "metadata", "MANIFEST.in": "manifest",
                          "deploy/constraints.txt": "core", "deploy/launcher-constraints.txt": "build",
                          "src/ninja_pce/__init__.py": '__version__ = "0.6.0"'}.items():
        (root / name).write_text(content)
    calls = []

    class Builder:
        def __init__(self, **kwargs):
            assert kwargs == {"with_pip": True}

        def create(self, target):
            (target / "bin").mkdir(parents=True)
            (target / "bin/python").write_text("owned interpreter")

    monkeypatch.setattr(launcher.venv, "EnvBuilder", Builder)
    monkeypatch.setattr(launcher, "run", lambda args, cwd: calls.append((args, cwd)))
    return root, calls


def test_first_install_then_offline_reuse_and_source_only_update(checkout):
    root, calls = checkout
    state = root / ".ninja-pce/workspace/data/state/authority.json"
    state.parent.mkdir(parents=True)
    state.write_bytes(b"existing operations and held reservations")
    python, runtime = launcher.prepare(root)
    os.close(runtime)
    assert python == root / ".ninja-pce/venv/bin/python"
    assert all(arguments[0] == str(python) and "-I" in arguments and cwd == root for arguments, cwd in calls)
    assert len(calls) == 3
    manifest = root / ".ninja-pce/installation.json"
    assert json.loads(manifest.read_text())["ready"] is True
    assert manifest.stat().st_mode & 0o777 == 0o600
    assert state.read_bytes() == b"existing operations and held reservations"
    calls.clear()
    _, runtime = launcher.prepare(root)
    os.close(runtime)
    assert calls == []  # Reopening needs neither pip nor a network request.
    (root / "src/ninja_pce/application.py").write_text("updated application")
    _, runtime = launcher.prepare(root)
    os.close(runtime)
    assert len(calls) == 2
    assert all(flag in calls[0][0] for flag in ("--no-index", "--no-deps", "--no-build-isolation"))
    assert state.read_bytes() == b"existing operations and held reservations"


def test_update_refuses_active_application_without_rewriting_manifest_or_state(checkout):
    root, calls = checkout
    _, runtime = launcher.prepare(root)
    try:
        manifest = root / ".ninja-pce/installation.json"
        before = manifest.read_bytes()
        (root / "src/ninja_pce/__init__.py").write_text("new source")
        calls.clear()
        with pytest.raises(ValueError, match="Stop locally launched"):
            launcher.prepare(root)
        assert manifest.read_bytes() == before and calls == []
    finally:
        os.close(runtime)
    _, runtime = launcher.prepare(root)
    os.close(runtime)


def test_failed_setup_retains_workspace_and_can_be_retried(checkout, monkeypatch):
    root, calls = checkout
    state = root / ".ninja-pce/workspace/data/state/owned.json"
    state.parent.mkdir(parents=True)
    state.write_bytes(b"keep owner and hold")

    def fail(args, cwd):
        raise subprocess.CalledProcessError(1, args)

    monkeypatch.setattr(launcher, "run", fail)
    with pytest.raises(subprocess.CalledProcessError):
        launcher.prepare(root)
    assert state.read_bytes() == b"keep owner and hold"
    assert json.loads((root / ".ninja-pce/installation.json").read_text())["ready"] is False
    monkeypatch.setattr(launcher, "run", lambda args, cwd: calls.append((args, cwd)))
    _, runtime = launcher.prepare(root)
    os.close(runtime)
    assert state.read_bytes() == b"keep owner and hold"


def test_southbound_extra_is_opt_in_and_persists_for_later_launches(checkout):
    root, calls = checkout
    _, runtime = launcher.prepare(root)
    os.close(runtime)
    assert not any("[southbound]" in value for args, _ in calls for value in args)
    calls.clear()
    _, runtime = launcher.prepare(root, southbound=True)
    os.close(runtime)
    assert any(str(root) + "[southbound]" in args for args, _ in calls)
    calls.clear()
    _, runtime = launcher.prepare(root)
    os.close(runtime)
    assert calls == []
    assert json.loads((root / ".ninja-pce/installation.json").read_text())["southbound"] is True


@pytest.mark.parametrize("target", [".ninja-pce", ".ninja-pce/venv", ".ninja-pce/installation.json",
                                   ".ninja-pce/.installation.lock", ".ninja-pce/.runtime.lock"])
def test_bootstrap_refuses_symlink_outputs_without_touching_destination(checkout, target, tmp_path):
    root, calls = checkout
    destination = tmp_path / "important"
    destination.write_bytes(b"keep")
    alias = root / target
    alias.parent.mkdir(parents=True, exist_ok=True)
    alias.symlink_to(destination)
    with pytest.raises((ValueError, OSError)):
        launcher.prepare(root)
    assert destination.read_bytes() == b"keep" and alias.is_symlink() and calls == []


def test_unknown_environment_is_never_adopted_or_overwritten(checkout):
    root, calls = checkout
    existing = root / ".ninja-pce/venv/bin/python"
    existing.parent.mkdir(parents=True)
    existing.write_bytes(b"an unrelated interpreter")
    with pytest.raises(ValueError, match="no installation record"):
        launcher.prepare(root)
    assert existing.read_bytes() == b"an unrelated interpreter" and calls == []


def test_defaults_are_checkout_relative_and_explicit_user_paths_remain_unchanged(checkout, monkeypatch, tmp_path):
    root, _ = checkout
    monkeypatch.chdir(tmp_path)
    assert launcher.application_arguments(root, []) == ["app", "demo"]
    assert launcher.application_arguments(root, ["demo", "--port", "8090"]) == ["app", "demo", "--port", "8090"]
    assert launcher.application_arguments(root, ["init"])[-1] == str(root / ".ninja-pce/workspace")
    for action in ("doctor", "start", "status"):
        assert launcher.application_arguments(root, [action])[-1] == str(root / ".ninja-pce/workspace/config/controller.json")
        assert launcher.application_arguments(root, [action, "--config=relative.json"]) == ["app", action, "--config=relative.json"]
    original = ["compute", "--topology", "my network.yml"]
    assert launcher.application_arguments(root, original) == original


def test_help_version_and_invalid_setup_need_no_environment(checkout, monkeypatch, capsys):
    root, _ = checkout
    monkeypatch.setattr(launcher, "ROOT", root)
    monkeypatch.setattr(launcher, "prepare", lambda *a, **kw: pytest.fail("Created environment"))
    assert launcher.main(["--help"]) == 0
    assert "sample dashboard" in capsys.readouterr().out
    assert launcher.main(["--version"]) == 0
    assert "0.6.0" in capsys.readouterr().out
    with pytest.raises(ValueError, match="Usage"):
        launcher.main(["setup", "--other"])


def test_exec_replaces_launcher_preserving_runtime_lock_and_sanitizing_import_environment(checkout, monkeypatch):
    root, _ = checkout
    monkeypatch.setattr(launcher, "ROOT", root)
    for key in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "PIP_TARGET", "PYTHONUSERBASE"):
        monkeypatch.setenv(key, "unrelated installation")
    descriptors = []
    actual_prepare = launcher.prepare

    def capture_prepare(*args, **kwargs):
        python, descriptor = actual_prepare(*args, **kwargs)
        descriptors.append(descriptor)
        return python, descriptor

    def execute(path, args, env):
        assert args[:4] == [path, "-I", "-m", "ninja_pce.cli"]
        assert args[4:] == ["app", "demo", "--port", "8091"]
        assert all(key not in env for key in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "PIP_TARGET", "PYTHONUSERBASE"))
        assert os.get_inheritable(descriptors[0])

    monkeypatch.setattr(launcher, "prepare", capture_prepare)
    monkeypatch.setattr(launcher.os, "execve", execute)
    try:
        assert launcher.main(["demo", "--port", "8091"]) == 0
    finally:
        for descriptor in descriptors:
            os.close(descriptor)


def test_executable_wrapper_works_from_an_unrelated_directory(tmp_path):
    wrapper = Path(__file__).parents[1] / "ninja-pce"
    result = subprocess.run([sys.executable, str(wrapper), "--version"], cwd=tmp_path,
                            capture_output=True, text=True, check=True)
    assert result.stdout.startswith("ninja-pce ") and result.stderr == ""
