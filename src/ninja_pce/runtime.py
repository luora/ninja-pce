"""Read-only listener diagnostics and the managed, single-child GoBGP launcher."""
from __future__ import annotations

import argparse
import errno
import importlib
import ipaddress
import json
import os
import signal
import stat
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, NoReturn

_CONFIG_LIMIT = 1024 * 1024
_LOG_LIMIT = 16 * 1024
_DRY_RUN_TIMEOUT = 30.0
_STOP_TIMEOUT = 10.0
_POLL_INTERVAL = 0.05
_NATIVE_FLAGS = ("--pprof-disable", "--metrics-path=", "--config-strict")


def add_runtime_commands(subparsers: Any) -> None:
    runtime = subparsers.add_parser("runtime", help="Inspect listener ownership or run managed GoBGP")
    actions = runtime.add_subparsers(dest="runtime_action", required=True)
    inspect = actions.add_parser("inspect-listener", help="Report TCP listener ownership as JSON")
    inspect.add_argument("--address", required=True, help="Numeric IPv4 or IPv6 bind address")
    inspect.add_argument("--port", required=True, type=int, help="TCP port")
    inspect.add_argument("--service", help="Expected owning systemd service")
    gobgp = actions.add_parser("gobgp", help="Validate listeners and supervise one GoBGP child")
    gobgp.add_argument("--binary", required=True, help="Installed gobgpd executable")
    gobgp.add_argument("--config", required=True, help="GoBGP TOML configuration")
    gobgp.add_argument("--api-address", default="127.0.0.1", help="Numeric gRPC bind address")
    gobgp.add_argument("--api-port", type=int, default=50051, help="gRPC TCP port")
    gobgp.add_argument("--service", default="ninja-gobgpd.service", help="Managed systemd service name")


def runtime_command(args: argparse.Namespace) -> int:
    if args.runtime_action == "inspect-listener":
        from .listeners import inspect_listener

        finding = inspect_listener(args.address, args.port, expected_service=args.service)
        print(json.dumps(finding, sort_keys=True))
        return 0 if finding["code"] in {"PORT_AVAILABLE", "PORT_OWNED_BY_SERVICE"} else 1
    if args.runtime_action == "gobgp":
        return _run_gobgp(args)
    raise ValueError(f"Unknown runtime action: {args.runtime_action}")


def _finding(code: str, message: str, **details: Any) -> dict[str, Any]:
    return {"code": code, "component": "gobgpd", "severity": "blocker", "message": message, **details}


def _invalid(message: str, *, code: str = "GOBGP_CONFIG_INVALID") -> NoReturn:
    from .startup import PermanentStartupError

    raise PermanentStartupError(_finding(code, message))


def _ordinary_path(value: str, *, executable: bool = False) -> Path:
    if not isinstance(value, str) or not value or len(os.fsencode(value)) > 4096:
        _invalid("GoBGP paths must be nonempty ordinary file paths of at most 4096 bytes.")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        _invalid("GoBGP paths must not contain control characters.")
    path = Path(value).absolute()
    try:
        metadata = path.stat()
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("Path is not a regular file")
        if executable and not os.access(path, os.X_OK):
            raise ValueError("Binary is not executable")
    except (OSError, ValueError) as exc:
        _invalid(f"Cannot use GoBGP {'binary' if executable else 'configuration'}: {exc}",
                 code="GOBGP_BINARY_INVALID" if executable else "GOBGP_CONFIG_INVALID")
    return path


def _read_config(path: Path) -> bytes:
    # Nonblocking open plus fstat prevents a FIFO or device from becoming an
    # unbounded read if the operator changes a path during validation.
    descriptor: int | None = None
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > _CONFIG_LIMIT:
            _invalid("GoBGP configuration must be a regular TOML file of at most 1 MiB.")
        content = bytearray()
        while len(content) <= _CONFIG_LIMIT:
            block = os.read(descriptor, min(65536, _CONFIG_LIMIT + 1 - len(content)))
            if not block:
                return bytes(content)
            content.extend(block)
        _invalid("GoBGP configuration exceeds the 1 MiB limit.")
    except OSError as exc:
        _invalid(f"Cannot read GoBGP configuration: {exc}")
    finally:
        if descriptor is not None:
            os.close(descriptor)
    raise AssertionError("Configuration read did not terminate")


def _numeric_address(value: Any, label: str) -> str:
    try:
        if not isinstance(value, str) or "%" in value:
            raise ValueError("Expected an unscoped numeric address")
        return str(ipaddress.ip_address(value))
    except ValueError:
        _invalid(f"{label} must be an unscoped numeric IPv4 or IPv6 address.")
    raise AssertionError("Invalid address was accepted")


def _port(value: Any, label: str, *, disabled: bool = False) -> int:
    if type(value) is not int or not (1 <= value <= 65535 or disabled and value == -1):
        _invalid(f"{label} must be an integer from 1 to 65535" + (" or -1 to disable BGP." if disabled else "."))
    return int(value)


