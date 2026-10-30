import importlib.util
import unittest
from pathlib import Path


SCRIPT = (
    Path(__file__).parents[2]
    / "tools/archive-preservation/kurt_schwarz_deep_review.py"
)
SPEC = importlib.util.spec_from_file_location("kurt_schwarz_deep_review", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(MODULE)


class KurtSchwarzDeepReviewTests(unittest.TestCase):
    def test_website_is_boundary_hold(self):
        result = MODULE.classify("archive/WF/WF-Sender Projekt/Webseite/site.sql")
        self.assertEqual("website_or_lightroom_metadata_source", result[0])
        self.assertEqual("boundary_hold", result[1])

    def test_cross_project_xmp_is_metadata_only(self):
        result = MODULE.classify("archive/WF/Baecker/FUJI-Scans/FXE41312.xmp")
        self.assertEqual("cross_project_creator_metadata", result[0])
        self.assertEqual("catalog_verified_metadata_only", result[2])

    def test_recovered_windows_payload_is_noise(self):
        result = MODULE.classify(
            "archive/MEDIATHEK-Backup/Alte PC-Datein/Windows/System32/file.xml"
        )
        self.assertEqual("recovery_artifact_false_positive", result[0])
        self.assertEqual("excluded_noise", result[1])

    def test_google_backup_keeps_shared_container(self):
        result = MODULE.classify(
            "archive/WF/GoogleDocsBackup/"
            "drive-download-20220713T123037Z-001.zip"
        )
        self.assertEqual("direct_project_members_in_shared_container", result[0])
        self.assertEqual("retain_shared_container", result[4])

    def test_general_book_pdf_is_metadata_only(self):
        result = MODULE.classify(
            "archive/WF/Buchprojekt/2TXT03226Schoeneweide_korr.pdf"
        )
        self.assertEqual("cross_project_book_context", result[0])
        self.assertEqual("catalog_verified_metadata_only", result[2])

    def test_kurt_book_image_pdf_is_copied(self):
        result = MODULE.classify(
            "archive/WF/Buchprojekt/Buchprojket_Kurt-Schwarz-Bilder.pdf"
        )
        self.assertEqual("copy_exact_document", result[2])


if __name__ == "__main__":
    unittest.main()
