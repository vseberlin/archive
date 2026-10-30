#!/usr/bin/env python3
"""Stage, validate, publish, or roll back the Trafo 1988 metadata enrichment.

Writes are restricted to the Trafo work run and six explicit metadata files in
the accepted Trafo unit. No representation file is opened, copied, or hashed.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import shutil
import sqlite3
import sys
import xml.etree.ElementTree as ET
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


RUN_ID = "trafo-1988-masthead-enrichment-001"
FINAL = Path("/mnt/volume/iss-archive/units/trafo")
WORK = Path("/mnt/volume/iss-archive-work/trafo")
DRY_RUN = (
    WORK
    / "work/metadata-runs/trafo-1988-masthead-enrichment-dry-run-001"
)
RUN_ROOT = WORK / "work/metadata-runs" / RUN_ID
STAGE = RUN_ROOT / "stage"
ROLLBACK = RUN_ROOT / "rollback"
PLAN = RUN_ROOT / "plan.json"
VALIDATION = RUN_ROOT / "staged-validation.json"
PUBLICATION = RUN_ROOT / "publication.json"
FINAL_VALIDATION = RUN_ROOT / "final-validation.json"
DRY_PLAN = DRY_RUN / "plan.json"
DRY_VALIDATION = DRY_RUN / "validation.json"
DRY_DB = DRY_RUN / "proposed-RESTORE.sqlite"

EXPECTED = [f"TRO-1988-{number:03d}" for number in range(1, 43)]
DECISION_SHA256 = "0b784c73bcff0a86667c04f5cb04e44051d7072f747b122878518e285d0cf6b6"
CATALOG_FIELDS = [
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
REPLACED_FILES = [
    "CATALOG.csv",
    "CATALOG.xlsx",
    "README.md",
    "db-restore/RESTORE.sql",
    "db-restore/RESTORE.sqlite",
    "db-restore/checksums.csv",
]
HASHED_REPLACEMENTS = [
    path for path in REPLACED_FILES if path != "db-restore/checksums.csv"
]
UNCHANGED_METADATA_FILES = [
    "db-restore/RESTORE.csv",
    "db-restore/RESTORE.xlsx",
    "db-restore/manifests/accession-plan.json",
    "db-restore/manifests/representations.csv",
]


class PublishError(RuntimeError):
    pass


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def replace_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def replace_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    with temporary.open("wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    with temporary.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def identity(path: Path) -> dict[str, Any]:
    stat = path.stat()
    return {
        "path": str(path),
        "device": stat.st_dev,
        "inode": stat.st_ino,
        "size_bytes": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
    }


def relative_identity(root: Path, relative: str) -> dict[str, Any]:
    value = identity(root / relative)
    value["relative_path"] = relative
    return value


def all_metadata_identity() -> list[dict[str, Any]]:
    return [
        relative_identity(FINAL, relative)
        for relative in REPLACED_FILES + UNCHANGED_METADATA_FILES
    ]


def assert_allowed_paths() -> None:
    for path in (RUN_ROOT, STAGE, ROLLBACK, PLAN, VALIDATION, PUBLICATION):
        if WORK not in path.parents:
            raise PublishError(f"work path outside allowed root: {path}")
    for relative in REPLACED_FILES:
        path = FINAL / relative
        if FINAL not in path.parents:
            raise PublishError(f"final path outside allowed unit: {path}")


def assert_dry_run() -> tuple[dict[str, Any], dict[str, Any]]:
    for path in (DRY_PLAN, DRY_VALIDATION, DRY_DB):
        if not path.is_file() or path.is_symlink():
            raise PublishError(f"dry-run artifact unavailable: {path}")
    dry_plan = read_json(DRY_PLAN)
    dry_validation = read_json(DRY_VALIDATION)
    if dry_plan.get("decision_sha256") != DECISION_SHA256:
        raise PublishError("dry-run decision identity differs")
    if dry_validation.get("decision_sha256") != DECISION_SHA256:
        raise PublishError("dry-run validation decision identity differs")
    if dry_validation.get("status") != "ok":
        raise PublishError("dry-run validation is not ok")
    if dry_validation.get("comparison", {}).get("changed_inventories") != EXPECTED:
        raise PublishError("dry-run changed-object scope differs")
    if dry_validation.get("media_files_hashed") != 0:
        raise PublishError("dry-run media hash policy differs")
    return dry_plan, dry_validation


def assert_baseline(dry_plan: dict[str, Any]) -> list[dict[str, Any]]:
    expected = {
        row["relative_path"]: row
        for row in dry_plan["final_package_metadata_before"]
    }
    current = all_metadata_identity()
    for row in current:
        relative = row["relative_path"]
        baseline = expected.get(relative)
        if not baseline:
            raise PublishError(f"baseline identity unavailable: {relative}")
        for field in ("device", "inode", "size_bytes", "mtime_ns"):
            if row[field] != baseline[field]:
                raise PublishError(
                    f"final metadata changed since dry run: {relative} {field}"
                )
    if (FINAL / "db-restore/RESTORE.sqlite").stat().st_dev != RUN_ROOT.parent.stat().st_dev:
        raise PublishError("work and final roots are not on the same filesystem")
    return current


def xlsx_column_name(index: int) -> str:
    result = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        result = chr(65 + remainder) + result
    return result


def xml_bytes(element: ET.Element) -> bytes:
    return ET.tostring(element, encoding="utf-8", xml_declaration=True)


def table_xlsx_bytes(
    headers: list[str],
    rows: list[dict[str, str]],
    *,
    sheet_name: str,
    table_name: str,
) -> bytes:
    matrix = [headers]
    matrix.extend([[str(row.get(header, "")) for header in headers] for row in rows])
    spreadsheet_ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    relationship_ns = (
        "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    )
    ET.register_namespace("", spreadsheet_ns)
    ET.register_namespace("r", relationship_ns)
    worksheet = ET.Element(f"{{{spreadsheet_ns}}}worksheet")
    views = ET.SubElement(worksheet, f"{{{spreadsheet_ns}}}sheetViews")
    view = ET.SubElement(
        views, f"{{{spreadsheet_ns}}}sheetView", {"workbookViewId": "0"}
    )
    ET.SubElement(
        view,
        f"{{{spreadsheet_ns}}}pane",
        {
            "ySplit": "1",
            "topLeftCell": "A2",
            "activePane": "bottomLeft",
            "state": "frozen",
        },
    )
    columns = ET.SubElement(worksheet, f"{{{spreadsheet_ns}}}cols")
    ET.SubElement(
        columns,
        f"{{{spreadsheet_ns}}}col",
        {
            "min": "1",
            "max": str(max(1, len(headers))),
            "width": "18",
            "customWidth": "1",
        },
    )
    sheet_data = ET.SubElement(worksheet, f"{{{spreadsheet_ns}}}sheetData")
    for row_number, values in enumerate(matrix, 1):
        row_element = ET.SubElement(
            sheet_data, f"{{{spreadsheet_ns}}}row", {"r": str(row_number)}
        )
        for column_number, value in enumerate(values, 1):
            cell = ET.SubElement(
                row_element,
                f"{{{spreadsheet_ns}}}c",
                {
                    "r": f"{xlsx_column_name(column_number)}{row_number}",
                    "t": "inlineStr",
                    "s": "1" if row_number == 1 else "0",
                },
            )
            inline = ET.SubElement(cell, f"{{{spreadsheet_ns}}}is")
            text = ET.SubElement(inline, f"{{{spreadsheet_ns}}}t")
            if value[:1].isspace() or value[-1:].isspace() or "\n" in value:
                text.set(
                    "{http://www.w3.org/XML/1998/namespace}space", "preserve"
                )
            text.text = value
    last_cell = (
        f"{xlsx_column_name(max(1, len(headers)))}{max(1, len(matrix))}"
    )
    ET.SubElement(
        worksheet, f"{{{spreadsheet_ns}}}autoFilter", {"ref": f"A1:{last_cell}"}
    )
    table_parts = ET.SubElement(
        worksheet, f"{{{spreadsheet_ns}}}tableParts", {"count": "1"}
    )
    ET.SubElement(
        table_parts,
        f"{{{spreadsheet_ns}}}tablePart",
        {f"{{{relationship_ns}}}id": "rId1"},
    )
    table = ET.Element(
        f"{{{spreadsheet_ns}}}table",
        {
            "id": "1",
            "name": table_name,
            "displayName": table_name,
            "ref": f"A1:{last_cell}",
            "totalsRowShown": "0",
        },
    )
    ET.SubElement(
        table, f"{{{spreadsheet_ns}}}autoFilter", {"ref": f"A1:{last_cell}"}
    )
    table_columns = ET.SubElement(
        table,
        f"{{{spreadsheet_ns}}}tableColumns",
        {"count": str(len(headers))},
    )
    for index, name in enumerate(headers, 1):
        ET.SubElement(
            table_columns,
            f"{{{spreadsheet_ns}}}tableColumn",
            {"id": str(index), "name": name},
        )
    ET.SubElement(
        table,
        f"{{{spreadsheet_ns}}}tableStyleInfo",
        {
            "name": "TableStyleMedium2",
            "showFirstColumn": "0",
            "showLastColumn": "0",
            "showRowStripes": "1",
            "showColumnStripes": "0",
        },
    )
    entries = {
        "[Content_Types].xml": b"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>
<Override PartName="/xl/tables/table1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.table+xml"/>
</Types>""",
        "_rels/.rels": b"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>""",
        "xl/workbook.xml": f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
