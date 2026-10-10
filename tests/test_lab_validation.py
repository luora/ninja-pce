"""Only synthetic/offline harness tests; no integration qualification claims."""
import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _module(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "gns3/validation" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_all_prepared_scenarios_have_honest_offline_and_integration_statuses(tmp_path):
    setup, runner = _module("setup_lab"), _module("run")
    manifest = setup.prepare(tmp_path / "cases")
    offline = runner.run(manifest, "offline")
    assert sum(row["status"] == "PASS" for row in offline["results"]) == 13
    assert [row["id"] for row in offline["results"] if row["status"] == "SKIP"] == ["T10", "T15"]
    integration = runner.run(manifest, "integration")
    assert len(integration["results"]) == 15
    assert all(row["status"] == "SKIP" for row in integration["results"])
    assert all("NOT EXECUTED" in row["reason"] for row in integration["results"])


def test_wrong_golden_link_identity_fails(tmp_path):
    setup, runner = _module("setup_lab"), _module("run")
    manifest = setup.prepare(tmp_path / "cases")
    data = json.loads(manifest.read_text())
    data["cases"][0]["expected_links"] = ["wrong-member"]
    manifest.write_text(json.dumps(data))
    report = runner.run(manifest, "offline", selected="T01")
    assert report["results"][0]["status"] == "FAIL"


def test_mock_evidence_cannot_be_reported_as_device_pass(tmp_path):
    setup, runner = _module("setup_lab"), _module("run")
    manifest = setup.prepare(tmp_path / "cases")
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    (evidence / "T01.json").write_text(json.dumps({"scope": "mock", "lab_id": "ninja-pce-six-router-isolated"}))
    report = runner.run(manifest, "integration", evidence, "T01")
    assert report["results"][0]["status"] == "INCONCLUSIVE"


def test_manifest_path_cannot_escape_its_directory(tmp_path):
    setup, runner = _module("setup_lab"), _module("run")
    manifest = setup.prepare(tmp_path / "cases")
    data = json.loads(manifest.read_text())
    data["cases"][0]["topology"] = "../outside.yml"
    manifest.write_text(json.dumps(data))
    report = runner.run(manifest, "offline", selected="T01")
    assert report["results"][0]["status"] == "FAIL"
    assert "inside" in report["results"][0]["reason"]


def test_missing_or_duplicate_manifest_cases_cannot_succeed(tmp_path):
    import pytest

    setup, runner = _module("setup_lab"), _module("run")
    manifest = setup.prepare(tmp_path / "cases")
    data = json.loads(manifest.read_text())
    data["cases"][1] = data["cases"][0]
    manifest.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="exactly one"):
        runner.run(manifest, "offline")
