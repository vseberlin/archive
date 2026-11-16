#!/usr/bin/env python3
"""Replace the noisy Paragon document bundle with reviewed USB originals."""

from __future__ import annotations

import csv
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path


FINAL = Path("/mnt/volume/iss-archive/units/paragon")
RUN = Path(__file__).resolve().parent
USB = Path("/media/vladimir/Elements")
DOCUMENTS = FINAL / "documents"
DOCUMENTS_MANIFEST = FINAL / "db-restore/manifests/documents.csv"
CHECKSUMS = FINAL / "db-restore/checksums.csv"
README = FINAL / "README.md"
RESTORE = FINAL / "db-restore/RESTORE.sqlite"
STAGE = RUN / "stage"
BEFORE = RUN / "before"
RETIRED = RUN / "retired-documents"
RECEIPT = RUN / "receipt.json"

MANIFEST_FIELDS = [
    "document_id",
    "relative_path",
    "category",
    "format",
    "description",
    "relation_to_paragon",
    "date_note",
    "condition",
    "source_kind",
    "source_path",
    "duplicate_source_paths",
    "source_size_bytes",
    "source_mtime_ns",
    "source_mtime_utc",
    "final_sha256",
    "size_bytes",
    "copy_verification",
]


@dataclass(frozen=True)
class Document:
    document_id: str
    relative_path: str
    category: str
    format: str
    description: str
    relation: str
    date_note: str
    condition: str
    source_path: Path
    expected_sha256: str
    duplicate_source_paths: tuple[Path, ...] = ()


