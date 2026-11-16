#!/usr/bin/env python3
"""Prepare a non-executing accession initialization for MD collection 555.

The output is proposal evidence only.  It does not select a Master, copy source
media, embed metadata, generate derivatives, or authorize deletion.
"""

from __future__ import annotations

import csv
import hashlib
import json
import subprocess
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

from PIL import Image, ImageOps


UNIT_ID = "pressebildarchiv-wf"
COLLECTION_ID = "555"
WORK_ROOT = Path("/mnt/volume/iss-archive-work/pressebildarchiv-wf")
OUTPUT = WORK_ROOT / "work/accession/collection-555-init-20260829"
MD_RUN = (
    WORK_ROOT
    / "work/reconciliation/museum-digital/runs/"
    "20260803T085022Z-pressebildarchiv-wf-collection-555"
)
AUDIT_RUN = (
    WORK_ROOT
    / "work/usb-master-search/runs/"
    "20260803T101000Z-pressebildarchiv-wf-usb-master-search-corrected"
)
HIRES_CSV = WORK_ROOT / "work/collection-555-hires-candidate-search-20260829.csv"
OVERLAP_JSON = WORK_ROOT / "work/collection-555-cross-unit-overlap-20260829.json"
MD_REFERENCE_ROOT = AUDIT_RUN.parent / (
    "20260803T094421Z-pressebildarchiv-wf-usb-master-search/md-reference"
)


