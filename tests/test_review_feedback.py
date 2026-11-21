from copy import deepcopy
import unittest

from archive_research.review_feedback import reviewed_candidates


class PairFeedbackTests(unittest.TestCase):
    def test_rejection_is_exact_and_reversible_not_a_person_blacklist(self):
        page = {'source': {'root': 'archive', 'path': 'issue.pdf', 'size_bytes': 10, 'mtime_ns': 20},
                'page': 2, 'region': [0, 0, 1, 1]}
        photo = {'source': {'root': 'archive', 'path': 'portrait.jpg', 'size_bytes': 30, 'mtime_ns': 40},
                 'page': 0, 'region': [0., 0., 1., 1.]}
        rows = [{'locator': photo, 'score': .95}]
        feedback = [{'pair': {'query': page, 'candidate': photo}, 'review': 'rejected'}]
        self.assertEqual(reviewed_candidates(page, rows, feedback), ([], 1))
        self.assertEqual(len(reviewed_candidates(page, rows, feedback, show_rejected=True)[0]), 1)
        self.assertEqual(reviewed_candidates(photo, [{'locator': page}], feedback), ([], 1))
        for modified in [{**page, 'page': 3}, {**page, 'region': [0, 0, .5, .5]},
                         {**page, 'source': {**page['source'], 'mtime_ns': 21}}]:
            self.assertEqual(reviewed_candidates(modified, rows, feedback)[0][0]['review_state'], 'unreviewed')
        feedback[0]['review'] = 'unresolved'
        self.assertEqual(len(reviewed_candidates(page, rows, feedback)[0]), 1)

    def test_conflicting_reviews_are_visible_and_scores_are_unchanged(self):
        loc = {'source': {'path': 'page'}, 'page': 0, 'region': [0, 0, 1, 1]}
        other = {**loc, 'page': 1}
        feedback = [{'pair': {'query': loc, 'candidate': other}, 'review': state} for state in ['accepted', 'rejected']]
        result, hidden = reviewed_candidates(loc, [{'locator': other, 'score': .72}], feedback)
        self.assertEqual(hidden, 0)
        self.assertEqual(result[0]['review_state'], 'conflicting')
        self.assertEqual(result[0]['score'], .72)


if __name__ == '__main__':
    unittest.main()
