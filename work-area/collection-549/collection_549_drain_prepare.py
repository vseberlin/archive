#!/usr/bin/env python3
"""Prepare the non-deleting local-trace drain projection for collection 549."""

from __future__ import annotations

import csv
import json
import os
import re
import subprocess
import uuid
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path


SOURCE_ROOT = Path("/media/vladimir/Elements")
ARCHIVE_ROOT = SOURCE_ROOT / "restore/archive"
WORK_ROOT = Path("/mnt/volume/iss-archive-work/collection-549")
RECONCILIATION = WORK_ROOT / "work/reconciliation/unit-reconciliation-v1"
OUTPUT = RECONCILIATION / "drain-list.csv"
SUMMARY = RECONCILIATION / "local-trace-search-summary.json"
FINDBOOK_ROOTS = (
    ARCHIVE_ROOT / "Findbücher",
    ARCHIVE_ROOT / "WF/Findbücher",
)
EXCLUDED_FINDBOOK_PARTS = {"Andere Scans vom Ricoah"}
SELECTED_LOCAL_MASTER = (
    ARCHIVE_ROOT
    / "WF/Findbücher/Findbücher I-IV/Findbuch V/Findbuch 83-90.pdf"
)
LOCAL_MASTER_DUPLICATE = (
    ARCHIVE_ROOT
    / "Findbücher/Findbücher I-IV/Findbuch V/Findbuch 83-90.pdf"
)
DIRECT_RULES = (
    ("49944", "Obj_10001", re.compile(r"leningrad", re.I)),
    ("51892", "Obj_10003", re.compile(r"(?<![a-z0-9])srs[ _-]*4451(?![0-9])", re.I)),
    ("51893", "Obj_10004", re.compile(r"(?<![a-z0-9])lv3n?(?![a-z0-9])", re.I)),
    ("57594", "Obj_10006", re.compile(r"colorskop", re.I)),
    ("57596", "Object_1007", re.compile(r"campingleuchte", re.I)),
    ("117742", "Object_1008", re.compile(r"(?:wettersond|radiosond)", re.I)),
    ("117744", "Object_1009", re.compile(r"(?:blitzzusatz|(?<![a-z0-9])bzg[ _-]*1(?![0-9]))", re.I)),
    ("117745", "Object_1010", re.compile(r"(?:gebrauchsanleitung.*(?:bgz|bzg)|(?<![a-z0-9])bgz[ _-]*1(?![0-9]))", re.I)),
)
FIELDS = (
    "md_object_id",
    "inventory",
    "source_path",
    "source_class",
    "source_kind",
    "relationship",
    "evidence_state",
    "source_sha256",
    "size_bytes",
    "selected_master_candidate_id",
    "selected_master_path",
    "covered_by_unit",
    "coverage_state",
    "final_archive_path",
    "drain_state",
    "deletion_authorized",
    "note",
)


class DrainPreparationError(RuntimeError):
    pass


