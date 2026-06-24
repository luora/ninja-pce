from dataclasses import replace
from pathlib import Path

import pytest

from ninja_pce.cspf import compute_cspf
from ninja_pce.inventory import device_for_node
from ninja_pce.loaders import load_demand, load_inventory, load_topology
from ninja_pce.southbound import SouthboundError, build_push_plan, push_plan

ROOT = Path(__file__).resolve().parents[1]
TOPOLOGY = ROOT / "examples" / "topologies" / "simple_multiarea.yml"
DEMAND = ROOT / "examples" / "demands" / "pe1_to_pe3_50m.yml"
INVENTORY = ROOT / "gns3" / "lab-data" / "inventory" / "gns3_inventory.yml"


class FakeConnection:
    def __init__(self):
        self.config_commands = []
        self.show_commands = []
        self.disconnected = False

    def send_command(self, command):
        self.show_commands.append(command)
        return f"output for {command}"

    def send_config_set(self, commands, **kwargs):
        self.config_commands = commands
        return "configuration accepted"

    def disconnect(self):
        self.disconnected = True

    def commit(self):
        return "commit complete"


def _plan():
    topology = load_topology(TOPOLOGY)
    demand = load_demand(DEMAND)
    inventory = load_inventory(INVENTORY)
    result = compute_cspf(topology, demand)
    return build_push_plan(result, topology, device_for_node(inventory, "PE1"))


def test_push_plan_contains_config_only_and_preserves_cisco_submodes():
    plan = _plan()

    assert plan.output_format == "iosxr"
    assert "exit" in plan.commands
    assert not any(command.startswith("show ") for command in plan.commands)
    assert any(command.startswith("interface ") for command in plan.commands)


def test_push_uses_secret_reference_and_runs_checks():
    plan = _plan()
    connection = FakeConnection()
    captured = {}

    def connect_handler(**kwargs):
        captured.update(kwargs)
        return connection

    result = push_plan(
        plan,
        environ={"NINJA_PCE_PE1_PASSWORD": "test-secret"},
        connect_handler=connect_handler,
    )

    assert captured["device_type"] == "cisco_xr"
    assert captured["password"] == "test-secret"
    assert connection.config_commands == list(plan.commands)
    assert connection.show_commands == list(plan.precheck_commands + plan.postcheck_commands)
    assert connection.disconnected
    assert result.commands_sent == len(plan.commands)


def test_push_refuses_missing_secret():
    with pytest.raises(SouthboundError, match="is not set"):
        push_plan(_plan(), environ={}, connect_handler=lambda **kwargs: FakeConnection())


def test_postcheck_failure_is_reported_without_reclassifying_applied_config():
    plan = _plan()
    connection = FakeConnection()
    original_send = connection.send_command

    def send_command(command):
        if command == plan.postcheck_commands[-1]:
            raise RuntimeError("verification timeout")
        return original_send(command)

    connection.send_command = send_command
    result = push_plan(
        plan,
        environ={"NINJA_PCE_PE1_PASSWORD": "test-secret"},
        connect_handler=lambda **kwargs: connection,
    )

    assert result.commands_sent == len(plan.commands)
    assert result.verification_errors == (
        f"{plan.postcheck_commands[-1]}: verification timeout",
    )


def test_junos_plan_never_treats_run_show_as_configuration():
    plan = _plan()
    junos_device = replace(plan.device, device_type="juniper_junos")
    topology = load_topology(TOPOLOGY)
    demand = load_demand(DEMAND)
    result = compute_cspf(topology, demand)

    junos_plan = build_push_plan(result, topology, junos_device)

    assert junos_plan.commands
    assert all(command.startswith("set ") for command in junos_plan.commands)
    assert not any(command.startswith("run show ") for command in junos_plan.commands)