<sheets><sheet name="{sheet_name}" sheetId="1" r:id="rId1"/></sheets>
</workbook>""".encode(),
        "xl/_rels/workbook.xml.rels": b"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
</Relationships>""",
        "xl/styles.xml": b"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
<fonts count="2"><font><sz val="11"/><name val="Calibri"/></font><font><b/><sz val="11"/><name val="Calibri"/></font></fonts>
<fills count="2"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill></fills>
<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>
<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
<cellXfs count="2"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/><xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0"/></cellXfs>
<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>
</styleSheet>""",
        "xl/worksheets/sheet1.xml": xml_bytes(worksheet),
        "xl/worksheets/_rels/sheet1.xml.rels": b"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/table" Target="../tables/table1.xml"/>
</Relationships>""",
        "xl/tables/table1.xml": xml_bytes(table),
    }
    output = io.BytesIO()
    with zipfile.ZipFile(
        output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
    ) as archive:
        for name in sorted(entries):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, entries[name])
    return output.getvalue()


def readme_bytes() -> bytes:
    return b"""# Betriebszeitung TRO \xe2\x80\x93 Der Transformator

Unit ID: `trafo`

Status: unsealed accession package.

This filesystem-readable unit contains 1590 newspaper objects, 10377
metadata-enriched TIFF preservation pages, four exact-PDF preservation
fallbacks, and 1591 access PDFs.

The 42 issues from 1988 were added from 294 locally held TIFF scans. They were
absent from both the accepted museum-digital capture and the local metadata
table. Their exact issue dates, issue numbers, and Jahrgang statements were
subsequently confirmed through curator review of the local issue mastheads.
Rights and metadata licence remain explicitly unresolved. The accepted TIFF
masters and access PDFs are immutable and were not rewritten or rehashed by
this catalog-only enrichment. The 1988 access PDFs remain generated
derivatives from the seven TIFF scan files per issue; the combined scan of
printed pages 4 and 5 remains one PDF page.

Three earlier TIFF source pages were confirmed as genuine source losses during
the original accession and are represented by their exact issue PDFs. The
damaged byte streams are not retained in this final unit. Those loss decisions
and original intake identities remain recorded in
`documents/source-losses.json`.

TIFF metadata readback, technical structure, decoded pixels, and the
post-enrichment hashes were verified during their original staging. Existing
accepted media were not rewritten or rehashed by later catalog enrichment.

Restore state and complete fixity information are under `db-restore/`.
"""


