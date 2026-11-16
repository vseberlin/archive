#!/usr/bin/env python3
"""Apply reviewed exclusions to the Trafo drain queue without source changes."""

from __future__ import annotations

import csv
import json
import os
import sqlite3
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


SOURCE = Path(
    "/mnt/volume/iss-archive-work/trafo/work/drain/trafo/"
    "source-remnants-20260727-v1"
)
OUTPUT = Path(
    "/mnt/volume/iss-archive-work/trafo/work/drain/trafo/"
    "source-remnants-20260727-v2"
)
FINAL_DB = Path(
    "/mnt/volume/iss-archive/units/trafo/db-restore/RESTORE.sqlite"
)
EXCLUDED_GROUP = "handbook-pdfs"
DECISION_REASON = (
    "User confirmed these files concern the WF-Sender newspaper and are not "
    "related to the Trafo unit."
)


class DecisionError(RuntimeError):
    pass


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def write_csv(path: Path, fields: list[str], rows: list[dict[str, Any]]) -> None:
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


def apply() -> dict[str, Any]:
    allowed = Path("/mnt/volume/iss-archive-work/trafo")
    if allowed not in OUTPUT.parents:
        raise DecisionError("output is outside the Trafo work root")
    if OUTPUT.exists():
        raise DecisionError(f"refusing existing output: {OUTPUT}")

    rows = read_csv(SOURCE / "drain-list.csv")
    groups = read_csv(SOURCE / "group-summary.csv")
    if len(rows) != 89:
        raise DecisionError("v1 queue does not contain 89 paths")

    excluded = [row for row in rows if row["review_group"] == EXCLUDED_GROUP]
    retained = [row for row in rows if row["review_group"] != EXCLUDED_GROUP]
    if len(excluded) != 4:
        raise DecisionError("expected four handbook paths")
    if len(retained) != 85:
        raise DecisionError("expected 85 retained review paths")

    for row in excluded:
        row["review_status"] = "excluded-from-trafo"
        row["decision"] = "route-to-wf-sender-review"
        row["decision_reason"] = DECISION_REASON
        row["accession_authorized"] = "no"
        row["move_authorized"] = "no"
        row["deletion_authorized"] = "no"

    database = sqlite3.connect(
        f"file:{FINAL_DB}?mode=ro&immutable=1", uri=True
    )
    try:
        exact = database.execute(
            "SELECT inventory, title, date_created FROM archive_object "
            "WHERE inventory = 'TRO-1957-037-02'"
        ).fetchall()
        nearby = database.execute(
            "SELECT inventory, title, date_created FROM archive_object "
            "WHERE inventory IN "
            "('TRO-1957-036', 'TRO-1957-037-01', 'TRO-1957-038') "
            "ORDER BY inventory"
        ).fetchall()
    finally:
        database.close()
    if exact:
        raise DecisionError("exact TRO-1957-037-02 unexpectedly exists")
    if [row[0] for row in nearby] != [
        "TRO-1957-036",
        "TRO-1957-037-01",
        "TRO-1957-038",
    ]:
        raise DecisionError("nearby accepted catalog context differs")

    OUTPUT.mkdir(parents=True, exist_ok=False)
    output_fields = list(retained[0])
    write_csv(OUTPUT / "drain-list.csv", output_fields, retained)

    retained_groups = [
        row for row in groups if row["review_group"] != EXCLUDED_GROUP
    ]
    write_csv(
        OUTPUT / "group-summary.csv",
        list(retained_groups[0]),
        retained_groups,
    )
    excluded_fields = list(excluded[0])
    for field in ("decision", "decision_reason"):
        if field not in excluded_fields:
            excluded_fields.append(field)
    write_csv(
        OUTPUT / "excluded-non-trafo.csv",
        excluded_fields,
        excluded,
    )

    group_counts = Counter(row["review_group"] for row in retained)
    retained_bytes = sum(int(row["size_bytes"]) for row in retained)
    excluded_bytes = sum(int(row["size_bytes"]) for row in excluded)
    summary = {
        "format": "iss-trafo-source-remnant-review-v2",
        "created_at": now(),
        "status": "prepared-not-authorized",
        "parent_queue": str(SOURCE),
        "decision": {
            "excluded_group": EXCLUDED_GROUP,
            "excluded_paths": len(excluded),
            "excluded_bytes": excluded_bytes,
            "classification": "WF-Sender newspaper material, not Trafo",
            "reason": DECISION_REASON,
            "source_files_changed": False,
        },
        "remaining_paths": len(retained),
        "remaining_bytes": retained_bytes,
        "remaining_groups": dict(group_counts),
        "catalog_observation": {
            "database": str(FINAL_DB),
            "query_mode": "read-only immutable",
            "exact_inventory": "TRO-1957-037-02",
            "exact_record_count": len(exact),
            "nearby_records": [
                {
                    "inventory": row[0],
                    "title": row[1],
                    "date_created": row[2],
                }
                for row in nearby
            ],
        },
        "candidate_files_opened": 0,
        "candidate_files_hashed": 0,
        "candidate_files_copied": 0,
        "candidate_files_moved": 0,
        "candidate_files_deleted": 0,
        "authorization": {
            "accession": False,
            "move": False,
            "deletion": False,
        },
    }
    if retained_bytes != 945_509_333 or excluded_bytes != 16_638_939:
        raise DecisionError("v2 byte totals differ")
    write_json(OUTPUT / "summary.json", summary)
    write_text(
        OUTPUT / "README.md",
        """# Trafo source-remnant drain review v2

Status: **prepared for review; no action authorized**

The four handbook PDFs from v1 were excluded from the Trafo queue after the
user confirmed that they concern the WF-Sender newspaper. They are recorded
in `excluded-non-trafo.csv` for routing and audit only. Their source files were
not deleted, moved, copied, opened, or hashed.

The active Trafo queue now contains 85 paths in five groups. The exact accepted
catalog has no `TRO-1957-037-02` record; it contains
`TRO-1957-037-01` between issues 36 and 38. The eight `-02` paths therefore
remain the first visual-review group.

No row authorizes accession, movement, or deletion.
""",
    )
    return summary


if __name__ == "__main__":
    print(json.dumps(apply(), ensure_ascii=False, indent=2, sort_keys=True))
