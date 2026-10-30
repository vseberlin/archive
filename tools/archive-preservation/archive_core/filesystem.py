"""Filesystem safety, layout, and copy primitives for archive workflows."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import uuid
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

import catalog_core

from archive_core import ArchiveError


LEGACY_FIXTURE_MODE = "legacy-fixture"
PROTECTED_SOURCE_ROOTS = (
    Path("/mnt/volume/archive"),
    Path("/mnt/volume/archive-intake"),
)


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")

def json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

def sha256_file(path: Path, chunk_size: int = 8 * 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()

def safe_relative_path(value: str) -> PurePosixPath:
    if not value or any(char in value for char in ("\x00", "\r", "\n", "%")):
        raise ArchiveError(f"unsafe relative path: {value!r}")
    path = PurePosixPath(value.replace("\\", "/"))
    if path.is_absolute() or ".." in path.parts or "." in path.parts:
        raise ArchiveError(f"unsafe relative path: {value!r}")
    return path

def require_under(path: Path, root: Path) -> Path:
    resolved = path.resolve()
    root_resolved = root.resolve()
    if resolved != root_resolved and root_resolved not in resolved.parents:
        raise ArchiveError(f"path escapes controlled root: {path}")
    return resolved

def require_safe_work_root(root: Path) -> Path:
    resolved = root.resolve()
    for protected in PROTECTED_SOURCE_ROOTS:
        protected_resolved = protected.resolve()
        if resolved == protected_resolved or protected_resolved in resolved.parents:
            raise ArchiveError(
                f"archive tools may only read or copy from protected source tree: {protected}"
            )
    return resolved

def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ArchiveError(f"cannot read catalog mode config: {path}") from error
    if not isinstance(payload, dict):
        raise ArchiveError(f"catalog mode config must contain a JSON object: {path}")
    return payload

def root_paths(
    root: Path,
    *,
    allow_uninitialized: bool = False,
) -> dict[str, Any]:
    root = require_safe_work_root(root)
    work = root / "work"
    catalog_db = work / "catalog.sqlite"
    legacy_db = work / "index.sqlite"
    dedicated_registry_db = work / "source-registry" / "index.sqlite"
    core_config = work / "catalog-config.json"
    legacy_config = work / "catalog-mode.json"

    if core_config.exists():
        configured_mode = _read_json_object(core_config).get("catalog_mode")
        if configured_mode != catalog_core.CATALOG_MODE:
            raise ArchiveError(
                "catalog-config.json does not declare an accepted catalog mode; "
                "run the explicit core migration instead of selecting a database "
                "by file existence"
            )
        mode = catalog_core.CATALOG_MODE
        if not catalog_db.is_file():
            raise ArchiveError(
                f"configured core catalog is missing; refusing legacy fallback: {catalog_db}"
            )
        db = catalog_db
        legacy_writable = False
    elif legacy_config.exists():
        configured_mode = _read_json_object(legacy_config).get("catalog_mode")
        if configured_mode != LEGACY_FIXTURE_MODE:
            raise ArchiveError(f"unsupported legacy catalog mode: {configured_mode!r}")
        mode = LEGACY_FIXTURE_MODE
        db = catalog_db if catalog_db.is_file() else legacy_db
        if not db.is_file() and not allow_uninitialized:
            raise ArchiveError(f"configured fixture catalog is missing: {catalog_db}")
        legacy_writable = True
    elif catalog_db.is_file():
        mode = "legacy-read-only"
        db = catalog_db
        legacy_writable = False
    elif legacy_db.is_file():
        raise ArchiveError(
            "catalog.sqlite is missing and only legacy index.sqlite remains; "
            "refusing existence-based fallback"
        )
    elif allow_uninitialized:
        mode = "uninitialized"
        db = catalog_db
        legacy_writable = False
    else:
        raise ArchiveError(f"archive root has no explicitly usable catalog: {root}")

    if mode == catalog_core.CATALOG_MODE:
        registry_db = dedicated_registry_db
    else:
        registry_db = (
            dedicated_registry_db
            if dedicated_registry_db.exists() or not legacy_db.exists()
            else legacy_db
        )
    return {
        "root": root,
        "mode": mode,
        "legacy_writable": legacy_writable,
        "registry_writable": mode in (
            LEGACY_FIXTURE_MODE,
            catalog_core.CATALOG_MODE,
        ),
        "db": db,
        "catalog_db": catalog_db,
        "legacy_db": legacy_db,
        "registry_db": registry_db,
        "dedicated_registry_db": dedicated_registry_db,
        "backups": work / "backups",
        "intake": work / "intake",
        "seal": work / "seal",
        "units": root / "units",
        "manifests": root / "00_MANIFESTS",
        "derived": root / "derived",
        "gaps": root / "90_GAPS",
        "transient": root / "99_TRANSIENT_REFERENCE",
    }

def create_root_readme(root: Path) -> None:
    text = """# ISS Preservation Archive

BagIt units under `units/` preserve accepted masters and canonical unit
catalogs. `work/catalog.sqlite` is the rebuildable working catalog;
`work/source-registry/index.sqlite` is rebuildable search state. Nothing is
preserved until a bag and its independent preservation copy both verify.
`derived/`, registry search state, and `99_TRANSIENT_REFERENCE/` may be rebuilt
without losing accepted state.

`/mnt/volume/archive` and `/mnt/volume/archive-intake` are read/copy-only
source trees. Never move, rename, delete, rewrite, or change NTFS permissions
there. Recovery USB media must remain mounted read-only.

For ordinary work on the archive workstation, start the local ISS Archive
Workbench supplied with the archive tooling. It provides browse, intake,
reconciliation, source search, and health pages. It is a convenience interface,
not a second archive and not a network service.
"""
    (root / "00_README.md").write_text(text, encoding="utf-8", newline="\n")
    config = """# ISS Archive Health Thresholds

fixity_months: 3
intake_age_days: 30
minimum_free_bytes: 1073741824
"""
    (root / "00_CONFIG.md").write_text(config, encoding="utf-8", newline="\n")

def copy_and_verify(source: Path, destination: Path) -> tuple[int, str]:
    source_digest = sha256_file(source)
    source_size = source.stat().st_size
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".partial-" + uuid.uuid4().hex)
    try:
        with source.open("rb") as source_handle, temporary.open("xb") as target_handle:
            shutil.copyfileobj(source_handle, target_handle, length=8 * 1024 * 1024)
            target_handle.flush()
            os.fsync(target_handle.fileno())
        if temporary.stat().st_size != source_size:
            raise ArchiveError(f"intake copy size mismatch: {source}")
        destination_digest = sha256_file(temporary)
        if destination_digest != source_digest:
            raise ArchiveError(f"intake copy digest mismatch: {source}")
        os.replace(temporary, destination)
        destination.chmod(0o444)
        return source_size, source_digest
    finally:
        if temporary.exists():
            temporary.unlink()
