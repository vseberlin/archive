#!/usr/bin/env python3
"""Consolidate completed TFA visual-object review into accession candidates."""

from __future__ import annotations

import csv
import importlib.util
import json
from collections import Counter, defaultdict
from contextlib import closing
from pathlib import Path
from typing import Any

TOOL = Path(__file__).with_name("tfa_reference_photo_autoselect.py")
SPEC = importlib.util.spec_from_file_location("tfa_reference_photo", TOOL)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("cannot load reference-photo reconciliation helpers")
M = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(M)

ROOT = M.REVIEW_ROOT
REVIEWED = ROOT / "reference-photo-reviewed-visual-decisions.csv"
CONSOLIDATION_RECEIPT = ROOT / "reference-photo-visual-review-consolidation.json"
REVIEWED_FIELDS = (
    "visual_group_id", "candidate_inventory", "record_key", "decision",
    "inventory_evidence", "selected_asset_id", "selected_source_path",
    "redundant_source_paths_json", "noise_source_paths_json", "metadata_state",
    "note", "decided_by", "decided_at", "deletion_authorized",
)


def reviewed_inventory(group_id: str) -> str:
    return "TFA-VISUAL-" + group_id.removeprefix("visual-").upper()


def main() -> int:
    if M.workbench_listening():
        raise M.ReconciliationError("stop port 8768 before consolidating visual review")
    mount = M.source_mount()
    decisions = M.read_csv(M.ORPHAN_DECISIONS)
    dispositions = M.read_csv(M.ORPHAN_DISPOSITIONS)
    workbench = M.read_csv(M.WORKBENCH_HOLDS)
    if not decisions or any(row["decision"] == "unresolved" for row in decisions):
        raise M.ReconciliationError("visual-object review is not complete")
    if Counter(row["decision"] for row in decisions) != Counter({"selected_master": 13, "noise": 1}):
        raise M.ReconciliationError("unexpected completed visual-review decision counts")

    rows_by_group: defaultdict[str, list[dict[str, str]]] = defaultdict(list)
    for row in workbench:
        rows_by_group[row["visual_group_id"]].append(row)
    disposition_by_group: defaultdict[str, list[dict[str, str]]] = defaultdict(list)
    for row in dispositions:
        disposition_by_group[row["visual_group_id"]].append(row)
    decision_by_group = {
        row["record_key"].removeprefix("orphan:"): row for row in decisions
    }
    if set(rows_by_group) != set(decision_by_group) or set(disposition_by_group) != set(decision_by_group):
        raise M.ReconciliationError("visual-review decisions and candidate paths disagree")

    selected_groups: list[dict[str, Any]] = []
    reviewed_rows: list[dict[str, Any]] = []
    replacement_dispositions: dict[str, dict[str, Any]] = {}
    for group_id in sorted(decision_by_group):
        decision = decision_by_group[group_id]
        rows = rows_by_group[group_id]
        inventory_evidence = "; ".join(sorted({row["inventory_members"] for row in rows}))
        candidate_inventory = reviewed_inventory(group_id)
        record_key = "local:" + candidate_inventory
        selected = next(
            (row for row in rows if row["asset_id"] == decision["selected_asset_id"]), None
        )
        if decision["decision"] == "selected_master" and selected is None:
            raise M.ReconciliationError(f"selected asset escaped visual group: {group_id}")
        if selected is not None:
            path = Path(selected["source_path"])
            stat = path.stat()
            if stat.st_size != int(selected["size_bytes"]) or stat.st_mtime_ns != int(selected["mtime_ns"]):
                raise M.ReconciliationError(f"selected source drifted: {path}")
            selected_groups.append({
                "group_id": group_id, "inventory": candidate_inventory,
                "record_key": record_key, "decision": decision,
                "selected": selected, "rows": rows,
                "inventory_evidence": inventory_evidence,
            })
        redundant_paths = []
        noise_paths = []
        for row in rows:
            source_path = Path(row["source_path"]).resolve(strict=True)
            source_path.relative_to(M.ARCHIVE_ROOT.resolve(strict=True))
            source_stat = source_path.stat()
            if source_stat.st_size != int(row["size_bytes"]) or source_stat.st_mtime_ns != int(row["mtime_ns"]):
                raise M.ReconciliationError(f"reviewed source drifted: {source_path}")
            if decision["decision"] == "noise":
                status, basis, drain = "noise", "human_dismissed_visual_object_as_noise", "proposed_noise"
                noise_paths.append(row["source_path"])
                row_record_key = ""
                row_inventory = candidate_inventory
            elif row["asset_id"] == decision["selected_asset_id"]:
                status, basis, drain = "selected_master", "human_selected_visual_group_master", "retained_master"
                row_record_key = record_key
                row_inventory = candidate_inventory
            else:
                status, basis, drain = "redundant", "redundant_after_human_visual_group_master_selection", "proposed_redundant"
                redundant_paths.append(row["source_path"])
                row_record_key = record_key
                row_inventory = candidate_inventory
            replacement_dispositions[row["source_path"]] = {
                "record_key": row_record_key, "inventory": row_inventory,
                "asset_id": row["asset_id"], "source_path": row["source_path"],
                "extension": row["extension"], "width_px": row["width_px"],
                "height_px": row["height_px"], "size_bytes": row["size_bytes"],
                "preservation_status": status, "preservation_basis": basis,
                "drain_state": drain, "deletion_authorized": "false",
            }
        reviewed_rows.append({
            "visual_group_id": group_id, "candidate_inventory": candidate_inventory,
            "record_key": record_key if selected is not None else "",
            "decision": decision["decision"],
            "inventory_evidence": inventory_evidence,
            "selected_asset_id": selected["asset_id"] if selected else "",
            "selected_source_path": selected["source_path"] if selected else "",
            "redundant_source_paths_json": json.dumps(redundant_paths, ensure_ascii=False),
            "noise_source_paths_json": json.dumps(noise_paths, ensure_ascii=False),
            "metadata_state": "metadata_deferred_next_pass" if selected else "not_applicable_noise",
            "note": decision["note"], "decided_by": decision["decided_by"],
            "decided_at": decision["decided_at"], "deletion_authorized": "false",
        })

    source_rows = M.read_csv(M.SOURCE_DISPOSITION)
    source_by_path = {row["source_path"]: row for row in source_rows}
    source_by_path.update(replacement_dispositions)
    final_source_rows = sorted(source_by_path.values(), key=lambda row: row["source_path"])
    final_drain_rows = [row for row in final_source_rows if row["preservation_status"] == "redundant"]

    master_rows = M.read_csv(M.MASTER_DECISIONS)
    accession_rows = M.read_csv(M.ACCESSION_CANDIDATES)
    metadata_rows = M.read_csv(M.METADATA_OUTPUT)
    for group in selected_groups:
        selected = group["selected"]
        decision = group["decision"]
        master_rows.append({
            "record_key": group["record_key"], "inventory": group["inventory"],
            "decision": "selected_master", "selected_asset_id": selected["asset_id"],
            "selected_source_path": selected["source_path"],
            "selected_width_px": selected["width_px"], "selected_height_px": selected["height_px"],
            "selected_size_bytes": selected["size_bytes"], "candidate_path_count": len(group["rows"]),
            "selection_basis": "human_selected_visual_group_master",
            "metadata_state": "metadata_deferred_next_pass",
            "accession_candidate_state": "master_selected_metadata_deferred",
            "decided_by": decision["decided_by"], "decided_at": decision["decided_at"],
            "deletion_authorized": "false",
        })
        accession_rows.append({
            "record_key": group["record_key"], "inventory": group["inventory"],
            "selected_asset_id": selected["asset_id"], "selected_master_path": selected["source_path"],
            "extension": selected["extension"], "width_px": selected["width_px"],
            "height_px": selected["height_px"], "size_bytes": selected["size_bytes"],
            "title": "", "description": "", "date_text": "", "object_type": "",
            "material": "", "dimensions": "", "collection_text": "",
            "historical_md_id": "", "metadata_state": "metadata_deferred_next_pass",
            "metadata_source": "", "metadata_source_row": "",
            "provenance_chain": (
                f"{selected['source_path']} -> {group['record_key']} -> pending accession; "
                f"inventory evidence: {group['inventory_evidence']}"
            ),
            "accession_candidate_state": "master_selected_metadata_deferred",
            "final_path": "", "final_hash": "", "deletion_authorized": "false",
        })
        metadata_rows.append({
            "record_key": group["record_key"], "inventory": group["inventory"],
            "title": "", "description": "", "date_text": "", "object_type": "",
            "material": "", "dimensions": "", "collection_text": "",
            "historical_md_id": "", "metadata_state": "metadata_deferred_next_pass",
            "preferred_source": "", "preferred_row": "", "source_count": "0",
            "source_rows_json": "[]",
        })

    stamp = M.now().replace("-", "").replace(":", "")
    backup_targets = (
        M.DATABASE, M.CORE_LOCAL_DECISIONS, M.CORE_LOCAL_DISPOSITIONS,
        M.CORE_SOURCE_DISPOSITION, M.CORE_DRAIN_LIST, M.MASTER_DECISIONS,
        M.SOURCE_DISPOSITION, M.DRAIN_LIST, M.ACCESSION_CANDIDATES,
        M.METADATA_OUTPUT, M.HOLDS, M.WORKBENCH_HOLDS, M.ORPHAN_DECISIONS,
        M.ORPHAN_DISPOSITIONS, M.RECEIPT,
    )
    backups = [value for value in (M.backup(path, stamp) for path in backup_targets) if value]

    asset_by_id = {row["asset_id"]: row for row in workbench}
    candidate_ids: dict[tuple[str, str], int] = {}
    with closing(M.connect(M.DATABASE)) as connection:
        connection.execute("BEGIN IMMEDIATE")
        fields = [str(row["name"]) for row in connection.execute("PRAGMA table_info(source_disposition_proposal)")]
        for group in selected_groups:
            record_key, inventory = group["record_key"], group["inventory"]
            connection.execute(
                """
                INSERT INTO objects(
                    record_key,md_object_id,inventory,title,description,date_text,object_type,material,
                    dimensions,places,tags,creator,md_url,collection_ids,collections,subdivision_ids,
                    subdivisions,last_updated,record_state,decision_state
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(record_key) DO UPDATE SET record_state='local_tfa_review'
                """,
                (record_key,"",inventory,"","","","","","","","","","","","","","","",
                 "local_tfa_review","unresolved"),
            )
            for row in group["rows"]:
                cid = M.candidate_id_for(connection, record_key, asset_by_id[row["asset_id"]])
                candidate_ids[(record_key, row["source_path"])] = cid
            selected_id = candidate_ids[(record_key, group["selected"]["source_path"])]
            decision = group["decision"]
            connection.execute(
                """
                INSERT INTO local_object_decisions(
                    record_key,outcome,selected_candidate_id,matched_md_object_id,title,description,
                    date_text,object_type,material,dimensions,note,decided_by,decided_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(record_key) DO UPDATE SET
                    outcome=excluded.outcome,selected_candidate_id=excluded.selected_candidate_id,
                    matched_md_object_id=NULL,note=excluded.note,decided_by=excluded.decided_by,
                    decided_at=excluded.decided_at
                """,
                (record_key,"new_local_object",selected_id,None,"","","","","","",
                 "Human-selected visual object; metadata and final identifier deferred. " + group["inventory_evidence"],
                 decision["decided_by"],decision["decided_at"]),
            )
        for row in replacement_dispositions.values():
            existing = connection.execute(
                "SELECT * FROM source_disposition_proposal WHERE source_path=?", (row["source_path"],)
            ).fetchone()
            core = M.core_disposition(row, existing)
            connection.execute(
                "INSERT OR REPLACE INTO source_disposition_proposal VALUES(" + ",".join("?" for _ in fields) + ")",
                [core[field] for field in fields],
            )
            connection.execute("DELETE FROM drain_list_proposal WHERE source_path=?", (row["source_path"],))
            if row["preservation_status"] == "redundant":
                connection.execute(
                    "INSERT INTO drain_list_proposal VALUES(" + ",".join("?" for _ in fields) + ")",
                    [core[field] for field in fields],
                )
        technical_count = connection.execute("SELECT COUNT(*) FROM local_candidate_technical").fetchone()[0]
        connection.execute(
            "INSERT OR REPLACE INTO meta(key,value) VALUES('tfa_local_candidate_technical_count',?)",
            (str(technical_count),),
        )
        connection.commit()

    with closing(M.connect(M.DATABASE, read_only=True)) as connection:
        quick = connection.execute("PRAGMA quick_check").fetchone()[0]
        foreign = connection.execute("PRAGMA foreign_key_check").fetchall()
        duplicate_selected = connection.execute(
            """SELECT COUNT(*) FROM (SELECT selected_candidate_id,COUNT(*) n FROM local_object_decisions
            WHERE outcome='new_local_object' GROUP BY selected_candidate_id HAVING n>1)"""
        ).fetchone()[0]
        local_decisions = [dict(row) for row in connection.execute(
            """SELECT d.*,o.inventory,COALESCE(c.source_path,'') selected_source_path
            FROM local_object_decisions d JOIN objects o ON o.record_key=d.record_key
            LEFT JOIN candidates c ON c.candidate_id=d.selected_candidate_id
            ORDER BY o.inventory COLLATE NOCASE"""
        )]
        local_dispositions = []
        for row in final_source_rows:
            if not row["record_key"].startswith("local:"):
                continue
            candidate = connection.execute(
                "SELECT candidate_id FROM candidates WHERE record_key=? AND source_path=?",
                (row["record_key"], row["source_path"]),
            ).fetchone()
            local_dispositions.append({
                "record_key": row["record_key"], "inventory": row["inventory"],
                "outcome": "new_local_object", "matched_md_object_id": "",
                "candidate_id": candidate[0] if candidate else "", "source_path": row["source_path"],
                "extension": "." + row["extension"], "size_bytes": row["size_bytes"],
                "disposition": row["preservation_status"],
                "disposition_basis": row["preservation_basis"], "deletion_authorized": "false",
            })
        source_core = [dict(row) for row in connection.execute(
            "SELECT * FROM source_disposition_proposal ORDER BY source_path"
        )]
        drain_core = [dict(row) for row in connection.execute(
            "SELECT * FROM drain_list_proposal ORDER BY source_path"
        )]
        catalogue_local_objects = connection.execute(
            "SELECT COUNT(*) FROM objects WHERE record_state='local_tfa_review'"
        ).fetchone()[0]
        catalogue_local_candidates = connection.execute(
            "SELECT COUNT(*) FROM local_candidate_technical"
        ).fetchone()[0]
        catalogue_drain_rows = connection.execute("SELECT COUNT(*) FROM drain_list_proposal").fetchone()[0]
    if quick != "ok" or foreign or duplicate_selected:
        raise M.ReconciliationError(
            f"post-consolidation validation failed: quick={quick} foreign={len(foreign)} duplicate={duplicate_selected}"
        )

    final_holds = [row for row in M.read_csv(M.HOLDS) if row["hold_reason"] != "cross_inventory_visual_collision"]
    empty: list[dict[str, Any]] = []
    M.atomic_csv(M.MASTER_DECISIONS, M.MASTER_FIELDS, sorted(master_rows, key=lambda row: row["inventory"]))
    M.atomic_csv(M.SOURCE_DISPOSITION, M.SOURCE_FIELDS, final_source_rows)
    M.atomic_csv(M.DRAIN_LIST, M.SOURCE_FIELDS, final_drain_rows)
    M.atomic_csv(M.ACCESSION_CANDIDATES, M.ACCESSION_FIELDS, sorted(accession_rows, key=lambda row: row["inventory"]))
    M.atomic_csv(M.METADATA_OUTPUT, M.METADATA_FIELDS, sorted(metadata_rows, key=lambda row: row["inventory"]))
    M.atomic_csv(M.HOLDS, M.HOLD_FIELDS, final_holds)
    M.atomic_csv(M.WORKBENCH_HOLDS, M.WORKBENCH_FIELDS, empty)
    M.atomic_csv(M.ORPHAN_DECISIONS, M.ORPHAN_DECISION_FIELDS if hasattr(M, "ORPHAN_DECISION_FIELDS") else (
        "record_key","inventory","decision","selected_asset_id","selected_visual_group_id",
        "selected_source_path","note","decided_by","decided_at","deletion_authorized"), empty)
    M.atomic_csv(M.ORPHAN_DISPOSITIONS, (
        "record_key","inventory","visual_group_id","asset_id","source_path","extension",
        "size_bytes","preservation_status","preservation_basis","drain_state","deletion_authorized"), empty)
    M.atomic_csv(REVIEWED, REVIEWED_FIELDS, reviewed_rows)
    M.atomic_csv(M.CORE_LOCAL_DECISIONS, (
        "record_key","outcome","selected_candidate_id","matched_md_object_id","title","description",
        "date_text","object_type","material","dimensions","note","decided_by","decided_at",
        "inventory","selected_source_path"), local_decisions)
    M.atomic_csv(M.CORE_LOCAL_DISPOSITIONS, (
        "record_key","inventory","outcome","matched_md_object_id","candidate_id","source_path",
        "extension","size_bytes","disposition","disposition_basis","deletion_authorized"), local_dispositions)
    M.atomic_csv(M.CORE_SOURCE_DISPOSITION, source_core[0].keys(), source_core)
    M.atomic_csv(M.CORE_DRAIN_LIST, drain_core[0].keys(), drain_core)

    receipt = json.loads(M.RECEIPT.read_text(encoding="utf-8"))
    receipt.update({
        "applied_at": M.now(), "local_objects_with_selected_master": len(master_rows),
        "local_objects_metadata_deferred": sum(row["metadata_state"] == "metadata_deferred_next_pass" for row in master_rows),
        "local_redundant_paths": sum(row["record_key"].startswith("local:") and row["preservation_status"] == "redundant" for row in final_source_rows),
        "hold_rows": len(final_holds), "hold_reason_counts": dict(sorted(Counter(row["hold_reason"] for row in final_holds).items())),
        "drain_rows_in_scope": len(final_drain_rows), "workbench_candidate_paths": 0,
        "workbench_visual_collision_components": 0, "workbench_visual_groups": 0,
        "catalogue_local_objects": catalogue_local_objects,
        "catalogue_local_candidates": catalogue_local_candidates,
        "catalogue_drain_rows": catalogue_drain_rows,
        "sqlite_quick_check": quick, "sqlite_foreign_key_violations": len(foreign),
    })
    receipt["outputs"]["reviewed_visual_decisions"] = str(REVIEWED)
    M.atomic_json(M.RECEIPT, receipt)
    consolidation = {
        "format": "iss-tfa-visual-review-consolidation-v1", "result": "ok",
        "applied_at": receipt["applied_at"], "selected_visual_objects": len(selected_groups),
        "noise_visual_objects": 1, "reviewed_candidate_paths": len(workbench),
        "redundant_paths_added": sum(row["preservation_status"] == "redundant" for row in replacement_dispositions.values()),
        "temporary_candidate_identifiers": True, "metadata_deferred": len(selected_groups),
        "masters_copied": False, "final_hashes_generated": False,
        "source_read_only": True, "source_files_modified": False,
        "deletion_authorized": False, "sqlite_quick_check": quick,
        "sqlite_foreign_key_violations": len(foreign), "backups": backups,
        "reviewed_decisions": str(REVIEWED), "reference_receipt": str(M.RECEIPT),
        "source_mount": mount,
    }
    M.atomic_json(CONSOLIDATION_RECEIPT, consolidation)
    print(json.dumps(consolidation, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
