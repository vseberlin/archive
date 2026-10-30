#!/usr/bin/env python3
"""Remove mis-scoped documentation from the published Objektsammlung unit.

This is an exact, no-USB-write correction.  It removes only the document
copies and document claims already enumerated by the reviewed correction CSV.
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path


SOURCE_ROOT = Path("/media/vladimir/Elements")
UNIT = Path("/mnt/volume/iss-archive/units/objektsammlung")
RECON = Path(
    "/mnt/volume/iss-archive-work/collection-549/work/reconciliation/"
    "unit-reconciliation-v1"
)
PROPOSAL = RECON / "current-unit-document-correction-proposal.csv"
DB_BACKUP = RECON / "pre-no-documents-correction-RESTORE.sqlite"


class CorrectionError(RuntimeError):
    pass


def now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        return list(reader.fieldnames or []), list(reader)


def atomic_csv(path: Path, fields: list[str], rows: list[dict[str, object]]) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("x", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=fields,
                extrasaction="ignore",
                quoting=csv.QUOTE_ALL,
            )
            writer.writeheader()
            writer.writerows(rows)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_text(path: Path, text: str) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(8 * 1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def guarded_mount() -> dict[str, str]:
    result = subprocess.run(
        ["findmnt", "-J", "-T", str(SOURCE_ROOT), "-o", "TARGET,SOURCE,FSTYPE,OPTIONS"],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    rows = json.loads(result.stdout).get("filesystems") or []
    if len(rows) != 1:
        raise CorrectionError("Elements did not resolve to one filesystem")
    row = rows[0]
    options = set(str(row.get("options", "")).split(","))
    if (
        row.get("target") != str(SOURCE_ROOT)
        or row.get("source") != "/dev/sdf1"
        or row.get("fstype") != "ntfs3"
        or "ro" not in options
    ):
        raise CorrectionError(f"protected Elements mount guard failed: {row}")
    return {key: str(row.get(key, "")) for key in ("target", "source", "fstype", "options")}


def main() -> int:
    arguments = sys.argv[1:]
    if arguments not in ([], ["--check"]):
        raise CorrectionError("usage: collection_549_remove_documents.py [--check]")
    check_only = arguments == ["--check"]
    corrected_at = now()
    mount_before = guarded_mount()
    documents_dir = UNIT / "documents"
    if not documents_dir.is_dir():
        raise CorrectionError("published unit documents directory is missing")

    document_fields, document_rows = read_csv(UNIT / "00_MANIFESTS/documents.csv")
    proposal_fields, proposal_rows = read_csv(PROPOSAL)
    if len(document_rows) != 125 or len(proposal_rows) != 125:
        raise CorrectionError("reviewed correction is not exactly 125 documents")
    if any(
        row["proposed_unit_action"] != "remove_from_objektsammlung_documents"
        or row["source_action"] != "retain_unchanged"
        or row["implemented"] != "false"
        or row["deletion_authorized"] != "false"
        for row in proposal_rows
    ):
        raise CorrectionError("document correction proposal is not the approved no-source-delete set")

    documents_by_id = {row["document_id"]: row for row in document_rows}
    proposal_by_id = {row["document_id"]: row for row in proposal_rows}
    if documents_by_id.keys() != proposal_by_id.keys():
        raise CorrectionError("unit documents and reviewed correction IDs disagree")

    unit_document_paths: set[str] = set()
    for document_id, row in documents_by_id.items():
        proposal = proposal_by_id[document_id]
        relative = row["relative_path"]
        path = UNIT / relative
        if Path(relative).parent != Path("documents") or path.parent != documents_dir:
            raise CorrectionError(f"unsafe document target: {relative}")
        if proposal["unit_relative_path"] != relative or proposal["sha256"] != row["sha256"]:
            raise CorrectionError(f"reviewed correction binding mismatch: {document_id}")
        if not path.is_file() or path.stat().st_size != int(row["size_bytes"]):
            raise CorrectionError(f"unit document missing or changed: {relative}")
        if sha256(path) != row["sha256"]:
            raise CorrectionError(f"unit document checksum changed: {relative}")
        unit_document_paths.add(relative)

    physical_documents = {str(path.relative_to(UNIT)) for path in documents_dir.iterdir()}
    if physical_documents != unit_document_paths:
        raise CorrectionError("documents directory contains entries outside the reviewed correction")

    expected_metadata = {
        "metadata/metadata-decisions.csv",
        "metadata/museum-digital-objects.csv",
        "metadata/museum-digital-resources.csv",
    }
    physical_metadata = {
        str(path.relative_to(UNIT)) for path in (UNIT / "metadata").iterdir()
    }
    if physical_metadata != expected_metadata:
        raise CorrectionError(
            f"metadata supporting evidence is not the exact approved set: {sorted(physical_metadata)}"
        )

    representation_fields, representation_rows = read_csv(
        UNIT / "00_MANIFESTS/representations.csv"
    )
    if any(row["relative_path"].startswith("documents/") for row in representation_rows):
        raise CorrectionError("document paths unexpectedly occur in representations.csv")

    payload_fields, payload_rows = read_csv(UNIT / "00_MANIFESTS/payload-checksums.csv")
    payload_by_path = {row["relative_path"]: row for row in payload_rows}
    if set(payload_by_path).intersection(unit_document_paths) != unit_document_paths:
        raise CorrectionError("payload checksum manifest does not cover every reviewed document")
    retained_payload_rows = [
        row for row in payload_rows if row["relative_path"] not in unit_document_paths
    ]
    if len(retained_payload_rows) != 65:
        raise CorrectionError("corrected payload is not exactly 65 files")

    disposition_fields, disposition_rows = read_csv(
        UNIT / "00_MANIFESTS/source-disposition.csv"
    )
    drain_fields, drain_rows = read_csv(UNIT / "00_MANIFESTS/drain-list.csv")
    work_drain_fields, work_drain_rows = read_csv(RECON / "drain-list.csv")
    if disposition_rows != drain_rows or disposition_rows != work_drain_rows:
        raise CorrectionError("published and work-root source/drain rows disagree before correction")
    removed_source_rows = [
        row for row in disposition_rows if row["relationship"] == "related_local_documentation"
    ]
    retained_source_rows = [
        row for row in disposition_rows if row["relationship"] != "related_local_documentation"
    ]
    if len(removed_source_rows) != 168 or len(retained_source_rows) != 43:
        raise CorrectionError("document source/drain correction is not exactly 168 removed and 43 retained")
    if any(row["deletion_authorized"] != "false" for row in disposition_rows):
        raise CorrectionError("a source/drain row unexpectedly authorizes deletion")

    source_snapshot: dict[str, tuple[int, int]] = {}
    for row in removed_source_rows:
        path = Path(row["source_path"])
        stat = path.stat()
        if stat.st_size != int(row["size_bytes"]):
            raise CorrectionError(f"protected USB source size changed: {path}")
        source_snapshot[str(path)] = (stat.st_size, stat.st_mtime_ns)

    for row in retained_payload_rows:
        path = UNIT / row["relative_path"]
        if not path.is_file() or path.stat().st_size != int(row["size_bytes"]):
            raise CorrectionError(f"retained payload missing or changed: {row['relative_path']}")
        if sha256(path) != row["sha256"]:
            raise CorrectionError(f"retained payload checksum mismatch: {row['relative_path']}")

    database = UNIT / "db-restore/RESTORE.sqlite"
    if DB_BACKUP.exists():
        raise CorrectionError(f"refusing to replace existing SQLite backup: {DB_BACKUP}")

    current_connection = sqlite3.connect(f"file:{database}?mode=ro&immutable=1", uri=True)
    current_quick_check = current_connection.execute("PRAGMA quick_check").fetchone()[0]
    current_foreign_keys = current_connection.execute("PRAGMA foreign_key_check").fetchall()
    current_counts = {
        table: current_connection.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]
        for table in ("objects", "representations", "documents", "source_disposition")
    }
    current_connection.close()
    if current_quick_check != "ok" or current_foreign_keys:
        raise CorrectionError("current SQLite validation failed")
    if current_counts != {
        "objects": 10,
        "representations": 62,
        "documents": 125,
        "source_disposition": 211,
    }:
        raise CorrectionError(f"current SQLite counts are wrong: {current_counts}")

    if check_only:
        mount_after = guarded_mount()
        if mount_after != mount_before:
            raise CorrectionError("protected USB mount changed during preflight")
        for source, before in source_snapshot.items():
            stat = Path(source).stat()
            if (stat.st_size, stat.st_mtime_ns) != before:
                raise CorrectionError(f"protected USB source changed during preflight: {source}")
        print(
            json.dumps(
                {
                    "result": "ok",
                    "mode": "check-only",
                    "unit_document_copies_to_remove": len(document_rows),
                    "document_source_claims_to_remove": len(removed_source_rows),
                    "retained_payload_files": len(retained_payload_rows),
                    "retained_source_disposition_rows": len(retained_source_rows),
                    "metadata_supporting_evidence_files": sorted(expected_metadata),
                    "sqlite_counts_before": current_counts,
                    "source_mount": mount_after,
                    "source_files_modified": 0,
                    "source_files_deleted": 0,
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
        return 0

    shutil.copy2(database, DB_BACKUP)

    temporary_db = database.with_name(".RESTORE.sqlite.no-documents.tmp")
    temporary_db.unlink(missing_ok=True)
    shutil.copy2(database, temporary_db)
    connection = sqlite3.connect(temporary_db)
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("DELETE FROM documents")
    connection.execute(
        "DELETE FROM source_disposition WHERE relationship='related_local_documentation'"
    )
    connection.commit()
    if connection.execute("SELECT count(*) FROM documents").fetchone()[0] != 0:
        raise CorrectionError("SQLite document rows remain")
    if connection.execute("SELECT count(*) FROM source_disposition").fetchone()[0] != 43:
        raise CorrectionError("SQLite source disposition is not exactly 43 rows")
    quick_check = connection.execute("PRAGMA quick_check").fetchone()[0]
    foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
    dump = "\n".join(connection.iterdump()) + "\n"
    connection.close()
    if quick_check != "ok" or foreign_keys:
        raise CorrectionError(
            f"corrected SQLite validation failed: quick_check={quick_check}, foreign_keys={foreign_keys}"
        )

    readme = (
        "# Objektsammlung\n\n"
        "This unit is the human-readable archival authority for museum-digital collection 549. "
        "Museum-digital is retained as an external projection and provenance source.\n\n"
        "Each of the ten objects has exactly one selected Master in `masters/`. Access derivatives "
        "generated during accession and retained related resources are in `export/`. No local "
        "documentation is accessioned in this unit. Museum-digital-derived metadata and review "
        "decisions in `metadata/` are supporting evidence. Essential decisions are in `CATALOG.csv` "
        "and `00_MANIFESTS/`; SQLite is rebuildable catalogue support only.\n\n"
        "Source paths remain read-only. Drain rows cover only retained Master/image traces, are "
        "proposals, and carry `deletion_authorized=false`.\n"
    )

    old_completion = json.loads(
        (UNIT / "00_MANIFESTS/accession-completion.json").read_text(encoding="utf-8")
    )
    completion = dict(old_completion)
    completion.update(
        {
            "completed_at": corrected_at,
            "correction_applied_at": corrected_at,
            "correction_scope": "remove non-Objektsammlung document copies and claims",
            "retained_unique_documents": 0,
            "source_disposition_rows": len(retained_source_rows),
            "payload_files": len(retained_payload_rows),
            "payload_bytes": sum(int(row["size_bytes"]) for row in retained_payload_rows),
            "unit_document_copies_removed": len(document_rows),
            "document_source_claims_removed": len(removed_source_rows),
            "source_files_modified": 0,
            "source_files_deleted": 0,
            "deletion_authorized": False,
            "result": "ok",
        }
    )

    os.replace(temporary_db, database)
    atomic_text(UNIT / "db-restore/RESTORE.sql", dump)
    atomic_csv(UNIT / "00_MANIFESTS/documents.csv", document_fields, [])
    atomic_csv(
        UNIT / "00_MANIFESTS/source-disposition.csv",
        disposition_fields,
        retained_source_rows,
    )
    atomic_csv(UNIT / "00_MANIFESTS/drain-list.csv", drain_fields, retained_source_rows)
    atomic_csv(
        UNIT / "00_MANIFESTS/payload-checksums.csv",
        payload_fields,
        retained_payload_rows,
    )
    atomic_text(UNIT / "00_README.md", readme)

    for relative in sorted(unit_document_paths):
        (UNIT / relative).unlink()

    corrected_proposal_rows = [dict(row, implemented="true") for row in proposal_rows]
    atomic_csv(RECON / "drain-list.csv", work_drain_fields, retained_source_rows)
    atomic_csv(PROPOSAL, proposal_fields, corrected_proposal_rows)

    if any(documents_dir.iterdir()):
        raise CorrectionError("documents directory is not empty after correction")
    actual_payload = {
        str(path.relative_to(UNIT))
        for directory in ("masters", "export", "metadata", "documents")
        for path in (UNIT / directory).iterdir()
        if path.is_file()
    }
    if actual_payload != {row["relative_path"] for row in retained_payload_rows}:
        raise CorrectionError("final payload census disagrees with corrected checksum manifest")

    final_connection = sqlite3.connect(f"file:{database}?mode=ro&immutable=1", uri=True)
    final_quick_check = final_connection.execute("PRAGMA quick_check").fetchone()[0]
    final_foreign_keys = final_connection.execute("PRAGMA foreign_key_check").fetchall()
    final_counts = {
        table: final_connection.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]
        for table in ("objects", "representations", "documents", "source_disposition")
    }
    final_connection.close()
    if final_quick_check != "ok" or final_foreign_keys:
        raise CorrectionError("final SQLite verification failed")
    if final_counts != {
        "objects": 10,
        "representations": 62,
        "documents": 0,
        "source_disposition": 43,
    }:
        raise CorrectionError(f"final SQLite counts are wrong: {final_counts}")

    mount_after = guarded_mount()
    if mount_after != mount_before:
        raise CorrectionError("protected USB mount changed during correction")
    for source, before in source_snapshot.items():
        stat = Path(source).stat()
        if (stat.st_size, stat.st_mtime_ns) != before:
            raise CorrectionError(f"protected USB source changed during correction: {source}")

    atomic_text(
        UNIT / "00_MANIFESTS/accession-completion.json",
        json.dumps(completion, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )
    print(json.dumps({**completion, "sqlite_counts": final_counts}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
