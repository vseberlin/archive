"""Guarded editor reconciliation for the minimal-core pilot workbench."""

from __future__ import annotations

import sqlite3
import json
import uuid
from typing import Any

import catalog_core


FIELD_PREDICATES = {
    "title": (
        "archive_descriptive_title",
        "descriptive_title",
        "Archive descriptive title",
    ),
    "description": (
        "archive_description",
        "description",
        "Archive description",
    ),
    "object_type": (
        "archive_object_type",
        "",
        "Archive object type",
    ),
    "material": (
        "archive_material_technique",
        "material_technique",
        "Archive material or technique",
    ),
    "dimensions": (
        "archive_dimensions",
        "dimensions",
        "Archive dimensions",
    ),
}
KEYWORD_PREDICATE = "archive_keyword"
SOURCE_CHOICES = {"archive", "local", "museum_digital", "corrected"}
UNIT_DEFAULT_KEYWORDS = {"WF-PARAGON": ("Paragon",)}


def accepted_metadata(connection: sqlite3.Connection, entity_id: str) -> dict[str, Any]:
    """Return assertion-backed object type and repeatable tags."""

    object_type = connection.execute(
        """SELECT value_text
           FROM evidence_assertions
           WHERE subject_entity_id = ?
             AND predicate = 'archive_object_type'
             AND status = 'accepted'
           ORDER BY asserted_at DESC, assertion_id DESC
           LIMIT 1""",
        (entity_id,),
    ).fetchone()
    tags = [
        str(row["value_text"])
        for row in connection.execute(
            """SELECT value_text
               FROM evidence_assertions
               WHERE subject_entity_id = ?
                 AND predicate = 'archive_keyword'
                 AND status = 'accepted'
               ORDER BY lower(value_text), value_text""",
            (entity_id,),
        )
        if str(row["value_text"] or "").strip()
    ]
    return {
        "object_type": str(object_type["value_text"] or "") if object_type else "",
        "tags": tags,
    }


def add_archive_keyword(
    connection: sqlite3.Connection,
    *,
    unit_id: str,
    entity_id: str,
    value: str,
    actor: str,
    reason: str = "",
    evidence: str = "",
) -> dict[str, Any]:
    """Add one archive-assigned keyword without replacing sourced keywords."""

    value = value.strip()
    actor = actor.strip()
    reason = reason.strip()
    if not value:
        raise catalog_core.CoreCatalogError("archive keyword cannot be empty")
    if not actor:
        raise catalog_core.CoreCatalogError("named keyword actor is required")
    now = catalog_core.utc_now()
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            """SELECT e.kind, e.status, u.status AS unit_status
               FROM entities e
               JOIN entity_unit_memberships m ON m.entity_id=e.entity_id
               JOIN units u ON u.unit_id=m.unit_id
               WHERE e.entity_id=? AND m.unit_id=? AND m.role='primary'""",
            (entity_id, unit_id),
        ).fetchone()
        if (
            not row
            or row["kind"] != "archive_object"
            or row["status"] != "active"
            or row["unit_status"] != "open"
        ):
            raise catalog_core.CoreCatalogError(
                "archive keyword requires an active object in an open unit"
            )
        existing = connection.execute(
            """SELECT assertion_id FROM evidence_assertions
               WHERE subject_entity_id=? AND predicate='archive_keyword'
                 AND status='accepted' AND lower(trim(value_text))=lower(?)
               ORDER BY asserted_at DESC, assertion_id DESC LIMIT 1""",
            (entity_id, value),
        ).fetchone()
        if existing:
            connection.rollback()
            return {
                "result": "ok",
                "state": "unchanged",
                "assertion_id": str(existing["assertion_id"]),
                "catalog_revision": int(
                    catalog_core.catalog_state(connection)["catalog_revision"]
                ),
            }
        _ensure_predicates(connection)
        source_id = _source(
            connection,
            source_choice="archive",
            reason=reason,
            evidence=evidence or f"unit:{unit_id}",
            external_id="",
            now=now,
        )
        assertion_id = _accepted_assertion(
            connection,
            entity_id=entity_id,
            predicate=KEYWORD_PREDICATE,
            value=value,
            source_id=source_id,
            actor=actor,
            reason=reason,
            now=now,
            supersede=False,
        )
        catalog_core._append_log(
            connection,
            actor,
            "editor_add_archive_keyword",
            "archive_object",
            entity_id,
            unit_id,
            {
                "assertion_id": assertion_id,
                "keyword": value,
                "source": "archive",
            },
            note=reason,
            evidence=evidence or f"unit:{unit_id}",
            at_utc=now,
        )
        revision = int(catalog_core.catalog_state(connection)["catalog_revision"])
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    return {
        "result": "ok",
        "state": "unpublished",
        "assertion_id": assertion_id,
        "catalog_revision": revision,
    }


