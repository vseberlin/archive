from __future__ import annotations

import csv
import hashlib
import http.client
import json
import sqlite3
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.parse
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[2]
TOOLS = REPO_ROOT / "tools" / "archive-preservation"
sys.path.insert(0, str(TOOLS))
import catalog_core as CORE  # noqa: E402
import core_workbench as WORKBENCH  # noqa: E402
import reconciliation as RECONCILIATION  # noqa: E402
from archive_core.layout import object_relative_path  # noqa: E402
from workflows import editor_reconcile  # noqa: E402


class CoreReconciliationWorkbenchTest(unittest.TestCase):
    def test_kurt_wrong_candidate_reassignment_stays_in_one_workbench(self) -> None:
        source = (TOOLS / "core_workbench.py").read_text(encoding="utf-8")
        self.assertIn("Missing selection", source)
        self.assertIn("Wrong candidates", source)
        self.assertIn('action="/kurt-schwarz/candidate-link"', source)
        self.assertIn("Add to target review", source)
        self.assertIn("resolve_record_reference", source)

    def test_next_pending_kurt_record_advances_and_wraps(self) -> None:
        records = [
            {"record_key": "md:1", "review_state": "pending"},
            {"record_key": "md:2", "review_state": "decided"},
            {"record_key": "md:3", "review_state": "pending"},
        ]
        self.assertEqual(
            "md:3", WORKBENCH.next_pending_kurt_record(records, "md:1")
        )
        self.assertEqual(
            "md:1", WORKBENCH.next_pending_kurt_record(records, "md:3")
        )
        self.assertEqual(
            "md:1", WORKBENCH.next_pending_kurt_record(records, "missing")
        )
        self.assertEqual(
            "",
            WORKBENCH.next_pending_kurt_record(
                [{"record_key": "md:1", "review_state": "pending"}],
                "md:1",
            ),
        )

    def test_next_pending_kurt_document_advances_and_wraps(self) -> None:
        documents = [
            {"document_id": "DOC-1", "review_state": "pending"},
            {"document_id": "DOC-2", "review_state": "decided"},
            {"document_id": "DOC-3", "review_state": "pending"},
        ]
        self.assertEqual(
            "DOC-3",
            WORKBENCH.next_pending_kurt_document(documents, "DOC-1"),
        )
        self.assertEqual(
            "DOC-1",
            WORKBENCH.next_pending_kurt_document(documents, "DOC-3"),
        )

    def test_kurt_document_review_stays_in_existing_workbench(self) -> None:
        source = (TOOLS / "core_workbench.py").read_text(encoding="utf-8")
        self.assertIn("/kurt-schwarz/documents", source)
        self.assertIn("/kurt-schwarz/document-decision", source)
        self.assertIn("Cross-unit reference only", source)
        self.assertIn("bounded pages of {page_size}", source)

    def test_kurt_master_wall_can_show_all_lazy_thumbnails(self) -> None:
        source = (TOOLS / "core_workbench.py").read_text(encoding="utf-8")
        self.assertIn('parameters.get("per_page", "all")', source)
        self.assertIn('<label>Per page<select name="per_page">', source)
        self.assertIn("option('all', 'All', per_page)", source)
        self.assertIn("Showing all matching masters", source)

    def test_kurt_object_census_reviews_new_objects_and_master_upgrades(self) -> None:
        source = (TOOLS / "core_workbench.py").read_text(encoding="utf-8")
        self.assertIn("/kurt-schwarz/object-census", source)
        self.assertIn("New objects outside MD scope", source)
        self.assertIn("TIFF/PSD versus current surrogate", source)
        self.assertIn("Auto-reconciled existing objects", source)
        self.assertIn("Exact companions of new objects", source)
        self.assertIn("polite pages of {page_size}", source)
        self.assertIn("Metadata proposed for this object", source)
        self.assertIn("Object description (editable)", source)
        self.assertIn("Object title (editable)", source)
        self.assertIn('name="curated_title"', source)
        self.assertIn("Object date (editable)", source)
        self.assertIn('name="curated_date_text"', source)
        self.assertIn('name="curated_description"', source)
        self.assertIn("Keywords / tags (editable)", source)
        self.assertIn('name="curated_tags"', source)
        self.assertIn('selected_metadata_basis = "latest curator decision"', source)
        self.assertIn("Mark this file as a Redundant duplicate", source)
        self.assertIn('name="retained_candidate_id"', source)
        self.assertNotIn('name="retained_candidate_id" required', source)
        self.assertNotIn('name="reason" rows="3" required', source)
        self.assertIn("keep drain evidence", source)
        self.assertIn(
            "/kurt-schwarz/object-census-manual-duplicate", source
        )
        self.assertIn(
            "save_manual_object_census_duplicate_decision", source
        )
        self.assertIn("Same object — use TIFF/PSD as Master", source)
        self.assertIn("Primary new-object candidate", source)
        self.assertIn("Belongs to another or future archive unit", source)
        self.assertIn('name="cross_unit_group_key"', source)
        self.assertIn("Capture instruction note", source)
        self.assertIn("does not create the target unit", source)
        self.assertNotIn("Raw spreadsheet row", source)
        self.assertIn("/kurt-schwarz/object-census-decision", source)

    def test_kurt_duplicate_review_requires_pairwise_curator_decisions(self) -> None:
        source = (TOOLS / "core_workbench.py").read_text(encoding="utf-8")
        self.assertIn("/kurt-schwarz/object-census-duplicates", source)
        self.assertIn(
            "/kurt-schwarz/object-census-similarity-decision", source
        )
        self.assertIn("No pair is grouped automatically", source)
        self.assertIn("Same photograph", source)
        self.assertIn("Different photographs", source)
        self.assertIn("Mark {e(left['filename'])} Redundant", source)
        self.assertIn("Mark {e(right['filename'])} Redundant", source)
        self.assertIn('value="redundant_a"', source)
        self.assertIn('value="redundant_b"', source)
        self.assertIn("Marked {companion['filename']} Redundant", source)
        self.assertIn("{primary['filename']} as Master", source)
        self.assertIn(
            "Confirm relationships pairwise or use the explicit Master/Redundant group form",
            source,
        )
        self.assertIn(
            "save_object_census_similarity_decision", source
        )

    def test_kurt_duplicate_group_supports_one_master_and_redundant_files(
        self,
    ) -> None:
        source = (TOOLS / "core_workbench.py").read_text(encoding="utf-8")
        self.assertIn("/kurt-schwarz/object-census-duplicate-group", source)
        self.assertIn(
            "/kurt-schwarz/object-census-similarity-group-decision", source
        )
        self.assertIn("Choose one Master and its Redundant representations", source)
        self.assertIn("select exactly one group Master", source)
        self.assertIn("role__", source)
        self.assertIn(
            "save_object_census_similarity_group_decision", source
        )

    def test_confirmed_group_master_does_not_require_another_master_decision(
        self,
    ) -> None:
        pairs = [
            {
                "decision": {
                    "outcome": "same_photo",
                    "primary_candidate_id": "KSC-MASTER",
                    "companion_candidate_id": "KSC-REDUNDANT",
                }
            }
        ]
        self.assertTrue(
            WORKBENCH.object_census_candidate_is_confirmed_group_master(
                "KSC-MASTER", pairs
            )
        )
        self.assertFalse(
            WORKBENCH.object_census_candidate_is_confirmed_group_master(
                "KSC-REDUNDANT", pairs
            )
        )
        self.assertFalse(
            WORKBENCH.object_census_candidate_is_confirmed_group_master(
                "KSC-PENDING", [{"decision": {"outcome": "defer"}}]
            )
        )
        source = (TOOLS / "core_workbench.py").read_text(encoding="utf-8")
        self.assertIn("Confirmed Master / Redundant group", source)
        self.assertIn("No further Master or copy decision is required", source)
        self.assertIn("New local object metadata", source)
        self.assertIn("and not confirmed_group_master", source)

    def test_kurt_census_integrates_whole_unit_master_matches(self) -> None:
        source = (TOOLS / "core_workbench.py").read_text(encoding="utf-8")
        self.assertIn("Possible retained Master matches", source)
        self.assertIn("Mark this file Redundant under this object", source)
        self.assertIn("Possible new-object copies", source)
        self.assertIn("Redundant copy of an existing Kurt object", source)
        self.assertIn("Move Master and Redundant copies to this object", source)
        self.assertIn(
            "assign_object_census_similarity_group_to_existing_object", source
        )
        self.assertIn("Existing Redundant files and generated", source)
        self.assertNotIn(
            '("object_duplicates", "/kurt-schwarz/object-census-duplicates", "Duplicate review")',
            source,
        )

    def test_wf_sender_decided_record_keeps_reviewed_representation(self) -> None:
        candidates = [
            {"candidate_id": "group:15"},
            {"candidate_id": "group:1812"},
        ]
        decision = {
            "basis": json.dumps({"candidate_id": "group:1812"}),
        }
        self.assertEqual(
            "group:1812",
            WORKBENCH.wf_sender_candidate_selection(
                candidates,
                decision=decision,
            ),
        )
        self.assertEqual(
            "group:15",
            WORKBENCH.wf_sender_candidate_selection(
                candidates,
                requested="group:15",
                decision=decision,
            ),
        )

    def test_completed_recovery_correction_is_reconciled_and_locked(self) -> None:
        self.assertEqual(
            "reconciled",
            WORKBENCH.recovery_correction_queue_state("complete"),
        )
        self.assertEqual(
            "ready",
            WORKBENCH.recovery_correction_queue_state("ready"),
        )
        self.assertEqual(
            "visual_review",
            WORKBENCH.recovery_correction_queue_state("open"),
        )
        self.assertTrue(WORKBENCH.recovery_correction_is_locked("complete"))
        self.assertFalse(WORKBENCH.recovery_correction_is_locked("open"))

    def test_completed_correction_ui_uses_archive_values_and_refuses_edits(
        self,
    ) -> None:
        source = (TOOLS / "core_workbench.py").read_text(encoding="utf-8")
        self.assertIn("Corrective accession complete — metadata locked", source)
        self.assertIn("Reopen correction to edit", source)
        self.assertIn("Historical local source", source)
        self.assertIn("/recovery/active-image", source)
        self.assertIn("require_recovery_record_editable", source)
        self.assertIn("completed corrective accession", source)

    def test_ready_correction_has_guarded_manual_executor(self) -> None:
        source = (TOOLS / "core_workbench.py").read_text(encoding="utf-8")
        self.assertIn('action="/recovery/corrective-publish"', source)
        self.assertIn("Run corrective re-accession", source)
        self.assertIn("verified rollback backup", source)
        self.assertIn("recovery_correction.apply(", source)
        self.assertIn("recovery_accession_lock.acquire(blocking=False)", source)

    def test_fast_visual_confirmation_requires_both_evidence_sides(self) -> None:
        local = {
            "role": "source_record_image",
            "status": "available",
            "cache_relpath": "cache/local.jpg",
        }
        external = {
            "role": "museum_digital_snapshot",
            "status": "available",
            "cache_relpath": "cache/md.jpg",
        }
        self.assertFalse(WORKBENCH.recovery_visual_confirmation_ready([local]))
        self.assertTrue(
            WORKBENCH.recovery_visual_confirmation_ready([local, external])
        )
        self.assertFalse(
            WORKBENCH.recovery_visual_confirmation_ready(
                [local, {**external, "cache_relpath": ""}]
            )
        )

    def test_fast_visual_review_and_batch_queue_are_one_workbench(self) -> None:
        source = (TOOLS / "core_workbench.py").read_text(encoding="utf-8")
        self.assertIn('action="/recovery/visual-confirm"', source)
        self.assertIn(
            "Accept identity, metadata, and use this master",
            source,
        )
        self.assertIn("Ready for accession", source)
        self.assertIn("--batch-accession-dry-run", source)
        self.assertIn("--batch-accession-run", source)

    def test_batch_plan_is_stable_and_selection_bound(self) -> None:
        server = WORKBENCH.CoreWorkbenchServer.__new__(
            WORKBENCH.CoreWorkbenchServer
        )
        server.root = Path("/fixture/root")
        server.recovery_scope = {"unit_id": "WF-PARAGON"}
        server.recovery_records = {"VLV-ONE": {}, "VLV-TWO": {}}
        plans = {
            "VLV-ONE": {
                "inventory": "VLV-ONE",
                "entity_id": "ISS-2026-AAAA-A",
                "unit_id": "WF-PARAGON",
                "state": "ready",
                "master_evidence": {
                    "evidence_id": "VIS-ONE",
                    "content_digest": "sha256:" + "a" * 64,
                    "source_locator": "/protected/VLV-ONE.tif",
                },
                "derivative_evidence": None,
            },
            "VLV-TWO": {
                "inventory": "VLV-TWO",
                "state": "visual_review",
            },
        }
        with mock.patch.object(
            server,
            "recovery_accession_plan",
            side_effect=lambda inventory: plans[inventory],
        ), mock.patch.object(
            WORKBENCH.recovery_metadata,
            "require_embedding_metadata",
            return_value={
                "workbook_record_digest": "sha256:" + "c" * 64,
            },
        ):
            first = server.recovery_batch_plan()
            second = server.recovery_batch_plan()
        self.assertEqual(first["plan_digest"], second["plan_digest"])
        self.assertEqual(1, first["count"])
        self.assertEqual("VLV-ONE", first["items"][0]["inventory"])
        self.assertTrue(first["items"][0]["binding"].startswith("sha256:"))

    def test_batch_run_refuses_changed_selection_binding(self) -> None:
        server = WORKBENCH.CoreWorkbenchServer.__new__(
            WORKBENCH.CoreWorkbenchServer
        )
        plan = {
            "inventory": "VLV-ONE",
            "entity_id": "ISS-2026-AAAA-A",
            "unit_id": "WF-PARAGON",
            "state": "ready",
            "reason": "ready",
            "master_evidence": {
                "evidence_id": "VIS-CHANGED",
                "content_digest": "sha256:" + "b" * 64,
            },
            "derivative_evidence": None,
        }
        with mock.patch.object(
            server, "recovery_accession_plan", return_value=plan
        ), mock.patch.object(
            WORKBENCH.recovery_metadata,
            "require_embedding_metadata",
            return_value={
                "workbook_record_digest": "sha256:" + "c" * 64,
            },
        ):
            with self.assertRaisesRegex(
                RECONCILIATION.ReconciliationError,
                "selection changed",
            ):
                server.accession_recovery_record(
                    "VLV-ONE",
                    expected_binding="sha256:" + "0" * 64,
                )

    def test_single_editor_recovery_accession_has_no_typed_confirmation(self) -> None:
        source = (TOOLS / "core_workbench.py").read_text(encoding="utf-8")
        self.assertNotIn("Type ACCESSION to confirm", source)
        action = source.split(
            "def action_recovery_accession_publish", 1
        )[1].split("\n    def ", 1)[0]
        self.assertNotIn('form.get("confirmation"', action)

    def test_recovery_accession_is_exclusive(self) -> None:
        source = (TOOLS / "core_workbench.py").read_text(encoding="utf-8")
        action = source.split(
            "def action_recovery_accession_publish", 1
        )[1].split("\n    def ", 1)[0]
        self.assertIn("recovery_accession_lock.acquire(blocking=False)", action)
        self.assertIn("recovery_accession_lock.release()", action)

    def test_jpeg_surrogate_copy_is_explained(self) -> None:
        source = (TOOLS / "core_workbench.py").read_text(encoding="utf-8")
        self.assertIn("Missing TIFF does not block accession", source)
        self.assertIn("metadata-enriched preservation-surrogate TIFF", source)
        self.assertIn("Queued for the later accession batch", source)

    def test_jpeg_surrogate_does_not_require_a_second_derivative(self) -> None:
        self.assertTrue(
            WORKBENCH.recovery_derivative_ready(
                "/protected/high-resolution.jpg",
                available_derivatives=1,
                selected_derivatives=0,
            )
        )
        self.assertFalse(
            WORKBENCH.recovery_derivative_ready(
                "/protected/master.tif",
                available_derivatives=1,
                selected_derivatives=0,
            )
        )

    def test_recovery_master_candidate_uses_pixels_before_compressed_bytes(self) -> None:
        large_path = "/protected/raw/VLV-1023.jpg"
        small_path = "/protected/export/VLV-1023.jpg"
        record = {
            "files": [
                {"path": large_path, "size": 120_000},
                {"path": small_path, "size": 180_000},
            ]
        }
        large = {
            "source_locator": large_path,
            "width_px": 719,
            "height_px": 1198,
        }
        small = {
            "source_locator": small_path,
            "width_px": 600,
            "height_px": 800,
        }
        classify = WORKBENCH.CoreWorkbenchHandler._recovery_candidate_role
        self.assertEqual(
            "high_resolution_jpeg_candidate",
            classify(None, record, large, [large, small])[0],
        )
        self.assertEqual(
            "local_derivative",
            classify(None, record, small, [large, small])[0],
        )

    def test_async_save_uses_literal_form_action_attribute(self) -> None:
        script = (TOOLS / "workbench.js").read_text(encoding="utf-8")
        self.assertIn('form.getAttribute("action")', script)
        self.assertNotIn("fetch(form.action", script)

    def test_kurt_media_decision_uses_fast_four_choice_toggles(self) -> None:
        handler = object.__new__(WORKBENCH.CoreWorkbenchHandler)
        handler.server = SimpleNamespace(csrf_token="fixture-token")
        candidate = {
            "candidate_id": "MEDIA-ONE",
            "content_role": "preservation_master",
            "filename": "fixture.tif",
            "size_bytes": 123,
            "content_sha256": "a" * 64,
            "final_path": "/unit/fixture.tif",
            "source_occurrences": [],
            "decision": None,
            "analysis": None,
            "preview_failed": True,
        }
        markup = handler._kurt_media_card("md:1", candidate)
        self.assertIn('data-candidate-id="MEDIA-ONE"', markup)
        self.assertIn('form="kurt-media-review"', markup)
        self.assertIn('name="disposition__MEDIA-ONE"', markup)
        self.assertIn('value="selected_master"', markup)
        self.assertIn('value="redundant"', markup)
        self.assertIn('value="wrong_object"', markup)
        self.assertIn('value="defer"', markup)
        self.assertIn("Master", markup)
        self.assertIn("Decide later", markup)
        self.assertNotIn("Record media decision", markup)
        self.assertNotIn('value="genuine_variant"', markup)
        self.assertNotIn('value="access_derivative"', markup)

        source = (TOOLS / "core_workbench.py").read_text(encoding="utf-8")
        self.assertIn('form="kurt-media-review">Record media decision', source)
        self.assertIn('action="/kurt-schwarz/media-decisions"', source)

    def test_async_media_save_removes_only_the_decided_panel(self) -> None:
        script = (TOOLS / "workbench.js").read_text(encoding="utf-8")
        self.assertIn("response.status === 204", script)
        self.assertIn("removablePanel.remove()", script)
        self.assertIn('nextPanel.focus({ preventScroll: true })', script)
        self.assertIn('selectedDisposition === "selected_master"', script)
        self.assertIn("All other candidates were marked redundant", script)
        self.assertIn('form.dataset.mediaBatchSave === "yes" ? 0', script)

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.root = self.base / "core"
        self.independent = self.base / "independent"
        CORE.initialize_root(
            self.root,
            tool_version="test",
            actor="Fixture Archivist",
        )
        with mock.patch.object(CORE, "assert_independent_device"):
            CORE.publish_generation(
                self.root,
                self.independent,
                tool_version="test",
                actor="Fixture Archivist",
            )
        connection = CORE.connect_catalog(self.root / "work" / "catalog.sqlite")
        try:
            CORE.create_unit(
                connection,
                unit_id="UNIT-ONE",
                title="Fixture unit",
                kind="retrospective",
                actor="Fixture Archivist",
            )
            created = CORE.create_entity(
                connection,
                kind="archive_object",
                canonical_name="Fixture photograph",
                unit_id="UNIT-ONE",
                membership_role="primary",
                actor="Fixture Archivist",
                assignment_year=2026,
                domain={
                    "archival_label": "VLV-1112",
                    "descriptive_title": "Current catalog title",
                },
            )
            self.entity_id = created["entity_id"]
        finally:
            connection.close()
        self.run = self.create_run()
        self.source_fingerprints = {
            path: (
                path.stat().st_size,
                path.stat().st_mtime_ns,
                hashlib.sha256(path.read_bytes()).hexdigest(),
            )
            for path in self.run.rglob("*")
            if path.is_file()
        }
        result = RECONCILIATION.import_museum_digital_run(
            self.root,
            self.run,
            campaign_id="MD-FIXTURE",
            title="Fixture campaign",
        )
        self.assertEqual(1, result["records"])
        self.assertEqual(1, result["linked_entities"])
        with closing(
            RECONCILIATION.connect_campaign(self.root, read_only=True)
        ) as campaign:
            self.record_id = campaign.execute(
                "SELECT record_id FROM reconciliation_records"
            ).fetchone()[0]

    def tearDown(self) -> None:
        self.temp.cleanup()

    def create_run(
        self,
        *,
        source_status: str = "exact_inventory",
        include_local_visual: bool = True,
    ) -> Path:
        run = self.base / f"run-{source_status}-{int(include_local_visual)}"
        (run / "visual").mkdir(parents=True)
        (run / "raw").mkdir()
        image = b"P6\n2 2\n255\n" + bytes(
            (255, 0, 0, 0, 255, 0, 0, 0, 255, 255, 255, 255)
        )
        (run / "visual" / "local.ppm").write_bytes(image)
        (run / "visual" / "md.ppm").write_bytes(image)
        (run / "raw" / "70001.json").write_text(
            '{"object_id":"70001","object_name":"Museum title"}\n',
            encoding="utf-8",
        )
        record = {
            "local_archival_id": self.entity_id,
            "local_inventory": "VLV-1112",
            "museum_digital_id": "70001",
            "museum_digital_url": "https://example.invalid/md/70001",
            "match_status": "exact_inventory",
            "source_record_status": source_status,
            "source_record_file": "/read-only/source.xlsx",
            "source_record_sheet": "Tabelle1",
            "source_record_row": "148",
            "source_title": "Local source title",
            "museum_digital_title": "Museum title",
            "source_description": "Local description",
            "museum_digital_description": "Museum description",
            "source_material_technique": "Photographic paper",
            "museum_digital_material_technique": "Gelatin silver print",
            "source_dimensions": "20 × 30 cm",
            "museum_digital_dimensions": "21 × 29.7 cm",
            "local_object_type": "photograph",
            "source_object_type": "Foto",
            "museum_digital_object_type": "Photograph",
            "source_date": "1987",
            "museum_digital_event_times": "1974",
            "source_image_rights": "unknown",
            "museum_digital_primary_image_rights": "CC BY-NC-SA",
        }
        with (run / "records.csv").open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(record))
            writer.writeheader()
            writer.writerow(record)
        visuals = [
            {
                "image_id": "LOCAL",
                "local_archival_id": self.entity_id,
                "role": "source_record_image",
                "label": "Local source",
                "original_path": "/read-only/local.tif",
                "snapshot_relpath": (
                    "visual/local.ppm" if include_local_visual else ""
                ),
                "evidence_relpath": "",
                "sha256": "a" * 64,
                "width": "2",
                "height": "2",
                "status": "available" if include_local_visual else "missing",
                "detail": "",
            },
            {
                "image_id": "MD",
                "local_archival_id": self.entity_id,
                "role": "museum_digital_snapshot",
                "label": "Museum-digital snapshot",
                "original_path": "https://example.invalid/image.jpg",
                "snapshot_relpath": "visual/md.ppm",
                "evidence_relpath": "",
                "sha256": "b" * 64,
                "width": "2",
                "height": "2",
                "status": "available",
                "detail": "",
            },
        ]
        with (run / "visual-comparison.csv").open(
            "w", encoding="utf-8", newline=""
        ) as handle:
            writer = csv.DictWriter(handle, fieldnames=list(visuals[0]))
            writer.writeheader()
            writer.writerows(visuals)
        return run

    def assert_source_run_unchanged(self) -> None:
        after = {
            path: (
                path.stat().st_size,
                path.stat().st_mtime_ns,
                hashlib.sha256(path.read_bytes()).hexdigest(),
            )
            for path in self.run.rglob("*")
            if path.is_file()
        }
        self.assertEqual(self.source_fingerprints, after)

    def review_identity(self, reason: str = "Local and MD previews show the same photograph.") -> dict:
        with closing(RECONCILIATION.connect_campaign(self.root)) as campaign:
            with closing(CORE.connect_catalog(
                self.root / "work" / "catalog.sqlite"
            )) as catalog:
                return RECONCILIATION.review_identity(
                    campaign,
                    catalog,
                    record_id=self.record_id,
                    outcome="same_entity",
                    actor="Fixture Archivist",
                    reason=reason,
                )

    def test_identity_and_field_reasons_are_optional(self) -> None:
        result = self.review_identity(reason="")
        self.assertEqual("unpublished", result["state"])
        with closing(RECONCILIATION.connect_campaign(self.root)) as campaign:
            with closing(CORE.connect_catalog(
                self.root / "work" / "catalog.sqlite"
            )) as catalog:
                result = RECONCILIATION.decide_field(
                    campaign,
                    catalog,
                    record_id=self.record_id,
                    field_key="archive_description",
                    action="accept_local",
                    actor="Fixture Archivist",
                    reason="",
                )
                self.assertEqual("unpublished", result["state"])
                core_notes = catalog.execute(
                    """SELECT note FROM logbook
                       WHERE action IN (
                           'review_external_identity',
                           'accept_reconciliation_field'
                       )
                       ORDER BY log_id"""
                ).fetchall()
                self.assertEqual(["", ""], [row["note"] for row in core_notes])
        with closing(RECONCILIATION.connect_campaign(
            self.root, read_only=True
        )) as campaign:
            identity_reason = campaign.execute(
                """SELECT reason FROM identity_decisions
                   WHERE record_id = ?""",
                (self.record_id,),
            ).fetchone()[0]
            field_reason = campaign.execute(
                """SELECT reason FROM field_decisions
                   WHERE record_id = ? AND field_key = 'archive_description'""",
                (self.record_id,),
            ).fetchone()[0]
            self.assertEqual("", identity_reason)
            self.assertEqual("", field_reason)

    def test_exact_campaign_retries_are_no_ops(self) -> None:
        first = self.review_identity(reason="")
        second = self.review_identity(reason="")
        self.assertEqual("unpublished", first["state"])
        self.assertEqual("unchanged", second["state"])
        with closing(RECONCILIATION.connect_campaign(self.root)) as campaign:
            with closing(CORE.connect_catalog(
                self.root / "work" / "catalog.sqlite"
            )) as catalog:
                first_field = RECONCILIATION.decide_field(
                    campaign,
                    catalog,
                    record_id=self.record_id,
                    field_key="archive_description",
                    action="accept_local",
                    actor="Fixture Archivist",
                    reason="",
                )
                second_field = RECONCILIATION.decide_field(
                    campaign,
                    catalog,
                    record_id=self.record_id,
                    field_key="archive_description",
                    action="accept_local",
                    actor="Fixture Archivist",
                    reason="",
                )
                self.assertEqual("unpublished", first_field["state"])
                self.assertEqual("unchanged", second_field["state"])
                self.assertEqual(
                    1,
                    campaign.execute(
                        "SELECT count(*) FROM identity_decisions WHERE record_id=?",
                        (self.record_id,),
                    ).fetchone()[0],
                )
                self.assertEqual(
                    1,
                    campaign.execute(
                        """SELECT count(*) FROM field_decisions
                           WHERE record_id=? AND field_key='archive_description'""",
                        (self.record_id,),
                    ).fetchone()[0],
                )

    def test_source_candidate_redundancy_review_is_logged_and_reversible(self) -> None:
        digest = "sha256:" + "d" * 64
        with closing(CORE.connect_catalog(
            self.root / "work" / "catalog.sqlite"
        )) as catalog:
            first = CORE.review_source_candidate(
                catalog,
                entity_id=self.entity_id,
                evidence_id="VIS-LOCAL-CANDIDATE",
                content_digest=digest,
                candidate_role="local_derivative",
                source_name="VLV-1112.jpg",
                state="redundant",
                actor="Fixture Archivist",
                reason="Same content is retained at higher quality.",
            )
            repeated = CORE.review_source_candidate(
                catalog,
                entity_id=self.entity_id,
                evidence_id="VIS-LOCAL-CANDIDATE",
                content_digest=digest,
                candidate_role="local_derivative",
                source_name="VLV-1112.jpg",
                state="redundant",
                actor="Fixture Archivist",
                reason="Same content is retained at higher quality.",
            )
            restored = CORE.review_source_candidate(
                catalog,
                entity_id=self.entity_id,
                evidence_id="VIS-LOCAL-CANDIDATE",
                content_digest=digest,
                candidate_role="local_derivative",
                source_name="VLV-1112.jpg",
                state="candidate",
                actor="Fixture Archivist",
                reason="Retain for derivative comparison.",
            )
            self.assertEqual("reviewed", first["operation_state"])
            self.assertEqual("unchanged", repeated["operation_state"])
            self.assertEqual("reviewed", restored["operation_state"])
            latest = CORE.source_candidate_reviews(
                catalog, entity_id=self.entity_id
            )["VIS-LOCAL-CANDIDATE"]
            self.assertEqual("candidate", latest["state"])
            self.assertEqual(
                2,
                catalog.execute(
                    """SELECT count(*) FROM logbook
                       WHERE action='review_source_candidate'"""
                ).fetchone()[0],
            )

    def test_source_candidate_master_and_derivative_selections_are_exclusive(self) -> None:
        with closing(CORE.connect_catalog(
            self.root / "work" / "catalog.sqlite"
        )) as catalog:
            master = CORE.review_source_candidate(
                catalog,
                entity_id=self.entity_id,
                evidence_id="VIS-MASTER",
                content_digest="sha256:" + "a" * 64,
                candidate_role="high_resolution_jpeg_candidate",
                source_name="VLV-1112-large.jpg",
                state="selected_master",
                actor="Fixture Archivist",
            )
            derivative = CORE.review_source_candidate(
                catalog,
                entity_id=self.entity_id,
                evidence_id="VIS-DERIVATIVE",
                content_digest="sha256:" + "b" * 64,
                candidate_role="local_derivative",
                source_name="VLV-1112.jpg",
                state="selected_derivative",
                actor="Fixture Archivist",
            )
            self.assertEqual("selected_master", master["state"])
            self.assertEqual("selected_derivative", derivative["state"])
            with self.assertRaisesRegex(
                CORE.CoreCatalogError, "another source candidate"
            ):
                CORE.review_source_candidate(
                    catalog,
                    entity_id=self.entity_id,
                    evidence_id="VIS-SECOND-MASTER",
                    content_digest="sha256:" + "c" * 64,
                    candidate_role="master_tiff_candidate",
                    source_name="other.tif",
                    state="selected_master",
                    actor="Fixture Archivist",
                )

    def test_tiff_master_policy_retires_supplied_derivatives_atomically(self) -> None:
        master = {
            "evidence_id": "VIS-TIFF",
            "content_digest": "sha256:" + "a" * 64,
            "candidate_role": "master_tiff_candidate",
            "source_name": "VLV-1112.tif",
        }
        others = [
            {
                "evidence_id": "VIS-HIRES",
                "content_digest": "sha256:" + "b" * 64,
                "candidate_role": "high_resolution_jpeg_candidate",
                "source_name": "VLV-1112-large.jpg",
            },
            {
                "evidence_id": "VIS-SMALL",
                "content_digest": "sha256:" + "c" * 64,
                "candidate_role": "local_derivative",
                "source_name": "VLV-1112.jpg",
            },
        ]
        with closing(CORE.connect_catalog(
            self.root / "work" / "catalog.sqlite"
        )) as catalog:
            result = CORE.select_tiff_master_with_generated_derivative(
                catalog,
                entity_id=self.entity_id,
                master_candidate=master,
                other_candidates=others,
                actor="Fixture Archivist",
            )
            repeated = CORE.select_tiff_master_with_generated_derivative(
                catalog,
                entity_id=self.entity_id,
                master_candidate=master,
                other_candidates=others,
                actor="Fixture Archivist",
            )
            reviews = CORE.source_candidate_reviews(
                catalog, entity_id=self.entity_id
            )
            self.assertEqual("reviewed", result["operation_state"])
            self.assertEqual("unchanged", repeated["operation_state"])
            self.assertEqual("selected_master", reviews["VIS-TIFF"]["state"])
            self.assertEqual("redundant", reviews["VIS-HIRES"]["state"])
            self.assertEqual("redundant", reviews["VIS-SMALL"]["state"])
            with self.assertRaisesRegex(
                CORE.CoreCatalogError, "uniformly generated archive derivative"
            ):
                CORE.review_source_candidate(
                    catalog,
                    entity_id=self.entity_id,
                    evidence_id="VIS-SMALL",
                    content_digest="sha256:" + "c" * 64,
                    candidate_role="local_derivative",
                    source_name="VLV-1112.jpg",
                    state="selected_derivative",
                    actor="Fixture Archivist",
                )
            self.assertEqual(
                3,
                catalog.execute(
                    """SELECT count(*) FROM logbook
                       WHERE action='review_source_candidate'"""
                ).fetchone()[0],
            )
            with self.assertRaisesRegex(
                CORE.CoreCatalogError, "only a local derivative"
            ):
                CORE.review_source_candidate(
                    catalog,
                    entity_id=self.entity_id,
                    evidence_id="VIS-WRONG-ROLE",
                    content_digest="sha256:" + "d" * 64,
                    candidate_role="high_resolution_jpeg_candidate",
                    source_name="large.jpg",
                    state="selected_derivative",
                    actor="Fixture Archivist",
                )

    def test_editor_reconciliation_persists_tags_with_optional_reason(self) -> None:
        with closing(
            CORE.connect_catalog(self.root / "work" / "catalog.sqlite")
        ) as catalog:
            result = editor_reconcile.reconcile_metadata(
                catalog,
                unit_id="UNIT-ONE",
                entity_id=self.entity_id,
                field_decisions={
                    "title": {
                        "source": "museum_digital",
                        "value": "Accepted museum title",
                    },
                    "object_type": {
                        "source": "local",
                        "value": "Photograph",
                    },
                },
                tag_decision={
                    "source": "museum_digital",
                    "values": ["Industry", "Factory", "industry"],
                },
                actor="Fixture Archivist",
                reason="",
                evidence="sha256:" + "a" * 64,
                external_id="70001",
            )
            self.assertEqual("", result["reason"])
            self.assertEqual(["Industry", "Factory"], result["tags"])
            title = catalog.execute(
                """SELECT descriptive_title FROM archive_objects
                   WHERE entity_id = ?""",
                (self.entity_id,),
            ).fetchone()[0]
            self.assertEqual("Accepted museum title", title)
            accepted = editor_reconcile.accepted_metadata(
                catalog,
                self.entity_id,
            )
            self.assertEqual("Photograph", accepted["object_type"])
            self.assertEqual(["Factory", "Industry"], accepted["tags"])
            reason = catalog.execute(
                """SELECT note FROM logbook
                   WHERE action = 'editor_reconcile_metadata'
                   ORDER BY at_utc DESC LIMIT 1"""
            ).fetchone()[0]
            self.assertEqual("", reason)
            repeated = editor_reconcile.reconcile_metadata(
                catalog,
                unit_id="UNIT-ONE",
                entity_id=self.entity_id,
                field_decisions={
                    "title": {
                        "source": "museum_digital",
                        "value": "Accepted museum title",
                    },
                    "object_type": {
                        "source": "local",
                        "value": "Photograph",
                    },
                },
                tag_decision={
                    "source": "museum_digital",
                    "values": ["Industry", "Factory", "industry"],
                },
                actor="Fixture Archivist",
                reason="",
                evidence="sha256:" + "a" * 64,
                external_id="70001",
            )
            self.assertEqual("unchanged", repeated["state"])
            self.assertEqual(
                1,
                catalog.execute(
                    """SELECT count(*) FROM logbook
                       WHERE action='editor_reconcile_metadata'"""
                ).fetchone()[0],
            )

    def test_import_visual_review_and_field_choices_reach_core_guardedly(self) -> None:
        result = self.review_identity()
        self.assertEqual("unpublished", result["state"])
        self.assert_source_run_unchanged()

        with closing(CORE.connect_catalog(
            self.root / "work" / "catalog.sqlite"
        )) as catalog:
            alias = catalog.execute(
                """SELECT entity_id FROM entity_identifiers
                   WHERE namespace = 'museum-digital'
                     AND normalized_value = '70001'
                     AND status = 'active'"""
            ).fetchone()
            self.assertEqual(self.entity_id, alias["entity_id"])
            identity_log = catalog.execute(
                """SELECT after_json FROM logbook
                   WHERE action = 'review_external_identity'"""
            ).fetchone()
            self.assertIsNotNone(identity_log)

        choices = (
            (
                "archive_descriptive_title",
                "keep_catalog",
                "",
                "The current catalog title is already the preferred title.",
            ),
            (
                "archive_description",
                "accept_local",
                "",
                "The preserved source row is more precise.",
            ),
            (
                "archive_material_technique",
                "accept_external",
                "",
                "Museum-digital supplies the more specific material.",
            ),
            (
                "archive_dimensions",
                "corrected",
                "20.0 × 30.0 cm",
                "Normalized punctuation without changing the measured fact.",
            ),
        )
        for field_key, action, corrected, reason in choices:
            with closing(RECONCILIATION.connect_campaign(self.root)) as campaign:
                with closing(CORE.connect_catalog(
                    self.root / "work" / "catalog.sqlite"
                )) as catalog:
                    result = RECONCILIATION.decide_field(
                        campaign,
                        catalog,
                        record_id=self.record_id,
                        field_key=field_key,
                        action=action,
                        actor="Fixture Archivist",
                        reason=reason,
                        corrected_value=corrected,
                    )
                    self.assertEqual("unpublished", result["state"])

        with closing(RECONCILIATION.connect_campaign(
            self.root, read_only=True
        )) as campaign:
            record = campaign.execute(
                """SELECT field_status, queue_status
                   FROM reconciliation_records WHERE record_id = ?""",
                (self.record_id,),
            ).fetchone()
            self.assertEqual(("complete", "complete"), tuple(record))
        with closing(CORE.connect_catalog(
            self.root / "work" / "catalog.sqlite", read_only=True
        )) as catalog:
            row = catalog.execute(
                """SELECT descriptive_title, description, material_technique,
                          dimensions
                   FROM archive_objects WHERE entity_id = ?""",
                (self.entity_id,),
            ).fetchone()
            self.assertEqual(
                (
                    "Current catalog title",
                    "Local description",
                    "Gelatin silver print",
                    "20.0 × 30.0 cm",
                ),
                tuple(row),
            )
            self.assertEqual(
                4,
                catalog.execute(
                    """SELECT count(*) FROM evidence_assertions
                       WHERE subject_entity_id = ? AND status = 'accepted'""",
                    (self.entity_id,),
                ).fetchone()[0],
            )
            state = CORE.catalog_state(catalog)
            self.assertGreater(
                int(state["catalog_revision"]),
                int(state["published_revision"]),
            )
        self.assert_source_run_unchanged()

    def test_campaign_import_can_be_bounded_to_one_requested_record(self) -> None:
        filtered_run = self.base / "two-record-run"
        shutil.copytree(self.run, filtered_run)
        records_path = filtered_run / "records.csv"
        with records_path.open(encoding="utf-8", newline="") as handle:
            rows = list(csv.DictReader(handle))
        other = dict(rows[0])
        other.update(
            {
                "local_archival_id": "LEGACY-OTHER",
                "local_inventory": "VLV-OTHER",
                "museum_digital_id": "70002",
                "museum_digital_url": "https://example.invalid/md/70002",
            }
        )
        with records_path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows((rows[0], other))

        result = RECONCILIATION.import_museum_digital_run(
            self.root,
            filtered_run,
            campaign_id="MD-FILTERED",
            record_keys=("VLV-1112",),
        )
        self.assertEqual(1, result["records"])
        self.assertEqual(["vlv-1112"], result["record_filter"])
        with closing(
            RECONCILIATION.connect_campaign(self.root, read_only=True)
        ) as campaign:
            imported = campaign.execute(
                """SELECT inventory_label
                   FROM reconciliation_records
                   WHERE campaign_id = 'MD-FILTERED'"""
            ).fetchall()
        self.assertEqual(["VLV-1112"], [row["inventory_label"] for row in imported])

    def test_attach_preview_verifies_source_digest_and_writes_only_cache(self) -> None:
        source = self.run / "visual" / "local.ppm"
        source_before = (
            source.stat().st_size,
            source.stat().st_mtime_ns,
            hashlib.sha256(source.read_bytes()).hexdigest(),
        )
        with closing(RECONCILIATION.connect_campaign(self.root)) as campaign:
            evidence_id = campaign.execute(
                """SELECT evidence_id FROM visual_evidence
                   WHERE role = 'source_record_image' LIMIT 1"""
            ).fetchone()[0]
            campaign.execute(
                """UPDATE visual_evidence
                   SET content_digest = ?, cache_relpath = '',
                       status = 'preview_unavailable'
                   WHERE evidence_id = ?""",
                (
                    f"sha256:{hashlib.sha256(source.read_bytes()).hexdigest()}",
                    evidence_id,
                ),
            )
            campaign.commit()

        def fake_magick(command: list[str], **_arguments: object) -> SimpleNamespace:
            destination = Path(command[-1].removeprefix("jpg:"))
            destination.write_bytes(b"fixture-jpeg")
            return SimpleNamespace(returncode=0, stderr="")

        with mock.patch.object(
            RECONCILIATION.shutil,
            "which",
            return_value="/fixture/magick",
        ), mock.patch.object(
            RECONCILIATION.subprocess,
            "run",
            side_effect=fake_magick,
        ):
            result = RECONCILIATION.attach_visual_preview(
                self.root,
                evidence_id=evidence_id,
                source_path=source,
            )
        self.assertEqual("ok", result["result"])
        self.assertTrue(Path(result["preview"]).is_file())
        source_after = (
            source.stat().st_size,
            source.stat().st_mtime_ns,
            hashlib.sha256(source.read_bytes()).hexdigest(),
        )
        self.assertEqual(source_before, source_after)

    def test_identity_decision_is_idempotent(self) -> None:
        first = self.review_identity()
        with closing(CORE.connect_catalog(
            self.root / "work" / "catalog.sqlite"
        )) as catalog:
            aliases_before = catalog.execute(
                "SELECT count(*) FROM entity_identifiers"
            ).fetchone()[0]
            sources_before = catalog.execute(
                "SELECT count(*) FROM evidence_sources"
            ).fetchone()[0]
            second = CORE.review_external_identity(
                catalog,
                decision_id=first["decision_id"],
                entity_id=self.entity_id,
                external_system="museum-digital",
                external_id="70001",
                outcome="same_entity",
                actor="Fixture Archivist",
                reason="Retry after an uncertain response.",
                captured_at=first.get("captured_at"),
                snapshot_digest="",
            )
            aliases_after = catalog.execute(
                "SELECT count(*) FROM entity_identifiers"
            ).fetchone()[0]
            sources_after = catalog.execute(
                "SELECT count(*) FROM evidence_sources"
            ).fetchone()[0]
        self.assertEqual("unchanged", second["state"])
        self.assertEqual(aliases_before, aliases_after)
        self.assertEqual(sources_before, sources_after)

    def test_campaign_sweep_reports_cached_core_drift_as_yellow(self) -> None:
        self.review_identity()
        with closing(
            RECONCILIATION.connect_campaign(self.root)
        ) as campaign, closing(
            CORE.connect_catalog(
                self.root / "work" / "catalog.sqlite",
                read_only=True,
            )
        ) as catalog:
            health = RECONCILIATION.campaign_health(campaign, catalog)
            self.assertEqual("safe", health["status"])
            campaign.execute(
                """UPDATE identity_decisions
                   SET core_state = 'reviewed'
                   WHERE record_id = ?""",
                (self.record_id,),
            )
            campaign.commit()
            health = RECONCILIATION.campaign_health(campaign, catalog)
            self.assertEqual("yellow", health["status"])
            self.assertEqual(
                {"cached_core_state_mismatch"},
                {issue["code"] for issue in health["issues"]},
            )

    def test_not_supplied_never_activates_external_metadata_fallback(self) -> None:
        other = self.create_run(source_status="not_supplied")
        result = RECONCILIATION.import_museum_digital_run(
            self.root,
            other,
            campaign_id="MD-NOT-SUPPLIED",
        )
        self.assertEqual(1, result["records"])
        with closing(RECONCILIATION.connect_campaign(self.root)) as campaign:
            record_id = campaign.execute(
                """SELECT record_id FROM reconciliation_records
                   WHERE campaign_id = 'MD-NOT-SUPPLIED'"""
            ).fetchone()[0]
            with closing(CORE.connect_catalog(
                self.root / "work" / "catalog.sqlite"
            )) as catalog:
                RECONCILIATION.review_identity(
                    campaign,
                    catalog,
                    record_id=record_id,
                    outcome="same_entity",
                    actor="Fixture Archivist",
                    reason="The visual identity is resolved.",
                )
        with closing(RECONCILIATION.connect_campaign(self.root)) as campaign:
            with closing(CORE.connect_catalog(
                self.root / "work" / "catalog.sqlite"
            )) as catalog:
                with self.assertRaisesRegex(
                    CORE.CoreCatalogError,
                    "completed not-found search",
                ):
                    RECONCILIATION.decide_field(
                        campaign,
                        catalog,
                        record_id=record_id,
                        field_key="archive_description",
                        action="accept_external",
                        actor="Fixture Archivist",
                        reason="This must be refused.",
                    )

    def test_missing_visual_evidence_blocks_accepted_identity(self) -> None:
        other = self.create_run(include_local_visual=False)
        RECONCILIATION.import_museum_digital_run(
            self.root,
            other,
            campaign_id="MD-MISSING-VISUAL",
        )
        with closing(RECONCILIATION.connect_campaign(self.root)) as campaign:
            record_id = campaign.execute(
                """SELECT record_id FROM reconciliation_records
                   WHERE campaign_id = 'MD-MISSING-VISUAL'"""
            ).fetchone()[0]
            with closing(CORE.connect_catalog(
                self.root / "work" / "catalog.sqlite"
            )) as catalog:
                with self.assertRaisesRegex(
                    RECONCILIATION.ReconciliationError,
                    "visual evidence",
                ):
                    RECONCILIATION.review_identity(
                        campaign,
                        catalog,
                        record_id=record_id,
                        outcome="same_entity",
                        actor="Fixture Archivist",
                        reason="Insufficient evidence.",
                    )

    def test_exact_local_record_can_resolve_without_external_identity(self) -> None:
        with closing(RECONCILIATION.connect_campaign(self.root)) as campaign:
            campaign.execute(
                """UPDATE reconciliation_records
                   SET local_archival_id='VLV-1112', external_id='',
                       external_url='', source_record_status='exact_inventory',
                       identity_status='blocked', field_status='blocked'
                   WHERE record_id=?""",
                (self.record_id,),
            )
            campaign.execute(
                """DELETE FROM visual_evidence
                   WHERE record_id=? AND role='museum_digital_snapshot'""",
                (self.record_id,),
            )
            campaign.commit()
            with closing(CORE.connect_catalog(
                self.root / "work" / "catalog.sqlite"
            )) as catalog:
                result = RECONCILIATION.review_local_identity(
                    campaign,
                    catalog,
                    record_id=self.record_id,
                    outcome="same_entity",
                    actor="Fixture Archivist",
                    reason="Exact local record and image are present.",
                )
                self.assertEqual("unpublished", result["state"])
                self.assertEqual(
                    "review_local_identity",
                    catalog.execute(
                        """SELECT action FROM logbook
                           WHERE action='review_local_identity'
                           ORDER BY at_utc DESC LIMIT 1""",
                    ).fetchone()[0],
                )
                metadata = RECONCILIATION.decide_field(
                    campaign,
                    catalog,
                    record_id=self.record_id,
                    field_key="archive_descriptive_title",
                    action="accept_local",
                    actor="Fixture Archivist",
                    reason="Use the exact local source title.",
                )
                self.assertEqual("unpublished", metadata["state"])
                self.assertEqual(
                    "Local source title",
                    catalog.execute(
                        """SELECT descriptive_title FROM archive_objects
                           WHERE entity_id=?""",
                        (self.entity_id,),
                    ).fetchone()[0],
                )
            record = campaign.execute(
                """SELECT identity_status, field_status
                   FROM reconciliation_records WHERE record_id=?""",
                (self.record_id,),
            ).fetchone()
            self.assertEqual(("resolved", "in_progress"), tuple(record))

    def test_local_identity_requires_local_metadata(self) -> None:
        with closing(RECONCILIATION.connect_campaign(self.root)) as campaign:
            campaign.execute(
                """UPDATE reconciliation_records
                   SET local_archival_id='VLV-1112', external_id='',
                       source_record_status='exact_inventory'
                   WHERE record_id=?""",
                (self.record_id,),
            )
            campaign.execute(
                """UPDATE field_candidates SET status='missing', value_text=''
                   WHERE record_id=? AND source_side='local'""",
                (self.record_id,),
            )
            campaign.commit()
            with closing(CORE.connect_catalog(
                self.root / "work" / "catalog.sqlite"
            )) as catalog:
                with self.assertRaisesRegex(
                    RECONCILIATION.ReconciliationError,
                    "local image and metadata evidence",
                ):
                    RECONCILIATION.review_local_identity(
                        campaign,
                        catalog,
                        record_id=self.record_id,
                        outcome="same_entity",
                        actor="Fixture Archivist",
                        reason="Insufficient local evidence.",
                    )

    def test_source_only_record_can_resolve_from_selected_inventory_source(self) -> None:
        with closing(RECONCILIATION.connect_campaign(self.root)) as campaign:
            campaign.execute(
                """UPDATE reconciliation_records
                   SET local_archival_id=?, external_id='', external_url='',
                       source_record_status='not_supplied',
                       identity_status='blocked', field_status='blocked'
                   WHERE record_id=?""",
                (self.entity_id, self.record_id),
            )
            campaign.execute(
                """UPDATE field_candidates SET status='missing', value_text=''
                   WHERE record_id=? AND source_side='local'""",
                (self.record_id,),
            )
            campaign.execute(
                """DELETE FROM visual_evidence
                   WHERE record_id=? AND role='museum_digital_snapshot'""",
                (self.record_id,),
            )
            local = campaign.execute(
                """SELECT evidence_id, content_digest FROM visual_evidence
                   WHERE record_id=? AND role<>'museum_digital_snapshot'
                   LIMIT 1""",
                (self.record_id,),
            ).fetchone()
            campaign.execute(
                """UPDATE visual_evidence
                   SET source_locator='/read-only/VLV-1112.tif'
                   WHERE evidence_id=?""",
                (local["evidence_id"],),
            )
            campaign.commit()
            with closing(CORE.connect_catalog(
                self.root / "work" / "catalog.sqlite"
            )) as catalog:
                CORE.review_source_candidate(
                    catalog,
                    entity_id=self.entity_id,
                    evidence_id=str(local["evidence_id"]),
                    content_digest=str(local["content_digest"]),
                    candidate_role="master_tiff_candidate",
                    source_name="VLV-1112.tif",
                    state="selected_master",
                    actor="Fixture Archivist",
                    reason="Selected exact-inventory preservation source.",
                )
                RECONCILIATION.review_local_identity(
                    campaign,
                    catalog,
                    record_id=self.record_id,
                    outcome="same_entity",
                    actor="Fixture Archivist",
                    reason="Filename inventory and selected source agree.",
                )
            record = campaign.execute(
                """SELECT local_archival_id, source_record_status,
                          identity_status, field_status
                   FROM reconciliation_records WHERE record_id=?""",
                (self.record_id,),
            ).fetchone()
            self.assertEqual(
                (self.entity_id, "not_supplied", "resolved", "pending"),
                tuple(record),
            )

    def test_new_workbench_serves_core_and_visual_review_pages(self) -> None:
        pdf_dir = self.root / "work" / "reconciliation" / "pdf"
        pdf_dir.mkdir(parents=True)
        pdf_plan = {
            "format": "iss-pdf-reconciliation-v1",
            "unit_id": "UNIT-ONE",
            "entity_id": self.entity_id,
            "archival_label": "WFS-1971-01",
            "catalog_revision": 7,
            "selected_candidate": "local:1",
            "selection_note": "",
            "museum_digital": {
                "object_id": "86406",
                "inventory_number": "WFS-1971-01",
                "title": "Betriebszeitung",
            },
            "indexing_branch": {"ocr_execution": "not_implemented"},
            "candidates": [
                {
                    "candidate_id": "local:1",
                    "source_kind": "local",
                    "inspection": {
                        "pages": 8,
                        "size_bytes": 1000,
                        "sha256": "a" * 64,
                        "text_status": "ocr_assessment_needed",
                    },
                }
            ],
        }
        plan_name = "UNIT-ONE-fixture.plan.json"
        (pdf_dir / plan_name).write_text(
            json.dumps(pdf_plan),
            encoding="utf-8",
        )
        server = WORKBENCH.CoreWorkbenchServer(
            ("127.0.0.1", 0),
            self.root,
            "Fixture Archivist",
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            connection = http.client.HTTPConnection(
                "127.0.0.1", server.server_port, timeout=5
            )
            connection.request("GET", "/")
            response = connection.getresponse()
            body = response.read()
            self.assertEqual(200, response.status)
            self.assertIn(b"Archive workbench", body)
            self.assertIn(b"Unpublished changes", body)

            connection.request(
                "GET",
                f"/reconcile/record?id={urllib.parse.quote(self.record_id)}",
            )
            response = connection.getresponse()
            body = response.read()
            self.assertEqual(200, response.status)
            self.assertIn(b"Local source", body)
            self.assertIn(b"Museum-digital snapshot", body)
            self.assertIn(b"Do these images show the same archive item?", body)
            self.assertIn(b"Local and external imagery", body)
            self.assertIn(b"Add a reason", body)
            self.assertIn(b"(optional)", body)
            self.assertNotIn(b'name="reason" required', body)

            connection.request("GET", "/pdfs")
            response = connection.getresponse()
            body = response.read()
            self.assertEqual(200, response.status)
            self.assertIn(b"PDF reconciliation", body)
            self.assertIn(b"WFS-1971-01", body)

            connection.request(
                "GET",
                f"/pdf?plan={urllib.parse.quote(plan_name)}",
            )
            response = connection.getresponse()
            body = response.read()
            self.assertEqual(200, response.status)
            self.assertIn(b"Candidate comparison", body)
            self.assertIn(b"ocr_assessment_needed", body)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_stress_editor_mode_hides_technical_evidence_from_comparison(self) -> None:
        digest = "sha256:" + "c" * 64
        relative = object_relative_path(
            unit_id="UNIT-ONE",
            inventory="VLV-1112",
            entity_id=self.entity_id,
            role="preservation_master",
            suffix=".tif",
        ).as_posix()
        master = self.root / relative
        master.parent.mkdir(parents=True)
        master.write_bytes(b"fixture-tiff")
        with closing(
            CORE.connect_catalog(self.root / "work" / "catalog.sqlite")
        ) as catalog:
            catalog.execute(
                """INSERT INTO content_blobs (
                       content_digest, size_bytes, media_type, width_px,
                       height_px, bit_depth, color_space, compression,
                       page_count, duration_ms, created_at
                   ) VALUES (?, 1024, 'image/tiff', 100, 80, 8, 'RGB',
                             'Uncompressed', 1, NULL, ?)""",
                (digest, "2026-07-18T00:00:00Z"),
            )
            catalog.execute(
                """INSERT INTO file_occurrences (
                       occurrence_id, unit_id, batch_id, content_digest,
                       occurrence_kind, source_root_id, relative_path,
                       original_filename, observed_mtime_utc,
                       observed_at_utc, status, note
                   ) VALUES (
                       'OCC-EDITOR-FIXTURE', 'UNIT-ONE', NULL, ?,
                       'archive', '', ?, 'master.tif', '', ?, 'verified', ''
                   )""",
                (digest, relative, "2026-07-18T00:00:00Z"),
            )
            catalog.execute(
                """INSERT INTO entity_files (
                       entity_id, occurrence_id, role, status, created_at
                   ) VALUES (?, 'OCC-EDITOR-FIXTURE',
                             'preservation_master', 'active', ?)""",
                (self.entity_id, "2026-07-18T00:00:00Z"),
            )
            catalog.commit()

        retained = self.base / "retained.sqlite"
        index = sqlite3.connect(retained)
        index.executescript(
            """
            CREATE TABLE objects (
                inventory_number TEXT PRIMARY KEY,
                title TEXT, description TEXT, object_type TEXT,
                collection TEXT, preferred_source TEXT
            );
            CREATE TABLE online_md_objects (
                md_id TEXT PRIMARY KEY,
                object_name TEXT, object_description TEXT, object_type TEXT,
                tags_json TEXT, raw_json TEXT, url TEXT
            );
            CREATE TABLE online_md_images (
                id INTEGER PRIMARY KEY,
                md_id TEXT, quell_id TEXT, name TEXT, is_main TEXT,
                folder TEXT, filename_loc TEXT, rights TEXT,
                owner TEXT, creator TEXT
            );
            CREATE TABLE files (
                id INTEGER PRIMARY KEY,
                path TEXT, basename TEXT, size INTEGER, extension TEXT
            );
            CREATE TABLE file_object_links (
                file_id INTEGER, inventory_number TEXT, confidence INTEGER
            );
            """
        )
        index.execute(
            """INSERT INTO objects VALUES (
                   'VLV-1112', 'Local title', 'Local description',
                   'Foto', 'Fixture', '/read-only/source.xlsx'
               )"""
        )
        index.execute(
            """INSERT INTO online_md_objects VALUES (
                   '70001', 'Museum title', 'Museum description',
                   'Photograph',
                   '[{"tag_name":"Industry"},{"tag_name":"Factory"}]',
                   '{"object_material_technique":"Gelatin silver print",
                     "object_dimensions":"20 x 30 cm"}',
                   'https://example.invalid/70001'
               )"""
        )
        index.commit()
        index.close()
        stat = retained.stat()
        manifest = {
            "format": "iss-provenance-stress-pilot-v1",
            "pilot_root": str(self.root),
            "retained_index": {
                "path": str(retained),
                "device": stat.st_dev,
                "inode": stat.st_ino,
                "size_bytes": stat.st_size,
                "mtime_ns": stat.st_mtime_ns,
            },
            "cases": [
                {
                    "case_id": "STRESS-EDITOR",
                    "unit_id": "UNIT-ONE",
                    "inventory_label": "VLV-1112",
                    "museum_digital_id": "70001",
                    "scenario": "local_master_to_md",
                    "action": "tiff_master",
                    "source_path": "",
                }
            ],
        }
        manifest_path = self.base / "stress.json"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        server = WORKBENCH.CoreWorkbenchServer(
            ("127.0.0.1", 0),
            self.root,
            "Fixture Archivist",
            manifest_path,
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            connection = http.client.HTTPConnection(
                "127.0.0.1", server.server_port, timeout=5
            )
            connection.request("GET", "/browse")
            response = connection.getresponse()
            browse = response.read()
            self.assertEqual(200, response.status)
            self.assertIn(b"Browse archive", browse)
            self.assertIn(b"VLV-1112", browse)
            self.assertNotIn(b">Create<", browse)
            self.assertNotIn(b">Publication<", browse)

            connection.request("GET", "/compare?unit=UNIT-ONE")
            response = connection.getresponse()
            compare = response.read()
            self.assertEqual(200, response.status)
            self.assertIn(b"Master TIFF", compare)
            self.assertIn(b"master.tif", compare)
            self.assertIn(b"1.0 KB", compare)
            self.assertIn(b"100 \xc3\x97 80 px", compare)
            self.assertIn(b"Industry, Factory", compare)
            self.assertIn(b"Admin technical evidence", compare)
            self.assertIn(b"Save reconciliation", compare)
            self.assertIn(b"Reason for change", compare)
            self.assertNotIn(b'name="reason" required', compare)
            self.assertNotIn(digest.encode(), compare)
            self.assertNotIn(relative.encode(), compare)

            connection.request("GET", "/admin?unit=UNIT-ONE")
            response = connection.getresponse()
            admin = response.read()
            self.assertEqual(200, response.status)
            self.assertIn(digest.encode(), admin)
            self.assertIn(relative.encode(), admin)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_visual_candidates_are_editor_cards_and_admin_evidence(self) -> None:
        retained = self.base / "candidate-retained.sqlite"
        index = sqlite3.connect(retained)
        index.executescript(
            """
            CREATE TABLE objects (
                inventory_number TEXT PRIMARY KEY,
                title TEXT, description TEXT, object_type TEXT,
                collection TEXT, preferred_source TEXT
            );
            CREATE TABLE online_md_objects (
                md_id TEXT PRIMARY KEY,
                object_name TEXT, object_description TEXT, object_type TEXT,
                tags_json TEXT, raw_json TEXT, url TEXT
            );
            CREATE TABLE online_md_images (
                id INTEGER PRIMARY KEY,
                md_id TEXT, quell_id TEXT, name TEXT, is_main TEXT,
                folder TEXT, filename_loc TEXT, rights TEXT,
                owner TEXT, creator TEXT
            );
            CREATE TABLE files (
                id INTEGER PRIMARY KEY,
                path TEXT, basename TEXT, size INTEGER, extension TEXT
            );
            CREATE TABLE file_object_links (
                file_id INTEGER, inventory_number TEXT, confidence INTEGER
            );
            """
        )
        index.execute(
            """INSERT INTO objects VALUES (
                   'VLV-1112', 'Local title', 'Local description',
                   'Foto', 'Fixture', '/read-only/source.xlsx'
               )"""
        )
        index.commit()
        index.close()
        stat = retained.stat()
        manifest = {
            "format": "iss-provenance-stress-pilot-v1",
            "pilot_root": str(self.root),
            "retained_index": {
                "path": str(retained),
                "device": stat.st_dev,
                "inode": stat.st_ino,
                "size_bytes": stat.st_size,
                "mtime_ns": stat.st_mtime_ns,
            },
            "cases": [
                {
                    "case_id": "STRESS-EDITOR",
                    "unit_id": "UNIT-ONE",
                    "inventory_label": "VLV-1112",
                    "museum_digital_id": "",
                    "scenario": "missing_master",
                    "action": "metadata_only",
                    "source_path": "",
                }
            ],
        }
        manifest_path = self.base / "candidate-stress.json"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        protected_root = self.base / "protected"
        protected_root.mkdir()
        protected_source = protected_root / "master-candidate.tif"
        subprocess.run(
            [
                "magick",
                "-size",
                "1200x800",
                "xc:#6f7f68",
                "-depth",
                "8",
                "-compress",
                "none",
                str(protected_source),
            ],
            check=True,
            capture_output=True,
        )
        protected_stat = protected_source.stat()
        protected_path = str(protected_source)
        alternate_path = str(protected_root / "mirror-candidate.tiff")
        surrogate_relative = object_relative_path(
            unit_id="UNIT-ONE",
            inventory="VLV-1112",
            entity_id=self.entity_id,
            role="preservation_surrogate",
            suffix=".tif",
        ).as_posix()
        surrogate = self.root / surrogate_relative
        surrogate.parent.mkdir(parents=True)
        shutil.copy2(protected_source, surrogate)
        surrogate_digest = (
            "sha256:" + hashlib.sha256(surrogate.read_bytes()).hexdigest()
        )
        with closing(
            CORE.connect_catalog(self.root / "work" / "catalog.sqlite")
        ) as catalog:
            catalog.execute(
                """INSERT OR IGNORE INTO content_blobs (
                       content_digest, size_bytes, media_type, width_px,
                       height_px, bit_depth, color_space, compression,
                       page_count, duration_ms, created_at
                   ) VALUES (?, ?, 'image/tiff', 1200, 800, 8, 'RGB',
                             'Uncompressed', 1, NULL, ?)""",
                (
                    surrogate_digest,
                    surrogate.stat().st_size,
                    "2026-07-19T00:00:00Z",
                ),
            )
            catalog.execute(
                """INSERT INTO file_occurrences (
                       occurrence_id, unit_id, batch_id, content_digest,
                       occurrence_kind, source_root_id, relative_path,
                       original_filename, observed_mtime_utc,
                       observed_at_utc, status, note
                   ) VALUES (
                       'OCC-SURROGATE-FIXTURE', 'UNIT-ONE', NULL, ?,
                       'archive', '', ?, 'generated-surrogate.tif', '',
                       ?, 'verified', ''
                   )""",
                (
                    surrogate_digest,
                    surrogate_relative,
                    "2026-07-19T00:00:00Z",
                ),
            )
            catalog.execute(
                """INSERT INTO entity_files (
                       entity_id, occurrence_id, role, status, created_at
                   ) VALUES (?, 'OCC-SURROGATE-FIXTURE',
                             'preservation_surrogate', 'active', ?)""",
                (self.entity_id, "2026-07-19T00:00:00Z"),
            )
            catalog.commit()
        asset_id = "VASSET-" + "A" * 32
        report = {
            "format": "iss-visual-candidate-search-v1",
            "pilot_root": str(self.root),
            "manifest_sha256": hashlib.sha256(
                manifest_path.read_bytes()
            ).hexdigest(),
            "queries": [
                {
                    "unit_id": "UNIT-ONE",
                    "candidates": [
                        {
                            "asset_id": asset_id,
                            "path": protected_path,
                            "filename": "master-candidate.tif",
                            "extension": "tif",
                            "size_bytes": protected_stat.st_size,
                            "mtime_ns": protected_stat.st_mtime_ns,
                            "width_px": 1200,
                            "height_px": 800,
                            "distance_total": 7,
                            "distance_phash": 2,
                            "distance_dhash": 3,
                            "distance_ahash": 2,
                            "distance_band": "very_close",
                            "occurrence_count": 2,
                            "alternate_occurrences": [
                                {
                                    "asset_id": "VASSET-ALTERNATE",
                                    "path": alternate_path,
                                    "filename": "mirror-candidate.tiff",
                                    "size_bytes": protected_stat.st_size,
                                    "mtime_ns": protected_stat.st_mtime_ns,
                                    "width_px": 1200,
                                    "height_px": 800,
                                }
                            ],
                        }
                    ],
                }
            ],
        }
        report_path = self.base / "visual-candidates.json"
        report_path.write_text(json.dumps(report), encoding="utf-8")
        with mock.patch.object(
            WORKBENCH,
            "EDITOR_PREVIEW_ROOTS",
            (protected_root,),
        ):
            server = WORKBENCH.CoreWorkbenchServer(
                ("127.0.0.1", 0),
                self.root,
                "Fixture Archivist",
                manifest_path,
                report_path,
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                connection = http.client.HTTPConnection(
                    "127.0.0.1", server.server_port, timeout=15
                )
                connection.request("GET", "/compare?unit=UNIT-ONE")
                response = connection.getresponse()
                compare = response.read()
                self.assertEqual(200, response.status)
                self.assertIn(b"Assign original master", compare)
                self.assertIn(b"Very close candidate", compare)
                self.assertIn(b"master-candidate.tif", compare)
                self.assertIn(b"1200 \xc3\x97 800 px", compare)
                self.assertIn(b"Use as original master candidate", compare)
                self.assertIn(
                    b"generated surrogate remains preserved as provenance",
                    compare,
                )
                self.assertNotIn(protected_path.encode(), compare)
                self.assertNotIn(asset_id.encode(), compare)
                self.assertNotIn(b"VASSET-ALTERNATE", compare)
                self.assertNotIn(alternate_path.encode(), compare)
                self.assertNotIn(b"distance_total", compare)

                payload = urllib.parse.urlencode(
                    {
                        "csrf_token": server.csrf_token,
                        "unit_id": "UNIT-ONE",
                        "choice": "1",
                        "reason": "",
                    }
                )
                connection.request(
                    "POST",
                    "/editor/master/pin",
                    body=payload,
                    headers={
                        "Content-Type": (
                            "application/x-www-form-urlencoded"
                        ),
                        "Content-Length": str(len(payload)),
                    },
                )
                response = connection.getresponse()
                response.read()
                self.assertEqual(303, response.status)
                staged = WORKBENCH.master_candidate.latest_candidate(
                    self.root,
                    unit_id="UNIT-ONE",
                    entity_id=self.entity_id,
                )
                self.assertIsNotNone(staged)
                self.assertEqual(
                    "protected_visual_index_candidate",
                    staged["source_evidence"]["kind"],
                )
                self.assertEqual(
                    asset_id,
                    staged["source_evidence"]["asset_id"],
                )
                self.assertEqual(
                    hashlib.sha256(protected_source.read_bytes()).hexdigest(),
                    staged["sha256"].removeprefix("sha256:"),
                )

                connection.request("GET", "/admin?unit=UNIT-ONE")
                response = connection.getresponse()
                admin = response.read()
                self.assertEqual(200, response.status)
                self.assertIn(b"Visual candidate evidence", admin)
                self.assertIn(protected_path.encode(), admin)
                self.assertIn(asset_id.encode(), admin)
                self.assertIn(alternate_path.encode(), admin)
                self.assertIn(b"Occurrences", admin)
                self.assertIn(b"2 / 3 / 2", admin)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
