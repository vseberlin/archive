#!/usr/bin/env python3
"""Accession the current decided Kurt Schwarz local objects, and nothing else."""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


UNIT = Path("/mnt/volume/iss-archive/units/kurt-schwarz")
WORK = Path("/mnt/volume/iss-archive-work/kurt-schwarz")
SOURCE = Path("/media/vladimir/Elements")
RECONCILIATION = WORK / "work/reconciliation/unit-reconciliation-v1/reconciliation.sqlite"
SCRIPT_DIR = WORK / "work/md-finalization"
MANIFEST = SCRIPT_DIR / "local-object-accession.csv"
COMPLETION = SCRIPT_DIR / "local-object-accession-completion.json"
BEFORE_DB = SCRIPT_DIR / "local-object-accession-before.sqlite"
BEFORE_README = SCRIPT_DIR / "local-object-accession-before-readme.md"
EXPECTED_LOCAL_OBJECTS = 186

TOOL_ROOT = Path("/home/vladimir/wp/tools/archive-preservation")
sys.path.insert(0, str(TOOL_ROOT))
sys.path.insert(0, str(SCRIPT_DIR))
import paragon_document_bundle  # noqa: E402
import kurt_schwarz_metadata_derivatives as metadata_tools  # noqa: E402


class AccessionError(RuntimeError):
    pass


def now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def fsync_file(path: Path) -> None:
    with path.open("rb") as handle:
        os.fsync(handle.fileno())


