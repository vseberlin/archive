#!/usr/bin/env python3
"""Classify Kurt Schwarz deep-search hits outside the exclusive USB roots."""

from __future__ import annotations

import argparse
import csv
import json
import os
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


FORMAT = "iss-kurt-schwarz-deep-review-v1"
WORK_ROOT = Path("/mnt/volume/iss-archive-work/kurt-schwarz")
DEFAULT_SEARCH = WORK_ROOT / (
    "work/deep-search/runs/"
    "20260729T190100Z-kurt-schwarz-restore-search"
)
FIELDS = (
    "source_path",
    "relative_path",
    "search_scopes",
    "match_reasons",
    "matched_members",
    "matched_snippets",
    "relationship",
    "review_state",
    "unit_action",
    "final_area",
    "drain_state",
    "review_basis",
    "deletion_authorized",
)


class DeepReviewError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def inside(path: Path, root: Path) -> Path:
    resolved = path.expanduser().resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError as error:
        raise DeepReviewError(f"path escaped allowed root: {resolved}") from error
    return resolved


def classify(relative: str) -> tuple[str, str, str, str, str, str]:
    lower = relative.casefold()
    suffix = Path(relative).suffix.casefold()
    if lower == (
        "archive/wf/googledocsbackup/"
        "drive-download-20220713t123037z-001.zip"
    ):
        return (
            "direct_project_members_in_shared_container",
            "included_members_separately",
            "member_inventory_and_selective_accession",
            "documents_and_access",
            "retain_shared_container",
            "explicit Kurt Schwarz member root; container also remains shared",
        )
    if (
        "/webseite/" in lower
        or "lightroom" in lower
        or suffix in {".lrcat", ".lrprev"}
        or ".lrdata/" in lower
    ):
        return (
            "website_or_lightroom_metadata_source",
            "boundary_hold",
            "catalog_verified_metadata_only",
            "object_metadata",
            "retain_separate_project_source",
            "website and Lightroom trees remain outside this unit review",
        )
    if "/mediathek-backup/alte pc-datein/windows/" in lower:
        return (
            "recovery_artifact_false_positive",
            "excluded_noise",
            "log_only",
            "",
            "retain_outside_unit",
            "system-file payload contains unrelated recovered Kurt data",
        )
    if Path(relative).name.casefold().startswith("results_duplicates"):
        return (
            "recovery_diagnostic",
            "excluded_noise",
            "log_only",
            "",
            "retain_outside_unit",
            "global duplicate-search diagnostic is not project evidence",
        )
    if suffix == ".xmp":
        return (
            "cross_project_creator_metadata",
            "included_metadata_only",
            "catalog_verified_metadata_only",
            "object_metadata",
            "retain_cross_project_source",
            "sidecar credits Kurt Schwarz but belongs to another project tree",
        )
    if lower.startswith("archive/wf/archiv/export_29_20191120_lido/"):
        return (
            "historical_museum_digital_record",
            "included",
            "copy_exact_document",
            "export/md/historical",
            "retain_shared_source",
            "historical object export directly describes Kurt Schwarz records",
        )
    if lower.startswith("archive/wf/archiv/primus_fuer_"):
        return (
            "historical_museum_digital_record",
            "included",
            "copy_exact_document",
            "export/md/historical",
            "retain_shared_source",
            "historical museum-digital preparation table contains project records",
        )
    if (
        relative == "archive/101 Abbildungen_06_work.xlsx"
        or "bildtexte-und-bildnachweis" in lower
        or "bildtexte_buchprojekt_wordpress_final" in lower
        or "buchprojket_kurt-schwarz-bilder" in lower
    ):
        return (
            "cross_project_context_document",
            "included_contextual",
            "copy_exact_document",
            "documents/contextual",
            "retain_cross_project_source",
            "book/image-credit record contains substantive Kurt Schwarz context",
        )
    if "buchprojekt" in lower:
        return (
            "cross_project_book_context",
            "included_metadata_only",
            "catalog_verified_metadata_only",
            "object_metadata",
            "retain_cross_project_source",
            "general book-project source remains with its owning project",
        )
    if "arbeitszeugnis" in lower:
        return (
            "restricted_project_history",
            "included_contextual_restricted",
            "copy_exact_document",
            "documents/restricted",
            "retain_shared_source",
            "employment record names the Kurt Schwarz project; restrict access",
        )
    if lower.startswith("archive/mik-daten/") or "/wf-sender projekt/" in lower:
        return (
            "cross_unit_photo_credit_context",
            "included_metadata_only",
            "catalog_verified_metadata_only",
            "object_metadata",
            "retain_cross_unit_source",
            "source belongs to WF-Sender or MIK unit and only credits Kurt Schwarz",
        )
    return (
        "cross_project_context",
        "included_metadata_only",
        "catalog_verified_metadata_only",
        "object_metadata",
        "retain_cross_project_source",
        "keyword context retained as metadata; source remains with owning project",
    )


