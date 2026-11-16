#!/usr/bin/env python3
"""Publish Trafo drain-review decisions as a new work-only queue version."""

from __future__ import annotations

import csv
import json
import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


V2 = Path(
    "/mnt/volume/iss-archive-work/trafo/work/drain/trafo/"
    "source-remnants-20260727-v2"
)
OCR = Path(
    "/mnt/volume/iss-archive-work/trafo/work/drain/trafo/"
    "ocr-alias-review-20260727-v3"
)
EVIDENCE = Path(
    "/mnt/volume/iss-archive-work/trafo/work/drain/trafo/"
    "candidate-evidence-20260727-v1"
)
OUTPUT = Path(
    "/mnt/volume/iss-archive-work/trafo/work/drain/trafo/"
    "source-remnants-20260727-v3"
)
DATABASE = Path(
    "/mnt/volume/iss-archive/units/trafo/db-restore/RESTORE.sqlite"
)

DECKBLATT_SELECTED = {
    "/media/vladimir/Elements/restore/archive/Trafo_Bibliocopy-gesamt/"
    "Charge 02 pdf/TRO-1989-Deckblatt.pdf",
    "/media/vladimir/Elements/restore/archive/Trafo_Bibliocopy-gesamt/"
    "Trafo tifs 1970-1990/TRO-1989-Deckblatt/"
    "TRO-1989-Deckblatt_001.tif",
    "/media/vladimir/Elements/restore/archive/Trafo_Bibliocopy-gesamt/"
    "Trafo tifs 1970-1990/TRO-1989-Deckblatt/"
    "TRO-1989-Deckblatt_002.tif",
}

SPECIAL_MAP = {
    "TRO-1951-SN": {
        "accepted_inventory": "TRO-1951-SN-Weltfestspiele",
        "evidence": (
            "OCR identifies Sondernummer zu den Weltfestspielen; candidate "
            "contains a duplicated cover before the same four accepted pages."
        ),
    },
    "TRO-1951-SN-02": {
        "accepted_inventory": "TRO-1951-SN-August",
        "evidence": (
            "Sixteen candidate pages match the accepted sixteen-page OCR text "
            "page-for-page."
        ),
    },
    "TRO-1953-01-1": {
        "accepted_inventory": "TRO-1953-01",
        "evidence": "Four-page OCR text is identical to accepted issue 1/1953.",
    },
    "TRO-1953-02-1": {
        "accepted_inventory": "TRO-1953-02",
        "evidence": "Four-page OCR text is identical to accepted issue 2/1953.",
    },
    "TRO-1953-03-1": {
        "accepted_inventory": "TRO-1953-03",
        "evidence": "Four-page OCR text is identical to accepted issue 3/1953.",
    },
}


class QueueError(RuntimeError):
    pass


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        raise QueueError(f"refusing empty CSV: {path}")
    fields: list[str] = []
    for row in rows:
        for field in row:
            if field not in fields:
                fields.append(field)
    temporary = path.with_name(f".{path.name}.tmp")
    with temporary.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=fields,
            extrasaction="ignore",
            quoting=csv.QUOTE_ALL,
        )
        writer.writeheader()
        writer.writerows(rows)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def write_json(path: Path, value: Any) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def write_text(path: Path, value: str) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        handle.write(value)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def catalog_context() -> dict[str, Any]:
    database = sqlite3.connect(
        f"file:{DATABASE}?mode=ro&immutable=1", uri=True
    )
    database.row_factory = sqlite3.Row
    try:
        exact_1957 = database.execute(
            "SELECT inventory FROM archive_object "
            "WHERE inventory = 'TRO-1957-037-02'"
        ).fetchall()
        exact_deckblatt = database.execute(
            "SELECT inventory FROM archive_object "
            "WHERE inventory = 'TRO-1989-Deckblatt'"
        ).fetchall()
        nearby = [
            dict(row)
            for row in database.execute(
                "SELECT inventory, title, date_created, page_count "
                "FROM archive_object WHERE inventory IN "
                "('TRO-1957-036', 'TRO-1957-037-01', 'TRO-1957-038') "
                "ORDER BY inventory"
            )
        ]
    finally:
        database.close()
    if exact_1957 or exact_deckblatt:
        raise QueueError("candidate exact inventory unexpectedly exists")
    return {
        "TRO-1957-037-02_exact_record_count": 0,
        "TRO-1989-Deckblatt_exact_record_count": 0,
        "nearby_1957_records": nearby,
    }


