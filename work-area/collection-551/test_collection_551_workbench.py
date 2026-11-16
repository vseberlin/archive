#!/usr/bin/env python3

import copy
import csv
import importlib.util
import io
import json
import unittest
from collections import Counter
from pathlib import Path
from unittest import mock


SCRIPT = Path(__file__).with_name("collection_551_workbench.py")
SPEC = importlib.util.spec_from_file_location("collection_551_workbench", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(MODULE)


class Collection551WorkbenchTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workbench = MODULE.Workbench("Test actor")

    def setUp(self):
        # Reconciliation behavior remains testable after the live unit is accessioned.
        patcher = mock.patch.object(MODULE, 'accession_receipt', return_value={})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_accessioned_unit_reports_completion_and_blocks_master_changes(self):
        with mock.patch.object(MODULE, 'accession_receipt', return_value={'accession_performed': True}), mock.patch.object(MODULE, 'atomic_replace') as write:
            self.assertTrue(self.workbench.check()['accession_performed'])
            with self.assertRaisesRegex(MODULE.WorkbenchError, 'accessioned'):
                self.workbench.save(next(iter(self.workbench.review_ids)), 'unresolved', '')
            write.assert_not_called()
            page = MODULE.page('Review', '<form method="post"><button>Save</button></form>').decode()
            self.assertIn('<fieldset disabled>', page)
            self.assertIn('Drain list (359 paths)', page)
            self.assertIn('<form method="get"><button>Filter</button></form>', MODULE.page('Filter', '<form method="get"><button>Filter</button></form>').decode())

    def test_difference_first_workbench_is_complete_and_decision_consistent(self):
        result = self.workbench.check()
        self.assertEqual("ok", result["result"])
        self.assertEqual(79, result["objects"])
        self.assertEqual(112, result["resources"])
        self.assertEqual(271, result["master_candidates"])
        decisions = self.workbench.decisions()
        reviewed_resolved = sum(decisions[object_id]["decision_state"] == "selected_master" for object_id in self.workbench.review_ids)
        self.assertEqual(13, result["review_queue_unresolved"] + reviewed_resolved)
        self.assertEqual(23, result["binary_differences"])
        self.assertEqual(71, result["preview_rows"])
        self.assertEqual(79, result["resolved_objects"] + result["unresolved_objects"])
        self.assertFalse(result["deletion_authorized"])
        self.assertFalse(result["accession_performed"])

    def test_proposal_partition_matches_preparation(self):
        states = Counter(row["proposal_state"] for row in self.workbench.proposals)
        self.assertEqual(66, sum(count for state, count in states.items() if state.startswith("md_origin_")))
        self.assertEqual(13, states["local_binary_enrichment_review_required"])
        self.assertTrue(all(row["human_confirmation_required"] == "true" for row in self.workbench.proposals))
        self.assertEqual(13, len(self.workbench.review_ids))

    def test_completed_reviews_are_hidden_but_pending_metadata_stays_visible(self):
        workbench = copy.copy(self.workbench)
        decisions = copy.deepcopy(workbench.decisions())
        object_ids = sorted(workbench.review_ids)
        for object_id in object_ids:
            decisions[object_id]["decision_state"] = "selected_master"
        decisions[object_ids[0]]["decision_state"] = "unresolved"
        workbench.metadata_groups_by_object = {
            object_ids[1]: [{"members": [{"decision_action": "accept"}, {"decision_action": "pending"}]}],
            object_ids[2]: [{"members": [{"decision_action": "accept"}, {"decision_action": "substitute"}]}],
        }
        with mock.patch.object(workbench, "decisions", return_value=decisions):
            self.assertEqual(set(object_ids[:2]), {row["md_object_id"] for row in workbench.ordered_objects()})
            self.assertIn(workbench.next_object(object_ids[2]), object_ids[:2])
            decisions[object_ids[0]]["decision_state"] = "selected_master"
            workbench.metadata_groups_by_object = {}
            self.assertEqual([], workbench.ordered_objects())
            self.assertIsNone(workbench.next_object(object_ids[1]))

    def test_review_shows_only_distinct_material_binary_differences_with_previews(self):
        self.assertEqual(self.workbench.review_ids, set(self.workbench.binary_differences_by_object))
        displayed = {row["local_candidate_id"] for row in self.workbench.binary_differences}
        self.assertTrue(displayed)
        for candidate_id in displayed:
            candidate = self.workbench.candidates_by_id[candidate_id]
            self.assertEqual("potential_local_binary_enrichment", candidate["candidate_role"])
            preview = self.workbench.previews_by_candidate[candidate_id]
            self.assertTrue(Path(preview["preview_path"]).is_file())
        self.assertTrue(all(row["difference_summary"] for row in self.workbench.binary_differences))

    def test_feb_eki1_preserves_five_member_order_but_flags_local_variants(self):
        obj = next(row for row in self.workbench.objects if row["inventory"] == "FEB-EKI1")
        proposal = self.workbench.proposals_by_id[obj["md_object_id"]]
        resources = self.workbench.resources_by_object[obj["md_object_id"]]
        self.assertEqual(5, len(resources))
        self.assertEqual("local_binary_enrichment_review_required", proposal["proposal_state"])
        self.assertEqual("", proposal["proposed_candidate_id"])
        decisions = copy.deepcopy(self.workbench.decisions())
        decisions[obj["md_object_id"]].update({
            "decision_state": "selected_master",
            "selected_candidate_id": MODULE.COMPOUND_MD_SELECTION,
            "master_representation_type": "ordered_md_resource_set",
            "selected_resource_ids": json.dumps([row["resource_id"] for row in resources]),
            "decision_note": "test ordered MD set",
            "actor": "Test actor",
            "decided_at": "2026-09-02T00:00:00Z",
        })
        self.assertEqual([], self.workbench.validate_decisions(decisions))

    def test_metadata_enrichment_is_preserved_separately_from_binary_choice(self):
        enriched = [row for row in self.workbench.metadata if row["local_enrichment_fields"]]
        covered = [row for row in self.workbench.metadata if int(row["metadata_evidence_rows"])]
        self.assertEqual(75, len(enriched))
        self.assertEqual(75, len(covered))
        self.assertTrue(any("storage_location" in row["local_enrichment_fields"] for row in enriched))
        self.assertTrue(self.workbench.metadata_differences)
        self.assertTrue(all(row["decision_action"] in MODULE.METADATA_ACTIONS for row in self.workbench.metadata_differences))

    def test_semantic_metadata_duplicates_are_consolidated_without_losing_evidence(self):
        stats = self.workbench.metadata_group_stats
        self.assertEqual(104, stats["raw_rows"])
        self.assertEqual(59, stats["visible_groups"])
        self.assertEqual(31, stats["consolidated_rows"])
        self.assertEqual(14, stats["hidden_equivalent_groups"])
        groups = self.workbench.metadata_groups_by_object["54376"]
        self.assertEqual(8, len(groups))
        values = {str(row["local_value"]) for row in groups}
        self.assertTrue(any("Z 861 X" in value for value in values))
        self.assertIn("Kaltkatoden-Thyratron", values)
        self.assertNotIn("Juni 1961", values)
        self.assertIn("Dezember 1961", values)

    def test_metadata_actions_have_bounded_effective_values(self):
        row = {"md_value": "MD", "local_value": "Local"}
        self.assertEqual("MD", self.workbench.effective_metadata_value(row, "accept"))
        self.assertEqual("MD", self.workbench.effective_metadata_value(row, "ignore"))
        self.assertEqual("Local", self.workbench.effective_metadata_value(row, "substitute"))
        self.assertEqual("MD; Local", self.workbench.effective_metadata_value(row, "add"))

    def test_save_projects_review_actions_without_source_writes(self):
        workbench = MODULE.Workbench("Test actor")
        group = workbench.metadata_groups_by_object["60904"][0]
        with mock.patch.object(MODULE, "atomic_replace") as replace:
            workbench.save("60904", "accept_md", "bounded test", {str(group["group_id"]): "add"})
        self.assertEqual(4, replace.call_count)
        decision_call = replace.call_args_list[0]
        self.assertEqual(MODULE.DECISIONS_CSV, decision_call.args[0])
        decisions = list(csv.DictReader(io.StringIO(decision_call.args[1].decode())))
        selected = next(row for row in decisions if row["md_object_id"] == "60904")
        self.assertEqual(MODULE.COMPOUND_MD_SELECTION, selected["selected_candidate_id"])
        self.assertEqual("ordered_md_resource_set", selected["master_representation_type"])
        updated = workbench.metadata_differences_by_id[str(group["preferred_difference_id"])]
        self.assertEqual("add", updated["decision_action"])
        self.assertTrue(updated["effective_value"])

    def test_drain_is_comprehensive_but_never_authorizes_deletion(self):
        counts = Counter(row["classification"] for row in self.workbench.drain_base)
        self.assertEqual({
            "local_derivative": 126,
            "projection_staging_file": 14,
            "transient_local_file": 8,
            "transient_work_capture": 112,
            "transient_workbench_preview": 71,
        }, dict(counts))
        self.assertTrue(all(row["deletion_authorized"] == "false" for row in self.workbench.drain_base))
        self.assertTrue(all(row["drain_state"].startswith("blocked_") for row in self.workbench.drain_base))

    def test_human_confirmation_projects_exactly_one_master_representation(self):
        object_id = next(row["md_object_id"] for row in self.workbench.proposals if row["proposed_candidate_id"])
        proposal = self.workbench.proposals_by_id[object_id]
        decisions = copy.deepcopy(self.workbench.decisions())
        decisions[object_id].update({
            "decision_state": "selected_master",
            "selected_candidate_id": proposal["proposed_candidate_id"],
            "master_representation_type": proposal["proposed_representation_type"],
            "selected_resource_ids": proposal["proposed_resource_ids"],
            "decision_note": "test proposal confirmation",
            "actor": "Test actor",
            "decided_at": "2026-09-02T00:00:00Z",
        })
        self.assertEqual([], self.workbench.validate_decisions(decisions))
        selected = self.workbench.selected_candidates(object_id, decisions[object_id])
        self.assertTrue(selected)
        dispositions = [row for row in self.workbench.dispositions_for(decisions) if row["md_object_id"] == object_id]
        self.assertEqual(len(selected), sum(row["disposition"].startswith("selected_master") for row in dispositions))
        drain = self.workbench.drain_for(decisions)
        selected_paths = {row["candidate_path"] for row in selected}
        selected_drain = [row for row in drain if row["source_path"] in selected_paths]
        self.assertTrue(selected_drain)
        self.assertTrue(all(row["drain_state"] == "blocked_until_validated_accession" for row in selected_drain))
        self.assertTrue(all(row["deletion_authorized"] == "false" for row in selected_drain))

    def test_derivatives_cannot_be_selected_as_masters(self):
        derivatives = [row for row in self.workbench.candidates if row["candidate_role"] == "local_derivative_or_transient"]
        self.assertTrue(derivatives)
        self.assertTrue(all(row["master_eligible"] == "false" for row in derivatives))


if __name__ == "__main__":
    unittest.main()
