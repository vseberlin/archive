#!/usr/bin/env python3
"""Publish the eight approved Kurt Schwarz unreadable-Master corrections."""

from __future__ import annotations

import csv
import json
import os
import sqlite3
import subprocess
from collections import Counter
from pathlib import Path

import kurt_schwarz_metadata_derivatives as metadata


UNIT = Path("/mnt/volume/iss-archive/units/kurt-schwarz")
WORK = Path("/mnt/volume/iss-archive-work/kurt-schwarz")
RUN = WORK / "work/md-finalization/metadata-derivatives-001"
CANDIDATES = RUN / "candidates"
RECEIPTS = RUN / "receipts"
RECONCILIATION = WORK / "work/reconciliation/unit-reconciliation-v1/reconciliation.sqlite"
REPAIR_RECEIPT = RUN / "unreadable-master-replacement.json"

REPAIRS = (
    {
        "record_key": "md:98437",
        "inventory": "KS-6-BZ_0200-1",
        "old_candidate": "MEDIA-EB7BF1E4F6D7D9C640DEE82A",
        "new_candidate": "MEDIA-07A67401C51C0150F43710B6",
        "source": "/media/vladimir/Elements/restore/archive/WF/Kurt Schwarz/Alle-KS-Bilder_5000px/KS-6-BZ_0200-1.jpg",
        "source_sha256": "sha256:88d896ba27a724f85c9801d4346d40400ab3b8319fdf51b2e0404c86b48ac3fb",
    },
    {
        "record_key": "md:98547",
        "inventory": "KS-6-BZ_0415-1",
        "old_candidate": "MEDIA-7C8534D4E27BD61C5590AE73",
        "new_candidate": "MEDIA-F8608A7FCCF11146FB9D96DE",
        "source": "/media/vladimir/Elements/restore/archive/WF/Kurt Schwarz/UPLOADS/3.Upload/1629536_MF/D09_T139_001.tif",
        "source_sha256": "sha256:1197f0971142ad4000bfd19007b3fd5413279dc974274cd7c1bf55741a3419e2",
    },
    {
        "record_key": "md:101493",
        "inventory": "KS-6-BZ_0442",
        "old_candidate": "MEDIA-60A98906987874125F94384A",
        "new_candidate": "MEDIA-AA654D89CACF4EE641189407",
        "source": "/media/vladimir/Elements/restore/archive/WF/Kurt Schwarz/Alle-KS-Bilder_5000px/KS-6-BZ_0442.jpg",
        "source_sha256": "sha256:0186136c454ab8afa172da17b3225824ddd6ddd3df8b91ca0e7b7e8d02d3a3f2",
    },
    {
        "record_key": "md:101358",
        "inventory": "KS-6-BZ_0484-5",
        "old_candidate": "MEDIA-D181B9AF5562EB4935423DE2",
        "new_candidate": "MEDIA-A01EC113A25BEE4094C0FB1A",
        "source": "/media/vladimir/Elements/restore/archive/WF/Kurt Schwarz/Alle-KS-Bilder_5000px/KS-6-BZ_0484-5.jpg",
        "source_sha256": "sha256:751bcaf939175e09c7d16d900c5d4f5c9c5997a54e1b648517bc7ca2d6d26c12",
    },
    {
        "record_key": "md:103558",
        "inventory": "KS-6-BZ_0728",
        "old_candidate": "MEDIA-0538E75D7739D7C1AEC3CC3E",
        "new_candidate": "MEDIA-854130617732A36209A60787",
        "source": "/media/vladimir/Elements/restore/archive/WF/Kurt Schwarz/Alle-KS-Bilder_5000px/KS-6-BZ_0728.jpg",
        "source_sha256": "sha256:398ed95550812d7d721e53f353aad8b892489a2fc14e526e75680cef55904cd1",
    },
    {
        "record_key": "md:96886",
        "inventory": "KS-7-BZ_0171",
        "old_candidate": "MEDIA-A05A9B8CCDC7E8729BB2D20F",
        "new_candidate": "MEDIA-D74BA35F5338271BCD60F556",
        "source": "/media/vladimir/Elements/restore/archive/WF/Kurt Schwarz/Alle-KS-Bilder_5000px/KS-7-BZ_0171.jpg",
        "source_sha256": "sha256:a8133db23df6cbd0773fa7e1d6860d4f0f544d0c698f78172ea68fb25a9d0e99",
    },
    {
        "record_key": "md:96891",
        "inventory": "KS-7-BZ_0188-1",
        "old_candidate": "MEDIA-E5AD831DC7CE06A210DE473F",
        "new_candidate": "MEDIA-14C87C54D15E94A9060356C1",
        "source": "/media/vladimir/Elements/restore/archive/WF/Kurt Schwarz/Alle-KS-Bilder_5000px/KS-7-BZ_0188-1.jpg",
        "source_sha256": "sha256:1c09be95da38e61e1af1af2f0a46100ac2f0fbad3e34fc32bbf04a65ebd94b7e",
    },
    {
        "record_key": "md:103600",
        "inventory": "KS-7-BZ_0553-2",
        "old_candidate": "MEDIA-3A7FCD0AB84ADB0FAD7F70D4",
        "new_candidate": "MEDIA-C9E48E896051EB7776D88D47",
        "source": "/media/vladimir/Elements/restore/archive/WF/Kurt Schwarz/Alle-KS-Bilder_5000px/KS-7-BZ_0553-2.jpg",
        "source_sha256": "sha256:b4d6c036d6ce5ee90ee1ce4a7457a635b48eba9fa77f311987068828f4962da4",
    },
)


