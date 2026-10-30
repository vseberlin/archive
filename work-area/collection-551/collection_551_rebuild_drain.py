#!/usr/bin/env python3
"""Expand the prepared collection-551 drain list from bounded source rules."""

from __future__ import annotations

import csv
import importlib.util
import json
import os
import uuid
from collections import Counter
from pathlib import Path


HERE = Path(__file__).resolve().parent
PREP_PATH = HERE / "collection_551_prepare.py"
SPEC = importlib.util.spec_from_file_location("collection_551_prepare", PREP_PATH)
PREP = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(PREP)
ROOT = PREP.DEFAULT_OUTPUT
DRAIN = ROOT / "drain-list.csv"
OBJECTS = ROOT / "objects.csv"
DECISIONS = ROOT / "master-decisions.csv"
SUMMARY = ROOT / "preparation-summary.json"
RECEIPT = ROOT / "drain-list-rebuild-receipt.json"


def read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        return list(reader.fieldnames or []), [dict(row) for row in reader]


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


def main() -> None:
    mount = PREP.source_guard()
    if RECEIPT.exists():
        raise RuntimeError(f"drain rebuild already recorded: {RECEIPT}")
    _decision_fields, decisions = read_csv(DECISIONS)
    if any(row["decision_state"] != "unresolved" for row in decisions):
        raise RuntimeError("drain expansion must run before human Master decisions")
    fields, rows = read_csv(DRAIN)
    _object_fields, objects = read_csv(OBJECTS)
    object_by_key = {PREP.inventory_key(row["inventory"]): row for row in objects}
    by_path = {row["source_path"]: row for row in rows}
    added = []
    for root in PREP.COLLECTION_SOURCE_DIRS:
        for directory, _subdirs, filenames in os.walk(root):
            for filename in filenames:
                path = Path(directory) / filename
                classification = PREP.drain_class(path)
                if not classification or str(path) in by_path:
                    continue
                object_row = None
                for key in PREP.filename_keys(path.name):
                    if key in object_by_key:
                        object_row = object_by_key[key]
                        break
                row = {
                    "md_object_id": object_row["md_object_id"] if object_row else "",
                    "inventory": object_row["inventory"] if object_row else "",
                    "source_path": str(path), "source_class": "protected_usb_source",
                    "source_kind": path.suffix.casefold().lstrip(".") or "file",
                    "classification": classification[0],
                    "relationship": "derivative_or_transient_cleanup_candidate",
                    "evidence": classification[1], "sha256": PREP.sha256_file(path),
                    "size_bytes": str(path.stat().st_size), "selected_master_candidate_id": "",
                    "covered_by_selected_master": "false", "coverage_state": "not_yet_accessioned",
                    "drain_state": "blocked_until_human_decisions_and_validated_accession",
                    "deletion_authorized": "false",
                    "note": "Drain listing records cleanup intent only; it never authorizes deletion.",
                }
                by_path[str(path)] = row
                added.append(row)
    output = sorted(by_path.values(), key=lambda row: (row["source_class"], row["source_path"].casefold()))
    if any(row["deletion_authorized"] != "false" for row in output):
        raise RuntimeError("drain projection carries deletion authority")
    atomic_replace(DRAIN, PREP.csv_payload(fields, output))
    summary = json.loads(SUMMARY.read_text(encoding="utf-8"))
    summary["drain_rows"] = len(output)
    summary["drain_protected_source_rows"] = sum(row["source_class"] == "protected_usb_source" for row in output)
    summary["drain_work_capture_rows"] = sum(row["source_class"] == "work_capture" for row in output)
    summary["drain_classifications"] = dict(sorted(Counter(row["classification"] for row in output).items()))
    atomic_replace(SUMMARY, (json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode())
    receipt = {
        "format": "iss-collection-551-drain-list-rebuild-v1", "created_at": PREP.now(),
        "result": "ok", "reason": "include root-level museum-digital staging files and derivative bundle",
        "mount": mount, "previous_rows": len(rows), "added_rows": len(added), "final_rows": len(output),
        "added_classifications": dict(sorted(Counter(row["classification"] for row in added).items())),
        "source_files_modified": 0, "master_decisions_changed": 0,
        "accession_performed": False, "deletion_authorized": False,
    }
    PREP.atomic_json(RECEIPT, receipt)
    print(json.dumps(receipt, ensure_ascii=False, sort_keys=True, indent=2))


if __name__ == "__main__":
    main()
