#!/usr/bin/env python3
"""Rebuild collection-555 post-accession source coverage and drain manifests."""

from __future__ import annotations

import csv
import hashlib
import json
import os
import subprocess
import uuid
from pathlib import Path


WORK_ROOT = Path("/mnt/volume/iss-archive-work/pressebildarchiv-wf")
INIT_ROOT = WORK_ROOT / "work/accession/collection-555-init-20260829"
AUDIT_CSV = WORK_ROOT / "work/usb-master-search/runs/20260803T101000Z-pressebildarchiv-wf-usb-master-search-corrected/candidates.csv"
CANDIDATES_CSV = INIT_ROOT / "master-candidates.csv"
DECISIONS_CSV = INIT_ROOT / "master-decisions.csv"
RESOURCES_CSV = INIT_ROOT / "related-md-resources.csv"
DOCUMENTS_CSV = INIT_ROOT / "document-source-disposition.csv"
DRAIN_CSV = INIT_ROOT / "drain-list.csv"
DOCUMENT_DRAIN_CSV = INIT_ROOT / "document-drain-list.csv"

FIELDS = [
    "md_object_id", "inventory", "source_path", "source_class", "source_kind",
    "relationship", "evidence_state", "source_sha256", "size_bytes",
    "selected_master_candidate_id", "selected_master_path", "covered_by_unit",
    "coverage_state", "final_archive_path", "drain_state",
    "deletion_authorized", "note",
]


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def require_read_only_source() -> str:
    result = subprocess.run(
        ["findmnt", "-S", "/dev/sdf1", "-n", "-o", "SOURCE,FSTYPE,OPTIONS,TARGET"],
        capture_output=True, text=True, check=True,
    )
    record = result.stdout.strip()
    source, filesystem, options, target = record.split()
    if source != "/dev/sdf1" or filesystem != "ntfs3" or target != "/media/vladimir/Elements" or "ro" not in options.split(","):
        raise RuntimeError(f"protected Elements mount guard failed: {record}")
    return record


def sha256(path: Path, known: str = "") -> str:
    if known.startswith("sha256:"):
        return known
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def atomic_write(path: Path, rows: list[dict[str, str]]) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=FIELDS, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> None:
    mount = require_read_only_source()
    candidates = read_csv(CANDIDATES_CSV)
    candidates_by_id = {row["candidate_id"]: row for row in candidates}
    decisions = {row["md_object_id"]: row for row in read_csv(DECISIONS_CSV)}
    selected_by_md = {
        md_id: candidates_by_id[row["selected_candidate_id"]]
        for md_id, row in decisions.items()
        if row["selected_candidate_id"]
    }
    rows_by_path: dict[str, dict[str, str]] = {}

    def add(
        *, path_text: str, md_id: str = "", inventory: str = "",
        source_class: str, source_kind: str, relationship: str,
        evidence: str, known_sha256: str = "", note: str,
    ) -> None:
        if not path_text or path_text in rows_by_path:
            return
        path = Path(path_text)
        if not path.is_file():
            raise RuntimeError(f"covered source is missing: {path}")
        selected = selected_by_md.get(md_id, {})
        rows_by_path[path_text] = {
            "md_object_id": md_id,
            "inventory": inventory,
            "source_path": path_text,
            "source_class": source_class,
            "source_kind": source_kind,
            "relationship": relationship,
            "evidence_state": evidence,
            "source_sha256": sha256(path, known_sha256),
            "size_bytes": str(path.stat().st_size),
            "selected_master_candidate_id": selected.get("candidate_id", ""),
            "selected_master_path": selected.get("candidate_path", ""),
            "covered_by_unit": "true",
            "coverage_state": "planned_unit_coverage",
            "final_archive_path": "",
            "drain_state": "blocked_until_validated_accession",
            "deletion_authorized": "false",
            "note": note,
        }

    for row in read_csv(AUDIT_CSV):
        if row["candidate_kind"] != "whole_usb_filename" or not row["candidate_path"]:
            continue
        relationship = (
            "matched_local_image_occurrence"
            if row["visual_state"] in {"confirmed_visual_match", "probable_visual_match_review"}
            else "inventory_linked_derivative_or_noise"
        )
        add(
            path_text=row["candidate_path"], md_id=row["md_object_id"],
            inventory=row["inventory"], source_class="protected_usb_source",
            source_kind="image", relationship=relationship,
            evidence=row["visual_state"],
            note="Exhaustive whole-USB filename occurrence for a collection-555 inventory number.",
        )

    for row in candidates:
        if not row["candidate_path"].startswith("/media/vladimir/Elements/"):
            continue
        add(
            path_text=row["candidate_path"], md_id=row["md_object_id"],
            inventory=row["inventory"], source_class="protected_usb_source",
            source_kind="image", relationship="reconciled_local_image_candidate",
            evidence=row["evidence_state"], known_sha256=row["known_sha256"],
            note=f"Reviewed candidate role: {row['candidate_role']}.",
        )

    for md_id, selected in selected_by_md.items():
        if not selected["candidate_path"].startswith("/mnt/volume/iss-archive-work/"):
            continue
        add(
            path_text=selected["candidate_path"], md_id=md_id,
            inventory=selected["inventory"], source_class="work_capture",
            source_kind="image", relationship="selected_master_input",
            evidence="human_selected_master", known_sha256=selected["known_sha256"],
            note="Selected museum-digital capture input; removable only after validated accession coverage.",
        )

    for row in read_csv(RESOURCES_CSV):
        if not row["retained_work_path"]:
            continue
        add(
            path_text=row["retained_work_path"], md_id=row["md_object_id"],
            inventory=row["inventory"], source_class="work_capture",
            source_kind="image", relationship=(
                "selected_master_input" if row["is_main"] == "true"
                else "related_catalogue_image_input"
            ), evidence=row["resource_role"],
            note="Retained museum-digital resource covered by the accessioned unit.",
        )

    for row in read_csv(DOCUMENTS_CSV):
        if not row["disposition"].startswith("retain_project_document"):
            continue
        add(
            path_text=row["source_path"], source_class="protected_usb_source",
            source_kind="document", relationship="selected_document_input",
            evidence=row["integrity_state"], known_sha256=row["sha256"],
            note=row["review_note"],
        )

    rows = sorted(rows_by_path.values(), key=lambda row: (row["source_class"], row["source_kind"], row["source_path"]))
    if any(row["deletion_authorized"] != "false" for row in rows):
        raise RuntimeError("drain projection carries deletion authority")
    atomic_write(DRAIN_CSV, rows)
    atomic_write(DOCUMENT_DRAIN_CSV, [row for row in rows if row["source_kind"] == "document"])
    print(json.dumps({
        "result": "ok", "mount": mount, "drain_rows": len(rows),
        "usb_rows": sum(row["source_class"] == "protected_usb_source" for row in rows),
        "work_capture_rows": sum(row["source_class"] == "work_capture" for row in rows),
        "image_rows": sum(row["source_kind"] == "image" for row in rows),
        "document_rows": sum(row["source_kind"] == "document" for row in rows),
        "deletion_authorized_rows": 0,
    }, indent=2))


if __name__ == "__main__":
    main()
