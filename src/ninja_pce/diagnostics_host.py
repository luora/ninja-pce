"""Bounded, read-only observations of the managed deployment host.

Findings describe the calling process's view. They do not certify that a
service user has the same permissions, or authenticate a copied release using
an independent trust root. Live commands only read whitelisted metadata.
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import platform
import re
import selectors
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

_OS_RELEASE = (Path("/etc/os-release"), Path("/usr/lib/os-release"))
_MEMINFO = Path("/proc/meminfo")
_MAX_PATHS = 32
_MAX_ADDRESSES = 32
_MAX_METADATA = 1024 * 1024
_MAX_BINARY = 128 * 1024 * 1024
_COMMAND_BUDGET = 2.0
_UNITS = frozenset({"ninja-controller.service", "ninja-gobgpd.service", "ninja-admin.service"})
_PROPERTIES = ("LoadState", "ActiveState", "SubState", "Result", "NRestarts", "UnitFileState",
               "ExecMainStatus", "MainPID")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")


def _finding(code: str, severity: str, category: str, message: str,
             evidence: dict[str, Any], remediation: str) -> dict[str, Any]:
    return {"code": code, "severity": severity, "category": category,
            "message": message, "evidence": evidence, "remediation": remediation}


def _display(path: Path | str) -> str:
    return str(path)[:512]


def _read(path: Path, maximum: int) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > maximum:
            raise ValueError("metadata is not a bounded ordinary file")
        contents = stream.read(maximum + 1)
    if len(contents) > maximum:
        raise ValueError("metadata exceeds its size limit")
    return contents


def _document(path: Path) -> tuple[bytes, dict[str, Any]]:
    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate metadata key")
            result[key] = value
        return result

    def finite(value: str) -> None:
        raise ValueError("nonfinite metadata number")

    contents = _read(path, _MAX_METADATA)
    document = json.loads(contents, object_pairs_hook=unique, parse_constant=finite)
    if not isinstance(document, dict):
        raise ValueError("metadata must be an object")
    return contents, document


def _ordinary_path(path: Path) -> os.stat_result:
    """Reject symlinks in every component without opening any configured file."""
    absolute = path.absolute()
    current = Path(absolute.anchor)
    info = current.lstat()
    for part in absolute.parts[1:]:
        if part in {".", ".."}:
            raise ValueError("path contains a traversal component")
        current /= part
        info = current.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise ValueError("path contains a symbolic link")
    if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
        raise ValueError("path is not an ordinary file or directory")
    return info


def _executable(name: str) -> Path | None:
    if not name or len(name) > 4096 or "\x00" in name:
        return None
    candidate = shutil.which(name)
    if candidate is None:
        return None
    try:
        path = Path(candidate).resolve(strict=True)
        info = path.stat()
        return path if stat.S_ISREG(info.st_mode) and os.access(path, os.X_OK) else None
    except (OSError, ValueError, RuntimeError):
        return None


def _run(argv: list[str], *, timeout: float, maximum: int = 65536) -> tuple[bytes, str | None]:
    """Drain a capped pipe; timeout cleanup concerns only this owned child."""
    if timeout <= 0:
        return b"", "budget_exhausted"
    process: subprocess.Popen[bytes] | None = None
    collected = bytearray()
    deadline = time.monotonic() + min(timeout, _COMMAND_BUDGET)
    error: str | None = None
    try:
        process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, close_fds=True)
        assert process.stdout is not None
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    error = "timeout"
                    break
                for key, _event in selector.select(remaining):
                    chunk = os.read(key.fd, min(8192, maximum + 1 - len(collected)))
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    collected.extend(chunk)
                    if len(collected) > maximum:
                        error = "output_limit"
                        break
                if error:
                    break
        if error is None:
            try:
                status = process.wait(timeout=max(0.001, deadline - time.monotonic()))
                if status:
                    error = "command_failed"
            except subprocess.TimeoutExpired:
                error = "timeout"
    except (OSError, ValueError):
        error = "command_unavailable"
    finally:
        if process is not None:
            if process.poll() is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
            try:
                process.wait(timeout=0.2)
            except subprocess.TimeoutExpired:
                pass
            if process.stdout is not None:
                process.stdout.close()
    return (bytes(collected) if error is None else b""), error


def _profile_finding() -> dict[str, Any]:
    release: dict[str, str] = {}
    for path in _OS_RELEASE:
        try:
            for line in _read(path, 65536).decode("utf-8").splitlines():
                key, separator, value = line.partition("=")
                if separator and key in {"ID", "VERSION_ID"}:
                    release[key] = value.strip().strip("\"'")[:64]
            break
        except (OSError, ValueError, UnicodeError):
            continue
    evidence = {"system": sys.platform[:64], "architecture": platform.machine()[:64],
                "distribution": release.get("ID"), "distribution_version": release.get("VERSION_ID"),
                "python_implementation": platform.python_implementation()[:64],
                "python_version": f"{sys.version_info[0]}.{sys.version_info[1]}"}
    qualified = (evidence["system"] == "linux" and evidence["architecture"] in {"x86_64", "amd64"}
                 and evidence["distribution"] == "ubuntu" and evidence["distribution_version"] == "24.04"
                 and evidence["python_implementation"] == "CPython" and evidence["python_version"] == "3.12")
    return _finding("HOST_PROFILE_QUALIFIED" if qualified else "HOST_PROFILE_UNQUALIFIED",
                    "ok" if qualified else ("unknown" if not release else "warning"), "host",
                    "Host matches the qualified offline deployment profile." if qualified else
                    "The qualified offline profile is Ubuntu 24.04 amd64 with CPython 3.12; legacy use remains possible.",
                    evidence, "Use the qualified host profile for managed offline deployment.")


def _memory_finding() -> dict[str, Any]:
    values: dict[str, int] = {}
    try:
        for line in _read(_MEMINFO, 65536).decode("ascii").splitlines():
            match = re.fullmatch(r"(MemTotal|MemAvailable):\s+(\d{1,16})\s+kB", line)
            if match:
                values[match[1]] = int(match[2]) * 1024
        total, available = values["MemTotal"], values["MemAvailable"]
        if total <= 0 or not 0 <= available <= total:
            raise ValueError("invalid memory counters")
    except (OSError, ValueError, KeyError, UnicodeError):
        return _finding("HOST_MEMORY_UNKNOWN", "unknown", "host", "Memory availability could not be read.",
                        {}, "Read Linux MemTotal and MemAvailable counters on the deployment host.")
    low = available < 256 * 1024 * 1024
    return _finding("HOST_MEMORY_LOW" if low else "HOST_MEMORY_AVAILABLE", "warning" if low else "info",
                    "host", "Available memory is below 256 MiB." if low else "Host memory counters are available.",
                    {"total_bytes": total, "available_bytes": available, "warning_threshold_bytes": 268435456},
                    "Review memory pressure before starting managed services.")


def _path_findings(path: Path) -> list[dict[str, Any]]:
    evidence: dict[str, Any] = {"path": _display(path), "permission_scope": "calling_process"}
    try:
        info = _ordinary_path(path)
    except FileNotFoundError:
        return [_finding("HOST_PATH_MISSING", "warning", "persistence", "Configured path does not exist.",
                         evidence, "Prepare the configured path during installation; diagnostics create nothing.")]
    except (OSError, ValueError):
        return [_finding("HOST_PATH_UNSAFE_OR_UNREADABLE", "blocker", "persistence",
                         "Configured path cannot be inspected as an ordinary path without symbolic links.",
                         evidence, "Use an accessible ordinary path with no symbolic-link components.")]
    directory = path if stat.S_ISDIR(info.st_mode) else path.parent
    readable = os.access(path, os.R_OK)
    writable = os.access(path, os.W_OK)
    searchable = os.access(directory, os.X_OK)
    evidence.update({"kind": "directory" if stat.S_ISDIR(info.st_mode) else "file",
                     "mode": f"{stat.S_IMODE(info.st_mode):04o}", "readable": readable,
                     "writable": writable, "searchable_parent": searchable})
    findings = [_finding("HOST_PATH_ACCESSIBLE" if readable and searchable else "HOST_PATH_PERMISSION_DENIED",
                         "info" if readable and searchable else "blocker", "persistence",
                         "Configured path permissions were inspected without writing." if readable and searchable
                         else "The calling process cannot read or traverse the configured path.", evidence,
                         "Verify permissions separately for the managed service account.")]
    if stat.S_IMODE(info.st_mode) & 0o022:
        findings.append(_finding("HOST_PATH_WRITABLE_BY_OTHERS", "warning", "security",
                                 "A configured path permits group or other users to write.", evidence,
                                 "Review the intended service account and restrict write access before using this path for protected state."))
    try:
        disk = os.statvfs(directory)
        available = disk.f_bavail * disk.f_frsize
        total = disk.f_blocks * disk.f_frsize
        low = available < 256 * 1024 * 1024
        findings.append(_finding("HOST_DISK_LOW" if low else "HOST_DISK_AVAILABLE",
                                 "warning" if low else "info", "persistence",
                                 "Available disk space is below 256 MiB." if low else
                                 "Filesystem space counters are available.",
                                 {"path": _display(directory), "available_bytes": available,
                                  "total_bytes": total, "warning_threshold_bytes": 268435456},
                                 "Keep sufficient free space for state, journals, and installed releases."))
    except (OSError, ValueError, OverflowError):
        findings.append(_finding("HOST_DISK_UNKNOWN", "unknown", "persistence",
                                 "Filesystem space counters could not be read.", {"path": _display(directory)},
                                 "Inspect the configured filesystem on the deployment host."))
    return findings


def _address_findings(addresses: list[str], *, live: bool, deadline: float) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    requested: list[tuple[str, Any]] = []
    for address in addresses[:_MAX_ADDRESSES]:
        try:
            if not isinstance(address, str) or len(address) > 128 or "%" in address:
                raise ValueError("invalid numeric address")
            parsed = ipaddress.ip_address(address.strip("[]"))
        except ValueError:
            findings.append(_finding("HOST_ADDRESS_INVALID", "blocker", "host",
                                     "Configured local address must be an unscoped numeric IP address.",
                                     {"address": _display(address)}, "Configure an IPv4 or IPv6 local address."))
            continue
        if parsed.is_unspecified:
            findings.append(_finding("HOST_ADDRESS_WILDCARD", "info", "host",
                                     "Wildcard listener addresses do not require one interface assignment.",
                                     {"address": str(parsed)}, "Review intended listener exposure separately."))
        else:
            requested.append((str(parsed), parsed))
    if not requested:
        return findings
    executable = _executable("ip") if live else None
    output, error = (_run([str(executable), "-j", "address", "show"],
                          timeout=max(0, deadline - time.monotonic())) if executable is not None
                     else (b"", "live_checks_disabled" if not live else "command_unavailable"))
    assigned: dict[str, bool] = {}
    if error is None:
        try:
            interfaces = json.loads(output)
            if not isinstance(interfaces, list) or len(interfaces) > 256:
                raise ValueError("invalid interface inventory")
            count = 0
            for interface in interfaces:
                if not isinstance(interface, dict) or not isinstance(interface.get("addr_info", []), list):
                    raise ValueError("invalid address inventory")
                flags = interface.get("flags")
                if not isinstance(flags, list) or len(flags) > 64 or not all(isinstance(flag, str) for flag in flags):
                    raise ValueError("invalid interface flags")
                up = "UP" in flags
                for item in interface.get("addr_info", []):
                    count += 1
                    if count > 4096 or not isinstance(item, dict):
                        raise ValueError("excessive address inventory")
                    local = item.get("local")
                    if isinstance(local, str) and len(local) <= 128:
                        try:
                            canonical = str(ipaddress.ip_address(local))
                            assigned[canonical] = assigned.get(canonical, False) or up
                        except ValueError:
                            continue
        except (ValueError, TypeError, UnicodeError):
            error = "invalid_metadata"
    for address, _parsed in requested:
        if error:
            code, severity = "HOST_ADDRESS_ASSIGNMENT_UNKNOWN", "unknown"
            message = "Local interface assignment was not established."
        elif address not in assigned:
            code, severity = "HOST_ADDRESS_NOT_ASSIGNED", "blocker"
            message = "Configured local address was not found on this host."
        elif not assigned[address]:
            code, severity = "HOST_ADDRESS_INTERFACE_DOWN", "warning"
            message = "Configured local address belongs only to an interface without the UP flag."
        else:
            code, severity = "HOST_ADDRESS_ASSIGNED", "info"
            message = "Configured local address is assigned to an interface with the UP flag."
        findings.append(_finding(code, severity, "host", message,
                                 {"address": address, "source": "ip_address" if not error else error},
                                 "Use an assigned local address; the eventual listener bind remains authoritative."))
    return findings


def _clock_finding(*, live: bool, deadline: float) -> dict[str, Any]:
    executable = _executable("timedatectl") if live else None
    output, error = (_run([str(executable), "show", "--property=NTPSynchronized", "--property=NTP"],
                          timeout=max(0, deadline - time.monotonic())) if executable is not None
                     else (b"", "live_checks_disabled" if not live else "command_unavailable"))
    properties = _parse_properties(output, ("NTPSynchronized", "NTP")) if not error else {}
    synchronized = properties.get("NTPSynchronized")
    if synchronized not in {"yes", "no"}:
        return _finding("HOST_TIME_SYNC_UNKNOWN", "unknown", "host",
                        "System clock synchronization could not be established.", {"source": error or "invalid_metadata"},
                        "Check the host's time synchronization service before interpreting timestamps.")
    return _finding("HOST_TIME_SYNCHRONIZED" if synchronized == "yes" else "HOST_TIME_UNSYNCHRONIZED",
                    "info" if synchronized == "yes" else "warning", "host",
                    "The host reports its system clock synchronized." if synchronized == "yes" else
                    "The host reports its system clock unsynchronized.", properties,
                    "Maintain host time synchronization for freshness and journal timestamps.")


def host_findings(*, paths: list[Path], addresses: list[str], live: bool = False) -> list[dict[str, Any]]:
    """Inspect host metadata, optionally querying local address/clock metadata."""
    deadline = time.monotonic() + _COMMAND_BUDGET
    findings = [_profile_finding(), _memory_finding()]
    for path in paths[:_MAX_PATHS]:
        findings.extend(_path_findings(Path(path)))
    findings.extend(_address_findings(addresses, live=live, deadline=deadline))
    findings.append(_clock_finding(live=live, deadline=deadline))
    if len(paths) > _MAX_PATHS or len(addresses) > _MAX_ADDRESSES:
        findings.append(_finding("HOST_INPUT_LIMIT", "unknown", "host", "Host diagnostic input was capped.",
                                 {"paths_checked": min(len(paths), _MAX_PATHS),
                                  "addresses_checked": min(len(addresses), _MAX_ADDRESSES)},
                                 "Inspect remaining configured paths and addresses in a separate request."))
    return findings


def _parse_properties(output: bytes, allowed: tuple[str, ...]) -> dict[str, str]:
    result: dict[str, str] = {}
    try:
        for line in output.decode("utf-8").splitlines():
            key, separator, value = line.partition("=")
            if separator and key in allowed:
                if key in result or len(value) > 128 or not re.fullmatch(r"[A-Za-z0-9_.:-]*", value):
                    return {}
                result[key] = value
    except UnicodeError:
        return {}
    return result


def service_findings(units: tuple[str, ...], *, live: bool = False) -> list[dict[str, Any]]:
    """Read a fixed set of systemd properties for managed units only."""
    deadline = time.monotonic() + _COMMAND_BUDGET
    findings: list[dict[str, Any]] = []
    executable = _executable("systemctl") if live else None
    for unit in tuple(dict.fromkeys(units[:8])):
        if unit not in _UNITS:
            findings.append(_finding("SERVICE_UNIT_NOT_ALLOWED", "unknown", "runtime",
                                     "Service inspection accepts only managed deployment units.",
                                     {"unit": _display(unit)}, "Use a managed controller, GoBGP, or admin unit name."))
            continue
        output, error = (_run([str(executable), "--no-pager", "show", "--property=" + ",".join(_PROPERTIES),
                               "--", unit], timeout=max(0, deadline - time.monotonic()))
                         if executable is not None else
                         (b"", "live_checks_disabled" if not live else "command_unavailable"))
        properties = _parse_properties(output, _PROPERTIES) if not error else {}
        evidence: dict[str, Any] = {"unit": unit, **properties}
        if error or not {"LoadState", "ActiveState", "SubState", "Result", "MainPID"} <= properties.keys():
            code, severity, message = "SERVICE_STATE_UNKNOWN", "unknown", "Managed service state could not be established."
            evidence["source"] = error or "incomplete_metadata"
        elif properties["LoadState"] != "loaded":
            code, severity, message = "SERVICE_NOT_LOADED", "warning", "Managed service is not loaded by systemd."
        elif properties["ActiveState"] == "failed" or properties["Result"] not in {"", "success"}:
            code, severity, message = "SERVICE_FAILED", "blocker", "Managed service reports a failure or start limit."
        elif properties["ActiveState"] == "active" and properties["SubState"] == "running":
            pid = properties["MainPID"]
            if pid.isascii() and pid.isdecimal() and 0 < int(pid) <= 2147483647:
                code, severity = "SERVICE_RUNNING", "info"
                message = "Systemd reports the managed service running; protocol readiness is assessed separately."
            else:
                code, severity, message = "SERVICE_STATE_UNKNOWN", "unknown", "Running state lacks a valid main process ID."
        else:
            code, severity, message = "SERVICE_NOT_RUNNING", "warning", "Managed service is not currently running."
        findings.append(_finding(code, severity, "runtime", message, evidence,
                                 "Review the managed service's structured startup findings and repair its configuration; "
                                 "service activation and start-limit resets are operator actions."))
        if properties.get("LoadState") == "loaded":
            enabled = properties.get("UnitFileState")
            boot_enabled = enabled in {"enabled", "enabled-runtime", "linked", "linked-runtime"}
            findings.append(_finding("SERVICE_BOOT_ENABLED" if enabled == "enabled" else "SERVICE_BOOT_START_UNCONFIRMED",
                                     "info" if enabled == "enabled" else "warning" if enabled in {"disabled", "masked", "masked-runtime"} else "unknown",
                                     "persistence", "The managed unit is enabled for boot." if enabled == "enabled" else
                                     "Persistent boot activation of the managed unit is not established.",
                                     {"unit": unit, "unit_file_state": enabled, "runtime_or_linked": boot_enabled and enabled != "enabled"},
                                     "Review the intended installation policy before explicitly enabling a managed unit."))
    if len(units) > 8:
        findings.append(_finding("SERVICE_INPUT_LIMIT", "unknown", "runtime", "Service diagnostic input was capped.",
                                 {"maximum_inputs": 8}, "Inspect only the three managed deployment units."))
    return findings


def _hash(path: Path) -> str:
    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size > _MAX_BINARY:
            raise ValueError("binary exceeds the bounded ordinary-file profile")
        digest = hashlib.sha256()
        length = 0
        while True:
            chunk = stream.read(min(1024 * 1024, _MAX_BINARY + 1 - length))
            if not chunk:
                break
            length += len(chunk)
            if length > _MAX_BINARY:
                raise ValueError("binary exceeds hash limit")
            digest.update(chunk)
        after = os.fstat(stream.fileno())
        fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
        if any(getattr(before, field) != getattr(after, field) for field in fields) or length != before.st_size:
            raise ValueError("binary changed during inspection")
    return digest.hexdigest()


def _qualified_provenance(component: str, data: dict[str, Any]) -> bool:
    common = {"schema_version": 1, "component": component, "target_os": "linux", "target_arch": "amd64"}
    if type(data.get("schema_version")) is not int or any(data.get(key) != value for key, value in common.items()):
        return False
    if component == "grpcurl":
        return (data.get("version") == "1.9.3" and data.get("source_url") ==
                "https://github.com/fullstorydev/grpcurl/releases/download/v1.9.3/grpcurl_1.9.3_linux_x86_64.tar.gz"
                and isinstance(data.get("archive_sha256"), str) and bool(_DIGEST.fullmatch(data["archive_sha256"])))
    commit = "9bc8e113e4de6b5542cfafe0c64403b129f4e544"
    pins = {"gobgp_version": "3.37.0", "gobgp_commit": commit,
            "source_archive_sha256": "354f4b46f94d08c6f5f064db319a4b029b60e410955d89e96e8ea5552d7aa58a",
            "patch_file": "0001-bgpls-node-canonical-identity.patch",
            "patch_sha256": "ff833d21abe13098a87be92a960ec3b018359748b686691527b70af5e696ce10",
            "go_version": "go1.24.1",
            "go_toolchain_sha256": "cb2396bae64183cdccf81a9a6df0aea3bce9511fc21469fb89a0c00470088073",
            "binary_version": f"3.37.0-ninja-node-identity.1+commit.{commit}"}
    tests = data.get("tests")
    return (all(data.get(key) == value for key, value in pins.items()) and data.get("cgo_enabled") is False
            and isinstance(tests, dict) and all(tests.get(name) == "passed" for name in
                                               ("negative_control", "packet", "table", "module_verify")))


def _artifact_findings(component: str, configured: str, release: Path | None,
                       inventory: dict[str, Any] | None) -> list[dict[str, Any]]:
    prefix = component.upper()
    path = _executable(configured)
    if path is None:
        return [_finding(f"{prefix}_BINARY_UNAVAILABLE", "blocker", "runtime",
                         f"Configured {component} executable is unavailable or not an ordinary executable file.",
                         {"component": component}, "Install the verified managed executable or correct its configured path.")]
    try:
        info = path.stat()
    except OSError:
        return [_finding(f"{prefix}_BINARY_UNAVAILABLE", "blocker", "runtime",
                         f"Configured {component} executable disappeared during inspection.",
                         {"component": component}, "Repair the managed release and repeat diagnostics.")]
    findings = [_finding(f"{prefix}_BINARY_AVAILABLE", "info", "runtime",
                         f"Configured {component} executable is present; it was not executed.",
                         {"component": component, "path": _display(path), "size_bytes": info.st_size},
                         "Use the verified offline release to qualify native dependency provenance.")]
    if release is None:
        findings.append(_finding(f"{prefix}_PROVENANCE_UNKNOWN", "unknown", "security",
                                 "External executable provenance was not established.", {"component": component},
                                 "Use a verified release with its recorded native provenance."))
        return findings
    try:
        provenance_contents, provenance = _document(release / "provenance" / f"{component}.json")
        expected = provenance.get("binary_sha256")
        if not isinstance(expected, str) or not _DIGEST.fullmatch(expected):
            raise ValueError("provenance lacks a checksum")
        observed = _hash(path)
    except (OSError, ValueError, RecursionError, UnicodeError):
        findings.append(_finding(f"{prefix}_PROVENANCE_UNKNOWN", "unknown", "security",
                                 "Native provenance or its bounded binary checksum could not be inspected.",
                                 {"component": component, "maximum_hash_bytes": _MAX_BINARY},
                                 "Restore ordinary provenance files and a binary within the inspection size limit."))
        return findings
    match = observed == expected
    qualified = _qualified_provenance(component, provenance)
    findings.append(_finding(f"{prefix}_PROVENANCE_HASH_MATCH" if match else f"{prefix}_PROVENANCE_HASH_MISMATCH",
                             "info" if match else "blocker", "security",
                             "Binary matches its recorded local provenance; independent authentication remains unknown."
                             if match else "Binary differs from the checksum in its recorded local provenance.",
                             {"component": component, "sha256": observed, "recorded_sha256": expected,
                              "qualified_profile_matches": qualified},
                             "Authenticate the offline release using the separately supplied operator trust key."))
    if match and not qualified:
        findings.append(_finding(f"{prefix}_PROFILE_UNQUALIFIED", "warning", "security",
                                 "Local provenance does not describe the qualified native dependency profile.",
                                 {"component": component}, "Install the qualified release and its native build evidence."))
    if inventory is not None:
        artifacts = inventory.get("artifacts")
        components = inventory.get("components")
        expected_component = components.get(component) if isinstance(components, dict) else None
        inventory_match = (isinstance(artifacts, dict) and isinstance(expected_component, dict)
                           and expected_component.get("path") == f"bin/{component}"
                           and expected_component.get("provenance") == f"provenance/{component}.json"
                           and artifacts.get(f"bin/{component}") == observed
                           and artifacts.get(f"provenance/{component}.json") ==
                           hashlib.sha256(provenance_contents).hexdigest())
        findings.append(_finding(f"{prefix}_INVENTORY_HASH_MATCH" if inventory_match else
                                 f"{prefix}_INVENTORY_HASH_MISMATCH", "info" if inventory_match else "blocker",
                                 "security", "Binary and provenance match the local release inventory." if inventory_match
                                 else "Binary or provenance differs from the local release inventory.",
                                 {"component": component, "independent_authentication": "unknown"},
                                 "Verify the release signature with the independently supplied operator trust key."))
    return findings


def artifact_findings(*, grpcurl: str, gobgpd: str | None = None,
                      release_dir: Path | None = None) -> list[dict[str, Any]]:
    """Observe executable metadata and local hashes without running native code.

    A matching inventory is evidence of local consistency, not a fresh signature
    verification. This API deliberately has no implicit trust-key discovery.
    """
    findings: list[dict[str, Any]] = []
    inventory: dict[str, Any] | None = None
    release = Path(release_dir) if release_dir is not None else None
    if release is not None:
        signature_present = False
        manifest_link_matches: bool | None = None
        try:
            info = _ordinary_path(release)
            if not stat.S_ISDIR(info.st_mode):
                raise ValueError("release path is not a directory")
            _ordinary_path(release / "provenance")
            contents, inventory = _document(release / "provenance" / "distribution-manifest.json")
            try:
                signature_present = bool(_read(release / "provenance" / "distribution-manifest.sig", 65536))
            except (OSError, ValueError):
                pass
            try:
                _deployment_contents, deployment = _document(release / "deployment-manifest.json")
                manifest_link_matches = deployment.get("bundle_manifest_sha256") == hashlib.sha256(contents).hexdigest()
            except (OSError, ValueError, RecursionError, UnicodeError):
                pass
        except (OSError, ValueError, RecursionError, UnicodeError):
            inventory = None
            findings.append(_finding("RELEASE_INVENTORY_UNKNOWN", "unknown", "security",
                                     "Release inventory could not be read as bounded ordinary metadata.",
                                     {"release": _display(release)}, "Restore the installed release's provenance metadata."))
            # An unsafe release path must not be followed by the per-artifact reader.
            try:
                _ordinary_path(release)
                _ordinary_path(release / "provenance")
            except (OSError, ValueError):
                release = None
        if manifest_link_matches is False:
            findings.append(_finding("RELEASE_MANIFEST_LINK_MISMATCH", "blocker", "security",
                                     "Installed deployment metadata differs from its recorded bundle manifest checksum.",
                                     {}, "Repair the installed release using a verified offline bundle."))
        findings.append(_finding("RELEASE_SIGNATURE_AUTHENTICATION_UNKNOWN", "unknown", "security",
                                 "Diagnostics have not independently authenticated the release signature.",
                                 {"signature_present": signature_present,
                                  "deployment_manifest_link_matches": manifest_link_matches,
                                  "independent_trust_root_checked": False},
                                 "Use the offline verifier with a separately supplied trusted public key."))
    findings.extend(_artifact_findings("grpcurl", grpcurl, release, inventory))
    if gobgpd is not None:
        findings.extend(_artifact_findings("gobgpd", gobgpd, release, inventory))
    return findings
