#!/usr/bin/env python3
"""Lightweight management reachability check for the GNS3 lab inventory.

This does not authenticate to routers. It only checks TCP reachability to the
configured SSH port so the operator can fix GNS3 management links first.
"""
from __future__ import annotations

import argparse
import socket

from ninja_pce.loaders import InputError, load_inventory


def check_tcp(host: str, port: int, timeout: float) -> tuple[bool, str]:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, "open"
    except OSError as exc:
        return False, str(exc)


def main() -> int:
    parser = argparse.ArgumentParser(description="Check TCP/SSH reachability to GNS3 lab routers")
    parser.add_argument("--inventory", required=True, help="Inventory YAML")
    parser.add_argument("--timeout", type=float, default=2.0, help="TCP timeout in seconds")
    args = parser.parse_args()

    try:
        inventory = load_inventory(args.inventory)
    except InputError as exc:
        raise SystemExit(str(exc)) from exc
    print("ninja-pce GNS3 SSH precheck")
    print("===========================")
    failed = False
    for device in inventory.devices.values():
        ok, msg = check_tcp(device.host, device.port, args.timeout)
        status = "OK" if ok else "FAIL"
        print(f"{device.name:8} {device.host}:{device.port:<5} {status:4} {msg}")
        failed = failed or not ok
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
