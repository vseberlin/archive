#!/usr/bin/env python3
"""Plan and apply the bounded Trafo supporting-document accession.

This is deliberately unit-specific. It preserves the reviewed 21-file scope,
records already-preserved USB media in one source-disposition table, and never
authorizes or performs source deletion.
"""

from __future__ import annotations

import argparse
import csv
import filecmp
import hashlib
import io
import json
import os
import shutil
import sqlite3
import subprocess
import zipfile
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable


FORMAT = "iss-trafo-documentation-accession-v1"
USB_ROOT = Path("/media/vladimir/Elements")
FINAL_ROOT = Path("/mnt/volume/iss-archive/units/trafo")
WORK_ROOT = Path("/mnt/volume/iss-archive-work/trafo/work")
DEFAULT_RUN_ROOT = WORK_ROOT / "documentation-accession/trafo-documentation-001"
FINALIZATION_MANIFEST = (
    WORK_ROOT
    / "finalization-plan/trafo-confirmed-reconciliation-001/final-manifest.csv"
)
REPRESENTATIONS = FINAL_ROOT / "db-restore/manifests/representations.csv"
CHECKSUMS = FINAL_ROOT / "db-restore/checksums.csv"
DOCUMENTS_MANIFEST = FINAL_ROOT / "db-restore/manifests/documents.csv"
SOURCE_DISPOSITION = FINAL_ROOT / "db-restore/manifests/source-disposition.csv"
CONTENTS = FINAL_ROOT / "documents/CONTENTS.md"
README = FINAL_ROOT / "README.md"

SELECTED_FIELDS = [
    "document_id",
    "category",
    "source_kind",
    "source_path",
    "container_path",
    "container_member",
    "relative_path",
    "format",
    "condition",
    "description",
    "relation_to_trafo",
    "source_size_bytes",
    "source_mtime_ns",
    "validation_status",
]
DOCUMENT_FIELDS = [
    "document_id",
    "relative_path",
    "category",
    "format",
    "description",
    "relation_to_trafo",
    "condition",
    "source_kind",
    "source_path",
    "container_path",
    "container_member",
    "source_size_bytes",
    "source_mtime_ns",
    "final_sha256",
    "size_bytes",
    "copy_verification",
]
DISPOSITION_FIELDS = [
    "source_path",
    "source_kind",
    "unit",
    "inventory",
    "source_role",
    "final_path",
    "preservation_status",
    "preservation_basis",
    "source_sha256",
    "final_sha256",
    "source_size_bytes",
    "exact_copy_proven",
    "drain_state",
    "deletion_authorized",
    "notes",
]


class AccessionError(RuntimeError):
    pass


@dataclass(frozen=True)
class DocumentSpec:
    document_id: str
    category: str
    source_path: str
    relative_path: str
    format: str
    condition: str
    description: str
    relation_to_trafo: str
    container_member: str = ""
    expected_members: int = 0
    expected_csv_rows: int = 0

    @property
    def source_kind(self) -> str:
        return "archive_member" if self.container_member else "file"

    @property
    def source_reference(self) -> str:
        if self.container_member:
            return f"{self.source_path}!/{self.container_member}"
        return self.source_path


