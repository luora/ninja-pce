"""Authenticate and snapshot offline releases before using any supplied code.

This module uses only the standard library and the host's OpenSSL. Trust keys
are supplied independently by the operator, never taken from the release.
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import zipfile
from contextlib import contextmanager
from dataclasses import dataclass
from email.parser import BytesParser
from pathlib import Path, PurePosixPath
from typing import Any, Iterator

MANIFEST = "distribution-manifest.json"
SIGNATURE = "distribution-manifest.sig"
METADATA = {MANIFEST, SIGNATURE, "SHA256SUMS"}
TARGET = {"os": "linux", "arch": "amd64", "distribution": "ubuntu", "version": "24.04", "python": "3.12"}
GOBGP_COMMIT = "9bc8e113e4de6b5542cfafe0c64403b129f4e544"
GOBGP_VERSION = f"3.37.0-ninja-node-identity.1+commit.{GOBGP_COMMIT}"
GRPCURL_SOURCE = ("https://github.com/fullstorydev/grpcurl/releases/download/v1.9.3/"
                  "grpcurl_1.9.3_linux_x86_64.tar.gz")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_RELATIVE = re.compile(r"[A-Za-z0-9_.+/-]+\Z")
_MAX_FILE = 512 * 1024 * 1024
_MAX_BUNDLE = 2 * 1024 * 1024 * 1024


def regular_file(path: Path, *, maximum: int = _MAX_FILE) -> bytes:
    """Read a bounded ordinary file without following its final symlink."""
    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > maximum:
            raise ValueError(f"Bundle input must be a bounded regular file: {path}")
        contents = stream.read(maximum + 1)
    if len(contents) > maximum:
        raise ValueError(f"Bundle input exceeds its size limit: {path}")
    return contents


def _json(contents: bytes) -> dict[str, Any]:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise ValueError(f"Duplicate bundle JSON field: {key}")
            result[key] = value
        return result

    def finite(value: str) -> Any:
        raise ValueError(f"Bundle JSON cannot contain {value}")

    result = json.loads(contents, object_pairs_hook=pairs, parse_constant=finite)
    if not isinstance(result, dict):
        raise ValueError("Bundle JSON must be an object")
    return result


def read_json(path: Path) -> dict[str, Any]:
    return _json(regular_file(path, maximum=1024 * 1024))


def artifact_path(name: str) -> PurePosixPath:
    if not isinstance(name, str) or not _RELATIVE.fullmatch(name):
        raise ValueError(f"Invalid bundle artifact path: {name!r}")
    path = PurePosixPath(name)
    if path.is_absolute() or any(part in {".", ".."} for part in name.split("/")) or str(path) != name:
        raise ValueError(f"Invalid bundle artifact path: {name!r}")
    return path


def validate_native(binary: Path, provenance: Path, component: str) -> dict[str, Any]:
    """Check the qualified binary contract without executing native input."""
    contents = regular_file(binary)
    if (len(contents) < 64 or contents[:6] != b"\x7fELF\x02\x01" or
            struct.unpack_from("<H", contents, 16)[0] not in {2, 3} or
            struct.unpack_from("<H", contents, 18)[0] != 62):
        raise ValueError(f"{component} must be a Linux amd64 ELF executable")
    data = read_json(provenance)
    expected = {"schema_version": 1, "component": component, "target_os": "linux", "target_arch": "amd64",
                "binary_sha256": hashlib.sha256(contents).hexdigest()}
    if type(data.get("schema_version")) is not int or any(data.get(key) != value for key, value in expected.items()):
        raise ValueError(f"{component} provenance does not match the binary or supported target")
    if component == "gobgpd":
        pins = {
            "gobgp_version": "3.37.0", "gobgp_commit": GOBGP_COMMIT,
            "source_archive_sha256": "354f4b46f94d08c6f5f064db319a4b029b60e410955d89e96e8ea5552d7aa58a",
            "patch_file": "0001-bgpls-node-canonical-identity.patch",
            "patch_sha256": "ff833d21abe13098a87be92a960ec3b018359748b686691527b70af5e696ce10",
            "go_version": "go1.24.1",
            "go_toolchain_sha256": "cb2396bae64183cdccf81a9a6df0aea3bce9511fc21469fb89a0c00470088073",
            "binary_version": GOBGP_VERSION,
        }
        tests = data.get("tests")
        if (any(data.get(key) != value for key, value in pins.items()) or data.get("cgo_enabled") is not False or
                not isinstance(tests, dict) or any(tests.get(key) != "passed" for key in
                                                 ("negative_control", "packet", "table", "module_verify"))):
            raise ValueError("GoBGP provenance requires the qualified multi-area patch and passing build evidence")
    elif component == "grpcurl":
        if (data.get("version") != "1.9.3" or data.get("source_url") != GRPCURL_SOURCE or
                not isinstance(data.get("archive_sha256"), str) or not _DIGEST.fullmatch(data["archive_sha256"])):
            raise ValueError("grpcurl provenance requires the pinned 1.9.3 release and archive checksum")
    else:
        raise ValueError(f"Unsupported bundled component: {component}")
    return data


def validate_wheel(path: Path) -> None:
    """Disallow metadata that could make an offline pip invocation use a URL."""
    with zipfile.ZipFile(path) as archive:
        metadata = [name for name in archive.namelist() if name.endswith(".dist-info/METADATA")]
        if len(metadata) != 1 or archive.getinfo(metadata[0]).file_size > 1024 * 1024:
            raise ValueError(f"Wheel must have one bounded distribution metadata file: {path.name}")
        message = BytesParser().parsebytes(archive.read(metadata[0]))
        for dependency in message.get_all("Requires-Dist", []):
            if "@" in dependency or "://" in dependency or "\\" in dependency:
                raise ValueError(f"Offline wheels cannot contain direct URL dependencies: {path.name}")


def validate_constraints(path: Path) -> None:
    """Accept version pins, excluding pip options, URLs and recursive includes."""
    constraints = regular_file(path, maximum=1024 * 1024).decode("utf-8")
    for line in constraints.splitlines():
        if line.strip() and not line.lstrip().startswith("#") and not re.fullmatch(
                r"[A-Za-z0-9_.-]+==[A-Za-z0-9_.+!-]+(?:\s*;\s*[A-Za-z0-9_.<>=! '()\"-]+)?", line.strip()):
            raise ValueError("Offline constraints must contain only exact version pins")


@dataclass(frozen=True)
class VerifiedBundle:
    root: Path
    manifest: dict[str, Any]
    manifest_sha256: str

    @property
    def wheel(self) -> Path:
        wheels = list(self.root.glob("*.whl"))
        if len(wheels) != 1:
            raise ValueError("Offline bundle must contain exactly one application wheel")
        return wheels[0]

    @property
    def wheelhouse(self) -> Path:
        return self.root / "wheelhouse"

    def validate_host(self) -> None:
        """Render anywhere; installation is qualified only on this exact target."""
        release = platform.freedesktop_os_release() if sys.platform == "linux" else {}
        if (sys.platform != "linux" or platform.machine() not in {"x86_64", "amd64"} or
                release.get("ID") != "ubuntu" or release.get("VERSION_ID") != "24.04" or
                platform.python_implementation() != "CPython" or sys.version_info[:2] != (3, 12)):
            raise ValueError("Offline installation supports only Ubuntu 24.04 amd64 with CPython 3.12")


def _inventory(root: Path) -> set[str]:
    def fail(error: OSError) -> None:
        raise error

    files: set[str] = set()
    for directory, subdirectories, names in os.walk(root, followlinks=False, onerror=fail):
        parent = Path(directory)
        for name in subdirectories:
            if (parent / name).is_symlink():
                raise ValueError(f"Bundle directories cannot be symbolic links: {parent / name}")
        for name in names:
            path = parent / name
            if not stat.S_ISREG(path.lstat().st_mode):
                raise ValueError(f"Bundle artifacts must be regular files: {path}")
            files.add(path.relative_to(root).as_posix())
    return files


@contextmanager
def verified_bundle(directory: Path, trusted_key: Path) -> Iterator[VerifiedBundle]:
    """Authenticate exact manifest bytes, then checksum private staged copies.

    Consumers keep this context open until installation ends. Original bundle
    files are never executed or passed to pip, even if modified after validation.
    """
    root = directory.absolute()
    if not root.is_dir() or any(path.is_symlink() for path in (root, *root.parents)):
        raise ValueError("--bundle must identify an extracted directory without symbolic links")
    key = trusted_key.absolute()
    if key.resolve().is_relative_to(root.resolve()):
        raise ValueError("The release trust key must be distributed independently outside the bundle")
    manifest_bytes = regular_file(root / MANIFEST, maximum=1024 * 1024)
    signature = regular_file(root / SIGNATURE, maximum=16 * 1024)
    key_bytes = regular_file(key, maximum=64 * 1024)
    with tempfile.TemporaryDirectory(prefix="ninja-pce-verified-") as temporary:
        private = Path(temporary)
        manifest_path = private / MANIFEST
        manifest_path.write_bytes(manifest_bytes)
        signature_path = private / SIGNATURE
        signature_path.write_bytes(signature)
        trust = private / "release-trust.pem"
        trust.write_bytes(key_bytes)
        try:
            result = subprocess.run(["openssl", "dgst", "-sha256", "-verify", str(trust),
                                     "-signature", str(signature_path), str(manifest_path)],
                                    capture_output=True, timeout=15, check=False)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise ValueError(f"Cannot verify release signature with host OpenSSL: {error}") from error
        if result.returncode != 0:
            raise ValueError("Release manifest signature verification failed")
        document = _json(manifest_bytes)
        if (type(document.get("version")) is not int or document["version"] != 2 or
                document.get("profile") != "offline-controller" or document.get("target") != TARGET):
            raise ValueError("Unsupported offline release manifest profile or target")
        artifacts = document.get("artifacts")
        if not isinstance(artifacts, dict) or not 1 <= len(artifacts) <= 10000:
            raise ValueError("Release manifest requires a bounded artifact inventory")
        for name, digest in artifacts.items():
            artifact_path(name)
            if name in METADATA or not isinstance(digest, str) or not _DIGEST.fullmatch(digest):
                raise ValueError(f"Invalid release artifact checksum: {name}")
        if _inventory(root) - METADATA != set(artifacts):
            raise ValueError("Release artifact inventory differs from the signed manifest")
        payload = private / "payload"
        payload.mkdir(mode=0o700)
        total = 0
        for name, digest in artifacts.items():
            path = root / name
            if any(parent.is_symlink() for parent in path.parents if parent != root):
                raise ValueError(f"Bundle artifact contains a symbolic link: {name}")
            contents = regular_file(path)
            total += len(contents)
            if total > _MAX_BUNDLE:
                raise ValueError("Offline bundle exceeds the supported size limit")
            if hashlib.sha256(contents).hexdigest() != digest:
                raise ValueError(f"Release artifact checksum mismatch: {name}")
            target = payload / name
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            target.write_bytes(contents)
        components = document.get("components")
        if not isinstance(components, dict) or set(components) != {"gobgpd", "grpcurl"}:
            raise ValueError("Offline bundle requires exactly gobgpd and grpcurl components")
        for component, version in (("gobgpd", GOBGP_VERSION), ("grpcurl", "1.9.3")):
            expected = {"path": f"bin/{component}", "provenance": f"provenance/{component}.json", "version": version}
            if components[component] != expected or any(expected[key] not in artifacts for key in ("path", "provenance")):
                raise ValueError(f"Invalid offline component contract: {component}")
            validate_native(payload / expected["path"], payload / expected["provenance"], component)
            (payload / expected["path"]).chmod(0o755)
        if "deploy/constraints.txt" not in artifacts:
            raise ValueError("Offline bundle requires signed dependency constraints")
        # Constraint files must be simple version pins; no pip options, includes,
        # editable projects, filesystem references or direct URL requirements.
        validate_constraints(payload / "deploy/constraints.txt")
        bundle = VerifiedBundle(payload, document, hashlib.sha256(manifest_bytes).hexdigest())
        validate_wheel(bundle.wheel)
        with zipfile.ZipFile(bundle.wheel) as archive:
            metadata = next(name for name in archive.namelist() if name.endswith(".dist-info/METADATA"))
            message = BytesParser().parsebytes(archive.read(metadata))
            if (message.get("Name", "").lower().replace("_", "-") != "ninja-pce" or
                    message.get("Version") != document.get("application_version")):
                raise ValueError("Application wheel does not match signed release name/version")
        if not bundle.wheelhouse.is_dir() or not list(bundle.wheelhouse.iterdir()):
            raise ValueError("Offline bundle requires a nonempty wheelhouse")
        for wheel in bundle.wheelhouse.iterdir():
            if not wheel.is_file() or wheel.suffix != ".whl":
                raise ValueError("Offline wheelhouse must contain only wheels")
            validate_wheel(wheel)
        # Preserve the authenticated statement alongside installed provenance.
        shutil.copyfile(manifest_path, payload / MANIFEST)
        shutil.copyfile(signature_path, payload / SIGNATURE)
        yield bundle
