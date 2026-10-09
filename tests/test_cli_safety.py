from pathlib import Path

from ninja_pce.cli import _state_path, build_parser, main

ROOT = Path(__file__).resolve().parents[1]


def test_push_preview_never_imports_or_connects_netmiko(tmp_path, monkeypatch):
    import sys

    class Forbidden:
        def __getattr__(self, _name):
            raise AssertionError("Preview accessed Netmiko")

    monkeypatch.setitem(sys.modules, "netmiko", Forbidden())
    rc = main(["push", "--topology", str(ROOT / "examples/topologies/simple_multiarea.yml"),
               "--demand", str(ROOT / "examples/demands/pe1_to_pe3_50m.yml"),
               "--inventory", str(ROOT / "examples/inventory/network.yml"),
               "--output-dir", str(tmp_path / "outputs"), "--state-dir", str(tmp_path / "state")])
    assert rc == 0
    assert list((tmp_path / "state").glob("*.json"))


def test_different_output_dirs_share_default_reservation_directory():
    parser = build_parser()
    common = ["push", "--topology", "topology.yml", "--demand", "demand.yml", "--inventory", "inventory.yml"]
    first = _state_path(parser.parse_args([*common, "--output-dir", "one"]))
    second = _state_path(parser.parse_args([*common, "--output-dir", "two"]))
    assert first == second
    assert first.parent == Path("build/state")


def test_search_limit_is_indeterminate_and_has_distinct_exit_code(tmp_path, capsys):
    demand = tmp_path / "demand.yml"
    demand.write_text("service: {name: A, source: PE1, destination: PE3, bandwidth_mbps: 50, path: {search_budget: 1}}")
    rc = main(["compute", "--topology", str(ROOT / "examples/topologies/simple_multiarea.yml"),
               "--demand", str(demand), "--output-dir", str(tmp_path / "outputs")])
    assert rc == 3
    assert "INDETERMINATE / SEARCH_LIMIT" in capsys.readouterr().err
    assert not (tmp_path / "outputs").exists()
