#!/usr/bin/env python3
"""Prepare a no-copy, no-hash dry run for two reviewed Trafo candidates."""

from __future__ import annotations

import csv
import json
import os
import sqlite3
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


RUN_ID = "trafo-remnant-candidates-dry-run-003"
FINAL = Path("/mnt/volume/iss-archive/units/trafo")
DATABASE = FINAL / "db-restore/RESTORE.sqlite"
OUTPUT = (
    Path("/mnt/volume/iss-archive-work/trafo/work/accession-runs") / RUN_ID
)
SOURCE_ROOT = Path("/media/vladimir/Elements")

ISSUE_TIFFS = [
    SOURCE_ROOT
    / "restore/archive/Trafo_Bibliocopy-gesamt/Charge 02 tif bis 1969/"
    f"TRO-1957-037-02/TRO-1957-037-02_{page:03d}.tif"
    for page in range(1, 5)
]
DECK_TIFFS = [
    SOURCE_ROOT
    / "restore/archive/Trafo_Bibliocopy-gesamt/Trafo tifs 1970-1990/"
    f"TRO-1989-Deckblatt/TRO-1989-Deckblatt_{page:03d}.tif"
    for page in range(1, 3)
]
DECK_PDF = (
    SOURCE_ROOT
    / "restore/archive/Trafo_Bibliocopy-gesamt/Charge 02 pdf/"
    "TRO-1989-Deckblatt.pdf"
)

OBJECT_FIELDS = [
    "inventory",
    "museum_digital_object_id",
    "title",
    "description",
    "object_type",
    "material_technique",
    "dimensions",
    "date_created",
    "collection_name",
    "rights_usage_terms",
    "metadata_license",
    "metadata_source",
    "page_count",
    "representation_count",
    "status",
]
REPRESENTATION_FIELDS = [
    "inventory",
    "role",
    "sequence",
    "source_path",
    "planned_relative_path",
    "planned_transformation",
    "source_identity_verification",
    "source_hash_planned_at_staging",
    "content_hash_planned_after_transformation",
]


class DryRunError(RuntimeError):
    pass


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def write_csv(
    path: Path, rows: list[dict[str, Any]], fields: list[str]
) -> None:
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


def source_identity(path: Path) -> dict[str, Any]:
    if not path.is_file() or path.is_symlink():
        raise DryRunError(f"source unavailable or symlinked: {path}")
    if SOURCE_ROOT not in path.parents:
        raise DryRunError(f"source outside reviewed USB root: {path}")
    stat = path.stat()
    return {
        "path": str(path),
        "device": stat.st_dev,
        "inode": stat.st_ino,
        "size_bytes": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
    }


