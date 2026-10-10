"""Checksummed, durable local controller state (single filesystem only)."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 2


def digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def load_state(path: Path, *, legacy: bool = False) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        raise ValueError(f"Unreadable controller state: {path}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"Controller state must contain an object: {path}")
    version = data.get("version")
    if isinstance(version, bool) or not isinstance(version, int):
        raise ValueError(f"Controller state version must be an integer: {path}")
    if version == 1 and legacy:
        # Old state contained no reservation ownership or operational proof.
        return {**data, "version": SCHEMA_VERSION, "migrated_from": 1}
    if version != SCHEMA_VERSION:
        raise ValueError(f"Unsupported state schema {version!r}: {path}")
    checksum = data.pop("checksum", None)
    if checksum != digest(data):
        raise ValueError(f"Controller state checksum mismatch: {path}")
    return data


def write_state(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {key: value for key, value in data.items() if key != "checksum"}
    payload["version"] = SCHEMA_VERSION
    payload["checksum"] = digest(payload)
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
        ) as handle:
            temporary = handle.name
            json.dump(payload, handle, indent=2, sort_keys=True, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
        descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)
