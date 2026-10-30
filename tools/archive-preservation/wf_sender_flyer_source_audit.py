#!/usr/bin/env python3
"""Audit the correct local PDF/TIFF sources for all collection-552 flyers.

The primary MIK tree contains regular issues whose numeric keys can collide
with museum-digital ``WFS-FB-*`` flyer inventories.  The separate
``HF-Sender_Sonderausgaben`` tree is the paired PDF/TIFF source for those
flyers.  This unit-specific audit proves the mapping with exact PDF bytes or
rendered-page similarity and records exact TIFF page paths.  It never changes
source files or performs accession.
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
import tempfile
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable


FORMAT = "iss-wf-sender-flyer-source-audit-v1"
SAFE_WORK_ROOT = Path("/mnt/volume/iss-archive-work")
DEFAULT_WORK_ROOT = SAFE_WORK_ROOT / "wf-sender"
DEFAULT_PRIMARY_RUN = DEFAULT_WORK_ROOT / (
    "work/reconciliation/collection-552/runs/"
    "20260728T062423Z-collection-552-primary-match"
)
DEFAULT_SOURCE_ROOT = Path(
    "/media/vladimir/Elements/restore/archive/MIK-Daten/1953/"
    "HF-Sender_Sonderausgaben"
)
DEFAULT_SPLIT_ROOT = Path(
    "/media/vladimir/Elements/restore/archive/WF/WF-Sender Projekt/"
    "Scans_WF-Sender_MIK/Umbenannt_WFS-Sonderausgaben"
)
MIN_SSIM = 0.90
MAX_PDF_BYTES = 256 * 1024 * 1024

AGGREGATE_FOLDER = {
    "WFS-1954_SA-01": "01",
    "WFS-1955_SA-02": "02",
    "WFS-1955_SA-03-1": "03",
    "WFS-1955_SA-03-2": "03",
    "WFS-1954_SA-04": "04",
    "WFS-1954_SA-05": "05",
    "WFS-1954_SA-06": "06",
    "WFS-1954_SA-07": "07",
    "WFS-1954_SA-08": "08",
    "WFS-1954_SA-09": "09",
    "WFS-1951-54_SA-10": "10",
    "WFS-1951-54_SA-11": "11",
}


class FlyerAuditError(RuntimeError):
    """Raised when the bounded flyer-source audit cannot prove its contract."""


@dataclass(frozen=True)
class LocalBinding:
    folder_name: str
    pdf_path: Path
    tiff_paths: tuple[Path, ...]
    mapping_basis: str


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def ensure_read_only_source(source_root: Path) -> str:
    result = subprocess.run(
        [
            "findmnt",
            "-n",
            "-o",
            "SOURCE,TARGET,FSTYPE,OPTIONS",
            "--target",
            str(source_root),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    fields = result.stdout.strip().split(None, 3)
    if len(fields) != 4:
        raise FlyerAuditError(f"cannot parse source mount: {result.stdout.strip()}")
    options = set(fields[3].split(","))
    if "ro" not in options:
        raise FlyerAuditError(f"source mount is not read-only: {result.stdout.strip()}")
    return result.stdout.strip()


def require_safe_output(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    safe = SAFE_WORK_ROOT.resolve()
    if resolved == safe or not resolved.is_relative_to(safe):
        raise FlyerAuditError(f"output escaped the archive work root: {resolved}")
    if resolved.exists():
        raise FlyerAuditError(f"refusing existing output directory: {resolved}")
    return resolved


def atomic_write(path: Path, payload: bytes) -> None:
    if path.exists():
        raise FlyerAuditError(f"refusing to replace artifact: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    with temporary.open("xb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


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


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(8 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def pdf_pages(path: Path) -> int:
    result = subprocess.run(
        ["pdfinfo", str(path)],
        check=True,
        capture_output=True,
        text=True,
        timeout=90,
    )
    match = re.search(r"^Pages:\s+(\d+)\s*$", result.stdout, re.MULTILINE)
    if not match:
        raise FlyerAuditError(f"pdfinfo did not report pages: {path}")
    return int(match.group(1))


def source_files(directory: Path, suffixes: set[str]) -> list[Path]:
    if not directory.is_dir():
        raise FlyerAuditError(f"source directory is missing: {directory}")
    return sorted(
        path
        for path in directory.iterdir()
        if path.is_file() and path.suffix.casefold() in suffixes
    )


def local_binding(
    inventory: str, source_root: Path, split_root: Path
) -> LocalBinding | None:
    flyer = re.fullmatch(r"WFS-FB-(195[34])-(\d{2}(?:_[AB])?)", inventory)
    if flyer:
        folder_name = f"HFS-{flyer.group(1)}-{flyer.group(2)}"
        pdfs = source_files(source_root / "PDF" / folder_name, {".pdf"})
        tiffs = source_files(source_root / "TIF" / folder_name, {".tif", ".tiff"})
        if len(pdfs) != 1 or not tiffs:
            raise FlyerAuditError(
                f"expected one PDF and TIFF pages for {inventory}: {folder_name}"
            )
        return LocalBinding(
            folder_name,
            pdfs[0],
            tuple(tiffs),
            "inventory year/number maps WFS-FB to the paired HFS source folder",
        )
    if inventory == "WFS-FB-1952-01":
        return None
    aggregate = AGGREGATE_FOLDER.get(inventory)
    if aggregate is None:
        raise FlyerAuditError(f"unmapped flyer inventory: {inventory}")
    folder_name = f"HFS-1951-54_Sonderausgaben-{aggregate}"
    tiffs = source_files(source_root / "TIF" / folder_name, {".tif", ".tiff"})
    if inventory.endswith("SA-03-1"):
        pdf = split_root / "WFS-1951-54_SA-03-1.pdf"
        tiffs = tiffs[:2]
        basis = (
            "saved crosswalk split aggregate special-edition folder 03; "
            "first two TIFF pages"
        )
    elif inventory.endswith("SA-03-2"):
        pdf = split_root / "WFS-1951-54_SA-03-2.pdf"
        tiffs = tiffs[2:]
        basis = (
            "saved crosswalk split aggregate special-edition folder 03; "
            "last two TIFF pages"
        )
    else:
        pdfs = source_files(source_root / "PDF" / folder_name, {".pdf"})
        if len(pdfs) != 1:
            raise FlyerAuditError(f"expected one aggregate PDF: {folder_name}")
        pdf = pdfs[0]
        basis = "saved upload-workbook crosswalk to aggregate special-edition folder"
    if not pdf.is_file() or not tiffs:
        raise FlyerAuditError(f"incomplete aggregate source binding: {inventory}")
    return LocalBinding(folder_name, pdf, tuple(tiffs), basis)


def download_pdf(url: str, destination: Path) -> None:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "ISS-Archive-Reconciliation/1.0"},
    )
    temporary = destination.with_name(f".{destination.name}.tmp-{os.getpid()}")
    total = 0
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            if response.status != 200:
                raise FlyerAuditError(f"museum-digital returned HTTP {response.status}")
            content_type = response.headers.get_content_type()
            if content_type not in {"application/pdf", "application/octet-stream"}:
                raise FlyerAuditError(
                    f"unexpected museum-digital content type: {content_type}"
                )
            with temporary.open("xb") as handle:
                while chunk := response.read(1024 * 1024):
                    total += len(chunk)
                    if total > MAX_PDF_BYTES:
                        raise FlyerAuditError(f"museum-digital PDF exceeds size limit: {url}")
                    handle.write(chunk)
                handle.flush()
                os.fsync(handle.fileno())
        if total < 5 or temporary.read_bytes()[:5] != b"%PDF-":
            raise FlyerAuditError(f"download is not a PDF: {url}")
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def render_pdf(path: Path, output_prefix: Path) -> list[Path]:
    subprocess.run(
        ["pdftoppm", "-r", "72", "-png", str(path), str(output_prefix)],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        timeout=180,
    )
    return sorted(output_prefix.parent.glob(f"{output_prefix.name}-*.png"))


def page_ssim(left: Path, right: Path) -> float:
    result = subprocess.run(
        ["compare", "-metric", "SSIM", str(left), str(right), "null:"],
        capture_output=True,
        text=True,
        timeout=90,
    )
    metric = result.stderr.strip().splitlines()[-1] if result.stderr.strip() else ""
    try:
        return float(metric)
    except ValueError as error:
        raise FlyerAuditError(
            f"cannot parse SSIM for {left.name} / {right.name}: {metric}"
        ) from error


def rendered_similarity(md_pdf: Path, local_pdf: Path, temporary_root: Path) -> list[float]:
    md_pages = render_pdf(md_pdf, temporary_root / "md")
    local_pages = render_pdf(local_pdf, temporary_root / "local")
    if len(md_pages) != len(local_pages):
        raise FlyerAuditError(
            f"rendered page-count mismatch: {md_pdf} / {local_pdf}"
        )
    return [page_ssim(md, local) for md, local in zip(md_pages, local_pages)]


def read_flyers(database: Path) -> list[dict[str, str]]:
    uri = f"file:{database.resolve()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    try:
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise FlyerAuditError("primary reconciliation database integrity failed")
        rows = connection.execute(
            """SELECT md.md_object_id, md.inventory_raw, md.title, md.date_text,
                      md.object_url, resource.resource_url
               FROM md_objects AS md
               JOIN md_resources AS resource USING (md_object_id)
               WHERE md.object_type='Flugblatt'
               ORDER BY md.inventory_raw, md.md_object_id"""
        ).fetchall()
    finally:
        connection.close()
    result = [dict(row) for row in rows]
    if len(result) != 112 or len({row["md_object_id"] for row in result}) != 112:
        raise FlyerAuditError(f"expected 112 unique flyer rows, found {len(result)}")
    return result


def identify_tiffs(paths: tuple[Path, ...]) -> None:
    result = subprocess.run(
        ["identify", "-quiet", *map(str, paths)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        timeout=300,
    )
    if result.returncode:
        raise FlyerAuditError(
            f"TIFF decode/read check failed for {paths[0].parent}: "
            f"{result.stderr.decode(errors='replace').strip()}"
        )


def run_audit(args: argparse.Namespace) -> Path:
    output = require_safe_output(args.output)
    database = args.primary_run.resolve(strict=True) / "reconciliation.sqlite"
    if not database.is_file():
        raise FlyerAuditError(f"primary database is missing: {database}")
    source_root = args.source_root.resolve(strict=True)
    split_root = args.split_root.resolve(strict=True)
    mount_state = ensure_read_only_source(source_root)
    flyers = read_flyers(database)
    output.mkdir(parents=True)
    md_root = output / "museum-digital-pdfs"
    md_root.mkdir()
    object_rows: list[dict[str, Any]] = []
    representation_rows: list[dict[str, Any]] = []
    for position, row in enumerate(flyers, start=1):
        object_id = row["md_object_id"]
        inventory = row["inventory_raw"]
        destination = md_root / f"{object_id}.pdf"
        download_pdf(row["resource_url"], destination)
        md_pages = pdf_pages(destination)
        md_sha256 = sha256(destination)
        binding = local_binding(inventory, source_root, split_root)
        if binding is None:
            object_rows.append(
                {
                    **row,
                    "binding_state": "no_hfs_source",
                    "folder_name": "",
                    "mapping_basis": (
                        "no paired HFS source folder found; retain reviewed fallback PDF"
                    ),
                    "md_pdf_pages": md_pages,
                    "local_pdf_path": "",
                    "local_pdf_pages": "",
                    "tiff_pages": 0,
                    "pdf_comparison": "not_applicable",
                    "minimum_ssim": "",
                }
            )
            representation_rows.append(
                {
                    "md_object_id": object_id,
                    "inventory": inventory,
                    "role": "museum_digital_reference_pdf",
                    "sequence": "",
                    "source_path": str(destination),
                    "source_size_bytes": destination.stat().st_size,
                    "source_mtime_ns": destination.stat().st_mtime_ns,
                    "sha256": md_sha256,
                    "binding_state": "no_hfs_source",
                }
            )
            print(f"[{position:03d}/112] {inventory}: no paired HFS source", flush=True)
            continue
        local_pages = pdf_pages(binding.pdf_path)
        if local_pages != len(binding.tiff_paths) or md_pages != local_pages:
            raise FlyerAuditError(
                f"page extent mismatch for {inventory}: "
                f"MD={md_pages}, local PDF={local_pages}, TIFF={len(binding.tiff_paths)}"
            )
        identify_tiffs(binding.tiff_paths)
        local_sha256 = sha256(binding.pdf_path)
        if local_sha256 == md_sha256:
            comparison = "exact_pdf_bytes"
            similarities: list[float] = []
        else:
            with tempfile.TemporaryDirectory(
                prefix=f"{object_id}-", dir=output
            ) as temporary:
                similarities = rendered_similarity(
                    destination, binding.pdf_path, Path(temporary)
                )
            comparison = (
                "rendered_pages_equivalent"
                if similarities and min(similarities) >= MIN_SSIM
                else "needs_visual_review"
            )
        binding_state = (
            "proven_local_pdf_tiff_binding"
            if comparison != "needs_visual_review"
            else "needs_review"
        )
        object_rows.append(
            {
                **row,
                "binding_state": binding_state,
                "folder_name": binding.folder_name,
                "mapping_basis": binding.mapping_basis,
                "md_pdf_pages": md_pages,
                "local_pdf_path": str(binding.pdf_path),
                "local_pdf_pages": local_pages,
                "tiff_pages": len(binding.tiff_paths),
                "pdf_comparison": comparison,
                "minimum_ssim": (
                    f"{min(similarities):.6f}" if similarities else ""
                ),
            }
        )
        representation_rows.append(
            {
                "md_object_id": object_id,
                "inventory": inventory,
                "role": "local_source_pdf",
                "sequence": "",
                "source_path": str(binding.pdf_path),
                "source_size_bytes": binding.pdf_path.stat().st_size,
                "source_mtime_ns": binding.pdf_path.stat().st_mtime_ns,
                "sha256": local_sha256,
                "binding_state": binding_state,
            }
        )
        for sequence, tiff in enumerate(binding.tiff_paths, start=1):
            representation_rows.append(
                {
                    "md_object_id": object_id,
                    "inventory": inventory,
                    "role": "source_tiff_page",
                    "sequence": sequence,
                    "source_path": str(tiff),
                    "source_size_bytes": tiff.stat().st_size,
                    "source_mtime_ns": tiff.stat().st_mtime_ns,
                    "sha256": sha256(tiff),
                    "binding_state": binding_state,
                }
            )
        print(
            f"[{position:03d}/112] {inventory}: {comparison}, "
            f"{len(binding.tiff_paths)} TIFF pages",
            flush=True,
        )
    object_fields = [
        "md_object_id",
        "inventory_raw",
        "title",
        "date_text",
        "object_url",
        "resource_url",
        "binding_state",
        "folder_name",
        "mapping_basis",
        "md_pdf_pages",
        "local_pdf_path",
        "local_pdf_pages",
        "tiff_pages",
        "pdf_comparison",
        "minimum_ssim",
    ]
    representation_fields = [
        "md_object_id",
        "inventory",
        "role",
        "sequence",
        "source_path",
        "source_size_bytes",
        "source_mtime_ns",
        "sha256",
        "binding_state",
    ]
    atomic_write(
        output / "flyer-object-bindings.csv",
        csv_bytes(object_rows, object_fields),
    )
    atomic_write(
        output / "flyer-source-representations.csv",
        csv_bytes(representation_rows, representation_fields),
    )
    state_counts: dict[str, int] = {}
    comparison_counts: dict[str, int] = {}
    for row in object_rows:
        state_counts[row["binding_state"]] = state_counts.get(row["binding_state"], 0) + 1
        comparison_counts[row["pdf_comparison"]] = (
            comparison_counts.get(row["pdf_comparison"], 0) + 1
        )
    summary = {
        "format": FORMAT,
        "created_utc": utc_now(),
        "source_mount": mount_state,
        "source_policy": "read-only; no source mutation; no accession",
        "primary_run": str(args.primary_run.resolve()),
        "source_root": str(source_root),
        "split_root": str(split_root),
        "objects": len(object_rows),
        "binding_states": state_counts,
        "pdf_comparisons": comparison_counts,
        "tiff_representations": sum(
            row["role"] == "source_tiff_page" for row in representation_rows
        ),
        "errors": 0,
    }
    atomic_write(
        output / "summary.json",
        (json.dumps(summary, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
    )
    findings = f"""# WF-Sender flyer PDF/TIFF source audit

