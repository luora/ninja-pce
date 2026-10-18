"""Permanent startup failures stop, while transient crashes have a bounded retry budget."""
from __future__ import annotations

import configparser
import shlex
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_application_deployment import installer


@pytest.fixture
def managed_plan(tmp_path):
    return installer.Deployment(
        wheel=tmp_path / "ninja_pce.whl", wheel_sha256="0" * 64, version="0.8.0",
        prefix=tmp_path / "application with spaces", config_dir=tmp_path / "configuration with spaces",
        data_dir=tmp_path / "data with spaces", unit_dir=tmp_path / "units with spaces",
        service_user="ninja-pce", service_group="ninja-pce", unit_name="ninja-controller",
        constraints=b"", controller=b"{}", domain_policy=b"{}", bundle=SimpleNamespace(),
        gobgp_config=installer.default_gobgp_configuration(),
    )


def parsed_unit(contents: bytes) -> configparser.ConfigParser:
    unit = configparser.ConfigParser(interpolation=None)
    unit.read_string(contents.decode())
    return unit


@pytest.mark.parametrize("unit_method", ["unit", "gobgp_unit"])
def test_managed_units_stop_configuration_failures_and_bound_other_restarts(managed_plan, unit_method):
    unit = parsed_unit(getattr(managed_plan, unit_method)())
    assert unit["Unit"]["StartLimitIntervalSec"] == "60"
    assert unit["Unit"]["StartLimitBurst"] == "3"
    assert unit["Service"]["Restart"] == "on-failure"
    assert unit["Service"]["RestartSec"] == "5"
    assert unit["Service"]["RestartPreventExitStatus"] == "78"
    # The main process must emit 78: systemd does not apply this restart
    # exemption to a failed ExecStartPre command.
    assert "ExecStartPre" not in unit["Service"]


def test_gobgp_launches_installed_python_wrapper_with_unambiguous_spaced_paths(managed_plan):
    unit = parsed_unit(managed_plan.gobgp_unit())
    assert shlex.split(unit["Service"]["ExecStart"]) == [
        str(managed_plan.python), "-I", "-m", "ninja_pce.cli", "runtime", "gobgp",
        "--binary", str(managed_plan.release / "bin/gobgpd"),
        "--config", str(managed_plan.config_dir / "gobgpd.toml"),
        "--api-address", "127.0.0.1", "--api-port", "50051", "--service", "ninja-gobgpd.service",
    ]
    assert unit["Service"]["UnsetEnvironment"] == "PYTHONPATH PYTHONHOME"
    assert unit["Service"]["AmbientCapabilities"] == "CAP_NET_BIND_SERVICE"
    assert unit["Service"]["CapabilityBoundingSet"] == "CAP_NET_BIND_SERVICE"
    for setting in ("NoNewPrivileges", "PrivateTmp", "ProtectHome"):
        assert unit["Service"][setting] == "true"
    assert unit["Service"]["ProtectSystem"] == "strict"


@pytest.mark.parametrize("name", ["ninja-bgpls", "ninja-pcep", "ninja-dashboard"])
def test_lab_python_service_examples_follow_startup_exit_contract(name):
    path = Path(__file__).resolve().parents[1] / "gns3/controller/ubuntu-24.04" / f"{name}.service"
    unit = parsed_unit(path.read_bytes())
    assert unit["Unit"]["StartLimitIntervalSec"] == "60"
    assert unit["Unit"]["StartLimitBurst"] == "3"
    assert unit["Service"]["Restart"] == "on-failure"
    assert unit["Service"]["RestartPreventExitStatus"] == "78"
    assert "--managed-startup" in shlex.split(unit["Service"]["ExecStart"])
    assert "ExecStartPre" not in unit["Service"]


def test_historical_raw_gobgp_unit_bounds_restarts_without_python_exit_contract():
    path = Path(__file__).resolve().parents[1] / "gns3/controller/gobgp/gobgpd.service"
    unit = parsed_unit(path.read_bytes())
    assert shlex.split(unit["Service"]["ExecStart"]) == [
        "/usr/local/bin/gobgpd", "-f", "/etc/gobgp/gobgpd.toml", "--api-hosts=127.0.0.1:50051",
    ]
    assert unit["Unit"]["StartLimitIntervalSec"] == "60"
    assert unit["Unit"]["StartLimitBurst"] == "3"
    assert unit["Service"]["Restart"] == "on-failure"
    assert "RestartPreventExitStatus" not in unit["Service"]