def atomic_csv(path: Path, rows: list[dict[str, str]]) -> None:
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    with temporary.open("x", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS, quoting=csv.QUOTE_ALL)
        writer.writeheader()
        writer.writerows(rows)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def atomic_json(path: Path, payload: Any) -> None:
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    with temporary.open("x", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def run(search: Path, output: Path) -> dict[str, Any]:
    output.mkdir(parents=True, exist_ok=False)
    grouped: dict[str, dict[str, Any]] = {}
    for filename in ("path-hits.csv", "content-and-archive-hits.csv"):
        with (search / filename).open(encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                source = row["source_path"]
                entry = grouped.setdefault(
                    source,
                    {
                        "source_path": source,
                        "relative_path": row["relative_path"],
                        "scopes": set(),
                        "reasons": set(),
                        "members": set(),
                        "snippets": set(),
                    },
                )
                if entry["relative_path"] != row["relative_path"]:
                    raise DeepReviewError(f"source path changed relative identity: {source}")
                if row.get("source_scope"):
                    entry["scopes"].add(row["source_scope"])
                if row.get("match_reason"):
                    entry["reasons"].add(row["match_reason"])
                if row.get("member"):
                    entry["members"].add(row["member"])
                if row.get("snippet"):
                    entry["snippets"].add(" ".join(row["snippet"].split()))
    rows: list[dict[str, str]] = []
    for entry in grouped.values():
        relationship, state, action, final_area, drain, basis = classify(
            entry["relative_path"]
        )
        rows.append(
            {
                "source_path": entry["source_path"],
                "relative_path": entry["relative_path"],
                "search_scopes": "; ".join(sorted(entry["scopes"])),
                "match_reasons": "; ".join(sorted(entry["reasons"])),
                "matched_members": "; ".join(sorted(entry["members"])),
                "matched_snippets": " || ".join(sorted(entry["snippets"])),
                "relationship": relationship,
                "review_state": state,
                "unit_action": action,
                "final_area": final_area,
                "drain_state": drain,
                "review_basis": basis,
                "deletion_authorized": "false",
            }
        )
    rows.sort(key=lambda row: row["relative_path"])
    atomic_csv(output / "deep-hit-review.csv", rows)
    with (search / "search-errors.csv").open(
        encoding="utf-8-sig", newline=""
    ) as handle:
        errors = list(csv.DictReader(handle))
    summary = {
        "format": FORMAT,
        "created_at": utc_now(),
        "deep_search_run": str(search),
        "reviewed_source_paths": len(rows),
        "relationship_counts": dict(Counter(row["relationship"] for row in rows)),
        "review_state_counts": dict(Counter(row["review_state"] for row in rows)),
        "unit_action_counts": dict(Counter(row["unit_action"] for row in rows)),
        "non_destructive_search_errors": len(errors),
        "unreviewed_rows": 0,
        "deletion_authorized_rows": 0,
    }
    atomic_json(output / "summary.json", summary)
    return summary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--search", type=Path, default=DEFAULT_SEARCH)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        output = inside(args.output, WORK_ROOT)
        summary = run(args.search.expanduser().resolve(), output)
    except (OSError, csv.Error, ValueError, DeepReviewError) as error:
        print(f"ERROR: {error}", file=os.sys.stderr)
        return 1
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
