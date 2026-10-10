from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from ninja_pce.cspf import compute_cspf
from ninja_pce.inventory import device_for_node
from ninja_pce.loaders import load_demand, load_inventory, load_topology
from ninja_pce.southbound import (
    SouthboundError,
    build_push_plan,
    extract_config_commands,
    push_plan,
    verify_plan,
)

ROOT = Path(__file__).resolve().parents[1]
TOPOLOGY = ROOT / "examples/topologies/simple_multiarea.yml"
DEMAND = ROOT / "examples/demands/pe1_to_pe3_50m.yml"
INVENTORY = ROOT / "gns3/lab-data/inventory/gns3_inventory.yml"
FIXTURES = ROOT / "tests/fixtures/operational"
VERSIONS = {
    "iosxr": "Cisco IOS XR Software, Version 7.9.2",
    "iosxe": "Cisco IOS XE Software, Version 17.12.04",
    "junos": "Junos: 23.4R2.13",
}
FILES = {
    "iosxr": "iosxr_7.9_up.mock.txt",
    "iosxe": "iosxe_17.12_up.mock.txt",
    "junos": "junos_23.4_up.mock.txt",
}


def _plan(fmt="iosxr"):
    topology = load_topology(TOPOLOGY)
    demand = load_demand(DEMAND)
    inventory = load_inventory(INVENTORY)
    device = device_for_node(inventory, "PE1")
    device = replace(
        device, device_type={"iosxr": "cisco_xr", "iosxe": "cisco_ios", "junos": "juniper_junos"}[fmt]
    )
    return build_push_plan(compute_cspf(topology, demand), topology, device)


class FakeConnection:
    """Mock transport; fixtures are synthetic, never evidence of a live lab."""

    def __init__(self, fmt="iosxr", *, details=None, fail_postchecks=0):
        self.fmt = fmt
        self.details = (FIXTURES / FILES[fmt]).read_text() if details is None else details
        self.fail_postchecks = fail_postchecks
        self.config_commands = []
        self.show_commands = []
        self.commits = []
        self.disconnected = False
        self.snapshot = ""
        self.version = VERSIONS[fmt]

    def send_command(self, command, **kwargs):
        assert 0 < kwargs["read_timeout"] <= 60
        self.show_commands.append(command)
        if command == "show version":
            return self.version
        if "config" in command:
            return self.snapshot
        if "route " in command:
            return "Routing entry for 10.255.0.6/32"
        if self.fail_postchecks:
            self.fail_postchecks -= 1
            raise TimeoutError("verification timeout test-secret")
        return self.details

    def send_config_set(self, commands, **kwargs):
        self.config_commands = commands
        assert kwargs["read_timeout"] <= 60
        return "configuration accepted"

    def disconnect(self):
        self.disconnected = True

    def exit_config_mode(self):
        return ""

    def commit(self, **kwargs):
        self.commits.append(kwargs)
        return "configuration check succeeds" if kwargs.get("check") else "commit complete"


def _execute(plan, connection, **kwargs):
    return push_plan(
        plan,
        execute=True,
        approved=True,
        approved_plan_id=plan.plan_id,
        environ={"NINJA_PCE_PE1_PASSWORD": "test-secret"},
        connect_handler=lambda **params: connection,
        **kwargs,
    )


def test_plan_preserves_exact_selected_ero_and_cisco_submodes():
    plan = _plan()
    assert plan.output_format == "iosxr"
    assert "exit" in plan.commands
    assert not any(command.startswith("show ") for command in plan.commands)
    assert any("strict ipv4 unicast 10.0.13.3" in command for command in plan.commands)
    assert plan.plan_id == plan.plan_hash == plan.calculated_id()


