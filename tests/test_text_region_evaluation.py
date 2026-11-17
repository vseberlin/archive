"""Evaluation must not turn repeated candidates or missing results into success."""
import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'src'))
from archive_research.text_region_evaluation import evaluate


class EvaluationTests(unittest.TestCase):
    def fixture(self):
        region = {'x': .1, 'y': .2, 'width': .3, 'height': .1}
        case = {'id': 'invented', 'split': 'held_out', 'complete_annotations': True,
                'regions': [{'label': 'plate', 'region': region, 'expected_text': 'AB 123'}]}
        receipt = {'result': {'status': 'partial', 'page': {'id': 'synthetic'}},
                   'discovery': {'coverage': {'scan_complete': True},
                                 'regions': [{'region': region, 'readings': ['wrong', 'AB 123']}]}}
        return case, receipt

    def test_duplicate_candidates_do_not_inflate_recall(self):
        case, receipt = self.fixture()
        receipt['discovery']['regions'] *= 2
        result = evaluate(case, receipt)
        self.assertEqual(result['recall'], 1)
        self.assertEqual(result['precision'], .5)
        self.assertEqual(result['unmatched_detections'], 1)

    def test_partial_annotations_cannot_estimate_precision(self):
        case, receipt = self.fixture();case['complete_annotations'] = False
        result = evaluate(case, receipt)
        self.assertIsNone(result['precision']);self.assertIsNone(result['unmatched_detections'])

    def test_empty_and_unavailable_are_different(self):
        case, receipt = self.fixture();receipt['discovery']['regions'] = []
        self.assertEqual(evaluate(case, receipt)['recall'], 0)
        receipt['result']['status'] = 'error'
        with self.assertRaisesRegex(ValueError, 'unavailable'):evaluate(case, receipt)

    def test_alternative_hit_is_distinct_from_baseline(self):
        case, receipt = self.fixture()
        baseline = {'result': receipt['result'], 'words': []}
        result = evaluate(case, receipt, baseline=baseline)['recognition_diagnostic'][0]
        self.assertTrue(result['any_variant_exact']);self.assertFalse(result['baseline_contains_exact'])
        baseline['result'] = {'status': 'complete', 'page': {'id': 'other'}}
        with self.assertRaisesRegex(ValueError, 'page differs'):evaluate(case, receipt, baseline=baseline)


if __name__ == '__main__':unittest.main()