def _secret_values(value: Any) -> set[str]:
    secrets: set[str] = set()
    pending = [value]
    while pending:
        current = pending.pop()
        if isinstance(current, dict):
            for key, item in current.items():
                if any(word in str(key).lower() for word in ("password", "secret", "token", "private-key")):
                    if isinstance(item, str) and item:
                        secrets.add(item)
                pending.append(item)
        elif isinstance(current, list):
            pending.extend(current)
    return secrets


def _configuration(content: bytes, args: argparse.Namespace) -> tuple[list[tuple[str, int]], set[str]]:
    try:
        tomllib = importlib.import_module("tomllib")
    except ModuleNotFoundError:
        _invalid("Managed GoBGP TOML validation requires Python 3.11 or newer.")
    try:
        config = tomllib.loads(content.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, RecursionError):
        # Parser exceptions can contain values from the input; do not disclose
        # configuration credentials in a permanent startup finding.
        _invalid("GoBGP configuration is not valid UTF-8 TOML.")
    global_section = config.get("global")
    global_config = global_section.get("config") if isinstance(global_section, dict) else None
    if not isinstance(global_config, dict):
        _invalid("GoBGP configuration requires [global.config].")
    bgp_port = _port(global_config.get("port", 179), "global.config.port", disabled=True)
    addresses = global_config.get("local-address-list", [])
    if not isinstance(addresses, list) or (bgp_port > 0 and not addresses):
        _invalid("Enabled BGP requires an explicit nonempty global.config.local-address-list.")
    bgp_addresses = [_numeric_address(address, "BGP local address") for address in addresses]
    api_address = _numeric_address(args.api_address, "gRPC API address")
    api_port = _port(args.api_port, "gRPC API port")
    if (not isinstance(args.service, str) or not args.service or len(args.service) > 255
            or any(not (character.isalnum() or character in "_.@:-") for character in args.service)
            or not args.service.endswith(".service")):
        _invalid("Expected service must be an ordinary systemd .service name.")
    endpoints = [(api_address, api_port)]
    if bgp_port > 0:
        endpoints.extend((address, bgp_port) for address in bgp_addresses)
    # A wildcard plus a specific address cannot both be owned independently.
    # Treat the IPv6 wildcard as overlapping IPv4 too: its dual-stack behavior
    # depends on the host and native runtime, so this wrapper requires explicit
    # nonoverlapping binds instead of relying on that platform default.
    for index, (address, port) in enumerate(endpoints):
        parsed = ipaddress.ip_address(address)
        for previous_address, previous_port in endpoints[:index]:
            previous = ipaddress.ip_address(previous_address)
            if port == previous_port and (
                parsed == previous
                or parsed.version == previous.version and (parsed.is_unspecified or previous.is_unspecified)
                or parsed.version == 6 and parsed.is_unspecified
                or previous.version == 6 and previous.is_unspecified
            ):
                _invalid("GoBGP gRPC and BGP listeners must have distinct, nonoverlapping bind addresses and ports.")
    return endpoints, _secret_values(config)


@dataclass
class _LogTail:
    content: bytearray = field(default_factory=bytearray)
    truncated: bool = False

    def drain(self, descriptor: int) -> None:
        try:
            while True:
                block = os.read(descriptor, 8192)
                if not block:
                    return
                self.content.extend(block)
                if len(self.content) > _LOG_LIMIT:
                    del self.content[:-_LOG_LIMIT]
                    self.truncated = True
        except OSError:
            return

    def safe_text(self, secrets: set[str]) -> str:
        value = bytes(self.content).decode("utf-8", errors="replace")
        variants = secrets | {json.dumps(secret, ensure_ascii=ascii_only)[1:-1]
                              for secret in secrets for ascii_only in (False, True)}
        for secret in sorted(variants, key=len, reverse=True):
            value = value.replace(secret, "[REDACTED]")
            # A bounded tail can begin partway through a credential. Mask the
            # suffix too, including short fragments at that truncation edge.
            if self.truncated:
                for length in range(min(len(secret) - 1, len(value)), 0, -1):
                    if value.startswith(secret[-length:]):
                        value = "[REDACTED]" + value[length:]
                        break
        return value


@dataclass
class _ChildResult:
    returncode: int
    stdout: _LogTail
    stderr: _LogTail
    timed_out: bool = False

    def logs(self, secrets: set[str]) -> dict[str, Any]:
        return {"returncode": self.returncode,
                "stdout": self.stdout.safe_text(secrets), "stderr": self.stderr.safe_text(secrets),
                "stdout_truncated": self.stdout.truncated, "stderr_truncated": self.stderr.truncated}


