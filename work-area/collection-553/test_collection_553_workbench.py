#!/usr/bin/env python3

import copy
import importlib.util
from collections import Counter
import unittest
from pathlib import Path


SCRIPT = Path(__file__).with_name("collection_553_workbench.py")
SPEC = importlib.util.spec_from_file_location("collection_553_workbench", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(MODULE)


class Collection553WorkbenchTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workbench = MODULE.Workbench("Test actor")

    def test_live_prepared_projection_passes_check(self):
        result = self.workbench.check()
        self.assertEqual("ok", result["result"])
        self.assertEqual(54, result["objects"])
        self.assertEqual(244, result["master_candidates"])
        self.assertEqual(54, result["resolved_objects"])
        self.assertEqual(0, result["unresolved_objects"])
        self.assertFalse(result["deletion_authorized"])

    def test_user_authorized_master_representations_are_complete_and_bounded(self):
        decisions = self.workbench.decisions()
        types = Counter(decision["master_representation_type"] for decision in decisions.values())
        self.assertEqual({"single_file": 45, "ordered_md_resource_set": 9}, dict(types))
        for object_id, decision in decisions.items():
            resources = self.workbench.resources_by_object[object_id]
            selected = self.workbench.candidates_by_id[decision["selected_candidate_id"]]
            resource_ids = MODULE.parsed_resource_ids(decision)
            if decision["master_representation_type"] == "ordered_md_resource_set":
                self.assertGreater(len(resources), 1)
                self.assertEqual(
                    [row["resource_id"] for row in self.workbench.ordered_resources(object_id)],
                    resource_ids,
                )
                self.assertEqual(self.workbench.main_md_candidate(object_id)["candidate_id"], selected["candidate_id"])
            elif object_id == "51891":
                self.assertEqual("md-51891-candidate-017", selected["candidate_id"])
                self.assertEqual([], resource_ids)
                self.assertEqual(59, int(selected["page_count"]))
                self.assertEqual(
                    "4344cab8ccdb1874122a3364ba9ed0e3642b5adcb46a9a6987327a21c80d88ed",
                    selected["known_sha256"],
                )
            else:
                self.assertEqual(1, len(resources))
                self.assertEqual("museum-digital_reference", selected["source_kind"])
                self.assertEqual("captured_md_main_resource", selected["match_basis"])
                self.assertEqual(resources[0]["sha256"], selected["known_sha256"])
                self.assertEqual([resources[0]["resource_id"]], resource_ids)

    def test_one_selection_projects_every_other_candidate_as_redundant(self):
        decisions = copy.deepcopy(self.workbench.decisions())
        object_id = "49951"
        selected = self.workbench.candidates_by_object[object_id][0]["candidate_id"]
        decisions[object_id].update(
            {
                "decision_state": "selected_master",
                "selected_candidate_id": selected,
                "decision_note": "test decision",
                "actor": "Test actor",
                "decided_at": "2026-08-31T00:00:00Z",
            }
        )
        rows = [row for row in self.workbench.dispositions_for(decisions) if row["md_object_id"] == object_id]
        self.assertEqual(1, sum(row["disposition"] == "selected_master" for row in rows))
        self.assertEqual(len(rows) - 1, sum(row["disposition"] == "redundant" for row in rows))
        self.assertTrue(all(row["deletion_authorized"] == "false" for row in rows))

    def test_shared_70007_70011_resource_cannot_be_selected_twice(self):
        left = "49984"
        right = "51891"
        left_by_hash = {
            row["known_sha256"]: row for row in self.workbench.candidates_by_object[left] if row["known_sha256"]
        }
        right_by_hash = {
            row["known_sha256"]: row for row in self.workbench.candidates_by_object[right] if row["known_sha256"]
        }
        shared = sorted(set(left_by_hash) & set(right_by_hash))
        self.assertTrue(shared)
        decisions = copy.deepcopy(self.workbench.decisions())
        for object_id, candidate in (
            (left, left_by_hash[shared[0]]),
            (right, right_by_hash[shared[0]]),
        ):
            decisions[object_id].update(
                {
                    "decision_state": "selected_master",
                    "selected_candidate_id": candidate["candidate_id"],
                    "master_representation_type": "single_file",
                    "selected_resource_ids": "[]",
                    "decision_note": "test identity",
                    "actor": "Test actor",
                    "decided_at": "2026-08-31T00:00:00Z",
                }
            )
        errors = self.workbench.validate_decisions(decisions)
        self.assertTrue(any("same candidate identity" in error for error in errors))

    def test_integrity_exceptions_are_not_master_candidates(self):
        candidate_paths = {row["candidate_path"] for row in self.workbench.candidates}
        exception_paths = {row["source_path"] for row in self.workbench.exceptions}
        self.assertFalse(candidate_paths & exception_paths)

    def test_notes_are_bounded_and_whitespace_normalized(self):
        self.assertEqual("a short note", MODULE.normalized_note("  a\n short\t note  "))
        self.assertEqual(500, len(MODULE.normalized_note("x" * 600)))

    def test_one_page_scan_candidates_are_scoped_to_component_objects(self):
        expected = {"60648": 6, "60660": 4, "60662": 4}
        for object_id, count in expected.items():
            candidates = self.workbench.candidates_by_object[object_id]
            self.assertEqual(count, len(candidates))
            self.assertTrue(any(row["format"] in {"jpg", "tiff"} for row in candidates))
        for row in self.workbench.page_scan_evidence:
            if int(row["pdf_page_count"]) > 1:
                self.assertEqual("related_component_evidence_only", row["candidate_scope"])

    def test_required_reviews_are_ordered_before_standard_decisions(self):
        ordered = self.workbench.ordered_objects()
        categories = [bool(self.workbench.review_category(row["md_object_id"])) for row in ordered]
        self.assertEqual(sorted(categories, reverse=True), categories)


if __name__ == "__main__":
    unittest.main()
