"""Read-only listener diagnostics use bounded kernel and process metadata."""
from __future__ import annotations

import ipaddress
import json
import os
import sys
from pathlib import Path

import pytest

from ninja_pce import listeners

HEADER = "  sl  local_address rem_address st tx_queue rx_queue tr tm->when retrnsmt uid timeout inode\n"


def kernel_address(address: str) -> str:
    packed = ipaddress.ip_address(address).packed
    return "".join(f"{int.from_bytes(packed[index:index + 4], sys.byteorder):08X}"
                   for index in range(0, len(packed), 4))


def row(address: str, port: int = 4189, inode: int = 1234, state: str = "0A") -> str:
    return (f" 0: {kernel_address(address)}:{port:04X} 00000000:0000 {state} "
            f"00000000:00000000 00:00000000 00000000 1000 0 {inode} 1\n")


@pytest.fixture
def proc_root(tmp_path, monkeypatch):
    monkeypatch.setattr(listeners.sys, "platform", "linux")
    path = tmp_path / "proc"
    (path / "net").mkdir(parents=True)
    (path / "net/tcp").write_text(HEADER)
    (path / "net/tcp6").write_text(HEADER)
    return path


def add_listener(proc_root: Path, address: str, port: int = 4189,
                 inode: int = 1234, state: str = "0A") -> None:
    table = proc_root / "net" / ("tcp" if ipaddress.ip_address(address).version == 4 else "tcp6")
    with table.open("a") as handle:
        handle.write(row(address, port, inode, state))


def add_owner(proc_root: Path, pid: int = 4321, inodes: tuple[int, ...] = (1234,), *,
              cgroup: str = "0::/system.slice/ninja-pce.service\n") -> Path:
    process = proc_root / str(pid)
    (process / "fd").mkdir(parents=True)
    for index, inode in enumerate(inodes):
        (process / "fd" / str(index)).symlink_to(f"socket:[{inode}]")
    (process / "exe").symlink_to("/opt/ninja-pce/bin/python3")
    (process / "cgroup").write_text(cgroup)
    # Twenty fields follow comm; field 22 (index 19) is the start time.
    (process / "stat").write_text(f"{pid} (python (worker)) S " + "0 " * 18 + "98765 0\n")
    return process


def inspect(proc_root: Path, address: str = "127.0.0.1", port: int = 4189,
            expected_service: str | None = None):
    return listeners.inspect_listener(address, port, expected_service, proc_root=proc_root)


def test_available_only_after_both_tables_are_read(proc_root):
    result = inspect(proc_root)
    assert result == {
        "code": "PORT_AVAILABLE", "severity": "info", "address": "127.0.0.1", "port": 4189,
        "expected_service": None, "owners": [], "owner_visibility": "complete",
        "message": "No overlapping TCP listener was observed in the Linux kernel tables.",
        "remediation": "The port can be used for startup; the eventual bind remains authoritative.",
    }
    json.dumps(result)


@pytest.mark.parametrize(("requested", "bound"), [
    ("127.0.0.1", "127.0.0.1"), ("127.0.0.1", "0.0.0.0"), ("0.0.0.0", "127.0.0.2"),
    ("::1", "::1"), ("::1", "::"), ("::", "2001:db8::1"),
    ("127.0.0.1", "::"), ("0.0.0.0", "::"), ("::", "127.0.0.1"),
    ("127.0.0.1", "::ffff:127.0.0.1"), ("::ffff:127.0.0.1", "127.0.0.1"),
    ("0.0.0.0", "::ffff:192.0.2.1"), ("[::1]", "::1"),
])
def test_overlapping_listener_is_conflict_even_without_owner(proc_root, requested, bound):
    add_listener(proc_root, bound)
    result = inspect(proc_root, requested)
    assert result["code"] == "PORT_CONFLICT"
    assert result["severity"] == "blocker"
    assert result["owners"] == []
    assert result["owner_visibility"] == "limited"


@pytest.mark.parametrize(("requested", "bound"), [
    ("127.0.0.1", "127.0.0.2"), ("::1", "2001:db8::1"),
    ("::1", "0.0.0.0"), ("127.0.0.1", "2001:db8::1"),
    ("127.0.0.1", "::ffff:127.0.0.2"),
])
def test_disjoint_addresses_do_not_conflict(proc_root, requested, bound):
    add_listener(proc_root, bound)
    assert inspect(proc_root, requested)["code"] == "PORT_AVAILABLE"


def test_other_ports_and_non_listening_sockets_do_not_conflict(proc_root):
    add_listener(proc_root, "127.0.0.1", port=4190)
    add_listener(proc_root, "127.0.0.1", state="01")
    assert inspect(proc_root)["code"] == "PORT_AVAILABLE"


