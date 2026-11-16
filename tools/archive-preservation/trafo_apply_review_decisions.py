#!/usr/bin/env python3
"""Apply reviewed Trafo TIFF decisions to a delta finalization manifest."""

from __future__ import annotations

import argparse
import csv
import json
import os
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


FORMAT = "iss-trafo-review-decisions-v2"
CONFIRMED_FORMAT = "iss-trafo-confirmed-reconciliation-v1"
WORK_ROOT_PREFIX = "/mnt/volume/iss-archive-work/"
FINAL_FIELDNAMES = [
    "inventory",
    "md_object_id",
    "role",
    "source_path",
    "destination_path",
    "sha256",
    "size_bytes",
    "sequence",
    "selected_role",
    "md_pdf_url",
    "pdfinfo_status",
    "pdf_pages",
    "pdf_error",
    "final_action",
    "decision",
    "final_component",
    "final_role",
    "final_notes",
    "storage_root_id",
    "storage_root",
    "supersedes_storage_root_id",
    "supersedes_destination_path",
]


class DecisionError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=fieldnames,
            extrasaction="ignore",
            quoting=csv.QUOTE_ALL,
        )
        writer.writeheader()
        writer.writerows(rows)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def page_sequence(row: dict[str, str]) -> int | None:
    try:
        return int(row["sequence"])
    except (KeyError, TypeError, ValueError):
        return None


def page_component(inventory: str, decision: str, row: dict[str, str]) -> tuple[str, str, str]:
    role = row["role"]
    if role == "local_pdf":
        return (
            "retained_local_pdf",
            "provenance_access_pdf",
            "local PDF retained as provenance/access evidence for TIFF-backed record",
        )
    if role != "tiff_page":
        return ("reviewed_non_page", role, "reviewed non-page row")

    sequence = page_sequence(row)
    if decision == "confirmed":
        return (
            "md_pdf_matching_pages",
            "preservation_master_page",
            "curator confirmed all TIFF pages match the MD/PDF issue",
        )
    if inventory in {"TRO-1953-02", "TRO-1953-03"}:
        if sequence is not None and sequence <= 4:
            return (
                "md_pdf_matching_pages",
                "preservation_master_page",
                "curator confirmed pages 1-4 match the MD/PDF issue",
            )
        return (
            "separate_letter_addition",
            "related_addition_page",
            "curator marked this page as separate valuable letter material",
        )
    if inventory == "TRO-1953-26":
        if sequence is not None and sequence <= 6:
            return (
                "md_pdf_matching_pages",
                "preservation_master_page",
                "curator confirmed pages 1-6 match the MD/PDF issue",
            )
        return (
            "full_local_version_extra",
            "related_full_version_page",
            "curator marked this page as full local newspaper content outside the MD/PDF",
        )
    raise DecisionError(f"unsupported partial decision for {inventory}: {decision}")


def require_unique_decisions(rows: list[dict[str, str]]) -> dict[str, dict[str, str]]:
    by_inventory: dict[str, dict[str, str]] = {}
    duplicates: list[str] = []
    for row in rows:
        inventory = row["inventory"]
        if inventory in by_inventory:
            duplicates.append(inventory)
        by_inventory[inventory] = row
    if duplicates:
        raise DecisionError("duplicate decisions: " + ", ".join(sorted(set(duplicates))))
    return by_inventory


def parse_shared_page_maps(values: list[str] | None) -> dict[str, tuple[str, int, int]]:
    mappings: dict[str, tuple[str, int, int]] = {}
    for value in values or []:
        try:
            target, source_range = value.split("=", 1)
            source, page_range = source_range.split(":", 1)
            first_text, last_text = page_range.split("-", 1)
            first = int(first_text)
            last = int(last_text)
        except (TypeError, ValueError) as error:
            raise DecisionError(
                "shared page maps must use TARGET=SOURCE:FIRST-LAST"
            ) from error
        if not target or not source or first < 1 or last < first:
            raise DecisionError(f"invalid shared page map: {value}")
        if target in mappings:
            raise DecisionError(f"duplicate shared page target: {target}")
        mappings[target] = (source, first, last)
    return mappings


def require_args(args: argparse.Namespace, names: list[str]) -> None:
    missing = [name for name in names if getattr(args, name, None) is None]
    if missing:
        raise DecisionError("missing required arguments: " + ", ".join(missing))


