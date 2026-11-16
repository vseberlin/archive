#!/usr/bin/env python3
"""Continue the Trafo staging batch, logging file errors without aborting."""

from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import os
import shutil
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


TOOL = Path(
    "/mnt/volume/iss-archive-work/trafo/work/tools/trafo_accession_full.py"
)
spec = importlib.util.spec_from_file_location("trafo_accession_full", TOOL)
if spec is None or spec.loader is None:
    raise RuntimeError(f"cannot load Trafo accession module: {TOOL}")
full = importlib.util.module_from_spec(spec)
spec.loader.exec_module(full)

PASS_ID = "exception-pass-001"
STATE = full.RUN_ROOT / PASS_ID
STATUS_JSONL = STATE / "row-status.jsonl"
ERRORS_JSONL = STATE / "errors.jsonl"
COMPLETE = STATE / "batch-complete.json"
FINAL_UNIT = full.FINAL_UNIT
FINAL_STAGE = full.FINAL_STAGE


class ContinueError(RuntimeError):
    pass


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def row_key(row: dict[str, str]) -> str:
    return "|".join(
        (
            row["inventory"],
            row["role"],
            row["sequence"],
            row["destination_path"],
        )
    )


def append_json(handle: Any, value: dict[str, Any]) -> None:
    handle.write(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n")
    handle.flush()
    os.fsync(handle.fileno())


def load_statuses() -> dict[str, dict[str, Any]]:
    if not STATUS_JSONL.exists():
        return {}
    result: dict[str, dict[str, Any]] = {}
    with STATUS_JSONL.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            value = json.loads(line)
            key = str(value.get("row_key") or "")
            if not key or key in result:
                raise ContinueError(
                    f"invalid or duplicate row status at line {line_number}"
                )
            result[key] = value
    return result


def progress(
    statuses: dict[str, dict[str, Any]],
    total: int,
    inventory: str,
) -> None:
    errors = sum(row["status"] == "error" for row in statuses.values())
    full.replace_json(
        STATE / "progress.json",
        {
            "pass_id": PASS_ID,
            "updated_at": now(),
            "phase": "exception-tolerant-staging",
            "completed_rows": len(statuses),
            "total_rows": total,
            "exceptions": errors,
            "current_inventory": inventory,
        },
    )


def target_for_page(inventory: str, row: dict[str, str]) -> Path:
    return (
        full.PACKAGE
        / "masters"
        / inventory
        / "pages"
        / Path(row["destination_path"]).name
    )


def report_for_page(inventory: str, page_number: int) -> Path:
    return (
        full.PACKAGE
        / "db-restore"
        / "manifests"
        / "pages"
        / inventory
        / f"page-{page_number:03d}.json"
    )


def quarantine(path: Path, inventory: str) -> None:
    if not path.exists():
        return
    destination = STATE / "quarantine" / inventory / path.name
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        destination = destination.with_name(
            f"{destination.name}.{uuid.uuid4().hex}"
        )
    os.rename(path, destination)


def existing_page(
    target: Path,
    report_path: Path,
    row: dict[str, str],
) -> dict[str, Any] | None:
    if not target.exists() and not report_path.exists():
        return None
    if not target.is_file() or not report_path.is_file():
        return None
    report = json.loads(report_path.read_text(encoding="utf-8"))
    if (
        report.get("target") != target.relative_to(full.PACKAGE).as_posix()
        or report.get("source_sha256") != row["sha256"]
        or int(report.get("size_bytes") or -1) != target.stat().st_size
        or report.get("embedding_profile") != full.PROFILE
    ):
        return None
    return report


def create_page(
    inventory: str,
    page_number: int,
    page_count: int,
    row: dict[str, str],
    metadata: dict[str, str],
    md_object_id: str,
    plan_digest: str,
) -> dict[str, Any]:
    source = full.source_path(row)
    target = target_for_page(inventory, row)
    report_path = report_for_page(inventory, page_number)
    target.parent.mkdir(parents=True, exist_ok=True)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    technical_source = full.tiff_accession._exif_values(
        source, full.tiff_accession.TECHNICAL_TAGS
    )
    bit_depth = full.tiff_accession._scalar_sample_depth(
        technical_source.get("IFD0:BitsPerSample")
    )
    if full.tiff_accession._page_count(source) != 1:
        raise ContinueError(f"TIFF is not one image directory: {source}")
    source_pixel = full.tiff_accession._decoded_pixel_digest(source, bit_depth)
    tags = full.page_tags(
        inventory,
        page_number,
        page_count,
        row,
        metadata,
        md_object_id,
        plan_digest,
    )
    full.tiff_accession._write_candidate(source, target, tags)
    if full.tiff_accession._exif_values(target, tuple(tags)) != tags:
        raise ContinueError(f"embedded metadata readback differs: {target}")
    technical_target = full.tiff_accession._exif_values(
        target, full.tiff_accession.TECHNICAL_TAGS
    )
    if technical_target != technical_source:
        raise ContinueError(f"TIFF technical metadata changed: {target}")
    target_pixel = full.tiff_accession._decoded_pixel_digest(target, bit_depth)
    if target_pixel != source_pixel:
        raise ContinueError(f"decoded TIFF pixels changed: {target}")
    report = {
        "inventory": inventory,
        "page_number": page_number,
        "page_count": page_count,
        "source": str(source),
        "source_sha256": row["sha256"],
        "target": target.relative_to(full.PACKAGE).as_posix(),
        "target_sha256": full.sha256_file(target),
        "size_bytes": target.stat().st_size,
        "decoded_pixel_sha256": target_pixel,
        "embedding_profile": full.PROFILE,
        "embedded_metadata": tags,
        "technical_metadata": technical_target,
    }
    full.write_json(report_path, report)
    return report


def copy_destination_verified(
    source: Path,
    destination: Path,
    expected_digest: str,
) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise ContinueError(f"refusing to replace file: {destination}")
    temporary = destination.with_name(
        f".{destination.name}.{uuid.uuid4().hex}.tmp"
    )
    try:
        shutil.copy2(source, temporary)
        with temporary.open("rb") as handle:
            os.fsync(handle.fileno())
        observed = full.sha256_file(temporary)
        if observed != expected_digest:
            raise ContinueError(
                f"destination digest mismatch: {destination}"
            )
        os.rename(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def pdf_targets(inventory: str, row: dict[str, str]) -> list[Path]:
    if row["role"] == "local_pdf_fallback":
        return [
            full.PACKAGE / "masters" / inventory / f"{inventory}.pdf",
            full.PACKAGE / "export" / "access" / f"{inventory}.pdf",
        ]
    name = (
        "TRO-1953-26-2-md.pdf"
        if inventory == "TRO-1953-26-2"
        else f"{inventory}.pdf"
    )
    return [full.PACKAGE / "export" / "access" / name]


def ensure_pdf(inventory: str, row: dict[str, str]) -> list[Path]:
    source = full.source_path(row)
    targets = pdf_targets(inventory, row)
    first = targets[0]
    if not first.exists():
        copy_destination_verified(source, first, row["sha256"])
    if (
        not first.is_file()
        or first.stat().st_size != int(row["size_bytes"])
        or full.pdf_pages(first) != int(row["pdf_pages"])
    ):
        raise ContinueError(f"invalid staged PDF: {first}")
    for target in targets[1:]:
        if not target.exists():
            copy_destination_verified(first, target, row["sha256"])
        if (
            not target.is_file()
            or target.stat().st_size != int(row["size_bytes"])
            or full.pdf_pages(target) != int(row["pdf_pages"])
        ):
            raise ContinueError(f"invalid staged PDF: {target}")
    return targets


def ensure_metadata(
    inventory: str,
    metadata: dict[str, str],
    raw: dict[str, Any],
    local: dict[str, str] | None,
) -> None:
    snapshot_path = (
        full.PACKAGE / "documents" / "metadata-sources" / f"{inventory}.json"
    )
    md_path = (
        full.PACKAGE / "export" / "md" / "metadata" / f"{inventory}.json"
    )
    if not snapshot_path.exists():
        full.write_json(
            snapshot_path,
            {
                "inventory": inventory,
                "metadata_source": metadata["metadata_source"],
                "accepted_metadata": metadata,
                "museum_digital": raw,
                "local_catalog": local,
            },
        )
    if not md_path.exists():
        full.write_json(md_path, raw)


def status_record(
    row: dict[str, str],
    *,
    status: str,
    target: str,
    detail: str = "",
) -> dict[str, Any]:
    return {
        "row_key": row_key(row),
        "inventory": row["inventory"],
        "role": row["role"],
        "sequence": row["sequence"],
        "source_path": str(full.source_path(row)),
        "source_sha256": row["sha256"],
        "target": target,
        "status": status,
        "detail": detail,
        "recorded_at": now(),
    }


def dry_run() -> dict[str, Any]:
    by_inventory, _, _, _ = full.load_inputs()
    existing = 0
    missing = 0
    inconsistent = 0
    for inventory, rows in by_inventory.items():
        pages = sorted(
            [row for row in rows if row["role"] == "tiff_page"],
            key=lambda row: int(row["sequence"]),
        )
        for page_number, row in enumerate(pages, 1):
            target = target_for_page(inventory, row)
            report = report_for_page(inventory, page_number)
            if existing_page(target, report, row):
                existing += 1
            elif target.exists() or report.exists():
                inconsistent += 1
            else:
                missing += 1
        pdf_row = next(row for row in rows if row["role"] != "tiff_page")
        targets = pdf_targets(inventory, pdf_row)
        if all(target.is_file() for target in targets):
            existing += 1
        elif any(target.exists() for target in targets):
            inconsistent += 1
        else:
            missing += 1
    return {
        "result": "ok",
        "dry_run": True,
        "pass_id": PASS_ID,
        "total_manifest_rows": sum(len(rows) for rows in by_inventory.values()),
        "existing_success_rows": existing,
        "missing_rows_to_process": missing,
        "inconsistent_rows": inconsistent,
        "state": str(STATE),
        "final_unit_absent": not FINAL_UNIT.exists(),
        "final_stage_absent": not FINAL_STAGE.exists(),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.dry_run:
        print(json.dumps(dry_run(), ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    if COMPLETE.exists():
        raise ContinueError(f"continuation already completed: {COMPLETE}")
    if FINAL_UNIT.exists() or FINAL_STAGE.exists():
        raise ContinueError("final unit or final staging unexpectedly exists")
    by_inventory, md, raw, local = full.load_inputs()
    plan = json.loads((full.RUN_ROOT / "plan.json").read_text(encoding="utf-8"))
    plan_digest = str(plan["plan_digest"])
    STATE.mkdir(parents=True, exist_ok=True)
    statuses = load_statuses()
    total = sum(len(rows) for rows in by_inventory.values())
    status_mode = "a" if STATUS_JSONL.exists() else "x"
    error_mode = "a" if ERRORS_JSONL.exists() else "x"
    with (
        STATUS_JSONL.open(status_mode, encoding="utf-8") as status_handle,
        ERRORS_JSONL.open(error_mode, encoding="utf-8") as error_handle,
    ):
        for inventory in sorted(by_inventory):
            rows = by_inventory[inventory]
            metadata = (
                full.metadata_from_local(local[inventory])
                if inventory in local
                else full.metadata_from_raw(raw[inventory])
            )
            pages = sorted(
                [row for row in rows if row["role"] == "tiff_page"],
                key=lambda row: int(row["sequence"]),
            )
            for page_number, row in enumerate(pages, 1):
                key = row_key(row)
                if key in statuses:
                    continue
                target = target_for_page(inventory, row)
                report_path = report_for_page(inventory, page_number)
                try:
                    report = existing_page(target, report_path, row)
                    if report is not None:
                        value = status_record(
                            row,
                            status="existing",
                            target=report["target"],
                        )
                    else:
                        if target.exists() or report_path.exists():
                            quarantine(target, inventory)
                            quarantine(report_path, inventory)
                        report = create_page(
                            inventory,
                            page_number,
                            len(pages),
                            row,
                            metadata,
                            md[inventory]["object_id"],
                            plan_digest,
                        )
                        value = status_record(
                            row,
                            status="created",
                            target=report["target"],
                        )
                except Exception as error:
                    quarantine(target, inventory)
                    quarantine(report_path, inventory)
                    value = status_record(
                        row,
                        status="error",
                        target=target.relative_to(full.PACKAGE).as_posix(),
                        detail=f"{type(error).__name__}: {error}",
                    )
                    append_json(error_handle, value)
                append_json(status_handle, value)
                statuses[key] = value
                progress(statuses, total, inventory)

            pdf_row = next(row for row in rows if row["role"] != "tiff_page")
            key = row_key(pdf_row)
            if key not in statuses:
                targets = pdf_targets(inventory, pdf_row)
                try:
                    already = all(target.is_file() for target in targets)
                    ensure_pdf(inventory, pdf_row)
                    value = status_record(
                        pdf_row,
                        status="existing" if already else "created",
                        target="; ".join(
                            target.relative_to(full.PACKAGE).as_posix()
                            for target in targets
                        ),
                    )
                except Exception as error:
                    value = status_record(
                        pdf_row,
                        status="error",
                        target="; ".join(
                            target.relative_to(full.PACKAGE).as_posix()
                            for target in targets
                        ),
                        detail=f"{type(error).__name__}: {error}",
                    )
                    append_json(error_handle, value)
                append_json(status_handle, value)
                statuses[key] = value
                progress(statuses, total, inventory)
            try:
                ensure_metadata(
                    inventory,
                    metadata,
                    raw[inventory],
                    local.get(inventory),
                )
            except Exception as error:
                metadata_error = {
                    "row_key": f"{inventory}|metadata",
                    "inventory": inventory,
                    "role": "metadata",
                    "sequence": "",
                    "source_path": "",
                    "source_sha256": "",
                    "target": "",
                    "status": "error",
                    "detail": f"{type(error).__name__}: {error}",
                    "recorded_at": now(),
                }
                append_json(error_handle, metadata_error)

    if len(statuses) != total:
        raise ContinueError(f"row coverage differs: {len(statuses)} != {total}")
    full_pdf = full.PACKAGE / "export" / "access" / "TRO-1953-26-full.pdf"
    derivative_error = ""
    try:
        if not full_pdf.exists():
            six = full.PACKAGE / "export" / "access" / "TRO-1953-26.pdf"
            eight = (
                full.PACKAGE
                / "export"
                / "access"
                / "TRO-1953-26-2-md.pdf"
            )
            full.run(["pdfunite", str(six), str(eight), str(full_pdf)])
        if full.pdf_pages(full_pdf) != 14:
            raise ContinueError("full issue PDF is not 14 pages")
    except Exception as error:
        derivative_error = f"{type(error).__name__}: {error}"
        with ERRORS_JSONL.open("a", encoding="utf-8") as error_handle:
            append_json(
                error_handle,
                {
                    "row_key": "TRO-1953-26|derived-full-pdf",
                    "inventory": "TRO-1953-26",
                    "role": "derived_full_pdf",
                    "sequence": "",
                    "source_path": "",
                    "source_sha256": "",
                    "target": full_pdf.relative_to(full.PACKAGE).as_posix(),
                    "status": "error",
                    "detail": derivative_error,
                    "recorded_at": now(),
                },
            )

    ordered = sorted(
        statuses.values(),
        key=lambda row: (
            row["inventory"],
            row["role"],
            str(row["sequence"]),
            row["row_key"],
        ),
    )
    fields = list(ordered[0])
    full.write_csv(STATE / "row-status.csv", ordered, fields)
    exceptions = [row for row in ordered if row["status"] == "error"]
    with ERRORS_JSONL.open(encoding="utf-8") as error_handle:
        exception_events = [
            json.loads(line)
            for line in error_handle
            if line.strip()
        ]
    summary = {
        "format": "iss-trafo-exception-tolerant-batch-v1",
        "pass_id": PASS_ID,
        "completed_at": now(),
        "result": (
            "completed_with_exceptions"
            if exception_events
            else "ok"
        ),
        "batch_traversal_complete": True,
        "manifest_rows": total,
        "successful_rows": total - len(exceptions),
        "exception_rows": len(exceptions),
        "exception_events": len(exception_events),
        "derived_pdf_error": derivative_error,
        "exceptions_jsonl": str(ERRORS_JSONL),
        "row_status_csv": str(STATE / "row-status.csv"),
        "final_unit_created": False,
        "publication_blocked_until_exceptions_resolved": bool(exception_events),
    }
    full.write_json(COMPLETE, summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (
        ContinueError,
        full.AccessionError,
        full.copyhash_runner.CopyHashError,
        full.tiff_accession.TiffAccessionError,
        full.tiff_metadata.TiffMetadataError,
        OSError,
    ) as error:
        raise SystemExit(f"error: {error}") from error
