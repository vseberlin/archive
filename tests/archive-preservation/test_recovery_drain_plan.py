import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path


MODULE_PATH = (
    Path(__file__).resolve().parents[2]
    / "tools"
    / "archive-preservation"
    / "recovery_drain_plan.py"
)
SPEC = importlib.util.spec_from_file_location("recovery_drain_plan", MODULE_PATH)
DRAIN = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
sys.modules[SPEC.name] = DRAIN
SPEC.loader.exec_module(DRAIN)


class RecoveryDrainPlanTests(unittest.TestCase):
    def record(self, base: Path, digest: str, size: int):
        return DRAIN.SelectedMaster(
            inventory="VLV-1000",
            entity_id="ISS-2026-AAAA-A",
            evidence_id="VIS-1",
            digest=digest,
            size_bytes=size,
            selected_source_path=str(base / "source.bin"),
            preservation_role="preservation_master",
            preservation_digest=digest,
            preservation_size_bytes=size,
            preservation_relative_path="units/U/data/master.bin",
            primary_preservation_path=str(base / "primary.bin"),
            independent_preservation_path=str(base / "independent.bin"),
        )

    def test_scan_hashes_only_size_candidates_and_never_mutates(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "source"
            root.mkdir()
            selected = root / "source.bin"
            selected.write_bytes(b"selected")
            duplicate = root / "duplicate.any-extension"
            duplicate.write_bytes(b"selected")
            unrelated = root / "unrelated.bin"
            unrelated.write_bytes(b"different-size")
            digest = DRAIN.sha256_file_stable(selected, len(b"selected"))
            record = self.record(root, digest, len(b"selected"))

            occurrences, errors, counters = DRAIN.scan_occurrences([record], [root])

            self.assertEqual([], errors)
            self.assertEqual(3, counters["files_seen"])
            self.assertEqual(2, counters["files_hashed"])
            self.assertEqual(
                {str(selected), str(duplicate)},
                {item.path for item in occurrences},
            )
            self.assertEqual(b"selected", duplicate.read_bytes())

    def test_rows_are_explicitly_non_destructive(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = base / "source.bin"
            source.write_bytes(b"x")
            digest = DRAIN.sha256_file_stable(source, 1)
            record = self.record(base, digest, 1)
            occurrences = [
                DRAIN.Occurrence(str(base), str(source), 1, digest),
                DRAIN.Occurrence(str(base), str(base / "copy.bin"), 1, digest),
            ]

            rows = DRAIN.build_rows([record], occurrences)

            self.assertEqual("selected_provenance_source", rows[0]["occurrence_class"])
            self.assertEqual("exact_duplicate_occurrence", rows[1]["occurrence_class"])
            self.assertTrue(all(row["deletion_authorized"] == "false" for row in rows))

    def test_overlapping_roots_are_refused(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            nested = root / "nested"
            nested.mkdir()
            with self.assertRaises(DRAIN.DrainPlanError):
                DRAIN.validate_scan_roots([root, nested], [])

    def test_output_inside_source_root_is_refused(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            with self.assertRaises(DRAIN.DrainPlanError):
                DRAIN.validate_output_dir(root / "report", [root])

    def test_report_directory_must_be_new(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "existing"
            output.mkdir()
            with self.assertRaises(FileExistsError):
                DRAIN.write_report(output, [], [], [], {}, [], DRAIN.utc_now())


if __name__ == "__main__":
    unittest.main()
