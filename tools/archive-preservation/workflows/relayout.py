"""Non-destructively move active catalog links to flat role directories."""

from __future__ import annotations

import argparse
import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

import catalog_core
import tiff_accession
from archive_core.catalog import write_unit_catalog
from archive_core.layout import (
    OBJECT_LAYOUT_VERSION,
    matches_object_path,
    object_relative_path,
)


class RelayoutError(RuntimeError):
    """Raised when a representation cannot be copied and relinked safely."""


def _ensure_verified_copy(source: Path, destination: Path, digest: str) -> None:
    if destination.exists():
        if not destination.is_file() or f"sha256:{catalog_core.sha256_file(destination)}" != digest:
            raise RelayoutError(f"existing flat-layout destination differs: {destination}")
        tiff_accession._mark_read_only(destination)
        return
    tiff_accession._atomic_copy_verified(source, destination, digest)
    tiff_accession._mark_read_only(destination)


def _catalog_backup(root: Path, unit_id: str) -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    destination = root / "work" / "backups" / f"{stamp}-PRE-RELAYOUT-{unit_id}.sqlite"
    destination.parent.mkdir(parents=True, exist_ok=True)
    source = catalog_core.connect_catalog(root / "work" / "catalog.sqlite")
    backup = sqlite3.connect(destination)
    try:
        source.backup(backup)
        backup.commit()
    finally:
        backup.close()
        source.close()
    check = sqlite3.connect(f"file:{destination}?mode=ro&immutable=1", uri=True)
    try:
        if check.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RelayoutError("pre-relayout catalog backup failed integrity")
    finally:
        check.close()
    return destination


def relayout_unit(root: Path, unit_id: str, actor: str) -> dict[str, Any]:
    """Copy active representations, then atomically supersede old links."""
    root = catalog_core.assert_safe_work_root(root)
    config = catalog_core.require_core_mode(root)
    independent_text = str(config.get("independent_generation_root") or "")
    if not independent_text:
        raise RelayoutError("flat relayout requires a configured independent root")
    independent = catalog_core.assert_safe_work_root(Path(independent_text))
    catalog_core.assert_independent_device(root, independent)
    with closing(
        catalog_core.connect_catalog(root / "work" / "catalog.sqlite", read_only=True)
    ) as catalog:
        rows = catalog.execute(
            """SELECT ef.entity_id, ao.archival_label, ef.role,
                      fo.occurrence_id, fo.relative_path, fo.original_filename,
                      fo.content_digest, fo.observed_mtime_utc
               FROM entity_files ef
               JOIN file_occurrences fo ON fo.occurrence_id=ef.occurrence_id
               JOIN archive_objects ao ON ao.entity_id=ef.entity_id
               WHERE fo.unit_id=? AND ef.status='active'
                 AND fo.status='verified' AND fo.occurrence_kind='archive'
               ORDER BY ao.archival_label, ef.role, fo.occurrence_id""",
            (unit_id,),
        ).fetchall()
    changes: list[dict[str, str]] = []
    for row in rows:
        old_relative = Path(str(row["relative_path"]))
        role = str(row["role"])
        if matches_object_path(
            PurePosixPath(old_relative.as_posix()),
            unit_id=unit_id,
            entity_id=str(row["entity_id"]),
            role=role,
        ):
            continue
        suffix = old_relative.suffix
        new_relative_posix = object_relative_path(
            unit_id=unit_id,
            inventory=str(row["archival_label"]),
            entity_id=str(row["entity_id"]),
            role=role,
            suffix=suffix,
        )
        new_relative = Path(*new_relative_posix.parts)
        digest = str(row["content_digest"])
        for base in (root, independent):
            old_path = base / old_relative
            if not old_path.is_file() or f"sha256:{catalog_core.sha256_file(old_path)}" != digest:
                raise RelayoutError(f"old representation failed fixity: {old_path}")
            _ensure_verified_copy(old_path, base / new_relative, digest)
        changes.append(
            {
                "entity_id": str(row["entity_id"]),
                "role": role,
                "old_occurrence_id": str(row["occurrence_id"]),
                "new_relative_path": new_relative_posix.as_posix(),
                "new_original_filename": new_relative_posix.name,
                "content_digest": digest,
                "observed_mtime_utc": str(row["observed_mtime_utc"]),
            }
        )
    backup = _catalog_backup(root, unit_id) if changes else None
    with closing(catalog_core.connect_catalog(root / "work" / "catalog.sqlite")) as catalog:
        result = catalog_core.relayout_active_representations(
            catalog,
            unit_id=unit_id,
            changes=changes,
            actor=actor,
        )
    with closing(
        catalog_core.connect_catalog(root / "work" / "catalog.sqlite", read_only=True)
    ) as catalog:
        state = catalog_core.catalog_state(catalog)
    publication_needed = (
        int(state["catalog_revision"]) != int(state["published_revision"])
    )
    if publication_needed:
        publication = catalog_core.publish_generation(
            root,
            independent,
            tool_version=catalog_core.TOOL_VERSION,
            actor=actor,
        )
        with closing(
            catalog_core.connect_catalog(root / "work" / "catalog.sqlite", read_only=True)
        ) as catalog:
            primary_catalog = write_unit_catalog(catalog, unit_id, root / "units" / unit_id)
            independent_catalog = write_unit_catalog(
                catalog, unit_id, independent / "units" / unit_id
            )
        if primary_catalog["csv_sha256"] != independent_catalog["csv_sha256"] or primary_catalog["xlsx_sha256"] != independent_catalog["xlsx_sha256"]:
            raise RelayoutError("independent unit catalog differs after relayout")
        result.update(
            {
                "backup": str(backup) if backup else "",
                "publication": publication,
                "unit_catalog": primary_catalog,
            }
        )
    config["object_layout"] = OBJECT_LAYOUT_VERSION
    config["config_version"] = max(int(config.get("config_version") or 0), 3)
    catalog_core.save_config(root, config)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True)
    parser.add_argument("--unit", required=True)
    parser.add_argument("--actor", required=True)
    parser.add_argument("--confirmation", required=True)
    args = parser.parse_args()
    if args.confirmation != "RELAYOUT-NO-DELETE":
        raise SystemExit("confirmation must be RELAYOUT-NO-DELETE")
    print(json.dumps(relayout_unit(Path(args.root), args.unit, args.actor), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
