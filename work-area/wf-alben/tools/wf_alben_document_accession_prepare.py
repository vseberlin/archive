#!/usr/bin/env python3
"""Prepare the WF-Alben documentation and reconciliation records for accession."""

from __future__ import annotations

import csv
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path


WORK_ROOT = Path("/mnt/volume/iss-archive-work/wf-alben")
RECON = WORK_ROOT / "work/reconciliation"
UNIT = Path("/mnt/volume/iss-archive/units/wf-alben")
DOCUMENTS = UNIT / "documents"
METADATA = UNIT / "metadata"
MANIFESTS = UNIT / "00_MANIFESTS"
SOURCE_ROOT = Path("/media/vladimir/Elements/restore/archive")
CANDIDATES_CSV = RECON / "document-candidates.csv"
DB = RECON / "workbench.sqlite"

METADATA_FILES = (
    "archive-index-comparison.csv",
    "catalogue-surrogate-resolution.json",
    "catalogue-surrogates.csv",
    "collection-records.csv",
    "derived-image-relationships.csv",
    "document-candidates.csv",
    "linked-derivative-correction.json",
    "linked-object-relationships.csv",
    "metadata-crosswalk.csv",
    "metadata-editorial-decisions.csv",
    "metadata-editorial-resolution.json",
    "summary.json",
    "workbench-candidate-dispositions.csv",
    "workbench-decisions.csv",
    "workbench-preflight.json",
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_copy(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        if target.stat().st_size == source.stat().st_size and sha256(target) == sha256(source):
            return
        raise RuntimeError(f"refusing to replace differing output: {target}")
    fd, temporary_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    os.close(fd)
    temporary = Path(temporary_name)
    try:
        shutil.copy2(source, temporary)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_write_text(target: Path, text: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_write_csv(target: Path, fieldnames: list[str], rows: list[dict[str, object]]) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def assert_source_mount() -> str:
    result = subprocess.run(
        ["findmnt", "-T", str(SOURCE_ROOT), "-no", "SOURCE,FSTYPE,OPTIONS"],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    mount = result.stdout.strip()
    parts = mount.split(maxsplit=2)
    if len(parts) != 3 or parts[0] != "/dev/sdf1" or parts[1] != "ntfs3":
        raise RuntimeError(f"unexpected source mount: {mount}")
    if "ro" not in parts[2].split(","):
        raise RuntimeError(f"source mount is not read-only: {mount}")
    return mount


def classify(row: dict[str, str]) -> tuple[str, str]:
    role = row["document_role"]
    path = row["source_path"]
    if role == "album_working_table":
        return "include", "direct WF-Alben working table or caption export"
    if role == "book_project_context":
        return "include", "caption sheet with direct WF-Alben object coverage"
    if role == "thesaurus_context":
        if "/Thesaurus/11_1960-1962-Annette_Ausstellung_ALB_KB/" in path:
            return "include", "direct ALB exhibition metadata and reconciliation evidence"
        if "/Thesaurus/1960-1962-Annette_Ausstellung_ALB_KB/" in path:
            return "include", "exact-copy location for direct ALB exhibition evidence"
        if "/Thesaurus/15_NEF-Bilder_1954/" in path and not path.lower().endswith(".zip"):
            return "include", "direct NEF metadata and upload working table"
        if path.lower().endswith(".zip"):
            return "exclude_context", "transfer bundle is not a documentation file"
        return "exclude_context", "collection-wide thesaurus export; retained at source and referenced only"
    if path.endswith("/Album_NEF.pdf"):
        return "include", "direct album scan; extension is PDF but detected content is JPEG"
    if role == "website_backup_context":
        return "exclude_context", "whole-site backup; not unit documentation and retained at source"
    return "exclude_false_positive", "application configuration file; filename match is unrelated"


def choose_representative(rows: list[dict[str, str]]) -> dict[str, str]:
    def preference(row: dict[str, str]) -> tuple[int, str]:
        path = row["source_path"]
        if "/WF/Buchprojekt/" in path and "/Archiv/" not in path:
            return (0, path)
        if "/Thesaurus/11_1960-1962-" in path:
            return (0, path)
        return (1, path)

    return sorted(rows, key=preference)[0]


def workbench_counts() -> dict[str, int]:
    connection = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    try:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise RuntimeError(f"workbench SQLite integrity check failed: {integrity}")
        return {
            "catalogue_records": connection.execute("SELECT count(*) FROM objects").fetchone()[0],
            "selected_masters": connection.execute(
                "SELECT count(*) FROM decisions WHERE decision_state='selected_master'"
            ).fetchone()[0],
            "redundant_derivative_records": connection.execute(
                "SELECT count(*) FROM decisions WHERE decision_state='redundant_derivative'"
            ).fetchone()[0],
            "redundant_candidates": connection.execute(
                "SELECT count(*) FROM candidate_dispositions WHERE disposition='redundant'"
            ).fetchone()[0],
            "unresolved_master_decisions": connection.execute(
                "SELECT count(*) FROM decisions WHERE decision_state='unresolved'"
            ).fetchone()[0],
            "unresolved_metadata_decisions": connection.execute(
                "SELECT count(*) FROM metadata_editorial_decisions WHERE decision_state='unresolved'"
            ).fetchone()[0],
            "deletion_authorized": connection.execute(
                "SELECT coalesce(sum(deletion_authorized),0) FROM candidate_dispositions"
            ).fetchone()[0],
        }
    finally:
        connection.close()


def main() -> None:
    mount = assert_source_mount()
    counts = workbench_counts()
    if counts["unresolved_master_decisions"] or counts["unresolved_metadata_decisions"]:
        raise RuntimeError(f"workbench still has unresolved decisions: {counts}")
    if counts["deletion_authorized"]:
        raise RuntimeError("a candidate unexpectedly has deletion authorization")

    with CANDIDATES_CSV.open(encoding="utf-8-sig", newline="") as handle:
        candidates = list(csv.DictReader(handle))
    if len(candidates) != 64:
        raise RuntimeError(f"expected 64 indexed document candidates, found {len(candidates)}")

    for row in candidates:
        source = Path(row["source_path"])
        if not source.is_relative_to(SOURCE_ROOT):
            raise RuntimeError(f"candidate escapes protected source root: {source}")
        stat = source.stat()
        if stat.st_size != int(row["size_bytes"]):
            raise RuntimeError(f"source size changed since indexing: {source}")
        row["classification"], row["classification_reason"] = classify(row)

    included_by_hash: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in candidates:
        if row["classification"] == "include":
            included_by_hash[row["content_sha256"]].append(row)

    representatives = {
        content_hash: choose_representative(rows)
        for content_hash, rows in included_by_hash.items()
    }
    if len(representatives) != 31:
        raise RuntimeError(f"expected 31 unique documentation files, found {len(representatives)}")

    DOCUMENTS.mkdir(parents=True, exist_ok=True)
    METADATA.mkdir(parents=True, exist_ok=True)
    MANIFESTS.mkdir(parents=True, exist_ok=True)

    copied_documents: dict[str, str] = {}
    checksum_rows: list[dict[str, object]] = []
    for content_hash, row in sorted(representatives.items(), key=lambda item: item[1]["source_path"]):
        source = Path(row["source_path"])
        actual_source_hash = sha256(source)
        if actual_source_hash != content_hash:
            raise RuntimeError(f"source hash changed since indexing: {source}")
        output_name = f'{row["document_id"]}__{source.name}'
        target = DOCUMENTS / output_name
        atomic_copy(source, target)
        final_hash = sha256(target)
        if final_hash != content_hash:
            raise RuntimeError(f"copied document hash mismatch: {target}")
        relative_output = str(target.relative_to(UNIT))
        copied_documents[content_hash] = relative_output
        checksum_rows.append(
            {
                "relative_path": relative_output,
                "size_bytes": target.stat().st_size,
                "sha256": final_hash,
                "payload_type": "source_document",
            }
        )

    for filename in METADATA_FILES:
        source = RECON / filename
        if not source.is_file():
            raise RuntimeError(f"missing current reconciliation record: {source}")
        target = METADATA / filename
        atomic_copy(source, target)
        checksum_rows.append(
            {
                "relative_path": str(target.relative_to(UNIT)),
                "size_bytes": target.stat().st_size,
                "sha256": sha256(target),
                "payload_type": "reconciliation_record",
            }
        )

    manifest_rows: list[dict[str, object]] = []
    for row in candidates:
        content_hash = row["content_sha256"]
        representative = representatives.get(content_hash)
        if row["classification"] == "include" and row is representative:
            disposition = "included_representative"
            reason = row["classification_reason"]
            exact_duplicate_of = ""
            accession_path = copied_documents[content_hash]
        elif row["classification"] == "include":
            disposition = "exact_duplicate_of_included_document"
            reason = "exact SHA-256 duplicate; occurrence retained at source and recorded here"
            exact_duplicate_of = representative["source_path"] if representative else ""
            accession_path = copied_documents.get(content_hash, "")
        else:
            disposition = row["classification"]
            reason = row["classification_reason"]
            exact_duplicate_of = ""
            accession_path = ""
        manifest_rows.append(
            {
                "document_id": row["document_id"],
                "source_path": row["source_path"],
                "filename": row["filename"],
                "document_role": row["document_role"],
                "size_bytes": row["size_bytes"],
                "content_sha256": content_hash,
                "inventory_coverage_count": row["inventory_coverage_count"],
                "disposition": disposition,
                "reason": reason,
                "exact_duplicate_of": exact_duplicate_of,
                "accession_path": accession_path,
                "source_preserved": "true",
                "deletion_authorized": "false",
            }
        )

    atomic_write_csv(
        MANIFESTS / "document-provenance.csv",
        list(manifest_rows[0]),
        manifest_rows,
    )
    atomic_write_csv(
        MANIFESTS / "payload-checksums.csv",
        ["relative_path", "size_bytes", "sha256", "payload_type"],
        sorted(checksum_rows, key=lambda row: str(row["relative_path"])),
    )

    generated_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    receipt = {
        "schema": "iss-wf-alben-document-accession-preparation-v1",
        "generated_at": generated_at,
        "state": "prepared_not_accessioned",
        "unit_root": str(UNIT),
        "source_root": str(SOURCE_ROOT),
        "source_mount": mount,
        "document_candidates": len(candidates),
        "included_unique_documents": len(representatives),
        "included_source_occurrences": sum(
            1 for row in candidates if row["classification"] == "include"
        ),
        "exact_duplicate_occurrences_not_copied": sum(
            1 for row in manifest_rows if row["disposition"] == "exact_duplicate_of_included_document"
        ),
        "context_or_false_positive_occurrences_not_copied": sum(
            1 for row in manifest_rows if str(row["disposition"]).startswith("exclude_")
        ),
        "reconciliation_records_copied": len(METADATA_FILES),
        "workbench": counts,
        "source_files_modified": 0,
        "deletion_authorized": False,
    }
    atomic_write_text(
        MANIFESTS / "accession-preparation.json",
        json.dumps(receipt, ensure_ascii=False, indent=2) + "\n",
    )

    readme = f"""# WF-Alben archive unit — accession preparation

This package prepares the reconciled WF-Alben unit for accession. It is not yet
a completed accession: selected masters and accession-generated derivatives
have not been copied into this unit.

- Museum-digital catalogue records reviewed: {counts['catalogue_records']}
- Selected masters: {counts['selected_masters']}
- Linked derivative records marked redundant: {counts['redundant_derivative_records']}
- Other candidate files marked redundant: {counts['redundant_candidates']}
- Unresolved master decisions: {counts['unresolved_master_decisions']}
- Unresolved metadata decisions: {counts['unresolved_metadata_decisions']}
- Unique source documents collected: {len(representatives)}
- Reconciliation records copied: {len(METADATA_FILES)}

`documents/` contains direct album tables and caption exports, unique ALB and
NEF metadata working files, and the original album scan. The source file named
`Album_NEF.pdf` contains JPEG data (6456 x 4480); its bytes and original name
are deliberately preserved and the mismatch is recorded in the provenance
manifest.

`metadata/` contains the current human-readable reconciliation and metadata
decision exports. `00_MANIFESTS/document-provenance.csv` records all 64 indexed
document candidates, including exact-copy occurrences and contextual exclusions.
`00_MANIFESTS/payload-checksums.csv` records one final SHA-256 for each copied
payload. `00_MANIFESTS/accession-preparation.json` records the preparation state.

Whole-site backups, a transfer bundle, collection-wide thesaurus exports, and
an unrelated OpenOffice configuration file were not copied. They remain at
their recorded read-only source paths. No source file was modified or deleted,
and no file has deletion authorization.
"""
    atomic_write_text(UNIT / "00_README.md", readme)
    print(json.dumps(receipt, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
