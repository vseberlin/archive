#!/usr/bin/env python3
"""Prepare corrected TFA document and metadata packages without accessioning them."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import sqlite3
import subprocess
import tempfile
import zipfile
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path


WORK_ROOT = Path("/mnt/volume/iss-archive-work/tfa/work")
REVIEW_ROOT = WORK_ROOT / "reconciliation/unit-reconciliation"
SOURCE_ROOT = Path("/media/vladimir/Elements")
INDEX_DB = Path("/mnt/volume/iss-archive-work/document-index/document-index.sqlite")
OLD_PLAN = REVIEW_ROOT / "tfa-document-accession-plan.csv"
OLD_DISPOSITION = REVIEW_ROOT / "tfa-document-source-disposition-proposal.csv"
DOCUMENT_CANDIDATES = REVIEW_ROOT / "tfa-document-candidates.csv"

DOC_PLAN = REVIEW_ROOT / "tfa-document-accession-plan.corrected.csv"
DOC_DISPOSITION = REVIEW_ROOT / "tfa-document-source-disposition-proposal.corrected.csv"
DOC_CONTENTS = REVIEW_ROOT / "tfa-document-contents.corrected.md"
DOC_STAGE = REVIEW_ROOT / "tfa-document-accession-stage.corrected.zip"
DOC_SUMMARY = REVIEW_ROOT / "tfa-document-accession-preparation.corrected.json"

META_PLAN = REVIEW_ROOT / "tfa-metadata-accession-plan.csv"
META_DISPOSITION = REVIEW_ROOT / "tfa-metadata-source-disposition-proposal.csv"
META_CONTENTS = REVIEW_ROOT / "tfa-metadata-contents.prepared.md"
META_STAGE = REVIEW_ROOT / "tfa-metadata-accession-stage.zip"
META_SUMMARY = REVIEW_ROOT / "tfa-metadata-accession-preparation.json"

CURRENT_CAPTURE = WORK_ROOT / "reconciliation/museum-digital/runs/20260814T200634Z-tfa-collection-857-local-only-recheck"
PRIOR_CAPTURE = WORK_ROOT / "reconciliation/museum-digital/runs/20260810T071711Z-tfa-collection-857"
THESAURUS_PREFIX = "/media/vladimir/Elements/restore/archive/WF/Thesaurus/"
IMAGE_ZIP_MARKERS = (
    "wetransfer-603375.zip",
    "wetransfer-dbd63b.zip",
    "digis_1954_jpgs.zip",
)
HISTORICAL_ARCHIVE_ZIPS = (
    Path("/media/vladimir/Elements/restore/archive/WF/TFA_Export_29_20200212_lido.zip"),
    Path("/media/vladimir/Elements/restore/archive/WF/export_29_20200212_generisch.zip"),
    Path("/media/vladimir/Elements/restore/archive/WF/export_Menschen-md-minimal_29_20200218_generisch.zip"),
    Path("/media/vladimir/Elements/restore/archive/WF/md_export_29_20190925_lido.zip"),
)
LOOSE_DUPLICATE_EXPORTS = (
    (
        Path("/media/vladimir/Elements/restore/archive/WF/TFA_Export_29_20200212_lido"),
        HISTORICAL_ARCHIVE_ZIPS[0],
    ),
    (
        Path("/media/vladimir/Elements/restore/archive/WF/export_29_20200212_generisch"),
        HISTORICAL_ARCHIVE_ZIPS[1],
    ),
)
LOOSE_ONLY_EXPORTS = (
    Path("/media/vladimir/Elements/restore/archive/WF/Archiv/export_29_20191120_lido"),
    Path("/media/vladimir/Elements/restore/archive/export_Menschen-im-WF_18-02-2020_29_20200218_lido"),
)
DDB_BLANK = Path("/media/vladimir/Elements/restore/archive/WF/Digis/ddb_kooperationsvertrag_2017_v2.1_2017-07-26.pdf")
DDB_FILLED = Path("/media/vladimir/Elements/restore/archive/WF/Digis/ddb_kooperationsvertrag_2017_v2.1_2017-07-26_ausgefüllt.pdf")
EXTRA_WORKING_METADATA = (
    Path("/media/vladimir/Elements/restore/archive/WF/Mustertabelle_fuer_museum-digital-Upload.xlsx"),
    Path("/media/vladimir/Elements/restore/archive/WF/Steffens-Arbeitsliste_alle-md-Texte_Schlagwörter_31-01-2020_02.xlsx"),
    Path("/media/vladimir/Elements/restore/archive/WF/mdExcel_TFA1.xlsx"),
    Path("/media/vladimir/Elements/restore/archive/WF/mdExcel_TFA2.xlsx"),
)
METADATA_ONLY_PATHS = frozenset(str(path) for path in HISTORICAL_ARCHIVE_ZIPS + EXTRA_WORKING_METADATA)

DOC_FIELDS = (
    "document_id", "source_path", "filename", "extension", "size_bytes", "source_sha256",
    "evidence_confidence", "extraction_state", "proposed_decision", "classification",
    "final_relative_path", "condition", "selection_basis", "deletion_authorized",
)
META_FIELDS = (
    "metadata_id", "source_path", "filename", "extension", "size_bytes", "source_sha256",
    "category", "evidence_confidence", "extraction_state", "parser", "matched_tfa_inventory_ids",
    "evidence_basis", "evidence_excerpt", "proposed_decision", "final_relative_path", "condition",
    "selection_basis", "deletion_authorized",
)
DISPOSITION_FIELDS = (
    "source_path", "source_kind", "unit", "inventory", "source_role", "final_path",
    "preservation_status", "preservation_basis", "source_sha256", "final_sha256",
    "source_size_bytes", "exact_copy_proven", "drain_state", "deletion_authorized", "notes",
)


class PrepareError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def csv_bytes(rows: list[dict[str, object]], fields: tuple[str, ...]) -> bytes:
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore", quoting=csv.QUOTE_ALL)
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue().encode("utf-8")


def atomic_write(path: Path, payload: bytes) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(payload)
    temporary.replace(path)


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(8 * 1024 * 1024):
            value.update(block)
    return value.hexdigest()


def metadata_id(path: str) -> str:
    return "META-" + hashlib.sha256(path.encode("utf-8")).hexdigest()[:32].upper()


def mount_state() -> dict[str, str]:
    result = subprocess.run(["findmnt", "-J", "-T", str(SOURCE_ROOT)], capture_output=True, text=True)
    if result.returncode:
        raise PrepareError("Elements mount cannot be resolved")
    filesystems = json.loads(result.stdout).get("filesystems", [])
    if len(filesystems) != 1:
        raise PrepareError("Elements mount resolution is ambiguous")
    mount = filesystems[0]
    options = set(str(mount.get("options", "")).split(","))
    if mount.get("fstype") != "ntfs3" or "ro" not in options:
        raise PrepareError(f"Elements is not protected ntfs3 read-only: {mount}")
    return {key: str(mount.get(key, "")) for key in ("source", "target", "fstype", "options")}


def require_boundaries() -> None:
    work = WORK_ROOT.resolve(strict=True)
    review = REVIEW_ROOT.resolve(strict=True)
    review.relative_to(work)
    outputs = (DOC_PLAN, DOC_DISPOSITION, DOC_CONTENTS, DOC_STAGE, DOC_SUMMARY,
               META_PLAN, META_DISPOSITION, META_CONTENTS, META_STAGE, META_SUMMARY)
    for path in outputs:
        if path.parent.resolve(strict=True) != review:
            raise PrepareError(f"output escapes approved work root: {path}")
        if path.exists():
            raise PrepareError(f"preserving existing output; refusing overwrite: {path}")


def index_connection() -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{INDEX_DB}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    status = connection.execute("SELECT value FROM meta WHERE key='extraction_status'").fetchone()
    if status is None or status[0] != "complete" or connection.execute("PRAGMA foreign_key_check").fetchall():
        raise PrepareError("document index is not complete and clean")
    return connection


def indexed_rows(connection: sqlite3.Connection, paths: list[str]) -> dict[str, dict[str, object]]:
    result: dict[str, dict[str, object]] = {}
    for start in range(0, len(paths), 400):
        batch = paths[start:start + 400]
        marks = ",".join("?" for _ in batch)
        for row in connection.execute(
            f"SELECT * FROM documents WHERE source_path IN ({marks})", batch
        ):
            result[row["source_path"]] = dict(row)
    return result


def validate_indexed(row: dict[str, object]) -> None:
    if row["observed_state"] != "present":
        raise PrepareError(f"indexed source is not ready: {row['source_path']}")
    path = Path(str(row["source_path"])).resolve(strict=True)
    path.relative_to(SOURCE_ROOT.resolve(strict=True))
    stat = path.stat()
    if stat.st_size != row["size_bytes"] or stat.st_mtime_ns != row["mtime_ns"]:
        raise PrepareError(f"source changed after indexing: {path}")
    indexed_hash = str(row["content_sha256"])
    if len(indexed_hash) != 64:
        if row.get("extraction_state") != "skipped_known_unit":
            raise PrepareError(f"indexed source lacks a hash: {path}")
        row["content_sha256"] = digest(path)


def source_disposition(row: dict[str, str], *, role: str, final: str, status: str,
                       basis: str, exact: bool = False, drain: str = "pending_human_confirmation") -> dict[str, str]:
    return {
        "source_path": row["source_path"], "source_kind": "file", "unit": "tfa", "inventory": "",
        "source_role": role, "final_path": final, "preservation_status": status,
        "preservation_basis": basis, "source_sha256": "sha256:" + row["source_sha256"],
        "final_sha256": "", "source_size_bytes": row["size_bytes"],
        "exact_copy_proven": str(exact).lower(), "drain_state": drain,
        "deletion_authorized": "false",
        "notes": "Preparation only; source remains unchanged and deletion is not authorized.",
    }


def document_kind(path: str) -> str:
    lower = path.casefold()
    if path in METADATA_ONLY_PATHS:
        return "metadata"
    if THESAURUS_PREFIX.casefold() not in lower:
        return "document"
    extension = Path(path).suffix.casefold()
    if extension in {".csv", ".xlsx", ".xml", ".txt"}:
        return "metadata"
    if extension == ".zip":
        return "media" if any(marker in lower for marker in IMAGE_ZIP_MARKERS) else "metadata"
    return "document"


def build_corrected_documents(connection: sqlite3.Connection) -> tuple[list[dict[str, str]], list[dict[str, str]], dict[str, object]]:
    plan = [row for row in read_csv(OLD_PLAN) if document_kind(row["source_path"]) == "document"]
    disposition = [row for row in read_csv(OLD_DISPOSITION) if document_kind(row["source_path"]) == "document"]
    additions = indexed_rows(connection, [str(DDB_BLANK), str(DDB_FILLED)])
    for path, classification, final, basis in (
        (DDB_FILLED, "restricted_contracts", "documents/restricted/contracts.zip",
         "Filled Industriesalon DDB cooperation agreement; direct TFA institutional project evidence"),
        (DDB_BLANK, "historical", "documents/historical/drafts-and-superseded.zip",
         "Blank DDB agreement template retained as project context"),
    ):
        indexed = additions.get(str(path))
        if indexed is None:
            raise PrepareError(f"DDB document is absent from the completed index: {path}")
        validate_indexed(indexed)
        row = {
            "document_id": "DOC-" + hashlib.sha256(str(path).encode()).hexdigest()[:32].upper(),
            "source_path": str(path), "filename": path.name, "extension": path.suffix.casefold(),
            "size_bytes": str(indexed["size_bytes"]), "source_sha256": str(indexed["content_sha256"]),
            "evidence_confidence": "strong", "extraction_state": str(indexed["extraction_state"]),
            "proposed_decision": "proposed_accession", "classification": classification,
            "final_relative_path": final, "condition": "indexed_and_readable",
            "selection_basis": basis + "; human confirmation still required", "deletion_authorized": "false",
        }
        plan.append(row)
        disposition.append(source_disposition(
            row, role=classification, final=final, status="proposed_accession",
            basis=row["selection_basis"], exact=False, drain="pending_accession_confirmation",
        ))
    plan.sort(key=lambda row: (row["proposed_decision"], row["classification"], row["source_path"]))
    disposition.sort(key=lambda row: row["source_path"])
    if len({row["source_path"] for row in disposition}) != len(disposition):
        raise PrepareError("corrected document disposition contains duplicate source paths")
    selected = [row for row in plan if row["proposed_decision"] == "proposed_accession"]
    review_ids = {
        row["document_id"] for row in plan
        if row["evidence_confidence"] == "ambiguous" or row["extraction_state"] == "error"
    }
    summary = {
        "format": "iss-tfa-documentation-accession-preparation-v2", "created_at": utc_now(),
        "status": "ready_for_human_confirmation", "source_read_only": True, "source_modified": False,
        "published_tfa_unit_changed": False, "final_hashes_generated": False,
        "deletion_authorized": False, "proposed_accession_sources": len(selected),
        "proposed_accession_bytes": sum(int(row["size_bytes"]) for row in selected),
        "proposed_classifications": dict(sorted(Counter(row["classification"] for row in selected).items())),
        "proposed_exact_redundant_copies": sum(row["preservation_status"] == "proposed_redundant_exact_copy" for row in disposition),
        "reviewed_not_proposed": len(plan) - len(selected), "source_disposition_rows": len(disposition),
        "document_review_scope": len(review_ids),
        "candidate_input_sha256": digest(DOCUMENT_CANDIDATES), "stage_path": str(DOC_STAGE),
    }
    return plan, disposition, summary


def zip_manifest(rows: list[dict[str, str]], id_field: str) -> bytes:
    fields = ("member", id_field, "original_filename", "source_path", "source_sha256", "size_bytes",
              "condition", "category", "deletion_authorized")
    manifest = []
    for number, row in enumerate(sorted(rows, key=lambda value: value["source_path"]), 1):
        manifest.append({
            "member": f"{number:05d}-{Path(row['source_path']).name}", id_field: row[id_field],
            "original_filename": Path(row["source_path"]).name, "source_path": row["source_path"],
            "source_sha256": "sha256:" + row["source_sha256"], "size_bytes": row["size_bytes"],
            "condition": row.get("condition", ""), "category": row.get("category", row.get("classification", "")),
            "deletion_authorized": "false",
        })
    return csv_bytes(manifest, fields)


def build_simple_inner(path: Path, rows: list[dict[str, str]], id_field: str, title: str) -> None:
    ordered = sorted(rows, key=lambda row: row["source_path"])
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
        for number, row in enumerate(ordered, 1):
            archive.write(row["source_path"], f"{number:05d}-{Path(row['source_path']).name}")
        archive.writestr("MANIFEST.csv", zip_manifest(ordered, id_field))
        archive.writestr("README.md", (
            f"# {title}\n\nExact source bytes with original paths and indexed hashes are listed in MANIFEST.csv. "
            "This is preparation evidence, not accession or deletion authority.\n"
        ).encode())
    with zipfile.ZipFile(path) as archive:
        if bad := archive.testzip():
            raise PrepareError(f"ZIP CRC failure in {path}: {bad}")


def build_document_stage(plan: list[dict[str, str]], disposition: list[dict[str, str]], summary: dict[str, object]) -> str:
    selected = [row for row in plan if row["proposed_decision"] == "proposed_accession"]
    grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in selected:
        grouped[row["classification"]].append(row)
    contents = "\n".join((
        "# TFA documentation accession preparation (corrected)", "",
        "Metadata tables and MD/LIDO/XML/CSV export evidence are excluded from this documentation package.",
        "The filled DDB cooperation agreement is proposed for restricted contracts; the blank template is historical context.",
        "No source file is changed and no deletion is authorized.", "",
        f"- Proposed source documents: {len(selected)}",
        f"- Project-final files: {len(grouped['project_final'])}",
        f"- Restricted contracts: {len(grouped['restricted_contracts'])}",
        f"- Restricted finance: {len(grouped['restricted_finance'])}",
        f"- Restricted personal: {len(grouped['restricted_personal'])}",
        f"- Historical documents: {len(grouped['historical'])}",
        f"- Exact-copy suggestions: {summary['proposed_exact_redundant_copies']}",
        "- Final hashes are deferred until accession.", "",
    ))
    temporary: dict[str, Path] = {}
    stage_temp = DOC_STAGE.with_suffix(".zip.tmp")
    try:
        for classification in ("restricted_contracts", "restricted_finance", "restricted_personal", "historical"):
            if not grouped[classification]:
                continue
            handle = tempfile.NamedTemporaryFile(prefix=".tfa-doc-", suffix=".zip", dir=REVIEW_ROOT, delete=False)
            handle.close(); temporary[classification] = Path(handle.name)
            build_simple_inner(temporary[classification], grouped[classification], "document_id", classification)
        with zipfile.ZipFile(stage_temp, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
            for row in grouped["project_final"]:
                archive.write(row["source_path"], row["final_relative_path"])
            for classification, package in temporary.items():
                final = "documents/historical/drafts-and-superseded.zip" if classification == "historical" else "documents/restricted/" + classification.removeprefix("restricted_") + ".zip"
                archive.write(package, final)
            archive.writestr("documents/CONTENTS.md", contents.encode())
            archive.writestr("db-restore/manifests/documents.csv", csv_bytes(plan, DOC_FIELDS))
            archive.writestr("db-restore/manifests/source-disposition.csv", csv_bytes(disposition, DISPOSITION_FIELDS))
            archive.writestr("PREPARATION.json", json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=2).encode() + b"\n")
        with zipfile.ZipFile(stage_temp) as archive:
            if bad := archive.testzip():
                raise PrepareError(f"corrected document stage CRC failure: {bad}")
        stage_temp.replace(DOC_STAGE)
    finally:
        for path in temporary.values(): path.unlink(missing_ok=True)
        stage_temp.unlink(missing_ok=True)
    return contents


def candidate_evidence() -> dict[str, dict[str, str]]:
    return {row["source_path"]: row for row in read_csv(DOCUMENT_CANDIDATES)}


def metadata_row(indexed: dict[str, object], category: str, final: str, condition: str,
                 basis: str, evidence: dict[str, dict[str, str]]) -> dict[str, str]:
    source_path = str(indexed["source_path"])
    candidate = evidence.get(source_path, {})
    return {
        "metadata_id": metadata_id(source_path), "source_path": source_path,
        "filename": str(indexed["filename"]), "extension": str(indexed["extension"]).casefold(),
        "size_bytes": str(indexed["size_bytes"]), "source_sha256": str(indexed["content_sha256"]),
        "category": category, "evidence_confidence": candidate.get("evidence_confidence", "strong"),
        "extraction_state": str(indexed["extraction_state"]), "parser": str(indexed["parser"]),
        "matched_tfa_inventory_ids": candidate.get("matched_tfa_inventory_ids", ""),
        "evidence_basis": candidate.get("evidence_basis", basis),
        "evidence_excerpt": candidate.get("evidence_excerpt", ""),
        "proposed_decision": "proposed_accession", "final_relative_path": final,
        "condition": condition, "selection_basis": basis + "; human confirmation still required",
        "deletion_authorized": "false",
    }


def current_capture_rows() -> list[dict[str, str]]:
    coverage = json.loads((CURRENT_CAPTURE / "coverage.json").read_text(encoding="utf-8"))
    object_count = coverage.get("unique_objects", coverage.get("captured_objects", coverage.get("objects", 0)))
    if int(object_count) != 5282 or int(coverage.get("reported_total", 0)) != 5282 or coverage.get("request_failures", []) not in ([], 0, None):
        raise PrepareError(f"current MD capture coverage is not complete: {coverage}")
    rows = []
    for path in sorted(item for item in CURRENT_CAPTURE.rglob("*") if item.is_file()):
        path.resolve().relative_to(WORK_ROOT.resolve(strict=True))
        rows.append({
            "metadata_id": metadata_id(str(path)), "source_path": str(path), "filename": path.name,
            "extension": path.suffix.casefold(), "size_bytes": str(path.stat().st_size), "source_sha256": digest(path),
            "category": "museum_digital_current", "evidence_confidence": "strong", "extraction_state": "captured",
            "parser": "museum-digital-json-capture", "matched_tfa_inventory_ids": "", "evidence_basis": "complete collection 857 capture with 5,282 unique objects",
            "evidence_excerpt": "", "proposed_decision": "proposed_accession",
            "final_relative_path": "metadata/museum-digital-current.zip", "condition": "complete_api_capture",
            "selection_basis": "latest complete collection 857 raw JSON capture; human confirmation still required",
            "deletion_authorized": "false",
        })
    if len(rows) != 57:
        raise PrepareError(f"current MD capture file count drifted: {len(rows)}/57")
    return rows


def historical_rows(connection: sqlite3.Connection, evidence: dict[str, dict[str, str]]) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    selected_paths = [str(path) for path in HISTORICAL_ARCHIVE_ZIPS]
    for directory in LOOSE_ONLY_EXPORTS:
        selected_paths.extend(str(path) for path in sorted(directory.iterdir()) if path.is_file())
    indexed = indexed_rows(connection, selected_paths)
    if len(indexed) != len(selected_paths):
        missing = sorted(set(selected_paths) - set(indexed))[:5]
        raise PrepareError(f"historical export files absent from document index: {missing}")
    selected = []
    for path in selected_paths:
        row = indexed[path]; validate_indexed(row)
        if Path(path).suffix.casefold() == ".zip":
            with zipfile.ZipFile(path) as archive:
                if bad := archive.testzip(): raise PrepareError(f"historical export ZIP CRC failure: {path}: {bad}")
            category, condition = "historical_md_export_archive", "original_export_zip_crc_ok"
        else:
            category, condition = "historical_lido_snapshot", "indexed_exact_source_bytes"
        selected.append(metadata_row(row, category, "metadata/museum-digital-historical-exports.zip", condition,
                                     "historical museum-digital export snapshot", evidence))
    duplicate_rows: list[dict[str, str]] = []
    for directory, archive_path in LOOSE_DUPLICATE_EXPORTS:
        paths = [str(path) for path in sorted(directory.iterdir()) if path.is_file()]
        loose_index = indexed_rows(connection, paths)
        if len(loose_index) != len(paths):
            raise PrepareError(f"loose export is not fully indexed: {directory}")
        with zipfile.ZipFile(archive_path) as archive:
            members = {Path(info.filename).name: info for info in archive.infolist() if not info.is_dir()}
            for path in paths:
                row = loose_index[path]; validate_indexed(row)
                info = members.get(Path(path).name)
                if info is None:
                    raise PrepareError(f"loose export member absent from ZIP: {path}")
                value = hashlib.sha256()
                with archive.open(info) as handle:
                    while block := handle.read(1024 * 1024): value.update(block)
                if value.hexdigest() != row["content_sha256"]:
                    raise PrepareError(f"loose export differs from ZIP member: {path}")
                duplicate_rows.append({
                    "source_path": path, "source_sha256": str(row["content_sha256"]),
                    "size_bytes": str(row["size_bytes"]),
                })
    return selected, duplicate_rows


def working_metadata_rows(connection: sqlite3.Connection, evidence: dict[str, dict[str, str]]) -> tuple[list[dict[str, str]], list[dict[str, str]], list[dict[str, object]]]:
    rows = [dict(row) for row in connection.execute(
        """SELECT * FROM documents WHERE source_path LIKE ? AND observed_state='present'
           AND lower(extension) IN ('.csv','.xlsx','.xml','.zip','.txt') ORDER BY source_path""",
        (THESAURUS_PREFIX + "%",),
    )]
    extra = indexed_rows(connection, [str(path) for path in EXTRA_WORKING_METADATA])
    if len(extra) != len(EXTRA_WORKING_METADATA):
        raise PrepareError("one or more explicit TFA museum-digital working spreadsheets are absent from the index")
    rows.extend(extra[str(path)] for path in EXTRA_WORKING_METADATA)
    eligible: list[dict[str, object]] = []
    excluded: list[dict[str, object]] = []
    for row in rows:
        lower = str(row["source_path"]).casefold()
        if str(row["extension"]).casefold() == ".zip" and any(marker in lower for marker in IMAGE_ZIP_MARKERS):
            excluded.append({**row, "exclusion": "image_container_not_metadata"}); continue
        name = Path(str(row["source_path"])).name
        if int(row["size_bytes"]) < 500 and name.startswith(("~", "�")):
            excluded.append({**row, "exclusion": "office_temporary_file"}); continue
        validate_indexed(row); eligible.append(row)
    by_hash: dict[str, list[dict[str, object]]] = defaultdict(list)
    for row in eligible: by_hash[str(row["content_sha256"])].append(row)
    selected: list[dict[str, str]] = []
    duplicates: list[dict[str, str]] = []
    for group in by_hash.values():
        group.sort(key=lambda row: (
            "/archiv/" in str(row["source_path"]).casefold(),
            "kopie" in str(row["source_path"]).casefold(), len(str(row["source_path"])), str(row["source_path"]),
        ))
        representative = group[0]
        extension = str(representative["extension"]).casefold()
        condition = "malformed_first_xml_element_preserve_exact_bytes" if extension == ".xml" else (
            "parser_error_preserve_exact_bytes" if representative["extraction_state"] == "error" else "indexed_exact_source_bytes"
        )
        selected.append(metadata_row(
            representative, "tfa_working_metadata", "metadata/tfa-working-metadata.zip", condition,
            "WF/Thesaurus historical TFA metadata and import evidence", evidence,
        ))
        for duplicate in group[1:]:
            duplicates.append({"source_path": str(duplicate["source_path"]),
                               "source_sha256": str(duplicate["content_sha256"]),
                               "size_bytes": str(duplicate["size_bytes"]),
                               "representative_path": str(representative["source_path"])})
    selected.sort(key=lambda row: row["source_path"])
    duplicates.sort(key=lambda row: row["source_path"])
    return selected, duplicates, excluded


def build_metadata(connection: sqlite3.Connection) -> tuple[list[dict[str, str]], list[dict[str, str]], dict[str, object], str]:
    evidence = candidate_evidence()
    current = current_capture_rows()
    historical, loose_duplicates = historical_rows(connection, evidence)
    working, working_duplicates, excluded = working_metadata_rows(connection, evidence)
    plan = sorted(current + historical + working, key=lambda row: (row["category"], row["source_path"]))
    disposition: list[dict[str, str]] = []
    for row in plan:
        disposition.append(source_disposition(
            row, role=row["category"], final=row["final_relative_path"], status="proposed_accession",
            basis=row["selection_basis"], drain="pending_accession_confirmation",
        ))
    for row in loose_duplicates:
        disposition.append(source_disposition(
            row, role="exact_loose_export_copy", final="metadata/museum-digital-historical-exports.zip",
            status="proposed_redundant_exact_copy",
            basis="SHA-256 equals the corresponding member in the retained original export ZIP",
            exact=True,
        ))
    for row in working_duplicates:
        disposition.append(source_disposition(
            row, role="exact_working_metadata_copy", final="metadata/tfa-working-metadata.zip",
            status="proposed_redundant_exact_copy",
            basis="SHA-256 equals selected metadata representative " + row["representative_path"], exact=True,
        ))
    current_by_relative = {Path(row["source_path"]).relative_to(CURRENT_CAPTURE): row for row in current}
    for path in sorted(item for item in PRIOR_CAPTURE.rglob("*") if item.is_file()):
        relative = path.relative_to(PRIOR_CAPTURE); sha = digest(path)
        current_row = current_by_relative.get(relative)
        exact = current_row is not None and current_row["source_sha256"] == sha
        prior = {"source_path": str(path), "source_sha256": sha, "size_bytes": str(path.stat().st_size)}
        disposition.append(source_disposition(
            prior, role="prior_museum_digital_capture", final="metadata/museum-digital-current.zip" if exact else "",
            status="proposed_redundant_exact_copy" if exact else "reviewed_not_accessioned_prior_capture_receipt",
            basis="Exact duplicate of selected current capture file" if exact else "Prior capture receipt differs and remains preserved in work storage",
            exact=exact, drain="pending_human_confirmation" if exact else "retained_work_evidence",
        ))
    for row in excluded:
        disposition.append({
            "source_path": str(row["source_path"]), "source_kind": "file", "unit": "tfa", "inventory": "",
            "source_role": str(row["exclusion"]), "final_path": "", "preservation_status": "excluded_from_metadata_package",
            "preservation_basis": "Media container or technical temporary file; not metadata evidence",
            "source_sha256": "sha256:" + str(row["content_sha256"]), "final_sha256": "",
            "source_size_bytes": str(row["size_bytes"]), "exact_copy_proven": "false",
            "drain_state": "not_assessed", "deletion_authorized": "false",
            "notes": "Excluded from this package only; source remains unchanged.",
        })
    disposition.sort(key=lambda row: row["source_path"])
    if len({row["source_path"] for row in disposition}) != len(disposition):
        raise PrepareError("metadata disposition contains duplicate source paths")
    review_scope = sum(row["evidence_confidence"] == "ambiguous" for row in plan)
    summary = {
        "format": "iss-tfa-metadata-accession-preparation-v1", "created_at": utc_now(),
        "status": "ready_for_human_confirmation", "authority": "museum-digital current collection 857 capture",
        "source_read_only": True, "source_modified": False, "published_tfa_unit_changed": False,
        "final_hashes_generated": False, "deletion_authorized": False,
        "proposed_accession_sources": len(plan), "proposed_accession_bytes": sum(int(row["size_bytes"]) for row in plan),
        "proposed_categories": dict(sorted(Counter(row["category"] for row in plan).items())),
        "metadata_review_scope": review_scope,
        "proposed_exact_redundant_copies": sum(row["preservation_status"] == "proposed_redundant_exact_copy" for row in disposition),
        "source_disposition_rows": len(disposition), "excluded_nonmetadata_or_technical_paths": len(excluded),
        "current_md_objects": 5282, "ddb_object_export_found": False,
        "ddb_agreement_handled_in_corrected_documents": True, "stage_path": str(META_STAGE),
    }
    contents = "\n".join((
        "# TFA metadata consolidation preparation", "",
        "museum-digital is the authoritative current descriptive metadata source for the 5,282 TFA records.",
        "Historical MD/LIDO/generic exports and WF/Thesaurus working tables remain provenance evidence, not competing authority.",
        "No local DDB object export was found; the filled and blank DDB agreements are handled in the corrected document package.",
        "Malformed historical XML is retained byte-for-byte with its condition recorded.",
        "Exact duplicate paths are disposition proposals only and deletion is not authorized.", "",
        f"- Current capture files: {len(current)}",
        f"- Historical export sources: {len(historical)}",
        f"- Selected working-metadata representatives: {len(working)}",
        f"- Ambiguous metadata records for workbench: {review_scope}",
        f"- Exact-copy proposals: {summary['proposed_exact_redundant_copies']}",
        "- Final hashes are deferred until accession.", "",
    ))
    return plan, disposition, summary, contents


def build_metadata_stage(plan: list[dict[str, str]], disposition: list[dict[str, str]], summary: dict[str, object], contents: str) -> None:
    grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in plan: grouped[row["category"]].append(row)
    current = grouped["museum_digital_current"]
    historical = grouped["historical_md_export_archive"] + grouped["historical_lido_snapshot"]
    working = grouped["tfa_working_metadata"]
    packages: list[Path] = []
    stage_temp = META_STAGE.with_suffix(".zip.tmp")
    try:
        for rows, title in ((current, "Current museum-digital collection 857 capture"),
                            (historical, "Historical museum-digital and LIDO exports"),
                            (working, "TFA working metadata and import evidence")):
            handle = tempfile.NamedTemporaryFile(prefix=".tfa-meta-", suffix=".zip", dir=REVIEW_ROOT, delete=False)
            handle.close(); path = Path(handle.name); packages.append(path)
            build_simple_inner(path, rows, "metadata_id", title)
        with zipfile.ZipFile(stage_temp, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
            archive.write(packages[0], "metadata/museum-digital-current.zip")
            archive.write(packages[1], "metadata/museum-digital-historical-exports.zip")
            archive.write(packages[2], "metadata/tfa-working-metadata.zip")
            archive.writestr("metadata/METADATA-CONTENTS.md", contents.encode())
            archive.writestr("db-restore/manifests/metadata-files.csv", csv_bytes(plan, META_FIELDS))
            archive.writestr("db-restore/manifests/metadata-source-disposition.csv", csv_bytes(disposition, DISPOSITION_FIELDS))
            archive.writestr("METADATA-PREPARATION.json", json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=2).encode() + b"\n")
        with zipfile.ZipFile(stage_temp) as archive:
            if bad := archive.testzip(): raise PrepareError(f"metadata stage CRC failure: {bad}")
        stage_temp.replace(META_STAGE)
    finally:
        for path in packages: path.unlink(missing_ok=True)
        stage_temp.unlink(missing_ok=True)


def main() -> int:
    require_boundaries()
    mount = mount_state()
    connection = index_connection()
    try:
        doc_plan, doc_disposition, doc_summary = build_corrected_documents(connection)
        metadata_plan, metadata_disposition, metadata_summary, metadata_contents = build_metadata(connection)
    finally:
        connection.close()
    doc_summary["source_mount"] = mount
    metadata_summary["source_mount"] = mount
    if any(row["deletion_authorized"] != "false" for row in doc_plan + doc_disposition + metadata_plan + metadata_disposition):
        raise PrepareError("preparation cannot authorize deletion")
    document_contents = build_document_stage(doc_plan, doc_disposition, doc_summary)
    build_metadata_stage(metadata_plan, metadata_disposition, metadata_summary, metadata_contents)
    doc_summary["stage_size_bytes"] = DOC_STAGE.stat().st_size
    metadata_summary["stage_size_bytes"] = META_STAGE.stat().st_size
    atomic_write(DOC_PLAN, csv_bytes(doc_plan, DOC_FIELDS))
    atomic_write(DOC_DISPOSITION, csv_bytes(doc_disposition, DISPOSITION_FIELDS))
    atomic_write(DOC_CONTENTS, document_contents.encode())
    atomic_write(DOC_SUMMARY, json.dumps(doc_summary, ensure_ascii=False, sort_keys=True, indent=2).encode() + b"\n")
    atomic_write(META_PLAN, csv_bytes(metadata_plan, META_FIELDS))
    atomic_write(META_DISPOSITION, csv_bytes(metadata_disposition, DISPOSITION_FIELDS))
    atomic_write(META_CONTENTS, metadata_contents.encode())
    atomic_write(META_SUMMARY, json.dumps(metadata_summary, ensure_ascii=False, sort_keys=True, indent=2).encode() + b"\n")
    print(json.dumps({"documents": doc_summary, "metadata": metadata_summary}, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
