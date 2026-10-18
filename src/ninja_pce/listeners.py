"""Read-only Linux TCP listener inspection; findings do not grant port ownership.

The kernel tables establish whether a port is occupied. Process metadata only
explains that finding: PID and descriptor races mean it is never a fencing token
or authorization to stop a process. No sockets or service operations are used.
"""
from __future__ import annotations

import ipaddress
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_MAX_TABLE_BYTES = 8 * 1024 * 1024
_MAX_METADATA_BYTES = 64 * 1024
_MAX_PROCESSES = 8192
_MAX_PROC_ENTRIES = 32768
_MAX_FDS_PER_PROCESS = 4096
_MAX_TOTAL_FDS = 131072

_Address = ipaddress.IPv4Address | ipaddress.IPv6Address


@dataclass(frozen=True)
class _Listener:
    address: _Address
    port: int
    inode: str


def _read_bounded(path: Path, limit: int) -> str:
    with path.open("rb") as handle:
        data = handle.read(limit + 1)
    if len(data) > limit:
        raise ValueError("inspection input exceeds the size limit")
    return data.decode("utf-8")


def _kernel_address(value: str, version: int) -> _Address:
    width = 8 if version == 4 else 32
    if len(value) != width:
        raise ValueError("invalid kernel address")
    # /proc prints each native-endian 32-bit word separately, including for IPv6.
    packed = b"".join(int(value[index:index + 8], 16).to_bytes(4, sys.byteorder)
                      for index in range(0, width, 8))
    return ipaddress.ip_address(packed)


def _read_table(path: Path, version: int) -> tuple[list[_Listener], bool]:
    try:
        content = _read_bounded(path, _MAX_TABLE_BYTES)
    except (OSError, ValueError, UnicodeError):
        return [], False
    lines = content.splitlines()
    if not lines or not {"local_address", "st", "inode"}.issubset(lines[0].split()):
        return [], False
    found: list[_Listener] = []
    complete = True
    for line in lines[1:]:
        if not line.strip():
            continue
        parts = line.split()
        try:
            state = int(parts[3], 16)
            if state != 0x0A:  # TCP_LISTEN
                continue
            raw_address, raw_port = parts[1].split(":")
            address = _kernel_address(raw_address, version)
            port = int(raw_port, 16)
            inode = str(int(parts[9]))
            if not 0 <= port <= 65535 or int(inode) < 0:
                raise ValueError("invalid kernel listener")
        except (ValueError, IndexError, OverflowError):
            complete = False
            continue
        found.append(_Listener(address, port, inode))
    return found, complete


def _overlaps(requested: _Address, bound: _Address) -> bool:
    if isinstance(requested, ipaddress.IPv6Address) and requested.ipv4_mapped is not None:
        requested = requested.ipv4_mapped
    if isinstance(bound, ipaddress.IPv6Address) and bound.ipv4_mapped is not None:
        bound = bound.ipv4_mapped
    if requested.version == bound.version:
        return requested.is_unspecified or bound.is_unspecified or requested == bound
    # /proc does not expose IPV6_V6ONLY. Treat a wildcard IPv6 listener or a
    # requested dual-stack wildcard as overlapping IPv4, conservatively.
    ipv6_address = requested if requested.version == 6 else bound
    return ipv6_address.is_unspecified


def _start_time(process: Path) -> str | None:
    try:
        value = _read_bounded(process / "stat", _MAX_METADATA_BYTES)
        # comm (field 2) may contain spaces and parentheses. starttime is field 22.
        fields = value[value.rindex(")") + 1:].split()
        start = fields[19]
        return start if start.isdecimal() else None
    except (OSError, ValueError, IndexError, UnicodeError):
        return None


def _service(process: Path) -> str | None:
    content = _read_bounded(process / "cgroup", _MAX_METADATA_BYTES)
    units: set[str] = set()
    for line in content.splitlines():
        parts = line.split(":", 2)
        if len(parts) != 3 or not parts[2].startswith("/"):
            raise ValueError("invalid cgroup metadata")
        units.update(part for part in parts[2].split("/") if part.endswith(".service"))
    # Conflicting hierarchies or nested service units are ambiguous.
    return next(iter(units)) if len(units) == 1 else None