DOCS = (
    Document(
        "PARAGON-DOC-001",
        "documents/project-history/Betriebschronik.pdf",
        "project-history",
        "pdf",
        "Scanned VEB Kassenblock company chronicle.",
        "direct",
        "Historical content; USB scan created 2019-01-28.",
        "Readable 22-page image-only PDF; no text layer.",
        USB
        / "restore/archive/Findbücher/Andere Scans vom Ricoah/Paragon/Betriebschronik.pdf",
        "sha256:5d81279a4f67a0d9772fe3eeed9be7b124f6b7080af364da6fabad648b914d19",
        (
            USB
            / "restore/archive/WF/Findbücher/Andere Scans vom Ricoah/Paragon/Betriebschronik.pdf",
        ),
    ),
    Document(
        "PARAGON-DOC-002",
        "documents/project-history/Kurzinfo aus mellertarchiv.pdf",
        "project-history",
        "pdf",
        "Short historical information on the Paragon/Kassenblock enterprise from the Mellert archive.",
        "direct",
        "Historical content; USB scan created 2019-01-28.",
        "Readable 3-page image-only PDF; no text layer.",
        USB
        / "restore/archive/Findbücher/Andere Scans vom Ricoah/Paragon/Kurzinfo aus mellertarchiv.pdf",
        "sha256:c3ba860ab283ec102aa41304450deb8d50245305c596c3d44ebed89e2d5d7b4f",
        (
            USB
            / "restore/archive/WF/Findbücher/Andere Scans vom Ricoah/Paragon/Kurzinfo aus mellertarchiv.pdf",
        ),
    ),
    Document(
        "PARAGON-DOC-003",
        "documents/project-history/Manuskript festrede 40 jahre Kassenblock.pdf",
        "project-history",
        "pdf",
        "Scanned manuscript for a speech marking forty years of Kassenblock.",
        "direct",
        "Historical content; USB scan created 2019-01-28.",
        "Readable 5-page image-only PDF; no text layer.",
        USB
        / "restore/archive/Findbücher/Andere Scans vom Ricoah/Paragon/Manuskript festrede 40 jahre Kassenblock.pdf",
        "sha256:53e5d53df6a6f828b84e87b767d274e3d238d76b235b5a10043994be8f90160e",
        (
            USB
            / "restore/archive/WF/Findbücher/Andere Scans vom Ricoah/Paragon/Manuskript festrede 40 jahre Kassenblock.pdf",
        ),
    ),
    Document(
        "PARAGON-DOC-004",
        "documents/project-history/20190403120539.pdf",
        "project-history",
        "pdf",
        "Scanned structure chart of VEB Kassenblock-, Formular- und Billettdruck.",
        "direct",
        "Historical content undated; USB scan created 2019-04-03.",
        "Readable one-page image-only PDF; no text layer.",
        USB / "restore/archive/Paragon/20190403120539.pdf",
        "sha256:c6ea55e190a13dba32a808b7f5b0ae3e9e80a5a8ad839a0aa3e1fbfcf7947c9d",
    ),
    Document(
        "PARAGON-DOC-005",
        "documents/research-notes/Offizielle Veranstaltungen im VEB Kassenblock.docx",
        "research-notes",
        "docx",
        "Curatorial grouping notes for official events and working-life photographs.",
        "direct",
        "Created and modified 2019-04-10.",
        "Readable DOCX containing two short caption/category notes.",
        USB
        / "restore/archive/Paragon/Offizielle Veranstaltungen im VEB Kassenblock.docx",
        "sha256:20b59948dd5e92dd851027593670ff2bf8b681065c3457e6f511a96023fd9a6e",
    ),
    Document(
        "PARAGON-DOC-006",
        "documents/metadata/Bildtexte Paragon stand 05.02.2020.xlsx",
        "metadata",
        "xlsx",
        "Full Paragon caption and metadata workbook before the museum-digital upload subset was finalized.",
        "direct",
        "Version date 2020-02-05.",
        "Readable XLSX; 276 metadata rows plus the color-code sheet.",
        USB / "restore/archive/Paragon/Bildtexte Paragon stand 05.02.2020.xlsx",
        "sha256:8fbc94fb50c59bdc24ef1311437edd19f525b3afce6a026a4effb61e6dc4fd07",
    ),
    Document(
        "PARAGON-DOC-007",
        "documents/metadata/Paragon_Bildtexte-fertig-für-md_final.csv",
        "metadata",
        "csv",
        "Final machine-readable museum-digital import table from the USB workflow.",
        "direct",
        "Final export modified 2020-02-06.",
        "Readable UTF-8 semicolon-delimited CSV; 224 data rows.",
        USB
        / "restore/archive/WF/Paragon fertig für md/Paragon_Bildtexte-fertig-für-md_final.csv",
        "sha256:8753390d56f4c5bf6c3e86caa7addefeef7ee3af32a5614e22d40bdbbb698436",
    ),
    Document(
        "PARAGON-DOC-008",
        "documents/metadata/Paragon_Bildtexte-fertig-für-md_final.xlsx",
        "metadata",
        "xlsx",
        "Final human-readable museum-digital import workbook from the USB workflow.",
        "direct",
        "Final workbook modified 2020-02-06.",
        "Readable XLSX; 224 data rows, color-code sheet, and image-match sheet.",
        USB
        / "restore/archive/WF/Paragon fertig für md/Paragon_Bildtexte-fertig-für-md_final.xlsx",
        "sha256:abaaf8fcc16bce574fbbf0a8e7bc98c5928b736b72cc2cb432ef41d4b0b9c0ab",
    ),
)

EXCLUDED_INTERMEDIATES = (
    "restore/archive/Paragon/fertig für md/Bildtexte fertig für md.xlsx",
    "restore/archive/WF/Paragon fertig für md/Bildtexte fertig für md.xlsx",
    "restore/archive/WF/Paragon fertig für md/Bildtexte fertig für md_02.xlsx",
    "restore/archive/WF/Paragon fertig für md/Bildtexte fertig für md_03.xlsx",
    "restore/archive/WF/Paragon fertig für md/Bildtexte fertig für md_04.xlsx",
    "restore/archive/WF/Paragon fertig für md/Bildtexte fertig für md_05.csv",
    "restore/archive/WF/Paragon fertig für md/Bildtexte fertig für md_05.xlsx",
    "restore/archive/WF/Paragon fertig für md/Bildtexte fertig für md_06.csv",
    "restore/archive/WF/Paragon fertig für md/Paragon_Bildtexte-fertig-für-md_06.xlsx",
)


def utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            value.update(block)
    return "sha256:" + value.hexdigest()


