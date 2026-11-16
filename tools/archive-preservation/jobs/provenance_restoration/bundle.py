"""Portable accession-bundle contract for one-way provenance restoration."""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any


BUNDLE_FORMAT = "iss-provenance-restoration-bundle"
BUNDLE_VERSION = 1
BUNDLE_DIRECTION = "sources_to_archive"

DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
CASE_ID_RE = re.compile(r"^RESTORE-[A-Z0-9][A-Z0-9._-]*$")

SOURCE_KINDS = {
    "local_filesystem",
    "local_catalog_row",
    "museum_digital_snapshot",
    "museum_digital_derivative_capture",
}
FILE_ROLES = {
    "local_master",
    "local_master_candidate",
    "local_derivative",
    "museum_digital_derivative",
    "metadata_file",
}
LOCAL_SEARCH_STATUSES = {
    "exact_inventory",
    "not_found",
    "not_supplied",
    "ambiguous",
}
IDENTITY_STATES = {"pending", "resolved", "rejected", "deferred"}
IDENTITY_OUTCOMES = {
    "unresolved",
    "same_entity",
    "component_of",
    "not_same",
    "defer",
}
METADATA_ACTIONS = {
    "accept_local",
    "accept_museum_digital",
    "corrected",
    "defer",
}
READINESS_STATES = {"draft", "blocked", "ready_for_ingest"}
FILE_STATES = {
    "verified_local_master",
    "best_available_surrogate",
    "missing_master",
    "unresolved",
}
REVIEW_STATES = {"draft", "reviewed"}
DERIVATION_KINDS = {"source_extraction", "md_derivative_capture"}
FILE_DECISION_STATES = {"pending", "resolved", "deferred"}
FILE_DECISION_OUTCOMES = {
    "unresolved",
    "verified_local_master",
    "best_available_surrogate",
    "missing_master",
    "defer",
}


class ProvenanceBundleError(ValueError):
    """Raised when a restoration bundle violates the frozen contract."""


