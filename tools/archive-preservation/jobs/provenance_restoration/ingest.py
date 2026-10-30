#!/usr/bin/env python3
"""Guard one reviewed provenance case into a fresh writable archive pilot."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import uuid
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

TOOL_ROOT = Path(__file__).resolve().parents[2]
if str(TOOL_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOL_ROOT))

import catalog_core
import tiff_accession
import tiff_metadata

if __package__:
    from . import accession_plan as plan_contract
    from . import surrogate_accession
    from .safety import ProtectedEvidenceError, require_safe_output
else:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import accession_plan as plan_contract
    import surrogate_accession
    from safety import ProtectedEvidenceError, require_safe_output


INGEST_FORMAT = "iss-provenance-restoration-ingest-report"
INGEST_VERSION = 1
TOOL_VERSION = "provenance-restoration-ingest-0.1.0"
UNIT_RE = re.compile(r"^[A-Z0-9][A-Z0-9._-]{2,127}$")
FIELD_PREDICATES = {
    "descriptive_title": "archive_descriptive_title",
    "description": "archive_description",
    "material_technique": "archive_material_technique",
    "dimensions": "archive_dimensions",
}


class ProvenanceIngestError(RuntimeError):
    """Raised when a plan cannot enter a fresh writable pilot safely."""


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace(
        "+00:00",
        "Z",
    )


def _safe_new_root(path: Path, label: str) -> Path:
    resolved = require_safe_output(path / ".write-boundary")
    root = resolved.parent
    catalog_core.assert_safe_work_root(root)
    if root.exists():
        raise ProvenanceIngestError(
            f"refusing to replace existing {label}: {root}"
        )
    return root


def _unit_id(plan: dict[str, Any], requested: str = "") -> str:
    value = requested.strip().upper()
    if not value:
        value = f"RESTORE-{plan['inventory_label']}".upper()
    value = re.sub(r"[^A-Z0-9._-]+", "-", value).strip("-")
    if not UNIT_RE.fullmatch(value):
        raise ProvenanceIngestError(
            "unit ID must contain 3-128 uppercase letters, numbers, dots, "
            "underscores, or hyphens"
        )
    return value


def _canonical_values(plan: dict[str, Any]) -> dict[str, str]:
    return {
        str(item["field"]): str(item["value"])
        for item in plan["canonical_metadata"]
    }


def _md_source(plan: dict[str, Any]) -> dict[str, Any]:
    matches = [
        source
        for source in plan["source_evidence"]
        if source["kind"] == "museum_digital_snapshot"
    ]
    if len(matches) != 1:
        raise ProvenanceIngestError(
            "ingest requires exactly one museum-digital snapshot"
        )
    return matches[0]


def revalidate_plan(path: Path) -> dict[str, Any]:
    """Rebuild the exact plan from its bindings and compare every field."""

    path = path.expanduser().resolve()
    plan = plan_contract.read_accession_plan(path)
    bindings = plan["bindings"]
    rebuilt = plan_contract.build_accession_plan(
        Path(bindings["parent_draft"]["path"]),
        Path(bindings["captured_draft"]["path"]),
        Path(bindings["reviewed_bundle"]["path"]),
    )
    if rebuilt != plan:
        raise ProvenanceIngestError(
            "accession plan differs from current exact evidence"
        )
    selected = plan["selected_source_file"]
    outcome = selected["decision_outcome"]
    verified_master = (
        outcome == "verified_local_master"
        and selected["role"] == "local_master"
        and selected["media_type"] == "image/tiff"
    )
    verified_surrogate = (
        outcome == "best_available_surrogate"
        and selected["role"]
        in {"local_derivative", "museum_digital_derivative"}
        and selected["media_type"]
        in {"image/jpeg", "image/png", "image/webp"}
        and "missing_local_master"
        in plan.get("preserved_provenance_gaps", [])
    )
    if not (verified_master or verified_surrogate):
        raise ProvenanceIngestError(
            "bounded ingest requires either one verified local TIFF master or "
            "one explicit best-available image surrogate retaining "
            "missing_local_master"
        )
    return plan


def dry_run(
    plan_path: Path,
    root: Path,
    independent_root: Path,
    *,
    unit_id: str = "",
) -> dict[str, Any]:
    plan = revalidate_plan(plan_path)
    root = _safe_new_root(root, "archive pilot root")
    independent_root = _safe_new_root(
        independent_root,
        "independent pilot root",
    )
    selected = plan["selected_source_file"]
    operations = [
        "initialize fresh minimal-core V2 pilot",
        "create one provenance-restoration unit and archive object",
        "record inventory alias, external identity, and mapped metadata",
        "publish current catalog generation",
    ]
    if selected["decision_outcome"] == "best_available_surrogate":
        operations.extend(
            [
                "retain an exact copy of the selected derivative",
                "create and verify a pixel-equivalent iss-tiff-v2 surrogate",
                "catalog both files without claiming an original master",
            ]
        )
    else:
        operations.append(
            "create and verify metadata-enriched iss-tiff-v2 master"
        )
    operations.extend(
        [
            "create and verify independent representation copies",
            "publish final complete filesystem generation",
        ]
    )
    return {
        "result": "dry_run",
        "case_id": plan["case_id"],
        "plan_digest": plan_contract.plan_digest(plan),
        "root": str(root),
        "independent_root": str(independent_root),
        "unit_id": _unit_id(plan, unit_id),
        "assignment_year": datetime.now(UTC).year,
        "source": selected["locator"],
        "source_digest": selected["content_digest"],
        "file_decision": selected["decision_outcome"],
        "preserved_provenance_gaps": plan.get(
            "preserved_provenance_gaps",
            [],
        ),
        "embedding_profile": tiff_metadata.PROFILE_V2,
        "writes_authorized": False,
        "operations": operations,
    }


def _decision_id(
    plan: dict[str, Any],
    kind: str,
    field: str = "",
) -> str:
    digest = plan["bindings"]["review_decision_digest"].split(":", 1)[1]
    suffix = f"-{field.upper()}" if field else ""
    return f"RESTORE-{kind}-{digest[:16].upper()}{suffix}"


def _rebase_paths(
    value: Any,
    replacements: tuple[tuple[str, str], ...],
) -> Any:
    if isinstance(value, dict):
        return {
            key: _rebase_paths(item, replacements)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_rebase_paths(item, replacements) for item in value]
    if isinstance(value, str):
        for source, destination in replacements:
            if value == source or value.startswith(source + os.sep):
                return destination + value[len(source):]
    return value


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    payload = (
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2)
        + "\n"
    )
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def execute_ingest(
    plan_path: Path,
    root: Path,
    independent_root: Path,
    *,
    actor: str,
    unit_id: str = "",
) -> dict[str, Any]:
    """Execute one plan in fresh staging roots and atomically promote them."""

    actor = actor.strip()
    if not actor:
        raise ProvenanceIngestError("named ingest actor is required")
    plan = revalidate_plan(plan_path)
    final_root = _safe_new_root(root, "archive pilot root")
    final_independent = _safe_new_root(
        independent_root,
        "independent pilot root",
    )
    final_root.parent.mkdir(parents=True, exist_ok=True)
    final_independent.parent.mkdir(parents=True, exist_ok=True)
    operation_id = (
        "INGEST-"
        + datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        + "-"
        + uuid.uuid4().hex[:8].upper()
    )
    staging_root = final_root.with_name(
        f".{final_root.name}.{operation_id}.staging"
    )
    staging_independent = final_independent.with_name(
        f".{final_independent.name}.{operation_id}.staging"
    )
    unit = _unit_id(plan, unit_id)
    assignment_year = datetime.now(UTC).year
    plan_digest = plan_contract.plan_digest(plan)
    values = _canonical_values(plan)
    selected = plan["selected_source_file"]
    file_decision = selected["decision_outcome"]
    md = _md_source(plan)
    failure_path = staging_root / "work" / f"{operation_id}.failed.json"
    try:
        catalog_core.initialize_root(
            staging_root,
            tool_version=TOOL_VERSION,
            actor=actor,
        )
        with closing(
            catalog_core.connect_catalog(
                staging_root / "work" / "catalog.sqlite"
            )
        ) as catalog:
            catalog_core.create_unit(
                catalog,
                unit_id=unit,
                title=f"Provenance restoration {plan['inventory_label']}",
                kind="provenance_restoration",
                actor=actor,
                note=f"Bound to {plan_digest}",
            )
            entity = catalog_core.create_entity(
                catalog,
                kind="archive_object",
                canonical_name=(
                    values.get("descriptive_title")
                    or plan["inventory_label"]
                ),
                unit_id=unit,
                membership_role="primary",
                actor=actor,
                assignment_year=assignment_year,
                domain={
                    "archival_label": plan["inventory_label"],
                    "descriptive_title": values.get(
                        "descriptive_title",
                        "",
                    ),
                    "description": values.get("description", ""),
                    "material_technique": values.get(
                        "material_technique",
                        "",
                    ),
                    "dimensions": values.get("dimensions", ""),
                    "publication_status": "unknown",
                    "note": f"Restored from {plan['case_id']}",
                },
                note=f"Bound to reviewed plan {plan_digest}",
            )
            entity_id = entity["entity_id"]
            catalog_core.add_alias_identifier(
                catalog,
                entity_id=entity_id,
                namespace="inventory",
                value=plan["inventory_label"],
                normalized_value=plan["inventory_label"].upper(),
                role="legacy",
                actor=actor,
                note=f"Restored from {plan['case_id']}",
            )
            catalog_core.review_external_identity(
                catalog,
                decision_id=_decision_id(plan, "IDENTITY"),
                entity_id=entity_id,
                external_system="museum-digital",
                external_id=plan["identity"]["museum_digital_id"],
                outcome=plan["identity"]["outcome"],
                actor=actor,
                reason=plan["identity"]["comment"],
                captured_at=md["captured_at"],
                snapshot_digest=md["snapshot_digest"],
                citation=md["locator"],
            )
            for item in plan["canonical_metadata"]:
                field = str(item["field"])
                predicate = FIELD_PREDICATES.get(field)
                if not predicate:
                    continue
                action = {
                    "accept_museum_digital": "accept_external",
                    "accept_local": "accept_local",
                    "corrected": "corrected",
                }.get(str(item["action"]))
                if not action:
                    raise ProvenanceIngestError(
                        f"unsupported accepted metadata action: {item['action']}"
                    )
                source = next(
                    (
                        evidence
                        for evidence in plan["source_evidence"]
                        if evidence["source_id"] == item["source_id"]
                    ),
                    None,
                )
                source = source or {
                    "kind": "reviewed_correction",
                    "captured_at": item["decided_at"],
                    "snapshot_digest": plan["bindings"][
                        "review_decision_digest"
                    ],
                    "locator": plan["bindings"]["reviewed_bundle"]["path"],
                }
                catalog_core.accept_reconciliation_text(
                    catalog,
                    decision_id=_decision_id(plan, "FIELD", field),
                    entity_id=entity_id,
                    predicate=predicate,
                    action=action,
                    value_text=str(item["value"]),
                    actor=actor,
                    reason=str(item["comment"]),
                    external_system="museum-digital",
                    external_id=plan["identity"]["museum_digital_id"],
                    local_search_status="exact_inventory",
                    source_type=str(source["kind"]),
                    captured_at=str(source["captured_at"]),
                    snapshot_digest=str(source["snapshot_digest"]),
                    citation=str(source["locator"]),
                )
        catalog_core.publish_generation(
            staging_root,
            staging_independent,
            tool_version=TOOL_VERSION,
            actor=actor,
        )
        provenance_metadata = {
            "case_id": plan["case_id"],
            "source_file_id": selected["file_id"],
            "source_digest": selected["content_digest"],
            "reviewed_bundle_digest": plan["bindings"][
                "reviewed_bundle"
            ]["bundle_digest"],
            "museum_digital_id": plan["identity"]["museum_digital_id"],
        }
        provenance_metadata.update(
            {
                key: value
                for key, value in values.items()
                if key
                in {
                    "object_type",
                    "creator",
                    "date_created",
                    "rights_statement",
                    "rights_holder",
                    "rights_usage_terms",
                    "rights_web_statement",
                    "credit_line",
                }
            }
        )
        if file_decision == "best_available_surrogate":
            provenance_metadata.update(
                {
                    "representation_role": "preservation_surrogate",
                    "provenance_gaps": plan[
                        "preserved_provenance_gaps"
                    ],
                }
            )
        if file_decision == "best_available_surrogate":
            accession_result = (
                surrogate_accession.create_preservation_surrogate(
                    staging_root,
                    Path(selected["locator"]),
                    str(selected["media_type"]),
                    entity_id,
                    actor,
                    provenance_metadata=provenance_metadata,
                )
            )
        else:
            accession_result = tiff_accession.create_preservation_master(
                staging_root,
                Path(selected["locator"]),
                entity_id,
                actor,
                embedding_profile=tiff_metadata.PROFILE_V2,
                provenance_metadata=provenance_metadata,
            )
        publication = catalog_core.publish_generation(
            staging_root,
            staging_independent,
            tool_version=TOOL_VERSION,
            actor=actor,
        )
        with closing(
            catalog_core.connect_catalog(
                staging_root / "work" / "catalog.sqlite",
                read_only=True,
            )
        ) as catalog:
            fixity = catalog_core.verify_preservation_masters(
                catalog,
                staging_root,
                staging_independent,
            )
        expected_fixity = (
            (
                fixity["masters"] == 0
                and fixity["surrogates"] == 1
                and fixity["surrogate_sources"] == 0
                and fixity["derivatives"] == 1
            )
            if file_decision == "best_available_surrogate"
            else (
                fixity["masters"] == 1
                and fixity["surrogates"] == 0
                and fixity["derivatives"] == 0
            )
        )
        if fixity["fixity"] != "ok" or not expected_fixity:
            raise ProvenanceIngestError(
                "staged preservation-representation verification did not pass"
            )
        replacements = (
            (str(staging_root), str(final_root)),
            (str(staging_independent), str(final_independent)),
        )
        accession_result = _rebase_paths(
            accession_result,
            replacements,
        )
        publication = _rebase_paths(publication, replacements)
        accession_report = Path(
            _rebase_paths(
                str(accession_result["report"]),
                ((str(final_root), str(staging_root)),),
            )
        )
        _atomic_json(accession_report, accession_result)
        config = catalog_core.load_config(staging_root)
        config["independent_generation_root"] = str(final_independent)
        catalog_core.save_config(staging_root, config)
        report = {
            "format": INGEST_FORMAT,
            "version": INGEST_VERSION,
            "operation_id": operation_id,
            "completed_at": utc_now(),
            "actor": actor,
            "case_id": plan["case_id"],
            "plan_path": str(Path(plan_path).expanduser().resolve()),
            "plan_digest": plan_digest,
            "reviewed_bundle_digest": plan["bindings"][
                "reviewed_bundle"
            ]["bundle_digest"],
            "root": str(final_root),
            "independent_root": str(final_independent),
            "unit_id": unit,
            "entity_id": entity_id,
            "assignment_year": assignment_year,
            "source_file_id": selected["file_id"],
            "source_digest": selected["content_digest"],
            "file_decision": file_decision,
            "preserved_provenance_gaps": plan.get(
                "preserved_provenance_gaps",
                [],
            ),
            "embedding_profile": tiff_metadata.PROFILE_V2,
            "publication": publication,
            "fixity": fixity,
            "protected_sources_modified": False,
            "state": "complete",
        }
        report[
            (
                "surrogate"
                if file_decision == "best_available_surrogate"
                else "tiff"
            )
        ] = accession_result
        report_path = (
            staging_root
            / "work"
            / f"{operation_id}.provenance-ingest.json"
        )
        _atomic_json(report_path, report)
        os.replace(staging_independent, final_independent)
        try:
            os.replace(staging_root, final_root)
        except Exception:
            os.replace(final_independent, staging_independent)
            raise
        final_status = catalog_core.status(final_root)
        if final_status["catalog_revision"] != final_status[
            "published_revision"
        ]:
            raise ProvenanceIngestError(
                "promoted pilot filesystem generation is not current"
            )
        report["report"] = str(
            final_root / "work" / report_path.name
        )
        report["status"] = final_status
        _atomic_json(Path(report["report"]), report)
        return report
    except Exception as error:
        if staging_root.exists():
            try:
                failure_path.parent.mkdir(parents=True, exist_ok=True)
                _atomic_json(
                    failure_path,
                    {
                        "format": INGEST_FORMAT,
                        "version": INGEST_VERSION,
                        "operation_id": operation_id,
                        "failed_at": utc_now(),
                        "case_id": plan["case_id"],
                        "plan_digest": plan_digest,
                        "error": str(error),
                        "state": "failed_staging_preserved",
                        "staging_root": str(staging_root),
                        "staging_independent": str(staging_independent),
                    },
                )
            except OSError:
                pass
        raise


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--independent-root", type=Path, required=True)
    parser.add_argument("--unit", default="")
    parser.add_argument("--actor", required=True)
    parser.add_argument(
        "--execute",
        action="store_true",
        help="create a fresh pilot; default is revalidating dry-run only",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        if arguments.execute:
            result = execute_ingest(
                arguments.plan,
                arguments.root,
                arguments.independent_root,
                actor=arguments.actor,
                unit_id=arguments.unit,
            )
        else:
            result = dry_run(
                arguments.plan,
                arguments.root,
                arguments.independent_root,
                unit_id=arguments.unit,
            )
    except (
        ProvenanceIngestError,
        ProtectedEvidenceError,
        plan_contract.AccessionPlanError,
        catalog_core.CoreCatalogError,
        tiff_accession.TiffAccessionError,
        OSError,
    ) as error:
        print(f"error: {error}", file=sys.stderr)
        return 3
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
