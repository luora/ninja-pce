from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from ninja_pce.cspf import compute_cspf
from ninja_pce.inventory import device_for_node
from ninja_pce.loaders import load_demand, load_inventory, load_topology
from ninja_pce.models import Affinity
from ninja_pce.southbound import (
    SouthboundError,
    build_pce_push_plan,
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


# The exact 16.9.7 CLI dialect now has separately captured operational fixtures.
IOSXE_16_9_PARTIAL_VERSION = (
    "Cisco IOS XE Software, Version 16.09.07\n"
    "Cisco IOS Software [Fuji], Virtual XE Software "
    "(X86_64_LINUX_IOSD-UNIVERSALK9-M), Version 16.9.7\n"
)
SYNTHETIC_16_9_TUNNEL_OUTPUT = "SYNTHETIC test response: no operational tunnel evidence supplied.\n"


def test_unqualified_iosxe_16_9_patch_refuses_write_before_configuration():
    plan = _plan("iosxe")
    connection = FakeConnection("iosxe", details=SYNTHETIC_16_9_TUNNEL_OUTPUT)
    connection.version = IOSXE_16_9_PARTIAL_VERSION.replace("16.09.07", "16.09.08")

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
    assert "Missing, ambiguous or mismatched LSP identity" in result.verification_errors
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


def _pce_plan(address="172.16.100.10", fmt="iosxe"):
    topology = load_topology(TOPOLOGY)
    demand = load_demand(DEMAND)
    device = _plan(fmt).device
    return build_pce_push_plan(compute_cspf(topology, demand), topology, device, address)


def test_pce_plan_binds_primary_pce_and_selected_explicit_fallback_to_approval():
    ordinary = _plan("iosxe")
    plan = _pce_plan()
    assert "tunnel mpls traffic-eng path-option 1 dynamic pce address 172.16.100.10" in plan.commands
    assert "tunnel mpls traffic-eng affinity 0x0 mask 0x0" in plan.commands
    assert "tunnel mpls traffic-eng path-option 10 explicit name EP_CUSTOMER_A_PE1_PE3" in plan.commands
    assert "tunnel mpls traffic-eng bandwidth 50000" in plan.commands
    assert plan.precheck_commands == (
        ordinary.precheck_commands[0], "show running-config | section ^interface Tunnel100$",
        *ordinary.precheck_commands[2:],
    )
    assert plan.postcheck_commands == ordinary.postcheck_commands
    assert plan.expectation == ordinary.expectation
    assert plan.plan_id == plan.calculated_id()
    assert plan.plan_id != ordinary.plan_id
    assert plan.config_revision != ordinary.config_revision
    assert not any("dynamic pce" in command or "affinity" in command for command in ordinary.commands)
    assert _pce_plan("172.16.100.11").plan_id != plan.plan_id


def test_pce_plan_old_explicit_only_approval_cannot_authorize_configuration():
    plan = _pce_plan()
    with pytest.raises(SouthboundError, match="exact plan identity"):
        push_plan(
            plan, execute=True, approved=True, approved_plan_id=_plan("iosxe").plan_id,
            connect_handler=lambda **kwargs: pytest.fail("must not connect"),
        )


def test_pce_plan_uses_the_existing_exact_plan_execution_boundary():
    plan = _pce_plan()
    connection = FakeConnection("iosxe")
    assert _execute(plan, connection).verified
    assert connection.config_commands == list(plan.commands)
    tampered = replace(plan, commands=tuple(
        command.replace("172.16.100.10", "172.16.100.11") for command in plan.commands
    ))
    with pytest.raises(SouthboundError, match="identity"):
        _execute(tampered, FakeConnection("iosxe"))


def test_pce_new_interface_precheck_does_not_require_an_existing_interface():
    plan = _pce_plan()
    connection = FakeConnection("iosxe")
    original = connection.send_command

    def send(command, **kwargs):
        if command.startswith("show running-config interface Tunnel"):
            return "% Invalid input detected at '^' marker."
        return original(command, **kwargs)

    connection.send_command = send
    assert _execute(plan, connection).verified
    assert "show running-config | section ^interface Tunnel100$" in connection.show_commands


def test_pce_anchored_interface_precheck_still_refuses_an_existing_resource():
    connection = FakeConnection("iosxe")
    connection.snapshot = "interface Tunnel100\n tunnel destination 10.255.0.6\n"
    with pytest.raises(SouthboundError, match="Pre-existing TE resource"):
        _execute(_pce_plan(), connection)
    assert not connection.config_commands


def test_pce_plan_admission_and_expectation_use_actual_cisco_kbps_units():
    topology = load_topology(TOPOLOGY)
    demand = replace(load_demand(DEMAND), bandwidth_mbps=1, reservation_bw_bps=None)
    plan = build_pce_push_plan(compute_cspf(topology, demand), topology, _plan("iosxe").device,
                               "172.16.100.10")
    assert "tunnel mpls traffic-eng bandwidth 1000" in plan.commands
    assert plan.expectation.bandwidth_bps == 1_000_000


@pytest.mark.parametrize("unsupported", ["affinity", "loose"])
def test_pce_plan_does_not_silently_discard_unsupported_path_constraints(unsupported):
    topology = load_topology(TOPOLOGY)
    demand = load_demand(DEMAND)
    if unsupported == "loose":
        demand = replace(demand, hop_type="loose")
        result = compute_cspf(topology, demand)
    else:
        result = compute_cspf(topology, demand)
        result = replace(result, demand=replace(demand, affinity=Affinity(exclude_admin_groups=("blue",))))
    with pytest.raises(SouthboundError, match="no-affinity|strict numbered"):
        build_pce_push_plan(result, topology, _plan("iosxe").device, "172.16.100.10")


@pytest.mark.parametrize("address", ["", "::1", "172.16.100.10;reload", "0172.16.100.10",
                                    "0.0.0.0", "224.0.0.1", "255.255.255.255", 123])
def test_pce_plan_refuses_unsafe_or_nonunicast_controller_addresses(address):
    with pytest.raises(SouthboundError, match="unicast IPv4"):
        _pce_plan(address)


@pytest.mark.parametrize("fmt", ["iosxr", "junos"])
def test_pce_plan_cannot_change_another_platform_grammar(fmt):
    with pytest.raises(SouthboundError, match="IOS XE only"):
        _pce_plan(fmt=fmt)


@pytest.mark.parametrize("command", [
    "tunnel mpls traffic-eng path-option 2 dynamic pce address 172.16.100.10",
    "tunnel mpls traffic-eng path-option 1 dynamic pce address 999.16.100.10",
    "tunnel mpls traffic-eng path-option 1 dynamic pce address 172.16.100.10 verbatim",
    "tunnel mpls traffic-eng affinity 0x0 mask 0xffff",
])
def test_pce_delivery_grammar_accepts_only_the_bounded_profile(command):
    with pytest.raises(SouthboundError):
        extract_config_commands("interface Tunnel101\n " + command + "\n", "iosxe")


def test_supported_but_unqualified_upgrade_cannot_cross_the_profile_write_boundary():
    connection = FakeConnection("iosxe")
    with pytest.raises(SouthboundError, match="outside the exact qualified"):
        _execute(_pce_plan(), connection, qualified_versions=("16.09.07", "16.9.7"))
    assert not connection.config_commands
    assert not connection.commits
    assert connection.disconnected


@pytest.mark.parametrize("release", ["16.09.07", "16.9.7"])
def test_exact_qualified_iosxe_aliases_allow_approved_delivery(release):
    connection = FakeConnection("iosxe", details=(FIXTURES / "iosxe_16.9.7_tunnel100_up.live.txt").read_text())
    connection.version = f"Cisco IOS XE Software, Version {release}"
    result = _execute(_pce_plan(), connection, qualified_versions=("16.09.07", "16.9.7"))
    assert result.verified, result.verification_errors
    assert connection.config_commands


@pytest.mark.parametrize("policy", [(), ["16.09.07"], ("",), (17,), (True,),
                                    ("16.09.07", "16.09.07"), ("16.09.07",) * 17,
                                    ("16.09.07;reload",), ("1" * 65,)])
def test_invalid_exact_release_policy_is_rejected_before_connecting(policy):
    with pytest.raises(SouthboundError, match="Qualified version policy"):
        push_plan(_pce_plan(), execute=True, approved=True, approved_plan_id=_pce_plan().plan_id,
                  qualified_versions=policy, connect_handler=lambda **kwargs: pytest.fail("must not connect"))