def now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def source_guard() -> dict[str, str]:
    result = subprocess.run(
        ["findmnt", "-J", "-T", str(SOURCE_ROOT), "-o", "TARGET,SOURCE,FSTYPE,OPTIONS"],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    rows = json.loads(result.stdout).get("filesystems") or []
    if len(rows) != 1:
        raise DrainPreparationError("Elements did not resolve to one filesystem")
    row = rows[0]
    options = set(str(row.get("options", "")).split(","))
    if (
        row.get("target") != str(SOURCE_ROOT)
        or row.get("source") != "/dev/sdf1"
        or row.get("fstype") != "ntfs3"
        or "ro" not in options
    ):
        raise DrainPreparationError(f"protected Elements mount guard failed: {row}")
    return {key: str(row.get(key, "")) for key in ("target", "source", "fstype", "options")}


def source_kind(path: Path) -> str:
    suffix = path.suffix.casefold()
    if suffix in {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp", ".webp"}:
        return "image"
    if suffix in {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".csv", ".txt"}:
        return "document"
    if suffix == ".db" or path.name.casefold() == "thumbs.db":
        return "generated_cache"
    if suffix == ".zip":
        return "archive_container"
    return "other"


def atomic_csv(path: Path, rows: list[dict[str, str]]) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("x", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=FIELDS, quoting=csv.QUOTE_ALL)
            writer.writeheader()
            writer.writerows(rows)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_json(path: Path, payload: dict[str, object]) -> None:
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


def classification(path: Path, object_id: str) -> tuple[str, str, str]:
    if path == SELECTED_LOCAL_MASTER:
        return (
            "selected_master_source",
            "verified_byte_identical_local_pair_selected",
            "local-53133-candidate-03",
        )
    if path == LOCAL_MASTER_DUPLICATE:
        return (
            "redundant_local_copy",
            "verified_byte_identical_local_pair_redundant",
            "local-53133-candidate-03",
        )
    kind = source_kind(path)
    if object_id in {"49945", "53133"}:
        return ("related_local_documentation", "exact_findbook_scope_path", "")
    if kind == "image":
        return ("local_image_trace", "direct_object_name_or_path_match", "")
    if kind == "generated_cache":
        return ("generated_local_derivative", "direct_object_scope_cache", "")
    return ("related_local_documentation", "direct_object_name_or_path_match", "")


def main() -> int:
    mount = source_guard()
    decisions: dict[str, dict[str, str]] = {}
    with (RECONCILIATION / "master-decisions.csv").open(newline="", encoding="utf-8-sig") as handle:
        for row in csv.DictReader(handle):
            if row["decision_state"] != "selected_master" or not row["selected_candidate_id"]:
                raise DrainPreparationError(f"unresolved Master decision: {row['md_object_id']}")
            decisions[row["md_object_id"]] = row
    candidate_paths: dict[str, str] = {}
    with (RECONCILIATION / "master-candidates.csv").open(newline="", encoding="utf-8-sig") as handle:
        for row in csv.DictReader(handle):
            candidate_paths[row["candidate_id"]] = row["candidate_path"]

    discovered: dict[Path, tuple[str, str]] = {}
    errors: list[str] = []

    def onerror(error: OSError) -> None:
        errors.append(f"{error.filename}: {error}")

    for directory, _, names in os.walk(ARCHIVE_ROOT, onerror=onerror):
        parent = Path(directory)
        for name in names:
            path = parent / name
            relative = str(path.relative_to(ARCHIVE_ROOT))
            findbook_scope = next((root for root in FINDBOOK_ROOTS if path.is_relative_to(root)), None)
            if findbook_scope is not None:
                if EXCLUDED_FINDBOOK_PARTS.intersection(path.relative_to(findbook_scope).parts):
                    continue
                object_id = "53133" if "Findbuch V/Findbuch 83-90.pdf" in relative else "49945"
                inventory = "Obj_10002-5" if object_id == "53133" else "Obj_10002"
                discovered[path] = (object_id, inventory)
                continue
            for object_id, inventory, pattern in DIRECT_RULES:
                if pattern.search(relative):
                    discovered[path] = (object_id, inventory)
                    break

    rows: list[dict[str, str]] = []
    for path, (object_id, inventory) in sorted(discovered.items(), key=lambda item: str(item[0]).casefold()):
        try:
            stat = path.stat()
        except OSError as error:
            errors.append(f"{path}: {error}")
            continue
        relationship, evidence, selected_id = classification(path, object_id)
        selected = decisions[object_id]
        rows.append(
            {
                "md_object_id": object_id,
                "inventory": inventory,
                "source_path": str(path),
                "source_class": "protected_usb_source",
                "source_kind": source_kind(path),
                "relationship": relationship,
                "evidence_state": evidence,
                "source_sha256": "sha256:7166c4672e79bb89272c29e1b6391cd1b7dce7f62a06fda52090ea7f663c1bbb"
                if path in {SELECTED_LOCAL_MASTER, LOCAL_MASTER_DUPLICATE}
                else "",
                "size_bytes": str(stat.st_size),
                "selected_master_candidate_id": selected_id or selected["selected_candidate_id"],
                "selected_master_path": candidate_paths[selected["selected_candidate_id"]],
                "covered_by_unit": "false",
                "coverage_state": "not_yet_accessioned",
                "final_archive_path": "",
                "drain_state": "not_eligible_before_validated_accession",
                "deletion_authorized": "false",
                "note": "Source remains read-only. Drain disposition is a future proposal only; no deletion is authorized.",
            }
        )

    atomic_csv(OUTPUT, rows)
    counts = Counter(row["relationship"] for row in rows)
    summary: dict[str, object] = {
        "format": "iss-collection-549-local-trace-search-v1",
        "created_at": now(),
        "result": "ok" if not errors else "partial",
        "search_complete": not errors,
        "source_mount": mount,
        "search_root": str(ARCHIVE_ROOT),
        "direct_name_rules": len(DIRECT_RULES),
        "findbook_roots": [str(path) for path in FINDBOOK_ROOTS],
        "rows": len(rows),
        "relationship_counts": dict(sorted(counts.items())),
        "scan_errors": errors,
        "deletion_authorized": False,
        "drain_eligible_before_accession": False,
        "source_files_modified": 0,
        "source_files_deleted": 0,
    }
    atomic_json(SUMMARY, summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if not errors else 2


if __name__ == "__main__":
    raise SystemExit(main())
