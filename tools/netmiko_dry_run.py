#!/usr/bin/env python3
"""Safe Netmiko helper for ninja-pce-generated configs.

Default behavior is preview-only. It prints the target, platform, and config
that would be sent. Use --execute only in a lab, after reviewing the generated
change plan and rollback.
"""
from __future__ import annotations

import argparse
import getpass
import os
from pathlib import Path
from typing import Any

import yaml

PLATFORM_MAP = {
    "cisco_xr": "cisco_xr",
    "iosxr": "cisco_xr",
    "cisco_ios": "cisco_ios",
    "iosxe": "cisco_ios",
    "juniper_junos": "juniper_junos",
    "junos": "juniper_junos",
    "mikrotik_routeros": "mikrotik_routeros",
    "routeros": "mikrotik_routeros",
}


def load_inventory(path: str | Path) -> dict[str, Any]:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    if "devices" not in data:
        raise SystemExit("Inventory must contain a top-level 'devices' mapping")
    return data


def pick_device(inv: dict[str, Any], requested: str | None) -> tuple[str, dict[str, Any]]:
    devices = inv["devices"]
    if requested:
        if requested not in devices:
            raise SystemExit(f"Device {requested!r} not found in inventory")
        return requested, devices[requested]
    for name, dev in devices.items():
        if dev.get("role") == "headend":
            return name, dev
    raise SystemExit("No --device supplied and no inventory device has role: headend")


def config_lines(config_text: str, platform: str) -> list[str]:
    lines: list[str] = []
    for line in config_text.splitlines():
        stripped = line.strip()
        lowered = stripped.lower()
        if not stripped:
            continue
        if platform == "juniper_junos":
            if lowered.startswith(("set ", "delete ", "activate ", "deactivate ", "rename ")):
                lines.append(stripped)
            continue
        if lowered in {
            "! suggested pre-check commands",
            "! suggested verification commands",
            "! rollback reminder",
        }:
            break
        if stripped.startswith("!"):
            if stripped == "!" and lines and lines[-1] != "exit":
                lines.append("exit")
            continue
        if lowered.startswith("show "):
            continue
        if " !" in stripped:
            stripped = stripped.split(" !", 1)[0].rstrip()
        lines.append(stripped)
    while lines and lines[-1] == "exit":
        lines.pop()
    return lines


def main() -> int:
    parser = argparse.ArgumentParser(description="Preview or apply generated config using Netmiko")
    parser.add_argument("--inventory", required=True, help="Inventory YAML")
    parser.add_argument("--config", required=True, help="Generated config text file")
    parser.add_argument("--device", help="Inventory device name. Defaults to role=headend")
    parser.add_argument("--execute", action="store_true", help="Actually connect and send config. Default is preview-only.")
    parser.add_argument("--commit", action="store_true", help="Request commit for platforms that need it. Only with --execute.")
    args = parser.parse_args()

    inv = load_inventory(args.inventory)
    name, dev = pick_device(inv, args.device)
    raw_config = Path(args.config).read_text(encoding="utf-8")

    platform = PLATFORM_MAP.get(str(dev.get("platform", "")).lower())
    if not platform:
        raise SystemExit(f"Unsupported or missing platform for {name}: {dev.get('platform')!r}")
    lines = config_lines(raw_config, platform)

    host = str(dev["management_ip"])
    username = str(dev.get("username") or input(f"Username for {name}: "))
    password_env = dev.get("password_env")
    password = os.environ.get(password_env, "") if password_env else ""

    print("ninja-pce Netmiko helper")
    print("========================")
    print(f"Device:     {name}")
    print(f"Host:       {host}:{dev.get('ssh_port', 22)}")
    print(f"Platform:   {platform}")
    print(f"Mode:       {'EXECUTE' if args.execute else 'DRY-RUN PREVIEW ONLY'}")
    print("\nConfig commands that would be sent:")
    for line in lines:
        print(f"  {line}")

    if not args.execute:
        print("\nNo connection made. Add --execute only after review in the lab.")
        return 0

    if not password:
        password = getpass.getpass(f"Password for {name}: ")

    try:
        from netmiko import ConnectHandler
    except ImportError as exc:
        raise SystemExit("netmiko is not installed. Install with: pip install 'ninja-pce[gns3]'") from exc

    conn = ConnectHandler(
        device_type=platform,
        host=host,
        username=username,
        password=password,
        port=int(dev.get("ssh_port", 22)),
        fast_cli=False,
    )
    try:
        output = conn.send_config_set(
            lines,
            error_pattern=r"(?i)(% ?invalid|syntax error|error:|commit failed)",
        )
        print(output)
        if args.commit and hasattr(conn, "commit"):
            print(conn.commit())
    finally:
        conn.disconnect()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
