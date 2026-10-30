#!/usr/bin/env python3
"""Finalize Kurt Schwarz documentation without mutating USB source material."""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import shutil
import sqlite3
import sys
import time
import zipfile
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, "/home/vladimir/wp/tools/archive-preservation")
import paragon_document_bundle


UNIT = Path("/mnt/volume/iss-archive/units/kurt-schwarz")
WORK = Path("/mnt/volume/iss-archive-work/kurt-schwarz")
SOURCE_ROOT = Path("/media/vladimir/Elements/restore/archive")
DB = UNIT / "db-restore/RESTORE.sqlite"
REVIEW_DB = WORK / "work/reconciliation/unit-reconciliation-v1/reconciliation.sqlite"
MD_RUN = WORK / (
    "work/reconciliation/museum-digital/runs/"
    "20260803T084925Z-kurt-schwarz-current-collection-783"
)
RUN = WORK / "work/reconciliation/documentation/20260814T-kurt-schwarz-documentation-final"
STAGE = RUN / "stage"

DOC_EXTENSIONS = {
    ".bin", ".cdx", ".csv", ".dbf", ".doc", ".docx", ".fpt", ".html",
    ".json", ".lrcat", ".lrtemplate", ".mp3", ".odt", ".ods", ".pdf",
    ".pptx", ".txt", ".url", ".vcf", ".xls", ".xlsx", ".xml", ".zip",
}

PROJECT_FINAL = {
    "/media/vladimir/Elements/restore/archive/WF/Kurt Schwarz/Digis/01_Arbeitspakete_Zeitplanung_Kurt-Schwarz-Digitalisierungsprojekt.pdf": (
        "01-work-packages-and-schedule.pdf", "Work packages and project schedule", "2021"
    ),
    "/media/vladimir/Elements/restore/archive/WF/Kurt Schwarz/Digis/Industriesalon_1.Quartalsbericht.pdf": (
        "02-quarterly-report-1.pdf", "First quarterly project report", "2021"
    ),
    "/media/vladimir/Elements/restore/archive/WF/Kurt Schwarz/Digis/Industriesalon_Bericht_2.Quartal.pdf": (
        "03-quarterly-report-2.pdf", "Second quarterly project report", "2021"
    ),
    "/media/vladimir/Elements/restore/archive/WF/Kurt Schwarz/Digis/Industriesalon_3.Quartal_Bericht.pdf": (
        "04-quarterly-report-3.pdf", "Final revised third quarterly project report", "2021-10-29"
    ),
    "/media/vladimir/Elements/restore/archive/WF/Kurt Schwarz/Digis/Industriesalon-Schöneweide_Projektabschlussbericht_2021.pdf": (
        "05-project-completion-report.pdf", "Final project completion report", "2022-03"
    ),
    "/media/vladimir/Elements/restore/archive/WF/Kurt Schwarz/Digis/Jahreskonferenz_2021/Kurt-Schwarz_Kurzpräsentation.pdf": (
        "06-project-presentation.pdf", "Kurt Schwarz project conference presentation", "2021"
    ),
    "/media/vladimir/Elements/restore/archive/WF/Kurt Schwarz/Industriesalon_Kurt-Schwarz.pdf": (
        "07-kurt-schwarz-project-publication.pdf", "Final Kurt Schwarz project/exhibition publication", ""
    ),
    "/media/vladimir/Elements/restore/archive/WF/Kurt Schwarz/VG-Bildkunst_Reproduktionsrechte.csv": (
        "08-reproduction-rights.csv", "Final reproduction-rights record", ""
    ),
}

MD_DIRECT = {
    "/media/vladimir/Elements/restore/archive/WF/Kurt Schwarz/Kurt-Schwarz_Metadatenexport_2025-01-06.xlsx": "final-local-metadata-export-2025-01-06.xlsx",
    "/media/vladimir/Elements/restore/archive/WF/Kurt Schwarz/UPLOADS/1.Upload_Buch/csv_xml.zip": "upload-1-csv-xml.zip",
    "/media/vladimir/Elements/restore/archive/WF/Kurt Schwarz/UPLOADS/1.Upload_Buch/1.Upload_vollstaendig_final.csv": "upload-1-final.csv",
    "/media/vladimir/Elements/restore/archive/WF/Kurt Schwarz/UPLOADS/2.Upload/2.Upload_csv_xml.zip": "upload-2-csv-xml.zip",
    "/media/vladimir/Elements/restore/archive/WF/Kurt Schwarz/UPLOADS/2.Upload/2.upload-K01-AG_10_final.csv": "upload-2-final.csv",
    "/media/vladimir/Elements/restore/archive/WF/Kurt Schwarz/UPLOADS/3.Upload/3.Upload_csv_xml.zip": "upload-3-csv-xml.zip",
    "/media/vladimir/Elements/restore/archive/WF/Kurt Schwarz/UPLOADS/3.Upload/3.Upload_SW_10_final.csv": "upload-3-final.csv",
    "/media/vladimir/Elements/restore/archive/WF/Kurt Schwarz/UPLOADS/4.Upload/4.Upload_csv_xml.zip": "upload-4-csv-xml.zip",
    "/media/vladimir/Elements/restore/archive/WF/Kurt Schwarz/UPLOADS/4.Upload/4.upload-09_final.csv": "upload-4-final.csv",
    "/media/vladimir/Elements/restore/archive/WF/Kurt Schwarz/UPLOADS/5.Upload/Upload-5_csv_xml.zip": "upload-5-csv-xml.zip",
    "/media/vladimir/Elements/restore/archive/WF/Kurt Schwarz/UPLOADS/5.Upload/5.Upload-09_final.csv": "upload-5-final.csv",
    "/media/vladimir/Elements/restore/archive/WF/Kurt Schwarz/UPLOADS/6.Upload/5-6_csv_xml.zip": "upload-5-6-csv-xml-variant-a.zip",
    "/media/vladimir/Elements/restore/archive/WF/Kurt Schwarz/UPLOADS/6.Upload/Upload-6_csv_xml.zip": "upload-6-csv-xml-variant-b.zip",
    "/media/vladimir/Elements/restore/archive/WF/Kurt Schwarz/UPLOADS/6.Upload/5.und6.Upload-09_final.csv": "upload-5-6-final.csv",
    "/media/vladimir/Elements/restore/archive/WF/Kurt Schwarz/UPLOADS/6.Upload/6.Upload_10-final.csv": "upload-6-final.csv",
    "/media/vladimir/Elements/restore/archive/WF/Kurt Schwarz/UPLOADS/7.Upload/Upload-7_csv_xml.zip": "upload-7-csv-xml.zip",
    "/media/vladimir/Elements/restore/archive/WF/Kurt Schwarz/UPLOADS/7.Upload/7b._Upload_10_komplett_final.csv": "upload-7-final.csv",
    "/media/vladimir/Elements/restore/archive/WF/Kurt Schwarz/UPLOADS/8.Upload/csv_xml.zip": "upload-8-csv-xml.zip",
    "/media/vladimir/Elements/restore/archive/WF/Kurt Schwarz/UPLOADS/8.Upload/8.Upload_06_Baecker_final.csv": "upload-8-baecker-final.csv",
    "/media/vladimir/Elements/restore/archive/WF/Kurt Schwarz/UPLOADS/8.Upload/8.Upload_Color_06_final.csv": "upload-8-color-final.csv",
    "/media/vladimir/Elements/restore/archive/WF/Kurt Schwarz/UPLOADS/8.Upload/8.Upload_KSColor-Baecker_07_final.csv": "upload-8-combined-final.csv",
}

