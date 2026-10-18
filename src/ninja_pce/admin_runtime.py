"""Owned observation subprocesses for the independent administration server.

Only live Popen handles created here receive signals. Listener inspection is a
startup witness, never authority to stop a process. Failed and uncertain starts
retain bounded diagnostics and are never retried automatically.
"""
from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .listeners import inspect_listener

_LOG_LIMIT = 16 * 1024
_CONTROLLER_CONFIRM_SECONDS = 0.3
_GOBGP_READY_SECONDS = 35.0
_STOP_SECONDS = 15.0
_POLL_SECONDS = 0.05


def _bounded_text(value: bytes, limit: int = _LOG_LIMIT) -> str:
    text = value.decode("utf-8", errors="replace")
    return text.encode("utf-8")[-limit:].decode("utf-8", errors="ignore")


def _process_identity(pid: int) -> tuple[int, str] | None:
    """Read the parent and start ticks without trusting a saved PID alone."""
    try:
        with Path(f"/proc/{pid}/stat").open("rb") as stream:
            raw = stream.read(65_537)
        if len(raw) > 65_536:
            return None
        fields = raw.decode().rsplit(")", 1)[1].split()
        if fields[0] == "Z":
            return None
        return int(fields[1]), fields[19]
    except (OSError, UnicodeError, ValueError, IndexError):
        return None


def _process_arguments(pid: int) -> list[str]:
    try:
        with Path(f"/proc/{pid}/cmdline").open("rb") as stream:
            raw = stream.read(65_537)
        if len(raw) > 65_536:
            return []
        return [item.decode("utf-8") for item in raw.split(b"\0") if item]
    except (OSError, UnicodeError):
        return []


@dataclass
class _OwnedChild:
    component: str
    process: subprocess.Popen[bytes]
    identity: tuple[int, str] | None
    log: bytearray = field(default_factory=bytearray)
    log_lock: Any = field(default_factory=threading.Lock)
    readers: list[threading.Thread] = field(default_factory=list)
    requested_stop: bool = False
    error: str = ""
    truncated: bool = False

    def drain(self, descriptor: int) -> None:
        try:
            while True:
                block = os.read(descriptor, 8192)
                if not block:
                    return
                with self.log_lock:
                    self.log.extend(block)
                    if len(self.log) > _LOG_LIMIT:
                        del self.log[:-_LOG_LIMIT]
                        self.truncated = True
        except OSError:
            return

    def log_text(self) -> str:
        with self.log_lock:
            return _bounded_text(bytes(self.log))

    def finish_readers(self) -> None:
        if self.process.poll() is None:
            return
        for reader in self.readers:
            reader.join(timeout=0.05)
        for reader, stream in zip(self.readers, (self.process.stdout, self.process.stderr)):
            if not reader.is_alive() and stream is not None and not stream.closed:
                stream.close()

    def status(self) -> dict[str, Any]:
        returncode = self.process.poll()
        self.finish_readers()
        state = "running" if returncode is None else "failed" if self.error else "stopped"
        return {"state": state, "pid": self.process.pid, "returncode": returncode,
                "error": self.error[:1024], "log": self.log_text(), "log_truncated": self.truncated}


