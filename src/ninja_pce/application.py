"""Local application onboarding using the established controller and dashboard.

Workspaces contain only new generic configuration. Existing deployment paths,
credentials, observations and ownership state are never discovered or imported.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
from pathlib import Path
from typing import Any

from .controller import controller_command, load_controller_config

DEFAULT_WORKSPACE = Path(".ninja-pce/workspace")
DEFAULT_CONFIG = DEFAULT_WORKSPACE / "config/controller.json"


def _controller_example() -> dict[str, Any]:
    # Relative paths keep a newly initialized workspace portable. The examples
    # use documentation addresses and grant no authority over an existing lab.
    return {
        "version": 1,
        "files": {
            "bgpls_raw": "../data/live/bgpls-raw.json",
            "bgpls_status": "../data/live/bgpls-status.json",
            "topology": "../data/live/topology.json",
            "pcep_capture": "../data/live/pcep.json",
        },
        "state_dir": "../data/state",
        "runtime_dir": "../data/run",
        "bgpls": {
            "endpoint": "127.0.0.1:50051",
            "required_peers": ["192.0.2.20"],
            "interval_seconds": 5,
            "timeout_seconds": 10,
            "max_age_seconds": 30,
        },
        "pcep": {
            "listen_host": "192.0.2.10",
            "port": 4189,
            "domain_policy": "domain-policy.json",
        },
        "dashboard": {
            "enabled": True,
            "listen_host": "127.0.0.1",
            "port": 8088,
            "refresh_seconds": 5,
        },
    }


def _domain_example() -> dict[str, Any]:
    return {
        "version": 1,
        "domain": {
            "protocol_id": 3,
            "instance_id": 0,
            "autonomous_system": 65000,
            "bgpls_identifier": 0,
        },
        "pcc_prefixes": ["192.0.2.0/24"],
        "destination_prefixes": ["192.0.2.0/24"],
        "max_bandwidth_bps": 100000000,
        "max_live_lsps": 32,
    }


def _new_workspace(directory: str | Path) -> Path:
    value = Path(directory).expanduser()
    # Do not resolve first: resolving would hide a symlink in an existing path.
    target = Path(os.path.abspath(value))
    if ".." in value.parts:
        raise ValueError("Choose a workspace path without '..' components")
    for candidate in (*reversed(target.parents), target):
        if candidate.is_symlink():
            raise ValueError(f"Workspace path cannot contain a symbolic link: {candidate}")
        if candidate == target and candidate.exists():
            raise ValueError(f"Workspace already exists; choose a new directory: {target}")
        if candidate != target and candidate.exists() and not candidate.is_dir():
            raise ValueError(f"Workspace parent is not a directory: {candidate}")
    # Every newly created directory is private; existing parents keep their
    # ownership and permissions. mkdir refuses a target created in the meantime.
    for parent in reversed(target.parents):
        if not parent.exists():
            parent.mkdir(mode=0o700)
    target.mkdir(mode=0o700)
    return target


def initialize_workspace(directory: str | Path) -> Path:
    """Create only a new private workspace; refuse even a partial existing one."""
    try:
        root = _new_workspace(directory)
        for relative in ("config", "data", "data/live", "data/state", "data/run"):
            (root / relative).mkdir(mode=0o700)
        for name, document in (("controller.json", _controller_example()),
                               ("domain-policy.json", _domain_example())):
            descriptor = os.open(root / "config" / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(json.dumps(document, indent=2, sort_keys=True, allow_nan=False) + "\n")
        return root / "config/controller.json"
    except OSError as exc:
        raise ValueError(f"Cannot create a new application workspace: {exc}") from exc


def _existing_config(value: str | Path) -> Path:
    path = Path(value).expanduser()
    if not path.exists() and not path.is_symlink():
        raise ValueError(
            f"Controller configuration is missing: {path}. "
            "Run 'ninja-pce app init' to create a workspace, then edit its configuration "
            "for your network; or supply --config with an existing controller JSON file."
        )
    return path


def doctor(config_path: str | Path) -> int:
    """Check static configuration and local tools without probing network state."""
    config = load_controller_config(_existing_config(config_path))
    data = config.document
    executable = data["bgpls"]["grpcurl"]
    tool = shutil.which(executable)
    print(f"Configuration valid: {config.path}")
    if tool is None:
        print(f"Missing required executable: {executable}. Install grpcurl or set bgpls.grpcurl.")
    else:
        print(f"grpcurl available: {tool}")
    print(f"GoBGP must be configured separately at {data['bgpls']['endpoint']}.")
    print("Before starting, replace the example addresses and domain scope, and verify BGP-LS and PCC connectivity.")
    print("These checks validate local configuration and tools; live network health requires the running controller.")
    return 0 if tool is not None else 1


def add_application_commands(subparsers: Any) -> None:
    application = subparsers.add_parser("app", help="Launch the application and prepare a local network workspace")
    actions = application.add_subparsers(dest="app_action", required=True)
    demo = actions.add_parser("demo", help="Open the dashboard with labelled synthetic examples")
    demo.add_argument("--port", type=int, default=8088, help="Loopback HTTP port (default: 8088)")
    initialize = actions.add_parser("init", help="Create a new private workspace with generic network configuration")
    initialize.add_argument("--directory", default=str(DEFAULT_WORKSPACE), help="New workspace directory")
    for action, help_text in (
        ("doctor", "Check local configuration and required tools"),
        ("start", "Run the controller with your network configuration"),
        ("status", "Inspect the configured controller's current health"),
    ):
        command = actions.add_parser(action, help=help_text)
        command.add_argument("--config", default=str(DEFAULT_CONFIG), help="Controller JSON configuration")


def application_command(args: argparse.Namespace) -> int:
    if args.app_action == "demo":
        # Import lazily to reuse the existing dashboard lifecycle without a
        # module-level cycle: cli registers these onboarding commands.
        from .cli import cmd_dashboard

        return cmd_dashboard(argparse.Namespace(
            host="127.0.0.1", port=args.port, topology="build/live/topology.json",
            bgpls_status="build/live/bgpls-status.json", bgpls_raw="build/live/bgpls-raw.json",
            pcep="build/live/pcep.json", state_dir="build/state", max_age=30.0, refresh=5.0,
            demo=True, ospf_multi_area_policy=None,
        ))
    if args.app_action == "init":
        config_path = initialize_workspace(args.directory)
        print(f"Application workspace created: {config_path.parent.parent}")
        print(f"Edit {config_path} and {config_path.parent / 'domain-policy.json'} for your network.")
        print(f"Then run: ninja-pce app doctor --config {config_path}")
        print("Starting uses the existing controller; HTTPS tunnel provisioning requires separate enrollment and setup.")
        return 0
    if args.app_action == "doctor":
        return doctor(args.config)
    config = _existing_config(args.config)
    return controller_command(argparse.Namespace(
        config=str(config), controller_action="run" if args.app_action == "start" else "status", component=None,
    ))
