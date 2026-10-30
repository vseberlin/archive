#!/usr/bin/env python3
"""Resumable guarded accession of the reconciled WF-Sender collection."""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import re
import sqlite3
import subprocess
import sys
import traceback
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable


TOOL_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOL_ROOT))
import copyhash_runner  # noqa: E402
import paragon_document_bundle  # noqa: E402
import tiff_accession  # noqa: E402
import tiff_metadata  # noqa: E402


RUN_ID = "wf-sender-full-001"
UNIT_ID = "wf-sender"
PROFILE_TIFF = "iss-tiff-v2+wf-sender-page-1"
PROFILE_PDF = "iss-pdf-xmp-v1"
WORK_BASE = Path("/mnt/volume/iss-archive-work")
FINAL_BASE = Path("/mnt/volume/iss-archive")
WF_WORK = WORK_BASE / "wf-sender"
RUN_ROOT = WF_WORK / "work/accession-runs" / RUN_ID
PACKAGE = RUN_ROOT / "package"
FINAL_UNIT = FINAL_BASE / "units" / UNIT_ID
PLAN_ROOT = WF_WORK / (
    "work/accession-plan/runs/20260728T194218Z-pdf-tiff-plan"
)
MD_ROOT = WF_WORK / (
    "work/reconciliation/museum-digital/runs/"
    "20260728T060935Z-wf-sender"
)
DOCUMENT_DISCOVERY = WORK_BASE / (
    "produktion-propaganda-wf-sender/work/reconciliation/local-discovery/"
    "runs/20260728T055536Z-wf-sender-local-discovery"
)
COPYHASH = RUN_ROOT / "tools/archive-copyhash"
SOURCE_MEDIA = Path("/media/vladimir/Elements")

ROLE_COUNTS = {
    "access_pdf": 999,
    "preservation_master_page": 5335,
    "preservation_master_pdf": 864,
    "damaged_source_tiff_fragment": 10,
    "combined_source_carrier_pdf": 1,
}
ENRICHED_ROLES = {
    "access_pdf",
    "preservation_master_page",
    "preservation_master_pdf",
}
EXACT_ROLES = {
    "damaged_source_tiff_fragment",
    "combined_source_carrier_pdf",
}

DOCUMENTS = [
    (
        "WF/WF-Sender Projekt/Alle-WF-Sender_auf-md_2020-11-25.xlsx",
        "documents/metadata/Alle-WF-Sender_auf-md_2020-11-25.xlsx",
        "Final collection-wide museum-digital status workbook.",
    ),
    (
        "WF/WF-Sender Projekt/Basisaufnahme_komplett_Stand-2020-08-12.xlsx",
        "documents/metadata/Basisaufnahme_komplett_Stand-2020-08-12.xlsx",
        "Consolidated issue-level basis and catalog metadata.",
    ),
    (
        "WF/WF-Sender Projekt/Kategorievorschläge.xlsx",
        "documents/metadata/Kategorievorschläge.xlsx",
        "Project category proposals used during metadata preparation.",
    ),
    (
        "WF/WF-Sender Projekt/SW_Inhalt-1989-90_10.xlsx",
        "documents/metadata/SW_Inhalt-1989-90_10.xlsx",
        "Latest retained 1989-1990 content and metadata workbook.",
    ),
    (
        "WF/WF-Sender Projekt/Uploads/1. Upload/"
        "WF-Sender_erster-Upload_2020-07-28.csv",
        "documents/metadata/uploads/first-upload-2020-07-28.csv",
        "Final CSV used for the first museum-digital upload.",
    ),
    (
        "WF/WF-Sender Projekt/Uploads/1. Upload/"
        "WF-Sender_erster-Upload_09.xlsx",
        "documents/metadata/uploads/first-upload-09.xlsx",
        "Final spreadsheet form retained for the first upload.",
    ),
    (
        "WF/WF-Sender Projekt/Uploads/2.Upload_02/"
        "WF-Sender_zweiter-upload_2020-08-10.csv",
        "documents/metadata/uploads/second-upload-2020-08-10.csv",
        "Final CSV used for the second museum-digital upload.",
    ),
    (
        "WF/WF-Sender Projekt/Uploads/2.Upload_02/"
        "WF-Sender_zweiter-upload_2020-08-10.xlsx",
        "documents/metadata/uploads/second-upload-2020-08-10.xlsx",
        "Final spreadsheet form retained for the second upload.",
    ),
    (
        "WF/WF-Sender Projekt/Uploads/3.Upload_1957,1958,1959,1971/"
        "WF-Sender_dritter-upload_1957,1958,1959,1971_final.xlsx",
        "documents/metadata/uploads/third-upload-final.xlsx",
        "Final workbook for the third upload batch.",
    ),
    (
        "WF/WF-Sender Projekt/Uploads/1950/"
        "Inhalt_1950-Stand-11.10.20_07_final.xlsx",
        "documents/metadata/uploads/1950-final.xlsx",
        "Final retained 1950 issue-content workbook.",
    ),
    (
        "WF/WF-Sender Projekt/Uploads/Inhalt_1951-1952_AG_08_final.xlsx",
        "documents/metadata/uploads/1951-1952-final.xlsx",
        "Final retained 1951-1952 issue-content workbook.",
    ),
    (
        "WF/WF-Sender Projekt/Beauftragter zur Aufarbeitung der SED Diktatur/"
        "Archiv/Projektantrag_Industriesalon-Schöneweide_2020_02_15_FINALE.docx",
        "documents/project/project-application-final.docx",
        "Final project application for the WF-Sender digitization.",
    ),
    (
        "WF/WF-Sender Projekt/Beauftragter zur Aufarbeitung der SED Diktatur/"
        "Zuwendungsbescheid 2020.pdf",
        "documents/project/funding-award-2020.pdf",
        "Funding award covering the digitization project.",
    ),
    (
        "WF/WF-Sender Projekt/Sachbericht/"
        "Sachbericht_Industriesalon_Digitalisierung-WF-Sender.pdf",
        "documents/project/final-report.pdf",
        "Final project report for WF-Sender digitization.",
    ),
    (
        "WF/WF-Sender Projekt/Archiv/"
        "Projekt_Digitalisierung_WF-Sender_ Expose.pdf",
        "documents/project/project-expose.pdf",
        "Project description and digitization concept.",
    ),
    (
        "WF/WF-Sender Projekt/Finanzierungsplan_3._Fassung .pdf",
        "documents/project/financing-plan-final.pdf",
        "Retained final project financing plan.",
    ),
    (
        "WF/WF-Sender Projekt/Archiv/Angebote Scans/"
        "Angebot_MIK_32000078.pdf",
        "documents/scanning/MIK-scan-offer.pdf",
        "MIK offer for source scanning.",
    ),
    (
        "WF/WF-Sender Projekt/Archiv/Angebote Scans/"
        "4008561_Digitalisierung-Betriebszeitschrift_Auftrag_2020-06-16.pdf",
        "documents/scanning/scan-order-2020-06-16.pdf",
        "Digitization order for the newspaper collection.",
    ),
    (
        "WF/WF-Sender Projekt/"
        "Industriesalon-Schöneweide_Lieferschein-WF-Sender_alle-Ausgaben.pdf",
        "documents/scanning/delivery-note-all-issues.pdf",
        "Delivery note covering the WF-Sender issue set.",
    ),
    (
        "WF/WF-Sender Projekt/Rechnungen/"
        "Rechnung_WF-Sender-Digitalisierung_072_201007_Industriesalon.pdf",
        "documents/scanning/digitization-invoice.pdf",
        "Invoice documenting the completed digitization service.",
    ),
    (
        "MIK-Daten/1953/HF-Sender_Sonderausgaben/2020-09-30_Protokoll.ods",
        "documents/scanning/HF-Sender-special-editions-protocol.ods",
        "Source protocol for the paired special-edition PDF/TIFF set.",
    ),
]

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
CHECKSUM_FIELDS = ["path", "sha256", "size_bytes"]
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


