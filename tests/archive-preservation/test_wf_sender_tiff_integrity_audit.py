import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = (
    ROOT
    / "tools"
    / "archive-preservation"
    / "wf_sender_tiff_integrity_audit.py"
)
SPEC = importlib.util.spec_from_file_location(
    "wf_sender_tiff_integrity_audit", SCRIPT
)
AUDIT = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
sys.modules[SPEC.name] = AUDIT
SPEC.loader.exec_module(AUDIT)


class WfSenderTiffIntegrityAuditTest(unittest.TestCase):
    def test_known_incomplete_object_is_never_promoted_to_master_page(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "WFS-1971-19_001.tif"
            path.write_bytes(b"fixture")
            stat = path.stat()
            row = {
                "inventory": "WFS-1971-19",
                "sequence": "1",
                "source_path": str(path),
                "source_size_bytes": stat.st_size,
                "source_mtime_ns": stat.st_mtime_ns,
            }
            result = type(
                "Result",
                (),
                {
                    "returncode": 0,
                    "stdout": "TIFF|100|200|8|None",
                    "stderr": "",
                },
            )()
            with patch.object(AUDIT.subprocess, "run", return_value=result):
                audited = AUDIT.audit_one(row)
            self.assertEqual(
                "damaged_source_tiff_fragment", audited["archive_role"]
            )
            self.assertEqual("readable", audited["integrity_state"])

    def test_unexpected_decode_failure_blocks_representation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "WFS-1970-01_001.tif"
            path.write_bytes(b"fixture")
            stat = path.stat()
            row = {
                "inventory": "WFS-1970-01",
                "source_path": str(path),
                "source_size_bytes": stat.st_size,
                "source_mtime_ns": stat.st_mtime_ns,
            }
            result = type(
                "Result",
                (),
                {"returncode": 1, "stdout": "", "stderr": "decode failed"},
            )()
            with patch.object(AUDIT.subprocess, "run", return_value=result):
                audited = AUDIT.audit_one(row)
            self.assertEqual("blocked", audited["archive_role"])
            self.assertEqual(
                "unexpected_decode_error", audited["integrity_state"]
            )

    def test_documented_failed_page_is_an_expected_fragment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "WFS-1966-42_003.tif"
            path.write_bytes(b"fixture")
            stat = path.stat()
            row = {
                "inventory": "WFS-1966-42",
                "sequence": "3",
                "source_path": str(path),
                "source_size_bytes": stat.st_size,
                "source_mtime_ns": stat.st_mtime_ns,
            }
            result = type(
                "Result",
                (),
                {
                    "returncode": 1,
                    "stdout": "",
                    "stderr": "Read error on strip; TIFFFillStrip",
                },
            )()
            with patch.object(AUDIT.subprocess, "run", return_value=result):
                audited = AUDIT.audit_one(row)
            self.assertEqual(
                "expected_corrupt_fragment", audited["integrity_state"]
            )
            self.assertEqual(
                "damaged_source_tiff_fragment", audited["archive_role"]
            )

    def test_documented_page_with_different_failure_stays_unexpected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "WFS-1966-42_003.tif"
            path.write_bytes(b"fixture")
            stat = path.stat()
            row = {
                "inventory": "WFS-1966-42",
                "sequence": "3",
                "source_path": str(path),
                "source_size_bytes": stat.st_size,
                "source_mtime_ns": stat.st_mtime_ns,
            }
            result = type(
                "Result",
                (),
                {"returncode": 1, "stdout": "", "stderr": "permission denied"},
            )()
            with patch.object(AUDIT.subprocess, "run", return_value=result):
                audited = AUDIT.audit_one(row)
            self.assertEqual(
                "unexpected_decode_error", audited["integrity_state"]
            )
            self.assertEqual("blocked", audited["archive_role"])

    def test_reuse_rechecks_only_prior_unexpected_rows(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base_run = Path(directory)
            source_rows = [
                {
                    "local_object_id": "733",
                    "inventory": "WFS-1966-42",
                    "local_group_id": "2530",
                    "sequence": "1",
                    "source_path": "/source/page-1.tif",
                    "source_size_bytes": "10",
                    "source_mtime_ns": "20",
                },
                {
                    "local_object_id": "733",
                    "inventory": "WFS-1966-42",
                    "local_group_id": "2530",
                    "sequence": "3",
                    "source_path": "/source/page-3.tif",
                    "source_size_bytes": "30",
                    "source_mtime_ns": "40",
                },
            ]
            base_rows = [
                {
                    **source_rows[0],
                    "integrity_state": "readable",
                    "archive_role": "preservation_master_page",
                },
                {
                    **source_rows[1],
                    "integrity_state": "unexpected_decode_error",
                    "archive_role": "blocked",
                },
            ]
            (base_run / "summary.json").write_text(
                json.dumps(
                    {
                        "format": AUDIT.FORMAT,
                        "source_mount": "source mount",
                        "selected_tiff_files": 2,
                        "excluded_cross_unit_trafo_tiff_files": 214,
                    }
                ),
                encoding="utf-8",
            )
            (base_run / "tiff-integrity.csv").write_bytes(
                AUDIT.csv_bytes(base_rows, AUDIT.FIELDS)
            )
            rechecked_row = {
                **source_rows[1],
                "integrity_state": "expected_corrupt_fragment",
                "archive_role": "damaged_source_tiff_fragment",
            }
            with patch.object(
                AUDIT,
                "audit_one",
                return_value=rechecked_row,
            ) as audit_one:
                completed, rechecked = AUDIT.reuse_completed_audit(
                    base_run,
                    source_rows,
                    214,
                    "source mount",
                )
            self.assertEqual(1, rechecked)
            audit_one.assert_called_once_with(source_rows[1])
            self.assertEqual(
                "damaged_source_tiff_fragment",
                completed[0]["archive_role"],
            )
