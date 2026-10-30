#!/usr/bin/env python3
"""Accession the completed Kurt Schwarz census delta into the published unit."""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
BASE_PATH = SCRIPT_DIR / "access_decided_local_objects.py"
SPEC = importlib.util.spec_from_file_location("kurt_local_accession_base", BASE_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"cannot load established accession code: {BASE_PATH}")
base = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = base
SPEC.loader.exec_module(base)

EXPECTED_CENSUS_DECISIONS = 1267
EXPECTED_ACCEPTED_LOCAL_TOTAL = 270
EXPECTED_PUBLISHED_LOCAL = 186
EXPECTED_DELTA = 84
EXPECTED_BASE_OBJECTS = 1851
EXPECTED_FINAL_OBJECTS = 1935
EXPECTED_BASE_REPRESENTATIONS = 3702
EXPECTED_FINAL_REPRESENTATIONS = 3870
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

base.EXPECTED_LOCAL_OBJECTS = EXPECTED_ACCEPTED_LOCAL_TOTAL
base.MANIFEST = SCRIPT_DIR / "completed-object-census-accession.csv"
base.COMPLETION = SCRIPT_DIR / "completed-object-census-accession-completion.json"
base.BEFORE_DB = SCRIPT_DIR / "completed-object-census-accession-before.sqlite"
base.BEFORE_README = SCRIPT_DIR / "completed-object-census-accession-before-readme.md"


def load_scope() -> list[dict[str, Any]]:
    connection = sqlite3.connect(f"file:{base.RECONCILIATION}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    published = sqlite3.connect(
        f"file:{base.UNIT / 'db-restore/RESTORE.sqlite'}?mode=ro", uri=True
    )
    try:
        if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise base.AccessionError("reconciliation database quick check failed")
        if connection.execute("PRAGMA foreign_key_check").fetchall():
            raise base.AccessionError("reconciliation database has foreign-key violations")
        if published.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise base.AccessionError("published database quick check failed")
        if published.execute("PRAGMA foreign_key_check").fetchall():
            raise base.AccessionError("published database has foreign-key violations")

        decisions = base.latest_rows(connection, "object_census_decisions", "candidate_id")
        candidates = {
            str(row["candidate_id"]): dict(row)
            for row in connection.execute("SELECT * FROM object_census_candidates")
        }
        published_keys = {
            str(row[0]) for row in published.execute("SELECT record_key FROM objects")
        }
        decision_count = connection.execute(
            "SELECT COUNT(*) FROM object_census_decisions"
        ).fetchone()[0]
    finally:
        connection.close()
        published.close()

    if decision_count != EXPECTED_CENSUS_DECISIONS:
        raise base.AccessionError(
            f"completed census decision count drifted: {decision_count} != {EXPECTED_CENSUS_DECISIONS}"
        )
    dispositions = Counter(str(row["disposition"]) for row in decisions.values())
    if dispositions != EXPECTED_DISPOSITIONS:
        raise base.AccessionError(f"completed census dispositions drifted: {dispositions}")

    accepted = sorted(
        candidate_id
        for candidate_id, decision in decisions.items()
        if decision["disposition"] == "new_local_metadata_right"
    )
    if len(accepted) != EXPECTED_ACCEPTED_LOCAL_TOTAL:
        raise base.AccessionError("accepted local-object total drifted")
    existing = [candidate_id for candidate_id in accepted if candidate_id in published_keys]
    selected_ids = [candidate_id for candidate_id in accepted if candidate_id not in published_keys]
    if len(existing) != EXPECTED_PUBLISHED_LOCAL or len(selected_ids) != EXPECTED_DELTA:
        raise base.AccessionError(
            f"published/delta scope drifted: {len(existing)}/{len(selected_ids)}"
        )

    scope: list[dict[str, Any]] = []
    for candidate_id in selected_ids:
        candidate = candidates[candidate_id]
        decision = decisions[candidate_id]
        source_paths = json.loads(candidate["source_paths_json"])
        primary = candidate["primary_source_path"]
        if primary not in source_paths:
            raise base.AccessionError(f"primary source is not manifested: {candidate_id}")
        path = Path(primary)
        if not path.is_file() or not path.resolve().is_relative_to(base.SOURCE.resolve()):
            raise base.AccessionError(f"selected source is absent or outside Elements: {path}")
        suffix = path.suffix.casefold()
        if suffix not in {".tif", ".tiff", ".jpg", ".jpeg"}:
            raise base.AccessionError(f"unsupported selected Master format: {path}")
        if path.stat().st_size != int(candidate["size_bytes"]):
            raise base.AccessionError(f"selected source size drifted: {path}")
        scope.append(
            {
                "candidate_id": candidate_id,
                "decision_id": int(decision["decision_id"]),
                "decided_at": decision["decided_at"],
                "actor": decision["actor"],
                "reason": decision["reason"] or "",
                "filename": candidate["filename"],
                "extension": candidate["extension"],
                "content_sha256": candidate["content_sha256"],
                "size_bytes": int(candidate["size_bytes"]),
                "primary_source_path": primary,
                "source_paths": source_paths,
                "metadata": base.effective_metadata(candidate, decision),
                "companions": [],
                "master_relative_path": f"masters/{candidate['filename']}",
                "access_relative_path": f"export/access/{Path(candidate['filename']).stem}.jpg",
            }
        )
    return sorted(scope, key=lambda item: (item["filename"].casefold(), item["candidate_id"]))


