"""Permanent startup faults are actionable without altering existing owners."""
from __future__ import annotations

import errno
import fcntl
import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from importlib.resources import files
from pathlib import Path

import pytest
from test_controller import config_file as config_file
from test_controller import save_config

from ninja_pce import controller, startup
from ninja_pce.bgpls import BgplsError, GoBgpTopologyProvider
from ninja_pce.cli import main
from ninja_pce.state import load_state, write_state


def free_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def test_bind_probe_releases_the_port_and_never_listens():
    port = free_port()
    startup.require_available_listener("127.0.0.1", port, component="dashboard")
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", port))
        listener.listen()


@pytest.mark.parametrize("component,expected", [("dashboard", "DASHBOARD_PORT_IN_USE"),
                                                ("pcep", "PCEP_PORT_IN_USE")])
def test_real_cli_bind_conflict_is_permanent_and_preserves_foreign_capture(tmp_path, capsys, component, expected):
    capture = tmp_path / "existing-capture.json"
    write_state(capture, {"kind": "passive_pcep_capture", "peers": {}, "events": []})
    original = capture.read_bytes()
    with socket.socket() as occupied:
        occupied.bind(("127.0.0.1", 0))
        occupied.listen()
        port = occupied.getsockname()[1]
        arguments = (["dashboard", "--host", "127.0.0.1", "--port", str(port)] if component == "dashboard" else
                     ["pcep-listen", "--listen-host", "127.0.0.1", "--port", str(port),
                      "--allow-peer", "192.0.2.1", "--capture", str(capture)])
        assert main(arguments) == 78
        finding = json.loads(capsys.readouterr().err)
        assert finding["code"] == expected
        assert any(owner["pid"] == os.getpid() for owner in finding["owners"])
        assert capture.read_bytes() == original
        assert occupied.fileno() >= 0


def test_controller_run_configuration_errors_use_78_but_static_validation_retains_1(config_file, capsys):
    path, document = config_file
    document["version"] = 99
    save_config(config_file)
    before = {file: file.read_bytes() for file in path.parent.rglob("*") if file.is_file()}
    assert main(["controller", "run", "--config", str(path)]) == 78
    assert json.loads(capsys.readouterr().err)["code"] == "CONTROLLER_CONFIGURATION_INVALID"
    assert main(["controller", "validate", "--config", str(path)]) == 1
    assert "ERROR:" in capsys.readouterr().err
    assert {file: file.read_bytes() for file in before} == before


def test_opt_in_managed_startup_retains_legacy_standalone_validation_exit(capsys):
    assert main(["dashboard", "--refresh", "0"]) == 1
    capsys.readouterr()
    assert main(["dashboard", "--refresh", "0", "--managed-startup"]) == 78
    assert json.loads(capsys.readouterr().err)["code"] == "STARTUP_CONFIGURATION_INVALID"


def test_managed_missing_client_is_permanent_before_collector_outputs(tmp_path, capsys):
    arguments = ["bgpls-collect", "--managed-startup", "--grpcurl", "nonexistent-ninja-pce-test-client",
                 "--proto", str(files("ninja_pce").joinpath("data/gobgp-readonly.proto")), "--peer", "192.0.2.1",
                 "--output", str(tmp_path / "topology.json"), "--status", str(tmp_path / "status.json"), "--once"]
    assert main(arguments) == 78
    assert json.loads(capsys.readouterr().err)["code"] == "BGPLS_CLIENT_MISSING"
    assert not list(tmp_path.iterdir())


def test_unavailable_rpc_remains_transient_with_managed_startup(tmp_path, capsys, monkeypatch):
    def unreachable(self):
        raise BgplsError("Synthetic BGP-LS source unavailable")

    monkeypatch.setattr(GoBgpTopologyProvider, "capture", unreachable)
    status = tmp_path / "status.json"
    assert main(["bgpls-collect", "--managed-startup", "--grpcurl", sys.executable,
                 "--proto", str(files("ninja_pce").joinpath("data/gobgp-readonly.proto")), "--peer", "192.0.2.1",
                 "--output", str(tmp_path / "topology.json"), "--status", str(status), "--once"]) == 1
    assert load_state(status)["status"] == "unavailable"
    assert "source unavailable" in capsys.readouterr().err


@pytest.mark.parametrize("error_number", [errno.EAGAIN, errno.ENOMEM, errno.ECONNREFUSED, errno.ETIMEDOUT])
def test_transient_os_errors_do_not_get_permanent_exit_classification(error_number):
    assert startup.permanent_os_error(OSError(error_number, "transient")) is False


