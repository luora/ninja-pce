from __future__ import annotations

from .loaders import InputError
from .models import Inventory, ManagedDevice, Topology


DEVICE_TYPE_ALIASES = {
    "cisco_xr": "cisco_xr",
    "iosxr": "cisco_xr",
    "cisco_ios": "cisco_ios",
    "ios": "cisco_ios",
    "iosxe": "cisco_ios",
    "juniper_junos": "juniper_junos",
    "junos": "juniper_junos",
}

RENDER_FORMATS = {
    "cisco_xr": "iosxr",
    "cisco_ios": "iosxe",
    "juniper_junos": "junos",
}


def normalize_device_type(device_type: str) -> str:
    normalized = DEVICE_TYPE_ALIASES.get(device_type.strip().lower())
    if not normalized:
        raise InputError(
            f"Unsupported device_type {device_type!r}. "
            f"Supported values: {sorted(DEVICE_TYPE_ALIASES)}"
        )
    return normalized


def render_format_for_device(device: ManagedDevice) -> str:
    return RENDER_FORMATS[normalize_device_type(device.device_type)]


def validate_inventory(
    inventory: Inventory,
    topology: Topology,
    *,
    require_all_nodes: bool = True,
) -> tuple[str, ...]:
    errors: list[str] = []
    mapped_nodes: dict[str, str] = {}

    for device in inventory.devices.values():
        if device.topology_node_id not in topology.nodes:
            errors.append(
                f"Inventory device {device.name} maps to unknown topology node "
                f"{device.topology_node_id}"
            )
        previous = mapped_nodes.get(device.topology_node_id)
        if previous:
            errors.append(
                f"Topology node {device.topology_node_id} maps to both {previous} "
                f"and {device.name}"
            )
        mapped_nodes[device.topology_node_id] = device.name
        try:
            normalize_device_type(device.device_type)
        except InputError as exc:
            errors.append(f"Inventory device {device.name}: {exc}")

    if require_all_nodes:
        for node_id in topology.nodes:
            if node_id not in mapped_nodes:
                errors.append(f"Topology node {node_id} is missing from inventory")

    if errors:
        raise InputError("Inventory validation failed:\n- " + "\n- ".join(errors))
    return tuple(sorted(mapped_nodes))


def device_for_node(inventory: Inventory, node_id: str) -> ManagedDevice:
    try:
        return inventory.device_for_node(node_id)
    except KeyError as exc:
        raise InputError(f"Topology node {node_id} is missing from inventory") from exc
    except ValueError as exc:
        raise InputError(str(exc)) from exc
