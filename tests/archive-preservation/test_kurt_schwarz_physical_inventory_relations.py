import importlib.util
import json
import sys
import unittest
from pathlib import Path


SCRIPT = (
    Path(__file__).parents[2]
    / "tools/archive-preservation/kurt_schwarz_physical_inventory_relations.py"
)
sys.path.insert(0, str(SCRIPT.parent))
SPEC = importlib.util.spec_from_file_location(
    "kurt_schwarz_physical_inventory_relations", SCRIPT
)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(MODULE)


class KurtSchwarzPhysicalInventoryRelationTests(unittest.TestCase):
    def test_group_signature_normalizes_padding_and_separator(self):
        physical = MODULE.physical_relation_key("K02_B03_T035")
        self.assertEqual(("group", "B", 3, 35, ""), physical)
        self.assertIn(
            physical, MODULE.candidate_relation_keys("B03_T035_004.tif")
        )

    def test_variant_prefers_variant_but_retains_base_fallback(self):
        keys = MODULE.candidate_relation_keys("B03_T034_C_001.tif")
        self.assertEqual(("group", "B", 3, 34, "C"), keys[0])
        self.assertEqual(("group", "B", 3, 34, ""), keys[1])

    def test_flat_box_signature_maps_to_scan_filename(self):
        physical = MODULE.physical_relation_key("K06_F061")
        self.assertEqual(("flat", 6, "F", 61, ""), physical)
        self.assertIn(
            physical, MODULE.candidate_relation_keys("K06_F061_002.tif")
        )

    def test_lost_formula_prefix_can_be_reconstructed_from_source_columns(self):
        record = {
            "Karton": "K10",
            "Abschnitt Nr.": "L01",
            "Titel Nr.": "T001",
            "SIGNATUR": "-L01_T001",
        }
        reconstructed = "_".join(
            MODULE.first(record, name)
            for name in ("Karton", "Abschnitt Nr.", "Titel Nr.")
        )
        self.assertEqual(
            ("group", "L", 1, 1, ""),
            MODULE.physical_relation_key(reconstructed),
        )

    def test_unanimous_existing_alias_can_bridge_section_renumbering(self):
        physical_key = ("group", "H", 9, 117, "")
        digital_key = ("group", "H", 8, 117, "")
        physical = {physical_key: {"physical_signature": "K08_H09_T117"}}
        self.assertEqual(
            "K08_H09_T117",
            MODULE.resolve_relation(
                "H08_T117_002.tif", physical, {digital_key: physical_key}
            )["physical_signature"],
        )

    def test_enrichment_keeps_new_object_unassigned_and_pending(self):
        candidate = {
            "candidate_id": "KSC-test",
            "queue_class": "new_local_review",
            "metadata_matches_json": "[]",
            "metadata_match_count": "0",
            "selected_metadata_json": "{}",
            "selected_metadata_basis": "no_reliable_metadata_selected",
            "metadata_summary": "No reliable metadata selected",
            "review_basis": "No unique visual match.",
        }
        physical = {
            "physical_signature": "K01_A07_T142",
            "box": "K01",
            "section_title": "Berliner Alltag",
            "bag_description": "Fasching in Werder 2/83",
            "year": "1983",
            "note": "",
            "workbook_sheet": "Tabellen einzeln",
            "workbook_row": "143",
        }
        enriched, changed = MODULE.enrich_candidate(
            candidate, physical, Path("/read-only/finding-aid.xlsx")
        )
        self.assertTrue(changed)
        self.assertEqual("new_local_review", enriched["queue_class"])
        selected = json.loads(enriched["selected_metadata_json"])
        self.assertNotIn("inventory", selected)
        self.assertEqual("Fasching in Werder 2/83", selected["title"])
        self.assertEqual("1983", selected["date_text"])
        self.assertEqual(
            "physical_finding_aid_group_signature",
            enriched["selected_metadata_basis"],
        )

    def test_existing_object_receives_evidence_but_no_selected_overwrite(self):
        candidate = {
            "queue_class": "existing_object_reconciled",
            "metadata_matches_json": "[]",
            "metadata_match_count": "0",
            "selected_metadata_json": '{"title": "Existing"}',
        }
        physical = {
            "physical_signature": "K01_A01_T02",
            "box": "K01",
            "section_title": "Berlin",
            "bag_description": "Handel 1968",
            "year": "1968",
            "note": "",
            "workbook_sheet": "Tabellen einzeln",
            "workbook_row": "3",
        }
        enriched, changed = MODULE.enrich_candidate(
            candidate, physical, Path("/read-only/finding-aid.xlsx")
        )
        self.assertFalse(changed)
        self.assertEqual('{"title": "Existing"}', enriched["selected_metadata_json"])
        self.assertEqual(1, int(enriched["metadata_match_count"]))


if __name__ == "__main__":
    unittest.main()
