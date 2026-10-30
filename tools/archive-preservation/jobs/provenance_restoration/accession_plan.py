#!/usr/bin/env python3
"""Create one deterministic, non-writing accession plan from reviewed evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import uuid
from pathlib import Path
from typing import Any

if __package__:
    from . import bundle as bundle_contract
    from .evidence import file_digest
    from .review import ProvenanceReviewError, verify_evidence
    from .safety import ProtectedEvidenceError, require_safe_output
else:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import bundle as bundle_contract
    from evidence import file_digest
    from review import ProvenanceReviewError, verify_evidence
    from safety import ProtectedEvidenceError, require_safe_output


PLAN_FORMAT = "iss-provenance-restoration-accession-plan"
PLAN_VERSION = 4
PLAN_MODE = "dry_run"
PATH_TEMPLATE = (
    "units/{unit_id}/data/masters/"
    "{inventory_label}__{entity_id}__master.tif"
)
SURROGATE_PATH_TEMPLATE = (
    "units/{unit_id}/data/masters/"
    "{inventory_label}__{entity_id}__surrogate.tif"
)
SURROGATE_SOURCE_PATH_TEMPLATE = (
    "units/{unit_id}/data/masters/"
    "{inventory_label}__{entity_id}__source{source_suffix}"
)
CORE_METADATA_DESTINATIONS = {
    "descriptive_title": "archive_objects.descriptive_title",
    "description": "archive_objects.description",
    "material_technique": "archive_objects.material_technique",
    "dimensions": "archive_objects.dimensions",
}
TIFF_REQUIRED_FIELDS = (
    "descriptive_title",
    "description",
    "material_technique",
    "dimensions",
)


class AccessionPlanError(ValueError):
    """Raised when exact reviewed lineage cannot produce a safe dry-run plan."""


def canonical_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def plan_digest(plan: dict[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(canonical_bytes(plan)).hexdigest()


def _exact_source(
    bundle: dict[str, Any],
    *,
    kind: str,
) -> dict[str, Any]:
    matches = [source for source in bundle["sources"] if source["kind"] == kind]
    if len(matches) != 1:
        raise AccessionPlanError(
            f"reviewed bundle requires exactly one {kind} source"
        )
    return matches[0]


def _md_external_id(bundle: dict[str, Any]) -> str:
    source = _exact_source(bundle, kind="museum_digital_snapshot")
    objects = source.get("snapshot", {}).get("objects", [])
    if len(objects) != 1:
        raise AccessionPlanError(
            "museum-digital evidence must contain exactly one object row"
        )
    external_id = str(objects[0].get("md_id") or "").strip()
    if not external_id:
        raise AccessionPlanError(
            "museum-digital evidence does not contain an object ID"
        )
    return external_id


def _validate_lineage(
    parent: dict[str, Any],
    capture: dict[str, Any],
    reviewed: dict[str, Any],
) -> None:
    if parent["case_id"] != capture["case_id"] or capture["case_id"] != reviewed["case_id"]:
        raise AccessionPlanError("parent, capture, and review cases differ")
    if parent["lineage"]["state"] != "draft":
        raise AccessionPlanError("parent bundle must remain a draft")
    derivation = capture.get("derivation", {})
    if derivation.get("kind") != "md_derivative_capture":
        raise AccessionPlanError(
            "captured draft must record one MD derivative capture"
        )
    if derivation.get("parent_bundle_digest") != bundle_contract.bundle_digest(parent):
        raise AccessionPlanError("captured draft parent digest differs")
    if reviewed["lineage"]["state"] != "reviewed":
        raise AccessionPlanError("accession planning requires a reviewed bundle")
    if reviewed["lineage"]["draft_bundle_digest"] != bundle_contract.bundle_digest(capture):
        raise AccessionPlanError("reviewed bundle capture digest differs")
    if reviewed.get("derivation") != derivation:
        raise AccessionPlanError(
            "reviewed bundle does not preserve the exact capture derivation"
        )
    if reviewed["readiness"]["state"] != "ready_for_ingest":
        raise AccessionPlanError("reviewed bundle is not ready for ingest planning")
    if reviewed["identity"]["state"] != "resolved":
        raise AccessionPlanError("reviewed identity is not resolved")
    if reviewed["file_decision"]["state"] != "resolved":
        raise AccessionPlanError("reviewed file decision is not resolved")


def _selected_file(reviewed: dict[str, Any]) -> dict[str, Any]:
    selected_id = reviewed["readiness"]["selected_file_id"]
    if selected_id != reviewed["file_decision"]["selected_file_id"]:
        raise AccessionPlanError("readiness and file decision select different files")
    matches = [
        file for file in reviewed["files"] if file["file_id"] == selected_id
    ]
    if len(matches) != 1:
        raise AccessionPlanError("selected ingest file is unavailable or ambiguous")
    selected = matches[0]
    outcome = reviewed["file_decision"]["outcome"]
    expected_role = {
        "verified_local_master": "local_master",
        "best_available_surrogate": (
            "local_derivative",
            "museum_digital_derivative",
        ),
    }.get(outcome)
    if expected_role is None:
        raise AccessionPlanError("reviewed file outcome is not accessionable")
    roles = (
        expected_role
        if isinstance(expected_role, tuple)
        else (expected_role,)
    )
    if selected["role"] not in roles:
        raise AccessionPlanError("selected file role differs from its decision")
    verification = reviewed["file_decision"]["verification_details"]
    if outcome == "verified_local_master" and (
        verification.get("content_digest") != selected["content_digest"]
        or verification.get("size_bytes") != selected["size_bytes"]
    ):
        raise AccessionPlanError(
            "selected master differs from its technical verification"
        )
    return selected


def _metadata_plan(
    reviewed: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    source_digests = {
        source["source_id"]: source["snapshot_digest"]
        for source in reviewed["sources"]
    }
    accepted: list[dict[str, Any]] = []
    gates: list[dict[str, str]] = []
    values: dict[str, str] = {}
    for decision in reviewed["metadata_decisions"]:
        if decision["action"] == "defer":
            continue
        field = str(decision["field"])
        destination = CORE_METADATA_DESTINATIONS.get(
            field,
            "provenance_bundle.metadata_decisions",
        )
        mapped = field in CORE_METADATA_DESTINATIONS
        accepted.append(
            {
                "field": field,
                "value": decision["value"],
                "action": decision["action"],
                "source_id": decision["source_id"],
                "source_locator": decision["source_locator"],
                "source_snapshot_digest": source_digests.get(
                    decision["source_id"],
                    "",
                ),
                "actor": decision["actor"],
                "decided_at": decision["decided_at"],
                "comment": decision["reason"],
                "proposed_destination": destination,
                "mapping_state": (
                    "mapped_to_core"
                    if mapped
                    else "preserved_in_provenance"
                ),
            }
        )
        values[field] = str(decision["value"])
        if not mapped:
            gates.append(
                {
                    "key": f"metadata_mapping:{field}",
                    "status": "implementation_note",
                    "detail": (
                        f"Preserve accepted field {field!r} in provenance "
                        "until a canonical V2 destination exists; do not "
                        "discard it or block this case."
                    ),
                }
            )
    for field in TIFF_REQUIRED_FIELDS:
        if not values.get(field, "").strip():
            gates.append(
                {
                    "key": f"optional_metadata:{field}",
                    "status": "profile_change_required",
                    "detail": (
                        f"{field} was not accepted in this review. Update the "
                        "current iss-tiff-v1 implementation to omit this "
                        "optional field rather than inventing a value."
                    ),
                }
            )
    return accepted, gates


def build_accession_plan(
    parent_path: Path,
    capture_path: Path,
    reviewed_path: Path,
) -> dict[str, Any]:
    """Revalidate exact lineage and return a deterministic non-writing plan."""

    parent_path = parent_path.expanduser().resolve()
    capture_path = capture_path.expanduser().resolve()
    reviewed_path = reviewed_path.expanduser().resolve()
    parent = bundle_contract.read_bundle(parent_path)
    capture = bundle_contract.read_bundle(capture_path)
    reviewed = bundle_contract.read_bundle(reviewed_path)
    _validate_lineage(parent, capture, reviewed)
    evidence = verify_evidence(reviewed)
    selected = _selected_file(reviewed)
    metadata, gates = _metadata_plan(reviewed)
    is_surrogate = (
        reviewed["file_decision"]["outcome"]
        == "best_available_surrogate"
    )
    if is_surrogate:
        if "missing_local_master" not in reviewed[
            "readiness"
        ]["provenance_gaps"]:
            raise AccessionPlanError(
                "surrogate plan must retain missing_local_master"
            )
        gates = [
            gate
            for gate in gates
            if gate["status"] != "profile_change_required"
        ]
    gates.extend(
        [
            {
                "key": "target:unit_id",
                "status": "assign_during_ingest",
                "detail": (
                    "Choose or create the permanent archive unit in the "
                    "guarded ingest command."
                ),
            },
            {
                "key": "target:entity_id",
                "status": "generate_during_ingest",
                "detail": (
                    "Generate the immutable ISS entity ID transactionally "
                    "during guarded ingest."
                ),
            },
            {
                "key": "target:assignment_year",
                "status": "derive_from_ingest_time",
                "detail": (
                    "Use the guarded-ingest year for ID assignment; do not "
                    "infer it from descriptive object text."
                ),
            },
        ]
    )
    sources = [
        {
            "source_id": source["source_id"],
            "kind": source["kind"],
            "locator": source["locator"],
            "captured_at": source["captured_at"],
            "snapshot_digest": source["snapshot_digest"],
        }
        for source in reviewed["sources"]
    ]
    inventory = str(reviewed["inventory_label"])
    output_name = f"{inventory}.tif"
    if is_surrogate:
        source_suffix = Path(str(selected["locator"])).suffix.casefold()
        if source_suffix not in {".jpg", ".jpeg", ".png", ".webp"}:
            raise AccessionPlanError(
                "selected surrogate extension is not supported"
            )
        proposed_archive = {
            "target_unit_id": "{unit_id}",
            "target_entity_id": "{entity_id}",
            "output_filename": output_name,
            "selected_source_filename": (
                f"{inventory}-selected-source{source_suffix}"
            ),
            "primary_relative_path_template": (
                SURROGATE_PATH_TEMPLATE
            ),
            "independent_relative_path_template": (
                SURROGATE_PATH_TEMPLATE
            ),
            "selected_source_relative_path_template": (
                SURROGATE_SOURCE_PATH_TEMPLATE
            ),
            "source_copy_operation": (
                "retain exact selected derivative bytes; never relabel as "
                "an original master"
            ),
            "representation_operation": (
                "create pixel-equivalent metadata-enriched TIFF surrogate"
            ),
            "embedding_profile": "iss-tiff-v2",
        }
        accession_operation = {
            "sequence": 6,
            "operation": (
                "surrogate_accession.create_preservation_surrogate"
            ),
            "state": (
                "proposed_with_missing_local_master_gap_preserved"
            ),
        }
    else:
        proposed_archive = {
            "target_unit_id": "{unit_id}",
            "target_entity_id": "{entity_id}",
            "output_filename": output_name,
            "primary_relative_path_template": PATH_TEMPLATE,
            "independent_relative_path_template": PATH_TEMPLATE,
            "master_operation": (
                "create metadata-enriched copy; preserve selected source bytes"
            ),
            "embedding_profile": "iss-tiff-v1",
        }
        accession_operation = {
            "sequence": 6,
            "operation": "tiff_accession.create_preservation_master",
            "state": "proposed_after_optional_field_support",
        }
    reviewed_digest = bundle_contract.bundle_digest(reviewed)
    plan = {
        "format": PLAN_FORMAT,
        "version": PLAN_VERSION,
        "mode": PLAN_MODE,
        "direction": bundle_contract.BUNDLE_DIRECTION,
        "case_id": reviewed["case_id"],
        "inventory_label": inventory,
        "authorization": {
            "archive_writes": False,
            "catalog_writes": False,
            "source_writes": False,
            "repeat_curatorial_review": False,
            "execution_requires_guarded_command": True,
        },
        "bindings": {
            "parent_draft": {
                "path": str(parent_path),
                "bundle_digest": bundle_contract.bundle_digest(parent),
                "file_digest": file_digest(parent_path),
            },
            "captured_draft": {
                "path": str(capture_path),
                "bundle_digest": bundle_contract.bundle_digest(capture),
                "file_digest": file_digest(capture_path),
            },
            "reviewed_bundle": {
                "path": str(reviewed_path),
                "bundle_digest": reviewed_digest,
                "file_digest": file_digest(reviewed_path),
            },
            "capture_operation_digest": capture["derivation"][
                "operation_digest"
            ],
            "review_decision_digest": reviewed["lineage"][
                "decision_digest"
            ],
        },
        "evidence_revalidation": {
            "state": "passed",
            "catalog_fingerprint": evidence["catalog"],
            "file_fingerprints": evidence["files"],
        },
        "identity": {
            "outcome": reviewed["identity"]["outcome"],
            "actor": reviewed["identity"]["actor"],
            "decided_at": reviewed["identity"]["decided_at"],
            "comment": reviewed["identity"]["reason"],
            "evidence_source_ids": reviewed["identity"][
                "evidence_source_ids"
            ],
            "museum_digital_id": _md_external_id(reviewed),
        },
        "canonical_metadata": metadata,
        "selected_source_file": {
            **selected,
            "decision_outcome": reviewed["file_decision"]["outcome"],
            "verification_method": reviewed["file_decision"][
                "verification_method"
            ],
            "verification_details": reviewed["file_decision"][
                "verification_details"
            ],
        },
        "source_evidence": sources,
        "proposed_archive": proposed_archive,
        "proposed_catalog_operations": [
            {
                "sequence": 1,
                "operation": "catalog_core.create_unit",
                "state": "proposed_if_needed",
            },
            {
                "sequence": 2,
                "operation": "catalog_core.create_entity",
                "state": "proposed",
            },
            {
                "sequence": 3,
                "operation": "catalog_core.add_alias_identifier",
                "state": "proposed",
                "value": inventory,
            },
            {
                "sequence": 4,
                "operation": "catalog_core.review_external_identity",
                "state": "proposed",
                "external_system": "museum-digital",
                "external_id": _md_external_id(reviewed),
            },
            {
                "sequence": 5,
                "operation": "catalog_core.accept_reconciliation_text",
                "state": "proposed_for_each_mapped_field",
            },
            accession_operation,
            {
                "sequence": 7,
                "operation": "catalog_core.publish_generation",
                "state": "proposed_after_independent_copy_verification",
            },
        ],
        "implementation_requirements": gates,
        "plan_state": "ready_for_implementation",
        "execution_state": "not_authorized_by_dry_run",
    }
    if is_surrogate:
        plan["preserved_provenance_gaps"] = list(
            reviewed["readiness"]["provenance_gaps"]
        )
    validate_plan(plan)
    return plan


def validate_plan(plan: dict[str, Any]) -> None:
    """Validate the small fail-closed surface consumed by the workbench."""

    if not isinstance(plan, dict):
        raise AccessionPlanError("accession plan must be an object")
    if plan.get("format") != PLAN_FORMAT or plan.get("version") != PLAN_VERSION:
        raise AccessionPlanError("unsupported accession plan format")
    if plan.get("mode") != PLAN_MODE:
        raise AccessionPlanError("accession plan must remain dry-run only")
    if plan.get("direction") != bundle_contract.BUNDLE_DIRECTION:
        raise AccessionPlanError("accession plan direction is not sources_to_archive")
    authorization = plan.get("authorization")
    if not isinstance(authorization, dict) or any(
        authorization.get(key) is not False
        for key in ("archive_writes", "catalog_writes", "source_writes")
    ):
        raise AccessionPlanError("accession plan must not authorize writes")
    if authorization.get("repeat_curatorial_review") is not False:
        raise AccessionPlanError(
            "accession plan must not repeat the completed curatorial review"
        )
    if authorization.get("execution_requires_guarded_command") is not True:
        raise AccessionPlanError(
            "accession plan must require a separate guarded write command"
        )
    if plan.get("plan_state") != "ready_for_implementation":
        raise AccessionPlanError(
            "accession plan must remain ready_for_implementation"
        )
    if plan.get("execution_state") != "not_authorized_by_dry_run":
        raise AccessionPlanError("invalid accession execution state")
    bindings = plan.get("bindings")
    if not isinstance(bindings, dict):
        raise AccessionPlanError("accession plan bindings are missing")
    for name in ("parent_draft", "captured_draft", "reviewed_bundle"):
        binding = bindings.get(name)
        if not isinstance(binding, dict):
            raise AccessionPlanError(f"accession plan binding is missing: {name}")
        for key in ("bundle_digest", "file_digest"):
            value = binding.get(key)
            if (
                not isinstance(value, str)
                or not value.startswith("sha256:")
                or len(value) != 71
            ):
                raise AccessionPlanError(
                    f"accession plan {name} {key} is invalid"
                )
    selected = plan.get("selected_source_file")
    if not isinstance(selected, dict) or not selected.get("content_digest"):
        raise AccessionPlanError("accession plan selected file is missing")
    proposed = plan.get("proposed_archive")
    selected_outcome = selected.get("decision_outcome")
    expected_template = (
        SURROGATE_PATH_TEMPLATE
        if selected_outcome == "best_available_surrogate"
        else PATH_TEMPLATE
    )
    if (
        not isinstance(proposed, dict)
        or proposed.get("primary_relative_path_template")
        != expected_template
        or proposed.get("independent_relative_path_template")
        != expected_template
    ):
        raise AccessionPlanError("accession plan archive path template differs")
    if selected_outcome == "best_available_surrogate":
        gaps = plan.get("preserved_provenance_gaps")
        if (
            not isinstance(gaps, list)
            or "missing_local_master" not in gaps
            or proposed.get("selected_source_relative_path_template")
            != SURROGATE_SOURCE_PATH_TEMPLATE
        ):
            raise AccessionPlanError(
                "surrogate plan does not preserve its source and provenance gap"
            )
    requirements = plan.get("implementation_requirements")
    if not isinstance(requirements, list):
        raise AccessionPlanError(
            "accession plan implementation requirements must be a list"
        )


def read_accession_plan(path: Path) -> dict[str, Any]:
    try:
        plan = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise AccessionPlanError(f"cannot read accession plan: {path}") from error
    validate_plan(plan)
    return plan


def write_accession_plan(
    parent_path: Path,
    capture_path: Path,
    reviewed_path: Path,
    output_path: Path,
) -> tuple[dict[str, Any], str]:
    output_path = require_safe_output(output_path)
    if output_path.exists():
        raise AccessionPlanError(
            f"refusing to replace accession plan: {output_path}"
        )
    plan = build_accession_plan(parent_path, capture_path, reviewed_path)
    payload = canonical_bytes(plan)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_name(
        f".{output_path.name}.{uuid.uuid4().hex}.tmp"
    )
    try:
        with temporary.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, output_path)
    finally:
        temporary.unlink(missing_ok=True)
    written = read_accession_plan(output_path)
    if written != plan:
        raise AccessionPlanError("written accession plan differs on read-back")
    return plan, plan_digest(plan)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--parent-draft", type=Path, required=True)
    parser.add_argument("--captured-draft", type=Path, required=True)
    parser.add_argument("--reviewed", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        plan, digest = write_accession_plan(
            arguments.parent_draft,
            arguments.captured_draft,
            arguments.reviewed,
            arguments.output,
        )
    except (
        AccessionPlanError,
        ProtectedEvidenceError,
        ProvenanceReviewError,
        bundle_contract.ProvenanceBundleError,
        OSError,
    ) as error:
        print(f"error: {error}", file=sys.stderr)
        return 3
    print(
        json.dumps(
            {
                "case_id": plan["case_id"],
                "execution_state": plan["execution_state"],
                "implementation_requirements": len(
                    plan["implementation_requirements"]
                ),
                "output": str(arguments.output.expanduser().resolve()),
                "plan_digest": digest,
            },
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
