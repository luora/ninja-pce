"""Management survives protocol faults in existing active controller deployments."""
from __future__ import annotations

import asyncio
import os
import signal
import sys
from pathlib import Path

import pytest
from test_controller import config_file as config_file

from ninja_pce import controller
from ninja_pce.state import load_state


def _sleeping_child(component):
    return [sys.executable, "-c", f"import time; time.sleep(60)  # {component}"]


def _preserved_journals(config):
    directory = Path(config.document["state_dir"])
    directory.mkdir()
    contents = {
        directory / "managed-recovery.json": b"Existing uncertain recovery outcome and LSP ownership.\n",
        directory / ".ninja-pce-reservations.json": b"Existing reservation holds remain authoritative.\n",
    }
    for path, data in contents.items():
        path.write_bytes(data)
    return contents


@pytest.mark.parametrize("fault", ["exit", "spawn"])
def test_legacy_pcep_fault_stops_protocol_group_and_preserves_management(config_file, monkeypatch, fault):
    path, _ = config_file
    config = controller.load_controller_config(path)
    assert controller.effective_operating_mode(config) == "pcep-control"
    assert "operating_mode" not in config.document
    original_config = path.read_bytes()
    journals = _preserved_journals(config)
    commands = {component: _sleeping_child(component) for component in ("bgpls", "pcep", "dashboard")}
    commands["pcep"] = ([sys.executable, "-c", "import sys,time; time.sleep(.05); sys.exit(7)"]
                        if fault == "exit" else [str(path.parent / "missing-pcep-executable")])
    attempts = {component: 0 for component in commands}
    children = []
    original_spawn = asyncio.create_subprocess_exec

    async def spawn(*arguments, **kwargs):
        component = next(key for key, command in commands.items() if command == list(arguments))
        attempts[component] += 1
        process = await original_spawn(*arguments, **kwargs)
        children.append(process)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(controller, "_SHUTDOWN_SECONDS", .2)
    before_shutdown = {}

    async def stop_when_fault_is_visible(supervisor):
        while True:
            assert not supervisor.done(), "Protocol fault stopped the management supervisor"
            try:
                records = {component: load_state(controller._runtime_path(config, component))
                           for component in commands}
            except OSError:
                await asyncio.sleep(.005)
                continue
            if records["pcep"].get("state") != "failed" or records["bgpls"].get("state") != "stopped":
                await asyncio.sleep(.005)
                continue
            assert records["dashboard"]["state"] == "running"
            assert controller._runtime_status(config, "dashboard")["state"] == "running"
            assert "sibling failure" in records["bgpls"]["error"]
            assert "unexpectedly" not in records["bgpls"]["error"]
            assert controller._process_identity(records["bgpls"]["child"]["pid"]) is None
            if fault == "exit":
                assert records["pcep"]["returncode"] == 7
                assert "exited unexpectedly" in records["pcep"]["error"]
            else:
                assert records["pcep"].get("returncode") == 78
                assert "startup failed" in records["pcep"]["error"]
                assert "missing-pcep-executable" in records["pcep"]["error"]
            before_shutdown.update(records)
            # Allow another scheduler interval to expose a retry or accidental
            # management exit before the operator requests shutdown.
            await asyncio.sleep(.06)
            assert not supervisor.done()
            assert attempts == {component: 1 for component in commands}
            assert controller._runtime_status(config, "dashboard")["state"] == "running"
            assert {journal: journal.read_bytes() for journal in journals} == journals
            os.kill(os.getpid(), signal.SIGTERM)
            return

    async def exercise():
        supervisor = asyncio.create_task(controller._supervise(config, commands))
        try:
            await asyncio.wait_for(stop_when_fault_is_visible(supervisor), timeout=5)
            return await asyncio.wait_for(supervisor, timeout=5)
        finally:
            if not supervisor.done():
                supervisor.cancel()
            await asyncio.gather(supervisor, return_exceptions=True)

    assert asyncio.run(exercise()) == 0
    after = {component: load_state(controller._runtime_path(config, component)) for component in commands}
    assert after["pcep"]["state"] == "failed"
    assert after["pcep"]["error"] == before_shutdown["pcep"]["error"]
    assert after["pcep"].get("returncode") == before_shutdown["pcep"].get("returncode")
    assert after["bgpls"]["state"] == after["dashboard"]["state"] == "stopped"
    assert all(process.returncode is not None and controller._process_identity(process.pid) is None
               for process in children)
    assert attempts == {component: 1 for component in commands}
    assert {journal: journal.read_bytes() for journal in journals} == journals
    assert path.read_bytes() == original_config


def test_dashboard_failure_stops_and_reaps_existing_active_protocol_children(config_file, monkeypatch):
    path, _ = config_file
    config = controller.load_controller_config(path)
    journals = _preserved_journals(config)
    commands = {component: _sleeping_child(component) for component in ("bgpls", "pcep")}
    commands["dashboard"] = [sys.executable, "-c", "import sys,time; time.sleep(.05); sys.exit(7)"]
    monkeypatch.setattr(controller, "_SHUTDOWN_SECONDS", .2)

    async def exercise():
        return await asyncio.wait_for(controller._supervise(config, commands), timeout=5)

    assert asyncio.run(exercise()) == 1
    records = {component: load_state(controller._runtime_path(config, component)) for component in commands}
    assert records["dashboard"]["returncode"] == 7
    assert all(record["state"] == "stopped" for record in records.values())
    assert all(controller._process_identity(record["child"]["pid"]) is None for record in records.values())
    assert {journal: journal.read_bytes() for journal in journals} == journals
