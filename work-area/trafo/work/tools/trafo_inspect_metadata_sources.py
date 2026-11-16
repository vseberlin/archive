#!/usr/bin/env python3
"""Inspect shortlisted Trafo metadata sources without hashing or modifying them."""

from __future__ import annotations

import csv
import io
import json
import re
import zipfile
from collections import Counter
from pathlib import Path
from xml.etree import ElementTree as ET


RUN_DIR = Path(
    "/mnt/volume/iss-archive-work/trafo/work/deep-search/"
    "trafo-documentation-usb-20260727-001"
)
REVIEW_DIGEST = RUN_DIR / "review-digest.csv"
EMBEDDED_SOURCES = RUN_DIR / "embedded-source-members.csv"
OUTPUT = RUN_DIR / "metadata-source-structure.json"

NS_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
NS_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
NS_PKG_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
NS = {"m": NS_MAIN, "r": NS_REL, "pr": NS_PKG_REL}


def xml_root(zf: zipfile.ZipFile, member: str) -> ET.Element:
    return ET.fromstring(zf.read(member))


def shared_strings(zf: zipfile.ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in zf.namelist():
        return []
    root = xml_root(zf, "xl/sharedStrings.xml")
    values: list[str] = []
    for item in root.findall("m:si", NS):
        values.append("".join(node.text or "" for node in item.iter(f"{{{NS_MAIN}}}t")))
    return values


def cell_value(cell: ET.Element, strings: list[str]) -> str:
    cell_type = cell.get("t", "")
    if cell_type == "inlineStr":
        return "".join(node.text or "" for node in cell.iter(f"{{{NS_MAIN}}}t"))
    value = cell.find("m:v", NS)
    if value is None or value.text is None:
        formula = cell.find("m:f", NS)
        return f"={formula.text}" if formula is not None and formula.text else ""
    if cell_type == "s":
        try:
            return strings[int(value.text)]
        except (IndexError, ValueError):
            return value.text
    if cell_type == "b":
        return "TRUE" if value.text == "1" else "FALSE"
    return value.text


def column_number(reference: str) -> int:
    letters = re.match(r"[A-Z]+", reference.upper())
    if not letters:
        return 0
    number = 0
    for char in letters.group(0):
        number = number * 26 + ord(char) - ord("A") + 1
    return number


def inspect_xlsx_bytes(payload: bytes) -> dict:
    with zipfile.ZipFile(io.BytesIO(payload)) as zf:
        strings = shared_strings(zf)
        workbook = xml_root(zf, "xl/workbook.xml")
        rels = xml_root(zf, "xl/_rels/workbook.xml.rels")
        targets = {
            rel.get("Id"): rel.get("Target", "")
            for rel in rels.findall("pr:Relationship", NS)
        }
        sheets = []
        for sheet in workbook.findall("m:sheets/m:sheet", NS):
            relation_id = sheet.get(f"{{{NS_REL}}}id", "")
            target = targets.get(relation_id, "")
            member = target.lstrip("/")
            if not member.startswith("xl/"):
                member = f"xl/{member}"
            root = xml_root(zf, member)
            dimension = root.find("m:dimension", NS)
            rows = root.findall("m:sheetData/m:row", NS)
            sample_rows: list[list[str]] = []
            for row in rows[:12]:
                values: list[str] = []
                for cell in row.findall("m:c", NS):
                    col = column_number(cell.get("r", ""))
                    while len(values) < col - 1:
                        values.append("")
                    values.append(cell_value(cell, strings).strip())
                if any(values):
                    sample_rows.append(values[:35])
            sheets.append(
                {
                    "name": sheet.get("name", ""),
                    "dimension": dimension.get("ref", "") if dimension is not None else "",
                    "xml_row_count": len(rows),
                    "sample_rows": sample_rows,
                }
            )
        return {
            "format": "xlsx",
            "condition": "readable",
            "sheets": sheets,
        }


def inspect_xlsx(path: Path) -> dict:
    try:
        return inspect_xlsx_bytes(path.read_bytes())
    except Exception as exc:
        return {
            "format": "xlsx",
            "condition": "unreadable",
            "error": f"{type(exc).__name__}: {exc}",
        }


def inspect_csv(path: Path) -> dict:
    payload = path.read_bytes()
    decoded = None
    encoding = None
    for candidate in ("utf-8-sig", "utf-16", "cp1252", "latin-1"):
        try:
            decoded = payload.decode(candidate)
            encoding = candidate
            break
        except UnicodeDecodeError:
            continue
    if decoded is None:
        raise RuntimeError("No supported text encoding")
    sample = decoded[:65536]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
        delimiter = dialect.delimiter
    except csv.Error:
        delimiter = ";"
    rows = list(csv.reader(io.StringIO(decoded), delimiter=delimiter))
    return {
        "format": "csv",
        "condition": "readable",
        "encoding": encoding,
        "delimiter": delimiter,
        "row_count": len(rows),
        "sample_rows": [row[:35] for row in rows[:12]],
    }


def inspect_export_zip(path: Path) -> dict:
    with zipfile.ZipFile(path) as zf:
        members = [item for item in zf.infolist() if not item.is_dir()]
        extensions = Counter(Path(item.filename).suffix.lower() or "[none]" for item in members)
        names = [item.filename for item in members]
        return {
            "format": "zip",
            "condition": "readable",
            "member_count": len(members),
            "member_extensions": dict(sorted(extensions.items())),
            "sample_members": names[:20],
        }


def main() -> None:
    selected = []
    with REVIEW_DIGEST.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            if row["priority"] == "1":
                selected.append(row)

    results = []
    for row in selected:
        path = Path(row["path"])
        if path.suffix.lower() == ".xlsx":
            structure = inspect_xlsx(path)
        elif path.suffix.lower() == ".csv":
            structure = inspect_csv(path)
        elif path.suffix.lower() == ".zip":
            structure = inspect_export_zip(path)
        else:
            structure = {
                "format": path.suffix.lower().lstrip("."),
                "condition": "not-inspected",
            }
        results.append(
            {
                "source_kind": "direct-file",
                "path": str(path),
                "file_name": path.name,
                "size_bytes": path.stat().st_size,
                "classification": row["classification"],
                "structure": structure,
            }
        )

    with EMBEDDED_SOURCES.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            archive = Path(row["archive"])
            with zipfile.ZipFile(archive) as zf:
                payload = zf.read(row["member"])
            results.append(
                {
                    "source_kind": "embedded-member",
                    "path": f"{archive}!/{row['member']}",
                    "file_name": Path(row["member"]).name,
                    "size_bytes": len(payload),
                    "classification": "embedded-source-container-reference",
                    "structure": inspect_xlsx_bytes(payload),
                }
            )

    OUTPUT.write_text(
        json.dumps(
            {
                "method": (
                    "Structural inspection only: filenames, ZIP directory/XML, and "
                    "spreadsheet/CSV cells; no cryptographic hashing"
                ),
                "source_count": len(results),
                "sources": results,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(json.dumps({"output": str(OUTPUT), "source_count": len(results)}))


if __name__ == "__main__":
    main()
