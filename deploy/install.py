#!/usr/bin/env python3
"""Render a deployment for review, or explicitly install it without activation.

This installer consumes trusted local wheels. It never downloads dependencies,
changes firewall rules, overwrites configuration or journals, or starts services.
"""
from __future__ import annotations

import argparse
import grp
import hashlib
import json
import os
import pwd
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import venv
import zipfile
from dataclasses import dataclass
from email.parser import BytesParser
from pathlib import Path
from typing import Any

ACCOUNT = re.compile(r"[a-z_][a-z0-9_-]{0,30}\Z")
UNIT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.@-]{0,63}\Z")
VERSION = re.compile(r"[0-9][A-Za-z0-9_.+-]{0,63}\Z")
PATH = re.compile(r"/[A-Za-z0-9_./+ -]+\Z")
GOBGP_UNIT = "ninja-gobgpd"
ADMIN_UNIT = "ninja-admin"


def observation_configuration(config_dir: Path, data_dir: Path) -> dict[str, Any]:
    """An explicit observation configuration that cannot start a PCEP child."""
    return {
        "version": 1,
        "operating_mode": "observe",
        "files": {"bgpls_raw": str(data_dir / "live/bgpls-raw.json"),
                  "bgpls_status": str(data_dir / "live/bgpls-status.json"),
                  "topology": str(data_dir / "live/topology.json"),
                  "pcep_capture": str(data_dir / "live/pcep.json")},
        "state_dir": str(data_dir / "state"), "runtime_dir": str(data_dir / "run"),
        "bgpls": {"endpoint": "127.0.0.1:50051", "required_peers": ["192.0.2.20"]},
        "pcep": {"listen_host": "127.0.0.1", "domain_policy": str(config_dir / "domain-policy.json")},
        "dashboard": {"enabled": True, "listen_host": "127.0.0.1", "port": 8088},
    }


def observation_policy() -> dict[str, Any]:
    return {"version": 1, "domain": {"protocol_id": 3, "instance_id": 0,
                                    "autonomous_system": 65000, "bgpls_identifier": 0},
            "pcc_prefixes": ["192.0.2.0/24"], "destination_prefixes": ["192.0.2.0/24"],
            "max_bandwidth_bps": 100_000_000, "max_live_lsps": 32}


def default_gobgp_configuration() -> bytes:
    return ("# Bootstrap only: no peers and no BGP listener.\n"
            "[global.config]\n  as = 65000\n  router-id = \"127.0.0.1\"\n  port = -1\n"
            "  local-address-list = [\"127.0.0.1\"]\n"
            "[global.apply-policy.config]\n  default-import-policy = \"accept-route\"\n"
            "  default-export-policy = \"reject-route\"\n").encode()


def read_gobgp_configuration(path: Path) -> bytes:
    try:
        import tomllib
    except ModuleNotFoundError as error:
        raise ValueError("Rendering custom GoBGP TOML requires Python 3.11 or newer") from error

    without_symlinks(path.absolute())
    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError("GoBGP configuration must be a regular file")
        contents = stream.read(1024 * 1024 + 1)
    if len(contents) > 1024 * 1024:
        raise ValueError("GoBGP configuration exceeds 1 MiB")
    document = tomllib.loads(contents.decode())
    global_config = document.get("global")
    if not isinstance(global_config, dict) or not isinstance(global_config.get("config"), dict):
        raise ValueError("GoBGP configuration requires [global.config]")
    return contents


def encoded(document: Any) -> bytes:
    return (json.dumps(document, indent=2, sort_keys=True, allow_nan=False) + "\n").encode()