DOCUMENTS = (
    DocumentSpec(
        "TRAFO-DOC-001",
        "metadata",
        "/media/vladimir/Elements/restore/archive/WF/Trafo/1.Upload/Archiv/TRO Basisangaben-Stand 17.11.20.xlsx",
        "documents/metadata/upload-1/TRO Basisangaben-Stand 17.11.20.xlsx",
        "xlsx",
        "readable",
        "Archived 17 November 2020 base-data snapshot for the first upload.",
        "Direct Trafo issue metadata and first-upload working provenance.",
    ),
    DocumentSpec(
        "TRAFO-DOC-002",
        "metadata",
        "/media/vladimir/Elements/restore/archive/WF/Trafo/1.Upload/TRO-Ausgaben_WF-Trafo-Flugblätter_03_zusammen.xlsx",
        "documents/metadata/upload-1/TRO-Ausgaben_WF-Trafo-Flugblätter_03_zusammen.xlsx",
        "xlsx",
        "readable",
        "Merged upload workbook with a source-PDF-to-inventory matching sheet.",
        "Mixed Trafo and WF-Sender working data; retained for Trafo matching provenance.",
    ),
    DocumentSpec(
        "TRAFO-DOC-003",
        "metadata",
        "/media/vladimir/Elements/restore/archive/WF/Trafo/2.Upload/Annette_wetransfer-8f8507/TRO Basisangaben-Stand 11.12.20.xlsx",
        "documents/metadata/upload-2/direct-transfer/TRO Basisangaben-Stand 11.12.20.xlsx",
        "xlsx",
        "readable",
        "Direct-transfer 11 December 2020 base-data and reconciliation workbook.",
        "Direct Trafo metadata for the second upload.",
    ),
    DocumentSpec(
        "TRAFO-DOC-004",
        "metadata",
        "/media/vladimir/Elements/restore/archive/WF/GoogleDocsBackup/drive-download-20220713T123037Z-001.zip",
        "documents/metadata/upload-2/google-drive-backup/TRO Basisangaben-Stand 11.12.20.xlsx",
        "xlsx",
        "readable",
        "Independent Google Drive backup copy of the 11 December 2020 workbook.",
        "Direct Trafo metadata; exact container/member provenance is retained.",
        "Betriebszeitung TRO/TRO Basisangaben-Stand 11.12.20.xlsx",
    ),
    DocumentSpec(
        "TRAFO-DOC-005",
        "metadata",
        "/media/vladimir/Elements/restore/archive/WF/Trafo/2.Upload/TRO Basisangaben_03.xlsx",
        "documents/metadata/upload-2/TRO Basisangaben_03.xlsx",
        "xlsx",
        "readable",
        "Revised candidate metadata with existing-record and scan-availability sheets.",
        "Direct Trafo reconciliation and scan-availability evidence.",
    ),
    DocumentSpec(
        "TRAFO-DOC-006",
        "collection",
        "/media/vladimir/Elements/restore/archive/WF/Trafo/Trafo_Bestand.xlsx",
        "documents/collection/Trafo_Bestand.xlsx",
        "xlsx",
        "readable",
        "Collection holdings and digitization-planning workbook.",
        "Direct collection-level Trafo holdings evidence.",
    ),
    DocumentSpec(
        "TRAFO-DOC-007",
        "collection",
        "/media/vladimir/Elements/restore/archive/WF/Trafo/Trafo_Bestand_02.xlsx",
        "documents/collection/Trafo_Bestand_02.xlsx",
        "xlsx",
        "damaged-unreadable",
        "Later holdings workbook retained although its ZIP central directory is damaged.",
        "Direct by filename and provenance; content is currently unverified.",
    ),
    DocumentSpec(
        "TRAFO-DOC-008",
        "collection",
        "/media/vladimir/Elements/restore/archive/WF/Trafo/Trafo_Lieferschein-Bibliocopy.pdf",
        "documents/collection/Trafo_Lieferschein-Bibliocopy.pdf",
        "pdf",
        "readable",
        "Readable holdings, scan-plan, and delivery representation.",
        "Direct collection-level Trafo digitization evidence.",
    ),
    DocumentSpec(
        "TRAFO-DOC-009",
        "project",
        "/media/vladimir/Elements/restore/archive/WF/Trafo/Archiv/Antrag-TRO_05.doc",
        "documents/project/Antrag-TRO_05.doc",
        "doc",
        "readable",
        "Project application for online presentation of Der Transformator.",
        "Direct Trafo project and funding provenance.",
    ),
    DocumentSpec(
        "TRAFO-DOC-010",
        "project",
        "/media/vladimir/Elements/restore/archive/WF/Trafo/Archiv/Industriesalon-Schöneweide_Lieferschein-Transformator-Ausgaben.doc",
        "documents/project/Industriesalon-Schöneweide_Lieferschein-Transformator-Ausgaben.doc",
        "doc",
        "readable",
        "Delivery and receipt confirmation for bound Transformator issues.",
        "Direct physical-custody and scanning provenance.",
    ),
    DocumentSpec(
        "TRAFO-DOC-011",
        "project",
        "/media/vladimir/Elements/restore/archive/WF/Trafo/Archiv/Industriesalon-Schöneweide_Lieferschein-Transformator-Ausgaben_Bibliocopy.docx",
        "documents/project/Industriesalon-Schöneweide_Lieferschein-Transformator-Ausgaben_Bibliocopy.docx",
        "docx",
        "readable",
        "BiblioCopy delivery confirmation for additional Transformator volumes.",
        "Direct physical-custody and scanning provenance.",
    ),
    DocumentSpec(
        "TRAFO-DOC-012",
        "project",
        "/media/vladimir/Elements/restore/archive/WF/Trafo/Sachbericht/Abschlussbericht TRO-02_SW-korr.docx",
        "documents/project/Abschlussbericht TRO-02_SW-korr.docx",
        "docx",
        "readable",
        "Corrected final project report source document.",
        "Direct Trafo project-completion provenance.",
    ),
    DocumentSpec(
        "TRAFO-DOC-013",
        "project",
        "/media/vladimir/Elements/restore/archive/WF/Trafo/Sachbericht/Sachbericht Digitalisierung TRAfO - Industriesalon.pdf",
        "documents/project/Sachbericht Digitalisierung TRAfO - Industriesalon.pdf",
        "pdf",
        "readable",
        "Project report describing digitization and online publication.",
        "Direct Trafo project, rights, and publication provenance.",
    ),
    DocumentSpec(
        "TRAFO-DOC-014",
        "project",
        "/media/vladimir/Elements/restore/archive/WF/Trafo/Sachbericht/Abrechnung_Trafo-Digitalisierung.pdf",
        "documents/project/Abrechnung_Trafo-Digitalisierung.pdf",
        "pdf",
        "readable",
        "Final project accounting summary.",
        "Direct Trafo funding and project-closeout provenance.",
    ),
    DocumentSpec(
        "TRAFO-DOC-015",
        "project",
        "/media/vladimir/Elements/restore/archive/WF/Trafo/Rechnungen, Finanzen/Bewilligungsbescheid-Trafo_BAB.pdf",
        "documents/project/Bewilligungsbescheid-Trafo_BAB.pdf",
        "pdf",
        "damaged-unreadable",
        "Grant decision byte stream retained although its PDF structure is unreadable.",
        "Direct Trafo funding provenance; content is currently unverified.",
    ),
    DocumentSpec(
        "TRAFO-DOC-016",
        "metadata-export",
        "/media/vladimir/Elements/restore/archive/WF/Trafo/1.Upload/WFS_Trafo_csv_xml.zip",
        "export/md/upload-1/WFS_Trafo_csv_xml.zip",
        "zip",
        "readable",
        "First-upload package of 617 per-record XML files.",
        "Mixed Trafo and WF-Sender museum-digital import lineage.",
        expected_members=617,
    ),
    DocumentSpec(
        "TRAFO-DOC-017",
        "metadata-export",
        "/media/vladimir/Elements/restore/archive/WF/Trafo/1.Upload/TRO-Ausgaben_WF-Trafo_Flugblaetter_09_final.csv",
        "export/md/upload-1/TRO-Ausgaben_WF-Trafo_Flugblaetter_09_final.csv",
        "csv",
        "readable",
        "Final first-upload machine-readable import table.",
        "Mixed Trafo and WF-Sender final import provenance.",
        expected_csv_rows=618,
    ),
    DocumentSpec(
        "TRAFO-DOC-018",
        "metadata-export",
        "/media/vladimir/Elements/restore/archive/WF/Trafo/1.Upload/TRO-Ausgaben_WF-Trafo_Flugblaetter_09_final.xlsx",
        "export/md/upload-1/TRO-Ausgaben_WF-Trafo_Flugblaetter_09_final.xlsx",
        "xlsx",
        "readable",
        "Editable counterpart of the final first-upload import table.",
        "Mixed Trafo and WF-Sender final import provenance.",
    ),
    DocumentSpec(
        "TRAFO-DOC-019",
        "metadata-export",
        "/media/vladimir/Elements/restore/archive/WF/Trafo/2.Upload/csv_xml.zip",
        "export/md/upload-2/csv_xml.zip",
        "zip",
        "readable",
        "Second-upload package of 1,043 per-record XML files.",
        "Direct Trafo museum-digital import lineage.",
        expected_members=1043,
    ),
    DocumentSpec(
        "TRAFO-DOC-020",
        "metadata-export",
        "/media/vladimir/Elements/restore/archive/WF/Trafo/2.Upload/TRO-Basisangaben_08_finalAG.csv",
        "export/md/upload-2/TRO-Basisangaben_08_finalAG.csv",
        "csv",
        "readable",
        "AG-labelled final second-upload machine-readable import table.",
        "Direct Trafo final import provenance.",
        expected_csv_rows=1044,
    ),
    DocumentSpec(
        "TRAFO-DOC-021",
        "metadata-export",
        "/media/vladimir/Elements/restore/archive/WF/Trafo/2.Upload/TRO-Basisangaben_08_finalAG.xlsx",
        "export/md/upload-2/TRO-Basisangaben_08_finalAG.xlsx",
        "xlsx",
        "readable",
        "Editable counterpart of the AG-labelled final second-upload table.",
        "Direct Trafo final import provenance.",
    ),
)

