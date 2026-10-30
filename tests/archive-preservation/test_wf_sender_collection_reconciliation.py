from __future__ import annotations

import csv
import importlib.util
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = (
    REPO_ROOT
    / "tools"
    / "archive-preservation"
    / "wf_sender_collection_reconciliation.py"
)
SPEC = importlib.util.spec_from_file_location("wf_sender_reconciliation", SCRIPT)
assert SPEC and SPEC.loader
RECONCILIATION = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = RECONCILIATION
SPEC.loader.exec_module(RECONCILIATION)


class WfSenderCollectionReconciliationTest(unittest.TestCase):
    def test_aliases_preserve_exact_and_offer_bounded_variants(self) -> None:
        aliases = {
            (row.comparison_key, row.method)
            for row in RECONCILIATION.inventory_aliases("WFS-1980-1")
        }
        self.assertIn(("wfs-1980-1", "exact_inventory"), aliases)
        self.assertIn(("wfs-1980-01", "normalized_number"), aliases)

        flyer = {
            (row.comparison_key, row.method)
            for row in RECONCILIATION.inventory_aliases("WFS-FB-1953-02")
        }
        self.assertNotIn(("wfs-1953-02", "documented_alias"), flyer)

        special = {
            (row.comparison_key, row.method)
            for row in RECONCILIATION.inventory_aliases(
                "WFS-1950-01-Sonderausgabe"
            )
        }
        self.assertIn(("wfs-1950-01-sa", "documented_alias"), special)

        typo = {
            (row.comparison_key, row.method)
            for row in RECONCILIATION.inventory_aliases("WSF-1952-04")
        }
        self.assertIn(("wfs-1952-04", "documented_alias"), typo)

    def test_build_keeps_duplicate_md_rows_and_local_only_archive_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            capture = root / "capture"
            capture.mkdir()
            fields = [
                "object_id",
                "inventory",
                "title",
                "object_type",
                "date",
                "pdf_resource_ids",
                "pdf_urls",
                "object_url",
            ]
            rows = [
                {
                    "object_id": "1",
                    "inventory": "WFS-1951-01",
                    "title": "January",
                    "object_type": "Zeitung",
                    "date": "January 1951",
                    "pdf_resource_ids": "101",
                    "pdf_urls": "https://example.test/1.pdf",
                    "object_url": "https://example.test/object/1",
                },
                {
                    "object_id": "2",
                    "inventory": "WFS-1951-01",
                    "title": "March",
                    "object_type": "Zeitung",
                    "date": "March 1951",
                    "pdf_resource_ids": "102",
                    "pdf_urls": "https://example.test/2.pdf",
                    "object_url": "https://example.test/object/2",
                },
                {
                    "object_id": "3",
                    "inventory": "WFS-FB-1953-02",
                    "title": "Flyer",
                    "object_type": "Flugblatt",
                    "date": "1953",
                    "pdf_resource_ids": "103",
                    "pdf_urls": "https://example.test/3.pdf",
                    "object_url": "https://example.test/object/3",
                },
            ]
            with (capture / "objects.csv").open(
                "w", encoding="utf-8", newline=""
            ) as handle:
                writer = csv.DictWriter(handle, fieldnames=fields)
                writer.writeheader()
                writer.writerows(rows)
            (capture / "coverage.json").write_text(
                json.dumps({"captured_objects": 3}),
                encoding="utf-8",
            )

            source = root / "MIK-Daten"
            for family, key, filename in (
                ("PDF", "WFS-1951-01", "WFS-1951-01.pdf"),
                ("PDF", "WFS-1953-02", "WFS-1953-02.pdf"),
                ("TIF", "LOCAL-ONLY-01", "LOCAL-ONLY-01_001.tif"),
            ):
                directory = source / family / "1953" / key
                directory.mkdir(parents=True, exist_ok=True)
                (directory / filename).write_bytes(b"fixture")

            run_dir = root / "run"
            summary = RECONCILIATION.build_run(
                capture,
                source,
                run_dir,
                "fixture-run",
                "fixture-device fixture-mount fixture-fs ro",
            )
            self.assertEqual(summary["md_objects"], 3)
            self.assertEqual(summary["md_duplicate_inventory_rows"], 2)
            self.assertEqual(
                summary["local_objects_without_md_match_archive_candidates"], 2
            )
            self.assertEqual(summary["decisions"], 0)

            connection = sqlite3.connect(run_dir / "reconciliation.sqlite")
            try:
                duplicate_ids = {
                    row[0]
                    for row in connection.execute(
                        """
                        SELECT md_object_id FROM md_objects
                        WHERE duplicate_inventory = 1
                        """
                    )
                }
                self.assertEqual(duplicate_ids, {"1", "2"})
                local_only = connection.execute(
                    """
                    SELECT representative_inventory, local_only_archive_candidate
                    FROM local_objects
                    WHERE representative_inventory = 'LOCAL-ONLY-01'
                    """
                ).fetchone()
                self.assertEqual(local_only, ("LOCAL-ONLY-01", 1))
                decision_count = connection.execute(
                    "SELECT COUNT(*) FROM decisions"
                ).fetchone()[0]
                self.assertEqual(decision_count, 0)
                flyer_candidate_count = connection.execute(
                    """
                    SELECT local_candidate_count FROM md_objects
                    WHERE md_object_id = '3'
                    """
                ).fetchone()[0]
                self.assertEqual(flyer_candidate_count, 0)
            finally:
                connection.close()


if __name__ == "__main__":
    unittest.main()
