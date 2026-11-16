"""One-way local and museum-digital provenance reconciliation workflow."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

import catalog_core

from archive_core import ArchiveError
from archive_core.catalog import (
    connect_db,
    ensure_identity_review_schema,
    ensure_metadata_decision_schema,
    set_context,
)
from archive_core.filesystem import (
    require_under,
    root_paths,
    safe_relative_path,
    sha256_file,
    utc_now,
)
from archive_core.identity import generate_archival_id, require_unit
from archive_core.metadata import normalize_snapshot_digest, supersede_depicted_events
from archive_core.documents import (
    PDF_PLAN_FORMAT,
    canonical_json,
    capture_museum_digital_pdf,
    inspect_pdf,
)


EXTERNAL_SYSTEM_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")

MUSEUM_DIGITAL_EXPORT_URL = "https://berlin.museum-digital.de/export/json/{query}?limit=100"

MUSEUM_DIGITAL_OBJECT_URL = "https://berlin.museum-digital.de/object/{object_id}?navlang=de"

MUSEUM_DIGITAL_ASSET_URL = "https://asset.museum-digital.org/berlin/{folder}/{filename}"

MUSEUM_DIGITAL_USER_AGENT = "ISSArchiveReconciliation/0.2 (+local read-only reconciliation)"

VISUAL_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp"}

MAX_EXTERNAL_IMAGE_BYTES = 64 * 1024 * 1024


def _museum_digital_pdf_resources(payload: dict[str, Any]) -> list[dict[str, str]]:
    resources: list[dict[str, str]] = []
    images = payload.get("object_images")
    if not isinstance(images, list):
        return resources
    for item in images:
        if not isinstance(item, dict):
            continue
        locator = str(item.get("filename_loc") or item.get("name") or "")
        if (
            str(item.get("type") or "") != "text"
            or not locator.casefold().endswith(".pdf")
        ):
            continue
        resources.append(
            {
                "resource_id": str(item.get("quell_id") or ""),
                "url": locator,
                "label": str(item.get("name") or locator),
                "rights": str(item.get("rights") or ""),
                "owner": str(item.get("owner") or ""),
                "order": str(item.get("order") or ""),
            }
        )
    return resources


def command_core_pdf_reconcile(args: argparse.Namespace) -> dict[str, Any]:
    """Capture one explicit local/MD PDF comparison and selected candidate."""

    root = catalog_core.assert_safe_work_root(Path(args.root))
    catalog_core.require_core_mode(root)
    catalog = catalog_core.connect_catalog(
        root / "work" / "catalog.sqlite",
        read_only=True,
    )
    try:
        catalog_core.assert_catalog_integrity(catalog)
        catalog_core.require_filesystem_current(catalog)
        state = catalog_core.catalog_state(catalog)
        entity = catalog.execute(
            """SELECT e.entity_id, e.status, a.archival_label, u.unit_id,
                      u.status AS unit_status
               FROM entities e
               JOIN archive_objects a ON a.entity_id = e.entity_id
               JOIN entity_unit_memberships m ON m.entity_id = e.entity_id
               JOIN units u ON u.unit_id = m.unit_id
               WHERE e.entity_id = ? AND u.unit_id = ?""",
            (args.entity, args.unit),
        ).fetchone()
        if (
            not entity
            or entity["status"] != "active"
            or entity["unit_status"] != "open"
        ):
            raise catalog_core.CoreCatalogError(
                "PDF reconciliation requires an active object in an open unit"
            )
        revision = int(state["catalog_revision"])
    finally:
        catalog.close()

    plan_dir = root / "work" / "reconciliation" / "pdf"
    plan_path = plan_dir / f"{args.unit}-{args.entity}.plan.json"
    evidence_dir = plan_dir / f"{args.unit}-{args.entity}.evidence"
    if plan_path.exists() or evidence_dir.exists():
        raise ArchiveError(
            f"refusing to replace PDF reconciliation evidence: {plan_path}"
        )
    local_paths = [Path(value).expanduser().resolve(strict=True) for value in args.local_pdf]
    if not local_paths:
        raise ArchiveError("at least one local PDF candidate is required")
    candidates: list[dict[str, Any]] = []
    for index, path in enumerate(local_paths, 1):
        inspection = inspect_pdf(path)
        candidates.append(
            {
                "candidate_id": f"local:{index}",
                "source_kind": "local",
                "source_locator": str(path),
                "inspection": inspection,
            }
        )

    external, error = fetch_museum_digital_query(f"ids:{args.md_id}", args.timeout)
    if error:
        raise ArchiveError(f"museum-digital object capture failed: {error}")
    exact = [
        item
        for item in external
        if str(item.get("object_id") or "") == str(args.md_id)
    ]
    if len(exact) != 1:
        raise ArchiveError(
            f"museum-digital object lookup is not exact: {args.md_id}"
        )
    payload = exact[0]
    resources = _museum_digital_pdf_resources(payload)
    if not resources:
        raise ArchiveError(
            f"museum-digital object has no PDF resources: {args.md_id}"
        )
    evidence_dir.mkdir(parents=True)
    snapshot_path = evidence_dir / f"museum-digital-{args.md_id}.json"
    snapshot_bytes = (canonical_json(payload) + "\n").encode("utf-8")
    snapshot_path.write_bytes(snapshot_bytes)
    for index, resource in enumerate(resources, 1):
        destination = evidence_dir / (
            f"museum-digital-{args.md_id}-{resource['resource_id'] or index}.pdf"
        )
        inspection = capture_museum_digital_pdf(resource["url"], destination)
        candidates.append(
            {
                "candidate_id": f"md:{resource['resource_id'] or index}",
                "source_kind": "museum_digital",
                "source_locator": resource["url"],
                "captured_path": str(destination.resolve()),
                "resource": resource,
                "inspection": inspection,
            }
        )
    selected = next(
        (
            candidate
            for candidate in candidates
            if candidate["candidate_id"] == args.select
        ),
        None,
    )
    if not selected:
        raise ArchiveError(
            f"selected PDF candidate is unavailable: {args.select}"
        )
    plan = {
        "format": PDF_PLAN_FORMAT,
        "unit_id": args.unit,
        "entity_id": args.entity,
        "archival_label": str(entity["archival_label"]),
        "catalog_revision": revision,
        "actor": args.actor,
        "selection_note": args.note,
        "selected_candidate": args.select,
        "museum_digital": {
            "object_id": str(payload.get("object_id") or ""),
            "inventory_number": str(
                payload.get("object_inventory_number") or ""
            ),
            "title": str(payload.get("object_name") or ""),
            "description": str(payload.get("object_description") or ""),
            "snapshot_path": str(snapshot_path.resolve()),
            "snapshot_sha256": hashlib.sha256(snapshot_bytes).hexdigest(),
        },
        "candidates": candidates,
        "indexing_branch": {
            "authority": "derived_rebuildable",
            "status": str(
                selected["inspection"]["text_status"]
            ),
            "ocr_execution": "not_implemented",
        },
        "other_document_formats": {
            "status": "accession_not_implemented_for_format"
        },
    }
    plan_bytes = (canonical_json(plan) + "\n").encode("utf-8")
    plan_path.write_bytes(plan_bytes)
    return {
        "result": "ok",
        "plan": str(plan_path),
        "plan_sha256": hashlib.sha256(plan_bytes).hexdigest(),
        "candidates": len(candidates),
        "selected_candidate": args.select,
        "selected_sha256": selected["inspection"]["sha256"],
        "selected_pages": selected["inspection"]["pages"],
        "selected_text_status": selected["inspection"]["text_status"],
        "state": "ready_for_pdf_accession",
    }

def command_review_external_identity(args: argparse.Namespace) -> dict[str, Any]:
    paths = root_paths(Path(args.root))
    outcome = args.outcome.strip()
    external_system = args.external_system.strip().casefold()
    external_id = args.external_id.strip()
    reason = args.reason.strip()
    related_object_id = getattr(args, "related_object_id", "").strip()
    target_relationship_id = getattr(args, "target_relationship_id", "").strip()
    snapshot_digest = normalize_snapshot_digest(getattr(args, "snapshot_digest", ""))
    confidence = getattr(args, "confidence", "certain")
    if outcome not in {"same_object", "component_of_carrier", "wrong_master", "not_same", "defer"}:
        raise ArchiveError(f"unsupported identity outcome: {outcome}")
    if not EXTERNAL_SYSTEM_RE.fullmatch(external_system):
        raise ArchiveError("external system must use lowercase letters, numbers, dots, dashes, or underscores")
    if not external_id:
        raise ArchiveError("external identifier is required")
    if not reason:
        raise ArchiveError("identity decision reason is required")
    if confidence not in {"certain", "probable", "possible", "unknown"}:
        raise ArchiveError(f"unsupported identity confidence: {confidence}")
    if outcome == "component_of_carrier":
        if not related_object_id or target_relationship_id:
            raise ArchiveError("component-of-carrier requires one carrier object")
    elif outcome == "wrong_master":
        if not target_relationship_id or related_object_id:
            raise ArchiveError("wrong-master requires one master relationship")
    elif related_object_id or target_relationship_id:
        raise ArchiveError(f"{outcome} does not accept a related object or master relationship")

    with connect_db(paths["db"]) as connection:
        require_unit(connection, args.unit, open_only=True)
        ensure_identity_review_schema(connection)
        local = connection.execute(
            "SELECT * FROM objects WHERE object_id = ? AND unit_id = ?",
            (args.local_object_id, args.unit),
        ).fetchone()
        if not local:
            raise ArchiveError(f"unknown local object: {args.local_object_id}")
        if related_object_id:
            related = connection.execute(
                "SELECT * FROM objects WHERE object_id = ? AND unit_id = ?",
                (related_object_id, args.unit),
            ).fetchone()
            if not related:
                raise ArchiveError(f"unknown carrier object: {related_object_id}")
            if related_object_id == args.local_object_id:
                raise ArchiveError("component and carrier must be different objects")

        target_relationship = None
        if target_relationship_id:
            target_relationship = connection.execute(
                """
                SELECT * FROM relationships
                WHERE relationship_id = ? AND unit_id = ?
                  AND subject_type = 'object' AND subject_id = ?
                  AND object_type = 'source_file'
                  AND predicate IN ('primary_master', 'additional_master')
                  AND status != 'rejected'
                """,
                (target_relationship_id, args.unit, args.local_object_id),
            ).fetchone()
            if not target_relationship:
                raise ArchiveError("master relationship is unavailable or does not belong to the local object")

        evidence = getattr(args, "evidence", "").strip()
        set_context(connection, args.actor, evidence=evidence, note=reason)
        identifier_id = ""
        relationship_id = ""
        if outcome in {"same_object", "component_of_carrier"}:
            identifier = connection.execute(
                "SELECT * FROM identifiers WHERE scheme = ? AND value = ?",
                (external_system, external_id),
            ).fetchone()
            if identifier and (
                identifier["entity_type"] != "object"
                or identifier["entity_id"] != args.local_object_id
                or identifier["unit_id"] != args.unit
            ):
                raise ArchiveError(
                    f"{external_system} identifier {external_id} already belongs to another archive record"
                )
            if identifier:
                identifier_id = identifier["identifier_id"]
                connection.execute(
                    "UPDATE identifiers SET status = 'accepted', note = ? WHERE identifier_id = ?",
                    (f"Accepted by external identity review: {reason}", identifier_id),
                )
            else:
                identifier_id = "ID-" + uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO identifiers VALUES (?, ?, 'object', ?, ?, ?, 'accepted', ?)",
                    (
                        identifier_id,
                        args.unit,
                        args.local_object_id,
                        external_system,
                        external_id,
                        f"Accepted by external identity review: {reason}",
                    ),
                )
        elif outcome == "not_same":
            identifier = connection.execute(
                """
                SELECT * FROM identifiers
                WHERE unit_id = ? AND entity_type = 'object' AND entity_id = ?
                  AND scheme = ? AND value = ?
                """,
                (args.unit, args.local_object_id, external_system, external_id),
            ).fetchone()
            if identifier:
                identifier_id = identifier["identifier_id"]
                connection.execute(
                    "UPDATE identifiers SET status = 'rejected', note = ? WHERE identifier_id = ?",
                    (f"Rejected by external identity review: {reason}", identifier_id),
                )

        if outcome == "component_of_carrier":
            relationship = connection.execute(
                """
                SELECT * FROM relationships
                WHERE unit_id = ? AND subject_type = 'object' AND subject_id = ?
                  AND predicate = 'component_of'
                  AND object_type = 'object' AND object_id = ?
                """,
                (args.unit, args.local_object_id, related_object_id),
            ).fetchone()
            if relationship:
                relationship_id = relationship["relationship_id"]
                connection.execute(
                    """
                    UPDATE relationships
                    SET status = 'accepted', confidence = ?, note = ?
                    WHERE relationship_id = ?
                    """,
                    (confidence, f"Accepted by external identity review: {reason}", relationship_id),
                )
            else:
                relationship_id = "REL-" + uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO relationships VALUES (?, ?, 'object', ?, 'component_of', 'object', ?, 'accepted', ?, ?)",
                    (
                        relationship_id,
                        args.unit,
                        args.local_object_id,
                        related_object_id,
                        confidence,
                        f"Accepted by external identity review: {reason}",
                    ),
                )
        elif outcome == "wrong_master":
            relationship_id = target_relationship["relationship_id"]
            connection.execute(
                """
                UPDATE relationships
                SET status = 'rejected', confidence = 'certain', note = ?
                WHERE relationship_id = ?
                """,
                (f"Rejected by external identity review: {reason}", relationship_id),
            )

        review_id = "EIR-" + uuid.uuid4().hex
        status = "deferred" if outcome == "defer" else "resolved"
        connection.execute(
            """
            INSERT INTO external_identity_reviews (
                review_id, unit_id, local_object_id, external_system, external_id,
                snapshot_digest, outcome, related_object_id, target_relationship_id,
                reason, evidence, actor, reviewed_at_utc, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                review_id,
                args.unit,
                args.local_object_id,
                external_system,
                external_id,
                snapshot_digest,
                outcome,
                related_object_id or None,
                target_relationship_id or None,
                reason,
                evidence,
                args.actor,
                utc_now(),
                status,
            ),
        )
        connection.commit()
    return {
        "result": "pass",
        "review_id": review_id,
        "outcome": outcome,
        "status": status,
        "identifier_id": identifier_id,
        "relationship_id": relationship_id,
    }

