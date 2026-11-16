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
    / "wf_sender_flyer_source_audit.py"
)
SPEC = importlib.util.spec_from_file_location("wf_sender_flyer_source_audit", SCRIPT)
AUDIT = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
sys.modules[SPEC.name] = AUDIT
SPEC.loader.exec_module(AUDIT)


def _file(path: Path, payload: bytes = b"x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path


class WfSenderFlyerSourceAuditTest(unittest.TestCase):
    def test_regular_flyer_maps_to_hfs_pair(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pdf = _file(root / "PDF/HFS-1953-01/HFS-1953-01.pdf")
            tiff_1 = _file(root / "TIF/HFS-1953-01/HFS-1953-01_001.tif")
            tiff_2 = _file(root / "TIF/HFS-1953-01/HFS-1953-01_002.tif")
            binding = AUDIT.local_binding(
                "WFS-FB-1953-01", root, root / "unused-split-root"
            )
            self.assertIsNotNone(binding)
            assert binding is not None
            self.assertEqual(pdf, binding.pdf_path)
            self.assertEqual((tiff_1, tiff_2), binding.tiff_paths)

    def test_split_aggregate_binds_disjoint_tiff_pages(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "source"
            split = Path(directory) / "split"
            folder = "HFS-1951-54_Sonderausgaben-03"
            pages = tuple(
                _file(root / f"TIF/{folder}/{folder}_{sequence:03d}.tif")
                for sequence in range(1, 5)
            )
            first_pdf = _file(split / "WFS-1951-54_SA-03-1.pdf")
            second_pdf = _file(split / "WFS-1951-54_SA-03-2.pdf")
            first = AUDIT.local_binding("WFS-1955_SA-03-1", root, split)
            second = AUDIT.local_binding("WFS-1955_SA-03-2", root, split)
            self.assertIsNotNone(first)
            self.assertIsNotNone(second)
            assert first is not None and second is not None
            self.assertEqual(first_pdf, first.pdf_path)
            self.assertEqual(second_pdf, second.pdf_path)
            self.assertEqual(pages[:2], first.tiff_paths)
            self.assertEqual(pages[2:], second.tiff_paths)

    def test_known_pdf_only_flyer_has_no_hfs_binding(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertIsNone(AUDIT.local_binding("WFS-FB-1952-01", root, root))
