import importlib.util
import tempfile
import unittest
from pathlib import Path


SCRIPT = (
    Path(__file__).resolve().parents[2]
    / "tools"
    / "archive-preservation"
    / "jobs"
    / "provenance_restoration"
    / "stress_pilot.py"
)
SPEC = importlib.util.spec_from_file_location("stress_pilot", SCRIPT)
assert SPEC and SPEC.loader
stress_pilot = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(stress_pilot)


class StressPilotContractTest(unittest.TestCase):
    def _manifest(self, source: Path):
        cases = []
        for index in range(20):
            action = "metadata_only" if index == 19 else "tiff_master"
            cases.append(
                {
                    "case_id": f"CASE-{index:02d}",
                    "unit_id": f"UNIT-{index:02d}",
                    "inventory_label": f"INV-{index:02d}",
                    "action": action,
                    "source_path": "" if action == "metadata_only" else str(source),
                    "source_media_type": "" if action == "metadata_only" else "image/tiff",
                    "metadata": {},
                }
            )
        return {
            "format": stress_pilot.FORMAT,
            "pilot_root": str(stress_pilot.DEFAULT_PILOT_PARENT / "test"),
            "independent_root": str(
                stress_pilot.DEFAULT_INDEPENDENT_PARENT / "test"
            ),
            "limits": {"maximum_source_bytes": 1024},
            "cases": cases,
        }

    def test_rejects_source_outside_protected_roots(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "source.tif"
            source.write_bytes(b"II")
            with self.assertRaisesRegex(
                stress_pilot.StressPilotError,
                "outside protected evidence roots",
            ):
                stress_pilot.validate_manifest(
                    self._manifest(source),
                    include_fingerprints=False,
                )

    def test_rejects_case_count_below_floor(self):
        manifest = {
            "format": stress_pilot.FORMAT,
            "pilot_root": str(stress_pilot.DEFAULT_PILOT_PARENT / "test"),
            "independent_root": str(
                stress_pilot.DEFAULT_INDEPENDENT_PARENT / "test"
            ),
            "cases": [],
        }
        with self.assertRaisesRegex(
            stress_pilot.StressPilotError,
            "20-30 cases",
        ):
            stress_pilot.validate_manifest(
                manifest,
                include_fingerprints=False,
            )

    def test_rejects_duplicate_local_source_paths(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "a.tif"
            source.write_bytes(b"II")
            original = stress_pilot._protected_source
            try:
                stress_pilot._protected_source = lambda path: path
                manifest = self._manifest(source)
                with self.assertRaisesRegex(
                    stress_pilot.StressPilotError,
                    "duplicate source path",
                ):
                    stress_pilot.validate_manifest(
                        manifest,
                        include_fingerprints=False,
                    )
            finally:
                stress_pilot._protected_source = original


if __name__ == "__main__":
    unittest.main()
