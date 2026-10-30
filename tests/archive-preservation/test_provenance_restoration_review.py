from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
TOOLS = REPO_ROOT / "tools" / "archive-preservation"
sys.path.insert(0, str(TOOLS))
from jobs.provenance_restoration import bundle as BUNDLE  # noqa: E402
from jobs.provenance_restoration import evidence as EVIDENCE  # noqa: E402
from jobs.provenance_restoration import review as REVIEW  # noqa: E402
from jobs.provenance_restoration import workbench as WORKBENCH  # noqa: E402
from jobs.provenance_restoration.safety import (  # noqa: E402
    ProtectedEvidenceError,
)


class ProvenanceRestorationReviewTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.catalog = self.base / "archive_index.sqlite"
        self.catalog.write_bytes(b"retained catalog fixture")
        self.master = self.base / "VLV-1112.tif"
        self.master.write_bytes(b"fixture TIFF master")
        self.derivative = self.base / "VLV-1112.jpg"
        self.derivative.write_bytes(b"fixture JPEG derivative")
        self.md_derivative = self.base / "MD-70001.jpg"
        self.md_derivative.write_bytes(b"captured MD JPEG derivative")
        self.draft_path = self.base / "RESTORE-VLV-1112.draft.json"
        self.output_path = self.base / "RESTORE-VLV-1112.reviewed.json"
        self.draft = self._draft()
        BUNDLE.write_bundle(self.draft_path, self.draft)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _source_file(
        self,
        file_id: str,
        path: Path,
        role: str,
        media_type: str,
    ) -> tuple[dict, dict]:
        digest = EVIDENCE.file_digest(path)
        source_id = f"SOURCE-{file_id}"
        source = {
            "source_id": source_id,
            "kind": "local_filesystem",
            "locator": str(path),
            "captured_at": "2026-07-18T10:00:00Z",
            "snapshot_digest": digest,
            "note": "Read-only fixture evidence.",
        }
        file = {
            "file_id": file_id,
            "role": role,
            "source_id": source_id,
            "locator": str(path),
            "content_digest": digest,
            "size_bytes": path.stat().st_size,
            "media_type": media_type,
            "note": "",
        }
        return source, file

    def _draft(self) -> dict:
        local_snapshot = {
            "catalog_fingerprint": EVIDENCE.file_fingerprint(self.catalog),
            "objects": [
                {
                    "inventory_number": "VLV-1112",
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
                    "md_id": "70001",
                    "object_name": "MD title",
                    "object_description": "MD description",
                    "object_type": "Foto",
                    "raw_json": json.dumps(
                        {
                            "object_material_technique": "Fotoabzug",
                            "object_dimensions": "18x24 cm",
                        }
                    ),
                }
            ],
            "images": [
                {
                    "creator": "Kurt Schwarz",
                    "rights": "CC BY-NC-SA",
                    "owner": "Kurt Schwarz",
                }
            ],
        }
        master_source, master_file = self._source_file(
            "FILE-MASTER",
            self.master,
            "local_master_candidate",
            "image/tiff",
        )
        derivative_source, derivative_file = self._source_file(
            "FILE-DERIVATIVE",
            self.derivative,
            "local_derivative",
            "image/jpeg",
        )
        md_derivative_file = {
            "file_id": "FILE-MD-DERIVATIVE",
            "role": "museum_digital_derivative",
            "source_id": "SOURCE-MD",
            "locator": str(self.md_derivative),
            "content_digest": EVIDENCE.file_digest(self.md_derivative),
            "size_bytes": self.md_derivative.stat().st_size,
            "media_type": "image/jpeg",
            "note": "Captured fixture MD comparison image.",
        }
        return {
            "format": BUNDLE.BUNDLE_FORMAT,
            "version": BUNDLE.BUNDLE_VERSION,
            "direction": BUNDLE.BUNDLE_DIRECTION,
            "case_id": "RESTORE-VLV-1112",
            "created_at": "2026-07-18T10:00:00Z",
            "inventory_label": "VLV-1112",
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
                    "locator": "sqlite:fixture#inventory=VLV-1112",
                    "captured_at": "2026-07-18T10:00:00Z",
                    "snapshot": local_snapshot,
                    "snapshot_digest": BUNDLE.snapshot_digest(
                        local_snapshot
                    ),
                    "note": "Exact fixture rows.",
                },
                {
                    "source_id": "SOURCE-MD",
                    "kind": "museum_digital_snapshot",
                    "locator": "museum-digital:objects/70001",
                    "captured_at": "2026-07-18T10:00:00Z",
                    "snapshot": md_snapshot,
                    "snapshot_digest": BUNDLE.snapshot_digest(md_snapshot),
                    "note": "Cached fixture MD rows.",
                },
                master_source,
                derivative_source,
            ],
            "files": [
                master_file,
                derivative_file,
                md_derivative_file,
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
        return {
            "format": REVIEW.REVIEW_FORMAT,
            "version": REVIEW.REVIEW_VERSION,
            "case_id": "RESTORE-VLV-1112",
            "draft_bundle_digest": BUNDLE.bundle_digest(self.draft),
            "reviewed_at": "2026-07-18T11:00:00Z",
            "identity": {
                "outcome": "same_entity",
                "reason": "Inventory and evidence agree.",
                "actor": "Fixture Archivist",
                "decided_at": "2026-07-18T10:50:00Z",
                "evidence_source_ids": [
                    "SOURCE-LOCAL-CATALOG",
                    "SOURCE-MD",
                ],
            },
            "metadata_decisions": [
                {
                    "field": "descriptive_title",
                    "value": "MD title",
                    "action": "accept_museum_digital",
                    "source_id": "SOURCE-MD",
                    "source_locator": "objects[0].object_name",
                    "reason": "MD title is more complete.",
                    "actor": "Fixture Archivist",
                    "decided_at": "2026-07-18T10:55:00Z",
                }
            ],
            "file_decision": {
                "outcome": "verified_local_master",
                "selected_file_id": "FILE-MASTER",
                "reason": "TIFF passed the bounded technical checks.",
                "actor": "Fixture Archivist",
                "decided_at": "2026-07-18T10:58:00Z",
            },
        }

    def test_exports_new_ready_bundle_and_preserves_every_input(self) -> None:
        draft_before = EVIDENCE.file_fingerprint(self.draft_path)
        catalog_before = EVIDENCE.file_fingerprint(self.catalog)
        master_before = EVIDENCE.file_fingerprint(self.master)
        derivative_before = EVIDENCE.file_fingerprint(self.derivative)
        md_before = EVIDENCE.file_fingerprint(self.md_derivative)

        reviewed, digest = REVIEW.export_reviewed_bundle(
            self.draft_path,
            self.output_path,
            self._decision(),
            technical_verifier=lambda path: {
                "format": "TIFF",
                "width": 10,
                "height": 10,
                "image_directories": 1,
                "probes": ["fixture"],
            },
        )

        self.assertEqual("ready_for_ingest", reviewed["readiness"]["state"])
        self.assertEqual("reviewed", reviewed["lineage"]["state"])
        self.assertEqual(
            BUNDLE.bundle_digest(self.draft),
            reviewed["lineage"]["draft_bundle_digest"],
        )
        self.assertEqual(
            "local_master",
            next(
                file["role"]
                for file in reviewed["files"]
                if file["file_id"] == "FILE-MASTER"
            ),
        )
        self.assertEqual(digest, BUNDLE.bundle_digest(reviewed))
        self.assertEqual(reviewed, BUNDLE.read_bundle(self.output_path))
        self.assertEqual(
            draft_before,
            EVIDENCE.file_fingerprint(self.draft_path),
        )
        self.assertEqual(
            catalog_before,
            EVIDENCE.file_fingerprint(self.catalog),
        )
        self.assertEqual(
            master_before,
            EVIDENCE.file_fingerprint(self.master),
        )
        self.assertEqual(
            derivative_before,
            EVIDENCE.file_fingerprint(self.derivative),
        )
        self.assertEqual(
            md_before,
            EVIDENCE.file_fingerprint(self.md_derivative),
        )
        with self.assertRaisesRegex(
            BUNDLE.ProvenanceBundleError,
            "refusing to replace",
        ):
            REVIEW.export_reviewed_bundle(
                self.draft_path,
                self.output_path,
                self._decision(),
                technical_verifier=lambda path: {"format": "TIFF"},
            )

    def test_deferred_review_exports_blocked_without_master_promotion(self) -> None:
        decision = self._decision()
        decision["identity"]["outcome"] = "defer"
        decision["metadata_decisions"][0].update(
            {
                "value": "",
                "action": "defer",
                "source_id": "",
                "source_locator": "",
            }
        )
        decision["file_decision"].update(
            {
                "outcome": "defer",
                "selected_file_id": "",
            }
        )
        reviewed, _ = REVIEW.export_reviewed_bundle(
            self.draft_path,
            self.output_path,
            decision,
        )
        self.assertEqual("blocked", reviewed["readiness"]["state"])
        self.assertEqual("deferred", reviewed["identity"]["state"])
        self.assertEqual("deferred", reviewed["file_decision"]["state"])
        self.assertIn(
            "identity_review_deferred",
            reviewed["readiness"]["blocking_gaps"],
        )
        self.assertIn(
            "file_review_deferred",
            reviewed["readiness"]["blocking_gaps"],
        )
        self.assertIn(
            "local_master_candidate",
            {file["role"] for file in reviewed["files"]},
        )

    def test_rejects_wrong_draft_digest_before_output(self) -> None:
        decision = self._decision()
        decision["draft_bundle_digest"] = "sha256:" + "0" * 64
        with self.assertRaisesRegex(
            REVIEW.ProvenanceReviewError,
            "draft digest does not match",
        ):
            REVIEW.export_reviewed_bundle(
                self.draft_path,
                self.output_path,
                decision,
            )
        self.assertFalse(self.output_path.exists())

    def test_rejects_changed_source_before_output(self) -> None:
        self.master.write_bytes(b"changed fixture TIFF master")
        with self.assertRaisesRegex(
            REVIEW.ProvenanceReviewError,
            "size changed|digest changed",
        ):
            REVIEW.export_reviewed_bundle(
                self.draft_path,
                self.output_path,
                self._decision(),
            )
        self.assertFalse(self.output_path.exists())

    def test_rejects_protected_output_before_source_review(self) -> None:
        protected = (
            Path("/mnt/volume/archive")
            / f".review-output-{self.base.name}.json"
        )
        with self.assertRaises(ProtectedEvidenceError):
            REVIEW.export_reviewed_bundle(
                self.draft_path,
                protected,
                self._decision(),
            )
        self.assertFalse(protected.exists())

    def test_review_decision_is_embedded_and_digest_guarded(self) -> None:
        reviewed, _ = REVIEW.export_reviewed_bundle(
            self.draft_path,
            self.output_path,
            self._decision(),
            technical_verifier=lambda path: {"format": "TIFF"},
        )
        tampered = copy.deepcopy(reviewed)
        tampered["lineage"]["decision_snapshot"]["identity"][
            "reason"
        ] = "Changed later"
        with self.assertRaisesRegex(
            BUNDLE.ProvenanceBundleError,
            "decision snapshot digest does not match",
        ):
            BUNDLE.validate_bundle(tampered)

    def test_blank_review_comments_export_without_filler(self) -> None:
        decision = self._decision()
        decision["identity"]["reason"] = ""
        decision["file_decision"]["reason"] = ""
        for metadata in decision["metadata_decisions"]:
            metadata["reason"] = ""
        reviewed, _ = REVIEW.export_reviewed_bundle(
            self.draft_path,
            self.output_path,
            decision,
            technical_verifier=lambda path: {"format": "TIFF"},
        )
        self.assertEqual("ready_for_ingest", reviewed["readiness"]["state"])
        self.assertEqual("", reviewed["identity"]["reason"])
        self.assertEqual("", reviewed["file_decision"]["reason"])
        self.assertEqual(
            [""],
            [
                metadata["reason"]
                for metadata in reviewed["metadata_decisions"]
            ],
        )

    def test_workbench_renders_embedded_evidence_without_source_reads(self) -> None:
        self.catalog.rename(self.base / "catalog-temporarily-unavailable")
        page = WORKBENCH.render_page(
            self.draft,
            self.output_path,
            "fixture-token",
        )
        self.assertIn("Local title", page)
        self.assertIn("MD title", page)
        self.assertIn("FILE-MASTER", page)
        self.assertIn("does not query", page)

    def test_workbench_form_builds_explicit_review_document(self) -> None:
        form = {
            "confirm_export": ["yes"],
            "actor": ["Fixture Archivist"],
            "identity_outcome": ["same_entity"],
            "identity_reason": ["Inventory and evidence agree."],
            "metadata_descriptive_title_choice": ["museum_digital"],
            "metadata_descriptive_title_reason": ["More complete title."],
            "file_outcome": ["verified_local_master"],
            "selected_file_id": ["FILE-MASTER"],
            "technical_acknowledged": ["yes"],
            "file_reason": ["Technical checks required."],
        }
        decision = WORKBENCH.decision_from_form(
            form,
            self.draft,
            "2026-07-18T12:00:00Z",
        )
        REVIEW.validate_review(decision, self.draft)
        self.assertEqual(
            "accept_museum_digital",
            decision["metadata_decisions"][0]["action"],
        )
        self.assertEqual(
            "verified_local_master",
            decision["file_decision"]["outcome"],
        )

    def test_workbench_exposes_optional_physical_and_rights_evidence(
        self,
    ) -> None:
        page = WORKBENCH.render_page(
            self.draft,
            self.output_path,
            "fixture-token",
        )
        self.assertIn("Material / technique", page)
        self.assertIn("18x24 cm", page)
        self.assertIn("CC BY-NC-SA", page)
        self.assertIn("Rights holder", page)
        form = {
            "confirm_export": ["yes"],
            "actor": ["Fixture Archivist"],
            "identity_outcome": ["same_entity"],
            "metadata_material_technique_choice": ["museum_digital"],
            "metadata_dimensions_choice": ["museum_digital"],
            "metadata_creator_choice": ["museum_digital"],
            "metadata_rights_statement_choice": ["museum_digital"],
            "metadata_rights_holder_choice": ["museum_digital"],
            "file_outcome": ["best_available_surrogate"],
            "selected_file_id": ["FILE-MD-DERIVATIVE"],
        }
        decision = WORKBENCH.decision_from_form(
            form,
            self.draft,
            "2026-07-18T12:00:00Z",
        )
        REVIEW.validate_review(decision, self.draft)
        metadata = {
            item["field"]: item
            for item in decision["metadata_decisions"]
        }
        self.assertEqual(
            "objects[0].raw_json.object_dimensions",
            metadata["dimensions"]["source_locator"],
        )
        self.assertEqual(
            "images[0].rights",
            metadata["rights_statement"]["source_locator"],
        )
        self.assertEqual(
            "Kurt Schwarz",
            metadata["rights_holder"]["value"],
        )

    def test_workbench_requires_technical_acknowledgement(self) -> None:
        form = {
            "confirm_export": ["yes"],
            "actor": ["Fixture Archivist"],
            "identity_outcome": ["same_entity"],
            "identity_reason": ["Inventory and evidence agree."],
            "file_outcome": ["verified_local_master"],
            "selected_file_id": ["FILE-MASTER"],
            "file_reason": ["Technical checks required."],
        }
        with self.assertRaisesRegex(
            WORKBENCH.WorkbenchInputError,
            "Acknowledge",
        ):
            WORKBENCH.decision_from_form(form, self.draft)

    def test_workbench_error_preserves_submitted_choices(self) -> None:
        page = WORKBENCH.render_page(
            self.draft,
            self.output_path,
            "fixture-token",
            error="Selected creator value is empty.",
            submitted_form={
                "actor": ["Fixture Archivist"],
                "identity_outcome": ["same_entity"],
                "metadata_descriptive_title_choice": [
                    "museum_digital"
                ],
                "metadata_descriptive_title_reason": ["Reviewed title"],
                "file_outcome": ["best_available_surrogate"],
                "selected_file_id": ["FILE-MD-DERIVATIVE"],
                "confirm_export": ["yes"],
            },
        )
        self.assertIn("Review was not exported", page)
        self.assertIn("Selected creator value is empty.", page)
        self.assertIn(
            'value="Fixture Archivist"',
            page,
        )
        self.assertIn(
            'value="same_entity" selected',
            page,
        )
        self.assertIn(
            'value="best_available_surrogate" selected',
            page,
        )
        self.assertIn(
            "Reviewed title</textarea>",
            page,
        )

    def test_workbench_does_not_require_comment_fields(self) -> None:
        form = {
            "confirm_export": ["yes"],
            "actor": ["Fixture Archivist"],
            "identity_outcome": ["same_entity"],
            "metadata_descriptive_title_choice": ["museum_digital"],
            "file_outcome": ["verified_local_master"],
            "selected_file_id": ["FILE-MASTER"],
            "technical_acknowledged": ["yes"],
        }
        decision = WORKBENCH.decision_from_form(
            form,
            self.draft,
            "2026-07-18T12:00:00Z",
        )
        REVIEW.validate_review(decision, self.draft)
        self.assertEqual("", decision["identity"]["reason"])
        self.assertEqual("", decision["file_decision"]["reason"])
        self.assertEqual(
            "",
            decision["metadata_decisions"][0]["reason"],
        )

    def test_workbench_serves_only_revalidated_captured_images(self) -> None:
        path, media_type = WORKBENCH.evidence_file(
            self.draft,
            "FILE-MD-DERIVATIVE",
        )
        self.assertEqual(self.md_derivative, path)
        self.assertEqual("image/jpeg", media_type)
        page = WORKBENCH.render_page(
            self.draft,
            self.output_path,
            "fixture-token",
        )
        self.assertIn(
            "/evidence?file_id=FILE-MD-DERIVATIVE",
            page,
        )
        self.assertIn("No remote image is loaded", page)

        self.md_derivative.write_bytes(b"changed captured image")
        with self.assertRaisesRegex(
            WORKBENCH.WorkbenchInputError,
            "size no longer matches|digest no longer matches",
        ):
            WORKBENCH.evidence_file(
                self.draft,
                "FILE-MD-DERIVATIVE",
            )

    def test_positive_identity_requires_captured_md_image_bytes(self) -> None:
        draft = copy.deepcopy(self.draft)
        draft["files"] = [
            file
            for file in draft["files"]
            if file["role"] != "museum_digital_derivative"
        ]
        BUNDLE.validate_bundle(draft)
        decision = self._decision()
        decision["draft_bundle_digest"] = BUNDLE.bundle_digest(draft)
        with self.assertRaisesRegex(
            REVIEW.ProvenanceReviewError,
            "captured local and MD image bytes",
        ):
            REVIEW.validate_review(decision, draft)
        page = WORKBENCH.render_page(
            draft,
            self.output_path,
            "fixture-token",
        )
        self.assertIn("Positive identity is disabled", page)
        self.assertIn('value="same_entity" disabled', page)


if __name__ == "__main__":
    unittest.main()
