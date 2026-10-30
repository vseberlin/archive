import csv
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
    / "wf_sender_freeze_decisions.py"
)
SPEC = importlib.util.spec_from_file_location("wf_sender_freeze_decisions", SCRIPT)
FREEZE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
sys.modules[SPEC.name] = FREEZE
SPEC.loader.exec_module(FREEZE)


class WfSenderFreezeDecisionsTest(unittest.TestCase):
    def test_fallback_candidate_id_is_resolved_within_one_run(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory)
            fields = [
                "md_object_id",
                "extension",
                "source_path",
                "size_bytes",
                "mtime_ns",
            ]
            with (run / "filesystem-candidates.csv").open(
                "w", encoding="utf-8", newline=""
            ) as handle:
                writer = csv.DictWriter(handle, fieldnames=fields)
                writer.writeheader()
                writer.writerows(
                    [
                        {
                            "md_object_id": "1",
                            "extension": ".html",
                            "source_path": "/ignored",
                            "size_bytes": "1",
                            "mtime_ns": "1",
                        },
                        {
                            "md_object_id": "2",
                            "extension": ".pdf",
                            "source_path": "/first-preview",
                            "size_bytes": "2",
                            "mtime_ns": "2",
                        },
                    ]
                )
            row = FREEZE.fallback_candidate(run, "fallback:2")
            self.assertEqual("/first-preview", row["source_path"])

    def test_review_intervals_bind_to_the_run_active_at_decision_time(self) -> None:
        initial = Path("/initial")
        current = Path("/current")
        self.assertEqual(
            initial, FREEZE.fallback_run_for_decision(10, initial, current)
        )
        self.assertEqual(
            initial, FREEZE.fallback_run_for_decision(28, initial, current)
        )
        self.assertEqual(
            current, FREEZE.fallback_run_for_decision(36, initial, current)
        )
        self.assertEqual(
            current, FREEZE.fallback_run_for_decision(46, initial, current)
        )
        with self.assertRaises(FREEZE.DecisionBindingError):
            FREEZE.fallback_run_for_decision(35, initial, current)

    def test_stable_id_changes_when_observation_changes(self) -> None:
        path = Path("/evidence.pdf")
        first = FREEZE.stable_id(path, 10, 20, "abc")
        self.assertEqual(first, FREEZE.stable_id(path, 10, 20, "abc"))
        self.assertNotEqual(first, FREEZE.stable_id(path, 11, 20, "abc"))
