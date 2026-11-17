import copy
import json
import unittest
import test_research_layout as fixtures
from archive_research.caption_regions import caption_tasks
from archive_research.runner import extract_one


class CaptionRegionTests(unittest.TestCase):
    def setUp(self):
        self.fixture=fixtures.LayoutTests();self.fixture.setUp();self.addCleanup(self.fixture.doCleanups)
        _,self.attempt=self.fixture.run_task(self.fixture.task())

    def tasks(self):return caption_tasks(self.fixture.roots,'artifacts',self.attempt['key'],sides=('below',))

    def test_real_caption_crop_retains_reading_origin_and_cache(self):
        task=self.tasks()[0]
        attempt=extract_one(self.fixture.roots,'artifacts',task)
        self.assertEqual(attempt['state'],'complete',attempt)
        row=json.loads((self.fixture.output/(attempt['key']+'.json')).read_text())
        self.assertIn('Invented',' '.join(w['text'] for w in row['words']))
        self.assertEqual(row['result']['recipe']['parameters']['settings']['caption_origin']['key'],self.attempt['key'])
        self.assertTrue(extract_one(self.fixture.roots,'artifacts',task)['reused'])

    def test_changed_derived_region_or_parent_digest_refused(self):
        task=self.tasks()[0];changed=copy.deepcopy(task);changed['region']['x']+=.001
        self.assertEqual(extract_one(self.fixture.roots,'artifacts',changed)['state'],'error')
        task['settings']['caption_origin']['receipt_sha256']='0'*64
        self.assertEqual(extract_one(self.fixture.roots,'artifacts',task)['state'],'error')

    def test_invalid_selection_and_side_refused(self):
        for options in ({'figures':[9]},{'sides':('left',)},{'figures':[0,0]}):
            with self.assertRaises(ValueError):caption_tasks(self.fixture.roots,'artifacts',self.attempt['key'],**options)


if __name__=='__main__':unittest.main()