LIDO_PREFIXES = (
    "/media/vladimir/Elements/restore/archive/WF/Archiv/export_29_20191120_lido/",
    "/media/vladimir/Elements/restore/archive/export_Menschen-im-WF_18-02-2020_29_20200218_lido/",
)

SOURCE_FIELDS = (
    "source_path", "source_kind", "unit", "inventory", "source_role", "final_path",
    "preservation_status", "preservation_basis", "source_sha256", "final_sha256",
    "source_size_bytes", "exact_copy_proven", "drain_state", "deletion_authorized", "notes",
)
REP_FIELDS = (
    "index", "completed_at", "representation_key", "record_keys", "inventories", "role",
    "source_kind", "source_path", "source_member", "source_sha256", "final_sha256",
    "final_relative_path", "size_bytes", "mtime_ns", "copy_policy", "basis", "relationship",
    "access_restriction",
)
CHECKSUM_FIELDS = ("path", "sha256", "size_bytes", "content_role")
DOCUMENT_FIELDS = (
    "document_id", "final_relative_path", "filename", "file_role", "description", "date",
    "source_path", "source_kind", "relationship", "access_restriction", "condition",
    "disposition", "package_member_count", "size_bytes", "sha256", "deletion_authorized",
)
REVIEW_FIELDS = (
    "source_path", "filename", "classification", "final_relative_path", "relationship",
    "access_restriction", "review_basis", "deletion_authorized",
)


class FinalizeError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def csv_rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def csv_payload(rows: list[dict[str, object]], fields: tuple[str, ...]) -> bytes:
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore", quoting=csv.QUOTE_ALL)
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue().encode("utf-8")


def write_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)


def sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return "sha256:" + digest.hexdigest()


def source_parts(source_path: str) -> tuple[Path, str]:
    if "::" in source_path:
        container, member = source_path.split("::", 1)
        return Path(container), member
    return Path(source_path), ""


def source_bytes(source_path: str) -> bytes:
    container, member = source_parts(source_path)
    if member:
        with zipfile.ZipFile(container) as archive:
            return archive.read(member)
    return container.read_bytes()


def source_filename(source_path: str) -> str:
    container, member = source_parts(source_path)
    return Path(member).name if member else container.name


def source_size(source_path: str) -> int:
    container, member = source_parts(source_path)
    if member:
        with zipfile.ZipFile(container) as archive:
            return archive.getinfo(member).file_size
    return container.stat().st_size


def safe_member_name(number: int, name: str) -> str:
    cleaned = "".join("_" if char in "/\\\0" else char for char in name).strip()
    return f"{number:04d}-{cleaned or 'unnamed'}"


def is_lido_path(path: str) -> bool:
    return any(path.startswith(prefix) for prefix in LIDO_PREFIXES)


def restricted_group(path: str) -> str:
    lower = path.casefold()
    contracts = (
        "arbeitsvertrag", "vertrag", "angebot", "auftrag", "empfang", "letter of intent",
        "letter-of-intent", "bescheid", "einverständ", "mindestlohn", "projektantrag",
        "/antrag/", "antrag_us",
    )
    finance = (
        "rechnung", "abrechnung", "kontoaus", "beleg", "budget", "kosten", "stunde",
        "invoice", "ausgaben", "diabetrachter", "berola", "lohn", "honorar",
    )
    personal = (
        "/scans/", "arbeitszeugnis", "vita", "kurzbio", "bewerb", "personal",
        "lebenslauf", "brief_", "korrespondenz",
    )
    if any(token in lower for token in contracts):
        return "contracts"
    if any(token in lower for token in finance):
        return "finance"
    if any(token in lower for token in personal):
        return "personal"
    return ""