class InitError(RuntimeError):
    pass


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def write_csv(path: Path, fields: list[str], rows: list[dict[str, Any]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def require_read_only_source() -> None:
    result = subprocess.run(
        [
            "findmnt", "-S", "/dev/sdf1", "-n", "-o",
            "SOURCE,FSTYPE,OPTIONS,TARGET",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    fields = result.stdout.strip().split()
    if len(fields) != 4:
        raise InitError(f"unexpected Elements mount record: {result.stdout!r}")
    source, filesystem, options, target = fields
    if source != "/dev/sdf1" or filesystem != "ntfs3":
        raise InitError(f"unexpected Elements source/filesystem: {result.stdout!r}")
    if target != "/media/vladimir/Elements" or "ro" not in options.split(","):
        raise InitError(f"Elements is not mounted at the protected read-only target: {result.stdout!r}")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return "sha256:" + digest.hexdigest()


def image_dimensions(path: Path) -> tuple[int, int]:
    with Image.open(path) as image:
        normalized = ImageOps.exif_transpose(image)
        return normalized.size


def asset_url(image: dict[str, Any]) -> str:
    relative = "/".join(
        [str(image.get("folder") or "").strip("/"), str(image["filename_loc"])]
    )
    return "https://asset.museum-digital.org/berlin/" + quote(relative)


def candidate_id(md_id: str, sequence: int) -> str:
    return f"md-{md_id}-candidate-{sequence:02d}"


def main() -> None:
    require_read_only_source()
    if OUTPUT.exists():
        raise InitError(f"refusing to replace existing initialization: {OUTPUT}")

    md_objects = {
        row["object_id"]: row for row in read_csv(MD_RUN / "objects.csv")
    }
    audit_objects = {
        row["md_object_id"]: row for row in read_csv(AUDIT_RUN / "objects.csv")
    }
    if set(md_objects) != set(audit_objects) or len(md_objects) != 48:
        raise InitError("collection scope is not the same exact set of 48 MD objects")

    raw = json.loads(
        (MD_RUN / "raw/page-0001-offset-000000.json").read_text(encoding="utf-8")
    )["results"]["objects"]
    if set(raw) != set(md_objects):
        raise InitError("raw MD capture and object projection disagree")

    audit_candidates = defaultdict(list)
    for row in read_csv(AUDIT_RUN / "candidates.csv"):
        if row["visual_state"] in {
            "confirmed_visual_match", "probable_visual_match_review"
        } and row["candidate_path"]:
            audit_candidates[row["md_object_id"]].append(row)

    hires_candidates = defaultdict(list)
    retained_hires_states = {
        "confirmed_exact_decoded_pixel_counterpart",
        "confirmed_exact_byte_counterpart",
        "same_photo_hires_jpeg_review",
        "same_photo_lower_resolution_tiff_review",
    }
    for row in read_csv(HIRES_CSV):
        if row["review_state"] in retained_hires_states:
            hires_candidates[row["md_object_id"]].append(row)

    overlap = json.loads(OVERLAP_JSON.read_text(encoding="utf-8"))
    overlap_by_md = defaultdict(list)
    for row in overlap["findings"]:
        overlap_by_md[row["target_md_object_id"]].append(row)

    output_candidates: list[dict[str, Any]] = []
    object_rows: list[dict[str, Any]] = []
    decision_rows: list[dict[str, Any]] = []
    resource_rows: list[dict[str, Any]] = []
    candidate_paths_by_md: dict[str, set[str]] = defaultdict(set)

    def add_candidate(md_id: str, row: dict[str, Any]) -> None:
        path = str(row["candidate_path"])
        if path in candidate_paths_by_md[md_id]:
            return
        candidate_paths_by_md[md_id].add(path)
        source = Path(path)
        if not source.is_file():
            raise InitError(f"candidate is missing: {source}")
        width, height = image_dimensions(source)
        sequence = 1 + sum(c["md_object_id"] == md_id for c in output_candidates)
        output_candidates.append(
            {
                "candidate_id": candidate_id(md_id, sequence),
                "md_object_id": md_id,
                "inventory": md_objects[md_id]["inventory"],
                "candidate_path": path,
                "candidate_source": row["candidate_source"],
                "candidate_role": row["candidate_role"],
                "evidence_state": row["evidence_state"],
                "review_priority": row["review_priority"],
                "format": source.suffix.lower().lstrip("."),
                "size_bytes": source.stat().st_size,
                "width_px": width,
                "height_px": height,
                "known_sha256": row.get("known_sha256", ""),
                "provenance_note": row["provenance_note"],
                "human_master_decision": row.get("human_master_decision", "pending"),
                "deletion_authorized": "false",
            }
        )

    for md_id in sorted(md_objects, key=int):
        md = md_objects[md_id]
        audit = audit_objects[md_id]
        raw_object = raw[md_id]
        images = list(raw_object.get("object_images") or [])
        main_images = [image for image in images if str(image.get("is_main")) == "j"]
        if len(main_images) != 1:
            raise InitError(f"MD {md_id} does not have exactly one main image")

        md_surrogate = MD_REFERENCE_ROOT / f"md-{md_id}.jpg"
        if not md_surrogate.is_file():
            raise InitError(f"missing retained MD main-image reference: {md_surrogate}")
        md_width, md_height = image_dimensions(md_surrogate)
        md_hash = sha256(md_surrogate)

        # Retain every MD image relation, but only the sole main image is a
        # fallback Master candidate.  Reverse/additional views are not rivals.
        for position, image in enumerate(images, 1):
            is_main = str(image.get("is_main")) == "j"
            resource_rows.append(
                {
                    "md_object_id": md_id,
                    "inventory": md["inventory"],
                    "resource_position": position,
                    "resource_id": image.get("quell_id", ""),
                    "is_main": "true" if is_main else "false",
                    "description": image.get("beschreibung", ""),
                    "creator": image.get("creator", ""),
                    "rights": image.get("rights", ""),
                    "resource_url": asset_url(image),
                    "retained_work_path": str(md_surrogate) if is_main else "",
                    "resource_role": (
                        "master_fallback_candidate"
                        if is_main
                        else "related_catalogue_resource"
                    ),
                    "accession_state": (
                        "available_in_work"
                        if is_main
                        else "fetch_and_retain_during_accession"
                    ),
                    "deletion_authorized": "false",
                }
            )

        # Every object receives the MD main image as an explicit fallback.
        add_candidate(
            md_id,
            {
                "candidate_path": str(md_surrogate),
                "candidate_source": "museum-digital retained main image",
                "candidate_role": "preservation_surrogate_candidate",
                "evidence_state": "md_main_image_fallback",
                "review_priority": 3,
                "known_sha256": md_hash,
                "provenance_note": (
                    f"MD {md_id} main resource; URL and resource ID are retained "
                    "in related-md-resources.csv"
                ),
            },
        )

        for candidate in audit_candidates[md_id]:
            add_candidate(
                md_id,
                {
                    "candidate_path": candidate["candidate_path"],
                    "candidate_source": "read-only Elements archive",
                    "candidate_role": "local_jpeg_surrogate_candidate",
                    "evidence_state": candidate["visual_state"],
                    "review_priority": 1,
                    "provenance_note": (
                        "Earlier collection-555 filename and visual comparison; "
                        f"pHash={candidate['phash_distance']}, "
                        f"dHash={candidate['dhash_distance']}"
                    ),
                },
            )

        for candidate in hires_candidates[md_id]:
            role = (
                "local_tiff_master_candidate"
                if candidate["extension"].lower() in {"tif", "tiff"}
                else "local_jpeg_surrogate_candidate"
            )
            add_candidate(
                md_id,
                {
                    "candidate_path": candidate["candidate_path"],
                    "candidate_source": "read-only Elements archive",
                    "candidate_role": role,
                    "evidence_state": candidate["review_state"],
                    "review_priority": 1,
                    "provenance_note": candidate["review_note"],
                },
            )

        existing_canonical = ""
        cross_state = ""
        for finding in overlap_by_md[md_id]:
            if finding["state"] not in {
                "confirmed_shared_entity", "confirmed_same_photo_different_md_record"
            }:
                continue
            existing_canonical = finding["other_master_path"]
            cross_state = finding["state"]
            add_candidate(
                md_id,
                {
                    "candidate_path": finding["other_master_path"],
                    "candidate_source": f"published unit {finding['other_unit']}",
                    "candidate_role": "existing_canonical_master_candidate",
                    "evidence_state": finding["state"],
                    "review_priority": 1,
                    "provenance_note": finding["evidence"],
                    "human_master_decision": (
                        "confirmed_existing_canonical"
                        if finding["state"] == "confirmed_shared_entity"
                        else "pending"
                    ),
                },
            )

        if md_id == "51888":
            init_state = "existing_canonical_master"
            proposed_path = existing_canonical
            decision_state = "confirmed_existing_canonical"
            note = "Reuse the established Kurt Schwarz canonical Master; do not create a second Master."
        elif md_id == "51887":
            init_state = "cross_unit_master_review"
            proposed_path = (
                "/media/vladimir/Elements/restore/archive/WF/transfer/"
                "Lightroom Export/Archiv/WF-Fotos/Axel.jpg"
            )
            decision_state = "pending_human_selection"
            note = "Compare the larger local JPEG with the current Kurt Master before one canonical Master is selected."
        elif md_id == "53285":
            init_state = "local_hires_master_review"
            proposed_path = hires_candidates[md_id][0]["candidate_path"]
            decision_state = "pending_human_selection"
            note = "Compare the larger same-frame JPEG with the current crop; do not promote automatically."
        elif md_id == "51889" or audit["audit_state"] == "derivative_match_only":
            init_state = "local_surrogate_review"
            local_candidates = [
                candidate for candidate in output_candidates
                if candidate["md_object_id"] == md_id
                and candidate["candidate_source"] == "read-only Elements archive"
            ]
            if not local_candidates:
                raise InitError(f"MD {md_id} is marked local but has no local candidate")
            proposed_path = local_candidates[0]["candidate_path"]
            decision_state = "pending_human_selection"
            note = "Best-extant local JPEG counterpart proposed as a surrogate Master; human confirmation required."
        else:
            init_state = "md_surrogate_review"
            proposed_path = str(md_surrogate)
            decision_state = "pending_human_selection"
            note = "No local counterpart is confirmed; retain the MD main image as the fallback surrogate candidate."

        object_rows.append(
            {
                "unit_id": UNIT_ID,
                "archival_object_id": audit["entity_id"],
                "md_object_id": md_id,
                "inventory": md["inventory"],
                "title": md["title"],
                "object_type": md["object_type"],
                "date": md["date"],
                "object_url": md["object_url"],
                "init_state": init_state,
                "proposed_master_path": proposed_path,
                "master_decision_state": decision_state,
                "candidate_count": sum(
                    candidate["md_object_id"] == md_id
                    for candidate in output_candidates
                ),
                "md_surrogate_path": str(md_surrogate),
                "md_surrogate_sha256": md_hash,
                "md_surrogate_width_px": md_width,
                "md_surrogate_height_px": md_height,
                "additional_md_resource_count": len(images) - 1,
                "cross_unit_state": cross_state,
                "existing_canonical_master_path": existing_canonical,
                "derivative_policy": (
                    "generate access derivative from the human-selected Master during accession; "
                    "when the selected Master is an MD surrogate, do not describe the result as hi-res"
                ),
                "note": note,
                "deletion_authorized": "false",
            }
        )
        decision_rows.append(
            {
                "md_object_id": md_id,
                "inventory": md["inventory"],
                "decision_state": decision_state,
                "selected_candidate_id": (
                    next(
                        candidate["candidate_id"]
                        for candidate in output_candidates
                        if candidate["md_object_id"] == md_id
                        and candidate["human_master_decision"]
                        == "confirmed_existing_canonical"
                    )
                    if decision_state == "confirmed_existing_canonical"
                    else ""
                ),
                "decision_note": note if decision_state == "confirmed_existing_canonical" else "",
                "actor": "existing accepted cross-unit identity projection" if decision_state == "confirmed_existing_canonical" else "",
                "deletion_authorized": "false",
            }
        )

    state_counts = Counter(row["init_state"] for row in object_rows)
    if state_counts != Counter(
        {
            "existing_canonical_master": 1,
            "cross_unit_master_review": 1,
            "local_hires_master_review": 1,
            "local_surrogate_review": 13,
            "md_surrogate_review": 32,
        }
    ):
        raise InitError(f"unexpected initialization state counts: {state_counts}")
    if len(resource_rows) != 61 or len(output_candidates) < 48:
        raise InitError("resource or candidate coverage is incomplete")

    OUTPUT.mkdir(parents=True)
    write_csv(OUTPUT / "accession-objects.csv", list(object_rows[0]), object_rows)
    write_csv(
        OUTPUT / "master-candidates.csv",
        list(output_candidates[0]),
        sorted(
            output_candidates,
            key=lambda row: (
                int(row["md_object_id"]), int(row["review_priority"]), row["candidate_path"].casefold()
            ),
        ),
    )
    write_csv(OUTPUT / "master-decisions.csv", list(decision_rows[0]), decision_rows)
    write_csv(OUTPUT / "related-md-resources.csv", list(resource_rows[0]), resource_rows)

    summary = {
        "format": "iss-collection-555-accession-init-v1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "unit_id": UNIT_ID,
        "museum_digital_collection": int(COLLECTION_ID),
        "objects": len(object_rows),
        "master_candidates": len(output_candidates),
        "md_resources": len(resource_rows),
        "additional_md_resources": sum(row["is_main"] == "false" for row in resource_rows),
        "init_state_counts": dict(sorted(state_counts.items())),
        "pending_human_master_decisions": sum(
            row["master_decision_state"] == "pending_human_selection"
            for row in object_rows
        ),
        "confirmed_existing_canonical_decisions": sum(
            row["master_decision_state"] == "confirmed_existing_canonical"
            for row in object_rows
        ),
        "accession_execution_authorized": False,
        "metadata_embedding_performed": False,
        "derivatives_generated": 0,
        "source_files_modified": 0,
        "published_unit_writes": 0,
        "deletion_authorized": False,
        "source_mount": "/dev/sdf1",
        "source_filesystem": "ntfs3",
        "source_read_only": True,
        "inputs": {
            "md_capture": str(MD_RUN),
            "usb_audit": str(AUDIT_RUN),
            "hires_candidates": str(HIRES_CSV),
            "cross_unit_overlap": str(OVERLAP_JSON),
        },
    }
    (OUTPUT / "init-summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (OUTPUT / "00_README.md").write_text(
        """# Pressebildarchiv WF collection 555 accession initialization

This directory prepares, but does not execute, accession for the 48 current
museum-digital objects in collection 555.

- `accession-objects.csv` gives every object one explicit initialization state.
- `master-candidates.csv` presents local, existing-unit, and MD fallback files
  together for human Master selection.
- `master-decisions.csv` contains one carried canonical decision for MD 51888;
  the other 47 objects remain pending.
- `related-md-resources.csv` records all 61 MD images. The 48 main images are
  fallback surrogate candidates. The 13 additional views are related catalogue
  resources, not competing Masters.

A museum-digital image may be selected as the best-extant preservation
surrogate when no better local source is confirmed. It remains labelled a
surrogate. A derivative generated from it during accession is an access
derivative, not a high-resolution image.

No source or published archive file was modified or copied by this
initialization. No metadata was embedded, no derivative was generated, and no
deletion is authorized. Accession must not run until the pending human Master
decisions are recorded and the document/drain scope is complete.
""",
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
