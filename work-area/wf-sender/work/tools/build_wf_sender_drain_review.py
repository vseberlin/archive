#!/usr/bin/env python3
"""Build a conservative exact-path WF-Sender disposition/drain review register."""

from __future__ import annotations

import csv
import json
import os
import re
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path


RUN = Path(
    "/mnt/volume/iss-archive-work/wf-sender/work/full-restore-audit/runs/"
    "20260729T094604Z-wf-sender-full-restore"
)
SOURCE_ROOT = Path("/media/vladimir/Elements/restore")
FINAL_UNIT = Path("/mnt/volume/iss-archive/units/wf-sender")
FINAL_DISPOSITION = FINAL_UNIT / "db-restore/manifests/source-disposition.csv"

FIELDS = (
    "source_path",
    "source_kind",
    "unit",
    "inventory",
    "source_role",
    "final_path",
    "preservation_status",
    "preservation_basis",
    "source_sha256",
    "final_sha256",
    "source_size_bytes",
    "exact_copy_proven",
    "drain_state",
    "deletion_authorized",
    "notes",
)

LATEST_SITE = (
    "/media/vladimir/Elements/restore/archive/WF/WF-Sender Projekt/Webseite/"
    "Backup/Duplicator_2022-03-06/"
    "2022_03_06_11cba5e4ed9290c72781_20220306103703_archive.zip"
)
LATEST_SITE_COMPANIONS = {
    "/media/vladimir/Elements/restore/archive/WF/WF-Sender Projekt/Webseite/"
    "Backup/Duplicator_2022-03-06/installer.php",
    "/media/vladimir/Elements/restore/archive/WF/WF-Sender Projekt/Webseite/"
    "Backup/Duplicator_2022-03-06/TablePress_Backup/"
    "tablepress-export-2022-03-06-12-13-24-csv.zip",
}
LATEST_LRCAT = (
    "/media/vladimir/Elements/restore/archive/WF/Lightroom-Katalog_nur-WF/"
    "Lightroom-Katalog_WF_nur-WF-v10-2-v11.lrcat"
)
GOOGLE_EXPORT = (
    "/media/vladimir/Elements/restore/archive/WF/GoogleDocsBackup/"
    "WF-Sender-20220713T123002Z-001.zip"
)
CROSSWALK = (
    "/media/vladimir/Elements/restore/archive/WF/WF-Sender Projekt/"
    "Umbenennungen_WFS-1951-54-etc.txt"
)

RECOVERED_PROJECT_DOCS = {
    "/media/vladimir/Elements/restore/archive/WF/WF-Sender Projekt/"
    "2.Basisaufnahme_Stand.xlsx",
    "/media/vladimir/Elements/restore/archive/WF/WF-Sender Projekt/"
    "SW_Inhalt-1989-90_02.xlsx",
    "/media/vladimir/Elements/restore/archive/WF/WF-Sender Projekt/"
    "ARBEITSVERTRAG_WF-Sender_Digitalisierung.docx",
    "/media/vladimir/Elements/restore/archive/WF/WF-Sender Projekt/"
    "WFS-1970er.xlsx",
    "/media/vladimir/Elements/restore/archive/WF/WF-Sender Projekt/"
    "SW_Inhalt-1989-90_05_Kategorien.xlsx",
    "/media/vladimir/Elements/restore/archive/WF/WF-Sender Projekt/"
    "WF-1949-1952-ausfuehrlich.xlsx",
    "/media/vladimir/Elements/restore/archive/WF/WF-Sender Projekt/"
    "SW_Inhalt-1989-90_03.xlsx",
    "/media/vladimir/Elements/restore/archive/WF/WF-Sender Projekt/"
    "Beauftragter zur Aufarbeitung der SED Diktatur/"
    "Finanzierungsplan, 3. Fassung_Abrechnung .docx",
    "/media/vladimir/Elements/restore/archive/WF/WF-Sender Projekt/"
    "Uploads/1950/Inhalt_1950-Stand-11.10.20_06_horizontal.xlsx",
}

