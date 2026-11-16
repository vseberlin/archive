#!/usr/bin/env python3
"""Correct Kurt Schwarz documentation packages from the 2026-08-14 OCR review."""

from __future__ import annotations

import csv
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
import zipfile
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, "/mnt/volume/iss-archive-work/kurt-schwarz/tools")
import documentation_finalize as base


UNIT = Path("/mnt/volume/iss-archive/units/kurt-schwarz")
WORK = Path("/mnt/volume/iss-archive-work/kurt-schwarz")
SOURCE_ROOT = Path("/media/vladimir/Elements/restore/archive")
REVIEW_RUN = WORK / "work/reconciliation/documentation/20260814T-kurt-schwarz-documentation-final"
OCR_CSV = REVIEW_RUN / "ocr-document-review.csv"
RUN = WORK / "work/reconciliation/documentation/20260814T-kurt-schwarz-documentation-ocr-correction"
STAGE = RUN / "stage"
DB = UNIT / "db-restore/RESTORE.sqlite"
REVIEW_DB = WORK / "work/reconciliation/unit-reconciliation-v1/reconciliation.sqlite"

PACKAGE_PATHS = {
    "historical": "documents/historical/drafts-and-superseded.zip",
    "contracts": "documents/restricted/contracts.zip",
    "finance": "documents/restricted/finance.zip",
    "personal": "documents/restricted/personal.zip",
}
RIGHTS_NAMES = {
    "folgerechte.csv",
    "onlinerechte.csv",
    "reproduktionsrechte.xlsx",
    "vg-bildkunst_reproduktionsrechte.csv",
}
WRONG_FINAL = "documents/project-final/08-reproduction-rights.csv"
WRONG_SOURCE = str(SOURCE_ROOT / "WF/Kurt Schwarz/VG-Bildkunst_Reproduktionsrechte.csv")
REPLACED_PATHS = set(PACKAGE_PATHS.values()) | {"documents/CONTENTS.md"}


class CorrectionError(RuntimeError):
    pass


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def payload(rows: list[dict[str, object]], fields: tuple[str, ...] | list[str]) -> bytes:
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore", quoting=csv.QUOTE_ALL)
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue().encode("utf-8")


def write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def basename(source_path: str) -> str:
    return Path(source_path.split("::", 1)[-1]).name.casefold()


def exclusion(row: dict[str, str]) -> tuple[str, str]:
    name = basename(row["original_source_path"])
    if name in RIGHTS_NAMES:
        return "historical_reference_noise", "general rights-holder registry; no Kurt Schwarz project record"
    if row["package"] == "personal" and row["suggested_disposition"] != "retain_in_kurt_unit":
        if row["suggested_disposition"] == "cross_unit_reference":
            return "cross_unit_reference", "OCR identifies WF technical or correspondence material belonging outside the Kurt project"
        return "historical_reference_noise", "full-text/OCR review found no Kurt Schwarz project evidence"
    if name == "arbeitsvertrag_wf-sender_digitalisierung.docx":
        return "cross_unit_reference", "employment contract explicitly concerns the WF-Sender digitization project"
    if name == "erstellungshilfe_projektantrag_2021-1.pdf":
        return "historical_reference_noise", "generic project-application template, not a Kurt project record"
    if name == "weitere fotos archiv aus dem werk für fernsehelektronik.zip":
        return "cross_unit_reference", "mixed WF image reference package, not Kurt project documentation"
    if name == "muster_cc-lizenzen.docx":
        return "historical_reference_noise", "generic CC licence template, not a Kurt project record"
    if name.startswith("category-karl schmidt-rottluff") and name.endswith(".url"):
        return "historical_reference_noise", "unrelated generic Wikimedia reference"
    return "", ""


