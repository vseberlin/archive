#!/usr/bin/env python3
"""Focused invariants for the museum-digital-object Brigadebücher bench."""

import csv
import hashlib
import importlib.util
import io
import json
import os
import shutil
import sqlite3
import unittest
from pathlib import Path
from unittest import mock


SCRIPT = Path(__file__).with_name("brigadebuecher_md_workbench.py")
SPEC = importlib.util.spec_from_file_location("brigadebuecher_md_workbench", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


class BrigadebuecherMDWorkbenchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.collection = MODULE.Collection861MD("test-read-only")

    def test_one_archive_object_per_md_object(self):
        self.assertEqual(len(self.collection.objects), 73)
        self.assertEqual(len({row["md_object_id"] for row in self.collection.objects.values()}), 73)
        self.assertEqual(len({row["inventory"] for row in self.collection.objects.values()}), 73)

    def test_each_object_has_one_fixed_md_pdf_access_representation(self):
        paths = []
        for row in self.collection.objects.values():
            self.assertTrue(row["md_pdf_path"].endswith(".pdf"))
            self.assertGreater(row["md_pdf_pages"], 0)
            paths.append(row["md_pdf_path"])
        self.assertEqual(len(paths), 73)

    def test_document_appraisal_is_complete_and_has_one_master_per_group(self):
        rows = list(self.collection.appraisal.values())
        self.assertEqual(len(rows), 297)
        self.assertFalse(any(not row.get("decision") for row in rows))
        self.assertFalse(any(row.get("deletion_authorized") for row in rows))
        selected = [row for row in rows if row.get("selected_for_unit") is True]
        masters = [row for row in selected if row.get("representation_role") == "selected_master"]
        self.assertEqual(len(selected), 15)
        self.assertEqual(len(masters), 10)
        self.assertEqual(len({row["document_group"] for row in masters}), 10)

    def test_document_appraisal_and_source_projection_agree(self):
        source = {
            row["source_path"]: row
            for row in MODULE.read_csv(MODULE.SOURCE_DISPOSITION)
        }
        for row in self.collection.appraisal.values():
            projected = source[row["source_path"]]
            if row.get("selected_for_unit") is True:
                expected = "confirmed_unit_document_input"
            elif row["decision"].startswith("redundant_"):
                expected = "confirmed_redundant_unit_document"
            else:
                expected = "confirmed_outside_unit_documentation"
            self.assertEqual(projected["disposition"], expected)

    def test_review_items_are_one_flat_missing_duplicate_uncertain_route(self):
        rows = self.collection.review_items()
        self.assertEqual(
            len(rows),
            len({(row["object_key"], row["pdf_page"]) for row in rows}),
        )
        self.assertTrue(all(
            set(row["categories"]) <= {"missing", "duplicated", "uncertain"}
            for row in rows
        ))
        pending_audit = {
            (row["object_key"], row["pdf_page"])
            for row in self.collection.remnants()
        }
        review_keys = {(row["object_key"], row["pdf_page"]) for row in rows}
        self.assertTrue(pending_audit <= review_keys)

    def test_full_unit_rescan_candidate_is_a_bounded_page_choice(self):
        choices = self.collection.page_choices("md:114870", 1)
        candidate = next(
            choice for choice in choices["choices"]
            if choice["path"].endswith("BB-02_002.tif")
        )
        self.assertIn("full-unit rescan TIFF candidate", candidate["reasons"])
        self.assertEqual(choices["choices"][0]["path"], candidate["path"])

    def test_post_audit_page_save_removes_only_that_remnant_from_pending(self):
        initial = self.collection.remnants()
        if not initial:
            self.assertEqual(self.collection.review_items(), [])
            return
        first = initial[0]
        decisions = self.collection.page_decisions()
        decisions[(first["object_key"], first["pdf_page"])] = {
            "outcome": "selected_tiff",
            "selected_tiff_path": first["best_tiff_path"],
            "selected_tiff_relation": "full_page",
            "decided_utc": "2999-01-01T00:00:00Z",
        }
        with mock.patch.object(self.collection, "page_decisions", return_value=decisions):
            self.assertEqual(len(self.collection.remnants()), len(initial) - 1)
            reviewed = self.collection.remnants(include_reviewed=True)
        self.assertEqual(len(reviewed), len(self.collection.remnant_audit))
        matching = [
            row for row in reviewed
            if row["object_key"] == first["object_key"]
            and row["pdf_page"] == first["pdf_page"]
        ]
        self.assertEqual(len(matching), 1)
        self.assertTrue(matching[0]["reviewed"])

    def test_index_exposes_one_review_route_without_sets_or_reopen(self):
        handler = object.__new__(MODULE.Handler)
        handler.collection = self.collection
        handler.path = "/"
        responses = []
        handler.send_bytes = lambda payload, *_args, **_kwargs: responses.append(payload)

        handler.index()

        html = responses[0].decode()
        self.assertIn("<h1>PDF and corresponding TIFF review</h1>", html)
        if self.collection.review_items():
            self.assertIn("Review object", html)
        else:
            self.assertIn("No pages currently need review", html)
        self.assertNotIn("Remnants", html)
        self.assertNotIn("Reopen", html)
        self.assertNotIn("eligible ordered TIFF Master candidate", html)

    def test_approved_existing_jpeg_master_sets_are_complete(self):
        expected = {"md:114898": 40, "md:114900": 49, "md:114901": 56}
        for key, page_count in expected.items():
            record = self.collection._non_tiff_master_record(
                key, "ordered_jpeg_page_set", "approved test",
            )
            paths = record["selected_non_tiff_master_paths"]
            self.assertEqual(len(paths), page_count)
            self.assertEqual(len(paths), len(set(paths)))
            self.assertTrue(all(path.lower().endswith(".jpg") for path in paths))
            self.assertEqual(record["retained_md_pdf_path"], self.collection.objects[key]["md_pdf_path"])

    def test_bb28_part3_surviving_pdf_exception_is_separate_from_md_access_pdf(self):
        record = self.collection._non_tiff_master_record(
            "md:114897", "surviving_pdf_exception", "approved test",
        )
        self.assertEqual(len(record["selected_non_tiff_master_paths"]), 1)
        self.assertNotEqual(
            record["selected_non_tiff_master_paths"][0],
            record["retained_md_pdf_path"],
        )
        self.assertEqual(record["reviewed_missing_pdf_pages"], list(range(1, 24)))

    def test_global_navigation_has_only_the_review_route(self):
        html = MODULE.layout("test", "body").decode()
        self.assertIn("href='/'>Review</a>", html)
        for old_label in ("Unresolved", "Resolved", "All", "Remnants", "Drain"):
            self.assertNotIn(f">{old_label}</a>", html)

    def test_review_page_can_confirm_correct_tiff_rotated_90_ccw(self):
        handler = object.__new__(MODULE.Handler)
        handler.collection = self.collection
        handler.path = "/compare?key=md%3A114914&page=8"
        responses = []
        handler.send_bytes = lambda payload, *_args, **_kwargs: responses.append(payload)

        handler.review_page()

        html = responses[0].decode()
        self.assertIn("Confirm current TIFF — rotated 90° CCW", html)
        self.assertIn("value='rotated_90_ccw'", html)
        self.assertIn("Correct TIFF, rotated 90° counter-clockwise", html)

    def test_page_save_does_not_reopen_or_change_the_master_decision(self):
        key = "md:test"
        body = f"key={key.replace(':', '%3A')}&page=7&outcome=no_corresponding_tiff".encode()
        collection = mock.Mock()
        collection.objects = {key: {"inventory": "BB-TEST"}}
        collection.review_items.return_value = []
        handler = object.__new__(MODULE.Handler)
        handler.collection = collection
        handler.path = "/page-decision"
        handler.headers = {"Content-Length": str(len(body))}
        handler.rfile = io.BytesIO(body)
        redirects = []
        handler.redirect = redirects.append

        handler.do_POST()

        collection.save_page_decision.assert_called_once_with(
            key,
            7,
            "no_corresponding_tiff",
            "",
            "",
            "full_page",
        )
        collection.finalize_reviewed_object.assert_called_once_with(key)
        collection.save_decision.assert_not_called()
        self.assertEqual(redirects, ["/?saved=1"])

    def test_master_candidates_are_ordered_tiff_sets_only(self):
        for key, candidates in self.collection.candidates_by_object.items():
            for candidate in candidates:
                self.assertNotIn("pdf", candidate["role"])
                if candidate["eligible"]:
                    bound_pages = candidate["bound_pdf_page_count"] + len(candidate["reviewed_missing_pages"])
                    self.assertEqual(bound_pages, self.collection.objects[key]["md_pdf_pages"])
                    self.assertEqual(candidate["file_count"], len(set(candidate["paths"])))
                    self.assertTrue(all(Path(path).suffix.casefold() in {".tif", ".tiff"} for path in candidate["paths"]))

    def test_bb01_3_reviewed_gaps_form_an_eligible_human_candidate(self):
        key = "md:114859"  # BB-01_3
        candidate = next(
            item
            for item in self.collection.candidates_by_object[key]
            if item["role"] == "human_corrected_tiff_master_set"
        )
        self.assertTrue(candidate["page_review_complete"])
        self.assertTrue(candidate["eligible"])
        self.assertEqual(candidate["file_count"], 55)
        self.assertEqual(candidate["reviewed_missing_pages"], [24, 38, 42, 43, 53, 58])

    def test_preview_cache_stays_flat_in_the_approved_reconciliation_root(self):
        key = "md:114859"
        obj = self.collection.objects[key]
        tiff = self.collection._family_paths(obj)[0]
        tiff_cache = self.collection.tiff_preview_cache_path(tiff, 1000)
        pdf_cache = self.collection.pdf_preview_cache_path(key, 1, 1200)
        for path in (tiff_cache, pdf_cache):
            self.assertEqual(path.parent, MODULE.RECON)
            self.assertTrue(path.name.startswith(MODULE.PREVIEW_PREFIX))
            self.assertEqual(path.suffix, ".jpg")

    def test_family_paths_are_scanned_once_from_the_read_only_source(self):
        obj = self.collection.objects["md:114859"]
        self.collection._family_paths_cache.pop(obj["base_inventory"], None)
        with mock.patch.object(
            self.collection,
            "_scan_family_paths",
            wraps=self.collection._scan_family_paths,
        ) as scan:
            first = self.collection._family_paths(obj)
            second = self.collection._family_paths(obj)
        self.assertEqual(first, second)
        self.assertEqual(scan.call_count, 1)

    def test_unchanged_page_decision_manifest_reuses_validated_snapshot(self):
        self.collection._page_decisions_cache_key = None
        with mock.patch.object(MODULE, "source_path", wraps=MODULE.source_path) as resolve:
            first = self.collection.page_decisions()
            first_resolve_count = resolve.call_count
            second = self.collection.page_decisions()
        self.assertTrue(first_resolve_count)
        self.assertEqual(first, second)
        self.assertEqual(resolve.call_count, first_resolve_count)

    def test_visual_comparison_reports_uncertain_pages_without_repeated_assignments(self):
        key = "md:114847"  # BB-01_1
        candidate = next(
            item
            for item in self.collection.candidates_by_object[key]
            if item["role"] == "visual_match_tiff_master_set"
        )
        rows = self.collection.comparison_rows(key, candidate["candidate_id"])
        summary = self.collection.comparison_summary(key, candidate["candidate_id"])
        self.assertEqual(len(rows), self.collection.objects[key]["md_pdf_pages"])
        self.assertEqual(summary["duplicate_page_assignments"], 0)
        self.assertGreater(summary["needs_visual_review"], 0)
        self.assertTrue(summary["problem_pages"])

    def test_repeated_assignment_diagnostics_name_tiff_and_all_pdf_pages(self):
        key = "md:test-repeat"
        candidate_id = "CAND-REPEAT"
        tiff_path = "/media/vladimir/Elements/restore/archive/BB-X_020.tif"
        collection = object.__new__(MODULE.Collection861MD)
        collection.objects = {
            key: {
                "inventory": "BB-X",
                "md_pdf_pages": 2,
                "description_page_start": "",
                "description_page_end": "",
            },
        }
        collection.candidates = {
            candidate_id: {
                "candidate_id": candidate_id,
                "object_key": key,
                "role": "visual_match_tiff_master_set",
                "paths": [tiff_path],
            },
        }
        collection.page_rows = {
            "BB-X": [
                {"pdf_page": "1", "best_source_path": tiff_path, "match_state": "accepted"},
                {"pdf_page": "2", "best_source_path": tiff_path, "match_state": "accepted"},
            ],
        }
        collection.source_index = {}
        collection.page_decisions = lambda: {}

        rows = collection.comparison_rows(key, candidate_id)
        summary = collection.comparison_summary(key, candidate_id)

        self.assertEqual(rows[0]["duplicate_assignment_pages"], [1, 2])
        self.assertIn("PDF pages 1, 2", rows[0]["problems"][-1])
        self.assertEqual(summary["duplicate_page_assignments"], 2)
        self.assertEqual(summary["repeated_assignment_groups"], [{
            "tiff_path": tiff_path,
            "tiff_filename": "BB-X_020.tif",
            "pdf_pages": [1, 2],
        }])

    def test_bb48_crop_relation_retains_shared_tiff_once_without_duplicate_error(self):
        key = "md:114911"
        path = (
            "/media/vladimir/Elements/restore/archive/Bibliocopy/BB-48/"
            "BB-48_062.tif"
        )
        candidate = next(
            item
            for item in self.collection.candidates_by_object[key]
            if item["role"] == "human_corrected_tiff_master_set"
        )
        rows = self.collection.comparison_rows(key, candidate["candidate_id"])
        summary = self.collection.comparison_summary(key, candidate["candidate_id"])

        self.assertEqual(rows[60]["selected_tiff_relation"], "full_page")
        self.assertEqual(rows[61]["selected_tiff_relation"], "cropped_detail")
        self.assertEqual(rows[61]["shared_crop_assignment_pages"], [61, 62])
        self.assertFalse(rows[60]["duplicate_assignment"])
        self.assertFalse(rows[61]["duplicate_assignment"])
        self.assertEqual(candidate["paths"].count(path), 1)
        self.assertEqual(candidate["bound_pdf_page_count"], candidate["file_count"] + 1)
        self.assertEqual(summary["cropped_detail_pages"], 1)
        self.assertEqual(summary["duplicate_page_assignments"], 0)

    def test_bb48_crop_relation_is_human_readable_and_in_sqlite(self):
        decision = self.collection.page_decisions()[("md:114911", 62)]
        self.assertEqual(decision["selected_tiff_relation"], "cropped_detail")
        self.assertIn("cropped detail", decision["note"])
        con = sqlite3.connect(f"file:{MODULE.DATABASE}?mode=ro", uri=True)
        try:
            stored = con.execute(
                "SELECT selected_tiff_relation FROM page_decisions "
                "WHERE object_key = ? AND pdf_page = 62",
                ("md:114911",),
            ).fetchone()
        finally:
            con.close()
        self.assertEqual(stored, ("cropped_detail",))

    def test_bb48_crop_relation_is_visible_on_resolved_compare_page(self):
        key = "md:114911"
        candidate = next(
            item
            for item in self.collection.candidates_by_object[key]
            if item["role"] == "human_corrected_tiff_master_set"
        )
        handler = object.__new__(MODULE.Handler)
        handler.collection = self.collection
        handler.path = (
            f"/compare?key=md%3A114911&candidate={candidate['candidate_id']}"
            "&page=62&show_resolved=1"
        )
        responses = []
        handler.send_bytes = lambda payload, *_args, **_kwargs: responses.append(payload)

        handler.compare()

        html = responses[0].decode()
        self.assertIn("Confirmed crop relationship:", html)
        self.assertIn("Recorded cropped detail: BB-48_062.tif", html)
        self.assertNotIn("Repeated assignment:", html)
        self.assertIn("value='cropped_detail' checked", html)

    def test_page_18_now_proposes_the_sequence_context_match(self):
        choices = self.collection.page_choices("md:114847", 18)
        self.assertTrue(choices["automatic_path"].endswith("BB-01_021.tiff"))
        self.assertEqual(choices["expected_sequence"], 21)
        expected = [
            choice
            for choice in choices["choices"]
            if choice["sequence"] == 21
            and any(reason.startswith("neighbor-aligned sequence") for reason in choice["reasons"])
        ]
        self.assertTrue(expected)

    def test_recorded_page_decisions_leave_the_unresolved_list(self):
        key = "md:114847"
        candidate = next(
            item
            for item in self.collection.candidates_by_object[key]
            if item["role"] == "human_corrected_tiff_master_set"
        )
        unresolved = set(self.collection.unresolved_pages(key, candidate["candidate_id"]))
        decided = {
            page
            for (decision_key, page) in self.collection.page_decisions()
            if decision_key == key
        }
        self.assertTrue(decided)
        self.assertFalse(unresolved & decided)

    def test_pdf_only_master_requires_complete_explicit_no_tiff_review(self):
        self.assertTrue(self.collection.pdf_only_master_eligible("md:114897"))
        self.assertFalse(self.collection.pdf_only_master_eligible("md:114896"))

    def test_pdf_only_master_accepts_indexed_carrier_with_no_tiff_family(self):
        key = "md:114898"
        self.assertEqual(
            self.collection.pdf_only_master_basis(key),
            "indexed_carrier_has_no_tiff_family",
        )
        self.assertFalse(any(decision_key == key for decision_key, _page in self.collection.page_decisions()))
        self.assertFalse(self.collection.candidates_by_object[key])

    def test_pdf_only_master_button_is_visible_after_complete_review(self):
        handler = object.__new__(MODULE.Handler)
        handler.collection = self.collection
        handler.path = "/object?key=md%3A114897&page_review=complete"
        responses = []
        handler.send_bytes = lambda payload, *_args, **_kwargs: responses.append(payload)

        handler.object_page()

        html = responses[0].decode()
        self.assertIn("Save MD PDF as Master", html)
        self.assertIn("every page is explicitly reviewed with no corresponding TIFF", html)

    def test_pdf_only_master_button_is_visible_when_carrier_has_no_tiff_family(self):
        handler = object.__new__(MODULE.Handler)
        handler.collection = self.collection
        handler.path = "/object?key=md%3A114898"
        responses = []
        handler.send_bytes = lambda payload, *_args, **_kwargs: responses.append(payload)

        handler.object_page()

        html = responses[0].decode()
        self.assertIn("Save MD PDF as Master", html)
        self.assertIn("Carrier inventory confirms that no TIFF family exists", html)

    def test_object_compare_link_can_reopen_a_resolved_structural_problem(self):
        key = "md:test"
        candidate_id = "CAND-TEST"
        candidate = {
            "candidate_id": candidate_id,
            "role": "human_corrected_tiff_master_set",
            "label": "Human-corrected ordered TIFF set",
            "eligible": False,
            "evidence": "two pages currently share one TIFF",
            "paths": [],
        }
        row = {
            "object_key": key,
            "inventory": "BB-TEST",
            "title": "Regression fixture",
            "object_url": "https://example.invalid/object",
            "md_object_id": "test",
            "md_pdf_pages": 2,
            "md_pdf_size": 1,
            "md_pdf_path": "/protected/test.pdf",
            "verification_state": "test",
            "metadata_authority": "test",
            "decision": None,
            "candidates": [candidate],
        }
        collection = mock.Mock()
        collection.records.return_value = [row]
        collection.page_decisions.return_value = {
            (key, 2): {"outcome": "selected_tiff"},
        }
        collection.comparison_summary.return_value = {
            "mapped_pages": 2,
            "pdf_pages": 2,
            "unique_tiffs": 1,
            "missing_pages": 0,
            "reviewed_missing_pages": 0,
            "unreviewed_missing_pages": 0,
            "duplicate_page_assignments": 2,
            "repeated_assignment_groups": [{
                "tiff_path": "/protected/BB-X_020.tif",
                "tiff_filename": "BB-X_020.tif",
                "pdf_pages": [1, 2],
            }],
            "needs_visual_review": 0,
            "decoder_errors": 0,
            "problem_pages": [2],
        }
        collection.unresolved_pages.return_value = []
        handler = object.__new__(MODULE.Handler)
        handler.collection = collection
        handler.path = "/object?key=md%3Atest"
        responses = []
        handler.send_bytes = lambda payload, *_args, **_kwargs: responses.append(payload)

        handler.object_page()

        html = responses[0].decode()
        self.assertIn("candidate=CAND-TEST&amp;page=2&amp;show_resolved=1", html)
        self.assertIn("BB-X_020.tif", html)
        self.assertIn("candidate=CAND-TEST&amp;page=1&amp;show_resolved=1", html)
        self.assertIn("candidate=CAND-TEST&amp;page=2&amp;show_resolved=1", html)

    def test_compare_page_links_every_page_in_a_repeated_assignment(self):
        key = "md:test"
        candidate_id = "CAND-CORRECTED"
        tiff_path = "/protected/BB-X_020.tif"
        candidate = {
            "candidate_id": candidate_id,
            "object_key": key,
            "role": "human_corrected_tiff_master_set",
            "label": "Human-corrected ordered TIFF set",
        }
        repeated_row = {
            "pdf_page": 1,
            "tiff_path": tiff_path,
            "tiff_filename": "BB-X_020.tif",
            "sequence": 20,
            "match_state": "human_selected_tiff",
            "distance_total": "",
            "distance_phash": "",
            "distance_dhash": "",
            "distance_ahash": "",
            "runner_up_sequence": "",
            "runner_up_total": "",
            "selection_basis": "human decision",
            "global_best_scan_sequence": "",
            "global_best_total": "",
            "decoder_error": False,
            "technical_metadata": "",
            "duplicate_assignment": True,
            "duplicate_assignment_pages": [1, 2],
            "problems": ["same TIFF assigned to PDF pages 1, 2"],
        }
        collection = mock.Mock()
        collection.objects = {
            key: {"inventory": "BB-X", "md_pdf_pages": 2},
        }
        collection.candidates = {candidate_id: candidate}
        collection.candidates_by_object = {key: [candidate]}
        collection.comparison_rows.return_value = [repeated_row, dict(repeated_row, pdf_page=2)]
        collection.page_decisions.return_value = {
            (key, 1): {"outcome": "selected_tiff", "selected_tiff_path": tiff_path, "note": ""},
            (key, 2): {"outcome": "selected_tiff", "selected_tiff_path": tiff_path, "note": ""},
        }
        collection.unresolved_pages.return_value = []
        collection.page_choices.return_value = {
            "choices": [],
            "all_paths": [tiff_path],
            "current": collection.page_decisions.return_value[(key, 1)],
            "automatic_path": tiff_path,
            "expected_sequence": 20,
            "catalogue_sequence": None,
            "dominant_offset": 0,
            "runner_up_sequence": None,
        }
        handler = object.__new__(MODULE.Handler)
        handler.collection = collection
        handler.path = "/compare?key=md%3Atest&candidate=CAND-CORRECTED&page=1&show_resolved=1"
        responses = []
        handler.send_bytes = lambda payload, *_args, **_kwargs: responses.append(payload)

        handler.compare()

        html = responses[0].decode()
        self.assertIn("Repeated assignment:", html)
        self.assertIn("BB-X_020.tif", html)
        self.assertIn("candidate=CAND-CORRECTED&amp;page=1&amp;show_resolved=1", html)
        self.assertIn("candidate=CAND-CORRECTED&amp;page=2&amp;show_resolved=1", html)

    def test_visual_evidence_redirect_preserves_show_resolved(self):
        key = "md:test"
        automatic_id = "CAND-AUTOMATIC"
        corrected_id = "CAND-CORRECTED"
        collection = mock.Mock()
        collection.objects = {key: {"md_pdf_pages": 2}}
        collection.candidates = {
            automatic_id: {
                "candidate_id": automatic_id,
                "object_key": key,
                "role": "visual_match_tiff_master_set",
            },
            corrected_id: {
                "candidate_id": corrected_id,
                "object_key": key,
                "role": "human_corrected_tiff_master_set",
            },
        }
        collection.candidates_by_object = {
            key: [collection.candidates[automatic_id], collection.candidates[corrected_id]],
        }
        handler = object.__new__(MODULE.Handler)
        handler.collection = collection
        handler.path = (
            "/compare?key=md%3Atest&candidate=CAND-AUTOMATIC&page=1&show_resolved=1"
        )
        redirects = []
        handler.redirect = redirects.append

        handler.compare()

        self.assertEqual(len(redirects), 1)
        self.assertIn("candidate=CAND-CORRECTED", redirects[0])
        self.assertIn("current=1", redirects[0])
        self.assertIn("show_resolved=1", redirects[0])

    def test_automatic_problem_pages_stay_visible_until_explicitly_decided(self):
        decisions = self.collection.page_decisions()
        for key, candidates in self.collection.candidates_by_object.items():
            corrected = next(
                (item for item in candidates if item["role"] == "human_corrected_tiff_master_set"),
                None,
            )
            if corrected is None:
                continue
            decided = {
                page
                for decision_key, page in decisions
                if decision_key == key
            }
            expected = self.collection._automatic_problem_pages(self.collection.objects[key]) - decided
            unresolved = set(self.collection.unresolved_pages(key, corrected["candidate_id"]))
            self.assertTrue(expected <= unresolved, key)

    def test_range_comparison_preserves_missing_page_positions(self):
        key = "md:114914"  # BB-03_3
        candidate = next(
            item
            for item in self.collection.candidates_by_object[key]
            if item["role"] == "catalogue_range_tiff_master_set"
        )
        rows = self.collection.comparison_rows(key, candidate["candidate_id"])
        summary = self.collection.comparison_summary(key, candidate["candidate_id"])
        self.assertEqual(len(rows), 60)
        self.assertEqual(summary["missing_pages"], 5)
        self.assertTrue(any(not row["tiff_path"] for row in rows))

    def test_drain_is_filtered_subset_of_source_disposition(self):
        def rows(path):
            with path.open(encoding="utf-8-sig", newline="") as handle:
                return list(csv.DictReader(handle))
        source = rows(MODULE.SOURCE_DISPOSITION)
        drain = rows(MODULE.DRAIN_LIST)
        self.assertEqual(len(source), 9256)
        self.assertLess(len(drain), len(source))
        self.assertTrue({row["source_path"] for row in drain} <= {row["source_path"] for row in source})
        self.assertFalse(any(row["drain_state"].startswith("excluded_") for row in drain))
        drain_paths = {row["source_path"] for row in drain}
        unit_document_paths = {
            path for path, row in self.collection.appraisal.items()
            if row.get("selected_for_unit") is True
            or str(row.get("decision", "")).startswith("redundant_")
        }
        outside_unit_paths = set(self.collection.appraisal) - unit_document_paths
        self.assertTrue(unit_document_paths <= drain_paths)
        self.assertFalse(outside_unit_paths & drain_paths)

    def test_deletion_is_never_authorized(self):
        for path in (MODULE.DECISIONS, MODULE.SOURCE_DISPOSITION, MODULE.DRAIN_LIST):
            with path.open(encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertFalse(any(row["deletion_authorized"].casefold() != "false" for row in rows))

    def test_sqlite_is_integrity_clean_projection(self):
        con = sqlite3.connect(f"file:{MODULE.DATABASE}?mode=ro", uri=True)
        try:
            self.assertEqual(con.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(con.execute("SELECT count(*) FROM objects").fetchone()[0], 73)
            self.assertEqual(con.execute("SELECT count(*) FROM decisions WHERE deletion_authorized != 0").fetchone()[0], 0)
            self.assertEqual(con.execute("SELECT count(*) FROM page_decisions WHERE deletion_authorized != 0").fetchone()[0], 0)
        finally:
            con.close()

    def test_page_save_updates_only_the_affected_projection(self):
        object_decisions = {
            object_key: dict(decision)
            for object_key, decision in self.collection.decisions().items()
        }
        key = next(
            object_key
            for object_key in self.collection.objects
            if any(
                self.collection.page_choices(object_key, pdf_page)["automatic_path"]
                for pdf_page in range(
                    1, self.collection.objects[object_key]["md_pdf_pages"] + 1
                )
            )
        )
        # This isolated writer test exercises the pre-accession save path. The
        # live collection may already be accessioned, in which case production
        # correctly blocks page edits and the temporary fixture must say so explicitly.
        object_decisions[key]["accession_state"] = "not_accessioned"
        page = next(
            pdf_page
            for pdf_page in range(1, self.collection.objects[key]["md_pdf_pages"] + 1)
            if self.collection.page_choices(key, pdf_page)["automatic_path"]
        )
        choices = self.collection.page_choices(key, page)
        existing = {
            "outcome": "selected_tiff",
            "selected_tiff_path": choices["automatic_path"],
            "selected_tiff_relation": "rotated_90_ccw",
            "note": "isolated projection test",
        }
        suffix = f"test-{os.getpid()}"
        temp_page_decisions = MODULE.RECON / f".page-decisions-{suffix}.csv"
        temp_database = MODULE.RECON / f".workbench-{suffix}.sqlite"
        temp_preflight = MODULE.RECON / f".preflight-{suffix}.json"

        def digest(path):
            return hashlib.sha256(path.read_bytes()).hexdigest()

        source_before = digest(MODULE.SOURCE_DISPOSITION)
        drain_before = digest(MODULE.DRAIN_LIST)
        shutil.copy2(MODULE.PAGE_DECISIONS, temp_page_decisions)
        shutil.copy2(MODULE.DATABASE, temp_database)
        shutil.copy2(MODULE.PREFLIGHT, temp_preflight)
        try:
            con = sqlite3.connect(temp_database)
            try:
                unrelated_before = set(con.execute(
                    "SELECT candidate_id FROM tiff_candidates WHERE object_key != ?",
                    (key,),
                ))
            finally:
                con.close()

            with (
                mock.patch.object(MODULE, "PAGE_DECISIONS", temp_page_decisions),
                mock.patch.object(MODULE, "DATABASE", temp_database),
                mock.patch.object(MODULE, "PREFLIGHT", temp_preflight),
                mock.patch.object(
                    self.collection,
                    "decisions",
                    return_value=object_decisions,
                ),
                mock.patch.object(self.collection, "refresh", side_effect=AssertionError("full refresh called")),
                mock.patch.object(self.collection, "_source_rows", side_effect=AssertionError("source rows rebuilt")),
            ):
                corrected_id = self.collection.save_page_decision(
                    key,
                    page,
                    existing["outcome"],
                    existing["selected_tiff_path"],
                    existing["note"],
                    existing["selected_tiff_relation"],
                )
                manifest = self.collection.page_decisions()
                self.assertIn((key, page), manifest)
                self.assertEqual(
                    manifest[(key, page)]["selected_tiff_relation"],
                    "rotated_90_ccw",
                )
                self.assertIn(corrected_id, self.collection.candidates)
                selected_id = object_decisions[key]["selected_tiff_candidate_id"]
                if selected_id:
                    self.assertIn(selected_id, self.collection.candidates)
                con = sqlite3.connect(temp_database)
                try:
                    self.assertEqual(con.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                    self.assertEqual(
                        con.execute("SELECT count(*) FROM page_decisions").fetchone()[0],
                        len(manifest),
                    )
                    self.assertEqual(
                        con.execute(
                            "SELECT selected_tiff_relation FROM page_decisions "
                            "WHERE object_key = ? AND pdf_page = ?",
                            (key, page),
                        ).fetchone(),
                        ("rotated_90_ccw",),
                    )
                    unrelated_after = set(con.execute(
                        "SELECT candidate_id FROM tiff_candidates WHERE object_key != ?",
                        (key,),
                    ))
                finally:
                    con.close()
                self.assertEqual(unrelated_after, unrelated_before)
                self.assertEqual(json.loads(temp_preflight.read_text())["page_decisions"], len(manifest))

            self.assertEqual(digest(MODULE.SOURCE_DISPOSITION), source_before)
            self.assertEqual(digest(MODULE.DRAIN_LIST), drain_before)
        finally:
            temp_page_decisions.unlink(missing_ok=True)
            temp_database.unlink(missing_ok=True)
            temp_preflight.unlink(missing_ok=True)
            self.collection._rebuild_candidates()

    def test_pdf_only_master_save_updates_manifest_sqlite_and_source_projection(self):
        key = "md:114897"
        obj = self.collection.objects[key]
        suffix = f"pdf-master-test-{os.getpid()}"
        paths = {
            "page": MODULE.RECON / f".page-decisions-{suffix}.csv",
            "decisions": MODULE.RECON / f".object-decisions-{suffix}.csv",
            "source": MODULE.RECON / f".source-disposition-{suffix}.csv",
            "drain": MODULE.RECON / f".drain-list-{suffix}.csv",
            "database": MODULE.RECON / f".workbench-{suffix}.sqlite",
            "preflight": MODULE.RECON / f".preflight-{suffix}.json",
        }
        originals = {
            "page": MODULE.PAGE_DECISIONS,
            "decisions": MODULE.DECISIONS,
            "source": MODULE.SOURCE_DISPOSITION,
            "drain": MODULE.DRAIN_LIST,
            "database": MODULE.DATABASE,
            "preflight": MODULE.PREFLIGHT,
        }
        for name, target in paths.items():
            shutil.copy2(originals[name], target)
        try:
            with (
                mock.patch.object(MODULE, "PAGE_DECISIONS", paths["page"]),
                mock.patch.object(MODULE, "DECISIONS", paths["decisions"]),
                mock.patch.object(MODULE, "SOURCE_DISPOSITION", paths["source"]),
                mock.patch.object(MODULE, "DRAIN_LIST", paths["drain"]),
                mock.patch.object(MODULE, "DATABASE", paths["database"]),
                mock.patch.object(MODULE, "PREFLIGHT", paths["preflight"]),
            ):
                self.collection.save_decision(
                    key,
                    "selected_pdf_master",
                    "",
                    "all pages reviewed; no corresponding TIFF pages",
                )
                decision = self.collection.decisions()[key]
                self.assertEqual(decision["outcome"], "selected_master")
                self.assertEqual(decision["master_representation_role"], "museum_digital_pdf")
                self.assertEqual(decision["selected_tiff_candidate_id"], "")
                self.assertEqual(decision["selected_tiff_master_paths"], [])
                self.assertEqual(
                    decision["reviewed_missing_pdf_pages"],
                    list(range(1, obj["md_pdf_pages"] + 1)),
                )
                self.assertEqual(decision["retained_md_pdf_path"], obj["md_pdf_path"])

                con = sqlite3.connect(paths["database"])
                try:
                    stored = con.execute(
                        "SELECT outcome, master_representation_role, selected_tiff_candidate_id, retained_md_pdf_path "
                        "FROM decisions WHERE object_key = ?",
                        (key,),
                    ).fetchone()
                    self.assertEqual(
                        stored,
                        ("selected_master", "museum_digital_pdf", "", obj["md_pdf_path"]),
                    )
                    self.assertEqual(con.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                finally:
                    con.close()

                with paths["source"].open(encoding="utf-8-sig", newline="") as handle:
                    source_rows = list(csv.DictReader(handle))
                pdf_row = next(row for row in source_rows if row["source_path"] == obj["md_pdf_path"])
                self.assertEqual(pdf_row["disposition"], "selected_md_pdf_master_input")
                self.assertEqual(pdf_row["accession_relation"], "md_pdf_master_input")
                self.assertFalse(any(row["deletion_authorized"].casefold() != "false" for row in source_rows))
        finally:
            for path in paths.values():
                path.unlink(missing_ok=True)

    def test_pdf_only_master_without_tiff_family_does_not_claim_page_review(self):
        key = "md:114898"
        suffix = f"pdf-no-tiff-family-test-{os.getpid()}"
        temp_decisions = MODULE.RECON / f".object-decisions-{suffix}.csv"
        shutil.copy2(MODULE.DECISIONS, temp_decisions)
        try:
            with (
                mock.patch.object(MODULE, "DECISIONS", temp_decisions),
                mock.patch.object(self.collection, "refresh"),
            ):
                self.collection.save_decision(key, "selected_pdf_master", "", "no TIFF family exists")
                decision = self.collection.decisions()[key]
            self.assertEqual(decision["master_representation_role"], "museum_digital_pdf")
            self.assertEqual(decision["reviewed_missing_pdf_pages"], [])
            self.assertEqual(
                decision["metadata_authority"],
                "pre-accession_MD_pdf_master_indexed_carrier_has_no_tiff_family",
            )
        finally:
            temp_decisions.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
