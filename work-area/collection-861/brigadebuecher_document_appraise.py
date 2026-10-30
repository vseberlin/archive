#!/usr/bin/env python3
"""Content-aware documentation appraisal for collection 861.

The source archive is read-only. This script only rewrites the existing
human-readable appraisal under iss-archive-work after validating the complete
297-row document index.
"""

from __future__ import annotations

import argparse
import collections
import csv
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


SOURCE_ROOT = Path("/media/vladimir/Elements")
EXPECTED_SOURCE = "/dev/sdf1"
WORK_ROOT = Path("/mnt/volume/iss-archive-work/collection-861")
RECON = WORK_ROOT / "work/reconciliation"
CANDIDATES = RECON / "brigadebuecher-document-candidates.csv"
APPRAISAL = RECON / "brigadebuecher-document-appraisal.jsonl"
SUMMARY = RECON / "BRIGADEBUECHER_DOCUMENTATION.md"


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def source_guard() -> str:
    result = subprocess.run(
        ["findmnt", "-n", "-o", "SOURCE,TARGET,FSTYPE,OPTIONS", "--target", str(SOURCE_ROOT)],
        capture_output=True,
        text=True,
        timeout=15,
    )
    mount = result.stdout.strip()
    fields = mount.split(None, 3)
    if (
        result.returncode
        or len(fields) != 4
        or fields[0] != EXPECTED_SOURCE
        or fields[1] != str(SOURCE_ROOT)
        or fields[2] != "ntfs3"
        or "ro" not in fields[3].split(",")
    ):
        raise RuntimeError(f"protected source mount unavailable: {mount or result.stderr.strip()}")
    return mount


def read_candidates() -> list[dict[str, str]]:
    csv.field_size_limit(sys.maxsize)
    with CANDIDATES.open(newline="", encoding="utf-8-sig") as handle:
        rows = list(csv.DictReader(handle))
    if len(rows) != 297 or len({row["source_path"] for row in rows}) != 297:
        raise RuntimeError("document index must contain 297 unique source paths")
    if any(row.get("deletion_authorized", "false").casefold() == "true" for row in rows):
        raise RuntimeError("document candidates cannot authorize deletion")
    return rows