Created: {summary['created_utc']}

This audit corrects the unsafe `WFS-FB-*` normalization collision with regular
`WFS-*` issue folders. The paired `HF-Sender_Sonderausgaben` PDF/TIFF tree is
the bounded local source for collection-552 flyers.

- Museum-digital flyer objects: **{len(object_rows)}**
- Proven local PDF/TIFF bindings: **{state_counts.get('proven_local_pdf_tiff_binding', 0)}**
- Flyer objects without paired HFS source: **{state_counts.get('no_hfs_source', 0)}**
- TIFF pages bound and read successfully: **{summary['tiff_representations']}**
- Exact local/MD PDF byte matches: **{comparison_counts.get('exact_pdf_bytes', 0)}**
- Render-equivalent local/MD PDFs: **{comparison_counts.get('rendered_pages_equivalent', 0)}**
- Rows still needing visual review: **{state_counts.get('needs_review', 0)}**

`WFS-FB-1952-01` has no paired HFS TIFF folder and remains a PDF-only reviewed
fallback. Aggregate folder `HFS-1951-54_Sonderausgaben-03` is split by the
documented crosswalk: its first two TIFF pages bind to `WFS-1955_SA-03-1` and
its last two pages to `WFS-1955_SA-03-2`.

The downloaded museum-digital PDFs are comparison evidence under this work
run. No source file was modified and this run does not perform accession.
"""
    atomic_write(output / "FINDINGS.md", findings.encode("utf-8"))
    return output


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--primary-run", type=Path, default=DEFAULT_PRIMARY_RUN)
    result.add_argument("--source-root", type=Path, default=DEFAULT_SOURCE_ROOT)
    result.add_argument("--split-root", type=Path, default=DEFAULT_SPLIT_ROOT)
    result.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_WORK_ROOT
        / "work/reconciliation/collection-552/flyer-source-audit/runs"
        / f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-flyer-source-audit",
    )
    return result


def main() -> int:
    args = parser().parse_args()
    output: Path | None = None
    try:
        output = run_audit(args)
    except (FlyerAuditError, OSError, sqlite3.Error, subprocess.SubprocessError) as error:
        if output and output.exists():
            shutil.rmtree(output)
        raise SystemExit(f"error: {error}") from error
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