def test_inspection_reads_only_kernel_metadata_even_with_real_listener(capsys, monkeypatch):
    with socket.socket() as occupied:
        occupied.bind(("127.0.0.1", 0))
        occupied.listen()
        port = occupied.getsockname()[1]
        monkeypatch.setattr(socket, "socket", lambda *a, **kw: pytest.fail("Inspection opened a socket"))
        assert main(["runtime", "inspect-listener", "--address", "127.0.0.1", "--port", str(port)]) == 1
        finding = json.loads(capsys.readouterr().out)
        assert finding["code"] == "PORT_CONFLICT"
        assert any(owner["pid"] == os.getpid() for owner in finding["owners"])


def stop(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=3)
    if process.stdout:
        process.stdout.close()
    if process.stderr:
        process.stderr.close()


def wait_for_finding(process, origin, code):
    client = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        assert process.poll() is None, "Management exited with the blocked protocol component"
        try:
            with client.open(origin + "/api/controller", timeout=1) as response:
                snapshot = json.load(response)
            if any(finding["code"] == code for finding in snapshot["readiness"]["startup_findings"]):
                with client.open(origin + "/healthz", timeout=1) as response:
                    assert response.status == 200
                return snapshot
        except (OSError, urllib.error.URLError):
            pass
        time.sleep(.02)
    pytest.fail(f"Startup finding {code} was not exposed by the still-running dashboard")


def test_protocol_port_conflict_preserves_gui_owner_and_capture(config_file):
    path, document = config_file
    with socket.socket() as occupied:
        occupied.bind(("127.0.0.1", 0))
        occupied.listen()
        protocol_port = occupied.getsockname()[1]
        dashboard_port = free_port()
        while dashboard_port == protocol_port:
            dashboard_port = free_port()
        document["dashboard"]["port"] = dashboard_port
        document["pcep"].update(listen_host="127.0.0.1", port=protocol_port)
        document["bgpls"]["grpcurl"] = str(Path(sys.executable).resolve())
        config = controller.load_controller_config(save_config(config_file))
        capture = Path(config.document["files"]["pcep_capture"])
        capture.parent.mkdir()
        capture.write_bytes(b"existing foreign controller capture")
        marker = path.parent / "state/ownership.json"
        marker.parent.mkdir()
        marker.write_bytes(b"durable LSP ownership and recovery")
        process = subprocess.Popen([sys.executable, "-m", "ninja_pce.cli", "controller", "run", "--config", str(path)],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        try:
            snapshot = wait_for_finding(process, f"http://127.0.0.1:{dashboard_port}", "PCEP_PORT_IN_USE")
            finding = snapshot["readiness"]["startup_findings"][0]
            assert any(owner["pid"] == os.getpid() for owner in finding["owners"])
            assert not snapshot["readiness"]["ready"]
            record = load_state(controller._runtime_path(config, "pcep"))
            assert record["state"] == "failed" and record["returncode"] == 78
            assert not record["child"]
            assert occupied.fileno() >= 0
            assert capture.read_bytes() == b"existing foreign controller capture"
            assert marker.read_bytes() == b"durable LSP ownership and recovery"
            process.terminate()
            assert process.wait(timeout=8) == 0
        finally:
            stop(process)


def test_duplicate_component_lock_preserves_original_owner_record_and_gui(config_file):
    path, document = config_file
    document["operating_mode"] = "observe"
    document["dashboard"]["port"] = free_port()
    document["bgpls"]["grpcurl"] = str(Path(sys.executable).resolve())
    config = controller.load_controller_config(save_config(config_file))
    directory = Path(config.document["runtime_dir"])
    directory.mkdir(parents=True)
    lock = directory / ".ninja-pce-controller-bgpls.lock"
    owner_record = controller._runtime_path(config, "bgpls")
    identity = {"pid": os.getpid(), "start_ticks": controller._process_identity(os.getpid())}
    write_state(owner_record, {"kind": "controller_component", "component": "bgpls",
                              "config_digest": config.config_digest, "state": "running",
                              "supervisor": identity, "child": identity})
    original = owner_record.read_bytes()
    with lock.open("a+b") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        process = subprocess.Popen([sys.executable, "-m", "ninja_pce.cli", "controller", "run", "--config", str(path)],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        try:
            wait_for_finding(process, f"http://127.0.0.1:{document['dashboard']['port']}",
                             "COMPONENT_ALREADY_SUPERVISED")
            assert owner_record.read_bytes() == original
            assert load_state(controller._startup_fault_path(config, "bgpls"))["kind"] == "controller_startup_fault"
            process.terminate()
            assert process.wait(timeout=8) == 0
            assert owner_record.read_bytes() == original
            assert controller._runtime_status(config, "bgpls")["state"] == "running"
        finally:
            stop(process)


def test_permanent_dashboard_child_exit_propagates_78(config_file):
    import asyncio

    path, _ = config_file
    config = controller.load_controller_config(path)
    result = asyncio.run(controller._supervise(
        config, {"dashboard": [sys.executable, "-c", "raise SystemExit(78)"]}))
    assert result == 78