class AccessionError(RuntimeError):
    """Raised when a guarded accession invariant fails."""


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def csv_bytes(rows: Iterable[dict[str, Any]], fields: list[str]) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(
        output,
        fieldnames=fields,
        extrasaction="ignore",
        lineterminator="\n",
        quoting=csv.QUOTE_ALL,
    )
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue().encode("utf-8")


def require_inside(path: Path, root: Path, *, allow_root: bool = False) -> Path:
    resolved = path.resolve()
    safe = root.resolve()
    if (resolved == safe and not allow_root) or not resolved.is_relative_to(safe):
        raise AccessionError(f"path escaped allowed root {safe}: {resolved}")
    return resolved


def writable_path(path: Path) -> Path:
    resolved = path.resolve()
    if resolved.is_relative_to(WORK_BASE.resolve()):
        return resolved
    if resolved.is_relative_to(FINAL_BASE.resolve()):
        return resolved
    raise AccessionError(f"write escaped archive roots: {resolved}")


def mkdir(path: Path) -> None:
    writable_path(path)
    path.mkdir(parents=True, exist_ok=True)


def atomic_write(path: Path, payload: bytes, *, replace: bool = False) -> None:
    writable_path(path)
    mkdir(path.parent)
    if path.exists() and not replace:
        raise AccessionError(f"refusing existing output: {path}")
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    writable_path(temporary)
    if temporary.exists():
        temporary.unlink()
    with temporary.open("xb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    if replace:
        os.replace(temporary, path)
    else:
        os.rename(temporary, path)


def write_json(path: Path, value: Any, *, replace: bool = False) -> None:
    atomic_write(
        path,
        (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode(),
        replace=replace,
    )


def append_error(value: dict[str, Any]) -> None:
    path = RUN_ROOT / "errors.jsonl"
    writable_path(path)
    mkdir(path.parent)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def run(arguments: list[str], *, timeout: int = 900) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            arguments,
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        detail = ""
        if isinstance(error, subprocess.CalledProcessError):
            detail = (error.stderr or error.stdout or "").strip()
        raise AccessionError(
            f"{arguments[0]} failed: {detail or error}"
        ) from error


def mount_line(path: Path) -> str:
    return run(
        ["findmnt", "-n", "-o", "SOURCE,TARGET,FSTYPE,OPTIONS", "--target", str(path)]
    ).stdout.strip()


def ensure_boundaries() -> dict[str, str]:
    source_mount = mount_line(SOURCE_MEDIA)
    fields = source_mount.split(None, 3)
    if len(fields) != 4 or "ro" not in set(fields[3].split(",")):
        raise AccessionError(f"source media is not read-only: {source_mount}")
    work_mount = mount_line(WORK_BASE)
    final_mount = mount_line(FINAL_BASE)
    if WORK_BASE.stat().st_dev != FINAL_BASE.stat().st_dev:
        raise AccessionError("work and final archive roots are not on one filesystem")
    if FINAL_UNIT.exists():
        raise AccessionError(f"final unit already exists: {FINAL_UNIT}")
    if not COPYHASH.is_file() or not os.access(COPYHASH, os.X_OK):
        raise AccessionError(f"compiled copy/hash helper is unavailable: {COPYHASH}")
    version_commands = {
        "exiftool": ["exiftool", "-ver"],
        "magick": ["magick", "-version"],
        "pdfinfo": ["pdfinfo", "-v"],
    }
    for command in version_commands.values():
        run(command)
    return {
        "source_mount": source_mount,
        "work_mount": work_mount,
        "final_mount": final_mount,
    }


def plan_digest() -> str:
    digest = hashlib.sha256()
    for name in ("objects.csv", "representations.csv", "source-disposition.csv"):
        path = PLAN_ROOT / name
        digest.update(name.encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
    return f"sha256:{digest.hexdigest()}"


def load_inputs() -> tuple[
    list[dict[str, str]],
    list[dict[str, str]],
    list[dict[str, str]],
    dict[str, dict[str, str]],
]:
    summary = json.loads((PLAN_ROOT / "summary.json").read_text(encoding="utf-8"))
    if summary.get("errors") != 0 or summary.get("representations") != 7209:
        raise AccessionError("source-selection plan is not clean")
    objects = read_csv(PLAN_ROOT / "objects.csv")
    representations = read_csv(PLAN_ROOT / "representations.csv")
    dispositions = read_csv(PLAN_ROOT / "source-disposition.csv")
    if len(objects) != 1863 or len(representations) != 7209:
        raise AccessionError("source-selection counts drifted")
    counts = Counter(row["role"] for row in representations)
    if dict(counts) != ROLE_COUNTS:
        raise AccessionError(f"representation roles drifted: {dict(counts)}")
    final_paths = [row["final_relative_path"] for row in representations]
    if len(set(final_paths)) != len(final_paths):
        raise AccessionError("source plan contains duplicate final paths")
    object_map = {row["carrier_key"]: row for row in objects}
    if len(object_map) != len(objects):
        raise AccessionError("source plan contains duplicate carrier keys")
    for row in representations:
        source = Path(row["source_path"]).resolve(strict=True)
        if source.is_symlink() or not source.is_file():
            raise AccessionError(f"source is not one regular file: {source}")
        stat = source.stat()
        if (
            stat.st_size != int(row["source_size_bytes"])
            or stat.st_mtime_ns != int(row["source_mtime_ns"])
        ):
            raise AccessionError(f"source observation changed: {source}")
        if row["role"] not in ENRICHED_ROLES | EXACT_ROLES:
            raise AccessionError(f"unsupported representation role: {row['role']}")
    return objects, representations, dispositions, object_map


def metadata_for(
    row: dict[str, str],
    object_map: dict[str, dict[str, str]],
) -> dict[str, str]:
    obj = object_map.get(row["carrier_key"])
    if obj is None and row["role"] == "combined_source_carrier_pdf":
        return {
            "carrier_key": row["carrier_key"],
            "inventory": row["inventory"],
            "title": "Combined HF-Sender special-edition source carrier",
            "date_text": "1955",
            "object_type": "source carrier",
            "md_object_ids": row["md_object_ids"],
            "md_object_urls": (
                "https://berlin.museum-digital.de/object/87094;"
                "https://berlin.museum-digital.de/object/87095"
            ),
            "relationship_state": "combined_source_provenance",
        }
    if obj is None:
        raise AccessionError(f"representation has no object metadata: {row}")
    result = dict(obj)
    if not result["title"]:
        result["title"] = f"Betriebszeitung WF-Sender {result['inventory']}"
    if not result["object_type"] or result["object_type"] == "local_only":
        result["object_type"] = "Zeitung"
    if not result["date_text"]:
        match = re.search(r"(?:19|20)\d{2}", result["inventory"])
        result["date_text"] = match.group(0) if match else ""
    return result


def xmp_date(date_text: str) -> str:
    """Return a valid, conservative XMP date while preserving raw text elsewhere."""

    value = date_text.strip()
    exact = re.fullmatch(r"(\d{2})\.(\d{2})\.((?:19|20)\d{2})", value)
    if exact:
        day, month, year = exact.groups()
        return f"{year}-{month}-{day}"
    months = {
        "Januar": "01",
        "Februar": "02",
        "März": "03",
        "April": "04",
        "Mai": "05",
        "Juni": "06",
        "Juli": "07",
        "August": "08",
        "September": "09",
        "Oktober": "10",
        "November": "11",
        "Dezember": "12",
    }
    monthly = re.fullmatch(
        rf"({'|'.join(months)})\s+((?:19|20)\d{{2}})",
        value,
    )
    if monthly:
        month, year = monthly.groups()
        return f"{year}-{months[month]}"
    year = re.search(r"(?:19|20)\d{2}", value)
    return year.group(0) if year else ""


def tiff_tags(
    row: dict[str, str],
    metadata: dict[str, str],
    source_digest: str,
    digest: str,
    page_count: int,
) -> dict[str, str]:
    sequence = int(row["sequence"])
    title = (
        f"{metadata['title']}; Seite {sequence} von {page_count}"
    )
    description = (
        f"{metadata['title']}. Digitale Seitenrepräsentation: "
        f"Seite {sequence} von {page_count}; Archivsignatur "
        f"{metadata['inventory']}."
    )
    tags = tiff_metadata.expected_tags(
        tiff_metadata.PROFILE_V2,
        {
            "entity_id": (
                f"iss:wf-sender:{metadata['carrier_key']}:page:{sequence:03d}"
            ),
            "archival_label": metadata["inventory"],
            "descriptive_title": title,
            "description": description,
            "object_type": "Zeitungsseite",
            "date_created": xmp_date(metadata["date_text"]),
            "keywords": "WF-Sender; Betriebszeitung",
            "coverage": (
                f"Datierung laut Katalog: {metadata['date_text']}"
                if metadata["date_text"]
                else ""
            ),
            "source_collection": "Betriebszeitung WF-Sender",
        },
        {
            "case_id": RUN_ID,
            "source_file_id": Path(row["source_path"]).name,
            "source_digest": source_digest,
            "reviewed_bundle_digest": digest,
            "museum_digital_id": metadata["md_object_ids"],
            "representation_role": "preservation_master",
            "provenance_gaps": [],
        },
    )
    tags["XMP-dc:Relation"] += (
        f"; iss:carrier:{metadata['carrier_key']}; "
        f"iss:page:{sequence:03d}-of-{page_count:03d}"
    )
    tags["XMP-xmp:Label"] = PROFILE_TIFF
    return tags


def pdf_tags(
    row: dict[str, str],
    metadata: dict[str, str],
    source_digest: str,
) -> dict[str, str]:
    identifier = (
        f"iss:wf-sender:{metadata['carrier_key']}:pdf:{row['role']}"
    )
    description = (
        f"{metadata['title']}; Archivsignatur {metadata['inventory']}; "
        f"Rolle {row['role']}."
    )
    relation = "; ".join(
        value
        for value in (
            metadata["md_object_ids"],
            metadata["md_object_urls"],
        )
        if value
    )
    values = {
        "XMP-dc:Identifier": identifier,
        "XMP-dc:Title": metadata["title"],
        "XMP-dc:Description": description,
        "XMP-dc:Type": metadata["object_type"],
        "XMP-dc:Date": xmp_date(metadata["date_text"]),
        "XMP-dc:Coverage": (
            f"Datierung laut Katalog: {metadata['date_text']}"
            if metadata["date_text"]
            else ""
        ),
        "XMP-dc:Source": (
            f"WF-Sender accession {RUN_ID}; selected source "
            f"{row['source_path']}; source {source_digest}"
        ),
        "XMP-dc:Relation": relation,
        "XMP-dc:Subject": "WF-Sender; Betriebszeitung",
        "XMP-iptcExt:ArtworkSourceInventoryNo": metadata["inventory"],
        "XMP-xmp:Label": PROFILE_PDF,
    }
    return {key: value for key, value in values.items() if value}


def embed(path: Path, tags: dict[str, str]) -> None:
    arguments = ["exiftool", "-P", "-overwrite_original"]
    arguments.extend(f"-{key}={value}" for key, value in tags.items())
    arguments.append(str(path))
    result = run(arguments)
    output = (result.stdout + result.stderr).strip()
    if "Warning:" in output:
        raise AccessionError(f"ExifTool warning for {path}: {output}")
    with path.open("rb") as handle:
        os.fsync(handle.fileno())


def read_tags(path: Path, tags: dict[str, str]) -> dict[str, Any]:
    return tiff_accession._exif_values(path, tuple(tags))


def expected_readback(tags: dict[str, str]) -> dict[str, str]:
    """Mirror ExifTool's stable printable form without relaxing tag equality."""

    expected = dict(tags)
    date = expected.get("XMP-dc:Date", "")
    if re.fullmatch(r"(?:19|20)\d{2}(?:-\d{2}(?:-\d{2})?)?", date):
        expected["XMP-dc:Date"] = date.replace("-", ":")
    return expected


def pdf_pages(path: Path) -> int:
    output = run(["pdfinfo", str(path)]).stdout
    match = re.search(r"^Pages:\s+(\d+)\s*$", output, re.MULTILINE)
    if not match:
        raise AccessionError(f"cannot read PDF page count: {path}")
    return int(match.group(1))


def fsync_parent(path: Path) -> None:
    descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def receipt_path(status_root: Path, index: int) -> Path:
    return status_root / f"{index:05d}.json"


def validate_receipt(root: Path, receipt: dict[str, Any]) -> None:
    target = root / receipt["final_relative_path"]
    if not target.is_file() or target.is_symlink():
        raise AccessionError(f"completed target is absent: {target}")
    stat = target.stat()
    if (
        stat.st_size != receipt["size_bytes"]
        or stat.st_mtime_ns != receipt["mtime_ns"]
    ):
        raise AccessionError(f"completed target observation changed: {target}")


def process_representation(
    *,
    row: dict[str, str],
    index: int,
    root: Path,
    status_root: Path,
    preparing_root: Path,
    object_map: dict[str, dict[str, str]],
    digest: str,
    page_counts: dict[str, int],
) -> dict[str, Any]:
    receipt_file = receipt_path(status_root, index)
    if receipt_file.is_file():
        receipt = json.loads(receipt_file.read_text(encoding="utf-8"))
        validate_receipt(root, receipt)
        return receipt
    source = Path(row["source_path"]).resolve(strict=True)
    stat = source.stat()
    if (
        stat.st_size != int(row["source_size_bytes"])
        or stat.st_mtime_ns != int(row["source_mtime_ns"])
    ):
        raise AccessionError(f"source changed before copy: {source}")
    target = root / row["final_relative_path"]
    require_inside(target, root)
    if target.exists():
        raise AccessionError(f"unreceipted target already exists: {target}")
    mkdir(target.parent)
    mkdir(preparing_root)
    prepared = preparing_root / f"{index:05d}-{source.name}"
    require_inside(prepared, preparing_root)
    if prepared.exists():
        prepared.unlink()
    source_digest = copyhash_runner.copy_file(
        COPYHASH,
        source,
        prepared,
        row["source_sha256"],
    )
    metadata = metadata_for(row, object_map)
    embedding_profile = "exact_source_bytes"
    pixel_digest = ""
    metadata_readback: dict[str, Any] = {}
    technical_readback: dict[str, Any] = {}
    if row["role"] == "preservation_master_page":
        technical_before = tiff_accession._exif_values(
            prepared,
            tiff_accession.TECHNICAL_TAGS,
        )
        bit_depth = tiff_accession._scalar_sample_depth(
            technical_before.get("IFD0:BitsPerSample")
        )
        if tiff_accession._page_count(prepared) != 1:
            raise AccessionError(f"TIFF is not one image directory: {source}")
        pixel_before = tiff_accession._decoded_pixel_digest(prepared, bit_depth)
        tags = tiff_tags(
            row,
            metadata,
            source_digest,
            digest,
            page_counts[row["carrier_key"]],
        )
        embed(prepared, tags)
        metadata_readback = read_tags(prepared, tags)
        if metadata_readback != expected_readback(tags):
            raise AccessionError(f"TIFF metadata readback differs: {source}")
        technical_readback = tiff_accession._exif_values(
            prepared,
            tiff_accession.TECHNICAL_TAGS,
        )
        if technical_readback != technical_before:
            raise AccessionError(f"TIFF technical metadata changed: {source}")
        pixel_digest = tiff_accession._decoded_pixel_digest(prepared, bit_depth)
        if pixel_digest != pixel_before:
            raise AccessionError(f"decoded TIFF pixels changed: {source}")
        embedding_profile = PROFILE_TIFF
    elif row["role"] in {"access_pdf", "preservation_master_pdf"}:
        pages_before = pdf_pages(prepared)
        tags = pdf_tags(row, metadata, source_digest)
        embed(prepared, tags)
        metadata_readback = read_tags(prepared, tags)
        if metadata_readback != expected_readback(tags):
            raise AccessionError(f"PDF metadata readback differs: {source}")
        pages_after = pdf_pages(prepared)
        if pages_after != pages_before:
            raise AccessionError(f"PDF page count changed: {source}")
        technical_readback = {"pdf_pages": pages_after}
        embedding_profile = PROFILE_PDF
    final_digest = (
        source_digest
        if row["role"] in EXACT_ROLES
        else copyhash_runner.hash_file(COPYHASH, prepared)
    )
    if target.exists():
        raise AccessionError(f"target appeared during preparation: {target}")
    os.rename(prepared, target)
    fsync_parent(target)
    target_stat = target.stat()
    receipt = {
        "index": index,
        "completed_at": now(),
        "final_relative_path": row["final_relative_path"],
        "source_path": str(source),
        "source_sha256": source_digest,
        "final_sha256": final_digest,
        "size_bytes": target_stat.st_size,
        "mtime_ns": target_stat.st_mtime_ns,
        "role": row["role"],
        "carrier_key": row["carrier_key"],
        "inventory": row["inventory"],
        "sequence": row["sequence"],
        "basis": row["basis"],
        "embedding_profile": embedding_profile,
        "metadata_readback": metadata_readback,
        "technical_readback": technical_readback,
        "decoded_pixel_sha256": pixel_digest,
    }
    write_json(receipt_file, receipt)
    return receipt


def pilot(
    representations: list[dict[str, str]],
    object_map: dict[str, dict[str, str]],
    digest: str,
) -> None:
    root = RUN_ROOT / "preflight/metadata-pilot/package"
    status = RUN_ROOT / "preflight/metadata-pilot/status"
    preparing = RUN_ROOT / "preflight/metadata-pilot/preparing"
    page_counts = Counter(
        row["carrier_key"]
        for row in representations
        if row["role"] == "preservation_master_page"
    )
    tiff = min(
        (
            row
            for row in representations
            if row["role"] == "preservation_master_page"
        ),
        key=lambda row: int(row["source_size_bytes"]),
    )
    pdf = min(
        (
            row
            for row in representations
            if row["role"] == "access_pdf"
        ),
        key=lambda row: int(row["source_size_bytes"]),
    )
    results = []
    for index, row in enumerate((tiff, pdf), 1):
        results.append(
            process_representation(
                row=row,
                index=index,
                root=root,
                status_root=status,
                preparing_root=preparing,
                object_map=object_map,
                digest=digest,
                page_counts=dict(page_counts),
            )
        )
    write_json(
        RUN_ROOT / "preflight/metadata-pilot/summary.json",
        {
            "created_at": now(),
            "result": "ok",
            "representations": results,
        },
        replace=True,
    )


def load_receipts(status_root: Path) -> list[dict[str, Any]]:
    return [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(status_root.glob("*.json"))
    ]


def process_payloads(
    representations: list[dict[str, str]],
    object_map: dict[str, dict[str, str]],
    digest: str,
) -> list[dict[str, Any]]:
    status = RUN_ROOT / "status/payload"
    preparing = RUN_ROOT / "preparing/payload"
    page_counts = Counter(
        row["carrier_key"]
        for row in representations
        if row["role"] == "preservation_master_page"
    )
    pending = list(enumerate(representations, 1))
    unresolved: list[tuple[int, dict[str, str]]] = []
    for pass_number in (1, 2):
        unresolved = []
        for position, row in pending:
            try:
                process_representation(
                    row=row,
                    index=position,
                    root=PACKAGE,
                    status_root=status,
                    preparing_root=preparing,
                    object_map=object_map,
                    digest=digest,
                    page_counts=dict(page_counts),
                )
            except Exception as error:  # continue bounded traversal
                unresolved.append((position, row))
                append_error(
                    {
                        "at": now(),
                        "phase": "payload",
                        "pass": pass_number,
                        "index": position,
                        "source_path": row["source_path"],
                        "final_relative_path": row["final_relative_path"],
                        "error": str(error),
                        "traceback": traceback.format_exc(),
                    }
                )
            if position % 10 == 0 or position == len(representations):
                write_json(
                    RUN_ROOT / "progress.json",
                    {
                        "updated_at": now(),
                        "phase": "payload",
                        "pass": pass_number,
                        "completed": len(list(status.glob("*.json"))),
                        "total": len(representations),
                        "current_index": position,
                        "current_inventory": row["inventory"],
                        "unresolved_this_pass": len(unresolved),
                    },
                    replace=True,
                )
        if not unresolved:
            break
        pending = unresolved
    receipts = load_receipts(status)
    if unresolved or len(receipts) != len(representations):
        raise AccessionError(
            f"payload traversal ended with unresolved rows: {len(unresolved)}"
        )
    return receipts


def document_sources() -> list[dict[str, str]]:
    result = []
    for relative, final_path, description in DOCUMENTS:
        source = (SOURCE_MEDIA / "restore/archive" / relative).resolve(strict=True)
        if source.is_symlink() or not source.is_file():
            raise AccessionError(f"document source is not regular: {source}")
        result.append(
            {
                "source_path": str(source),
                "final_relative_path": final_path,
                "description": description,
            }
        )
    result.extend(
        [
            {
                "source_path": str((MD_ROOT / "objects.csv").resolve(strict=True)),
                "final_relative_path": "export/md/objects.csv",
                "description": "Immutable museum-digital collection metadata capture.",
            },
            {
                "source_path": str((MD_ROOT / "coverage.json").resolve(strict=True)),
                "final_relative_path": "export/md/coverage.json",
                "description": "Coverage receipt for the museum-digital capture.",
            },
        ]
    )
    return result


def process_documents() -> list[dict[str, Any]]:
    status = RUN_ROOT / "status/documents"
    mkdir(status)
    results = []
    unresolved = []
    for index, item in enumerate(document_sources(), 1):
        receipt_file = receipt_path(status, index)
        target = PACKAGE / item["final_relative_path"]
        if receipt_file.is_file():
            receipt = json.loads(receipt_file.read_text(encoding="utf-8"))
            validate_receipt(PACKAGE, receipt)
            results.append(receipt)
            continue
        try:
            source = Path(item["source_path"])
            mkdir(target.parent)
            digest = copyhash_runner.copy_file(COPYHASH, source, target)
            stat = target.stat()
            receipt = {
                "index": index,
                "completed_at": now(),
                "final_relative_path": item["final_relative_path"],
                "source_path": str(source),
                "source_sha256": digest,
                "final_sha256": digest,
                "size_bytes": stat.st_size,
                "mtime_ns": stat.st_mtime_ns,
                "role": (
                    "museum_digital_metadata_export"
                    if item["final_relative_path"].startswith("export/md/")
                    else "project_document"
                ),
                "description": item["description"],
                "relationship": "direct",
                "condition": "readable original bytes",
                "embedding_profile": "exact_source_bytes",
            }
            write_json(receipt_file, receipt)
            results.append(receipt)
        except Exception as error:
            unresolved.append(item)
            append_error(
                {
                    "at": now(),
                    "phase": "documents",
                    "index": index,
                    "source_path": item["source_path"],
                    "error": str(error),
                    "traceback": traceback.format_exc(),
                }
            )
    if unresolved:
        raise AccessionError(f"document accession errors: {len(unresolved)}")
    return results


def documentation_review(
    document_receipts: list[dict[str, Any]],
) -> list[dict[str, str]]:
    selected = {row["source_path"]: row for row in document_receipts}
    candidates = {
        row["source_path"]: row
        for row in read_csv(DOCUMENT_DISCOVERY / "local-candidates.csv")
    }
    reviews = []
    for row in read_csv(DOCUMENT_DISCOVERY / "document-review.csv"):
        source_path = str(Path(row["source_path"]).resolve())
        category = candidates.get(source_path, {}).get(
            "candidate_category",
            "contextual_candidate",
        )
        if source_path in selected:
            decision = "included"
            relationship = "direct"
            note = selected[source_path]["description"]
        elif category == "exhibition_documentation":
            decision = "excluded"
            relationship = "excluded"
            note = "Exhibition material is outside collection 552 unit scope."
        elif category == "project_documentation":
            decision = "excluded"
            relationship = "mixed"
            note = (
                "Reviewed working version, duplicate, administrative detail, "
                "or test derivative; retain at source."
            )
        else:
            decision = "excluded"
            relationship = "contextual"
            note = "Contextual research is not direct WF-Sender project documentation."
        reviews.append(
            {
                "source_path": source_path,
                "candidate_category": category,
                "relationship": relationship,
                "decision": decision,
                "note": note,
            }
        )
    return reviews


def disposition_rows(
    plan_rows: list[dict[str, str]],
    payload_receipts: list[dict[str, Any]],
    document_receipts: list[dict[str, Any]],
    reviews: list[dict[str, str]],
) -> list[dict[str, Any]]:
    selected: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for receipt in payload_receipts + document_receipts:
        selected[receipt["source_path"]].append(receipt)
    rows = {row["source_path"]: dict(row) for row in plan_rows}
    for source_path, receipts in selected.items():
        row = rows.get(
            source_path,
            {
                "source_path": source_path,
                "source_kind": "file",
                "unit": UNIT_ID,
                "inventory": "",
                "source_role": "",
                "final_path": "",
                "preservation_status": "",
                "preservation_basis": "",
                "source_sha256": "",
                "final_sha256": "",
                "source_size_bytes": receipts[0]["size_bytes"],
                "exact_copy_proven": "false",
                "drain_state": "not_yet_eligible",
                "deletion_authorized": "false",
                "notes": "",
            },
        )
        exact = all(
            receipt["embedding_profile"] == "exact_source_bytes"
            and receipt["source_sha256"] == receipt["final_sha256"]
            for receipt in receipts
        )
        row.update(
            {
                "final_path": ";".join(
                    sorted(receipt["final_relative_path"] for receipt in receipts)
                ),
                "preservation_status": (
                    "preserved_exact_copy"
                    if exact
                    else "preserved_metadata_enriched_copy"
                ),
                "preservation_basis": ";".join(
                    sorted({receipt["embedding_profile"] for receipt in receipts})
                ),
                "source_sha256": receipts[0]["source_sha256"],
                "final_sha256": ";".join(
                    sorted(receipt["final_sha256"] for receipt in receipts)
                ),
                "source_size_bytes": Path(source_path).stat().st_size,
                "exact_copy_proven": str(exact).lower(),
                "drain_state": (
                    "candidate_after_separate_review"
                    if exact
                    else "retain_source_not_exact_copy"
                ),
                "deletion_authorized": "false",
                "notes": (
                    "Accession complete; no deletion is authorized. "
                    + (
                        "Exact source bytes preserved."
                        if exact
                        else "Final representation has embedded metadata; retain source."
                    )
                ),
            }
        )
        rows[source_path] = row
    for review in reviews:
        source_path = review["source_path"]
        if source_path in rows:
            continue
        source = Path(source_path)
        rows[source_path] = {
            "source_path": source_path,
            "source_kind": "file",
            "unit": UNIT_ID,
            "inventory": "",
            "source_role": "documentation_candidate",
            "final_path": "",
            "preservation_status": "retain_not_preserved",
            "preservation_basis": review["relationship"],
            "source_sha256": "",
            "final_sha256": "",
            "source_size_bytes": source.stat().st_size,
            "exact_copy_proven": "false",
            "drain_state": "retain_source_not_preserved",
            "deletion_authorized": "false",
            "notes": review["note"],
        }
    return [rows[key] for key in sorted(rows)]


def catalog_rows(
    representations: list[dict[str, str]],
    payload_receipts: list[dict[str, Any]],
    document_receipts: list[dict[str, Any]],
    object_map: dict[str, dict[str, str]],
) -> list[dict[str, Any]]:
    receipt_by_path = {
        row["final_relative_path"]: row for row in payload_receipts
    }
    result = []
    for row in representations:
        receipt = receipt_by_path[row["final_relative_path"]]
        metadata = metadata_for(row, object_map)
        result.append(
            {
                "final_relative_path": row["final_relative_path"],
                "filename": Path(row["final_relative_path"]).name,
                "file_type": Path(row["final_relative_path"]).suffix.lower(),
                "carrier_key": row["carrier_key"],
                "inventory": row["inventory"],
                "role": row["role"],
                "sequence": row["sequence"],
                "title": metadata["title"],
                "description": (
                    f"{metadata['title']}; {row['role']}."
                ),
                "date_text": metadata["date_text"],
                "object_type": metadata["object_type"],
                "md_object_ids": metadata["md_object_ids"],
                "md_object_urls": metadata["md_object_urls"],
                "relationship_state": metadata["relationship_state"],
                "basis": row["basis"],
                "source_path": row["source_path"],
                "source_sha256": receipt["source_sha256"],
                "final_sha256": receipt["final_sha256"],
                "size_bytes": receipt["size_bytes"],
                "embedding_profile": receipt["embedding_profile"],
            }
        )
    for receipt in document_receipts:
        result.append(
            {
                "final_relative_path": receipt["final_relative_path"],
                "filename": Path(receipt["final_relative_path"]).name,
                "file_type": Path(receipt["final_relative_path"]).suffix.lower(),
                "carrier_key": "",
                "inventory": "",
                "role": receipt["role"],
                "sequence": "",
                "title": receipt["description"],
                "description": receipt["description"],
                "date_text": "",
                "object_type": "project documentation",
                "md_object_ids": "",
                "md_object_urls": "",
                "relationship_state": "direct",
                "basis": "bounded documentation appraisal",
                "source_path": receipt["source_path"],
                "source_sha256": receipt["source_sha256"],
                "final_sha256": receipt["final_sha256"],
                "size_bytes": receipt["size_bytes"],
                "embedding_profile": "exact_source_bytes",
            }
        )
    return sorted(result, key=lambda row: row["final_relative_path"])


def xlsx_bytes(
    rows: list[dict[str, Any]],
    fields: list[str],
    *,
    sheet: str,
    table: str,
) -> bytes:
    return paragon_document_bundle.table_xlsx_bytes(
        fields,
        [{field: str(row.get(field, "")) for field in fields} for row in rows],
        sheet_name=sheet,
        table_name=table,
    )


def build_restore(
    objects: list[dict[str, str]],
    catalog: list[dict[str, Any]],
    dispositions: list[dict[str, Any]],
    document_receipts: list[dict[str, Any]],
) -> None:
    db_root = PACKAGE / "db-restore"
    manifests = db_root / "manifests"
    mkdir(manifests)
    atomic_write(PACKAGE / "CATALOG.csv", csv_bytes(catalog, CATALOG_FIELDS))
    atomic_write(
        PACKAGE / "CATALOG.xlsx",
        xlsx_bytes(catalog, CATALOG_FIELDS, sheet="Catalog", table="Catalog"),
    )
    atomic_write(db_root / "RESTORE.csv", csv_bytes(catalog, CATALOG_FIELDS))
    atomic_write(
        db_root / "RESTORE.xlsx",
        xlsx_bytes(catalog, CATALOG_FIELDS, sheet="Restore", table="Restore"),
    )
    atomic_write(
        manifests / "representations.csv",
        csv_bytes(catalog, CATALOG_FIELDS),
    )
    disposition_fields = list(dispositions[0])
    atomic_write(
        manifests / "source-disposition.csv",
        csv_bytes(dispositions, disposition_fields),
    )
    documents = [
        {
            "final_relative_path": row["final_relative_path"],
            "source_path": row["source_path"],
            "description": row["description"],
            "relationship": "direct",
            "condition": "readable original bytes",
            "source_sha256": row["source_sha256"],
            "final_sha256": row["final_sha256"],
            "size_bytes": row["size_bytes"],
            "mtime_ns": row["mtime_ns"],
        }
        for row in document_receipts
        if row["role"] == "project_document"
    ]
    atomic_write(
        manifests / "documents.csv",
        csv_bytes(documents, DOCUMENT_FIELDS),
    )
    checksums = [
        {
            "path": row["final_relative_path"],
            "sha256": row["final_sha256"],
            "size_bytes": row["size_bytes"],
        }
        for row in catalog
    ]
    atomic_write(
        db_root / "checksums.csv",
        csv_bytes(checksums, CHECKSUM_FIELDS),
    )
    database_tmp = RUN_ROOT / "restore-building.sqlite"
    writable_path(database_tmp)
    if database_tmp.exists():
        database_tmp.unlink()
    connection = sqlite3.connect(database_tmp)
    try:
        connection.execute(
            "CREATE TABLE objects (" + ",".join(
                f'"{field}" TEXT NOT NULL' for field in objects[0]
            ) + ")"
        )
        connection.execute(
            "CREATE TABLE representations (" + ",".join(
                f'"{field}" TEXT NOT NULL' for field in CATALOG_FIELDS
            ) + ")"
        )
        connection.execute(
            "CREATE TABLE source_disposition (" + ",".join(
                f'"{field}" TEXT NOT NULL' for field in disposition_fields
            ) + ")"
        )
        connection.executemany(
            "INSERT INTO objects VALUES (" + ",".join("?" for _ in objects[0]) + ")",
            [[row[field] for field in objects[0]] for row in objects],
        )
        connection.executemany(
            "INSERT INTO representations VALUES ("
            + ",".join("?" for _ in CATALOG_FIELDS)
            + ")",
            [[str(row[field]) for field in CATALOG_FIELDS] for row in catalog],
        )
        connection.executemany(
            "INSERT INTO source_disposition VALUES ("
            + ",".join("?" for _ in disposition_fields)
            + ")",
            [
                [str(row.get(field, "")) for field in disposition_fields]
                for row in dispositions
            ],
        )
        connection.commit()
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise AccessionError("restore SQLite integrity failed")
        sql = "\n".join(connection.iterdump()) + "\n"
    finally:
        connection.close()
    os.rename(database_tmp, db_root / "RESTORE.sqlite")
    atomic_write(db_root / "RESTORE.sql", sql.encode("utf-8"))


def write_human_docs(
    catalog: list[dict[str, Any]],
    document_receipts: list[dict[str, Any]],
    reviews: list[dict[str, str]],
) -> None:
    lines = [
        "# WF-Sender project documentation",
        "",
        "Retained originals selected from the bounded collection-documentation review.",
        "Exhibition-only material, working duplicates, payroll records, OCR tests,",
        "and contextual research collections remain at source and are not part of",
        "this unit.",
        "",
    ]
    for row in sorted(
        (
            item
            for item in document_receipts
            if item["role"] == "project_document"
        ),
        key=lambda item: item["final_relative_path"],
    ):
        lines.extend(
            [
                f"## {Path(row['final_relative_path']).name}",
                "",
                f"- Description: {row['description']}",
                f"- Source: `{row['source_path']}`",
                "- Relationship: direct",
                "- Condition: readable original bytes",
                "",
            ]
        )
    atomic_write(
        PACKAGE / "documents/CONTENTS.md",
        ("\n".join(lines) + "\n").encode(),
    )
    review_fields = list(reviews[0])
    atomic_write(
        PACKAGE / "db-restore/manifests/documents-review.csv",
        csv_bytes(reviews, review_fields),
    )
    role_counts = Counter(row["role"] for row in catalog)
    readme = f"""# WF-Sender archive unit

This unit preserves the complete reconciled museum-digital collection 552,
`Betriebszeitung WF-Sender`, plus two accepted local-only carriers and bounded
direct project documentation.

- Carriers: 1,863
- Museum-digital object IDs represented: 1,862
- Archived payload and document files: {len(catalog)}
- TIFF preservation-master pages: {role_counts['preservation_master_page']}
- Damaged TIFF source fragments: {role_counts['damaged_source_tiff_fragment']}
- PDF access representations: {role_counts['access_pdf']}
- PDF preservation masters: {role_counts['preservation_master_pdf']}
- Project documents: {role_counts['project_document']}

TIFF masters use `{PROFILE_TIFF}` metadata with verified readback, unchanged
technical properties, and decoded-pixel equality. PDF access and preservation
representations use `{PROFILE_PDF}` XMP metadata with verified readback and
unchanged page counts. Damaged TIFF fragments and the combined special-edition
source carrier preserve exact source bytes.

Recovery begins with `db-restore/RESTORE.sqlite` or the paired SQL/CSV/XLSX
exports. `db-restore/checksums.csv` records one final SHA-256 for each archived
payload or retained document. `db-restore/manifests/source-disposition.csv`
records exact reviewed source paths and never authorizes deletion.
"""
    atomic_write(PACKAGE / "README.md", readme.encode())


def validate_package(
    objects: list[dict[str, str]],
    representations: list[dict[str, str]],
    payload_receipts: list[dict[str, Any]],
    document_receipts: list[dict[str, Any]],
    catalog: list[dict[str, Any]],
    dispositions: list[dict[str, Any]],
) -> dict[str, Any]:
    if len(objects) != 1863 or len(payload_receipts) != 7209:
        raise AccessionError("final object or payload count drifted")
    if Counter(row["role"] for row in payload_receipts) != Counter(ROLE_COUNTS):
        raise AccessionError("final role counts drifted")
    if any(row["deletion_authorized"] != "false" for row in dispositions):
        raise AccessionError("source disposition authorizes deletion")
    expected = payload_receipts + document_receipts
    for receipt in expected:
        validate_receipt(PACKAGE, receipt)
    if len(catalog) != len(expected):
        raise AccessionError("catalog does not cover every archived file")
    if len({row["final_relative_path"] for row in catalog}) != len(catalog):
        raise AccessionError("catalog contains duplicate final paths")
    checksum_rows = read_csv(PACKAGE / "db-restore/checksums.csv")
    if len(checksum_rows) != len(catalog):
        raise AccessionError("checksum inventory count drifted")
    connection = sqlite3.connect(
        f"file:{PACKAGE / 'db-restore/RESTORE.sqlite'}?mode=ro",
        uri=True,
    )
    try:
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise AccessionError("final restore database integrity failed")
        if connection.execute("SELECT COUNT(*) FROM objects").fetchone()[0] != 1863:
            raise AccessionError("restore object count drifted")
        if (
            connection.execute("SELECT COUNT(*) FROM representations").fetchone()[0]
            != len(catalog)
        ):
            raise AccessionError("restore representation count drifted")
    finally:
        connection.close()
    unwanted = [
        path
        for path in PACKAGE.rglob("*")
        if path.is_symlink()
        or ".tmp-" in path.name
        or path.name.endswith("_original")
    ]
    if unwanted:
        raise AccessionError(f"package contains temporary/symlink paths: {unwanted[:3]}")
    return {
        "validated_at": now(),
        "result": "ok",
        "objects": len(objects),
        "museum_digital_ids": len(
            {
                value
                for row in objects
                for value in row["md_object_ids"].split(";")
                if value
            }
        ),
        "payload_representations": len(payload_receipts),
        "documents_and_md_exports": len(document_receipts),
        "catalog_rows": len(catalog),
        "source_disposition_rows": len(dispositions),
        "planned_source_bytes": sum(
            int(row["source_size_bytes"]) for row in representations
        ),
    }


def publish(validation: dict[str, Any]) -> None:
    if FINAL_UNIT.exists():
        raise AccessionError(f"final unit already exists: {FINAL_UNIT}")
    if PACKAGE.stat().st_dev != FINAL_UNIT.parent.stat().st_dev:
        raise AccessionError("final promotion would cross filesystems")
    os.rename(PACKAGE, FINAL_UNIT)
    fsync_parent(FINAL_UNIT)
    post = dict(validation)
    post.update(
        {
            "published_at": now(),
            "final_unit": str(FINAL_UNIT),
            "promotion": "same-filesystem atomic rename",
            "media_rehashed_after_promotion": False,
        }
    )
    for receipt in load_receipts(RUN_ROOT / "status/payload") + load_receipts(
        RUN_ROOT / "status/documents"
    ):
        target = FINAL_UNIT / receipt["final_relative_path"]
        stat = target.stat()
        if (
            stat.st_size != receipt["size_bytes"]
            or stat.st_mtime_ns != receipt["mtime_ns"]
        ):
            raise AccessionError(f"post-promotion identity changed: {target}")
    write_json(RUN_ROOT / "publication.json", post)


def execute(*, pilot_only: bool = False) -> None:
    mounts = ensure_boundaries()
    objects, representations, plan_dispositions, object_map = load_inputs()
    digest = plan_digest()
    mkdir(RUN_ROOT)
    plan_receipt = RUN_ROOT / "plan.json"
    plan = {
        "format": "iss-wf-sender-accession-v1",
        "created_at": now(),
        "run_id": RUN_ID,
        "unit_id": UNIT_ID,
        "plan_root": str(PLAN_ROOT),
        "plan_digest": digest,
        "write_roots": [str(WORK_BASE), str(FINAL_BASE)],
        "script_root": str(TOOL_ROOT),
        "source_policy": "all sources read-only",
        "mounts": mounts,
        "objects": len(objects),
        "representations": len(representations),
        "role_counts": ROLE_COUNTS,
        "tiff_profile": PROFILE_TIFF,
        "pdf_profile": PROFILE_PDF,
        "error_policy": "log bounded row errors, retry once, block publication if unresolved",
    }
    if plan_receipt.exists():
        prior = json.loads(plan_receipt.read_text(encoding="utf-8"))
        if prior["plan_digest"] != digest:
            raise AccessionError("saved accession plan digest differs")
    else:
        write_json(plan_receipt, plan)
    pilot(representations, object_map, digest)
    if pilot_only:
        return
    payload_receipts = process_payloads(representations, object_map, digest)
    document_receipts = process_documents()
    reviews = documentation_review(document_receipts)
    dispositions = disposition_rows(
        plan_dispositions,
        payload_receipts,
        document_receipts,
        reviews,
    )
    catalog = catalog_rows(
        representations,
        payload_receipts,
        document_receipts,
        object_map,
    )
    build_restore(objects, catalog, dispositions, document_receipts)
    write_human_docs(catalog, document_receipts, reviews)
    validation = validate_package(
        objects,
        representations,
        payload_receipts,
        document_receipts,
        catalog,
        dispositions,
    )
    write_json(RUN_ROOT / "validation.json", validation)
    publish(validation)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument(
        "--pilot-only",
        action="store_true",
        help="run and validate one TIFF and one PDF without starting the full accession",
    )
    return result


def main() -> int:
    args = parser().parse_args()
    try:
        execute(pilot_only=args.pilot_only)
    except (
        AccessionError,
        copyhash_runner.CopyHashError,
        tiff_accession.TiffAccessionError,
        tiff_metadata.TiffMetadataError,
        OSError,
        sqlite3.Error,
    ) as error:
        append_error(
            {
                "at": now(),
                "phase": "fatal",
                "error": str(error),
                "traceback": traceback.format_exc(),
            }
        )
        raise SystemExit(f"error: {error}") from error
    print(FINAL_UNIT if not args.pilot_only else RUN_ROOT / "preflight/metadata-pilot")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