def checksum(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def absolute_path(value: str | Path) -> Path:
    text = str(value)
    path = Path(text)
    if (not PATH.fullmatch(text) or text != text.rstrip() or
            path != Path(os.path.normpath(text)) or ".." in path.parts):
        raise ValueError(f"Deployment path must be a canonical absolute path: {text!r}")
    if path == Path("/"):
        raise ValueError("Deployment path cannot be the filesystem root")
    return path


def without_symlinks(path: Path) -> None:
    for candidate in (path, *path.parents):
        if candidate.is_symlink():
            raise ValueError(f"Deployment target cannot contain a symlink: {candidate}")


def read_json(path: Path) -> dict[str, Any]:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise ValueError(f"Duplicate JSON field: {key}")
            result[key] = value
        return result

    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError(f"Deployment JSON input must be a regular file: {path}")
        contents = stream.read(1024 * 1024 + 1)
    if len(contents) > 1024 * 1024:
        raise ValueError("Deployment JSON input exceeds 1 MiB")

    def finite(value: str) -> Any:
        raise ValueError(f"Deployment JSON cannot contain {value}")

    document = json.loads(contents, object_pairs_hook=pairs, parse_constant=finite)
    if not isinstance(document, dict):
        raise ValueError(f"Expected a JSON object: {path}")
    return document


def wheel_version(wheel: Path) -> str:
    if wheel.suffix != ".whl" or not wheel.is_file():
        raise ValueError("--wheel must identify a local ninja-pce wheel")
    with zipfile.ZipFile(wheel) as archive:
        metadata = [name for name in archive.namelist() if name.endswith(".dist-info/METADATA")]
        if len(metadata) != 1:
            raise ValueError("Wheel must contain exactly one distribution metadata file")
        if archive.getinfo(metadata[0]).file_size > 1024 * 1024:
            raise ValueError("Wheel metadata exceeds its supported size")
        message = BytesParser().parsebytes(archive.read(metadata[0]))
    name = (message.get("Name") or "").lower().replace("_", "-")
    version = message.get("Version") or ""
    if name != "ninja-pce" or not VERSION.fullmatch(version):
        raise ValueError("Wheel must contain a safely named ninja-pce release")
    return version


@dataclass(frozen=True)
class Deployment:
    wheel: Path
    wheel_sha256: str
    version: str
    prefix: Path
    config_dir: Path
    data_dir: Path
    unit_dir: Path
    service_user: str
    service_group: str
    unit_name: str
    constraints: bytes
    controller: bytes
    domain_policy: bytes
    bundle: Any = None
    gobgp_config: bytes | None = None

    @property
    def release(self) -> Path:
        return self.prefix / "releases" / self.version

    @property
    def executable(self) -> Path:
        return self.release / "venv/bin/ninja-pce"

    @property
    def python(self) -> Path:
        return self.release / "venv/bin/python"

    @property
    def config_path(self) -> Path:
        return self.config_dir / "controller.json"

    @property
    def unit_path(self) -> Path:
        return self.unit_dir / f"{self.unit_name}.service"

    def unit(self) -> bytes:
        # ExecStart and ReadWritePaths accept quoted tokens. WorkingDirectory is
        # a scalar path, so quoting it would become part of the path. The path
        # allowlist excludes specifiers, substitutions, controls and trailing spaces.
        quote = json.dumps
        return ("[Unit]\nDescription=Ninja-PCE controller\n"
                "Wants=network-online.target\nAfter=network-online.target\n"
                "StartLimitIntervalSec=60\nStartLimitBurst=3\n\n"
                "[Service]\nType=simple\n"
                f"User={self.service_user}\nGroup={self.service_group}\n"
                f"WorkingDirectory={self.data_dir}\n"
                f"ExecStart={quote(str(self.python))} -I -m ninja_pce.cli controller run --config "
                f"{quote(str(self.config_path))}"
                + (" --component bgpls --component dashboard" if self.bundle is not None else "") + "\n"
                "UnsetEnvironment=PYTHONPATH PYTHONHOME\n"
                "Restart=on-failure\nRestartSec=5\nRestartPreventExitStatus=78\n"
                "TimeoutStopSec=30\nUMask=0077\n"
                "NoNewPrivileges=true\nPrivateTmp=true\nProtectSystem=strict\nProtectHome=true\n"
                f"ReadWritePaths={quote(str(self.data_dir))}\n"
                f"RuntimeDirectory={self.unit_name}\n"
                "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6\n"
                "[Install]\nWantedBy=multi-user.target\n").encode()

    def gobgp_unit(self) -> bytes:
        if self.bundle is None:
            raise ValueError("Managed GoBGP requires an authenticated release bundle")
        quote = json.dumps
        return ("[Unit]\nDescription=Ninja-PCE verified GoBGP topology source\n"
                "Wants=network-online.target\nAfter=network-online.target\n"
                "StartLimitIntervalSec=60\nStartLimitBurst=3\n\n"
                "[Service]\nType=simple\n"
                f"User={self.service_user}\nGroup={self.service_group}\n"
                f"ExecStart={quote(str(self.python))} -I -m ninja_pce.cli runtime gobgp --binary "
                f"{quote(str(self.release / 'bin/gobgpd'))} --config "
                f"{quote(str(self.config_dir / 'gobgpd.toml'))} --api-address 127.0.0.1 "
                f"--api-port 50051 --service {GOBGP_UNIT}.service\n"
                "UnsetEnvironment=PYTHONPATH PYTHONHOME\n"
                "Restart=on-failure\nRestartSec=5\nRestartPreventExitStatus=78\n"
                "TimeoutStopSec=30\nUMask=0077\n"
                "AmbientCapabilities=CAP_NET_BIND_SERVICE\nCapabilityBoundingSet=CAP_NET_BIND_SERVICE\n"
                "NoNewPrivileges=true\nPrivateTmp=true\nProtectSystem=strict\nProtectHome=true\n"
                "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6\n"
                "[Install]\nWantedBy=multi-user.target\n").encode()

    @property
    def admin_workspace(self) -> Path:
        return self.data_dir / "admin-workspace"

    def admin_unit(self) -> bytes:
        if self.bundle is None:
            raise ValueError("Managed administration requires an authenticated release bundle")
        quote = json.dumps
        return ("[Unit]\nDescription=Ninja-PCE observation administration\n"
                "Wants=network-online.target\nAfter=network-online.target\n"
                "StartLimitIntervalSec=60\nStartLimitBurst=3\n\n"
                "[Service]\nType=simple\n"
                f"User={self.service_user}\nGroup={self.service_group}\n"
                f"WorkingDirectory={self.data_dir}\n"
                f"ExecStart={quote(str(self.python))} -I -m ninja_pce.cli admin serve --directory "
                f"{quote(str(self.admin_workspace))} --grpcurl {quote(str(self.release / 'bin/grpcurl'))} "
                f"--gobgpd {quote(str(self.release / 'bin/gobgpd'))} --port 8080 --managed-startup\n"
                "UnsetEnvironment=PYTHONPATH PYTHONHOME\n"
                "Restart=on-failure\nRestartSec=5\nRestartPreventExitStatus=78\n"
                "TimeoutStopSec=90\nUMask=0077\n"
                "AmbientCapabilities=CAP_NET_BIND_SERVICE\nCapabilityBoundingSet=CAP_NET_BIND_SERVICE\n"
                "NoNewPrivileges=true\nPrivateTmp=true\nProtectSystem=strict\nProtectHome=true\n"
                f"ReadWritePaths={quote(str(self.data_dir))}\n"
                f"RuntimeDirectory={ADMIN_UNIT}\n"
                "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6\n"
                "[Install]\nWantedBy=multi-user.target\n").encode()

    def configuration_files(self) -> dict[str, bytes]:
        result = {"controller.json": self.controller, "domain-policy.json": self.domain_policy}
        if self.bundle is not None:
            assert self.gobgp_config is not None
            result["gobgpd.toml"] = self.gobgp_config
        return result

    def service_files(self) -> dict[str, bytes]:
        result = {f"{self.unit_name}.service": self.unit()}
        if self.bundle is not None:
            result[f"{GOBGP_UNIT}.service"] = self.gobgp_unit()
            result[f"{ADMIN_UNIT}.service"] = self.admin_unit()
        return result

    def manifest(self) -> dict[str, Any]:
        result = {
            "version": 1, "application_version": self.version,
            "wheel": self.wheel.name, "wheel_sha256": self.wheel_sha256,
            "constraints_sha256": hashlib.sha256(self.constraints).hexdigest(),
            "release": str(self.release), "config": str(self.config_path),
            "data_dir": str(self.data_dir), "unit": str(self.unit_path),
            "service_user": self.service_user, "service_group": self.service_group,
            "activation": "manual", "files": {
                name: hashlib.sha256(contents).hexdigest()
                for name, contents in {**self.configuration_files(), **self.service_files()}.items()},
        }
        if self.bundle is not None:
            result["bundle_manifest_sha256"] = self.bundle.manifest_sha256
            result["operating_intent"] = "observe"
            result["components"] = self.bundle.manifest["components"]
        return result

    def files(self) -> dict[str, bytes]:
        return {**self.configuration_files(), **self.service_files(),
                "deployment-manifest.json": encoded(self.manifest())}


def deployment(args: argparse.Namespace, bundle: Any = None) -> Deployment:
    for value in (args.service_user, args.service_group or args.service_user):
        if not ACCOUNT.fullmatch(value) or value == "root":
            raise ValueError("Service user/group must be a safe non-root system account name")
    if not UNIT.fullmatch(args.unit_name) or args.unit_name.endswith(".service"):
        raise ValueError("Unit name must be a safe systemd name without the .service suffix")
    if bundle is not None and args.unit_name in {GOBGP_UNIT, ADMIN_UNIT}:
        raise ValueError("Controller unit name must differ from the managed GoBGP and administration units")
    prefix, config_dir, data_dir, unit_dir = (
        absolute_path(value) for value in (args.prefix, args.config_dir, args.data_dir, args.unit_dir))
    # Persistent configuration, journals and source must be separate trees.
    roots = (prefix, config_dir, data_dir, unit_dir)
    if any(left == right or left in right.parents or right in left.parents
           for index, left in enumerate(roots) for right in roots[index + 1:]):
        raise ValueError("Release, config, data and systemd directories must be separate trees")
    if bundle is None and not all(getattr(args, field, None) for field in ("wheel", "config", "domain_policy")):
        raise ValueError("Legacy installation requires --wheel, --config and --domain-policy")
    if bundle is not None and (getattr(args, "wheel", None) or getattr(args, "wheelhouse", None)):
        raise ValueError("--bundle supplies its own verified wheel and wheelhouse")
    custom = getattr(args, "config", None)
    if bundle is not None and bool(custom) != bool(getattr(args, "domain_policy", None)):
        raise ValueError("Custom bundle configuration requires both --config and --domain-policy")
    if bundle is not None and custom and not getattr(args, "gobgp_config", None):
        raise ValueError("Custom bundle configuration also requires --gobgp-config")
    wheel = bundle.wheel if bundle is not None else Path(args.wheel).absolute()
    version = wheel_version(wheel)
    controller = read_json(Path(args.config)) if custom else observation_configuration(config_dir, data_dir)
    policy = read_json(Path(args.domain_policy)) if custom else observation_policy()
    gobgp_config = None
    if bundle is not None:
        if controller.get("operating_mode", "observe") != "observe":
            raise ValueError("Bundle observation deployment requires operating_mode observe")
        # The PR1 bundle path already selected only observation components.
        # Make that intent explicit when rendering an older custom document,
        # without changing the supplied file or existing installed files.
        controller["operating_mode"] = "observe"
        bgpls = controller.get("bgpls")
        if not isinstance(bgpls, dict) or bgpls.get("endpoint") != "127.0.0.1:50051":
            raise ValueError("Managed GoBGP requires bgpls.endpoint 127.0.0.1:50051")
        bgpls["grpcurl"] = str(prefix / "releases" / version / "bin/grpcurl")
        dashboard = controller.get("dashboard")
        if not isinstance(dashboard, dict) or dashboard.get("enabled", True) is not True:
            raise ValueError("Bundle observation deployment requires an enabled dashboard")
        gobgp_source = getattr(args, "gobgp_config", None)
        gobgp_config = read_gobgp_configuration(Path(gobgp_source)) if gobgp_source else default_gobgp_configuration()
    if (type(controller.get("version")) is not int or controller["version"] != 1 or
            type(policy.get("version")) is not int or policy["version"] != 1):
        raise ValueError("Deployment requires version 1 application and domain-policy documents")
    pcep = controller.get("pcep")
    if not isinstance(pcep, dict) or pcep.get("domain_policy") != str(config_dir / "domain-policy.json"):
        raise ValueError("Controller domain_policy must identify the installed domain-policy.json")
    paths = controller.get("files")
    if not isinstance(paths, dict) or not paths:
        raise ValueError("Controller files must identify its writable live data")
    writable = list(paths.values()) + [controller.get("state_dir"),
                                     controller.get("runtime_dir", str(data_dir / "live/.controller"))]
    failure = pcep.get("failover")
    recovery = pcep.get("managed_recovery")
    if isinstance(failure, dict):
        writable.append(failure.get("state_path"))
    if isinstance(recovery, dict):
        writable.append(recovery.get("journal_path"))
    for value in writable:
        if not isinstance(value, str) or data_dir not in absolute_path(value).parents:
            raise ValueError("All writable controller paths must be below the deployment data directory")
    # Relative static inputs change meaning after copying /etc configuration.
    # Home directories are inaccessible under this unit's ProtectHome policy.
    bgpls = controller.get("bgpls", {})
    if not isinstance(bgpls, dict):
        raise ValueError("Controller bgpls must be a JSON object")
    static_inputs = [bgpls[key] for key in ("proto_path", "node_map", "ospf_multi_area_policy") if key in bgpls]
    if isinstance(bgpls.get("grpcurl"), str) and "/" in bgpls["grpcurl"]:
        static_inputs.append(bgpls["grpcurl"])
    if "provisioning_state" in pcep:
        static_inputs.append(pcep["provisioning_state"])
    if isinstance(recovery, dict) and "reinstatement_path" in recovery:
        static_inputs.append(recovery["reinstatement_path"])
    for value in static_inputs:
        if not isinstance(value, str):
            raise ValueError("Static deployment input paths must be strings")
        path = absolute_path(value)
        if any(parent == Path("/home") or parent == Path("/root") for parent in (path, *path.parents)):
            raise ValueError("Static deployment inputs must be outside home directories protected by systemd")
    constraints_path = bundle.root / "deploy/constraints.txt" if bundle is not None else Path(__file__).with_name("constraints.txt")
    constraints = constraints_path.read_bytes()
    wheel_sha256 = bundle.manifest["artifacts"][wheel.name] if bundle is not None else checksum(wheel)
    return Deployment(wheel, wheel_sha256, version, prefix, config_dir, data_dir, unit_dir,
                      args.service_user, args.service_group or args.service_user,
                      args.unit_name, constraints, encoded(controller), encoded(policy), bundle, gobgp_config)


def ensure_compatible(path: Path, contents: bytes) -> None:
    without_symlinks(path)
    if path.exists() and (not path.is_file() or path.read_bytes() != contents):
        raise ValueError(f"Refusing to overwrite a different existing deployment file: {path}")


def create_file(path: Path, contents: bytes, mode: int = 0o640) -> bool:
    ensure_compatible(path, contents)
    if path.exists():
        return False
    # O_EXCL prevents a concurrent operator's file from being overwritten.
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    with os.fdopen(descriptor, "wb") as stream:
        os.fchmod(stream.fileno(), mode)
        stream.write(contents)
    return True


def render(plan: Deployment, output: Path) -> None:
    output = output.absolute()
    without_symlinks(output)
    files = plan.files()
    for name, contents in files.items():
        ensure_compatible(output / name, contents)
    output.mkdir(parents=True, exist_ok=True)
    for name, contents in files.items():
        create_file(output / name, contents)


def preflight(plan: Deployment) -> None:
    for root in (plan.prefix, plan.config_dir, plan.data_dir, plan.unit_dir, plan.release):
        without_symlinks(root)
        if root.exists() and not root.is_dir():
            raise ValueError(f"Deployment directory is not a directory: {root}")
    for name, contents in plan.configuration_files().items():
        ensure_compatible(plan.config_dir / name, contents)
    for name, contents in plan.service_files().items():
        ensure_compatible(plan.unit_dir / name, contents)
    if plan.release.exists():
        raise ValueError(f"Release already exists; it will not be overwritten: {plan.release}")


def systemd_access_preflight(plan: Deployment) -> None:
    """Reject runtime paths hidden by this unit, without restricting render previews."""
    document = json.loads(plan.controller)
    bgpls = document["bgpls"]
    pcep = document["pcep"]
    paths = [plan.prefix, plan.config_dir, plan.data_dir, plan.unit_dir]
    paths.extend(Path(bgpls[key]) for key in ("proto_path", "node_map", "ospf_multi_area_policy") if key in bgpls)
    if "/" in bgpls.get("grpcurl", ""):
        paths.append(Path(bgpls["grpcurl"]))
    if "provisioning_state" in pcep:
        paths.append(Path(pcep["provisioning_state"]))
    managed = pcep.get("managed_recovery", {})
    if "reinstatement_path" in managed:
        paths.append(Path(managed["reinstatement_path"]))
    hidden = (Path("/home"), Path("/root"), Path("/tmp"), Path("/var/tmp"))
    for path in paths:
        if any(path == root or root in path.parents for root in hidden):
            raise ValueError(f"Runtime deployment path is hidden by ProtectHome/PrivateTmp: {path}")


def account(plan: Deployment) -> tuple[int, int]:
    try:
        existing_user = pwd.getpwnam(plan.service_user)
    except KeyError:
        existing_user = None
    if existing_user is not None and existing_user.pw_uid == 0:
        raise ValueError("Existing service account must be non-root")
    try:
        group = grp.getgrnam(plan.service_group)
    except KeyError:
        subprocess.run(["groupadd", "--system", plan.service_group], check=True)
        group = grp.getgrnam(plan.service_group)
    if group.gr_gid == 0:
        raise ValueError("Existing service group must be non-root")
    try:
        user = pwd.getpwnam(plan.service_user)
    except KeyError:
        subprocess.run(["useradd", "--system", "--gid", plan.service_group,
                        "--home-dir", str(plan.data_dir), "--no-create-home",
                        "--shell", "/usr/sbin/nologin", plan.service_user], check=True)
        user = pwd.getpwnam(plan.service_user)
    if user.pw_uid == 0 or group.gr_gid == 0 or user.pw_gid != group.gr_gid:
        raise ValueError("Existing service account must be non-root with the selected primary group")
    return user.pw_uid, group.gr_gid


def state_directory(path: Path, uid: int, gid: int) -> None:
    without_symlinks(path)
    if path.exists():
        info = path.stat()
        if not path.is_dir() or info.st_uid != uid or info.st_gid != gid:
            raise ValueError(f"Existing state directory ownership needs operator review: {path}")
        if info.st_mode & 0o700 != 0o700:
            raise ValueError(f"Existing state directory requires service read/write/traverse permissions: {path}")
        return
    path.mkdir(mode=0o750, parents=False)
    path.chmod(0o750)
    os.chown(path, uid, gid)


def system_directory(path: Path) -> None:
    """Create only missing parent scaffolding; preserve existing permissions."""
    without_symlinks(path)
    missing = []
    candidate = path
    while not candidate.exists():
        missing.append(candidate)
        candidate = candidate.parent
    if not candidate.is_dir():
        raise ValueError(f"Deployment parent is not a directory: {candidate}")
    for directory in reversed(missing):
        directory.mkdir(mode=0o755)
        directory.chmod(0o755)


def copy_bundle_payload(plan: Deployment, destination: Path) -> None:
    """Copy only authenticated native artifacts, verifying each resulting byte."""
    assert plan.bundle is not None
    bundle = plan.bundle
    sources = {f"bin/{name}": component["path"] for name, component in bundle.manifest["components"].items()}
    sources.update({f"provenance/{name}.json": component["provenance"]
                    for name, component in bundle.manifest["components"].items()})
    sources.update({"provenance/distribution-manifest.json": "distribution-manifest.json",
                    "provenance/distribution-manifest.sig": "distribution-manifest.sig"})
    for relative, source in sources.items():
        path = bundle.root / source
        descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > 512 * 1024 * 1024:
                raise ValueError(f"Verified bundle artifact is not a bounded regular file: {source}")
            contents = stream.read(512 * 1024 * 1024 + 1)
        if len(contents) > 512 * 1024 * 1024:
            raise ValueError(f"Verified bundle artifact exceeds its size limit: {source}")
        expected = (bundle.manifest_sha256 if source == "distribution-manifest.json" else
                    bundle.manifest["artifacts"].get(source))
        if expected is not None and hashlib.sha256(contents).hexdigest() != expected:
            raise ValueError(f"Verified bundle artifact changed before installation: {source}")
        target = destination / relative
        system_directory(target.parent)
        create_file(target, contents, 0o755 if relative.startswith("bin/") else 0o644)


def install_wheel(environment: Path, wheelhouse: Path, constraints: Path, wheel: Path,
                  *, verified: bool) -> None:
    command = [str(environment / "bin/python"), "-I", "-m", "pip", "install", "--no-index",
               "--find-links", str(wheelhouse.absolute()), "--constraint", str(constraints)]
    options: dict[str, Any] = {"check": True}
    if verified:
        command.extend(("--only-binary=:all:", "--isolated", "--disable-pip-version-check"))
        options["env"] = {key: value for key, value in os.environ.items() if not key.upper().startswith("PIP_")}
        options["env"]["PIP_CONFIG_FILE"] = os.devnull
    subprocess.run([*command, str(wheel)], **options)


def install(plan: Deployment, wheelhouse: Path | None) -> None:
    if os.geteuid() != 0:
        raise ValueError("--install requires root; rendering requires no privileges")
    if plan.bundle is not None:
        plan.bundle.validate_host()
        wheelhouse = plan.bundle.wheelhouse
    if wheelhouse is None or not wheelhouse.is_dir():
        raise ValueError("--install requires a local --wheelhouse with all runtime dependencies")
    systemd_access_preflight(plan)
    preflight(plan)
    # Resolve and validate dependencies in a temporary isolated environment before
    # installing configuration, a service account or a service unit.
    with tempfile.TemporaryDirectory(prefix="ninja-pce-install-") as temporary:
        stage = Path(temporary)
        staged_wheel = stage / plan.wheel.name
        shutil.copyfile(plan.wheel, staged_wheel)
        if checksum(staged_wheel) != plan.wheel_sha256:
            raise ValueError("Wheel changed after deployment was rendered")
        constraints = stage / "constraints.txt"
        constraints.write_bytes(plan.constraints)
        environment = stage / "venv"
        venv.EnvBuilder(with_pip=True).create(environment)
        install_wheel(environment, wheelhouse, constraints, staged_wheel, verified=plan.bundle is not None)
        controller = json.loads(plan.controller)
        controller["pcep"]["domain_policy"] = str(stage / "domain-policy.json")
        if plan.bundle is not None:
            copy_bundle_payload(plan, stage)
            controller["bgpls"]["grpcurl"] = str(stage / "bin/grpcurl")
            assert plan.gobgp_config is not None
            (stage / "gobgpd.toml").write_bytes(plan.gobgp_config)
            try:
                subprocess.run([str(stage / "bin/gobgpd"), "--dry-run", "--pprof-disable", "--metrics-path=",
                                "--config-strict", "-f", str(stage / "gobgpd.toml")], check=True, timeout=30)
            except subprocess.TimeoutExpired as error:
                raise ValueError("GoBGP configuration validation exceeded 30 seconds") from error
        (stage / "domain-policy.json").write_bytes(plan.domain_policy)
        (stage / "controller.json").write_bytes(encoded(controller))
        subprocess.run([str(environment / "bin/python"), "-I", "-m", "ninja_pce.cli", "controller", "validate", "--config",
                        str(stage / "controller.json")], check=True)
        preflight(plan)
        uid, gid = account(plan)
        # Parent scaffolding is created without traversing or changing journals.
        system_directory(plan.data_dir.parent)
        state_directory(plan.data_dir, uid, gid)
        controller = json.loads(plan.controller)
        directories = {Path(value).parent for value in controller["files"].values()}
        directories.add(Path(controller["state_dir"]))
        directories.add(Path(controller.get("runtime_dir", str(plan.data_dir / "live/.controller"))))
        for directory in sorted(directories, key=lambda value: len(value.parts)):
            for candidate in reversed((directory, *directory.parents)):
                if plan.data_dir in candidate.parents:
                    state_directory(candidate, uid, gid)
        config_created = not plan.config_dir.exists()
        system_directory(plan.config_dir.parent)
        plan.config_dir.mkdir(exist_ok=True, mode=0o750)
        if config_created:
            plan.config_dir.chmod(0o750)
            os.chown(plan.config_dir, 0, gid)
        else:
            info = plan.config_dir.stat()
            if not (info.st_mode & 0o005 == 0o005 or
                    info.st_gid == gid and info.st_mode & 0o050 == 0o050):
                raise ValueError("Existing config directory must be readable by the service account")
        system_directory(plan.unit_dir)
        system_directory(plan.release.parent)
        # A virtualenv embeds absolute paths in entrypoints. Build directly at its
        # immutable final location after all offline preflight checks have passed.
        plan.release.mkdir(mode=0o755)
        plan.release.chmod(0o755)
        try:
            final_environment = plan.release / "venv"
            previous_umask = os.umask(0o022)
            try:
                venv.EnvBuilder(with_pip=True).create(final_environment)
                install_wheel(final_environment, wheelhouse, constraints, staged_wheel,
                              verified=plan.bundle is not None)
            finally:
                os.umask(previous_umask)
            if plan.bundle is not None:
                copy_bundle_payload(plan, plan.release)
            create_file(plan.release / "deployment-manifest.json", encoded(plan.manifest()), 0o644)
        except BaseException:
            shutil.rmtree(plan.release)
            raise
        for name, contents in plan.configuration_files().items():
            target = plan.config_dir / name
            if create_file(target, contents):
                os.chown(target, 0, gid)
            else:
                info = target.stat()
                if not (info.st_mode & 0o004 or info.st_uid == uid and info.st_mode & 0o400 or
                        info.st_gid == gid and info.st_mode & 0o040):
                    raise ValueError(f"Existing config file is not readable by the service account: {target}")
        for name, contents in plan.service_files().items():
            create_file(plan.unit_dir / name, contents, 0o644)
        # Verify the actual installed paths and reference permissions as the
        # service account. The earlier isolated validation ran as the installer.
        subprocess.run(["runuser", "--user", plan.service_user, "--", str(plan.python),
                        "-I", "-m", "ninja_pce.cli", "controller", "validate", "--config", str(plan.config_path)], check=True)
        subprocess.run(["systemctl", "daemon-reload"], check=True)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--bundle", type=Path, help="Extracted, signed offline release directory")
    result.add_argument("--trusted-key", type=Path, help="Independently distributed release verification key")
    result.add_argument("--wheel")
    result.add_argument("--config")
    result.add_argument("--domain-policy")
    result.add_argument("--gobgp-config", help="Reviewed GoBGP TOML for a custom bundle deployment")
    result.add_argument("--prefix", default="/opt/ninja-pce")
    result.add_argument("--config-dir", default="/etc/ninja-pce")
    result.add_argument("--data-dir", default="/var/lib/ninja-pce")
    result.add_argument("--unit-dir", default="/etc/systemd/system")
    result.add_argument("--service-user", default="ninja-pce")
    result.add_argument("--service-group")
    result.add_argument("--unit-name", default="ninja-controller")
    result.add_argument("--output-dir", default="build/deployment")
    result.add_argument("--wheelhouse", type=Path)
    result.add_argument("--install", action="store_true", help="Install reviewed files; never start the service")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.bundle is not None:
            if args.trusted_key is None:
                raise ValueError("--bundle requires an independently supplied --trusted-key")
            # A copied helper is part of the signed inventory. Importing it must
            # not create unsigned __pycache__ files inside the release first.
            previous_bytecode = sys.dont_write_bytecode
            sys.dont_write_bytecode = True
            try:
                # Support `python -m deploy.install` and a copied deploy/install.py.
                try:
                    from deploy.bundle import verified_bundle
                except ModuleNotFoundError as error:
                    if error.name != "deploy":
                        raise
                    from bundle import verified_bundle

                with verified_bundle(args.bundle, args.trusted_key) as bundle:
                    execute(deployment(args, bundle), args)
            finally:
                sys.dont_write_bytecode = previous_bytecode
        else:
            if args.trusted_key is not None or args.gobgp_config is not None:
                raise ValueError("--trusted-key and --gobgp-config require --bundle")
            execute(deployment(args), args)
        return 0
    except (OSError, ValueError, RecursionError, zipfile.BadZipFile, subprocess.CalledProcessError) as error:
        print(f"Deployment failed: {error}", file=sys.stderr)
        return 1


def execute(plan: Deployment, args: argparse.Namespace) -> None:
    if plan.bundle is not None and Path(args.output_dir).resolve().is_relative_to(args.bundle.resolve()):
        raise ValueError("Render output must be outside the authenticated bundle directory")
    if args.install and plan.bundle is not None:
        plan.bundle.validate_host()
    render(plan, Path(args.output_dir))
    if args.install:
        install(plan, args.wheelhouse)
        units = ", ".join(plan.service_files())
        print(f"Installed {units}; activation remains manual.")
    else:
        print(f"Rendered deployment in {Path(args.output_dir).absolute()}; no system changes made.")


if __name__ == "__main__":
    raise SystemExit(main())
