from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from difflib import unified_diff
from ipaddress import IPv4Address
from typing import Any, Callable, Mapping, cast

from .inventory import normalize_device_type, render_format_for_device
from .loaders import InputError
from .models import CspfResult, ManagedDevice, Topology
from .render import build_render_context, render_config
from .verification import (
    CliOperationalVerifier,
    OperationalVerifier,
    VerificationExpectation,
    supported_version,
)


class SouthboundError(RuntimeError):
    """Raised before execution when a configuration cannot be safely delivered."""


STATES = frozenset(
    {
        "PLANNED",
        "PUSHING",
        "APPLIED_UNVERIFIED",
        "VERIFIED",
        "VERIFICATION_FAILED",
        "ROLLBACK_REQUIRED",
        "ROLLED_BACK",
    }
)
ERROR_PATTERN = (
    r"(?i)(%\s*(?:invalid|error|failed)|syntax error|error:|commit failed|configuration check-out failed)"
)
IDENTIFIER = r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}"
IP = r"\d{1,3}(?:\.\d{1,3}){3}"


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def _topology_revision(topology: Topology) -> str:
    value = asdict(topology)
    value["nodes"] = {key: value["nodes"][key] for key in sorted(value["nodes"])}
    for node in value["nodes"].values():
        node["tags"] = sorted(node["tags"])
    value["links"] = sorted(
        (asdict(edge) for edge in topology.directional_links), key=lambda edge: edge["id"]
    )
    for edge in value["links"]:
        for key in ("admin_groups", "srlg", "capabilities"):
            if key in edge:
                edge[key] = sorted(edge[key])
    return _digest(value)


@dataclass(frozen=True)
class PushPlan:
    device: ManagedDevice
    output_format: str
    rendered_config: str
    commands: tuple[str, ...]
    precheck_commands: tuple[str, ...]
    postcheck_commands: tuple[str, ...]
    topology_revision: str = ""
    demand_revision: str = ""
    device_revision: str = ""
    config_revision: str = ""
    expectation: VerificationExpectation | None = None
    plan_id: str = ""

    @property
    def command_text(self) -> str:
        return "\n".join(self.commands) + "\n"

    @property
    def plan_hash(self) -> str:
        return self.plan_id

    def calculated_id(self) -> str:
        return _digest({key: value for key, value in asdict(self).items() if key != "plan_id"})


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
    status: str = "APPLIED_UNVERIFIED"
    plan_id: str = ""
    config_snapshot: str = ""
    config_diff: str = ""
    recovery_note: str | None = None

    @property
    def verified(self) -> bool:
        return self.status == "VERIFIED"