def verify_source_read_only() -> str:
    result = subprocess.run(
        ["findmnt", "-no", "SOURCE,FSTYPE,OPTIONS", "/media/vladimir/Elements"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    fields = result.split(None, 2)
    if len(fields) != 3 or fields[1] != "ntfs3" or "ro" not in fields[2].split(","):
        raise CorrectionError(f"source mount is not ntfs3 read-only: {result}")
    return result


def classify_ocr() -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    rows = read_csv(OCR_CSV)
    selected, excluded = [], []
    for row in rows:
        state, reason = exclusion(row)
        corrected = dict(row)
        corrected["corrected_disposition"] = state or "retain_in_kurt_unit"
        corrected["corrected_final_relative_path"] = "" if state else PACKAGE_PATHS[row["package"]]
        corrected["correction_basis"] = reason or "content/OCR and project-chain review confirms Kurt project relation"
        (excluded if state else selected).append(corrected)
    return selected, excluded


def build_package(group: str, rows: list[dict[str, str]]) -> dict[str, object]:
    target = STAGE / PACKAGE_PATHS[group]
    target.parent.mkdir(parents=True, exist_ok=True)
    by_hash: dict[str, list[dict[str, str]]] = {}
    for row in sorted(rows, key=lambda item: item["original_source_path"]):
        by_hash.setdefault(row["source_sha256"], []).append(row)
    member_for_hash = {}
    manifest = []
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
        for number, (digest, occurrences) in enumerate(sorted(by_hash.items()), 1):
            representative = occurrences[0]
            member = base.safe_member_name(number, representative["original_filename"])
            data = base.source_bytes(representative["original_source_path"])
            actual = base.sha256_path_bytes(data) if hasattr(base, "sha256_path_bytes") else "sha256:" + __import__("hashlib").sha256(data).hexdigest()
            if digest and actual != digest:
                raise CorrectionError(f"source hash mismatch: {representative['original_source_path']}")
            archive.writestr(member, data)
            member_for_hash[digest] = member
        for row in sorted(rows, key=lambda item: item["original_source_path"]):
            manifest.append({
                "member": member_for_hash[row["source_sha256"]],
                "original_filename": row["original_filename"],
                "original_source_path": row["original_source_path"],
                "source_sha256": row["source_sha256"],
                "relationship": "direct" if group != "historical" else "direct_or_contextual",
                "extraction_method": row["extraction_method"],
                "ocr_pages": row["ocr_pages"],
                "content_review": row["content_signal"],
                "condition": row["condition"],
                "duplicate_payload": "true" if len(by_hash[row["source_sha256"]]) > 1 else "false",
                "deletion_authorized": "false",
            })
        fields = (
            "member", "original_filename", "original_source_path", "source_sha256", "relationship",
            "extraction_method", "ocr_pages", "content_review", "condition", "duplicate_payload",
            "deletion_authorized",
        )
        archive.writestr("MANIFEST.csv", payload(manifest, fields))
        title = "Historical drafts and superseded documentation" if group == "historical" else f"Restricted {group} documentation"
        readme = (
            f"# {title}\n\n"
            "Exact source bytes are stored once per unique payload. MANIFEST.csv records every reviewed original "
            "source occurrence and maps duplicate occurrences to the retained member. Content and OCR review was "
            "used to exclude unrelated reference material. No source deletion is authorized.\n\n"
            f"Unique payload members: {len(by_hash)}\n\n"
            f"Source occurrences represented: {len(rows)}\n"
        )
        archive.writestr("README.md", readme.encode("utf-8"))
    with zipfile.ZipFile(target) as archive:
        bad = archive.testzip()
        if bad:
            raise CorrectionError(f"ZIP CRC failure {target}: {bad}")
    return {"group": group, "unique_payloads": len(by_hash), "source_occurrences": len(rows)}


def make_contents(documents: list[dict[str, str]], package_stats: dict[str, dict[str, object]], excluded: list[dict[str, str]]) -> str:
    by_path = {row["final_relative_path"]: row for row in documents}
    lines = [
        "# Kurt Schwarz documentation contents", "",
        "This index separates final project documentation from restricted administration and historical drafts. "
        "Selection is based on content/OCR and provenance evidence, not folder names alone. Source paths and "
        "dispositions are preserved in `../db-restore/manifests/documents.csv`, the package manifests, and the "
        "source/drain manifests. No source deletion is authorized.", "",
        "## Project-final records", "",
    ]
    for path in sorted(p for p in by_path if p.startswith("documents/project-final/")):
        row = by_path[path]
        lines.extend([
            f"### `{Path(path).name}`", "", row["description"], "",
            f"- Date: {row['date'] or 'not established'}",
            f"- Original source: `{row['source_path']}`",
            "- Relationship: direct Kurt Schwarz project record", "",
        ])
    lines.extend(["## Restricted records", ""])
    for group in ("contracts", "finance", "personal"):
        path = PACKAGE_PATHS[group]
        stat = package_stats[group]
        lines.extend([
            f"### `{Path(path).name}`", "", by_path[path]["description"], "",
            f"- Unique payload members: {stat['unique_payloads']}",
            f"- Source occurrences represented: {stat['source_occurrences']}",
            "- Original sources and duplicate mappings: package `MANIFEST.csv`",
            "- Access: restricted", "",
        ])
    stat = package_stats["historical"]
    lines.extend([
        "## Historical documentation", "", "### `drafts-and-superseded.zip`", "",
        by_path[PACKAGE_PATHS["historical"]]["description"], "",
        f"- Unique payload members: {stat['unique_payloads']}",
        f"- Source occurrences represented: {stat['source_occurrences']}",
        "- Includes drafts, superseded generations, legacy catalogue components, and project research evidence.",
        "- Original sources and duplicate mappings: package `MANIFEST.csv`", "",
        "## Reviewed but not accessioned", "",
        "The OCR correction removed unrelated WF/reference material and general rights registries from the unit "
        "payloads. Their source paths and reasons remain in the source/drain manifests.", "",
    ])
    counts = Counter(row["corrected_disposition"] for row in excluded)
    lines.extend([
        f"- `cross_unit_reference`: {counts['cross_unit_reference']}",
        f"- `historical_reference_noise`: {counts['historical_reference_noise'] + 1}",
        "- The additional noise row is the mistakenly accessioned direct VG Bild-Kunst CSV.", "",
        "## Metadata exports", "",
        "See `../export/md/README.md` for the selected museum-digital JSON, XML/CSV upload packages, final CSV "
        "exports, final local metadata spreadsheet, and condition-noted LIDO-labelled source evidence.", "",
    ])
    return "\n".join(lines)


def update_documents(package_stats: dict[str, dict[str, object]], selected: list[dict[str, str]], excluded: list[dict[str, str]]) -> list[dict[str, str]]:
    rows = [row for row in read_csv(UNIT / "db-restore/manifests/documents.csv") if row["final_relative_path"] != WRONG_FINAL]
    by_path = {row["final_relative_path"]: row for row in rows}
    descriptions = {
        "historical": "Drafts, superseded versions, legacy catalogue evidence, and content-verified contextual research",
        "contracts": "Contracts, grant records, offers, commissions, and project administration",
        "finance": "Project invoices, accounts, budgets, payroll, time records, and receipts",
        "personal": "Kurt-related biographical records and project employment documentation",
    }
    for group, path in PACKAGE_PATHS.items():
        row = by_path[path]
        target = STAGE / path
        row.update({
            "description": descriptions[group],
            "package_member_count": str(package_stats[group]["unique_payloads"]),
            "size_bytes": str(target.stat().st_size),
            "sha256": base.sha256_path(target),
            "condition": (
                f"Content/OCR reviewed; {package_stats[group]['source_occurrences']} source occurrences map to "
                f"{package_stats[group]['unique_payloads']} unique payloads."
            ),
            "deletion_authorized": "false",
        })
    contents = make_contents(rows, package_stats, excluded)
    write(STAGE / "documents/CONTENTS.md", contents.encode("utf-8"))
    c = by_path["documents/CONTENTS.md"]
    c.update({
        "date": "2026-08-14", "size_bytes": str((STAGE / "documents/CONTENTS.md").stat().st_size),
        "sha256": base.sha256_path(STAGE / "documents/CONTENTS.md"),
        "description": "Human-readable table of contents for the content/OCR-reviewed documentation section",
    })
    return rows


def corrected_source_rows(documents: list[dict[str, str]], selected: list[dict[str, str]], excluded: list[dict[str, str]]) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    source_rows = read_csv(UNIT / "db-restore/manifests/source-disposition.csv")
    drain_rows = read_csv(UNIT / "db-restore/manifests/drain-list.csv")
    final_hashes = {row["final_relative_path"]: row["sha256"] for row in documents}
    corrections: dict[str, dict[str, str]] = {}
    for item in selected + excluded:
        path = item["original_source_path"]
        final = item["corrected_final_relative_path"]
        corrections[path] = {
            "source_role": (
                f"restricted_{item['package']}" if final and item["package"] != "historical"
                else "historical" if final else item["corrected_disposition"]
            ),
            "final_path": final,
            "preservation_status": "accessioned_documentation" if final else "reviewed_not_accessioned",
            "preservation_basis": item["correction_basis"] + f"; extraction={item['extraction_method']}; OCR pages={item['ocr_pages']}",
            "final_sha256": final_hashes.get(final, ""),
            "exact_copy_proven": "false",
            "drain_state": "source_covered_by_documentation_accession" if final else item["corrected_disposition"],
            "deletion_authorized": "false",
            "notes": "Documentation OCR correction completed 2026-08-14. No deletion is authorized.",
        }
    corrections[WRONG_SOURCE] = {
        "source_role": "historical_reference_noise", "final_path": "",
        "preservation_status": "reviewed_not_accessioned",
        "preservation_basis": "general VG Bild-Kunst rights-holder registry; no Kurt Schwarz project record",
        "final_sha256": "", "exact_copy_proven": "false", "drain_state": "historical_reference_noise",
        "deletion_authorized": "false",
        "notes": "Corrected mistaken project-final classification 2026-08-14. Source remains read-only; no deletion is authorized.",
    }
    found = set()
    for row in source_rows:
        change = corrections.get(row["source_path"])
        if change:
            row.update(change); found.add(row["source_path"])
    missing = set(corrections) - found
    if missing:
        raise CorrectionError(f"source disposition missing corrected paths: {sorted(missing)[:5]}")
    source_by_path = {row["source_path"]: row for row in source_rows}
    drain_by_path = {row["source_path"]: row for row in drain_rows}
    for path in corrections:
        drain_by_path[path] = dict(source_by_path[path])
    return source_rows, list(drain_by_path.values())


def update_catalog(documents: list[dict[str, str]], source_rows: list[dict[str, str]], drain_rows: list[dict[str, str]], completed: str) -> None:
    old_doc_paths = {row["final_relative_path"] for row in read_csv(UNIT / "db-restore/manifests/documents.csv")}
    docs = {row["final_relative_path"]: row for row in documents}
    reps = [row for row in read_csv(UNIT / "db-restore/manifests/representations.csv") if row["final_relative_path"] != WRONG_FINAL]
    rep_by_path = {row["final_relative_path"]: row for row in reps}
    for path, doc in docs.items():
        rep = rep_by_path.get(path)
        if not rep:
            raise CorrectionError(f"representation missing: {path}")
        if path in REPLACED_PATHS:
            rep.update({
                "completed_at": completed, "representation_key": doc["sha256"],
                "final_sha256": doc["sha256"], "size_bytes": doc["size_bytes"],
                "mtime_ns": str(time.time_ns()), "basis": doc["description"],
            })
    checksums = [row for row in read_csv(UNIT / "db-restore/checksums.csv") if row["path"] != WRONG_FINAL]
    checksum_by_path = {row["path"]: row for row in checksums}
    for path in REPLACED_PATHS:
        doc = docs[path]
        checksum_by_path[path].update({"sha256": doc["sha256"], "size_bytes": doc["size_bytes"], "content_role": doc["file_role"]})
    catalog = [row for row in read_csv(UNIT / "CATALOG.csv") if row["final_relative_path"] != WRONG_FINAL]
    for row in catalog:
        path = row["final_relative_path"]
        if path in REPLACED_PATHS:
            doc, rep = docs[path], rep_by_path[path]
            row.update({
                "size_bytes": doc["size_bytes"], "final_sha256": doc["sha256"],
                "description": doc["description"], "date_text": doc["date"],
            })
            for field in base.REP_FIELDS:
                row[f"representation_{field}"] = rep[field]
    catalog_fields = list(catalog[0])
    write(STAGE / "CATALOG.csv", payload(catalog, catalog_fields))
    write(STAGE / "CATALOG.xlsx", base.xlsx_payload(catalog_fields, catalog, "Catalog"))
    write(STAGE / "db-restore/RESTORE.csv", payload(catalog, catalog_fields))
    write(STAGE / "db-restore/RESTORE.xlsx", base.xlsx_payload(catalog_fields, catalog, "Restore"))
    write(STAGE / "db-restore/manifests/representations.csv", payload(reps, base.REP_FIELDS))
    write(STAGE / "db-restore/checksums.csv", payload(checksums, base.CHECKSUM_FIELDS))
    write(STAGE / "db-restore/manifests/source-disposition.csv", payload(source_rows, base.SOURCE_FIELDS))
    write(STAGE / "db-restore/manifests/drain-list.csv", payload(drain_rows, base.SOURCE_FIELDS))
    write(STAGE / "db-restore/manifests/documents.csv", payload(documents, base.DOCUMENT_FIELDS))

    source_con = sqlite3.connect(DB)
    target_con = sqlite3.connect(STAGE / "db-restore/RESTORE.sqlite")
    try:
        source_con.backup(target_con)
        for table, rows, fields in (
            ("representations", reps, base.REP_FIELDS), ("checksums", checksums, base.CHECKSUM_FIELDS),
            ("source_disposition", source_rows, base.SOURCE_FIELDS), ("drain_list", drain_rows, base.SOURCE_FIELDS),
        ):
            target_con.execute(f'DELETE FROM "{table}"')
            columns = ",".join(f'"{field}"' for field in fields)
            marks = ",".join("?" for _ in fields)
            target_con.executemany(
                f'INSERT INTO "{table}" ({columns}) VALUES ({marks})',
                [[row.get(field, "") for field in fields] for row in rows],
            )
        target_con.commit()
        if target_con.execute("pragma quick_check").fetchone()[0] != "ok":
            raise CorrectionError("staged SQLite quick_check failed")
        if list(target_con.execute("pragma foreign_key_check")):
            raise CorrectionError("staged SQLite foreign keys failed")
        sql = "\n".join(target_con.iterdump()) + "\n"
    finally:
        target_con.close(); source_con.close()
    write(STAGE / "db-restore/RESTORE.sql", sql.encode("utf-8"))


def update_readme(package_stats: dict[str, dict[str, object]]) -> None:
    path = UNIT / "00_README.md"
    text = path.read_text(encoding="utf-8")
    marker = "\n## Documentation and metadata exports\n"
    text = text.split(marker, 1)[0].rstrip() + marker + f"""

The content/OCR-reviewed documentation is indexed in `documents/CONTENTS.md` and `db-restore/manifests/documents.csv`. It contains 7 directly readable final project records, three restricted packages, and one historical package. Exact duplicate payloads are stored once per ZIP while every source occurrence remains in the package and source/drain manifests.

- restricted contracts: {package_stats['contracts']['unique_payloads']} payloads from {package_stats['contracts']['source_occurrences']} source occurrences
- restricted finance: {package_stats['finance']['unique_payloads']} payloads from {package_stats['finance']['source_occurrences']} source occurrences
- restricted personal: {package_stats['personal']['unique_payloads']} payloads from {package_stats['personal']['source_occurrences']} source occurrences
- historical drafts/reference: {package_stats['historical']['unique_payloads']} payloads from {package_stats['historical']['source_occurrences']} source occurrences

`export/md/` preserves the dated current museum-digital JSON/CSV snapshot, selected final CSV exports, received XML/CSV upload containers, the final 2025 local metadata export, and condition-noted LIDO-labelled source evidence. The object/file `CATALOG.csv` and XLSX/restore companions include all current object metadata plus the complete provenance fields held in SQLite.

The general VG Bild-Kunst rights-holder registry and unrelated WF/reference records are not unit payloads. Their reviewed source paths remain in `drain-list.csv` and `source-disposition.csv`; every row remains `deletion_authorized=false`.
"""
    write(STAGE / "00_README.md", text.encode("utf-8"))


def validate(documents: list[dict[str, str]], selected: list[dict[str, str]], excluded: list[dict[str, str]], package_stats: dict[str, dict[str, object]], mount: str) -> dict[str, object]:
    errors = []
    for path in PACKAGE_PATHS.values():
        with zipfile.ZipFile(STAGE / path) as archive:
            if archive.testzip(): errors.append(f"ZIP CRC error: {path}")
    if any(row["final_relative_path"] == WRONG_FINAL for row in documents): errors.append("wrong rights document remains")
    if len(documents) != 36: errors.append(f"document count is {len(documents)}, expected 36")
    catalog = read_csv(STAGE / "CATALOG.csv")
    reps = read_csv(STAGE / "db-restore/manifests/representations.csv")
    checksums = read_csv(STAGE / "db-restore/checksums.csv")
    disposition = read_csv(STAGE / "db-restore/manifests/source-disposition.csv")
    drain = read_csv(STAGE / "db-restore/manifests/drain-list.csv")
    if len(catalog) != 3906 or len(reps) != 3906 or len(checksums) != 3906:
        errors.append(f"catalog/reps/checksums counts: {len(catalog)}/{len(reps)}/{len(checksums)}")
    if sum(r["file_role"] == "preservation_master" for r in catalog) != 1935: errors.append("master count changed")
    if sum(r["file_role"] == "access_derivative" for r in catalog) != 1935: errors.append("access count changed")
    if any(r["deletion_authorized"] != "false" for r in disposition + drain + documents): errors.append("deletion authorization found")
    reviewed = {r["source_path"] for r in read_csv(REVIEW_RUN / "reviewed-documents.csv")}
    if not reviewed <= {r["source_path"] for r in drain}: errors.append("drain does not cover all reviewed documents")
    doc_paths = {r["final_relative_path"] for r in documents}
    if doc_paths != {r["final_relative_path"] for r in reps if r["final_relative_path"] in doc_paths}: errors.append("document representations disagree")
    if doc_paths != {r["path"] for r in checksums if r["path"] in doc_paths}: errors.append("document checksums disagree")
    con = sqlite3.connect(STAGE / "db-restore/RESTORE.sqlite")
    try:
        quick = con.execute("pragma quick_check").fetchone()[0]
        sqlite_reps = con.execute("select count(*) from representations").fetchone()[0]
    finally: con.close()
    if quick != "ok" or sqlite_reps != len(reps): errors.append("SQLite validation mismatch")
    result = {
        "result": "ok" if not errors else "error", "errors": errors,
        "source_mount": mount, "source_modified": False, "deletion_authorized": False,
        "ocr_review_rows": len(selected) + len(excluded), "retained_source_occurrences": len(selected),
        "excluded_source_occurrences": len(excluded) + 1, "package_stats": package_stats,
        "documents": len(documents), "catalog_rows": len(catalog), "master_rows": 1935,
        "access_rows": 1935, "reviewed_drain_paths": len(reviewed), "sqlite_quick_check": quick,
    }
    if errors: raise CorrectionError(json.dumps(result, ensure_ascii=False))
    return result


def backup_sqlite(source: Path, target: Path) -> None:
    src = sqlite3.connect(source); dst = sqlite3.connect(target)
    try: src.backup(dst)
    finally: dst.close(); src.close()


def append_workbench_corrections(excluded: list[dict[str, str]], completed: str) -> int:
    desired = {
        row["original_source_path"]: ("cross_unit_reference" if row["corrected_disposition"] == "cross_unit_reference" else "exclude", row["correction_basis"])
        for row in excluded
    }
    desired[WRONG_SOURCE] = ("exclude", "general VG Bild-Kunst rights-holder registry; no Kurt Schwarz project record")
    con = sqlite3.connect(REVIEW_DB)
    con.row_factory = sqlite3.Row
    inserted = 0
    try:
        candidates = {r["source_path"]: dict(r) for r in con.execute("select * from document_candidates")}
        con.execute("begin immediate")
        for path, (disposition, reason) in desired.items():
            candidate = candidates.get(path)
            if not candidate: continue
            latest = con.execute(
                "select disposition from document_decisions where document_id=? order by decision_id desc limit 1",
                (candidate["document_id"],),
            ).fetchone()
            if latest and latest["disposition"] == disposition: continue
            con.execute(
                "insert into document_decisions(document_id,disposition,actor,reason,decided_at) values(?,?,?,?,?)",
                (candidate["document_id"], disposition, "documentation-ocr-correction-v1", reason, completed),
            )
            inserted += 1
        con.commit()
    except Exception:
        con.rollback(); raise
    finally: con.close()
    return inserted


def publish(excluded: list[dict[str, str]], completed: str) -> int:
    backup_sqlite(DB, RUN / "RESTORE.pre-ocr-correction.sqlite")
    backup_sqlite(REVIEW_DB, RUN / "reconciliation.pre-ocr-correction.sqlite")
    shutil.copyfile(UNIT / WRONG_FINAL, RUN / "removed-08-reproduction-rights.csv")
    replacements = [
        "00_README.md", "CATALOG.csv", "CATALOG.xlsx", "documents/CONTENTS.md",
        *PACKAGE_PATHS.values(), "db-restore/RESTORE.csv", "db-restore/RESTORE.xlsx",
        "db-restore/RESTORE.sqlite", "db-restore/RESTORE.sql", "db-restore/checksums.csv",
        "db-restore/manifests/representations.csv", "db-restore/manifests/source-disposition.csv",
        "db-restore/manifests/drain-list.csv", "db-restore/manifests/documents.csv",
    ]
    for relative in replacements:
        source, target = STAGE / relative, UNIT / relative
        temporary = target.with_name(f".{target.name}.ocr-correction-{os.getpid()}")
        shutil.copyfile(source, temporary)
        os.replace(temporary, target)
    (UNIT / WRONG_FINAL).unlink()
    return append_workbench_corrections(excluded, completed)


def main() -> int:
    if RUN.exists():
        print(f"error: run already exists: {RUN}", file=sys.stderr); return 1
    RUN.mkdir(parents=True); STAGE.mkdir()
    try:
        mount = verify_source_read_only()
        selected, excluded = classify_ocr()
        review_fields = list((selected or excluded)[0])
        write(RUN / "ocr-corrected-dispositions.csv", payload(selected + excluded, review_fields))
        package_stats = {}
        for group in PACKAGE_PATHS:
            package_stats[group] = build_package(group, [r for r in selected if r["package"] == group])
        documents = update_documents(package_stats, selected, excluded)
        source_rows, drain_rows = corrected_source_rows(documents, selected, excluded)
        completed = now()
        update_catalog(documents, source_rows, drain_rows, completed)
        update_readme(package_stats)
        result = validate(documents, selected, excluded, package_stats, mount)
        result["completed_at"] = completed
        write(RUN / "validation.pre-publish.json", (json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode())
        inserted = publish(excluded, completed)
        result["workbench_correction_decisions_appended"] = inserted
        result["wrong_project_final_removed"] = WRONG_FINAL
        result["removed_copy_backup"] = str(RUN / "removed-08-reproduction-rights.csv")
        write(RUN / "completion.json", (json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode())
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    except Exception as error:
        print(f"error: {error}", file=sys.stderr); return 1


if __name__ == "__main__":
    raise SystemExit(main())
