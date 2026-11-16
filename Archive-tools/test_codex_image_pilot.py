import unittest
from codex_image_pilot import harvest, command, remaining_candidates


class PilotTests(unittest.TestCase):
    schema = {'type': 'object', 'properties': {'title': {'type': 'string'}},
              'required': ['title'], 'additionalProperties': False}

    def test_remaining_set_excludes_completed_and_rejects_bad_prior(self):
        candidates = [{'id': str(i)} for i in range(111)]
        completed = [{'candidate_id': str(i), 'status': 'success'} for i in range(10)]
        remaining = remaining_candidates(candidates, completed)
        self.assertEqual(len(remaining), 101)
        self.assertFalse({c['id'] for c in remaining} & {r['candidate_id'] for r in completed})
        completed[-1]['candidate_id'] = '0'
        with self.assertRaises(AssertionError):
            remaining_candidates(candidates, completed)
        completed[-1] = {'candidate_id': '9', 'status': 'failed'}
        with self.assertRaises(AssertionError):
            remaining_candidates(candidates, completed)

    def test_usage_preserved_without_double_counting(self):
        events = [{'type': 'turn.completed', 'usage': {
            'input_tokens': 100, 'cached_input_tokens': 50,
            'output_tokens': 20, 'reasoning_output_tokens': 5}}]
        usage, errors = harvest(events, {'title': 'test'}, self.schema, 0)
        self.assertFalse(errors)
        self.assertEqual(usage['total_tokens'], 120)
        usage, errors = harvest(events, {'wrong': 'test'}, self.schema, 1)
        self.assertTrue(errors)
        self.assertEqual(usage['total_tokens'], 120)

    def test_missing_usage_and_tools_fail(self):
        events = [{'type': 'item.completed', 'item': {'type': 'command_execution'}}]
        usage, errors = harvest(events, {'title': 'test'}, self.schema, 0)
        self.assertIsNone(usage)
        self.assertEqual(len(errors), 2)

    def test_fresh_low_reasoning_command(self):
        cmd = command({'number': 1, 'preview_path': '/example.jpg'})
        self.assertIn('--ephemeral', cmd)
        self.assertIn('model_reasoning_effort="low"', cmd)
        self.assertIn('features.shell_tool=false', cmd)
        self.assertNotIn('resume', cmd)
        self.assertNotIn('fork', cmd)


if __name__ == '__main__':
    unittest.main()
