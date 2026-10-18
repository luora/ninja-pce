"""A signed native bundle is assembled from local verified inputs without running them."""
from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
import shutil
import struct
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("ninja_bundle_builder", ROOT / "tools/build_distribution.py")
assert SPEC is not None and SPEC.loader is not None
builder = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(builder)


def write_wheel(path: Path, *, direct_url: bool = False) -> None:
    metadata = "Metadata-Version: 2.1\nName: ninja-pce\nVersion: 0.8.0\n"
    if direct_url:
        metadata += "Requires-Dist: dependency @ https://example.invalid/dependency.whl\n"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("ninja_pce/__init__.py", "")
        archive.writestr("ninja_pce-0.8.0.dist-info/METADATA", metadata)


@pytest.fixture
def native_inputs(tmp_path):
    # Structurally valid ELF64 little-endian amd64 data, never executed.
    header = b"\x7fELF\x02\x01\x01" + b"\0" * 9
    header += struct.pack("<HHIQQQIHHHHHH", 2, 62, 1, 0, 0, 0, 0, 64, 56, 0, 64, 0, 0)
    gobgpd = tmp_path / "qualified-gobgpd"
    grpcurl = tmp_path / "qualified-grpcurl"
    gobgpd.write_bytes(header + b"qualified gobgpd fixture")
    grpcurl.write_bytes(header + b"qualified grpcurl fixture")
    gobgpd_provenance = tmp_path / "gobgpd.json"
    gobgpd_document = {
        "schema_version": 1, "component": "gobgpd", "gobgp_version": "3.37.0",
        "gobgp_commit": "9bc8e113e4de6b5542cfafe0c64403b129f4e544",
        "source_archive_sha256": "354f4b46f94d08c6f5f064db319a4b029b60e410955d89e96e8ea5552d7aa58a",
        "patch_file": "0001-bgpls-node-canonical-identity.patch",
        "patch_sha256": "ff833d21abe13098a87be92a960ec3b018359748b686691527b70af5e696ce10",
        "go_version": "go1.24.1",
        "go_toolchain_sha256": "cb2396bae64183cdccf81a9a6df0aea3bce9511fc21469fb89a0c00470088073",
        "binary_sha256": hashlib.sha256(gobgpd.read_bytes()).hexdigest(),
        "binary_version": "3.37.0-ninja-node-identity.1+commit.9bc8e113e4de6b5542cfafe0c64403b129f4e544",
        "target_os": "linux", "target_arch": "amd64", "cgo_enabled": False,
        "tests": {"negative_control": "passed", "packet": "passed", "table": "passed", "module_verify": "passed"},
    }
    gobgpd_provenance.write_text(json.dumps(gobgpd_document))
    grpcurl_provenance = tmp_path / "grpcurl.json"
    grpcurl_provenance.write_text(json.dumps({
        "schema_version": 1, "component": "grpcurl", "version": "1.9.3",
        "target_os": "linux", "target_arch": "amd64",
        "binary_sha256": hashlib.sha256(grpcurl.read_bytes()).hexdigest(),
        "archive_sha256": "a" * 64,
        "source_url": "https://github.com/fullstorydev/grpcurl/releases/download/v1.9.3/grpcurl_1.9.3_linux_x86_64.tar.gz",
    }))
    wheelhouse = tmp_path / "runtime-wheels"
    wheelhouse.mkdir()
    write_wheel(wheelhouse / "ninja_pce-0.8.0-py3-none-any.whl")
    # Non-wheel files are never imported into the payload.
    (wheelhouse / "unrelated.txt").write_text("outside the runtime set")
    return {"gobgpd": gobgpd, "gobgpd_provenance": gobgpd_provenance,
            "grpcurl": grpcurl, "grpcurl_provenance": grpcurl_provenance,
            "wheelhouse": wheelhouse}


@pytest.fixture
def signing_key(tmp_path):
    if shutil.which("openssl") is None:
        pytest.skip("OpenSSL is not installed")
    key = tmp_path / "release-private.pem"
    subprocess.run(["openssl", "genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:2048",
                    "-out", str(key)], check=True, capture_output=True)
    return key


