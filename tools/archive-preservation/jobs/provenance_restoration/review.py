"""Apply one explicit review to an immutable restoration draft."""

from __future__ import annotations

import copy
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

if __package__:
    from . import bundle as bundle_contract
    from .evidence import file_digest, file_fingerprint
    from .safety import require_safe_output
    from .technical import verify_image_derivative, verify_tiff_candidate
else:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import bundle as bundle_contract
    from evidence import file_digest, file_fingerprint
    from safety import require_safe_output
    from technical import verify_image_derivative, verify_tiff_candidate


REVIEW_FORMAT = "iss-provenance-restoration-review"
REVIEW_VERSION = 1
IDENTITY_OUTCOMES = {"same_entity", "component_of", "not_same", "defer"}
FILE_OUTCOMES = {
    "verified_local_master",
    "best_available_surrogate",
    "missing_master",
    "defer",
}


class ProvenanceReviewError(ValueError):
    """Raised when review evidence or decisions are incomplete."""


def _mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ProvenanceReviewError(f"{label} must be an object")
    return value


def _items(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise ProvenanceReviewError(f"{label} must be a list")
    return value


def _text(value: Any, label: str, *, required: bool = True) -> str:
    if not isinstance(value, str):
        raise ProvenanceReviewError(f"{label} must be text")
    if required and not value.strip():
        raise ProvenanceReviewError(f"{label} must not be empty")
    return value


def _keys(value: dict[str, Any], label: str, required: set[str]) -> None:
    missing = required - value.keys()
    unexpected = value.keys() - required
    if missing:
        raise ProvenanceReviewError(
            f"{label} is missing: {', '.join(sorted(missing))}"
        )
    if unexpected:
        raise ProvenanceReviewError(
            f"{label} has unexpected fields: {', '.join(sorted(unexpected))}"
        )


def _timestamp(value: Any, label: str) -> str:
    text = _text(value, label)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as error:
        raise ProvenanceReviewError(
            f"{label} must be an ISO-8601 timestamp"
        ) from error
    if parsed.tzinfo is None:
        raise ProvenanceReviewError(f"{label} must include a timezone")
    return text


def validate_review(
    decision: dict[str, Any],
    draft: dict[str, Any],
) -> None:
    """Validate one bounded review document against its exact draft."""

    decision = _mapping(decision, "review")
    _keys(
        decision,
        "review",
        {
            "format",
            "version",
            "case_id",
            "draft_bundle_digest",
            "reviewed_at",
            "identity",
            "metadata_decisions",
            "file_decision",
        },
    )
    if decision["format"] != REVIEW_FORMAT:
        raise ProvenanceReviewError("unsupported review format")
    if decision["version"] != REVIEW_VERSION:
        raise ProvenanceReviewError("unsupported review version")
    if decision["case_id"] != draft["case_id"]:
        raise ProvenanceReviewError("review case does not match draft")
    expected_digest = bundle_contract.bundle_digest(draft)
    if decision["draft_bundle_digest"] != expected_digest:
        raise ProvenanceReviewError("review draft digest does not match")
    _timestamp(decision["reviewed_at"], "review.reviewed_at")

    source_ids = {
        str(source["source_id"])
        for source in draft["sources"]
    }
    identity = _mapping(decision["identity"], "review.identity")
    _keys(
        identity,
        "review.identity",
        {
            "outcome",
            "reason",
            "actor",
            "decided_at",
            "evidence_source_ids",
        },
    )
    if identity["outcome"] not in IDENTITY_OUTCOMES:
        raise ProvenanceReviewError("unsupported identity outcome")
    if identity["outcome"] in {"same_entity", "component_of"}:
        roles = {str(file["role"]) for file in draft["files"]}
        has_local_image = bool(
            roles
            & {
                "local_master",
                "local_master_candidate",
                "local_derivative",
            }
        )
        if not has_local_image or "museum_digital_derivative" not in roles:
            raise ProvenanceReviewError(
                "positive identity requires captured local and MD image bytes"
            )
    _text(
        identity["reason"],
        "review.identity.reason",
        required=False,
    )
    _text(identity["actor"], "review.identity.actor")
    _timestamp(identity["decided_at"], "review.identity.decided_at")
    identity_evidence = [
        _text(value, "review identity evidence source")
        for value in _items(
            identity["evidence_source_ids"],
            "review.identity.evidence_source_ids",
        )
    ]
    if not identity_evidence:
        raise ProvenanceReviewError(
            "review identity requires at least one evidence source"
        )
    if len(identity_evidence) != len(set(identity_evidence)):
        raise ProvenanceReviewError(
            "review identity evidence contains duplicates"
        )
    unknown_sources = set(identity_evidence) - source_ids
    if unknown_sources:
        raise ProvenanceReviewError(
            "review identity references unknown source: "
            + ", ".join(sorted(unknown_sources))
        )

    fields: set[str] = set()
    for index, raw_metadata in enumerate(
        _items(
            decision["metadata_decisions"],
            "review.metadata_decisions",
        )
    ):
        metadata = _mapping(
            raw_metadata,
            f"review.metadata_decisions[{index}]",
        )
        _keys(
            metadata,
            f"review.metadata_decisions[{index}]",
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
        field = _text(
            metadata["field"],
            f"review.metadata_decisions[{index}].field",
        )
        if field in fields:
            raise ProvenanceReviewError(
                f"duplicate review metadata field: {field}"
            )
        fields.add(field)
        if metadata["action"] not in bundle_contract.METADATA_ACTIONS:
            raise ProvenanceReviewError(
                f"unsupported metadata action for {field}"
            )
        _text(
            metadata["value"],
            f"review.metadata_decisions[{index}].value",
            required=metadata["action"] != "defer",
        )
        source_id = _text(
            metadata["source_id"],
            f"review.metadata_decisions[{index}].source_id",
            required=False,
        )
        if source_id and source_id not in source_ids:
            raise ProvenanceReviewError(
                f"metadata field {field} references unknown source"
            )
        _text(
            metadata["source_locator"],
            f"review.metadata_decisions[{index}].source_locator",
            required=False,
        )
        _text(
            metadata["reason"],
            f"review.metadata_decisions[{index}].reason",
            required=False,
        )
        _text(
            metadata["actor"],
            f"review.metadata_decisions[{index}].actor",
        )
        _timestamp(
            metadata["decided_at"],
            f"review.metadata_decisions[{index}].decided_at",
        )

    file_decision = _mapping(
        decision["file_decision"],
        "review.file_decision",
    )
    _keys(
        file_decision,
        "review.file_decision",
        {
            "outcome",
            "selected_file_id",
            "reason",
            "actor",
            "decided_at",
        },
    )
    outcome = file_decision["outcome"]
    if outcome not in FILE_OUTCOMES:
        raise ProvenanceReviewError("unsupported file outcome")
    selected_file_id = _text(
        file_decision["selected_file_id"],
        "review.file_decision.selected_file_id",
        required=False,
    )
    _text(
        file_decision["reason"],
        "review.file_decision.reason",
        required=False,
    )
    _text(file_decision["actor"], "review.file_decision.actor")
    _timestamp(
        file_decision["decided_at"],
        "review.file_decision.decided_at",
    )
    file_roles = {
        str(file["file_id"]): str(file["role"])
        for file in draft["files"]
    }
    selected_role = file_roles.get(selected_file_id, "")
    if outcome == "verified_local_master":
        if selected_role != "local_master_candidate":
            raise ProvenanceReviewError(
                "master verification requires a selected local_master_candidate"
            )
    elif outcome == "best_available_surrogate":
        if selected_role not in {
            "local_derivative",
            "museum_digital_derivative",
        }:
            raise ProvenanceReviewError(
                "surrogate selection requires a selected derivative"
            )
    elif selected_file_id:
        raise ProvenanceReviewError(
            f"{outcome} review must not select a file"
        )


def _verify_catalog_snapshot(bundle: dict[str, Any]) -> dict[str, Any]:
    catalog_sources = [
        source
        for source in bundle["sources"]
        if source["kind"] == "local_catalog_row"
    ]
    if len(catalog_sources) != 1:
        raise ProvenanceReviewError(
            "review requires exactly one embedded local catalog snapshot"
        )
    snapshot = _mapping(
        catalog_sources[0].get("snapshot"),
        "local catalog snapshot",
    )
    expected = _mapping(
        snapshot.get("catalog_fingerprint"),
        "local catalog fingerprint",
    )
    path = Path(_text(expected.get("path"), "local catalog path"))
    try:
        actual = file_fingerprint(path)
    except OSError as error:
        raise ProvenanceReviewError(
            f"cannot revalidate retained catalog: {path}"
        ) from error
    if actual != expected:
        raise ProvenanceReviewError(
            "retained catalog fingerprint changed since extraction"
        )
    return actual


def _verify_local_files(
    bundle: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    fingerprints: dict[str, dict[str, Any]] = {}
    for file in bundle["files"]:
        source = next(
            (
                item
                for item in bundle["sources"]
                if item["source_id"] == file["source_id"]
            ),
            None,
        )
        if not source:
            raise ProvenanceReviewError(
                f"file references missing evidence source: {file['file_id']}"
            )
        if (
            source["kind"] != "local_filesystem"
            and file["role"] != "museum_digital_derivative"
        ):
            continue
        path = Path(str(file["locator"]))
        if not path.is_absolute():
            raise ProvenanceReviewError(
                f"local evidence path is not absolute: {path}"
            )
        if path.is_symlink() or not path.is_file():
            raise ProvenanceReviewError(
                f"local evidence is missing or is a symlink: {path}"
            )
        before = file_fingerprint(path)
        if before["size"] != file["size_bytes"]:
            raise ProvenanceReviewError(
                f"local evidence size changed: {path}"
            )
        if file_digest(path) != file["content_digest"]:
            raise ProvenanceReviewError(
                f"local evidence digest changed: {path}"
            )
        after = file_fingerprint(path)
        if after != before:
            raise ProvenanceReviewError(
                f"local evidence changed while verifying: {path}"
            )
        fingerprints[str(path)] = before
    return fingerprints


def verify_evidence(bundle: dict[str, Any]) -> dict[str, Any]:
    """Revalidate all structured snapshots and linked local source bytes."""

    bundle_contract.validate_bundle(bundle)
    return {
        "catalog": _verify_catalog_snapshot(bundle),
        "files": _verify_local_files(bundle),
    }


def _identity_from_review(decision: dict[str, Any]) -> dict[str, Any]:
    identity = copy.deepcopy(decision["identity"])
    outcome = identity["outcome"]
    identity["state"] = {
        "same_entity": "resolved",
        "component_of": "resolved",
        "not_same": "rejected",
        "defer": "deferred",
    }[outcome]
    return {
        "state": identity["state"],
        "outcome": outcome,
        "reason": identity["reason"],
        "actor": identity["actor"],
        "decided_at": identity["decided_at"],
        "evidence_source_ids": identity["evidence_source_ids"],
    }


def _apply_file_review(
    reviewed: dict[str, Any],
    decision: dict[str, Any],
    verification_method: str,
    verification_details: dict[str, Any],
) -> tuple[str, str, list[str]]:
    file_review = decision["file_decision"]
    outcome = str(file_review["outcome"])
    selected_file_id = str(file_review["selected_file_id"])
    blockers: list[str] = []
    state = "resolved"

    if outcome == "verified_local_master":
        selected = next(
            file
            for file in reviewed["files"]
            if file["file_id"] == selected_file_id
        )
        selected["role"] = "local_master"
        selected["note"] = (
            selected["note"].rstrip()
            + " Verified and promoted by provenance-restoration review."
        ).strip()
        if not verification_method:
            raise ProvenanceReviewError(
                "master promotion requires completed technical verification"
            )
    elif outcome == "best_available_surrogate":
        if not verification_method:
            raise ProvenanceReviewError(
                "surrogate selection requires completed fixity verification"
            )
        reviewed["readiness"]["provenance_gaps"].append(
            "missing_local_master"
        )
    elif outcome == "missing_master":
        reviewed["readiness"]["provenance_gaps"].append(
            "missing_local_master"
        )
        blockers.append("missing_master_no_surrogate")
    else:
        state = "deferred"
        blockers.append("file_review_deferred")

    reviewed["file_decision"] = {
        "state": state,
        "outcome": outcome,
        "selected_file_id": selected_file_id,
        "verification_method": verification_method,
        "verification_details": (
            copy.deepcopy(verification_details)
            if selected_file_id
            else {}
        ),
        "reason": file_review["reason"],
        "actor": file_review["actor"],
        "decided_at": file_review["decided_at"],
    }
    file_state = "unresolved" if outcome == "defer" else outcome
    return file_state, selected_file_id, blockers


def build_reviewed_bundle(
    draft: dict[str, Any],
    decision: dict[str, Any],
    verification_method: str = "",
    verification_details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a reviewed bundle after callers have revalidated source bytes."""

    bundle_contract.validate_bundle(draft)
    if draft["lineage"]["state"] != "draft":
        raise ProvenanceReviewError("review input must be an immutable draft")
    validate_review(decision, draft)

    reviewed = copy.deepcopy(draft)
    draft_digest = bundle_contract.bundle_digest(draft)
    reviewed["created_at"] = decision["reviewed_at"]
    reviewed["lineage"] = {
        "state": "reviewed",
        "draft_bundle_digest": draft_digest,
        "decision_digest": bundle_contract.snapshot_digest(decision),
        "decision_snapshot": copy.deepcopy(decision),
    }
    reviewed["identity"] = _identity_from_review(decision)
    reviewed["metadata_decisions"] = copy.deepcopy(
        decision["metadata_decisions"]
    )

    file_state, selected_file_id, new_blockers = _apply_file_review(
        reviewed,
        decision,
        verification_method,
        verification_details or {},
    )
    blockers = list(reviewed["readiness"]["blocking_gaps"])
    blockers.extend(new_blockers)
    identity_outcome = reviewed["identity"]["outcome"]
    if identity_outcome == "not_same":
        blockers.append("identity_not_same")
    elif identity_outcome == "defer":
        blockers.append("identity_review_deferred")
    if not reviewed["metadata_decisions"]:
        blockers.append("metadata_not_reviewed")
    for metadata in reviewed["metadata_decisions"]:
        if metadata["action"] == "defer":
            blockers.append(f"metadata_deferred:{metadata['field']}")

    blockers = sorted(set(blockers))
    gaps = sorted(set(reviewed["readiness"]["provenance_gaps"]))
    ready = (
        reviewed["identity"]["state"] == "resolved"
        and file_state in {
            "verified_local_master",
            "best_available_surrogate",
        }
        and bool(reviewed["metadata_decisions"])
        and not blockers
    )
    reviewed["readiness"] = {
        "state": "ready_for_ingest" if ready else "blocked",
        "file_state": file_state,
        "selected_file_id": selected_file_id,
        "provenance_gaps": gaps,
        "blocking_gaps": blockers,
    }
    bundle_contract.validate_bundle(reviewed)
    return reviewed


def export_reviewed_bundle(
    draft_path: Path,
    output_path: Path,
    decision: dict[str, Any],
    technical_verifier: Callable[[Path], dict[str, Any]] = verify_tiff_candidate,
    derivative_verifier: Callable[
        [Path],
        dict[str, Any],
    ] = verify_image_derivative,
) -> tuple[dict[str, Any], str]:
    """Revalidate evidence and create one new reviewed bundle."""

    output_path = require_safe_output(output_path)
    draft_path = draft_path.expanduser().resolve()
    draft_before = file_fingerprint(draft_path)
    draft = bundle_contract.read_bundle(draft_path)
    validate_review(decision, draft)

    evidence_before = verify_evidence(draft)
    selected_file_id = str(
        decision["file_decision"]["selected_file_id"]
    )
    verification_method = ""
    verification_details: dict[str, Any] = {}
    if selected_file_id:
        selected_file = next(
            file
            for file in draft["files"]
            if file["file_id"] == selected_file_id
        )
        verification_details = {
            "content_digest": next(
                file["content_digest"]
                for file in draft["files"]
                if file["file_id"] == selected_file_id
            ),
            "size_bytes": next(
                file["size_bytes"]
                for file in draft["files"]
                if file["file_id"] == selected_file_id
            ),
        }
        if decision["file_decision"]["outcome"] == "verified_local_master":
            verification_details["technical"] = technical_verifier(
                Path(str(selected_file["locator"]))
            )
            verification_method = (
                "sha256_stat_exiftool_imagemagick_reverification"
            )
        else:
            verification_details["technical"] = derivative_verifier(
                Path(str(selected_file["locator"]))
            )
            verification_method = (
                "sha256_stat_imagemagick_reverification"
            )
    reviewed = build_reviewed_bundle(
        draft,
        decision,
        verification_method,
        verification_details,
    )
    output_digest = bundle_contract.write_bundle(output_path, reviewed)
    written = bundle_contract.read_bundle(output_path)
    if written != reviewed:
        raise ProvenanceReviewError(
            "reviewed bundle read-back did not match export"
        )

    if file_fingerprint(draft_path) != draft_before:
        raise ProvenanceReviewError("draft changed during review export")
    evidence_after = {
        "catalog": _verify_catalog_snapshot(draft),
        "files": {
            path: file_fingerprint(Path(path))
            for path in evidence_before["files"]
        },
    }
    if evidence_after != evidence_before:
        raise ProvenanceReviewError(
            "protected evidence changed during review export"
        )
    return reviewed, output_digest
