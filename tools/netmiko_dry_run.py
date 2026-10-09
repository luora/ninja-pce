#!/usr/bin/env python3
"""Preview legacy generated files; execution must use the controller's exact plan."""
from __future__ import annotations

import argparse
from pathlib import Path

from ninja_pce.inventory import normalize_device_type, render_format_for_device
from ninja_pce.loaders import load_inventory
from ninja_pce.southbound import extract_config_commands


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inventory", required=True)
    parser.add_argument("--config", required=True)
    parser.add_argument("--device")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--commit", action="store_true")
    args = parser.parse_args(argv)
    if args.execute or args.commit:
        parser.error(
            "File-only execution cannot bind topology/demand/approval revisions. "
            "Use ninja-pce reconcile --execute --approve with the reviewed plan ID."
        )
    inventory = load_inventory(args.inventory)
    matches = [device for device in inventory.devices.values()
               if device.name == args.device] if args.device else [
                   device for device in inventory.devices.values()
                   if device.role == "headend" or "headend" in device.tags
               ]
    if len(matches) != 1:
        parser.error("Select one inventory device with --device")
    device = matches[0]
    normalize_device_type(device.device_type)
    commands = extract_config_commands(
        Path(args.config).read_text(encoding="utf-8"), render_format_for_device(device)
    )
    print(f"PREVIEW ONLY: {device.name} ({device.host}:{device.port})")
    print("\n".join(commands))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