@pytest.fixture
def fake_build(monkeypatch):
    commands = []
    real_run = subprocess.run
    monkeypatch.setattr(builder, "revision", lambda *a, **kw: ("f" * 40, False))
    monkeypatch.setattr(builder.platform, "system", lambda: "Linux")
    monkeypatch.setattr(builder.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(builder.sys, "version_info", (3, 12, 3))
    monkeypatch.setenv("PIP_FIND_LINKS", "https://example.invalid/host-pip-setting")
    monkeypatch.setenv("PIP_CONFIG_FILE", "/operator/host-pip.conf")

    def run(arguments, **kwargs):
        commands.append(arguments)
        if arguments[:3] == [sys.executable, "-m", "build"]:
            stage = Path(arguments[arguments.index("--outdir") + 1])
            write_wheel(stage / "ninja_pce-0.8.0-py3-none-any.whl")
            with tarfile.open(stage / "ninja_pce-0.8.0.tar.gz", "w:gz") as archive:
                content = b"reviewed source\n"
                member = tarfile.TarInfo("ninja_pce-0.8.0/README.md")
                member.size = len(content)
                archive.addfile(member, io.BytesIO(content))
            return SimpleNamespace(returncode=0)
        if "pip" in arguments:
            assert "--isolated" in arguments
            assert "--no-index" in arguments and "--only-binary=:all:" in arguments
            assert "--dry-run" in arguments and "--ignore-installed" in arguments
            assert "--disable-pip-version-check" in arguments
            assert Path(arguments[arguments.index("--constraint") + 1]).read_bytes() == (ROOT / "deploy/constraints.txt").read_bytes()
            assert {key: value for key, value in kwargs["env"].items() if key.startswith("PIP_")} == {"PIP_CONFIG_FILE": os.devnull}
            return SimpleNamespace(returncode=0)
        assert arguments[:3] == ["openssl", "dgst", "-sha256"] and arguments[3] in {"-sign", "-verify"}
        return real_run(arguments, **kwargs)

    monkeypatch.setattr(builder.subprocess, "run", run)
    return commands, real_run


def test_signed_offline_builder_binds_native_payload_and_uses_only_local_inputs(native_inputs, signing_key, fake_build, tmp_path):
    output = tmp_path / "release"
    builder.build(output, **native_inputs, signing_key=signing_key)
    manifest_path = output / "distribution-manifest.json"
    document = json.loads(manifest_path.read_text())
    assert document["version"] == 2
    assert document["profile"] == "offline-controller"
    assert document["application_version"] == "0.8.0"
    assert document["target"] == builder.OFFLINE_TARGET
    assert document["dependencies_downloaded"] is False
    assert document["components"]["grpcurl"] == {
        "path": "bin/grpcurl", "provenance": "provenance/grpcurl.json", "version": "1.9.3",
    }
    assert document["components"]["gobgpd"]["version"].startswith("3.37.0-ninja-node-identity.1+")
    assert (output / "bin/gobgpd").read_bytes() == native_inputs["gobgpd"].read_bytes()
    assert (output / "bin/grpcurl").stat().st_mode & 0o777 == 0o755
    actual_payload = {path.relative_to(output).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                      for path in output.rglob("*") if path.is_file()
                      and path.name not in {"distribution-manifest.json", "distribution-manifest.sig", "SHA256SUMS"}}
    assert document["artifacts"] == actual_payload
    assert not (output / "wheelhouse/unrelated.txt").exists()
    assert not any(path.name == signing_key.name for path in output.rglob("*"))
    commands, real_run = fake_build
    assert len([command for command in commands if "pip" in command]) == 1
    assert not any(str(native_inputs["gobgpd"]) in command or str(native_inputs["grpcurl"]) in command
                   for command in commands)
    public_key = tmp_path / "release-public.pem"
    real_run(["openssl", "pkey", "-in", str(signing_key), "-pubout", "-out", str(public_key)],
             check=True, capture_output=True)
    verification = real_run(["openssl", "dgst", "-sha256", "-verify", str(public_key), "-signature",
                             str(output / "distribution-manifest.sig"), str(manifest_path)], capture_output=True)
    assert verification.returncode == 0, verification.stderr
    from deploy.bundle import verified_bundle

    with verified_bundle(output, public_key) as verified:
        assert verified.manifest == document
        assert verified.wheel.name == "ninja_pce-0.8.0-py3-none-any.whl"
        assert (verified.root / "bin/gobgpd").read_bytes() == native_inputs["gobgpd"].read_bytes()


@pytest.mark.parametrize("missing", ["gobgpd", "gobgpd_provenance", "grpcurl", "grpcurl_provenance", "signing_key", "wheelhouse"])
def test_offline_profile_requires_complete_explicit_inputs(native_inputs, signing_key, missing, tmp_path, monkeypatch):
    arguments = {**native_inputs, "signing_key": signing_key}
    del arguments[missing]
    monkeypatch.setattr(builder.subprocess, "run", lambda *a, **kw: pytest.fail("Incomplete build ran a command"))
    with pytest.raises(ValueError, match="require"):
        builder.build(tmp_path / "release", **arguments)
    assert not (tmp_path / "release").exists()


def test_offline_profile_rejects_wrong_build_target_before_build(native_inputs, signing_key, tmp_path, monkeypatch):
    monkeypatch.setattr(builder.platform, "machine", lambda: "aarch64")
    monkeypatch.setattr(builder.subprocess, "run", lambda *a, **kw: pytest.fail("Wrong-target build ran a command"))
    with pytest.raises(ValueError, match="Linux amd64 / Python 3.12"):
        builder.build(tmp_path / "release", **native_inputs, signing_key=signing_key)


def test_offline_builder_rejects_tampered_native_input(native_inputs, signing_key, fake_build, tmp_path):
    native_inputs["gobgpd"].write_bytes(native_inputs["gobgpd"].read_bytes() + b"tampered")
    with pytest.raises(ValueError, match="provenance"):
        builder.build(tmp_path / "release", **native_inputs, signing_key=signing_key)
    assert not (tmp_path / "release").exists()
    assert not any("pip" in command or command[0] == "openssl" for command in fake_build[0])


@pytest.mark.parametrize("kind", ["symlink", "fifo"])
def test_wheelhouse_rejects_nonregular_wheel_inputs(native_inputs, signing_key, fake_build, tmp_path, kind):
    candidate = native_inputs["wheelhouse"] / "unsafe-1.0-py3-none-any.whl"
    if kind == "symlink":
        candidate.symlink_to(native_inputs["wheelhouse"] / "ninja_pce-0.8.0-py3-none-any.whl")
    else:
        os.mkfifo(candidate)
    with pytest.raises((ValueError, OSError)):
        builder.build(tmp_path / "release", **native_inputs, signing_key=signing_key)
    assert not (tmp_path / "release").exists()


def test_offline_builder_rejects_direct_url_dependencies_without_pip(native_inputs, signing_key, fake_build, tmp_path):
    write_wheel(native_inputs["wheelhouse"] / "ninja_pce-0.8.0-py3-none-any.whl", direct_url=True)
    with pytest.raises(ValueError, match="direct URL"):
        builder.build(tmp_path / "release", **native_inputs, signing_key=signing_key)
    assert not any("pip" in command for command in fake_build[0])
    assert not (tmp_path / "release").exists()


def test_offline_builder_does_not_publish_when_dependencies_are_incomplete(native_inputs, signing_key, fake_build, tmp_path, monkeypatch):
    run = builder.subprocess.run

    def incomplete(arguments, **kwargs):
        if "pip" in arguments:
            raise subprocess.CalledProcessError(1, arguments)
        return run(arguments, **kwargs)

    monkeypatch.setattr(builder.subprocess, "run", incomplete)
    with pytest.raises(subprocess.CalledProcessError):
        builder.build(tmp_path / "release", **native_inputs, signing_key=signing_key)
    assert not (tmp_path / "release").exists()
    assert not any(command[0] == "openssl" for command in fake_build[0])


def test_offline_builder_rejects_constraint_options_before_pip(native_inputs, signing_key, fake_build, tmp_path):
    constraints = tmp_path / "unsafe-constraints.txt"
    constraints.write_text("--find-links https://example.invalid/remote\n")
    with pytest.raises(ValueError, match="exact version pins"):
        builder.build(tmp_path / "release", **native_inputs, signing_key=signing_key, constraints=constraints)
    assert not any("pip" in command for command in fake_build[0])
    assert not (tmp_path / "release").exists()


def test_signing_key_cannot_be_inside_source_checkout(native_inputs, signing_key, fake_build, tmp_path, monkeypatch):
    repository = tmp_path / "source"
    repository.mkdir()
    unsafe_key = repository / "ignored-private-key.pem"
    unsafe_key.write_bytes(signing_key.read_bytes())
    monkeypatch.setattr(builder, "__file__", str(repository / "tools/build_distribution.py"))
    monkeypatch.setattr(builder.subprocess, "run", lambda *a, **kw: pytest.fail("Private-key leak check ran a command"))
    with pytest.raises(ValueError, match="outside the source checkout"):
        builder.build(tmp_path / "release", **native_inputs, signing_key=unsafe_key)


def test_signing_failure_does_not_publish_an_unsigned_release(native_inputs, signing_key, fake_build, tmp_path, monkeypatch):
    run = builder.subprocess.run

    def signing_failed(arguments, **kwargs):
        if arguments[0] == "openssl":
            raise subprocess.CalledProcessError(1, arguments)
        return run(arguments, **kwargs)

    monkeypatch.setattr(builder.subprocess, "run", signing_failed)
    with pytest.raises(subprocess.CalledProcessError):
        builder.build(tmp_path / "release", **native_inputs, signing_key=signing_key)
    assert not (tmp_path / "release").exists()
