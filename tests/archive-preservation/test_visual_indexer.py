from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

try:
    from PIL import Image
except ImportError:
    Image = None


TOOLS = (
    Path(__file__).resolve().parents[2]
    / "tools"
    / "archive-preservation"
)
sys.path.insert(0, str(TOOLS))
visual_indexer = None
shutdown_watchdog = None
candidate_search = None
if Image is not None:
    SPEC = importlib.util.spec_from_file_location(
        "visual_indexer",
        TOOLS / "visual_indexer.py",
    )
    assert SPEC and SPEC.loader
    visual_indexer = importlib.util.module_from_spec(SPEC)
    SPEC.loader.exec_module(visual_indexer)
    WATCHDOG_SPEC = importlib.util.spec_from_file_location(
        "shutdown_watchdog",
        Path("/home/vladimir/archive-visual-index/bin/shutdown-if-complete.py"),
    )
    assert WATCHDOG_SPEC and WATCHDOG_SPEC.loader
    shutdown_watchdog = importlib.util.module_from_spec(WATCHDOG_SPEC)
    WATCHDOG_SPEC.loader.exec_module(shutdown_watchdog)
    SEARCH_SPEC = importlib.util.spec_from_file_location(
        "visual_candidate_search",
        TOOLS / "visual_candidate_search.py",
    )
    assert SEARCH_SPEC and SEARCH_SPEC.loader
    candidate_search = importlib.util.module_from_spec(SEARCH_SPEC)
    SEARCH_SPEC.loader.exec_module(candidate_search)


