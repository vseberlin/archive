#!/usr/bin/env python3
"""Add the two verified local Volume V PDFs to collection-549 review data.

This is a bounded, one-time enrichment of proposal records in the approved
work root.  It does not alter either source PDF and makes no Master decision.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import subprocess
import uuid
from pathlib import Path


ROOT = Path("/mnt/volume/iss-archive-work/collection-549/work/reconciliation/unit-reconciliation-v1")
SOURCE_ROOT = Path("/media/vladimir/Elements")
OBJECTS = ROOT / "objects.csv"
CANDIDATES = ROOT / "master-candidates.csv"
DISPOSITIONS = ROOT / "candidate-dispositions.csv"
SUMMARY = ROOT / "preparation-summary.json"
LOCAL_PDFS = (
    Path("/media/vladimir/Elements/restore/archive/Findbücher/Findbücher I-IV/Findbuch V/Findbuch 83-90.pdf"),
    Path("/media/vladimir/Elements/restore/archive/WF/Findbücher/Findbücher I-IV/Findbuch V/Findbuch 83-90.pdf"),
)
EXPECTED_SHA256 = "7166c4672e79bb89272c29e1b6391cd1b7dce7f62a06fda52090ea7f663c1bbb"
EXPECTED_SIZE = 31_279_670


class EnrichmentError(RuntimeError):
    pass


def guard() -> None:
    result = subprocess.run(
        ["findmnt", "-J", "-T", str(SOURCE_ROOT), "-o", "TARGET,SOURCE,FSTYPE,OPTIONS"],
        check=True,
        capture_output=True,
        text=True,
    )
    rows = json.loads(result.stdout).get("filesystems") or []
    if len(rows) != 1:
        raise EnrichmentError("Elements does not resolve to one filesystem")
    row = rows[0]
    options = set(str(row.get("options", "")).split(","))
    if (
        row.get("target") != str(SOURCE_ROOT)
        or row.get("source") != "/dev/sdf1"
        or row.get("fstype") != "ntfs3"
        or "ro" not in options
    ):
        raise EnrichmentError(f"protected Elements mount guard failed: {row}")


def read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        return list(reader.fieldnames or []), [dict(row) for row in reader]


def csv_payload(fields: list[str], rows: list[dict[str, str]]) -> bytes:
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore", quoting=csv.QUOTE_ALL)
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue().encode("utf-8")


def atomic_replace(path: Path, payload: bytes) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(8 * 1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def main() -> None:
    guard()
    candidate_fields, candidates = read_csv(CANDIDATES)
    disposition_fields, dispositions = read_csv(DISPOSITIONS)
    object_fields, objects = read_csv(OBJECTS)
    existing_ids = {row["candidate_id"] for row in candidates}
    target_ids = {"local-53133-candidate-02", "local-53133-candidate-03"}
    if target_ids.issubset(existing_ids):
        print(json.dumps({"result": "already_enriched", "master_candidates": len(candidates)}, indent=2))
        return
    if target_ids & existing_ids or len(candidates) != 10 or len(dispositions) != 10:
        raise EnrichmentError("candidate projections are not in the expected initial state")

    for path in LOCAL_PDFS:
        if not path.is_file() or path.stat().st_size != EXPECTED_SIZE or sha256(path) != EXPECTED_SHA256:
            raise EnrichmentError(f"verified local PDF identity changed: {path}")

    base = next(row for row in candidates if row["md_object_id"] == "53133")
    additions: list[dict[str, str]] = []
    for number, path in enumerate(LOCAL_PDFS, start=2):
        candidate_id = f"local-53133-candidate-{number:02d}"
        additions.append(
            {
                **base,
                "candidate_id": candidate_id,
                "candidate_path": str(path),
                "candidate_source": "read-only Elements archive; local Volume V scan",
                "candidate_role": "local_master_candidate",
                "resource_id": "",
                "evidence_state": "verified_local_duplicate_pair",
                "evidence_detail": (
                    "10-page A3 PDF; byte-identical to the other local Volume V copy; "
                    "larger and earlier PDF representation than the museum-digital resource"
                ),
                "format": "pdf",
                "size_bytes": str(EXPECTED_SIZE),
                "width_px": "",
                "height_px": "",
                "known_sha256": f"sha256:{EXPECTED_SHA256}",
                "deletion_authorized": "false",
            }
        )

    candidates.extend(additions)
    candidates.sort(key=lambda row: (int(row["md_object_id"]), row["candidate_id"]))
    for row in additions:
        dispositions.append(
            {
                "candidate_id": row["candidate_id"],
                "md_object_id": row["md_object_id"],
                "candidate_path": row["candidate_path"],
                "disposition": "pending",
                "reason": "awaiting human Master decision; duplicate relationship verified",
                "deletion_authorized": "false",
            }
        )
    dispositions.sort(key=lambda row: (int(row["md_object_id"]), row["candidate_id"]))
    for row in objects:
        if row["md_object_id"] == "53133":
            row["candidate_count"] = "3"

    summary = json.loads(SUMMARY.read_text(encoding="utf-8"))
    if summary.get("master_selections_made") != 0 or summary.get("deletion_authorized") is not False:
        raise EnrichmentError("preparation summary no longer represents unresolved proposal state")
    summary["master_candidates"] = 12
    summary["verified_local_pdf_candidates"] = 2
    summary["verified_local_pdf_duplicate_groups"] = 1

    atomic_replace(CANDIDATES, csv_payload(candidate_fields, candidates))
    atomic_replace(DISPOSITIONS, csv_payload(disposition_fields, dispositions))
    atomic_replace(OBJECTS, csv_payload(object_fields, objects))
    atomic_replace(
        SUMMARY,
        (json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8"),
    )
    print(
        json.dumps(
            {
                "result": "ok",
                "master_candidates": len(candidates),
                "object_53133_candidates": 3,
                "master_selections_made": 0,
                "deletion_authorized": False,
                "source_files_modified": 0,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
