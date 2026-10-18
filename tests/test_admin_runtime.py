"""Administration owns its subprocess handles and leaves other processes alone."""
from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from ninja_pce import admin_runtime
from ninja_pce.admin_model import default_setup, render_setup
from ninja_pce.admin_runtime import ObserverRuntime, _OwnedChild


@pytest.fixture
def observer(tmp_path, monkeypatch):
    monkeypatch.setattr(admin_runtime, "_CONTROLLER_CONFIRM_SECONDS", .15)
    monkeypatch.setattr(admin_runtime, "_STOP_SECONDS", 1.0)
    runtime = ObserverRuntime(tmp_path, str(Path(sys.executable).resolve()), None)
    yield runtime
    try:
        runtime.stop()
    except ValueError:
        pass


def intercept_children(monkeypatch, body="import time;time.sleep(60)"):
    original = subprocess.Popen
    calls = []

    def spawn(command, **kwargs):
        calls.append((command, kwargs))
        program = body(command) if callable(body) else body
        return original([sys.executable, "-I", "-c", program], **kwargs)

    monkeypatch.setattr(admin_runtime.subprocess, "Popen", spawn)
    return calls


def wait_until(condition, timeout=3):
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() >= deadline:
            raise AssertionError("Timed out waiting for disposable child")
        time.sleep(.01)


def test_owned_controller_command_is_isolated_and_external_gobgp_is_untouched(observer, tmp_path, monkeypatch):
    monkeypatch.setenv("PYTHONPATH", "/untrusted/modules")
    monkeypatch.setenv("PYTHONHOME", "/untrusted/python")
    calls = intercept_children(monkeypatch)
    monkeypatch.setattr(admin_runtime, "inspect_listener", lambda *args, **kwargs: pytest.fail("external API probe"))
    observer.start(tmp_path / "revision")
    command, options = calls[0]
    assert command == [sys.executable, "-I", "-m", "ninja_pce.cli", "controller", "run", "--config",
                       str(tmp_path / "revision/controller.json"), "--component", "bgpls"]
    assert "PYTHONPATH" not in options["env"] and "PYTHONHOME" not in options["env"]
    assert options["stdin"] == subprocess.DEVNULL
    assert options["start_new_session"] and options["close_fds"]
    assert observer.status()["state"] == "running"
    assert observer.status()["gobgpd"]["state"] == "stopped"
    observer.stop()
    assert observer.status()["state"] == "stopped"
    assert observer.status()["controller"]["returncode"] is not None
    assert len(calls) == 1


def test_missing_grpcurl_fails_before_any_process(observer, tmp_path, monkeypatch):
    observer.grpcurl = str(tmp_path / "missing-grpcurl")
    monkeypatch.setattr(admin_runtime.subprocess, "Popen", lambda *args, **kwargs: pytest.fail("spawned child"))
    with pytest.raises(ValueError, match="grpcurl executable is unavailable"):
        observer.start(tmp_path / "revision")
    assert not observer.running


def test_live_owned_process_cannot_be_started_twice(observer, tmp_path, monkeypatch):
    calls = intercept_children(monkeypatch)
    observer.start(tmp_path / "revision")
    original_pid = observer.status()["controller"]["pid"]
    with pytest.raises(ValueError, match="already owned"):
        observer.start(tmp_path / "revision")
    assert observer.status()["controller"]["pid"] == original_pid
    assert len(calls) == 1


def test_failed_start_retains_native_cause_and_never_retries(observer, tmp_path, monkeypatch):
    observer.gobgpd = str(tmp_path / "trusted-native")
    calls = intercept_children(monkeypatch, "import sys;sys.stderr.write('GOBGP_CONFIG_INVALID: native bootstrap failed\\n');sys.exit(78)")
    with pytest.raises(ValueError, match="native bootstrap failed"):
        observer.start(tmp_path / "revision")
    status = observer.status()
    assert status["state"] == "failed" and status["gobgpd"]["returncode"] == 78
    assert status["controller"]["pid"] is None
    assert "GOBGP_CONFIG_INVALID" in status["gobgpd"]["log"]
    observer.stop()
    assert observer.status()["gobgpd"]["state"] == "failed"
    assert not observer.uncertain and len(calls) == 1