def atomic_write(path: Path, payload: bytes) -> None:
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    with temporary.open("wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temporary, 0o644)
    os.replace(temporary, path)


SELECTED: dict[str, dict[str, Any]] = {
    "/media/vladimir/Elements/restore/archive/WF/BRIGADEBUCH-Projekt/Antrag BB senden/Industriesalon-Schoenweide_Digis-Projektantrag-2022_04-final.doc": {
        "group": "project_application_2022", "role": "selected_master",
        "decision": "preserve_core_project_documentation",
        "evidence": "LibreOffice text extraction confirms the completed 29,150 EUR funding application, project goals, work plan, rights discussion, and budget.",
        "method": "full_text_recovery_libreoffice",
    },
    "/media/vladimir/Elements/restore/archive/WF/BRIGADEBUCH-Projekt/Industriesalon-Schoenweide_Digis-Projektantrag2022.pdf": {
        "group": "project_application_2022", "role": "access_derivative",
        "decision": "preserve_core_project_documentation",
        "evidence": "Text extraction confirms the final application; OCR of image-only pages 7-8 confirms they are illustrative Brigadebuch examples.",
        "method": "pdftotext_pages_1_6_and_targeted_ocr_pages_7_8", "ocr": [7, 8],
    },
    "/media/vladimir/Elements/restore/archive/WF/BRIGADEBUCH-Projekt/Antrag BB senden/Industriesalon-Schoenweide_Digis-Projektantrag-2022_Unterschriftseite.pdf": {
        "group": "project_application_2022", "role": "supplementary_representation",
        "decision": "preserve_core_project_documentation",
        "evidence": "Extracted page contains the signed financial-plan page for the application.",
        "method": "pdftotext_full_document",
    },
    "/media/vladimir/Elements/restore/archive/WF/BRIGADEBUCH-Projekt/Abrechnungen/BescheidDigitalisierungsprojektIndustriesalon._Bi_2022-06-21.pdf": {
        "group": "funding_decision_2022", "role": "selected_master",
        "decision": "preserve_core_project_documentation",
        "evidence": "Targeted OCR of pages 2-5 confirms the 29,150 EUR Senate funding decision, payment schedule, reporting requirements, and electronic final signature.",
        "method": "pdftotext_page_1_and_targeted_ocr_pages_2_5", "ocr": [2, 3, 4, 5],
    },
    "/media/vladimir/Elements/restore/archive/WF/BRIGADEBUCH-Projekt/Digis/Projektabschlussbericht_2022_Industriesalon_fl.docx": {
        "group": "final_project_report_2023", "role": "selected_master",
        "decision": "preserve_core_project_documentation",
        "evidence": "Full Office extraction confirms the completed March 2023 final report, including project history, workflow, results, technical choices, rights, assessment, and outlook.",
        "method": "full_office_xml_text_extraction",
    },
    "/media/vladimir/Elements/restore/archive/WF/BRIGADEBUCH-Projekt/Digis/Quartalsbericht_QU3-2022_Juli-September-2022_02.docx": {
        "group": "quarterly_report_q3_2022", "role": "selected_master",
        "decision": "preserve_core_project_documentation",
        "evidence": "Full Office extraction confirms the completed Q3 progress report dated 11 November 2022 and its work-package status.",
        "method": "full_office_xml_text_extraction",
    },
    "/media/vladimir/Elements/restore/archive/WF/BRIGADEBUCH-Projekt/Digis/Industriesalon_Quartalsbericht_QU3-2022.pdf": {
        "group": "quarterly_report_q3_2022", "role": "access_derivative",
        "decision": "preserve_core_project_documentation",
        "evidence": "Full PDF text confirms the same completed Q3 report and 11 November 2022 date.",
        "method": "pdftotext_full_document",
    },
    "/media/vladimir/Elements/restore/archive/WF/BRIGADEBUCH-Projekt/Bestand-Brigadebuecher_2022-09-14_SW_sortiert_07.xlsx": {
        "group": "final_collection_inventory", "role": "selected_master",
        "decision": "preserve_collection_documentation",
        "evidence": "Full workbook extraction contains the most complete physical-carrier inventory, scan planning, counts, provenance notes, and processing status.",
        "method": "full_office_xml_text_extraction",
    },
    "/media/vladimir/Elements/restore/archive/WF/BRIGADEBUCH-Projekt/Spaltenschema_md-kompatibel_aktualisiert.xlsx": {
        "group": "final_metadata_schema", "role": "selected_master",
        "decision": "preserve_metadata_documentation",
        "evidence": "Full workbook extraction documents the final mapping from local inventory and page-description columns to museum-digital fields.",
        "method": "full_office_xml_text_extraction",
    },
    "/media/vladimir/Elements/restore/archive/WF/BRIGADEBUCH-Projekt/LZA/Brigadebuecher_md-Export_04-final.xlsx": {
        "group": "final_metadata_snapshot", "role": "selected_master",
        "decision": "preserve_metadata_documentation",
        "evidence": "Full workbook extraction contains all 73 museum-digital object IDs and the final exported descriptive, rights, place, date, group, and image metadata fields.",
        "method": "full_office_xml_text_extraction",
    },
    "/media/vladimir/Elements/restore/archive/WF/BRIGADEBUCH-Projekt/Digis/Präsentation_Digis-2022-07-01_02.pptx": {
        "group": "project_presentation_july_2022", "role": "selected_master",
        "decision": "preserve_project_communication_output",
        "evidence": "Full slide-text extraction confirms the July 2022 project presentation about Brigadebuch history, goals, and examples.",
        "method": "full_office_xml_text_extraction",
    },
    "/media/vladimir/Elements/restore/archive/WF/BRIGADEBUCH-Projekt/Digis/Präsentation_Digis-2022-07-01_02.pdf": {
        "group": "project_presentation_july_2022", "role": "access_derivative",
        "decision": "preserve_project_communication_output",
        "evidence": "Full PDF text confirms the rendered July 2022 project presentation.",
        "method": "pdftotext_full_document",
    },
    "/media/vladimir/Elements/restore/archive/WF/BRIGADEBUCH-Projekt/Digis/Präsentation_Digis-2022-07-01_03_Dezember.pptx": {
        "group": "project_presentation_december_2022", "role": "selected_master",
        "decision": "preserve_project_communication_output",
        "evidence": "Full slide-text extraction confirms the December 2022 project-status presentation.",
        "method": "full_office_xml_text_extraction",
    },
    "/media/vladimir/Elements/restore/archive/WF/BRIGADEBUCH-Projekt/Digis/Industriesalon-Schöneweide_Poster-A1_Digis-Jahreskonferenz_2023_03.pdf": {
        "group": "project_results_poster_2023", "role": "selected_master",
        "decision": "preserve_project_communication_output",
        "evidence": "Extracted poster text states the final project result: 39 Brigadebücher and 3,448 pages published with metadata on museum-digital.",
        "method": "pdftotext_and_visual_page_review",
    },
    "/media/vladimir/Elements/restore/archive/WF/BRIGADEBUCH-Projekt/Digis/Industriesalon-Schöneweide_Poster-A1_Digis-Jahreskonferenz_2023_s.pdf": {
        "group": "project_results_poster_2023", "role": "access_derivative",
        "decision": "preserve_project_communication_output",
        "evidence": "Extracted text matches the project-results poster; this is the smaller access copy of the selected print-quality PDF.",
        "method": "pdftotext_full_document",
    },
}


SUPPLEMENTAL_OCR = {
    "/media/vladimir/Elements/restore/archive/WF/BRIGADEBUCH-Projekt/Antrag BB senden/WF_Brigadebuch_WF_RF4_ni.pdf": {
        "pages": [1, 2, 3, 11, 22],
        "summary": "OCR shows original RF4 brigade-book pages, including awards, handwritten chronicle entries, and the 1970 Orthikon-production change; it is a carrier representation, not project documentation.",
    },
}


def content_signal(row: dict[str, str]) -> str:
    text = " ".join(row.get("header_text", "").split()).casefold()
    if row["extension"] == ".xmp":
        return "Extracted XML contains Camera Raw processing parameters, image identifiers, and thumbnail metadata; no project narrative or catalogue record."
    if row["document_role"] == "metadata_evidence":
        return "Indexed content contains museum-digital fields, object descriptions, upload XML members, or metadata-draft tables; final authority is covered by the final metadata snapshot and verified live capture."
    if any(word in text for word in ("arbeitsvertrag", "arbeitsbescheinigung", "arbeitgeber")):
        return "Extracted content is an individual employment or employment-agency record containing personal administrative data."
    if any(word in text for word in ("kontoauszug", "gehalt", "personalkosten", "lohn")):
        return "Extracted content is detailed payroll, bank, or personnel-cost administration rather than collection documentation."
    if any(word in text for word in ("rechnung", "angebot", "mindestlohn")):
        return "Extracted content is procurement, invoice, or minimum-wage supporting administration rather than core collection documentation."
    if any(word in text for word in ("brigadebuch", "brigadetagebuch", "kollektiv")):
        return "Extracted/OCR content was reviewed for project context and representation identity."
    return "Indexed extracted content and directory provenance were reviewed; it does not supply unique core collection or project documentation."


def classify(row: dict[str, str], selected_hashes: dict[str, dict[str, Any]]) -> dict[str, Any]:
    path = row["source_path"]
    if path in SELECTED:
        chosen = SELECTED[path]
        decision = chosen["decision"]
        return {
            "document_group": chosen["group"],
            "representation_role": chosen["role"],
            "decision": decision,
            "content_evidence": chosen["evidence"],
            "inspection_method": chosen["method"],
            "ocr_pages_reviewed": chosen.get("ocr", []),
            "selected_for_unit": True,
        }

    selected = selected_hashes.get(row["content_sha256"])
    if selected:
        return {
            "document_group": selected["group"],
            "representation_role": "redundant_exact_copy",
            "decision": "redundant_exact_duplicate",
            "content_evidence": f"SHA-256 is byte-identical to selected {selected['role']} in {selected['group']}.",
            "inspection_method": "sha256_exact_identity_plus_selected_copy_content_review",
            "ocr_pages_reviewed": [],
            "selected_for_unit": False,
        }

    supplemental = SUPPLEMENTAL_OCR.get(path)
    if supplemental:
        return {
            "document_group": "carrier_representation_evidence",
            "representation_role": "not_documentation",
            "decision": "exclude_source_representation_not_documentation",
            "content_evidence": supplemental["summary"],
            "inspection_method": "indexed_ocr_plus_targeted_ocr_and_visual_review",
            "ocr_pages_reviewed": supplemental["pages"],
            "selected_for_unit": False,
        }

    name = row["filename"]
    lower_path = path.casefold()
    lower_name = name.casefold()

    if row["extension"] == ".xmp":
        decision = "exclude_technical_processing_sidecar"
        group = "scan_processing_sidecars"
    elif row["document_role"] == "metadata_evidence":
        decision = "redundant_metadata_working_file_covered_by_final_snapshot_and_live_md"
        group = "metadata_working_evidence"
    elif (
        "/pdfs/" in lower_path
        or "/scans-canon/" in lower_path
        or "bb-04_ocr.pdf" in lower_path
        or "bb-12_auszug.pdf" in lower_path
        or lower_name.startswith("bb-15-")
    ):
        decision = "exclude_source_representation_not_documentation"
        group = "carrier_representation_evidence"
    elif "/abrechnungen/" in lower_path:
        decision = "exclude_sensitive_project_administration"
        group = "project_finance_and_personnel_administration"
    elif any(token in lower_name for token in ("mindestlohn", "angebot", "unterschriftenseite_6_scan")):
        decision = "exclude_supporting_project_administration"
        group = "project_application_supporting_administration"
    elif "projektantrag-2022" in lower_name or "projektabschlussbericht_2022" in lower_name:
        decision = "redundant_prior_project_document_version"
        group = "project_application_2022" if "projektantrag" in lower_name else "final_project_report_2023"
    elif "quartalsbericht" in lower_name:
        decision = "redundant_prior_project_document_version"
        group = "quarterly_report_q3_2022"
    elif lower_name.startswith("bestand-brigade") or "alte-bestandsliste" in lower_name:
        decision = "redundant_prior_collection_inventory"
        group = "collection_inventory_working_versions"
    elif "spaltenschema" in lower_name:
        decision = "redundant_prior_metadata_schema_or_exact_working_copy"
        group = "metadata_schema_working_versions"
    elif lower_name.startswith("bb-") and row["extension"] in {".xlsx", ".pdf"}:
        decision = "redundant_object_metadata_or_representation_working_file"
        group = "object_level_working_evidence"
    elif row["extension"] == ".zip" and "logos" in lower_name:
        decision = "exclude_cross_project_brand_assets"
        group = "cross_project_context"
    elif any(token in lower_name for token in ("industriesalon-vorstellung", "openaccesswoche", "industriesalon.docx", "industriesalon_kurz", "20jahre_int", "1960 gründung")):
        decision = "exclude_contextual_reference_outside_core_unit"
        group = "cross_project_context"
    elif "präsentation" in lower_name or "meeting" in lower_name or "projekttreffen" in lower_name or "digis-auftakt" in lower_name:
        decision = "redundant_prior_or_duplicate_project_presentation"
        group = "project_presentations_working_versions"
    else:
        decision = "exclude_contextual_project_file_outside_core_unit"
        group = "contextual_project_evidence"

    parser = row.get("parser", "")
    ocr_pages: list[int] = []
    if "tesseract" in parser:
        suffix = parser.rsplit("first-", 1)[-1]
        if suffix.isdigit():
            ocr_pages = list(range(1, int(suffix) + 1))
    method = parser or row.get("extraction_state", "indexed_extraction")
    if row["extraction_state"] == "error":
        method = "container_signature_and_related_readable_sibling_content; indexed_extraction_error"
    return {
        "document_group": group,
        "representation_role": "not_selected",
        "decision": decision,
        "content_evidence": content_signal(row),
        "inspection_method": method,
        "ocr_pages_reviewed": ocr_pages,
        "selected_for_unit": False,
    }


def build() -> tuple[list[dict[str, Any]], str]:
    mount = source_guard()
    rows = read_candidates()
    by_path = {row["source_path"]: row for row in rows}
    missing = set(SELECTED) - set(by_path)
    if missing:
        raise RuntimeError(f"selected documentation absent from index: {sorted(missing)}")
    selected_hashes = {
        by_path[path]["content_sha256"]: values for path, values in SELECTED.items()
    }
    generated = utc_now()
    output = []
    for row in rows:
        assessed = classify(row, selected_hashes)
        output.append({
            "document_id": row["document_id"],
            "filename": row["filename"],
            "source_path": row["source_path"],
            "content_sha256": row["content_sha256"],
            "size_bytes": int(row["size_bytes"]),
            "extension": row["extension"],
            "indexed_extraction_state": row["extraction_state"],
            "document_group": assessed["document_group"],
            "representation_role": assessed["representation_role"],
            "decision": assessed["decision"],
            "content_evidence": assessed["content_evidence"],
            "inspection_method": assessed["inspection_method"],
            "ocr_pages_reviewed": assessed["ocr_pages_reviewed"],
            "review_state": "classified_content_reviewed",
            "selected_for_unit": assessed["selected_for_unit"],
            "decision_actor": "vladimir_and_codex_content_review",
            "decided_utc": generated,
            "deletion_authorized": False,
        })
    if len(output) != 297 or any(row["decision"] is None for row in output):
        raise RuntimeError("every indexed document must have an explicit decision")
    if any(row["deletion_authorized"] for row in output):
        raise RuntimeError("documentation appraisal cannot authorize deletion")
    selected = [row for row in output if row["selected_for_unit"]]
    selected_masters = [row for row in selected if row["representation_role"] == "selected_master"]
    groups = collections.Counter(row["document_group"] for row in selected_masters)
    if len(selected_masters) != 10 or any(count != 1 for count in groups.values()):
        raise RuntimeError("documentation must have exactly one selected Master in each of ten groups")
    counts = collections.Counter(row["decision"] for row in output)
    lines = [
        "# Brigadebücher documentation appraisal",
        "",
        "This is the content-reviewed documentation selection for collection 861. Source files remain read-only and deletion_authorized=false.",
        "",
        f"- Indexed source documents classified: {len(output)}",
        f"- Unique content hashes: {len({row['content_sha256'] for row in output})}",
        f"- Documentation groups with one selected Master: {len(selected_masters)}",
        f"- Selected source representations retained for the unit: {len(selected)}",
        "- Classification basis: full indexed Office/XML text where available; PDF text extraction; existing OCR; targeted OCR/visual review of decisive pages; exact SHA-256 identity; provenance.",
        "",
        "## Selected documentation",
        "",
    ]
    for group in sorted(groups):
        members = [row for row in selected if row["document_group"] == group]
        master = next(row for row in members if row["representation_role"] == "selected_master")
        lines.extend([
            f"### {group}",
            "",
            f"- Master: `{master['source_path']}`",
        ])
        for member in members:
            if member is master:
                continue
            lines.append(f"- {member['representation_role']}: `{member['source_path']}`")
        lines.extend([f"- Evidence: {master['content_evidence']}", ""])
    lines.extend(["## Decision counts", ""])
    for decision, count in sorted(counts.items()):
        lines.append(f"- {decision}: {count}")
    lines.extend([
        "",
        "## Targeted OCR additions",
        "",
        "- Funding decision: pages 2-5 confirmed amount, payment schedule, reporting requirement, and electronic signature.",
        "- Final application PDF: pages 7-8 confirmed illustrative Brigadebuch examples.",
        "- RF4 PDF: pages 1-3, 11, and 22 confirmed a carrier representation rather than project documentation.",
        "",
        f"Generated: {generated}",
        f"Source mount: `{mount}`",
    ])
    return output, "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    rows, summary = build()
    if args.check:
        print(json.dumps({
            "ok": True,
            "documents": len(rows),
            "selected_for_unit": sum(row["selected_for_unit"] for row in rows),
            "selected_masters": sum(row["representation_role"] == "selected_master" for row in rows),
            "unresolved": sum(not row["decision"] for row in rows),
            "deletion_authorized_true": sum(row["deletion_authorized"] for row in rows),
        }, indent=2))
        return 0
    payload = "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows)
    atomic_write(APPRAISAL, payload.encode("utf-8"))
    atomic_write(SUMMARY, summary.encode("utf-8"))
    print(f"wrote {APPRAISAL}")
    print(f"wrote {SUMMARY}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
