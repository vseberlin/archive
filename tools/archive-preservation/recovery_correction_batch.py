#!/usr/bin/env python3
"""Plan and run reviewed Paragon corrective re-accessions serially."""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path
from typing import Any

TOOL_ROOT = Path(__file__).resolve().parent
if str(TOOL_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOL_ROOT))

import catalog_core
import reconciliation
import tiff_accession
from jobs.provenance_restoration import surrogate_accession
from workflows import recovery_correction


FORMAT = "iss-recovery-correction-batch-plan-v1"
RESULT_FORMAT = "iss-recovery-correction-batch-result-v1"


class CorrectionBatchError(RuntimeError):
    pass


def _ready_inventories(root: Path) -> list[str]:
    with closing(
        catalog_core.connect_catalog(
            root / "work" / "catalog.sqlite",
            read_only=True,
        )
    ) as catalog:
        labels = {
            str(row["entity_id"]): str(row["archival_label"])
            for row in catalog.execute(
                """SELECT entity_id, archival_label
                   FROM archive_objects WHERE status='active'"""
            )
        }
        states = catalog_core.recovery_correction_states(catalog)
    return sorted(
        labels[entity_id]
        for entity_id, decision in states.items()
        if entity_id in labels and decision.get("state") == "ready"
    )


def build_plan(
    root: Path,
    *,
    inventories: list[str] | None = None,
    limit: int = 0,
) -> dict[str, Any]:
    root = catalog_core.assert_safe_work_root(root.resolve(strict=True))
    ready = _ready_inventories(root)
    requested = sorted(set(inventories or []))
    unknown = sorted(set(requested) - set(ready))
    if unknown:
        raise CorrectionBatchError(
            "requested inventory is not correction-ready: "
            + ", ".join(unknown)
        )
    selected = requested or ready
    if limit:
        selected = selected[:limit]
    items = [
        recovery_correction.plan(root, inventory=inventory)
        for inventory in selected
    ]
    encoded = json.dumps(
        items,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return {
        "format": FORMAT,
        "root": str(root),
        "count": len(items),
        "items": items,
        "plan_digest": "sha256:" + hashlib.sha256(encoded).hexdigest(),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--actor", required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--run", action="store_true")
    parser.add_argument("--plan-digest", default="")
    parser.add_argument("--inventory", action="append", default=[])
    parser.add_argument("--limit", type=int, default=0)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    if arguments.limit < 0:
        raise SystemExit("--limit must not be negative")
    try:
        plan = build_plan(
            arguments.root,
            inventories=arguments.inventory,
            limit=arguments.limit,
        )
        if arguments.dry_run:
            print(json.dumps(plan, ensure_ascii=False, sort_keys=True, indent=2))
            return 0
        if not arguments.plan_digest:
            raise CorrectionBatchError(
                "--run requires --plan-digest from a reviewed dry-run"
            )
        if arguments.plan_digest != plan["plan_digest"]:
            raise CorrectionBatchError(
                "correction batch plan changed; generate and review a new dry-run"
            )
        successes: list[dict[str, str]] = []
        failures: list[dict[str, str]] = []
        for item in plan["items"]:
            inventory = str(item["inventory"])
            try:
                result = recovery_correction.apply(
                    arguments.root,
                    inventory=inventory,
                    actor=arguments.actor,
                    reason=f"Bound correction batch {plan['plan_digest']}",
                    expected_plan_digest=str(item["plan_digest"]),
                )
                successes.append(
                    {
                        "inventory": inventory,
                        "operation": str(result["operation"]),
                        "generation_id": str(
                            result["publication"]["generation_id"]
                        ),
                    }
                )
                print(
                    f"corrected {inventory} as "
                    f"{result['publication']['generation_id']}",
                    flush=True,
                )
            except (
                CorrectionBatchError,
                recovery_correction.RecoveryCorrectionError,
                catalog_core.CoreCatalogError,
                reconciliation.ReconciliationError,
                tiff_accession.TiffAccessionError,
                surrogate_accession.SurrogateAccessionError,
                sqlite3.Error,
                OSError,
                ValueError,
            ) as error:
                failures.append(
                    {"inventory": inventory, "error": str(error)}
                )
                print(f"failed {inventory}: {error}", flush=True)
                break
        report = {
            "format": RESULT_FORMAT,
            "plan_digest": plan["plan_digest"],
            "planned": int(plan["count"]),
            "succeeded": successes,
            "failed": failures,
        }
        print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2))
        return 1 if failures else 0
    except (
        CorrectionBatchError,
        recovery_correction.RecoveryCorrectionError,
        catalog_core.CoreCatalogError,
        sqlite3.Error,
        OSError,
        ValueError,
    ) as error:
        raise SystemExit(str(error)) from error


if __name__ == "__main__":
    raise SystemExit(main())