@unittest.skipUnless(
    Image is not None and visual_indexer is not None,
    "visual-index dependencies are not in the pinned validator",
)
class VisualIndexerTest(unittest.TestCase):
    def test_dimensions_fall_back_to_vipsheader(self) -> None:
        with (
            mock.patch("PIL.Image.open", side_effect=OSError("fixture")),
            mock.patch.object(
                visual_indexer.subprocess,
                "run",
                side_effect=[
                    mock.Mock(returncode=0, stdout="16508\n", stderr=""),
                    mock.Mock(returncode=0, stdout="23290\n", stderr=""),
                ],
            ) as run,
        ):
            dimensions = visual_indexer._image_dimensions(Path("large.tif"))
        self.assertEqual((16508, 23290), dimensions)
        self.assertEqual(2, run.call_count)

    def test_writable_usb_root_is_refused_before_index_creation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = base / "usb"
            work = base / "work"
            source.mkdir()
            work.mkdir()
            original = visual_indexer.visual_index.PROTECTED_SOURCE_ROOTS
            try:
                visual_indexer.visual_index.PROTECTED_SOURCE_ROOTS = (base,)
                with (
                    mock.patch.object(
                        visual_indexer.source_registry,
                        "root_kind",
                        return_value="usb",
                    ),
                    mock.patch.object(
                        visual_indexer.source_registry,
                        "root_is_read_only",
                        return_value=False,
                    ),
                    self.assertRaisesRegex(
                        visual_indexer.VisualIndexerError,
                        "mounted read-only",
                    ),
                ):
                    visual_indexer.run_index(
                        database=work / "visual-index.sqlite",
                        work_root=work,
                        roots=[source],
                        token="0" * 32,
                        timeout=30,
                        min_free_bytes=0,
                    )
            finally:
                visual_indexer.visual_index.PROTECTED_SOURCE_ROOTS = original
            self.assertFalse((work / "visual-index.sqlite").exists())

    def test_complete_index_is_restartable_and_marks_exact_token(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            protected = base / "protected"
            archive = protected / "archive"
            intake = protected / "archive-intake"
            work = base / "work"
            archive.mkdir(parents=True)
            intake.mkdir()
            work.mkdir()
            Image.new("RGB", (80, 60), "#775533").save(
                archive / "one.tif"
            )
            Image.new("RGB", (40, 50), "#337755").save(
                intake / "two.jpg"
            )
            original = visual_indexer.visual_index.PROTECTED_SOURCE_ROOTS
            try:
                visual_indexer.visual_index.PROTECTED_SOURCE_ROOTS = (
                    protected,
                )
                database = work / "visual-index.sqlite"
                first = visual_indexer.run_index(
                    database=database,
                    work_root=work,
                    roots=[archive, intake],
                    token="1" * 32,
                    timeout=30,
                    min_free_bytes=0,
                )
                second = visual_indexer.run_index(
                    database=database,
                    work_root=work,
                    roots=[archive, intake],
                    token="2" * 32,
                    timeout=30,
                    min_free_bytes=0,
                )
            finally:
                visual_indexer.visual_index.PROTECTED_SOURCE_ROOTS = original
            self.assertEqual("complete", first["state"])
            self.assertEqual(2, first["indexed"])
            self.assertEqual(0, first["errors"])
            self.assertEqual(0, second["indexed"])
            self.assertTrue((work / "completions" / ("1" * 32 + ".json")).is_file())
            self.assertTrue((work / "completions" / ("2" * 32 + ".json")).is_file())
            connection = sqlite3.connect(database)
            try:
                self.assertEqual(
                    2,
                    connection.execute(
                        "SELECT count(*) FROM visual_assets"
                    ).fetchone()[0],
                )
                self.assertEqual(
                    6,
                    connection.execute(
                        "SELECT count(*) FROM visual_features"
                    ).fetchone()[0],
                )
                self.assertEqual(
                    2,
                    connection.execute(
                        """SELECT count(*) FROM index_runs
                           WHERE status = 'complete'"""
                    ).fetchone()[0],
                )
            finally:
                connection.close()

    def test_failed_token_resumes_the_same_run(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            protected = base / "protected"
            archive = protected / "archive"
            work = base / "work"
            archive.mkdir(parents=True)
            work.mkdir()
            Image.new("RGB", (32, 24), "#334455").save(
                archive / "resume.tif"
            )
            token = "5" * 32
            roots_json = json.dumps([str(archive.resolve())], separators=(",", ":"))
            database = work / "visual-index.sqlite"
            with visual_indexer.connect_index(database) as connection:
                connection.execute(
                    """INSERT INTO index_runs (
                           run_id, token, started_at, completed_at, status,
                           roots_json, note
                       ) VALUES (
                           'VINDEX-RESUME-FIXTURE', ?, 'fixture-start',
                           'fixture-stop', 'failed', ?, 'interrupted'
                       )""",
                    (token, roots_json),
                )
                connection.commit()
            original = visual_indexer.visual_index.PROTECTED_SOURCE_ROOTS
            try:
                visual_indexer.visual_index.PROTECTED_SOURCE_ROOTS = (
                    protected,
                )
                result = visual_indexer.run_index(
                    database=database,
                    work_root=work,
                    roots=[archive],
                    token=token,
                    timeout=30,
                    min_free_bytes=0,
                )
            finally:
                visual_indexer.visual_index.PROTECTED_SOURCE_ROOTS = original
            self.assertEqual("VINDEX-RESUME-FIXTURE", result["run_id"])
            with sqlite3.connect(database) as connection:
                self.assertEqual(
                    ("complete", 1),
                    connection.execute(
                        """SELECT status, count(*) FROM index_runs
                           WHERE token = ?""",
                        (token,),
                    ).fetchone(),
                )

    def test_watchdog_accepts_only_matching_completion_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            protected = base / "protected"
            archive = protected / "archive"
            intake = protected / "archive-intake"
            work = base / "work"
            archive.mkdir(parents=True)
            intake.mkdir()
            work.mkdir()
            (work / "shutdown-receipts").mkdir()
            Image.new("RGB", (20, 30), "#112233").save(
                archive / "one.tif"
            )
            token = "3" * 32
            original = visual_indexer.visual_index.PROTECTED_SOURCE_ROOTS
            try:
                visual_indexer.visual_index.PROTECTED_SOURCE_ROOTS = (
                    protected,
                )
                marker = visual_indexer.run_index(
                    database=work / "visual-index.sqlite",
                    work_root=work,
                    roots=[archive, intake],
                    token=token,
                    timeout=30,
                    min_free_bytes=0,
                )
            finally:
                visual_indexer.visual_index.PROTECTED_SOURCE_ROOTS = original
            marker_path = Path(marker["marker"])
            payload = json.loads(marker_path.read_text())
            payload["roots"] = sorted(shutdown_watchdog.EXPECTED_ROOTS)
            marker_path.write_text(
                json.dumps(payload, sort_keys=True, indent=2) + "\n"
            )
            (work / "current-run-token").write_text(token + "\n")
            observed_token, observed_marker = shutdown_watchdog.validate(work)
            self.assertEqual(token, observed_token)
            self.assertEqual("complete", observed_marker["state"])
            (work / "current-run-token").write_text("4" * 32 + "\n")
            with self.assertRaises(FileNotFoundError):
                shutdown_watchdog.validate(work)

    def test_candidate_search_ranks_without_asserting_identity(self) -> None:
        candidates = [
            {
                "asset_id": "far",
                "root_path": "/protected",
                "relative_path": "far.tif",
                "original_filename": "far.tif",
                "extension": "tif",
                "size_bytes": 2,
                "mtime_ns": 2,
                "width_px": 20,
                "height_px": 20,
                "ahash": "f" * 16,
                "dhash": "f" * 16,
                "phash": "f" * 16,
            },
            {
                "asset_id": "near",
                "root_path": "/protected",
                "relative_path": "near.tif",
                "original_filename": "near.tif",
                "extension": "tif",
                "size_bytes": 1,
                "mtime_ns": 1,
                "width_px": 10,
                "height_px": 10,
                "ahash": "0" * 16,
                "dhash": "0" * 16,
                "phash": "0" * 16,
            },
        ]
        ranked = candidate_search.rank_candidates(
            ("0" * 16, "0" * 16, "0" * 16),
            candidates,
            2,
        )
        self.assertEqual("near", ranked[0]["asset_id"])
        self.assertEqual("very_close", ranked[0]["distance_band"])
        self.assertNotIn("identity", ranked[0])

    def test_candidate_search_collapses_identical_visual_occurrences(self) -> None:
        common = {
            "original_filename": "same.tif",
            "extension": "tif",
            "size_bytes": 10,
            "mtime_ns": 20,
            "width_px": 30,
            "height_px": 40,
            "ahash": "0" * 16,
            "dhash": "0" * 16,
            "phash": "0" * 16,
        }
        candidates = [
            {
                **common,
                "asset_id": "usb-copy",
                "root_path": "/media/vladimir/Elements",
                "relative_path": "same.tif",
            },
            {
                **common,
                "asset_id": "archive-copy",
                "root_path": "/mnt/volume/archive",
                "relative_path": "same.tif",
            },
        ]
        ranked = candidate_search.rank_candidates(
            ("0" * 16, "0" * 16, "0" * 16), candidates, 10
        )
        self.assertEqual(1, len(ranked))
        self.assertEqual("archive-copy", ranked[0]["asset_id"])
        self.assertEqual(2, ranked[0]["occurrence_count"])
        self.assertEqual(
            "usb-copy",
            ranked[0]["alternate_occurrences"][0]["asset_id"],
        )


if __name__ == "__main__":
    unittest.main()