def test_log_capture_drains_both_pipes_and_bounds_invalid_utf8(observer, tmp_path, monkeypatch):
    calls = intercept_children(monkeypatch, "import os,sys;os.write(1,b'x'*100000);os.write(2,b'\\xff'*100000+b'FINAL-CAUSE');sys.exit(1)")
    with pytest.raises(ValueError):
        observer.start(tmp_path / "revision")
    child = observer.status()["controller"]
    assert child["log_truncated"]
    assert len(child["log"].encode()) <= 16 * 1024
    assert "FINAL-CAUSE" in child["log"]
    assert len(child["error"]) <= 1024 and child["returncode"] == 1
    assert len(calls) == 1 and not observer.uncertain


@pytest.mark.parametrize("returncode", [0, -signal.SIGKILL])
def test_unexpected_success_or_signal_exit_blocks_restart_as_uncertain(observer, tmp_path, monkeypatch, returncode):
    calls = intercept_children(monkeypatch, "import signal,sys,time;signal.signal(signal.SIGUSR1,lambda *args:sys.exit(0));time.sleep(60)")
    observer.start(tmp_path / "revision")
    child = observer._children["controller"].process
    if returncode == 0:
        child.send_signal(signal.SIGUSR1)
        child.wait()
    else:
        child.kill()
        child.wait()
    assert observer.status()["state"] == "uncertain"
    with pytest.raises(ValueError, match="ownership is uncertain"):
        observer.stop()
    with pytest.raises(ValueError, match="ownership is uncertain"):
        observer.start(tmp_path / "revision")
    assert len(calls) == 1


def test_explicit_stop_signals_only_owned_child_and_retains_foreign_process(observer, tmp_path, monkeypatch):
    foreign = subprocess.Popen([sys.executable, "-I", "-c", "import time;time.sleep(60)"])
    try:
        intercept_children(monkeypatch)
        observer.start(tmp_path / "revision")
        owned = observer._children["controller"].process
        observer.stop()
        assert owned.poll() is not None
        assert foreign.poll() is None
        observer.stop()
        assert not observer.uncertain
    finally:
        foreign.terminate()
        foreign.wait(timeout=3)


def test_forced_immediate_child_kill_is_uncertain_and_never_kills_descendants(observer, tmp_path, monkeypatch):
    intercept_children(monkeypatch, "import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(60)")
    monkeypatch.setattr(admin_runtime, "_STOP_SECONDS", .05)
    observer.start(tmp_path / "revision")
    child = observer._children["controller"].process
    with pytest.raises(ValueError, match="ownership is uncertain"):
        observer.stop()
    assert child.poll() == -signal.SIGKILL
    assert observer.status()["state"] == "uncertain"
    with pytest.raises(ValueError, match="ownership is uncertain"):
        observer.start(tmp_path / "revision")


def test_managed_api_timeout_stops_own_wrapper_without_starting_collector(observer, tmp_path, monkeypatch):
    observer.gobgpd = str(tmp_path / "native")
    calls = intercept_children(monkeypatch)
    monkeypatch.setattr(observer, "_owns_api", lambda *args: False)
    monkeypatch.setattr(admin_runtime, "_GOBGP_READY_SECONDS", .05)
    with pytest.raises(ValueError, match="owned loopback API listener"):
        observer.start(tmp_path / "revision")
    assert calls[0][0][4:] == ["runtime", "gobgp", "--binary", observer.gobgpd, "--config",
        str(tmp_path / "revision/gobgpd.toml"), "--api-address", "127.0.0.1", "--api-port", "50051"]
    assert observer.status()["gobgpd"]["returncode"] is not None
    assert observer.status()["state"] == "failed"
    assert not observer.uncertain and len(calls) == 1


def test_failed_controller_start_stops_owned_native_wrapper_and_retains_cause(observer, tmp_path, monkeypatch):
    observer.gobgpd = str(tmp_path / "native")
    calls = intercept_children(monkeypatch, lambda command: (
        "import time;time.sleep(60)" if "runtime" in command else
        "import sys;sys.stderr.write('COMPONENT_ALREADY_SUPERVISED: existing owner\\n');sys.exit(78)"
    ))
    monkeypatch.setattr(observer, "_owns_api", lambda *args: True)
    with pytest.raises(ValueError, match="existing owner"):
        observer.start(tmp_path / "revision")
    status = observer.status()
    assert status["controller"]["state"] == "failed"
    assert status["gobgpd"]["state"] == "stopped"
    assert "COMPONENT_ALREADY_SUPERVISED" in status["controller"]["log"]
    assert len(calls) == 2 and not observer.uncertain