def snapshot_digest(snapshot: Any) -> str:
    """Return the canonical SHA-256 for one embedded structured snapshot."""

    try:
        payload = json.dumps(
            snapshot,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as error:
        raise ProvenanceBundleError(
            "structured evidence snapshot must be valid JSON"
        ) from error
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ProvenanceBundleError(f"{label} must be an object")
    return value


def _list(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise ProvenanceBundleError(f"{label} must be a list")
    return value


def _string(value: Any, label: str, *, required: bool = True) -> str:
    if not isinstance(value, str):
        raise ProvenanceBundleError(f"{label} must be text")
    if required and not value.strip():
        raise ProvenanceBundleError(f"{label} must not be empty")
    return value


def _keys(
    value: dict[str, Any],
    label: str,
    required: set[str],
    optional: set[str] | None = None,
) -> None:
    optional = optional or set()
    missing = required - value.keys()
    unexpected = value.keys() - required - optional
    if missing:
        raise ProvenanceBundleError(
            f"{label} is missing: {', '.join(sorted(missing))}"
        )
    if unexpected:
        raise ProvenanceBundleError(
            f"{label} has unexpected fields: {', '.join(sorted(unexpected))}"
        )


def _choice(value: Any, label: str, choices: set[str]) -> str:
    text = _string(value, label)
    if text not in choices:
        raise ProvenanceBundleError(
            f"{label} must be one of: {', '.join(sorted(choices))}"
        )
    return text


def _timestamp(value: Any, label: str, *, required: bool = True) -> str:
    text = _string(value, label, required=required)
    if not text:
        return text
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as error:
        raise ProvenanceBundleError(f"{label} must be an ISO-8601 timestamp") from error
    if parsed.tzinfo is None:
        raise ProvenanceBundleError(f"{label} must include a timezone")
    return text


def _digest(value: Any, label: str) -> str:
    text = _string(value, label)
    if not DIGEST_RE.fullmatch(text):
        raise ProvenanceBundleError(f"{label} must be a sha256: digest")
    return text


def _unique_id(value: Any, label: str, seen: set[str]) -> str:
    text = _string(value, label)
    if text in seen:
        raise ProvenanceBundleError(f"duplicate {label}: {text}")
    seen.add(text)
    return text


def _validate_identity(
    identity: dict[str, Any],
    source_ids: set[str],
) -> str:
    _keys(
        identity,
        "identity",
        {
            "state",
            "outcome",
            "reason",
            "actor",
            "decided_at",
            "evidence_source_ids",
        },
    )
    state = _choice(identity["state"], "identity.state", IDENTITY_STATES)
    outcome = _choice(identity["outcome"], "identity.outcome", IDENTITY_OUTCOMES)
    reason = _string(identity["reason"], "identity.reason", required=False)
    actor = _string(identity["actor"], "identity.actor", required=False)
    decided_at = _timestamp(
        identity["decided_at"],
        "identity.decided_at",
        required=False,
    )
    evidence = _list(
        identity["evidence_source_ids"],
        "identity.evidence_source_ids",
    )
    if len(evidence) != len(set(evidence)):
        raise ProvenanceBundleError("identity.evidence_source_ids contains duplicates")
    for source_id in evidence:
        if _string(source_id, "identity evidence source ID") not in source_ids:
            raise ProvenanceBundleError(
                f"identity references unknown source: {source_id}"
            )

    expected_outcome = {
        "pending": "unresolved",
        "rejected": "not_same",
        "deferred": "defer",
    }
    if state in expected_outcome and outcome != expected_outcome[state]:
        raise ProvenanceBundleError(
            f"identity state {state!r} requires outcome "
            f"{expected_outcome[state]!r}"
        )
    if state == "resolved" and outcome not in {"same_entity", "component_of"}:
        raise ProvenanceBundleError(
            "resolved identity requires same_entity or component_of"
        )
    if state == "resolved":
        if not actor.strip() or not decided_at:
            raise ProvenanceBundleError(
                "resolved identity requires actor and decided_at"
            )
        if not evidence:
            raise ProvenanceBundleError(
                "resolved identity requires at least one evidence source"
            )
    return state


def _validate_lineage(lineage: dict[str, Any]) -> str:
    _keys(
        lineage,
        "lineage",
        {
            "state",
            "draft_bundle_digest",
            "decision_digest",
            "decision_snapshot",
        },
    )
    state = _choice(lineage["state"], "lineage.state", REVIEW_STATES)
    draft_digest = _string(
        lineage["draft_bundle_digest"],
        "lineage.draft_bundle_digest",
        required=False,
    )
    decision_digest = _string(
        lineage["decision_digest"],
        "lineage.decision_digest",
        required=False,
    )
    decision_snapshot = _object(
        lineage["decision_snapshot"],
        "lineage.decision_snapshot",
    )
    if state == "draft":
        if draft_digest or decision_digest or decision_snapshot:
            raise ProvenanceBundleError(
                "draft lineage must not claim review evidence"
            )
    else:
        _digest(draft_digest, "lineage.draft_bundle_digest")
        _digest(decision_digest, "lineage.decision_digest")
        if not decision_snapshot:
            raise ProvenanceBundleError(
                "reviewed lineage requires its decision snapshot"
            )
        if snapshot_digest(decision_snapshot) != decision_digest:
            raise ProvenanceBundleError(
                "lineage decision snapshot digest does not match"
            )
    return state


def _validate_derivation(derivation: dict[str, Any]) -> None:
    _keys(
        derivation,
        "derivation",
        {
            "kind",
            "parent_bundle_digest",
            "operation_digest",
            "operation_snapshot",
        },
    )
    kind = _choice(
        derivation["kind"],
        "derivation.kind",
        DERIVATION_KINDS,
    )
    parent_digest = _string(
        derivation["parent_bundle_digest"],
        "derivation.parent_bundle_digest",
        required=False,
    )
    operation_digest = _digest(
        derivation["operation_digest"],
        "derivation.operation_digest",
    )
    operation = _object(
        derivation["operation_snapshot"],
        "derivation.operation_snapshot",
    )
    if not operation:
        raise ProvenanceBundleError(
            "derivation requires an operation snapshot"
        )
    if snapshot_digest(operation) != operation_digest:
        raise ProvenanceBundleError(
            "derivation operation snapshot digest does not match"
        )
    if kind == "source_extraction" and parent_digest:
        raise ProvenanceBundleError(
            "source extraction must not claim a parent bundle"
        )
    if kind == "md_derivative_capture":
        _digest(parent_digest, "derivation.parent_bundle_digest")


def _validate_file_decision(
    decision: dict[str, Any],
    file_roles: dict[str, str],
) -> tuple[str, str, str]:
    _keys(
        decision,
        "file_decision",
        {
            "state",
            "outcome",
            "selected_file_id",
            "verification_method",
            "verification_details",
            "reason",
            "actor",
            "decided_at",
        },
    )
    state = _choice(
        decision["state"],
        "file_decision.state",
        FILE_DECISION_STATES,
    )
    outcome = _choice(
        decision["outcome"],
        "file_decision.outcome",
        FILE_DECISION_OUTCOMES,
    )
    selected_file_id = _string(
        decision["selected_file_id"],
        "file_decision.selected_file_id",
        required=False,
    )
    method = _string(
        decision["verification_method"],
        "file_decision.verification_method",
        required=False,
    )
    details = _object(
        decision["verification_details"],
        "file_decision.verification_details",
    )
    reason = _string(
        decision["reason"],
        "file_decision.reason",
        required=False,
    )
    actor = _string(
        decision["actor"],
        "file_decision.actor",
        required=False,
    )
    decided_at = _timestamp(
        decision["decided_at"],
        "file_decision.decided_at",
        required=False,
    )

    if state == "pending":
        if outcome != "unresolved":
            raise ProvenanceBundleError(
                "pending file decision requires unresolved outcome"
            )
        if (
            selected_file_id
            or method
            or details
            or reason
            or actor
            or decided_at
        ):
            raise ProvenanceBundleError(
                "pending file decision must not claim review details"
            )
        return state, outcome, selected_file_id

    if state == "deferred":
        if outcome != "defer":
            raise ProvenanceBundleError(
                "deferred file decision requires defer outcome"
            )
        if selected_file_id or method or details:
            raise ProvenanceBundleError(
                "deferred file decision must not select or verify a file"
            )
    elif outcome not in {
        "verified_local_master",
        "best_available_surrogate",
        "missing_master",
    }:
        raise ProvenanceBundleError(
            "resolved file decision requires a concrete file outcome"
        )

    if not actor.strip() or not decided_at:
        raise ProvenanceBundleError(
            "reviewed file decision requires actor and decided_at"
        )

    selected_role = file_roles.get(selected_file_id, "")
    if outcome == "verified_local_master":
        if selected_role != "local_master":
            raise ProvenanceBundleError(
                "verified file decision requires a selected local_master"
            )
        if not method.strip():
            raise ProvenanceBundleError(
                "verified local master requires a verification method"
            )
        if not details:
            raise ProvenanceBundleError(
                "verified local master requires verification details"
            )
    elif outcome == "best_available_surrogate":
        if selected_role not in {
            "local_derivative",
            "museum_digital_derivative",
        }:
            raise ProvenanceBundleError(
                "surrogate file decision requires a selected derivative"
            )
        if not method.strip():
            raise ProvenanceBundleError(
                "selected surrogate requires a verification method"
            )
        if not details:
            raise ProvenanceBundleError(
                "selected surrogate requires verification details"
            )
    elif selected_file_id or method or details:
        raise ProvenanceBundleError(
            f"{outcome} file decision must not select or verify a file"
        )
    return state, outcome, selected_file_id


def validate_bundle(bundle: dict[str, Any]) -> None:
    """Validate one complete, portable provenance-restoration bundle."""

    bundle = _object(bundle, "bundle")
    _keys(
        bundle,
        "bundle",
        {
            "format",
            "version",
            "direction",
            "case_id",
            "created_at",
            "inventory_label",
            "local_search_status",
            "lineage",
            "sources",
            "files",
            "identity",
            "metadata_decisions",
            "file_decision",
            "readiness",
        },
        {"derivation"},
    )
    if bundle["format"] != BUNDLE_FORMAT:
        raise ProvenanceBundleError(f"unsupported bundle format: {bundle['format']!r}")
    if bundle["version"] != BUNDLE_VERSION:
        raise ProvenanceBundleError(
            f"unsupported bundle version: {bundle['version']!r}"
        )
    if bundle["direction"] != BUNDLE_DIRECTION:
        raise ProvenanceBundleError(
            "provenance restoration is one-way from sources to archive"
        )
    case_id = _string(bundle["case_id"], "case_id")
    if not CASE_ID_RE.fullmatch(case_id):
        raise ProvenanceBundleError(
            "case_id must use RESTORE- followed by uppercase stable text"
        )
    _timestamp(bundle["created_at"], "created_at")
    _string(bundle["inventory_label"], "inventory_label")
    local_search_status = _choice(
        bundle["local_search_status"],
        "local_search_status",
        LOCAL_SEARCH_STATUSES,
    )
    lineage_state = _validate_lineage(
        _object(bundle["lineage"], "lineage")
    )
    if "derivation" in bundle:
        _validate_derivation(
            _object(bundle["derivation"], "derivation")
        )

    source_ids: set[str] = set()
    source_kinds: dict[str, str] = {}
    for index, raw_source in enumerate(_list(bundle["sources"], "sources")):
        source = _object(raw_source, f"sources[{index}]")
        _keys(
            source,
            f"sources[{index}]",
            {
                "source_id",
                "kind",
                "locator",
                "captured_at",
                "snapshot_digest",
                "note",
            },
            {"snapshot"},
        )
        source_id = _unique_id(
            source["source_id"],
            f"sources[{index}].source_id",
            source_ids,
        )
        source_kind = _choice(
            source["kind"],
            f"sources[{index}].kind",
            SOURCE_KINDS,
        )
        source_kinds[source_id] = source_kind
        _string(source["locator"], f"sources[{index}].locator")
        _timestamp(source["captured_at"], f"sources[{index}].captured_at")
        declared_snapshot_digest = _digest(
            source["snapshot_digest"],
            f"sources[{index}].snapshot_digest",
        )
        if source_kind in {
            "local_catalog_row",
            "museum_digital_snapshot",
            "museum_digital_derivative_capture",
        }:
            if "snapshot" not in source:
                raise ProvenanceBundleError(
                    f"sources[{index}] must embed its structured evidence snapshot"
                )
            actual_snapshot_digest = snapshot_digest(source["snapshot"])
            if actual_snapshot_digest != declared_snapshot_digest:
                raise ProvenanceBundleError(
                    f"sources[{index}] structured snapshot digest does not match"
                )
        elif "snapshot" in source:
            raise ProvenanceBundleError(
                f"sources[{index}] local filesystem evidence must not embed file bytes"
            )
        _string(source["note"], f"sources[{index}].note", required=False)
    if not source_ids:
        raise ProvenanceBundleError("bundle requires at least one evidence source")

    file_ids: set[str] = set()
    file_roles: dict[str, str] = {}
    for index, raw_file in enumerate(_list(bundle["files"], "files")):
        file = _object(raw_file, f"files[{index}]")
        _keys(
            file,
            f"files[{index}]",
            {
                "file_id",
                "role",
                "source_id",
                "locator",
                "content_digest",
                "size_bytes",
                "media_type",
                "note",
            },
        )
        file_id = _unique_id(
            file["file_id"],
            f"files[{index}].file_id",
            file_ids,
        )
        role = _choice(
            file["role"],
            f"files[{index}].role",
            FILE_ROLES,
        )
        file_roles[file_id] = role
        source_id = _string(file["source_id"], f"files[{index}].source_id")
        if source_id not in source_ids:
            raise ProvenanceBundleError(
                f"file {file_id} references unknown source: {source_id}"
            )
        source_kind = source_kinds[source_id]
        if role in {
            "local_master",
            "local_master_candidate",
            "local_derivative",
            "metadata_file",
        }:
            if source_kind != "local_filesystem":
                raise ProvenanceBundleError(
                    f"file {file_id} has local role {role!r} "
                    f"but source kind {source_kind!r}"
                )
        if (
            role == "museum_digital_derivative"
            and source_kind
            not in {
                "museum_digital_snapshot",
                "museum_digital_derivative_capture",
            }
        ):
            raise ProvenanceBundleError(
                f"file {file_id} is an MD derivative from a non-MD source"
            )
        _string(file["locator"], f"files[{index}].locator")
        _digest(file["content_digest"], f"files[{index}].content_digest")
        size = file["size_bytes"]
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise ProvenanceBundleError(
                f"files[{index}].size_bytes must be a non-negative integer"
            )
        _string(file["media_type"], f"files[{index}].media_type")
        _string(file["note"], f"files[{index}].note", required=False)

    (
        file_decision_state,
        file_decision_outcome,
        file_decision_selected_id,
    ) = _validate_file_decision(
        _object(bundle["file_decision"], "file_decision"),
        file_roles,
    )
    identity_state = _validate_identity(
        _object(bundle["identity"], "identity"),
        source_ids,
    )

    fields: set[str] = set()
    has_deferred_metadata = False
    for index, raw_decision in enumerate(
        _list(bundle["metadata_decisions"], "metadata_decisions")
    ):
        decision = _object(raw_decision, f"metadata_decisions[{index}]")
        _keys(
            decision,
            f"metadata_decisions[{index}]",
            {
                "field",
                "value",
                "action",
                "source_id",
                "source_locator",
                "reason",
                "actor",
                "decided_at",
            },
        )
        field = _unique_id(
            decision["field"],
            f"metadata_decisions[{index}].field",
            fields,
        )
        value = _string(
            decision["value"],
            f"metadata_decisions[{index}].value",
            required=False,
        )
        action = _choice(
            decision["action"],
            f"metadata_decisions[{index}].action",
            METADATA_ACTIONS,
        )
        source_id = _string(
            decision["source_id"],
            f"metadata_decisions[{index}].source_id",
            required=False,
        )
        _string(
            decision["source_locator"],
            f"metadata_decisions[{index}].source_locator",
            required=False,
        )
        _string(
            decision["reason"],
            f"metadata_decisions[{index}].reason",
            required=False,
        )
        _string(decision["actor"], f"metadata_decisions[{index}].actor")
        _timestamp(
            decision["decided_at"],
            f"metadata_decisions[{index}].decided_at",
        )

        if action in {"accept_local", "accept_museum_digital"}:
            if source_id not in source_ids:
                raise ProvenanceBundleError(
                    f"metadata field {field} references unknown source: {source_id}"
                )
            source_kind = source_kinds[source_id]
            if action == "accept_local" and source_kind == "museum_digital_snapshot":
                raise ProvenanceBundleError(
                    f"metadata field {field} accepts local data from an MD source"
                )
            if (
                action == "accept_museum_digital"
                and source_kind != "museum_digital_snapshot"
            ):
                raise ProvenanceBundleError(
                    f"metadata field {field} accepts MD data from a local source"
                )
            if action == "accept_museum_digital" and local_search_status in {
                "not_supplied",
                "ambiguous",
            }:
                raise ProvenanceBundleError(
                    "MD metadata requires a completed, unambiguous local search"
                )
        elif source_id:
            if source_id not in source_ids:
                raise ProvenanceBundleError(
                    f"metadata field {field} references unknown source: {source_id}"
                )
        if action != "defer" and not value.strip():
            raise ProvenanceBundleError(
                f"metadata field {field} requires an accepted value"
            )
        has_deferred_metadata = has_deferred_metadata or action == "defer"

    readiness = _object(bundle["readiness"], "readiness")
    _keys(
        readiness,
        "readiness",
        {
            "state",
            "file_state",
            "selected_file_id",
            "provenance_gaps",
            "blocking_gaps",
        },
    )
    readiness_state = _choice(
        readiness["state"],
        "readiness.state",
        READINESS_STATES,
    )
    file_state = _choice(
        readiness["file_state"],
        "readiness.file_state",
        FILE_STATES,
    )
    selected_file_id = _string(
        readiness["selected_file_id"],
        "readiness.selected_file_id",
        required=False,
    )
    provenance_gaps = [
        _string(value, "readiness provenance gap")
        for value in _list(readiness["provenance_gaps"], "readiness.provenance_gaps")
    ]
    blocking_gaps = [
        _string(value, "readiness blocking gap")
        for value in _list(readiness["blocking_gaps"], "readiness.blocking_gaps")
    ]
    if len(provenance_gaps) != len(set(provenance_gaps)):
        raise ProvenanceBundleError("readiness.provenance_gaps contains duplicates")
    if len(blocking_gaps) != len(set(blocking_gaps)):
        raise ProvenanceBundleError("readiness.blocking_gaps contains duplicates")

    selected_role = file_roles.get(selected_file_id, "")
    if selected_file_id and not selected_role:
        raise ProvenanceBundleError(
            f"readiness references unknown selected file: {selected_file_id}"
        )
    if file_state == "verified_local_master" and selected_role != "local_master":
        raise ProvenanceBundleError(
            "verified_local_master requires a selected local_master file"
        )
    if file_state == "best_available_surrogate":
        if selected_role not in {
            "local_derivative",
            "museum_digital_derivative",
        }:
            raise ProvenanceBundleError(
                "best_available_surrogate requires a selected derivative"
            )
        if "missing_local_master" not in provenance_gaps:
            raise ProvenanceBundleError(
                "a surrogate must retain the missing_local_master provenance gap"
            )
    if file_state in {"missing_master", "unresolved"} and selected_file_id:
        raise ProvenanceBundleError(
            f"{file_state} must not select an ingest file"
        )

    expected_file_state = {
        "unresolved": "unresolved",
        "defer": "unresolved",
        "verified_local_master": "verified_local_master",
        "best_available_surrogate": "best_available_surrogate",
        "missing_master": "missing_master",
    }[file_decision_outcome]
    if file_state != expected_file_state:
        raise ProvenanceBundleError(
            "readiness.file_state must match the technical file decision"
        )
    if selected_file_id != file_decision_selected_id:
        raise ProvenanceBundleError(
            "readiness selected file must match the technical file decision"
        )

    if lineage_state == "draft":
        if identity_state != "pending":
            raise ProvenanceBundleError(
                "draft lineage requires pending identity"
            )
        if file_decision_state != "pending":
            raise ProvenanceBundleError(
                "draft lineage requires pending file decision"
            )
        if fields:
            raise ProvenanceBundleError(
                "draft lineage must not contain metadata decisions"
            )
        if readiness_state != "draft":
            raise ProvenanceBundleError(
                "draft lineage requires draft readiness"
            )

    if readiness_state == "ready_for_ingest":
        if lineage_state != "reviewed":
            raise ProvenanceBundleError(
                "ready_for_ingest requires reviewed lineage"
            )
        if identity_state != "resolved":
            raise ProvenanceBundleError(
                "ready_for_ingest requires resolved identity"
            )
        if blocking_gaps:
            raise ProvenanceBundleError(
                "ready_for_ingest must not contain blocking gaps"
            )
        if file_state not in {
            "verified_local_master",
            "best_available_surrogate",
        }:
            raise ProvenanceBundleError(
                "ready_for_ingest requires a verified master or explicit surrogate"
            )
        if file_decision_state != "resolved":
            raise ProvenanceBundleError(
                "ready_for_ingest requires resolved technical file review"
            )
        if not selected_file_id:
            raise ProvenanceBundleError(
                "ready_for_ingest requires one selected file"
            )
        if not fields:
            raise ProvenanceBundleError(
                "ready_for_ingest requires metadata decisions"
            )
        if has_deferred_metadata:
            raise ProvenanceBundleError(
                "ready_for_ingest must not contain deferred metadata decisions"
            )


def canonical_json(bundle: dict[str, Any]) -> str:
    """Return the validated bundle in deterministic open JSON form."""

    validate_bundle(bundle)
    return json.dumps(
        bundle,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ) + "\n"


def bundle_digest(bundle: dict[str, Any]) -> str:
    """Return the digest of the exact canonical bundle representation."""

    return "sha256:" + hashlib.sha256(
        canonical_json(bundle).encode("utf-8")
    ).hexdigest()


def write_bundle(path: Path, bundle: dict[str, Any]) -> str:
    """Atomically create one immutable-by-contract bundle without overwriting."""

    text = canonical_json(bundle)
    path = path.expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError as error:
            raise ProvenanceBundleError(
                f"refusing to replace existing bundle: {path}"
            ) from error
    finally:
        if temporary.exists():
            temporary.unlink()
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def read_bundle(path: Path) -> dict[str, Any]:
    """Read and validate one previously exported bundle."""

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ProvenanceBundleError(f"cannot read provenance bundle: {path}") from error
    bundle = _object(payload, "bundle")
    validate_bundle(bundle)
    return bundle
