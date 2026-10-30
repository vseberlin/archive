#!/usr/bin/env python3
"""Project the remaining TFA reference-photo holds into the existing workbench.

The projection is deliberately non-decisional: each decodable held visual group
becomes a human review candidate, while an already selected MD/local Master is
shown only as comparison context.  No source file, archival Master decision, or
SQLite row is changed.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import importlib.util
import json
import os
import re
import shutil
import sqlite3
import subprocess
from collections import Counter, defaultdict
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable


VERSION = "1.0.0"
SOURCE_ROOT = Path("/media/vladimir/Elements")
WORK_ROOT = Path("/mnt/volume/iss-archive-work/tfa")
REVIEW_ROOT = WORK_ROOT / "work/reconciliation/unit-reconciliation"
DATABASE = REVIEW_ROOT / "reconciliation.sqlite"
HOLDS = REVIEW_ROOT / "reference-photo-holds.csv"
WORKBENCH = REVIEW_ROOT / "reference-photo-workbench-holds.csv"
CONTEXT = REVIEW_ROOT / "reference-photo-hold-context.csv"
RECEIPT = REVIEW_ROOT / "reference-photo-hold-workbench-preparation.json"
ORPHAN_DECISIONS = REVIEW_ROOT / "orphan-object-decisions.csv"
ORPHAN_DISPOSITIONS = REVIEW_ROOT / "orphan-source-disposition-proposal.csv"
INSTITUTION_VISUAL = REVIEW_ROOT / "institution-md-visual-references.csv"
PRIOR_ORPHAN_DECISIONS = (
    REVIEW_ROOT / "orphan-object-decisions.pre-reference-photo-20260819T160324Z.csv"
)
VISUAL_HELPER = Path(__file__).with_name("tfa_reference_photo_autoselect.py")

WORKBENCH_FIELDS = (
    "normalized_inventory", "inventory_members", "visual_group_id", "asset_id",
    "source_path", "filename", "extension", "size_bytes", "mtime_ns", "width_px",
    "height_px", "ahash", "dhash", "phash", "exact_visual_group_tiff_count",
    "classification_basis", "review_state", "deletion_authorized",
)
CONTEXT_FIELDS = (
    "context_id", "visual_group_id", "normalized_inventory", "hold_reason",
    "context_role", "context_record_key", "context_label", "asset_id", "source_path",
    "image_url", "filename", "extension", "size_bytes", "mtime_ns", "width_px",
    "height_px", "deletion_authorized",
)
ORPHAN_DECISION_FIELDS = (
    "record_key", "inventory", "decision", "selected_asset_id", "selected_visual_group_id",
    "selected_source_path", "note", "decided_by", "decided_at", "deletion_authorized",
)
ORPHAN_DISPOSITION_FIELDS = (
    "record_key", "inventory", "visual_group_id", "asset_id", "source_path", "extension",
    "size_bytes", "preservation_status", "preservation_basis", "drain_state",
    "deletion_authorized",
)


class PreparationError(RuntimeError):
    pass


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(1024 * 1024):
            digest.update(block)
    return "sha256:" + digest.hexdigest()


def assert_work_path(path: Path) -> None:
    try:
        path.parent.resolve(strict=True).relative_to(WORK_ROOT.resolve(strict=True))
    except (OSError, ValueError) as error:
        raise PreparationError(f"write path escaped the TFA work root: {path}") from error


def atomic_csv(path: Path, fields: Iterable[str], rows: Iterable[dict[str, Any]]) -> None:
    assert_work_path(path)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        with temporary.open("x", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(
                handle, fieldnames=list(fields), quoting=csv.QUOTE_ALL, extrasaction="ignore"
            )
            writer.writeheader()
            writer.writerows(rows)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    assert_work_path(path)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def backup(path: Path, stamp: str) -> str:
    if not path.exists():
        return ""
    assert_work_path(path)
    target = path.with_name(f"{path.stem}.pre-hold-workbench-{stamp}{path.suffix}")
    if target.exists():
        raise PreparationError(f"backup already exists: {target}")
    shutil.copy2(path, target)
    return str(target)


def source_mount() -> dict[str, Any]:
    completed = subprocess.run(
        ["findmnt", "-J", "-T", str(SOURCE_ROOT), "-o", "TARGET,SOURCE,FSTYPE,OPTIONS"],
        check=True, capture_output=True, text=True,
    )
    values = json.loads(completed.stdout).get("filesystems") or []
    if len(values) != 1:
        raise PreparationError("Elements did not resolve to one mounted filesystem")
    mount = values[0]
    options = set(str(mount.get("options", "")).split(","))
    if mount.get("source") != "/dev/sdf1" or mount.get("fstype") != "ntfs3" or "ro" not in options:
        raise PreparationError(f"Elements read-only guard failed: {mount}")
    return mount


def visual_group(row: dict[str, Any]) -> str:
    signature = f"{row['ahash']}:{row['dhash']}:{row['phash']}"
    return "visual-" + hashlib.sha256(signature.encode()).hexdigest()[:16]


def import_visual_helper() -> Any:
    spec = importlib.util.spec_from_file_location("tfa_reference_photo_autoselect", VISUAL_HELPER)
    if spec is None or spec.loader is None:
        raise PreparationError("reference-photo visual helper could not be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def indexed_context(
    visual_group_id: str,
    inventory: str,
    hold_reason: str,
    role: str,
    record_key: str,
    label: str,
    asset: dict[str, Any],
) -> dict[str, Any]:
    token = f"{visual_group_id}|{role}|{asset['source_path']}"
    return {
        "context_id": "CTX-" + hashlib.sha256(token.encode()).hexdigest()[:20].upper(),
        "visual_group_id": visual_group_id,
        "normalized_inventory": inventory,
        "hold_reason": hold_reason,
        "context_role": role,
        "context_record_key": record_key,
        "context_label": label,
        "asset_id": asset["asset_id"],
        "source_path": asset["source_path"],
        "image_url": "",
        "filename": asset["filename"],
        "extension": asset["extension"],
        "size_bytes": asset["size_bytes"],
        "mtime_ns": asset["mtime_ns"],
        "width_px": asset["width_px"],
        "height_px": asset["height_px"],
        "deletion_authorized": "false",
    }


def remote_context(
    visual_group_id: str, inventory: str, hold_reason: str, row: dict[str, str]
) -> dict[str, Any]:
    image_url = row["image_url"]
    token = f"{visual_group_id}|institution_md_display|{image_url}"
    return {
        "context_id": "CTX-" + hashlib.sha256(token.encode()).hexdigest()[:20].upper(),
        "visual_group_id": visual_group_id,
        "normalized_inventory": inventory,
        "hold_reason": hold_reason,
        "context_role": "institution_md_display",
        "context_record_key": "md:" + row["md_object_id"],
        "context_label": (
            f"Institution MD {row['md_object_id']} · {row['md_inventory']} · display image only"
        ),
        "asset_id": "md-display:" + row["md_object_id"],
        "source_path": "",
        "image_url": image_url,
        "filename": Path(image_url).name,
        "extension": Path(image_url).suffix.casefold().lstrip("."),
        "size_bytes": row["download_bytes"],
        "mtime_ns": "0",
        "width_px": row["image_width_px"],
        "height_px": row["image_height_px"],
        "deletion_authorized": "false",
    }


def base_inventory(value: str) -> str:
    match = re.match(r"(?i)^(TFA-\d+(?:-\d+)?)", value.strip())
    return match.group(1).upper() if match else ""


def build() -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    mount = source_mount()
    holds = read_csv(HOLDS)
    if len(holds) != 44 or any(row.get("deletion_authorized") != "false" for row in holds):
        raise PreparationError(f"remaining reference-photo hold scope drifted: {len(holds)} rows")

    helper = import_visual_helper()
    indexed, _folder, errors = helper.load_visual_assets()
    by_asset = {str(row["asset_id"]): row for row in indexed + errors}
    by_path = {str(row["source_path"]): row for row in indexed}
    by_filename: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    exact_counts: Counter[tuple[str, str, str]] = Counter()
    for row in indexed:
        if str(row["extension"]).casefold() in {"tif", "tiff"}:
            by_filename[str(row["filename"]).casefold()].append(row)
            exact_counts[(str(row["ahash"]), str(row["dhash"]), str(row["phash"]))] += 1

    workbench_rows: list[dict[str, Any]] = []
    substitutions = []
    for hold in holds:
        original = by_asset.get(hold["asset_id"])
        if original is None:
            raise PreparationError(f"held visual asset disappeared from the index: {hold['asset_id']}")
        asset = original
        if asset.get("status") != "indexed":
            replacements = [
                row for row in by_filename.get(hold["filename"].casefold(), [])
                if int(row["size_bytes"]) == int(hold["size_bytes"])
            ]
            if replacements:
                asset = sorted(
                    replacements,
                    key=lambda row: (
                        -(int(row["width_px"]) * int(row["height_px"])),
                        -int(row["size_bytes"]), str(row["source_path"]),
                    ),
                )[0]
                substitutions.append({
                    "held_path": hold["source_path"],
                    "healthy_comparison_path": asset["source_path"],
                    "filename": hold["filename"],
                })
        if asset.get("status") == "indexed":
            group_id = visual_group(asset)
            signature = (str(asset["ahash"]), str(asset["dhash"]), str(asset["phash"]))
            exact_count = exact_counts[signature]
            basis = hold["hold_reason"]
            if asset["asset_id"] != original["asset_id"]:
                basis += "; healthy exact-filename same-size TIFF used for visual review"
        else:
            group_id = "error-" + str(asset["asset_id"])
            exact_count = 0
            basis = hold["hold_reason"] + "; no decodable visual survives"
        workbench_rows.append({
            "normalized_inventory": hold["inventory"],
            "inventory_members": hold["inventory_members"],
            "visual_group_id": group_id,
            "asset_id": asset["asset_id"],
            "source_path": asset["source_path"],
            "filename": asset["filename"],
            "extension": asset["extension"],
            "size_bytes": asset["size_bytes"],
            "mtime_ns": asset["mtime_ns"],
            "width_px": asset["width_px"],
            "height_px": asset["height_px"],
            "ahash": asset["ahash"],
            "dhash": asset["dhash"],
            "phash": asset["phash"],
            "exact_visual_group_tiff_count": exact_count,
            "classification_basis": basis,
            "review_state": "unresolved_hold",
            "deletion_authorized": "false",
        })

    group_count = len({row["visual_group_id"] for row in workbench_rows})
    if group_count != len(workbench_rows):
        raise PreparationError("held paths unexpectedly collapsed across visual object boundaries")

    prior = {
        row["inventory"]: row for row in read_csv(PRIOR_ORPHAN_DECISIONS)
        if row.get("decision") == "selected_master"
    }
    institution = [row for row in read_csv(INSTITUTION_VISUAL) if row.get("status") == "indexed"]
    institution_by_base: defaultdict[str, list[dict[str, str]]] = defaultdict(list)
    for row in institution:
        institution_by_base[base_inventory(row.get("md_inventory", ""))].append(row)

    contexts: list[dict[str, Any]] = []
    with closing(sqlite3.connect(f"file:{DATABASE}?mode=ro", uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        for candidate in workbench_rows:
            inv = str(candidate["normalized_inventory"])
            reason = str(candidate["classification_basis"]).split(";", 1)[0]
            group_id = str(candidate["visual_group_id"])
            context: dict[str, Any] | None = None
            if reason == "current_md_inventory_identity_without_confirming_visual_match":
                md = connection.execute(
                    """
                    SELECT o.record_key,o.md_object_id,o.inventory,c.source_path
                    FROM objects o
                    JOIN reconciliation_decisions d ON d.record_key=o.record_key
                    JOIN candidates c ON c.candidate_id=d.selected_candidate_id
                    WHERE upper(o.inventory)=upper(?)
                      AND o.record_state='current_museum_digital'
                      AND d.decision_state='selected_master'
                    """,
                    (inv,),
                ).fetchone()
                if md is not None:
                    asset = by_path.get(str(md["source_path"]))
                    if asset is None:
                        raise PreparationError(f"selected MD comparison Master is not indexed: {md['source_path']}")
                    context = indexed_context(
                        group_id, inv, reason, "selected_md_master", str(md["record_key"]),
                        f"Selected MD Master · md:{md['md_object_id']} · {md['inventory']}", asset,
                    )
                else:
                    matches = institution_by_base.get(inv, [])
                    if len(matches) != 1:
                        raise PreparationError(
                            f"institution MD comparison is not unique for {inv}: {len(matches)}"
                        )
                    context = remote_context(group_id, inv, reason, matches[0])
            elif reason == "prior_human_selection_not_in_complete_visual_group":
                decision = prior.get(inv)
                if decision is None:
                    raise PreparationError(f"prior human Master comparison is missing for {inv}")
                asset = by_path.get(decision["selected_source_path"])
                if asset is None:
                    raise PreparationError(
                        f"prior human Master comparison is not indexed: {decision['selected_source_path']}"
                    )
                context = indexed_context(
                    group_id, inv, reason, "prior_human_selected_master",
                    decision["record_key"], "Earlier human-selected Master · comparison only", asset,
                )
            elif reason == "additional_visual_group_requires_separate_object_identity":
                local = connection.execute(
                    """
                    SELECT d.record_key,c.source_path
                    FROM local_object_decisions d
                    JOIN candidates c ON c.candidate_id=d.selected_candidate_id
                    WHERE d.record_key=? AND d.outcome='new_local_object'
                    """,
                    ("local:" + inv,),
                ).fetchone()
                if local is None:
                    raise PreparationError(f"current local selected Master comparison is missing for {inv}")
                asset = by_path.get(str(local["source_path"]))
                if asset is None:
                    raise PreparationError(
                        f"current local Master comparison is not indexed: {local['source_path']}"
                    )
                context = indexed_context(
                    group_id, inv, reason, "selected_local_master", str(local["record_key"]),
                    "Already selected local Master · comparison only", asset,
                )
            elif reason != "contains_visual_decode_error_tiff":
                raise PreparationError(f"unsupported hold reason: {reason}")
            if context is not None:
                contexts.append(context)

        quick = connection.execute("PRAGMA quick_check").fetchone()[0]
        foreign = len(connection.execute("PRAGMA foreign_key_check").fetchall())
    if quick != "ok" or foreign:
        raise PreparationError(f"SQLite validation failed: quick={quick}, foreign={foreign}")

    summary = {
        "result": "ok",
        "format": "iss-tfa-reference-photo-hold-workbench-v1",
        "version": VERSION,
        "mode": "dry-run",
        "prepared_at": now(),
        "source_mount": mount,
        "source_read_only": True,
        "source_files_modified": False,
        "deletion_authorized": False,
        "automatic_master_selection": False,
        "sqlite_modified": False,
        "sqlite_quick_check": quick,
        "sqlite_foreign_key_violations": foreign,
        "hold_rows": len(holds),
        "hold_reason_counts": dict(sorted(Counter(row["hold_reason"] for row in holds).items())),
        "workbench_candidate_paths": len(workbench_rows),
        "workbench_visual_groups": group_count,
        "decodable_candidate_paths": sum(bool(row["ahash"]) for row in workbench_rows),
        "unviewable_candidate_paths": sum(not bool(row["ahash"]) for row in workbench_rows),
        "healthy_exact_filename_substitutions": len(substitutions),
        "substitutions": substitutions,
        "comparison_context_rows": len(contexts),
        "comparison_context_role_counts": dict(
            sorted(Counter(row["context_role"] for row in contexts).items())
        ),
        "input_holds": str(HOLDS),
        "input_holds_sha256": sha256_file(HOLDS),
        "outputs": {"workbench": str(WORKBENCH), "context": str(CONTEXT)},
        "backups": [],
    }
    return workbench_rows, contexts, summary


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    arguments = parser.parse_args()
    workbench_rows, contexts, summary = build()
    if not arguments.apply:
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=2))
        return 0

    if read_csv(ORPHAN_DECISIONS) or read_csv(ORPHAN_DISPOSITIONS):
        raise PreparationError("existing orphan review decisions/dispositions are not empty")
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    backups = [
        value for value in (
            backup(WORKBENCH, stamp), backup(CONTEXT, stamp), backup(RECEIPT, stamp),
            backup(ORPHAN_DECISIONS, stamp), backup(ORPHAN_DISPOSITIONS, stamp),
        ) if value
    ]
    atomic_csv(WORKBENCH, WORKBENCH_FIELDS, workbench_rows)
    atomic_csv(CONTEXT, CONTEXT_FIELDS, contexts)
    atomic_csv(ORPHAN_DECISIONS, ORPHAN_DECISION_FIELDS, [])
    atomic_csv(ORPHAN_DISPOSITIONS, ORPHAN_DISPOSITION_FIELDS, [])
    summary.update({
        "mode": "apply",
        "applied_at": now(),
        "backups": backups,
        "output_sha256": {
            "workbench": sha256_file(WORKBENCH),
            "context": sha256_file(CONTEXT),
        },
    })
    atomic_json(RECEIPT, summary)
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
