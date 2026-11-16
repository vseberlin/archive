from __future__ import annotations

import copy
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[2]
TOOLS = REPO_ROOT / "tools" / "archive-preservation"
sys.path.insert(0, str(TOOLS))
from jobs.provenance_restoration import accession_plan as PLAN  # noqa: E402
from jobs.provenance_restoration import bundle as BUNDLE  # noqa: E402
from jobs.provenance_restoration import evidence as EVIDENCE  # noqa: E402
from jobs.provenance_restoration import ingest as INGEST  # noqa: E402
from jobs.provenance_restoration import review as REVIEW  # noqa: E402
from jobs.provenance_restoration import workbench as WORKBENCH  # noqa: E402
from jobs.provenance_restoration.safety import (  # noqa: E402
    ProtectedEvidenceError,
)


class ProvenanceRestorationAccessionPlanTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.catalog = self.base / "archive_index.sqlite"
        self.catalog.write_bytes(b"retained catalog fixture")
        self.master = self.base / "TFA-527856.tif"
        if shutil.which("magick"):
            subprocess.run(
                [
                    "magick",
                    "-size",
                    "8x6",
                    "gradient:",
                    "-colors",
                    "16",
                    "-type",
                    "Palette",
                    "-compress",
                    "None",
                    str(self.master),
                ],
                check=True,
                capture_output=True,
            )
        else:
            self.master.write_bytes(b"fixture TIFF master")
        self.local_image = self.base / "TFA-527856.jpg"
        self.md_image = self.base / "MD-85974.jpg"
        if shutil.which("magick"):
            for path, color in (
                (self.local_image, "gray20"),
                (self.md_image, "gray70"),
            ):
                subprocess.run(
                    [
                        "magick",
                        "-size",
                        "8x6",
                        f"xc:{color}",
                        str(path),
                    ],
                    check=True,
                    capture_output=True,
                )
        else:
            self.local_image.write_bytes(b"fixture local JPEG")
            self.md_image.write_bytes(b"fixture MD JPEG")
        self.parent_path = self.base / "RESTORE-TFA-527856.draft.json"
        self.capture_path = (
            self.base / "RESTORE-TFA-527856.with-md-image.draft.json"
        )
        self.reviewed_path = (
            self.base / "RESTORE-TFA-527856.reviewed.json"
        )
        self.plan_path = (
            self.base / "RESTORE-TFA-527856.accession-plan.v2.json"
        )
        self.parent = self._draft()
        BUNDLE.write_bundle(self.parent_path, self.parent)
        self.capture = copy.deepcopy(self.parent)
        operation = {
            "captured_at": "2026-07-18T10:30:00Z",
            "content_digest": EVIDENCE.file_digest(self.md_image),
            "md_id": "85974",
        }
        self.capture["derivation"] = {
            "kind": "md_derivative_capture",
            "parent_bundle_digest": BUNDLE.bundle_digest(self.parent),
            "operation_digest": BUNDLE.snapshot_digest(operation),
            "operation_snapshot": operation,
        }
        BUNDLE.write_bundle(self.capture_path, self.capture)
        self.reviewed, _ = REVIEW.export_reviewed_bundle(
            self.capture_path,
            self.reviewed_path,
            self._decision(),
            technical_verifier=lambda path: {
                "format": "TIFF",
                "width": 3232,
                "height": 2184,
                "image_directories": 1,
                "probes": ["fixture"],
            },
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _file_source(
        self,
        file_id: str,
        path: Path,
        role: str,
        media_type: str,
    ) -> tuple[dict, dict]:
        digest = EVIDENCE.file_digest(path)
        source_id = f"SOURCE-{file_id}"
        return (
            {
                "source_id": source_id,
                "kind": "local_filesystem",
                "locator": str(path),
                "captured_at": "2026-07-18T10:00:00Z",
                "snapshot_digest": digest,
                "note": "Read-only fixture evidence.",
            },
            {
                "file_id": file_id,
                "role": role,
                "source_id": source_id,
                "locator": str(path),
                "content_digest": digest,
                "size_bytes": path.stat().st_size,
                "media_type": media_type,
                "note": "",
            },
        )

    def _draft(self) -> dict:
        local_snapshot = {
            "catalog_fingerprint": EVIDENCE.file_fingerprint(self.catalog),
            "objects": [
                {
                    "inventory_number": "TFA-527856",
                    "title": "Local title",
                    "description": "Local description",
                    "object_type": "Foto",
                }
            ],
            "object_sources": [],
            "file_links": [],
            "provenance_until_md": [],
        }
        md_snapshot = {
            "objects": [
                {
                    "md_id": "85974",
                    "object_name": "MD title",
                    "object_description": "MD description",
                    "object_type": "Foto",
                }
            ],
            "images": [],
        }
        master_source, master_file = self._file_source(
            "FILE-MASTER",
            self.master,
            "local_master_candidate",
            "image/tiff",
        )
        local_source, local_file = self._file_source(
            "FILE-LOCAL-IMAGE",
            self.local_image,
            "local_derivative",
            "image/jpeg",
        )
        md_digest = EVIDENCE.file_digest(self.md_image)
        md_capture = {
            "source_id": "SOURCE-MD-CAPTURE",
            "kind": "museum_digital_derivative_capture",
            "locator": "https://asset.museum-digital.invalid/85974.jpg",
            "captured_at": "2026-07-18T10:30:00Z",
            "snapshot": {
                "content_digest": md_digest,
                "md_id": "85974",
            },
            "snapshot_digest": BUNDLE.snapshot_digest(
                {
                    "content_digest": md_digest,
                    "md_id": "85974",
                }
            ),
            "note": "",
        }
        return {
            "format": BUNDLE.BUNDLE_FORMAT,
            "version": BUNDLE.BUNDLE_VERSION,
            "direction": BUNDLE.BUNDLE_DIRECTION,
            "case_id": "RESTORE-TFA-527856",
            "created_at": "2026-07-18T10:00:00Z",
            "inventory_label": "TFA-527856",
            "local_search_status": "exact_inventory",
            "lineage": {
                "state": "draft",
                "draft_bundle_digest": "",
                "decision_digest": "",
                "decision_snapshot": {},
            },
            "sources": [
                {
                    "source_id": "SOURCE-LOCAL-CATALOG",
                    "kind": "local_catalog_row",
                    "locator": "sqlite:fixture#inventory=TFA-527856",
                    "captured_at": "2026-07-18T10:00:00Z",
                    "snapshot": local_snapshot,
                    "snapshot_digest": BUNDLE.snapshot_digest(
                        local_snapshot
                    ),
                    "note": "",
                },
                {
                    "source_id": "SOURCE-MD",
                    "kind": "museum_digital_snapshot",
                    "locator": "museum-digital:objects/85974",
                    "captured_at": "2026-07-18T10:00:00Z",
                    "snapshot": md_snapshot,
                    "snapshot_digest": BUNDLE.snapshot_digest(md_snapshot),
                    "note": "",
                },
                master_source,
                local_source,
                md_capture,
            ],
            "files": [
                master_file,
                local_file,
                {
                    "file_id": "FILE-MD-IMAGE",
                    "role": "museum_digital_derivative",
                    "source_id": "SOURCE-MD-CAPTURE",
                    "locator": str(self.md_image),
                    "content_digest": md_digest,
                    "size_bytes": self.md_image.stat().st_size,
                    "media_type": "image/jpeg",
                    "note": "",
                },
            ],
            "identity": {
                "state": "pending",
                "outcome": "unresolved",
                "reason": "",
                "actor": "",
                "decided_at": "",
                "evidence_source_ids": [
                    "SOURCE-LOCAL-CATALOG",
                    "SOURCE-MD",
                    "SOURCE-MD-CAPTURE",
                ],
            },
            "metadata_decisions": [],
            "file_decision": {
                "state": "pending",
                "outcome": "unresolved",
                "selected_file_id": "",
                "verification_method": "",
                "verification_details": {},
                "reason": "",
                "actor": "",
                "decided_at": "",
            },
            "readiness": {
                "state": "draft",
                "file_state": "unresolved",
                "selected_file_id": "",
                "provenance_gaps": [],
                "blocking_gaps": [],
            },
        }

    def _decision(self) -> dict:
        decisions = []
        for field, locator, value in (
            ("descriptive_title", "object_name", "MD title"),
            ("description", "object_description", "MD description"),
            ("object_type", "object_type", "Foto"),
        ):
            decisions.append(
                {
                    "field": field,
                    "value": value,
                    "action": "accept_museum_digital",
                    "source_id": "SOURCE-MD",
                    "source_locator": f"objects[0].{locator}",
                    "reason": "",
                    "actor": "Fixture Archivist",
                    "decided_at": "2026-07-18T10:55:00Z",
                }
            )
        return {
            "format": REVIEW.REVIEW_FORMAT,
            "version": REVIEW.REVIEW_VERSION,
            "case_id": "RESTORE-TFA-527856",
            "draft_bundle_digest": BUNDLE.bundle_digest(self.capture),
            "reviewed_at": "2026-07-18T11:00:00Z",
            "identity": {
                "outcome": "same_entity",
                "reason": "",
                "actor": "Fixture Archivist",
                "decided_at": "2026-07-18T10:50:00Z",
                "evidence_source_ids": [
                    "SOURCE-LOCAL-CATALOG",
                    "SOURCE-MD",
                    "SOURCE-MD-CAPTURE",
                ],
            },
            "metadata_decisions": decisions,
            "file_decision": {
                "outcome": "verified_local_master",
                "selected_file_id": "FILE-MASTER",
                "reason": "",
                "actor": "Fixture Archivist",
                "decided_at": "2026-07-18T10:58:00Z",
            },
        }

    def test_plan_is_deterministic_bound_and_non_writing(self) -> None:
        first = PLAN.build_accession_plan(
            self.parent_path,
            self.capture_path,
            self.reviewed_path,
        )
        second = PLAN.build_accession_plan(
            self.parent_path,
            self.capture_path,
            self.reviewed_path,
        )
        self.assertEqual(first, second)
        self.assertEqual(PLAN.plan_digest(first), PLAN.plan_digest(second))
        self.assertEqual(
            BUNDLE.bundle_digest(self.parent),
            first["bindings"]["parent_draft"]["bundle_digest"],
        )
        self.assertEqual(
            BUNDLE.bundle_digest(self.capture),
            first["bindings"]["captured_draft"]["bundle_digest"],
        )
        self.assertEqual(
            BUNDLE.bundle_digest(self.reviewed),
            first["bindings"]["reviewed_bundle"]["bundle_digest"],
        )
        self.assertFalse(first["authorization"]["archive_writes"])
        self.assertFalse(first["authorization"]["catalog_writes"])
        self.assertFalse(first["authorization"]["source_writes"])
        self.assertFalse(first["authorization"]["repeat_curatorial_review"])
        self.assertEqual(
            "not_authorized_by_dry_run",
            first["execution_state"],
        )

    def test_plan_carries_engineering_requirements_without_new_review(self) -> None:
        plan = PLAN.build_accession_plan(
            self.parent_path,
            self.capture_path,
            self.reviewed_path,
        )
        keys = {
            item["key"]
            for item in plan["implementation_requirements"]
        }
        self.assertIn("optional_metadata:material_technique", keys)
        self.assertIn("optional_metadata:dimensions", keys)
        self.assertIn("metadata_mapping:object_type", keys)
        self.assertIn("target:unit_id", keys)
        self.assertEqual(
            "ready_for_implementation",
            plan["plan_state"],
        )
        self.assertEqual(
            PLAN.PATH_TEMPLATE,
            plan["proposed_archive"]["primary_relative_path_template"],
        )

    def test_write_refuses_replacement_and_protected_output(self) -> None:
        written, digest = PLAN.write_accession_plan(
            self.parent_path,
            self.capture_path,
            self.reviewed_path,
            self.plan_path,
        )
        self.assertEqual(written, PLAN.read_accession_plan(self.plan_path))
        self.assertEqual(digest, PLAN.plan_digest(written))
        with self.assertRaisesRegex(
            PLAN.AccessionPlanError,
            "refusing to replace",
        ):
            PLAN.write_accession_plan(
                self.parent_path,
                self.capture_path,
                self.reviewed_path,
                self.plan_path,
            )
        protected = Path("/mnt/volume/archive") / (
            f".accession-plan-{self.base.name}.json"
        )
        with self.assertRaises(ProtectedEvidenceError):
            PLAN.write_accession_plan(
                self.parent_path,
                self.capture_path,
                self.reviewed_path,
                protected,
            )
        self.assertFalse(protected.exists())

    def test_plan_fails_if_parent_or_selected_evidence_changes(self) -> None:
        different_parent = copy.deepcopy(self.parent)
        different_parent["sources"][0]["note"] = "different parent"
        different_path = self.base / "different-parent.json"
        BUNDLE.write_bundle(different_path, different_parent)
        with self.assertRaisesRegex(
            PLAN.AccessionPlanError,
            "parent digest differs",
        ):
            PLAN.build_accession_plan(
                different_path,
                self.capture_path,
                self.reviewed_path,
            )
        self.master.write_bytes(b"changed TIFF evidence")
        with self.assertRaisesRegex(
            REVIEW.ProvenanceReviewError,
            "size changed|digest changed",
        ):
            PLAN.build_accession_plan(
                self.parent_path,
                self.capture_path,
                self.reviewed_path,
            )

    def test_reviewed_workbench_uses_staged_old_workbench_hierarchy(self) -> None:
        plan = PLAN.build_accession_plan(
            self.parent_path,
            self.capture_path,
            self.reviewed_path,
        )
        page = WORKBENCH.render_page(
            self.capture,
            self.reviewed_path,
            "fixture-token",
            reviewed=self.reviewed,
            plan=plan,
        )
        self.assertIn('href="/assets/workbench.css"', page)
        self.assertIn('class="visual-case"', page)
        self.assertIn("Accepted decisions", page)
        self.assertIn("Dry-run accession plan", page)
        self.assertIn("implementation requirements", page)
        self.assertIn("no second curatorial review", page)
        self.assertNotIn('action="/export"', page)

    def test_ingest_dry_run_revalidates_without_creating_roots(self) -> None:
        PLAN.write_accession_plan(
            self.parent_path,
            self.capture_path,
            self.reviewed_path,
            self.plan_path,
        )
        root = self.base / "pilot"
        independent = self.base / "independent"
        result = INGEST.dry_run(
            self.plan_path,
            root,
            independent,
        )
        self.assertEqual("dry_run", result["result"])
        self.assertFalse(result["writes_authorized"])
        self.assertEqual("iss-tiff-v2", result["embedding_profile"])
        self.assertFalse(root.exists())
        self.assertFalse(independent.exists())
        with self.assertRaises(INGEST.ProtectedEvidenceError):
            INGEST.dry_run(
                self.plan_path,
                Path("/mnt/volume/archive/forbidden-pilot"),
                independent,
            )

    def test_guarded_ingest_failure_preserves_only_named_staging(self) -> None:
        PLAN.write_accession_plan(
            self.parent_path,
            self.capture_path,
            self.reviewed_path,
            self.plan_path,
        )
        root = self.base / "failed-pilot"
        independent = self.base / "failed-independent"
        with mock.patch.object(
            INGEST.catalog_core,
            "publish_generation",
            side_effect=INGEST.catalog_core.CoreCatalogError(
                "forced publication failure"
            ),
        ):
            with self.assertRaisesRegex(
                INGEST.catalog_core.CoreCatalogError,
                "forced publication failure",
            ):
                INGEST.execute_ingest(
                    self.plan_path,
                    root,
                    independent,
                    actor="Fixture Archivist",
                )
        self.assertFalse(root.exists())
        self.assertFalse(independent.exists())
        staging = list(
            self.base.glob(".failed-pilot.INGEST-*.staging")
        )
        self.assertEqual(1, len(staging))
        reports = list(
            (staging[0] / "work").glob("*.failed.json")
        )
        self.assertEqual(1, len(reports))
        failure = json.loads(reports[0].read_text(encoding="utf-8"))
        self.assertEqual("failed_staging_preserved", failure["state"])

    @unittest.skipUnless(
        shutil.which("exiftool") and shutil.which("magick"),
        "ExifTool and ImageMagick are required",
    )
    def test_guarded_surrogate_ingest_never_claims_an_original_master(
        self,
    ) -> None:
        parent = copy.deepcopy(self.parent)
        parent["sources"] = [
            source
            for source in parent["sources"]
            if source["source_id"] != "SOURCE-FILE-MASTER"
        ]
        parent["files"] = [
            file
            for file in parent["files"]
            if file["file_id"] != "FILE-MASTER"
        ]
        parent["readiness"]["provenance_gaps"] = [
            "missing_local_master"
        ]
        parent_path = self.base / "RESTORE-SURROGATE.draft.json"
        BUNDLE.write_bundle(parent_path, parent)

        capture = copy.deepcopy(parent)
        operation = {
            "captured_at": "2026-07-18T10:30:00Z",
            "content_digest": EVIDENCE.file_digest(self.md_image),
            "md_id": "85974",
        }
        capture["derivation"] = {
            "kind": "md_derivative_capture",
            "parent_bundle_digest": BUNDLE.bundle_digest(parent),
            "operation_digest": BUNDLE.snapshot_digest(operation),
            "operation_snapshot": operation,
        }
        capture_path = self.base / "RESTORE-SURROGATE.captured.json"
        BUNDLE.write_bundle(capture_path, capture)

        decision = self._decision()
        decision["draft_bundle_digest"] = BUNDLE.bundle_digest(capture)
        decision["file_decision"].update(
            {
                "outcome": "best_available_surrogate",
                "selected_file_id": "FILE-MD-IMAGE",
            }
        )
        reviewed_path = self.base / "RESTORE-SURROGATE.reviewed.json"
        reviewed, _digest = REVIEW.export_reviewed_bundle(
            capture_path,
            reviewed_path,
            decision,
        )
        self.assertEqual(
            "best_available_surrogate",
            reviewed["readiness"]["file_state"],
        )
        self.assertIn(
            "missing_local_master",
            reviewed["readiness"]["provenance_gaps"],
        )
        plan_path = self.base / "RESTORE-SURROGATE.plan.json"
        plan, _plan_digest = PLAN.write_accession_plan(
            parent_path,
            capture_path,
            reviewed_path,
            plan_path,
        )
        self.assertEqual(
            PLAN.SURROGATE_PATH_TEMPLATE,
            plan["proposed_archive"][
                "primary_relative_path_template"
            ],
        )
        self.assertIn(
            "missing_local_master",
            plan["preserved_provenance_gaps"],
        )

        root = self.base / "surrogate-pilot"
        independent = self.base / "surrogate-independent"
        source_before = EVIDENCE.file_fingerprint(self.md_image)
        with mock.patch.object(
            INGEST.catalog_core,
            "assert_independent_device",
        ):
            result = INGEST.execute_ingest(
                plan_path,
                root,
                independent,
                actor="Fixture Archivist",
            )
        self.assertEqual(
            "best_available_surrogate",
            result["file_decision"],
        )
        self.assertEqual(0, result["fixity"]["masters"])
        self.assertEqual(1, result["fixity"]["surrogates"])
        self.assertEqual(0, result["fixity"]["surrogate_sources"])
        self.assertEqual(1, result["fixity"]["derivatives"])
        surrogate = Path(result["surrogate"]["surrogate"])
        selected_copy = Path(
            result["surrogate"]["selected_source_copy"]
        )
        self.assertTrue(surrogate.is_file())
        self.assertTrue(selected_copy.is_file())
        self.assertEqual(
            EVIDENCE.file_digest(self.md_image),
            EVIDENCE.file_digest(selected_copy),
        )
        self.assertEqual(
            source_before,
            EVIDENCE.file_fingerprint(self.md_image),
        )
        self.assertEqual(
            result["surrogate"]["decoded_pixel_digest"],
            INGEST.tiff_accession._decoded_pixel_digest(
                self.md_image,
                int(
                    result["surrogate"]["technical_metadata"][
                        "IFD0:BitsPerSample"
                    ]
                ),
            ),
        )
        self.assertIn(
            "representation preservation_surrogate",
            result["surrogate"]["embedded_metadata"]["XMP-dc:Source"],
        )
        self.assertIn(
            "iss:provenance-gap:missing_local_master",
            result["surrogate"]["embedded_metadata"]["XMP-dc:Relation"],
        )
        with closing(
            INGEST.catalog_core.connect_catalog(
                root / "work" / "catalog.sqlite",
                read_only=True,
            )
        ) as catalog:
            roles = [
                row["role"]
                for row in catalog.execute(
                    """SELECT role FROM entity_files
                       WHERE entity_id = ? ORDER BY role""",
                    (result["entity_id"],),
                )
            ]
        self.assertEqual(
            [
                "access_derivative",
                "preservation_surrogate",
            ],
            roles,
        )
        self.assertNotIn("preservation_master", roles)

    @unittest.skipUnless(
        shutil.which("exiftool") and shutil.which("magick"),
        "ExifTool and ImageMagick are required",
    )
    def test_guarded_ingest_promotes_one_fresh_copy_only_pilot(self) -> None:
        PLAN.write_accession_plan(
            self.parent_path,
            self.capture_path,
            self.reviewed_path,
            self.plan_path,
        )
        root = self.base / "pilot"
        independent = self.base / "independent"
        source_before = EVIDENCE.file_fingerprint(self.master)
        with mock.patch.object(
            INGEST.catalog_core,
            "assert_independent_device",
        ):
            result = INGEST.execute_ingest(
                self.plan_path,
                root,
                independent,
                actor="Fixture Archivist",
            )
        self.assertEqual("complete", result["state"])
        self.assertEqual("iss-tiff-v2", result["embedding_profile"])
        self.assertEqual("ok", result["fixity"]["fixity"])
        self.assertEqual(1, result["fixity"]["masters"])
        self.assertTrue(Path(result["tiff"]["master"]).is_file())
        self.assertTrue(Path(result["tiff"]["independent_copy"]).is_file())
        self.assertTrue(Path(result["report"]).is_file())
        self.assertEqual(
            source_before,
            EVIDENCE.file_fingerprint(self.master),
        )
        self.assertEqual(
            result["status"]["catalog_revision"],
            result["status"]["published_revision"],
        )
        with self.assertRaisesRegex(
            INGEST.ProvenanceIngestError,
            "refusing to replace",
        ):
            INGEST.execute_ingest(
                self.plan_path,
                root,
                independent,
                actor="Fixture Archivist",
            )


if __name__ == "__main__":
    unittest.main()
