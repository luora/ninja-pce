"""Local host evidence must fail closed without executing inspected binaries."""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

import pytest

from ninja_pce import diagnostics_host as host


def codes(findings):
    return {item["code"] for item in findings}


@pytest.fixture
def release(tmp_path):
    (tmp_path / "bin").mkdir()
    (tmp_path / "provenance").mkdir()
    binary = tmp_path / "bin/grpcurl"
    binary.write_bytes(b"TEST ONLY inert bytes; never execute")
    binary.chmod(0o700)
    digest = hashlib.sha256(binary.read_bytes()).hexdigest()
    provenance = {"schema_version": 1, "component": "grpcurl", "target_os": "linux", "target_arch": "amd64",
                  "version": "1.9.3", "binary_sha256": digest, "archive_sha256": "a" * 64,
                  "source_url": "https://github.com/fullstorydev/grpcurl/releases/download/v1.9.3/grpcurl_1.9.3_linux_x86_64.tar.gz"}
    document = tmp_path / "provenance/grpcurl.json"
    document.write_text(json.dumps(provenance))
    inventory = {"artifacts": {"bin/grpcurl": digest,
                              "provenance/grpcurl.json": hashlib.sha256(document.read_bytes()).hexdigest()},
                 "components": {"grpcurl": {"path": "bin/grpcurl", "provenance": "provenance/grpcurl.json"}}}
    manifest = tmp_path / "provenance/distribution-manifest.json"
    manifest.write_text(json.dumps(inventory))
    (tmp_path / "provenance/distribution-manifest.sig").write_text("TEST ONLY unverified signature")
    (tmp_path / "deployment-manifest.json").write_text(json.dumps({"bundle_manifest_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest()}))
    return tmp_path, binary


def test_local_hash_matches_never_authenticate_a_signature(release, monkeypatch):
    directory, binary = release
    monkeypatch.setattr(host, "_run", lambda *args, **kwargs: pytest.fail("Executed inspected binary"))
    result = host.artifact_findings(grpcurl=str(binary), release_dir=directory)
    assert {"GRPCURL_PROVENANCE_HASH_MATCH", "GRPCURL_INVENTORY_HASH_MATCH",
            "RELEASE_SIGNATURE_AUTHENTICATION_UNKNOWN"} <= codes(result)
    unknown = next(item for item in result if item["code"] == "RELEASE_SIGNATURE_AUTHENTICATION_UNKNOWN")
    assert unknown["severity"] == "unknown" and unknown["evidence"]["signature_present"] is True
    assert unknown["evidence"]["independent_trust_root_checked"] is False


def test_changed_binary_and_manifest_are_identified_separately(release):
    directory, binary = release
    binary.write_bytes(b"TEST ONLY replacement")
    (directory / "deployment-manifest.json").write_text('{"bundle_manifest_sha256":"incorrect"}')
    result = host.artifact_findings(grpcurl=str(binary), release_dir=directory)
    assert {"GRPCURL_PROVENANCE_HASH_MISMATCH", "GRPCURL_INVENTORY_HASH_MISMATCH",
            "RELEASE_MANIFEST_LINK_MISMATCH"} <= codes(result)
    assert "GRPCURL_PROVENANCE_HASH_MATCH" not in codes(result)


@pytest.mark.parametrize("contents", ['{"binary_sha256":"a","binary_sha256":"b"}', '{"number":NaN}', '{"number":Infinity}'])
def test_ambiguous_provenance_is_unknown(release, contents):
    directory, binary = release
    (directory / "provenance/grpcurl.json").write_text(contents)
    result = host.artifact_findings(grpcurl=str(binary), release_dir=directory)
    assert "GRPCURL_PROVENANCE_UNKNOWN" in codes(result)
    assert "GRPCURL_PROVENANCE_HASH_MATCH" not in codes(result)


@pytest.mark.parametrize("unsafe", ["symlink", "size_limit"])
def test_uninspectable_provenance_does_not_claim_a_hash(release, monkeypatch, unsafe):
    directory, binary = release
    if unsafe == "symlink":
        document = directory / "provenance/grpcurl.json"
        document.rename(directory / "original.json")
        document.symlink_to(directory / "original.json")
    else:
        monkeypatch.setattr(host, "_MAX_BINARY", 4)
    result = host.artifact_findings(grpcurl=str(binary), release_dir=directory)
    assert "GRPCURL_PROVENANCE_UNKNOWN" in codes(result)
    assert "GRPCURL_PROVENANCE_HASH_MATCH" not in codes(result)


def test_state_path_write_exposure_and_symlinks_are_reported_without_changes(tmp_path):
    path = tmp_path / "state"
    path.mkdir(mode=0o777)
    path.chmod(0o777)
    assert "HOST_PATH_WRITABLE_BY_OTHERS" in codes(host._path_findings(path))
    assert path.stat().st_mode & 0o777 == 0o777
    link = tmp_path / "linked"
    link.symlink_to(path, target_is_directory=True)
    assert "HOST_PATH_UNSAFE_OR_UNREADABLE" in codes(host._path_findings(link))
    assert list(path.iterdir()) == []


@pytest.mark.parametrize("flags,expected", [(["UP"], "HOST_ADDRESS_ASSIGNED"), ([], "HOST_ADDRESS_INTERFACE_DOWN"),
                                           ("UP", "HOST_ADDRESS_ASSIGNMENT_UNKNOWN"), (None, "HOST_ADDRESS_ASSIGNMENT_UNKNOWN")])
def test_address_assignment_requires_valid_local_metadata(monkeypatch, flags, expected):
    monkeypatch.setattr(host, "_executable", lambda name: Path("/usr/bin/ip"))
    monkeypatch.setattr(host, "_run", lambda *args, **kwargs: (json.dumps([{"flags": flags,
                         "addr_info": [{"local": "192.0.2.10"}]}]).encode(), None))
    result = host._address_findings(["192.0.2.10"], live=True, deadline=time.monotonic() + 2)
    assert codes(result) == {expected}


def test_absent_address_is_distinct_from_unavailable_inventory(monkeypatch):
    monkeypatch.setattr(host, "_executable", lambda name: Path("/usr/bin/ip"))
    monkeypatch.setattr(host, "_run", lambda *args, **kwargs: (b"[]", None))
    assert codes(host._address_findings(["192.0.2.10"], live=True, deadline=time.monotonic() + 2)) == {"HOST_ADDRESS_NOT_ASSIGNED"}
    monkeypatch.setattr(host, "_run", lambda *args, **kwargs: (b"", "timeout"))
    assert codes(host._address_findings(["192.0.2.10"], live=True, deadline=time.monotonic() + 2)) == {"HOST_ADDRESS_ASSIGNMENT_UNKNOWN"}


@pytest.mark.parametrize("metadata,expected", [(b"NTPSynchronized=yes\nNTP=yes\n", "HOST_TIME_SYNCHRONIZED"),
    (b"NTPSynchronized=no\nNTP=yes\n", "HOST_TIME_UNSYNCHRONIZED"),
    (b"NTP=yes\n", "HOST_TIME_SYNC_UNKNOWN"),
    (b"NTPSynchronized=yes\nNTPSynchronized=no\n", "HOST_TIME_SYNC_UNKNOWN")])
def test_enabled_time_service_alone_never_proves_synchronization(monkeypatch, metadata, expected):
    monkeypatch.setattr(host, "_executable", lambda name: Path("/usr/bin/timedatectl"))
    monkeypatch.setattr(host, "_run", lambda *args, **kwargs: (metadata, None))
    assert host._clock_finding(live=True, deadline=time.monotonic() + 2)["code"] == expected


@pytest.mark.parametrize("enabled,expected", [("enabled", "SERVICE_BOOT_ENABLED"), ("enabled-runtime", "SERVICE_BOOT_START_UNCONFIRMED"),
                                           ("disabled", "SERVICE_BOOT_START_UNCONFIRMED"), ("masked", "SERVICE_BOOT_START_UNCONFIRMED")])
def test_running_and_boot_persistence_are_separate(monkeypatch, enabled, expected):
    monkeypatch.setattr(host, "_executable", lambda name: Path("/usr/bin/systemctl"))
    monkeypatch.setattr(host, "_run", lambda *args, **kwargs: (f"LoadState=loaded\nActiveState=active\nSubState=running\nResult=success\nMainPID=42\nUnitFileState={enabled}\n".encode(), None))
    result = host.service_findings(("ninja-admin.service",), live=True)
    assert {"SERVICE_RUNNING", expected} <= codes(result)
    if enabled != "enabled":
        assert "SERVICE_BOOT_ENABLED" not in codes(result)
