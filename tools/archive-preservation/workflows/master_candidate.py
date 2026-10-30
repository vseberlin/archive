"""Pilot-owned TIFF candidate staging and guarded master accession."""

from __future__ import annotations

import json
import os
import re
import shutil
import uuid
from contextlib import closing
from pathlib import Path
from typing import Any

import catalog_core
import tiff_accession
import tiff_metadata


RECEIPT_FORMAT = "iss-editor-master-candidate-v1"
APPROVAL_FORMAT = "iss-editor-master-approval-v1"
MAX_UPLOAD_BYTES = 192 * 1024 * 1024
MAX_INDEXED_CANDIDATE_BYTES = 2 * 1024 * 1024 * 1024


def _safe_name(value: str) -> str:
    name = Path(value.replace("\\", "/")).name.strip()
    if (
        not name
        or name in {".", ".."}
        or "\x00" in name
        or not name.casefold().endswith((".tif", ".tiff"))
    ):
        raise catalog_core.CoreCatalogError(
            "master candidate must have a safe .tif or .tiff filename"
        )
    return re.sub(r"[^A-Za-z0-9._-]+", "-", name)


def _exclusive_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError as error:
            raise catalog_core.CoreCatalogError(
                f"refusing to replace master-candidate receipt: {path}"
            ) from error
    finally:
        temporary.unlink(missing_ok=True)


def _candidate_root(root: Path, unit_id: str) -> Path:
    safe_unit = re.sub(r"[^A-Za-z0-9._-]+", "-", unit_id).strip(".-")
    if not safe_unit or safe_unit != unit_id:
        raise catalog_core.CoreCatalogError("invalid pilot unit for master candidate")
    return root / "work" / "editor-master-candidates" / safe_unit


def _technical(path: Path) -> dict[str, Any]:
    values = tiff_accession._exif_values(path, tiff_accession.TECHNICAL_TAGS)
    if values.get("File:FileType") != "TIFF":
        raise catalog_core.CoreCatalogError(
            "uploaded master candidate is not structurally identified as TIFF"
        )
    bit_depth = tiff_accession._scalar_sample_depth(
        values.get("IFD0:BitsPerSample")
    )
    if tiff_accession._page_count(path) != 1:
        raise catalog_core.CoreCatalogError(
            "master candidate must contain exactly one TIFF image directory"
        )
    return {
        "width_px": int(values["IFD0:ImageWidth"]),
        "height_px": int(values["IFD0:ImageHeight"]),
        "bit_depth": bit_depth,
        "compression": str(values.get("IFD0:Compression") or ""),
        "color_space": str(
            values.get("IFD0:PhotometricInterpretation") or ""
        ),
    }


