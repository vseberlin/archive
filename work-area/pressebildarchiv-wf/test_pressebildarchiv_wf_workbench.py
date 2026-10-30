#!/usr/bin/env python3

import importlib.util
import unittest
from pathlib import Path


SCRIPT = Path(__file__).with_name("pressebildarchiv_wf_workbench.py")
SPEC = importlib.util.spec_from_file_location("pressebildarchiv_wf_workbench", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(MODULE)


class DecisionRulesTest(unittest.TestCase):
    def test_selected_master_makes_other_candidates_redundant(self):
        result = MODULE.decide_dispositions({"a", "b", "c"}, "b", set(), set())
        self.assertEqual(
            {"a": "redundant", "b": "selected_master", "c": "redundant"},
            result,
        )

    def test_explicit_reject_and_noise_are_not_overwritten(self):
        result = MODULE.decide_dispositions(
            {"a", "b", "c", "d"}, "a", {"c"}, {"d"}
        )
        self.assertEqual("selected_master", result["a"])
        self.assertEqual("redundant", result["b"])
        self.assertEqual("rejected", result["c"])
        self.assertEqual("noise", result["d"])

    def test_no_master_keeps_unmarked_candidates_pending(self):
        result = MODULE.decide_dispositions({"a", "b"}, "", {"a"}, set())
        self.assertEqual({"a": "rejected", "b": "pending"}, result)

    def test_candidate_states_are_mutually_exclusive(self):
        with self.assertRaises(MODULE.WorkbenchError):
            MODULE.decide_dispositions({"a"}, "", {"a"}, {"a"})
        with self.assertRaises(MODULE.WorkbenchError):
            MODULE.decide_dispositions({"a"}, "a", {"a"}, set())

    def test_published_archive_is_blocked_from_ordinary_drain(self):
        source_class, state = MODULE.drain_classification(
            "/mnt/volume/iss-archive/units/kurt-schwarz/masters/example.jpg"
        )
        self.assertEqual("published_archive", source_class)
        self.assertTrue(state.startswith("blocked_"))

    def test_usb_redundant_candidate_projects_to_drain_review(self):
        source_class, state = MODULE.drain_classification(
            "/media/vladimir/Elements/restore/archive/WF/example.jpg"
        )
        self.assertEqual("protected_source_archive", source_class)
        self.assertEqual("redundant_source_candidate", state)


if __name__ == "__main__":
    unittest.main()