def build_confirmed_reconciliation(
    args: argparse.Namespace,
    work_root: Path,
) -> dict[str, Any]:
    require_args(
        args,
        [
            "base_final_manifest",
            "resolved_manifest",
            "resolved_copy_root",
            "resolved_storage_root_id",
            "expected_fallback_inventory",
        ],
    )
    output_dir = work_root / "work" / "finalization-plan" / args.plan_id
    output_dir.mkdir(parents=True, exist_ok=False)

    base_rows = read_csv(args.base_final_manifest)
    resolved_rows = read_csv(args.resolved_manifest)
    resolved_inventories = set(args.resolved_inventory or [])
    shared_maps = parse_shared_page_maps(args.shared_page_map)
    if not resolved_inventories:
        raise DecisionError("at least one --resolved-inventory is required")
    if not shared_maps:
        raise DecisionError("at least one --shared-page-map is required")
    if resolved_inventories & set(shared_maps):
        raise DecisionError("resolved and shared-page inventories overlap")

    fallback_by_inventory: dict[str, dict[str, str]] = {}
    for row in base_rows:
        if row["role"] != "local_pdf_fallback":
            continue
        inventory = row["inventory"]
        if inventory in fallback_by_inventory:
            raise DecisionError(f"duplicate base fallback row for {inventory}")
        fallback_by_inventory[inventory] = row

    decision_targets = resolved_inventories | set(shared_maps)
    missing_fallbacks = sorted(decision_targets - set(fallback_by_inventory))
    if missing_fallbacks:
        raise DecisionError(
            "confirmed inventories without one base fallback row: "
            + ", ".join(missing_fallbacks)
        )

    resolved_by_inventory: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in resolved_rows:
        if row["inventory"] in resolved_inventories:
            resolved_by_inventory[row["inventory"]].append(row)
    missing_resolved = sorted(
        inventory
        for inventory in resolved_inventories
        if not resolved_by_inventory[inventory]
    )
    if missing_resolved:
        raise DecisionError(
            "resolved inventories without copied rows: "
            + ", ".join(missing_resolved)
        )

    shared_source_rows: dict[str, list[dict[str, str]]] = {}
    claimed_physical_rows: set[tuple[str, str]] = set()
    for target, (source, first, last) in shared_maps.items():
        rows = [
            row
            for row in base_rows
            if row["inventory"] == source
            and row["role"] == "tiff_page"
            and page_sequence(row) is not None
            and first <= int(row["sequence"]) <= last
        ]
        expected = last - first + 1
        sequences = sorted(int(row["sequence"]) for row in rows)
        if len(rows) != expected or sequences != list(range(first, last + 1)):
            raise DecisionError(
                f"{target}: expected source {source} pages {first}-{last}, "
                f"found {sequences}"
            )
        for row in rows:
            physical = (row["storage_root_id"], row["destination_path"])
            if physical in claimed_physical_rows:
                raise DecisionError(
                    f"shared page claimed twice: {row['destination_path']}"
                )
            claimed_physical_rows.add(physical)
        shared_source_rows[target] = rows

    removed_rows: list[dict[str, Any]] = []
    final_rows: list[dict[str, Any]] = []
    shared_physical = {
        (row["storage_root_id"], row["destination_path"])
        for rows in shared_source_rows.values()
        for row in rows
    }

    for row in base_rows:
        inventory = row["inventory"]
        physical = (row["storage_root_id"], row["destination_path"])
        if inventory in resolved_inventories and row["role"] == "local_pdf_fallback":
            removed_rows.append(
                {
                    **row,
                    "final_action": "suppress_base_row",
                    "decision": "confirmed_mislabeled_tiff_folder",
                    "final_component": "superseded_pdf_fallback",
                    "final_role": "superseded_access_pdf",
                    "final_notes": (
                        "base PDF fallback superseded by curator-confirmed TIFF pages"
                    ),
                }
            )
            continue
        if physical in shared_physical:
            continue
        if inventory in shared_maps and row["role"] == "local_pdf_fallback":
            final_rows.append(
                {
                    **row,
                    "role": "local_pdf",
                    "selected_role": "preservation_tiff_pages",
                    "final_action": "promote_existing_pdf",
                    "decision": "confirmed_shared_tiff_pages",
                    "final_component": "retained_local_pdf",
                    "final_role": "provenance_access_pdf",
                    "final_notes": (
                        "local PDF retained for a record whose TIFF pages are "
                        "shared with a related physical issue"
                    ),
                }
            )
            continue
        final_rows.append(row)

    resolved_applied: list[dict[str, Any]] = []
    for inventory in sorted(resolved_inventories):
        for row in sorted(
            resolved_by_inventory[inventory],
            key=lambda item: (
                item["role"] != "tiff_page",
                page_sequence(item) or 0,
                item["destination_path"],
            ),
        ):
            is_page = row["role"] == "tiff_page"
            applied = {
                **row,
                "final_action": "add_confirmed_delta_row",
                "decision": "confirmed_mislabeled_tiff_folder",
                "final_component": (
                    "md_pdf_matching_pages" if is_page else "retained_local_pdf"
                ),
                "final_role": (
                    "preservation_master_page"
                    if is_page
                    else "provenance_access_pdf"
                ),
                "final_notes": (
                    "curator confirmed every TIFF page against the local/MD PDF"
                    if is_page
                    else "local PDF retained as provenance/access evidence"
                ),
                "storage_root_id": args.resolved_storage_root_id,
                "storage_root": str(args.resolved_copy_root),
                "supersedes_storage_root_id": (
                    "full-001" if is_page else ""
                ),
                "supersedes_destination_path": (
                    f"masters/{inventory}/local.pdf" if is_page else ""
                ),
            }
            final_rows.append(applied)
            resolved_applied.append(applied)

    shared_applied: list[dict[str, Any]] = []
    for target, (source, first, last) in sorted(shared_maps.items()):
        target_pdf = fallback_by_inventory[target]
        for row in shared_source_rows[target]:
            relation_note = (
                f"curator reassigned {source} pages {first}-{last} to {target}; "
                "the underlying TIFF bytes remain in the validated delta"
            )
            applied = {
                **row,
                "inventory": target,
                "md_object_id": target_pdf["md_object_id"],
                "md_pdf_url": target_pdf["md_pdf_url"],
                "final_action": "reassign_shared_page",
                "decision": "confirmed_shared_tiff_pages",
                "final_component": "md_pdf_matching_pages",
                "final_role": "preservation_master_page",
                "final_notes": relation_note,
                "supersedes_storage_root_id": "full-001",
                "supersedes_destination_path": f"masters/{target}/local.pdf",
            }
            final_rows.append(applied)
            shared_applied.append(applied)

    final_rows.sort(
        key=lambda row: (
            row["inventory"],
            row["role"] != "tiff_page",
            page_sequence(row) or 0,
            row["storage_root_id"],
            row["destination_path"],
        )
    )
    duplicate_physical = [
        item
        for item, count in Counter(
            (row["storage_root_id"], row["destination_path"])
            for row in final_rows
        ).items()
        if count > 1
    ]
    if duplicate_physical:
        raise DecisionError(
            "duplicate physical rows in final manifest: "
            + ", ".join(path for _root, path in duplicate_physical[:10])
        )

    fallback_inventories = sorted(
        {
            row["inventory"]
            for row in final_rows
            if row["role"] == "local_pdf_fallback"
        }
    )
    expected_fallbacks = [args.expected_fallback_inventory]
    if fallback_inventories != expected_fallbacks:
        raise DecisionError(
            f"expected remaining fallback {expected_fallbacks}, "
            f"found {fallback_inventories}"
        )

    role_counts = Counter(row["role"] for row in final_rows)
    component_counts = Counter(row["final_component"] for row in final_rows)
    storage_counts = Counter(row["storage_root_id"] for row in final_rows)
    write_csv(output_dir / "final-manifest.csv", FINAL_FIELDNAMES, final_rows)
    write_csv(
        output_dir / "removed-base-fallbacks.csv",
        FINAL_FIELDNAMES,
        removed_rows,
    )
    write_csv(
        output_dir / "resolved-gap-applied-rows.csv",
        FINAL_FIELDNAMES,
        resolved_applied,
    )
    write_csv(
        output_dir / "shared-page-applied-rows.csv",
        FINAL_FIELDNAMES,
        shared_applied,
    )
    pdf_plan = [
        {
            "output": "TRO-1953-26-2-md.pdf",
            "kind": "md_matching",
            "page_sources": "TRO-1953-26-2:7-14",
            "status": "planned",
            "notes": "curator confirmed pages 7-14 match the MD PDF",
        },
        {
            "output": "TRO-1953-26-full.pdf",
            "kind": "full_local_issue",
            "page_sources": "TRO-1953-26:1-6 | TRO-1953-26-2:7-14",
            "status": "planned",
            "notes": "curator confirmed all 14 pages are one physical newspaper issue",
        },
    ]
    write_csv(
        output_dir / "pdf-derivative-plan.csv",
        ["output", "kind", "page_sources", "status", "notes"],
        pdf_plan,
    )

    summary = {
        "format": CONFIRMED_FORMAT,
        "created_at": utc_now(),
        "plan_id": args.plan_id,
        "output": str(output_dir),
        "base_final_manifest_rows": len(base_rows),
        "resolved_inventories": sorted(resolved_inventories),
        "shared_page_maps": {
            target: {
                "source_inventory": source,
                "first_page": first,
                "last_page": last,
            }
            for target, (source, first, last) in sorted(shared_maps.items())
        },
        "resolved_rows_added": len(resolved_applied),
        "shared_pages_reassigned": len(shared_applied),
        "final_manifest_rows": len(final_rows),
        "role_counts": dict(sorted(role_counts.items())),
        "final_component_counts": dict(sorted(component_counts.items())),
        "storage_root_counts": dict(sorted(storage_counts.items())),
        "remaining_pdf_fallback_inventories": fallback_inventories,
        "remaining_pdf_fallback_count": len(fallback_inventories),
        "pdf_derivative_plan_rows": len(pdf_plan),
        "validation": {
            "expected_fallback_inventory": args.expected_fallback_inventory,
            "remaining_fallback_ok": fallback_inventories == expected_fallbacks,
            "duplicate_physical_rows": len(duplicate_physical),
            "source_bytes_copied_by_this_step": False,
            "base_bytes_recopied": False,
        },
    }
    (output_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    (output_dir / "README.md").write_text(
        "# Trafo Confirmed Reconciliation Plan\n\n"
        "Delta-first logical final manifest. It keeps the validated `full-001` "
        "and `mik1953-001` bytes, adds the separately validated resolved-gap "
        "delta, and applies curator-confirmed shared-page assignments. It does "
        "not delete sources, recopy the base, generate derivatives, publish, "
        "or create/seal the final Trafo unit.\n\n"
        "- `final-manifest.csv`: logical final rows across the three work roots.\n"
        "- `resolved-gap-applied-rows.csv`: three confirmed mislabeled-folder issues.\n"
        "- `shared-page-applied-rows.csv`: TIFF pages reassigned to four MD records.\n"
        "- `pdf-derivative-plan.csv`: the accepted MD-matching and full-issue PDF plan.\n"
        "- `summary.json`: enforced counts and the sole remaining fallback.\n",
        encoding="utf-8",
    )
    return {"result": "ok", **summary}


def build(args: argparse.Namespace) -> dict[str, Any]:
    work_root = args.work_root.expanduser().resolve()
    if not str(work_root).startswith(WORK_ROOT_PREFIX):
        raise DecisionError(f"work root must be below {WORK_ROOT_PREFIX}")
    if args.base_final_manifest:
        return build_confirmed_reconciliation(args, work_root)

    require_args(
        args,
        [
            "full_manifest",
            "reviewed_manifest",
            "decisions",
            "full_copy_root",
            "reviewed_copy_root",
        ],
    )

    output_dir = work_root / "work" / "finalization-plan" / args.plan_id
    output_dir.mkdir(parents=True, exist_ok=False)

    full_rows = read_csv(args.full_manifest)
    reviewed_rows = read_csv(args.reviewed_manifest)
    decision_rows = read_csv(args.decisions)
    decisions = require_unique_decisions(decision_rows)
    reviewed_inventories = set(decisions)

    reviewed_by_inventory: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in reviewed_rows:
        if row["inventory"] in reviewed_inventories:
            reviewed_by_inventory[row["inventory"]].append(row)

    missing_reviewed = sorted(
        inventory for inventory in reviewed_inventories if inventory not in reviewed_by_inventory
    )
    if missing_reviewed:
        raise DecisionError(
            "review decisions without copied replacement rows: "
            + ", ".join(missing_reviewed)
        )

    base_removed_rows = [row for row in full_rows if row["inventory"] in reviewed_inventories]
    if len(base_removed_rows) != len(reviewed_inventories):
        raise DecisionError(
            "expected one old base fallback row per reviewed inventory; found "
            f"{len(base_removed_rows)} for {len(reviewed_inventories)} inventories"
        )
    unexpected_base_roles = sorted(
        {
            row["role"]
            for row in base_removed_rows
            if row["role"] != "local_pdf_fallback"
        }
    )
    if unexpected_base_roles:
        raise DecisionError(
            "reviewed inventories are not all old PDF fallbacks in full base: "
            + ", ".join(unexpected_base_roles)
        )

    final_rows: list[dict[str, Any]] = []
    removed_rows: list[dict[str, Any]] = []
    decision_applied_rows: list[dict[str, Any]] = []

    for row in full_rows:
        if row["inventory"] in reviewed_inventories:
            removed = {
                **row,
                "final_action": "suppress_base_row",
                "decision": decisions[row["inventory"]]["decision"],
                "final_component": "superseded_pdf_fallback",
                "final_role": "superseded_access_pdf",
                "final_notes": "old full-001 PDF fallback superseded by reviewed TIFF candidate",
                "storage_root_id": "full-001",
                "storage_root": str(args.full_copy_root),
                "supersedes_storage_root_id": "",
                "supersedes_destination_path": "",
            }
            removed_rows.append(removed)
            continue
        final_rows.append(
            {
                **row,
                "final_action": "keep_base_row",
                "decision": "",
                "final_component": "existing_full001_selection",
                "final_role": row["role"],
                "final_notes": "unchanged from validated full-001 base copy",
                "storage_root_id": "full-001",
                "storage_root": str(args.full_copy_root),
                "supersedes_storage_root_id": "",
                "supersedes_destination_path": "",
            }
        )

    for inventory in sorted(reviewed_inventories):
        decision = decisions[inventory]["decision"]
        rows = sorted(
            reviewed_by_inventory[inventory],
            key=lambda row: (row["role"] != "tiff_page", page_sequence(row) or 0, row["destination_path"]),
        )
        for row in rows:
            component, final_role, notes = page_component(inventory, decision, row)
            applied = {
                **row,
                "final_action": "add_delta_row",
                "decision": decision,
                "final_component": component,
                "final_role": final_role,
                "final_notes": notes,
                "storage_root_id": "mik1953-001",
                "storage_root": str(args.reviewed_copy_root),
                "supersedes_storage_root_id": (
                    "full-001" if row["role"] == "tiff_page" else ""
                ),
                "supersedes_destination_path": (
                    f"masters/{inventory}/local.pdf" if row["role"] == "tiff_page" else ""
                ),
            }
            final_rows.append(applied)
            decision_applied_rows.append(applied)

    inventory_summary: list[dict[str, Any]] = []
    for inventory in sorted(reviewed_inventories):
        rows = [row for row in decision_applied_rows if row["inventory"] == inventory]
        component_counts = Counter(row["final_component"] for row in rows)
        inventory_summary.append(
            {
                "inventory": inventory,
                "decision": decisions[inventory]["decision"],
                "md_object_id": rows[0]["md_object_id"] if rows else "",
                "md_pdf_url": rows[0]["md_pdf_url"] if rows else "",
                "md_pdf_matching_pages": component_counts["md_pdf_matching_pages"],
                "separate_letter_addition_pages": component_counts["separate_letter_addition"],
                "full_local_version_extra_pages": component_counts["full_local_version_extra"],
                "retained_local_pdf": component_counts["retained_local_pdf"],
                "reviewed_rows_added": len(rows),
                "old_full001_rows_removed": sum(
                    1 for row in removed_rows if row["inventory"] == inventory
                ),
                "notes": decisions[inventory]["notes"],
            }
        )

    write_csv(output_dir / "final-manifest.csv", FINAL_FIELDNAMES, final_rows)
    write_csv(
        output_dir / "removed-full001-rows.csv",
        FINAL_FIELDNAMES,
        removed_rows,
    )
    write_csv(
        output_dir / "review-decision-applied-rows.csv",
        FINAL_FIELDNAMES,
        decision_applied_rows,
    )
    write_csv(
        output_dir / "inventory-summary.csv",
        [
            "inventory",
            "decision",
            "md_object_id",
            "md_pdf_url",
            "md_pdf_matching_pages",
            "separate_letter_addition_pages",
            "full_local_version_extra_pages",
            "retained_local_pdf",
            "reviewed_rows_added",
            "old_full001_rows_removed",
            "notes",
        ],
        inventory_summary,
    )

    role_counts = Counter(row["role"] for row in final_rows)
    component_counts = Counter(row["final_component"] for row in final_rows)
    storage_counts = Counter(row["storage_root_id"] for row in final_rows)
    decision_counts = Counter(row["decision"] for row in decision_rows)
    remaining_reviewed_fallbacks = sorted(
        {
            row["inventory"]
            for row in final_rows
            if row["inventory"] in reviewed_inventories
            and row["role"] == "local_pdf_fallback"
        }
    )
    fallback_inventories = sorted(
        {
            row["inventory"]
            for row in final_rows
            if row["role"] == "local_pdf_fallback"
        }
    )
    summary = {
        "format": FORMAT,
        "created_at": utc_now(),
        "plan_id": args.plan_id,
        "output": str(output_dir),
        "full_base_rows": len(full_rows),
        "review_decisions": len(decision_rows),
        "old_full001_rows_removed": len(removed_rows),
        "reviewed_rows_added": len(decision_applied_rows),
        "final_manifest_rows": len(final_rows),
        "role_counts": dict(sorted(role_counts.items())),
        "final_component_counts": dict(sorted(component_counts.items())),
        "storage_root_counts": dict(sorted(storage_counts.items())),
        "decision_counts": dict(sorted(decision_counts.items())),
        "delta_bytes": sum(int(row["size_bytes"]) for row in decision_applied_rows),
        "remaining_pdf_fallback_inventories": fallback_inventories,
        "remaining_pdf_fallback_count": len(fallback_inventories),
        "remaining_reviewed_pdf_fallback_inventories": remaining_reviewed_fallbacks,
        "validation": {
            "old_reviewed_pdf_fallbacks_removed": not remaining_reviewed_fallbacks,
            "expected_remaining_pdf_fallback_count": 8,
            "remaining_pdf_fallback_count_ok": len(fallback_inventories) == 8,
            "delta_first_model": True,
            "base_bytes_recopied": False,
            "source_bytes_copied": False,
        },
    }
    (output_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    (output_dir / "README.md").write_text(
        "# Trafo Review Decision Finalization Plan\n\n"
        "Delta-first manifest only. This step keeps the validated `full-001` base "
        "in place and overlays the reviewed `mik1953-001` correction rows. It does "
        "not recopy the base package, delete superseded rows, or publish archive "
        "package files.\n\n"
        "- `final-manifest.csv`: logical final package row set across both storage roots.\n"
        "- `review-decision-applied-rows.csv`: only rows added from reviewed 1953 material.\n"
        "- `removed-full001-rows.csv`: old PDF fallback rows superseded by review.\n"
        "- `inventory-summary.csv`: one reviewed inventory per row with component counts.\n"
        "- `summary.json`: validation counts and remaining PDF fallbacks.\n",
        encoding="utf-8",
    )
    return {"result": "ok", **summary}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-root", type=Path, required=True)
    parser.add_argument("--full-manifest", type=Path)
    parser.add_argument("--reviewed-manifest", type=Path)
    parser.add_argument("--decisions", type=Path)
    parser.add_argument("--full-copy-root", type=Path)
    parser.add_argument("--reviewed-copy-root", type=Path)
    parser.add_argument("--base-final-manifest", type=Path)
    parser.add_argument("--resolved-manifest", type=Path)
    parser.add_argument("--resolved-copy-root", type=Path)
    parser.add_argument("--resolved-storage-root-id")
    parser.add_argument("--resolved-inventory", action="append")
    parser.add_argument("--shared-page-map", action="append")
    parser.add_argument("--expected-fallback-inventory")
    parser.add_argument("--plan-id", default="trafo-1953-review-001")
    return parser


def main() -> int:
    result = build(build_parser().parse_args())
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
