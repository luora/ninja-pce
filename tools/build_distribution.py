#!/usr/bin/env python3
"""Build reviewed application artifacts; network use is explicit and optional."""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import sysconfig
import tarfile
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath


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
                 ".aws", ".ssh", "node_modules"}
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


def build(output: Path, *, download_dependencies: bool = False, constraints: Path | None = None) -> None:
    repo = Path(__file__).resolve().parents[1]
    output = output.absolute()
    if output.is_symlink() or output.exists() and (not output.is_dir() or any(output.iterdir())):
        raise ValueError("Output must be absent or an empty directory; artifacts are never overwritten")
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
        if download_dependencies:
            wheelhouse = stage / "wheelhouse"
            wheelhouse.mkdir()
            command = [sys.executable, "-m", "pip", "download", "--only-binary=:all:",
                       "--dest", str(wheelhouse)]
            if constraints is not None:
                command.extend(["--constraint", str(constraints.absolute())])
            subprocess.run([*command, str(wheels[0])], check=True)
        final_commit, final_dirty = revision(repo, exclude=(Path(temporary),))
        if source_commit is None or final_commit is None:
            commit, dirty = source_commit, None
        else:
            commit = source_commit
            dirty = bool(source_dirty or final_dirty or final_commit != source_commit)
        artifacts = {str(path.relative_to(stage)): checksum(path)
                     for path in sorted(stage.rglob("*")) if path.is_file()}
        manifest = {
            "version": 1, "built_at": datetime.now(timezone.utc).isoformat(),
            "source_revision": commit, "source_dirty": dirty,
            "python": sys.version.split()[0], "platform": sysconfig.get_platform(),
            "dependencies_downloaded": download_dependencies,
            "artifacts": artifacts,
        }
        manifest_path = stage / "distribution-manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
        artifacts[manifest_path.name] = checksum(manifest_path)
        (stage / "SHA256SUMS").write_text("".join(f"{digest}  {name}\n"
                                                  for name, digest in sorted(artifacts.items())))
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
    args = parser.parse_args(argv)
    try:
        build(args.output_dir, download_dependencies=args.download_dependencies, constraints=args.constraints)
        print(f"Distribution built in {args.output_dir.absolute()}")
        return 0
    except (OSError, ValueError, subprocess.CalledProcessError, zipfile.BadZipFile, tarfile.TarError) as error:
        print(f"Distribution build failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