# These byte-identical USB copies are also safe future drain candidates once
# the selected copy above is accessioned. They are not additional documents.
DOCUMENT_DUPLICATES = (
    (
        "/media/vladimir/Elements/restore/archive/WF/Trafo/Industriesalon-Schöneweide_Lieferschein-Transformator-Ausgaben.doc",
        "documents/project/Industriesalon-Schöneweide_Lieferschein-Transformator-Ausgaben.doc",
    ),
    (
        "/media/vladimir/Elements/restore/archive/WF/Trafo/Industriesalon-Schöneweide_Lieferschein-Transformator-Ausgaben_Bibliocopy.docx",
        "documents/project/Industriesalon-Schöneweide_Lieferschein-Transformator-Ausgaben_Bibliocopy.docx",
    ),
    (
        "/media/vladimir/Elements/restore/archive/WF/Trafo/Archiv/Trafo_Lieferschein-Bibliocopy.pdf",
        "documents/collection/Trafo_Lieferschein-Bibliocopy.pdf",
    ),
)


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def atomic_write_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def atomic_write_text(path: Path, payload: str) -> None:
    atomic_write_bytes(path, payload.encode("utf-8"))


def write_json(path: Path, payload: dict[str, Any]) -> None:
    atomic_write_text(
        path, json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    )


def csv_bytes(rows: Iterable[dict[str, Any]], fields: list[str]) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({field: row.get(field, "") for field in fields})
    return output.getvalue().encode("utf-8")


def write_csv(path: Path, rows: Iterable[dict[str, Any]], fields: list[str]) -> None:
    atomic_write_bytes(path, csv_bytes(rows, fields))


def safe_relative(value: str) -> Path:
    path = Path(value)
    if path.is_absolute() or ".." in path.parts or not value.strip():
        raise AccessionError(f"unsafe final relative path: {value!r}")
    return path


def ensure_read_only_source_mount() -> None:
    result = subprocess.run(
        ["findmnt", "-n", "-o", "TARGET,OPTIONS", "--target", str(USB_ROOT)],
        check=True,
        capture_output=True,
        text=True,
    )
    fields = result.stdout.strip().split(maxsplit=1)
    if len(fields) != 2 or "ro" not in fields[1].split(","):
        raise AccessionError(f"USB source mount is not read-only: {result.stdout.strip()}")