def _ensure_predicates(connection: sqlite3.Connection) -> None:
    connection.execute(
        """INSERT OR IGNORE INTO assertion_predicates (
               predicate, label, value_kind, cardinality, status
           ) VALUES ('archive_object_type', 'Archive object type',
                     'text', 'one', 'active')"""
    )
    connection.execute(
        """INSERT OR IGNORE INTO assertion_predicates (
               predicate, label, value_kind, cardinality, status
           ) VALUES ('archive_keyword', 'Archive keyword or tag',
                     'text', 'many', 'active')"""
    )


def _source(
    connection: sqlite3.Connection,
    *,
    source_choice: str,
    reason: str,
    evidence: str,
    external_id: str,
    now: str,
) -> str:
    source_id = f"SOURCE-{uuid.uuid4().hex.upper()}"
    source_type = {
        "archive": "catalog",
        "local": "local_metadata_record",
        "museum_digital": "external_snapshot",
        "corrected": "editor_correction",
    }[source_choice]
    connection.execute(
        """INSERT INTO evidence_sources (
               source_id, source_type, system, external_id, captured_at,
               snapshot_digest, source_root_id, relative_path,
               local_absolute_path, citation, note
           ) VALUES (?, ?, ?, ?, ?, '', '', '', '', ?, ?)""",
        (
            source_id,
            source_type,
            "museum-digital" if source_choice == "museum_digital" else "",
            external_id if source_choice == "museum_digital" else "",
            now,
            evidence,
            reason,
        ),
    )
    return source_id


def _accepted_assertion(
    connection: sqlite3.Connection,
    *,
    entity_id: str,
    predicate: str,
    value: str,
    source_id: str,
    actor: str,
    reason: str,
    now: str,
    supersede: bool,
) -> str:
    assertion_id = f"ASSERT-{uuid.uuid4().hex.upper()}"
    if supersede:
        connection.execute(
            """UPDATE evidence_assertions
               SET status = 'superseded'
               WHERE subject_entity_id = ? AND predicate = ?
                 AND status = 'accepted'""",
            (entity_id, predicate),
        )
    connection.execute(
        """INSERT INTO evidence_assertions (
               assertion_id, subject_entity_id, predicate, value_text,
               object_entity_id, status, confidence, source_id, actor,
               asserted_at, supersedes_assertion_id, note
           ) VALUES (?, ?, ?, ?, NULL, 'accepted', 'certain', ?, ?, ?,
                     NULL, ?)""",
        (
            assertion_id,
            entity_id,
            predicate,
            value,
            source_id,
            actor,
            now,
            reason,
        ),
    )
    return assertion_id


