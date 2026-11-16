import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = (
    ROOT
    / "tools"
    / "archive-preservation"
    / "wf_sender_accession_plan.py"
)
SPEC = importlib.util.spec_from_file_location("wf_sender_accession_plan", SCRIPT)
PLAN = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
sys.modules[SPEC.name] = PLAN
SPEC.loader.exec_module(PLAN)


class WfSenderAccessionPlanTest(unittest.TestCase):
    def test_representation_records_exact_source_observation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.tif"
            source.write_bytes(b"tiff")
            row = PLAN.representation(
                carrier_key="WFS-TEST",
                inventory="WFS-TEST",
                md_ids="1",
                role="preservation_master_page",
                sequence=1,
                source_kind="fixture",
                source_path=source,
                final_path="masters/WFS-TEST/pages/source.tif",
                basis="fixture",
                source_sha256="abc",
            )
            self.assertEqual(source.stat().st_size, row["source_size_bytes"])
            self.assertEqual(source.stat().st_mtime_ns, row["source_mtime_ns"])
            self.assertEqual("exact_source_bytes", row["copy_policy"])
            self.assertEqual("abc", row["source_sha256"])

    def test_preferred_md_uses_exact_inventory_for_shared_carrier(self) -> None:
        rows = [
            {"md_object_id": "56022", "inventory_raw": "WFS-1990-SN-24"},
            {"md_object_id": "70622", "inventory_raw": "WFS-1989-24"},
        ]
        selected = PLAN.preferred_md(rows, "WFS-1989-24")
        self.assertEqual("70622", selected["md_object_id"])

    def test_damaged_tiff_set_uses_pdf_as_preservation_master(self) -> None:
        policy, pdf_role = PLAN.primary_representation_policy(
            "WFS-1966-42",
            [
                {"archive_role": "damaged_source_tiff_fragment"},
                {"archive_role": "damaged_source_tiff_fragment"},
            ],
        )
        self.assertEqual(
            "pdf_master_with_damaged_tiff_fragments",
            policy,
        )
        self.assertEqual("preservation_master_pdf", pdf_role)

    def test_mixed_tiff_roles_are_refused(self) -> None:
        with self.assertRaises(PLAN.AccessionPlanError):
            PLAN.primary_representation_policy(
                "WFS-TEST",
                [
                    {"archive_role": "preservation_master_page"},
                    {"archive_role": "damaged_source_tiff_fragment"},
                ],
            )
