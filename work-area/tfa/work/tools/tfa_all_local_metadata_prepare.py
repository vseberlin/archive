#!/usr/bin/env python3
"""Prepare a review-only metadata queue for every selected local TFA object."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import sqlite3
import subprocess
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable


VERSION = "1.0.0"
SOURCE_ROOT = Path("/media/vladimir/Elements")
ARCHIVE_ROOT = SOURCE_ROOT / "restore/archive"
WORK_ROOT = Path("/mnt/volume/iss-archive-work/tfa")
REVIEW_ROOT = WORK_ROOT / "work/reconciliation/unit-reconciliation"
PROJECT_CLASSIFICATION = REVIEW_ROOT / "local-object-project-classification.csv"
PROJECT_EVIDENCE = REVIEW_ROOT / "local-object-project-evidence.csv"
PROJECT_RECEIPT = REVIEW_ROOT / "local-object-project-scan.json"
LOCAL_METADATA_DECISIONS = REVIEW_ROOT / "local-object-metadata-review-decisions.csv"
BOOK_DECISIONS = REVIEW_ROOT / "buchprojekt-unbound-object-decisions.csv"
OUTPUT_CANDIDATES = REVIEW_ROOT / "all-local-metadata-review-candidates.csv"
OUTPUT_DECISIONS = REVIEW_ROOT / "all-local-metadata-review-decisions.csv"
OUTPUT_RECEIPT = REVIEW_ROOT / "all-local-metadata-review-preparation.json"
VISUAL_INDEX = Path("/home/vladimir/archive-visual-index/visual-index.sqlite")

METADATA_FIELDS = (
    "title", "description", "date_text", "object_type", "material",
    "dimensions", "keywords", "classification",
)
CANDIDATE_FIELDS = (
    "record_key", "inventory", "selected_master_path", "selected_master_size_bytes",
    "selected_master_mtime_ns", "extension", "width_px", "height_px",
    "proposed_title", "proposed_description", "proposed_date_text",
    "proposed_object_type", "proposed_material", "proposed_dimensions",
    "proposed_keywords", "proposed_classification", "proposal_basis",
    "prior_review_state", "prior_decision_source", "prior_decided_by", "prior_decided_at",
    "proposed_project", "project_assignment_state", "project_confidence",
    "project_candidates_json", "project_evidence_count", "project_evidence_paths_json",
    "candidate_fingerprint", "deletion_authorized",
)
DECISION_FIELDS = (
    "record_key", "decision", *METADATA_FIELDS, "note", "decided_by", "decided_at",
    "selected_master_path", "selected_master_size_bytes", "selected_master_mtime_ns",
    "candidate_fingerprint", "deletion_authorized",
)
ALLOWED_PRIOR = {
    "apply_metadata", "apply_edited_metadata", "reject_noise", "dismiss_object", "defer",
}


class PreparationError(RuntimeError):
    pass


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def row_fingerprint(row: dict[str, Any]) -> str:
    protected = {
        key: str(row.get(key, ""))
        for key in CANDIDATE_FIELDS
        if key not in {"candidate_fingerprint", "deletion_authorized"}
    }
    payload = json.dumps(protected, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def atomic_csv(path: Path, fields: Iterable[str], rows: Iterable[dict[str, Any]]) -> None:
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        with temporary.open("x", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(fields), quoting=csv.QUOTE_ALL, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def source_mount() -> dict[str, str]:
    completed = subprocess.run(
        ["findmnt", "-J", "-T", str(SOURCE_ROOT), "-o", "TARGET,SOURCE,FSTYPE,OPTIONS"],
        check=True, capture_output=True, text=True, timeout=15,
    )
    rows = json.loads(completed.stdout).get("filesystems") or []
    if len(rows) != 1:
        raise PreparationError("Elements did not resolve to one mount")
    row = rows[0]
    options = set(str(row.get("options", "")).split(","))
    if row.get("source") != "/dev/sdf1" or row.get("fstype") != "ntfs3" or "ro" not in options:
        raise PreparationError(f"Elements read-only guard failed: {row}")
    return {key: str(row.get(key, "")) for key in ("target", "source", "fstype", "options")}


def image_dimensions() -> dict[str, tuple[str, str]]:
    result: dict[str, tuple[str, str]] = {}
    with sqlite3.connect(f"file:{VISUAL_INDEX}?mode=ro", uri=True) as connection:
        root = connection.execute(
            "SELECT root_id FROM visual_roots WHERE root_path=?", (str(SOURCE_ROOT),)
        ).fetchone()
        if not root:
            raise PreparationError("Elements visual-index root is missing")
        for relative_path, width, height in connection.execute(
            "SELECT relative_path,width_px,height_px FROM visual_assets WHERE root_id=? AND status='indexed'",
            (root[0],),
        ):
            result[str(relative_path)] = (str(width or ""), str(height or ""))
    return result


def prior_decisions() -> dict[str, dict[str, str]]:
    result: dict[str, dict[str, str]] = {}
    for source, path in (
        ("local_metadata_review", LOCAL_METADATA_DECISIONS),
        ("buchprojekt_review", BOOK_DECISIONS),
    ):
        for row in read_csv(path):
            record_key = row.get("record_key", "")
            action = row.get("decision", "")
            if not record_key or action not in ALLOWED_PRIOR or record_key in result:
                raise PreparationError(f"invalid or duplicate prior metadata decision: {record_key}")
            result[record_key] = {**row, "prior_decision_source": source}
    return result


def prepare_rows() -> tuple[list[dict[str, Any]], dict[str, Any]]:
    mount = source_mount()
    project_receipt = json.loads(PROJECT_RECEIPT.read_text(encoding="utf-8"))
    if (
        project_receipt.get("result") != "ok"
        or project_receipt.get("review_projection_only") is not True
        or project_receipt.get("source_read_only") is not True
        or project_receipt.get("source_modified") is not False
        or project_receipt.get("human_project_decisions_written") != 0
        or project_receipt.get("deletion_authorized") is not False
        or project_receipt.get("output_sha256", {}).get(PROJECT_CLASSIFICATION.name) != sha256(PROJECT_CLASSIFICATION)
        or project_receipt.get("output_sha256", {}).get(PROJECT_EVIDENCE.name) != sha256(PROJECT_EVIDENCE)
    ):
        raise PreparationError("project-classification authority is unsafe or drifted")

    classifications = read_csv(PROJECT_CLASSIFICATION)
    if len(classifications) != int(project_receipt.get("classification_row_count", -1)):
        raise PreparationError("project-classification row count drifted")
    evidence_paths: defaultdict[str, list[str]] = defaultdict(list)
    for row in read_csv(PROJECT_EVIDENCE):
        path = row.get("evidence_path", "")
        if path and path not in evidence_paths[row["record_key"]]:
            evidence_paths[row["record_key"]].append(path)
    prior = prior_decisions()
    scope_keys = {row["record_key"] for row in classifications}
    prior_outside_scope = set(prior) - scope_keys
    if any(prior[key].get("decision") not in {"reject_noise", "dismiss_object"} for key in prior_outside_scope):
        raise PreparationError(f"applied prior decision escaped all-local scope: {sorted(prior_outside_scope)[:3]}")
    for key in prior_outside_scope:
        del prior[key]
    dimensions = image_dimensions()
    rows: list[dict[str, Any]] = []
    selected_paths: set[str] = set()
    for source in classifications:
        record_key = source["record_key"]
        path = Path(source["selected_master_path"]).resolve(strict=True)
        try:
            relative = path.relative_to(SOURCE_ROOT.resolve(strict=True))
            path.relative_to(ARCHIVE_ROOT.resolve(strict=True))
        except ValueError as error:
            raise PreparationError(f"selected local Master escaped the protected archive: {record_key}") from error
        if str(path) in selected_paths:
            raise PreparationError(f"selected local Master path is shared by two objects: {path}")
        selected_paths.add(str(path))
        stat = path.stat()
        prior_row = prior.get(record_key, {})
        prior_state = prior_row.get("decision", "")
        applied = prior_state in {"apply_metadata", "apply_edited_metadata"}
        proposed = {
            field: prior_row.get(field, "").strip() if applied else ""
            for field in METADATA_FIELDS
        }
        if not proposed["object_type"]:
            proposed["object_type"] = "Foto"
        basis = (
            "existing_human_metadata_decision" if applied
            else "object_type_only_user_policy; no reliable descriptive metadata proposal"
        )
        width, height = dimensions.get(str(relative), ("", ""))
        evidence = evidence_paths.get(record_key, [])
        row: dict[str, Any] = {
            "record_key": record_key,
            "inventory": source["inventory"],
            "selected_master_path": str(path),
            "selected_master_size_bytes": stat.st_size,
            "selected_master_mtime_ns": stat.st_mtime_ns,
            "extension": path.suffix.casefold(),
            "width_px": width,
            "height_px": height,
            **{f"proposed_{field}": proposed[field] for field in METADATA_FIELDS},
            "proposal_basis": basis,
            "prior_review_state": prior_state,
            "prior_decision_source": prior_row.get("prior_decision_source", ""),
            "prior_decided_by": prior_row.get("decided_by", ""),
            "prior_decided_at": prior_row.get("decided_at", ""),
            "proposed_project": source.get("proposed_project", ""),
            "project_assignment_state": source.get("assignment_state", ""),
            "project_confidence": source.get("confidence", ""),
            "project_candidates_json": source.get("project_candidates_json", "[]"),
            "project_evidence_count": source.get("project_evidence_count", "0"),
            "project_evidence_paths_json": json.dumps(evidence[:25], ensure_ascii=False),
            "deletion_authorized": "false",
        }
        row["candidate_fingerprint"] = row_fingerprint(row)
        rows.append(row)
    if len(rows) != 2483 or len({row["record_key"] for row in rows}) != len(rows):
        raise PreparationError(f"all-local object scope drifted: {len(rows)} != 2483")
    rows.sort(key=lambda row: (str(row["inventory"]).casefold(), str(row["record_key"])))
    receipt = {
        "format": "iss-tfa-all-local-metadata-review-v1",
        "version": VERSION,
        "result": "ok",
        "prepared_at": now(),
        "source_mount": mount,
        "source_read_only": True,
        "source_modified": False,
        "published_tfa_unit_changed": False,
        "selected_master_decisions_changed": False,
        "automatic_metadata_application": False,
        "human_metadata_decisions_written": 0,
        "review_projection_only": True,
        "deletion_authorized": False,
        "object_count": len(rows),
        "existing_applied_metadata": sum(
            row["prior_review_state"] in {"apply_metadata", "apply_edited_metadata"} for row in rows
        ),
        "existing_other_review_decisions": sum(
            bool(row["prior_review_state"])
            and row["prior_review_state"] not in {"apply_metadata", "apply_edited_metadata"}
            for row in rows
        ),
        "excluded_rejected_or_dismissed_nonobjects": len(prior_outside_scope),
        "open_without_prior_decision": sum(not row["prior_review_state"] for row in rows),
        "project_classification_sha256": sha256(PROJECT_CLASSIFICATION),
        "project_evidence_sha256": sha256(PROJECT_EVIDENCE),
    }
    return rows, receipt


def apply() -> dict[str, Any]:
    for path in (OUTPUT_CANDIDATES, OUTPUT_DECISIONS, OUTPUT_RECEIPT):
        if path.exists():
            raise PreparationError(f"refusing to overwrite existing all-local review projection: {path}")
    rows, receipt = prepare_rows()
    atomic_csv(OUTPUT_CANDIDATES, CANDIDATE_FIELDS, rows)
    atomic_csv(OUTPUT_DECISIONS, DECISION_FIELDS, ())
    receipt["candidate_sha256"] = sha256(OUTPUT_CANDIDATES)
    atomic_json(OUTPUT_RECEIPT, receipt)
    return receipt


def check() -> dict[str, Any]:
    source_mount()
    receipt = json.loads(OUTPUT_RECEIPT.read_text(encoding="utf-8"))
    rows = read_csv(OUTPUT_CANDIDATES)
    decisions = read_csv(OUTPUT_DECISIONS)
    if (
        receipt.get("format") != "iss-tfa-all-local-metadata-review-v1"
        or receipt.get("result") != "ok"
        or receipt.get("source_read_only") is not True
        or receipt.get("source_modified") is not False
        or receipt.get("published_tfa_unit_changed") is not False
        or receipt.get("selected_master_decisions_changed") is not False
        or receipt.get("automatic_metadata_application") is not False
        or receipt.get("deletion_authorized") is not False
        or len(rows) != int(receipt.get("object_count", -1))
        or len({row["record_key"] for row in rows}) != len(rows)
        or sha256(OUTPUT_CANDIDATES) != receipt.get("candidate_sha256")
        or any(row.get("deletion_authorized") != "false" for row in rows + decisions)
        or any(row.get("candidate_fingerprint") != row_fingerprint(row) for row in rows)
    ):
        raise PreparationError("all-local metadata review projection failed validation")
    return {
        "result": "ok", "objects": len(rows), "decisions": len(decisions),
        "source_read_only": True, "review_projection_only": True,
        "automatic_metadata_application": False, "deletion_authorized": False,
    }


def self_test() -> None:
    row = {key: "" for key in CANDIDATE_FIELDS}
    row["record_key"] = "local:TFA-1"
    first = row_fingerprint(row)
    row["deletion_authorized"] = "false"
    assert row_fingerprint(row) == first
    row["proposed_title"] = "Changed"
    assert row_fingerprint(row) != first
    assert re.fullmatch(r"[0-9a-f]{64}", first)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if sum((args.apply, args.check, args.self_test)) != 1:
        parser.error("choose exactly one action")
    if args.self_test:
        self_test()
        print("self-test: ok")
        return 0
    result = apply() if args.apply else check()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