def source_stat(spec: DocumentSpec) -> tuple[int, int]:
    source = Path(spec.source_path)
    stat = source.stat()
    if spec.container_member:
        with zipfile.ZipFile(source) as archive:
            info = archive.getinfo(spec.container_member)
            return info.file_size, stat.st_mtime_ns
    return stat.st_size, stat.st_mtime_ns


def validate_zip(path: Path, expected_members: int = 0) -> str:
    with zipfile.ZipFile(path) as archive:
        bad_member = archive.testzip()
        if bad_member:
            raise AccessionError(f"ZIP CRC failure in {path}: {bad_member}")
        members = [item for item in archive.infolist() if not item.is_dir()]
        if expected_members and len(members) != expected_members:
            raise AccessionError(
                f"ZIP member count mismatch in {path}: "
                f"expected {expected_members}, got {len(members)}"
            )
    return f"zip-readable;members={len(members)}"


def validate_document_path(path: Path, spec: DocumentSpec) -> str:
    if spec.condition == "damaged-unreadable":
        if spec.format == "xlsx":
            try:
                validate_zip(path)
            except (zipfile.BadZipFile, AccessionError) as error:
                return f"expected-damaged:{type(error).__name__}"
            raise AccessionError(f"expected damaged XLSX unexpectedly validates: {path}")
        if spec.format == "pdf":
            result = subprocess.run(
                ["pdfinfo", str(path)], capture_output=True, text=True
            )
            if result.returncode:
                return f"expected-damaged:pdfinfo-exit-{result.returncode}"
            raise AccessionError(f"expected damaged PDF unexpectedly validates: {path}")
    if spec.format in {"xlsx", "docx"}:
        return validate_zip(path)
    if spec.format == "zip":
        return validate_zip(path, spec.expected_members)
    if spec.format == "pdf":
        result = subprocess.run(
            ["pdfinfo", str(path)], capture_output=True, text=True
        )
        if result.returncode:
            raise AccessionError(
                f"PDF validation failed for {path}: {result.stderr.strip()}"
            )
        return "pdfinfo-readable"
    if spec.format == "doc":
        if path.read_bytes()[:8] != bytes.fromhex("d0cf11e0a1b11ae1"):
            raise AccessionError(f"legacy DOC signature missing: {path}")
        return "ole-compound-document"
    if spec.format == "csv":
        payload = path.read_bytes()
        try:
            text = payload.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = payload.decode("cp1252")
        rows = sum(1 for _row in csv.reader(io.StringIO(text), delimiter=";"))
        if rows != spec.expected_csv_rows:
            raise AccessionError(
                f"CSV row count mismatch for {path}: "
                f"expected {spec.expected_csv_rows}, got {rows}"
            )
        return f"csv-readable;rows={rows}"
    raise AccessionError(f"unsupported format: {spec.format}")


def validate_source(spec: DocumentSpec) -> str:
    source = Path(spec.source_path)
    if not source.is_file():
        raise AccessionError(f"source file missing: {source}")
    if spec.container_member:
        with zipfile.ZipFile(source) as archive:
            try:
                payload = archive.read(spec.container_member)
            except KeyError as error:
                raise AccessionError(
                    f"archive member missing: {spec.source_reference}"
                ) from error
        temporary = DEFAULT_RUN_ROOT.parent / f".validation-{os.getpid()}-{spec.document_id}"
        try:
            atomic_write_bytes(temporary, payload)
            return validate_document_path(temporary, spec)
        finally:
            temporary.unlink(missing_ok=True)
    return validate_document_path(source, spec)


def digest_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def selected_rows() -> list[dict[str, Any]]:
    rows = []
    for spec in DOCUMENTS:
        size, mtime_ns = source_stat(spec)
        row = asdict(spec)
        row.update(
            {
                "source_kind": spec.source_kind,
                "source_path": spec.source_reference,
                "container_path": spec.source_path if spec.container_member else "",
                "source_size_bytes": size,
                "source_mtime_ns": mtime_ns,
                "validation_status": validate_source(spec),
            }
        )
        rows.append(row)
    return rows


def blank_disposition() -> dict[str, Any]:
    return {field: "" for field in DISPOSITION_FIELDS}


def disposition_from_representation(
    source_path: str,
    inventory: str,
    source_role: str,
    source_sha256: str,
    source_size_bytes: str,
    representations: list[dict[str, str]],
) -> dict[str, Any]:
    row = blank_disposition()
    row.update(
        {
            "source_path": source_path,
            "source_kind": "file",
            "unit": "trafo",
            "inventory": inventory,
            "source_role": source_role,
            "source_sha256": source_sha256,
            "source_size_bytes": source_size_bytes,
            "deletion_authorized": "false",
        }
    )
    if not representations:
        row.update(
            {
                "preservation_status": "not_preserved_source_loss",
                "preservation_basis": (
                    "source TIFF is a recorded loss; an issue PDF fallback does "
                    "not preserve this byte stream"
                ),
                "exact_copy_proven": "false",
                "drain_state": "retain_not_preserved",
                "notes": "Do not delete; see documents/source-losses.json.",
            }
        )
        return row

    final_paths = sorted({item["relative_path"] for item in representations})
    final_hashes = sorted({item["content_sha256"] for item in representations})
    exact = any(item["content_sha256"] == source_sha256 for item in representations)
    if exact:
        status = "preserved_exact_copy"
        basis = "stored source SHA-256 equals at least one accepted final representation"
    else:
        status = "preserved_as_metadata_enriched_master"
        basis = (
            "accepted TIFF master preserves decoded source pixels and adds "
            "archive metadata; source SHA-256 remains recorded"
        )
    row.update(
        {
            "final_path": " | ".join(final_paths),
            "preservation_status": status,
            "preservation_basis": basis,
            "final_sha256": " | ".join(final_hashes),
            "exact_copy_proven": str(exact).lower(),
            "drain_state": "candidate_after_separate_review",
            "notes": "Evidence only; no deletion is authorized.",
        }
    )
    return row


