#!/usr/bin/env python3
"""Prepare unresolved new-object candidates from Objektsammlung export extras.

The published unit and protected USB are read-only inputs.  This script writes
only flat, advisory CSV/JSON evidence into the existing collection-549
reconciliation directory.  It does not select a master or accession anything.
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import subprocess
import sys
import uuid
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from PIL import ExifTags, Image


SOURCE_ROOT = Path("/media/vladimir/Elements")
UNIT = Path("/mnt/volume/iss-archive/units/objektsammlung")
RECON = Path(
    "/mnt/volume/iss-archive-work/collection-549/work/reconciliation/"
    "unit-reconciliation-v1"
)

APPRAISAL = RECON / "export-object-appraisal.csv"
OBJECTS = RECON / "new-object-candidates.csv"
MASTERS = RECON / "new-object-master-candidates.csv"
EVIDENCE = RECON / "new-object-metadata-evidence.csv"
SUMMARY = RECON / "export-appraisal-summary.json"


class AppraisalError(RuntimeError):
    pass


def now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


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
        raise AppraisalError("Elements did not resolve to one filesystem")
    row = rows[0]
    options = set(str(row.get("options", "")).split(","))
    if (
        row.get("target") != str(SOURCE_ROOT)
        or row.get("source") != "/dev/sdf1"
        or row.get("fstype") != "ntfs3"
        or "ro" not in options
    ):
        raise AppraisalError(f"protected Elements mount guard failed: {row}")
    return {key: str(row.get(key, "")) for key in ("target", "source", "fstype", "options")}


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


def atomic_json(path: Path, payload: object) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def exif_capture(path: Path) -> tuple[str, str, str]:
    with Image.open(path) as image:
        exif = image.getexif()
    values = {ExifTags.TAGS.get(key, str(key)): value for key, value in exif.items()}
    return (
        str(values.get("DateTimeOriginal") or values.get("DateTime") or ""),
        str(values.get("Make") or "").strip(),
        str(values.get("Model") or "").strip(),
    )


def classify(row: dict[str, str]) -> dict[str, str]:
    relative = row["relative_path"]
    name = Path(relative).name
    role = row["role"]

    if role == "generated_access_derivative":
        return {
            "identity_group": f"existing-md-{row['md_object_id']}",
            "assessment": "generated_derivative_keep",
            "candidate_object_id": "",
            "proposed_unit_action": "keep_as_single_generated_derivative",
            "visual_duplicate_group": "",
            "note": "Correct export payload under the one-Master/one-generated-derivative agreement.",
        }
    if role == "related_museum_digital_projection_resource":
        return {
            "identity_group": f"existing-md-{row['md_object_id']}",
            "assessment": "same_existing_object_or_projection_resource",
            "candidate_object_id": "",
            "proposed_unit_action": "remove_from_export_after_human_review",
            "visual_duplicate_group": "",
            "note": "Additional museum-digital resource for an existing object; retain URL and metadata evidence, not an export payload copy.",
        }
    if "Obj_10001__related-local-" in name:
        duplicate = "VIS-LENINGRAD-FRONT" if "Leningrad_T2" in name else ""
        return {
            "identity_group": "existing-md-49944",
            "assessment": "same_existing_object",
            "candidate_object_id": "",
            "proposed_unit_action": "remove_from_export_after_human_review",
            "visual_duplicate_group": duplicate,
            "note": "Alternate view of the already accessioned Leningrad T2; not a new object.",
        }

    local_number = ""
    marker = "__related-local-"
    if marker in name:
        local_number = name.split(marker, 1)[1].split("__", 1)[0]

    if local_number in {f"{number:03d}" for number in range(1, 14)}:
        return {
            "identity_group": "new-mrs-3010",
            "assessment": "new_object_candidate",
            "candidate_object_id": "CAND-MRS-3010",
            "proposed_unit_action": "candidate_for_human_master_selection",
            "visual_duplicate_group": "",
            "note": "High-resolution view of the MRS 3010 rocketsonde display/ensemble; object scope needs human confirmation.",
        }
    if local_number in {"014", "015", "016", "017"}:
        return {
            "identity_group": "existing-md-117742",
            "assessment": "same_existing_object",
            "candidate_object_id": "",
            "proposed_unit_action": "remove_from_export_after_human_review",
            "visual_duplicate_group": "VIS-S21-RACK" if local_number in {"016", "017"} else "",
            "note": "Alternate 2010 view of the already accessioned S 21/K radiosonde rack; not a new object.",
        }
    if local_number in {"018", "019", "020", "021"}:
        return {
            "identity_group": "new-marz-2",
            "assessment": "new_object_candidate",
            "candidate_object_id": "CAND-MARZ-2",
            "proposed_unit_action": "candidate_for_human_master_selection",
            "visual_duplicate_group": "VIS-MARZ-OVERVIEW" if local_number in {"018", "019", "020"} else "",
            "note": "High-resolution view of the boxed Soviet MARZ-2 radiosonde kit; subtype and object scope need human confirmation.",
        }
    if local_number == "022":
        return {
            "identity_group": "new-dfr-mars-3t",
            "assessment": "new_object_candidate",
            "candidate_object_id": "CAND-DFR-MARS-3T",
            "proposed_unit_action": "candidate_for_human_master_selection",
            "visual_duplicate_group": "",
            "note": "High-resolution view of the boxed DFR/Mars 3T, bound by label and local TFA-7429702 metadata.",
        }
    if local_number in {"023", "024", "025", "026"}:
        duplicate = "VIS-REFERENCE-RADIOSONDE" if local_number in {"023", "025"} else ""
        return {
            "identity_group": "external-reference-images",
            "assessment": "reference_not_object_master",
            "candidate_object_id": "",
            "proposed_unit_action": "remove_from_export_after_human_review",
            "visual_duplicate_group": duplicate,
            "note": "Low-resolution or externally sourced reference image; no local object/provenance binding.",
        }
    if local_number == "028":
        return {
            "identity_group": "exhibition-context",
            "assessment": "context_view_not_object_master",
            "candidate_object_id": "",
            "proposed_unit_action": "remove_from_export_after_human_review",
            "visual_duplicate_group": "",
            "note": "Exhibition/context photograph, not a single object Master.",
        }
    if local_number in {"031", "032", "033"}:
        duplicate = "VIS-WF-MANUFACTURING-0006" if local_number in {"031", "033"} else ""
        return {
            "identity_group": "cross-project-historical-photographs",
            "assessment": "historical_photo_not_physical_object_master",
            "candidate_object_id": "",
            "proposed_unit_action": "remove_from_export_after_human_review",
            "visual_duplicate_group": duplicate,
            "note": "Separate historical photograph/Fotostelle context, not a Master for a physical Objektsammlung object.",
        }
    raise AppraisalError(f"unclassified export representation: {relative}")


def object_rows() -> list[dict[str, object]]:
    return [
        {
            "candidate_object_id": "CAND-MRS-3010",
            "candidate_id_status": "advisory_not_archival_identifier",
            "proposed_title": "Raketensonde MRS 3010",
            "object_type": "Raketensonde",
            "manufacturer": "Werk für Fernsehelektronik (WF), Berlin",
            "production_place": "Berlin (DDR)",
            "date": "1984-1990 (provisional model period)",
            "description": "Improved rocketsonde developed in 1984 for high-atmosphere measurements; the WF later produced the MRS 3000/3010/3020 series. The photographed set shows a red rocket/dart display with open and closed MRS 3010 measuring units.",
            "object_scope_note": "Human must confirm whether the red body and the two measuring units form one accession ensemble or require separate objects.",
            "metadata_state": "provisional_scope_review_required",
            "master_decision_state": "unresolved",
            "selected_master_path": "",
            "deletion_authorized": "false",
        },
        {
            "candidate_object_id": "CAND-MARZ-2",
            "candidate_id_status": "advisory_not_archival_identifier",
            "proposed_title": "Radiosonde MARZ-2 (Variante unbestimmt)",
            "object_type": "Radiosonde",
            "manufacturer": "Sowjetische Fertigung; Hersteller noch unbestimmt",
            "production_place": "Sowjetunion",
            "date": "1980er Jahre (in Lindenberg 1986 eingeführt)",
            "description": "Boxed MARZ-2 radiosonde components for meteorological upper-air sounding. The photographed Russian note names MARZ-2; the exact 2-1 or 2-2 subtype is not visible.",
            "object_scope_note": "Human must confirm that the photographed box and loose components form one accession object and resolve subtype if another label exists.",
            "metadata_state": "provisional_variant_review_required",
            "master_decision_state": "unresolved",
            "selected_master_path": "",
            "deletion_authorized": "false",
        },
        {
            "candidate_object_id": "CAND-DFR-MARS-3T",
            "candidate_id_status": "advisory_not_archival_identifier",
            "proposed_title": "Radiosonde DFR/Mars 3T im Behälter",
            "object_type": "Radiosonde",
            "manufacturer": "LP Praha / WF Berlin (Rollen noch zu bestätigen)",
            "production_place": "Tschechoslowakei / Berlin (DDR)",
            "date": "bis spätestens Juli 1974",
            "description": "Radiosonde DFR/Mars 3T in opened protective container. The local 1974 TFA record describes it as a sensor for air temperature and pressure in the free atmosphere, probably used mainly in the military sector.",
            "object_scope_note": "One boxed sonde and accessories are visible; confirm the physical ensemble and maker roles from the label.",
            "metadata_state": "provisional_metadata_sufficient_for_human_review",
            "master_decision_state": "unresolved",
            "selected_master_path": "",
            "deletion_authorized": "false",
        },
    ]


def evidence_rows() -> list[dict[str, object]]:
    common_role = "metadata_supporting_evidence_only_not_document_payload"
    return [
        {
            "candidate_object_id": "CAND-MRS-3010",
            "evidence_kind": "physical_label_in_candidate_image",
            "evidence_path_or_url": "/media/vladimir/Elements/restore/archive/alter Rechner-Siemens/2010-08-03 Wettersonden/Wettersonden 016.JPG",
            "extracted_fact": "Closed measuring unit is marked MRS 3010, WF, DDR.",
            "binding_confidence": "high",
            "accession_role": common_role,
            "deletion_authorized": "false",
        },
        {
            "candidate_object_id": "CAND-MRS-3010",
            "evidence_kind": "local_archive_catalogue_row",
            "evidence_path_or_url": "/media/vladimir/Elements/restore/archive/WF/Archiv/FEBs-formatiert-fuer-md_UTF-8.csv",
            "extracted_fact": "FEB-008-1065: Ehrlich u.a., Meteorologische Radiosonde MRS 3010, Oktober 1983, 17 Seiten; WF, Berlin (DDR).",
            "binding_confidence": "high",
            "accession_role": common_role,
            "deletion_authorized": "false",
        },
        {
            "candidate_object_id": "CAND-MRS-3010",
            "evidence_kind": "official_technical_history",
            "evidence_path_or_url": "https://www.dwd.de/DE/leistungen/pbfb_verlag_promet/pdf_promethefte/31_2_4_pdf.pdf?__blob=publicationFile&v=3",
            "extracted_fact": "DWD history: MRS 3000 developed for rocket sounding, later serially produced by WF; improved MRS 3010 developed in 1984.",
            "binding_confidence": "high",
            "accession_role": common_role,
            "deletion_authorized": "false",
        },
        {
            "candidate_object_id": "CAND-MARZ-2",
            "evidence_kind": "physical_note_in_candidate_image",
            "evidence_path_or_url": "/media/vladimir/Elements/restore/archive/alter Rechner-Siemens/2010-08-03 Wettersonden/Wettersonden 022.JPG",
            "extracted_fact": "Handwritten Russian technical sheet reads 'Радиозонд МАРЗ-2'; exact subtype is not legible.",
            "binding_confidence": "high_for_model_family",
            "accession_role": common_role,
            "deletion_authorized": "false",
        },
        {
            "candidate_object_id": "CAND-MARZ-2",
            "evidence_kind": "official_site_history",
            "evidence_path_or_url": "https://www.dwd.de/DE/forschung/atmosphaerenbeob/lindenbergersaeule/rao_download/aktuell_2017_03.pdf?__blob=publicationFile&v=1",
            "extracted_fact": "DWD states that the Russian MARZ-2 radiosonde was introduced at Lindenberg in 1986.",
            "binding_confidence": "high_for_model_history",
            "accession_role": common_role,
            "deletion_authorized": "false",
        },
        {
            "candidate_object_id": "CAND-DFR-MARS-3T",
            "evidence_kind": "physical_label_in_candidate_image",
            "evidence_path_or_url": "/media/vladimir/Elements/restore/archive/alter Rechner-Siemens/2010-08-03 Wettersonden/Wettersonden 026.JPG",
            "extracted_fact": "Label identifies DFR-MARS 3T and names LP Praha and WF Berlin; the container label says made in Czechoslovakia/GDR.",
            "binding_confidence": "high",
            "accession_role": common_role,
            "deletion_authorized": "false",
        },
        {
            "candidate_object_id": "CAND-DFR-MARS-3T",
            "evidence_kind": "local_museum_digital_export_record",
            "evidence_path_or_url": "/media/vladimir/Elements/restore/archive/WF/export_29_20200212_generisch/60985.xml",
            "extracted_fact": "TFA-7429702: Radiosonde DFR/Mars 3T im Behälter, photo July 1974; measured air temperature and pressure in the free atmosphere and was probably mainly military.",
            "binding_confidence": "high_for_same_model",
            "accession_role": common_role,
            "deletion_authorized": "false",
        },
        {
            "candidate_object_id": "CAND-DFR-MARS-3T",
            "evidence_kind": "local_historical_image",
            "evidence_path_or_url": "/media/vladimir/Elements/restore/archive/WF/Thesaurus/09_1958-Rest_1959-Rest_1966-90/Rest-1958+1959_1966-1990_JPGs/TFA-NEG-7429702.jpg",
            "extracted_fact": "1974 WF Bildstelle photograph shows the same DFR/Mars 3T container and components.",
            "binding_confidence": "high_for_same_model",
            "accession_role": common_role,
            "deletion_authorized": "false",
        },
    ]


def main() -> int:
    arguments = sys.argv[1:]
    if arguments not in ([], ["--check"]):
        raise AppraisalError("usage: collection_549_export_appraisal.py [--check]")
    check_only = arguments == ["--check"]
    created_at = now()
    mount_before = guarded_mount()

    representations = read_csv(UNIT / "00_MANIFESTS/representations.csv")
    export_rows = [row for row in representations if row["relative_path"].startswith("export/")]
    if len(export_rows) != 52:
        raise AppraisalError(f"expected 52 current export representations, found {len(export_rows)}")
    if Counter(row["role"] for row in export_rows) != Counter(
        {
            "generated_access_derivative": 10,
            "related_museum_digital_projection_resource": 9,
            "retained_unique_local_related_image": 33,
        }
    ):
        raise AppraisalError("current export role census changed")

    unit_manifest_hash_before = sha256(UNIT / "00_MANIFESTS/payload-checksums.csv")
    source_snapshot: dict[str, tuple[int, int]] = {}
    appraisal_rows: list[dict[str, object]] = []
    master_rows: list[dict[str, object]] = []
    candidate_sequence = Counter()

    for row in export_rows:
        classification = classify(row)
        path = UNIT / row["relative_path"]
        if not path.is_file() or path.stat().st_size != int(row["size_bytes"]):
            raise AppraisalError(f"export payload missing or changed: {path}")
        if sha256(path) != row["sha256"]:
            raise AppraisalError(f"export payload checksum changed: {path}")

        source_path = Path(row["original_source_path"])
        source_matches_export = "not_applicable_derivative" if row["role"] == "generated_access_derivative" else "source_unavailable"
        if source_path.is_file():
            stat = source_path.stat()
            if source_path.is_relative_to(SOURCE_ROOT):
                source_snapshot[str(source_path)] = (stat.st_size, stat.st_mtime_ns)
            if row["role"] != "generated_access_derivative":
                if stat.st_size != path.stat().st_size or sha256(source_path) != row["sha256"]:
                    raise AppraisalError(f"source/export binding changed: {source_path}")
                source_matches_export = "true"

        width = int(row["width_px"] or 0)
        height = int(row["height_px"] or 0)
        high_resolution = width >= 2400 and height >= 2400 and width * height >= 8_000_000
        appraisal_rows.append(
            {
                "export_relative_path": row["relative_path"],
                "current_role": row["role"],
                "current_md_object_id": row["md_object_id"],
                "current_inventory": row["inventory"],
                "original_source_path": row["original_source_path"],
                "sha256": row["sha256"],
                "size_bytes": row["size_bytes"],
                "width_px": row["width_px"],
                "height_px": row["height_px"],
                "high_resolution": str(high_resolution).lower(),
                "source_matches_export": source_matches_export,
                **classification,
                "human_review_state": "pending" if classification["assessment"] != "generated_derivative_keep" else "not_required",
                "deletion_authorized": "false",
            }
        )

        candidate_object_id = classification["candidate_object_id"]
        if candidate_object_id:
            if not high_resolution:
                raise AppraisalError(f"new object candidate is not high-resolution: {path}")
            candidate_sequence[candidate_object_id] += 1
            capture, make, model = exif_capture(path)
            source_name = source_path.name
            suggested = source_name in {
                "Wettersonden 003.JPG",
                "Wettersonden 016.JPG",
                "Wettersonden 022.JPG",
                "Wettersonden 025.JPG",
                "Wettersonden 026.JPG",
            }
            view_note = {
                "Wettersonden 003.JPG": "overview showing red body plus open and closed MRS 3010 units",
                "Wettersonden 016.JPG": "detail showing open and closed MRS 3010 units and readable model label",
                "Wettersonden 022.JPG": "broad MARZ-2 kit overview including handwritten model sheet",
                "Wettersonden 025.JPG": "closer view of MARZ-2 components",
                "Wettersonden 026.JPG": "only high-resolution DFR/Mars 3T view; label and container visible",
            }.get(source_name, "alternate high-resolution view")
            master_rows.append(
                {
                    "candidate_master_id": f"{candidate_object_id}-IMG-{candidate_sequence[candidate_object_id]:02d}",
                    "candidate_object_id": candidate_object_id,
                    "candidate_path": str(source_path),
                    "current_export_copy": row["relative_path"],
                    "format": row["format"],
                    "size_bytes": row["size_bytes"],
                    "width_px": row["width_px"],
                    "height_px": row["height_px"],
                    "sha256": f"sha256:{row['sha256']}",
                    "capture_datetime": capture,
                    "camera_make": make,
                    "camera_model": model,
                    "suggested_for_review_first": str(suggested).lower(),
                    "view_note": view_note,
                    "master_decision_state": "unresolved",
                    "disposition_if_not_selected": "redundant_after_human_master_selection",
                    "deletion_authorized": "false",
                }
            )

    objects = object_rows()
    evidence = evidence_rows()
    if Counter(row["candidate_object_id"] for row in master_rows) != Counter(
        {"CAND-MRS-3010": 13, "CAND-MARZ-2": 4, "CAND-DFR-MARS-3T": 1}
    ):
        raise AppraisalError("new-object Master candidate census changed")
    if any(row["master_decision_state"] != "unresolved" for row in objects + master_rows):
        raise AppraisalError("a Master was selected during preparation")
    if any(row["deletion_authorized"] != "false" for row in appraisal_rows + objects + master_rows + evidence):
        raise AppraisalError("a prepared row unexpectedly authorizes deletion")

    assessment_counts = Counter(str(row["assessment"]) for row in appraisal_rows)
    summary = {
        "format": "iss-objektsammlung-export-object-appraisal-v1",
        "created_at": created_at,
        "mode": "check-only" if check_only else "prepared",
        "result": "ok",
        "unit": str(UNIT),
        "export_files_scanned": len(export_rows),
        "high_resolution_rule": "both dimensions >= 2400 px and total pixels >= 8,000,000",
        "assessment_counts": dict(sorted(assessment_counts.items())),
        "new_object_candidates": len(objects),
        "new_object_master_candidates": len(master_rows),
        "new_object_candidate_counts": dict(sorted(Counter(row["candidate_object_id"] for row in master_rows).items())),
        "master_selections_made": 0,
        "published_unit_files_modified": 0,
        "published_unit_files_deleted": 0,
        "source_files_modified": 0,
        "source_files_deleted": 0,
        "deletion_authorized": False,
        "source_mount": mount_before,
        "metadata_evidence_role": "supporting evidence only; no document payload accession proposed",
        "eventual_target_if_approved": "one Master and one generated export derivative per confirmed object",
    }

    if not check_only:
        atomic_csv(APPRAISAL, list(appraisal_rows[0]), appraisal_rows)
        atomic_csv(OBJECTS, list(objects[0]), objects)
        atomic_csv(MASTERS, list(master_rows[0]), master_rows)
        atomic_csv(EVIDENCE, list(evidence[0]), evidence)
        atomic_json(SUMMARY, summary)

    mount_after = guarded_mount()
    if mount_after != mount_before:
        raise AppraisalError("protected USB mount changed during appraisal")
    for source, before in source_snapshot.items():
        stat = Path(source).stat()
        if (stat.st_size, stat.st_mtime_ns) != before:
            raise AppraisalError(f"protected source changed during appraisal: {source}")
    if sha256(UNIT / "00_MANIFESTS/payload-checksums.csv") != unit_manifest_hash_before:
        raise AppraisalError("published unit changed during appraisal")

    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
