"""Release signatures, offline inputs and native provenance are trust boundaries."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import struct
import subprocess
import zipfile
from pathlib import Path

import pytest
from test_application_deployment import load_tool

bundle = load_tool("ninja_test_bundle", "deploy/bundle.py")


def native_inputs(root: Path, component: str) -> tuple[Path, Path]:
    binary = root / "bin" / component
    binary.parent.mkdir(exist_ok=True, parents=True)
    contents = bytearray(64)
    contents[:6] = b"\x7fELF\x02\x01"
    struct.pack_into("<HH", contents, 16, 2, 62)
    binary.write_bytes(contents)
    document = {
        "schema_version": 1, "component": component, "target_os": "linux", "target_arch": "amd64",
        "binary_sha256": hashlib.sha256(contents).hexdigest(),
    }
    if component == "gobgpd":
        document.update({
            "gobgp_version": "3.37.0", "gobgp_commit": bundle.GOBGP_COMMIT,
            "source_archive_sha256": "354f4b46f94d08c6f5f064db319a4b029b60e410955d89e96e8ea5552d7aa58a",
            "patch_file": "0001-bgpls-node-canonical-identity.patch",
            "patch_sha256": "ff833d21abe13098a87be92a960ec3b018359748b686691527b70af5e696ce10",
            "go_version": "go1.24.1",
            "go_toolchain_sha256": "cb2396bae64183cdccf81a9a6df0aea3bce9511fc21469fb89a0c00470088073",
            "binary_version": bundle.GOBGP_VERSION, "cgo_enabled": False,
            "tests": dict.fromkeys(("negative_control", "packet", "table", "module_verify"), "passed"),
        })
    else:
        document.update({"version": "1.9.3", "source_url": bundle.GRPCURL_SOURCE, "archive_sha256": "a" * 64})
    provenance = root / "provenance" / f"{component}.json"
    provenance.parent.mkdir(exist_ok=True)
    provenance.write_text(json.dumps(document))
    return binary, provenance


def wheel(path: Path, *, dependency: str = "") -> None:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("ninja_pce-0.7.0.dist-info/METADATA",
                         "Name: ninja-pce\nVersion: 0.7.0\n" + dependency)


@pytest.fixture(scope="module")
def signing_keys(tmp_path_factory):
    if shutil.which("openssl") is None:
        pytest.skip("host OpenSSL is unavailable")
    root = tmp_path_factory.mktemp("release-signing-test")
    private, public = root / "private.pem", root / "public.pem"
    subprocess.run(["openssl", "genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:2048",
                    "-out", str(private)], check=True, capture_output=True)
    subprocess.run(["openssl", "pkey", "-in", str(private), "-pubout", "-out", str(public)],
                   check=True, capture_output=True)
    return private, public


def sign(root: Path, private: Path, *, document: dict | None = None) -> dict:
    if document is None:
        document = {
            "version": 2, "profile": "offline-controller", "target": bundle.TARGET,
            "application_version": "0.7.0",
            "components": {
                component: {"path": f"bin/{component}", "provenance": f"provenance/{component}.json",
                            "version": version}
                for component, version in (("gobgpd", bundle.GOBGP_VERSION), ("grpcurl", "1.9.3"))
            },
            "artifacts": {path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                          for path in root.rglob("*") if path.is_file() and path.name not in bundle.METADATA},
        }
    (root / bundle.MANIFEST).write_text(json.dumps(document))
    subprocess.run(["openssl", "dgst", "-sha256", "-sign", str(private), "-out", str(root / bundle.SIGNATURE),
                    str(root / bundle.MANIFEST)], check=True, capture_output=True)
    return document


@pytest.fixture
def release(tmp_path, signing_keys):
    root = tmp_path / "release"
    root.mkdir()
    for component in ("gobgpd", "grpcurl"):
        native_inputs(root, component)
    (root / "deploy").mkdir()
    (root / "deploy/constraints.txt").write_text('networkx==3.7; python_version >= "3.12"\n')
    wheel(root / "ninja_pce-0.7.0-py3-none-any.whl")
    (root / "wheelhouse").mkdir()
    wheel(root / "wheelhouse/dependency-1.0-py3-none-any.whl")
    sign(root, signing_keys[0])
    return root


def test_verified_payload_is_a_private_snapshot_and_cleans_up(release, signing_keys):
    with bundle.verified_bundle(release, signing_keys[1]) as verified:
        snapshot = verified.root
        assert snapshot != release and snapshot.is_dir()
        assert verified.wheel.name == "ninja_pce-0.7.0-py3-none-any.whl"
        assert verified.manifest_sha256 == hashlib.sha256((release / bundle.MANIFEST).read_bytes()).hexdigest()
        expected = (snapshot / "bin/grpcurl").read_bytes()
        (release / "bin/grpcurl").write_bytes(b"changed after authentication")
        assert (snapshot / "bin/grpcurl").read_bytes() == expected
        assert (snapshot / "bin/grpcurl").stat().st_mode & 0o777 == 0o755
        assert (snapshot / bundle.SIGNATURE).read_bytes() == (release / bundle.SIGNATURE).read_bytes()
    assert not snapshot.exists()


@pytest.mark.parametrize("tamper", ["manifest", "signature", "binary", "missing", "extra"])
def test_tampered_release_is_rejected(release, signing_keys, tamper):
    if tamper == "manifest":
        (release / bundle.MANIFEST).write_text((release / bundle.MANIFEST).read_text() + " ")
    elif tamper == "signature":
        (release / bundle.SIGNATURE).write_bytes(b"invalid signature")
    elif tamper == "binary":
        (release / "bin/gobgpd").write_bytes(b"different executable")
    elif tamper == "missing":
        (release / "bin/grpcurl").unlink()
    else:
        (release / "unlisted.whl").write_bytes(b"unexpected executable input")
    with pytest.raises(ValueError):
        with bundle.verified_bundle(release, signing_keys[1]):
            pytest.fail("Unverified payload was exposed")


def test_trust_key_cannot_be_selected_from_release(release, signing_keys):
    key = release / "self-appointed-trust.pem"
    shutil.copyfile(signing_keys[1], key)
    with pytest.raises(ValueError, match="independently"):
        with bundle.verified_bundle(release, key):
            pytest.fail("Bundle supplied its own trust key")


@pytest.mark.parametrize("path", ["../outside", "/absolute", "bin/../outside", "bin//grpcurl", "./bin/grpcurl"])
def test_signed_manifest_cannot_escape_snapshot(release, signing_keys, path):
    document = json.loads((release / bundle.MANIFEST).read_text())
    document["artifacts"][path] = "0" * 64
    sign(release, signing_keys[0], document=document)
    with pytest.raises(ValueError, match="artifact path"):
        with bundle.verified_bundle(release, signing_keys[1]):
            pytest.fail("Manifest escaped its bundle")


@pytest.mark.parametrize("kind", ["symlink", "directory-symlink", "fifo"])
def test_special_files_cannot_enter_bundle(release, signing_keys, tmp_path, kind):
    if kind == "symlink":
        binary = release / "bin/grpcurl"
        external = tmp_path / "grpcurl"
        binary.rename(external)
        binary.symlink_to(external)
    elif kind == "directory-symlink":
        external = tmp_path / "provenance"
        (release / "provenance").rename(external)
        (release / "provenance").symlink_to(external, target_is_directory=True)
    else:
        os.mkfifo(release / "named-pipe")
    with pytest.raises(ValueError):
        with bundle.verified_bundle(release, signing_keys[1]):
            pytest.fail("Special file was followed")


def test_wrong_signing_key_rejected(release, tmp_path):
    key = tmp_path / "different-public-key.pem"
    subprocess.run(["openssl", "genpkey", "-algorithm", "EC", "-pkeyopt", "ec_paramgen_curve:P-256",
                    "-out", str(tmp_path / "different-private.pem")], check=True, capture_output=True)
    subprocess.run(["openssl", "pkey", "-in", str(tmp_path / "different-private.pem"), "-pubout", "-out", str(key)],
                   check=True, capture_output=True)
    with pytest.raises(ValueError, match="signature"):
        with bundle.verified_bundle(release, key):
            pytest.fail("Wrong release signer was accepted")


@pytest.mark.parametrize("field,value", [
    ("patch_sha256", "0" * 64), ("gobgp_commit", "0" * 40), ("cgo_enabled", True),
    ("binary_sha256", "0" * 64), ("target_arch", "arm64"), ("tests", {"packet": "passed"}),
])
def test_unqualified_native_provenance_rejected(release, signing_keys, field, value):
    path = release / "provenance/gobgpd.json"
    data = json.loads(path.read_text())
    data[field] = value
    path.write_text(json.dumps(data))
    sign(release, signing_keys[0])
    with pytest.raises(ValueError):
        with bundle.verified_bundle(release, signing_keys[1]):
            pytest.fail("Unqualified multi-area daemon was accepted")


@pytest.mark.parametrize("native", [b"#!/bin/sh\n", b"MZ" + b"\0" * 80])
def test_native_contract_rejects_non_elf_before_execution(release, native):
    binary = release / "bin/grpcurl"
    binary.write_bytes(native)
    with pytest.raises(ValueError, match="ELF"):
        bundle.validate_native(binary, release / "provenance/grpcurl.json", "grpcurl")


@pytest.mark.parametrize("input_kind", ["wheel-url", "constraint-url", "constraint-option", "sdist"])
def test_signed_inputs_cannot_escape_offline_installation(release, signing_keys, input_kind):
    if input_kind == "wheel-url":
        wheel(release / "wheelhouse/dependency-1.0-py3-none-any.whl",
              dependency="Requires-Dist: evil @ https://example.invalid/evil.whl\n")
    elif input_kind == "constraint-url":
        (release / "deploy/constraints.txt").write_text("dependency @ https://example.invalid/evil.whl\n")
    elif input_kind == "constraint-option":
        (release / "deploy/constraints.txt").write_text("--find-links https://example.invalid/\n")
    else:
        (release / "wheelhouse/source.tar.gz").write_bytes(b"source hook")
    sign(release, signing_keys[0])
    with pytest.raises(ValueError):
        with bundle.verified_bundle(release, signing_keys[1]):
            pytest.fail("A signed artifact could cause source building or network use")


@pytest.mark.parametrize("contents", [b'{"version":2,"version":2}', b'{"version":NaN}'])
def test_signed_ambiguous_json_rejected(release, signing_keys, contents):
    (release / bundle.MANIFEST).write_bytes(contents)
    subprocess.run(["openssl", "dgst", "-sha256", "-sign", str(signing_keys[0]), "-out",
                    str(release / bundle.SIGNATURE), str(release / bundle.MANIFEST)], check=True, capture_output=True)
    with pytest.raises(ValueError):
        with bundle.verified_bundle(release, signing_keys[1]):
            pytest.fail("Ambiguous signed metadata was accepted")


@pytest.mark.parametrize("release_info,machine,python", [
    ({"ID": "ubuntu", "VERSION_ID": "22.04"}, "x86_64", (3, 12)),
    ({"ID": "ubuntu", "VERSION_ID": "24.04"}, "aarch64", (3, 12)),
    ({"ID": "ubuntu", "VERSION_ID": "24.04"}, "x86_64", (3, 11)),
])
def test_unsupported_install_host_rejected(release_info, machine, python, monkeypatch, tmp_path):
    monkeypatch.setattr(bundle.platform, "freedesktop_os_release", lambda: release_info)
    monkeypatch.setattr(bundle.platform, "machine", lambda: machine)
    monkeypatch.setattr(bundle.sys, "version_info", python)
    verified = bundle.VerifiedBundle(tmp_path, {}, "0" * 64)
    with pytest.raises(ValueError, match="Ubuntu 24.04"):
        verified.validate_host()


def test_copied_standalone_installer_does_not_mutate_its_signed_inventory(release, signing_keys, tmp_path):
    import sys

    deploy_source = Path(__file__).resolve().parents[1] / "deploy"
    for name in ("install.py", "bundle.py"):
        shutil.copyfile(deploy_source / name, release / "deploy" / name)
    sign(release, signing_keys[0])
    before = {path.relative_to(release).as_posix() for path in release.rglob("*") if path.is_file()}
    environment = {name: value for name, value in os.environ.items()
                   if name not in {"PYTHONPATH", "PYTHONHOME", "PYTHONDONTWRITEBYTECODE"}}
    output = tmp_path / "review"
    result = subprocess.run([
        sys.executable, str(release / "deploy/install.py"), "--bundle", str(release),
        "--trusted-key", str(signing_keys[1]), "--output-dir", str(output),
    ], cwd=tmp_path, env=environment, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert (output / "ninja-gobgpd.service").is_file()
    assert "--component bgpls --component dashboard" in (output / "ninja-controller.service").read_text()
    assert {path.relative_to(release).as_posix() for path in release.rglob("*") if path.is_file()} == before
    assert not list(release.rglob("__pycache__"))


def test_signed_application_version_must_match_its_wheel(release, signing_keys):
    document = json.loads((release / bundle.MANIFEST).read_text())
    document["application_version"] = "99.0.0"
    sign(release, signing_keys[0], document=document)
    with pytest.raises(ValueError, match="name/version"):
        with bundle.verified_bundle(release, signing_keys[1]):
            pytest.fail("Contradictory signed release version was accepted")