def publish() -> dict[str, Any]:
    allowed = Path("/mnt/volume/iss-archive-work/trafo")
    if allowed not in OUTPUT.parents:
        raise QueueError("output is outside the Trafo work root")
    if OUTPUT.exists():
        raise QueueError(f"refusing existing output: {OUTPUT}")

    active = read_csv(V2 / "drain-list.csv")
    routed = read_csv(V2 / "excluded-non-trafo.csv")
    ocr_rows = read_csv(OCR / "ocr-results.csv")
    ocr_by_path = {row["source_path"]: row for row in ocr_rows}
    if len(active) != 85 or len(ocr_rows) != 63:
        raise QueueError("input queue counts differ")
    if any(row["review_status"] != "resolved" for row in ocr_rows):
        raise QueueError("one or more OCR aliases remain unresolved")

    aliases = [row for row in active if row["review_group"] == "probable-alias"]
    specials = [
        row
        for row in active
        if row["review_group"]
        in {"unresolved-1951-special", "alternate-1953-special-pdfs"}
    ]
    candidates = [
        row
        for row in active
        if row["review_group"]
        in {"TRO-1957-037-02", "TRO-1989-Deckblatt"}
    ]
    if (len(aliases), len(specials), len(candidates)) != (63, 8, 14):
        raise QueueError("decision partition differs")

    resolved_aliases: list[dict[str, Any]] = []
    for row in aliases:
        evidence = ocr_by_path.get(row["path"])
        if not evidence:
            raise QueueError(f"OCR evidence unavailable: {row['path']}")
        resolved_aliases.append(
            {
                **row,
                "review_status": "resolved-already-represented",
                "accepted_inventory": evidence["expected_accepted_inventory"],
                "evidence_basis": (
                    "first-page OCR plus full embedded-text comparison"
                ),
                "text_similarity_to_accepted": evidence[
                    "text_similarity_to_accepted"
                ],
                "ocr_review_id": evidence["review_id"],
                "source_action": "none",
                "accession_authorized": "no",
                "move_authorized": "no",
                "deletion_authorized": "no",
            }
        )

    resolved_specials: list[dict[str, Any]] = []
    for row in specials:
        decision = SPECIAL_MAP.get(row["normalized_inventory"])
        if not decision:
            raise QueueError(
                f"special mapping unavailable: {row['normalized_inventory']}"
            )
        resolved_specials.append(
            {
                **row,
                "review_status": "resolved-already-represented",
                "accepted_inventory": decision["accepted_inventory"],
                "evidence_basis": decision["evidence"],
                "source_action": "none",
                "accession_authorized": "no",
                "move_authorized": "no",
                "deletion_authorized": "no",
            }
        )

    candidate_rows: list[dict[str, Any]] = []
    for row in candidates:
        if row["review_group"] == "TRO-1957-037-02":
            candidate_rows.append(
                {
                    **row,
                    "candidate_inventory": "TRO-1957-037-02",
                    "candidate_type": "possible issue version or distinct copy",
                    "source_selection_status": "pending visual identity review",
                    "catalog_exact_record": "absent",
                    "evidence_note": (
                        "Four-page source says Nr. 37, September 1957. "
                        "TRO-1957-037-01 exists and has related text; exact "
                        "identity/version remains to be reviewed."
                    ),
                    "review_status": "accession-candidate",
                    "accession_authorized": "no",
                    "move_authorized": "no",
                    "deletion_authorized": "no",
                }
            )
        else:
            selected = row["path"] in DECKBLATT_SELECTED
            candidate_rows.append(
                {
                    **row,
                    "candidate_inventory": "TRO-1989-Deckblatt",
                    "candidate_type": "collection-level front matter",
                    "source_selection_status": (
                        "user-selected candidate source"
                        if selected
                        else "alternate source not selected"
                    ),
                    "catalog_exact_record": "absent",
                    "evidence_note": (
                        "Two sheets: Pressearchiv holdings note for 1989/1990 "
                        "and Heimatmuseum Köpenick seal sheet."
                    ),
                    "review_status": "accession-candidate",
                    "accession_authorized": "no",
                    "move_authorized": "no",
                    "deletion_authorized": "no",
                }
            )
    if sum(
        row["source_selection_status"] == "user-selected candidate source"
        for row in candidate_rows
    ) != 3:
        raise QueueError("Deckblatt selected-source count differs")

    OUTPUT.mkdir(parents=True, exist_ok=False)
    write_csv(OUTPUT / "accession-candidates.csv", candidate_rows)
    write_csv(OUTPUT / "resolved-aliases.csv", resolved_aliases)
    write_csv(OUTPUT / "resolved-existing-specials.csv", resolved_specials)
    write_csv(OUTPUT / "routed-non-trafo.csv", routed)

    context = catalog_context()
    summary = {
        "format": "iss-trafo-source-remnant-review-v3",
        "created_at": now(),
        "status": "reviewed-candidates-not-accessioned",
        "parent_queue": str(V2),
        "original_v1_paths": 89,
        "routed_wf_sender_paths": len(routed),
        "resolved_alias_paths": len(resolved_aliases),
        "resolved_existing_special_paths": len(resolved_specials),
        "accession_candidate_paths": len(candidate_rows),
        "accession_candidate_entities": [
            {
                "inventory": "TRO-1957-037-02",
                "path_count": 8,
                "candidate_type": "possible issue version or distinct copy",
                "source_selection": "pending",
            },
            {
                "inventory": "TRO-1989-Deckblatt",
                "path_count": 6,
                "candidate_type": "collection-level front matter",
                "user_selected_source_paths": sorted(DECKBLATT_SELECTED),
            },
        ],
        "catalog_context": context,
        "ocr_review": str(OCR),
        "candidate_evidence": str(EVIDENCE),
        "source_files_hashed": 0,
        "source_files_copied": 0,
        "source_files_moved": 0,
        "source_files_deleted": 0,
        "accepted_unit_changed": False,
        "authorization": {
            "accession": False,
            "move": False,
            "deletion": False,
        },
    }
    if 4 + 63 + 8 + 14 != 89:
        raise QueueError("review accounting differs")
    write_json(OUTPUT / "summary.json", summary)
    write_text(
        OUTPUT / "README.md",
        """# Trafo source-remnant review v3

Status: **two accession candidates identified; not accessioned**

The original 89-path queue now reconciles completely:

- four handbook PDFs were routed out of Trafo as WF-Sender material;
- 63 probable aliases were OCR-checked and map to accepted records;
- two 1951 and six 1953 special-name paths are already represented;
- 14 paths belong to two accession-candidate groups.

`TRO-1957-037-02` remains a candidate pending visual identity/version review
against accepted `TRO-1957-037-01`. `TRO-1989-Deckblatt` is collection-level
front matter; the user selected one Bibliocopy PDF and two TIFF sheets as the
preferred source chain.

No accession, copy, move, deletion, or source hashing was authorized or
performed. The accepted unit was not changed.
""",
    )
    return summary


if __name__ == "__main__":
    print(json.dumps(publish(), ensure_ascii=False, indent=2, sort_keys=True))