def fsync_dir(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def csv_bytes(fields: list[str], rows: list[dict[str, Any]]) -> bytes:
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(
        stream, fieldnames=fields, extrasaction="ignore", quoting=csv.QUOTE_ALL
    )
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue().encode("utf-8")


def read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        return list(reader.fieldnames or ()), list(reader)


def write_once(path: Path, payload: bytes) -> None:
    if path.exists():
        if path.read_bytes() != payload:
            raise AccessionError(f"existing accession artifact differs: {path}")
        return
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    with temporary.open("xb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    fsync_dir(path.parent)


def source_is_read_only() -> tuple[bool, str]:
    result = subprocess.run(
        ["findmnt", "-no", "TARGET,SOURCE,FSTYPE,OPTIONS", "--target", str(SOURCE)],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    fields = result.split(maxsplit=3)
    options = fields[3].split(",") if len(fields) == 4 else []
    return fields[:3] == [str(SOURCE), "/dev/sdf1", "ntfs3"] and "ro" in options, result


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(8 * 1024 * 1024):
            digest.update(block)
    return "sha256:" + digest.hexdigest()


def latest_rows(connection: sqlite3.Connection, table: str, key: str) -> dict[str, dict[str, Any]]:
    return {
        str(row[key]): dict(row)
        for row in connection.execute(
            f'''SELECT * FROM "{table}" WHERE decision_id IN (
                   SELECT MAX(decision_id) FROM "{table}" GROUP BY "{key}"
               )'''
        )
    }


def effective_metadata(candidate: dict[str, Any], decision: dict[str, Any]) -> dict[str, str]:
    review = json.loads(candidate["review_json"])
    metadata = json.loads(review.get("selected_metadata_json") or "{}")
    for target, source in (
        ("title", "curated_title"),
        ("date_text", "curated_date_text"),
        ("description", "curated_description"),
        ("tags", "curated_tags"),
    ):
        if decision.get(source) is not None:
            metadata[target] = decision[source]
    return {
        key: str(metadata.get(key) or "").strip()
        for key in (
            "inventory", "title", "date_text", "description", "tags", "places",
            "material", "dimensions", "scan_signature", "bag_label",
        )
    }


def load_scope() -> list[dict[str, Any]]:
    connection = sqlite3.connect(f"file:{RECONCILIATION}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise AccessionError("reconciliation database quick check failed")
        if connection.execute("PRAGMA foreign_key_check").fetchall():
            raise AccessionError("reconciliation database has foreign-key violations")
        decisions = latest_rows(connection, "object_census_decisions", "candidate_id")
        similarity = latest_rows(
            connection, "object_census_similarity_decisions", "pair_id"
        )
        candidates = {
            str(row["candidate_id"]): dict(row)
            for row in connection.execute("SELECT * FROM object_census_candidates")
        }
    finally:
        connection.close()

    selected_ids = sorted(
        candidate_id
        for candidate_id, decision in decisions.items()
        if decision["disposition"] == "new_local_metadata_right"
    )
    if len(selected_ids) != EXPECTED_LOCAL_OBJECTS:
        raise AccessionError(
            f"decided-local scope drifted: {len(selected_ids)} != {EXPECTED_LOCAL_OBJECTS}"
        )

    companions: dict[str, set[str]] = {candidate_id: set() for candidate_id in selected_ids}
    for decision in similarity.values():
        if decision["outcome"] != "same_photo":
            continue
        primary = str(decision["primary_candidate_id"] or "")
        companion = str(decision["companion_candidate_id"] or "")
        if primary in companions:
            companions[primary].add(companion)

    scope: list[dict[str, Any]] = []
    for candidate_id in selected_ids:
        candidate = candidates[candidate_id]
        decision = decisions[candidate_id]
        source_paths = json.loads(candidate["source_paths_json"])
        if candidate["primary_source_path"] not in source_paths:
            raise AccessionError(f"primary source is not manifested: {candidate_id}")
        path = Path(candidate["primary_source_path"])
        if not path.is_file() or not path.resolve().is_relative_to(SOURCE.resolve()):
            raise AccessionError(f"selected source is absent or outside Elements: {path}")
        suffix = path.suffix.casefold()
        if suffix not in {".tif", ".tiff", ".jpg", ".jpeg"}:
            raise AccessionError(f"unsupported selected Master format: {path}")
        if path.stat().st_size != int(candidate["size_bytes"]):
            raise AccessionError(f"selected source size drifted: {path}")
        companion_rows = []
        for companion_id in sorted(companions[candidate_id]):
            companion = candidates[companion_id]
            latest = decisions.get(companion_id) or {}
            if latest.get("disposition") != "same_new_candidate":
                raise AccessionError(
                    f"group companion is no longer Redundant: {companion_id}"
                )
            companion_rows.append(
                {
                    "candidate_id": companion_id,
                    "filename": companion["filename"],
                    "content_sha256": companion["content_sha256"],
                    "size_bytes": int(companion["size_bytes"]),
                    "source_paths": json.loads(companion["source_paths_json"]),
                }
            )
        master_relative_path = f"masters/{candidate['filename']}"
        access_relative_path = f"export/access/{Path(candidate['filename']).stem}.jpg"
        scope.append(
            {
                "candidate_id": candidate_id,
                "decision_id": int(decision["decision_id"]),
                "decided_at": decision["decided_at"],
                "actor": decision["actor"],
                "reason": decision["reason"] or "",
                "filename": candidate["filename"],
                "extension": candidate["extension"],
                "content_sha256": candidate["content_sha256"],
                "size_bytes": int(candidate["size_bytes"]),
                "primary_source_path": candidate["primary_source_path"],
                "source_paths": source_paths,
                "metadata": effective_metadata(candidate, decision),
                "companions": companion_rows,
                "master_relative_path": master_relative_path,
                "access_relative_path": access_relative_path,
            }
        )
    return sorted(scope, key=lambda item: (item["filename"].casefold(), item["candidate_id"]))


MANIFEST_FIELDS = [
    "candidate_id", "decision_id", "decided_at", "filename", "primary_source_path",
    "source_paths", "content_sha256", "size_bytes", "master_relative_path",
    "access_relative_path", "inventory", "title", "date_text", "description",
    "tags", "places", "material", "dimensions", "scan_signature", "bag_label",
    "redundant_candidate_ids", "redundant_source_paths", "deletion_authorized",
]


def manifest_rows(scope: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for item in scope:
        metadata = item["metadata"]
        rows.append(
            {
                **{field: item.get(field, "") for field in MANIFEST_FIELDS},
                **metadata,
                "source_paths": json.dumps(item["source_paths"], ensure_ascii=False),
                "redundant_candidate_ids": "; ".join(
                    companion["candidate_id"] for companion in item["companions"]
                ),
                "redundant_source_paths": json.dumps(
                    [
                        path
                        for companion in item["companions"]
                        for path in companion["source_paths"]
                    ],
                    ensure_ascii=False,
                ),
                "deletion_authorized": "false",
            }
        )
    return rows


def preflight(scope: list[dict[str, Any]]) -> dict[str, Any]:
    read_only, mount = source_is_read_only()
    if not read_only:
        raise AccessionError(f"Elements is not safely read-only: {mount}")
    for path in (UNIT, WORK, UNIT / "masters", UNIT / "export/access"):
        if not path.is_dir():
            raise AccessionError(f"required path is absent: {path}")
    if UNIT.stat().st_dev != WORK.stat().st_dev:
        raise AccessionError("archive and work roots are not on the same filesystem")
    master_paths = [item["master_relative_path"].casefold() for item in scope]
    access_paths = [item["access_relative_path"].casefold() for item in scope]
    if len(master_paths) != len(set(master_paths)) or len(access_paths) != len(set(access_paths)):
        raise AccessionError("decided-local destination paths collide")
    existing_master = {p.name.casefold() for p in (UNIT / "masters").iterdir() if p.is_file()}
    existing_access = {p.name.casefold() for p in (UNIT / "export/access").iterdir() if p.is_file()}
    for item in scope:
        master = UNIT / item["master_relative_path"]
        access = UNIT / item["access_relative_path"]
        if master.name.casefold() in existing_master and not master.is_file():
            raise AccessionError(f"case-colliding Master destination: {master}")
        if access.name.casefold() in existing_access and not access.is_file():
            raise AccessionError(f"case-colliding access destination: {access}")
    restore = sqlite3.connect(
        f"file:{UNIT / 'db-restore/RESTORE.sqlite'}?mode=ro", uri=True
    )
    try:
        objects = restore.execute("SELECT COUNT(*) FROM objects").fetchone()[0]
        representations = restore.execute("SELECT COUNT(*) FROM representations").fetchone()[0]
        if objects not in {1665, 1665 + EXPECTED_LOCAL_OBJECTS}:
            raise AccessionError(f"published object count is unexpected: {objects}")
        if representations not in {3330, 3330 + 2 * EXPECTED_LOCAL_OBJECTS}:
            raise AccessionError(
                f"published representation count is unexpected: {representations}"
            )
    finally:
        restore.close()
    return {
        "result": "ok",
        "source_mount": mount,
        "local_objects": len(scope),
        "tiff_masters": sum(item["extension"].casefold() in {".tif", ".tiff"} for item in scope),
        "jpeg_masters": sum(item["extension"].casefold() in {".jpg", ".jpeg"} for item in scope),
        "master_bytes": sum(item["size_bytes"] for item in scope),
        "redundant_companions": sum(len(item["companions"]) for item in scope),
        "deletion_authorized": False,
    }


def catalog_base_row(item: dict[str, Any]) -> dict[str, str]:
    metadata = item["metadata"]
    return {
        "record_key": item["candidate_id"],
        "inventory": metadata["inventory"],
        "master_relative_path": item["master_relative_path"],
        "access_derivative_status": "generated_full_resolution_jpeg_q95",
        "title": metadata["title"],
        "description": metadata["description"],
        "date_text": metadata["date_text"],
        "material": metadata["material"],
        "dimensions": metadata["dimensions"],
        "places": metadata["places"],
        "tags": metadata["tags"],
        "creator": "Kurt Schwarz",
        "md_object_id": "",
        "md_url": "",
        "match_status": "no_match_found",
        "match_basis": "curator_decided_local_object_outside_md_scope",
        "original_source_path": item["primary_source_path"],
        "md_subdivision_ids": "",
        "md_subdivisions": "",
    }


def process_media(scope: list[dict[str, Any]]) -> list[dict[str, Any]]:
    results = []
    total = len(scope)
    for index, item in enumerate(scope, 1):
        source = Path(item["primary_source_path"])
        master = UNIT / item["master_relative_path"]
        access = UNIT / item["access_relative_path"]
        if master.exists():
            master_hash = sha256_file(master)
            if master_hash != item["content_sha256"] or master.stat().st_size != item["size_bytes"]:
                raise AccessionError(f"existing local Master differs: {master}")
        else:
            temporary = SCRIPT_DIR / f".{item['candidate_id']}.master.tmp-{os.getpid()}{source.suffix}"
            if temporary.exists():
                raise AccessionError(f"unexpected Master temporary file: {temporary}")
            shutil.copyfile(source, temporary)
            fsync_file(temporary)
            master_hash = sha256_file(temporary)
            if master_hash != item["content_sha256"] or temporary.stat().st_size != item["size_bytes"]:
                temporary.unlink(missing_ok=True)
                raise AccessionError(f"exact Master copy verification failed: {source}")
            os.replace(temporary, master)
            fsync_dir(master.parent)

        base = catalog_base_row(item)
        master_catalog = {
            **base,
            "file_role": "preservation_master",
            "final_relative_path": item["master_relative_path"],
            "filename": master.name,
            "file_type": master.suffix.removeprefix(".").casefold(),
            "size_bytes": str(master.stat().st_size),
            "final_sha256": master_hash,
        }
        rights = metadata_tools.exif_json(master, metadata_tools.RIGHTS_TAGS)
        access_plan = metadata_tools.metadata_plan(master_catalog, master_hash)
        if access.exists():
            metadata_tools.verify_metadata(access, access_plan, rights)
            access_hash = sha256_file(access)
        else:
            temporary_access = SCRIPT_DIR / f".{item['candidate_id']}.access.tmp-{os.getpid()}.jpg"
            if temporary_access.exists():
                raise AccessionError(f"unexpected access temporary file: {temporary_access}")
            metadata_tools.generate_access(master, temporary_access)
            metadata_tools.write_metadata_in_place(temporary_access, access_plan, rights)
            metadata_tools.verify_metadata(temporary_access, access_plan, rights)
            source_width, source_height, orientation = metadata_tools.image_dimensions(master)
            access_width, access_height, _ = metadata_tools.image_dimensions(temporary_access)
            expected = (
                (source_height, source_width)
                if orientation in {5, 6, 7, 8}
                else (source_width, source_height)
            )
            if (access_width, access_height) != expected:
                temporary_access.unlink(missing_ok=True)
                raise AccessionError(
                    f"access dimensions changed for {master.name}: "
                    f"{(access_width, access_height)} != {expected}"
                )
            access_hash = sha256_file(temporary_access)
            os.replace(temporary_access, access)
            fsync_dir(access.parent)
        results.append(
            {
                **item,
                "master_hash": master_hash,
                "master_size": master.stat().st_size,
                "master_mtime_ns": master.stat().st_mtime_ns,
                "access_hash": access_hash,
                "access_size": access.stat().st_size,
                "access_mtime_ns": access.stat().st_mtime_ns,
                "master_catalog": master_catalog,
            }
        )
        print(f"[{index}/{total}] {master.name}", flush=True)
    return results


def object_row(fields: list[str], result: dict[str, Any]) -> dict[str, str]:
    item = result
    metadata = item["metadata"]
    candidate_files = [item["filename"]] + [row["filename"] for row in item["companions"]]
    all_paths = list(item["source_paths"]) + [
        path for companion in item["companions"] for path in companion["source_paths"]
    ]
    row = {field: "" for field in fields}
    row.update(
        {
            "record_key": item["candidate_id"],
            "record_state": "local_only_object_census",
            "current_inventory": metadata["inventory"],
            "current_title": metadata["title"],
            "current_description": metadata["description"],
            "current_tags": metadata["tags"],
            "current_places": metadata["places"],
            "current_date_text": metadata["date_text"],
            "current_material": metadata["material"],
            "current_dimensions": metadata["dimensions"],
            "scan_signature": metadata["scan_signature"],
            "original_title_filenames": "; ".join(candidate_files),
            "bag_label": metadata["bag_label"],
            "selected_source_path": item["primary_source_path"],
            "selected_source_format": (
                "tiff" if item["extension"].casefold() in {".tif", ".tiff"} else "jpeg"
            ),
            "selection_basis": "object_census_curator_decision",
            "selection_state": "curator_selected_master",
            "source_scope": "local_object_census",
            "tiff_candidate_count": str(
                sum(Path(name).suffix.casefold() in {".tif", ".tiff"} for name in candidate_files)
            ),
            "jpeg_candidate_count": str(
                sum(Path(name).suffix.casefold() in {".jpg", ".jpeg"} for name in candidate_files)
            ),
            "all_candidate_paths": "; ".join(all_paths),
            "reviewed_visual_state": "curator_selected_local_object",
            "object_metadata_state": "curated_local_metadata",
            "unit": "kurt-schwarz",
            "master_relative_path": item["master_relative_path"],
            "master_sha256": item["master_hash"],
            "access_relative_path": item["access_relative_path"],
            "access_derivative_status": "generated_full_resolution_jpeg_q95",
        }
    )
    return row


def table_fields(connection: sqlite3.Connection, table: str) -> list[str]:
    return [str(row[1]) for row in connection.execute(f'PRAGMA table_info("{table}")')]


def table_rows(connection: sqlite3.Connection, table: str, order: str = "rowid") -> list[dict[str, str]]:
    fields = table_fields(connection, table)
    return [
        {field: str(value) for field, value in zip(fields, row)}
        for row in connection.execute(f'SELECT * FROM "{table}" ORDER BY {order}')
    ]


def md_digest(connection: sqlite3.Connection) -> str:
    fields = table_fields(connection, "objects")
    rows = [
        list(row)
        for row in connection.execute(
            "SELECT * FROM objects WHERE md_object_id<>'' ORDER BY record_key"
        )
    ]
    payload = json.dumps([fields, rows], ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def build_controls(results: list[dict[str, Any]]) -> tuple[dict[Path, bytes], dict[str, int]]:
    restore_path = UNIT / "db-restore/RESTORE.sqlite"
    source = sqlite3.connect(f"file:{restore_path}?mode=ro", uri=True)
    source.row_factory = sqlite3.Row
    before_md = md_digest(source)
    if not BEFORE_DB.exists():
        backup = sqlite3.connect(BEFORE_DB)
        source.backup(backup)
        backup.close()
        fsync_file(BEFORE_DB)
    write_once(BEFORE_README, (UNIT / "00_README.md").read_bytes())

    temporary_db = SCRIPT_DIR / f".local-object-accession-RESTORE.tmp-{os.getpid()}.sqlite"
    if temporary_db.exists():
        raise AccessionError(f"unexpected restore temporary file: {temporary_db}")
    target = sqlite3.connect(temporary_db)
    source.backup(target)
    source.close()
    target.row_factory = sqlite3.Row

    object_fields = table_fields(target, "objects")
    representation_fields = table_fields(target, "representations")
    checksum_fields = table_fields(target, "checksums")
    decision_fields = table_fields(target, "reconciliation_decisions")
    next_index = int(
        target.execute('SELECT COALESCE(MAX(CAST("index" AS INTEGER)),0) FROM representations').fetchone()[0]
    )
    completed_at = now()

    catalog_fields, catalog = read_csv(UNIT / "CATALOG.csv")
    existing_record_keys = {
        row[0] for row in target.execute("SELECT record_key FROM objects")
    }
    existing_catalog_paths = {row["final_relative_path"] for row in catalog}
    source_disposition_paths = {
        row[0]
        for row in target.execute("SELECT source_path FROM source_disposition")
    }

    new_catalog: list[dict[str, str]] = []
    for result in results:
        if result["candidate_id"] in existing_record_keys:
            raise AccessionError(f"local object already exists in restore database: {result['candidate_id']}")
        if result["master_relative_path"] in existing_catalog_paths or result["access_relative_path"] in existing_catalog_paths:
            raise AccessionError(f"local object catalog path already exists: {result['candidate_id']}")
        target.execute(
            'INSERT INTO objects VALUES (' + ",".join("?" for _ in object_fields) + ")",
            [object_row(object_fields, result)[field] for field in object_fields],
        )

        base = catalog_base_row(result)
        master_catalog = result["master_catalog"]
        access_catalog = {
            **base,
            "file_role": "access_derivative",
            "final_relative_path": result["access_relative_path"],
            "filename": Path(result["access_relative_path"]).name,
            "file_type": "jpg",
            "size_bytes": str(result["access_size"]),
            "final_sha256": result["access_hash"],
        }
        new_catalog.extend((master_catalog, access_catalog))

        for role, relative, final_hash, size, mtime, source_kind, copy_policy, basis, relationship in (
            (
                "preservation_master", result["master_relative_path"], result["master_hash"],
                result["master_size"], result["master_mtime_ns"], "file", "verified_exact_copy",
                "curator_selected_local_master_metadata_in_catalog",
                "local_object_outside_md_scope",
            ),
            (
                "access_derivative", result["access_relative_path"], result["access_hash"],
                result["access_size"], result["access_mtime_ns"], "generated_from_master",
                "generated_full_resolution_jpeg_q95",
                "libvips_full_native_dimensions_metadata_embedded",
                f"access_derivative_of:{result['master_relative_path']}",
            ),
        ):
            next_index += 1
            representation = {field: "" for field in representation_fields}
            representation.update(
                {
                    "index": str(next_index),
                    "completed_at": completed_at,
                    "representation_key": final_hash,
                    "record_keys": result["candidate_id"],
                    "inventories": result["metadata"]["inventory"],
                    "role": role,
                    "source_kind": source_kind,
                    "source_path": result["primary_source_path"],
                    "source_member": result["master_relative_path"] if role == "access_derivative" else "",
                    "source_sha256": result["content_sha256"],
                    "final_sha256": final_hash,
                    "final_relative_path": relative,
                    "size_bytes": str(size),
                    "mtime_ns": str(mtime),
                    "copy_policy": copy_policy,
                    "basis": basis,
                    "relationship": relationship,
                }
            )
            target.execute(
                'INSERT INTO representations VALUES (' + ",".join("?" for _ in representation_fields) + ")",
                [representation[field] for field in representation_fields],
            )
            checksum = {field: "" for field in checksum_fields}
            checksum.update(
                {
                    "path": relative,
                    "sha256": final_hash,
                    "size_bytes": str(size),
                    "content_role": role,
                }
            )
            target.execute(
                'INSERT INTO checksums VALUES (' + ",".join("?" for _ in checksum_fields) + ")",
                [checksum[field] for field in checksum_fields],
            )

        primary = result["primary_source_path"]
        all_selected_occurrences = list(result["source_paths"])
        related_sources = [
            (
                path,
                "preservation_master" if path == primary else "redundant_exact_copy",
                "preserved_exact_local_master" if path == primary else "preserved_exact_copy_of_local_master",
                "true",
                "retain_accessioned_source" if path == primary else "retain_reviewed_redundant",
            )
            for path in all_selected_occurrences
        ]
        related_sources.extend(
            (
                path,
                "redundant_candidate",
                "reviewed_redundant_not_accessioned",
                "false",
                "retain_reviewed_redundant",
            )
            for companion in result["companions"]
            for path in companion["source_paths"]
        )
        for path, source_role, status, exact, drain_state in related_sources:
            if path not in source_disposition_paths:
                raise AccessionError(f"source disposition lacks exact path: {path}")
            target.execute(
                """UPDATE source_disposition SET inventory=?,source_role=?,final_path=?,
                          preservation_status=?,preservation_basis=?,final_sha256=?,
                          exact_copy_proven=?,drain_state=?,deletion_authorized='false',notes=?
                   WHERE source_path=?""",
                (
                    result["metadata"]["inventory"],
                    source_role,
                    result["master_relative_path"],
                    status,
                    "curator_decided_local_object_outside_md_scope",
                    result["master_hash"],
                    exact,
                    drain_state,
                    "Selected local object relationship preserved; source remains protected and no deletion is authorized.",
                    path,
                ),
            )
            if target.execute("SELECT changes()").fetchone()[0] != 1:
                raise AccessionError(f"source disposition path is not unique: {path}")
            decision = {field: "" for field in decision_fields}
            decision.update(
                {
                    "source_path": path,
                    "group_id": f"object-census:{result['candidate_id']}",
                    "disposition": "selected_master" if path == primary else "redundant",
                    "master_path": primary,
                    "note": result["reason"],
                    "actor": result["actor"],
                    "decided_at": result["decided_at"],
                    "deletion_authorized": "false",
                }
            )
            target.execute(
                'INSERT INTO reconciliation_decisions VALUES ('
                + ",".join("?" for _ in decision_fields)
                + ")",
                [decision[field] for field in decision_fields],
            )

    if md_digest(target) != before_md:
        raise AccessionError("existing museum-digital object rows changed")
    target.commit()
    if target.execute("PRAGMA quick_check").fetchone()[0] != "ok":
        raise AccessionError("new restore database quick check failed")
    if target.execute("PRAGMA foreign_key_check").fetchall():
        raise AccessionError("new restore database has foreign-key violations")
    expected_objects = 1665 + EXPECTED_LOCAL_OBJECTS
    expected_representations = 3330 + 2 * EXPECTED_LOCAL_OBJECTS
    if target.execute("SELECT COUNT(*) FROM objects").fetchone()[0] != expected_objects:
        raise AccessionError("new restore object count is wrong")
    if target.execute("SELECT COUNT(*) FROM representations").fetchone()[0] != expected_representations:
        raise AccessionError("new restore representation count is wrong")
    if target.execute("SELECT COUNT(*) FROM checksums").fetchone()[0] != expected_representations:
        raise AccessionError("new restore checksum count is wrong")
    if target.execute("SELECT COUNT(*) FROM source_disposition WHERE deletion_authorized<>'false'").fetchone()[0]:
        raise AccessionError("new restore state authorizes source deletion")

    target.commit()
    target.close()
    fsync_file(temporary_db)

    catalog.extend(new_catalog)
    if len(catalog) != expected_representations:
        raise AccessionError("new human-readable catalog count is wrong")
    controls: dict[Path, bytes] = {
        UNIT / "CATALOG.csv": csv_bytes(catalog_fields, catalog),
        UNIT / "db-restore/RESTORE.csv": csv_bytes(catalog_fields, catalog),
        UNIT / "CATALOG.xlsx": paragon_document_bundle.table_xlsx_bytes(
            catalog_fields, catalog, sheet_name="Catalog", table_name="Catalog"
        ),
        UNIT / "db-restore/RESTORE.xlsx": paragon_document_bundle.table_xlsx_bytes(
            catalog_fields, catalog, sheet_name="Restore", table_name="Restore"
        ),
    }

    generated = sqlite3.connect(f"file:{temporary_db}?mode=ro", uri=True)
    generated.row_factory = sqlite3.Row
    for table, path, order in (
        ("checksums", UNIT / "db-restore/checksums.csv", "rowid"),
        (
            "representations",
            UNIT / "db-restore/manifests/representations.csv",
            'CAST("index" AS INTEGER)',
        ),
        (
            "source_disposition",
            UNIT / "db-restore/manifests/source-disposition.csv",
            "rowid",
        ),
    ):
        fields = table_fields(generated, table)
        controls[path] = csv_bytes(fields, table_rows(generated, table, order))
    sql_stream = io.StringIO()
    for line in generated.iterdump():
        sql_stream.write(line)
        sql_stream.write("\n")
    generated.close()
    controls[UNIT / "db-restore/RESTORE.sql"] = sql_stream.getvalue().encode("utf-8")
    controls[UNIT / "db-restore/RESTORE.sqlite"] = temporary_db.read_bytes()

    readme = (UNIT / "00_README.md").read_text(encoding="utf-8")
    readme = readme.replace("# Kurt Schwarz MD-reconciled archive unit", "# Kurt Schwarz archive unit")
    readme = readme.replace("- Retained objects and Masters: 1665", f"- Retained objects and Masters: {expected_objects}")
    readme = readme.replace("- Generated access derivatives: 1665", f"- Generated access derivatives: {expected_objects}")
    readme = readme.replace(
        "- Museum-digital record relationships represented: 1668",
        "- Museum-digital record relationships represented: 1668\n"
        f"- Curator-decided local objects accessioned outside MD scope: {EXPECTED_LOCAL_OBJECTS}",
    )
    readme = readme.replace(
        "All 1,665 selected Masters have a generated full-resolution access derivative.",
        f"All {expected_objects:,} selected Masters have a generated full-resolution access derivative.",
    )
    readme = readme.replace(
        "Published Masters carry the reconciled catalog metadata while preserving their original image encoding, decoded pixels, ICC profile, and Photoshop layer data where present;",
        "The MD-reconciled Masters carry embedded catalog metadata while preserving their original image encoding, decoded pixels, ICC profile, and Photoshop layer data where present. The newly accessioned local Masters preserve exact source bytes; their accepted metadata is stored in the catalog and their access JPEGs;",
    )
    controls[UNIT / "00_README.md"] = readme.encode("utf-8")

    counts = {
        "objects": expected_objects,
        "representations": expected_representations,
        "checksums": expected_representations,
        "local_objects_added": EXPECTED_LOCAL_OBJECTS,
        "source_disposition": 17077,
    }
    temporary_db.unlink()
    return controls, counts


def publish_controls(controls: dict[Path, bytes]) -> None:
    staged: list[tuple[Path, Path]] = []
    for destination, payload in controls.items():
        temporary = SCRIPT_DIR / f".local-accession-control-{len(staged):02d}-{os.getpid()}.tmp"
        with temporary.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        staged.append((temporary, destination))
    for temporary, destination in staged:
        os.replace(temporary, destination)
        fsync_dir(destination.parent)


def validate_final(scope: list[dict[str, Any]], counts: dict[str, int]) -> dict[str, Any]:
    read_only, mount = source_is_read_only()
    if not read_only:
        raise AccessionError(f"Elements lost read-only state: {mount}")
    connection = sqlite3.connect(
        f"file:{UNIT / 'db-restore/RESTORE.sqlite'}?mode=ro", uri=True
    )
    try:
        if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise AccessionError("published restore database quick check failed")
        if connection.execute("PRAGMA foreign_key_check").fetchall():
            raise AccessionError("published restore database has foreign-key violations")
        for table in ("objects", "representations", "checksums", "source_disposition"):
            if connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0] != counts[table]:
                raise AccessionError(f"published count mismatch: {table}")
        if connection.execute("SELECT COUNT(*) FROM source_disposition WHERE deletion_authorized<>'false'").fetchone()[0]:
            raise AccessionError("published controls authorize deletion")
        for item in scope:
            if connection.execute(
                "SELECT COUNT(*) FROM objects WHERE record_key=? AND master_relative_path=?",
                (item["candidate_id"], item["master_relative_path"]),
            ).fetchone()[0] != 1:
                raise AccessionError(f"published local object is missing: {item['candidate_id']}")
    finally:
        connection.close()
    catalog_fields, catalog = read_csv(UNIT / "CATALOG.csv")
    if len(catalog) != counts["representations"] or not catalog_fields:
        raise AccessionError("published human-readable catalog is incomplete")
    master_rows = [row for row in catalog if row["file_role"] == "preservation_master"]
    if len(master_rows) != counts["objects"] or len({row["record_key"] for row in master_rows}) != counts["objects"]:
        raise AccessionError("published catalog does not have exactly one Master per object")
    if sum(p.is_file() for p in (UNIT / "masters").iterdir()) != counts["objects"]:
        raise AccessionError("published Master file count is wrong")
    if sum(p.is_file() for p in (UNIT / "export/access").iterdir()) != counts["objects"]:
        raise AccessionError("published access derivative count is wrong")
    return {
        "format": "iss-kurt-schwarz-decided-local-accession-v1",
        "completed_at": now(),
        "result": "ok",
        **counts,
        "source_read_only": True,
        "source_mount": mount,
        "selected_master_bytes_exact": True,
        "metadata_location": "CATALOG.csv/CATALOG.xlsx, RESTORE files, and access JPEGs",
        "existing_md_objects_changed": False,
        "deletion_authorized": False,
    }


def apply(scope: list[dict[str, Any]]) -> dict[str, Any]:
    if COMPLETION.exists():
        return json.loads(COMPLETION.read_text(encoding="utf-8"))
    write_once(MANIFEST, csv_bytes(MANIFEST_FIELDS, manifest_rows(scope)))
    results = process_media(scope)
    controls, counts = build_controls(results)
    publish_controls(controls)
    completion = validate_final(scope, counts)
    write_once(
        COMPLETION,
        (json.dumps(completion, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8"),
    )
    return completion


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    try:
        scope = load_scope()
        result = preflight(scope)
        if args.apply:
            result = apply(scope)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
        return 0
    except (
        AccessionError,
        OSError,
        ValueError,
        csv.Error,
        sqlite3.Error,
        subprocess.CalledProcessError,
        metadata_tools.WorkerError,
    ) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
