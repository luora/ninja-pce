"""Managed GoBGP refuses occupied ports and never retries native startup."""
from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from ninja_pce import runtime, startup
from ninja_pce.cli import main

pytestmark = pytest.mark.skipif(sys.version_info < (3, 11), reason="Managed TOML startup requires Python 3.11+")


def native(tmp_path: Path, body: str) -> Path:
    binary = tmp_path / "fake gobgpd"
    binary.write_text(f"#!{sys.executable}\nimport os,sys,time,signal\nfrom pathlib import Path\n" + body + "\n")
    binary.chmod(0o755)
    return binary


def config(tmp_path: Path, *, extra: str = "", port: int = -1) -> Path:
    path = tmp_path / "gobgpd.toml"
    path.write_text(f'[global.config]\nas=65000\nrouter-id="127.0.0.1"\nport={port}\n'
                    'local-address-list=["127.0.0.1"]\n' + extra)
    return path


def free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def command(binary: Path, configuration: Path, port: int) -> list[str]:
    return ["runtime", "gobgp", "--binary", str(binary), "--config", str(configuration),
            "--api-address", "127.0.0.1", "--api-port", str(port)]


def test_duplicate_gobgp_reports_owner_once_before_any_native_execution(tmp_path, capsys):
    marker = tmp_path / "executions"
    binary = native(tmp_path, f"Path({str(marker)!r}).write_text('unexpected native execution')")
    with socket.socket() as occupied:
        occupied.bind(("127.0.0.1", 0))
        occupied.listen()
        port = occupied.getsockname()[1]
        assert main(command(binary, config(tmp_path), port)) == 78
        finding = json.loads(capsys.readouterr().err)
        assert finding["code"] == "GOBGP_PORT_IN_USE"
        assert finding["port"] == port and finding["expected_service"] == "ninja-gobgpd.service"
        assert any(owner["pid"] == os.getpid() for owner in finding["owners"])
        assert occupied.fileno() >= 0
        assert not marker.exists()


@pytest.mark.parametrize("contents", [
    "[global.config\npassword='do-not-log-this-secret'", "[unrelated]\nport=179",
    '[global.config]\nport=0', '[global.config]\nport=179\nlocal-address-list=[]',
])
def test_invalid_configuration_never_executes_native_or_logs_credential_contents(tmp_path, capsys, contents):
    marker = tmp_path / "executed"
    binary = native(tmp_path, f"Path({str(marker)!r}).write_text('unexpected')")
    configuration = config(tmp_path)
    configuration.write_text(contents)
    assert main(command(binary, configuration, free_port())) == 78
    output = capsys.readouterr().err
    assert json.loads(output)["code"] == "GOBGP_CONFIG_INVALID"
    assert "do-not-log-this-secret" not in output
    assert not marker.exists()


def test_configured_bgp_port_is_checked_before_native_validation(tmp_path, capsys):
    marker = tmp_path / "executed"
    binary = native(tmp_path, f"Path({str(marker)!r}).write_text('unexpected')")
    with socket.socket() as occupied:
        occupied.bind(("127.0.0.1", 0))
        occupied.listen()
        port = occupied.getsockname()[1]
        api_port = free_port()
        while api_port == port:
            api_port = free_port()
        assert main(command(binary, config(tmp_path, port=port), api_port)) == 78
        assert json.loads(capsys.readouterr().err)["port"] == port
        assert not marker.exists()


def test_native_configuration_failure_preserves_bounded_redacted_cause(tmp_path, capsys):
    binary = native(tmp_path, "sys.stderr.write('native invalid configuration password=top-secret\\n');sys.exit(2)")
    configuration = config(tmp_path, extra='auth-password="top-secret"\n')
    before = configuration.read_bytes()
    assert main(command(binary, configuration, free_port())) == 78
    finding = json.loads(capsys.readouterr().err)
    assert finding["code"] == "GOBGP_CONFIG_INVALID"
    assert "native invalid configuration" in finding["stderr"]
    assert "[REDACTED]" in finding["stderr"] and "top-secret" not in json.dumps(finding)
    assert configuration.read_bytes() == before