def database_rows(
    connection: sqlite3.Connection, query: str
) -> list[dict[str, Any]]:
    connection.row_factory = sqlite3.Row
    return [dict(row) for row in connection.execute(query)]


def build_checksums() -> tuple[list[dict[str, Any]], dict[str, str]]:
    old_rows = read_csv(FINAL / "db-restore/checksums.csv")
    old_by_path = {row["path"]: row for row in old_rows}
    if len(old_by_path) != len(old_rows):
        raise PublishError("existing checksum paths are not unique")
    new_hashes: dict[str, str] = {}
    for relative in HASHED_REPLACEMENTS:
        staged = STAGE / relative
        digest = sha256_file(staged)
        new_hashes[relative] = f"sha256:{digest}"
        old_by_path[relative] = {
            "path": relative,
            "sha256": new_hashes[relative],
            "size_bytes": str(staged.stat().st_size),
        }
    rows = [old_by_path[path] for path in sorted(old_by_path)]
    write_csv(
        STAGE / "db-restore/checksums.csv",
        rows,
        ["path", "sha256", "size_bytes"],
    )
    return rows, new_hashes


def stage() -> dict[str, Any]:
    assert_allowed_paths()
    dry_plan, dry_validation = assert_dry_run()
    if RUN_ROOT.exists():
        raise PublishError(f"refusing existing run root: {RUN_ROOT}")
    RUN_ROOT.mkdir(parents=True)
    baseline = assert_baseline(dry_plan)
    (STAGE / "db-restore").mkdir(parents=True)
    shutil.copy2(DRY_DB, STAGE / "db-restore/RESTORE.sqlite")
    database = sqlite3.connect(
        f"file:{STAGE / 'db-restore/RESTORE.sqlite'}?mode=ro&immutable=1",
        uri=True,
    )
    try:
        catalog = database_rows(
            database, "SELECT * FROM archive_object ORDER BY inventory"
        )
        if database.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise PublishError("staged database integrity differs")
        sql_dump = "\n".join(database.iterdump()) + "\n"
    finally:
        database.close()
    if len(catalog) != 1590:
        raise PublishError(f"staged catalog count differs: {len(catalog)}")
    write_csv(STAGE / "CATALOG.csv", catalog, CATALOG_FIELDS)
    replace_bytes(
        STAGE / "CATALOG.xlsx",
        table_xlsx_bytes(
            CATALOG_FIELDS,
            [{field: str(row.get(field, "")) for field in CATALOG_FIELDS} for row in catalog],
            sheet_name="Trafo",
            table_name="TrafoCatalog",
        ),
    )
    replace_bytes(STAGE / "README.md", readme_bytes())
    replace_bytes(STAGE / "db-restore/RESTORE.sql", sql_dump.encode("utf-8"))
    checksum_rows, new_hashes = build_checksums()
    staged_identities = [
        relative_identity(STAGE, relative) for relative in REPLACED_FILES
    ]
    plan = {
        "format": "iss-trafo-metadata-enrichment-v1",
        "run_id": RUN_ID,
        "created_at": now(),
        "status": "staged-not-published",
        "decision_sha256": DECISION_SHA256,
        "dry_run": str(DRY_RUN),
        "dry_run_status": dry_validation["status"],
        "scope": "TRO-1988-001 through TRO-1988-042",
        "replaced_files": REPLACED_FILES,
        "hashed_replacements": HASHED_REPLACEMENTS,
        "same_filesystem_device": (FINAL / "db-restore/RESTORE.sqlite").stat().st_dev,
        "baseline_metadata_identity": baseline,
        "staged_metadata_identity": staged_identities,
        "staged_metadata_hashes": new_hashes,
        "checksum_rows": len(checksum_rows),
        "media_io": {
            "representation_files_opened": 0,
            "representation_files_copied": 0,
            "representation_files_hashed": 0,
            "unchanged_checksum_rows_reused": len(checksum_rows)
            - len(HASHED_REPLACEMENTS),
            "new_metadata_files_hashed_once": len(HASHED_REPLACEMENTS),
            "destination_rehash_planned": False,
        },
        "write_whitelist": {
            "work_root": str(RUN_ROOT),
            "final_files": [str(FINAL / relative) for relative in REPLACED_FILES],
        },
    }
    replace_json(PLAN, plan)
    replace_json(
        PUBLICATION,
        {"run_id": RUN_ID, "status": "staged", "updated_at": now()},
    )
    return validate_staged()