def command_decide_metadata(args: argparse.Namespace) -> dict[str, Any]:
    paths = root_paths(Path(args.root))
    field_name = args.field_name.strip()
    action = args.action.strip()
    accepted_value = getattr(args, "accepted_value", "").strip()
    external_system = getattr(args, "external_system", "").strip().casefold()
    external_id = getattr(args, "external_id", "").strip()
    snapshot_digest = normalize_snapshot_digest(getattr(args, "snapshot_digest", ""))
    reason = args.reason.strip()
    evidence = getattr(args, "evidence", "").strip()
    allowed_fields = {
        "descriptive_title",
        "description",
        "material_technique",
        "dimensions",
        "depicted_event",
    }
    allowed_actions = {
        "keep_local",
        "accept_external",
        "corrected",
        "split_event",
        "component_events",
        "defer",
    }
    if field_name not in allowed_fields:
        raise ArchiveError(f"unsupported metadata field: {field_name}")
    if action not in allowed_actions:
        raise ArchiveError(f"unsupported metadata action: {action}")
    if not reason:
        raise ArchiveError("metadata decision reason is required")
    if not EXTERNAL_SYSTEM_RE.fullmatch(external_system):
        raise ArchiveError("external system must use lowercase letters, numbers, dots, dashes, or underscores")
    if not external_id:
        raise ArchiveError("external identifier is required")
    if field_name == "depicted_event" and action not in {
        "split_event",
        "component_events",
        "defer",
    }:
        raise ArchiveError(
            "depicted-event decisions must split into an event, use component events, or defer"
        )
    if field_name != "depicted_event" and action in {
        "split_event",
        "component_events",
    }:
        raise ArchiveError(
            "event-scope actions are only valid for depicted-event decisions"
        )
    if action in {"keep_local", "accept_external", "corrected"} and not accepted_value:
        raise ArchiveError("accepted metadata value is required")

    with connect_db(paths["db"]) as connection:
        require_unit(connection, args.unit, open_only=True)
        ensure_identity_review_schema(connection)
        ensure_metadata_decision_schema(connection)
        local = connection.execute(
            "SELECT * FROM objects WHERE object_id = ? AND unit_id = ?",
            (args.object_id, args.unit),
        ).fetchone()
        if not local:
            raise ArchiveError(f"unknown local object: {args.object_id}")
        if action == "component_events":
            compound_file_roles = {
                row["predicate"]
                for row in connection.execute(
                    """
                    SELECT predicate FROM relationships
                    WHERE unit_id = ? AND subject_type = 'object' AND subject_id = ?
                      AND predicate IN ('complete_reference', 'high_resolution_part')
                      AND object_type = 'source_file' AND status = 'accepted'
                    """,
                    (args.unit, args.object_id),
                )
            }
            if (
                "carrier" not in str(local["object_type"]).casefold()
                and not {
                    "complete_reference",
                    "high_resolution_part",
                }.issubset(compound_file_roles)
            ):
                raise ArchiveError(
                    "component-events requires an explicitly represented compound carrier"
                )
        identity_review = connection.execute(
            """
            SELECT * FROM external_identity_reviews
            WHERE unit_id = ? AND local_object_id = ?
              AND external_system = ? AND external_id = ?
            ORDER BY reviewed_at_utc DESC, rowid DESC
            LIMIT 1
            """,
            (args.unit, args.object_id, external_system, external_id),
        ).fetchone()
        if not identity_review or identity_review["status"] != "resolved":
            raise ArchiveError("metadata decision requires a resolved external identity review")
        if identity_review["outcome"] not in {"same_object", "component_of_carrier"}:
            raise ArchiveError("metadata decision requires same-object or component-of-carrier identity")
        if (
            snapshot_digest
            and identity_review["snapshot_digest"]
            and snapshot_digest != identity_review["snapshot_digest"]
        ):
            raise ArchiveError("metadata snapshot differs from the reviewed identity snapshot")

        set_context(connection, args.actor, evidence=evidence, note=reason)
        event_id = ""
        relationship_id = ""
        status = "deferred" if action == "defer" else "accepted"
        if action in {"keep_local", "accept_external", "corrected"}:
            connection.execute(
                f'UPDATE objects SET "{field_name}" = ? WHERE object_id = ?',
                (accepted_value, args.object_id),
            )
        elif action == "component_events":
            supersede_depicted_events(
                connection,
                args.unit,
                args.object_id,
                reason,
            )
            accepted_value = (
                accepted_value
                or "No single carrier-level depicted date; dates belong to linked components."
            )
        elif action == "split_event":
            event_title = getattr(args, "event_title", "").strip()
            starts_on = getattr(args, "starts_on", "").strip()
            ends_on = getattr(args, "ends_on", "").strip()
            if not event_title:
                raise ArchiveError("split-event requires an event title")
            if not starts_on and not ends_on:
                raise ArchiveError("split-event requires an event date")
            assignment_year = int(getattr(args, "assignment_year"))
            supersede_depicted_events(
                connection,
                args.unit,
                args.object_id,
                reason,
            )
            event_id = generate_archival_id(connection, "event", assignment_year)
            event_description = getattr(args, "event_description", "").strip()
            connection.execute(
                """
                INSERT INTO events (
                    event_id, unit_id, title, starts_on, ends_on, circa, location,
                    description, status, note, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)
                """,
                (
                    event_id,
                    args.unit,
                    event_title,
                    starts_on,
                    ends_on,
                    int(bool(getattr(args, "circa", False))),
                    getattr(args, "location", "").strip(),
                    event_description,
                    reason,
                    utc_now(),
                ),
            )
            relationship_id = "REL-" + uuid.uuid4().hex
            confidence = getattr(args, "confidence", "certain")
            if confidence not in {"certain", "probable", "possible", "unknown"}:
                raise ArchiveError(f"unsupported metadata confidence: {confidence}")
            connection.execute(
                """
                INSERT INTO relationships (
                    relationship_id, unit_id, subject_type, subject_id, predicate,
                    object_type, object_id, status, confidence, note
                ) VALUES (?, ?, 'object', ?, 'depicts_event', 'event', ?, 'accepted', ?, ?)
                """,
                (
                    relationship_id,
                    args.unit,
                    args.object_id,
                    event_id,
                    confidence,
                    reason,
                ),
            )
            accepted_value = " – ".join(value for value in (starts_on, ends_on) if value)

        decision_id = "MDR-" + uuid.uuid4().hex
        connection.execute(
            """
            INSERT INTO metadata_decisions (
                decision_id, unit_id, object_id, field_name, action, accepted_value,
                external_system, external_id, snapshot_digest, event_id, reason,
                evidence, actor, decided_at_utc, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                decision_id,
                args.unit,
                args.object_id,
                field_name,
                action,
                accepted_value,
                external_system,
                external_id,
                snapshot_digest,
                event_id or None,
                reason,
                evidence,
                args.actor,
                utc_now(),
                status,
            ),
        )
        connection.commit()
    return {
        "result": "pass",
        "decision_id": decision_id,
        "field_name": field_name,
        "action": action,
        "status": status,
        "accepted_value": accepted_value,
        "event_id": event_id,
        "relationship_id": relationship_id,
    }

def normalize_museum_digital_inventory(value: str) -> str:
    normalized = re.sub(r"\s+", " ", str(value or "")).strip()
    normalized = re.sub(r"\.(?:tif|tiff|jpg|jpeg|png|webp)$", "", normalized, flags=re.IGNORECASE)
    match = re.fullmatch(r"TFA-(?:FOT|NEG)-(.+)", normalized, flags=re.IGNORECASE)
    if match:
        normalized = "TFA-" + match.group(1)
    return normalized.casefold()

def museum_digital_result_objects(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    results = payload.get("results")
    if not isinstance(results, dict):
        return []
    objects = results.get("objects")
    if isinstance(objects, dict):
        return [value for value in objects.values() if isinstance(value, dict)]
    if isinstance(objects, list):
        return [value for value in objects if isinstance(value, dict)]
    return []

def fetch_museum_digital_query(query: str, timeout: float) -> tuple[list[dict[str, Any]], str]:
    encoded = urllib.parse.quote(query, safe=":,")
    url = MUSEUM_DIGITAL_EXPORT_URL.format(query=encoded)
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": MUSEUM_DIGITAL_USER_AGENT},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as error:
        return [], f"{type(error).__name__}: {error}"
    return museum_digital_result_objects(payload), ""

def museum_digital_named_values(items: Any, key: str) -> str:
    if not isinstance(items, list):
        return ""
    values = []
    for item in items:
        if not isinstance(item, dict):
            continue
        value = re.sub(r"\s+", " ", str(item.get(key) or "")).strip()
        if value and value not in values:
            values.append(value)
    return "; ".join(values)

def museum_digital_event_summary(items: Any) -> str:
    if not isinstance(items, list):
        return ""
    values = []
    for item in items:
        if not isinstance(item, dict):
            continue
        event_type = re.sub(r"\s+", " ", str(item.get("event_type_name") or "")).strip()
        person = item.get("people") if isinstance(item.get("people"), dict) else {}
        place = item.get("place") if isinstance(item.get("place"), dict) else {}
        period = item.get("time") if isinstance(item.get("time"), dict) else {}
        parts = [
            event_type,
            re.sub(r"\s+", " ", str(person.get("displayname") or "")).strip(),
            re.sub(r"\s+", " ", str(place.get("place_name") or "")).strip(),
            re.sub(r"\s+", " ", str(period.get("time_name") or "")).strip(),
        ]
        summary = " | ".join(value for value in parts if value)
        if summary:
            values.append(summary)
    return "; ".join(values)

def museum_digital_main_image(payload: dict[str, Any]) -> dict[str, Any]:
    images = payload.get("object_images")
    if not isinstance(images, list):
        return {}
    for image in images:
        if isinstance(image, dict) and str(image.get("is_main") or "") == "j":
            return image
    return next((image for image in images if isinstance(image, dict)), {})

def visual_image_id(*values: str) -> str:
    digest = hashlib.sha256("\0".join(values).encode("utf-8")).hexdigest()
    return "VIS-" + digest[:20].upper()

def image_dimensions(path: Path) -> tuple[str, str]:
    identify = shutil.which("identify")
    if not identify:
        return "", ""
    try:
        result = subprocess.run(
            [identify, "-format", "%w %h", f"{path}[0]"],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return "", ""
    if result.returncode != 0:
        return "", ""
    values = result.stdout.strip().split()
    return (values[0], values[1]) if len(values) == 2 else ("", "")

def create_visual_preview(source: Path, destination: Path) -> tuple[bool, str]:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".partial")
    temporary.unlink(missing_ok=True)
    magick = shutil.which("magick") or shutil.which("convert")
    if magick:
        try:
            result = subprocess.run(
                [
                    magick,
                    f"{source}[0]",
                    "-auto-orient",
                    "-thumbnail",
                    "1600x1600>",
                    "-strip",
                    "-quality",
                    "86",
                    f"jpg:{temporary}",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=120,
            )
        except (OSError, subprocess.SubprocessError) as error:
            result = None
            detail = f"preview command failed: {error}"
        else:
            detail = ""
        if result and result.returncode == 0 and temporary.is_file():
            temporary.replace(destination)
            return True, ""
    elif source.suffix.casefold() in {".jpg", ".jpeg"}:
        shutil.copyfile(source, temporary)
        temporary.replace(destination)
        return True, ""
    else:
        detail = "ImageMagick is required to preview this format"
    temporary.unlink(missing_ok=True)
    return False, detail or "preview generation failed"

def comparison_image_index(roots: list[Path]) -> dict[str, list[Path]]:
    index: dict[str, list[Path]] = {}
    for root in roots:
        if not root.is_dir():
            raise ArchiveError(f"comparison image root is not a directory: {root}")
        for directory, names, filenames in os.walk(root, followlinks=False):
            names[:] = sorted(name for name in names if not Path(directory, name).is_symlink())
            for filename in sorted(filenames):
                path = Path(directory, filename)
                if path.is_symlink() or path.suffix.casefold() not in VISUAL_IMAGE_SUFFIXES:
                    continue
                index.setdefault(filename.casefold(), []).append(path.resolve())
    return index

def fetch_external_image(image: dict[str, Any], destination: Path, timeout: float) -> tuple[bool, str]:
    folder_text = str(image.get("folder") or "").strip("/")
    filename_text = str(image.get("filename_loc") or "").strip()
    if not folder_text or not filename_text or Path(filename_text).name != filename_text:
        return False, "museum-digital image location is unavailable"
    try:
        folder = safe_relative_path(folder_text)
    except ArchiveError as error:
        return False, str(error)
    quoted_folder = "/".join(urllib.parse.quote(part, safe="") for part in folder.parts)
    url = MUSEUM_DIGITAL_ASSET_URL.format(
        folder=quoted_folder,
        filename=urllib.parse.quote(filename_text, safe=""),
    )
    request = urllib.request.Request(url, headers={"User-Agent": MUSEUM_DIGITAL_USER_AGENT})
    temporary = destination.with_name(destination.name + ".partial")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary.unlink(missing_ok=True)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response, temporary.open("xb") as handle:
            content_length = int(response.headers.get("Content-Length", "0") or 0)
            if content_length > MAX_EXTERNAL_IMAGE_BYTES:
                raise ArchiveError("museum-digital image exceeds the comparison snapshot limit")
            total = 0
            while chunk := response.read(1024 * 1024):
                total += len(chunk)
                if total > MAX_EXTERNAL_IMAGE_BYTES:
                    raise ArchiveError("museum-digital image exceeds the comparison snapshot limit")
                handle.write(chunk)
            handle.flush()
            os.fsync(handle.fileno())
        if not temporary.is_file() or temporary.stat().st_size == 0:
            raise ArchiveError("museum-digital image snapshot is empty")
        temporary.replace(destination)
    except (urllib.error.URLError, TimeoutError, OSError, ValueError, ArchiveError) as error:
        temporary.unlink(missing_ok=True)
        return False, f"{type(error).__name__}: {error}"
    return True, url

def normalized_source_header(value: str) -> str:
    return re.sub(r"_+", "_", re.sub(r"[^a-z0-9]+", "_", value.strip().casefold())).strip("_")

def xlsx_column_index(reference: str) -> int:
    letters = re.match(r"[A-Z]+", reference)
    if not letters:
        raise ArchiveError(f"invalid XLSX cell reference: {reference}")
    index = 0
    for letter in letters.group():
        index = index * 26 + ord(letter) - 64
    return index - 1

def read_xlsx_source_rows(path: Path) -> list[dict[str, str]]:
    spreadsheet_ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    document_rel_ns = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    package_rel_ns = "http://schemas.openxmlformats.org/package/2006/relationships"
    namespace = {"m": spreadsheet_ns}
    rows: list[dict[str, str]] = []
    try:
        with zipfile.ZipFile(path) as archive:
            shared_strings: list[str] = []
            if "xl/sharedStrings.xml" in archive.namelist():
                shared_root = ElementTree.fromstring(archive.read("xl/sharedStrings.xml"))
                for item in shared_root.findall(f"{{{spreadsheet_ns}}}si"):
                    shared_strings.append(
                        "".join(node.text or "" for node in item.iter(f"{{{spreadsheet_ns}}}t"))
                    )
            workbook = ElementTree.fromstring(archive.read("xl/workbook.xml"))
            relationships = ElementTree.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
            targets = {
                relation.attrib["Id"]: relation.attrib["Target"]
                for relation in relationships.findall(f"{{{package_rel_ns}}}Relationship")
            }
            for sheet in workbook.findall(f".//{{{spreadsheet_ns}}}sheet"):
                target = targets[sheet.attrib[f"{{{document_rel_ns}}}id"]].lstrip("/")
                worksheet_path = target if target.startswith("xl/") else f"xl/{target}"
                worksheet = ElementTree.fromstring(archive.read(worksheet_path))
                sheet_rows: list[list[str]] = []
                for xml_row in worksheet.findall(".//m:sheetData/m:row", namespace):
                    values: list[str] = []
                    for cell in xml_row.findall("m:c", namespace):
                        index = xlsx_column_index(cell.attrib["r"])
                        while len(values) <= index:
                            values.append("")
                        cell_type = cell.attrib.get("t")
                        value_node = cell.find("m:v", namespace)
                        if cell_type == "inlineStr":
                            value = "".join(
                                node.text or "" for node in cell.iter(f"{{{spreadsheet_ns}}}t")
                            )
                        elif value_node is None:
                            value = ""
                        elif cell_type == "s":
                            value = shared_strings[int(value_node.text or "0")]
                        else:
                            value = value_node.text or ""
                        values[index] = value
                    sheet_rows.append(values)
                if not sheet_rows:
                    continue
                headers = [normalized_source_header(value) for value in sheet_rows[0]]
                if "inventory_number" not in headers:
                    continue
                for row_number, values in enumerate(sheet_rows[1:], start=2):
                    record = {key: values[index] if index < len(values) else "" for index, key in enumerate(headers) if key}
                    record["_source_file"] = str(path)
                    record["_source_sheet"] = sheet.attrib.get("name", "")
                    record["_source_row"] = str(row_number)
                    rows.append(record)
    except (KeyError, OSError, ValueError, zipfile.BadZipFile, ElementTree.ParseError) as error:
        raise ArchiveError(f"cannot read source workbook {path}: {error}") from error
    return rows

def read_tabular_source_rows(path: Path) -> list[dict[str, str]]:
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise ArchiveError(f"source record file not found: {path}")
    if resolved.suffix.casefold() == ".xlsx":
        return read_xlsx_source_rows(resolved)
    if resolved.suffix.casefold() != ".csv":
        raise ArchiveError(f"source records must be CSV or XLSX: {resolved}")
    rows: list[dict[str, str]] = []
    with resolved.open("r", encoding="utf-8-sig", newline="") as handle:
        sample = handle.read(8192)
        handle.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
        except csv.Error:
            dialect = csv.excel
        reader = csv.DictReader(handle, dialect=dialect)
        for row_number, raw in enumerate(reader, start=2):
            record = {
                normalized_source_header(str(key)): str(value or "")
                for key, value in raw.items()
                if key is not None
            }
            record["_source_file"] = str(resolved)
            record["_source_sheet"] = ""
            record["_source_row"] = str(row_number)
            rows.append(record)
    if rows and "inventory_number" not in rows[0]:
        raise ArchiveError(f"source record file has no inventory_number column: {resolved}")
    return rows

def source_values(record: dict[str, str], *keys: str) -> str:
    values: list[str] = []
    for key in keys:
        value = re.sub(r"\s+", " ", record.get(key, "")).strip()
        if value and value not in values:
            values.append(value)
    return "; ".join(values)

def source_comparison(source: str, external: str) -> str:
    source_value = re.sub(r"\s+", " ", source).strip().casefold()
    external_value = re.sub(r"\s+", " ", external).strip().casefold()
    if not source_value or not external_value:
        return "not_available"
    return "same" if source_value == external_value else "different"

def museum_digital_local_identifiers(
    connection: sqlite3.Connection, object_row: sqlite3.Row
) -> tuple[list[str], list[str]]:
    inventory_values = []
    display_identifier = str(object_row["display_identifier"] or "").strip()
    if display_identifier:
        inventory_values.append(display_identifier)
    md_values = []
    for row in connection.execute(
        """
        SELECT scheme, value, status
        FROM identifiers
        WHERE entity_type = 'object' AND entity_id = ? AND status != 'rejected'
        ORDER BY CASE status WHEN 'accepted' THEN 0 ELSE 1 END, scheme, value
        """,
        (object_row["object_id"],),
    ):
        scheme = str(row["scheme"] or "").strip().casefold()
        value = str(row["value"] or "").strip()
        if scheme in {"inventory", "inventory-number", "inventarnummer"} and value:
            if value not in inventory_values:
                inventory_values.append(value)
        if scheme in {"museum-digital", "museum-digital-id", "md"} and value:
            if value not in md_values:
                md_values.append(value)
    return inventory_values, md_values

def museum_digital_record_row(
    local: sqlite3.Row,
    inventory_values: list[str],
    existing_md_ids: list[str],
    candidate: dict[str, Any] | None,
    match_status: str,
    match_method: str,
    fetch_error: str,
    source_matches: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    payload = candidate or {}
    md_id = str(payload.get("object_id") or "")
    md_inventory = str(payload.get("object_inventory_number") or "")
    md_title = re.sub(r"\s+", " ", str(payload.get("object_name") or "")).strip()
    local_title = re.sub(r"\s+", " ", str(local["archival_label"] or "")).strip()
    md_type = re.sub(r"\s+", " ", str(payload.get("object_type") or "")).strip()
    local_type = re.sub(r"\s+", " ", str(local["object_type"] or "")).strip()
    licence = payload.get("licence") if isinstance(payload.get("licence"), dict) else {}
    main_image = museum_digital_main_image(payload)
    images = payload.get("object_images") if isinstance(payload.get("object_images"), list) else []
    events = payload.get("object_events") if isinstance(payload.get("object_events"), list) else []
    tags = payload.get("object_tags") if isinstance(payload.get("object_tags"), list) else []
    collections = payload.get("object_collection") if isinstance(payload.get("object_collection"), list) else []
    languages = payload.get("langs") if isinstance(payload.get("langs"), list) else []
    source_records_supplied = source_matches is not None
    source_matches = source_matches or []
    source = source_matches[0] if len(source_matches) == 1 else {}
    source_status = (
        "not_supplied"
        if not source_records_supplied
        else "exact_inventory"
        if len(source_matches) == 1
        else "ambiguous"
        if source_matches
        else "not_found"
    )
    source_title = source_values(source, "object_title")
    source_type = source_values(source, "object_type")
    source_description = source_values(source, "object_description")
    source_material = source_values(source, "object_material_technique")
    source_dimensions = source_values(source, "object_dimensions")
    source_date = source_values(source, "printed_when_1", "printed_when1", "related_time1")
    md_description = re.sub(r"\s+", " ", str(payload.get("object_description") or "")).strip()
    md_material = str(payload.get("object_material_technique") or "")
    md_dimensions = str(payload.get("object_dimensions") or "")
    md_event_times = museum_digital_named_values(events, "time_name")
    if not md_event_times:
        md_event_times = "; ".join(
            re.sub(r"\s+", " ", str(item.get("time", {}).get("time_name") or "")).strip()
            for item in events
            if isinstance(item, dict) and isinstance(item.get("time"), dict) and item.get("time", {}).get("time_name")
        )
    if match_status == "exact_inventory" and source_status == "not_found":
        sync_action = (
            "accept_museum_digital_fallback_and_link_id"
            if md_id not in existing_md_ids
            else "accept_museum_digital_fallback_update"
        )
    elif match_status == "exact_inventory" and source_status == "not_supplied":
        sync_action = "locate_local_source_before_metadata_review"
    elif match_status == "exact_inventory" and source_status == "ambiguous":
        sync_action = "manual_source_record_review"
    elif match_status == "exact_inventory":
        sync_action = "review_metadata_and_link_id" if md_id not in existing_md_ids else "review_metadata_update"
    elif match_status in {"ambiguous", "external_id_inventory_mismatch", "local_identifier_conflict"}:
        sync_action = "manual_identity_review"
    else:
        sync_action = "none"
    return {
        "local_archival_id": str(local["object_id"]),
        "local_unit_id": str(local["unit_id"]),
        "local_inventory": "; ".join(inventory_values),
        "existing_museum_digital_id": "; ".join(existing_md_ids),
        "museum_digital_id": md_id,
        "match_status": match_status,
        "match_method": match_method,
        "sync_stub_action": sync_action,
        "source_record_status": source_status,
        "source_record_file": source.get("_source_file", ""),
        "source_record_sheet": source.get("_source_sheet", ""),
        "source_record_row": source.get("_source_row", ""),
        "source_inventory": source_values(source, "inventory_number"),
        "source_title": source_title,
        "source_object_type": source_type,
        "source_description": source_description,
        "source_material_technique": source_material,
        "source_dimensions": source_dimensions,
        "source_collection": source_values(source, "collection", "collection_name1"),
        "source_previous_owner": source_values(source, "owner_previous"),
        "source_places": source_values(source, "related_place1"),
        "source_actors": source_values(source, "related_actor1", "related_actor2", "printed_who1", "production_who"),
        "source_date": source_date,
        "source_image_names": source_values(source, "image_name1", "image_name_2"),
        "source_image_rights": source_values(source, "image_rights1"),
        "source_tags": source_values(source, *(f"tag{index}" for index in range(1, 11))),
        "local_archival_label": local_title,
        "museum_digital_title": md_title,
        "title_comparison": "same" if local_title.casefold() == md_title.casefold() and md_title else "different" if md_title else "not_available",
        "local_object_type": local_type,
        "museum_digital_object_type": md_type,
        "type_comparison": "same" if local_type.casefold() == md_type.casefold() and md_type else "different" if md_type else "not_available",
        "source_to_museum_digital_title": source_comparison(source_title, md_title),
        "source_to_museum_digital_description": source_comparison(source_description, md_description),
        "source_to_museum_digital_material": source_comparison(source_material, md_material),
        "source_to_museum_digital_dimensions": source_comparison(source_dimensions, md_dimensions),
        "source_to_museum_digital_date": source_comparison(source_date, md_event_times),
        "source_to_museum_digital_image_rights": source_comparison(
            source_values(source, "image_rights1"), str(main_image.get("rights") or "")
        ),
        "museum_digital_description": md_description,
        "museum_digital_material_technique": md_material,
        "museum_digital_dimensions": md_dimensions,
        "museum_digital_languages": "; ".join(str(value) for value in languages if value),
        "museum_digital_collections": museum_digital_named_values(collections, "collection_name"),
        "museum_digital_events": museum_digital_event_summary(events),
        "museum_digital_event_times": md_event_times,
        "museum_digital_subjects": museum_digital_named_values(tags, "tag_name"),
        "museum_digital_metadata_rights": str(licence.get("metadata_rights_status") or ""),
        "museum_digital_metadata_rights_holder": str(licence.get("metadata_rights_holder") or ""),
        "museum_digital_primary_image_rights": str(main_image.get("rights") or ""),
        "museum_digital_primary_image_owner": str(main_image.get("owner") or ""),
        "museum_digital_image_count": len(images),
        "museum_digital_event_count": len(events),
        "museum_digital_subject_count": len(tags),
        "museum_digital_last_updated": str(payload.get("object_last_updated") or ""),
        "museum_digital_url": MUSEUM_DIGITAL_OBJECT_URL.format(object_id=md_id) if md_id else "",
        "fetch_error": fetch_error,
    }

def museum_digital_schema_coverage(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    matched = [record for record in records if record["match_status"] == "exact_inventory"]
    fields = (
        ("museum-digital ID", "museum_digital_id", "identifier alias", "use existing identifiers table"),
        ("inventory number", "local_inventory", "object display identifier + alias", "already represented"),
        ("public title", "museum_digital_title", "not separate from archival label", "keep separate; resolve in ADR-006"),
        ("object type", "museum_digital_object_type", "free-text archival type", "add vocabulary mapping in projection"),
        ("description", "museum_digital_description", "not represented on object", "descriptive assertion deferred to ADR-006"),
        ("material / technique", "museum_digital_material_technique", "not represented", "projection metadata gap"),
        ("dimensions", "museum_digital_dimensions", "not represented", "projection metadata gap"),
        ("language", "museum_digital_languages", "not represented", "projection metadata gap"),
        ("collection", "museum_digital_collections", "unit is not a public collection", "projection relation gap"),
        ("structured event", "museum_digital_events", "event exists; actor/place vocabulary incomplete", "crosswalk existing events before extending"),
        ("subjects", "museum_digital_subjects", "not represented", "descriptive vocabulary deferred to ADR-006"),
        ("metadata rights", "museum_digital_metadata_rights", "publication gate only", "rights semantics deferred to ADR-006"),
        ("digital image rights", "museum_digital_primary_image_rights", "publication gate only", "must remain distinct from metadata rights"),
    )
    return [
        {
            "museum_digital_field": label,
            "records_with_value": sum(1 for record in matched if str(record[key]).strip()),
            "matched_records": len(matched),
            "current_archive_representation": current,
            "pilot_decision": decision,
        }
        for label, key, current, decision in fields
    ]

def write_csv_rows(path: Path, rows: list[dict[str, Any]], columns: list[str] | None = None) -> None:
    if columns is None:
        columns = list(rows[0].keys()) if rows else []
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)

def command_reconcile_museum_digital(args: argparse.Namespace) -> dict[str, Any]:
    paths = root_paths(Path(args.root))
    output_base = Path(args.output).resolve() if args.output else paths["root"] / "work" / "reconciliation" / "museum-digital"
    require_under(output_base, paths["root"])
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    run_dir = output_base / run_id
    suffix = 1
    while run_dir.exists():
        run_dir = output_base / f"{run_id}-{suffix}"
        suffix += 1
    raw_dir = run_dir / "raw"
    raw_dir.mkdir(parents=True)

    explicit_source_files = [Path(value).expanduser().resolve() for value in getattr(args, "source_records", [])]
    comparison_roots = [Path(value).expanduser().resolve() for value in getattr(args, "comparison_image_root", [])]
    local_image_index = comparison_image_index(comparison_roots)
    source_rows_by_path: dict[Path, list[dict[str, str]]] = {}
    visual_rows: list[dict[str, Any]] = []

    def source_rows(source_file: Path) -> list[dict[str, str]]:
        if source_file not in source_rows_by_path:
            source_rows_by_path[source_file] = read_tabular_source_rows(source_file)
        return source_rows_by_path[source_file]

    for source_file in explicit_source_files:
        source_rows(source_file)

    records: list[dict[str, Any]] = []
    with connect_db(paths["db"], read_only=True) as connection:
        query = "SELECT * FROM objects"
        parameters: tuple[Any, ...] = ()
        if args.unit:
            require_unit(connection, args.unit)
            query += " WHERE unit_id = ?"
            parameters = (args.unit,)
        query += " ORDER BY unit_id, display_identifier, object_id LIMIT ?"
        parameters += (args.limit,)
        objects = list(connection.execute(query, parameters))
        linked_source_files: dict[str, list[Path]] = {}
        for row in connection.execute(
            """
            SELECT r.subject_id, sf.unit_id, sf.archive_path, sf.intake_relpath
            FROM relationships r
            JOIN source_files sf
              ON r.object_type = 'source_file' AND r.object_id = sf.source_file_id
            WHERE r.subject_type = 'object'
              AND r.predicate = 'source_metadata'
              AND r.status = 'accepted'
              AND sf.status = 'active'
            """
        ):
            candidates = (
                paths["units"] / str(row["unit_id"]) / str(row["archive_path"]),
                paths["root"] / str(row["intake_relpath"]),
            )
            source_file = next((candidate.resolve() for candidate in candidates if candidate.is_file()), None)
            if source_file and source_file.suffix.casefold() in {".csv", ".xlsx"}:
                linked_source_files.setdefault(str(row["subject_id"]), []).append(source_file)
        linked_masters: dict[str, list[tuple[sqlite3.Row, Path | None]]] = {}
        for row in connection.execute(
            """
            SELECT r.subject_id, r.predicate, r.note AS relationship_note, sf.*
            FROM relationships r
            JOIN source_files sf
              ON r.object_type = 'source_file' AND r.object_id = sf.source_file_id
            WHERE r.subject_type = 'object'
              AND r.predicate IN (
                'primary_master', 'additional_master',
                'complete_reference', 'high_resolution_part'
              )
              AND r.status != 'rejected'
              AND sf.status = 'active'
            ORDER BY r.subject_id,
              CASE r.predicate
                WHEN 'primary_master' THEN 0
                WHEN 'complete_reference' THEN 1
                WHEN 'additional_master' THEN 2
                ELSE 3
              END,
              sf.original_filename,
              sf.source_file_id
            """
        ):
            candidates = (
                paths["units"] / str(row["unit_id"]) / str(row["archive_path"]),
                paths["root"] / str(row["intake_relpath"]),
            )
            payload = next((candidate.resolve() for candidate in candidates if candidate.is_file()), None)
            linked_masters.setdefault(str(row["subject_id"]), []).append((row, payload))
        for index, local in enumerate(objects):
            inventories, existing_md_ids = museum_digital_local_identifiers(connection, local)
            source_matches: list[dict[str, str]] = []
            seen_source_rows: set[tuple[str, str, str]] = set()
            object_source_files = list(explicit_source_files)
            for source_file in linked_source_files.get(str(local["object_id"]), []):
                if source_file not in object_source_files:
                    object_source_files.append(source_file)
            source_lookup: dict[str, list[dict[str, str]]] = {}
            for source_file in object_source_files:
                for source_record in source_rows(source_file):
                    source_inventory = normalize_museum_digital_inventory(
                        source_record.get("inventory_number", "")
                    )
                    if source_inventory:
                        source_lookup.setdefault(source_inventory, []).append(source_record)
            for inventory in inventories:
                for source_record in source_lookup.get(normalize_museum_digital_inventory(inventory), []):
                    source_key = (
                        source_record.get("_source_file", ""),
                        source_record.get("_source_sheet", ""),
                        source_record.get("_source_row", ""),
                    )
                    if source_key not in seen_source_rows:
                        source_matches.append(source_record)
                        seen_source_rows.add(source_key)
            candidate: dict[str, Any] | None = None
            status = "not_found"
            method = ""
            error = ""
            if len(existing_md_ids) > 1:
                status = "local_identifier_conflict"
                method = "museum-digital identifier"
            elif existing_md_ids:
                method = "museum-digital identifier"
                candidates, error = fetch_museum_digital_query(f"ids:{existing_md_ids[0]}", args.timeout)
                candidates = [item for item in candidates if str(item.get("object_id") or "") == existing_md_ids[0]]
                if len(candidates) == 1:
                    candidate = candidates[0]
                    md_inventory = normalize_museum_digital_inventory(str(candidate.get("object_inventory_number") or ""))
                    local_inventories = {normalize_museum_digital_inventory(value) for value in inventories}
                    status = "exact_inventory" if not local_inventories or md_inventory in local_inventories else "external_id_inventory_mismatch"
                elif len(candidates) > 1:
                    status = "ambiguous"
            elif not inventories:
                status = "no_local_inventory"
            else:
                method = "exact inventory"
                normalized = {normalize_museum_digital_inventory(value) for value in inventories}
                all_candidates: dict[str, dict[str, Any]] = {}
                for inventory in inventories:
                    candidates, fetch_error = fetch_museum_digital_query(inventory, args.timeout)
                    if fetch_error:
                        error = fetch_error
                        continue
                    for item in candidates:
                        institution = item.get("object_institution") if isinstance(item.get("object_institution"), dict) else {}
                        if int(institution.get("institution_id") or 0) != args.institution_id:
                            continue
                        if normalize_museum_digital_inventory(str(item.get("object_inventory_number") or "")) not in normalized:
                            continue
                        all_candidates[str(item.get("object_id") or "")] = item
                    if all_candidates:
                        break
                if len(all_candidates) == 1:
                    candidate = next(iter(all_candidates.values()))
                    status = "exact_inventory"
                elif len(all_candidates) > 1:
                    status = "ambiguous"
            if candidate:
                md_id = str(candidate.get("object_id") or "")
                if md_id:
                    (raw_dir / f"{md_id}.json").write_text(
                        json.dumps(candidate, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
                        encoding="utf-8",
                        newline="\n",
                    )
            if error and not candidate and status == "not_found":
                status = "fetch_error"
            record = museum_digital_record_row(
                local,
                inventories,
                existing_md_ids,
                candidate,
                status,
                method,
                error,
                source_matches if object_source_files else None,
            )
            records.append(record)

            object_id = str(local["object_id"])
            inventory = str(record["local_inventory"])
            masters = linked_masters.get(object_id, [])
            if not masters:
                visual_rows.append(
                    {
                        "image_id": visual_image_id(object_id, "preservation_master", "missing"),
                        "local_archival_id": object_id,
                        "local_inventory": inventory,
                        "role": "preservation_master",
                        "label": "Preservation master",
                        "source_file_id": "",
                        "original_path": "",
                        "snapshot_relpath": "",
                        "evidence_relpath": "",
                        "sha256": "",
                        "size_bytes": "",
                        "width": "",
                        "height": "",
                        "status": "missing",
                        "detail": "No preservation master is linked to this object",
                    }
                )
            for master_row, payload in masters:
                width, height = image_dimensions(payload) if payload else ("", "")
                master_detail = str(master_row["predicate"]).replace("_", " ")
                master_note = (
                    str(master_row["relationship_note"]).strip()
                    or str(master_row["note"]).strip()
                )
                if master_note:
                    first_sentence = master_note.split(". ", 1)[0].rstrip(".")
                    master_detail = f"{master_detail} — {first_sentence}"
                visual_role = {
                    "complete_reference": "complete_carrier_reference",
                    "high_resolution_part": "high_resolution_part",
                }.get(str(master_row["predicate"]), "preservation_master")
                visual_rows.append(
                    {
                        "image_id": visual_image_id(object_id, visual_role, str(master_row["source_file_id"])),
                        "local_archival_id": object_id,
                        "local_inventory": inventory,
                        "role": visual_role,
                        "label": str(master_row["original_filename"]),
                        "source_file_id": str(master_row["source_file_id"]),
                        "original_path": str(master_row["relative_path"]),
                        "snapshot_relpath": "",
                        "evidence_relpath": "",
                        "sha256": str(master_row["file_id"]).removeprefix("sha256:"),
                        "size_bytes": str(master_row["size_bytes"]),
                        "width": width,
                        "height": height,
                        "status": "available" if payload else "missing",
                        "detail": master_detail if payload else "Preserved payload is missing",
                    }
                )

            source_image_names = [
                value.strip()
                for value in str(record["source_image_names"]).split(";")
                if value.strip()
            ]
            if not source_image_names:
                visual_rows.append(
                    {
                        "image_id": visual_image_id(object_id, "source_record_image", "missing-name"),
                        "local_archival_id": object_id,
                        "local_inventory": inventory,
                        "role": "source_record_image",
                        "label": "Image named by source row",
                        "source_file_id": "",
                        "original_path": "",
                        "snapshot_relpath": "",
                        "evidence_relpath": "",
                        "sha256": "",
                        "size_bytes": "",
                        "width": "",
                        "height": "",
                        "status": "missing",
                        "detail": "The source row does not name an image",
                    }
                )
            for source_name in source_image_names:
                matches = local_image_index.get(Path(source_name).name.casefold(), [])
                if not matches:
                    visual_rows.append(
                        {
                            "image_id": visual_image_id(object_id, "source_record_image", source_name, "missing"),
                            "local_archival_id": object_id,
                            "local_inventory": inventory,
                            "role": "source_record_image",
                            "label": source_name,
                            "source_file_id": "",
                            "original_path": "",
                            "snapshot_relpath": "",
                            "evidence_relpath": "",
                            "sha256": "",
                            "size_bytes": "",
                            "width": "",
                            "height": "",
                            "status": "missing",
                            "detail": "The image named by the source row was not found in the supplied comparison roots",
                        }
                    )
                for match in matches:
                    image_id = visual_image_id(object_id, "source_record_image", str(match))
                    preview_relpath = Path("visual", "source", f"{image_id}.jpg")
                    preview_ok, preview_error = create_visual_preview(match, run_dir / preview_relpath)
                    width, height = image_dimensions(match)
                    visual_rows.append(
                        {
                            "image_id": image_id,
                            "local_archival_id": object_id,
                            "local_inventory": inventory,
                            "role": "source_record_image",
                            "label": source_name,
                            "source_file_id": "",
                            "original_path": str(match),
                            "snapshot_relpath": str(preview_relpath) if preview_ok else "",
                            "evidence_relpath": "",
                            "sha256": sha256_file(match),
                            "size_bytes": str(match.stat().st_size),
                            "width": width,
                            "height": height,
                            "status": "available" if preview_ok else "preview_unavailable",
                            "detail": (
                                f"{len(matches)} exact filename candidates found; compare every variant"
                                if len(matches) > 1
                                else preview_error
                            ),
                        }
                    )

            md_image = museum_digital_main_image(candidate or {})
            md_id = str((candidate or {}).get("object_id") or "")
            if not md_image or not md_id:
                visual_rows.append(
                    {
                        "image_id": visual_image_id(object_id, "museum_digital_snapshot", "missing"),
                        "local_archival_id": object_id,
                        "local_inventory": inventory,
                        "role": "museum_digital_snapshot",
                        "label": "Museum-digital image",
                        "source_file_id": "",
                        "original_path": "",
                        "snapshot_relpath": "",
                        "evidence_relpath": "",
                        "sha256": "",
                        "size_bytes": "",
                        "width": "",
                        "height": "",
                        "status": "missing",
                        "detail": "The museum-digital candidate has no usable main image",
                    }
                )
            else:
                image_id = visual_image_id(object_id, "museum_digital_snapshot", md_id, str(md_image.get("quell_id") or ""))
                filename = Path(str(md_image.get("filename_loc") or "image.jpg")).name
                suffix = Path(filename).suffix.casefold() if Path(filename).suffix.casefold() in VISUAL_IMAGE_SUFFIXES else ".bin"
                evidence_relpath = Path("visual", "museum-digital-evidence", f"{image_id}{suffix}")
                fetched, fetch_detail = fetch_external_image(md_image, run_dir / evidence_relpath, args.timeout)
                preview_relpath = Path("visual", "museum-digital", f"{image_id}.jpg")
                preview_ok, preview_error = (
                    create_visual_preview(run_dir / evidence_relpath, run_dir / preview_relpath)
                    if fetched
                    else (False, fetch_detail)
                )
                evidence_path = run_dir / evidence_relpath
                width, height = image_dimensions(evidence_path) if fetched else ("", "")
                visual_rows.append(
                    {
                        "image_id": image_id,
                        "local_archival_id": object_id,
                        "local_inventory": inventory,
                        "role": "museum_digital_snapshot",
                        "label": str(md_image.get("name") or filename or f"Museum-digital {md_id}"),
                        "source_file_id": "",
                        "original_path": fetch_detail if fetched else "",
                        "snapshot_relpath": str(preview_relpath) if preview_ok else "",
                        "evidence_relpath": str(evidence_relpath) if fetched else "",
                        "sha256": sha256_file(evidence_path) if fetched else "",
                        "size_bytes": str(evidence_path.stat().st_size) if fetched else "",
                        "width": width,
                        "height": height,
                        "status": "available" if preview_ok else "missing" if not fetched else "preview_unavailable",
                        "detail": preview_error,
                    }
                )
            if args.delay > 0 and index + 1 < len(objects):
                time.sleep(args.delay)

    coverage = museum_digital_schema_coverage(records)
    write_csv_rows(run_dir / "records.csv", records)
    write_csv_rows(run_dir / "field-coverage.csv", coverage)
    write_csv_rows(
        run_dir / "visual-comparison.csv",
        visual_rows,
        [
            "image_id",
            "local_archival_id",
            "local_inventory",
            "role",
            "label",
            "source_file_id",
            "original_path",
            "snapshot_relpath",
            "evidence_relpath",
            "sha256",
            "size_bytes",
            "width",
            "height",
            "status",
            "detail",
        ],
    )
    counts: dict[str, int] = {}
    for record in records:
        counts[record["match_status"]] = counts.get(record["match_status"], 0) + 1
    summary_lines = [
        "# Museum-digital reconciliation run",
        "",
        f"Generated: {utc_now()}",
        f"Catalog: `{paths['db']}`",
        f"Unit filter: `{args.unit or 'all'}`",
        f"Original source records: `{'; '.join(str(path) for path in source_rows_by_path) or 'not found'}`",
        f"Comparison image roots: `{'; '.join(str(path) for path in comparison_roots) or 'not supplied'}`",
        "",
        "This is a read-only reconciliation run. It does not change the catalog, WordPress, or museum-digital.",
        "Original source rows are the evidence baseline. Museum-digital JSON is an external comparison snapshot.",
        "The `sync_stub_action` column is advisory only.",
        "",
        "## Results",
        "",
    ]
    summary_lines.extend(f"- `{status}`: {count}" for status, count in sorted(counts.items()))
    summary_lines.extend(
        [
            "",
            "## Files",
            "",
            "- `records.csv`: one filterable original/local/museum-digital comparison row per local object; no JSON cells.",
            "- `field-coverage.csv`: museum-digital field coverage and the current archive boundary.",
            "- `visual-comparison.csv`: flat manifest for preservation, source-row, and museum-digital images shown side by side.",
            "- `raw/`: unchanged museum-digital JSON responses retained only as timestamped external comparison snapshots.",
            "- `visual/`: local comparison-image evidence and disposable previews; editors do not navigate source folders.",
            "",
            "Exact inventory matches are candidates, not silent overwrites. Museum-digital IDs remain aliases, and MD edits require evidence review before local acceptance.",
        ]
    )
    (run_dir / "README.md").write_text("\n".join(summary_lines) + "\n", encoding="utf-8", newline="\n")
    return {
        "result": "pass",
        "mode": "read_only_stub",
        "objects_checked": len(records),
        "counts": counts,
        "output": str(run_dir),
        "records_csv": str(run_dir / "records.csv"),
        "field_coverage_csv": str(run_dir / "field-coverage.csv"),
        "visual_comparison_csv": str(run_dir / "visual-comparison.csv"),
        "source_record_files": [str(path) for path in source_rows_by_path],
    }
