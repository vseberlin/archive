from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path


MODULE_PATH = (
    Path(__file__).resolve().parents[2]
    / "tools/archive-preservation/kurt_schwarz_unusual_review.py"
)
SPEC = importlib.util.spec_from_file_location("kurt_schwarz_unusual_review", MODULE_PATH)
assert SPEC and SPEC.loader
review = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(review)


class KurtSchwarzUnusualReviewTests(unittest.TestCase):
    def test_baecker_tiff_is_a_held_local_object_master(self) -> None:
        state, record, role = review.classify(
            "archive/WF/Baecker/SCANS-BAECKER/KS-6-BZ_0588-3.tif"
        )
        self.assertEqual("preserve_local_only_image", state)
        self.assertEqual("local-shared:KS-6-BZ_0588-3", record)
        self.assertEqual("local_object_master", role)

    def test_book_project_pdf_is_contextual_document(self) -> None:
        state, _, role = review.classify(
            "archive/WF/Buchprojekt/Buchprojket_Kurt-Schwarz-Bilder.pdf"
        )
        self.assertEqual("preserve_contextual_project_document", state)
        self.assertEqual("project_document", role)


if __name__ == "__main__":
    unittest.main()