AGGREGATE_1959 = re.compile(r"/WFS_1959_gesamt(?:_komprimiert)?\.pdf$", re.I)
CARRIER_NAME = re.compile(
    r"(?:^|/)(?:WFS|HFS)[_-](?:FB[_-])?\d{4}(?:[-_][A-Z0-9]+)+", re.I
)
CARRIER_DERIVATIVE_EXTENSIONS = {
    ".pdf",
    ".tif",
    ".tiff",
    ".jpg",
    ".jpeg",
    ".png",
    ".htm",
    ".html",
    ".css",
}
CROSS_UNIT_MARKERS = (
    "/Trafo",
    "/Kurt",
    "/Brigade",
    "/TFA",
    "/Forschungs- und Entwicklungsberichte",
    "/Archivarien/",
    "/Thesaurus",
    "/Ausstellung-",
    "/MEDIATHEK-Backup/",
    "/alter Rechner-",
)


def load_csv(path: Path) -> list[dict[str, str]]:
    csv.field_size_limit(sys.maxsize)
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def root_is_read_only(path: Path) -> bool:
    return bool(os.statvfs(path).f_flag & getattr(os, "ST_RDONLY", 1))


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def classify(
    path: str,
    inventory_row: dict[str, str],
    evidence: set[str],
    exact: dict[str, str] | None,
) -> tuple[str, str, str, str]:
    """Return status, basis, drain state, notes."""
    lower = path.casefold()
    name = Path(path).name.casefold()
    suffix = Path(path).suffix.casefold()

    if exact:
        return (
            "preserved_exact_duplicate",
            "fresh whole-restore SHA-256 and size match to final unit",
            "candidate_after_separate_review",
            f"Exact final duplicate: {exact['final_path']}.",
        )
    if name.startswith(("~$", ".~lock.")) or name in {
        "thumbs.db",
        "desktop.ini",
        ".ds_store",
    }:
        return (
            "workflow_noise_candidate",
            "temporary lock or operating-system metadata filename",
            "candidate_after_separate_review",
            "Noise label is a review hypothesis; exact path only.",
        )
    if path == LATEST_SITE or path in LATEST_SITE_COMPANIONS:
        return (
            "retain_pending_accession",
            "latest identified project website restoration set",
            "retain",
            "Accession and validate as a site-restoration package before older backups are reviewed.",
        )
    if path == LATEST_LRCAT:
        return (
            "retain_pending_accession",
            "latest identified WF-only Lightroom catalog",
            "retain",
            "Meaningful scan/catalog provenance; SQLite quick-check was inconclusive due slow USB I/O.",
        )
    if path == GOOGLE_EXPORT:
        return (
            "retain_pending_accession",
            "dedicated WF-Sender Google Docs export with ten project workbooks",
            "retain",
            "Preserve the export container and appraise workbook overlap after accession.",
        )
    if path == CROSSWALK:
        return (
            "retain_pending_accession",
            "direct inventory-rename and museum-digital identity crosswalk",
            "retain",
            "Required provenance for historical aliases and special-edition mappings.",
        )
    if path in RECOVERED_PROJECT_DOCS:
        sensitivity = " Contains personal data." if "ARBEITSVERTRAG" in path else ""
        return (
            "retain_pending_appraisal",
            "recoverable direct project document; 7-Zip integrity test passed",
            "retain",
            "Appraise against later workbook/document versions before any supersession decision."
            + sensitivity,
        )
    if AGGREGATE_1959.search(path):
        return (
            "covered_derivative_candidate",
            "aggregate has 194 pages; final unit has 48 WFS-1959 access PDFs totaling 194 pages",
            "candidate_after_separate_review",
            "Page-count coverage is proven; no deletion authority granted.",
        )
    if any(marker.casefold() in lower for marker in CROSS_UNIT_MARKERS):
        return (
            "retain_cross_unit_or_contextual",
            "hit occurs in another unit, research, or mixed contextual tree",
            "retain",
            "WF-Sender text/name is not proof that WF-Sender owns this source.",
        )
    if "/lightroom-katalog_nur-wf/" in lower:
        return (
            "hold_catalog_supersession_review",
            "older catalog, backup, preview, or alternate in WF-only Lightroom tree",
            "retain",
            "Review only after latest valid catalog is accessioned and catalog relationships are recorded.",
        )
    if "/wf-sender projekt/webseite/backup/" in lower:
        return (
            "hold_site_backup_supersession_review",
            "historical website restoration backup",
            "retain",
            "Review only after a complete latest restoration set is accessioned and restore-tested.",
        )
    if "/wf-sender projekt/webseite/" in lower:
        return (
            "hold_site_workflow_review",
            "website content, export, or generated access artifact",
            "retain",
            "Do not drain until the authoritative website snapshot and data exports are accessioned.",
        )
    if "/wf-sender projekt/" in lower and (
        "csv_xml" in lower or suffix in {".zip", ".7z", ".rar"}
    ):
        return (
            "retain_pending_package_appraisal",
            "project archive/import/export package",
            "retain",
            "Inspect member coverage and retain meaningful museum-digital or restoration provenance.",
        )
    if CARRIER_NAME.search(path) and suffix in CARRIER_DERIVATIVE_EXTENSIONS:
        return (
            "covered_carrier_derivative_candidate",
            "inventory-like carrier filename in scan, upload, search, or access tree",
            "candidate_after_separate_review",
            "Candidate only; verify inventory alias and final representation before drain action.",
        )
    if "/wf-sender projekt/" in lower:
        return (
            "retain_unresolved_project_artifact",
            "direct project-tree hit not proven represented in final unit",
            "retain",
            "Requires document/package appraisal or explicit supersession evidence.",
        )
    if evidence == {"archive_member_hit"}:
        return (
            "retain_contextual_container",
            "WF-Sender term appears inside a mixed or external archive container",
            "retain",
            "Container ownership and other-unit coverage must be resolved first.",
        )
    return (
        "retain_contextual_or_unresolved",
        "fresh name/content/member hit outside proven final disposition",
        "retain",
        "The match establishes relevance, not ownership or supersession.",
    )


