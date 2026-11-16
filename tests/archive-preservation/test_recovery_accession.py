from __future__ import annotations

import sys
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]
TOOL_ROOT = REPO_ROOT / "tools" / "archive-preservation"
if str(TOOL_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOL_ROOT))

from workflows import recovery_accession  # noqa: E402


class RecoveryAccessJpegTest(unittest.TestCase):
    def test_cross_process_accession_lock_refuses_overlap(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with (
                mock.patch.object(
                    recovery_accession.catalog_core,
                    "assert_safe_work_root",
                    return_value=root,
                ),
                mock.patch.object(
                    recovery_accession.fcntl,
                    "flock",
                    side_effect=BlockingIOError,
                ),
            ):
                with self.assertRaisesRegex(
                    recovery_accession.RecoveryAccessionError,
                    "another recovery accession",
                ):
                    recovery_accession.accession_and_publish(
                        root,
                        root / "independent",
                        unit_id="WF-PARAGON",
                        entity_id="ISS-2026-TEST-A",
                        inventory="VLV-1112",
                        master_evidence={},
                        derivative_evidence=None,
                        museum_digital={},
                        actor="Fixture Archivist",
                        tool_version="test",
                    )

    def test_paragon_keyword_is_ensured_before_publication(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            master = base / "master.tif"
            master.write_bytes(b"tiff fixture")
            catalog = mock.MagicMock()
            with (
                mock.patch.object(
                    recovery_accession.catalog_core,
                    "assert_safe_work_root",
                    side_effect=[base / "primary", base / "independent"],
                ),
                mock.patch.object(
                    recovery_accession, "_assert_source", return_value=master
                ),
                mock.patch.object(
                    recovery_accession.catalog_core,
                    "connect_catalog",
                    return_value=catalog,
                ),
                mock.patch.object(
                    recovery_accession.editor_reconcile,
                    "add_archive_keyword",
                ) as add_keyword,
                mock.patch.object(
                    recovery_accession.catalog_core,
                    "publish_generation",
                    side_effect=RuntimeError("publication reached"),
                ),
            ):
                with self.assertRaisesRegex(RuntimeError, "publication reached"):
                    recovery_accession.accession_and_publish(
                        base / "primary",
                        base / "independent",
                        unit_id="WF-PARAGON",
                        entity_id="ISS-2026-TEST-A",
                        inventory="VLV-1112",
                        master_evidence={
                            "content_digest": "sha256:" + "a" * 64,
                            "evidence_id": "VIS-TIFF",
                        },
                        derivative_evidence=None,
                        museum_digital={"object_id": "70001"},
                        actor="Fixture Archivist",
                        tool_version="test",
                    )
            add_keyword.assert_called_once_with(
                catalog,
                unit_id="WF-PARAGON",
                entity_id="ISS-2026-TEST-A",
                value="Paragon",
                actor="Fixture Archivist",
                evidence="unit:WF-PARAGON",
            )

    def test_full_resolution_jpeg_is_generated_resumably(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "master.tif"
            destination = root / "derivatives" / "item.jpg"
            subprocess.run(
                ["magick", "-size", "640x480", "xc:#5078a0", str(source)],
                check=True,
                capture_output=True,
            )
            first_digest, first_technical = (
                recovery_accession._ensure_generated_access_jpeg(
                    source,
                    destination,
                    width_px=640,
                    height_px=480,
                    scratch=root / "scratch",
                )
            )
            self.assertEqual((640, 480), (
                first_technical["width"], first_technical["height"]
            ))
            self.assertTrue(destination.is_file())
            second_digest, second_technical = (
                recovery_accession._ensure_generated_access_jpeg(
                    source,
                    destination,
                    width_px=640,
                    height_px=480,
                    scratch=root / "scratch",
                )
            )
            self.assertEqual(first_digest, second_digest)
            self.assertEqual(first_technical, second_technical)

    def test_tiff_master_refuses_supplied_derivative(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            master = base / "master.tif"
            derivative = base / "legacy.jpg"
            master.write_bytes(b"tiff fixture")
            derivative.write_bytes(b"jpeg fixture")
            evidence = {
                "content_digest": "sha256:" + "a" * 64,
                "evidence_id": "VIS-TIFF",
            }
            derivative_evidence = {
                "content_digest": "sha256:" + "b" * 64,
                "evidence_id": "VIS-JPEG",
            }
            with (
                mock.patch.object(
                    recovery_accession.catalog_core,
                    "assert_safe_work_root",
                    side_effect=[base / "primary", base / "independent"],
                ),
                mock.patch.object(
                    recovery_accession,
                    "_assert_source",
                    side_effect=[master, derivative],
                ),
            ):
                with self.assertRaisesRegex(
                    recovery_accession.RecoveryAccessionError,
                    "uniformly generated archive derivative",
                ):
                    recovery_accession.accession_and_publish(
                        base / "primary",
                        base / "independent",
                        unit_id="UNIT-ONE",
                        entity_id="ISS-2026-TEST-A",
                        inventory="VLV-1112",
                        master_evidence=evidence,
                        derivative_evidence=derivative_evidence,
                        museum_digital={},
                        actor="Fixture Archivist",
                        tool_version="test",
                    )


if __name__ == "__main__":
    unittest.main()