def classify(path: str, decision: str) -> tuple[str, str, str, str]:
    lower = path.casefold()
    if path in PROJECT_FINAL:
        name = PROJECT_FINAL[path][0]
        return "project_final", f"documents/project-final/{name}", "direct", ""
    if path in MD_DIRECT:
        return "md_export", f"export/md/{MD_DIRECT[path]}", "direct", ""
    if is_lido_path(path):
        return "lido_condition_evidence", "export/md/lido-source-evidence.zip", "contextual", ""
    if "/backup_lightroom_ks/" in lower or lower.endswith((".lrcat", ".lrtemplate")):
        return "noise_lightroom", "", "historical_reference_noise", ""
    if source_filename(path).startswith("~$"):
        return "noise_office_lock", "", "historical_reference_noise", ""
    if "/ks-cd_wf-doku_70er_80er_alle-bereiche/catalog/" in lower:
        return "cross_unit_catalog", "", "cross_unit_reference", ""
    if "/wf-sender/" in lower or decision == "cross_unit_reference":
        return "cross_unit_reference", "", "cross_unit_reference", ""
    if "/antragshilfe-vorlagen/" in lower:
        return "noise_template", "", "historical_reference_noise", ""
    if lower.endswith(".mp3"):
        return "noise_audio", "", "historical_reference_noise", ""
    group = restricted_group(path)
    if group:
        return f"restricted_{group}", f"documents/restricted/{group}.zip", "direct", "restricted"
    return "historical", "documents/historical/drafts-and-superseded.zip", "direct_or_contextual", ""


def load_review_corpus() -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    source_rows = csv_rows(UNIT / "db-restore/manifests/source-disposition.csv")
    source_by_path: dict[str, dict[str, str]] = {}
    for row in source_rows:
        source_by_path.setdefault(row["source_path"], row)
    review = sqlite3.connect(f"file:{REVIEW_DB}?mode=ro", uri=True)
    review.row_factory = sqlite3.Row
    try:
        latest = {
            row["document_id"]: row
            for row in review.execute(
                "SELECT d.* FROM document_decisions d JOIN "
                "(SELECT document_id,MAX(decision_id) id FROM document_decisions GROUP BY document_id) x "
                "ON x.id=d.decision_id"
            )
        }
        old = [dict(row) for row in review.execute("SELECT * FROM document_candidates")]
    finally:
        review.close()
    paths = {
        row["source_path"]
        for row in source_rows
        if (
            row["source_role"] in {"project_document", "project_metadata_document", "reviewed_noise"}
            or "/WF/Kurt Schwarz/Backup_Lightroom_KS/" in row["source_path"]
        )
        and Path(row["source_path"]).suffix.casefold() in DOC_EXTENSIONS
    }
    paths.update(row["source_path"] for row in old)
    paths.update(path for path in source_by_path if is_lido_path(path))
    old_by_path = {row["source_path"]: row for row in old}
    decisions = {
        row["source_path"]: (
            latest[row["document_id"]]["disposition"] if row["document_id"] in latest else ""
        )
        for row in old
    }
    results = []
    for path in sorted(paths):
        decision = decisions.get(path, "")
        classification, final_path, relationship, restriction = classify(path, decision)
        basis = {
            "project_final": "selected final project record",
            "md_export": "selected final or received museum-digital export evidence",
            "lido_condition_evidence": "Kurt-related source found in LIDO-labelled export; payload condition retained",
            "noise_lightroom": "iterative technical catalogue backup or preset; not current metadata authority",
            "noise_office_lock": "temporary Office lock file",
            "cross_unit_catalog": "mixed WF catalogue belongs outside the Kurt unit",
            "cross_unit_reference": "cross-unit reference retained only in disposition evidence",
            "noise_template": "generic project template",
            "noise_audio": "audio reference outside documentation accession",
            "restricted_contracts": "contractual or grant record; restricted access",
            "restricted_finance": "financial or accounting record; restricted access",
            "restricted_personal": "personal, HR, biographical, or correspondence record; restricted access",
            "historical": "draft, superseded generation, legacy catalogue, research, or contextual evidence",
        }[classification]
        source = source_by_path.get(path, {})
        known_size = source.get("source_size_bytes") or old_by_path.get(path, {}).get("size_bytes", "")
        results.append({
            "source_path": path,
            "filename": source_filename(path),
            "classification": classification,
            "final_relative_path": final_path,
            "relationship": relationship,
            "access_restriction": restriction,
            "review_basis": basis + (f"; prior decision={decision}" if decision else ""),
            "deletion_authorized": "false",
            "source_sha256": source.get("source_sha256", old_by_path.get(path, {}).get("content_sha256", "")),
            "source_size_bytes": known_size or str(source_size(path)),
            "source_kind": source.get("source_kind", "container_member" if "::" in path else "file"),
        })
    return results, source_rows


def package_zip(target: Path, rows: list[dict[str, str]], title: str, condition_note: str = "") -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    manifest = []
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
        for number, row in enumerate(sorted(rows, key=lambda item: item["source_path"]), 1):
            member = safe_member_name(number, row["filename"])
            payload = source_bytes(row["source_path"])
            archive.writestr(member, payload)
            manifest.append({
                "member": member,
                "original_filename": row["filename"],
                "original_source_path": row["source_path"],
                "classification": row["classification"],
                "relationship": row["relationship"],
                "condition": condition_note,
                "size_bytes": str(len(payload)),
                "source_sha256": row["source_sha256"],
                "deletion_authorized": "false",
            })
        fields = (
            "member", "original_filename", "original_source_path", "classification", "relationship",
            "condition", "size_bytes", "source_sha256", "deletion_authorized",
        )
        archive.writestr("MANIFEST.csv", csv_payload(manifest, fields))
        readme = (
            f"# {title}\n\n"
            "This package preserves exact source bytes without retaining the original folder structure. "
            "MANIFEST.csv records each original path and disposition. It is preservation evidence, not "
            "deletion authority.\n\n"
            f"Members: {len(rows)}\n"
        )
        if condition_note:
            readme += f"\nCondition: {condition_note}\n"
        archive.writestr("README.md", readme.encode("utf-8"))
    with zipfile.ZipFile(target) as archive:
        bad = archive.testzip()
        if bad:
            raise FinalizeError(f"ZIP CRC failure in {target}: {bad}")