def test_runtime_failure_is_reported_without_automatic_restart(observer, tmp_path, monkeypatch):
    observer.gobgpd = str(tmp_path / "native")
    calls = intercept_children(monkeypatch, lambda command: (
        "import signal,sys,time;signal.signal(signal.SIGUSR1,lambda *args:sys.exit(1));time.sleep(60)"
        if "runtime" in command else "import time;time.sleep(60)"
    ))
    monkeypatch.setattr(observer, "_owns_api", lambda *args: True)
    observer.start(tmp_path / "revision")
    wrapper = observer._children["gobgpd"].process
    wrapper.send_signal(signal.SIGUSR1)
    wrapper.wait()
    status = observer.status()
    assert status["state"] == "failed" and not status["running"]
    assert status["controller"]["state"] == "running"
    assert status["gobgpd"]["returncode"] == 1
    assert len(calls) == 2 and not observer.uncertain
    observer.stop()
    assert observer.status()["controller"]["state"] == "stopped"


@pytest.mark.parametrize("defect", [None, "foreign", "pid_reused", "dry_run", "wrong_config"])
def test_api_readiness_requires_exact_live_native_child_identity(observer, tmp_path, monkeypatch, defect):
    observer.gobgpd = str(tmp_path / "native")
    wrapper_process = SimpleNamespace(pid=100, poll=lambda: None)
    wrapper = _OwnedChild("gobgpd", wrapper_process, (os.getpid(), "1000"))
    owner_pid = 200
    monkeypatch.setattr(admin_runtime, "inspect_listener", lambda *args: {"owners": [{"pid": owner_pid}]})
    calls = {100: 0}

    def identity(pid):
        if pid == 100:
            calls[100] += 1
            return (os.getpid(), "changed" if defect == "pid_reused" and calls[100] > 1 else "1000")
        return (999 if defect == "foreign" else 100, "1100")

    arguments = [observer.gobgpd, "-f", str(tmp_path / "revision/gobgpd.toml"), "--api-hosts=127.0.0.1:50051"]
    if defect == "dry_run":
        arguments[-1] = "--dry-run"
    if defect == "wrong_config":
        arguments[2] = "/foreign/config.toml"
    monkeypatch.setattr(admin_runtime, "_process_identity", identity)
    monkeypatch.setattr(admin_runtime, "_process_arguments", lambda pid: arguments)
    assert observer._owns_api(wrapper, tmp_path / "revision") is (defect is None)


@pytest.mark.skipif(sys.version_info < (3, 11), reason="Managed GoBGP TOML validation requires Python 3.11+")
def test_real_managed_launcher_and_controller_lifecycle(tmp_path):
    # Refuse to compete with any existing local service; all children and
    # configuration below are private disposable test fixtures.
    with socket.socket() as probe:
        try:
            probe.bind(("127.0.0.1", 50051))
        except OSError:
            pytest.skip("Loopback50051 already has an owner")
    revision = tmp_path / "revision"
    revision.mkdir()
    native = tmp_path / "fake-gobgpd"
    native.write_text(f"#!{Path(sys.executable).resolve()}\n"
                      "import socket,sys,time\n"
                      "if '--dry-run' in sys.argv: sys.exit(0)\n"
                      "s=socket.socket();s.bind(('127.0.0.1',50051));s.listen()\n"
                      "while True: time.sleep(.05)\n")
    native.chmod(0o700)
    grpcurl = str(Path(sys.executable).resolve())
    setup = default_setup()
    for name, value in render_setup(setup, tmp_path, revision, grpcurl=grpcurl).items():
        (revision / name).write_bytes(value)
    runtime = ObserverRuntime(tmp_path, grpcurl, str(native))
    try:
        runtime.start(revision)
        status = runtime.status()
        assert status["state"] == "running"
        assert status["controller"]["pid"] != status["gobgpd"]["pid"]
        assert "network readiness" in status["message"]
        runtime.stop()
        assert runtime.status()["state"] == "stopped"
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 50051))
    finally:
        runtime.stop()