def _owners(proc_root: Path, inodes: set[str]) -> tuple[list[dict[str, Any]], str]:
    owners: list[dict[str, Any]] = []
    found_inodes: set[str] = set()
    complete = True
    pids: list[int] = []
    try:
        with os.scandir(proc_root) as entries:
            for count, entry in enumerate(entries):
                if count >= _MAX_PROC_ENTRIES:
                    complete = False
                    break
                if entry.name.isdecimal() and len(entry.name) <= 10:
                    pids.append(int(entry.name))
                    if len(pids) >= _MAX_PROCESSES:
                        complete = False
                        break
    except OSError:
        return [], "unknown"
    total_fds = 0
    for pid in sorted(pids):
        process = proc_root / str(pid)
        before = _start_time(process)
        matched: dict[Path, str] = {}
        try:
            with os.scandir(process / "fd") as descriptors:
                for count, descriptor in enumerate(descriptors):
                    if count >= _MAX_FDS_PER_PROCESS or total_fds >= _MAX_TOTAL_FDS:
                        complete = False
                        break
                    total_fds += 1
                    try:
                        target = os.readlink(descriptor.path)
                    except OSError:
                        complete = False
                        continue
                    if target.startswith("socket:[") and target.endswith("]"):
                        inode = target[8:-1]
                        if inode in inodes:
                            matched[Path(descriptor.path)] = inode
        except OSError:
            complete = False
            continue
        if not matched:
            continue
        executable = None
        service = None
        try:
            executable = os.readlink(process / "exe")
        except OSError:
            # Executable names are explanatory, not necessary for unit membership.
            pass
        try:
            service = _service(process)
        except (OSError, ValueError, UnicodeError):
            complete = False
        still_matched: set[str] = set()
        for descriptor_path, inode in matched.items():
            try:
                if os.readlink(descriptor_path) == f"socket:[{inode}]":
                    still_matched.add(inode)
                else:
                    complete = False
            except OSError:
                complete = False
        after = _start_time(process)
        if before is None or after != before:
            # The PID may have been reused. Retain only unattributed diagnostic
            # PID information, and never use metadata to verify a service.
            executable = None
            service = None
            complete = False
        if still_matched:
            found_inodes.update(still_matched)
            owners.append({"pid": pid, "executable": executable, "service": service})
        if total_fds >= _MAX_TOTAL_FDS:
            complete = False
            break
    if not inodes.issubset(found_inodes):
        complete = False
    return owners, "complete" if complete else "limited"


def inspect_listener(address: str, port: int, expected_service: str | None = None, *,
                     proc_root: Path = Path("/proc")) -> dict[str, Any]:
    """Inspect TCP binding overlap without binding, connecting or changing services.

    Numeric addresses avoid DNS and are interpreted conservatively for IPv6
    wildcards. A confirmed socket remains a conflict when owner visibility is
    incomplete, including with restricted /proc mounts. An expected service is
    trusted only through exact, unambiguous cgroup unit membership for all owners.
    """
    finding: dict[str, Any] = {
        "code": "PORT_INSPECTION_UNAVAILABLE", "severity": "warning", "address": address,
        "port": port, "expected_service": expected_service, "owners": [],
        "owner_visibility": "unknown", "message": "TCP listener inspection is unavailable.",
        "remediation": "Inspect listeners locally with sufficient read access before starting this listener.",
    }
    if sys.platform != "linux":
        finding["message"] = "Read-only TCP listener inspection requires Linux /proc."
        return finding
    try:
        requested = ipaddress.ip_address(address.removeprefix("[").removesuffix("]"))
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise ValueError("invalid port")
    except ValueError:
        finding["message"] = "TCP listener inspection requires a numeric IP address and a port from 1 to 65535."
        return finding
    listeners: list[_Listener] = []
    tables_complete = True
    for table, version in (("tcp", 4), ("tcp6", 6)):
        rows, complete = _read_table(proc_root / "net" / table, version)
        listeners.extend(rows)
        tables_complete = tables_complete and complete
    matching = {row.inode for row in listeners if row.port == port and _overlaps(requested, row.address)}
    if not matching:
        if tables_complete:
            finding.update(code="PORT_AVAILABLE", severity="info", owner_visibility="complete",
                           message="No overlapping TCP listener was observed in the Linux kernel tables.",
                           remediation="The port can be used for startup; the eventual bind remains authoritative.")
        else:
            finding["message"] = "Linux TCP tables could not be read completely; port availability is unknown."
        return finding
    owners, visibility = _owners(proc_root, matching)
    if not tables_complete:
        visibility = "limited"
    finding.update(code="PORT_CONFLICT", severity="blocker", owners=owners,
                   owner_visibility=visibility,
                   message="An overlapping TCP listener already occupies this address and port.",
                   remediation="Review the reported listener and use its managed service workflow, or configure another port. Do not stop an unverified PID.")
    if (expected_service is not None and expected_service.endswith(".service") and
            "/" not in expected_service and visibility == "complete" and owners and
            all(owner["service"] == expected_service for owner in owners)):
        finding.update(code="PORT_OWNED_BY_SERVICE", severity="ok",
                       message="The overlapping TCP listener belongs to the expected systemd service.",
                       remediation="Use the expected service's managed restart workflow before starting another instance.")
    elif visibility != "complete":
        finding["message"] += " Owner visibility is incomplete; service ownership is unverified."
    return finding