def validate_catalog_view(root: Path) -> dict[str, Any]:
    catalog = read_csv(root / "CATALOG.csv")
    if len(catalog) != 1590:
        raise PublishError("catalog object count differs")
    catalog_1988 = [
        row for row in catalog if row["inventory"].startswith("TRO-1988-")
    ]
    if [row["inventory"] for row in catalog_1988] != EXPECTED:
        raise PublishError("1988 catalog coverage differs")
    if any(
        row["date_created"] == "1988"
        or row["metadata_source"]
        != "local-source-masthead-curator-reviewed-no-md-record"
        or row["rights_usage_terms"] != "unknown"
        or row["metadata_license"] != "not supplied"
        or row["dimensions"]
        or row["material_technique"]
        for row in catalog_1988
    ):
        raise PublishError("1988 enrichment or preservation policy differs")
    with zipfile.ZipFile(root / "CATALOG.xlsx") as archive:
        if archive.testzip() is not None:
            raise PublishError("catalog XLSX integrity differs")
        sheet = archive.read("xl/worksheets/sheet1.xml")
        if sheet.count(b"<row") != 1591:
            raise PublishError("catalog XLSX row count differs")
    database = sqlite3.connect(
        f"file:{root / 'db-restore/RESTORE.sqlite'}?mode=ro&immutable=1",
        uri=True,
    )
    try:
        integrity = database.execute("PRAGMA integrity_check").fetchone()[0]
        objects = database.execute("SELECT count(*) FROM archive_object").fetchone()[0]
        representations = database.execute(
            "SELECT count(*) FROM representation"
        ).fetchone()[0]
        units = database.execute("SELECT count(*) FROM unit").fetchone()[0]
        db_1988 = database.execute(
            "SELECT inventory, title, description, date_created, metadata_source "
            "FROM archive_object WHERE inventory LIKE 'TRO-1988-%' "
            "ORDER BY inventory"
        ).fetchall()
    finally:
        database.close()
    if integrity != "ok" or (objects, representations, units) != (1590, 11972, 1):
        raise PublishError("restore database validation differs")
    if len(db_1988) != 42:
        raise PublishError("restore database 1988 coverage differs")
    if [
        (row["inventory"], row["title"], row["description"], row["date_created"], row["metadata_source"])
        for row in catalog_1988
    ] != db_1988:
        raise PublishError("catalog CSV and restore database differ")
    sql_memory = sqlite3.connect(":memory:")
    try:
        sql_memory.executescript(
            (root / "db-restore/RESTORE.sql").read_text(encoding="utf-8")
        )
        sql_counts = (
            sql_memory.execute("SELECT count(*) FROM archive_object").fetchone()[0],
            sql_memory.execute("SELECT count(*) FROM representation").fetchone()[0],
        )
        sql_1988 = sql_memory.execute(
            "SELECT inventory, title, description, date_created, metadata_source "
            "FROM archive_object WHERE inventory LIKE 'TRO-1988-%' "
            "ORDER BY inventory"
        ).fetchall()
    finally:
        sql_memory.close()
    if sql_counts != (1590, 11972) or sql_1988 != db_1988:
        raise PublishError("restore SQL replay differs")
    return {
        "objects": objects,
        "representations": representations,
        "units": units,
        "1988_objects": len(catalog_1988),
        "sqlite_integrity": integrity,
        "xlsx_rows": 1591,
        "sql_replay": "ok",
    }


