#!/usr/bin/env python3
"""Build a conservative review digest from the Trafo USB documentation search."""

from __future__ import annotations

import csv
import json
import os
import re
from collections import Counter
from pathlib import Path
from typing import Any


RUN = Path(
    "/mnt/volume/iss-archive-work/trafo/work/deep-search/"
    "trafo-documentation-usb-20260727-001"
)
STRUCTURED = {
    ".xlsx", ".xls", ".xlsm", ".ods", ".csv", ".tsv", ".docx", ".doc",
    ".odt", ".rtf", ".txt", ".zip",
}
NAME_RE = re.compile(
    r"(?i)(trafo|\bTRO\b|basis|angaben|bestand|findbuch|"
    r"archiv.*(?:liste|buch|zeitung)|fehlende.?maße|AG.?Inhalt|"
    r"betriebszeitung)"
)
EXCLUDED_CONTEXT = re.compile(
    r"(?i)(/MIK-Daten/PDF/|/Alte PC-Datein/Windows/|/Pressebildarchiv/|"
    r"/BRIGADEBUCH-Projekt/|/Thesaurus/)"
)
CORE_EXPORTS = {
    (
        "/media/vladimir/Elements/restore/archive/WF/Trafo/1.Upload/"
        "WFS_Trafo_csv_xml.zip"
    ),
    (
        "/media/vladimir/Elements/restore/archive/WF/Trafo/2.Upload/"
        "csv_xml.zip"
    ),
}
GOOGLE_BACKUP = (
    "/media/vladimir/Elements/restore/archive/WF/GoogleDocsBackup/"
    "drive-download-20220713T123037Z-001.zip"
)


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, quoting=csv.QUOTE_ALL)
        writer.writeheader()
        writer.writerows(rows)
        handle.flush()
        os.fsync(handle.fileno())


def write_json(path: Path, value: Any) -> None:
    with path.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, sort_keys=True, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())


def classify(
    row: dict[str, str], seed_paths: set[str]
) -> tuple[str, int, str] | None:
    path = row["path"]
    base = Path(path).name
    extension = row["extension"]
    terms = set(row["terms"].split(";"))
    if path in seed_paths:
        if base.startswith("~$"):
            return (
                "temporary-office-lock",
                5,
                "temporary lock artifact; do not treat as an original source",
            )
        if "/WF-Sender Projekt/" in path:
            return (
                "cross-unit-source-review",
                3,
                "prior index seed located in WF-Sender scope",
            )
        if "Abrechnung Projekt" in base:
            return (
                "project-administration",
                4,
                "Trafo project administration, not descriptive metadata",
            )
        if base == "Trafo_Bestand_02.xlsx":
            return (
                "core-metadata-lineage-damaged",
                1,
                "core holdings workbook; ZIP central directory is damaged",
            )
        return (
            "core-metadata-lineage",
            1,
            "prior archive index metadata-source reference",
        )
    if path in CORE_EXPORTS:
        member_count = 502 if "WFS_Trafo" in base else 1043
        return (
            "core-metadata-export-archive",
            1,
            f"original CSV/XML export ZIP with {member_count} TRO metadata records",
        )
    if path == GOOGLE_BACKUP:
        return (
            "embedded-source-container-reference",
            2,
            (
                "393 MB mixed Google backup; extract the exact Basisangaben "
                "member rather than preserving the whole container"
            ),
        )
    if extension == ".zip" and "bibliocopy" in base.casefold():
        return (
            "media-container-not-metadata-documentation",
            5,
            "Bibliocopy issue-media ZIP; outside metadata-document bundle",
        )
    if "/WF/Trafo/Sachbericht/" in path or "/WF/Trafo/Rechnungen" in path:
        return (
            "project-documentation",
            3,
            "project report, finance, delivery, or administration evidence",
        )
    if "/WF/WF-Sender Projekt/" in path and (
        NAME_RE.search(base) or "prior-index-seed" in row["reasons"]
    ):
        return (
            "cross-unit-source-review",
            3,
            "located in WF-Sender scope; do not assign to Trafo automatically",
        )
    if extension not in STRUCTURED or EXCLUDED_CONTEXT.search(path):
        return None
    if (
        NAME_RE.search(base)
        or (
            "/WF/Trafo/" in path
            and extension != ".pdf"
        )
        or (
            terms & {"tro-inventory", "der-trafo"}
            and re.search(
                r"(?i)(findbuch|archiv|bestand|geschichte|digis|google)",
                path,
            )
        )
    ):
        if extension == ".zip":
            return (
                "finding-aid-or-document-archive-review",
                3,
                "archive container with supporting document matches",
            )
        return (
            "additional-documentation-review",
            2,
            "structured document with Trafo path, title, or strong content",
        )
    return None