class ObserverRuntime:
    """Explicitly start/stop only this administration process's observer group."""

    def __init__(self, root: Path, grpcurl: str, gobgpd: str | None):
        self.root = Path(root).resolve()
        self.grpcurl = str(Path(grpcurl).absolute()) if "/" in grpcurl else grpcurl
        self.gobgpd = str(Path(gobgpd).absolute()) if gobgpd is not None else None
        self.uncertain = False
        self._children: dict[str, _OwnedChild] = {}
        self._started = False
        self._error = ""

    def _refresh(self) -> None:
        for child in self._children.values():
            returncode = child.process.poll()
            if returncode is not None and not child.requested_stop:
                child.finish_readers()
                if not child.error:
                    cause = child.log_text().strip()
                    child.error = f"{child.component} exited with status {returncode}."
                    if cause:
                        child.error += " " + cause[-900:]
                if not self._error:
                    self._error = child.error[:1024]
                # These two managed exits complete their own child cleanup.
                # Signals, unexpected success, and unknown exit paths cannot
                # establish whether an observer descendant remains alive.
                if returncode not in {1, 78}:
                    self.uncertain = True

    @property
    def running(self) -> bool:
        self._refresh()
        return (self._started and not self.uncertain and bool(self._children)
                and all(child.process.poll() is None for child in self._children.values()))

    def status(self) -> dict[str, Any]:
        running = self.running
        state = "uncertain" if self.uncertain else "running" if running else "failed" if self._error else "stopped"
        message = ("Process ownership is uncertain; reconcile locally before starting the observer."
                   if self.uncertain else "Observer processes are running; network readiness is evaluated separately."
                   if running else self._error or "Observer processes are stopped.")
        stopped = {"state": "stopped", "pid": None, "returncode": None, "error": "", "log": ""}
        return {"running": running, "state": state, "message": message[:1024],
                "controller": self._children["controller"].status() if "controller" in self._children else dict(stopped),
                "gobgpd": self._children["gobgpd"].status() if "gobgpd" in self._children else dict(stopped)}

    def _spawn(self, component: str, command: list[str]) -> _OwnedChild:
        environment = dict(os.environ)
        for name in ("PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "PYTHONINSPECT", "PYTHONUSERBASE"):
            environment.pop(name, None)
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, cwd=self.root, env=environment,
                                   close_fds=True, start_new_session=True)
        child = _OwnedChild(component, process, _process_identity(process.pid))
        self._children[component] = child
        assert process.stdout is not None and process.stderr is not None
        child.readers = [threading.Thread(target=child.drain, args=(stream.fileno(),), daemon=True)
                         for stream in (process.stdout, process.stderr)]
        for reader in child.readers:
            reader.start()
        return child

    def _check_alive(self) -> None:
        self._refresh()
        failed = next((child for child in self._children.values() if child.process.poll() is not None), None)
        if failed is not None:
            raise ValueError(failed.error[:1024])

    def _owns_api(self, wrapper: _OwnedChild, revision: Path) -> bool:
        if wrapper.identity is None or _process_identity(wrapper.process.pid) != wrapper.identity:
            return False
        finding = inspect_listener("127.0.0.1", 50051)
        owners = finding.get("owners", [])
        if not owners:
            return False
        binary = str(Path(self.gobgpd or "").absolute())
        required = ["-f", str(revision / "gobgpd.toml"), "--api-hosts=127.0.0.1:50051"]
        for owner in owners:
            pid = owner.get("pid")
            if type(pid) is not int:
                return False
            identity = _process_identity(pid)
            if identity is None or identity[0] != wrapper.process.pid:
                return False
            arguments = _process_arguments(pid)
            # A script executable has its interpreter in argv[0]; both paths
            # come from the local CLI, never the submitted setup document.
            if binary not in arguments[:2] or not any(arguments[index:index + 3] == required
                                                      for index in range(len(arguments) - 2)):
                return False
            if _process_identity(pid) != identity:
                return False
        return (wrapper.process.poll() is None
                and _process_identity(wrapper.process.pid) == wrapper.identity)

    def start(self, revision_dir: Path) -> None:
        self._refresh()
        if self.uncertain:
            raise ValueError("Process ownership is uncertain; reconcile locally before starting the observer.")
        if any(child.process.poll() is None for child in self._children.values()):
            raise ValueError("Observer processes are already owned by this administration server; stop them first.")
        if shutil.which(self.grpcurl) is None:
            raise ValueError("Required grpcurl executable is unavailable; repair the local installation before starting.")
        revision = Path(revision_dir).resolve()
        self._children = {}
        self._error = ""
        self._started = False
        prefix = [sys.executable, "-I", "-m", "ninja_pce.cli"]
        try:
            if self.gobgpd is not None:
                wrapper = self._spawn("gobgpd", [*prefix, "runtime", "gobgp", "--binary", self.gobgpd,
                    "--config", str(revision / "gobgpd.toml"), "--api-address", "127.0.0.1", "--api-port", "50051"])
                deadline = time.monotonic() + _GOBGP_READY_SECONDS
                while True:
                    self._check_alive()
                    if self._owns_api(wrapper, revision):
                        break
                    if time.monotonic() >= deadline:
                        raise ValueError("Managed GoBGP did not establish its owned loopback API listener within 35 seconds.")
                    time.sleep(_POLL_SECONDS)
            self._spawn("controller", [*prefix, "controller", "run", "--config", str(revision / "controller.json"),
                                       "--component", "bgpls"])
            deadline = time.monotonic() + _CONTROLLER_CONFIRM_SECONDS
            while time.monotonic() < deadline:
                self._check_alive()
                time.sleep(_POLL_SECONDS)
            self._check_alive()
            self._started = True
        except Exception as error:
            self._error = str(error)[:1024]
            try:
                self.stop()
            except ValueError:
                pass
            raise ValueError(self._error) from error

    def stop(self) -> None:
        self._refresh()
        self._started = False
        deadline = time.monotonic() + _STOP_SECONDS
        for child in self._children.values():
            if child.process.poll() is None:
                child.requested_stop = True
                try:
                    child.process.send_signal(signal.SIGTERM)
                except ProcessLookupError:
                    pass
        for child in self._children.values():
            if child.process.poll() is None:
                try:
                    child.process.wait(timeout=max(0.0, deadline - time.monotonic()))
                except subprocess.TimeoutExpired:
                    self.uncertain = True
                    child.error = "Owned supervisor did not stop in time; descendant ownership is uncertain."
                    self._error = child.error
                    # Killing only our immediate child cannot prove its
                    # descendants stopped; never signal a process group/PID.
                    try:
                        child.process.kill()
                    except ProcessLookupError:
                        pass
                    try:
                        child.process.wait(timeout=1.0)
                    except subprocess.TimeoutExpired:
                        pass
            child.finish_readers()
        if self.uncertain:
            raise ValueError("Process ownership is uncertain; reconcile locally before starting the observer.")
