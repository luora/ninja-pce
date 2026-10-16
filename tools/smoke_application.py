#!/usr/bin/env python3
"""Check an installed local launcher using only a disposable sample and workspace."""
from __future__ import annotations

import argparse
import json
import os
import re
import selectors
import signal
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path


def sample(checkout: Path, cwd: Path, *, terminate: bool = False) -> None:
    env = {**os.environ, "PIP_NO_INDEX": "1", "PIP_INDEX_URL": "file:///nonexistent-package-index"}
    process = subprocess.Popen([str(checkout / "ninja-pce"), "demo", "--port", "0"], cwd=cwd,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env)
    assert process.stdout is not None
    try:
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ)
        deadline = time.monotonic() + 30
        origin = None
        while time.monotonic() < deadline:
            if not selector.select(timeout=.2):
                if process.poll() is not None:
                    raise RuntimeError("Sample exited before opening its listener")
                continue
            line = process.stdout.readline()
            match = re.search(r"http://127\.0\.0\.1:[0-9]+", line)
            if match:
                origin = match[0]
                break
        selector.close()
        if origin is None:
            raise RuntimeError("Sample startup timed out")
        client = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with client.open(origin + "/healthz", timeout=5) as response:
            assert json.load(response) == {"status": "ok", "read_only": True, "mode": "demo"}
        with client.open(origin + "/api/controller", timeout=5) as response:
            snapshot = json.load(response)
            assert snapshot["mode"] == "demo" and snapshot["read_only"]
            assert len(snapshot["topology"]["nodes"]) == 6 and snapshot["lsps"]
        with client.open(origin + "/", timeout=5) as response:
            assert "Sample workspace" in response.read().decode()
        with client.open(origin + "/app.js", timeout=5) as response:
            assert "headend_readiness" in response.read().decode()
        request = urllib.request.Request(origin + "/api/tunnels", data=b"{}", method="POST")
        try:
            client.open(request, timeout=5)
        except urllib.error.HTTPError as error:
            assert error.code == 405
        else:
            raise AssertionError("Sample accepted a mutation")
        process.send_signal(signal.SIGTERM if terminate else signal.SIGINT)
        assert process.wait(timeout=10) == (-signal.SIGTERM if terminate else 0)
        try:
            client.open(origin + "/healthz", timeout=1)
        except urllib.error.URLError:
            pass
        else:
            raise AssertionError("Sample listener remained after shutdown")
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)
        process.stdout.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkout", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    checkout = args.checkout.resolve()
    manifest = checkout / ".ninja-pce/installation.json"
    before = manifest.read_bytes()
    assert json.loads(before)["ready"], "Run ./ninja-pce setup first"
    with tempfile.TemporaryDirectory(prefix="ninja-app-check-") as temporary:
        cwd = Path(temporary)
        sample(checkout, cwd)
        sample(checkout, cwd, terminate=True)
        workspace = cwd / "new network workspace"
        command = [str(checkout / "ninja-pce"), "init", "--directory", str(workspace)]
        subprocess.run(command, cwd=cwd, check=True, capture_output=True, text=True)
        files = {path: path.read_bytes() for path in workspace.rglob("*") if path.is_file()}
        result = subprocess.run(command, cwd=cwd, capture_output=True, text=True)
        assert result.returncode == 1 and "already exists" in result.stderr
        assert all(path.read_bytes() == data for path, data in files.items())
        status = subprocess.run([str(checkout / "ninja-pce"), "status", "--config",
                                 str(workspace / "config/controller.json")], cwd=cwd,
                                capture_output=True, text=True)
        assert status.returncode == 1 and json.loads(status.stdout)["status"] == "degraded"
        assert manifest.read_bytes() == before, "Offline reopen changed the installation"
    print("Launcher smoke passed: offline sample/assets, read-only boundary, signals, workspace preservation and status.")


if __name__ == "__main__":
    main()