def validate_staged() -> dict[str, Any]:
    if not PLAN.is_file():
        raise PublishError("stage plan unavailable")
    plan = read_json(PLAN)
    baseline = assert_baseline(read_json(DRY_PLAN))
    catalog_validation = validate_catalog_view(STAGE)
    old_checksums = read_csv(FINAL / "db-restore/checksums.csv")
    new_checksums = read_csv(STAGE / "db-restore/checksums.csv")
    old_by_path = {row["path"]: row for row in old_checksums}
    new_by_path = {row["path"]: row for row in new_checksums}
    if len(old_checksums) != 25540 or len(new_checksums) != 25540:
        raise PublishError("checksum row count differs")
    if set(old_by_path) != set(new_by_path):
        raise PublishError("checksum path coverage differs")
    changed_checksum_paths = sorted(
        path for path in old_by_path if old_by_path[path] != new_by_path[path]
    )
    if changed_checksum_paths != sorted(HASHED_REPLACEMENTS):
        raise PublishError(
            f"checksum changes differ: {changed_checksum_paths}"
        )
    for relative in HASHED_REPLACEMENTS:
        row = new_by_path[relative]
        if row["sha256"] != plan["staged_metadata_hashes"][relative]:
            raise PublishError(f"staged checksum digest differs: {relative}")
        if int(row["size_bytes"]) != (STAGE / relative).stat().st_size:
            raise PublishError(f"staged checksum size differs: {relative}")
    result = {
        "run_id": RUN_ID,
        "validated_at": now(),
        "status": "ok",
        "catalog": catalog_validation,
        "checksum_rows": len(new_checksums),
        "changed_checksum_paths": changed_checksum_paths,
        "unchanged_checksum_rows_reused": len(new_checksums)
        - len(changed_checksum_paths),
        "final_baseline_identity_unchanged": baseline
        == plan["baseline_metadata_identity"],
        "media_files_opened": 0,
        "media_files_hashed": 0,
        "metadata_files_hashed_once": len(HASHED_REPLACEMENTS),
        "destination_rehash_planned": False,
    }
    replace_json(VALIDATION, result)
    replace_json(
        PUBLICATION,
        {"run_id": RUN_ID, "status": "validated", "updated_at": now()},
    )
    return result


