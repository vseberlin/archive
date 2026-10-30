#!/usr/bin/env python3
"""Finish the WF-Sender unit and build a non-authorizing USB drain list.

The source medium is read-only. The mixed-project WordPress and Lightroom
trees are excluded from this closeout and remain subjects of separate review.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import zipfile
from collections import Counter
from pathlib import Path
from typing import Any, Iterable


TOOLS = Path(__file__).resolve().parent
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import paragon_document_bundle  # noqa: E402


UNIT = Path("/mnt/volume/iss-archive/units/wf-sender")
WORK = Path("/mnt/volume/iss-archive-work/wf-sender")
SOURCE_MOUNT = Path("/media/vladimir/Elements")
SOURCE_ROOT = SOURCE_MOUNT / "restore"
SOURCE_UUID = "6CAAB316AAB2DC30"
AUDIT = (
    WORK
    / "work/full-restore-audit/runs/"
    "20260729T094604Z-wf-sender-full-restore"
)
RUN = WORK / "work/drain-closeout/runs/wf-sender-closeout-001"
PROJECT = SOURCE_ROOT / "archive/WF/WF-Sender Projekt"
WEBSITE = PROJECT / "Webseite"
LIGHTROOM = SOURCE_ROOT / "archive/WF/Lightroom-Katalog_nur-WF"
GOOGLE = (
    SOURCE_ROOT
    / "archive/WF/GoogleDocsBackup/WF-Sender-20220713T123002Z-001.zip"
)

CATALOG_FIELDS = [
    "final_relative_path",
    "filename",
    "file_type",
    "carrier_key",
    "inventory",
    "role",
    "sequence",
    "title",
    "description",
    "date_text",
    "object_type",
    "md_object_ids",
    "md_object_urls",
    "relationship_state",
    "basis",
    "source_path",
    "source_sha256",
    "final_sha256",
    "size_bytes",
    "embedding_profile",
]
DISPOSITION_FIELDS = [
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
]
DOCUMENT_FIELDS = [
    "final_relative_path",
    "source_path",
    "description",
    "relationship",
    "condition",
    "source_sha256",
    "final_sha256",
    "size_bytes",
    "mtime_ns",
]
DRAIN_FIELDS = [
    "source_root_uuid",
    "source_relative_path",
    *DISPOSITION_FIELDS,
    "drain_reason",
]
CHECKSUM_FIELDS = ["path", "sha256", "size_bytes"]
MEMBER_FIELDS = [
    "final_container",
    "member",
    "size_bytes",
    "crc32",
    "sha256",
]


def src(relative: str) -> Path:
    return SOURCE_ROOT / relative


ADDITIONS = [
    {
        "source": GOOGLE,
        "final": "documents/metadata/google-docs/WF-Sender-20220713T123002Z-001.zip",
        "description": "Dedicated Google Docs export containing ten WF-Sender metadata workbooks.",
        "condition": "ZIP integrity passed; exact source bytes.",
    },
    {
        "source": src(
            "archive/WF/WF-Sender Projekt/Umbenennungen_WFS-1951-54-etc.txt"
        ),
        "final": "documents/metadata/inventory-rename-crosswalk.txt",
        "description": "Inventory rename and museum-digital link crosswalk.",
        "condition": "Readable original bytes.",
    },
    {
        "source": src(
            "archive/WF/WF-Sender Projekt/1-WFS-Tabelle_nur-Links.xlsx-2020-11-25.csv"
        ),
        "final": "documents/metadata/WFS-links-2020-11-25.csv",
        "description": "WF-Sender issue-link table exported for the historical website.",
        "condition": "Readable original bytes.",
    },
    {
        "source": src("archive/WF/WF-Sender Projekt/AG_Autoren-1950.xlsx"),
        "final": "documents/metadata/AG_Autoren-1950.xlsx",
        "description": "Author metadata for the 1950 WF-Sender issues.",
        "condition": "Readable original bytes.",
    },
    {
        "source": src("archive/WF/WF-Sender Projekt/WFS-1970.xlsx"),
        "final": "documents/metadata/WFS-1970.xlsx",
        "description": "Issue-level metadata working source for 1970 and 1972.",
        "condition": "Readable original bytes.",
    },
    {
        "source": src("archive/WF/WF-Sender Projekt/WFS-1972.xlsx"),
        "final": "documents/metadata/WFS-1972.xlsx",
        "description": "Detailed issue metadata source including 1972.",
        "condition": "Readable original bytes.",
    },
    {
        "source": src("archive/WF/WF-Sender Projekt/WFS-1970er.xlsx"),
        "final": "documents/metadata/WFS-1970er.xlsx",
        "description": "Consolidated 1970s WF-Sender metadata workbook.",
        "condition": "7-Zip integrity passed; nonstandard workbook container preserved exactly.",
    },
    {
        "source": src(
            "archive/WF/WF-Sender Projekt/Uploads/1957,1958/"
            "1957,1958_03_final.xlsx"
        ),
        "final": "documents/metadata/uploads/1957-1958-final.xlsx",
        "description": "Final 1957-1958 metadata workbook.",
        "condition": "Readable original bytes.",
    },
    {
        "source": src(
            "archive/WF/WF-Sender Projekt/Uploads/1957,1958,1959,1971/"
            "1957,1958,1959,1971_final.csv"
        ),
        "final": "documents/metadata/uploads/1957-1958-1959-1971-final.csv",
        "description": "Final CSV for the combined 1957, 1958, 1959 and 1971 metadata.",
        "condition": "Readable original bytes.",
    },
    {
        "source": src(
            "archive/WF/WF-Sender Projekt/Uploads/1957,1958,1959,1971/"
            "1957,1958,1959,1971_final.xlsx"
        ),
        "final": "documents/metadata/uploads/1957-1958-1959-1971-final.xlsx",
        "description": "Final workbook for the combined 1957, 1958, 1959 and 1971 metadata.",
        "condition": "Readable original bytes.",
    },
    {
        "source": src(
            "archive/WF/WF-Sender Projekt/Uploads/1959_1971/"
            "1959_1971_09-final.csv"
        ),
        "final": "documents/metadata/uploads/1959-1971-final.csv",
        "description": "Final CSV for the 1959 and 1971 article metadata.",
        "condition": "Readable original bytes.",
    },
    {
        "source": src(
            "archive/WF/WF-Sender Projekt/Uploads/1959_1971/"
            "1959_1971_09-final.xlsx"
        ),
        "final": "documents/metadata/uploads/1959-1971-final.xlsx",
        "description": "Final workbook for the 1959 and 1971 article metadata.",
        "condition": "Readable original bytes.",
    },
    {
        "source": src(
            "archive/WF/WF-Sender Projekt/Uploads/Spaltenschema-angepasst.xlsx"
        ),
        "final": "documents/metadata/uploads/column-schema.xlsx",
        "description": "Adjusted column schema used by the upload workflow.",
        "condition": "Readable original bytes.",
    },
    {
        "source": src(
            "archive/WF/WF-Sender Projekt/Uploads/2.Upload_02/csv_xml.zip"
        ),
        "final": "documents/metadata/import-packages/second-upload-csv-xml.zip",
        "description": "Museum-digital XML package for the second upload.",
        "condition": "ZIP integrity passed; exact source bytes.",
    },
    {
        "source": src(
            "archive/WF/WF-Sender Projekt/Uploads/1957,1958,1959,1971/csv_xml.zip"
        ),
        "final": "documents/metadata/import-packages/third-upload-csv-xml.zip",
        "description": "Museum-digital XML package for the third upload.",
        "condition": "ZIP integrity passed; exact source bytes.",
    },
    {
        "source": src(
            "archive/WF/WF-Sender Projekt/ARBEITSVERTRAG_WF-Sender_Digitalisierung.docx"
        ),
        "final": "documents/project/restricted/employment-contract-wf-sender.docx",
        "description": "Employment contract documenting staffing of the WF-Sender digitisation.",
        "condition": "Readable original bytes; contains personal information.",
    },
    {
        "source": src(
            "archive/WF/WF-Sender Projekt/Beauftragter zur Aufarbeitung der SED Diktatur/"
            "Finanzierungsplan, 3. Fassung_Abrechnung .docx"
        ),
        "final": "documents/project/restricted/financing-plan-accounting-source.docx",
        "description": "Editable accounting source for the final project financing plan.",
        "condition": "Readable original bytes; contains financial information.",
    },
    {
        "source": src(
            "archive/WF/WF-Sender Projekt/Stundenabrechnungen/Abrechnung Digi 2020.xlsx"
        ),
        "final": "documents/project/restricted/time-accounting-2020.xlsx",
        "description": "Project-level 2020 digitisation time-accounting summary.",
        "condition": "Readable original bytes; contains personal and financial information.",
    },
    {
        "source": src(
            "archive/WF/WF-Sender Projekt/Stundenabrechnungen/Abrechnung Digi 2020_SW.xlsx"
        ),
        "final": "documents/project/restricted/time-accounting-2020-sw.xlsx",
        "description": "Project-level 2020 WF-Sender time-accounting summary.",
        "condition": "Readable original bytes; contains personal and financial information.",
    },
    {
        "source": src(
            "archive/WF/WF-Sender Projekt/Rechnungen/MIK_Rechnung1.pdf"
        ),
        "final": "documents/scanning/MIK-invoice-1.pdf",
        "description": "First MIK scanning invoice for project 4008561.",
        "condition": "Readable original bytes; contains financial information.",
    },
    {
        "source": src(
            "archive/WF/WF-Sender Projekt/Rechnungen/MIK_Rechnung2_VRE12000576.PDF"
        ),
        "final": "documents/scanning/MIK-invoice-2.pdf",
        "description": "Second MIK scanning invoice for project 4008561.",
        "condition": "Readable original bytes; contains financial information.",
    },
]


class CloseoutError(RuntimeError):
    pass


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def csv_bytes(rows: Iterable[dict[str, Any]], fields: list[str]) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({field: row.get(field, "") for field in fields})
    return output.getvalue().encode("utf-8")


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    with temporary.open("xb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(8 * 1024 * 1024):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


class SourceHashCache:
    def __init__(self, path: Path):
        self.path = path
        self.rows: dict[str, tuple[int, int, str]] = {}
        if path.exists():
            with path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    if not line.strip():
                        continue
                    row = json.loads(line)
                    self.rows[row["path"]] = (
                        int(row["size_bytes"]),
                        int(row["mtime_ns"]),
                        row["sha256"],
                    )
        path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = path.open("a", encoding="utf-8")

    def hash(self, path: Path) -> str:
        stat = path.stat()
        saved = self.rows.get(str(path))
        if saved and saved[:2] == (stat.st_size, stat.st_mtime_ns):
            return saved[2]
        digest = sha256_file(path)
        row = {
            "path": str(path),
            "size_bytes": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
            "sha256": digest,
        }
        self.handle.write(
            json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n"
        )
        self.handle.flush()
        self.rows[str(path)] = (stat.st_size, stat.st_mtime_ns, digest)
        return digest

    def close(self) -> None:
        self.handle.close()


def mount_is_read_only() -> None:
    result = subprocess.run(
        ["findmnt", "-no", "UUID,OPTIONS", "--target", str(SOURCE_MOUNT)],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    ).stdout.strip()
    parts = result.split(None, 1)
    if len(parts) != 2 or parts[0].upper() != SOURCE_UUID:
        raise CloseoutError(f"unexpected source mount identity: {result}")
    options = set(parts[1].split(","))
    if "ro" not in options or "rw" in options:
        raise CloseoutError(f"source mount is not read-only: {result}")


def is_deferred_shared(path: str) -> bool:
    candidate = Path(path)
    return candidate == WEBSITE or WEBSITE in candidate.parents or (
        candidate == LIGHTROOM or LIGHTROOM in candidate.parents
    )


def is_cross_project(path: str, status: str = "") -> bool:
    if status == "excluded_cross_unit_trafo":
        return True
    markers = (
        "/PDF3/1953-Trafo/",
        "/pdf-Wasserzeichen-komprimiert/TRO-",
        "/Umbenannt_WFS-Sonderausgaben/TRO-",
        "/Scans_WF-Sender_MIK/Rechnungen/FEB-",
        "/Antrag-TRO_",
        "/Anlage_Angebot-Dienstleister_MIK-TRO.pdf",
        "/Uploads/1. Upload/TRO-Ausgaben_WF-Trafo-Flugblätter_05.xlsx",
        "/Rechnungen/Rechnung_20-10-0325_26.10.2020.pdf",
    )
    return any(marker in path for marker in markers)


def is_direct_wfs_tree(path: str) -> bool:
    prefixes = (
        str(PROJECT) + "/",
        str(
            SOURCE_ROOT
            / "archive/WF/Archivarien/Telefonverzeichnisse durchsuchbar/WF-Sender"
        )
        + "/",
        str(SOURCE_ROOT / "archive/WF/Namenssuche+WF-Sender/WF-Sender") + "/",
        str(SOURCE_ROOT / "archive/WF/Namenssuche+WF-Sender+FBs/WF-Sender")
        + "/",
        str(SOURCE_ROOT / "archive/WF/WF-Sender") + "/",
    )
    return any(path.startswith(prefix) for prefix in prefixes)


def is_workflow_noise(path: str) -> bool:
    name = Path(path).name
    lower = path.casefold()
    return (
        name.startswith("~$")
        or name in {"Thumbs.db", ".DS_Store"}
        or name.endswith((".tmp", ".crdownload"))
        or "/stundenabrechnungen/belege/" in lower
        or "/stundenabrechnungen/belege_s/" in lower
        or lower.endswith("/stundenabrechnungen/belege_s.zip")
    )


def selected_map() -> dict[str, dict[str, Any]]:
    return {str(row["source"]): row for row in ADDITIONS}


def addition_receipts(staging: Path) -> list[dict[str, Any]]:
    receipts = []
    for item in ADDITIONS:
        source = Path(item["source"])
        if not source.is_file():
            raise CloseoutError(f"selected source missing: {source}")
        final = str(item["final"])
        target = staging / final
        source_hash = sha256_file(source)
        if target.exists():
            if target.stat().st_size != source.stat().st_size:
                raise CloseoutError(f"staged size changed: {target}")
            if sha256_file(target) != source_hash:
                raise CloseoutError(f"staged hash changed: {target}")
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            with source.open("rb") as incoming, target.open("xb") as outgoing:
                shutil.copyfileobj(incoming, outgoing, 8 * 1024 * 1024)
                outgoing.flush()
                os.fsync(outgoing.fileno())
            shutil.copystat(source, target)
        final_hash = sha256_file(target)
        if final_hash != source_hash:
            raise CloseoutError(f"staged copy differs: {source}")
        stat = source.stat()
        receipts.append(
            {
                "final_relative_path": final,
                "source_path": str(source),
                "description": item["description"],
                "relationship": "direct",
                "condition": item["condition"],
                "source_sha256": source_hash,
                "final_sha256": final_hash,
                "size_bytes": str(stat.st_size),
                "mtime_ns": str(stat.st_mtime_ns),
                "role": "project_document",
            }
        )
    return receipts


def catalog_addition(receipt: dict[str, Any]) -> dict[str, str]:
    final = receipt["final_relative_path"]
    return {
        "final_relative_path": final,
        "filename": Path(final).name,
        "file_type": Path(final).suffix.lower(),
        "carrier_key": "",
        "inventory": "",
        "role": "project_document",
        "sequence": "",
        "title": receipt["description"],
        "description": receipt["description"],
        "date_text": "",
        "object_type": "project documentation",
        "md_object_ids": "",
        "md_object_urls": "",
        "relationship_state": "direct",
        "basis": "WF-Sender closeout appraisal",
        "source_path": receipt["source_path"],
        "source_sha256": receipt["source_sha256"],
        "final_sha256": receipt["final_sha256"],
        "size_bytes": receipt["size_bytes"],
        "embedding_profile": "exact_source_bytes",
    }


def member_rows(receipts: list[dict[str, Any]], staging: Path) -> list[dict[str, str]]:
    rows = []
    for receipt in receipts:
        final = receipt["final_relative_path"]
        if not final.lower().endswith(".zip"):
            continue
        with zipfile.ZipFile(staging / final) as archive:
            if archive.testzip() is not None:
                raise CloseoutError(f"ZIP member CRC failed: {final}")
            for info in archive.infolist():
                if info.is_dir():
                    continue
                digest = hashlib.sha256()
                with archive.open(info) as handle:
                    while chunk := handle.read(1024 * 1024):
                        digest.update(chunk)
                rows.append(
                    {
                        "final_container": final,
                        "member": info.filename,
                        "size_bytes": str(info.file_size),
                        "crc32": f"{info.CRC:08x}",
                        "sha256": "sha256:" + digest.hexdigest(),
                    }
                )
    return rows


def xlsx_bytes(rows: list[dict[str, Any]], fields: list[str], sheet: str) -> bytes:
    return paragon_document_bundle.table_xlsx_bytes(
        fields,
        [{field: str(row.get(field, "")) for field in fields} for row in rows],
        sheet_name=sheet,
        table_name=sheet.replace(" ", ""),
    )


def source_kind(path: str) -> str:
    suffix = Path(path).suffix.casefold()
    if suffix == ".pdf":
        return "pdf"
    if suffix in {".xlsx", ".docx", ".ods", ".pptx"}:
        return "office_zip"
    if suffix in {".zip", ".7z", ".tar", ".gz"}:
        return "archive"
    if suffix in {".txt", ".csv", ".xml", ".css", ".htm", ".html"}:
        return "plain"
    return "file"


def normalize_sha256(value: str) -> str:
    parts = [part.strip() for part in value.split(";") if part.strip()]
    normalized = []
    for part in parts:
        if re.fullmatch(r"[0-9a-fA-F]{64}", part):
            normalized.append("sha256:" + part.lower())
        else:
            normalized.append(part)
    return ";".join(normalized)


def relative_source(path: str) -> str:
    return str(Path(path).relative_to(SOURCE_MOUNT))


def is_usb_path(path: str) -> bool:
    candidate = Path(path)
    return candidate == SOURCE_MOUNT or SOURCE_MOUNT in candidate.parents


def drain_reason(row: dict[str, str]) -> str:
    status = row["preservation_status"]
    if status == "preserved_exact_copy":
        return "meaningful source preserved as exact bytes in WF-Sender unit"
    if status == "preserved_metadata_enriched_copy":
        return "meaningful source preserved with accepted decoded-content equivalence"
    if status == "reviewed_workflow_noise":
        return "reviewed WF-Sender workflow or administrative noise"
    if status == "reviewed_superseded_or_derivative":
        return "superseded generation or derivative covered by the WF-Sender unit"
    if status == "covered_carrier_derivative_candidate":
        return "carrier derivative covered by the authoritative issue representations"
    return row.get("preservation_basis", "reviewed WF-Sender source disposition")


def build_dispositions(
    receipts: list[dict[str, Any]],
    hash_cache: SourceHashCache,
) -> tuple[list[dict[str, str]], list[dict[str, str]], list[dict[str, str]]]:
    current = {
        row["source_path"]: row
        for row in read_csv(UNIT / "db-restore/manifests/source-disposition.csv")
    }
    audit_rows = read_csv(AUDIT / "wf-sender-disposition-review.csv")
    audit = {row["source_path"]: row for row in audit_rows}
    document_reviews = {
        row["source_path"]: row
        for row in read_csv(UNIT / "db-restore/manifests/documents-review.csv")
    }
    selected = selected_map()

    candidates: set[str] = set()
    for path, row in current.items():
        review = document_reviews.get(path)
        contextual_document = bool(
            review and review["relationship"] in {"contextual", "excluded"}
        )
        if (
            is_usb_path(path)
            and not is_deferred_shared(path)
            and not is_cross_project(path, row["preservation_status"])
            and not contextual_document
        ):
            candidates.add(path)
    for path, row in audit.items():
        if is_deferred_shared(path) or is_cross_project(
            path, row["preservation_status"]
        ):
            continue
        if is_direct_wfs_tree(path):
            candidates.add(path)
        elif row["preservation_status"] in {
            "covered_carrier_derivative_candidate",
            "covered_derivative_candidate",
            "preserved_exact_duplicate",
        } and (
            Path(path).name.upper().startswith(("WFS", "WSF"))
            or "WF-Sender" in Path(path).name
        ):
            candidates.add(path)
    candidates.add(str(GOOGLE))

    dispositions: dict[str, dict[str, str]] = {}
    drain: list[dict[str, str]] = []
    last_progress = time.monotonic()
    total = len(candidates)
    for index, path in enumerate(sorted(candidates), start=1):
        if not Path(path).is_file():
            raise CloseoutError(f"drain source missing: {path}")
        old = current.get(path) or audit.get(path) or {}
        selected_item = selected.get(path)
        selected_receipt = next(
            (row for row in receipts if row["source_path"] == path), None
        )
        row = {field: str(old.get(field, "")) for field in DISPOSITION_FIELDS}
        row.update(
            {
                "source_path": path,
                "source_kind": row["source_kind"] or source_kind(path),
                "unit": "wf-sender",
                "source_size_bytes": str(Path(path).stat().st_size),
                "deletion_authorized": "false",
                "drain_state": "listed_for_later_drain",
            }
        )
        row["source_sha256"] = normalize_sha256(row["source_sha256"])
        row["final_sha256"] = normalize_sha256(row["final_sha256"])
        if selected_item and selected_receipt:
            row.update(
                {
                    "source_role": "project_document",
                    "final_path": selected_receipt["final_relative_path"],
                    "preservation_status": "preserved_exact_copy",
                    "preservation_basis": "exact_source_bytes",
                    "source_sha256": selected_receipt["source_sha256"],
                    "final_sha256": selected_receipt["final_sha256"],
                    "exact_copy_proven": "true",
                    "notes": "Selected closeout document; exact bytes preserved. No deletion authorized.",
                }
            )
        elif row["preservation_status"] == "preserved_metadata_enriched_copy":
            row["source_sha256"] = row["source_sha256"] or hash_cache.hash(Path(path))
            row["preservation_basis"] = (
                row["preservation_basis"]
                + "; accepted accession readback and decoded-content equivalence"
            ).strip("; ")
            row["notes"] = (
                "Authoritative enriched representation validated during accession; "
                "source hash retained. No deletion authorized."
            )
        elif row["preservation_status"] in {
            "preserved_exact_copy",
            "preserved_exact_duplicate",
        }:
            row["source_sha256"] = row["source_sha256"] or hash_cache.hash(Path(path))
            if not row["final_sha256"] and row["exact_copy_proven"] == "true":
                row["final_sha256"] = row["source_sha256"]
            row["preservation_status"] = "preserved_exact_copy"
            row["notes"] = "Exact source bytes preserved in the unit. No deletion authorized."
        else:
            row["source_sha256"] = row["source_sha256"] or hash_cache.hash(Path(path))
            if is_workflow_noise(path):
                row["preservation_status"] = "reviewed_workflow_noise"
                row["preservation_basis"] = (
                    "temporary, cache, compressed accounting support, or operating-system artifact"
                )
                row["notes"] = "Reviewed project noise; no archival payload selected. No deletion authorized."
            elif row["preservation_status"] == "covered_carrier_derivative_candidate":
                row["preservation_basis"] = (
                    row["preservation_basis"]
                    or "inventory-bearing derivative covered by authoritative carrier representations"
                )
                row["notes"] = "Covered carrier derivative; no deletion authorized."
            else:
                row["preservation_status"] = "reviewed_superseded_or_derivative"
                row["preservation_basis"] = (
                    row["preservation_basis"]
                    or "reviewed working generation, access derivative, or duplicate project artifact"
                )
                row["notes"] = "Reviewed superseded or derivative project file; no deletion authorized."
        dispositions[path] = row
        drain_row = {
            "source_root_uuid": SOURCE_UUID,
            "source_relative_path": relative_source(path),
            **row,
            "drain_reason": drain_reason(row),
        }
        drain.append(drain_row)
        now = time.monotonic()
        if now - last_progress >= 30 or index == total:
            print(f"hashed/classified {index}/{total} drain paths", flush=True)
            last_progress = now

    excluded: list[dict[str, str]] = []
    for path, old in audit.items():
        if not is_direct_wfs_tree(path):
            continue
        if path in dispositions:
            continue
        reason = (
            "mixed website or Lightroom source reserved for separate review"
            if is_deferred_shared(path)
            else "cross-project Trafo/FEB material excluded from WF-Sender drain"
        )
        excluded.append(
            {
                "source_path": path,
                "source_root_uuid": SOURCE_UUID,
                "source_relative_path": relative_source(path),
                "state": "excluded_from_wf_sender_drain",
                "reason": reason,
                "deletion_authorized": "false",
            }
        )

    # Retain existing reviewed contextual rows in the canonical disposition,
    # but never project them into the drain list.
    for path, old in current.items():
        if path in dispositions:
            continue
        row = {field: str(old.get(field, "")) for field in DISPOSITION_FIELDS}
        row["drain_state"] = (
            "deferred_separate_review"
            if is_deferred_shared(path)
            else "excluded_other_project_or_context"
        )
        row["deletion_authorized"] = "false"
        dispositions[path] = row

    if any(row["deletion_authorized"] != "false" for row in dispositions.values()):
        raise CloseoutError("canonical disposition authorizes deletion")
    if any(row["deletion_authorized"] != "false" for row in drain):
        raise CloseoutError("drain list authorizes deletion")
    unhashed = [
        row
        for row in drain
        if not row["source_sha256"].startswith("sha256:")
    ]
    if unhashed:
        sample = ", ".join(
            f"{row['source_path']}={row['source_sha256']!r}"
            for row in unhashed[:5]
        )
        raise CloseoutError(
            f"drain list contains {len(unhashed)} unhashed sources: {sample}"
        )
    return (
        [dispositions[path] for path in sorted(dispositions)],
        sorted(drain, key=lambda row: row["source_path"]),
        sorted(excluded, key=lambda row: row["source_path"]),
    )


def restore_objects() -> tuple[list[str], list[list[str]]]:
    connection = sqlite3.connect(
        f"file:{UNIT / 'db-restore/RESTORE.sqlite'}?mode=ro", uri=True
    )
    try:
        fields = [
            str(row[1])
            for row in connection.execute("PRAGMA table_info(objects)").fetchall()
        ]
        rows = [
            [str(value) for value in row]
            for row in connection.execute("SELECT * FROM objects").fetchall()
        ]
    finally:
        connection.close()
    return fields, rows


def build_metadata(
    receipts: list[dict[str, Any]],
    dispositions: list[dict[str, str]],
    drain: list[dict[str, str]],
    excluded: list[dict[str, str]],
    members: list[dict[str, str]],
    output: Path,
) -> dict[str, Any]:
    catalog = read_csv(UNIT / "CATALOG.csv")
    existing_paths = {row["final_relative_path"] for row in catalog}
    for receipt in receipts:
        if receipt["final_relative_path"] in existing_paths:
            raise CloseoutError(
                f"catalog destination already exists: {receipt['final_relative_path']}"
            )
        catalog.append(catalog_addition(receipt))
    catalog.sort(key=lambda row: row["final_relative_path"])

    documents = read_csv(UNIT / "db-restore/manifests/documents.csv")
    documents.extend(
        {field: str(receipt.get(field, "")) for field in DOCUMENT_FIELDS}
        for receipt in receipts
    )
    documents.sort(key=lambda row: row["final_relative_path"])

    reviews = read_csv(UNIT / "db-restore/manifests/documents-review.csv")
    review_paths = {row["source_path"] for row in reviews}
    for receipt in receipts:
        if receipt["source_path"] not in review_paths:
            reviews.append(
                {
                    "source_path": receipt["source_path"],
                    "candidate_category": "project_documentation",
                    "relationship": "direct",
                    "decision": "included",
                    "note": receipt["description"],
                }
            )
    reviews.sort(key=lambda row: row["source_path"])

    checksums = [
        {
            "path": row["final_relative_path"],
            "sha256": row["final_sha256"],
            "size_bytes": row["size_bytes"],
        }
        for row in catalog
    ]

    files = {
        "CATALOG.csv": csv_bytes(catalog, CATALOG_FIELDS),
        "CATALOG.xlsx": xlsx_bytes(catalog, CATALOG_FIELDS, "Catalog"),
        "db-restore/RESTORE.csv": csv_bytes(catalog, CATALOG_FIELDS),
        "db-restore/RESTORE.xlsx": xlsx_bytes(catalog, CATALOG_FIELDS, "Restore"),
        "db-restore/checksums.csv": csv_bytes(checksums, CHECKSUM_FIELDS),
        "db-restore/manifests/representations.csv": csv_bytes(
            catalog, CATALOG_FIELDS
        ),
        "db-restore/manifests/source-disposition.csv": csv_bytes(
            dispositions, DISPOSITION_FIELDS
        ),
        "db-restore/manifests/drain-list.csv": csv_bytes(drain, DRAIN_FIELDS),
        "db-restore/manifests/documents.csv": csv_bytes(
            documents, DOCUMENT_FIELDS
        ),
        "db-restore/manifests/documents-review.csv": csv_bytes(
            reviews, list(reviews[0])
        ),
        "db-restore/manifests/document-container-members.csv": csv_bytes(
            members, MEMBER_FIELDS
        ),
        "db-restore/manifests/excluded-cross-project.csv": csv_bytes(
            excluded,
            [
                "source_path",
                "source_root_uuid",
                "source_relative_path",
                "state",
                "reason",
                "deletion_authorized",
            ],
        ),
    }

    object_fields, object_rows = restore_objects()
    sqlite_path = output / "db-restore/RESTORE.sqlite"
    sqlite_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(sqlite_path)
    try:
        connection.execute(
            "CREATE TABLE objects ("
            + ",".join(f'"{field}" TEXT NOT NULL' for field in object_fields)
            + ")"
        )
        connection.execute(
            "CREATE TABLE representations ("
            + ",".join(f'"{field}" TEXT NOT NULL' for field in CATALOG_FIELDS)
            + ")"
        )
        connection.execute(
            "CREATE TABLE source_disposition ("
            + ",".join(f'"{field}" TEXT NOT NULL' for field in DISPOSITION_FIELDS)
            + ")"
        )
        connection.executemany(
            "INSERT INTO objects VALUES ("
            + ",".join("?" for _ in object_fields)
            + ")",
            object_rows,
        )
        connection.executemany(
            "INSERT INTO representations VALUES ("
            + ",".join("?" for _ in CATALOG_FIELDS)
            + ")",
            [[row[field] for field in CATALOG_FIELDS] for row in catalog],
        )
        connection.executemany(
            "INSERT INTO source_disposition VALUES ("
            + ",".join("?" for _ in DISPOSITION_FIELDS)
            + ")",
            [[row[field] for field in DISPOSITION_FIELDS] for row in dispositions],
        )
        connection.commit()
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise CloseoutError("generated restore SQLite integrity failed")
        sql = "\n".join(connection.iterdump()) + "\n"
    finally:
        connection.close()
    files["db-restore/RESTORE.sql"] = sql.encode("utf-8")

    contents = (UNIT / "documents/CONTENTS.md").read_text(encoding="utf-8")
    contents += "\n# WF-Sender closeout additions\n\n"
    for receipt in sorted(receipts, key=lambda row: row["final_relative_path"]):
        contents += (
            f"## {Path(receipt['final_relative_path']).name}\n\n"
            f"- Description: {receipt['description']}\n"
            f"- Source: `{receipt['source_path']}`\n"
            "- Relationship: direct\n"
            f"- Condition: {receipt['condition']}\n\n"
        )
    files["documents/CONTENTS.md"] = contents.encode("utf-8")

    readme = (UNIT / "README.md").read_text(encoding="utf-8")
    role_counts = Counter(row["role"] for row in catalog)
    readme = readme.replace(
        "- Archived payload and document files: 7232",
        f"- Archived payload and document files: {len(catalog)}",
    ).replace(
        "- Project documents: 21",
        f"- Project documents: {role_counts['project_document']}",
    )
    readme += f"""

