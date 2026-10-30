#!/usr/bin/env python3
"""Build verified Paragon source/MD/catalog document evidence bundles."""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import shutil
import sys
import xml.etree.ElementTree as ET
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


FORMAT = "iss-paragon-document-bundle-v1"
DEFAULT_SUFFIXES = {
    ".csv",
    ".doc",
    ".docx",
    ".pdf",
    ".xls",
    ".xlsx",
    ".xml",
    ".zip",
}
IGNORED_WORKFLOW_SUFFIXES = {".doc", ".docx", ".pdf"}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def xml_bytes(element: ET.Element) -> bytes:
    return ET.tostring(element, encoding="utf-8", xml_declaration=True)


def xlsx_column_name(index: int) -> str:
    result = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        result = chr(65 + remainder) + result
    return result


def table_xlsx_bytes(
    headers: list[str],
    rows: list[dict[str, str]],
    *,
    sheet_name: str = "Data",
    table_name: str = "DataTable",
) -> bytes:
    matrix = [headers]
    matrix.extend([[row.get(header, "") for header in headers] for row in rows])
    spreadsheet_ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    relationship_ns = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    ET.register_namespace("", spreadsheet_ns)
    ET.register_namespace("r", relationship_ns)

    worksheet = ET.Element(f"{{{spreadsheet_ns}}}worksheet")
    views = ET.SubElement(worksheet, f"{{{spreadsheet_ns}}}sheetViews")
    view = ET.SubElement(
        views,
        f"{{{spreadsheet_ns}}}sheetView",
        {"workbookViewId": "0"},
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
            sheet_data,
            f"{{{spreadsheet_ns}}}row",
            {"r": str(row_number)},
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
            value = str(value)
            if value[:1].isspace() or value[-1:].isspace() or "\n" in value:
                text.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
            text.text = value
    last_cell = f"{xlsx_column_name(max(1, len(headers)))}{max(1, len(matrix))}"
    ET.SubElement(
        worksheet,
        f"{{{spreadsheet_ns}}}autoFilter",
        {"ref": f"A1:{last_cell}"},
    )
    table_parts = ET.SubElement(
        worksheet,
        f"{{{spreadsheet_ns}}}tableParts",
        {"count": "1"},
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
        table,
        f"{{{spreadsheet_ns}}}autoFilter",
        {"ref": f"A1:{last_cell}"},
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
</workbook>""".encode("utf-8"),
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
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name in sorted(entries):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, entries[name])
    return output.getvalue()


def collect_source_documents(roots: list[Path]) -> list[Path]:
    paths: list[Path] = []
    for root in roots:
        for path in root.rglob("*"):
            if path.is_file() and path.suffix.casefold() in DEFAULT_SUFFIXES:
                paths.append(path.resolve(strict=True))
    return sorted(paths, key=lambda item: str(item))


def relative_source_name(path: Path, roots: list[Path]) -> str:
    for index, root in enumerate(roots, start=1):
        try:
            relative = path.relative_to(root)
        except ValueError:
            continue
        return f"source-{index:02d}/{relative.as_posix()}"
    return path.name


def write_manifest_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="raise")
        writer.writeheader()
        writer.writerows(rows)


def read_md_final_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle, delimiter=";")
        headers = [str(item or "") for item in (reader.fieldnames or [])]
        rows = [
            {header: str(raw.get(header) or "").strip() for header in headers}
            for raw in reader
        ]
    return headers, rows


def md_csv_bytes(headers: list[str], rows: list[dict[str, str]]) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(
        output,
        fieldnames=headers,
        delimiter=";",
        quoting=csv.QUOTE_MINIMAL,
        lineterminator="\r\n",
        extrasaction="raise",
    )
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue().encode("utf-8-sig")


def copy_verified(source: Path, destination: Path) -> dict[str, Any]:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise FileExistsError(f"refusing to replace existing file: {destination}")
    shutil.copy2(source, destination)
    source_digest = sha256_file(source)
    destination_digest = sha256_file(destination)
    if source_digest != destination_digest:
        raise RuntimeError(f"copy digest mismatch: {source} -> {destination}")
    return {
        "path": str(destination),
        "sha256": "sha256:" + destination_digest,
        "size_bytes": destination.stat().st_size,
    }


def build_bundle(
    roots: list[Path],
    output: Path,
    md_final_csv: Path,
    md_xml_zip: Path,
    catalog_csv: Path,
    catalog_xlsx: Path,
) -> dict[str, Any]:
    output.mkdir(parents=True, exist_ok=False)
    docs_dir = output / "documents"
    docs_dir.mkdir()
    manifests_dir = output / "manifests"
    manifests_dir.mkdir()

    source_paths = collect_source_documents(roots)
    manifest_rows: list[dict[str, Any]] = []
    zip_path = docs_dir / "paragon-source-documents.zip"
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in source_paths:
            arcname = relative_source_name(path, roots)
            stat = path.stat()
            digest = sha256_file(path)
            workflow_scope = (
                "ignored_document_noise"
                if path.suffix.casefold() in IGNORED_WORKFLOW_SUFFIXES
                else "metadata_or_export_evidence"
            )
            manifest_rows.append(
                {
                    "archive_member": arcname,
                    "source_path": str(path),
                    "filename": path.name,
                    "suffix": path.suffix.casefold(),
                    "workflow_scope": workflow_scope,
                    "size_bytes": stat.st_size,
                    "mtime_utc": datetime.fromtimestamp(stat.st_mtime, UTC)
                    .isoformat()
                    .replace("+00:00", "Z"),
                    "sha256": "sha256:" + digest,
                }
            )
            info = zipfile.ZipInfo(arcname)
            info.date_time = datetime.fromtimestamp(stat.st_mtime).timetuple()[:6]
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            with path.open("rb") as handle:
                archive.writestr(info, handle.read())
        manifest_payload = io.StringIO(newline="")
        writer = csv.DictWriter(
            manifest_payload,
            fieldnames=[
                "archive_member",
                "source_path",
                "filename",
                "suffix",
                "workflow_scope",
                "size_bytes",
                "mtime_utc",
                "sha256",
            ],
        )
        writer.writeheader()
        writer.writerows(manifest_rows)
        info = zipfile.ZipInfo("MANIFEST.csv", date_time=(1980, 1, 1, 0, 0, 0))
        info.compress_type = zipfile.ZIP_DEFLATED
        info.external_attr = 0o100644 << 16
        archive.writestr(info, manifest_payload.getvalue().encode("utf-8"))

    write_manifest_csv(
        manifests_dir / "source-documents-manifest.csv",
        manifest_rows,
        [
            "archive_member",
            "source_path",
            "filename",
            "suffix",
            "workflow_scope",
            "size_bytes",
            "mtime_utc",
            "sha256",
        ],
    )

    md_headers, md_rows = read_md_final_csv(md_final_csv)
    md_csv_payload = md_csv_bytes(md_headers, md_rows)
    md_xlsx_payload = table_xlsx_bytes(
        md_headers,
        md_rows,
        sheet_name="MD export",
        table_name="MuseumDigitalExport",
    )
    md_csv_out = docs_dir / "md-export.csv"
    md_xlsx_out = docs_dir / "md-export.xlsx"
    md_csv_out.write_bytes(md_csv_payload)
    md_xlsx_out.write_bytes(md_xlsx_payload)
    md_xml_copy = copy_verified(md_xml_zip, docs_dir / "md-import-xml.zip")
    catalog_csv_copy = copy_verified(catalog_csv, docs_dir / "catalog.csv")
    catalog_xlsx_copy = copy_verified(catalog_xlsx, docs_dir / "catalog.xlsx")

    lido_dir = docs_dir / "md-lido-export"
    lido_dir.mkdir()
    (lido_dir / "README.md").write_text(
        "LIDO export not captured in this local run.\n\n"
        "Keep museum-digital LIDO XML exports here when fetched from the published "
        "museum-digital object/export list. Do not treat the local csv_xml.zip as "
        "LIDO; it is the museum-digital import XML form.\n",
        encoding="utf-8",
    )

    outputs = [
        {
            "role": "source_documents_zip",
            "path": str(zip_path),
            "sha256": "sha256:" + sha256_file(zip_path),
            "size_bytes": zip_path.stat().st_size,
            "source_rows": len(manifest_rows),
        },
        {
            "role": "md_export_csv",
            "path": str(md_csv_out),
            "sha256": "sha256:" + sha256_bytes(md_csv_payload),
            "size_bytes": len(md_csv_payload),
            "rows": len(md_rows),
        },
        {
            "role": "md_export_xlsx",
            "path": str(md_xlsx_out),
            "sha256": "sha256:" + sha256_bytes(md_xlsx_payload),
            "size_bytes": len(md_xlsx_payload),
            "rows": len(md_rows),
        },
        {"role": "md_import_xml_zip", **md_xml_copy},
        {"role": "catalog_csv", **catalog_csv_copy},
        {"role": "catalog_xlsx", **catalog_xlsx_copy},
    ]
    summary = {
        "format": FORMAT,
        "created_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "roots": [str(root) for root in roots],
        "source_document_files": len(manifest_rows),
        "ignored_document_noise_files": sum(
            1 for row in manifest_rows if row["workflow_scope"] == "ignored_document_noise"
        ),
        "md_export_rows": len(md_rows),
        "outputs": outputs,
        "lido_status": "not_captured",
        "lido_note": "Place fetched museum-digital LIDO XML exports in documents/md-lido-export/.",
    }
    (output / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    (output / "README.md").write_text(
        "# Paragon Document Evidence Bundle\n\n"
        f"Generated: {summary['created_at']}\n\n"
        "This bundle preserves source metadata/export documents, the consolidated "
        "museum-digital final export, the museum-digital import XML ZIP, and the "
        "current ISS catalog export. PDF/DOCX files are retained only as historical "
        "source evidence and marked as ignored workflow noise.\n\n"
        "## Important Boundary\n\n"
        "`documents/md-import-xml.zip` is the museum-digital import XML form, not "
        "LIDO. LIDO exports from museum-digital should be stored under "
        "`documents/md-lido-export/` once fetched.\n",
        encoding="utf-8",
    )
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", action="append", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--md-final-csv", required=True, type=Path)
    parser.add_argument("--md-xml-zip", required=True, type=Path)
    parser.add_argument("--catalog-csv", required=True, type=Path)
    parser.add_argument("--catalog-xlsx", required=True, type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    roots = [root.resolve(strict=True) for root in args.root]
    summary = build_bundle(
        roots=roots,
        output=args.output.resolve(),
        md_final_csv=args.md_final_csv.resolve(strict=True),
        md_xml_zip=args.md_xml_zip.resolve(strict=True),
        catalog_csv=args.catalog_csv.resolve(strict=True),
        catalog_xlsx=args.catalog_xlsx.resolve(strict=True),
    )
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