def stage_candidate(
    root: Path,
    *,
    unit_id: str,
    entity_id: str,
    uploaded_path: Path,
    original_filename: str,
    actor: str,
    reason: str = "",
    max_bytes: int = MAX_UPLOAD_BYTES,
    source_evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Retain one validated upload under the pilot without replacing anything."""

    root = catalog_core.assert_safe_work_root(root)
    catalog_core.require_core_mode(root)
    entity_id = catalog_core.normalize_entity_id(entity_id)
    actor = actor.strip()
    reason = reason.strip()
    if not actor:
        raise catalog_core.CoreCatalogError("named candidate submitter is required")
    source = uploaded_path.expanduser().resolve(strict=True)
    try:
        source.relative_to(root / "work")
    except ValueError as error:
        raise catalog_core.CoreCatalogError(
            "uploaded candidate staging path escaped pilot work"
        ) from error
    size = source.stat().st_size
    if max_bytes not in {MAX_UPLOAD_BYTES, MAX_INDEXED_CANDIDATE_BYTES}:
        raise catalog_core.CoreCatalogError(
            "unsupported master-candidate size boundary"
        )
    if size <= 0 or size > max_bytes:
        raise catalog_core.CoreCatalogError(
            f"master candidate size must be between 1 byte and "
            f"{max_bytes // (1024 * 1024)} MB"
        )
    name = _safe_name(original_filename)
    with closing(
        catalog_core.connect_catalog(
            root / "work" / "catalog.sqlite",
            read_only=True,
        )
    ) as connection:
        row = connection.execute(
            """SELECT 1
               FROM entity_unit_memberships m
               JOIN entities e ON e.entity_id = m.entity_id
               JOIN units u ON u.unit_id = m.unit_id
               WHERE m.entity_id = ? AND m.unit_id = ?
                 AND m.role = 'primary'
                 AND e.kind = 'archive_object' AND e.status = 'active'
                 AND u.status = 'open'""",
            (entity_id, unit_id),
        ).fetchone()
        master = connection.execute(
            """SELECT 1 FROM entity_files
               WHERE entity_id = ? AND role = 'preservation_master'
                 AND status = 'active'""",
            (entity_id,),
        ).fetchone()
    if not row:
        raise catalog_core.CoreCatalogError(
            "master candidate requires an active object in an open unit"
        )
    if master:
        raise catalog_core.CoreCatalogError(
            "this archive object already has an active preservation master"
        )
    technical = _technical(source)
    digest = f"sha256:{catalog_core.sha256_file(source)}"
    candidate_id = f"MASTER-CANDIDATE-{uuid.uuid4().hex.upper()}"
    destination_dir = _candidate_root(root, unit_id) / candidate_id
    destination_dir.mkdir(parents=True, exist_ok=False)
    destination = destination_dir / name
    try:
        shutil.copy2(source, destination)
        with destination.open("rb") as handle:
            os.fsync(handle.fileno())
        if f"sha256:{catalog_core.sha256_file(destination)}" != digest:
            raise catalog_core.CoreCatalogError(
                "master candidate copy failed fixity verification"
            )
        os.chmod(destination, 0o444)
        receipt = {
            "format": RECEIPT_FORMAT,
            "candidate_id": candidate_id,
            "unit_id": unit_id,
            "entity_id": entity_id,
            "original_filename": name,
            "candidate_path": str(destination),
            "size_bytes": destination.stat().st_size,
            "sha256": digest,
            "technical": technical,
            "actor": actor,
            "reason": reason,
            "source_evidence": dict(source_evidence or {}),
            "submitted_at": catalog_core.utc_now(),
            "state": "validated_candidate",
        }
        _exclusive_json(destination_dir / "receipt.json", receipt)
    except Exception:
        if destination.exists():
            os.chmod(destination, 0o644)
            destination.unlink(missing_ok=True)
        try:
            destination_dir.rmdir()
        except OSError:
            pass
        raise
    return receipt


def _read_receipt(path: Path) -> dict[str, Any]:
    try:
        receipt = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise catalog_core.CoreCatalogError(
            f"cannot read master-candidate receipt: {path}"
        ) from error
    if not isinstance(receipt, dict) or receipt.get("format") != RECEIPT_FORMAT:
        raise catalog_core.CoreCatalogError("master-candidate receipt is invalid")
    candidate = Path(str(receipt.get("candidate_path") or "")).resolve(strict=True)
    try:
        candidate.relative_to(path.parent)
    except ValueError as error:
        raise catalog_core.CoreCatalogError(
            "master-candidate receipt path escaped its immutable container"
        ) from error
    if (
        candidate.stat().st_size != int(receipt.get("size_bytes") or -1)
        or f"sha256:{catalog_core.sha256_file(candidate)}"
        != receipt.get("sha256")
    ):
        raise catalog_core.CoreCatalogError(
            "master candidate changed after validation"
        )
    return receipt


def latest_candidate(
    root: Path,
    *,
    unit_id: str,
    entity_id: str,
) -> dict[str, Any] | None:
    directory = _candidate_root(
        catalog_core.assert_safe_work_root(root),
        unit_id,
    )
    if not directory.is_dir():
        return None
    matches = []
    for path in directory.glob("MASTER-CANDIDATE-*/receipt.json"):
        receipt = _read_receipt(path)
        if receipt["entity_id"] == entity_id:
            matches.append((str(receipt["submitted_at"]), path, receipt))
    if not matches:
        return None
    _submitted, path, receipt = max(matches, key=lambda item: item[0])
    receipt["approved"] = (path.parent / "approval.json").is_file()
    return receipt


def approve_candidate(
    root: Path,
    *,
    unit_id: str,
    entity_id: str,
    candidate_id: str,
    actor: str,
    manifest_digest: str,
    museum_digital_id: str = "",
    reason: str = "",
) -> dict[str, Any]:
    """Create a verified master from one unchanged, explicitly selected TIFF."""

    root = catalog_core.assert_safe_work_root(root)
    candidate_dir = _candidate_root(root, unit_id) / candidate_id
    receipt_path = candidate_dir / "receipt.json"
    receipt = _read_receipt(receipt_path)
    if (
        receipt["candidate_id"] != candidate_id
        or receipt["unit_id"] != unit_id
        or receipt["entity_id"] != entity_id
    ):
        raise catalog_core.CoreCatalogError(
            "master-candidate approval does not match this archive object"
        )
    approval_path = candidate_dir / "approval.json"
    if approval_path.exists():
        raise catalog_core.CoreCatalogError(
            "this master candidate was already approved"
        )
    with closing(
        catalog_core.connect_catalog(
            root / "work" / "catalog.sqlite",
            read_only=True,
        )
    ) as connection:
        active_surrogates = int(
            connection.execute(
                """SELECT count(*) FROM entity_files
                   WHERE entity_id = ?
                     AND role = 'preservation_surrogate'
                     AND status = 'active'""",
                (entity_id,),
            ).fetchone()[0]
        )
    provenance = {
        "case_id": unit_id,
        "source_file_id": candidate_id,
        "source_digest": receipt["sha256"],
        "reviewed_bundle_digest": manifest_digest,
        "museum_digital_id": museum_digital_id,
        "representation_role": "preservation_master",
        "provenance_gaps": [],
    }
    result = tiff_accession.create_preservation_master(
        root,
        Path(str(receipt["candidate_path"])),
        entity_id,
        actor,
        embedding_profile=tiff_metadata.PROFILE_V2,
        provenance_metadata=provenance,
    )
    approval = {
        "format": APPROVAL_FORMAT,
        "candidate_id": candidate_id,
        "unit_id": unit_id,
        "entity_id": entity_id,
        "actor": actor,
        "reason": reason.strip(),
        "approved_at": catalog_core.utc_now(),
        "accession_report": result["report"],
        "master": result["master"],
        "representation_transition": {
            "assigned_role": "preservation_master",
            "prior_active_surrogates": active_surrogates,
            "surrogates_retained": active_surrogates,
            "preferred_representation_after_approval": (
                "preservation_master"
            ),
        },
        "state": "accessioned_as_preservation_master",
    }
    _exclusive_json(approval_path, approval)
    return {**result, "approval": str(approval_path)}
