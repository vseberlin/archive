import importlib.util
import sys
import unittest
from pathlib import Path


SCRIPT = (
    Path(__file__).parents[2]
    / "tools/archive-preservation/kurt_schwarz_documentation_consolidation.py"
)
sys.path.insert(0, str(SCRIPT.parent))
SPEC = importlib.util.spec_from_file_location(
    "kurt_schwarz_documentation_consolidation", SCRIPT
)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(MODULE)


class DocumentationConsolidationTests(unittest.TestCase):
    def classify(self, path, relationship="exclusive_usb_scope", restriction=""):
        return MODULE.classify_document(path, relationship, restriction)

    def test_office_lock_is_historical_noise(self):
        family, relationship, disposition, packaged, _ = self.classify(
            "documents/usb/~$draft.xlsx"
        )
        self.assertEqual("office_lock_file", family)
        self.assertEqual("excluded", relationship)
        self.assertEqual("package_historical", disposition)
        self.assertTrue(packaged)

    def test_wf_sender_issue_is_cross_unit_reference(self):
        family, relationship, disposition, packaged, _ = self.classify(
            "documents/usb/WF-Sender/WFS-1985-12.pdf"
        )
        self.assertEqual("cross_unit_wf_sender", family)
        self.assertEqual("mixed", relationship)
        self.assertEqual("cross_unit_reference", disposition)
        self.assertFalse(packaged)

    def test_catalog_generation_enters_historical_package(self):
        family, relationship, disposition, packaged, _ = self.classify(
            "documents/usb/mdExport_Gesamtverzeichnis_Kurt-Schwarz.xlsx"
        )
        self.assertEqual("catalog_or_description_source", family)
        self.assertEqual("direct", relationship)
        self.assertEqual("package_historical", disposition)
        self.assertTrue(packaged)

    def test_collection_555_is_not_a_kurt_subdivision(self):
        pairs = [("555", "Pressebildarchiv WF"), ("852", "Kurt subdivision")]
        result = [pair for pair in pairs if pair[0] and pair[0] != "555"]
        self.assertEqual([("852", "Kurt subdivision")], result)

    def test_evidence_root_archive_component_is_not_a_draft_marker(self):
        self.assertFalse(
            MODULE.is_historical_draft_path(
                "documents/usb/archive/WF/Kurt Schwarz/Industriesalon_Kurt-Schwarz.pdf"
            )
        )
        self.assertTrue(
            MODULE.is_historical_draft_path(
                "documents/usb/archive/WF/Kurt Schwarz/Archiv/old-version.pdf"
            )
        )

    def test_effective_value_matches_workbench_fallback(self):
        field = {
            "md_value": "A; B",
            "local_value": "A, B",
            "comparison_state": "same",
        }
        self.assertEqual(("A; B", "same"), MODULE.effective_value(field, None))
        decision = {"action": "defer", "accepted_value": ""}
        self.assertEqual(("A; B", "defer"), MODULE.effective_value(field, decision))


if __name__ == "__main__":
    unittest.main()