@pytest.mark.parametrize("fmt", VERSIONS)
def test_push_requires_operational_proof_and_vendor_commits(fmt):
    plan = _plan(fmt)
    connection = FakeConnection(fmt)
    result = _execute(plan, connection)
    assert result.verified, result.verification_errors
    assert result.status == "VERIFIED"
    assert connection.config_commands == list(plan.commands)
    assert connection.show_commands == list(plan.precheck_commands + plan.postcheck_commands)
    assert connection.disconnected
    assert result.commands_sent == len(plan.commands)
    if fmt == "junos":
        assert connection.commits[0]["check"]
        assert connection.commits[1]["confirm"]
        assert "confirm" not in connection.commits[2]
    elif fmt == "iosxr":
        assert connection.commits[0]["confirm"]
        assert "confirm" not in connection.commits[1]
    else:
        assert not connection.commits


def test_secret_reference_and_bounded_transport_settings():
    plan = _plan()
    connection = FakeConnection()
    captured = {}

    def handler(**params):
        captured.update(params)
        return connection

    result = push_plan(
        plan,
        execute=True,
        approved=True,
        approved_plan_id=plan.plan_id,
        environ={"NINJA_PCE_PE1_PASSWORD": "test-secret"},
        connect_handler=handler,
    )
    assert captured["password"] == "test-secret"
    assert captured["conn_timeout"] == captured["read_timeout_override"] == 20
    assert "test-secret" not in repr(result)


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"execute": True},
        {"approved": True},
        {"execute": True, "approved": True},
        {"execute": True, "approved": True, "approved_plan_id": "stale"},
    ],
)
def test_missing_or_stale_approval_never_connects(params):
    with pytest.raises(SouthboundError):
        push_plan(_plan(), connect_handler=lambda **kwargs: pytest.fail("must not connect"), **params)


def test_expired_approval_never_connects():
    plan = _plan()
    with pytest.raises(SouthboundError, match="expired"):
        push_plan(
            plan,
            execute=True,
            approved=True,
            approved_plan_id=plan.plan_id,
            approval_expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
            connect_handler=lambda **kwargs: pytest.fail("must not connect"),
        )


def test_kill_switch_at_actual_execution_boundary():
    plan = _plan()
    with pytest.raises(SouthboundError, match="PREVIEW_ONLY"):
        push_plan(
            plan,
            execute=True,
            approved=True,
            approved_plan_id=plan.plan_id,
            environ={"NINJA_PCE_PREVIEW_ONLY": "true"},
            connect_handler=lambda **kwargs: pytest.fail("must not connect"),
        )


def test_push_refuses_missing_secret():
    plan = _plan()
    with pytest.raises(SouthboundError, match="is not set"):
        push_plan(
            plan,
            execute=True,
            approved=True,
            approved_plan_id=plan.plan_id,
            environ={},
            connect_handler=lambda **kwargs: pytest.fail("must not connect"),
        )


def test_changed_plan_commands_are_not_covered_by_old_approval():
    plan = _plan()
    changed = replace(plan, commands=plan.commands + ("shutdown",))
    with pytest.raises(SouthboundError, match="identity"):
        _execute(changed, FakeConnection())


def test_plan_id_changes_for_material_inputs():
    plan = _plan()
    topology = load_topology(TOPOLOGY)
    demand = load_demand(DEMAND)
    changed = build_push_plan(
        compute_cspf(topology, replace(demand, bandwidth_mbps=51, reservation_bw_bps=None)),
        topology,
        plan.device,
    )
    assert changed.plan_id != plan.plan_id
    changed_device = build_push_plan(
        compute_cspf(topology, demand), topology, replace(plan.device, host="192.0.2.2")
    )
    assert changed_device.plan_id != plan.plan_id


def test_successful_commands_with_unknown_operational_output_fail_verification():
    connection = FakeConnection(details="configuration accepted")
    result = _execute(_plan(), connection)
    assert result.commands_sent > 0
    assert not result.verified
    assert result.status == "VERIFICATION_FAILED"
    assert len(connection.commits) == 1  # confirmed commit is deliberately not made permanent
    assert result.recovery_note


def test_delayed_establishment_is_retried_without_repeating_configuration():
    connection = FakeConnection(fail_postchecks=2)
    result = _execute(_plan(), connection)
    assert result.verified
    assert len(connection.show_commands) == len(_plan().precheck_commands) + 3


