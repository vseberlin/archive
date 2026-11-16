#!/usr/bin/env python3
"""Preserve independent Kurt-Schwarz subdivision schemes without flattening."""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import zipfile
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


FORMAT = "iss-kurt-schwarz-subdivision-register-v1"
SOURCE_ROOT = Path("/media/vladimir/Elements/restore")
WORK_ROOT = Path("/mnt/volume/iss-archive-work/kurt-schwarz")
DEFAULT_DISCOVERY = WORK_ROOT / "work/discovery/runs/20260729T233000Z-kurt-schwarz-discovery"
OWN_REGISTER_ROOT = (
    SOURCE_ROOT / "archive/WF/Kurt Schwarz/KurtSchwarzeigeneVerzeichnisse"
)
EXHIBITION_SOURCE = (
    SOURCE_ROOT
    / "archive/WF/Ausstellung_Kurt-Schwarz/weitere Fotos und Vita/"
    "Vita KS_03_A3.docx"
)
EXHIBITION_SERIES = (
    "Abschiedsbewältigung - tausendjähriges Mecklenburg",
    "Menschen und Menschliches",
    "Reiseeindrücke - Marokko - Bolivien - USA - Karibik",
    "'Seh-landschaften' der Küsten der Nordländer",
    "Jahreszeiten",
    "Küstenlandschaft",
    "Kalenderblätter",
    "SW-Fotos aus dem Archiv 1960 - 1990",
    "Vier Jahreszeiten",
    "Ostberliner Zeitreise 2022",
)
REGISTER_FIELDS = (
    "scheme_id",
    "subdivision_id",
    "parent_subdivision_id",
    "label",
    "subdivision_type",
    "source_path",
    "assigned_object_count",
    "assignment_state",
    "notes",
)
MEMBERSHIP_FIELDS = (
    "record_key",
    "md_object_id",
    "inventory",
    "scheme_id",
    "subdivision_id",
    "label",
    "membership_basis",
)


class SubdivisionError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def inside(path: Path, root: Path) -> Path:
    resolved = path.expanduser().resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError as error:
        raise SubdivisionError(f"path escaped allowed root: {resolved}") from error
    return resolved


def clean(value: Any) -> str:
    return " ".join(str(value or "").split())


def docx_text(path: Path) -> str:
    with zipfile.ZipFile(path) as archive:
        payload = archive.read("word/document.xml").decode("utf-8", "replace")
    return clean(re.sub(r"<[^>]+>", " ", payload))


def slug(value: str) -> str:
    value = value.casefold()
    value = re.sub(r"[^a-z0-9äöüß]+", "-", value)
    return value.strip("-")


def build(discovery: Path, output: Path) -> dict[str, Any]:
    with (discovery / "md-objects.csv").open(
        encoding="utf-8-sig", newline=""
    ) as handle:
        objects = list(csv.DictReader(handle))
    registers: list[dict[str, str]] = []
    memberships: list[dict[str, str]] = []

    collection_counts: Counter[tuple[str, str]] = Counter()
    for row in objects:
        collection_ids = row["collection_ids"].split("; ")
        collection_names = row["collections"].split("; ")
        for collection_id, label in zip(collection_ids, collection_names):
            if not collection_id:
                continue
            collection_counts[(collection_id, label)] += 1
            memberships.append(
                {
                    "record_key": f"md:{row['md_object_id']}",
                    "md_object_id": row["md_object_id"],
                    "inventory": row["inventory"],
                    "scheme_id": "museum-digital-collection",
                    "subdivision_id": f"md-collection:{collection_id}",
                    "label": label,
                    "membership_basis": "current museum-digital collection membership",
                }
            )
    for (collection_id, label), count in sorted(
        collection_counts.items(), key=lambda item: int(item[0][0])
    ):
        registers.append(
            {
                "scheme_id": "museum-digital-collection",
                "subdivision_id": f"md-collection:{collection_id}",
                "parent_subdivision_id": (
                    "" if collection_id == "783" else "md-collection:783"
                ),
                "label": label,
                "subdivision_type": (
                    "collection_root"
                    if collection_id == "783"
                    else "collection_subdivision"
                ),
                "source_path": str(discovery / "md-objects.csv"),
                "assigned_object_count": str(count),
                "assignment_state": "object_memberships_explicit",
                "notes": "Current museum-digital relationship; not flattened with local schemes.",
            }
        )

    own_files = sorted(
        path for path in OWN_REGISTER_ROOT.iterdir() if path.is_file()
    )
    for path in own_files:
        label = clean(path.stem)
        registers.append(
            {
                "scheme_id": "kurt-schwarz-own-register",
                "subdivision_id": "ks-register:" + slug(label),
                "parent_subdivision_id": "",
                "label": label,
                "subdivision_type": "source_defined_register",
                "source_path": str(path),
                "assigned_object_count": "",
                "assignment_state": "source_preserved_assignments_not_normalized",
                "notes": (
                    "Kurt Schwarz's own register label; individual object links "
                    "remain in the source document and bag/scan metadata."
                ),
            }
        )

    exhibition_text = docx_text(EXHIBITION_SOURCE)
    for label in EXHIBITION_SERIES:
        words = [word for word in re.split(r"\W+", label) if len(word) > 3]
        if not all(word.casefold() in exhibition_text.casefold() for word in words):
            raise SubdivisionError(
                f"exhibition series not verified in source: {label}"
            )
        registers.append(
            {
                "scheme_id": "kurt-schwarz-exhibition-series",
                "subdivision_id": "ks-exhibition:" + slug(label),
                "parent_subdivision_id": "",
                "label": label,
                "subdivision_type": "exhibition_series",
                "source_path": str(EXHIBITION_SOURCE),
                "assigned_object_count": "",
                "assignment_state": "series_label_only_no_object_assignment_claimed",
                "notes": (
                    "Biography/exhibition source label; kept separate from "
                    "museum-digital collection subdivisions."
                ),
            }
        )

    registers.sort(key=lambda row: (row["scheme_id"], row["subdivision_id"]))
    memberships.sort(
        key=lambda row: (
            int(row["md_object_id"]),
            row["subdivision_id"],
        )
    )
    output.mkdir(parents=True, exist_ok=False)
    for filename, fields, rows in (
        ("subdivision-register.csv", REGISTER_FIELDS, registers),
        ("subdivision-memberships.csv", MEMBERSHIP_FIELDS, memberships),
    ):
        with (output / filename).open(
            "x", encoding="utf-8", newline=""
        ) as handle:
            writer = csv.DictWriter(handle, fieldnames=fields, quoting=csv.QUOTE_ALL)
            writer.writeheader()
            writer.writerows(rows)
            handle.flush()
            os.fsync(handle.fileno())
    summary = {
        "format": FORMAT,
        "created_at": utc_now(),
        "discovery_run": str(discovery),
        "register_rows": len(registers),
        "membership_rows": len(memberships),
        "scheme_counts": dict(Counter(row["scheme_id"] for row in registers)),
        "museum_digital_objects": len(objects),
        "schemes_kept_independent": True,
        "deletion_authorized_rows": 0,
    }
    with (output / "summary.json").open("x", encoding="utf-8") as handle:
        json.dump(summary, handle, ensure_ascii=False, sort_keys=True, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    return summary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--discovery", type=Path, default=DEFAULT_DISCOVERY)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        output = inside(args.output, WORK_ROOT)
        summary = build(args.discovery.expanduser().resolve(), output)
    except (SubdivisionError, OSError, csv.Error, ValueError) as error:
        print(f"ERROR: {error}", file=os.sys.stderr)
        return 1
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