def test_transient_exit_is_not_misclassified_or_restarted(tmp_path, capsys):
    marker = tmp_path / "executions"
    body = f"""
with Path({str(marker)!r}).open('a') as log:
    log.write('validate\\n' if '--dry-run' in sys.argv else 'run\\n')
if '--dry-run' in sys.argv:
    assert all(flag in sys.argv for flag in ('--pprof-disable','--metrics-path=','--config-strict'))
    raise SystemExit(0)
sys.stderr.write('independent runtime failure\\n')
raise SystemExit(4)
"""
    binary = native(tmp_path, body)
    assert main(command(binary, config(tmp_path), free_port())) == 1
    finding = json.loads(capsys.readouterr().err)
    assert finding["code"] == "GOBGP_UNEXPECTED_EXIT"
    assert finding["returncode"] == 4 and "independent runtime failure" in finding["stderr"]
    assert marker.read_text().splitlines() == ["validate", "run"]


def test_post_preflight_bind_race_retains_the_native_failure_cause(tmp_path, capsys, monkeypatch):
    checks = []
    port = free_port()

    def check(address, checked_port, **options):
        checks.append((address, checked_port))
        if len(checks) == 3:
            raise startup.PermanentStartupError({
                "code": "GOBGP_PORT_IN_USE", "severity": "blocker", "message": "A concurrent owner obtained the port",
                "owners": [{"pid": 123456, "executable": "other-gobgpd", "service": None}],
            })

    monkeypatch.setattr(startup, "require_available_listener", check)
    binary = native(tmp_path, "sys.exit(0) if '--dry-run' in sys.argv else None\n"
                              "sys.stderr.write('native bind failure\\n');sys.exit(1)")
    assert main(command(binary, config(tmp_path), port)) == 78
    finding = json.loads(capsys.readouterr().err)
    assert finding["code"] == "GOBGP_PORT_IN_USE" and "native bind failure" in finding["stderr"]
    assert len(checks) == 3


def test_validation_timeout_kills_and_reaps_only_its_own_child(tmp_path, capsys, monkeypatch):
    child_pid = tmp_path / "child.pid"
    body = f"""
Path({str(child_pid)!r}).write_text(str(os.getpid()))
signal.signal(signal.SIGTERM, signal.SIG_IGN)
while True:
    time.sleep(.01)
"""
    binary = native(tmp_path, body)
    monkeypatch.setattr(runtime, "_DRY_RUN_TIMEOUT", .2)
    monkeypatch.setattr(runtime, "_STOP_TIMEOUT", .1)
    assert main(command(binary, config(tmp_path), free_port())) == 78
    assert "timed out" in json.loads(capsys.readouterr().err)["message"]
    pid = int(child_pid.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_long_native_output_is_bounded_and_preserves_its_final_cause(tmp_path, capsys):
    binary = native(tmp_path, "sys.stderr.write('x'*100000+'\\nfinal native cause\\n');sys.exit(1)")
    assert main(command(binary, config(tmp_path), free_port())) == 78
    finding = json.loads(capsys.readouterr().err)
    assert finding["stderr_truncated"] is True
    assert len(finding["stderr"].encode()) <= runtime._LOG_LIMIT
    assert finding["stderr"].endswith("final native cause\n")


def test_missing_binary_is_permanent_before_configuration_or_execution(tmp_path, capsys):
    assert main(command(tmp_path / "missing", config(tmp_path), free_port())) == 78
    assert json.loads(capsys.readouterr().err)["code"] == "GOBGP_BINARY_INVALID"


def test_signal_forwarding_reaps_native_and_returns_clean_operator_stop(tmp_path):
    marker = tmp_path / "child.pid"
    stopped = tmp_path / "child.stopped"
    body = f"""
if '--dry-run' in sys.argv:
    raise SystemExit(0)
def stop(signum, frame):
    Path({str(stopped)!r}).write_text(str(signum))
    raise SystemExit(0)
signal.signal(signal.SIGTERM, stop)
Path({str(marker)!r}).write_text(str(os.getpid()))
while True:
    time.sleep(.01)
"""
    binary = native(tmp_path, body)
    process = subprocess.Popen([sys.executable, "-m", "ninja_pce.cli",
                                *command(binary, config(tmp_path), free_port())],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    try:
        deadline = time.monotonic() + 5
        while not marker.exists() and time.monotonic() < deadline:
            assert process.poll() is None
            time.sleep(.01)
        assert marker.exists()
        child_pid = int(marker.read_text())
        process.terminate()
        assert process.wait(timeout=5) == 0
        assert stopped.read_text() == str(signal.SIGTERM)
        with pytest.raises(ProcessLookupError):
            os.kill(child_pid, 0)
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=3)
        if process.stdout:
            process.stdout.close()
        if process.stderr:
            process.stderr.close()