def read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        return list(reader.fieldnames or ()), list(reader)


def source_mount() -> str:
    line = subprocess.run(
        ["findmnt", "-n", "-o", "SOURCE,TARGET,FSTYPE,OPTIONS", "--target", REPAIRS[0]["source"]],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    options = set(line.split(None, 3)[3].split(","))
    if "ro" not in options or "rw" in options:
        raise metadata.IntegrityError(f"source is not read-only: {line}")
    return line


def validate_decisions() -> dict[str, dict[str, str]]:
    connection = sqlite3.connect(f"file:{RECONCILIATION}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise metadata.IntegrityError("reconciliation database quick_check failed")
        if connection.execute("PRAGMA foreign_key_check").fetchall():
            raise metadata.IntegrityError("reconciliation database foreign keys failed")
        decisions: dict[str, dict[str, str]] = {}
        for repair in REPAIRS:
            rows = {}
            for label, candidate_id, disposition in (
                ("old", repair["old_candidate"], "redundant"),
                ("new", repair["new_candidate"], "selected_master"),
            ):
                row = connection.execute(
                    """SELECT c.record_key,c.content_sha256,c.size_bytes,d.disposition,
                              d.actor,d.reason,d.decided_at
                         FROM media_candidates c
                         JOIN media_decisions d ON d.decision_id=(
                              SELECT MAX(d2.decision_id) FROM media_decisions d2
                               WHERE d2.candidate_id=c.candidate_id)
                        WHERE c.candidate_id=?""",
                    (candidate_id,),
                ).fetchone()
                if row is None or row["record_key"] != repair["record_key"] or row["disposition"] != disposition:
                    raise metadata.IntegrityError(f"decision binding drifted: {candidate_id}")
                rows[label] = dict(row)
            if rows["new"]["content_sha256"] != repair["source_sha256"]:
                raise metadata.IntegrityError(f"selected source digest drifted: {repair['inventory']}")
            decisions[repair["record_key"]] = rows
        return decisions
    finally:
        connection.close()


def stage(catalog_masters: dict[str, dict[str, str]], decisions: dict[str, dict[str, str]]) -> list[dict]:
    staged: list[dict] = []
    for position, repair in enumerate(REPAIRS, 1):
        source = Path(repair["source"])
        if not source.is_file() or source.stat().st_size != int(decisions[repair["record_key"]]["new"]["size_bytes"]):
            raise metadata.IntegrityError(f"selected source missing or size drifted: {source}")
        if metadata.sha256_once(source) != repair["source_sha256"]:
            raise metadata.IntegrityError(f"selected source hash drifted: {source}")
        row = dict(catalog_masters[repair["record_key"]])
        old_master_relative_path = row["final_relative_path"]
        old_master = UNIT / old_master_relative_path
        suffix = source.suffix.lower()
        if suffix not in {".tif", ".tiff", ".jpg", ".jpeg"}:
            raise metadata.IntegrityError(f"unsupported replacement format: {source}")
        master_relative_path = f"masters/{repair['inventory']}{suffix}"
        access_relative_path = f"export/access/{repair['inventory']}.jpg"
        row["final_relative_path"] = master_relative_path
        row["master_relative_path"] = master_relative_path
        row["original_source_path"] = str(source)
        master_candidate = CANDIDATES / f"{repair['inventory']}.master{suffix}"
        access_candidate = CANDIDATES / f"{repair['inventory']}.access.jpg"
        receipt_path = RECEIPTS / f"{repair['inventory']}.json"
        for path in (master_candidate, access_candidate, receipt_path):
            if path.exists():
                raise metadata.IntegrityError(f"repair output already exists: {path}")
        rights = metadata.exif_json(source, metadata.RIGHTS_TAGS)
        master_plan = metadata.metadata_plan(row)
        metadata.copy_with_metadata(source, master_candidate, master_plan, rights)
        metadata.verify_metadata(master_candidate, master_plan, rights)
        technical, depth, pixel_digest = metadata.verify_structure(source, master_candidate)
        master_hash = metadata.sha256_once(master_candidate)
        access_plan = metadata.metadata_plan(row, master_hash)
        metadata.generate_access(master_candidate, access_candidate)
        metadata.write_metadata_in_place(access_candidate, access_plan, rights)
        metadata.verify_metadata(access_candidate, access_plan, rights)
        source_width, source_height, orientation = metadata.image_dimensions(master_candidate)
        access_width, access_height, _ = metadata.image_dimensions(access_candidate)
        expected = (source_height, source_width) if orientation in {5, 6, 7, 8} else (source_width, source_height)
        if (access_width, access_height) != expected:
            raise metadata.IntegrityError(f"access dimensions changed: {repair['inventory']}")
        access_hash = metadata.sha256_once(access_candidate)
        staged.append({
            **repair,
            "old_master_relative_path": old_master_relative_path,
            "old_master_sha256": row.get("final_sha256", ""),
            "old_source_path": catalog_masters[repair["record_key"]]["original_source_path"],
            "master_relative_path": master_relative_path,
            "master_sha256": master_hash,
            "master_size_bytes": master_candidate.stat().st_size,
            "master_mtime_ns": master_candidate.stat().st_mtime_ns,
            "master_pixel_digest": pixel_digest,
            "master_depth": depth,
            "master_technical": technical,
            "master_metadata": master_plan,
            "access_relative_path": access_relative_path,
            "access_sha256": access_hash,
            "access_size_bytes": access_candidate.stat().st_size,
            "access_mtime_ns": access_candidate.stat().st_mtime_ns,
            "access_width": access_width,
            "access_height": access_height,
            "access_metadata": access_plan,
            "rights_preserved": rights,
            "master_candidate_path": str(master_candidate),
            "access_candidate_path": str(access_candidate),
            "decision": decisions[repair["record_key"]],
        })
        print(f"staged={position}/{len(REPAIRS)} {repair['inventory']}", flush=True)
    return staged


def prepare_metadata(staged: list[dict]) -> tuple[dict[Path, bytes], Path, list[dict[str, str]]]:
    catalog_fields, catalog = read_csv(UNIT / "CATALOG.csv")
    representation_fields, representations = read_csv(UNIT / "db-restore/manifests/representations.csv")
    checksum_fields, checksums = read_csv(UNIT / "db-restore/checksums.csv")
    source_fields, source_rows = read_csv(UNIT / "db-restore/manifests/source-disposition.csv")
    drain_fields, drain_rows = read_csv(UNIT / "db-restore/manifests/drain-list.csv")
    decision_fields, decision_rows = read_csv(UNIT / "db-restore/manifests/reconciliation-decisions.csv")
    gap_fields, gaps = read_csv(UNIT / "db-restore/manifests/access-derivative-gaps.csv")
    repair_by_record = {row["record_key"]: row for row in staged}
    if {row["record_key"] for row in gaps} != set(repair_by_record):
        raise metadata.IntegrityError("published gap manifest drifted")

    new_catalog: list[dict[str, str]] = []
    for row in catalog:
        repair = repair_by_record.get(row["record_key"])
        if repair is None:
            new_catalog.append(row)
            continue
        if row["file_role"] != "preservation_master":
            raise metadata.IntegrityError(f"gap object unexpectedly has access row: {row['record_key']}")
        master = dict(row)
        master.update({
            "final_relative_path": repair["master_relative_path"],
            "filename": Path(repair["master_relative_path"]).name,
            "file_type": Path(repair["master_relative_path"]).suffix.lstrip("."),
            "size_bytes": str(repair["master_size_bytes"]),
            "final_sha256": repair["master_sha256"],
            "master_relative_path": repair["master_relative_path"],
            "access_derivative_status": "generated_full_resolution_jpeg_q95",
            "original_source_path": repair["source"],
        })
        access = dict(master)
        access.update({
            "file_role": "access_derivative",
            "final_relative_path": repair["access_relative_path"],
            "filename": Path(repair["access_relative_path"]).name,
            "file_type": "jpg",
            "size_bytes": str(repair["access_size_bytes"]),
            "final_sha256": repair["access_sha256"],
        })
        new_catalog.extend((master, access))

    existing_repairs = set(repair_by_record)
    new_representations: list[dict[str, str]] = []
    for row in representations:
        repair = repair_by_record.get(row["record_keys"])
        if repair is None:
            new_representations.append(row)
            continue
        if row["role"] != "preservation_master":
            raise metadata.IntegrityError(f"gap representation drifted: {row['record_keys']}")
        master = dict(row)
        master.update({
            "completed_at": metadata.now(),
            "representation_key": repair["master_sha256"],
            "source_kind": "file",
            "source_path": repair["source"],
            "source_member": "",
            "source_sha256": repair["source_sha256"],
            "final_sha256": repair["master_sha256"],
            "final_relative_path": repair["master_relative_path"],
            "size_bytes": str(repair["master_size_bytes"]),
            "mtime_ns": str(repair["master_mtime_ns"]),
            "copy_policy": "verified_metadata_rewrite_atomic_replace",
            "basis": "curator_confirmed_unreadable_master_replacement",
            "relationship": "approved_master_replacement",
        })
        access = dict(master)
        access.update({
            "role": "access_derivative",
            "representation_key": repair["access_sha256"],
            "source_kind": "generated_from_master",
            "source_member": repair["master_relative_path"],
            "source_sha256": repair["master_sha256"],
            "final_sha256": repair["access_sha256"],
            "final_relative_path": repair["access_relative_path"],
            "size_bytes": str(repair["access_size_bytes"]),
            "mtime_ns": str(repair["access_mtime_ns"]),
            "copy_policy": "generated_full_resolution_jpeg_q95",
            "basis": "libvips_full_native_dimensions_metadata_embedded",
            "relationship": f"access_derivative_of:{repair['master_relative_path']}",
        })
        new_representations.extend((master, access))
        existing_repairs.remove(row["record_keys"])
    if existing_repairs:
        raise metadata.IntegrityError(f"missing gap representations: {sorted(existing_repairs)}")
    for index, row in enumerate(new_representations, 1):
        row["index"] = str(index)

    new_checksums: list[dict[str, str]] = []
    old_paths = {row["old_master_relative_path"]: row for row in staged}
    for row in checksums:
        repair = old_paths.get(row["path"])
        if repair is None:
            new_checksums.append(row)
            continue
        new_checksums.extend((
            {"path": repair["master_relative_path"], "sha256": repair["master_sha256"], "size_bytes": str(repair["master_size_bytes"]), "content_role": "preservation_master"},
            {"path": repair["access_relative_path"], "sha256": repair["access_sha256"], "size_bytes": str(repair["access_size_bytes"]), "content_role": "access_derivative"},
        ))

    for repair in staged:
        old_matches = [row for row in source_rows if row["source_path"] == repair["old_source_path"] and row["source_role"] == "selected_master"]
        new_matches = [row for row in source_rows if row["source_path"] == repair["source"]]
        if len(old_matches) != 1 or len(new_matches) != 1:
            raise metadata.IntegrityError(f"source disposition binding drifted: {repair['inventory']}")
        old = old_matches[0]
        old.update({
            "source_role": "redundant",
            "final_path": repair["master_relative_path"],
            "final_sha256": repair["master_sha256"],
            "preservation_status": "retained_redundant_damaged_tiff_source",
            "preservation_basis": "curator_confirmed_unreadable_master_replacement",
            "exact_copy_proven": "false",
            "drain_state": "reviewed_redundant_not_accessioned",
            "deletion_authorized": "false",
            "notes": "Damaged TIFF retained on read-only source and in preserved predecessor evidence; approved alternate selected as Master. No deletion is authorized.",
        })
        selected = new_matches[0]
        selected.update({
            "inventory": repair["inventory"],
            "source_role": "selected_master",
            "final_path": repair["master_relative_path"],
            "final_sha256": repair["master_sha256"],
            "preservation_status": "preserved_metadata_enriched_master",
            "preservation_basis": "curator_confirmed_unreadable_master_replacement",
            "exact_copy_proven": "false",
            "drain_state": "retained_selected_master",
            "deletion_authorized": "false",
            "notes": "Approved decodable alternate accessioned as the single Master with reconciled embedded metadata. No deletion is authorized.",
        })
        drain_rows.append(dict(old))
        group_id = repair["record_key"]
        common_note = "Approved unreadable-Master correction; retain all source evidence and keep deletion unauthorized."
        decision_rows.extend((
            {
                "source_path": repair["old_source_path"], "group_id": group_id,
                "disposition": "redundant", "master_path": repair["source"],
                "note": common_note, "actor": repair["decision"]["old"]["actor"],
                "decided_at": repair["decision"]["old"]["decided_at"], "deletion_authorized": "false",
            },
            {
                "source_path": repair["source"], "group_id": group_id,
                "disposition": "selected_master", "master_path": repair["source"],
                "note": common_note, "actor": repair["decision"]["new"]["actor"],
                "decided_at": repair["decision"]["new"]["decided_at"], "deletion_authorized": "false",
            },
        ))

    if len(new_catalog) != 3330 or Counter(row["file_role"] for row in new_catalog) != Counter({"preservation_master": 1665, "access_derivative": 1665}):
        raise metadata.IntegrityError("catalog replacement counts drifted")
    if len(new_representations) != 3330 or len(new_checksums) != 3330:
        raise metadata.IntegrityError("representation/checksum replacement counts drifted")
    if len(source_rows) != 17077 or len(drain_rows) != 2347 or len(decision_rows) != 2382:
        raise metadata.IntegrityError("human-readable decision counts drifted")
    if any(row["deletion_authorized"] != "false" for row in source_rows + drain_rows + decision_rows):
        raise metadata.IntegrityError("deletion authorization detected")

    old_db = sqlite3.connect(f"file:{UNIT / 'db-restore/RESTORE.sqlite'}?mode=ro", uri=True)
    old_db.row_factory = sqlite3.Row
    try:
        table_names = [row[0] for row in old_db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        tables = {name: metadata.table_rows(old_db, name) for name in table_names}
    finally:
        old_db.close()
    tables["representations"] = (representation_fields, new_representations)
    tables["checksums"] = (checksum_fields, new_checksums)
    tables["source_disposition"] = (source_fields, source_rows)
    tables["drain_list"] = (drain_fields, drain_rows)
    tables["reconciliation_decisions"] = (decision_fields, decision_rows)
    tables["access_derivative_gaps"] = (gap_fields, [])
    object_fields, objects = tables["objects"]
    for row in objects:
        repair = repair_by_record.get(row["record_key"])
        if repair:
            row.update({
                "selected_source_path": repair["source"],
                "selected_source_format": Path(repair["source"]).suffix.lstrip("."),
                "selection_basis": "curator_confirmed_unreadable_master_replacement",
                "selection_state": "resolved",
                "object_metadata_state": "reconciled",
                "master_relative_path": repair["master_relative_path"],
                "master_sha256": repair["master_sha256"],
                "access_relative_path": repair["access_relative_path"],
                "access_derivative_status": "generated_full_resolution_jpeg_q95",
            })
    tables["objects"] = (object_fields, objects)

    build_db = RUN / f"RESTORE.gap-repair-building-{os.getpid()}.sqlite"
    if build_db.exists():
        raise metadata.IntegrityError(f"database build path exists: {build_db}")
    connection = sqlite3.connect(build_db)
    try:
        for name in table_names:
            fields, rows = tables[name]
            metadata.create_table(connection, name, fields, rows)
        connection.commit()
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise metadata.IntegrityError("rebuilt RESTORE.sqlite integrity check failed")
        sql = "\n".join(connection.iterdump()) + "\n"
    finally:
        connection.close()

    import paragon_document_bundle
    catalog_xlsx = paragon_document_bundle.table_xlsx_bytes(catalog_fields, new_catalog, sheet_name="Catalog", table_name="Catalog")
    restore_xlsx = paragon_document_bundle.table_xlsx_bytes(catalog_fields, new_catalog, sheet_name="Restore", table_name="Restore")
    readme_path = UNIT / "00_README.md"
    readme = readme_path.read_text(encoding="utf-8")
    readme = readme.replace("- Generated access derivatives: 1657", "- Generated access derivatives: 1665")
    readme = readme.replace("- Explicit derivative gaps: 8", "- Explicit derivative gaps: 0")
    readme = readme.replace(
        "Eight Masters remain unchanged without an access derivative; see\n`db-restore/manifests/access-derivative-gaps.csv`.",
        "All 1,665 selected Masters have a generated full-resolution access derivative.\nThe eight damaged TIFF candidates remain protected redundant source evidence.",
    )
    readme = readme.replace(
        "the documented decode-gap Masters remain unchanged with metadata correction deferred.",
        "the eight approved replacements resolve the former damaged-TIFF decode gaps.",
    )
    updates = {
        UNIT / "CATALOG.csv": metadata.csv_bytes(catalog_fields, new_catalog),
        UNIT / "CATALOG.xlsx": catalog_xlsx,
        UNIT / "db-restore/RESTORE.csv": metadata.csv_bytes(catalog_fields, new_catalog),
        UNIT / "db-restore/RESTORE.xlsx": restore_xlsx,
        UNIT / "db-restore/checksums.csv": metadata.csv_bytes(checksum_fields, sorted(new_checksums, key=lambda row: row["path"])),
        UNIT / "db-restore/manifests/representations.csv": metadata.csv_bytes(representation_fields, new_representations),
        UNIT / "db-restore/manifests/source-disposition.csv": metadata.csv_bytes(source_fields, source_rows),
        UNIT / "db-restore/manifests/drain-list.csv": metadata.csv_bytes(drain_fields, drain_rows),
        UNIT / "db-restore/manifests/reconciliation-decisions.csv": metadata.csv_bytes(decision_fields, decision_rows),
        UNIT / "db-restore/manifests/access-derivative-gaps.csv": metadata.csv_bytes(gap_fields, []),
        UNIT / "db-restore/RESTORE.sql": sql.encode("utf-8"),
        readme_path: readme.encode("utf-8"),
    }
    return updates, build_db, objects


def publish(staged: list[dict], updates: dict[Path, bytes], build_db: Path) -> None:
    source_mount()
    for repair in staged:
        target_master = UNIT / repair["master_relative_path"]
        target_access = UNIT / repair["access_relative_path"]
        old_master = UNIT / repair["old_master_relative_path"]
        if target_master != old_master and target_master.exists():
            raise metadata.IntegrityError(f"replacement target already exists: {target_master}")
        if target_access.exists():
            raise metadata.IntegrityError(f"replacement derivative already exists: {target_access}")
        os.replace(repair["master_candidate_path"], target_master)
        metadata.fsync_dir(target_master.parent)
        os.replace(repair["access_candidate_path"], target_access)
        metadata.fsync_dir(target_access.parent)
        repair["master_inode"] = target_master.stat().st_ino
        repair["master_mtime_ns"] = target_master.stat().st_mtime_ns
        repair["access_inode"] = target_access.stat().st_ino
        repair["access_mtime_ns"] = target_access.stat().st_mtime_ns
    for path, payload in updates.items():
        metadata.atomic_replace_bytes(path, payload)
    os.replace(build_db, UNIT / "db-restore/RESTORE.sqlite")
    metadata.fsync_dir(UNIT / "db-restore")
    for repair in staged:
        old_master = UNIT / repair["old_master_relative_path"]
        new_master = UNIT / repair["master_relative_path"]
        if old_master != new_master:
            if not old_master.is_file() or old_master.parent.resolve() != (UNIT / "masters").resolve():
                raise metadata.IntegrityError(f"old damaged Master path drifted: {old_master}")
            old_master.unlink()
            metadata.fsync_dir(old_master.parent)
        receipt = {
            key: value for key, value in repair.items()
            if key not in {"master_candidate_path", "access_candidate_path", "decision"}
        }
        receipt.update({
            "format": "iss-kurt-schwarz-metadata-derivative-receipt-v1",
            "completed_at": metadata.now(),
            "metadata_profile": metadata.PROFILE,
            "published": True,
        })
        metadata.atomic_json(RECEIPTS / f"{repair['inventory']}.json", receipt)


def validate(staged: list[dict]) -> dict:
    catalog_fields, catalog = read_csv(UNIT / "CATALOG.csv")
    _, checksums = read_csv(UNIT / "db-restore/checksums.csv")
    _, gaps = read_csv(UNIT / "db-restore/manifests/access-derivative-gaps.csv")
    if len(catalog) != 3330 or Counter(row["file_role"] for row in catalog) != Counter({"preservation_master": 1665, "access_derivative": 1665}):
        raise metadata.IntegrityError("published catalog validation failed")
    if gaps:
        raise metadata.IntegrityError("published derivative gaps remain")
    missing = []
    mismatched = []
    for row in checksums:
        path = UNIT / row["path"]
        if not path.is_file():
            missing.append(row["path"])
        elif path.stat().st_size != int(row["size_bytes"]):
            mismatched.append(row["path"])
    if missing or mismatched or len(checksums) != 3330:
        raise metadata.IntegrityError(f"published checksum path validation failed: missing={missing} mismatched={mismatched}")
    connection = sqlite3.connect(f"file:{UNIT / 'db-restore/RESTORE.sqlite'}?mode=ro", uri=True)
    try:
        quick = connection.execute("PRAGMA quick_check").fetchone()[0]
        foreign = len(connection.execute("PRAGMA foreign_key_check").fetchall())
        counts = {name: connection.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0] for name in (
            "objects", "representations", "checksums", "access_derivative_gaps",
            "source_disposition", "drain_list", "reconciliation_decisions",
        )}
    finally:
        connection.close()
    expected = {
        "objects": 1665, "representations": 3330, "checksums": 3330,
        "access_derivative_gaps": 0, "source_disposition": 17077,
        "drain_list": 2347, "reconciliation_decisions": 2382,
    }
    if quick != "ok" or foreign or counts != expected:
        raise metadata.IntegrityError(f"published SQLite validation failed: quick={quick} foreign={foreign} counts={counts}")
    for repair in staged:
        for path_key, hash_key in (("master_relative_path", "master_sha256"), ("access_relative_path", "access_sha256")):
            if metadata.sha256_once(UNIT / repair[path_key]) != repair[hash_key]:
                raise metadata.IntegrityError(f"published repair hash mismatch: {repair[path_key]}")
    mount = source_mount()
    return {"sqlite_quick_check": quick, "sqlite_foreign_key_violations": foreign, "sqlite_counts": counts, "source_mount": mount}


def main() -> int:
    if REPAIR_RECEIPT.exists():
        raise SystemExit(f"repair receipt already exists: {REPAIR_RECEIPT}")
    if UNIT.stat().st_dev != WORK.stat().st_dev:
        raise metadata.IntegrityError("archive and work must share a filesystem for atomic publication")
    initial_mount = source_mount()
    catalog_fields, catalog = read_csv(UNIT / "CATALOG.csv")
    masters = {row["record_key"]: row for row in catalog if row["file_role"] == "preservation_master"}
    if len(masters) != 1665 or not set(row["record_key"] for row in REPAIRS).issubset(masters):
        raise metadata.IntegrityError("published Master scope drifted")
    decisions = validate_decisions()
    staged = stage(masters, decisions)
    updates, build_db, _ = prepare_metadata(staged)
    publish(staged, updates, build_db)
    validation = validate(staged)
    metadata.atomic_replace_bytes(RUN / "errors.jsonl", b"")
    completion = {
        "format": "iss-kurt-schwarz-metadata-derivatives-completion-v1",
        "completed_at": metadata.now(), "result": "ok", "metadata_profile": metadata.PROFILE,
        "objects_total": 1665, "objects_updated": 1665, "noncritical_errors": 0,
        "data_integrity_suspected": False, "normalization_deferred": False,
        "final_hash_policy": "one SHA-256 per finalized replacement Master and derivative candidate",
        "deletion_authorized": False, "source_or_predecessor_mutated": False,
        "sqlite_quick_check": "ok",
    }
    metadata.atomic_json(RUN / "completion.json", completion)
    metadata.atomic_json(RUN / "status.json", {**completion, "state": "complete"})
    receipt = {
        "format": "iss-kurt-schwarz-unreadable-master-replacement-v1",
        "completed_at": metadata.now(), "result": "ok", "objects_repaired": len(staged),
        "masters": 1665, "access_derivatives": 1665, "access_derivative_gaps": 0,
        "representations": 3330, "deletion_authorized": False,
        "source_or_predecessor_mutated": False, "source_mount_before": initial_mount,
        "source_mount_after": validation["source_mount"], "validation": validation,
        "objects": [
            {key: row[key] for key in (
                "record_key", "inventory", "old_master_relative_path", "old_master_sha256",
                "source", "source_sha256", "master_relative_path", "master_sha256",
                "access_relative_path", "access_sha256",
            )} for row in staged
        ],
    }
    metadata.atomic_json(REPAIR_RECEIPT, receipt)
    print(json.dumps(receipt, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
