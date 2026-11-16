#!/usr/bin/env python3
"""Focused tests for content-only Brigadebuecher PDF page matching."""

import csv
import importlib.util
import sys
import unittest
from pathlib import Path


SCRIPT = Path(__file__).with_name("brigadebuecher_reconcile.py")
SPEC = importlib.util.spec_from_file_location("brigadebuecher_reconcile", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)
sys.path.insert(0, str(MODULE.VISUAL_TOOL_ROOT))
import visual_index  # type: ignore  # noqa: E402
from PIL import Image  # noqa: E402


class BrigadebuecherContentMatchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        MODULE.ensure_source_guard()
        with (MODULE.RECONCILIATION_ROOT / "brigadebuecher-pdf-occurrences.csv").open(
            encoding="utf-8-sig", newline=""
        ) as handle:
            cls.pdf_rows = {
                row["inventory"]: row
                for row in csv.DictReader(handle)
                if row["relationship"] == "preferred_occurrence_for_review"
            }

    def hashes(self, inventory):
        row = self.pdf_rows[inventory]
        before = set(MODULE.RECONCILIATION_ROOT.glob(".bb-scan-*"))
        hashes = MODULE.embedded_scan_hashes(
            row["source_path"], int(row["page_count"]), visual_index, Image
        )
        self.assertEqual(set(MODULE.RECONCILIATION_ROOT.glob(".bb-scan-*")), before)
        return hashes

    def test_pdf_furniture_is_excluded_from_bb03_2_page_9(self):
        pdf_hash = self.hashes("BB-03_2")[8]
        tiff_path = Path(
            "/media/vladimir/Elements/restore/archive/WF/BRIGADEBUCH-Projekt/"
            "SCANS-Canon/BB-03/BB-03_042.tif"
        )
        preview, _ = visual_index.normalized_preview(
            tiff_path, MODULE.RECONCILIATION_ROOT, 30
        )
        try:
            tiff_hash = visual_index.feature_hashes(preview)
        finally:
            preview.close()
        fields = ("phash_dct_64", "dhash_64", "ahash_64")
        distances = [
            MODULE.hamming(pdf_hash[field], tiff_hash[field]) for field in fields
        ]
        self.assertEqual(distances, [2, 4, 0])

    def test_mixed_resolution_embedded_scan_tiles_are_reconstructed(self):
        hashes = self.hashes("BB-15_1")
        self.assertEqual(len(hashes), int(self.pdf_rows["BB-15_1"]["page_count"]))
        self.assertTrue(all(set(row) == {"ahash_64", "dhash_64", "phash_dct_64"} for row in hashes))

    def test_rotated_bb48_pdf_page_is_compared_in_its_displayed_orientation(self):
        pdf_hash = self.hashes("BB-48")[0]
        tiff_path = Path(
            "/media/vladimir/Elements/restore/archive/Bibliocopy/BB-48/BB-48_001.tif"
        )
        preview, _ = visual_index.normalized_preview(
            tiff_path, MODULE.RECONCILIATION_ROOT, 30
        )
        try:
            tiff_hash = visual_index.feature_hashes(preview)
        finally:
            preview.close()
        distances = [
            MODULE.hamming(pdf_hash[field], tiff_hash[field])
            for field in ("phash_dct_64", "dhash_64", "ahash_64")
        ]
        self.assertLessEqual(sum(distances), 8)

    def test_decisive_low_distance_is_accepted_without_sequence_consistency(self):
        row = {
            "distance_total": 2,
            "distance_phash": 0,
            "runner_up_total": 61,
            "offset_consistent": 0,
        }
        self.assertEqual(MODULE.classify_match_state(row), "strong_visual_identity")

    def test_low_distance_with_a_close_runner_still_needs_review(self):
        row = {
            "distance_total": 4,
            "distance_phash": 2,
            "runner_up_total": 12,
            "offset_consistent": 0,
        }
        self.assertEqual(MODULE.classify_match_state(row), "needs_review")

    def test_monotonic_alignment_can_leave_an_unmatched_pdf_page(self):
        def ranked(first, second):
            return [
                (first, first, 0, 0, 1, {}),
                (second, second, 0, 0, 2, {}),
            ]

        alignment = MODULE.monotonic_page_alignment([
            ranked(0, 50),
            ranked(50, 50),
            ranked(50, 0),
        ])

        self.assertEqual(alignment, [1, None, 2])


if __name__ == "__main__":
    unittest.main()
