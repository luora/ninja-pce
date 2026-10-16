#!/usr/bin/env python3
"""Bootstrap the checkout's isolated application, then replace this process with it."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import venv
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

ROOT = Path(__file__).resolve().parents[1]
HELP = """Ninja-PCE local application

Usage: ./ninja-pce [command] [options]

  (no command)       Start the sample dashboard at http://127.0.0.1:8088
  demo [--port PORT] Start the sample on another loopback port
  setup             Install the application without starting it
  setup --southbound Also install optional SSH adapter dependencies
  init              Create a new network workspace; edit its example configuration
  doctor            Validate workspace configuration and local prerequisites
  start             Run the live controller using your workspace configuration
  status            Inspect the configured controller

init accepts --directory; doctor/start/status accept --config.
Other commands are passed to the installed Ninja-PCE CLI.
The first installation needs Internet access. Local data stays in .ninja-pce/.
See docs/getting_started.md for network configuration and deployment.
"""
SHORTCUTS = {"demo", "init", "doctor", "start", "status"}


def environment() -> dict[str, str]:
    result = dict(os.environ)
    for key in ("PYTHONPATH", "PYTHONHOME", "PYTHONUSERBASE", "VIRTUAL_ENV", "PIP_TARGET", "PIP_PREFIX", "PIP_USER"):
        result.pop(key, None)
    return result


def source_digest(root: Path) -> str:
    paths = [root / name for name in ("pyproject.toml", "README.md", "MANIFEST.in")]
    paths.extend(path for path in (root / "src/ninja_pce").rglob("*")
                 if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc")
    digest = hashlib.sha256()
    for path in sorted(paths):
        if path.is_symlink():
            raise ValueError(f"Packaged source cannot be a symbolic link: {path}")
        digest.update(str(path.relative_to(root)).encode() + b"\0")
        digest.update(path.read_bytes() + b"\0")
    return digest.hexdigest()


def dependency_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for name in ("pyproject.toml", "deploy/constraints.txt", "deploy/launcher-constraints.txt"):
        digest.update((root / name).read_bytes() + b"\0")
    return digest.hexdigest()


def read_manifest(path: Path) -> dict[str, Any] | None:
    if path.is_symlink():
        raise ValueError("The application installation manifest cannot be a symbolic link")
    if not path.exists():
        return None
    if not path.is_file() or path.stat().st_size > 65536:
        raise ValueError("Invalid local application installation manifest")
    data = json.loads(path.read_bytes())
    if not isinstance(data, dict) or data.get("schema") != 1 or type(data.get("ready")) is not bool:
        raise ValueError("Invalid local application installation manifest")
    return data


def write_manifest(path: Path, data: dict[str, Any]) -> None:
    if path.is_symlink():
        raise ValueError("The application installation manifest cannot be a symbolic link")
    descriptor, temporary = tempfile.mkstemp(prefix=".installation-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w") as stream:
            json.dump(data, stream, sort_keys=True, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextmanager
def lock(path: Path, operation: int) -> Iterator[int]:
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(descriptor, operation)
        yield descriptor
    finally:
        os.close(descriptor)


def run(arguments: list[str], root: Path) -> None:
    subprocess.run(arguments, cwd=root, env=environment(), check=True)


def prepare(root: Path, *, southbound: bool = False) -> tuple[Path, int]:
    """Install only owned environment files; return a shared lock for the runtime.

    The caller inherits the runtime lock through exec. Package replacement is
    refused while another launcher-owned application uses this environment.
    """
    base = root / ".ninja-pce"
    if base.is_symlink() or base.exists() and not base.is_dir():
        raise ValueError(".ninja-pce must be a local directory, not a symbolic link or file")
    base.mkdir(mode=0o700, exist_ok=True)
    runtime = os.open(base / ".runtime.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        with lock(base / ".installation.lock", fcntl.LOCK_EX):
            manifest_path = base / "installation.json"
            previous = read_manifest(manifest_path)
            destination = base / "venv"
            if destination.is_symlink() or destination.exists() and not destination.is_dir():
                raise ValueError("The application environment cannot be a symbolic link or file")
            if destination.exists() and previous is None:
                raise ValueError("Existing .ninja-pce/venv has no installation record; choose a fresh checkout")
            python_version = list(sys.version_info[:2])
            if previous and previous.get("python") != python_version:
                raise ValueError("This environment uses another Python version; launch with its original interpreter")
            source = source_digest(root)
            dependencies = dependency_digest(root)
            extras = southbound or bool(previous and previous.get("southbound"))
            python = destination / "bin/python"
            existing_python = python.is_file()
            update = (not previous or not previous["ready"] or not python.is_file()
                      or previous.get("source") != source or previous.get("dependencies") != dependencies
                      or previous.get("southbound") != extras)
            if update:
                try:
                    fcntl.flock(runtime, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as exc:
                    raise ValueError("Stop locally launched applications before updating this installation") from exc
                record = {"schema": 1, "ready": False, "python": python_version, "southbound": extras}
                write_manifest(manifest_path, record)
                if not python.is_file():
                    print("Creating the local application environment…", flush=True)
                    try:
                        venv.EnvBuilder(with_pip=True).create(destination)
                    except (OSError, subprocess.CalledProcessError) as exc:
                        raise ValueError("Python venv/pip support is required; install python3-venv on Debian/Ubuntu") from exc
                pip = [str(python), "-I", "-m", "pip", "--disable-pip-version-check"]
                constraints = ["--constraint", str(root / "deploy/constraints.txt"),
                               "--constraint", str(root / "deploy/launcher-constraints.txt")]
                refresh_dependencies = (not existing_python or not previous or not previous["ready"]
                                        or previous.get("dependencies") != dependencies
                                        or previous.get("southbound") != extras)
                package = str(root) + ("[southbound]" if extras else "")
                if refresh_dependencies:
                    print("Installing application dependencies and package…", flush=True)
                    run([*pip, "install", "--no-input", *constraints, "setuptools", "wheel", "packaging"], root)
                    run([*pip, "install", "--no-input", "--no-build-isolation", *constraints, package], root)
                else:
                    print("Updating the application from this checkout…", flush=True)
                    run([*pip, "install", "--no-input", "--no-index", "--no-deps", "--no-build-isolation", package], root)
                run([str(python), "-I", "-c", "import ninja_pce, networkx, yaml, jinja2"], root)
                write_manifest(manifest_path, {**record, "ready": True, "source": source, "dependencies": dependencies})
            fcntl.flock(runtime, fcntl.LOCK_SH)
        return python, runtime
    except BaseException:
        os.close(runtime)
        raise


def application_arguments(root: Path, arguments: list[str]) -> list[str]:
    values = arguments or ["demo"]
    if values[0] not in SHORTCUTS:
        return values
    action, *options = values
    flag = "--directory" if action == "init" else "--config"
    if action != "demo" and not any(value == flag or value.startswith(flag + "=") for value in options):
        target = root / ".ninja-pce/workspace"
        if action != "init":
            target = target / "config/controller.json"
        options.extend([flag, str(target)])
    return ["app", action, *options]


def main(arguments: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if arguments is None else arguments)
    if arguments in (["--help"], ["-h"]):
        print(HELP)
        return 0
    if arguments == ["--version"]:
        text = (ROOT / "src/ninja_pce/__init__.py").read_text()
        match = re.search(r'__version__\s*=\s*[\'"]([^\'"]+)', text)
        if match is None:
            raise ValueError("Application version is missing")
        print("ninja-pce " + match[1])
        return 0
    if sys.version_info < (3, 10):
        raise ValueError("Ninja-PCE needs Python 3.10 or later")
    setup = bool(arguments and arguments[0] == "setup")
    if setup and any(option != "--southbound" for option in arguments[1:]):
        raise ValueError("Usage: ./ninja-pce setup [--southbound]")
    python, runtime = prepare(ROOT, southbound=setup and "--southbound" in arguments)
    if setup:
        os.close(runtime)
        print("Ninja-PCE installed. Run ./ninja-pce to open the sample, or ./ninja-pce init for your network.")
        return 0
    os.set_inheritable(runtime, True)
    os.execve(str(python), [str(python), "-I", "-m", "ninja_pce.cli", *application_arguments(ROOT, arguments)],
              environment())
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        print(f"Application setup failed: {error}", file=sys.stderr)
        print("Your workspace is preserved. Resolve the error, then rerun ./ninja-pce setup.", file=sys.stderr)
        raise SystemExit(1)