def copy_direct(source: str, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    container, member = source_parts(source)
    if member:
        write_bytes(target, source_bytes(source))
    else:
        shutil.copyfile(container, target)


def build_current_md_zip(target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    files = sorted(path for path in MD_RUN.rglob("*") if path.is_file())
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
        for path in files:
            archive.write(path, path.relative_to(MD_RUN).as_posix())
    with zipfile.ZipFile(target) as archive:
        bad = archive.testzip()
        if bad:
            raise FinalizeError(f"current MD ZIP CRC failure: {bad}")


def file_description(final_path: str) -> tuple[str, str, str, str]:
    if final_path.startswith("documents/project-final/"):
        source = next(path for path, spec in PROJECT_FINAL.items() if final_path.endswith(spec[0]))
        spec = PROJECT_FINAL[source]
        return "project_document", spec[1], spec[2], ""
    if final_path == "documents/restricted/contracts.zip":
        return "restricted_contract_package", "Contracts, grant applications, decisions, offers, and commissions", "2021-2022", "restricted"
    if final_path == "documents/restricted/finance.zip":
        return "restricted_finance_package", "Invoices, accounts, budgets, payroll, time records, and receipts", "2021-2022", "restricted"
    if final_path == "documents/restricted/personal.zip":
        return "restricted_personal_package", "Personal, HR, biographical, correspondence, and scanned records", "", "restricted"
    if final_path == "documents/historical/drafts-and-superseded.zip":
        return "historical_document_package", "Drafts, superseded versions, legacy catalogue evidence, and contextual research", "", ""
    if final_path == "export/md/current-md-collection-783-2026-08-03.zip":
        return "current_md_export", "Current museum-digital collection 783 API snapshot: raw JSON, objects CSV, coverage, and request manifest", "2026-08-03", ""
    if final_path == "export/md/lido-source-evidence.zip":
        return "condition_noted_lido_evidence", "Kurt-related payloads found under LIDO-labelled source exports; payload formats are not valid LIDO XML", "2019-2020", ""
    if final_path == "export/md/README.md":
        return "md_export_index", "Human-readable index and condition notes for museum-digital exports", "2026-08-14", ""
    if final_path.startswith("export/md/"):
        if final_path.endswith(".zip"):
            return "historical_md_xml_export", "Received museum-digital CSV/XML upload package", "2021-2022", ""
        if final_path.endswith(".csv"):
            return "historical_md_csv_export", "Selected final CSV upload/export file", "2021-2022", ""
        return "final_local_metadata_export", "Selected final local metadata export", "2025-01-06", ""
    if final_path == "documents/CONTENTS.md":
        return "documentation_index", "Human-readable table of contents for retained documentation", "2026-08-14", ""
    raise FinalizeError(f"no description for {final_path}")


def build_stage(review_rows: list[dict[str, str]]) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    if STAGE.exists():
        raise FinalizeError(f"stage already exists: {STAGE}")
    STAGE.mkdir(parents=True)
    for source, name in PROJECT_FINAL.items():
        copy_direct(source, STAGE / "documents/project-final" / name[0])
    for source, name in MD_DIRECT.items():
        copy_direct(source, STAGE / "export/md" / name)
    groups = {
        "documents/historical/drafts-and-superseded.zip": [r for r in review_rows if r["classification"] == "historical"],
        "documents/restricted/contracts.zip": [r for r in review_rows if r["classification"] == "restricted_contracts"],
        "documents/restricted/finance.zip": [r for r in review_rows if r["classification"] == "restricted_finance"],
        "documents/restricted/personal.zip": [r for r in review_rows if r["classification"] == "restricted_personal"],
        "export/md/lido-source-evidence.zip": [r for r in review_rows if r["classification"] == "lido_condition_evidence"],
    }
    for relative, rows in groups.items():
        condition = "Seven selected .xml-named payloads are image/data files and one is CSV-like text; no valid Kurt-specific LIDO XML was found in this bounded evidence set." if "lido" in relative else ""
        package_zip(STAGE / relative, rows, Path(relative).stem.replace("-", " ").title(), condition)
    build_current_md_zip(STAGE / "export/md/current-md-collection-783-2026-08-03.zip")
    md_readme = """# Kurt Schwarz museum-digital exports

This directory preserves the selected current museum-digital collection snapshot, the final local metadata export, received CSV/XML upload packages, selected final CSV files, and condition-noted LIDO-labelled source evidence.

- `current-md-collection-783-2026-08-03.zip`: 17 raw JSON pages, `objects.csv`, `coverage.json`, request manifest, and capture README for 1,661 museum-digital objects.
- `final-local-metadata-export-2025-01-06.xlsx`: final local metadata spreadsheet retained separately from working generations.
- `upload-*.zip`: received XML/CSV containers. Upload 4 contains damaged member `112.xml`. The two Upload 6 variants differ at `553.xml`; both are retained.
- `upload-*-final.csv`: selected final CSV submission/export evidence.
- `lido-source-evidence.zip`: source items found in LIDO-labelled locations. They are preserved with a condition note because the selected payloads are not valid LIDO XML.

Original source paths and dispositions are recorded in `db-restore/manifests/documents.csv` and the package manifests. Source deletion is not authorized.
"""
    write_bytes(STAGE / "export/md/README.md", md_readme.encode("utf-8"))

    payload_paths = sorted(
        path.relative_to(STAGE).as_posix()
        for path in STAGE.rglob("*") if path.is_file()
    )
    package_counts = Counter(r["final_relative_path"] for r in review_rows if r["final_relative_path"])
    documents = []
    for number, final_path in enumerate(payload_paths, 1):
        path = STAGE / final_path
        role, description, date, restriction = file_description(final_path)
        direct = [r for r in review_rows if r["final_relative_path"] == final_path]
        source = direct[0]["source_path"] if len(direct) == 1 else ("multiple; see package MANIFEST.csv" if direct else str(MD_RUN))
        condition = ""
        if final_path == "export/md/lido-source-evidence.zip":
            condition = "Condition-noted: selected .xml-named payloads are not valid LIDO XML."
        if final_path == "export/md/upload-4-csv-xml.zip":
            condition = "Damaged member 112.xml retained as received."
        if "upload-5-6-csv-xml" in final_path or "upload-6-csv-xml" in final_path:
            condition = "Both source variants retained because member 553.xml differs."
        documents.append({
            "document_id": f"KSDOC-{number:04d}",
            "final_relative_path": final_path,
            "filename": Path(final_path).name,
            "file_role": role,
            "description": description,
            "date": date,
            "source_path": source,
            "source_kind": (
                "file" if final_path.endswith(".zip") and len(direct) == 1 and direct[0]["classification"] == "md_export"
                else "generated_package" if final_path.endswith(".zip")
                else "generated_index" if final_path.endswith("README.md")
                else "file"
            ),
            "relationship": "direct" if not final_path.endswith("lido-source-evidence.zip") else "contextual",
            "access_restriction": restriction,
            "condition": condition,
            "disposition": "accessioned",
            "package_member_count": str(package_counts.get(final_path, 21 if final_path.startswith("export/md/current-") else 0)),
            "size_bytes": str(path.stat().st_size),
            "sha256": sha256_path(path),
            "deletion_authorized": "false",
        })
    return documents, groups


def contents_markdown(documents: list[dict[str, str]], review_rows: list[dict[str, str]]) -> str:
    by_path = {row["final_relative_path"]: row for row in documents}
    lines = [
        "# Kurt Schwarz documentation contents", "",
        "This section preserves selected final project records, restricted administration, and one historical package of drafts and superseded documentation. Original source paths are recorded here, in package manifests, and in `../db-restore/manifests/documents.csv`.", "",
        "The restricted packages contain contracts, finance, or personal information and are not intended for public access. Drain/source-disposition records are review evidence only; every row remains `deletion_authorized=false`.", "",
        "## Project-final records", "",
    ]
    for path in sorted(p for p in by_path if p.startswith("documents/project-final/")):
        row = by_path[path]
        lines += [f"### `{Path(path).name}`", "", row["description"], "", f"- Date: {row['date'] or 'not established'}", f"- Original source: `{row['source_path']}`", "- Relationship: direct project record", "- Condition: retained as received", ""]
    lines += ["## Restricted records", ""]
    for path in ("documents/restricted/contracts.zip", "documents/restricted/finance.zip", "documents/restricted/personal.zip"):
        row = by_path[path]
        lines += [f"### `{Path(path).name}`", "", row["description"], "", f"- Package members: {row['package_member_count']}", "- Original sources: listed in the package `MANIFEST.csv`", "- Relationship: direct project administration", "- Access: restricted", "- Condition: exact source bytes retained in a flat ZIP package", ""]
    hist = by_path["documents/historical/drafts-and-superseded.zip"]
    lines += ["## Historical documentation", "", "### `drafts-and-superseded.zip`", "", hist["description"], "", f"- Package members: {hist['package_member_count']}", "- Original sources: listed in the package `MANIFEST.csv`", "- Relationship: direct or contextual historical evidence", "- Access: ordinary archival access unless a member requires separate restriction", "- Condition: exact source bytes retained in a flat ZIP package", ""]
    counts = Counter(row["classification"] for row in review_rows)
    lines += ["## Reviewed but not accessioned", "", "The following categories are recorded in the drain/source-disposition evidence and are not present as payloads in this unit:", ""]
    for name in ("noise_lightroom", "noise_office_lock", "noise_template", "noise_audio", "cross_unit_catalog", "cross_unit_reference"):
        if counts[name]:
            lines.append(f"- `{name}`: {counts[name]}")
    lines += ["", "See `../export/md/README.md` for museum-digital JSON/XML/CSV and LIDO-labelled export evidence.", ""]
    return "\n".join(lines)


def xlsx_payload(fields: list[str], rows: list[dict[str, str]], title: str) -> bytes:
    return paragon_document_bundle.table_xlsx_bytes(
        fields, rows, sheet_name=title[:31], table_name=title.replace(" ", "") + "Table"
    )


def update_disposition(review_rows: list[dict[str, str]], source_rows: list[dict[str, str]], final_hashes: dict[str, str]) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    reviewed = {row["source_path"]: row for row in review_rows}
    seen = set()
    updated = []
    for source in source_rows:
        row = dict(source)
        review = reviewed.get(row["source_path"])
        if review:
            seen.add(row["source_path"])
            cls = review["classification"]
            final = review["final_relative_path"]
            selected = bool(final)
            row.update({
                "source_role": cls,
                "final_path": final,
                "preservation_status": "accessioned_documentation" if selected else "reviewed_not_accessioned",
                "preservation_basis": review["review_basis"],
                "final_sha256": final_hashes.get(final, ""),
                "exact_copy_proven": "true" if cls in {"project_final", "md_export"} else "false",
                "drain_state": "source_covered_by_documentation_accession" if selected else ("cross_unit_reference" if "cross_unit" in cls else "historical_reference_noise"),
                "deletion_authorized": "false",
                "notes": "Documentation review completed 2026-08-14. No deletion is authorized.",
            })
        updated.append(row)
    for path in sorted(set(reviewed) - seen):
        review = reviewed[path]
        final = review["final_relative_path"]
        updated.append({
            "source_path": path,
            "source_kind": review["source_kind"],
            "unit": "kurt-schwarz",
            "inventory": "",
            "source_role": review["classification"],
            "final_path": final,
            "preservation_status": "accessioned_documentation" if final else "reviewed_not_accessioned",
            "preservation_basis": review["review_basis"],
            "source_sha256": review["source_sha256"],
            "final_sha256": final_hashes.get(final, ""),
            "source_size_bytes": review["source_size_bytes"],
            "exact_copy_proven": "true" if review["classification"] in {"project_final", "md_export"} else "false",
            "drain_state": "source_covered_by_documentation_accession" if final else ("cross_unit_reference" if "cross_unit" in review["classification"] else "historical_reference_noise"),
            "deletion_authorized": "false",
            "notes": "Documentation review completed 2026-08-14. No deletion is authorized.",
        })
    existing_drain = csv_rows(UNIT / "db-restore/manifests/drain-list.csv")
    drain = [row for row in existing_drain if row["source_path"] not in reviewed]
    canonical = {}
    for row in updated:
        if row["source_path"] in reviewed:
            canonical[row["source_path"]] = row
    drain.extend(canonical[path] for path in sorted(canonical))
    return updated, drain


def build_catalog_and_restore(documents: list[dict[str, str]], source_rows: list[dict[str, str]], drain_rows: list[dict[str, str]], completed_at: str) -> None:
    old_catalog = csv_rows(UNIT / "CATALOG.csv")
    old_reps = csv_rows(UNIT / "db-restore/manifests/representations.csv")
    old_checksums = csv_rows(UNIT / "db-restore/checksums.csv")
    connection = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        objects = {row["record_key"]: dict(row) for row in connection.execute("SELECT * FROM objects")}
    finally:
        connection.close()
    reps_by_path = {row["final_relative_path"]: row for row in old_reps}
    object_fields = [f"object_{field}" for field in next(iter(objects.values())).keys()]
    rep_extra_fields = [f"representation_{field}" for field in REP_FIELDS]
    base_fields = list(old_catalog[0].keys())
    catalog_fields = base_fields + object_fields + rep_extra_fields
    catalog = []
    for row in old_catalog:
        item = dict(row)
        obj = objects.get(row["record_key"], {})
        rep = reps_by_path.get(row["final_relative_path"], {})
        item.update({f"object_{key}": value for key, value in obj.items()})
        item.update({f"representation_{key}": rep.get(key, "") for key in REP_FIELDS})
        catalog.append(item)
    next_index = max(int(row["index"]) for row in old_reps) + 1
    new_reps = []
    new_checksums = []
    docs_by_path = {row["final_relative_path"]: row for row in documents}
    for offset, final_path in enumerate(sorted(docs_by_path)):
        doc = docs_by_path[final_path]
        source_sha = ""
        if doc["source_path"].startswith(str(SOURCE_ROOT)) and "multiple" not in doc["source_path"]:
            source_sha = next((row["source_sha256"] for row in source_rows if row["source_path"] == doc["source_path"]), "")
        rep = {
            "index": str(next_index + offset),
            "completed_at": completed_at,
            "representation_key": doc["sha256"],
            "record_keys": "unit:kurt-schwarz",
            "inventories": "",
            "role": doc["file_role"],
            "source_kind": doc["source_kind"],
            "source_path": doc["source_path"],
            "source_member": "",
            "source_sha256": source_sha,
            "final_sha256": doc["sha256"],
            "final_relative_path": final_path,
            "size_bytes": doc["size_bytes"],
            "mtime_ns": str(time.time_ns()),
            "copy_policy": "exact_copy" if source_sha and source_sha == doc["sha256"] else "generated_review_package_or_index",
            "basis": doc["description"],
            "relationship": doc["relationship"],
            "access_restriction": doc["access_restriction"],
        }
        new_reps.append(rep)
        new_checksums.append({"path": final_path, "sha256": doc["sha256"], "size_bytes": doc["size_bytes"], "content_role": doc["file_role"]})
        item = {field: "" for field in base_fields}
        item.update({
            "record_key": "unit:kurt-schwarz", "inventory": "", "file_role": doc["file_role"],
            "final_relative_path": final_path, "filename": doc["filename"],
            "file_type": Path(final_path).suffix.lstrip("."), "size_bytes": doc["size_bytes"],
            "final_sha256": doc["sha256"], "title": doc["filename"], "description": doc["description"],
            "date_text": doc["date"], "creator": "Industriesalon Schöneweide",
            "match_status": "unit_documentation", "match_basis": doc["disposition"],
            "original_source_path": doc["source_path"],
        })
        item.update({field: "" for field in object_fields})
        item.update({f"representation_{key}": rep[key] for key in REP_FIELDS})
        catalog.append(item)
    reps = old_reps + new_reps
    checksum_paths = {row["path"] for row in new_checksums}
    checksums = [row for row in old_checksums if row["path"] not in checksum_paths] + new_checksums
    write_bytes(STAGE / "CATALOG.csv", csv_payload(catalog, tuple(catalog_fields)))
    write_bytes(STAGE / "CATALOG.xlsx", xlsx_payload(catalog_fields, catalog, "Catalog"))
    write_bytes(STAGE / "db-restore/RESTORE.csv", csv_payload(catalog, tuple(catalog_fields)))
    write_bytes(STAGE / "db-restore/RESTORE.xlsx", xlsx_payload(catalog_fields, catalog, "Restore"))
    write_bytes(STAGE / "db-restore/manifests/representations.csv", csv_payload(reps, REP_FIELDS))
    write_bytes(STAGE / "db-restore/checksums.csv", csv_payload(checksums, CHECKSUM_FIELDS))
    write_bytes(STAGE / "db-restore/manifests/source-disposition.csv", csv_payload(source_rows, SOURCE_FIELDS))
    write_bytes(STAGE / "db-restore/manifests/drain-list.csv", csv_payload(drain_rows, SOURCE_FIELDS))

    staged_db = STAGE / "db-restore/RESTORE.sqlite"
    source_con = sqlite3.connect(DB)
    target_con = sqlite3.connect(staged_db)
    try:
        source_con.backup(target_con)
        for table, rows, fields in (
            ("representations", reps, REP_FIELDS),
            ("checksums", checksums, CHECKSUM_FIELDS),
            ("source_disposition", source_rows, SOURCE_FIELDS),
            ("drain_list", drain_rows, SOURCE_FIELDS),
        ):
            target_con.execute(f'DELETE FROM "{table}"')
            placeholders = ",".join("?" for _ in fields)
            columns = ",".join(f'"{field}"' for field in fields)
            target_con.executemany(
                f'INSERT INTO "{table}" ({columns}) VALUES ({placeholders})',
                [[row.get(field, "") for field in fields] for row in rows],
            )
        target_con.commit()
        if target_con.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise FinalizeError("staged SQLite quick_check failed")
        if list(target_con.execute("PRAGMA foreign_key_check")):
            raise FinalizeError("staged SQLite foreign keys failed")
        sql = "\n".join(target_con.iterdump()) + "\n"
    finally:
        target_con.close()
        source_con.close()
    write_bytes(STAGE / "db-restore/RESTORE.sql", sql.encode("utf-8"))


def update_readme(documents: list[dict[str, str]], review_rows: list[dict[str, str]]) -> str:
    text = (UNIT / "00_README.md").read_text(encoding="utf-8")
    marker = "\n## Documentation and metadata exports\n"
    if marker in text:
        text = text.split(marker, 1)[0].rstrip() + "\n"
    counts = Counter(row["classification"] for row in review_rows)
    text += marker + f"""
The finalized documentation is indexed in `documents/CONTENTS.md` and `db-restore/manifests/documents.csv`. It contains {counts['project_final']} directly readable final project records, three restricted packages for contracts, finance, and personal information, and one historical package containing {counts['historical']} drafts, superseded generations, legacy catalogue components, and contextual records.

`export/md/` preserves the dated current museum-digital JSON/CSV snapshot, selected final CSV exports, received XML/CSV upload containers, the final 2025 local metadata export, and condition-noted LIDO-labelled source evidence. The object/file `CATALOG.csv` and XLSX/restore companions include all current object metadata plus the complete provenance fields held in SQLite.

All reviewed documentation sources are represented in `drain-list.csv` and `source-disposition.csv`; every row remains `deletion_authorized=false`.
"""
    return text


def validate_stage(documents: list[dict[str, str]], review_rows: list[dict[str, str]]) -> dict[str, object]:
    errors = []
    if any(row["deletion_authorized"] != "false" for row in review_rows):
        errors.append("review row authorized deletion")
    for row in documents:
        path = STAGE / row["final_relative_path"]
        if not path.is_file() or sha256_path(path) != row["sha256"]:
            errors.append(f"document payload mismatch: {row['final_relative_path']}")
        if path.suffix == ".zip":
            with zipfile.ZipFile(path) as archive:
                bad_member = archive.testzip()
                expected_damaged_upload = (
                    row["final_relative_path"] == "export/md/upload-4-csv-xml.zip"
                    and bad_member == "112.xml"
                )
                if bad_member and not expected_damaged_upload:
                    errors.append(
                        f"ZIP CRC failure: {row['final_relative_path']}::{bad_member}"
                    )
    catalog = csv_rows(STAGE / "CATALOG.csv")
    reps = csv_rows(STAGE / "db-restore/manifests/representations.csv")
    disposition = csv_rows(STAGE / "db-restore/manifests/source-disposition.csv")
    drain = csv_rows(STAGE / "db-restore/manifests/drain-list.csv")
    checksums = csv_rows(STAGE / "db-restore/checksums.csv")
    if len({row["record_key"] for row in catalog if row["file_role"] == "preservation_master"}) != 1935:
        errors.append("catalog does not contain 1935 master objects")
    if len([row for row in catalog if row["file_role"] == "preservation_master"]) != 1935:
        errors.append("catalog master count is not exactly 1935")
    if len([row for row in catalog if row["file_role"] == "access_derivative"]) != 1935:
        errors.append("catalog access derivative count is not exactly 1935")
    if any(row["deletion_authorized"] != "false" for row in disposition + drain):
        errors.append("manifest authorized deletion")
    reviewed_paths = {row["source_path"] for row in review_rows}
    drain_paths = {row["source_path"] for row in drain}
    if not reviewed_paths <= drain_paths:
        errors.append(f"drain missing {len(reviewed_paths - drain_paths)} reviewed paths")
    rep_paths = {row["final_relative_path"] for row in reps}
    doc_paths = {row["final_relative_path"] for row in documents}
    if not doc_paths <= rep_paths:
        errors.append("representations missing documentation payloads")
    checksum_paths = {row["path"] for row in checksums}
    if not doc_paths <= checksum_paths:
        errors.append("checksums missing documentation payloads")
    con = sqlite3.connect(STAGE / "db-restore/RESTORE.sqlite")
    try:
        quick = con.execute("pragma quick_check").fetchone()[0]
        fk = len(list(con.execute("pragma foreign_key_check")))
    finally:
        con.close()
    if quick != "ok" or fk:
        errors.append("staged SQLite validation failed")
    result = {
        "result": "ok" if not errors else "error",
        "errors": errors,
        "reviewed_source_paths": len(reviewed_paths),
        "classification_counts": dict(Counter(row["classification"] for row in review_rows)),
        "retained_document_files": len(documents),
        "catalog_rows": len(catalog),
        "representation_rows": len(reps),
        "source_disposition_rows": len(disposition),
        "drain_rows": len(drain),
        "checksum_rows": len(checksums),
        "sqlite_quick_check": quick,
        "sqlite_foreign_key_violations": fk,
        "deletion_authorized": False,
        "source_files_modified": 0,
    }
    if errors:
        raise FinalizeError(json.dumps(result, ensure_ascii=False))
    return result


def publish() -> None:
    new_roots = [
        "documents/project-final", "documents/restricted", "documents/historical", "export/md"
    ]
    for relative in new_roots:
        target = UNIT / relative
        if target.exists() and any(target.iterdir()):
            raise FinalizeError(f"refusing to merge into non-empty target: {target}")
    backup = RUN / "RESTORE.pre-documentation.sqlite"
    source_con = sqlite3.connect(DB)
    backup_con = sqlite3.connect(backup)
    try:
        source_con.backup(backup_con)
    finally:
        backup_con.close()
        source_con.close()
    for relative in new_roots:
        source = STAGE / relative
        target = UNIT / relative
        target.mkdir(parents=True, exist_ok=True)
        for path in source.iterdir():
            destination = target / path.name
            if destination.exists():
                raise FinalizeError(f"refusing to overwrite payload: {destination}")
            shutil.copyfile(path, destination)
    replacements = [
        "00_README.md", "CATALOG.csv", "CATALOG.xlsx", "documents/CONTENTS.md",
        "db-restore/RESTORE.csv", "db-restore/RESTORE.xlsx", "db-restore/RESTORE.sqlite",
        "db-restore/RESTORE.sql", "db-restore/checksums.csv",
        "db-restore/manifests/representations.csv", "db-restore/manifests/source-disposition.csv",
        "db-restore/manifests/drain-list.csv", "db-restore/manifests/documents.csv",
    ]
    for relative in replacements:
        source = STAGE / relative
        target = UNIT / relative
        temporary = target.with_name(f".{target.name}.documentation-{os.getpid()}")
        shutil.copyfile(source, temporary)
        os.replace(temporary, target)


def execute(apply: bool) -> dict[str, object]:
    if RUN.exists():
        raise FinalizeError(f"run already exists: {RUN}")
    RUN.mkdir(parents=True)
    review_rows, original_source_rows = load_review_corpus()
    write_bytes(RUN / "reviewed-documents.csv", csv_payload(review_rows, REVIEW_FIELDS))
    if not apply:
        result = {
            "result": "plan_only",
            "run": str(RUN),
            "reviewed_source_paths": len(review_rows),
            "classification_counts": dict(Counter(row["classification"] for row in review_rows)),
            "deletion_authorized": False,
        }
        write_bytes(RUN / "plan.json", (json.dumps(result, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
        return result
    documents, _ = build_stage(review_rows)
    contents = contents_markdown(documents, review_rows)
    write_bytes(STAGE / "documents/CONTENTS.md", contents.encode("utf-8"))
    role, description, date, restriction = file_description("documents/CONTENTS.md")
    contents_row = {
        "document_id": f"KSDOC-{len(documents)+1:04d}", "final_relative_path": "documents/CONTENTS.md",
        "filename": "CONTENTS.md", "file_role": role, "description": description, "date": date,
        "source_path": "generated from db-restore/manifests/documents.csv", "source_kind": "generated_index",
        "relationship": "direct", "access_restriction": restriction, "condition": "",
        "disposition": "accessioned", "package_member_count": "0",
        "size_bytes": str((STAGE / "documents/CONTENTS.md").stat().st_size),
        "sha256": sha256_path(STAGE / "documents/CONTENTS.md"), "deletion_authorized": "false",
    }
    documents.append(contents_row)
    # Refresh generated MD README row after all payloads are known.
    final_hashes = {row["final_relative_path"]: row["sha256"] for row in documents}
    source_rows, drain_rows = update_disposition(review_rows, original_source_rows, final_hashes)
    completed_at = utc_now()
    write_bytes(STAGE / "db-restore/manifests/documents.csv", csv_payload(documents, DOCUMENT_FIELDS))
    build_catalog_and_restore(documents, source_rows, drain_rows, completed_at)
    write_bytes(STAGE / "00_README.md", update_readme(documents, review_rows).encode("utf-8"))
    validation = validate_stage(documents, review_rows)
    validation["completed_at"] = completed_at
    write_bytes(RUN / "validation.json", (json.dumps(validation, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8"))
    publish()
    return {"result": "ok", "run": str(RUN), **validation}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    try:
        print(json.dumps(execute(args.apply), ensure_ascii=False, indent=2))
        return 0
    except (FinalizeError, OSError, csv.Error, sqlite3.Error, zipfile.BadZipFile, KeyError, ValueError) as error:
        print(f"error: {error}", file=os.sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
