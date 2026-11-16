import importlib.util
import unittest
from pathlib import Path


SCRIPT = (
    Path(__file__).resolve().parents[2]
    / "tools/archive-preservation/wf_sender_closeout.py"
)
SPEC = importlib.util.spec_from_file_location("wf_sender_closeout", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(MODULE)


class WfSenderCloseoutTests(unittest.TestCase):
    def test_website_and_lightroom_are_deferred(self):
        self.assertTrue(
            MODULE.is_deferred_shared(
                str(MODULE.WEBSITE / "Backup/example.zip")
            )
        )
        self.assertTrue(
            MODULE.is_deferred_shared(
                str(MODULE.LIGHTROOM / "catalog.lrcat")
            )
        )
        self.assertFalse(
            MODULE.is_deferred_shared(
                str(MODULE.PROJECT / "Scans/WFS-1970-01.pdf")
            )
        )

    def test_cross_project_trafo_sources_are_excluded(self):
        self.assertTrue(
            MODULE.is_cross_project(
                str(
                    MODULE.PROJECT
                    / "Scans_WF-Sender_MIK/PDF3/1953-Trafo/WFS-1953-01.pdf"
                )
            )
        )
        self.assertFalse(
            MODULE.is_cross_project(
                str(MODULE.PROJECT / "Uploads/aus-TRO-Upload/WFS-FB-1953-01.pdf")
            )
        )

    def test_workflow_noise_is_bounded(self):
        self.assertTrue(
            MODULE.is_workflow_noise(
                str(MODULE.PROJECT / "Stundenabrechnungen/Belege/receipt.pdf")
            )
        )
        self.assertTrue(
            MODULE.is_workflow_noise(str(MODULE.PROJECT / "~$working.xlsx"))
        )
        self.assertFalse(
            MODULE.is_workflow_noise(str(MODULE.PROJECT / "WFS-1970.xlsx"))
        )

    def test_only_usb_paths_can_enter_drain_list(self):
        self.assertTrue(
            MODULE.is_usb_path(str(MODULE.PROJECT / "WFS-1970.xlsx"))
        )
        self.assertFalse(
            MODULE.is_usb_path(
                "/mnt/volume/iss-archive-work/wf-sender/work/coverage.json"
            )
        )

    def test_legacy_bare_sha256_is_normalized(self):
        digest = "a" * 64
        self.assertEqual(
            MODULE.normalize_sha256(digest), f"sha256:{digest}"
        )
        self.assertEqual(
            MODULE.normalize_sha256(f"sha256:{digest}"),
            f"sha256:{digest}",
        )


if __name__ == "__main__":
    unittest.main()