def media_disposition_rows() -> list[dict[str, Any]]:
    final_rows = read_csv(FINALIZATION_MANIFEST)
    representation_rows = read_csv(REPRESENTATIONS)
    by_source_hash: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in representation_rows:
        by_source_hash[row["source_sha256"]].append(row)

    rows: list[dict[str, Any]] = []
    finalization_sources: set[str] = set()
    for source in final_rows:
        source_path = source["source_path"]
        source_file = Path(source_path)
        if not source_file.is_file():
            raise AccessionError(f"recorded USB source is missing: {source_path}")
        actual_size = source_file.stat().st_size
        if actual_size != int(source["size_bytes"]):
            raise AccessionError(
                f"recorded USB source size changed: {source_path}; "
                f"expected {source['size_bytes']}, got {actual_size}"
            )
        finalization_sources.add(source_path)
        rows.append(
            disposition_from_representation(
                source_path,
                source["inventory"],
                "source_" + source["role"],
                source["sha256"],
                source["size_bytes"],
                by_source_hash.get(source["sha256"], []),
            )
        )

    for representation in representation_rows:
        source_path = representation["source_path"]
        if not source_path.startswith(str(USB_ROOT) + os.sep):
            continue
        if source_path in finalization_sources:
            continue
        source_file = Path(source_path)
        if not source_file.is_file():
            raise AccessionError(f"recorded USB source is missing: {source_path}")
        rows.append(
            disposition_from_representation(
                source_path,
                representation["inventory"],
                "source_for_" + representation["role"],
                representation["source_sha256"],
                str(source_file.stat().st_size),
                [representation],
            )
        )
    return rows


def document_disposition_rows(
    phase: str, document_rows: list[dict[str, Any]] | None = None
) -> list[dict[str, Any]]:
    final_by_id = {
        row["document_id"]: row for row in (document_rows or [])
    }
    rows: list[dict[str, Any]] = []
    for spec in DOCUMENTS:
        row = blank_disposition()
        final = final_by_id.get(spec.document_id)
        if phase == "applied" and not final:
            raise AccessionError(f"missing final document row for {spec.document_id}")
        if phase == "planned":
            status = "pending_document_accession"
            basis = "selected for bounded exact-byte accession"
            exact = "false"
            drain = "not_yet_eligible"
            final_sha = ""
            notes = "Plan only; source must be retained until accession succeeds."
        elif spec.container_member:
            status = "preserved_exact_archive_member"
            basis = "final file was byte-compared with the exact ZIP member"
            exact = "true"
            drain = "retain_mixed_container"
            final_sha = final["final_sha256"]
            notes = (
                "The outer ZIP contains other projects and is not a drain "
                "candidate; no deletion is authorized."
            )
        else:
            status = "preserved_exact_copy"
            basis = "final file was byte-compared with the USB source"
            exact = "true"
            drain = "candidate_after_separate_review"
            final_sha = final["final_sha256"]
            notes = "Evidence only; no deletion is authorized."
        size, _mtime_ns = source_stat(spec)
        row.update(
            {
                "source_path": spec.source_reference,
                "source_kind": spec.source_kind,
                "unit": "trafo",
                "source_role": "supporting_document",
                "final_path": spec.relative_path,
                "preservation_status": status,
                "preservation_basis": basis,
                "final_sha256": final_sha,
                "source_size_bytes": size,
                "exact_copy_proven": exact,
                "drain_state": drain,
                "deletion_authorized": "false",
                "notes": notes,
            }
        )
        rows.append(row)

    specs_by_final = {spec.relative_path: spec for spec in DOCUMENTS}
    for duplicate_path, final_path in DOCUMENT_DUPLICATES:
        spec = specs_by_final[final_path]
        duplicate = Path(duplicate_path)
        size = duplicate.stat().st_size
        row = blank_disposition()
        if phase == "planned":
            status = "pending_duplicate_proof"
            basis = "candidate duplicate of selected supporting document"
            exact = "false"
            drain = "not_yet_eligible"
            final_sha = ""
            notes = "Plan only; source must be retained until accession succeeds."
        else:
            final_file = FINAL_ROOT / safe_relative(final_path)
            if not filecmp.cmp(duplicate, final_file, shallow=False):
                raise AccessionError(f"expected duplicate differs: {duplicate}")
            final = final_by_id[spec.document_id]
            status = "preserved_exact_duplicate"
            basis = "byte-identical to the accepted final supporting document"
            exact = "true"
            drain = "candidate_after_separate_review"
            final_sha = final["final_sha256"]
            notes = "Redundant USB copy; evidence only, no deletion is authorized."
        row.update(
            {
                "source_path": duplicate_path,
                "source_kind": "file",
                "unit": "trafo",
                "source_role": "duplicate_supporting_document",
                "final_path": final_path,
                "preservation_status": status,
                "preservation_basis": basis,
                "final_sha256": final_sha,
                "source_size_bytes": size,
                "exact_copy_proven": exact,
                "drain_state": drain,
                "deletion_authorized": "false",
                "notes": notes,
            }
        )
        rows.append(row)
    return rows