def test_exact_unit_membership_verifies_expected_service(proc_root):
    add_listener(proc_root, "127.0.0.1")
    add_owner(proc_root)
    result = inspect(proc_root, expected_service="ninja-pce.service")
    assert result["code"] == "PORT_OWNED_BY_SERVICE"
    assert result["severity"] == "ok"
    assert result["owner_visibility"] == "complete"
    assert result["owners"] == [{"pid": 4321, "executable": "/opt/ninja-pce/bin/python3",
                                 "service": "ninja-pce.service"}]


@pytest.mark.parametrize("cgroup", [
    "0::/system.slice/other.service\n",
    "0::/system.slice/prefix-ninja-pce.service\n",
    "0::/system.slice/ninja-pce.service-copy\n",
    "0::/user.slice/ninja-pce.service.scope\n",
    "0::/system.slice/ninja-pce.service/other.service\n",
    "0::/system.slice/ninja-pce.service\n1:name=systemd:/system.slice/other.service\n",
    "0::/user.slice/session-1.scope\n",
])
def test_executable_names_and_partial_unit_names_never_verify_service(proc_root, cgroup):
    add_listener(proc_root, "127.0.0.1")
    process = add_owner(proc_root, cgroup=cgroup)
    (process / "exe").unlink()
    (process / "exe").symlink_to("/opt/ninja-pce.service")
    assert inspect(proc_root, expected_service="ninja-pce.service")["code"] == "PORT_CONFLICT"


def test_cgroup_v1_exact_unit_is_supported(proc_root):
    add_listener(proc_root, "127.0.0.1")
    add_owner(proc_root, cgroup="1:name=systemd:/system.slice/ninja-pce.service\n2:cpu:/\n")
    assert inspect(proc_root, expected_service="ninja-pce.service")["code"] == "PORT_OWNED_BY_SERVICE"


def test_all_shared_socket_owners_must_match_the_service(proc_root):
    add_listener(proc_root, "127.0.0.1")
    add_owner(proc_root)
    add_owner(proc_root, pid=5432, cgroup="0::/system.slice/other.service\n")
    result = inspect(proc_root, expected_service="ninja-pce.service")
    assert result["code"] == "PORT_CONFLICT"
    assert result["owner_visibility"] == "complete"
    assert [owner["pid"] for owner in result["owners"]] == [4321, 5432]


def test_every_matching_socket_needs_a_verified_owner(proc_root):
    add_listener(proc_root, "127.0.0.1")
    add_listener(proc_root, "0.0.0.0", inode=4567)
    add_owner(proc_root)
    result = inspect(proc_root, expected_service="ninja-pce.service")
    assert result["code"] == "PORT_CONFLICT"
    assert result["owner_visibility"] == "limited"


@pytest.mark.parametrize("missing", ["cgroup", "stat", "fd"])
def test_missing_owner_metadata_cannot_verify_service(proc_root, missing):
    add_listener(proc_root, "127.0.0.1")
    process = add_owner(proc_root)
    if missing == "fd":
        (process / "fd/0").unlink()
        (process / "fd").rmdir()
    else:
        (process / missing).unlink()
    result = inspect(proc_root, expected_service="ninja-pce.service")
    assert result["code"] == "PORT_CONFLICT"
    assert result["owner_visibility"] == "limited"


def test_executable_visibility_is_not_service_identity(proc_root):
    add_listener(proc_root, "127.0.0.1")
    process = add_owner(proc_root)
    (process / "exe").unlink()
    result = inspect(proc_root, expected_service="ninja-pce.service")
    assert result["code"] == "PORT_OWNED_BY_SERVICE"
    assert result["owners"][0]["executable"] is None


def test_pid_reuse_removes_service_attribution(proc_root, monkeypatch):
    add_listener(proc_root, "127.0.0.1")
    add_owner(proc_root)
    times = iter(("old", "new"))
    monkeypatch.setattr(listeners, "_start_time", lambda _: next(times))
    result = inspect(proc_root, expected_service="ninja-pce.service")
    assert result["code"] == "PORT_CONFLICT"
    assert result["owner_visibility"] == "limited"
    assert result["owners"] == [{"pid": 4321, "executable": None, "service": None}]


def test_fd_race_discards_owner_attribution(proc_root, monkeypatch):
    add_listener(proc_root, "127.0.0.1")
    process = add_owner(proc_root)
    readlink = os.readlink
    reads = 0

    def changing_readlink(path):
        nonlocal reads
        if Path(path) == process / "fd/0":
            reads += 1
            if reads > 1:
                return "socket:[9999]"
        return readlink(path)

    monkeypatch.setattr(listeners.os, "readlink", changing_readlink)
    result = inspect(proc_root, expected_service="ninja-pce.service")
    assert result["code"] == "PORT_CONFLICT"
    assert result["owner_visibility"] == "limited"
    assert result["owners"] == []


