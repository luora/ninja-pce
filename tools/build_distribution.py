#!/usr/bin/env python3
"""Build reviewed application artifacts; network use is explicit and optional."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import sysconfig
import tarfile
import tempfile
import zipfile
from datetime import datetime, timezone
from email.parser import BytesParser
from pathlib import Path, PurePosixPath

OFFLINE_TARGET = {"os": "linux", "arch": "amd64", "distribution": "ubuntu",
                  "version": "24.04", "python": "3.12"}


def copy_regular(source: Path, target: Path) -> None:
    """Snapshot local inputs without following a final symlink or opening a FIFO."""
    descriptor = os.open(source, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        maximum = 512 * 1024 * 1024
        if not stat.S_ISREG(info.st_mode) or info.st_size > maximum:
            raise ValueError(f"Bundle input must be a bounded regular file: {source}")
        with target.open("wb") as destination:
            copied = 0
            while chunk := stream.read(1024 * 1024):
                copied += len(chunk)
                if copied > maximum:
                    raise ValueError(f"Bundle input exceeds its size limit: {source}")
                destination.write(chunk)


def wheel_metadata(wheel: Path):
    with zipfile.ZipFile(wheel) as archive:
        metadata = [name for name in archive.namelist() if name.endswith(".dist-info/METADATA")]
        if len(metadata) != 1 or archive.getinfo(metadata[0]).file_size > 1024 * 1024:
            raise ValueError(f"Wheel must contain one bounded metadata file: {wheel.name}")
        return BytesParser().parsebytes(archive.read(metadata[0]))


def application_version(wheel: Path) -> str:
    metadata = wheel_metadata(wheel)
    name = (metadata.get("Name") or "").lower().replace("_", "-")
    version = metadata.get("Version") or ""
    if name != "ninja-pce" or not re.fullmatch(r"[0-9][A-Za-z0-9_.+-]{0,63}", version):
        raise ValueError("Application wheel must identify a safely named ninja-pce release")
    return version


def offline_dependencies(wheel: Path, wheelhouse: Path, constraints: Path) -> None:
    from deploy.bundle import validate_constraints, validate_wheel

    # --no-index alone still permits direct URL dependencies. Reject them in
    # every local candidate before asking pip to resolve the offline set.
    for candidate in (wheel, *sorted(wheelhouse.glob("*.whl"))):
        validate_wheel(candidate)
    validate_constraints(constraints)
    environment = {name: value for name, value in os.environ.items() if not name.startswith("PIP_")}
    environment["PIP_CONFIG_FILE"] = os.devnull
    subprocess.run([sys.executable, "-I", "-m", "pip", "--isolated", "install", "--dry-run", "--ignore-installed",
                    "--disable-pip-version-check", "--no-index", "--only-binary=:all:",
                    "--find-links", str(wheelhouse), "--constraint", str(constraints), str(wheel)],
                   check=True, env=environment)


def checksum(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def safe_member(name: str) -> bool:
    path = PurePosixPath(name)
    return not path.is_absolute() and ".." not in path.parts


def inspect_artifacts(wheel: Path, source: Path) -> None:
    with zipfile.ZipFile(wheel) as archive:
        for name in archive.namelist():
            parts = PurePosixPath(name).parts
            if (not safe_member(name) or not parts or
                    not (parts[0] == "ninja_pce" or parts[0].endswith(".dist-info"))):
                raise ValueError(f"Unexpected wheel member: {name}")
    forbidden = {".git", ".env", ".venv", "venv", "build", "dist", "__pycache__", ".codex",
                 ".aws", ".ssh", ".ninja-pce", "node_modules"}
    with tarfile.open(source, "r:gz") as archive:
        for member in archive.getmembers():
            parts = PurePosixPath(member.name).parts
            if (not safe_member(member.name) or member.issym() or member.islnk() or
                    any(part in forbidden or part.startswith(".env.") for part in parts)):
                raise ValueError(f"Unexpected source archive member: {member.name}")


def revision(repo: Path, *, exclude: tuple[Path, ...] = ()) -> tuple[str | None, bool | None]:
    try:
        commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True,
                                text=True, capture_output=True).stdout.strip()
        command = ["git", "status", "--porcelain", "--", "."]
        for path in exclude:
            try:
                relative = path.resolve().relative_to(repo.resolve())
            except ValueError:
                continue
            # Exclude only this invocation's scratch, with literal path matching;
            # other untracked files and genuine source modifications still count.
            command.append(f":(top,exclude,literal){relative.as_posix()}")
        dirty = subprocess.run(command, cwd=repo, check=True,
                               text=True, capture_output=True).stdout != ""
        return commit, dirty
    except (OSError, subprocess.CalledProcessError):
        return None, None


def build(output: Path, *, download_dependencies: bool = False, constraints: Path | None = None,
          gobgpd: Path | None = None, gobgpd_provenance: Path | None = None,
          grpcurl: Path | None = None, grpcurl_provenance: Path | None = None,
          wheelhouse: Path | None = None, signing_key: Path | None = None) -> None:
    repo = Path(__file__).resolve().parents[1]
    output = output.absolute()
    if output.is_symlink() or output.exists() and (not output.is_dir() or any(output.iterdir())):
        raise ValueError("Output must be absent or an empty directory; artifacts are never overwritten")
    native_inputs = (gobgpd, gobgpd_provenance, grpcurl, grpcurl_provenance)
    offline = any(value is not None for value in native_inputs)
    if offline:
        if not all(value is not None for value in native_inputs) or signing_key is None:
            raise ValueError("Offline controller bundles require both native binaries, both provenance files and --signing-key")
        if wheelhouse is None and not download_dependencies:
            raise ValueError("Offline controller bundles require --wheelhouse or explicit --download-dependencies")
        if platform.system() != "Linux" or platform.machine() not in {"x86_64", "amd64"} or sys.version_info[:2] != (3, 12):
            raise ValueError("Offline controller bundles require a Linux amd64 / Python 3.12 build host")
        if signing_key.is_symlink() or not signing_key.is_file():
            raise ValueError("--signing-key must identify a regular private key file")
        if signing_key.resolve().is_relative_to(repo.resolve()) or signing_key.resolve().is_relative_to(output.resolve()):
            raise ValueError("Keep the release signing key outside the source checkout and output bundle")
    elif signing_key is not None:
        raise ValueError("--signing-key requires the complete offline native-tool inputs")
    if wheelhouse is not None and download_dependencies:
        raise ValueError("Use either a local --wheelhouse or --download-dependencies")
    if wheelhouse is not None and (wheelhouse.is_symlink() or not wheelhouse.is_dir()):
        raise ValueError("--wheelhouse must identify a local directory without a symlink")
    source_commit, source_dirty = revision(repo)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="ninja-pce-build-", dir=output.parent) as temporary:
        stage = Path(temporary) / "distribution"
        stage.mkdir()
        subprocess.run([sys.executable, "-m", "build", "--no-isolation", "--outdir", str(stage)],
                       cwd=repo, check=True)
        wheels = list(stage.glob("*.whl"))
        sources = list(stage.glob("*.tar.gz"))
        if len(wheels) != 1 or len(sources) != 1:
            raise ValueError("Expected exactly one application wheel and one source archive")
        inspect_artifacts(wheels[0], sources[0])
        deploy = stage / "deploy"
        shutil.copytree(repo / "deploy", deploy, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        if offline:
            if str(repo) not in sys.path:
                sys.path.insert(0, str(repo))
            from deploy.bundle import validate_constraints, validate_native

            copy_regular(constraints or repo / "deploy/constraints.txt", deploy / "constraints.txt")
            validate_constraints(deploy / "constraints.txt")
        shutil.copyfile(repo / "README.md", stage / "README.md")
        documentation = stage / "docs"
        documentation.mkdir()
        for document in sorted((repo / "docs").glob("*.md")):
            shutil.copyfile(document, documentation / document.name)
        references = repo / "gns3/controller/gobgp"
        for reference in sorted(references.rglob("*")):
            if reference.is_file() and reference.suffix in {".md", ".proto", ".sh", ".patch", ".toml", ".service"}:
                target = stage / reference.relative_to(repo)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(reference, target)
        examples = repo / "examples"
        for example in sorted(examples.rglob("*")):
            if (example.is_file() and "outputs" not in example.relative_to(examples).parts and
                    example.suffix in {".json", ".yml", ".yaml"}):
                target = stage / example.relative_to(repo)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(example, target)
        bundled_wheels = stage / "wheelhouse"
        if wheelhouse is not None:
            bundled_wheels.mkdir()
            candidates = sorted(wheelhouse.glob("*.whl"))
            if not candidates:
                raise ValueError("Local wheelhouse contains no runtime wheels")
            for candidate in candidates:
                copy_regular(candidate, bundled_wheels / candidate.name)
        if download_dependencies:
            bundled_wheels.mkdir()
            command = [sys.executable, "-m", "pip", "download", "--only-binary=:all:",
                       "--dest", str(bundled_wheels)]
            selected_constraints = deploy / "constraints.txt" if offline else constraints
            if selected_constraints is not None:
                command.extend(["--constraint", str(selected_constraints.absolute())])
            subprocess.run([*command, str(wheels[0])], check=True)
        components = {}
        if offline:
            # The copied deploy helper is also shipped with the bundle. Native
            # binaries are inspected as data and are never run by the builder.
            (stage / "bin").mkdir()
            (stage / "provenance").mkdir()
            for component, binary, provenance in (("gobgpd", gobgpd, gobgpd_provenance),
                                                   ("grpcurl", grpcurl, grpcurl_provenance)):
                assert binary is not None and provenance is not None
                bundled_binary = stage / "bin" / component
                bundled_provenance = stage / "provenance" / f"{component}.json"
                copy_regular(binary, bundled_binary)
                copy_regular(provenance, bundled_provenance)
                evidence = validate_native(bundled_binary, bundled_provenance, component)
                bundled_binary.chmod(0o755)
                components[component] = {
                    "path": f"bin/{component}", "provenance": f"provenance/{component}.json",
                    "version": evidence["binary_version"] if component == "gobgpd" else "1.9.3",
                }
            offline_dependencies(wheels[0], bundled_wheels, deploy / "constraints.txt")
        final_commit, final_dirty = revision(repo, exclude=(Path(temporary),))
        if source_commit is None or final_commit is None:
            commit, dirty = source_commit, None
        else:
            commit = source_commit
            dirty = bool(source_dirty or final_dirty or final_commit != source_commit)
        artifacts = {str(path.relative_to(stage)): checksum(path)
                     for path in sorted(stage.rglob("*")) if path.is_file()}
        manifest = {
            "version": 2 if offline else 1, "built_at": datetime.now(timezone.utc).isoformat(),
            "source_revision": commit, "source_dirty": dirty,
            "python": sys.version.split()[0], "platform": sysconfig.get_platform(),
            "dependencies_downloaded": download_dependencies,
            "artifacts": artifacts,
        }
        if offline:
            manifest.update(profile="offline-controller", target=OFFLINE_TARGET,
                            application_version=application_version(wheels[0]), components=components)
        manifest_path = stage / "distribution-manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
        sums = dict(artifacts)
        sums[manifest_path.name] = checksum(manifest_path)
        if offline:
            signature = stage / "distribution-manifest.sig"
            assert signing_key is not None
            # Keep private key material outside the payload, including while
            # staging. OpenSSL reads only the explicitly supplied local key.
            subprocess.run(["openssl", "dgst", "-sha256", "-sign", str(signing_key.absolute()),
                            "-out", str(signature), str(manifest_path)], check=True)
            sums[signature.name] = checksum(signature)
        (stage / "SHA256SUMS").write_text("".join(f"{digest}  {name}\n"
                                                  for name, digest in sorted(sums.items())))
        if output.exists():
            output.rmdir()
        stage.rename(output)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=Path("dist"))
    parser.add_argument("--download-dependencies", action="store_true",
                        help="Explicitly download a platform/Python-specific offline wheelhouse")
    parser.add_argument("--constraints", type=Path,
                        default=Path(__file__).resolve().parents[1] / "deploy/constraints.txt")
    parser.add_argument("--wheelhouse", type=Path, help="Copy an existing local directory of runtime wheels")
    parser.add_argument("--gobgpd", type=Path, help="Qualified patched Linux amd64 GoBGP binary")
    parser.add_argument("--gobgpd-provenance", type=Path, help="Reviewed GoBGP build provenance JSON")
    parser.add_argument("--grpcurl", type=Path, help="Qualified Linux amd64 grpcurl 1.9.3 binary")
    parser.add_argument("--grpcurl-provenance", type=Path, help="Reviewed grpcurl provenance JSON")
    parser.add_argument("--signing-key", type=Path, help="Release private key for a signed offline controller bundle")
    args = parser.parse_args(argv)
    try:
        build(args.output_dir, download_dependencies=args.download_dependencies, constraints=args.constraints,
              gobgpd=args.gobgpd, gobgpd_provenance=args.gobgpd_provenance,
              grpcurl=args.grpcurl, grpcurl_provenance=args.grpcurl_provenance,
              wheelhouse=args.wheelhouse, signing_key=args.signing_key)
        print(f"Distribution built in {args.output_dir.absolute()}")
        return 0
    except (OSError, ValueError, subprocess.CalledProcessError, zipfile.BadZipFile, tarfile.TarError) as error:
        print(f"Distribution build failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
