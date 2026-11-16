#!/usr/bin/env python3
"""Build the corrected, no-copy WF-Sender PDF/TIFF accession plan.

This unit-specific planner combines the frozen primary inventory, stable
curator-decision bindings, corrected flyer PDF/TIFF bindings, and primary TIFF
integrity results.  It emits exact final paths and a complete source
disposition projection, but it does not copy, mutate, or accession files.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import sqlite3
import subprocess
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable


FORMAT = "iss-wf-sender-accession-plan-v1"
SAFE_WORK_ROOT = Path("/mnt/volume/iss-archive-work")
DEFAULT_WORK_ROOT = SAFE_WORK_ROOT / "wf-sender"
DEFAULT_PRIMARY_RUN = DEFAULT_WORK_ROOT / (
    "work/reconciliation/collection-552/runs/"
    "20260728T062423Z-collection-552-primary-match"
)
DEFAULT_DECISION_BINDINGS = DEFAULT_WORK_ROOT / (
    "work/reconciliation/collection-552/decision-bindings/runs/"
    "20260728T095500Z-stable-bindings"
)
DEFAULT_FLYER_AUDIT = DEFAULT_WORK_ROOT / (
    "work/reconciliation/collection-552/flyer-source-audit/runs/"
    "20260728T094500Z-flyer-source-audit"
)
DEFAULT_TIFF_AUDIT = DEFAULT_WORK_ROOT / (
    "work/reconciliation/collection-552/tiff-integrity/runs/"
    "20260728T101500Z-tiff-integrity"
)
DEFAULT_CURRENT_FALLBACK_RUN = DEFAULT_WORK_ROOT / (
    "work/reconciliation/collection-552/fallback-search/runs/"
    "20260728T083759Z-collection-552-documented-crosswalk"
)
DEFAULT_INITIAL_FALLBACK_RUN = DEFAULT_WORK_ROOT / (
    "work/reconciliation/collection-552/fallback-search/runs/"
    "20260728T062928Z-collection-552-fallback-search"
)
HFS_SOURCE_ROOT = Path(
    "/media/vladimir/Elements/restore/archive/MIK-Daten/1953/"
    "HF-Sender_Sonderausgaben"
)
MD_ONLY_CACHE = (
    DEFAULT_WORK_ROOT / "work/workbench-cache/wf-sender/museum-digital/55996.pdf"
)
MD_ONLY_OBJECT_ID = "55996"
FB_1952_OBJECT_ID = "87104"
CORRECTED_1989_OBJECT_ID = "56022"
CORRECTED_1989_LOCAL_KEY = "wfs-1989-24"
EXCLUDED_CROSS_UNIT_GLOB = "WFS-1953-*"

OBJECT_FIELDS = [
    "carrier_key",
    "inventory",
    "title",
    "date_text",
    "object_type",
    "md_object_ids",
    "md_object_urls",
    "relationship_state",
    "representation_policy",
]
REPRESENTATION_FIELDS = [
    "carrier_key",
    "inventory",
    "md_object_ids",
    "role",
    "sequence",
    "source_kind",
    "source_path",
    "source_size_bytes",
    "source_mtime_ns",
    "source_sha256",
    "final_relative_path",
    "copy_policy",
    "basis",
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


class AccessionPlanError(RuntimeError):
    """Raised when the corrected accession plan cannot be proven."""


def utc_now() -> str:
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


def atomic_write(path: Path, payload: bytes) -> None:
    if path.exists():
        raise AccessionPlanError(f"refusing to replace artifact: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    with temporary.open("xb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def require_new_output(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    safe = SAFE_WORK_ROOT.resolve()
    if resolved == safe or not resolved.is_relative_to(safe):
        raise AccessionPlanError(f"output escaped archive work root: {resolved}")
    if resolved.exists():
        raise AccessionPlanError(f"refusing existing output: {resolved}")
    return resolved


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
        raise AccessionPlanError(
            f"accession source mount is not read-only: {result.stdout.strip()}"
        )
    return result.stdout.strip()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(8 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def joined(values: Iterable[str]) -> str:
    return ";".join(sorted({value for value in values if value}))


def preferred_md(rows: list[dict[str, Any]], inventory: str) -> dict[str, Any]:
    exact = next(
        (
            row
            for row in rows
            if str(row["inventory_raw"]).casefold() == inventory.casefold()
        ),
        None,
    )
    return exact or sorted(rows, key=lambda row: str(row["md_object_id"]))[0]


def primary_representation_policy(
    inventory: str,
    object_tiffs: list[dict[str, str]],
) -> tuple[str, str]:
    roles = {row["archive_role"] for row in object_tiffs}
    if not object_tiffs:
        return "pdf_preservation_master", "preservation_master_pdf"
    if roles == {"preservation_master_page"}:
        return "tiff_master_pages_with_pdf_access", "access_pdf"
    if roles == {"damaged_source_tiff_fragment"}:
        return (
            "pdf_master_with_damaged_tiff_fragments",
            "preservation_master_pdf",
        )
    raise AccessionPlanError(f"unexpected TIFF role mix for {inventory}: {roles}")


def representation(
    *,
    carrier_key: str,
    inventory: str,
    md_ids: str,
    role: str,
    sequence: int | str,
    source_kind: str,
    source_path: Path,
    final_path: str,
    basis: str,
    source_sha256: str = "",
) -> dict[str, Any]:
    stat = source_path.stat()
    return {
        "carrier_key": carrier_key,
        "inventory": inventory,
        "md_object_ids": md_ids,
        "role": role,
        "sequence": sequence,
        "source_kind": source_kind,
        "source_path": str(source_path),
        "source_size_bytes": stat.st_size,
        "source_mtime_ns": stat.st_mtime_ns,
        "source_sha256": source_sha256,
        "final_relative_path": final_path,
        "copy_policy": "exact_source_bytes",
        "basis": basis,
    }


def build(args: argparse.Namespace) -> Path:
    output = require_new_output(args.output)
    primary_run = args.primary_run.resolve(strict=True)
    decision_run = args.decision_bindings.resolve(strict=True)
    flyer_run = args.flyer_audit.resolve(strict=True)
    tiff_run = args.tiff_audit.resolve(strict=True)
    database = primary_run / "reconciliation.sqlite"
    if not database.is_file():
        raise AccessionPlanError(f"primary database is missing: {database}")
    mount_state = ensure_read_only_source(Path("/media/vladimir/Elements"))
    for summary_path in (
        decision_run / "summary.json",
        flyer_run / "summary.json",
        tiff_run / "summary.json",
    ):
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        if summary.get("errors", summary.get("unexpected_errors", 0)) != 0:
            raise AccessionPlanError(f"input audit is not clean: {summary_path}")
    decision_rows = read_csv(decision_run / "decision-bindings.csv")
    relationship_rows = read_csv(decision_run / "latest-per-relationship.csv")
    flyer_objects = read_csv(flyer_run / "flyer-object-bindings.csv")
    flyer_representations = read_csv(
        flyer_run / "flyer-source-representations.csv"
    )
    tiff_rows = read_csv(tiff_run / "tiff-integrity.csv")
    tiffs_by_object: dict[int, list[dict[str, str]]] = defaultdict(list)
    for row in tiff_rows:
        tiffs_by_object[int(row["local_object_id"])].append(row)

    uri = f"file:{database.resolve()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    try:
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise AccessionPlanError("primary database integrity failed")
        md = {
            row["md_object_id"]: dict(row)
            for row in connection.execute(
                """SELECT md.*, resource.resource_url
                   FROM md_objects AS md
                   JOIN md_resources AS resource USING (md_object_id)"""
            )
        }
        news_mapping: dict[str, int] = {}
        for row in connection.execute(
            """SELECT md.md_object_id, groups.local_object_id
               FROM md_objects AS md
               JOIN candidate_matches AS matches USING (md_object_id)
               JOIN local_groups AS groups USING (local_group_id)
               WHERE md.object_type='Zeitung'
               GROUP BY md.md_object_id, groups.local_object_id"""
        ):
            object_id = str(row["md_object_id"])
            if object_id in news_mapping:
                raise AccessionPlanError(
                    f"newspaper MD row has several local objects: {object_id}"
                )
            news_mapping[object_id] = int(row["local_object_id"])
        for row in relationship_rows:
            if (
                row["subject_type"] == "md_object"
                and row["decision"] == "confirm_local_candidate"
                and row["local_object_id"]
            ):
                news_mapping[row["subject_id"]] = int(row["local_object_id"])
        corrected_1989 = connection.execute(
            """SELECT local_object_id FROM local_objects
               WHERE local_object_key=?""",
            (CORRECTED_1989_LOCAL_KEY,),
        ).fetchone()
        if corrected_1989 is None:
            raise AccessionPlanError("corrected WFS-1989-24 local object is missing")
        news_mapping[CORRECTED_1989_OBJECT_ID] = int(
            corrected_1989["local_object_id"]
        )
        news_ids = {
            object_id
            for object_id, row in md.items()
            if row["object_type"] == "Zeitung"
        }
        if set(news_mapping) != news_ids - {MD_ONLY_OBJECT_ID}:
            missing = sorted(news_ids - {MD_ONLY_OBJECT_ID} - set(news_mapping))
            extra = sorted(set(news_mapping) - news_ids)
            raise AccessionPlanError(
                f"newspaper mapping incomplete: missing={missing}, extra={extra}"
            )
        md_by_local: dict[int, list[dict[str, Any]]] = defaultdict(list)
        for md_id, local_object_id in news_mapping.items():
            md_by_local[local_object_id].append(md[md_id])
        local_only_ids = {
            int(row["subject_id"])
            for row in decision_rows
            if row["subject_type"] == "local_object"
            and row["decision"] == "retain_as_archive_candidate"
        }
        if len(local_only_ids) != 2:
            raise AccessionPlanError(
                f"expected two accepted local-only objects, found {local_only_ids}"
            )
        kept_local = connection.execute(
            """SELECT * FROM local_objects
               WHERE representative_inventory NOT GLOB ?
               ORDER BY representative_inventory, local_object_id""",
            (EXCLUDED_CROSS_UNIT_GLOB,),
        ).fetchall()
        if len(kept_local) != 1750:
            raise AccessionPlanError(
                f"corrected primary carrier count drifted: {len(kept_local)}"
            )

        object_rows: list[dict[str, Any]] = []
        representation_rows: list[dict[str, Any]] = []
        for local in kept_local:
            local_id = int(local["local_object_id"])
            inventory = str(local["representative_inventory"])
            md_rows = md_by_local.get(local_id, [])
            if not md_rows and local_id not in local_only_ids:
                raise AccessionPlanError(
                    f"kept primary object has no accepted relationship: {inventory}"
                )
            md_ids = joined(str(row["md_object_id"]) for row in md_rows)
            selected_md = preferred_md(md_rows, inventory) if md_rows else None
            pdf = connection.execute(
                """SELECT files.source_path
                   FROM local_files AS files
                   JOIN local_groups AS groups USING (local_group_id)
                   WHERE groups.local_object_id=?
                     AND groups.source_family='PDF'
                     AND lower(files.extension)='.pdf'""",
                (local_id,),
            ).fetchone()
            if pdf is None:
                raise AccessionPlanError(f"primary PDF is missing: {inventory}")
            pdf_path = Path(str(pdf["source_path"])).resolve(strict=True)
            object_tiffs = tiffs_by_object.get(local_id, [])
            policy, pdf_role = primary_representation_policy(
                inventory,
                object_tiffs,
            )
            pdf_final = (
                f"export/access/{inventory}.pdf"
                if pdf_role == "access_pdf"
                else f"masters/{inventory}/{pdf_path.name}"
            )
            object_rows.append(
                {
                    "carrier_key": inventory,
                    "inventory": inventory,
                    "title": selected_md["title"] if selected_md else "",
                    "date_text": selected_md["date_text"] if selected_md else "",
                    "object_type": (
                        selected_md["object_type"] if selected_md else "local_only"
                    ),
                    "md_object_ids": md_ids,
                    "md_object_urls": joined(
                        str(row["object_url"]) for row in md_rows
                    ),
                    "relationship_state": (
                        "matched" if md_rows else "accepted_local_only"
                    ),
                    "representation_policy": policy,
                }
            )
            representation_rows.append(
                representation(
                    carrier_key=inventory,
                    inventory=inventory,
                    md_ids=md_ids,
                    role=pdf_role,
                    sequence="",
                    source_kind="primary_mik_pdf",
                    source_path=pdf_path,
                    final_path=pdf_final,
                    basis=policy,
                )
            )
            for tiff in object_tiffs:
                source_path = Path(tiff["source_path"]).resolve(strict=True)
                role = tiff["archive_role"]
                final_path = (
                    f"masters/{inventory}/pages/{source_path.name}"
                    if role == "preservation_master_page"
                    else f"masters/{inventory}/source-fragments/{source_path.name}"
                )
                representation_rows.append(
                    representation(
                        carrier_key=inventory,
                        inventory=inventory,
                        md_ids=md_ids,
                        role=role,
                        sequence=tiff["sequence"],
                        source_kind="primary_mik_tiff",
                        source_path=source_path,
                        final_path=final_path,
                        basis=(
                            "corrected primary TIFF integrity audit"
                            if role == "preservation_master_page"
                            else "known incomplete/corrupt TIFF group retained as evidence"
                        ),
                    )
                )
    finally:
        connection.close()

    flyer_rep_by_md: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in flyer_representations:
        flyer_rep_by_md[row["md_object_id"]].append(row)
    fallback_1952 = next(
        (
            row
            for row in decision_rows
            if row["subject_id"] == FB_1952_OBJECT_ID
            and row["decision"] == "confirm_local_candidate"
        ),
        None,
    )
    if fallback_1952 is None:
        raise AccessionPlanError("reviewed WFS-FB-1952-01 fallback is missing")
    for flyer in flyer_objects:
        md_id = flyer["md_object_id"]
        inventory = flyer["inventory_raw"]
        md_ids = md_id
        if flyer["binding_state"] == "proven_local_pdf_tiff_binding":
            sources = flyer_rep_by_md[md_id]
            pdf_source = next(row for row in sources if row["role"] == "local_source_pdf")
            tiff_sources = [
                row for row in sources if row["role"] == "source_tiff_page"
            ]
            policy = "tiff_master_pages_with_pdf_access"
            representation_rows.append(
                representation(
                    carrier_key=inventory,
                    inventory=inventory,
                    md_ids=md_ids,
                    role="access_pdf",
                    sequence="",
                    source_kind="hfs_paired_pdf",
                    source_path=Path(pdf_source["source_path"]).resolve(strict=True),
                    final_path=f"export/access/{inventory}.pdf",
                    basis=flyer["pdf_comparison"],
                    source_sha256=pdf_source["sha256"],
                )
            )
            for source in tiff_sources:
                path = Path(source["source_path"]).resolve(strict=True)
                representation_rows.append(
                    representation(
                        carrier_key=inventory,
                        inventory=inventory,
                        md_ids=md_ids,
                        role="preservation_master_page",
                        sequence=source["sequence"],
                        source_kind="hfs_paired_tiff",
                        source_path=path,
                        final_path=f"masters/{inventory}/pages/{path.name}",
                        basis="paired HFS PDF/TIFF source and proven flyer binding",
                        source_sha256=source["sha256"],
                    )
                )
        elif md_id == FB_1952_OBJECT_ID:
            source = Path(fallback_1952["source_path"]).resolve(strict=True)
            policy = "pdf_preservation_master"
            representation_rows.append(
                representation(
                    carrier_key=inventory,
                    inventory=inventory,
                    md_ids=md_ids,
                    role="preservation_master_pdf",
                    sequence="",
                    source_kind="reviewed_fallback_pdf",
                    source_path=source,
                    final_path=f"masters/{inventory}/{inventory}.pdf",
                    basis="stable curator-decision binding; no paired HFS TIFF source",
                    source_sha256=fallback_1952["source_sha256"],
                )
            )
        else:
            raise AccessionPlanError(f"unresolved flyer binding: {inventory}")
        object_rows.append(
            {
                "carrier_key": inventory,
                "inventory": inventory,
                "title": flyer["title"],
                "date_text": flyer["date_text"],
                "object_type": "Flugblatt",
                "md_object_ids": md_ids,
                "md_object_urls": flyer["object_url"],
                "relationship_state": (
                    "matched"
                    if flyer["binding_state"] == "proven_local_pdf_tiff_binding"
                    else "matched_pdf_only_fallback"
                ),
                "representation_policy": policy,
            }
        )

    md_only = MD_ONLY_CACHE.resolve(strict=True)
    md_only_row = next(
        (
            row
            for row in decision_rows
            if row["subject_id"] == MD_ONLY_OBJECT_ID
            and row["decision"] == "accept_md_only_fallback"
        ),
        None,
    )
    if md_only_row is None:
        raise AccessionPlanError("MD-only fallback decision is missing")
    md_only_metadata = md[MD_ONLY_OBJECT_ID]
    md_only_inventory = str(md_only_metadata["inventory_raw"])
    object_rows.append(
        {
            "carrier_key": md_only_inventory,
            "inventory": md_only_inventory,
            "title": md_only_metadata["title"],
            "date_text": md_only_metadata["date_text"],
            "object_type": md_only_metadata["object_type"],
            "md_object_ids": MD_ONLY_OBJECT_ID,
            "md_object_urls": md_only_metadata["object_url"],
            "relationship_state": "museum_digital_pdf_fallback",
            "representation_policy": "pdf_preservation_master",
        }
    )
    representation_rows.append(
        representation(
            carrier_key=md_only_inventory,
            inventory=md_only_inventory,
            md_ids=MD_ONLY_OBJECT_ID,
            role="preservation_master_pdf",
            sequence="",
            source_kind="museum_digital_download_cache",
            source_path=md_only,
            final_path=f"masters/{md_only_inventory}/{md_only_inventory}.pdf",
            basis="curator accepted current public museum-digital PDF fallback",
            source_sha256=sha256(md_only),
        )
    )

    combined_folder = HFS_SOURCE_ROOT / "PDF/HFS-1951-54_Sonderausgaben-03"
    combined_pdf = next(combined_folder.glob("*.pdf"))
    representation_rows.append(
        representation(
            carrier_key="WFS-1955_SA-03-combined-source",
            inventory="WFS-1955_SA-03-1;WFS-1955_SA-03-2",
            md_ids="87094;87095",
            role="combined_source_carrier_pdf",
            sequence="",
            source_kind="hfs_combined_source_pdf",
            source_path=combined_pdf,
            final_path=(
                "masters/_source-carriers/"
                "HFS-1951-54_Sonderausgaben-03.pdf"
            ),
            basis="original four-page source carrier retained alongside documented split",
            source_sha256=sha256(combined_pdf),
        )
    )

    object_rows.sort(key=lambda row: (row["inventory"], row["carrier_key"]))
    representation_rows.sort(
        key=lambda row: (
            row["final_relative_path"],
            row["source_path"],
        )
    )
    if len(object_rows) != 1863:
        raise AccessionPlanError(f"expected 1,863 carriers, found {len(object_rows)}")
    role_counts: dict[str, int] = {}
    for row in representation_rows:
        role_counts[row["role"]] = role_counts.get(row["role"], 0) + 1
    expected_roles = {
        "preservation_master_page": 5335,
        "damaged_source_tiff_fragment": 10,
        "access_pdf": 999,
        "preservation_master_pdf": 864,
        "combined_source_carrier_pdf": 1,
    }
    if role_counts != expected_roles:
        raise AccessionPlanError(
            f"representation role counts drifted: {role_counts}"
        )

    selected_by_source: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in representation_rows:
        selected_by_source[row["source_path"]].append(row)
    observed: dict[str, dict[str, Any]] = {}
    uri = f"file:{database.resolve()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    try:
        for row in connection.execute(
            """SELECT files.source_path, files.size_bytes, files.mtime_ns,
                      files.file_role, files.extension,
                      objects.representative_inventory AS inventory
               FROM local_files AS files
               JOIN local_groups AS groups USING (local_group_id)
               JOIN local_objects AS objects USING (local_object_id)"""
        ):
            observed[row["source_path"]] = dict(row)
    finally:
        connection.close()
    for path in HFS_SOURCE_ROOT.rglob("*"):
        if path.is_file():
            stat = path.stat()
            observed.setdefault(
                str(path.resolve()),
                {
                    "source_path": str(path.resolve()),
                    "size_bytes": stat.st_size,
                    "mtime_ns": stat.st_mtime_ns,
                    "file_role": "hfs_source_file",
                    "extension": path.suffix.casefold(),
                    "inventory": path.parent.name,
                },
            )
    for fallback_run in (
        args.initial_fallback_run.resolve(strict=True),
        args.current_fallback_run.resolve(strict=True),
    ):
        for row in read_csv(fallback_run / "filesystem-candidates.csv"):
            path = Path(row["source_path"])
            if not path.is_file():
                continue
            observed.setdefault(
                str(path.resolve()),
                {
                    "source_path": str(path.resolve()),
                    "size_bytes": int(row["size_bytes"]),
                    "mtime_ns": int(row["mtime_ns"]),
                    "file_role": "fallback_candidate",
                    "extension": row["extension"].casefold(),
                    "inventory": row["inventory_raw"],
                },
            )
    known_hashes = {
        row["source_path"]: row["source_sha256"]
        for row in representation_rows
        if row["source_sha256"]
    }
    known_hashes.update(
        {
            row["source_path"]: row["source_sha256"]
            for row in decision_rows
            if row["source_path"] and row["source_sha256"]
        }
    )
    disposition_rows: list[dict[str, Any]] = []
    for source_path in sorted(observed):
        source = observed[source_path]
        selected = selected_by_source.get(source_path, [])
        extension = str(source.get("extension") or "").casefold()
        if selected:
            final_paths = joined(row["final_relative_path"] for row in selected)
            inventories = joined(row["inventory"] for row in selected)
            roles = joined(row["role"] for row in selected)
            status = "planned_for_exact_copy"
            basis = joined(row["basis"] for row in selected)
            notes = "Accession plan only; exact-copy proof is pending."
        elif "/MIK-Daten/" in source_path and "/1953/WFS-1953-" in source_path:
            final_paths = ""
            inventories = str(source.get("inventory") or "")
            roles = str(source.get("file_role") or "")
            status = "excluded_cross_unit_trafo"
            basis = "masthead and visual evidence identify Der Transformator"
            notes = "Retain under Trafo provenance; not a WF-Sender drain candidate."
        elif extension in {".htm", ".html", ".css"}:
            final_paths = ""
            inventories = str(source.get("inventory") or "")
            roles = str(source.get("file_role") or "")
            status = "not_selected_access_generation_context"
            basis = "generated HTML/CSS context is not a preservation representation"
            notes = "Retain source; not preserved and not drain-eligible."
        else:
            final_paths = ""
            inventories = str(source.get("inventory") or "")
            roles = str(source.get("file_role") or "")
            status = "retained_unselected_candidate_or_derivative"
            basis = "review evidence or duplicate/derivative path not selected"
            notes = "Retain source; no deletion is authorized."
        disposition_rows.append(
            {
                "source_path": source_path,
                "source_kind": "file",
                "unit": "wf-sender",
                "inventory": inventories,
                "source_role": roles,
                "final_path": final_paths,
                "preservation_status": status,
                "preservation_basis": basis,
                "source_sha256": known_hashes.get(source_path, ""),
                "final_sha256": "",
                "source_size_bytes": source["size_bytes"],
                "exact_copy_proven": "false",
                "drain_state": "not_yet_eligible",
                "deletion_authorized": "false",
                "notes": notes,
            }
        )

    output.mkdir(parents=True)
    atomic_write(output / "objects.csv", csv_bytes(object_rows, OBJECT_FIELDS))
    atomic_write(
        output / "representations.csv",
        csv_bytes(representation_rows, REPRESENTATION_FIELDS),
    )
    atomic_write(
        output / "source-disposition.csv",
        csv_bytes(disposition_rows, DISPOSITION_FIELDS),
    )
    summary = {
        "format": FORMAT,
        "created_utc": utc_now(),
        "source_mount": mount_state,
        "source_policy": "read-only; plan only; no copy; no source mutation",
        "primary_run": str(primary_run),
        "decision_bindings": str(decision_run),
        "flyer_audit": str(flyer_run),
        "tiff_audit": str(tiff_run),
        "carriers": len(object_rows),
        "museum_digital_rows": len(md),
        "accepted_local_only_carriers": 2,
        "representations": len(representation_rows),
        "representation_roles": role_counts,
        "source_disposition_rows": len(disposition_rows),
        "planned_payload_bytes": sum(
            int(row["source_size_bytes"]) for row in representation_rows
        ),
        "errors": 0,
    }
    atomic_write(
        output / "summary.json",
        (json.dumps(summary, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
    )
    findings = f"""# WF-Sender corrected PDF/TIFF accession plan