def extract_config_commands(rendered_config: str, output_format: str) -> tuple[str, ...]:
    """Parse the complete supported grammar; unknown config lines are fatal.

    Documentation sections are not executable. Unlike prefix allowlists, every
    argument and Cisco submode is checked, including separator/control rejection.
    """
    fmt = output_format.lower()
    if fmt not in {"iosxr", "iosxe", "junos"}:
        raise SouthboundError(f"Output format {output_format!r} cannot be pushed")
    commands: list[str] = []
    mode: str | None = None
    patterns: tuple[str, ...]
    for raw in rendered_config.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.lower().startswith(("! suggested", "! rollback", "## suggested", "## rollback")):
            break
        if line.startswith(("!", "#")):
            if line == "!" and mode is not None:
                commands.append("exit")
                mode = None
            continue
        if re.search(r"[;|&`<>\x00-\x1f\x7f]", line):
            raise SouthboundError("Forbidden separator or control character in configuration")
        if " !" in line:
            line = line.split(" !", 1)[0].rstrip()
        if fmt == "junos":
            patterns = (
                rf"set protocols mpls path {IDENTIFIER} {IP} (?:strict|loose)",
                rf"set protocols mpls label-switched-path {IDENTIFIER} to {IP}",
                rf"set protocols mpls label-switched-path {IDENTIFIER} bandwidth [0-9]+",
                rf"set protocols mpls label-switched-path {IDENTIFIER} primary {IDENTIFIER}",
                rf"set protocols mpls label-switched-path {IDENTIFIER} priority [0-7] [0-7]",
            )
        elif mode is None:
            patterns = (
                (rf"explicit-path name {IDENTIFIER}", r"interface tunnel-te[0-9]{1,5}")
                if fmt == "iosxr"
                else (rf"ip explicit-path name {IDENTIFIER} enable", r"interface Tunnel[0-9]{1,5}")
            )
        elif mode == "path":
            patterns = (
                (rf"index [0-9]+ next-address (?:strict|loose) ipv4 unicast {IP}",)
                if fmt == "iosxr"
                else (rf"next-address (?:strict|loose) {IP}",)
            )
        elif fmt == "iosxr":
            patterns = (
                rf"description ninja-pce {IDENTIFIER} {IDENTIFIER} to {IDENTIFIER}",
                r"ipv4 unnumbered Loopback0",
                rf"destination {IP}",
                r"signalled-bandwidth [0-9]+",
                r"priority [0-7] [0-7]",
                rf"path-option 10 explicit name {IDENTIFIER}",
            )
        else:
            patterns = (
                rf"description ninja-pce {IDENTIFIER} {IDENTIFIER} to {IDENTIFIER}",
                r"ip unnumbered Loopback0",
                r"tunnel mode mpls traffic-eng",
                rf"tunnel destination {IP}",
                r"tunnel mpls traffic-eng bandwidth [0-9]+",
                r"tunnel mpls traffic-eng priority [0-7] [0-7]",
                rf"tunnel mpls traffic-eng path-option 10 explicit name {IDENTIFIER}",
            )
        if not any(re.fullmatch(pattern, line) for pattern in patterns):
            raise SouthboundError(f"Unsupported configuration command or submode: {line!r}")
        for address in re.findall(rf"\b{IP}\b", line):
            try:
                IPv4Address(address)
            except ValueError as exc:
                raise SouthboundError("Invalid IPv4 address in configuration") from exc
        if fmt != "junos" and mode is None:
            mode = "interface" if line.startswith("interface ") else "path"
        commands.append(line)
    if not commands:
        raise SouthboundError("No supported configuration commands found")
    while commands and commands[-1] == "exit":
        commands.pop()
    return tuple(commands)


def build_push_plan(result: CspfResult, topology: Topology, device: ManagedDevice) -> PushPlan:
    if device.topology_node_id != result.demand.source:
        raise SouthboundError("Inventory device is not the selected LSP head-end")
    output_format = render_format_for_device(device)
    rendered = render_config(result, topology, output_format)
    context = build_render_context(result, topology)
    commands = extract_config_commands(rendered, output_format)
    destination = topology.nodes[result.demand.destination].loopback
    output = result.demand.output
    if output_format == "iosxr":
        identity = output.iosxr_interface
        interface_match = re.fullmatch(r"tunnel-te([0-9]+)", identity)
        assert interface_match is not None  # validated by renderer
        tunnel_id = interface_match.group(1)
        prechecks = (
            "show version",
            f"show running-config interface {identity}",
            f"show running-config explicit-path name {output.explicit_path_name}",
            f"show route {destination}/32",
        )
        postchecks = (f"show mpls traffic-eng tunnels {tunnel_id} detail",)
    elif output_format == "iosxe":
        identity = f"Tunnel{output.tunnel_id}"
        prechecks = (
            "show version",
            f"show running-config interface {identity}",
            f"show running-config | section ^ip explicit-path name {output.explicit_path_name}",
            f"show ip route {destination}",
        )
        postchecks = (f"show mpls traffic-eng tunnels tunnel {output.tunnel_id}",)
    else:
        identity = output.junos_lsp_name
        prechecks = (
            "show version",
            f"show configuration protocols mpls label-switched-path {identity} | display set",
            f"show configuration protocols mpls path {output.explicit_path_name} | display set",
            f"show route {destination}",
        )
        postchecks = (f"show mpls lsp name {identity} extensive",)
    expectation = VerificationExpectation(
        identity,
        destination,
        context["bandwidth_kbps"] * 1000 if output_format != "junos" else context["bandwidth_bps"],
        tuple((hop.hop_type, cast(str, hop.address)) for hop in result.explicit_hops),
    )
    plan = PushPlan(
        device,
        output_format,
        rendered,
        commands,
        prechecks,
        postchecks,
        _topology_revision(topology),
        _digest(asdict(result.demand)),
        _digest(asdict(device)),
        _digest(rendered),
        expectation,
    )
    return replace(plan, plan_id=plan.calculated_id())