def main() -> int:
    candidates = read_csv(RUN / "candidate-files.csv")
    archive_hits = read_csv(RUN / "archive-member-hits.csv")
    seeds = read_csv(RUN / "prior-index-seeds.csv")
    errors = json.loads((RUN / "errors.json").read_text(encoding="utf-8"))
    seed_paths = {row["usb_path"] for row in seeds}
    error_by_path = {
        row["path"]: row["error"]
        for row in errors
        if "!/" not in row["path"]
    }

    digest: list[dict[str, Any]] = []
    for row in candidates:
        result = classify(row, seed_paths)
        if not result:
            continue
        classification, priority, note = result
        digest.append(
            {
                "priority": priority,
                "classification": classification,
                "path": row["path"],
                "extension": row["extension"],
                "size_bytes": row["size_bytes"],
                "reasons": row["reasons"],
                "terms": row["terms"],
                "identity_matches_inventory": row[
                    "identity_matches_inventory"
                ],
                "read_error": error_by_path.get(row["path"], ""),
                "note": note,
            }
        )
    digest.sort(
        key=lambda row: (
            int(row["priority"]),
            row["classification"],
            row["path"],
        )
    )

    embedded_by_member: dict[tuple[str, str], dict[str, Any]] = {}
    for row in archive_hits:
        if (
            row["archive"] != GOOGLE_BACKUP
            or not re.search(
                r"(?i)TRO.*Basisangaben.*\.xlsx$", row["member"]
            )
        ):
            continue
        key = (row["archive"], row["member"])
        current = embedded_by_member.setdefault(
            key,
            {
                "archive": row["archive"],
                "member": row["member"],
                "member_size_bytes": row["member_size_bytes"],
                "terms": set(),
                "selection_reason": (
                    "embedded TRO Basisangaben workbook in Google Drive backup"
                ),
            },
        )
        current["terms"].update(row["terms"].split(";"))
    embedded = [
        {
            **row,
            "terms": ";".join(sorted(term for term in row["terms"] if term)),
        }
        for row in embedded_by_member.values()
    ]
    write_csv(
        RUN / "review-digest.csv",
        digest,
        [
            "priority", "classification", "path", "extension", "size_bytes",
            "reasons", "terms", "identity_matches_inventory", "read_error",
            "note",
        ],
    )
    write_csv(
        RUN / "embedded-source-members.csv",
        embedded,
        [
            "archive", "member", "member_size_bytes", "terms",
            "selection_reason",
        ],
    )
    counts = Counter(row["classification"] for row in digest)
    summary = {
        "status": "review-digest-ready-no-copy",
        "raw_candidate_files": len(candidates),
        "review_digest_rows": len(digest),
        "classifications": dict(sorted(counts.items())),
        "priority_1_rows": sum(int(row["priority"]) == 1 for row in digest),
        "core_metadata_lineage_rows": counts["core-metadata-lineage"],
        "core_metadata_lineage_damaged_rows": counts[
            "core-metadata-lineage-damaged"
        ],
        "core_metadata_export_archives": counts[
            "core-metadata-export-archive"
        ],
        "embedded_source_containers": counts[
            "embedded-source-container-reference"
        ],
        "embedded_source_members": len(embedded),
        "preservation_source_artifacts": (
            sum(int(row["priority"]) == 1 for row in digest)
            + len(embedded)
        ),
        "temporary_office_locks": counts["temporary-office-lock"],
        "usb_files_copied": 0,
        "usb_files_changed": 0,
        "usb_files_hashed": 0,
        "deletion_authorized": False,
        "artifacts": {
            "review_digest": str(RUN / "review-digest.csv"),
            "embedded_source_members": str(
                RUN / "embedded-source-members.csv"
            ),
        },
    }
    write_json(RUN / "review-summary.json", summary)
    readme = f"""# Trafo USB documentation deep search

Status: **review digest ready; no source copied or changed**

The read-only search used the prior 455,089-file USB census, the surviving
1.27 GB `/mnt/volume/archive/WF` SQLite index as a seed source, direct content
inspection of plain and Office documents, targeted database/catalog queries,
all readable archive member names and document members, and Windows recycle
metadata.

The raw high-recall scan found {len(candidates)} candidate containers. The
conservative review digest contains {len(digest)} rows. Priority 1 consists of
{summary['priority_1_rows']} direct source files: the non-temporary Trafo
spreadsheet/CSV lineage and the two original CSV/XML export ZIPs. One additional
Basisangaben workbook is embedded in a 393 MB mixed Google Drive backup; preserve
that exact 131395-byte member with its container/member provenance instead of
copying the unrelated outer ZIP. One core workbook (`Trafo_Bestand_02.xlsx`) is
present but structurally damaged and is labeled as such.

The digest deliberately separates finding aids, historical context, project
administration, WF-Sender material, temporary Office locks, and Bibliocopy
media containers. No row authorizes copying into the accepted unit, draining,
movement, or deletion.
"""
    (RUN / "README.md").write_text(readme, encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