Created: {summary['created_utc']}

- Catalog carriers: **{summary['carriers']}**
- Museum-digital rows represented: **{summary['museum_digital_rows']}**
- Accepted local-only carriers: **2**
- TIFF preservation-master pages: **{role_counts['preservation_master_page']}**
- Damaged TIFF source fragments retained: **{role_counts['damaged_source_tiff_fragment']}**
- Access PDFs paired with TIFF masters: **{role_counts['access_pdf']}**
- PDF preservation masters where complete TIFF is absent: **{role_counts['preservation_master_pdf']}**
- Combined original special-edition source carrier PDFs: **1**
- Planned payload files: **{len(representation_rows)}**
- Planned payload bytes: **{summary['planned_payload_bytes']:,}**

The 47 mislabeled primary `WFS-1953-*` Trafo carriers are excluded. Correct
HFS flyer PDF/TIFF sources replace their unsafe flyer aliases. `WFS-1966-42`
and `WFS-1971-19` use their complete PDFs as masters and retain their TIFFs
only as damaged source fragments. `WFS-FB-1952-01` and MD 55996 are PDF-only.

This is a no-copy plan. Final accession still requires a guarded exact-byte
copy, one final SHA-256 inventory, catalog/restore generation, and validation.
Every source-disposition row remains `deletion_authorized=false`.
"""
    atomic_write(output / "FINDINGS.md", findings.encode("utf-8"))
    return output


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--primary-run", type=Path, default=DEFAULT_PRIMARY_RUN)
    result.add_argument(
        "--decision-bindings", type=Path, default=DEFAULT_DECISION_BINDINGS
    )
    result.add_argument("--flyer-audit", type=Path, default=DEFAULT_FLYER_AUDIT)
    result.add_argument("--tiff-audit", type=Path, default=DEFAULT_TIFF_AUDIT)
    result.add_argument(
        "--initial-fallback-run",
        type=Path,
        default=DEFAULT_INITIAL_FALLBACK_RUN,
    )
    result.add_argument(
        "--current-fallback-run",
        type=Path,
        default=DEFAULT_CURRENT_FALLBACK_RUN,
    )
    result.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_WORK_ROOT
        / "work/accession-plan/runs"
        / f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-pdf-tiff-plan",
    )
    return result


def main() -> int:
    args = parser().parse_args()
    try:
        output = build(args)
    except (
        AccessionPlanError,
        OSError,
        sqlite3.Error,
        subprocess.SubprocessError,
    ) as error:
        raise SystemExit(f"error: {error}") from error
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
