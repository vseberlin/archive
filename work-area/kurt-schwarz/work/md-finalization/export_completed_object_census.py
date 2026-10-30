#!/usr/bin/env python3
"""Export the frozen completed Kurt Schwarz object census as a readable manifest."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import sqlite3
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path


WORK = Path("/mnt/volume/iss-archive-work/kurt-schwarz")
UNIT = Path("/mnt/volume/iss-archive/units/kurt-schwarz")
RECONCILIATION = WORK / "work/reconciliation/unit-reconciliation-v1/reconciliation.sqlite"
WORK_MANIFEST = WORK / "work/md-finalization/completed-object-census-decisions.csv"
UNIT_MANIFEST = UNIT / "db-restore/manifests/object-census-decisions.csv"
COMPLETION = WORK / "work/md-finalization/completed-object-census-decisions-completion.json"
EXPECTED_DECISIONS = 1267
EXPECTED_CANDIDATES = 1032
EXPECTED_DISPOSITIONS = Counter(
    {
        "context_not_object": 4,
        "cross_unit_hold": 389,
        "existing_object": 163,
        "new_local_metadata_right": 270,
        "new_local_metadata_wrong": 4,
        "redundant": 44,
        "same_new_candidate": 126,
        "surrogate_keep": 4,
        "surrogate_master": 28,
    }
)
FIELDS = (
    "candidate_id", "filename", "primary_source_path", "source_paths_json",
    "content_sha256", "size_bytes", "queue_class", "decision_id", "disposition",
    "target_record_key", "retained_candidate_id", "published_record_key",
    "selected_master_path", "accession_state", "actor", "reason", "decided_at",
    "curated_title", "curated_date_text", "curated_description", "curated_tags",
    "cross_unit_hold_json", "deletion_authorized",
)


def now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def csv_payload(rows: list[dict[str, object]]) -> bytes:
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=FIELDS, extrasaction="ignore", quoting=csv.QUOTE_ALL)
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue().encode("utf-8")


def write_once(path: Path, payload: bytes) -> None:
    if path.exists():
        if path.read_bytes() != payload:
            raise RuntimeError(f"existing manifest differs: {path}")
        return
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    with temporary.open("xb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def main() -> int:
    reconciliation = sqlite3.connect(f"file:{RECONCILIATION}?mode=ro", uri=True)
    reconciliation.row_factory = sqlite3.Row
    published = sqlite3.connect(
        f"file:{UNIT / 'db-restore/RESTORE.sqlite'}?mode=ro", uri=True
    )
    published.row_factory = sqlite3.Row
    try:
        if reconciliation.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise RuntimeError("reconciliation quick check failed")
        if reconciliation.execute("PRAGMA foreign_key_check").fetchall():
            raise RuntimeError("reconciliation foreign keys failed")
        if published.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise RuntimeError("published quick check failed")
        if published.execute("PRAGMA foreign_key_check").fetchall():
            raise RuntimeError("published foreign keys failed")
        if reconciliation.execute("SELECT COUNT(*) FROM object_census_decisions").fetchone()[0] != EXPECTED_DECISIONS:
            raise RuntimeError("object-census decision count drifted")

        candidates = {
            str(row["candidate_id"]): dict(row)
            for row in reconciliation.execute("SELECT * FROM object_census_candidates")
        }
        decisions = {
            str(row["candidate_id"]): dict(row)
            for row in reconciliation.execute(
                """SELECT * FROM object_census_decisions d WHERE decision_id=(
                       SELECT MAX(d2.decision_id) FROM object_census_decisions d2
                        WHERE d2.candidate_id=d.candidate_id)"""
            )
        }
        if len(candidates) != EXPECTED_CANDIDATES or set(candidates) != set(decisions):
            raise RuntimeError("completed census candidate/decision binding drifted")
        dispositions = Counter(str(row["disposition"]) for row in decisions.values())
        if dispositions != EXPECTED_DISPOSITIONS:
            raise RuntimeError(f"completed census dispositions drifted: {dispositions}")

        similarity = [
            dict(row)
            for row in reconciliation.execute(
                """SELECT * FROM object_census_similarity_decisions d WHERE decision_id=(
                       SELECT MAX(d2.decision_id) FROM object_census_similarity_decisions d2
                        WHERE d2.pair_id=d.pair_id) AND outcome='same_photo'"""
            )
        ]
        retained_by_companion = {
            str(row["companion_candidate_id"]): str(row["primary_candidate_id"])
            for row in similarity
            if row["companion_candidate_id"] and row["primary_candidate_id"]
        }
        objects = {
            str(row["record_key"]): dict(row)
            for row in published.execute("SELECT * FROM objects")
        }
        master_source = {
            str(row["record_keys"]): str(row["source_path"])
            for row in published.execute(
                "SELECT record_keys,source_path FROM representations WHERE role='preservation_master'"
            )
        }

        rows: list[dict[str, object]] = []
        for candidate_id in sorted(candidates):
            candidate = candidates[candidate_id]
            decision = decisions[candidate_id]
            disposition = str(decision["disposition"])
            target = str(decision["target_record_key"] or "")
            retained = retained_by_companion.get(candidate_id, "")
            published_key = ""
            master_path = ""
            state = "not_accessioned_by_decision"
            if disposition == "new_local_metadata_right":
                published_key = candidate_id
                state = "accessioned_new_local_master"
            elif disposition == "surrogate_master":
                published_key = target
                state = "accessioned_selected_master"
            elif disposition == "surrogate_keep":
                published_key = target
                state = "retained_existing_master"
            elif disposition == "existing_object":
                published_key = target
                state = "redundant_to_existing_object"
            elif disposition in {"same_new_candidate", "redundant"}:
                published_key = retained if retained in objects else ""
                state = "reviewed_redundant"
            elif disposition == "new_local_metadata_wrong":
                state = "not_accessioned_metadata_unresolved"
            elif disposition == "cross_unit_hold":
                state = "not_accessioned_cross_unit_hold"
            elif disposition == "context_not_object":
                state = "not_accessioned_context"
            if published_key:
                if published_key not in objects:
                    raise RuntimeError(f"decision target is absent from published objects: {candidate_id}")
                master_path = str(objects[published_key]["master_relative_path"])
            if disposition == "surrogate_master":
                if master_source.get(published_key) != candidate["primary_source_path"]:
                    raise RuntimeError(f"surrogate Master is not reflected in publication: {candidate_id}")
            rows.append(
                {
                    "candidate_id": candidate_id,
                    "filename": candidate["filename"],
                    "primary_source_path": candidate["primary_source_path"],
                    "source_paths_json": candidate["source_paths_json"],
                    "content_sha256": candidate["content_sha256"],
                    "size_bytes": candidate["size_bytes"],
                    "queue_class": candidate["queue_class"],
                    "decision_id": decision["decision_id"],
                    "disposition": disposition,
                    "target_record_key": target,
                    "retained_candidate_id": retained,
                    "published_record_key": published_key,
                    "selected_master_path": master_path,
                    "accession_state": state,
                    "actor": decision["actor"],
                    "reason": decision["reason"] or "",
                    "decided_at": decision["decided_at"],
                    "curated_title": decision["curated_title"] or "",
                    "curated_date_text": decision["curated_date_text"] or "",
                    "curated_description": decision["curated_description"] or "",
                    "curated_tags": decision["curated_tags"] or "",
                    "cross_unit_hold_json": decision["cross_unit_hold_json"] or "",
                    "deletion_authorized": "false",
                }
            )
    finally:
        reconciliation.close()
        published.close()

    payload = csv_payload(rows)
    digest = "sha256:" + hashlib.sha256(payload).hexdigest()
    write_once(WORK_MANIFEST, payload)
    write_once(UNIT_MANIFEST, payload)
    completion = {
        "format": "iss-kurt-schwarz-completed-object-census-manifest-v1",
        "completed_at": now(),
        "result": "ok",
        "candidates": len(rows),
        "latest_dispositions": dict(sorted(EXPECTED_DISPOSITIONS.items())),
        "manifest_sha256": digest,
        "work_manifest": str(WORK_MANIFEST),
        "unit_manifest": str(UNIT_MANIFEST),
        "deletion_authorized": False,
    }
    write_once(
        COMPLETION,
        (json.dumps(completion, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8"),
    )
    print(json.dumps(completion, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