def all_disposition_rows(
    phase: str, document_rows: list[dict[str, Any]] | None = None
) -> list[dict[str, Any]]:
    rows = media_disposition_rows() + document_disposition_rows(phase, document_rows)
    rows.sort(key=lambda row: (row["source_path"], row["final_path"]))
    if len({row["source_path"] for row in rows}) != len(rows):
        raise AccessionError("source-disposition contains duplicate source paths")
    if any(row["deletion_authorized"] != "false" for row in rows):
        raise AccessionError("source-disposition must never authorize deletion")
    return rows


def disposition_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "rows": len(rows),
        "preservation_status": dict(
            sorted(Counter(row["preservation_status"] for row in rows).items())
        ),
        "drain_state": dict(
            sorted(Counter(row["drain_state"] for row in rows).items())
        ),
        "deletion_authorized_true": sum(
            row["deletion_authorized"] == "true" for row in rows
        ),
    }


def validate_specs() -> None:
    if len(DOCUMENTS) != 21:
        raise AccessionError(f"expected 21 documents, got {len(DOCUMENTS)}")
    ids = {spec.document_id for spec in DOCUMENTS}
    destinations = {spec.relative_path for spec in DOCUMENTS}
    references = {spec.source_reference for spec in DOCUMENTS}
    if len(ids) != 21 or len(destinations) != 21 or len(references) != 21:
        raise AccessionError("document IDs, destinations, and sources must be unique")
    for spec in DOCUMENTS:
        safe_relative(spec.relative_path)
        if not (
            spec.relative_path.startswith("documents/")
            or spec.relative_path.startswith("export/md/")
        ):
            raise AccessionError(f"unexpected destination component: {spec.relative_path}")
        source = Path(spec.source_path)
        if USB_ROOT not in source.parents:
            raise AccessionError(f"source outside protected USB root: {source}")


def command_plan(args: argparse.Namespace) -> dict[str, Any]:
    validate_specs()
    ensure_read_only_source_mount()
    run_root = args.run_root.resolve()
    if run_root.exists():
        raise AccessionError(f"run root already exists: {run_root}")
    if WORK_ROOT not in run_root.parents:
        raise AccessionError(f"run root must be below {WORK_ROOT}")
    run_root.mkdir(parents=True)

    selected = selected_rows()
    disposition = all_disposition_rows("planned")
    write_csv(run_root / "selected-documents.csv", selected, SELECTED_FIELDS)
    write_csv(
        run_root / "source-disposition.planned.csv",
        disposition,
        DISPOSITION_FIELDS,
    )
    summary = {
        "format": FORMAT,
        "phase": "planned",
        "created_at": utc_now(),
        "run_root": str(run_root),
        "source_mount_read_only": True,
        "selected_documents": len(selected),
        "selected_bytes": sum(int(row["source_size_bytes"]) for row in selected),
        "category_counts": dict(
            sorted(Counter(row["category"] for row in selected).items())
        ),
        "format_counts": dict(
            sorted(Counter(row["format"] for row in selected).items())
        ),
        "condition_counts": dict(
            sorted(Counter(row["condition"] for row in selected).items())
        ),
        "source_disposition": disposition_summary(disposition),
        "deletion_performed": False,
        "final_unit_changed": False,
    }
    write_json(run_root / "plan-summary.json", summary)
    atomic_write_text(
        run_root / "README.md",
        (
            "# Trafo documentation accession\n\n"
            "This run is the reviewed 21-file Trafo supporting-document scope.\n"
            "`selected-documents.csv` is the exact dry-run path plan. "
            "`source-disposition.planned.csv` is evidence only: every row has "
            "`deletion_authorized=false`, and no deletion operation exists in "
            "this tool.\n"
        ),
    )
    return summary


def selected_rows_equal(
    actual: list[dict[str, str]], expected: list[dict[str, Any]]
) -> bool:
    normalized = [
        {field: str(row.get(field, "")) for field in SELECTED_FIELDS}
        for row in expected
    ]
    return actual == normalized