def test_postcheck_timeout_is_bounded_and_redacted():
    connection = FakeConnection(fail_postchecks=10)
    result = _execute(_plan(), connection)
    assert result.status == "VERIFICATION_FAILED"
    assert result.verification_errors == ("verification timeout [REDACTED]",)
    assert connection.fail_postchecks == 7


def test_readonly_revalidation_has_zero_configuration_calls():
    plan = _plan()
    connection = FakeConnection()
    result = verify_plan(
        plan, environ={"NINJA_PCE_PE1_PASSWORD": "test-secret"}, connect_handler=lambda **kwargs: connection
    )
    assert result.verified
    assert result.commands_sent == 0
    assert not connection.config_commands
    assert not connection.commits


def test_unknown_version_refuses_configuration():
    connection = FakeConnection()
    connection.version = "unknown version"
    with pytest.raises(SouthboundError, match="version"):
        _execute(_plan(), connection)
    assert not connection.config_commands
    assert connection.disconnected


# Partial software records reported by the user; no operational LSP capture was supplied.
IOSXE_16_9_PARTIAL_VERSION = (
    "Cisco IOS XE Software, Version 16.09.07\n"
    "Cisco IOS Software [Fuji], Virtual XE Software "
    "(X86_64_LINUX_IOSD-UNIVERSALK9-M), Version 16.9.7\n"
)
SYNTHETIC_16_9_TUNNEL_OUTPUT = "SYNTHETIC test response: no operational tunnel evidence supplied.\n"


def test_reported_iosxe_16_9_partial_version_refuses_write_before_configuration():
    plan = _plan("iosxe")
    connection = FakeConnection("iosxe", details=SYNTHETIC_16_9_TUNNEL_OUTPUT)
    connection.version = IOSXE_16_9_PARTIAL_VERSION

    with pytest.raises(SouthboundError, match="Unsupported or missing software version"):
        _execute(plan, connection)

    assert connection.show_commands == list(plan.precheck_commands)
    assert not connection.config_commands
    assert not connection.commits
    assert connection.disconnected


def test_reported_iosxe_16_9_readonly_collection_retains_unverified_raw_evidence():
    plan = _plan("iosxe")
    connection = FakeConnection("iosxe", details=SYNTHETIC_16_9_TUNNEL_OUTPUT)
    connection.version = IOSXE_16_9_PARTIAL_VERSION

    result = verify_plan(
        plan,
        environ={"NINJA_PCE_PE1_PASSWORD": "test-secret"},
        connect_handler=lambda **kwargs: connection,
        verification_attempts=1,
    )

    assert result.precheck_output == (("show version", IOSXE_16_9_PARTIAL_VERSION),)
    assert result.postcheck_output == ((plan.postcheck_commands[0], SYNTHETIC_16_9_TUNNEL_OUTPUT),)
    assert result.status == "VERIFICATION_FAILED"
    assert result.verification_errors == ("Unsupported or missing platform/software version",)
    assert not result.verified
    assert result.commands_sent == 0
    assert connection.show_commands == ["show version", *plan.postcheck_commands]
    assert not connection.config_commands
    assert not connection.commits
    assert connection.disconnected


def test_existing_resources_are_not_merged_or_blindly_deleted():
    connection = FakeConnection()
    connection.snapshot = "interface tunnel-te100\n destination 192.0.2.1\n"
    with pytest.raises(SouthboundError, match="ownership"):
        _execute(_plan(), connection)
    assert not connection.config_commands
    assert not connection.commits


def test_ssh_loss_during_write_requires_manual_recovery_and_no_retry():
    connection = FakeConnection()

    def fail(commands, **kwargs):
        connection.config_commands = commands
        raise TimeoutError("write outcome unknown password=test-secret")

    connection.send_config_set = fail
    result = _execute(_plan(), connection)
    assert result.status == "ROLLBACK_REQUIRED"
    assert not result.verified
    assert "test-secret" not in repr(result)
    assert result.recovery_note


