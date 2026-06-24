from pathlib import Path

import pytest

from ninja_pce.inventory import device_for_node, render_format_for_device, validate_inventory
from ninja_pce.loaders import InputError, load_inventory, load_topology

ROOT = Path(__file__).resolve().parents[1]
TOPOLOGY = ROOT / "examples" / "topologies" / "simple_multiarea.yml"


def test_loads_canonical_inventory_and_maps_topology_nodes(tmp_path):
    inventory_file = tmp_path / "inventory.yml"
    inventory_file.write_text(
        """
devices:
  - name: pe1-mgmt
    topology_node_id: PE1
    host: 192.0.2.11
    device_type: iosxr
    username: automation
    password_env: PE1_PASSWORD
""".strip(),
        encoding="utf-8",
    )

    inventory = load_inventory(inventory_file)
    device = device_for_node(inventory, "PE1")

    assert device.name == "pe1-mgmt"
    assert device.host == "192.0.2.11"
    assert render_format_for_device(device) == "iosxr"


def test_loads_legacy_gns3_inventory_format():
    inventory = load_inventory(ROOT / "gns3" / "lab-data" / "inventory" / "gns3_inventory.yml")
    device = device_for_node(inventory, "PE1")

    assert device.host == "172.16.100.101"
    assert device.port == 22
    assert render_format_for_device(device) == "iosxr"


def test_inventory_validation_requires_every_topology_node(tmp_path):
    inventory_file = tmp_path / "inventory.yml"
    inventory_file.write_text(
        """
devices:
  PE1:
    host: 192.0.2.11
    device_type: cisco_xr
    username: automation
""".strip(),
        encoding="utf-8",
    )

    topology = load_topology(TOPOLOGY)
    inventory = load_inventory(inventory_file)

    with pytest.raises(InputError, match="P1 is missing"):
        validate_inventory(inventory, topology, require_all_nodes=True)


def test_duplicate_topology_mapping_is_rejected(tmp_path):
    inventory_file = tmp_path / "inventory.yml"
    inventory_file.write_text(
        """
devices:
  - name: pe1-a
    topology_node_id: PE1
    host: 192.0.2.11
    device_type: cisco_xr
    username: automation
  - name: pe1-b
    topology_node_id: PE1
    host: 192.0.2.12
    device_type: cisco_xr
    username: automation
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(InputError, match="mapped by both"):
        load_inventory(inventory_file)
