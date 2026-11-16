#!/usr/bin/env python3
"""Create a read-only plan for one-master/one-derivative recovery cleanup."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from archive_core.layout import object_relative_path


class CleanupPlanError(RuntimeError):
    """Raised when live state cannot support an exact cleanup plan."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def sha256_file(path: Path, expected_size: int) -> str:
    before = path.stat()
    if not path.is_file() or before.st_size != expected_size:
        raise CleanupPlanError(f"missing or changed-size file: {path}")
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(8 * 1024 * 1024):
            digest.update(chunk)
    after = path.stat()
    if (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
    ) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ):
        raise CleanupPlanError(f"file changed while hashing: {path}")
    return f"sha256:{digest.hexdigest()}"


def _open_read_only(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def load_state(catalog_path: Path, unit_id: str) -> tuple[dict, list[dict]]:
    connection = _open_read_only(catalog_path)
    try:
        state = dict(connection.execute("SELECT * FROM catalog_state").fetchone())
        generation = connection.execute(
            """
            SELECT generation_id, catalog_revision, manifest_digest
            FROM filesystem_generations
            WHERE status='current'
            """
        ).fetchone()
        if not generation:
            raise CleanupPlanError("catalog has no current filesystem generation")
        if (
            state["dirty_since"] is not None
            or state["catalog_revision"] != state["published_revision"]
            or state["catalog_revision"] != generation["catalog_revision"]
        ):
            raise CleanupPlanError("catalog is not clean and fully published")
        rows = [
            dict(row)
            for row in connection.execute(
                """
                SELECT
                    ao.archival_label AS inventory,
                    ef.entity_id,
                    ef.role,
                    ef.status AS link_status,
                    fo.occurrence_id,
                    fo.relative_path,
                    fo.original_filename,
                    fo.observed_mtime_utc,
                    fo.status AS occurrence_status,
                    fo.content_digest,
                    cb.size_bytes
                FROM entity_files ef
                JOIN file_occurrences fo
                  ON fo.occurrence_id=ef.occurrence_id
                JOIN content_blobs cb
                  ON cb.content_digest=fo.content_digest
                JOIN archive_objects ao
                  ON ao.entity_id=ef.entity_id
                WHERE fo.unit_id=?
                  AND ef.status IN ('active', 'superseded')
                ORDER BY ao.archival_label, ef.entity_id, ef.role,
                         ef.status, fo.relative_path
                """,
                (unit_id,),
            )
        ]
        return {
            "catalog_revision": int(state["catalog_revision"]),
            "published_revision": int(state["published_revision"]),
            "generation_id": generation["generation_id"],
            "generation_manifest_digest": generation["manifest_digest"],
        }, rows
    finally:
        connection.close()


def build_plan(rows: list[dict], unit_id: str, expected_objects: int) -> dict:
    active_by_entity: dict[str, list[dict]] = defaultdict(list)
    superseded: list[dict] = []
    for row in rows:
        if row["occurrence_status"] != "verified":
            raise CleanupPlanError(
                f"catalog occurrence is not verified: {row['occurrence_id']}"
            )
        if row["link_status"] == "active":
            active_by_entity[row["entity_id"]].append(row)
        else:
            superseded.append(row)
    if len(active_by_entity) != expected_objects:
        raise CleanupPlanError(
            f"expected {expected_objects} active represented objects, "
            f"found {len(active_by_entity)}"
        )

    migrations: list[dict] = []
    unchanged: list[dict] = []
    active_role_counts: dict[str, int] = defaultdict(int)
    for entity_id, entity_rows in active_by_entity.items():
        by_role = {row["role"]: row for row in entity_rows}
        if len(by_role) != len(entity_rows):
            raise CleanupPlanError(f"duplicate active role for {entity_id}")
        for role in by_role:
            active_role_counts[role] += 1
        representations = [
            row
            for role, row in by_role.items()
            if role in {"preservation_master", "preservation_surrogate"}
        ]
        if len(representations) != 1:
            raise CleanupPlanError(
                f"{entity_id} does not have exactly one preservation representation"
            )
        source = by_role.get("preservation_surrogate_source")
        access = by_role.get("access_derivative")
        representation = representations[0]
        if representation["role"] == "preservation_master":
            if source or not access:
                raise CleanupPlanError(
                    f"TIFF master object has invalid source/access roles: {entity_id}"
                )
            unchanged.append(
                {
                    "inventory": representation["inventory"],
                    "entity_id": entity_id,
                    "master": representation,
                    "derivative": access,
                }
            )
            continue
        if not source:
            raise CleanupPlanError(
                f"surrogate object lacks exact selected source: {entity_id}"
            )
        suffix = Path(source["original_filename"]).suffix.casefold()
        destination = str(
            object_relative_path(
                unit_id=unit_id,
                inventory=source["inventory"],
                entity_id=entity_id,
                role="access_derivative",
                suffix=suffix,
            )
        )
        migrations.append(
            {
                "inventory": source["inventory"],
                "entity_id": entity_id,
                "master": representation,
                "source": source,
                "existing_access": access,
                "new_access_relative_path": destination,
                "new_access_filename": Path(destination).name,
            }
        )

    expected_roles = {
        "preservation_master": 19,
        "preservation_surrogate": 194,
        "preservation_surrogate_source": 194,
        "access_derivative": 20,
    }
    if dict(active_role_counts) != expected_roles:
        raise CleanupPlanError(
            f"active role baseline changed: {dict(active_role_counts)}"
        )
    if len(migrations) != 194 or len(unchanged) != 19:
        raise CleanupPlanError("unexpected master/surrogate split")
    collisions = [item for item in migrations if item["existing_access"]]
    if len(collisions) != 1 or collisions[0]["inventory"] != "VLV-1004-2":
        raise CleanupPlanError("unexpected access-derivative collision set")
    if len(superseded) != 22:
        raise CleanupPlanError(
            f"expected 22 historical superseded paths, found {len(superseded)}"
        )
    return {
        "migrations": sorted(migrations, key=lambda item: item["inventory"]),
        "unchanged": sorted(unchanged, key=lambda item: item["inventory"]),
        "historical_superseded": superseded,
        "active_role_counts": dict(active_role_counts),
    }


def verify_and_expand(plan: dict, roots: list[Path]) -> dict:
    verified: set[tuple[str, str]] = set()
    copy_rows: list[dict] = []
    retirement_rows: list[dict] = []

    def verify(row: dict, root: Path) -> None:
        key = (str(root), row["relative_path"])
        if key in verified:
            return
        actual = sha256_file(root / row["relative_path"], int(row["size_bytes"]))
        if actual != row["content_digest"]:
            raise CleanupPlanError(
                f"digest mismatch: {root / row['relative_path']}"
            )
        verified.add(key)

    for item in plan["migrations"]:
        source = item["source"]
        existing = item["existing_access"]
        for root in roots:
            verify(source, root)
            destination = root / item["new_access_relative_path"]
            collision = destination.is_file()
            if existing:
                verify(existing, root)
                if destination != root / existing["relative_path"]:
                    raise CleanupPlanError(
                        f"catalog/path collision mismatch for {item['entity_id']}"
                    )
                if not collision:
                    raise CleanupPlanError(
                        f"expected derivative collision is absent: {destination}"
                    )
                collision_digest = existing["content_digest"]
            else:
                if destination.exists():
                    raise CleanupPlanError(
                        f"uncataloged destination collision: {destination}"
                    )
                collision_digest = ""
            copy_rows.append(
                {
                    "storage_root": str(root),
                    "inventory": item["inventory"],
                    "entity_id": item["entity_id"],
                    "source_path": str(root / source["relative_path"]),
                    "destination_path": str(destination),
                    "sha256": source["content_digest"],
                    "size_bytes": source["size_bytes"],
                    "destination_collision": str(collision).lower(),
                    "collision_sha256": collision_digest,
                    "operation": (
                        "stage_then_replace_verified_access"
                        if collision
                        else "copy_and_verify"
                    ),
                    "writes_authorized": "false",
                }
            )
            retirement_rows.append(
                {
                    "storage_root": str(root),
                    "inventory": item["inventory"],
                    "entity_id": item["entity_id"],
                    "occurrence_id": source["occurrence_id"],
                    "role": source["role"],
                    "path": str(root / source["relative_path"]),
                    "sha256": source["content_digest"],
                    "size_bytes": source["size_bytes"],
                    "reason": "old active surrogate-source path after relink",
                    "deletion_authorized": "false",
                }
            )
    for row in plan["historical_superseded"]:
        for root in roots:
            verify(row, root)
            retirement_rows.append(
                {
                    "storage_root": str(root),
                    "inventory": row["inventory"],
                    "entity_id": row["entity_id"],
                    "occurrence_id": row["occurrence_id"],
                    "role": row["role"],
                    "path": str(root / row["relative_path"]),
                    "sha256": row["content_digest"],
                    "size_bytes": row["size_bytes"],
                    "reason": "historical superseded layout path",
                    "deletion_authorized": "false",
                }
            )
    return {
        "copy_rows": copy_rows,
        "retirement_rows": retirement_rows,
        "verified_physical_files": len(verified),
    }


def catalog_rows(plan: dict) -> list[dict]:
    rows: list[dict] = []
    for item in plan["migrations"]:
        source = item["source"]
        existing = item["existing_access"]
        if existing:
            rows.append(
                {
                    "phase": "migration",
                    "sequence": 1,
                    "inventory": item["inventory"],
                    "entity_id": item["entity_id"],
                    "operation": "supersede_active_link",
                    "old_occurrence_id": existing["occurrence_id"],
                    "old_role": "access_derivative",
                    "new_role": "",
                    "new_relative_path": "",
                    "sha256": existing["content_digest"],
                    "catalog_write_authorized": "false",
                }
            )
        rows.extend(
            [
                {
                    "phase": "migration",
                    "sequence": 2,
                    "inventory": item["inventory"],
                    "entity_id": item["entity_id"],
                    "operation": "supersede_active_link",
                    "old_occurrence_id": source["occurrence_id"],
                    "old_role": "preservation_surrogate_source",
                    "new_role": "",
                    "new_relative_path": "",
                    "sha256": source["content_digest"],
                    "catalog_write_authorized": "false",
                },
                {
                    "phase": "migration",
                    "sequence": 3,
                    "inventory": item["inventory"],
                    "entity_id": item["entity_id"],
                    "operation": "register_verified_access_derivative",
                    "old_occurrence_id": source["occurrence_id"],
                    "old_role": "preservation_surrogate_source",
                    "new_role": "access_derivative",
                    "new_relative_path": item["new_access_relative_path"],
                    "sha256": source["content_digest"],
                    "catalog_write_authorized": "false",
                },
            ]
        )
    retired_occurrences: dict[str, dict] = {}
    for item in plan["migrations"]:
        for old in (item["source"], item["existing_access"]):
            if old:
                retired_occurrences[old["occurrence_id"]] = old
    for old in plan["historical_superseded"]:
        retired_occurrences[old["occurrence_id"]] = old
    for old in retired_occurrences.values():
        rows.append(
            {
                "phase": "post_retirement",
                "sequence": 4,
                "inventory": old["inventory"],
                "entity_id": old["entity_id"],
                "operation": "mark_file_occurrence_superseded",
                "old_occurrence_id": old["occurrence_id"],
                "old_role": old["role"],
                "new_role": "",
                "new_relative_path": "",
                "sha256": old["content_digest"],
                "catalog_write_authorized": "false",
            }
        )
    return sorted(
        rows,
        key=lambda row: (
            row["phase"],
            row["inventory"],
            row["entity_id"],
            row["sequence"],
        ),
    )


def write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        raise CleanupPlanError(f"refusing empty report: {path.name}")
    with path.open("x", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def write_report(
    output_dir: Path,
    *,
    catalog_path: Path,
    state: dict,
    plan: dict,
    expanded: dict,
    roots: list[Path],
    unit_id: str,
) -> dict:
    output_dir.mkdir(parents=True, exist_ok=False)
    catalog_actions = catalog_rows(plan)
    write_csv(output_dir / "copy-plan.csv", expanded["copy_rows"])
    write_csv(output_dir / "catalog-plan.csv", catalog_actions)
    write_csv(output_dir / "retirement-plan.csv", expanded["retirement_rows"])
    semantic = {
        "schema": "iss-recovery-layout-cleanup-plan-v1",
        "unit_id": unit_id,
        "catalog": str(catalog_path.resolve()),
        "catalog_state": state,
        "storage_roots": [str(root) for root in roots],
        "target_contract": {
            "masters": 213,
            "derivatives": 213,
            "documents": 0,
            "objects": 0,
            "surrogates": 0,
        },
        "migration_records": len(plan["migrations"]),
        "unchanged_tiff_master_records": len(plan["unchanged"]),
        "historical_superseded_paths_per_root": len(
            plan["historical_superseded"]
        ),
        "copy_operations": len(expanded["copy_rows"]),
        "catalog_operations": len(catalog_actions),
        "retirement_candidates": len(expanded["retirement_rows"]),
        "verified_physical_files": expanded["verified_physical_files"],
        "writes_authorized": False,
        "deletion_authorized": False,
    }
    operation_payload = {
        "copy_operations": expanded["copy_rows"],
        "catalog_operations": catalog_actions,
        "retirement_candidates": expanded["retirement_rows"],
    }
    operation_canonical = json.dumps(
        operation_payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("ascii")
    semantic["operation_set_digest"] = (
        f"sha256:{hashlib.sha256(operation_canonical).hexdigest()}"
    )
    canonical = json.dumps(
        semantic, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")
    semantic["plan_digest"] = f"sha256:{hashlib.sha256(canonical).hexdigest()}"
    semantic["created_at_utc"] = utc_now()
    with (output_dir / "summary.json").open("x", encoding="utf-8") as handle:
        json.dump(semantic, handle, indent=2, sort_keys=True)
        handle.write("\n")
    return semantic


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", required=True, type=Path)
    parser.add_argument("--unit-id", required=True)
    parser.add_argument("--primary-root", required=True, type=Path)
    parser.add_argument("--independent-root", required=True, type=Path)
    parser.add_argument("--expect-objects", required=True, type=int)
    parser.add_argument("--output-dir", required=True, type=Path)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    roots = [args.primary_root.resolve(), args.independent_root.resolve()]
    for root in roots:
        if not root.is_dir():
            raise CleanupPlanError(f"storage root is absent: {root}")
    output = args.output_dir.resolve()
    if any(output == root or root in output.parents for root in roots):
        # Reports belong under primary work/, so allow only that explicit case.
        allowed = roots[0] / "work" / "layout-cleanup"
        if allowed not in output.parents:
            raise CleanupPlanError("output is outside the bounded cleanup work area")
    state, rows = load_state(args.catalog, args.unit_id)
    plan = build_plan(rows, args.unit_id, args.expect_objects)
    expanded = verify_and_expand(plan, roots)
    summary = write_report(
        output,
        catalog_path=args.catalog,
        state=state,
        plan=plan,
        expanded=expanded,
        roots=roots,
        unit_id=args.unit_id,
    )
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except CleanupPlanError as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(2)
