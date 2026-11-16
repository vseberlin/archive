#!/usr/bin/env python3
"""Aggregate collection 861 into physical carriers and compare metadata.

Inputs are the frozen museum-digital capture and the read-only index projection
created by ``brigadebuecher_reconcile.py``. Outputs are JSON/JSONL and Markdown;
no source file or museum-digital record is changed and no master is selected.
"""

from __future__ import annotations

import collections
import csv
import functools
import io
import json
import os
import re
import subprocess
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Iterable


WORK = Path("/mnt/volume/iss-archive-work/collection-861")
RECON = WORK / "work/reconciliation"
CAPTURE = RECON / "museum-digital/runs/20260823T093033Z-collection-861"
SOURCE = Path("/media/vladimir/Elements")
FINAL_METADATA = SOURCE / (
    "restore/archive/WF/BRIGADEBUCH-Projekt/LZA/"
    "Brigadebuecher_md-Export_04-final.xlsx"
)
XLSX_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


class CarrierError(RuntimeError):
    pass


def ensure_source_guard() -> str:
    result = subprocess.run(
        [
            "findmnt", "-n", "-o", "SOURCE,TARGET,FSTYPE,OPTIONS",
            "--target", str(SOURCE),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    fields = result.stdout.strip().split(maxsplit=3)
    if (
        len(fields) != 4
        or fields[0] != "/dev/sdf1"
        or fields[1] != str(SOURCE)
        or fields[2] != "ntfs3"
        or "ro" not in fields[3].split(",")
    ):
        raise CarrierError(f"protected source mount check failed: {result.stdout.strip()}")
    return result.stdout.strip()


def atomic_write(path: Path, payload: bytes) -> None:
    if path.exists():
        raise CarrierError(f"refusing to replace existing output: {path}")
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    with temporary.open("xb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    payload = "".join(
        json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows
    ).encode("utf-8")
    atomic_write(path, payload)


def read_csv(name: str) -> list[dict[str, str]]:
    csv.field_size_limit(64 * 1024 * 1024)
    with (RECON / name).open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def base_inventory(value: str) -> str:
    return re.sub(r"_\d+$", "", value.strip().upper())


def normalize(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip().casefold()


def column_number(reference: str) -> int:
    letters = re.match(r"[A-Z]+", reference).group()
    value = 0
    for letter in letters:
        value = value * 26 + ord(letter) - 64
    return value


def read_first_xlsx_sheet(path: Path) -> list[dict[str, str]]:
    with zipfile.ZipFile(path) as archive:
        strings_root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
        strings = [
            "".join(node.text or "" for node in item.iter(XLSX_NS + "t"))
            for item in strings_root.findall(XLSX_NS + "si")
        ]
        sheet = ET.fromstring(archive.read("xl/worksheets/sheet1.xml"))
    rows: list[list[str]] = []
    for row in sheet.iter(XLSX_NS + "row"):
        values: dict[int, str] = {}
        for cell in row.findall(XLSX_NS + "c"):
            value_node = cell.find(XLSX_NS + "v")
            value = "" if value_node is None else value_node.text or ""
            if cell.attrib.get("t") == "s" and value:
                value = strings[int(value)]
            values[column_number(cell.attrib["r"])] = value
        rows.append([values.get(index, "") for index in range(1, max(values, default=0) + 1)])
    header = rows[0]
    return [
        dict(zip(header, row + [""] * (len(header) - len(row)))) for row in rows[1:]
    ]


def raw_md_objects() -> dict[str, dict[str, Any]]:
    files = sorted((CAPTURE / "raw").glob("*.json"))
    if len(files) != 1:
        raise CarrierError(f"expected one raw museum-digital response, found {len(files)}")
    payload = json.loads(files[0].read_text(encoding="utf-8"))
    objects = payload["results"]["objects"]
    if len(objects) != 73:
        raise CarrierError(f"expected 73 museum-digital objects, found {len(objects)}")
    return objects


def event_time(obj: dict[str, Any]) -> str:
    for event in obj.get("object_events", []):
        if event.get("event_type_name") == "Verfasst":
            return event.get("time", {}).get("time_name", "")
    return ""


@functools.lru_cache(maxsize=1)
def metadata_comparison() -> list[dict[str, Any]]:
    workbook = read_first_xlsx_sheet(FINAL_METADATA)
    raw = raw_md_objects()
    if len(workbook) != 73:
        raise CarrierError(f"final metadata workbook has {len(workbook)} data rows, not 73")
    if {row["md:number"] for row in workbook} != set(raw):
        raise CarrierError("final metadata workbook and museum-digital IDs do not agree")
    comparisons: list[dict[str, Any]] = []
    for row in workbook:
        obj = raw[row["md:number"]]
        pdf_rights = sorted(
            {
                resource.get("rights", "")
                for resource in obj.get("object_images", [])
                if resource.get("type") == "text"
            }
        )
        image_rights = sorted(
            {
                resource.get("rights", "")
                for resource in obj.get("object_images", [])
                if resource.get("type") == "image"
            }
        )
        live_tags = ", ".join(item.get("tag_name", "") for item in obj.get("object_tags", []))
        field_pairs = {
            "inventory": (row["Inventarnummer"], obj.get("object_inventory_number", "")),
            "object_type": (row["Objektart"], obj.get("object_type", "")),
            "material": (row["Material / Technik"], obj.get("object_material_technique", "")),
            "title": (row["Objektname"], obj.get("object_name", "")),
            "description": (row["Beschreibung"], obj.get("object_description", "")),
            "dimensions": (row["Maße"], obj.get("object_dimensions", "")),
            "date": (row["Zeit"], event_time(obj)),
            "collection": (
                row["Sammlung"],
                "; ".join(item.get("collection_name", "") for item in obj.get("object_collection", [])),
            ),
            "tags": (row["Schlagwort"], live_tags),
            "metadata_rights_holder": (
                row["Metadaten-Rechte (Inhaber)"],
                obj.get("licence", {}).get("metadata_rights_holder", ""),
            ),
            "metadata_license": (
                row["Metadaten-Nutzungsrechte"],
                obj.get("licence", {}).get("metadata_rights_status", ""),
            ),
        }
        changed = [
            field for field, (local, live) in field_pairs.items() if normalize(local) != normalize(live)
        ]
        comparisons.append(
            {
                "inventory": row["Inventarnummer"],
                "base_inventory": base_inventory(row["Inventarnummer"]),
                "md_object_id": row["md:number"],
                "local_metadata_source": str(FINAL_METADATA),
                "local_original_scan_reference": row["Originalscan"],
                "live_md_last_updated": obj.get("object_last_updated", ""),
                "comparison_state": "live_supersedes_snapshot" if changed else "matches_snapshot",
                "changed_fields": changed,
                "local_description": row["Beschreibung"] if "description" in changed else "",
                "live_description": obj.get("object_description", "") if "description" in changed else "",
                "local_tags": row["Schlagwort"] if "tags" in changed else "",
                "live_tags": live_tags if "tags" in changed else "",
                "pdf_resource_rights": pdf_rights,
                "image_resource_rights": image_rights,
                "metadata_license": obj.get("licence", {}).get("metadata_rights_status", ""),
                "metadata_authority": "live_museum_digital_capture",
                "workbook_role": "preserved_provenance_snapshot",
            }
        )
    return comparisons


def preferred_family(base: str, families: list[dict[str, str]]) -> dict[str, str] | None:
    sources = [
        row
        for row in families
        if row["candidate_role"] in {"bibliocopy_tiff_source", "canon_tiff_source"}
        and "/undo" not in row["family_path"]
    ]
    if not sources:
        return None
    if base == "BB-03":
        canon = [row for row in sources if row["candidate_role"] == "canon_tiff_source"]
        if canon:
            return max(canon, key=lambda row: int(row["file_count"]))
    bibliocopy = [row for row in sources if row["candidate_role"] == "bibliocopy_tiff_source"]
    if bibliocopy:
        return max(bibliocopy, key=lambda row: int(row["file_count"]))
    return max(sources, key=lambda row: int(row["file_count"]))


def carrier_projection() -> list[dict[str, Any]]:
    md = read_csv("brigadebuecher-md-objects.csv")
    families = read_csv("brigadebuecher-scan-families.csv")
    pdfs = read_csv("brigadebuecher-pdf-occurrences.csv")
    other_pdfs = read_csv("brigadebuecher-other-pdfs.csv")
    local_only = read_csv("brigadebuecher-local-only.csv")
    page_matches = read_csv("brigadebuecher-page-matches.csv")
    md_by_base: dict[str, list[dict[str, str]]] = collections.defaultdict(list)
    families_by_base: dict[str, list[dict[str, str]]] = collections.defaultdict(list)
    pdfs_by_base: dict[str, list[dict[str, str]]] = collections.defaultdict(list)
    other_pdfs_by_base: dict[str, list[dict[str, str]]] = collections.defaultdict(list)
    matches_by_inventory: dict[str, list[dict[str, str]]] = collections.defaultdict(list)
    for row in md:
        md_by_base[row["base_inventory"]].append(row)
    for row in families:
        families_by_base[row["base_inventory"]].append(row)
    for row in pdfs:
        pdfs_by_base[base_inventory(row["inventory"])].append(row)
    for row in other_pdfs:
        stem = base_inventory(Path(row["filename"]).stem)
        if re.fullmatch(r"(?:BB|BX)-\d+", stem):
            other_pdfs_by_base[stem].append(row)
    for row in page_matches:
        matches_by_inventory[row["inventory"]].append(row)
    local_bases = {row["base_inventory"] for row in local_only}
    bases = sorted(set(md_by_base) | local_bases)

    explicit_review = {
        "BB-01": ["replacement_or_rescan_page_variants"],
        "BB-03": ["multiple_original_tiff_variants"],
        "BB-05": ["original_tiff_BB-05_099_decode_error"],
        "BB-09": ["live_md_page_range_does_not_equal_pdf_page_count"],
        "BB-28": [
            "live_md_page_range_overlap",
            "BB-28_3_has_no_close_tiff_page_matches",
            "90_pdf_pages_but_67_original_tiff_files",
        ],
        "BB-30": ["no_original_tiff; pdf_and_low_resolution_jpg_only"],
        "BB-33": [
            "no_original_tiff; pdf_and_low_resolution_jpg_only",
            "old_full_pdf_has_104_pages_but_new_full_pdf_has_105",
        ],
        "BB-50": ["two_different_orientation_variants_for_page_011"],
    }
    carriers: list[dict[str, Any]] = []
    for base in bases:
        md_rows = sorted(md_by_base.get(base, []), key=lambda row: row["inventory"])
        family_rows = families_by_base.get(base, [])
        primary = preferred_family(base, family_rows)
        source_families = [
            row for row in family_rows
            if row["candidate_role"] in {"bibliocopy_tiff_source", "canon_tiff_source"}
        ]
        copy_families = [row for row in family_rows if row["candidate_role"] == "project_tiff_copy"]
        jpeg_families = [row for row in family_rows if row["candidate_role"] == "jpeg_derivative"]
        direct_pdf_rows = pdfs_by_base.get(base, [])
        full_pdf_rows = [
            row for row in other_pdfs_by_base.get(base, [])
            if row["relationship"] == "whole_book_pdf_related_to_split_md_objects"
            or Path(row["filename"]).stem.upper() == base
        ]
        issues = list(explicit_review.get(base, []))
        if not md_rows:
            issues.extend(["no_museum_digital_record", "local_metadata_not_reconciled"])
        if primary and int(primary["error_count"]):
            issue = "primary_original_tiff_family_has_decode_errors"
            if issue not in issues:
                issues.append(issue)
        role = (
            "preservation_tiff_page_set_candidate"
            if primary
            else "preservation_pdf_candidate"
        )
        candidate_path = primary["family_path"] if primary else ""
        if not candidate_path:
            preferred_pdf_paths = [
                row["source_path"] for row in direct_pdf_rows
                if row["relationship"] == "preferred_occurrence_for_review"
            ]
            whole_book_paths = [row["source_path"] for row in full_pdf_rows]
            candidate_path = ";".join(whole_book_paths or preferred_pdf_paths)
        close_counts = []
        for md_row in md_rows:
            matches = matches_by_inventory.get(md_row["inventory"], [])
            close_counts.append(
                {
                    "inventory": md_row["inventory"],
                    "pages_compared": len(matches),
                    "close_pages": sum(
                        int(row["distance_total"]) <= 30 and int(row["distance_phash"]) <= 16
                        for row in matches
                    ),
                }
            )
        carriers.append(
            {
                "carrier_key": base,
                "museum_digital_state": "matched" if md_rows else "local_only_candidate",
                "md_object_ids": [row["object_id"] for row in md_rows],
                "md_inventories": [row["inventory"] for row in md_rows],
                "md_titles": [row["title"] for row in md_rows],
                "md_object_urls": [row["object_url"] for row in md_rows],
                "local_original_scan_references": sorted(
                    {
                        row["local_original_scan_reference"]
                        for row in metadata_comparison()
                        if row["base_inventory"] == base
                    }
                ),
                "proposed_master_role": role,
                "proposed_master_path": candidate_path,
                "primary_original_tiff_count": int(primary["file_count"]) if primary else 0,
                "primary_original_tiff_errors": int(primary["error_count"]) if primary else 0,
                "other_original_tiff_families": [row["family_path"] for row in source_families if row is not primary],
                "project_tiff_copy_families": [row["family_path"] for row in copy_families],
                "jpeg_derivative_families": [row["family_path"] for row in jpeg_families],
                "direct_pdf_occurrences": len(direct_pdf_rows),
                "direct_pdf_unique_hashes": len({row["content_sha256"] for row in direct_pdf_rows}),
                "whole_book_pdf_candidates": [row["source_path"] for row in full_pdf_rows],
                "pdf_to_tiff_similarity_summary": close_counts,
                "review_reasons": issues,
                "review_state": "needs_individual_review" if issues else "ready_for_batch_confirmation",
                "selected_master": None,
                "redundancy_decision": None,
                "deletion_authorized": False,
            }
        )
    return carriers


def document_appraisal() -> list[dict[str, Any]]:
    reviewed_path = RECON / "brigadebuecher-document-appraisal.jsonl"
    if reviewed_path.is_file():
        reviewed = read_jsonl(reviewed_path)
        if (
            len(reviewed) == 297
            and all(row.get("review_state") == "classified_content_reviewed" for row in reviewed)
            and all(row.get("decision") for row in reviewed)
        ):
            return reviewed
    rows = read_csv("brigadebuecher-document-candidates.csv")
    preferred_exact_pdf = (
        "/media/vladimir/Elements/restore/archive/WF/BRIGADEBUCH-Projekt/"
        "Industriesalon-Schoenweide_Digis-Projektantrag2022.pdf"
    )
    output: list[dict[str, Any]] = []
    for row in rows:
        name = row["filename"]
        path = row["source_path"]
        if name == "Brigadebuecher_md-Export_04-final.xlsx":
            suggestion = "preserve_final_metadata_snapshot"
            state = "ready_for_batch_confirmation"
        elif re.fullmatch(r"Brigadebuecher_md-Export_0[1-3]\.xlsx", name):
            suggestion = "redundant_prior_metadata_snapshot"
            state = "ready_for_batch_confirmation"
        elif name == "Industriesalon-Schoenweide_Digis-Projektantrag-2022_04-final.doc":
            suggestion = "preserve_final_editable_project_application"
            state = "ready_for_batch_confirmation"
        elif row["content_sha256"] == "b7bd38af4ce489d50f13ce92ee41bdb25dd7a5f1845d6ef227aa3ae8d3db3039":
            suggestion = (
                "preserve_final_project_application_pdf"
                if path == preferred_exact_pdf
                else "redundant_exact_project_application_pdf_occurrence"
            )
            state = "ready_for_batch_confirmation"
        elif name == "Industriesalon-Schoenweide_Digis-Projektantrag-2022_Unterschriftseite.pdf":
            suggestion = "preserve_signature_page"
            state = "ready_for_batch_confirmation"
        elif name.startswith("Industriesalon-Schoenweide_Digis-Projektantrag-2022"):
            suggestion = "redundant_prior_project_application_version"
            state = "ready_for_batch_confirmation"
        elif name == "Projektabschlussbericht_2022_Industriesalon_fl.docx":
            suggestion = "preserve_final_project_report_candidate"
            state = "needs_human_confirmation"
        elif name.startswith("Projektabschlussbericht_2022_Industriesalon"):
            suggestion = "prior_project_report_version_candidate"
            state = "needs_human_confirmation"
        elif row["document_role"] == "metadata_evidence":
            suggestion = "metadata_process_evidence; retain_only_if_not_covered_by_final_snapshot"
            state = "needs_bounded_document_review"
        else:
            suggestion = "contextual_project_file_outside_core_unit_documentation"
            state = "not_proposed_for_unit"
        output.append(
            {
                "document_id": row["document_id"],
                "filename": name,
                "source_path": path,
                "content_sha256": row["content_sha256"],
                "size_bytes": int(row["size_bytes"]),
                "indexed_extraction_state": row["extraction_state"],
                "suggested_relationship": suggestion,
                "review_state": state,
                "decision": None,
                "deletion_authorized": False,
            }
        )
    return output


def main() -> int:
    mount = ensure_source_guard()
    for required in (
        FINAL_METADATA,
        RECON / "brigadebuecher-md-objects.csv",
        RECON / "brigadebuecher-scan-families.csv",
        RECON / "brigadebuecher-page-matches.csv",
        RECON / "brigadebuecher-document-candidates.csv",
    ):
        if not required.is_file():
            raise CarrierError(f"required input unavailable: {required}")
    metadata = metadata_comparison()
    carriers = carrier_projection()
    documents = document_appraisal()
    ready = [row for row in carriers if row["review_state"] == "ready_for_batch_confirmation"]
    review = [row for row in carriers if row["review_state"] == "needs_individual_review"]
    md_review = [row for row in review if row["museum_digital_state"] == "matched"]
    local_review = [row for row in review if row["museum_digital_state"] == "local_only_candidate"]
    metadata_changed = [row for row in metadata if row["comparison_state"] != "matches_snapshot"]
    summary = {
        "format": "iss-brigadebuecher-carrier-projection-v1",
        "source_mount": mount,
        "source_policy": "read_only_no_source_changes",
        "physical_carrier_candidates": len(carriers),
        "museum_digital_backed_carriers": sum(row["museum_digital_state"] == "matched" for row in carriers),
        "museum_digital_part_records": 73,
        "local_only_carriers": len(local_review),
        "ready_for_batch_master_confirmation": len(ready),
        "md_backed_carriers_needing_individual_review": len(md_review),
        "metadata_rows_compared": len(metadata),
        "metadata_rows_with_live_changes": len(metadata_changed),
        "metadata_changed_fields": dict(
            collections.Counter(field for row in metadata_changed for field in row["changed_fields"])
        ),
        "document_candidates_appraised": len(documents),
        "selected_masters": 0,
        "source_files_changed": 0,
        "deletion_authorized": False,
    }
    write_jsonl(RECON / "brigadebuecher-carriers.jsonl", carriers)
    write_jsonl(RECON / "brigadebuecher-carriers-ready.jsonl", ready)
    write_jsonl(RECON / "brigadebuecher-carrier-review.jsonl", review)
    write_jsonl(RECON / "brigadebuecher-metadata-comparison.jsonl", metadata)
    write_jsonl(RECON / "brigadebuecher-document-appraisal.jsonl", documents)
    atomic_write(
        RECON / "brigadebuecher-carrier-summary.json",
        (json.dumps(summary, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
    )
    lines = [
        "# Brigadebücher: carrier-level reconciliation",
        "",
        "The 73 museum-digital records describe 37 physical carriers; suffixes such as `_1` and `_2` are PDF parts, not separate local books. The reconciliation therefore keeps one pending Master decision per physical carrier and links every relevant museum-digital part record to it.",
        "",
        f"- Physical carrier candidates: {len(carriers)}",
        f"- Museum-digital-backed carriers: {summary['museum_digital_backed_carriers']} (73 MD part records)",
        f"- Local-only carrier candidates: {summary['local_only_carriers']}",
        f"- Ready for one batch human confirmation: {len(ready)}",
        f"- MD-backed carriers needing individual review: {len(md_review)}",
        f"- Metadata rows compared with the final local export: {len(metadata)}",
        f"- Rows changed subsequently in live museum-digital: {len(metadata_changed)}",
        "",
        "## Individual carrier review",
        "",
    ]
    for row in review:
        lines.append(f"- `{row['carrier_key']}`: " + "; ".join(row["review_reasons"]))
    lines.extend(
        [
            "",
            "## Metadata authority",
            "",
            "The final local workbook contains exactly the same 73 museum-digital IDs and inventories. Live museum-digital is newer: four descriptions and five tag lists changed after the workbook snapshot. The live capture is therefore the current descriptive authority; the workbook remains direct provenance documentation.",
            "",
            "## Decision boundary",
            "",
            "No Master is selected by this projection. Project TIFF copies, JPG derivatives, older PDF variants, and older documentation versions are recorded as candidates or redundancy evidence only. No source is changed and no deletion is authorized.",
        ]
    )
    atomic_write(
        RECON / "BRIGADEBUECHER_CARRIER_RECONCILIATION.md",
        ("\n".join(lines) + "\n").encode("utf-8"),
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