def config_diff(previous: str | None, desired: str) -> str:
    return "".join(
        unified_diff(
            (previous or "").splitlines(keepends=True),
            desired.splitlines(keepends=True),
            fromfile="pre-change",
            tofile="desired",
        )
    )


def _validate_plan(plan: PushPlan) -> None:
    if not plan.plan_id or plan.calculated_id() != plan.plan_id:
        raise SouthboundError("Plan identity mismatch: compute and approve a new exact plan")
    if plan.expectation is None or not all(
        (plan.topology_revision, plan.demand_revision, plan.device_revision, plan.config_revision)
    ):
        raise SouthboundError("Plan is missing input revisions or verification expectations")
    if (
        _digest(asdict(plan.device)) != plan.device_revision
        or _digest(plan.rendered_config) != plan.config_revision
    ):
        raise SouthboundError("Plan input revision changed")
    if render_format_for_device(plan.device) != plan.output_format:
        raise SouthboundError("Device platform and rendering format disagree")
    if extract_config_commands(plan.rendered_config, plan.output_format) != plan.commands:
        raise SouthboundError("Plan commands differ from the reviewed rendered configuration")
    read_patterns = {
        "iosxr": (
            r"show version",
            r"show running-config interface tunnel-te[0-9]{1,5}",
            rf"show running-config explicit-path name {IDENTIFIER}",
            rf"show route {IP}/32",
            r"show mpls traffic-eng tunnels [0-9]{1,5} detail",
        ),
        "iosxe": (
            r"show version",
            r"show running-config interface Tunnel[0-9]{1,5}",
            rf"show running-config \| section \^ip explicit-path name {IDENTIFIER}",
            rf"show ip route {IP}",
            r"show mpls traffic-eng tunnels tunnel [0-9]{1,5}",
        ),
        "junos": (
            r"show version",
            rf"show configuration protocols mpls label-switched-path {IDENTIFIER} \| display set",
            rf"show configuration protocols mpls path {IDENTIFIER} \| display set",
            rf"show route {IP}",
            rf"show mpls lsp name {IDENTIFIER} extensive",
        ),
    }
    for command in (*plan.precheck_commands, *plan.postcheck_commands):
        if not any(re.fullmatch(pattern, command) for pattern in read_patterns[plan.output_format]):
            raise SouthboundError("Unsupported read-only command")


def _redact(text: Any, secrets: tuple[str, ...]) -> str:
    value = str(text)
    for secret in sorted((value for value in secrets if value), key=len, reverse=True):
        value = value.replace(secret, "[REDACTED]")
    return re.sub(
        r"(?im)(\b(?:password|secret|community|token)\s*(?:[:=]\s*|\s+))(\S+)", r"\1[REDACTED]", value
    )