def backup_metadata() -> list[dict[str, Any]]:
    ROLLBACK.mkdir(parents=True, exist_ok=False)
    backups = []
    for relative in REPLACED_FILES:
        source = FINAL / relative
        destination = ROLLBACK / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        with destination.open("rb") as handle:
            os.fsync(handle.fileno())
        source_identity = relative_identity(FINAL, relative)
        backup_identity = relative_identity(ROLLBACK, relative)
        if source_identity["size_bytes"] != backup_identity["size_bytes"]:
            raise PublishError(f"rollback backup size differs: {relative}")
        backups.append(
            {
                "relative_path": relative,
                "source_identity": source_identity,
                "backup_identity": backup_identity,
                "verification": "same-size-copy-no-rehash",
            }
        )
    replace_json(
        RUN_ROOT / "rollback-manifest.json",
        {
            "run_id": RUN_ID,
            "created_at": now(),
            "files": backups,
            "media_files_copied": 0,
            "media_files_hashed": 0,
        },
    )
    return backups


def restore_backups(reason: str) -> None:
    if not ROLLBACK.is_dir():
        raise PublishError("rollback directory unavailable")
    for relative in REPLACED_FILES:
        backup = ROLLBACK / relative
        if not backup.is_file():
            raise PublishError(f"rollback file unavailable: {relative}")
        destination = FINAL / relative
        temporary = destination.with_name(f".{destination.name}.rollback.tmp")
        shutil.copy2(backup, temporary)
        with temporary.open("rb") as handle:
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    os.sync()
    replace_json(
        PUBLICATION,
        {
            "run_id": RUN_ID,
            "status": "rolled-back",
            "rolled_back_at": now(),
            "reason": reason,
            "media_files_rewritten": False,
        },
    )


