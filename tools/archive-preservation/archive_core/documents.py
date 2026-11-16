"""PDF preservation inspection and exact-byte copy primitives."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import urllib.parse
import urllib.request
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import catalog_core

from archive_core import ArchiveError


PDF_PLAN_FORMAT = "iss-pdf-reconciliation-v1"
PDF_MAX_BYTES = 768 * 1024 * 1024
PDF_RESOURCE_HOSTS = {
    "berlin.museum-digital.de",
    "www.museum-digital.de",
    "museum-digital.de",
}
PDF_RESOURCE_PREFIX = "/data/berlin/resources/documents/"
DOCUMENT_STUB_SUFFIXES = {
    ".csv",
    ".doc",
    ".docx",
    ".md",
    ".ods",
    ".odt",
    ".rtf",
    ".tsv",
    ".txt",
    ".xls",
    ".xlsx",
}


class DocumentError(ArchiveError):
    """Raised when a document cannot cross the preservation boundary."""


def sha256_path(path: Path, chunk_size: int = 8 * 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def _tool_version(command: str) -> str:
    executable = shutil.which(command)
    if not executable:
        raise DocumentError(f"required PDF tool is unavailable: {command}")
    result = subprocess.run(
        [executable, "-v"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
        timeout=15,
    )
    first = result.stdout.splitlines()[0].strip() if result.stdout else ""
    if result.returncode != 0 or not first:
        raise DocumentError(f"cannot determine {command} version")
    return first


def _pdfinfo(path: Path) -> dict[str, str]:
    result = subprocess.run(
        ["pdfinfo", "-isodates", str(path)],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
        timeout=120,
    )
    if result.returncode != 0:
        raise DocumentError(f"pdfinfo rejected {path}: {result.stdout.strip()}")
    values: dict[str, str] = {}
    for line in result.stdout.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        values[key.strip().casefold().replace(" ", "_")] = value.strip()
    return values


def _sample_text_status(path: Path, encrypted: bool) -> str:
    if encrypted:
        return "encrypted_ocr_blocked"
    result = subprocess.run(
        ["pdftotext", "-f", "1", "-l", "10", str(path), "-"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=120,
    )
    if result.returncode != 0:
        return "ocr_assessment_needed"
    text = result.stdout.decode("utf-8", errors="replace")
    visible = re.sub(r"\s+", "", text)
    return (
        "embedded_text_detected_sample"
        if len(visible) >= 20
        else "ocr_assessment_needed"
    )


def inspect_pdf(path: Path) -> dict[str, Any]:
    """Validate and describe one PDF without modifying it."""

    resolved = path.expanduser().resolve(strict=True)
    if not resolved.is_file() or resolved.is_symlink():
        raise DocumentError(f"PDF source is not a regular file: {resolved}")
    size = resolved.stat().st_size
    if size <= 0 or size > PDF_MAX_BYTES:
        raise DocumentError(f"PDF size is outside the guarded range: {size}")
    with resolved.open("rb") as handle:
        prefix = handle.read(1024)
    match = re.search(rb"%PDF-(\d\.\d)", prefix)
    if not match:
        raise DocumentError(f"PDF signature is missing: {resolved}")
    info = _pdfinfo(resolved)
    try:
        pages = int(info.get("pages", "0"))
    except ValueError as error:
        raise DocumentError("pdfinfo returned an invalid page count") from error
    if pages <= 0:
        raise DocumentError("PDF must contain at least one page")
    encrypted = info.get("encrypted", "").casefold().startswith("yes")
    stat = resolved.stat()
    return {
        "path": str(resolved),
        "sha256": sha256_path(resolved),
        "size_bytes": size,
        "media_type": "application/pdf",
        "pdf_version": info.get(
            "pdf_version",
            match.group(1).decode("ascii"),
        ),
        "pages": pages,
        "encrypted": encrypted,
        "page_size": info.get("page_size", ""),
        "tagged": info.get("tagged", ""),
        "form": info.get("form", ""),
        "javascript": info.get("javascript", ""),
        "text_status": _sample_text_status(resolved, encrypted),
        "indexing_status": "not_started",
        "validation_tool": _tool_version("pdfinfo"),
        "observed_mtime_utc": datetime.fromtimestamp(
            stat.st_mtime,
            tz=UTC,
        ).isoformat(timespec="microseconds").replace("+00:00", "Z"),
        "device": stat.st_dev,
        "inode": stat.st_ino,
    }


def assess_document_stub(path: Path) -> dict[str, Any]:
    """Identify a non-PDF document without claiming archival support."""

    resolved = path.expanduser().resolve(strict=True)
    if not resolved.is_file() or resolved.is_symlink():
        raise DocumentError(f"document is not a regular file: {resolved}")
    suffix = resolved.suffix.casefold()
    if suffix == ".pdf":
        return {
            "result": "supported",
            "format": "pdf",
            "inspection": inspect_pdf(resolved),
        }
    if suffix not in DOCUMENT_STUB_SUFFIXES:
        raise DocumentError(f"unsupported document suffix: {suffix or '<none>'}")
    return {
        "result": "stub",
        "format": suffix.removeprefix("."),
        "path": str(resolved),
        "size_bytes": resolved.stat().st_size,
        "sha256": sha256_path(resolved),
        "accession_status": "accession_not_implemented_for_format",
    }


class _NoCrossHostRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        parsed = urllib.parse.urlparse(newurl)
        if (
            parsed.scheme != "https"
            or parsed.hostname not in PDF_RESOURCE_HOSTS
            or not parsed.path.startswith(PDF_RESOURCE_PREFIX)
        ):
            raise DocumentError(f"unsafe museum-digital PDF redirect: {newurl}")
        return super().redirect_request(
            request,
            fp,
            code,
            msg,
            headers,
            newurl,
        )


def capture_museum_digital_pdf(url: str, destination: Path) -> dict[str, Any]:
    """Capture one bounded museum-digital PDF as replaceable work evidence."""

    parsed = urllib.parse.urlparse(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname not in PDF_RESOURCE_HOSTS
        or not parsed.path.startswith(PDF_RESOURCE_PREFIX)
        or not parsed.path.casefold().endswith(".pdf")
    ):
        raise DocumentError(f"unsafe museum-digital PDF URL: {url}")
    if destination.exists():
        raise DocumentError(f"refusing to replace PDF evidence: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/pdf",
            "User-Agent": "ISSArchivePDFReconciliation/0.1",
        },
    )
    opener = urllib.request.build_opener(_NoCrossHostRedirect())
    total = 0
    try:
        with opener.open(request, timeout=60) as response, temporary.open("wb") as handle:
            while chunk := response.read(1024 * 1024):
                total += len(chunk)
                if total > PDF_MAX_BYTES:
                    raise DocumentError("museum-digital PDF exceeds capture limit")
                handle.write(chunk)
            handle.flush()
            os.fsync(handle.fileno())
        inspection = inspect_pdf(temporary)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    inspection["path"] = str(destination.resolve())
    inspection["source_url"] = url
    return inspection


def atomic_copy_verified(source: Path, destination: Path, digest: str) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise DocumentError(f"refusing to replace preserved document: {destination}")
    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
    try:
        shutil.copy2(source, temporary)
        with temporary.open("rb") as handle:
            os.fsync(handle.fileno())
        if sha256_path(temporary) != digest:
            raise DocumentError(f"PDF copy fixity failed: {destination}")
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def mark_read_only(path: Path) -> str:
    os.chmod(path, 0o444)
    if path.stat().st_mode & 0o222 == 0:
        return "posix-permissions"
    try:
        attributes = int.from_bytes(
            os.getxattr(path, "system.ntfs_attrib"),
            "little",
        )
        os.setxattr(
            path,
            "system.ntfs_attrib",
            (attributes | 0x1).to_bytes(4, "little"),
        )
        verified = int.from_bytes(
            os.getxattr(path, "system.ntfs_attrib"),
            "little",
        )
    except OSError as error:
        raise DocumentError(f"filesystem cannot mark PDF read-only: {path}") from error
    if verified & 0x1 == 0:
        raise DocumentError(f"NTFS read-only marker did not persist: {path}")
    return "ntfs-read-only-attribute-with-fixity-sweep"


def backup_catalog(root: Path, entity_id: str) -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    destination = (
        root / "work" / "backups" / f"{stamp}-PRE-PDF-{entity_id}.sqlite"
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    source = catalog_core.connect_catalog(root / "work" / "catalog.sqlite")
    backup = sqlite3.connect(destination)
    try:
        source.backup(backup)
        backup.commit()
    finally:
        backup.close()
        source.close()
    check = sqlite3.connect(f"file:{destination}?mode=ro&immutable=1", uri=True)
    try:
        if check.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise DocumentError("pre-PDF catalog backup failed integrity")
    finally:
        check.close()
    return destination


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