def main() -> None:
    if not SOURCE_ROOT.is_dir() or not root_is_read_only(SOURCE_ROOT):
        raise SystemExit(f"protected source is unavailable or writable: {SOURCE_ROOT}")

    inventory = {r["source_path"]: r for r in load_csv(RUN / "restore-inventory.csv")}
    existing = {r["source_path"]: r for r in load_csv(FINAL_DISPOSITION)}
    exact = {r["source_path"]: r for r in load_csv(RUN / "exact-final-matches.csv")}
    evidence: dict[str, set[str]] = {}

    def add(path: str, label: str) -> None:
        evidence.setdefault(path, set()).add(label)

    for row in load_csv(RUN / "path-hits.csv"):
        add(row["source_path"], "path_hit")
    for row in load_csv(RUN / "content-hits.csv"):
        add(row["source_path"], "content_hit")
    for row in load_csv(RUN / "archive-member-hits.csv"):
        add(row["source_path"], "archive_member_hit")
    for row in load_csv(RUN / "search-errors.csv"):
        path = row["source_path"]
        if path in inventory and (
            inventory[path]["path_match"] == "true"
            or "/WF-Sender Projekt/" in path
            or "/Lightroom-Katalog_nur-WF/" in path
        ):
            add(path, "extraction_error")
    for path in exact:
        add(path, "exact_final_match")
    for path in existing:
        add(path, "published_disposition")

    rows: list[dict[str, str]] = []
    counts: Counter[str] = Counter()
    for path in sorted(evidence):
        if path in existing:
            row = dict(existing[path])
            row["notes"] = (
                row.get("notes", "")
                + " Fresh full-restore audit evidence: "
                + "|".join(sorted(evidence[path]))
                + "."
            ).strip()
        else:
            inv = inventory.get(path, {})
            status, basis, drain_state, notes = classify(
                path, inv, evidence[path], exact.get(path)
            )
            match = exact.get(path, {})
            row = {
                "source_path": path,
                "source_kind": inv.get("category", "container"),
                "unit": "wf-sender",
                "inventory": "",
                "source_role": "restore_search_candidate",
                "final_path": match.get("final_path", ""),
                "preservation_status": status,
                "preservation_basis": basis,
                "source_sha256": match.get("source_sha256", ""),
                "final_sha256": match.get("final_sha256", ""),
                "source_size_bytes": inv.get("size_bytes", ""),
                "exact_copy_proven": "true" if match else "false",
                "drain_state": drain_state,
                "deletion_authorized": "false",
                "notes": notes
                + " Audit evidence: "
                + "|".join(sorted(evidence[path]))
                + ".",
            }
        if row.get("deletion_authorized") != "false":
            raise SystemExit(f"unsafe deletion authorization: {path}")
        counts[row["preservation_status"]] += 1
        counts["rows"] += 1
        if row.get("drain_state") == "candidate_after_separate_review":
            counts["drain_candidates"] += 1
        else:
            counts["retained_or_unresolved"] += 1
        rows.append({field: row.get(field, "") for field in FIELDS})

    output = RUN / "wf-sender-disposition-review.csv"
    with output.open("x", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS, quoting=csv.QUOTE_ALL)
        writer.writeheader()
        writer.writerows(rows)

    remaining = [
        row
        for row in rows
        if row["preservation_status"].startswith("retain_pending")
        or row["preservation_status"].startswith("retain_unresolved")
        or row["preservation_status"].startswith("hold_")
    ]
    with (RUN / "remaining-to-archive-or-appraise.csv").open(
        "x", encoding="utf-8", newline=""
    ) as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS, quoting=csv.QUOTE_ALL)
        writer.writeheader()
        writer.writerows(remaining)

    candidates = [
        row
        for row in rows
        if row["drain_state"] == "candidate_after_separate_review"
    ]
    with (RUN / "drain-candidates-review.csv").open(
        "x", encoding="utf-8", newline=""
    ) as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS, quoting=csv.QUOTE_ALL)
        writer.writeheader()
        writer.writerows(candidates)

    summary = {
        "format": "iss-wf-sender-disposition-review-v1",
        "created_at": now(),
        "source_root": str(SOURCE_ROOT),
        "source_read_only_observed": True,
        "published_disposition_rows": len(existing),
        "review_rows": len(rows),
        "remaining_to_archive_or_appraise_rows": len(remaining),
        "drain_candidate_rows": len(candidates),
        "deletion_authorized_rows": 0,
        "counts_by_preservation_status": dict(sorted(counts.items())),
        "limitations": [
            "Labels are conservative review hypotheses and do not authorize deletion.",
            "The scan extracted text from all PDFs and supported office/plain/database files, "
            "but did not OCR every raster image.",
            "Mixed containers and other-unit trees remain retained until their owning units "
            "are accessioned.",
        ],
    }
    with (RUN / "disposition-review-summary.json").open(
        "x", encoding="utf-8"
    ) as handle:
        json.dump(summary, handle, ensure_ascii=False, sort_keys=True, indent=2)
        handle.write("\n")

    findings = f"""# WF-Sender full restore audit and drain review

Created: {summary['created_at']}

## Safety state

- Source scanned: `{SOURCE_ROOT}`
- Source observed read-only: yes
- Published unit: `{FINAL_UNIT}`
- Deletion authorized: **no** (zero rows)

## Exhaustive search result

- Restore inventory: 454,190 files / 5,189,169,198,024 bytes
- Published source-disposition paths found in restore: 11,890
- New path/name hits: 26,293
- New content hits: 15,144
- Archive-member hits: 11,423 across 75 containers
- Exact-byte duplicates of final files: 11
- Extraction errors: 216; relevant WF project Office files were separately tested
  with 7-Zip and all nine passed integrity testing.

## What still prevents single-source authority

1. Latest WF project website restoration set:
   `{LATEST_SITE}` plus installer and TablePress export.
2. Latest WF-only Lightroom catalog:
   `{LATEST_LRCAT}`.
3. Dedicated Google Docs export:
   `{GOOGLE_EXPORT}`.
4. Direct inventory rename / museum-digital crosswalk:
   `{CROSSWALK}`.
5. Nine recoverable direct project documents requiring version/sensitivity appraisal.
6. Project import/export packages and historical website/catalog backups require
   supersession review after the selected authoritative artifacts are accessioned.

## Covered or likely noise

- Eleven paths are exact SHA-256/size duplicates of final-unit files.
- WFS 1959 aggregate PDFs have 194 pages; the 48 final WFS 1959 issue PDFs also
  total 194 pages. They are covered-derivative candidates.
- Repeated scan, upload, watermarked, compressed, HTML/CSS, search, and website
  derivatives are candidate drain paths only after exact alias/final coverage review.
- Office lock files and operating-system metadata are workflow-noise candidates.
- Trafo, Kurt Schwarz, TFA, Brigadebuch, telephone/research, and mixed-container
  hits remain retained as cross-unit/contextual material.

## Register meaning

`wf-sender-disposition-review.csv` is the expanded exact-path working register.
`drain-candidates-review.csv` is a candidate list only. Every row has
`deletion_authorized=false`. Promote rows into the final unit manifest only after
the remaining artifacts are accessioned and the final checksums/supersession
evidence are recorded.
"""
    (RUN / "FINDINGS.md").write_text(findings, encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=2))


if __name__ == "__main__":
    main()