def publish() -> dict[str, Any]:
    assert_allowed_paths()
    if not VALIDATION.is_file() or read_json(VALIDATION).get("status") != "ok":
        raise PublishError("validated stage unavailable")
    plan = read_json(PLAN)
    assert_baseline(read_json(DRY_PLAN))
    backups = backup_metadata()
    staged_identity = {
        row["relative_path"]: row for row in plan["staged_metadata_identity"]
    }
    replace_json(
        PUBLICATION,
        {
            "run_id": RUN_ID,
            "status": "publishing",
            "started_at": now(),
            "replacement_count": len(REPLACED_FILES),
            "media_files_rewritten": False,
        },
    )
    try:
        for relative in REPLACED_FILES:
            source = STAGE / relative
            destination = FINAL / relative
            if not source.is_file() or source.is_symlink():
                raise PublishError(f"staged replacement unavailable: {relative}")
            os.replace(source, destination)
        os.sync()
        for relative in REPLACED_FILES:
            observed = relative_identity(FINAL, relative)
            expected = staged_identity[relative]
            for field in ("device", "inode", "size_bytes"):
                if observed[field] != expected[field]:
                    raise PublishError(
                        f"published identity differs: {relative} {field}"
                    )
        final_catalog = validate_catalog_view(FINAL)
        final_checksums = read_csv(FINAL / "db-restore/checksums.csv")
        final_by_path = {row["path"]: row for row in final_checksums}
        for relative, digest in plan["staged_metadata_hashes"].items():
            if final_by_path[relative]["sha256"] != digest:
                raise PublishError(
                    f"published checksum ledger differs: {relative}"
                )
        current_identity = all_metadata_identity()
        before_by_path = {
            row["relative_path"]: row for row in plan["baseline_metadata_identity"]
        }
        after_by_path = {row["relative_path"]: row for row in current_identity}
        unexpected_changes = [
            relative
            for relative in UNCHANGED_METADATA_FILES
            if before_by_path[relative] != after_by_path[relative]
        ]
        if unexpected_changes:
            raise PublishError(
                f"unchanged metadata identities differ: {unexpected_changes}"
            )
        result = {
            "run_id": RUN_ID,
            "status": "published",
            "completed_at": now(),
            "metadata_replaced": len(REPLACED_FILES),
            "catalog": final_catalog,
            "checksum_rows": len(final_checksums),
            "unchanged_metadata_identity": "unchanged",
            "media_files_opened": 0,
            "media_files_copied": 0,
            "media_files_hashed": 0,
            "destination_files_rehashed": 0,
            "publication_verification": "same-filesystem-device-inode-size",
            "rollback_files": len(backups),
            "rollback_root": str(ROLLBACK),
        }
        replace_json(FINAL_VALIDATION, result)
        replace_json(PUBLICATION, result)
        return result
    except Exception as error:
        restore_backups(f"automatic rollback after publication error: {error}")
        raise


def rollback() -> dict[str, Any]:
    restore_backups("explicit rollback")
    result = {
        "run_id": RUN_ID,
        "status": "rolled-back",
        "rolled_back_at": now(),
        "metadata_restored": len(REPLACED_FILES),
        "media_files_rewritten": False,
    }
    replace_json(PUBLICATION, result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("stage", "validate", "publish", "rollback"))
    arguments = parser.parse_args()
    if arguments.command == "stage":
        result = stage()
    elif arguments.command == "validate":
        result = validate_staged()
    elif arguments.command == "publish":
        result = publish()
    else:
        result = rollback()
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.dont_write_bytecode = True
    raise SystemExit(main())