def _connection(
    plan: PushPlan, env: Mapping[str, str], handler: Callable[..., Any] | None, read_timeout: float
) -> tuple[Any, tuple[str, ...]]:
    device = plan.device
    if not device.password_env:
        raise SouthboundError(f"Inventory device {device.name} has no password_env")
    password = env.get(device.password_env)
    if not password:
        raise SouthboundError(f"Required secret environment variable {device.password_env} is not set")
    secrets = (password,)
    if handler is None:
        try:
            from netmiko import ConnectHandler
        except ImportError as exc:
            raise SouthboundError("Netmiko is not installed; install ninja-pce[southbound]") from exc
        handler = ConnectHandler
    try:
        connection = handler(
            device_type=normalize_device_type(device.device_type),
            host=device.host,
            username=device.username,
            password=password,
            port=device.port,
            fast_cli=False,
            conn_timeout=read_timeout,
            auth_timeout=read_timeout,
            banner_timeout=read_timeout,
            timeout=read_timeout,
            blocking_timeout=read_timeout,
            read_timeout_override=read_timeout,
        )
        return connection, secrets
    except Exception as exc:
        raise SouthboundError(f"Could not connect to {device.name}: {_redact(exc, secrets)}") from None


def _disconnect(connection: Any) -> None:
    try:
        connection.disconnect()
    except Exception:
        pass


def _commit_accepted(output: str, output_format: str, *, confirmed: bool) -> bool:
    if re.search(ERROR_PATTERN, output):
        return False
    if re.search(r"^\s*commit complete\s*$", output, re.M | re.I):
        return True
    if output_format == "iosxr":
        # XR normally acknowledges a commit with command echo, timestamp and a
        # returned config prompt instead of the Junos 'commit complete' line.
        command = r"commit confirmed 300" if confirmed else r"commit"
        return bool(
            re.search(rf"^RP/[A-Za-z0-9/]+:{IDENTIFIER}\(config\)#\s*{command}\s*$", output, re.M)
            and re.search(rf"^RP/[A-Za-z0-9/]+:{IDENTIFIER}\(config\)#\s*$", output, re.M)
        )
    return False


def _bounds(verification_attempts: int, read_timeout: float) -> None:
    if type(verification_attempts) is not int or not 1 <= verification_attempts <= 5:
        raise SouthboundError("Verification attempts must be in 1..5")
    if isinstance(read_timeout, bool) or not 0 < read_timeout <= 60:
        raise SouthboundError("Read timeout must be in (0, 60] seconds")
    if verification_attempts * read_timeout > 120:
        raise SouthboundError("Total verification I/O budget must not exceed 120 seconds")


def _checks(
    connection: Any,
    plan: PushPlan,
    version: str,
    verifier: OperationalVerifier,
    attempts: int,
    timeout: float,
    secrets: tuple[str, ...],
) -> tuple[bool, tuple[tuple[str, str], ...], tuple[str, ...]]:
    assert plan.expectation is not None  # validated at every public execution boundary
    errors: tuple[str, ...] = ()
    outputs: dict[str, str] = {}
    for _ in range(attempts):
        outputs = {}
        try:
            for command in plan.postcheck_commands:
                outputs[command] = str(connection.send_command(command, read_timeout=timeout))
            evidence = verifier.verify(plan.output_format, version, outputs, plan.expectation)
            errors = evidence.errors
            if evidence.verified:
                return True, tuple((cmd, _redact(value, secrets)) for cmd, value in outputs.items()), ()
        except Exception as exc:
            errors = (_redact(exc, secrets),)
    return False, tuple((cmd, _redact(value, secrets)) for cmd, value in outputs.items()), errors


def verify_plan(
    plan: PushPlan,
    *,
    environ: Mapping[str, str] | None = None,
    connect_handler: Callable[..., Any] | None = None,
    verifier: OperationalVerifier | None = None,
    verification_attempts: int = 3,
    read_timeout: float = 20,
) -> PushResult:
    """Read-only operational revalidation; never enters configuration mode."""
    _validate_plan(plan)
    _bounds(verification_attempts, read_timeout)
    env = os.environ if environ is None else environ
    connection, secrets = _connection(plan, env, connect_handler, read_timeout)
    try:
        version = str(connection.send_command("show version", read_timeout=read_timeout))
        ok, outputs, errors = _checks(
            connection,
            plan,
            version,
            verifier or CliOperationalVerifier(),
            verification_attempts,
            read_timeout,
            secrets,
        )
        return PushResult(
            plan.device.name,
            plan.device.host,
            0,
            "",
            precheck_output=(("show version", _redact(version, secrets)),),
            postcheck_output=outputs,
            verification_errors=errors,
            status="VERIFIED" if ok else "VERIFICATION_FAILED",
            plan_id=plan.plan_id,
        )
    except Exception as exc:
        return PushResult(
            plan.device.name,
            plan.device.host,
            0,
            "",
            verification_errors=(_redact(exc, secrets),),
            status="VERIFICATION_FAILED",
            plan_id=plan.plan_id,
        )
    finally:
        _disconnect(connection)