## USB source closeout

The 2026-07-29 closeout added {len(receipts)} direct WF-Sender documents and
metadata sources. The canonical disposition now records {len(dispositions)}
source paths. `db-restore/manifests/drain-list.csv` contains {len(drain)}
hash-bound paths for a later drain, all with `deletion_authorized=false`.

The historical WordPress website and Lightroom catalogs are mixed-project
sources. They remain untouched and outside this drain list. A separate
read-only review may copy verified, provenance-linked metadata into WF-Sender
object records without changing those sources.
"""
    files["README.md"] = readme.encode("utf-8")

    for relative, data in files.items():
        atomic_write(output / relative, data)

    return {
        "catalog_rows": len(catalog),
        "document_rows": len(documents),
        "source_disposition_rows": len(dispositions),
        "drain_rows": len(drain),
        "excluded_cross_project_rows": len(excluded),
        "container_member_rows": len(members),
        "drain_bytes": sum(int(row["source_size_bytes"]) for row in drain),
        "drain_status_counts": dict(
            sorted(Counter(row["preservation_status"] for row in drain).items())
        ),
    }


def validate_generated(
    metadata: Path,
    staging: Path,
    receipts: list[dict[str, Any]],
    summary: dict[str, Any],
) -> None:
    catalog = read_csv(metadata / "CATALOG.csv")
    checksums = read_csv(metadata / "db-restore/checksums.csv")
    drain = read_csv(metadata / "db-restore/manifests/drain-list.csv")
    disposition = read_csv(
        metadata / "db-restore/manifests/source-disposition.csv"
    )
    if len(catalog) != summary["catalog_rows"] or len(checksums) != len(catalog):
        raise CloseoutError("generated catalog/checksum count mismatch")
    if len(drain) != summary["drain_rows"]:
        raise CloseoutError("generated drain count mismatch")
    if len(disposition) != summary["source_disposition_rows"]:
        raise CloseoutError("generated disposition count mismatch")
    if any(row["deletion_authorized"] != "false" for row in drain):
        raise CloseoutError("generated drain list authorizes deletion")
    if any(not row["source_sha256"].startswith("sha256:") for row in drain):
        raise CloseoutError("generated drain list has missing source hashes")
    for receipt in receipts:
        path = staging / receipt["final_relative_path"]
        if sha256_file(path) != receipt["final_sha256"]:
            raise CloseoutError(f"staged document validation failed: {path}")
    connection = sqlite3.connect(
        f"file:{metadata / 'db-restore/RESTORE.sqlite'}?mode=ro", uri=True
    )
    try:
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise CloseoutError("generated SQLite validation failed")
        if (
            connection.execute("SELECT COUNT(*) FROM representations").fetchone()[0]
            != len(catalog)
        ):
            raise CloseoutError("generated SQLite representation count mismatch")
        if (
            connection.execute("SELECT COUNT(*) FROM source_disposition").fetchone()[0]
            != len(disposition)
        ):
            raise CloseoutError("generated SQLite disposition count mismatch")
    finally:
        connection.close()


def apply_to_unit(
    metadata: Path, staging: Path, receipts: list[dict[str, Any]]
) -> None:
    rollback = RUN / "rollback"
    rollback.mkdir(parents=True, exist_ok=True)
    metadata_files = [
        path.relative_to(metadata)
        for path in metadata.rglob("*")
        if path.is_file()
    ]
    installed: list[Path] = []
    replaced: list[Path] = []
    try:
        for receipt in receipts:
            relative = Path(receipt["final_relative_path"])
            destination = UNIT / relative
            if destination.exists():
                raise CloseoutError(f"refusing existing document destination: {destination}")
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.rename(staging / relative, destination)
            installed.append(destination)
        for relative in metadata_files:
            destination = UNIT / relative
            if destination.exists():
                backup = rollback / relative
                backup.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(destination, backup)
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.replace(metadata / relative, destination)
            replaced.append(destination)
    except Exception:
        for destination in reversed(replaced):
            relative = destination.relative_to(UNIT)
            backup = rollback / relative
            if backup.exists():
                os.replace(backup, destination)
        for destination in reversed(installed):
            if destination.exists():
                destination.unlink()
        raise


def validate_unit(receipts: list[dict[str, Any]], summary: dict[str, Any]) -> dict[str, Any]:
    catalog = read_csv(UNIT / "CATALOG.csv")
    drain = read_csv(UNIT / "db-restore/manifests/drain-list.csv")
    disposition = read_csv(UNIT / "db-restore/manifests/source-disposition.csv")
    documents = read_csv(UNIT / "db-restore/manifests/documents.csv")
    if len(catalog) != summary["catalog_rows"]:
        raise CloseoutError("published catalog count mismatch")
    if len(documents) != summary["document_rows"]:
        raise CloseoutError("published document count mismatch")
    if len(drain) != summary["drain_rows"]:
        raise CloseoutError("published drain count mismatch")
    if len(disposition) != summary["source_disposition_rows"]:
        raise CloseoutError("published disposition count mismatch")
    for receipt in receipts:
        path = UNIT / receipt["final_relative_path"]
        if sha256_file(path) != receipt["final_sha256"]:
            raise CloseoutError(f"published document differs: {path}")
    connection = sqlite3.connect(
        f"file:{UNIT / 'db-restore/RESTORE.sqlite'}?mode=ro", uri=True
    )
    try:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        representation_rows = connection.execute(
            "SELECT COUNT(*) FROM representations"
        ).fetchone()[0]
        disposition_rows = connection.execute(
            "SELECT COUNT(*) FROM source_disposition"
        ).fetchone()[0]
    finally:
        connection.close()
    if integrity != "ok":
        raise CloseoutError("published SQLite integrity failed")
    if representation_rows != len(catalog) or disposition_rows != len(disposition):
        raise CloseoutError("published SQLite counts mismatch")
    return {
        "result": "ok",
        "source_mount_uuid": SOURCE_UUID,
        "source_read_only": True,
        "catalog_rows": len(catalog),
        "document_rows": len(documents),
        "source_disposition_rows": len(disposition),
        "drain_rows": len(drain),
        "drain_bytes": sum(int(row["source_size_bytes"]) for row in drain),
        "deletion_authorized_rows": sum(
            row["deletion_authorized"] != "false" for row in drain
        ),
        "sqlite_integrity": integrity,
        "website_and_lightroom_untouched": True,
    }


def execute(apply: bool) -> int:
    mount_is_read_only()
    RUN.mkdir(parents=True, exist_ok=True)
    staging = RUN / "staging"
    metadata = RUN / "generated-unit-metadata"
    if metadata.exists():
        shutil.rmtree(metadata)
    metadata.mkdir(parents=True)
    receipts = addition_receipts(staging)
    members = member_rows(receipts, staging)
    hash_cache = SourceHashCache(RUN / "hash-cache.jsonl")
    try:
        dispositions, drain, excluded = build_dispositions(receipts, hash_cache)
    finally:
        hash_cache.close()
    summary = build_metadata(
        receipts, dispositions, drain, excluded, members, metadata
    )
    summary.update(
        {
            "format": "iss-wf-sender-closeout-v1",
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "apply_requested": apply,
            "selected_additions": len(receipts),
            "source_mount_uuid": SOURCE_UUID,
            "source_read_only": True,
            "deletion_authorized_rows": 0,
            "website_and_lightroom_untouched": True,
        }
    )
    validate_generated(metadata, staging, receipts, summary)
    atomic_write(
        RUN / "summary.json",
        (json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(),
    )
    if not apply:
        print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    apply_to_unit(metadata, staging, receipts)
    validation = validate_unit(receipts, summary)
    atomic_write(
        RUN / "validation.json",
        (
            json.dumps(validation, ensure_ascii=False, indent=2, sort_keys=True)
            + "\n"
        ).encode(),
    )
    findings = f"""# WF-Sender closeout

- Result: `{validation['result']}`
- Direct documents added: {len(receipts)}
- Catalog rows: {validation['catalog_rows']}
- Canonical source-disposition rows: {validation['source_disposition_rows']}
- Later drain-list rows: {validation['drain_rows']}
- Later drain-list bytes: {validation['drain_bytes']}
- Deletion-authorized rows: {validation['deletion_authorized_rows']}
- Source UUID: `{SOURCE_UUID}` mounted read-only

The WordPress website and Lightroom catalogs were not accessioned, rewritten,
or added to the drain list. They remain mixed-project sources for separate
review. Verified metadata may later enrich WF-Sender object records with
provenance.
"""
    atomic_write(RUN / "FINDINGS.md", findings.encode("utf-8"))
    print(json.dumps(validation, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument(
        "--apply",
        action="store_true",
        help="publish selected documents and generated current-state metadata",
    )
    return result


def main() -> int:
    try:
        return execute(parser().parse_args().apply)
    except (CloseoutError, OSError, csv.Error, sqlite3.Error, zipfile.BadZipFile) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
