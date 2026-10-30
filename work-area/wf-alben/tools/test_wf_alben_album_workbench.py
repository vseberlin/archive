#!/usr/bin/env python3
"""Focused invariant checks for the album-level WF-Alben workbench."""

import csv
import importlib.util
import sqlite3
import unittest
from pathlib import Path


SCRIPT = Path(__file__).with_name("wf_alben_album_workbench.py")
SPEC = importlib.util.spec_from_file_location("wf_alben_album_workbench", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


class AlbumWorkbenchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workbench = MODULE.AlbumWorkbench("test-read-only")

    def test_four_physical_album_objects(self):
        self.assertEqual(set(self.workbench.objects), {"ALB-001", "ALB-002", "ALB-003", "ALBUM-NEF"})

    def test_no_invalid_pdf_is_master_eligible(self):
        pdfs = [
            candidate
            for groups in self.workbench.candidates.values()
            for candidate in groups
            if candidate["role"] == "whole_album_pdf"
        ]
        self.assertEqual(len(pdfs), 1)
        self.assertFalse(pdfs[0]["eligible"])
        self.assertIn("invalid PDF signature", pdfs[0]["evidence"])

    def test_tiff_sets_are_separate_from_pdf_master(self):
        for carrier, groups in self.workbench.candidates.items():
            tiffs = [candidate for candidate in groups if candidate["role"] == "corresponding_tiff_page_set"]
            self.assertTrue(tiffs, carrier)
            for candidate in tiffs:
                self.assertTrue(candidate["eligible"])
                self.assertTrue(candidate["members"])

    def test_every_manifest_row_forbids_deletion(self):
        for path in (MODULE.SOURCE_DISPOSITION, MODULE.DRAIN_LIST):
            with path.open(encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertTrue(rows)
            self.assertEqual({row["deletion_authorized"] for row in rows}, {"false"})

    def test_context_is_not_in_drain_list(self):
        with MODULE.DRAIN_LIST.open(encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
        self.assertNotIn("excluded_from_drain_scope", {row["drain_state"] for row in rows})

    def test_sqlite_is_derived_and_integrity_clean(self):
        con = sqlite3.connect(f"file:{MODULE.DATABASE}?mode=ro", uri=True)
        try:
            self.assertEqual(con.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(con.execute("SELECT count(*) FROM objects").fetchone()[0], 4)
            self.assertEqual(con.execute("SELECT count(*) FROM decisions WHERE deletion_authorized != 0").fetchone()[0], 0)
        finally:
            con.close()


if __name__ == "__main__":
    unittest.main()