def push_plan(
    plan: PushPlan,
    *,
    execute: bool = False,
    approved: bool = False,
    approved_plan_id: str | None = None,
    approved_plan_hash: str | None = None,
    approval_expires_at: datetime | str | None = None,
    environ: Mapping[str, str] | None = None,
    connect_handler: Callable[..., Any] | None = None,
    verifier: OperationalVerifier | None = None,
    verification_attempts: int = 3,
    read_timeout: float = 20,
    pre_write_guard: Callable[[], None] | None = None,
) -> PushResult:
    """Execute an exact approved plan. No device contact is made without approval.

    Writes are never retried. An uncertain write or commit retains reservations
    and requires operator recovery. No existing object is blindly deleted.
    """
    _validate_plan(plan)
    _bounds(verification_attempts, read_timeout)
    if execute is not True or approved is not True:
        raise SouthboundError("Refusing configuration without execute=True and approved=True")
    approval_id = approved_plan_id or approved_plan_hash
    if approval_id != plan.plan_id or (
        approved_plan_id and approved_plan_hash and approved_plan_id != approved_plan_hash
    ):
        raise SouthboundError("Approval does not match the exact plan identity")
    if approval_expires_at is not None:
        try:
            expiry = (
                datetime.fromisoformat(approval_expires_at.replace("Z", "+00:00"))
                if isinstance(approval_expires_at, str)
                else approval_expires_at
            )
            if expiry.tzinfo is None or expiry <= datetime.now(timezone.utc):
                raise ValueError("expired or timezone missing")
        except (AttributeError, TypeError, ValueError) as exc:
            raise SouthboundError("Approval is expired or invalid") from exc
    env = os.environ if environ is None else environ
    if env.get("NINJA_PCE_PREVIEW_ONLY", "").strip().lower() in {"1", "true", "yes", "on"}:
        raise SouthboundError("NINJA_PCE_PREVIEW_ONLY forbids configuration execution")
    connection, secrets = _connection(plan, env, connect_handler, read_timeout)
    prechecks: list[tuple[str, str]] = []
    postchecks: tuple[tuple[str, str], ...] = ()
    snapshot = ""
    output = ""
    commit_output: str | None = None
    commands_sent = 0
    write_started = False
    try:
        for command in plan.precheck_commands:
            value = str(connection.send_command(command, read_timeout=read_timeout))
            if re.search(ERROR_PATTERN, value):
                raise SouthboundError(f"Precheck command rejected: {command}")
            prechecks.append((command, _redact(value, secrets)))
        version = prechecks[0][1]
        if supported_version(plan.output_format, version) is None:
            raise SouthboundError("Unsupported or missing software version; no configuration sent")
        snapshot = "\n".join(value for command, value in prechecks if "config" in command)
        assert plan.expectation is not None
        destination = plan.expectation.destination
        route_output = prechecks[-1][1]
        if not re.search(
            rf"(?:Routing entry for\s+|^\s*){re.escape(destination)}(?:/32)?(?:\s|$)", route_output, re.M
        ) or re.search(r"not found|not in table|unreachable|no route", route_output, re.I):
            raise SouthboundError("Precheck lacks positive destination reachability evidence")
        # Changing a pre-existing object needs an ownership-aware replace adapter.
        # Merging an ERO could retain old trailing hops, so fail closed here.
        if re.search(
            r"(?m)^\s*(?:interface (?:tunnel-te|Tunnel)[0-9]+|(?:ip )?explicit-path name |set protocols mpls (?:path |label-switched-path ))",
            snapshot,
        ):
            raise SouthboundError(
                "Pre-existing TE resource requires ownership-aware manual update; merge could retain stale hops"
            )
        # Recheck immutable authorization directly before the first configuration operation.
        _validate_plan(plan)
        if pre_write_guard is not None:
            pre_write_guard()
        if approval_expires_at is not None and expiry <= datetime.now(timezone.utc):
            raise SouthboundError("Approval expired during prechecks")
        if env.get("NINJA_PCE_PREVIEW_ONLY", "").strip().lower() in {"1", "true", "yes", "on"}:
            raise SouthboundError("NINJA_PCE_PREVIEW_ONLY forbids configuration execution")
        write_started = True
        commands_sent = len(plan.commands)  # attempted, not proof of acceptance
        output = _redact(
            connection.send_config_set(
                list(plan.commands),
                error_pattern=ERROR_PATTERN,
                read_timeout=read_timeout,
                exit_config_mode=plan.output_format == "iosxe",
            ),
            secrets,
        )
        if re.search(ERROR_PATTERN, output):
            raise SouthboundError("Router rejected configuration")
        if plan.output_format == "junos":
            check = str(connection.commit(check=True, read_timeout=read_timeout))
            if not re.search(r"configuration check succeeds", check, re.I) or re.search(ERROR_PATTERN, check):
                raise SouthboundError("Junos commit-check failed or returned unknown output")
            commit_output = _redact(
                connection.commit(confirm=True, confirm_delay=5, read_timeout=read_timeout), secrets
            )
        elif plan.output_format == "iosxr":
            commit_output = _redact(
                connection.commit(confirm=True, confirm_delay=300, read_timeout=read_timeout), secrets
            )
        if plan.output_format != "iosxe" and not _commit_accepted(
            commit_output or "", plan.output_format, confirmed=True
        ):
            raise SouthboundError("Commit was rejected or lacks positive acceptance evidence")
        if plan.output_format != "iosxe":
            connection.exit_config_mode()
        ok, postchecks, errors = _checks(
            connection,
            plan,
            version,
            verifier or CliOperationalVerifier(),
            verification_attempts,
            read_timeout,
            secrets,
        )
        if ok and plan.output_format != "iosxe":
            confirmation = _redact(connection.commit(read_timeout=read_timeout), secrets)
            if not _commit_accepted(confirmation, plan.output_format, confirmed=False):
                raise SouthboundError(
                    "Final commit confirmation rejected or lacks positive acceptance evidence"
                )
            commit_output = (commit_output or "") + "\n" + confirmation
        return PushResult(
            plan.device.name,
            plan.device.host,
            commands_sent,
            output,
            commit_output,
            tuple(prechecks),
            postchecks,
            errors,
            "VERIFIED" if ok else "VERIFICATION_FAILED",
            plan.plan_id,
            snapshot,
            config_diff(snapshot, plan.command_text),
            None
            if ok
            else "Inspect pre-change state and ownership manually; no automatic deletion. Confirmed commit may expire; reobserve before releasing reservations.",
        )
    except Exception as exc:
        if not write_started:
            raise SouthboundError(f"Precheck failed on {plan.device.name}: {_redact(exc, secrets)}") from None
        return PushResult(
            plan.device.name,
            plan.device.host,
            commands_sent,
            output,
            commit_output,
            tuple(prechecks),
            postchecks,
            (_redact(exc, secrets),),
            "ROLLBACK_REQUIRED",
            plan.plan_id,
            snapshot,
            config_diff(snapshot, plan.command_text),
            "Configuration outcome is uncertain. Preserve pending reservations; inspect saved pre-change state and resource ownership manually. No blind rollback was attempted.",
        )
    finally:
        _disconnect(connection)


def require_approved_execution(execute: bool, approved: bool) -> None:
    if execute and not approved:
        raise InputError("Refusing execution without --approve")
    if approved and not execute:
        raise InputError("--approve is only valid together with --execute")
