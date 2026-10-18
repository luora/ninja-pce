"""A collector fences its outputs across lost-supervisor crash windows."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from importlib.resources import files
from pathlib import Path

import pytest

from ninja_pce import bgpls, live
from ninja_pce.admin_store import AdminError, AdminStore, initialize_admin
from ninja_pce.cli import main
from ninja_pce.state import write_state


def arguments(tmp_path, *, client=None):
    return ["bgpls-collect", "--proto", str(files("ninja_pce").joinpath("data/gobgp-readonly.proto")),
            "--peer", "192.0.2.20", "--grpcurl", client or str(Path(sys.executable).resolve()),
            "--output", str(tmp_path / "topology.json"), "--status", str(tmp_path / "status.json"),
            "--raw", str(tmp_path / "raw.json"), "--interval", "60", "--timeout", "1"]


def preserved(tmp_path):
    result = {}
    for name in ("topology.json", "status.json", "raw.json"):
        path = tmp_path / name
        path.write_bytes(("Original owned " + name).encode())
        result[path] = path.read_bytes()
    return result


def assert_unchanged(expected):
    for path, payload in expected.items():
        assert path.read_bytes() == payload


def test_competing_collector_returns_permanent_finding_before_polling_or_mutating(tmp_path, monkeypatch, capsys):
    lock = tmp_path / "writer.lock"
    original = preserved(tmp_path)

    def unexpected_capture(_self):
        pytest.fail("A blocked collector must not poll")

    monkeypatch.setattr(bgpls.GoBgpTopologyProvider, "capture", unexpected_capture)
    with live._collector_ownership(str(lock), []):
        assert main([*arguments(tmp_path), "--ownership-lock", str(lock), "--once"]) == 78
        assert json.loads(capsys.readouterr().err)["code"] == "BGPLS_ALREADY_COLLECTING"
        assert_unchanged(original)


@pytest.mark.parametrize("name", ["topology.json", "status.json", "raw.json", "proto.json", "nodes.json", "areas.json"])
def test_ownership_lock_cannot_alias_any_input_or_output(tmp_path, name, capsys):
    original = preserved(tmp_path)
    proto = tmp_path / "proto.json"
    proto.write_bytes(b"read-only protobuf fixture")
    nodes = tmp_path / "nodes.json"
    nodes.write_text("{}")
    areas = tmp_path / "areas.json"
    areas.write_text(json.dumps({"schema_version": 1, "protocol_id": 3, "instance_id": 0,
        "autonomous_system": 64512, "bgpls_identifier": 0,
        "bindings": [{"node_id": "ABR", "te_router_id": "192.0.2.1", "igp_router_id": "192.0.2.1",
                      "areas": ["0.0.0.0", "0.0.0.1"]}]}))
    original.update({path: path.read_bytes() for path in (proto, nodes, areas)})
    command = arguments(tmp_path)
    command[command.index("--proto") + 1] = str(proto)
    command.extend(["--node-map", str(nodes), "--ospf-multi-area-policy", str(areas),
                    "--ownership-lock", str(tmp_path / name), "--once"])
    assert main(command) == 1
    assert "distinct from every input and output" in capsys.readouterr().err
    assert_unchanged(original)


@pytest.mark.parametrize("replacement", ["public", "fifo", "hardlink", "symlink"])
def test_unsafe_lock_files_are_refused_without_output_mutation(tmp_path, replacement, capsys):
    original = preserved(tmp_path)
    lock = tmp_path / "writer.lock"
    other = tmp_path / "foreign-private-file"
    other.write_bytes(b"foreign content")
    other.chmod(0o600)
    if replacement == "public":
        lock.write_bytes(b"")
        lock.chmod(0o644)
    elif replacement == "fifo":
        os.mkfifo(lock, 0o600)
    elif replacement == "hardlink":
        os.link(other, lock)
    else:
        lock.symlink_to(other)
    assert main([*arguments(tmp_path), "--ownership-lock", str(lock), "--once"]) == 1
    capsys.readouterr()
    assert other.read_bytes() == b"foreign content"
    assert_unchanged(original)


def test_static_validation_and_missing_client_precede_lock_creation(tmp_path, capsys):
    lock = tmp_path / "run/writer.lock"
    command = arguments(tmp_path)
    command[command.index("--interval") + 1] = "0"
    assert main([*command, "--ownership-lock", str(lock), "--once"]) == 1
    assert not lock.parent.exists()
    assert main([*arguments(tmp_path, client="missing-ninja-client-123"), "--ownership-lock", str(lock),
                 "--managed-startup", "--once"]) == 78
    assert not lock.parent.exists()
    capsys.readouterr()


def test_lock_is_private_and_not_inherited_by_rpc_children(tmp_path):
    lock = tmp_path / "run/writer.lock"
    with live._collector_ownership(str(lock), []):
        assert lock.stat().st_mode & 0o777 == 0o600
        program = ("import os,json\nresult=[]\n"
                   "for entry in os.scandir('/proc/self/fd'):\n"
                   " try: result.append(os.readlink(entry.path))\n"
                   " except OSError: pass\n"
                   "print(json.dumps(result))")
        result = subprocess.run([sys.executable, "-c", program], stdout=subprocess.PIPE,
                                check=True, text=True, close_fds=False)
        assert str(lock) not in json.loads(result.stdout)
    with live._collector_ownership(str(lock), []):
        pass


def test_unrecorded_live_collector_blocks_reconciliation_and_second_writer(tmp_path, capsys):
    root, _token = initialize_admin(tmp_path / "admin-workspace")
    client = root / "fake-grpcurl"
    client.write_text(f"#!{sys.executable}\nimport sys\nsys.exit(1)\n")
    client.chmod(0o700)
    lock = root / "data/run/.ninja-pce-bgpls-writer.lock"
    live_dir = root / "data/live"
    command = [*arguments(live_dir, client=str(client)), "--ownership-lock", str(lock)]
    child = subprocess.Popen([sys.executable, "-m", "ninja_pce.cli", *command],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 10
        while not (live_dir / "status.json").exists():
            if child.poll() is not None:
                stdout, stderr = child.communicate()
                pytest.fail(f"Collector exited early: {stdout!r} {stderr!r}")
            if time.monotonic() >= deadline:
                pytest.fail("Collector failed to acquire ownership and publish its first status")
            time.sleep(0.01)
        assert not (root / "data/run/.ninja-pce-controller-bgpls.json").exists()
        original = preserved(live_dir)
        assert main([*command, "--once"]) == 78
        assert json.loads(capsys.readouterr().err)["code"] == "BGPLS_ALREADY_COLLECTING"
        manager = AdminStore(root, grpcurl=str(client))
        try:
            write_state(root / "admin/transaction.json", {"kind": "admin_transaction", "status": "UNCERTAIN",
                        "previous_revision": None, "target_revision": None})
            with pytest.raises(AdminError) as caught:
                manager.reconcile()
            assert caught.value.code == "LIVE_COLLECTOR_REMAINS"
            assert manager.transaction()["status"] == "UNCERTAIN"
            assert child.poll() is None
            assert_unchanged(original)
        finally:
            manager.close()
    finally:
        if child.poll() is None:
            child.terminate()
        try:
            child.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            child.kill()
            child.communicate(timeout=5)


def test_controller_supplies_server_owned_writer_lock(tmp_path):
    from ninja_pce.controller import component_commands, load_controller_config

    root, _token = initialize_admin(tmp_path / "workspace")
    manager = AdminStore(root)
    try:
        from ninja_pce.admin_model import default_setup

        preview = manager.preview(default_setup(), None, "operator")
        path = root / "revisions" / preview["revision"] / "controller.json"
        command = component_commands(load_controller_config(path))["bgpls"]
        assert command[command.index("--ownership-lock") + 1] == str(root / "data/run/.ninja-pce-bgpls-writer.lock")
    finally:
        manager.close()