def preflight(scope: list[dict[str, Any]]) -> dict[str, Any]:
    read_only, mount = base.source_is_read_only()
    if not read_only:
        raise base.AccessionError(f"Elements is not safely read-only: {mount}")
    for path in (base.UNIT, base.WORK, base.UNIT / "masters", base.UNIT / "export/access"):
        if not path.is_dir():
            raise base.AccessionError(f"required path is absent: {path}")
    if base.UNIT.stat().st_dev != base.WORK.stat().st_dev:
        raise base.AccessionError("archive and work roots are not on the same filesystem")
    master_paths = [item["master_relative_path"].casefold() for item in scope]
    access_paths = [item["access_relative_path"].casefold() for item in scope]
    if len(master_paths) != len(set(master_paths)) or len(access_paths) != len(set(access_paths)):
        raise base.AccessionError("accession destination paths collide")
    for item in scope:
        if (base.UNIT / item["master_relative_path"]).exists():
            raise base.AccessionError(f"new Master destination exists: {item['master_relative_path']}")
        if (base.UNIT / item["access_relative_path"]).exists():
            raise base.AccessionError(f"new access destination exists: {item['access_relative_path']}")
    with sqlite3.connect(
        f"file:{base.UNIT / 'db-restore/RESTORE.sqlite'}?mode=ro", uri=True
    ) as restore:
        objects = restore.execute("SELECT COUNT(*) FROM objects").fetchone()[0]
        representations = restore.execute("SELECT COUNT(*) FROM representations").fetchone()[0]
    if objects not in {EXPECTED_BASE_OBJECTS, EXPECTED_FINAL_OBJECTS}:
        raise base.AccessionError(f"published object count is unexpected: {objects}")
    if representations not in {EXPECTED_BASE_REPRESENTATIONS, EXPECTED_FINAL_REPRESENTATIONS}:
        raise base.AccessionError(f"published representation count is unexpected: {representations}")
    return {
        "result": "ok",
        "source_mount": mount,
        "census_candidates": sum(EXPECTED_DISPOSITIONS.values()),
        "accepted_local_total": EXPECTED_ACCEPTED_LOCAL_TOTAL,
        "already_published_local": EXPECTED_PUBLISHED_LOCAL,
        "local_objects_this_accession": len(scope),
        "tiff_masters": sum(item["extension"].casefold() in {".tif", ".tiff"} for item in scope),
        "jpeg_masters": sum(item["extension"].casefold() in {".jpg", ".jpeg"} for item in scope),
        "master_bytes": sum(item["size_bytes"] for item in scope),
        "deletion_authorized": False,
    }


original_build_controls = base.build_controls


def build_controls(results: list[dict[str, Any]]):
    controls, counts = original_build_controls(results)
    readme_path = base.UNIT / "00_README.md"
    text = controls[readme_path].decode("utf-8")
    lines: list[str] = []
    local_line_written = False
    for line in text.splitlines():
        if line.startswith("- Retained objects and Masters:"):
            line = f"- Retained objects and Masters: {EXPECTED_FINAL_OBJECTS}"
        elif line.startswith("- Generated access derivatives:"):
            line = f"- Generated access derivatives: {EXPECTED_FINAL_OBJECTS}"
        elif line.startswith("- Curator-decided local objects accessioned outside MD scope:"):
            if local_line_written:
                continue
            line = f"- Curator-decided local objects accessioned outside MD scope: {EXPECTED_ACCEPTED_LOCAL_TOTAL}"
            local_line_written = True
        line = line.replace(
            "All 1,851 selected Masters have a generated full-resolution access derivative.",
            f"All {EXPECTED_FINAL_OBJECTS:,} selected Masters have a generated full-resolution access derivative.",
        )
        lines.append(line)
    controls[readme_path] = ("\n".join(lines) + "\n").encode("utf-8")
    counts["local_objects_added"] = EXPECTED_DELTA
    counts["local_objects_total"] = EXPECTED_ACCEPTED_LOCAL_TOTAL
    return controls, counts


base.load_scope = load_scope
base.preflight = preflight
base.build_controls = build_controls


if __name__ == "__main__":
    raise SystemExit(base.main())
