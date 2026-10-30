#!/usr/bin/env python3
"""Read-only WF-Sender local discovery from an existing museum-digital capture.

This is deliberately a discovery/reconciliation tool, not an accession tool:

* the recovery source must be mounted read-only;
* source files are never copied, renamed, hashed, or modified;
* all discovered files remain ``needs_review``;
* museum-digital data is parsed from an immutable prior capture;
* bounded text extraction is used only to identify documentation candidates.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import html
import io
import json
import os
import re
import subprocess
import tempfile
import unicodedata
import zipfile
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse


DEFAULT_SOURCE = Path("/media/vladimir/Elements/restore/archive")
DEFAULT_CAPTURE = Path(
    "/mnt/volume/iss-archive-work/produktion-propaganda-wf-sender/work/"
    "reconciliation/museum-digital/runs/"
    "20260726T191434Z-produktion-propaganda-wf-sender/"
    "raw/page-0001-offset-000000.json"
)
DEFAULT_OUTPUT_ROOT = Path(
    "/mnt/volume/iss-archive-work/produktion-propaganda-wf-sender/work/"
    "reconciliation/local-discovery/runs"
)
SAFE_WORK_ROOT = Path("/mnt/volume/iss-archive-work")

ISSUE_KEYS = {
    "WFS-1950-07": ("1950", "07", "7/50"),
    "WFS-1960-22": ("1960", "22", "22/60"),
    "WFS-1970-21": ("1970", "21", "21/70"),
    "WFS-1980-23": ("1980", "23", "23/80"),
    "WFS-1989-24": ("1989", "24", "24/89"),
    "WFS-1990-13": ("1990", "13", "13/90"),
}

DOCUMENT_EXTENSIONS = {
    ".csv", ".doc", ".docx", ".htm", ".html", ".ods", ".odt", ".pdf",
    ".ppt", ".pptx", ".rtf", ".txt", ".xls", ".xlsx", ".xml",
}
PLAIN_EXTENSIONS = {".csv", ".htm", ".html", ".rtf", ".txt", ".xml"}
OOXML_EXTENSIONS = {".docx", ".xlsx", ".pptx", ".odt", ".ods"}

CONTENT_TERMS = {
    "exhibition_title": re.compile(
        r"produktion\s*(?:&|und)\s*propaganda|produkts?propaganda",
        re.IGNORECASE,
    ),
    "wf_sender": re.compile(r"\bwf[\s_-]*sender\b", re.IGNORECASE),
    "vitrine": re.compile(r"\bvitrine\b", re.IGNORECASE),
    "bzg_1": re.compile(r"\bbzg\s*1\b", re.IGNORECASE),
    "raketensonde": re.compile(r"\braketen(?:sonde|sonden)\b", re.IGNORECASE),
    "mrs_3000": re.compile(r"\bmrs\s*3000\b", re.IGNORECASE),
    "feb_008_839": re.compile(r"\bfeb[\s_-]*008[\s_-]*839\b", re.IGNORECASE),
    "feb_008_910": re.compile(r"\bfeb[\s_-]*008[\s_-]*910\b", re.IGNORECASE),
    "kinderarbeit": re.compile(r"\bkinderarbeit\b", re.IGNORECASE),
    "polytechnic": re.compile(r"\bpolytechn", re.IGNORECASE),
    "landesverteidigung": re.compile(r"\blandesverteidigung\b", re.IGNORECASE),
}

PROJECT_DOC_WORDS = (
    "antrag", "arbeitsvertrag", "basisaufnahme", "bewilligung", "expose",
    "finanz", "förder", "foerder", "inhalt", "kosten", "kategorie",
    "konzept", "lieferschein", "projekt", "sachbericht", "sender",
)
PROJECT_DOC_PATHS = (
    "/wf/wf-sender projekt/archiv/",
    "/wf/wf-sender projekt/sachbericht/",
    "/wf/wf-sender projekt/beauftragter zur aufarbeitung",
)
BULK_PATH_PARTS = (
    "/scans/", "/uploads_alle-pdf-geordnet/", "/webseite/", "/brotkrumen/",
    "/mik-daten/", "/pdf/", "/tif/",
)


class DiscoveryError(RuntimeError):
    """A failed discovery precondition."""


def normalize(value: str) -> str:
    value = unicodedata.normalize("NFKD", value)
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]+", "", value.casefold())


def flatten(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, list):
        return " | ".join(filter(None, (flatten(item) for item in value)))
    if isinstance(value, dict):
        return " | ".join(
            filter(None, (flatten(value[key]) for key in sorted(value)))
        )
    return str(value)


def csv_bytes(rows: Iterable[dict[str, Any]], fields: list[str]) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({field: row.get(field, "") for field in fields})
    return output.getvalue().encode("utf-8")


def atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    with temporary.open("xb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def write_csv(path: Path, rows: Iterable[dict[str, Any]], fields: list[str]) -> None:
    atomic_write(path, csv_bytes(rows, fields))


def ensure_preconditions(source: Path, capture: Path, output_root: Path) -> str:
    source = source.resolve()
    capture = capture.resolve()
    output_root = output_root.resolve()
    if not source.is_dir():
        raise DiscoveryError(f"source directory is unavailable: {source}")
    if not capture.is_file():
        raise DiscoveryError(f"museum-digital capture is unavailable: {capture}")
    if not output_root.is_relative_to(SAFE_WORK_ROOT):
        raise DiscoveryError(f"output root is outside safe work root: {output_root}")
    if output_root.is_relative_to(source):
        raise DiscoveryError("output root must not be on the recovery source")
    result = subprocess.run(
        ["findmnt", "-n", "-o", "SOURCE,TARGET,FSTYPE,OPTIONS", "--target", str(source)],
        check=True,
        capture_output=True,
        text=True,
    )
    fields = result.stdout.strip().split(maxsplit=3)
    if len(fields) != 4 or "ro" not in fields[3].split(","):
        raise DiscoveryError(
            f"recovery source mount is not read-only: {result.stdout.strip()}"
        )
    return result.stdout.strip()


def load_objects(capture: Path) -> list[dict[str, Any]]:
    payload = json.loads(capture.read_text(encoding="utf-8"))
    objects = payload.get("results", {}).get("objects", {})
    if not isinstance(objects, dict) or len(objects) != 8:
        raise DiscoveryError(
            f"expected 8 museum-digital objects, found {len(objects)}"
        )
    return list(objects.values())


def issue_keys_for_text(value: str) -> set[str]:
    found: set[str] = set()
    for key, (year, number, _notation) in ISSUE_KEYS.items():
        short_year = year[-2:]
        patterns = (
            rf"\bWFS?[\s_-]*{year}[\s_-]*0?{int(number)}\b",
            (
                rf"\bWF[\s_-]*Sender\b.{{0,80}}?"
                rf"(?<!\d)0?{int(number)}\s*/\s*{short_year}(?!\d)"
            ),
            (
                rf"(?<!\d)0?{int(number)}\s*/\s*{short_year}(?!\d)"
                rf".{{0,80}}?\bWF[\s_-]*Sender\b"
            ),
        )
        if any(re.search(pattern, value, re.IGNORECASE) for pattern in patterns):
            found.add(key)
    return found


def inventory_refs(value: str) -> set[str]:
    refs: set[str] = set()
    patterns = (
        r"Inv\.-Nr\.\s*([A-Za-zÄÖÜäöü0-9][A-Za-zÄÖÜäöü0-9_.\-/ ]{2,40})",
        r"\b(FEB[\s_-]*008[\s_-]*(?:839|910))\b",
        r"\b(KS[\s_-]*7[\s_-]*WF[_-]*0046)\b",
        r"\b(TFA(?:-NEG)?-\d{6,8})\b",
        r"\b(Object[_-]\d+(?:[_-]\d+)?)\b",
        r"\bInv\.\s*(\d{7,8})\b",
    )
    for pattern in patterns:
        for match in re.finditer(pattern, value, flags=re.IGNORECASE):
            candidate = match.group(1).strip().rstrip(".,;:)]}")
            candidate = candidate.splitlines()[0].strip()
            if candidate:
                refs.add(candidate)
    return refs


def parse_md(
    objects: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, set[str]]]:
    object_rows: list[dict[str, Any]] = []
    resource_rows: list[dict[str, Any]] = []
    ref_to_objects: dict[str, set[str]] = defaultdict(set)

    for obj in objects:
        object_id = str(obj.get("object_id", ""))
        combined = " | ".join(
            (
                flatten(obj.get("object_name")),
                flatten(obj.get("object_inventory_number")),
                flatten(obj.get("object_description")),
            )
        )
        object_issues = issue_keys_for_text(combined)
        object_rows.append(
            {
                "object_id": object_id,
                "inventory_number": flatten(obj.get("object_inventory_number")),
                "name": flatten(obj.get("object_name")),
                "type": flatten(obj.get("object_type")),
                "description": flatten(obj.get("object_description")),
                "institution": flatten(obj.get("object_institution")),
                "collections": flatten(obj.get("object_collection")),
                "events": flatten(obj.get("object_events")),
                "tags": flatten(obj.get("object_tags")),
                "issue_keys": " | ".join(sorted(object_issues)),
                "last_updated": flatten(obj.get("object_last_updated")),
            }
        )

        for resource in obj.get("object_images", []):
            description = flatten(resource.get("beschreibung"))
            filename_or_url = flatten(resource.get("filename_loc"))
            refs = inventory_refs(" | ".join((description, filename_or_url)))
            issues = object_issues | issue_keys_for_text(description)
            basename = Path(urlparse(filename_or_url).path).name
            for ref in refs:
                ref_to_objects[normalize(ref)].add(object_id)
            if basename:
                ref_to_objects[normalize(basename)].add(object_id)
            for issue in issues:
                ref_to_objects[normalize(issue)].add(object_id)
            resource_rows.append(
                {
                    "object_id": object_id,
                    "object_inventory_number": flatten(
                        obj.get("object_inventory_number")
                    ),
                    "object_name": flatten(obj.get("object_name")),
                    "resource_id": flatten(resource.get("quell_id")),
                    "order": flatten(resource.get("order")),
                    "resource_type": flatten(resource.get("type")),
                    "filename_or_url": filename_or_url,
                    "source_basename": basename,
                    "description": description,
                    "inventory_refs": " | ".join(sorted(refs)),
                    "issue_keys": " | ".join(sorted(issues)),
                    "owner": flatten(resource.get("owner")),
                    "creator": flatten(resource.get("creator")),
                    "origin": flatten(resource.get("herkunft")),
                    "rights": flatten(resource.get("rights")),
                    "is_main": flatten(resource.get("is_main")),
                }
            )
    return object_rows, resource_rows, ref_to_objects


def exact_issue_matches(path_text: str) -> set[str]:
    normalized = normalize(path_text)
    found: set[str] = set()
    for key, (year, number, _) in ISSUE_KEYS.items():
        variants = (
            normalize(key),
            normalize(f"WFS_{year}-{number}"),
            normalize(f"WFS-{year}-{int(number)}"),
        )
        if any(variant in normalized for variant in variants):
            found.add(key)
    return found


def classify(
    relative: str,
    suffix: str,
    rules: set[str],
    issues: set[str],
) -> tuple[str, str]:
    lower = f"/{relative.casefold()}"
    basename = Path(relative).name
    if basename.startswith("~$"):
        return "temporary_or_noise", "excluded"
    if issues:
        if "/mik-daten/tif/" in lower or suffix in {".tif", ".tiff"}:
            return "preservation_media", "direct"
        if "/mik-daten/pdf/" in lower:
            return "preservation_media", "direct"
        if any(part in lower for part in ("/scans/", "/uploads_", "/webseite/")):
            return "access_or_upload_copy", "direct"
        return "issue_candidate", "direct"
    if "exact_md_inventory_ref" in rules:
        return "object_or_resource_candidate", "direct"
    if "exact_md_pdf_basename" in rules:
        return "access_or_upload_copy", "direct"
    if "/wf/forschungs- und entwicklungsberichte/" in lower:
        return "research_metadata_or_report", "needs_review"
    if lower.startswith("/sammlung aeb/"):
        return "contextual_research", "needs_review"
    if "/wf/ausstellung/" in lower:
        return "exhibition_documentation", "needs_review"
    if "project_documentation" in rules:
        return "project_documentation", "needs_review"
    return "contextual_candidate", "needs_review"


def bounded_project_document(relative: str, suffix: str) -> bool:
    if suffix not in DOCUMENT_EXTENSIONS:
        return False
    lower = f"/{relative.casefold()}"
    if not lower.startswith("/wf/wf-sender projekt/"):
        return False
    if any(part in lower for part in BULK_PATH_PARTS):
        return False
    return (
        any(path in lower for path in PROJECT_DOC_PATHS)
        or any(word in Path(relative).name.casefold() for word in PROJECT_DOC_WORDS)
    )


def bounded_special_document(relative: str, suffix: str) -> bool:
    if suffix not in DOCUMENT_EXTENSIONS:
        return False
    lower = f"/{relative.casefold()}"
    if lower.startswith("/wf/ausstellung/"):
        return True
    if lower.startswith("/wf/forschungs- und entwicklungsberichte/"):
        name = Path(relative).name.casefold()
        return suffix in {".xls", ".xlsx", ".csv"} or any(
            term in name for term in ("liste", "autor", "008", "mrs", "bzg")
        )
    if lower.startswith("/sammlung aeb/"):
        return True
    return False


def discover_paths(
    source: Path,
    ref_to_objects: dict[str, set[str]],
) -> tuple[list[dict[str, Any]], list[Path], list[Path], list[dict[str, Any]]]:
    rows: list[dict[str, Any]] = []
    content_queue: list[Path] = []
    zip_queue: list[Path] = []
    errors: list[dict[str, Any]] = []
    reference_keys = set(ref_to_objects)

    for directory, _, filenames in os.walk(source):
        for filename in filenames:
            path = Path(directory, filename)
            try:
                relative = path.relative_to(source).as_posix()
                suffix = path.suffix.casefold()
                normalized_path = normalize(relative)
                rules: set[str] = set()
                issues = exact_issue_matches(relative)
                matched_refs = {
                    ref for ref in reference_keys if ref and ref in normalized_path
                }
                issue_ref_keys = {normalize(issue) for issue in issues}

                if issues:
                    rules.add("exact_issue_source")
                for ref in matched_refs:
                    if ref in issue_ref_keys:
                        continue
                    if ref.endswith("pdf") and ref.isdigit() is False:
                        rules.add("exact_md_pdf_basename")
                    else:
                        rules.add("exact_md_inventory_ref")

                is_project_doc = bounded_project_document(relative, suffix)
                is_special_doc = bounded_special_document(relative, suffix)
                if is_project_doc:
                    rules.add("project_documentation")
                if is_special_doc:
                    rules.add("bounded_subject_document")

                filename_lower = filename.casefold()
                if any(
                    term in filename_lower
                    for term in (
                        "produktion", "propaganda", "raketensonde",
                        "kinderarbeit", "bzg", "wf-sender",
                    )
                ):
                    rules.add("subject_keyword")

                if not rules:
                    continue

                stat = path.stat()
                object_ids: set[str] = set()
                for ref in matched_refs:
                    object_ids.update(ref_to_objects[ref])
                for issue in issues:
                    object_ids.update(ref_to_objects.get(normalize(issue), set()))
                category, relation = classify(relative, suffix, rules, issues)
                rows.append(
                    {
                        "source_path": str(path),
                        "relative_path": relative,
                        "filename": filename,
                        "extension": suffix,
                        "size_bytes": stat.st_size,
                        "mtime_ns": stat.st_mtime_ns,
                        "candidate_category": category,
                        "relation_suggestion": relation,
                        "review_state": (
                            "excluded" if relation == "excluded" else "needs_review"
                        ),
                        "match_rules": " | ".join(sorted(rules)),
                        "issue_keys": " | ".join(sorted(issues)),
                        "matched_md_refs": " | ".join(sorted(matched_refs)),
                        "md_object_ids": " | ".join(sorted(object_ids)),
                        "note": (
                            "Discovery candidate only; source remains read-only. "
                            "No inclusion/accession decision made."
                        ),
                    }
                )
                if (
                    (is_project_doc or is_special_doc)
                    and stat.st_size <= 40_000_000
                    and not filename.startswith("~$")
                ):
                    content_queue.append(path)
                if suffix == ".zip" and (
                    is_project_doc
                    or "/wf/googledocsbackup/" in f"/{relative.casefold()}"
                    or "/wf/wf-sender projekt/" in f"/{relative.casefold()}"
                ):
                    zip_queue.append(path)
            except (OSError, ValueError) as error:
                errors.append(
                    {
                        "stage": "path_discovery",
                        "path": str(path),
                        "error": f"{type(error).__name__}: {error}",
                    }
                )
    rows.sort(key=lambda row: row["relative_path"].casefold())
    return rows, sorted(set(content_queue)), sorted(set(zip_queue)), errors


def clean_xml_text(payload: bytes) -> str:
    text = payload.decode("utf-8", errors="replace")
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def extract_text(path: Path) -> tuple[str, str]:
    suffix = path.suffix.casefold()
    if suffix in PLAIN_EXTENSIONS:
        return path.read_text(encoding="utf-8", errors="replace"), "plain"
    if suffix in OOXML_EXTENSIONS:
        fragments: list[str] = []
        with zipfile.ZipFile(path) as archive:
            for name in archive.namelist():
                lower = name.casefold()
                if lower.endswith(".xml") and (
                    lower.startswith(("word/", "xl/", "ppt/", "content.xml"))
                    or lower == "content.xml"
                ):
                    fragments.append(clean_xml_text(archive.read(name)))
        return "\n".join(fragments), "zip_xml"
    if suffix == ".pdf":
        result = subprocess.run(
            ["pdftotext", "-layout", "-nopgbrk", str(path), "-"],
            check=False,
            capture_output=True,
            text=True,
            timeout=45,
        )
        if result.returncode:
            raise DiscoveryError(
                f"pdftotext returned {result.returncode}: {result.stderr.strip()}"
            )
        return result.stdout, "pdftotext"
    if suffix == ".xls":
        result = subprocess.run(
            ["xls2csv", str(path)],
            check=False,
            capture_output=True,
            text=True,
            timeout=45,
        )
        if result.returncode:
            raise DiscoveryError(
                f"xls2csv returned {result.returncode}: {result.stderr.strip()}"
            )
        return result.stdout, "xls2csv"
    return "", "unsupported"


def content_matches(
    source: Path,
    paths: list[Path],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    match_rows: list[dict[str, Any]] = []
    review_rows: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for path in paths:
        relative = path.relative_to(source).as_posix()
        try:
            text, method = extract_text(path)
            term_names = [
                name for name, pattern in CONTENT_TERMS.items() if pattern.search(text)
            ]
            issues = issue_keys_for_text(text)
            snippets: list[str] = []
            for name in term_names[:8]:
                match = CONTENT_TERMS[name].search(text)
                if not match:
                    continue
                start = max(0, match.start() - 110)
                end = min(len(text), match.end() + 180)
                snippets.append(re.sub(r"\s+", " ", text[start:end]).strip())
            direct_signal = bool(
                {"exhibition_title", "feb_008_839", "feb_008_910"} & set(term_names)
                or (
                    issues
                    and relative.casefold().startswith("wf/wf-sender projekt/")
                )
            )
            review_rows.append(
                {
                    "source_path": str(path),
                    "relative_path": relative,
                    "extraction_method": method,
                    "text_characters": len(text),
                    "matched_terms": " | ".join(term_names),
                    "issue_keys": " | ".join(sorted(issues)),
                    "relation_suggestion": "direct" if direct_signal else "needs_review",
                    "review_state": "needs_review",
                    "note": (
                        "Content signal only; curator must confirm direct, mixed, "
                        "contextual, or excluded."
                    ),
                }
            )
            if term_names or issues:
                match_rows.append(
                    {
                        "source_path": str(path),
                        "relative_path": relative,
                        "matched_terms": " | ".join(term_names),
                        "issue_keys": " | ".join(sorted(issues)),
                        "snippets": " || ".join(snippets),
                    }
                )
        except (OSError, RuntimeError, subprocess.SubprocessError, zipfile.BadZipFile) as error:
            errors.append(
                {
                    "stage": "content_search",
                    "path": str(path),
                    "error": f"{type(error).__name__}: {error}",
                }
            )
    return match_rows, review_rows, errors


def inspect_zip_members(
    source: Path,
    paths: list[Path],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    rows: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for path in paths:
        try:
            with zipfile.ZipFile(path) as archive:
                for info in archive.infolist():
                    if info.is_dir():
                        continue
                    issues = exact_issue_matches(info.filename)
                    lower = info.filename.casefold()
                    terms = [
                        term for term in (
                            "produktion", "propaganda", "wf-sender", "raketensonde",
                            "bzg", "kinderarbeit", "vitrine", "sachbericht",
                            "projekt", "ausstellung",
                        )
                        if term in lower
                    ]
                    if not issues and not terms:
                        continue
                    rows.append(
                        {
                            "container_path": str(path),
                            "container_relative_path": path.relative_to(source).as_posix(),
                            "member_path": info.filename,
                            "member_size_bytes": info.file_size,
                            "member_crc32": f"{info.CRC:08x}",
                            "matched_terms": " | ".join(terms),
                            "issue_keys": " | ".join(sorted(issues)),
                            "review_state": "needs_review",
                            "note": "ZIP inspected by directory only; member not extracted.",
                        }
                    )
        except (OSError, zipfile.BadZipFile) as error:
            errors.append(
                {
                    "stage": "zip_member_search",
                    "path": str(path),
                    "error": f"{type(error).__name__}: {error}",
                }
            )
    rows.sort(key=lambda row: (
        row["container_relative_path"].casefold(),
        row["member_path"].casefold(),
    ))
    return rows, errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--capture", type=Path, default=DEFAULT_CAPTURE)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument(
        "--run-id",
        help="Unique run directory name (default: current UTC timestamp).",
    )
    args = parser.parse_args()

    source = args.source.resolve()
    capture = args.capture.resolve()
    output_root = args.output_root.resolve()
    mount_state = ensure_preconditions(source, capture, output_root)
    run_id = args.run_id or (
        dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        + "-wf-sender-local-discovery"
    )
    run_dir = output_root / run_id
    run_dir.mkdir(parents=True, exist_ok=False)

    objects = load_objects(capture)
    object_rows, resource_rows, ref_to_objects = parse_md(objects)
    candidates, content_queue, zip_queue, path_errors = discover_paths(
        source, ref_to_objects
    )
    matches, document_rows, content_errors = content_matches(source, content_queue)
    zip_rows, zip_errors = inspect_zip_members(source, zip_queue)
    errors = path_errors + content_errors + zip_errors

    write_csv(
        run_dir / "md-objects.csv",
        object_rows,
        [
            "object_id", "inventory_number", "name", "type", "description",
            "institution", "collections", "events", "tags", "issue_keys",
            "last_updated",
        ],
    )
    write_csv(
        run_dir / "md-resources.csv",
        resource_rows,
        [
            "object_id", "object_inventory_number", "object_name", "resource_id",
            "order", "resource_type", "filename_or_url", "source_basename",
            "description", "inventory_refs", "issue_keys", "owner", "creator",
            "origin", "rights", "is_main",
        ],
    )
    write_csv(
        run_dir / "local-candidates.csv",
        candidates,
        [
            "source_path", "relative_path", "filename", "extension", "size_bytes",
            "mtime_ns", "candidate_category", "relation_suggestion", "review_state",
            "match_rules", "issue_keys", "matched_md_refs", "md_object_ids", "note",
        ],
    )
    write_csv(
        run_dir / "document-review.csv",
        document_rows,
        [
            "source_path", "relative_path", "extraction_method", "text_characters",
            "matched_terms", "issue_keys", "relation_suggestion", "review_state",
            "note",
        ],
    )
    write_csv(
        run_dir / "content-matches.csv",
        matches,
        [
            "source_path", "relative_path", "matched_terms", "issue_keys", "snippets",
        ],
    )
    write_csv(
        run_dir / "container-members.csv",
        zip_rows,
        [
            "container_path", "container_relative_path", "member_path",
            "member_size_bytes", "member_crc32", "matched_terms", "issue_keys",
            "review_state", "note",
        ],
    )
    write_csv(run_dir / "errors.csv", errors, ["stage", "path", "error"])

    category_counts = Counter(row["candidate_category"] for row in candidates)
    issue_counts = Counter()
    for row in candidates:
        for issue in row["issue_keys"].split(" | "):
            if issue:
                issue_counts[issue] += 1
    exact_pdf_basenames = {
        row["source_basename"]
        for row in resource_rows
        if row["resource_type"] == "text" and row["source_basename"]
    }
    matched_pdf_basenames = {
        Path(row["source_path"]).name
        for row in candidates
        if "exact_md_pdf_basename" in row["match_rules"]
    }
    summary = {
        "run_id": run_id,
        "created_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "source_root": str(source),
        "source_mount": mount_state,
        "source_policy": "read-only; no copy; no hash; no mutation",
        "museum_digital_capture": str(capture),
        "museum_digital_objects": len(object_rows),
        "museum_digital_resources": len(resource_rows),
        "museum_digital_pdf_resources": len(exact_pdf_basenames),
        "exact_md_pdf_basenames_found_locally": sorted(matched_pdf_basenames),
        "exact_md_pdf_basenames_not_found_by_path": sorted(
            exact_pdf_basenames - matched_pdf_basenames
        ),
        "local_candidates": len(candidates),
        "candidate_categories": dict(sorted(category_counts.items())),
        "candidate_counts_by_issue": dict(sorted(issue_counts.items())),
        "documents_searched": len(document_rows),
        "documents_with_content_matches": len(matches),
        "zip_members_with_path_matches": len(zip_rows),
        "errors": len(errors),
        "decision_state": (
            "All filesystem and container candidates remain needs_review. "
            "No accession, deletion, or no_match_found decision was made."
        ),
    }
    atomic_write(
        run_dir / "summary.json",
        (json.dumps(summary, indent=2, ensure_ascii=False) + "\n").encode("utf-8"),
    )
    readme = f"""# WF-Sender local discovery

Run: `{run_id}`

This run parses the existing immutable museum-digital collection 1046 capture and
searches the read-only recovery source for local reconciliation candidates.

## Boundaries

- Source mount: `{mount_state}`
- No source file was copied, renamed, hashed, or modified.
- Candidate status is discovery-only: every non-noise record remains
  `needs_review`.
- `exact_md_pdf_basenames_not_found_by_path` means only that the numeric
  museum-digital upload filename was not present in a source path. It is not a
  final `no_match_found` decision.
- Documentation signals are suggestions. Curatorial review must classify each
  item as direct, mixed, contextual, or excluded before accession.

## Outputs

- `md-objects.csv`: all eight collection objects.
- `md-resources.csv`: image and text resources with local reconciliation keys.
- `local-candidates.csv`: path-level local candidates and match basis.
- `document-review.csv`: bounded documents searched and suggested relation.
- `content-matches.csv`: content terms and short diagnostic snippets.
- `container-members.csv`: matching ZIP directory entries; not extracted.
- `errors.csv`: non-fatal discovery/extraction errors.
- `summary.json`: counts and explicit decision boundary.
"""
    atomic_write(run_dir / "README.md", readme.encode("utf-8"))
    print(json.dumps({"run_dir": str(run_dir), **summary}, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
