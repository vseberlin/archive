import importlib.util
import unittest
from pathlib import Path


SCRIPT = (
    Path(__file__).parents[2]
    / "tools/archive-preservation/kurt_schwarz_accession_plan.py"
)
SPEC = importlib.util.spec_from_file_location("kurt_schwarz_accession_plan", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(MODULE)


class KurtSchwarzAccessionPlanTests(unittest.TestCase):
    def test_960px_export_is_reviewed_noise(self):
        include, role, _ = MODULE.source_policy(
            "archive/WF/Kurt Schwarz/UPLOADS/"
            "Alle-md-Bilder-als-960px-JPGs/KS-6-BZ_0001.jpg",
            ".jpg",
        )
        self.assertFalse(include)
        self.assertEqual("reviewed_noise", role)

    def test_tiff_is_always_preserved(self):
        include, role, _ = MODULE.source_policy(
            "archive/WF/Kurt Schwarz/UPLOADS/a.tif", ".tif"
        )
        self.assertTrue(include)
        self.assertEqual("preservation_source_tiff", role)

    def test_restricted_paths_are_detected(self):
        self.assertTrue(
            MODULE.is_restricted(
                "archive/WF/Kurt Schwarz/Stundenabrechnungen_Ausgaben/a.pdf"
            )
        )

    def test_safe_component_removes_path_separators(self):
        self.assertEqual("a_b", MODULE.safe_component("a/b"))


if __name__ == "__main__":
    unittest.main()