def ensure_usb_read_only() -> None:
    output = subprocess.run(
        [
            "findmnt",
            "-rn",
            "-T",
            str(USB / "restore/archive/Paragon"),
            "-o",
            "SOURCE,FSTYPE,OPTIONS",
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if not output:
        raise RuntimeError("Elements USB mount not found")
    mount_line = output.splitlines()[0]
    options = mount_line.split(None, 2)[-1].split(",")
    if "ro" not in options or "rw" in options:
        raise RuntimeError(f"Elements USB is not read-only: {mount_line}")


def validate_source(document: Document) -> None:
    if not document.source_path.is_file():
        raise RuntimeError(f"missing source: {document.source_path}")
    if digest(document.source_path) != document.expected_sha256:
        raise RuntimeError(f"source digest changed: {document.source_path}")
    for duplicate in document.duplicate_source_paths:
        if not duplicate.is_file() or digest(duplicate) != document.expected_sha256:
            raise RuntimeError(f"duplicate source differs: {duplicate}")


def validate_format(path: Path, document: Document) -> str:
    if document.format == "pdf":
        result = subprocess.run(
            ["pdfinfo", str(path)],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        pages = next(
            line.split(":", 1)[1].strip()
            for line in result.splitlines()
            if line.startswith("Pages:")
        )
        return f"pdf-readable;pages={pages}"
    if document.format in {"xlsx", "docx"}:
        with zipfile.ZipFile(path) as archive:
            problem = archive.testzip()
            if problem:
                raise RuntimeError(f"damaged ZIP member in {path}: {problem}")
            members = len(archive.infolist())
        return f"zip-readable;members={members}"
    if document.format == "csv":
        with path.open(encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.reader(handle, delimiter=";"))
        if len(rows) != 225 or max(map(len, rows)) != 40:
            raise RuntimeError(f"unexpected final CSV shape: {len(rows)} rows")
        return "utf8-csv-readable;rows=225;columns=40"
    raise RuntimeError(f"unsupported format: {document.format}")


def copy_documents() -> list[dict[str, str | int]]:
    rows: list[dict[str, str | int]] = []
    for document in DOCS:
        validate_source(document)
        destination = STAGE / document.relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(document.source_path, destination)
        if digest(destination) != document.expected_sha256:
            raise RuntimeError(f"copy verification failed: {destination}")
        stat = document.source_path.stat()
        format_check = validate_format(destination, document)
        rows.append(
            {
                "document_id": document.document_id,
                "relative_path": document.relative_path,
                "category": document.category,
                "format": document.format,
                "description": document.description,
                "relation_to_paragon": document.relation,
                "date_note": document.date_note,
                "condition": document.condition,
                "source_kind": "file",
                "source_path": str(document.source_path),
                "duplicate_source_paths": " | ".join(
                    str(path) for path in document.duplicate_source_paths
                ),
                "source_size_bytes": stat.st_size,
                "source_mtime_ns": stat.st_mtime_ns,
                "source_mtime_utc": datetime.fromtimestamp(
                    stat.st_mtime, UTC
                ).isoformat().replace("+00:00", "Z"),
                "final_sha256": document.expected_sha256,
                "size_bytes": destination.stat().st_size,
                "copy_verification": f"exact-byte-copy;{format_check}",
            }
        )
    return rows


def contents_text(rows: list[dict[str, str | int]]) -> str:
    lines = [
        "# Paragon supporting documents",
        "",
        "This is the reviewed table of contents for the Paragon unit. It retains",
        "only confirmed metadata, research, and enterprise-history documents from",
        "the authoritative read-only Elements USB.",
        "",
        "Exact source paths, duplicate paths, timestamps, SHA-256 values, and copy",
        "verification are recorded in `../db-restore/manifests/documents.csv`.",
        "",
        "| ID | File | Description | Date | Source path | Relation | Condition |",
        "|---|---|---|---|---|---|---|",
    ]
    for row in rows:
        values = {
            key: str(row[key]).replace("|", "\\|")
            for key in (
                "document_id",
                "relative_path",
                "description",
                "date_note",
                "source_path",
                "relation_to_paragon",
                "condition",
            )
        }
        lines.append(
            f"| {values['document_id']} | `{values['relative_path']}` | "
            f"{values['description']} | {values['date_note']} | "
            f"`{values['source_path']}` | "
            f"{values['relation_to_paragon']} | {values['condition']} |"
        )
    lines.extend(
        [
            "",
            "## Related package records not duplicated here",
            "",
            "- `../export/md/metadata/md-export.csv` and `.xlsx`: normalized museum-digital submission tables.",
            "- `../export/md/metadata/md-import-xml.zip`: exact USB `csv_xml.zip` import XML package (224 XML files); this is not LIDO.",
            "- `../CATALOG-HUMAN.csv` and `../CATALOG.xlsx`: current readable archive catalogs.",
            "- `../db-restore/`: authoritative machine-restorable catalog and manifests.",
            "",
            "## Selection boundary",
            "",
            "Nine intermediate upload paths, including an exact duplicate workbook",
            "pair, were excluded because the full pre-upload workbook and the",
            "final CSV/XLSX pair are retained. Exact duplicate copies of the three",
            "history PDFs under the USB `WF/` branch are recorded in the manifest but",
            "not retained twice. The previous all-in-one source ZIP and generated",
            "catalog/export duplicates were removed from the final document section;",
            "their pre-correction state is recoverable from the accession receipt.",
            "",
        ]
    )
    return "\n".join(lines)


def write_csv(path: Path, rows: list[dict], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="raise")
        writer.writeheader()
        writer.writerows(rows)


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def updated_readme() -> str:
    current = README.read_text(encoding="utf-8")
    old = "- `documents/`: source and historical export evidence."
    new = (
        "- `documents/`: eight reviewed USB-source metadata, research, and "
        "enterprise-history documents indexed by `documents/CONTENTS.md`."
    )
    if old not in current:
        raise RuntimeError("README document description changed unexpectedly")
    return current.replace(old, new)


def stage_controls(document_rows: list[dict[str, str | int]]) -> None:
    contents = STAGE / "documents/CONTENTS.md"
    contents.parent.mkdir(parents=True, exist_ok=True)
    contents.write_text(contents_text(document_rows), encoding="utf-8")

    manifest = STAGE / "db-restore/manifests/documents.csv"
    write_csv(manifest, document_rows, MANIFEST_FIELDS)

    readme = STAGE / "README.md"
    readme.write_text(updated_readme(), encoding="utf-8")

    old_checksums = read_csv(CHECKSUMS)
    kept = [
        row
        for row in old_checksums
        if not row["path"].startswith("documents/")
        and row["path"] not in {"README.md", "db-restore/manifests/documents.csv"}
    ]
    additions = []
    for path in sorted((STAGE / "documents").rglob("*")):
        if path.is_file():
            additions.append(
                {
                    "path": path.relative_to(STAGE).as_posix(),
                    "sha256": digest(path),
                    "size_bytes": path.stat().st_size,
                }
            )
    for relative, path in (
        ("README.md", readme),
        ("db-restore/manifests/documents.csv", manifest),
    ):
        additions.append(
            {
                "path": relative,
                "sha256": digest(path),
                "size_bytes": path.stat().st_size,
            }
        )
    combined = sorted(kept + additions, key=lambda row: row["path"])
    if len({row["path"] for row in combined}) != len(combined):
        raise RuntimeError("duplicate checksum paths")
    write_csv(
        STAGE / "db-restore/checksums.csv",
        combined,
        ["path", "sha256", "size_bytes"],
    )


def backup_current() -> None:
    if BEFORE.exists() or RETIRED.exists():
        raise RuntimeError("run already has backup state")
    shutil.copytree(DOCUMENTS, BEFORE / "documents", copy_function=shutil.copy2)
    for source in (DOCUMENTS_MANIFEST, CHECKSUMS, README):
        destination = BEFORE / source.relative_to(FINAL)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)


def install_file(staged: Path, final: Path) -> None:
    temporary = final.with_name(final.name + ".paragon-docs-new")
    if temporary.exists():
        raise RuntimeError(f"temporary path exists: {temporary}")
    shutil.copy2(staged, temporary)
    os.replace(temporary, final)


def rollback() -> None:
    if DOCUMENTS.exists():
        shutil.rmtree(DOCUMENTS)
    if RETIRED.exists():
        shutil.move(str(RETIRED), str(DOCUMENTS))
    for relative in (
        "db-restore/manifests/documents.csv",
        "README.md",
        "db-restore/checksums.csv",
    ):
        source = BEFORE / relative
        if source.is_file():
            shutil.copy2(source, FINAL / relative)


def promote() -> None:
    shutil.move(str(DOCUMENTS), str(RETIRED))
    try:
        shutil.copytree(STAGE / "documents", DOCUMENTS, copy_function=shutil.copy2)
        install_file(STAGE / "db-restore/manifests/documents.csv", DOCUMENTS_MANIFEST)
        install_file(STAGE / "README.md", README)
        install_file(STAGE / "db-restore/checksums.csv", CHECKSUMS)
    except Exception:
        rollback()
        raise


def validate_final(rows: list[dict[str, str | int]]) -> dict:
    expected_document_paths = {
        Path(row["relative_path"]).relative_to("documents").as_posix()
        for row in rows
    } | {"CONTENTS.md"}
    actual_document_paths = {
        path.relative_to(DOCUMENTS).as_posix()
        for path in DOCUMENTS.rglob("*")
        if path.is_file()
    }
    if actual_document_paths != expected_document_paths:
        raise RuntimeError(
            f"unexpected final documents: {sorted(actual_document_paths ^ expected_document_paths)}"
        )

    manifest_rows = read_csv(DOCUMENTS_MANIFEST)
    if len(manifest_rows) != len(DOCS):
        raise RuntimeError("documents manifest row count mismatch")
    for document in DOCS:
        final_path = FINAL / document.relative_path
        if digest(final_path) != document.expected_sha256:
            raise RuntimeError(f"final document digest mismatch: {final_path}")
        validate_format(final_path, document)
        validate_source(document)

    checksum_rows = read_csv(CHECKSUMS)
    checksum_map = {row["path"]: row for row in checksum_rows}
    for relative in [
        *(f"documents/{path}" for path in sorted(actual_document_paths)),
        "README.md",
        "db-restore/manifests/documents.csv",
    ]:
        path = FINAL / relative
        row = checksum_map.get(relative)
        if (
            not row
            or row["sha256"] != digest(path)
            or int(row["size_bytes"]) != path.stat().st_size
        ):
            raise RuntimeError(f"checksum control mismatch: {relative}")

    export_zip = FINAL / "export/md/metadata/md-import-xml.zip"
    usb_zip = USB / "restore/archive/WF/Paragon fertig für md/csv_xml.zip"
    if digest(export_zip) != digest(usb_zip):
        raise RuntimeError("museum-digital XML export no longer matches USB source")
    with zipfile.ZipFile(export_zip) as archive:
        if archive.testzip() or len(archive.infolist()) != 224:
            raise RuntimeError("museum-digital XML export validation failed")

    connection = sqlite3.connect(f"file:{RESTORE}?mode=ro", uri=True)
    try:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        foreign_keys = len(connection.execute("PRAGMA foreign_key_check").fetchall())
        state = connection.execute(
            "SELECT catalog_revision,published_revision,dirty_since "
            "FROM catalog_state WHERE singleton=1"
        ).fetchone()
    finally:
        connection.close()
    if integrity != "ok" or foreign_keys or state != (9976, 9976, None):
        raise RuntimeError("restore catalog changed during documentation correction")
    ensure_usb_read_only()
    return {
        "document_files": len(DOCS),
        "contents_present": True,
        "documents_manifest_rows": len(manifest_rows),
        "checksum_rows": len(checksum_rows),
        "restore_integrity": integrity,
        "restore_foreign_key_violations": foreign_keys,
        "catalog_state": list(state),
        "usb_read_only": True,
        "source_deleted": False,
        "deletion_authorized": False,
    }


def main() -> int:
    if STAGE.exists() or BEFORE.exists() or RETIRED.exists() or RECEIPT.exists():
        raise RuntimeError("documentation correction run already executed")
    ensure_usb_read_only()
    for document in DOCS:
        validate_source(document)
    backup_current()
    rows = copy_documents()
    stage_controls(rows)
    promote()
    try:
        validation = validate_final(rows)
    except Exception:
        rollback()
        raise
    receipt = {
        "format": "iss-paragon-documentation-accession-receipt-v1",
        "completed_at": utc_now(),
        "authority": str(USB),
        "final_unit": str(FINAL),
        "documents": rows,
        "excluded_intermediate_paths": [
            str(USB / relative) for relative in EXCLUDED_INTERMEDIATES
        ],
        "related_not_duplicated": [
            "export/md/metadata/md-export.csv",
            "export/md/metadata/md-export.xlsx",
            "export/md/metadata/md-import-xml.zip",
            "CATALOG-HUMAN.csv",
            "CATALOG.xlsx",
            "db-restore/RESTORE.sqlite",
        ],
        "recoverable_previous_documents": str(RETIRED),
        "validation": validation,
    }
    RECEIPT.write_text(
        json.dumps(receipt, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(receipt["validation"], sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