@dataclass
class _Supervisor:
    child: subprocess.Popen[bytes] | None = None
    stopped: bool = False
    stop_signal: int = signal.SIGTERM

    def request_stop(self, signum: int, _frame: Any) -> None:
        self.stopped = True
        self.stop_signal = signum
        if self.child is not None:
            try:
                self.child.send_signal(signum)
            except ProcessLookupError:
                pass

    def run(self, command: list[str], *, timeout: float | None = None) -> _ChildResult:
        if self.stopped:
            return _ChildResult(0, _LogTail(), _LogTail())
        self.child = subprocess.Popen(command, stdin=subprocess.DEVNULL,
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        child = self.child
        assert child.stdout is not None and child.stderr is not None
        stdout, stderr = _LogTail(), _LogTail()
        readers = [threading.Thread(target=tail.drain, args=(stream.fileno(),), daemon=True)
                   for tail, stream in ((stdout, child.stdout), (stderr, child.stderr))]
        for reader in readers:
            reader.start()
        started = time.monotonic()
        terminate_at: float | None = None
        timed_out = False
        try:
            while child.poll() is None:
                now = time.monotonic()
                if terminate_at is None and (self.stopped or timeout is not None and now - started >= timeout):
                    timed_out = not self.stopped
                    try:
                        child.send_signal(self.stop_signal if self.stopped else signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                    terminate_at = now
                elif terminate_at is not None and now - terminate_at >= _STOP_TIMEOUT:
                    try:
                        child.kill()
                    except ProcessLookupError:
                        pass
                time.sleep(_POLL_INTERVAL)
            child.wait()
        finally:
            if child.poll() is None:
                try:
                    child.terminate()
                except ProcessLookupError:
                    pass
                try:
                    child.wait(timeout=_STOP_TIMEOUT)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()
            for reader in readers:
                reader.join(timeout=1.0)
            # Normally both readers finish at EOF. Avoid a blocking close on a
            # stream still held open by an unexpected descendant process.
            for reader, stream in zip(readers, (child.stdout, child.stderr)):
                if not reader.is_alive():
                    stream.close()
            self.child = None
        return _ChildResult(child.returncode, stdout, stderr, timed_out)


def _emit(finding: dict[str, Any]) -> None:
    print(json.dumps(finding, sort_keys=True), file=sys.stderr, flush=True)


def _run_gobgp(args: argparse.Namespace) -> int:
    from .startup import PERMANENT_EXIT, PermanentStartupError, require_available_listener

    supervisor = _Supervisor()
    previous_handlers: dict[int, Any] = {}
    secrets: set[str] = set()
    try:
        binary = _ordinary_path(args.binary, executable=True)
        config_path = _ordinary_path(args.config)
        content = _read_config(config_path)
        endpoints, secrets = _configuration(content, args)

        def check_listeners() -> None:
            for address, port in endpoints:
                require_available_listener(address, port, component="gobgpd", expected_service=args.service)

        check_listeners()
        for signum in (signal.SIGTERM, signal.SIGINT):
            previous_handlers[signum] = signal.signal(signum, supervisor.request_stop)
        result = supervisor.run([str(binary), "--dry-run", "-f", str(config_path), *_NATIVE_FLAGS],
                                timeout=_DRY_RUN_TIMEOUT)
        if supervisor.stopped:
            return 0
        if result.timed_out or result.returncode != 0:
            _emit(_finding("GOBGP_CONFIG_INVALID", "GoBGP configuration validation "
                           + ("timed out." if result.timed_out else "failed."), **result.logs(secrets)))
            return PERMANENT_EXIT
        if _read_config(config_path) != content:
            _invalid("GoBGP configuration changed during validation; restart with a stable configuration.")
        # Validation may take time. Recheck immediately before spawning so an
        # intervening owner is diagnosed without starting an unnecessary child.
        check_listeners()
        if supervisor.stopped:
            return 0
        api_address = _numeric_address(args.api_address, "gRPC API address")
        api_host = f"[{api_address}]" if ":" in api_address else api_address
        result = supervisor.run([str(binary), "-f", str(config_path),
                                 f"--api-hosts={api_host}:{args.api_port}", *_NATIVE_FLAGS])
        if supervisor.stopped:
            return 0
        if result.returncode != 0:
            try:
                check_listeners()
            except PermanentStartupError as exc:
                _emit({**exc.finding, **result.logs(secrets)})
                return PERMANENT_EXIT
        _emit({**_finding("GOBGP_UNEXPECTED_EXIT", "GoBGP exited without an operator stop."),
               "severity": "warning", **result.logs(secrets)})
        return 1
    except PermanentStartupError as exc:
        _emit(exc.finding)
        return PERMANENT_EXIT
    except OSError as exc:
        permanent = exc.errno in {errno.ENOENT, errno.EACCES, errno.ENOEXEC, errno.EISDIR, errno.ENOTDIR}
        _emit({**_finding("GOBGP_BINARY_INVALID" if permanent else "GOBGP_START_FAILED",
                         f"Cannot launch GoBGP: {exc}"), "severity": "blocker" if permanent else "warning"})
        return PERMANENT_EXIT if permanent else 1
    finally:
        for previous_signum, previous in previous_handlers.items():
            signal.signal(previous_signum, previous)
