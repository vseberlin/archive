import importlib.util
import unittest
from pathlib import Path


SCRIPT = (
    Path(__file__).parents[2]
    / "tools/archive-preservation/kurt_schwarz_shared_workbooks.py"
)
SPEC = importlib.util.spec_from_file_location("kurt_schwarz_shared_workbooks", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(MODULE)


class KurtSchwarzSharedWorkbookTests(unittest.TestCase):
    def test_unique_headers_names_blank_and_duplicate_columns(self):
        self.assertEqual(
            ["Name", "column_2", "Name_2"],
            MODULE.unique_headers(["Name", "", "Name"]),
        )

    def test_record_kinds(self):
        self.assertEqual(
            "finding_aid",
            MODULE.record_kind(Path("Kurt-Schwarz_GESAMTVERZEICHNIS_09.xlsx")),
        )
        self.assertEqual(
            "newspaper_research",
            MODULE.record_kind(Path("BerlinerZeitung_Kurt-Schwarz_03.xlsx")),
        )
        self.assertEqual(
            "upload_metadata", MODULE.record_kind(Path("7b._Upload_03.xlsx"))
        )

    def test_inventory_detection_prefers_explicit_column(self):
        self.assertEqual(
            "KS-6-BZ_0107-1",
            MODULE.find_inventory(
                {
                    "inventory_number": "KS-6-BZ_0107-1",
                    "notes": "not an inventory",
                }
            ),
        )

    def test_inventory_tail_ignores_renumbered_prefix(self):
        self.assertEqual(
            "bz_0148-2", MODULE.inventory_tail("KS-7-BZ_0148-2")
        )

    def test_normalized_title_removes_rights_suffix(self):
        self.assertEqual(
            "platzkonzert",
            MODULE.normalized_title(
                "Platzkonzert. SW-Foto © Kurt Schwarz."
            ),
        )


if __name__ == "__main__":
    unittest.main()