def copy_to_stage(spec: DocumentSpec, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if spec.container_member:
        with zipfile.ZipFile(spec.source_path) as archive:
            with archive.open(spec.container_member) as source:
                with destination.open("wb") as target:
                    shutil.copyfileobj(source, target, length=1024 * 1024)
                    target.flush()
                    os.fsync(target.fileno())
    else:
        shutil.copy2(spec.source_path, destination)


def archive_member_equal(spec: DocumentSpec, destination: Path) -> bool:
    with zipfile.ZipFile(spec.source_path) as archive:
        with archive.open(spec.container_member) as source:
            with destination.open("rb") as target:
                while True:
                    left = source.read(1024 * 1024)
                    right = target.read(1024 * 1024)
                    if left != right:
                        return False
                    if not left:
                        return True


def verify_exact_copy(spec: DocumentSpec, destination: Path) -> None:
    if spec.container_member:
        exact = archive_member_equal(spec, destination)
    else:
        exact = filecmp.cmp(spec.source_path, destination, shallow=False)
    if not exact:
        raise AccessionError(f"staged copy differs from source: {spec.source_reference}")


def build_documents_manifest() -> list[dict[str, Any]]:
    rows = []
    for spec in DOCUMENTS:
        final_path = FINAL_ROOT / safe_relative(spec.relative_path)
        verify_exact_copy(spec, final_path)
        validation = validate_document_path(final_path, spec)
        size, mtime_ns = source_stat(spec)
        rows.append(
            {
                "document_id": spec.document_id,
                "relative_path": spec.relative_path,
                "category": spec.category,
                "format": spec.format,
                "description": spec.description,
                "relation_to_trafo": spec.relation_to_trafo,
                "condition": spec.condition,
                "source_kind": spec.source_kind,
                "source_path": spec.source_reference,
                "container_path": spec.source_path if spec.container_member else "",
                "container_member": spec.container_member,
                "source_size_bytes": size,
                "source_mtime_ns": mtime_ns,
                "final_sha256": digest_file(final_path),
                "size_bytes": final_path.stat().st_size,
                "copy_verification": f"exact-byte-copy;{validation}",
            }
        )
    return rows


def contents_text(rows: list[dict[str, Any]]) -> str:
    lines = [
        "# Trafo supporting documents",
        "",
        "This index covers the unit's supporting documentation and metadata exports.",
        "Exact source or container/member provenance and final SHA-256 values are in",
        "`../db-restore/manifests/documents.csv`.",
        "",
        "Existing accession evidence:",
        "",
        "- `metadata-sources/`: one JSON metadata-source record per archive object.",
        "- `source-losses.json`: three recorded TIFF source losses represented by exact issue-PDF fallbacks.",
        "",
        "Accessioned supporting documents:",
        "",
        "| ID | Final path | Purpose | Condition |",
        "|---|---|---|---|",
    ]
    for row in rows:
        path = row["relative_path"].replace("|", "\\|")
        description = row["description"].replace("|", "\\|")
        lines.append(
            f"| {row['document_id']} | `{path}` | {description} | {row['condition']} |"
        )
    lines.extend(
        [
            "",
            "The two mixed first-upload metadata exports retain WF-Sender rows because",
            "they are part of the original Trafo upload provenance. Their presence does",
            "not assign those records to the Trafo collection.",
            "",
            "`Trafo_Bestand_02.xlsx` and `Bewilligungsbescheid-Trafo_BAB.pdf` are",
            "retained as received with explicit unreadable-condition notes.",
            "",
        ]
    )
    return "\n".join(lines)


def updated_readme_text() -> str:
    current = README.read_text(encoding="utf-8")
    marker = "## Supporting documentation"
    if marker in current:
        raise AccessionError("Trafo README already contains supporting-document section")
    return (
        current.rstrip()
        + "\n\n"
        + marker
        + "\n\n"
        + "The unit also preserves 21 reviewed supporting artifacts: collection and "
        + "project records, selected source workbooks, final CSV/XLSX import tables, "
        + "and the two original XML export ZIPs. Two damaged source files are "
        + "retained as received and condition-noted. `documents/CONTENTS.md` is the "
        + "readable index; `db-restore/manifests/documents.csv` records exact source "
        + "or container/member provenance and final SHA-256 values.\n\n"
        + "`db-restore/manifests/source-disposition.csv` lists preserved USB inputs "
        + "and blocked exceptions for later drain review. It is evidence only: every "
        + "row has `deletion_authorized=false`, and no source has been deleted.\n"
    )


def updated_checksums_rows(
    document_rows: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    rows = read_csv(CHECKSUMS)
    known = {row["path"] for row in rows}
    for document in document_rows:
        if document["relative_path"] in known:
            raise AccessionError(
                f"checksum row already exists: {document['relative_path']}"
            )
        rows.append(
            {
                "path": document["relative_path"],
                "sha256": document["final_sha256"],
                "size_bytes": document["size_bytes"],
            }
        )
    rows.sort(key=lambda row: row["path"])
    return rows


def command_apply(args: argparse.Namespace) -> dict[str, Any]:
    validate_specs()
    ensure_read_only_source_mount()
    run_root = args.run_root.resolve()
    if WORK_ROOT not in run_root.parents or not run_root.is_dir():
        raise AccessionError(f"planned run root missing or unsafe: {run_root}")
    if (run_root / "apply-receipt.json").exists():
        raise AccessionError(f"run already applied: {run_root}")

    expected_selected = selected_rows()
    actual_selected = read_csv(run_root / "selected-documents.csv")
    if not selected_rows_equal(actual_selected, expected_selected):
        raise AccessionError("current source state differs from the reviewed dry-run")

    new_control_paths = (DOCUMENTS_MANIFEST, SOURCE_DISPOSITION, CONTENTS)
    existing = [
        str(FINAL_ROOT / safe_relative(spec.relative_path))
        for spec in DOCUMENTS
        if (FINAL_ROOT / safe_relative(spec.relative_path)).exists()
    ]
    existing.extend(str(path) for path in new_control_paths if path.exists())
    if existing:
        raise AccessionError("refusing existing final destinations: " + ", ".join(existing))

    stage_root = run_root / "staging/unit-addition"
    if stage_root.exists():
        raise AccessionError(f"staging root already exists: {stage_root}")
    for spec in DOCUMENTS:
        staged = stage_root / safe_relative(spec.relative_path)
        copy_to_stage(spec, staged)
        verify_exact_copy(spec, staged)
        validate_document_path(staged, spec)

    rollback = run_root / "rollback"
    rollback.mkdir()
    shutil.copy2(README, rollback / "README.md")
    shutil.copy2(CHECKSUMS, rollback / "checksums.csv")

    for spec in DOCUMENTS:
        staged = stage_root / safe_relative(spec.relative_path)
        final = FINAL_ROOT / safe_relative(spec.relative_path)
        final.parent.mkdir(parents=True, exist_ok=True)
        os.replace(staged, final)

    documents = build_documents_manifest()
    disposition = all_disposition_rows("applied", documents)
    checksums = updated_checksums_rows(documents)

    write_csv(DOCUMENTS_MANIFEST, documents, DOCUMENT_FIELDS)
    atomic_write_text(CONTENTS, contents_text(documents))
    write_csv(SOURCE_DISPOSITION, disposition, DISPOSITION_FIELDS)
    write_csv(CHECKSUMS, checksums, ["path", "sha256", "size_bytes"])
    atomic_write_text(README, updated_readme_text())
    write_csv(run_root / "source-disposition.csv", disposition, DISPOSITION_FIELDS)

    summary = {
        "format": FORMAT,
        "phase": "applied",
        "created_at": utc_now(),
        "run_root": str(run_root),
        "final_root": str(FINAL_ROOT),
        "source_mount_read_only": True,
        "documents_added": len(documents),
        "document_bytes": sum(int(row["size_bytes"]) for row in documents),
        "final_payload_hashes_added": len(documents),
        "source_disposition": disposition_summary(disposition),
        "restore_representation_rows_unchanged": len(read_csv(REPRESENTATIONS)),
        "deletion_performed": False,
        "deletion_authorized": False,
    }
    write_json(run_root / "apply-receipt.json", summary)
    return summary


def command_verify(args: argparse.Namespace) -> dict[str, Any]:
    ensure_read_only_source_mount()
    documents = read_csv(DOCUMENTS_MANIFEST)
    disposition = read_csv(SOURCE_DISPOSITION)
    checksums = {row["path"]: row for row in read_csv(CHECKSUMS)}
    errors: list[str] = []
    if len(documents) != 21:
        errors.append(f"documents.csv rows: expected 21, got {len(documents)}")
    for document in documents:
        path = FINAL_ROOT / safe_relative(document["relative_path"])
        if not path.is_file():
            errors.append(f"missing document: {document['relative_path']}")
            continue
        if path.stat().st_size != int(document["size_bytes"]):
            errors.append(f"size mismatch: {document['relative_path']}")
        checksum = checksums.get(document["relative_path"])
        if not checksum or checksum["sha256"] != document["final_sha256"]:
            errors.append(f"checksum catalog mismatch: {document['relative_path']}")
    if len(disposition) != len({row["source_path"] for row in disposition}):
        errors.append("duplicate source paths in source-disposition")
    if any(row["deletion_authorized"] != "false" for row in disposition):
        errors.append("source-disposition contains deletion authorization")
    for required in (CONTENTS, README, REPRESENTATIONS):
        if not required.is_file():
            errors.append(f"required control file missing: {required}")
    connection = sqlite3.connect(FINAL_ROOT / "db-restore/RESTORE.sqlite")
    try:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        representation_count = connection.execute(
            "SELECT COUNT(*) FROM representation"
        ).fetchone()[0]
    finally:
        connection.close()
    if integrity != "ok":
        errors.append(f"RESTORE.sqlite integrity: {integrity}")
    if representation_count != len(read_csv(REPRESENTATIONS)):
        errors.append("RESTORE.sqlite and representations.csv row counts differ")
    result = {
        "format": FORMAT,
        "phase": "verified",
        "created_at": utc_now(),
        "source_mount_read_only": True,
        "documents": len(documents),
        "source_disposition": disposition_summary(disposition),
        "restore_sqlite_integrity": integrity,
        "restore_representation_rows": representation_count,
        "payload_rehashed": False,
        "errors": errors,
        "result": "ok" if not errors else "error",
    }
    if args.output:
        write_json(args.output.resolve(), result)
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("plan", "apply"):
        subparser = subparsers.add_parser(command)
        subparser.add_argument("--run-root", type=Path, default=DEFAULT_RUN_ROOT)
    verify = subparsers.add_parser("verify")
    verify.add_argument("--output", type=Path)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        if args.command == "plan":
            result = command_plan(args)
        elif args.command == "apply":
            result = command_apply(args)
        else:
            result = command_verify(args)
    except (AccessionError, OSError, csv.Error, zipfile.BadZipFile) as error:
        print(json.dumps({"result": "error", "error": str(error)}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 0 if result.get("result", "ok") != "error" else 1


if __name__ == "__main__":
    raise SystemExit(main())
