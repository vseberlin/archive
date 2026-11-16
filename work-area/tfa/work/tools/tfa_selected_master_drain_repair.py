#!/usr/bin/env python3
"""Remove selected TFA MD Masters from the redundant/drain projection."""

from __future__ import annotations

import csv
import importlib.util
import json
import sqlite3
import subprocess
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


HELPER = Path(__file__).with_name("tfa_reference_photo_autoselect.py")
SPEC = importlib.util.spec_from_file_location("tfa_reference_photo", HELPER)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("cannot load reference-photo helpers")
M = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(M)

RECEIPT = M.REVIEW_ROOT / "selected-master-drain-repair.json"
EXPECTED = 43


def now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def selected_drain_rows(db: sqlite3.Connection) -> list[dict[str, Any]]:
    return [dict(r) for r in db.execute(
        """SELECT d.record_key,o.inventory,c.source_path,x.preservation_basis
           FROM reconciliation_decisions d
           JOIN objects o ON o.record_key=d.record_key
           JOIN candidates c ON c.candidate_id=d.selected_candidate_id
           JOIN drain_list_proposal x ON x.source_path=c.source_path
           WHERE d.decision_state='selected_master'
           ORDER BY o.inventory"""
    )]


def main() -> int:
    mount = M.source_mount()
    state = subprocess.run(
        ["systemctl","--user","show","iss-archive-tfa-workbench.service","-p","ActiveState","--value"],
        check=True,capture_output=True,text=True,
    ).stdout.strip()
    if state not in {"inactive","failed"}:
        raise M.ReconciliationError("stop port 8768 before repairing the drain projection")
    with closing(M.connect(M.DATABASE, read_only=True)) as db:
        rows = selected_drain_rows(db)
        quick = db.execute("PRAGMA quick_check").fetchone()[0]
    if quick != "ok" or len(rows) != EXPECTED:
        raise M.ReconciliationError(f"selected/drain repair scope drifted: quick={quick} rows={len(rows)}")
    if any(r["preservation_basis"] != "visual_match_to_current_md_record" for r in rows):
        raise M.ReconciliationError("selected/drain conflict contains an unexpected disposition")
    paths = {r["source_path"] for r in rows}
    stamp = now().replace("-", "").replace(":", "")
    targets = (
        M.DATABASE,M.CORE_SOURCE_DISPOSITION,M.CORE_DRAIN_LIST,
        M.SOURCE_DISPOSITION,M.DRAIN_LIST,RECEIPT,
    )
    backups = [x for x in (M.backup(p,stamp) for p in targets) if x]
    with closing(M.connect(M.DATABASE)) as db:
        db.execute("BEGIN IMMEDIATE")
        db.executemany("DELETE FROM drain_list_proposal WHERE source_path=?", ((p,) for p in paths))
        db.executemany(
            """UPDATE source_disposition_proposal
               SET source_role='selected_master',preservation_status='selected_master',
                   preservation_basis='current_md_selected_master_not_redundant',
                   drain_state='retain_selected_master',deletion_authorized='false',
                   notes='Current selected MD Master; excluded from redundant/drain projection.'
               WHERE source_path=?""",
            ((p,) for p in paths),
        )
        db.commit()
    with closing(M.connect(M.DATABASE, read_only=True)) as db:
        quick = db.execute("PRAGMA quick_check").fetchone()[0]
        foreign = db.execute("PRAGMA foreign_key_check").fetchall()
        remaining = selected_drain_rows(db)
        core_source = [dict(r) for r in db.execute("SELECT * FROM source_disposition_proposal ORDER BY source_path")]
        core_drain = [dict(r) for r in db.execute("SELECT * FROM drain_list_proposal ORDER BY source_path")]
    if quick != "ok" or foreign or remaining:
        raise M.ReconciliationError(
            f"post-repair validation failed: quick={quick} foreign={len(foreign)} remaining={len(remaining)}"
        )
    M.atomic_csv(M.CORE_SOURCE_DISPOSITION, core_source[0].keys(), core_source)
    M.atomic_csv(M.CORE_DRAIN_LIST, core_drain[0].keys(), core_drain)

    reference_source = M.read_csv(M.SOURCE_DISPOSITION)
    by_path = {r["source_path"]: r for r in reference_source}
    missing = paths - set(by_path)
    if missing:
        raise M.ReconciliationError(f"reference source projection lacks selected paths: {len(missing)}")
    for path in paths:
        row = by_path[path]
        row.update({
            "preservation_status": "selected_master",
            "preservation_basis": "current_md_selected_master_not_redundant",
            "drain_state": "retained_master", "deletion_authorized": "false",
        })
    reference_source = sorted(by_path.values(), key=lambda r: r["source_path"])
    reference_drain = [r for r in reference_source if r["preservation_status"] == "redundant"]
    M.atomic_csv(M.SOURCE_DISPOSITION,M.SOURCE_FIELDS,reference_source)
    M.atomic_csv(M.DRAIN_LIST,M.SOURCE_FIELDS,reference_drain)

    receipt = {
        "format":"iss-tfa-selected-master-drain-repair-v1","result":"ok","applied_at":now(),
        "source_mount":mount,"source_read_only":True,"source_files_modified":False,
        "deletion_authorized":False,"selected_master_paths_removed_from_drain":len(paths),
        "remaining_selected_master_drain_intersection":0,
        "catalogue_drain_rows":len(core_drain),"reference_drain_rows":len(reference_drain),
        "sqlite_quick_check":quick,"sqlite_foreign_key_violations":len(foreign),"backups":backups,
    }
    M.atomic_json(RECEIPT,receipt)
    print(json.dumps(receipt,ensure_ascii=False,indent=2,sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
