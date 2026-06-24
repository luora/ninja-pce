from __future__ import annotations

import os
from dataclasses import dataclass
from difflib import unified_diff
from typing import Any, Callable, Mapping

from .inventory import normalize_device_type, render_format_for_device
from .loaders import InputError
from .models import CspfResult, ManagedDevice, Topology
from .render import render_config


class SouthboundError(RuntimeError):
    """Raised when a configuration cannot be safely prepared or pushed."""


@dataclass(frozen=True)
class PushPlan:
    device: ManagedDevice
    output_format: str
    rendered_config: str
    commands: tuple[str, ...]
    precheck_commands: tuple[str, ...]
    postcheck_commands: tuple[str, ...]

    @property
    def command_text(self) -> str:
        return "\n".join(self.commands) + "\n"


@dataclass(frozen=True)
class PushResult:
    device: str
    host: str
    commands_sent: int
    output: str
    commit_output: str | None = None
    precheck_output: tuple[tuple[str, str], ...] = ()
    postcheck_output: tuple[tuple[str, str], ...] = ()
    verification_errors: tuple[str, ...] = ()


def extract_config_commands(rendered_config: str, output_format: str) -> tuple[str, ...]:
    fmt = output_format.lower()
    commands: list[str] = []

    for raw_line in rendered_config.splitlines():
        stripped = raw_line.strip()
        lowered = stripped.lower()
        if not stripped:
            continue

        if fmt in {"iosxr", "iosxe"}:
            if lowered in {
                "! suggested pre-check commands",
                "! suggested verification commands",
                "! rollback reminder",
            }:
                break
            if stripped.startswith("!"):
                if stripped == "!" and commands and commands[-1] != "exit":
                    commands.append("exit")
                continue
            if lowered.startswith("show "):
                continue
            if " !" in stripped:
                stripped = stripped.split(" !", 1)[0].rstrip()
            commands.append(stripped)
            continue

        if fmt == "junos":
            if lowered.startswith(
                ("## suggested pre-check commands", "## suggested verification commands", "## rollback reminder")
            ):
                break
            if lowered.startswith(("set ", "delete ", "activate ", "deactivate ", "rename ")):
                commands.append(stripped)
            continue

        raise SouthboundError(f"Output format {output_format!r} cannot be pushed")

    while commands and commands[-1] == "exit":
        commands.pop()
    if not commands:
        raise SouthboundError(f"No configuration commands found for format {output_format}")
    return tuple(commands)


def build_push_plan(
    result: CspfResult,
    topology: Topology,
    device: ManagedDevice,
) -> PushPlan:
    output_format = render_format_for_device(device)
    rendered = render_config(result, topology, output_format)
    commands = extract_config_commands(rendered, output_format)
    destination = topology.nodes[result.demand.destination].loopback
    if output_format == "iosxr":
        prechecks = (
            "show mpls traffic-eng tunnels summary",
            f"show route {destination}/32",
        )
        postchecks = (
            f"show mpls traffic-eng tunnels {result.demand.output.iosxr_interface} detail",
            "show rsvp session",
        )
    elif output_format == "iosxe":
        tunnel = f"Tunnel{result.demand.output.tunnel_id}"
        prechecks = (
            "show mpls traffic-eng tunnels brief",
            f"show ip route {destination}",
        )
        postchecks = (
            f"show mpls traffic-eng tunnels {tunnel} detail",
            "show ip rsvp reservation",
        )
    else:
        lsp_name = result.demand.output.junos_lsp_name
        prechecks = (
            "show mpls lsp ingress brief",
            f"show route table inet.3 {destination}",
        )
        postchecks = (
            f"show mpls lsp name {lsp_name} extensive",
            f"show rsvp session name {lsp_name} extensive",
        )
    return PushPlan(
        device=device,
        output_format=output_format,
        rendered_config=rendered,
        commands=commands,
        precheck_commands=prechecks,
        postcheck_commands=postchecks,
    )


def config_diff(previous: str | None, desired: str) -> str:
    previous_lines = (previous or "").splitlines(keepends=True)
    desired_lines = desired.splitlines(keepends=True)
    return "".join(
        unified_diff(
            previous_lines,
            desired_lines,
            fromfile="last-applied",
            tofile="desired",
        )
    )


def push_plan(
    plan: PushPlan,
    *,
    environ: Mapping[str, str] | None = None,
    connect_handler: Callable[..., Any] | None = None,
) -> PushResult:
    env = os.environ if environ is None else environ
    device = plan.device
    if not device.password_env:
        raise SouthboundError(
            f"Inventory device {device.name} has no password_env; "
            "inline passwords are intentionally unsupported"
        )
    password = env.get(device.password_env)
    if not password:
        raise SouthboundError(
            f"Required secret environment variable {device.password_env} is not set"
        )

    if connect_handler is None:
        try:
            from netmiko import ConnectHandler
        except ImportError as exc:
            raise SouthboundError(
                "Netmiko is not installed. Install with: pip install 'ninja-pce[southbound]'"
            ) from exc
        connect_handler = ConnectHandler

    device_type = normalize_device_type(device.device_type)
    try:
        connection = connect_handler(
            device_type=device_type,
            host=device.host,
            username=device.username,
            password=password,
            port=device.port,
            fast_cli=False,
        )
    except Exception as exc:
        raise SouthboundError(
            f"Could not connect to {device.name} at {device.host}:{device.port}: {exc}"
        ) from exc

    commit_output: str | None = None
    precheck_output: list[tuple[str, str]] = []
    postcheck_output: list[tuple[str, str]] = []
    verification_errors: list[str] = []
    try:
        for command in plan.precheck_commands:
            precheck_output.append((command, str(connection.send_command(command))))
    except Exception as exc:
        try:
            connection.disconnect()
        except Exception:
            pass
        raise SouthboundError(f"Pre-check failed on {device.name}: {exc}") from exc

    try:
        output = str(
            connection.send_config_set(
                list(plan.commands),
                error_pattern=r"(?i)(% ?invalid|syntax error|error:|commit failed)",
            )
        )
        if device_type == "juniper_junos":
            commit_output = str(connection.commit())
    except Exception as exc:
        try:
            connection.disconnect()
        except Exception:
            pass
        raise SouthboundError(f"Configuration push failed on {device.name}: {exc}") from exc

    try:
        for command in plan.postcheck_commands:
            try:
                postcheck_output.append((command, str(connection.send_command(command))))
            except Exception as exc:
                verification_errors.append(f"{command}: {exc}")
    finally:
        try:
            connection.disconnect()
        except Exception:
            pass

    return PushResult(
        device=device.name,
        host=device.host,
        commands_sent=len(plan.commands),
        output=output,
        commit_output=commit_output,
        precheck_output=tuple(precheck_output),
        postcheck_output=tuple(postcheck_output),
        verification_errors=tuple(verification_errors),
    )


def require_approved_execution(execute: bool, approved: bool) -> None:
    if execute and not approved:
        raise InputError("Refusing execution without --approve")
    if approved and not execute:
        raise InputError("--approve is only valid together with --execute")
