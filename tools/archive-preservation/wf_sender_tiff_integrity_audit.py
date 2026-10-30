#!/usr/bin/env python3
"""Decode-check or safely reuse the corrected primary WF-Sender TIFF selection.

The frozen primary inventory includes 47 ``WFS-1953-*`` groups whose pages are
actually the cross-unit newspaper *Der Transformator*.  This bounded audit
excludes those groups, decode-checks every remaining TIFF source, and records
known incomplete/corrupt TIFF groups as retained source fragments rather than
preservation-master pages.  A completed audit may be reused: the tool verifies
the frozen scope, rechecks only prior unexpected rows, reapplies the current
documented damaged-object policy, and writes a new derived run.  It never
changes an earlier run, source files, or performs accession.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import csv
import io
import json
import os
import sqlite3
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable


FORMAT = "iss-wf-sender-primary-tiff-integrity-v1"
SAFE_WORK_ROOT = Path("/mnt/volume/iss-archive-work")
DEFAULT_WORK_ROOT = SAFE_WORK_ROOT / "wf-sender"
DEFAULT_PRIMARY_RUN = DEFAULT_WORK_ROOT / (
    "work/reconciliation/collection-552/runs/"
    "20260728T062423Z-collection-552-primary-match"
)
KNOWN_DAMAGED_OBJECTS = {
    "WFS-1966-42": {
        "expected_failed_sequences": {3},
        "expected_diagnostic_tokens": {"Read error on strip", "TIFFFillStrip"},
        "fallback_pdf": Path(
            "/media/vladimir/Elements/restore/archive/MIK-Daten/PDF/1966/"
            "WFS-1966-42/WFS-1966-42.pdf"
        ),
        "fallback_pdf_pages": 7,
        "basis": (
            "page 3 has a repeatable strip-read failure; the seven-page local "
            "PDF decodes completely and is the usable preservation representation"
        ),
    },
    "WFS-1971-19": {
        "expected_failed_sequences": {3},
        "expected_diagnostic_tokens": {"Read error on strip", "TIFFFillStrip"},
        "fallback_pdf": Path(
            "/media/vladimir/Elements/restore/archive/MIK-Daten/PDF/1971/"
            "WFS-1971-19/WFS-1971-19.pdf"
        ),
        "fallback_pdf_pages": 7,
        "basis": (
            "the primary TIFF group is incomplete and page 3 has a repeatable "
            "strip-read failure; the seven-page local PDF is complete"
        ),
    },
}
FIELDS = [
    "local_object_id",
    "inventory",
    "local_group_id",
    "sequence",
    "source_path",
    "source_size_bytes",
    "source_mtime_ns",
    "integrity_state",
    "archive_role",
    "image_format",
    "width",
    "height",
    "bit_depth",
    "compression",
    "diagnostic",
]
DAMAGED_OBJECT_FIELDS = [
    "inventory",
    "expected_failed_sequences",
    "fallback_pdf_path",
    "fallback_pdf_size_bytes",
    "fallback_pdf_mtime_ns",
    "fallback_pdf_pages",
    "basis",
]


class TiffIntegrityError(RuntimeError):
    """Raised when the bounded TIFF integrity audit cannot run safely."""


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def ensure_read_only_source(path: Path) -> str:
    result = subprocess.run(
        [
            "findmnt",
            "-n",
            "-o",
            "SOURCE,TARGET,FSTYPE,OPTIONS",
            "--target",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    fields = result.stdout.strip().split(None, 3)
    if len(fields) != 4 or "ro" not in set(fields[3].split(",")):
        raise TiffIntegrityError(
            f"TIFF source mount is not read-only: {result.stdout.strip()}"
        )
    return result.stdout.strip()


def require_new_output(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    safe = SAFE_WORK_ROOT.resolve()
    if resolved == safe or not resolved.is_relative_to(safe):
        raise TiffIntegrityError(f"output escaped archive work root: {resolved}")
    if resolved.exists():
        raise TiffIntegrityError(f"refusing existing output: {resolved}")
    return resolved


def atomic_write(path: Path, payload: bytes) -> None:
    if path.exists():
        raise TiffIntegrityError(f"refusing to replace artifact: {path}")
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


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def selected_tiffs(database: Path) -> tuple[list[dict[str, Any]], int]:
    uri = f"file:{database.resolve()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    try:
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise TiffIntegrityError("primary reconciliation database failed integrity")
        excluded = connection.execute(
            """SELECT COUNT(*)
               FROM local_files AS files
               JOIN local_groups AS groups USING (local_group_id)
               JOIN local_objects AS objects USING (local_object_id)
               WHERE groups.source_family='TIF'
                 AND objects.representative_inventory GLOB 'WFS-1953-*'
                 AND lower(files.extension) IN ('.tif', '.tiff')"""
        ).fetchone()[0]
        rows = connection.execute(
            """SELECT objects.local_object_id,
                      objects.representative_inventory AS inventory,
                      groups.local_group_id,
                      files.page_sequence AS sequence,
                      files.source_path,
                      files.size_bytes AS source_size_bytes,
                      files.mtime_ns AS source_mtime_ns
               FROM local_files AS files
               JOIN local_groups AS groups USING (local_group_id)
               JOIN local_objects AS objects USING (local_object_id)
               WHERE groups.source_family='TIF'
                 AND objects.representative_inventory NOT GLOB 'WFS-1953-*'
                 AND lower(files.extension) IN ('.tif', '.tiff')
               ORDER BY objects.representative_inventory,
                        groups.local_group_id,
                        COALESCE(files.page_sequence, 0),
                        files.filename"""
        ).fetchall()
    finally:
        connection.close()
    result = [dict(row) for row in rows]
    if len(result) != 5124 or excluded != 214:
        raise TiffIntegrityError(
            f"corrected TIFF scope drifted: selected={len(result)}, excluded={excluded}"
        )
    return result, excluded


def sequence_number(row: dict[str, Any]) -> int:
    try:
        return int(row["sequence"])
    except (KeyError, TypeError, ValueError) as error:
        raise TiffIntegrityError(
            f"invalid TIFF sequence for {row.get('source_path', '')}"
        ) from error


def known_damaged_policy(row: dict[str, Any]) -> dict[str, Any] | None:
    return KNOWN_DAMAGED_OBJECTS.get(str(row["inventory"]))


def is_expected_failure(
    row: dict[str, Any],
    diagnostic: str,
) -> bool:
    policy = known_damaged_policy(row)
    return bool(
        policy
        and sequence_number(row) in policy["expected_failed_sequences"]
        and all(
            token in diagnostic
            for token in policy["expected_diagnostic_tokens"]
        )
    )


def audit_one(row: dict[str, Any]) -> dict[str, Any]:
    path = Path(str(row["source_path"])).resolve(strict=True)
    stat = path.stat()
    if (
        stat.st_size != int(row["source_size_bytes"])
        or stat.st_mtime_ns != int(row["source_mtime_ns"])
    ):
        return {
            **row,
            "integrity_state": "source_observation_changed",
            "archive_role": "blocked",
            "image_format": "",
            "width": "",
            "height": "",
            "bit_depth": "",
            "compression": "",
            "diagnostic": "source size or mtime differs from frozen inventory",
        }
    result = subprocess.run(
        [
            "identify",
            "-quiet",
            "-format",
            "%m|%w|%h|%z|%[compression]",
            str(path),
        ],
        capture_output=True,
        text=True,
        timeout=180,
    )
    fields = result.stdout.strip().split("|", 4)
    readable = result.returncode == 0 and len(fields) == 5
    damaged_policy = known_damaged_policy(row)
    expected_fragment = is_expected_failure(row, result.stderr)
    if readable:
        integrity_state = "readable"
    elif expected_fragment:
        integrity_state = "expected_corrupt_fragment"
    else:
        integrity_state = "unexpected_decode_error"
    archive_role = (
        "damaged_source_tiff_fragment"
        if damaged_policy and (readable or expected_fragment)
        else "preservation_master_page"
        if readable
        else "blocked"
    )
    return {
        **row,
        "integrity_state": integrity_state,
        "archive_role": archive_role,
        "image_format": fields[0] if len(fields) == 5 else "",
        "width": fields[1] if len(fields) == 5 else "",
        "height": fields[2] if len(fields) == 5 else "",
        "bit_depth": fields[3] if len(fields) == 5 else "",
        "compression": fields[4] if len(fields) == 5 else "",
        "diagnostic": result.stderr.strip(),
    }


def validate_reuse_scope(
    base_run: Path,
    source_rows: list[dict[str, Any]],
    excluded: int,
    mount_state: str,
) -> list[dict[str, str]]:
    summary_path = base_run / "summary.json"
    rows_path = base_run / "tiff-integrity.csv"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    if summary.get("format") != FORMAT:
        raise TiffIntegrityError(f"unsupported base audit format: {summary_path}")
    if int(summary.get("selected_tiff_files", -1)) != len(source_rows):
        raise TiffIntegrityError("base audit selected-file count drifted")
    if int(summary.get("excluded_cross_unit_trafo_tiff_files", -1)) != excluded:
        raise TiffIntegrityError("base audit excluded-file count drifted")
    if summary.get("source_mount") != mount_state:
        raise TiffIntegrityError("base audit source mount differs from current mount")
    rows = read_csv(rows_path)
    if len(rows) != len(source_rows):
        raise TiffIntegrityError("base audit row count drifted")
    source_by_path = {str(row["source_path"]): row for row in source_rows}
    if len(source_by_path) != len(source_rows):
        raise TiffIntegrityError("frozen TIFF scope contains duplicate source paths")
    base_by_path = {row["source_path"]: row for row in rows}
    if len(base_by_path) != len(rows) or set(base_by_path) != set(source_by_path):
        raise TiffIntegrityError("base audit paths differ from frozen TIFF scope")
    compared_fields = (
        "local_object_id",
        "inventory",
        "local_group_id",
        "sequence",
        "source_size_bytes",
        "source_mtime_ns",
    )
    for source_path, source in source_by_path.items():
        base = base_by_path[source_path]
        if any(str(base[field]) != str(source[field]) for field in compared_fields):
            raise TiffIntegrityError(
                f"base audit observation differs from frozen scope: {source_path}"
            )
    return rows


def reuse_completed_audit(
    base_run: Path,
    source_rows: list[dict[str, Any]],
    excluded: int,
    mount_state: str,
) -> tuple[list[dict[str, Any]], int]:
    base_rows = validate_reuse_scope(
        base_run,
        source_rows,
        excluded,
        mount_state,
    )
    source_by_path = {str(row["source_path"]): row for row in source_rows}
    completed: list[dict[str, Any]] = []
    rechecked = 0
    allowed_reused_states = {"readable", "expected_corrupt_fragment"}
    for base in base_rows:
        if base["integrity_state"] not in allowed_reused_states:
            completed.append(audit_one(source_by_path[base["source_path"]]))
            rechecked += 1
            continue
        row: dict[str, Any] = dict(base)
        damaged_policy = known_damaged_policy(row)
        if row["integrity_state"] == "readable":
            row["archive_role"] = (
                "damaged_source_tiff_fragment"
                if damaged_policy
                else "preservation_master_page"
            )
        else:
            expected_sequences = (
                damaged_policy["expected_failed_sequences"]
                if damaged_policy
                else set()
            )
            if (
                sequence_number(row) not in expected_sequences
                or not is_expected_failure(row, row["diagnostic"])
            ):
                raise TiffIntegrityError(
                    "base audit contains an undocumented corrupt fragment: "
                    f"{row['source_path']}"
                )
            row["archive_role"] = "damaged_source_tiff_fragment"
        completed.append(row)
    return completed, rechecked


def validate_pdf_fallback(path: Path, expected_pages: int) -> tuple[int, int]:
    resolved = path.resolve(strict=True)
    info = subprocess.run(
        ["pdfinfo", str(resolved)],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    pages = next(
        (
            int(line.split(":", 1)[1].strip())
            for line in info.stdout.splitlines()
            if line.startswith("Pages:")
        ),
        None,
    )
    if pages != expected_pages:
        raise TiffIntegrityError(
            f"fallback PDF page count drifted: {resolved}: {pages}"
        )
    with tempfile.TemporaryDirectory(prefix="wf-sender-pdf-check-") as directory:
        render = subprocess.run(
            [
                "pdftoppm",
                "-f",
                "1",
                "-l",
                str(pages),
                "-scale-to",
                "64",
                "-png",
                str(resolved),
                str(Path(directory) / "page"),
            ],
            capture_output=True,
            text=True,
            timeout=180,
        )
        rendered = list(Path(directory).glob("page-*.png"))
    if render.returncode != 0 or len(rendered) != pages:
        raise TiffIntegrityError(
            f"fallback PDF does not render completely: {resolved}: "
            f"{render.stderr.strip()}"
        )
    stat = resolved.stat()
    return stat.st_size, stat.st_mtime_ns


def validate_damaged_objects(
    completed: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    by_inventory: dict[str, list[dict[str, Any]]] = {}
    for row in completed:
        by_inventory.setdefault(str(row["inventory"]), []).append(row)
    evidence: list[dict[str, Any]] = []
    for inventory, policy in KNOWN_DAMAGED_OBJECTS.items():
        rows = by_inventory.get(inventory, [])
        if not rows:
            raise TiffIntegrityError(
                f"documented damaged object is absent from audit: {inventory}"
            )
        actual_failed = {
            sequence_number(row)
            for row in rows
            if row["integrity_state"] == "expected_corrupt_fragment"
        }
        if actual_failed != policy["expected_failed_sequences"]:
            raise TiffIntegrityError(
                f"documented damaged sequences drifted for {inventory}: "
                f"{sorted(actual_failed)}"
            )
        if any(
            row["archive_role"] != "damaged_source_tiff_fragment"
            for row in rows
        ):
            raise TiffIntegrityError(
                f"documented damaged object has a master-page role: {inventory}"
            )
        size, mtime_ns = validate_pdf_fallback(
            policy["fallback_pdf"],
            policy["fallback_pdf_pages"],
        )
        evidence.append(
            {
                "inventory": inventory,
                "expected_failed_sequences": ";".join(
                    str(value)
                    for value in sorted(policy["expected_failed_sequences"])
                ),
                "fallback_pdf_path": str(policy["fallback_pdf"].resolve()),
                "fallback_pdf_size_bytes": size,
                "fallback_pdf_mtime_ns": mtime_ns,
                "fallback_pdf_pages": policy["fallback_pdf_pages"],
                "basis": policy["basis"],
            }
        )
    return evidence


def write_results(
    *,
    output: Path,
    completed: list[dict[str, Any]],
    excluded: int,
    mount_state: str,
    primary_run: Path,
    reused_from: Path | None,
    rechecked: int,
) -> Path:
    completed.sort(
        key=lambda row: (
            row["inventory"],
            int(row["local_group_id"]),
            int(row["sequence"] or 0),
            row["source_path"],
        )
    )
    state_counts: dict[str, int] = {}
    role_counts: dict[str, int] = {}
    for row in completed:
        state_counts[row["integrity_state"]] = (
            state_counts.get(row["integrity_state"], 0) + 1
        )
        role_counts[row["archive_role"]] = role_counts.get(row["archive_role"], 0) + 1
    unexpected = (
        state_counts.get("unexpected_decode_error", 0)
        + state_counts.get("source_observation_changed", 0)
    )
    damaged_evidence = validate_damaged_objects(completed)
    output.mkdir(parents=True)
    atomic_write(output / "tiff-integrity.csv", csv_bytes(completed, FIELDS))
    atomic_write(
        output / "exceptions.csv",
        csv_bytes(
            [
                row
                for row in completed
                if row["archive_role"] != "preservation_master_page"
            ],
            FIELDS,
        ),
    )
    atomic_write(
        output / "known-damaged-objects.csv",
        csv_bytes(damaged_evidence, DAMAGED_OBJECT_FIELDS),
    )
    summary = {
        "format": FORMAT,
        "created_utc": utc_now(),
        "source_mount": mount_state,
        "source_policy": "read-only; no hash; no copy; no source mutation",
        "primary_run": str(primary_run),
        "audit_mode": "derived_reuse" if reused_from else "full_decode",
        "reused_from": str(reused_from) if reused_from else "",
        "reused_decode_results": len(completed) - rechecked if reused_from else 0,
        "rechecked_decode_results": rechecked,
        "selected_tiff_files": len(completed),
        "excluded_cross_unit_trafo_tiff_files": excluded,
        "integrity_states": state_counts,
        "archive_roles": role_counts,
        "unexpected_errors": unexpected,
    }
    atomic_write(
        output / "summary.json",
        (json.dumps(summary, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
    )
    findings = f"""# Corrected primary WF-Sender TIFF integrity audit