def source_mount() -> dict[str, Any]:
    result = subprocess.run(
        ["findmnt", "-J", "-T", str(SOURCE_ROOT)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        raise DryRunError(f"findmnt failed: {result.stderr.strip()}")
    filesystems = json.loads(result.stdout)["filesystems"]
    if not filesystems:
        raise DryRunError("source mount unavailable")
    mount = filesystems[0]
    options = set(str(mount.get("options", "")).split(","))
    if "ro" not in options or "rw" in options:
        raise DryRunError(f"source USB is not read-only: {mount}")
    return {
        "target": mount.get("target"),
        "source": mount.get("source"),
        "fstype": mount.get("fstype"),
        "options": mount.get("options"),
        "read_only": True,
    }


def tiff_technical(paths: list[Path]) -> list[dict[str, Any]]:
    result = subprocess.run(
        [
            "exiftool",
            "-json",
            "-FileName",
            "-FileSize#",
            "-FileType",
            "-ImageWidth",
            "-ImageHeight",
            "-BitsPerSample",
            "-Compression",
            "-XResolution",
            "-YResolution",
            "-ResolutionUnit",
            *[str(path) for path in paths],
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        raise DryRunError(f"TIFF inspection failed: {result.stderr.strip()}")
    rows = json.loads(result.stdout)
    if len(rows) != len(paths):
        raise DryRunError("TIFF technical row count differs")
    for row in rows:
        if (
            row.get("FileType") != "TIFF"
            or row.get("BitsPerSample") != "8 8 8"
            or row.get("Compression") != "Uncompressed"
            or row.get("XResolution") != 300
            or row.get("YResolution") != 300
        ):
            raise DryRunError(f"unexpected TIFF technical profile: {row}")
    return rows


def pdf_pages(path: Path) -> int:
    result = subprocess.run(
        ["pdfinfo", str(path)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        raise DryRunError(f"PDF inspection failed: {result.stderr.strip()}")
    for line in result.stdout.splitlines():
        if line.startswith("Pages:"):
            return int(line.split(":", 1)[1].strip())
    raise DryRunError("PDF page count unavailable")


def object_rows() -> list[dict[str, Any]]:
    return [
        {
            "inventory": "TRO-1957-037-02",
            "museum_digital_object_id": "",
            "title": (
                "TRO-Betriebszeitung 'Der Transformator'; Nr. 37/1957 "
                "(September 1957), Bestand 02"
            ),
            "description": (
                "Der Transformator, Betriebszeitung des Transformatorenwerkes "
                "\"Karl Liebknecht\"; 9. Jahrgang; Nr. 37, September 1957; "
                "Herausgeber: SED-Betriebsparteiorganisation; 4 Seiten. "
                "Lokale Bibliocopy-Digitalisierung. Der Bestand wurde vom "
                "Kurator als eigenständiger Zugang TRO-1957-037-02 bestätigt; "
                "ein gleichnamiger museum-digital-Datensatz liegt nicht vor."
            ),
            "object_type": "Zeitung",
            "material_technique": "Papier",
            "dimensions": "",
            "date_created": "September 1957",
            "collection_name": "Betriebszeitung des TRO - Der Trafo",
            "rights_usage_terms": "unknown",
            "metadata_license": "not supplied",
            "metadata_source": (
                "local-source-curator-reviewed-no-md-record"
            ),
            "page_count": 4,
            "representation_count": 5,
            "status": "unsealed",
        },
        {
            "inventory": "TRO-1989-Deckblatt",
            "museum_digital_object_id": "",
            "title": (
                "Bestandsdeckblatt des Pressearchivs des Heimatmuseums "
                "Köpenick zu 'Der Trafo' (1989–1990)"
            ),
            "description": (
                "Zweiseitige Sammlungsbegleitunterlage des Pressearchivs des "
                "Heimatmuseums Köpenick. Blatt 1 verzeichnet für 'Der Trafo' "
                "die Bestände 1989 Nr. 1–29 und 31–49 sowie 1990 Nr. 1–4, "
                "6–14 und 18–23; Blatt 2 zeigt das Köpenicker Siegelmotiv. "
                "Lokale Bibliocopy-Digitalisierung; vom Kurator als "
                "collection-level front matter bestätigt."
            ),
            "object_type": "Sammlungsbegleitmaterial",
            "material_technique": "Papier",
            "dimensions": "",
            "date_created": "1989–1990",
            "collection_name": "Betriebszeitung des TRO - Der Trafo",
            "rights_usage_terms": "unknown",
            "metadata_license": "not supplied",
            "metadata_source": (
                "local-source-curator-reviewed-no-md-record"
            ),
            "page_count": 2,
            "representation_count": 3,
            "status": "unsealed",
        },
    ]


def representation_rows() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for sequence, source in enumerate(ISSUE_TIFFS, 1):
        rows.append(
            {
                "inventory": "TRO-1957-037-02",
                "role": "preservation_master_page",
                "sequence": sequence,
                "source_path": str(source),
                "planned_relative_path": (
                    "masters/TRO-1957-037-02/pages/"
                    f"TRO-1957-037-02_{sequence:03d}.tif"
                ),
                "planned_transformation": "embed reviewed metadata",
                "source_identity_verification": "device+inode+size+mtime",
                "source_hash_planned_at_staging": "yes-once",
                "content_hash_planned_after_transformation": "yes-once",
            }
        )
    rows.append(
        {
            "inventory": "TRO-1957-037-02",
            "role": "generated_access_pdf",
            "sequence": "",
            "source_path": "derived from the four planned master pages",
            "planned_relative_path": (
                "export/access/TRO-1957-037-02.pdf"
            ),
            "planned_transformation": "generate four-page access PDF",
            "source_identity_verification": "derived-output validation",
            "source_hash_planned_at_staging": "not-applicable",
            "content_hash_planned_after_transformation": "yes-once",
        }
    )
    for sequence, source in enumerate(DECK_TIFFS, 1):
        rows.append(
            {
                "inventory": "TRO-1989-Deckblatt",
                "role": "preservation_master_page",
                "sequence": sequence,
                "source_path": str(source),
                "planned_relative_path": (
                    "masters/TRO-1989-Deckblatt/pages/"
                    f"TRO-1989-Deckblatt_{sequence:03d}.tif"
                ),
                "planned_transformation": "embed reviewed metadata",
                "source_identity_verification": "device+inode+size+mtime",
                "source_hash_planned_at_staging": "yes-once",
                "content_hash_planned_after_transformation": "yes-once",
            }
        )
    rows.append(
        {
            "inventory": "TRO-1989-Deckblatt",
            "role": "retained_access_pdf",
            "sequence": "",
            "source_path": str(DECK_PDF),
            "planned_relative_path": (
                "export/access/TRO-1989-Deckblatt.pdf"
            ),
            "planned_transformation": "retain selected two-page PDF",
            "source_identity_verification": "device+inode+size+mtime",
            "source_hash_planned_at_staging": "yes-once",
            "content_hash_planned_after_transformation": (
                "reuse staging identity if bytes unchanged"
            ),
        }
    )
    return rows


def prepare() -> dict[str, Any]:
    allowed = Path("/mnt/volume/iss-archive-work/trafo")
    if allowed not in OUTPUT.parents:
        raise DryRunError("output outside Trafo work root")
    if OUTPUT.exists():
        raise DryRunError(f"refusing existing output: {OUTPUT}")
    mount = source_mount()
    selected_sources = ISSUE_TIFFS + DECK_TIFFS + [DECK_PDF]
    identities = [source_identity(path) for path in selected_sources]
    technical = tiff_technical(ISSUE_TIFFS + DECK_TIFFS)
    if pdf_pages(DECK_PDF) != 2:
        raise DryRunError("Deckblatt PDF is not two pages")

    objects = object_rows()
    representations = representation_rows()
    database = sqlite3.connect(
        f"file:{DATABASE}?mode=ro&immutable=1", uri=True
    )
    try:
        integrity = database.execute("PRAGMA integrity_check").fetchone()[0]
        current_objects = database.execute(
            "SELECT count(*) FROM archive_object"
        ).fetchone()[0]
        current_representations = database.execute(
            "SELECT count(*) FROM representation"
        ).fetchone()[0]
        current_tiffs = database.execute(
            "SELECT count(*) FROM representation "
            "WHERE role='preservation_master_page'"
        ).fetchone()[0]
        current_access = database.execute(
            "SELECT count(*) FROM representation "
            "WHERE relative_path LIKE 'export/access/%.pdf'"
        ).fetchone()[0]
        collisions = database.execute(
            "SELECT inventory FROM archive_object "
            "WHERE inventory IN ('TRO-1957-037-02','TRO-1989-Deckblatt')"
        ).fetchall()
        representation_paths = {
            row[0]
            for row in database.execute(
                "SELECT relative_path FROM representation"
            )
        }
    finally:
        database.close()
    if integrity != "ok" or collisions:
        raise DryRunError("final database baseline or inventory collision differs")
    planned_paths = {
        row["planned_relative_path"] for row in representations
    }
    if len(planned_paths) != 8 or planned_paths & representation_paths:
        raise DryRunError("planned representation path collision")
    for relative in planned_paths:
        if (FINAL / relative).exists():
            raise DryRunError(f"planned final path already exists: {relative}")
    if (current_objects, current_representations) != (1590, 11972):
        raise DryRunError("current final counts differ")
    if (current_tiffs, current_access) != (10377, 1591):
        raise DryRunError("current representation-role counts differ")

    current_checksums = 0
    with (FINAL / "db-restore/checksums.csv").open(
        encoding="utf-8-sig", newline=""
    ) as handle:
        current_checksums = sum(1 for _ in csv.DictReader(handle))
    if current_checksums != 25540:
        raise DryRunError("current checksum count differs")

    OUTPUT.mkdir(parents=True, exist_ok=False)
    write_csv(OUTPUT / "proposed-objects.csv", objects, OBJECT_FIELDS)
    write_csv(
        OUTPUT / "proposed-representations.csv",
        representations,
        REPRESENTATION_FIELDS,
    )
    write_json(OUTPUT / "source-identities.json", identities)
    write_json(OUTPUT / "tiff-technical.json", technical)
    decisions = {
        "run_id": RUN_ID,
        "recorded_at": now(),
        "decisions": [
            {
                "inventory": "TRO-1957-037-02",
                "decision": "confirmed accession candidate",
                "source_policy": "four TIFF masters; generated access PDF",
                "user_wording": "confirmed with tifs",
            },
            {
                "inventory": "TRO-1989-Deckblatt",
                "decision": "confirmed collection-level front matter",
                "source_policy": "two TIFF masters; selected two-page PDF",
                "user_wording": "collection level front",
            },
        ],
        "publication_authorized": False,
    }
    write_json(OUTPUT / "decisions.json", decisions)
    final_counts = {
        "objects": {"before": current_objects, "after": current_objects + 2},
        "representations": {
            "before": current_representations,
            "after": current_representations + 8,
        },
        "preservation_tiff_pages": {
            "before": current_tiffs,
            "after": current_tiffs + 6,
        },
        "access_pdfs": {
            "before": current_access,
            "after": current_access + 2,
        },
        "checksum_rows": {
            "before": current_checksums,
            "after": current_checksums + 19,
            "basis": (
                "six TIFFs, two access PDFs, six page manifests, two access-PDF "
                "reports, two metadata-source records, and one addition manifest"
            ),
        },
        "new_package_files": 19,
    }
    write_json(OUTPUT / "proposed-final-counts.json", final_counts)
    validation = {
        "run_id": RUN_ID,
        "validated_at": now(),
        "status": "ok",
        "source_mount": mount,
        "selected_source_files": len(selected_sources),
        "selected_source_bytes": sum(
            row["size_bytes"] for row in identities
        ),
        "source_identity_checks": "ok",
        "tiff_technical_checks": "ok",
        "deckblatt_pdf_pages": 2,
        "final_sqlite_integrity": integrity,
        "inventory_collisions": 0,
        "representation_path_collisions": 0,
        "source_files_hashed": 0,
        "source_files_copied": 0,
        "source_files_modified": 0,
        "accepted_unit_modified": False,
        "publication_authorized": False,
    }
    write_json(OUTPUT / "validation.json", validation)
    write_text(
        OUTPUT / "README.md",
        """# Trafo remnant-candidate accession dry run

Status: **validated dry run; publication not authorized**

This plan proposes two objects:

- `TRO-1957-037-02`: four TIFF preservation masters and a generated
  four-page access PDF;
- `TRO-1989-Deckblatt`: two TIFF preservation masters and the selected
  two-page PDF, modeled as collection-level front matter.

The proposed catalog metadata is intentionally conservative. Neither object
has a museum-digital ID; rights are `unknown`, metadata licence is
`not supplied`, and dimensions remain blank pending any later field-level
decision.

Preparation used source stat, TIFF technical readback, PDF page count, and
read-only catalog queries. It copied, modified, or hashed no source file and
did not change the accepted Trafo unit.
""",
    )
    return validation


if __name__ == "__main__":
    print(json.dumps(prepare(), ensure_ascii=False, indent=2, sort_keys=True))
