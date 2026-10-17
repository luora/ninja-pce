"""Fresh observation defaults and existing active deployment migration boundaries."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from test_application_deployment import installer
from test_bundle_install import options as options
from test_bundle_install import verified as verified

from ninja_pce import controller
from ninja_pce.cli import main


def _custom_configuration(options, tmp_path, mode="observe"):
    document = installer.observation_configuration(Path(options.config_dir), Path(options.data_dir))
    if mode is None:
        del document["operating_mode"]
    else:
        document["operating_mode"] = mode
    config = tmp_path / "custom-controller.json"
    config.write_bytes(installer.encoded(document))
    policy = tmp_path / "custom-policy.json"
    policy.write_bytes(installer.encoded(installer.observation_policy()))
    gobgp = tmp_path / "custom-gobgpd.toml"
    gobgp.write_bytes(installer.default_gobgp_configuration())
    options.config, options.domain_policy, options.gobgp_config = str(config), str(policy), str(gobgp)
    return config, policy, gobgp


def _load_rendered(plan):
    plan.config_dir.mkdir()
    plan.config_path.write_bytes(plan.controller)
    (plan.config_dir / "domain-policy.json").write_bytes(plan.domain_policy)
    if plan.bundle is not None:
        # Config validation checks regular-file inputs without executing them.
        binary = plan.release / "bin/grpcurl"
        binary.parent.mkdir(parents=True)
        binary.write_bytes(b"inert verified fixture")
    return controller.load_controller_config(plan.config_path)


def test_bundle_default_cannot_start_pcep_even_without_unit_component_selection(options, verified):
    plan = installer.deployment(options, verified)
    config = _load_rendered(plan)
    assert config.document["operating_mode"] == "observe"
    assert list(controller.component_commands(config)) == ["bgpls", "dashboard"]
    with pytest.raises(ValueError):
        controller.component_commands(config, ["pcep"])


@pytest.mark.skipif(sys.version_info < (3, 11), reason="Custom TOML rendering requires stdlib tomllib")
def test_bundle_legacy_custom_config_becomes_explicit_observe_without_rewriting_inputs(options, verified, tmp_path):
    inputs = _custom_configuration(options, tmp_path, mode=None)
    before = {path: path.read_bytes() for path in inputs}
    plan = installer.deployment(options, verified)
    assert json.loads(plan.controller)["operating_mode"] == "observe"
    assert {path: path.read_bytes() for path in inputs} == before
    # An existing PR1 installation is reviewed and migrated separately.
    # Rendering new intent must not silently rewrite its installed document.
    plan.config_dir.mkdir()
    old_installed = json.loads(plan.controller)
    del old_installed["operating_mode"]
    old_bytes = installer.encoded(old_installed)
    plan.config_path.write_bytes(old_bytes)
    with pytest.raises(ValueError, match="overwrite"):
        installer.preflight(plan)
    assert plan.config_path.read_bytes() == old_bytes


@pytest.mark.parametrize("mode", ["pcep-control", "pcep-monitor", "provisioning", "unknown", False])
def test_bundle_refuses_custom_mode_inconsistent_with_observation_service(options, verified, tmp_path, mode):
    inputs = _custom_configuration(options, tmp_path, mode=mode)
    before = {path: path.read_bytes() for path in inputs}
    with pytest.raises(ValueError, match="operating_mode observe"):
        installer.deployment(options, verified)
    assert {path: path.read_bytes() for path in inputs} == before


def test_legacy_installer_preserves_omitted_mode_and_active_runtime(options, verified, tmp_path):
    inputs = _custom_configuration(options, tmp_path, mode=None)
    options.wheel = str(verified.wheel)
    before = {path: path.read_bytes() for path in inputs}
    plan = installer.deployment(options)
    assert "operating_mode" not in json.loads(plan.controller)
    assert "--component" not in plan.unit().decode()
    config = _load_rendered(plan)
    assert list(controller.component_commands(config)) == ["bgpls", "pcep", "dashboard"]
    assert {path: path.read_bytes() for path in inputs} == before


def test_legacy_installer_explicit_observe_is_enforced_by_runtime(options, verified, tmp_path):
    _custom_configuration(options, tmp_path)
    options.wheel = str(verified.wheel)
    plan = installer.deployment(options)
    config = _load_rendered(plan)
    assert "--component" not in plan.unit().decode()
    assert list(controller.component_commands(config)) == ["bgpls", "dashboard"]


def test_init_explains_dashboard_first_observation_start(tmp_path, capsys):
    workspace = tmp_path / "new-workspace"
    assert main(["app", "init", "--directory", str(workspace)]) == 0
    output = capsys.readouterr().out
    assert "Observation mode is enabled" in output
    assert "PCEP is disabled" in output and "no PCC session is required" in output
    assert "Open the local dashboard with: ninja-pce app start" in output
    assert "Before connecting topology, replace the example addresses" in output
    assert json.loads((workspace / "config/controller.json").read_text())["operating_mode"] == "observe"