@pytest.mark.parametrize("table", ["tcp", "tcp6"])
def test_unreadable_or_missing_kernel_table_never_means_available(proc_root, table):
    (proc_root / "net" / table).unlink()
    result = inspect(proc_root)
    assert result["code"] == "PORT_INSPECTION_UNAVAILABLE"
    assert result["severity"] == "warning"
    assert result["owner_visibility"] == "unknown"


def test_known_conflict_remains_blocking_when_other_table_unreadable(proc_root):
    add_listener(proc_root, "127.0.0.1")
    add_owner(proc_root)
    (proc_root / "net/tcp6").unlink()
    result = inspect(proc_root, expected_service="ninja-pce.service")
    assert result["code"] == "PORT_CONFLICT"
    assert result["owner_visibility"] == "limited"


@pytest.mark.parametrize("content", ["", "garbage\n", HEADER + "bad row\n",
                                    HEADER + row("127.0.0.1").replace("0100007F", "INVALID!")])
def test_malformed_kernel_tables_never_mean_available(proc_root, content):
    (proc_root / "net/tcp").write_text(content)
    assert inspect(proc_root)["code"] == "PORT_INSPECTION_UNAVAILABLE"


def test_size_bound_never_means_available(proc_root, monkeypatch):
    monkeypatch.setattr(listeners, "_MAX_TABLE_BYTES", 10)
    assert inspect(proc_root)["code"] == "PORT_INSPECTION_UNAVAILABLE"


@pytest.mark.parametrize("limit", ["_MAX_PROCESSES", "_MAX_FDS_PER_PROCESS", "_MAX_TOTAL_FDS",
                                 "_MAX_PROC_ENTRIES"])
def test_scan_limits_never_verify_service_ownership(proc_root, monkeypatch, limit):
    add_listener(proc_root, "127.0.0.1")
    add_owner(proc_root)
    monkeypatch.setattr(listeners, limit, 0)
    result = inspect(proc_root, expected_service="ninja-pce.service")
    assert result["code"] == "PORT_CONFLICT"
    assert result["owner_visibility"] == "limited"


def test_permission_error_keeps_known_conflict(proc_root, monkeypatch):
    add_listener(proc_root, "127.0.0.1")
    process = add_owner(proc_root)
    scandir = os.scandir

    def denied_scandir(path):
        if Path(path) == process / "fd":
            raise PermissionError("restricted /proc")
        return scandir(path)

    monkeypatch.setattr(listeners.os, "scandir", denied_scandir)
    result = inspect(proc_root, expected_service="ninja-pce.service")
    assert result["code"] == "PORT_CONFLICT"
    assert result["owner_visibility"] == "limited"


@pytest.mark.parametrize(("address", "port"), [("localhost", 4189), ("", 4189), ("invalid", 4189),
                                              ("127.0.0.1", 0), ("127.0.0.1", 65536),
                                              ("127.0.0.1", True), ("127.0.0.1", "4189")])
def test_invalid_or_non_numeric_endpoint_is_unavailable(proc_root, address, port):
    assert inspect(proc_root, address, port)["code"] == "PORT_INSPECTION_UNAVAILABLE"


def test_non_linux_is_explicitly_unknown(proc_root, monkeypatch):
    monkeypatch.setattr(listeners.sys, "platform", "darwin")
    result = inspect(proc_root)
    assert result["code"] == "PORT_INSPECTION_UNAVAILABLE"
    assert result["owner_visibility"] == "unknown"


def test_inspection_does_not_modify_files_or_read_process_secrets(proc_root, monkeypatch):
    add_listener(proc_root, "127.0.0.1")
    process = add_owner(proc_root)
    (process / "cmdline").write_text("secret command line")
    (process / "environ").write_text("secret environment")
    before = {path: path.read_bytes() for path in proc_root.rglob("*")
              if path.is_file() and not path.is_symlink()}
    bounded_read = listeners._read_bounded

    def safe_read(path, limit):
        assert path.name not in {"cmdline", "environ"}
        return bounded_read(path, limit)

    monkeypatch.setattr(listeners, "_read_bounded", safe_read)
    assert inspect(proc_root, expected_service="ninja-pce.service")["code"] == "PORT_OWNED_BY_SERVICE"
    assert before == {path: path.read_bytes() for path in proc_root.rglob("*")
                      if path.is_file() and not path.is_symlink()}
