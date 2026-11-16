#!/usr/bin/env python3
"""Resolve the unambiguous TFA reference-photo recovery holds.

The tool uses the existing complete visual index and never writes to source
media.  A decodable TIFF with the same filename is preferred.  Three objects
whose TIFFs have no decodable copy receive the best surviving 800 px JPEG as
an explicitly labelled surrogate Master.  Ambiguous filenames, visually
different same-inventory images, and objects with no surviving image remain
held.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import importlib.util
import json
import shutil
import sqlite3
import subprocess
from collections import Counter
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

ROOT = M.REVIEW_ROOT
RECEIPT = ROOT / "reference-photo-hold-recovery.json"
ACTOR = "codex:user-directed-damaged-tiff-recovery"
AMBIGUOUS_DAMAGE = {"TFA-549016-54", "TFA-549984-54", "TFA-5511116"}
JPEG_SURROGATES = {"TFA-5510941", "TFA-5510960", "TFA-5510967"}
PUNCTUATION_FIXES = {
    "TFA-5510910": "TFA_FOT-5510910.tif",
    "TFA-5510911": "TFA_FOT-5510911.tif",
}
ALREADY_ROUTED_PUNCTUATION = {"TFA-5612846", "TFA-5612847"}
FIXED_NONSTANDARD_FILENAMES = {
    "TFA.FOT-5612846.tif", "TFA.FOT-5612847.tif", *PUNCTUATION_FIXES.values(),
}
PORTRAIT_NAME = "TFA-FOT-Portrait-Frau-nach5817530.tif"


def now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def read_rows(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        return list(reader.fieldnames or ()), list(reader)


def service_inactive() -> None:
    result = subprocess.run(
        ["systemctl", "--user", "show", "iss-archive-tfa-workbench.service", "-p", "ActiveState", "--value"],
        check=True, capture_output=True, text=True,
    )
    if result.stdout.strip() not in {"inactive", "failed"}:
        raise M.ReconciliationError("stop port 8768 before applying recovery decisions")


def visual_assets() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    indexed, _folder, errors = M.load_visual_assets()
    return indexed, errors


def exact_name_tiffs(indexed: list[dict[str, Any]], filename: str) -> list[dict[str, Any]]:
    return sorted(
        [r for r in indexed if r["filename"].casefold() == filename.casefold() and r["extension"] in M.TIFF_EXTENSIONS],
        key=lambda r: (int(r["width_px"]) * int(r["height_px"]), int(r["size_bytes"]), r["source_path"]),
        reverse=True,
    )


def same_visual(indexed: list[dict[str, Any]], selected: dict[str, Any]) -> list[dict[str, Any]]:
    signature = M.visual_signature(selected)
    return sorted(
        [r for r in indexed if M.visual_signature(r) == signature],
        key=lambda r: r["source_path"],
    )


def portrait_inventory(selected: dict[str, Any]) -> str:
    token = hashlib.sha256(":".join(M.visual_signature(selected)).encode()).hexdigest()[:16].upper()
    return "TFA-VISUAL-" + token


def build_plan() -> dict[str, Any]:
    mount = M.source_mount()
    indexed, errors = visual_assets()
    holds = M.read_csv(M.HOLDS)
    damage_rows = [r for r in holds if r["hold_reason"] == "contains_visual_decode_error_tiff" and r["error"]]
    if len(damage_rows) != 47:
        raise M.ReconciliationError(f"damaged TIFF scope drifted: {len(damage_rows)}")

    portrait_hold = next(
        (r for r in holds if r["filename"].casefold() == PORTRAIT_NAME.casefold()), None
    )
    if portrait_hold is None:
        raise M.ReconciliationError("portrait hold disappeared")
    with closing(M.connect(M.DATABASE, read_only=True)) as db:
        md_by_inventory = {
            str(r["inventory"]).upper(): dict(r)
            for r in db.execute(
                """SELECT o.record_key,o.inventory,d.selected_candidate_id,c.source_path
                   FROM objects o JOIN reconciliation_decisions d ON d.record_key=o.record_key
                   LEFT JOIN candidates c ON c.candidate_id=d.selected_candidate_id
                   WHERE o.record_key LIKE 'md:%'"""
            )
        }

    recoveries: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    for damaged in damage_rows:
        inv = damaged["inventory"]
        if inv in AMBIGUOUS_DAMAGE:
            unresolved.append({"inventory": inv, "filename": damaged["filename"], "reason": "ambiguous_identity_or_no_surviving_image"})
            continue
        healthy = exact_name_tiffs(indexed, damaged["filename"])
        if not healthy and inv == "TFA-5612478":
            healthy = exact_name_tiffs(indexed, "TFA-FOT-5612478..tif")
        if healthy:
            selected = healthy[0]
            kind = "existing_md_tiff_master" if inv in md_by_inventory else "decodable_tiff_recovery_master"
        elif inv in JPEG_SURROGATES:
            candidates = [
                r for r in indexed
                if inv.removeprefix("TFA-") in r["filename"] and r["extension"] in {"jpg", "jpeg"}
            ]
            if not candidates:
                raise M.ReconciliationError(f"surrogate JPEG disappeared: {inv}")
            selected = max(candidates, key=lambda r: (r["width_px"] * r["height_px"], r["size_bytes"], r["source_path"]))
            kind = "jpeg_surrogate_master"
        else:
            raise M.ReconciliationError(f"unexpected unresolved damaged TIFF: {inv}")
        if inv in md_by_inventory:
            current_path = md_by_inventory[inv]["source_path"]
            current = next((r for r in indexed if r["source_path"] == current_path), None)
            if current is None:
                raise M.ReconciliationError(f"current MD Master is absent from the visual index: {inv}")
            selected = current
        recoveries.append({
            "inventory": inv, "record_key": md_by_inventory.get(inv, {}).get("record_key", "local:" + inv),
            "damaged": damaged, "selected": selected, "related": same_visual(indexed, selected),
            "kind": kind, "is_md": inv in md_by_inventory,
        })

    for inv, filename in PUNCTUATION_FIXES.items():
        selected = next((r for r in indexed if r["filename"] == filename and r["source_path"].startswith(str(M.REFERENCE_ROOT))), None)
        if selected is None:
            raise M.ReconciliationError(f"punctuation-routed TIFF disappeared: {filename}")
        recoveries.append({
            "inventory": inv, "record_key": "local:" + inv, "damaged": None,
            "selected": selected, "related": same_visual(indexed, selected),
            "kind": "normalized_inventory_filename_master", "is_md": False,
        })

    portrait = exact_name_tiffs(indexed, PORTRAIT_NAME)
    if not portrait:
        raise M.ReconciliationError("healthy portrait recovery TIFF disappeared")
    portrait_selected = portrait[0]
    portrait_inv = portrait_inventory(portrait_selected)
    recoveries.append({
        "inventory": portrait_inv, "record_key": "local:" + portrait_inv,
        "damaged": portrait_hold, "selected": portrait_selected,
        "related": same_visual(indexed, portrait_selected),
        "kind": "unnumbered_visual_object_tiff_master", "is_md": False,
    })

    local = [r for r in recoveries if not r["is_md"]]
    if len(recoveries) != 47 or len(local) != 32:
        raise M.ReconciliationError(f"recovery plan drifted: all={len(recoveries)} local={len(local)}")
    if len({r["inventory"] for r in local}) != len(local):
        raise M.ReconciliationError("local recovery inventories are not unique")
    for recovery in recoveries:
        selected = recovery["selected"]
        stat = Path(selected["source_path"]).stat()
        if stat.st_size != int(selected["size_bytes"]) or stat.st_mtime_ns != int(selected["mtime_ns"]):
            raise M.ReconciliationError(f"selected recovery source drifted: {selected['source_path']}")
    return {"mount": mount, "holds": holds, "recoveries": recoveries, "unresolved": unresolved, "prepared_at": now()}


def csv_upsert(path: Path, fields: list[str] | tuple[str, ...], additions: list[dict[str, Any]], keys: tuple[str, ...]) -> list[dict[str, Any]]:
    rows = M.read_csv(path) if path.exists() else []
    merged = {tuple(str(r.get(k, "")) for k in keys): r for r in rows}
    for row in additions:
        merged[tuple(str(row.get(k, "")) for k in keys)] = row
    result = sorted(merged.values(), key=lambda r: tuple(str(r.get(k, "")) for k in keys))
    M.atomic_csv(path, fields, result)
    return result


def disposition(asset: dict[str, Any], record_key: str, inventory: str, selected: bool, basis: str) -> dict[str, Any]:
    return {
        "record_key": record_key, "inventory": inventory, "asset_id": asset["asset_id"],
        "source_path": asset["source_path"], "extension": asset["extension"],
        "width_px": asset["width_px"], "height_px": asset["height_px"], "size_bytes": asset["size_bytes"],
        "preservation_status": "selected_master" if selected else "redundant",
        "preservation_basis": basis,
        "drain_state": "retained_master" if selected else "proposed_redundant",
        "deletion_authorized": "false",
    }


def apply(plan: dict[str, Any]) -> dict[str, Any]:
    service_inactive()
    M.source_mount()
    stamp = plan["prepared_at"].replace("-", "").replace(":", "")
    targets = (
        M.DATABASE, M.MASTER_DECISIONS, M.SOURCE_DISPOSITION, M.DRAIN_LIST,
        M.ACCESSION_CANDIDATES, M.HOLDS, M.CORE_LOCAL_DECISIONS,
        M.CORE_LOCAL_DISPOSITIONS, M.CORE_SOURCE_DISPOSITION, M.CORE_DRAIN_LIST, RECEIPT,
    )
    backups = [x for x in (M.backup(p, stamp) for p in targets) if x]

    selected_ids: dict[str, int] = {}
    all_dispositions: list[dict[str, Any]] = []
    with closing(M.connect(M.DATABASE)) as db:
        db.execute("BEGIN IMMEDIATE")
        source_fields = [str(r["name"]) for r in db.execute("PRAGMA table_info(source_disposition_proposal)")]
        for recovery in plan["recoveries"]:
            inv, record_key, selected = recovery["inventory"], recovery["record_key"], recovery["selected"]
            if recovery["is_md"]:
                selected_ids[inv] = int(db.execute(
                    "SELECT selected_candidate_id FROM reconciliation_decisions WHERE record_key=?", (record_key,)
                ).fetchone()[0])
            else:
                prior = db.execute("SELECT selected_candidate_id FROM local_object_decisions WHERE record_key=?", (record_key,)).fetchone()
                if prior is not None and inv not in ALREADY_ROUTED_PUNCTUATION:
                    raise M.ReconciliationError(f"local recovery would replace an existing decision: {inv}")
                db.execute(
                    """INSERT INTO objects(record_key,md_object_id,inventory,title,description,date_text,object_type,
                       material,dimensions,places,tags,creator,md_url,collection_ids,collections,subdivision_ids,
                       subdivisions,last_updated,record_state,decision_state)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(record_key) DO NOTHING""",
                    (record_key,"",inv,"Portrait einer Frau" if inv.startswith("TFA-VISUAL-") else "","","","","","",
                     "","","","","","","","","","local_tfa_review","unresolved"),
                )
                for asset in recovery["related"]:
                    cid = M.candidate_id_for(db, record_key, asset)
                    if asset["asset_id"] == selected["asset_id"]:
                        selected_ids[inv] = cid
                if inv not in selected_ids:
                    selected_ids[inv] = M.candidate_id_for(db, record_key, selected)
                if recovery["kind"] == "jpeg_surrogate_master":
                    cid = selected_ids[inv]
                    db.execute("UPDATE candidates SET proposed_role='surrogate_master_candidate',match_basis='damaged_tiff_best_surviving_derivative' WHERE candidate_id=?", (cid,))
                    db.execute("UPDATE local_candidate_technical SET accession_eligible=1,eligibility_basis='user-authorized JPEG surrogate after no decodable TIFF or higher-resolution derivative was found' WHERE candidate_id=?", (cid,))
                note = {
                    "jpeg_surrogate_master": "User-authorized best surviving JPEG surrogate; no decodable TIFF or higher-resolution derivative exists in the complete visual index.",
                    "unnumbered_visual_object_tiff_master": "Unnumbered portrait visual object; healthy high-resolution TIFF selected, metadata and final identifier deferred.",
                    "normalized_inventory_filename_master": "Inventory punctuation normalized; decodable TIFF selected as the single Master.",
                }.get(recovery["kind"], "Healthy same-filename TIFF selected after the reference-folder copy proved undecodable.")
                db.execute(
                    """INSERT INTO local_object_decisions(record_key,outcome,selected_candidate_id,matched_md_object_id,
                       title,description,date_text,object_type,material,dimensions,note,decided_by,decided_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(record_key) DO UPDATE SET outcome=excluded.outcome,
                       selected_candidate_id=excluded.selected_candidate_id,note=excluded.note,
                       decided_by=excluded.decided_by,decided_at=excluded.decided_at""",
                    (record_key,"new_local_object",selected_ids[inv],None,
                     "Portrait einer Frau" if inv.startswith("TFA-VISUAL-") else "","","","","","",note,ACTOR,plan["prepared_at"]),
                )

            seen: set[str] = set()
            for asset in recovery["related"]:
                if asset["source_path"] in seen:
                    continue
                seen.add(asset["source_path"])
                chosen = asset["asset_id"] == selected["asset_id"]
                basis = (
                    recovery["kind"] if chosen else
                    "redundant_visual_equivalent_after_single_master_selection"
                )
                row = disposition(asset, record_key, inv, chosen, basis)
                all_dispositions.append(row)
            damaged = recovery.get("damaged")
            if damaged is not None:
                bad = {
                    "asset_id": damaged["asset_id"], "source_path": damaged["source_path"],
                    "extension": damaged["extension"], "width_px": 0, "height_px": 0,
                    "size_bytes": int(damaged["size_bytes"]),
                }
                all_dispositions.append(disposition(
                    bad, record_key, inv, False,
                    "undecodable_same_filename_source_preserved_as_redundant_evidence",
                ))

        by_path: dict[str, dict[str, Any]] = {}
        rank = {"selected_master": 2, "redundant": 1}
        for row in all_dispositions:
            old = by_path.get(row["source_path"])
            if old is None or rank[row["preservation_status"]] > rank[old["preservation_status"]]:
                by_path[row["source_path"]] = row
        all_dispositions = sorted(by_path.values(), key=lambda r: r["source_path"])
        for row in all_dispositions:
            existing = db.execute("SELECT * FROM source_disposition_proposal WHERE source_path=?", (row["source_path"],)).fetchone()
            core = M.core_disposition(row, existing)
            if row["preservation_status"] == "selected_master" and row["extension"] in {"jpg", "jpeg"}:
                core.update({
                    "source_role": "surrogate_master", "preservation_status": "selected_for_accession",
                    "preservation_basis": row["preservation_basis"], "drain_state": "retain_selected_master",
                })
            db.execute("INSERT OR REPLACE INTO source_disposition_proposal VALUES(" + ",".join("?" for _ in source_fields) + ")", [core[f] for f in source_fields])
            db.execute("DELETE FROM drain_list_proposal WHERE source_path=?", (row["source_path"],))
            if row["preservation_status"] == "redundant":
                db.execute("INSERT INTO drain_list_proposal VALUES(" + ",".join("?" for _ in source_fields) + ")", [core[f] for f in source_fields])
        db.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('tfa_reference_photo_hold_recovery_receipt',?)", (str(RECEIPT),))
        db.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('tfa_local_candidate_technical_count',(SELECT COUNT(*) FROM local_candidate_technical))")
        db.commit()

    local_recoveries = [r for r in plan["recoveries"] if not r["is_md"]]
    master_add = []
    accession_add = []
    for r in local_recoveries:
        a, inv = r["selected"], r["inventory"]
        master_add.append({
            "record_key": r["record_key"], "inventory": inv, "decision": "selected_master",
            "selected_asset_id": a["asset_id"], "selected_source_path": a["source_path"],
            "selected_width_px": a["width_px"], "selected_height_px": a["height_px"],
            "selected_size_bytes": a["size_bytes"], "candidate_path_count": len(r["related"]),
            "selection_basis": r["kind"], "metadata_state": "metadata_deferred_next_pass",
            "accession_candidate_state": "ready_for_accession", "decided_by": ACTOR,
            "decided_at": plan["prepared_at"], "deletion_authorized": "false",
        })
        accession_add.append({
            "record_key": r["record_key"], "inventory": inv, "selected_asset_id": a["asset_id"],
            "selected_master_path": a["source_path"], "extension": a["extension"],
            "width_px": a["width_px"], "height_px": a["height_px"], "size_bytes": a["size_bytes"],
            "title": "Portrait einer Frau" if inv.startswith("TFA-VISUAL-") else "",
            "description": "", "date_text": "", "object_type": "", "material": "", "dimensions": "",
            "collection_text": "", "historical_md_id": "", "metadata_state": "metadata_deferred_next_pass",
            "metadata_source": "", "metadata_source_row": "", "provenance_chain":
            f"read-only source {a['source_path']} | {r['kind']}",
            "accession_candidate_state": "ready_for_accession", "final_path": "", "final_hash": "",
            "deletion_authorized": "false",
        })
    master_rows = csv_upsert(M.MASTER_DECISIONS, M.MASTER_FIELDS, master_add, ("record_key",))
    accession_rows = csv_upsert(M.ACCESSION_CANDIDATES, M.ACCESSION_FIELDS, accession_add, ("record_key",))
    source_rows = csv_upsert(M.SOURCE_DISPOSITION, M.SOURCE_FIELDS, all_dispositions, ("source_path",))
    drain_rows = [r for r in source_rows if r["preservation_status"] == "redundant"]
    M.atomic_csv(M.DRAIN_LIST, M.SOURCE_FIELDS, drain_rows)

    resolved_damage = {r["inventory"] for r in plan["recoveries"] if r.get("damaged") is not None and r["inventory"].startswith("TFA-") and not r["inventory"].startswith("TFA-VISUAL-")}
    final_holds: list[dict[str, Any]] = []
    for row in plan["holds"]:
        reason, inv = row["hold_reason"], row["inventory"]
        if reason == "nonstandard_or_ambiguous_inventory_filename":
            if row["filename"] == PORTRAIT_NAME or row["filename"] in FIXED_NONSTANDARD_FILENAMES:
                continue
        if reason == "contains_visual_decode_error_tiff" and inv in resolved_damage:
            if row["error"]:
                continue
            row = dict(row)
            row["hold_reason"] = "additional_visual_group_requires_separate_object_identity"
        final_holds.append(row)
    M.atomic_csv(M.HOLDS, M.HOLD_FIELDS, final_holds)

    with closing(M.connect(M.DATABASE, read_only=True)) as db:
        quick = db.execute("PRAGMA quick_check").fetchone()[0]
        foreign = db.execute("PRAGMA foreign_key_check").fetchall()
        duplicates = db.execute("""SELECT COUNT(*) FROM (
            SELECT selected_candidate_id,COUNT(*) n FROM local_object_decisions
            WHERE outcome='new_local_object' GROUP BY selected_candidate_id HAVING n>1)""").fetchone()[0]
        local_decisions = [dict(r) for r in db.execute("""SELECT d.*,o.inventory,COALESCE(c.source_path,'') selected_source_path
            FROM local_object_decisions d JOIN objects o ON o.record_key=d.record_key
            LEFT JOIN candidates c ON c.candidate_id=d.selected_candidate_id ORDER BY o.inventory COLLATE NOCASE""")]
        core_source = [dict(r) for r in db.execute("SELECT * FROM source_disposition_proposal ORDER BY source_path")]
        core_drain = [dict(r) for r in db.execute("SELECT * FROM drain_list_proposal ORDER BY source_path")]
    if quick != "ok" or foreign or duplicates:
        raise M.ReconciliationError(f"post-apply validation failed: quick={quick} foreign={len(foreign)} duplicates={duplicates}")
    M.atomic_csv(M.CORE_LOCAL_DECISIONS, (
        "record_key","outcome","selected_candidate_id","matched_md_object_id","title","description",
        "date_text","object_type","material","dimensions","note","decided_by","decided_at",
        "inventory","selected_source_path",
    ), local_decisions)
    local_disp = []
    with closing(M.connect(M.DATABASE, read_only=True)) as db:
        for row in source_rows:
            if not row["record_key"].startswith("local:"):
                continue
            candidate = db.execute("SELECT candidate_id FROM candidates WHERE record_key=? AND source_path=?", (row["record_key"],row["source_path"])).fetchone()
            local_disp.append({
                "record_key": row["record_key"], "inventory": row["inventory"], "outcome": "new_local_object",
                "matched_md_object_id": "", "candidate_id": candidate[0] if candidate else "",
                "source_path": row["source_path"], "extension": "." + row["extension"],
                "size_bytes": row["size_bytes"], "disposition": row["preservation_status"],
                "disposition_basis": row["preservation_basis"], "deletion_authorized": "false",
            })
    csv_upsert(M.CORE_LOCAL_DISPOSITIONS, (
        "record_key","inventory","outcome","matched_md_object_id","candidate_id","source_path",
        "extension","size_bytes","disposition","disposition_basis","deletion_authorized",
    ), local_disp, ("source_path",))
    M.atomic_csv(M.CORE_SOURCE_DISPOSITION, core_source[0].keys(), core_source)
    M.atomic_csv(M.CORE_DRAIN_LIST, core_drain[0].keys(), core_drain)

    receipt = {
        "format": "iss-tfa-reference-photo-hold-recovery-v1", "result": "ok", "applied_at": plan["prepared_at"],
        "source_mount": plan["mount"], "source_read_only": True, "source_files_modified": False,
        "deletion_authorized": False, "final_hashes_generated": False, "masters_copied": False,
        "recovered_existing_md_objects": sum(r["is_md"] for r in plan["recoveries"]),
        "new_local_tiff_master_objects": sum(not r["is_md"] and r["kind"] != "jpeg_surrogate_master" for r in plan["recoveries"]),
        "new_local_jpeg_surrogate_objects": sum(r["kind"] == "jpeg_surrogate_master" for r in plan["recoveries"]),
        "unresolved_damaged_tiffs": plan["unresolved"],
        "remaining_hold_reason_counts": dict(sorted(Counter(r["hold_reason"] for r in final_holds).items())),
        "remaining_hold_rows": len(final_holds), "local_master_decisions": len(master_rows),
        "local_accession_candidates": len(accession_rows), "reference_disposition_rows": len(source_rows),
        "reference_drain_rows": len(drain_rows), "sqlite_quick_check": quick,
        "catalogue_drain_rows": len(core_drain),
        "sqlite_foreign_key_violations": len(foreign), "backups": backups,
    }
    M.atomic_json(RECEIPT, receipt)
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    plan = build_plan()
    if args.apply:
        result = apply(plan)
    else:
        result = {
            "result": "ok", "mode": "dry-run", "source_read_only": True,
            "recoveries": len(plan["recoveries"]),
            "existing_md_objects": sum(r["is_md"] for r in plan["recoveries"]),
            "new_local_objects": sum(not r["is_md"] for r in plan["recoveries"]),
            "jpeg_surrogates": sum(r["kind"] == "jpeg_surrogate_master" for r in plan["recoveries"]),
            "unresolved": plan["unresolved"], "writes_performed": False,
        }
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