def test_commit_failure_does_not_become_verified():
    connection = FakeConnection()

    def fail(**kwargs):
        raise RuntimeError("commit failed")

    connection.commit = fail
    result = _execute(_plan(), connection)
    assert result.status == "ROLLBACK_REQUIRED"
    assert not result.verified


@pytest.mark.parametrize("fmt", VERSIONS)
@pytest.mark.parametrize(
    "command",
    [
        "set system root-authentication plain-text-password",
        "delete protocols mpls path OTHER",
        "interface Tunnel1; reload",
        "show clock",
        "shutdown",
        "rename protocols mpls path A to B",
    ],
)
def test_unknown_commands_and_injection_are_rejected(fmt, command):
    with pytest.raises(SouthboundError):
        extract_config_commands(command, fmt)


def test_junos_configuration_contains_only_exact_supported_set_grammar():
    plan = _plan("junos")
    assert all(command.startswith("set protocols mpls ") for command in plan.commands)
    assert not any("explicit-path" in command for command in plan.commands)


def test_equivalent_topology_order_has_same_exact_plan_identity():
    plan = _plan()
    topology = load_topology(TOPOLOGY)
    demand = load_demand(DEMAND)
    reordered = replace(
        topology, nodes=dict(reversed(list(topology.nodes.items()))), links=list(reversed(topology.links))
    )
    other = build_push_plan(compute_cspf(reordered, demand), reordered, plan.device)
    assert other.plan_id == plan.plan_id


def test_string_false_is_not_authorization():
    plan = _plan()
    with pytest.raises(SouthboundError, match="Refusing"):
        push_plan(
            plan,
            execute="false",
            approved="false",
            approved_plan_id=plan.plan_id,
            connect_handler=lambda **params: pytest.fail("must not connect"),
        )


@pytest.mark.parametrize("fmt", ["iosxr", "junos"])
@pytest.mark.parametrize("response", ["", "command accepted", "unknown acknowledgement"])
def test_unknown_initial_commit_acknowledgement_cannot_verify(fmt, response):
    connection = FakeConnection(fmt)
    original = connection.commit

    def commit(**kwargs):
        return original(**kwargs) if kwargs.get("check") else response

    connection.commit = commit
    result = _execute(_plan(fmt), connection)
    assert result.status == "ROLLBACK_REQUIRED"
    assert not result.verified


@pytest.mark.parametrize("fmt", ["iosxr", "junos"])
def test_unknown_final_commit_acknowledgement_cannot_verify(fmt):
    connection = FakeConnection(fmt)
    original = connection.commit

    def commit(**kwargs):
        return original(**kwargs) if kwargs.get("check") or kwargs.get("confirm") else ""

    connection.commit = commit
    result = _execute(_plan(fmt), connection)
    assert result.status == "ROLLBACK_REQUIRED"
    assert not result.verified


def test_file_revision_guard_runs_after_prechecks_before_any_write():
    plan = _plan()
    connection = FakeConnection()
    called = []

    def guard():
        assert connection.show_commands == list(plan.precheck_commands)
        assert not connection.config_commands
        called.append(True)
        raise SouthboundError("Topology revision changed during prechecks")

    with pytest.raises(SouthboundError, match="revision changed"):
        _execute(plan, connection, pre_write_guard=guard)
    assert called == [True]
    assert not connection.config_commands
    assert not connection.commits
    assert connection.disconnected


def test_xr_documented_commit_echo_and_prompt_are_positive_acceptance():
    connection = FakeConnection()

    def commit(**kwargs):
        command = "commit confirmed 300" if kwargs.get("confirm") else "commit"
        return (
            f"RP/0/RP0/CPU0:router(config)#{command}\nFri Oct 9 10:00:00 UTC\nRP/0/RP0/CPU0:router(config)#"
        )

    connection.commit = commit
    assert _execute(_plan(), connection).verified
