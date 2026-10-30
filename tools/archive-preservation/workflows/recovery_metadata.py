"""Normalized rich metadata from the frozen Paragon workbook source."""

from __future__ import annotations

import hashlib
import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from recovery_workbook_backfill import workbook_rows


WORKBOOK = Path(
    "/mnt/volume/archive-intake/elements-restore-md-candidates/"
    "source_restore_archive/Paragon/"
    "Bildtexte Paragon stand 05.02.2020.xlsx"
)


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


@lru_cache(maxsize=1)
def _source() -> tuple[str, str, dict[str, dict[str, Any]]]:
    path = WORKBOOK.resolve(strict=True)
    sheet, rows = workbook_rows(path)
    return _digest(path), sheet, rows


def embedding_metadata(inventory: str) -> dict[str, str]:
    """Return normalized optional TIFF metadata with exact row provenance."""

    workbook_digest, sheet, rows = _source()
    row = rows.get(inventory)
    if not row:
        return {}
    values = row["values"]

    def first(*keys: str) -> str:
        return next(
            (
                str(values.get(key) or "").strip()
                for key in keys
                if str(values.get(key) or "").strip()
            ),
            "",
        )

    contributors: list[str] = []
    for key in (
        "related_actor1",
        "related_actor2",
        "printed_who1",
        "production_who",
        "production_where1",
    ):
        value = str(values.get(key) or "").strip()
        if value and value not in contributors:
            contributors.append(value)
    image_references = [
        str(values.get(key) or "").strip()
        for key in ("image_name1", "image_name 2")
        if str(values.get(key) or "").strip()
    ]
    certainty = [
        key
        for key, value in values.items()
        if "sure" in key.casefold()
        and str(value or "").strip().casefold() in {"y", "yes", "ja", "1"}
    ]
    canonical_row = json.dumps(
        values,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    result = {
        "creator": first("production_who", "printed_who1"),
        "date_created": first("related_time1", "printed_when_1"),
        "coverage": first("related_place1"),
        "contributor": "; ".join(contributors),
        "source_collection": first("collection"),
        "previous_owner": first("owner_previous"),
        "image_reference": "; ".join(image_references),
        "certainty_context": ", ".join(certainty),
        "workbook_source": (
            f"{WORKBOOK.name}#{sheet}!row={int(row['row'])}"
        ),
        "workbook_sha256": "sha256:" + workbook_digest,
        "workbook_record_digest": (
            "sha256:" + hashlib.sha256(canonical_row).hexdigest()
        ),
    }
    return {key: value for key, value in result.items() if value}


def require_embedding_metadata(inventory: str) -> dict[str, str]:
    result = embedding_metadata(inventory)
    if not result:
        raise ValueError(
            f"exact rich workbook metadata is unavailable for {inventory}"
        )
    return result