def reconcile_metadata(
    connection: sqlite3.Connection,
    *,
    unit_id: str,
    entity_id: str,
    field_decisions: dict[str, dict[str, str]],
    tag_decision: dict[str, Any] | None,
    actor: str,
    reason: str = "",
    evidence: str = "",
    external_id: str = "",
) -> dict[str, Any]:
    """Atomically accept selected editor values into the authoritative catalog.

    ``reason`` is deliberately optional. Evidence source and actor remain
    mandatory so an empty comment never means an untracked change.
    """

    actor = actor.strip()
    reason = reason.strip()
    if not actor:
        raise catalog_core.CoreCatalogError("named reconciliation actor is required")
    unknown = set(field_decisions) - set(FIELD_PREDICATES)
    if unknown:
        raise catalog_core.CoreCatalogError(
            f"unsupported editor metadata fields: {', '.join(sorted(unknown))}"
        )
    now = catalog_core.utc_now()
    accepted: dict[str, dict[str, str]] = {}
    accepted_tags: list[str] | None = None
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            """SELECT e.kind, e.status, u.status AS unit_status
               FROM entities e
               JOIN entity_unit_memberships m ON m.entity_id = e.entity_id
               JOIN units u ON u.unit_id = m.unit_id
               WHERE e.entity_id = ? AND m.unit_id = ?
                 AND m.role = 'primary'""",
            (entity_id, unit_id),
        ).fetchone()
        if (
            not row
            or row["kind"] != "archive_object"
            or row["status"] != "active"
            or row["unit_status"] != "open"
        ):
            raise catalog_core.CoreCatalogError(
                "editor reconciliation requires an active object in an open unit"
            )
        _ensure_predicates(connection)
        normalized_fields: dict[str, dict[str, str]] = {}
        for field, decision in field_decisions.items():
            normalized_fields[field] = {
                "source": str(decision.get("source") or "").strip(),
                "value": str(decision.get("value") or "").strip(),
            }
        normalized_tags: list[str] | None = None
        normalized_tag_source = ""
        if tag_decision is not None:
            normalized_tag_source = str(tag_decision.get("source") or "").strip()
            raw_tags = tag_decision.get("values")
            if isinstance(raw_tags, list):
                seen: set[str] = set()
                normalized_tags = []
                for raw in raw_tags:
                    value = str(raw or "").strip()
                    folded = value.casefold()
                    if value and folded not in seen:
                        seen.add(folded)
                        normalized_tags.append(value)
        latest = connection.execute(
            """SELECT entity_id, after_json, note
               FROM logbook WHERE action = 'editor_reconcile_metadata'
                 AND entity_id = ? ORDER BY at_utc DESC, log_id DESC LIMIT 1""",
            (entity_id,),
        ).fetchone()
        if latest:
            try:
                prior = json.loads(str(latest["after_json"] or "{}"))
            except json.JSONDecodeError:
                prior = {}
            prior_fields = {
                key: {
                    "source": str(value.get("source") or ""),
                    "value": str(value.get("value") or ""),
                }
                for key, value in dict(prior.get("fields") or {}).items()
                if isinstance(value, dict)
            }
            prior_tags = prior.get("tags")
            prior_tag_source = str(prior.get("tag_source") or "")
            if (
                prior_fields == normalized_fields
                and prior_tags == normalized_tags
                and prior_tag_source == normalized_tag_source
                and str(latest["note"] or "") == reason
            ):
                connection.rollback()
                return {
                    "result": "ok",
                    "decision_id": str(prior.get("decision_id") or ""),
                    "catalog_revision": int(
                        catalog_core.catalog_state(connection)["catalog_revision"]
                    ),
                    "fields": prior.get("fields") or {},
                    "tags": prior_tags,
                    "reason": reason,
                    "state": "unchanged",
                }
        for field, decision in field_decisions.items():
            source_choice = str(decision.get("source") or "").strip()
            value = str(decision.get("value") or "").strip()
            if source_choice not in SOURCE_CHOICES:
                raise catalog_core.CoreCatalogError(
                    f"unsupported source choice for {field}: {source_choice}"
                )
            if not value:
                raise catalog_core.CoreCatalogError(
                    f"accepted {field.replace('_', ' ')} value cannot be empty"
                )
            predicate, column, _label = FIELD_PREDICATES[field]
            source_id = _source(
                connection,
                source_choice=source_choice,
                reason=reason,
                evidence=evidence,
                external_id=external_id,
                now=now,
            )
            assertion_id = _accepted_assertion(
                connection,
                entity_id=entity_id,
                predicate=predicate,
                value=value,
                source_id=source_id,
                actor=actor,
                reason=reason,
                now=now,
                supersede=True,
            )
            if column:
                connection.execute(
                    f'UPDATE archive_objects SET "{column}" = ?, updated_at = ? '
                    "WHERE entity_id = ?",
                    (value, now, entity_id),
                )
            accepted[field] = {
                "source": source_choice,
                "value": value,
                "assertion_id": assertion_id,
            }
        if tag_decision is not None:
            source_choice = str(tag_decision.get("source") or "").strip()
            if source_choice not in SOURCE_CHOICES:
                raise catalog_core.CoreCatalogError(
                    f"unsupported tag source choice: {source_choice}"
                )
            raw_tags = tag_decision.get("values")
            if not isinstance(raw_tags, list):
                raise catalog_core.CoreCatalogError("accepted tags must be a list")
            seen: set[str] = set()
            accepted_tags = []
            for raw in raw_tags:
                value = str(raw or "").strip()
                folded = value.casefold()
                if value and folded not in seen:
                    seen.add(folded)
                    accepted_tags.append(value)
            source_id = _source(
                connection,
                source_choice=source_choice,
                reason=reason,
                evidence=evidence,
                external_id=external_id,
                now=now,
            )
            connection.execute(
                """UPDATE evidence_assertions
                   SET status = 'superseded'
                   WHERE subject_entity_id = ?
                     AND predicate = 'archive_keyword'
                     AND status = 'accepted'""",
                (entity_id,),
            )
            for tag in accepted_tags:
                _accepted_assertion(
                    connection,
                    entity_id=entity_id,
                    predicate=KEYWORD_PREDICATE,
                    value=tag,
                    source_id=source_id,
                    actor=actor,
                    reason=reason,
                    now=now,
                    supersede=False,
                )
        if not accepted and accepted_tags is None:
            raise catalog_core.CoreCatalogError(
                "choose at least one metadata field or tag source"
            )
        decision_id = f"EDITOR-META-{uuid.uuid4().hex.upper()}"
        payload = {
            "decision_id": decision_id,
            "entity_id": entity_id,
            "fields": accepted,
            "tags": accepted_tags,
            "tag_source": (
                str(tag_decision.get("source") or "").strip()
                if tag_decision is not None
                else ""
            ),
        }
        catalog_core._append_log(
            connection,
            actor,
            "editor_reconcile_metadata",
            "archive_object",
            entity_id,
            unit_id,
            payload,
            note=reason,
            evidence=evidence,
            at_utc=now,
        )
        revision = int(catalog_core.catalog_state(connection)["catalog_revision"])
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    return {
        "result": "ok",
        "decision_id": decision_id,
        "catalog_revision": revision,
        "fields": accepted,
        "tags": accepted_tags,
        "reason": reason,
        "state": "unpublished",
    }
