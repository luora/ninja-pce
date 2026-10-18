"""The authenticated installer path is offline, inert and preserves existing state."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import zipfile
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from test_application_deployment import installer

from ninja_pce import controller
from ninja_pce.cli import build_parser


@pytest.fixture
def verified(tmp_path):
    # Authentication itself is covered by test_release_bundle.py. These
    # fixtures model the private snapshot passed to the installer afterward.
    root = tmp_path / "verified"
    for relative in ("bin", "provenance", "deploy", "wheelhouse"):
        (root / relative).mkdir(parents=True, exist_ok=True)
    wheel = root / "ninja_pce-0.8.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("ninja_pce-0.8.0.dist-info/METADATA", "Name: ninja-pce\nVersion: 0.8.0\n")
    components = {}
    for name in ("gobgpd", "grpcurl"):
        (root / "bin" / name).write_bytes(f"authenticated {name} bytes".encode())
        (root / "provenance" / f"{name}.json").write_text(json.dumps({"component": name}))
        components[name] = {"path": f"bin/{name}", "provenance": f"provenance/{name}.json",
                            "version": "fixture-version"}
    (root / "deploy/constraints.txt").write_bytes(b"networkx==3.4.2\n")
    (root / "wheelhouse/dependency.whl").write_bytes(b"authenticated dependency")
    artifacts = {path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                 for path in root.rglob("*") if path.is_file()}
    manifest = {"artifacts": artifacts, "components": components}
    manifest_bytes = json.dumps(manifest).encode()
    (root / "distribution-manifest.json").write_bytes(manifest_bytes)
    (root / "distribution-manifest.sig").write_bytes(b"verified signature")
    return SimpleNamespace(root=root, wheel=wheel, wheelhouse=root / "wheelhouse", manifest=manifest,
                           manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(), validate_host=Mock())


@pytest.fixture
def options(tmp_path, verified):
    return installer.parser().parse_args([
        "--bundle", str(verified.root), "--trusted-key", str(tmp_path / "independent-key.pem"),
        "--prefix", str(tmp_path / "application"), "--config-dir", str(tmp_path / "configuration"),
        "--data-dir", str(tmp_path / "data"), "--unit-dir", str(tmp_path / "units"),
        "--output-dir", str(tmp_path / "review"),
    ])


def test_fresh_bundle_render_is_observation_only_and_cross_host(options, verified, tmp_path, monkeypatch):
    monkeypatch.setattr(installer.subprocess, "run", lambda *a, **kw: pytest.fail("Render executed supplied code"))
    plan = installer.deployment(options, verified)
    installer.execute(plan, options)
    verified.validate_host.assert_not_called()
    output = Path(options.output_dir)
    assert {path.name for path in output.iterdir()} == {
        "controller.json", "domain-policy.json", "gobgpd.toml", "ninja-controller.service",
        "ninja-gobgpd.service", "ninja-admin.service", "deployment-manifest.json",
    }
    document = json.loads(plan.controller)
    assert document["bgpls"]["grpcurl"] == str(plan.release / "bin/grpcurl")
    assert document["bgpls"]["required_peers"] == ["192.0.2.20"]
    assert document["pcep"]["listen_host"] == "127.0.0.1"
    unit = plan.unit().decode()
    assert "--component bgpls --component dashboard\n" in unit
    assert "--component pcep" not in unit
    args = build_parser().parse_args(["controller", "run", "--config", str(plan.config_path),
                                     "--component", "bgpls", "--component", "dashboard"])
    assert args.component == ["bgpls", "dashboard"]
    gobgp = plan.gobgp_unit().decode()
    assert f'ExecStart="{plan.python}" -I -m ninja_pce.cli runtime gobgp --binary ' in gobgp
    assert f'"{plan.release / "bin/gobgpd"}" --config "{plan.config_dir / "gobgpd.toml"}"' in gobgp
    assert "--api-address 127.0.0.1 --api-port 50051 --service ninja-gobgpd.service" in gobgp
    assert "StartLimitIntervalSec=60\nStartLimitBurst=3" in gobgp
    assert "RestartPreventExitStatus=78" in gobgp
    admin = plan.admin_unit().decode()
    assert f'ExecStart="{plan.python}" -I -m ninja_pce.cli admin serve --directory ' in admin
    assert f'"{plan.data_dir / "admin-workspace"}"' in admin
    assert "--port 8080 --managed-startup" in admin
    assert "TimeoutStopSec=90" in admin
    assert "port = -1" in plan.gobgp_config.decode()
    assert "[[neighbors]]" not in plan.gobgp_config.decode()
    deployment_manifest = json.loads((output / "deployment-manifest.json").read_text())
    assert deployment_manifest["bundle_manifest_sha256"] == verified.manifest_sha256
    assert deployment_manifest["operating_intent"] == "observe"
    for target in (plan.prefix, plan.config_dir, plan.data_dir, plan.unit_dir):
        assert not target.exists()


@pytest.mark.parametrize("arguments, message", [
    (["--bundle", "/missing"], "trusted-key"),
    ([], "--wheel, --config and --domain-policy"),
    (["--trusted-key", "/outside/key"], "require --bundle"),
])
def test_required_inputs_fail_before_render(arguments, message, tmp_path, capsys):
    output = tmp_path / "review"
    assert installer.main([*arguments, "--output-dir", str(output)]) == 1
    assert message in capsys.readouterr().err
    assert not output.exists()


@pytest.mark.parametrize("field", ["wheel", "wheelhouse"])
def test_bundle_refuses_unverified_overrides(options, verified, field):
    setattr(options, field, "/untrusted/payload")
    with pytest.raises(ValueError, match="verified wheel and wheelhouse"):
        installer.deployment(options, verified)


@pytest.mark.parametrize("name", ["ninja-gobgpd", "ninja-admin"])
def test_bundle_reserves_managed_unit_names(options, verified, name):
    options.unit_name = name
    with pytest.raises(ValueError, match="must differ"):
        installer.deployment(options, verified)


def test_bundle_requires_complete_custom_configuration(options, verified, tmp_path):
    options.config = str(tmp_path / "controller.json")
    with pytest.raises(ValueError, match="both --config and --domain-policy"):
        installer.deployment(options, verified)
    options.domain_policy = str(tmp_path / "policy.json")
    with pytest.raises(ValueError, match="also requires --gobgp-config"):
        installer.deployment(options, verified)


@pytest.mark.skipif(sys.version_info < (3, 11), reason="Custom TOML rendering requires stdlib tomllib")
def test_bundle_custom_config_uses_verified_grpcurl_and_preserves_inputs(options, verified, tmp_path):
    config_dir, data_dir = Path(options.config_dir), Path(options.data_dir)
    document = installer.observation_configuration(config_dir, data_dir)
    document["bgpls"]["required_peers"] = ["10.201.0.1"]
    document["bgpls"]["grpcurl"] = "/unverified/grpcurl"
    config = tmp_path / "custom-controller.json"
    config.write_bytes(installer.encoded(document))
    policy = tmp_path / "custom-policy.json"
    policy.write_bytes(installer.encoded(installer.observation_policy()))
    gobgp = tmp_path / "custom-gobgpd.toml"
    gobgp.write_bytes(installer.default_gobgp_configuration())
    options.config, options.domain_policy, options.gobgp_config = str(config), str(policy), str(gobgp)
    original = {path: path.read_bytes() for path in (config, policy, gobgp)}
    plan = installer.deployment(options, verified)
    assert json.loads(plan.controller)["bgpls"]["required_peers"] == ["10.201.0.1"]
    assert json.loads(plan.controller)["bgpls"]["grpcurl"] == str(plan.release / "bin/grpcurl")
    assert {path: path.read_bytes() for path in original} == original
    document["bgpls"]["endpoint"] = "127.0.0.1:51000"
    config.write_bytes(installer.encoded(document))
    with pytest.raises(ValueError, match="127.0.0.1:50051"):
        installer.deployment(options, verified)


def test_bundle_host_rejection_precedes_render_and_system_changes(options, verified, monkeypatch):
    verified.validate_host.side_effect = ValueError("unsupported target")
    options.install = True
    plan = installer.deployment(options, verified)
    monkeypatch.setattr(installer, "account", lambda p: pytest.fail("Host rejection created account"))
    with pytest.raises(ValueError, match="unsupported target"):
        installer.execute(plan, options)
    assert not Path(options.output_dir).exists()
    assert not plan.release.exists()


def test_bundle_render_cannot_add_unsigned_output_to_release(options, verified):
    options.output_dir = str(verified.root / "review")
    plan = installer.deployment(options, verified)
    with pytest.raises(ValueError, match="outside the authenticated bundle"):
        installer.execute(plan, options)
    assert not Path(options.output_dir).exists()


@pytest.mark.parametrize("conflict", ["gobgpd.toml", "ninja-gobgpd.service", "ninja-admin.service"])
def test_bundle_preflight_preserves_existing_gobgp_configuration_and_unit(options, verified, conflict):
    plan = installer.deployment(options, verified)
    target = (plan.config_dir if conflict.endswith(".toml") else plan.unit_dir) / conflict
    target.parent.mkdir()
    target.write_bytes(b"existing deployment ownership")
    with pytest.raises(ValueError, match="overwrite"):
        installer.preflight(plan)
    assert target.read_bytes() == b"existing deployment ownership"
    assert not plan.release.exists()


@pytest.fixture
def mocked_system(options, verified, monkeypatch):
    plan = installer.deployment(options, verified)
    commands = []
    monkeypatch.setattr(installer.os, "geteuid", lambda: 0)
    monkeypatch.setattr(installer, "systemd_access_preflight", lambda p: None)
    monkeypatch.setattr(installer.os, "chown", lambda *a: None)
    account = Mock(return_value=(os.getuid(), os.getgid()))
    monkeypatch.setattr(installer, "account", account)

    class Builder:
        def __init__(self, **kwargs):
            assert kwargs == {"with_pip": True}

        def create(self, path):
            (path / "bin").mkdir(parents=True)

    def run(arguments, **kwargs):
        commands.append((arguments, kwargs))
        if "pip" in arguments:
            assert "--no-index" in arguments and "--only-binary=:all:" in arguments
            assert "--isolated" in arguments
            assert kwargs["env"]["PIP_CONFIG_FILE"] == os.devnull
            assert Path(arguments[arguments.index("--find-links") + 1]) == verified.wheelhouse
            assert Path(arguments[arguments.index("--constraint") + 1]).read_bytes() == plan.constraints
        elif "--dry-run" in arguments:
            assert kwargs == {"check": True, "timeout": 30}
            assert all(flag in arguments for flag in ("--pprof-disable", "--metrics-path=", "--config-strict"))
        else:
            assert kwargs == {"check": True}
        if "--dry-run" in arguments:
            assert Path(arguments[0]).read_bytes() == (verified.root / "bin/gobgpd").read_bytes()
            assert not plan.release.exists() and not plan.config_path.exists()
            account.assert_not_called()
        if "validate" in arguments:
            checked = controller.load_controller_config(arguments[-1])
            assert Path(checked.document["bgpls"]["grpcurl"]).is_file()
            if arguments[0] != "runuser":
                account.assert_not_called()
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(installer.venv, "EnvBuilder", Builder)
    monkeypatch.setattr(installer.subprocess, "run", run)
    return plan, commands, account


def test_verified_install_stages_tools_before_mutation_and_never_activates(mocked_system, verified):
    plan, commands, account = mocked_system
    journals = plan.data_dir / "state"
    journals.mkdir(parents=True)
    protected = journals / "recovery.json"
    protected.write_bytes(b"existing LSP recovery and reservation ownership")
    installer.install(plan, None)
    account.assert_called_once_with(plan)
    assert protected.read_bytes() == b"existing LSP recovery and reservation ownership"
    for name in ("gobgpd", "grpcurl"):
        native = plan.release / "bin" / name
        assert native.read_bytes() == (verified.root / "bin" / name).read_bytes()
        assert native.stat().st_mode & 0o777 == 0o755
        assert (plan.release / "provenance" / f"{name}.json").exists()
    for name in ("distribution-manifest.json", "distribution-manifest.sig"):
        assert (plan.release / "provenance" / name).read_bytes() == (verified.root / name).read_bytes()
    for name, contents in plan.configuration_files().items():
        assert (plan.config_dir / name).read_bytes() == contents
    assert (plan.unit_dir / "ninja-gobgpd.service").read_bytes() == plan.gobgp_unit()
    assert (plan.unit_dir / "ninja-admin.service").read_bytes() == plan.admin_unit()
    assert not plan.admin_workspace.exists()
    assert [args for args, _ in commands if args[0] == "systemctl"] == [["systemctl", "daemon-reload"]]
    assert sum("pip" in args for args, _ in commands) == 2
    assert sum("validate" in args for args, _ in commands) == 2
    assert not any("admin" in args or "enable" in args or "start" in args for args, _ in commands)


def test_invalid_gobgp_dry_run_leaves_installation_untouched(mocked_system, monkeypatch):
    plan, commands, account = mocked_system
    run = installer.subprocess.run

    def reject(arguments, **kwargs):
        if "--dry-run" in arguments:
            raise subprocess.CalledProcessError(1, arguments)
        return run(arguments, **kwargs)

    monkeypatch.setattr(installer.subprocess, "run", reject)
    with pytest.raises(subprocess.CalledProcessError):
        installer.install(plan, None)
    account.assert_not_called()
    for target in (plan.prefix, plan.config_dir, plan.data_dir, plan.unit_dir):
        assert not target.exists()
    assert not any(args[0] == "systemctl" for args, _ in commands)


def test_hanging_gobgp_validation_stops_before_system_changes(mocked_system, monkeypatch):
    plan, commands, account = mocked_system
    run = installer.subprocess.run

    def timeout(arguments, **kwargs):
        if "--dry-run" in arguments:
            assert kwargs["timeout"] == 30
            raise subprocess.TimeoutExpired(arguments, 30)
        return run(arguments, **kwargs)

    monkeypatch.setattr(installer.subprocess, "run", timeout)
    with pytest.raises(ValueError, match="exceeded 30 seconds"):
        installer.install(plan, None)
    account.assert_not_called()
    assert not plan.release.exists()
    assert not any(args[0] == "systemctl" for args, _ in commands)


def test_snapshot_native_tampering_is_rejected_before_execution(mocked_system, verified):
    plan, commands, account = mocked_system
    (verified.root / "bin/gobgpd").write_bytes(b"modified after verification")
    with pytest.raises(ValueError, match="changed before installation"):
        installer.install(plan, None)
    account.assert_not_called()
    assert not any("--dry-run" in args for args, _ in commands)
    assert not plan.release.exists()


def test_bundle_pip_ignores_host_network_and_source_configuration(mocked_system, monkeypatch):
    plan, commands, _ = mocked_system
    for name in ("PIP_FIND_LINKS", "PIP_INDEX_URL", "PIP_EXTRA_INDEX_URL", "PIP_CONFIG_FILE",
                 "PIP_NO_BINARY", "PIP_REQUIREMENT", "PIP_CONSTRAINT"):
        monkeypatch.setenv(name, "https://untrusted.example.invalid/source")
    installer.install(plan, None)
    pip_calls = [(arguments, kwargs) for arguments, kwargs in commands if "pip" in arguments]
    assert len(pip_calls) == 2
    for arguments, kwargs in pip_calls:
        assert "--disable-pip-version-check" in arguments
        assert {key: value for key, value in kwargs["env"].items() if key.upper().startswith("PIP_")} == {
            "PIP_CONFIG_FILE": os.devnull,
        }


def test_custom_gobgp_render_has_actionable_error_without_tomllib(options, verified, tmp_path, monkeypatch):
    import builtins

    source = tmp_path / "gobgpd.toml"
    source.write_bytes(installer.default_gobgp_configuration())
    options.gobgp_config = str(source)
    real_import = builtins.__import__

    def missing(name, *args, **kwargs):
        if name == "tomllib":
            raise ModuleNotFoundError("No module named tomllib", name="tomllib")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", missing)
    with pytest.raises(ValueError, match="Python 3.11 or newer"):
        installer.deployment(options, verified)


def test_generated_bundle_units_are_accepted_by_systemd_with_spaced_paths(options, verified, tmp_path):
    analyzer = shutil.which("systemd-analyze")
    if analyzer is None:
        pytest.skip("systemd parser is not installed")
    for field in ("prefix", "config_dir", "data_dir", "unit_dir"):
        setattr(options, field, str(tmp_path / f"{field} with spaces"))
    plan = installer.deployment(options, verified)
    # verify inspects executable paths but never starts these harmless stubs.
    for executable in (plan.python, plan.release / "bin/gobgpd"):
        executable.parent.mkdir(parents=True, exist_ok=True)
        executable.write_text("#!/bin/sh\nexit 0\n")
        executable.chmod(0o755)
    plan.unit_dir.mkdir()
    assert set(plan.service_files()) == {"ninja-controller.service", "ninja-gobgpd.service", "ninja-admin.service"}
    units = []
    for name, contents in plan.service_files().items():
        unit = plan.unit_dir / name
        unit.write_bytes(contents)
        units.append(str(unit))
    result = subprocess.run([analyzer, "verify", "--man=no", *units], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert not any(f"{name}:" in result.stderr for name in plan.service_files()), result.stderr


@pytest.mark.parametrize("reject", [False, True])
def test_bundle_helper_import_suppresses_bytecode_and_restores_process_state(options, verified, monkeypatch, reject):
    import builtins

    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1]))
    from deploy import bundle as bundle_module

    previous = sys.dont_write_bytecode
    real_import = builtins.__import__
    imports = []

    def without_bytecode(name, *args, **kwargs):
        if name == "deploy.bundle":
            imports.append(name)
            assert sys.dont_write_bytecode is True
        return real_import(name, *args, **kwargs)

    @contextmanager
    def authenticated(*args):
        assert sys.dont_write_bytecode is True
        if reject:
            raise ValueError("invalid signed inventory")
        yield verified
        assert sys.dont_write_bytecode is True

    monkeypatch.setattr(builtins, "__import__", without_bytecode)
    monkeypatch.setattr(bundle_module, "verified_bundle", authenticated)
    monkeypatch.setattr(installer, "execute", lambda *a: None)
    assert installer.main(["--bundle", str(verified.root), "--trusted-key", str(options.trusted_key)]) == int(reject)
    assert imports == ["deploy.bundle"]
    assert sys.dont_write_bytecode is previous