Created: {summary['created_utc']}

- Audit mode: **{summary['audit_mode']}**
- Reused prior decode results: **{summary['reused_decode_results']}**
- TIFF files decode-checked in this run: **{summary['rechecked_decode_results']}**
- Selected TIFF source files: **{len(completed)}**
- Cross-unit 1953 Trafo TIFF files excluded: **{excluded}**
- Readable preservation-master page candidates: **{role_counts.get('preservation_master_page', 0)}**
- Retained damaged TIFF source fragments: **{role_counts.get('damaged_source_tiff_fragment', 0)}**
- Unexpected decode/stat errors: **{unexpected}**

The 47 primary `WFS-1953-*` objects are excluded because visual and masthead
evidence identifies them as *Der Transformator*, not WF-Sender. Their separate
HFS flyer counterparts are handled by the flyer-source audit.

`WFS-1966-42` and `WFS-1971-19` have documented incomplete/corrupt TIFF sets.
Their TIFFs remain source fragments, not preservation-master pages; each
complete seven-page PDF was rendered successfully and is the usable
preservation representation. No source file was changed and no accession was
performed.
"""
    atomic_write(output / "FINDINGS.md", findings.encode("utf-8"))
    if unexpected:
        raise TiffIntegrityError(
            f"TIFF audit found {unexpected} unexpected errors; see {output}"
        )
    return output


def run(args: argparse.Namespace) -> Path:
    output = require_new_output(args.output)
    primary_run = args.primary_run.resolve(strict=True)
    database = primary_run / "reconciliation.sqlite"
    if not database.is_file():
        raise TiffIntegrityError(f"primary database is missing: {database}")
    mount_state = ensure_read_only_source(Path("/media/vladimir/Elements"))
    source_rows, excluded = selected_tiffs(database)
    reused_from: Path | None = None
    if args.reuse_audit:
        reused_from = args.reuse_audit.resolve(strict=True)
        completed, rechecked = reuse_completed_audit(
            reused_from,
            source_rows,
            excluded,
            mount_state,
        )
    else:
        completed = []
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=args.workers
        ) as executor:
            futures = [executor.submit(audit_one, row) for row in source_rows]
            for position, future in enumerate(
                concurrent.futures.as_completed(futures), start=1
            ):
                completed.append(future.result())
                if position % 100 == 0 or position == len(futures):
                    print(
                        f"[{position}/{len(futures)}] TIFF files checked",
                        flush=True,
                    )
        rechecked = len(completed)
    return write_results(
        output=output,
        completed=completed,
        excluded=excluded,
        mount_state=mount_state,
        primary_run=primary_run,
        reused_from=reused_from,
        rechecked=rechecked,
    )


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--primary-run", type=Path, default=DEFAULT_PRIMARY_RUN)
    result.add_argument("--workers", type=int, default=2, choices=range(1, 5))
    result.add_argument(
        "--reuse-audit",
        type=Path,
        help=(
            "reuse a completed immutable audit, rechecking only prior "
            "unexpected rows and writing a new derived run"
        ),
    )
    result.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_WORK_ROOT
        / "work/reconciliation/collection-552/tiff-integrity/runs"
        / f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-tiff-integrity",
    )
    return result


def main() -> int:
    args = parser().parse_args()
    try:
        output = run(args)
    except (
        TiffIntegrityError,
        OSError,
        sqlite3.Error,
        subprocess.SubprocessError,
    ) as error:
        raise SystemExit(f"error: {error}") from error
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
