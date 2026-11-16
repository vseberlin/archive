"""Reclassify generated deliveries without rerunning image comparisons."""
import csv
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from PIL import Image

from archive_common import ReviewError
from avantmedia_derivatives import pairing, rebuild, enable_jpeg_fallback, inherit_fallback_comparisons
from matching import stamp
from workbench import Workbench
import test_contract as fixtures


class DerivativeTests(unittest.TestCase):
    setUp = fixtures.ContractTests.setUp
    picture = fixtures.ContractTests.picture
    run_prepare = fixtures.ContractTests.run_prepare
    save = fixtures.ContractTests.save

    def delivery(self, covered=True, caption=''):
        unit = self.root / 'archive'; unit.mkdir()
        master = self.picture(unit / 'master.tif', (80, 60) if covered else (30, 20))
        cat = unit / 'CATALOG.csv'
        with cat.open('w', newline='') as f:
            row = {'record_key': 'photo', 'inventory': 'photo', 'file_role': 'preservation_master',
                   'final_relative_path': master.name, 'master_relative_path': master.name,
                   'final_sha256': hashlib.sha256(master.read_bytes()).hexdigest()}
            writer = csv.DictWriter(f, fieldnames=list(row)); writer.writeheader(); writer.writerow(row)
        self.args.against = [unit]
        self.tiff = self.picture(self.source / 'batch' / 'Ersatzoriginale' / 'photo.tif')
        self.jpeg = self.source / 'batch' / 'Nutzungsfiles' / 'photo.jpg'; self.jpeg.parent.mkdir(parents=True)
        image = Image.new('RGB', (40, 30), (40, 80, 130))
        exif = Image.Exif()
        if caption:
            exif[270] = caption
        image.save(self.jpeg, exif=exif)
        lead = {'engine': 'cached-pda', 'target_id': str(unit) + '#photo', 'phash_distance': 0}
        with patch('search.visual_matches', return_value=[lead]):
            _, self.old = self.run_prepare()
        self.prior = self.args.work_root
        self.output = self.root / 'masters'

    def test_covered_derivative_skipped_with_retained_link_and_no_input_mutation(self):
        self.delivery()
        originals = {p: (stamp(p), p.read_bytes()) for p in [self.tiff, self.jpeg, self.prior / 'workbench-input.json']}
        with patch('search.prepare', side_effect=AssertionError('No comparison rerun')):
            result = rebuild(self.prior, self.output, self.source)
        self.assertEqual(result['review_cases'], 0)
        with (self.output / 'source-dispositions.jsonl').open() as handle:
            rows = {r['source_path']: r for r in map(json.loads, handle)}
        jpeg = rows[str(self.jpeg)]
        self.assertEqual(jpeg['disposition'], 'redundant')
        self.assertEqual(jpeg['derivative_of'], str(self.tiff))
        self.assertEqual(jpeg['retained_master_path'], str(self.root / 'archive' / 'master.tif'))
        self.assertFalse(jpeg['deletion_authorized'])
        for path, old in originals.items():
            self.assertEqual((stamp(path), path.read_bytes()), old)
        self.assertTrue(Workbench(self.output, 'test').snapshot()['projection_agrees'])

    def test_jpeg_only_metadata_creates_metadata_case_without_jpeg_master(self):
        self.delivery(caption='Description found only in the derivative')
        result = rebuild(self.prior, self.output, self.source)
        self.assertEqual(result['counts'], {'metadata_review': 1})
        app = Workbench(self.output, 'test'); oid = next(iter(app.objects)); snap = app.snapshot(oid)
        derivative = next(c for c in snap['object']['candidates'] if c.get('source_role') == 'derivative')
        self.assertFalse(derivative['eligible'])
        self.assertTrue(derivative['metadata'])
        self.assertIn(derivative['id'], snap['decision']['redundant_candidate_ids'])
        self.assertTrue(snap['decision']['selected_candidate_id'].startswith('archive:'))
        self.assertTrue(snap['object']['metadata_review_required'])

    def test_fixed_archive_derivative_metadata_does_not_reopen_covered_parent(self):
        self.delivery(caption='Additional derivative caption')
        path = self.prior / 'workbench-input.json'
        config = json.loads(path.read_text()); config['archive_masters_fixed'] = True
        path.write_text(json.dumps(config))
        result = rebuild(self.prior, self.output, self.source)
        self.assertEqual(result['review_cases'], 0)
        self.assertTrue(Workbench(self.output, 'test').config['archive_masters_fixed'])

    def test_saved_tiff_selection_is_carried_forward_and_derivative_attached(self):
        self.delivery(covered=False)
        oid = next(oid for oid, o in self.old.objects.items() if o['candidates'][0]['members'][0]['path'] == str(self.tiff))
        self.save(self.old, oid, selected_candidate_id='source', note='Keep this TIFF', destination_unit=str(self.root / 'archive'))
        old_decisions = (self.prior / 'decisions.jsonl').read_bytes()
        result = rebuild(self.prior, self.output, self.source)
        self.assertEqual(result['derivative_reclassification']['saved_choices_carried_forward'], 1)
        app = Workbench(self.output, 'test'); snap = app.snapshot(oid)
        self.assertEqual(snap['decision']['selected_candidate_id'], 'source')
        self.assertEqual(snap['decision']['note'], 'Keep this TIFF')
        self.assertEqual(snap['decision']['decided_at'], self.old.snapshot(oid)['decision']['decided_at'])
        self.assertTrue((self.output / 'decisions.jsonl').exists())
        self.assertEqual(len(snap['decision']['redundant_candidate_ids']), 1)
        self.assertTrue(snap['object']['prior_review_evidence'])
        self.assertEqual((self.prior / 'decisions.jsonl').read_bytes(), old_decisions)

    def test_unselected_parent_keeps_derivative_unresolved(self):
        self.delivery(covered=False)
        result = rebuild(self.prior, self.output, self.source)
        self.assertEqual(result['review_cases'], 1)
        app = Workbench(self.output, 'test'); snap = app.snapshot(next(iter(app.objects)))
        self.assertEqual(snap['decision']['selected_candidate_id'], '')
        self.assertFalse(snap['decision']['redundant_candidate_ids'])
        self.assertEqual(len(snap['object']['source_candidate_ids']), 2)

    def test_scope_magazine_ids_ambiguous_suffix_and_website_asset(self):
        root = self.source
        names = ['a/Ersatzoriginale/p.tif', 'b/Nutzungsfiles/p.jpg',
            'a/Ersatzoriginale/q.tif', 'a/Ersatzoriginale/q 2.tif', 'a/Nutzungsfiles/q 1.jpg',
            'Auftrag 1614930/TIFF/001 Magazin 001/Dia 00001.tif',
            'Auftrag 1614930/Nutzungsderivate/001 Magazin 001_b1/b1_00001.jpg',
            'Start/WebTemplate/SimpleViewer2/images/sandstone.jpg']
        links = pairing({str(root / n): {} for n in names}, root)
        self.assertEqual(links[str(root / names[1])]['parent'], '')  # Never cross deliveries.
        self.assertEqual(len(links[str(root / names[4])]['parents']), 2)
        self.assertEqual(links[str(root / names[4])]['parent'], '')
        self.assertEqual(links[str(root / names[6])]['parent'], str(root / names[5]))
        self.assertEqual(links[str(root / names[7])]['role'], 'website_asset')
        with self.assertRaisesRegex(ReviewError, 'outside the approved'):
            pairing({str(root / 'unknown.jpg'): {}}, root)

    def test_usable_jpeg_becomes_master_fallback_when_tiff_is_unreadable(self):
        obj = {'candidates': [
            {'id':'tiff','eligible':False,'complete':False,'members':[{'path':'/test/a.tif'}]},
            {'id':'jpeg','eligible':False,'complete':True,'source_role':'derivative',
             'members':[{'path':'/test/a.jpg'}],'technical':{'width':1200,'height':800}}]}
        row = {'selected_candidate_id':''}
        enable_jpeg_fallback(obj,row)
        self.assertEqual(row['selected_candidate_id'],'jpeg')
        self.assertTrue(obj['candidates'][1]['eligible'])
        self.assertEqual(obj['candidates'][1]['source_role'],'master_fallback')

    def test_fallback_keeps_its_own_stamped_archive_leads_without_confirming_identity(self):
        member = {'path':'/test/a.jpg','size_bytes':12,'mtime_ns':34,'pages':1}
        obj = {'candidates':[{'id':'jpeg','eligible':True,'complete':True,'source_role':'master_fallback',
                              'members':[member]}], 'source_candidate_ids':['jpeg'], 'matches':[]}
        old = {'candidates':[{'id':'source','members':[deepcopy(member)]},
                             {'id':'archive:md:7','reference_object_id':'unit#md:7',
                              'members':[{'path':'/archive/master.tif','size_bytes':56,'mtime_ns':78,'pages':1}]}],
               'matches':[{'a':'source','b':'archive:md:7','engine':'sift','status':'supported','inliers':500}]}
        original = deepcopy(old)
        inherit_fallback_comparisons(obj,[old])
        self.assertEqual(obj['matches'],[dict(old['matches'][0],a='jpeg')])
        self.assertEqual(obj['candidates'][1]['id'],'archive:md:7')
        self.assertNotIn('confirmed_reference_ids',obj)
        self.assertNotIn('same_pairs',obj)
        self.assertEqual(old,original)
        inherit_fallback_comparisons(obj,[old])
        self.assertEqual(len(obj['matches']),1)
        self.assertEqual(len(obj['candidates']),2)
        stale = deepcopy(obj);stale['matches']=[];stale['candidates']=stale['candidates'][:1]
        stale['candidates'][0]['members'][0]['mtime_ns']+=1
        inherit_fallback_comparisons(stale,[old])
        self.assertEqual(stale['matches'],[])

    def test_source_changes_and_input_overlap_are_refused_before_writes(self):
        self.delivery()
        with self.assertRaisesRegex(ReviewError, 'overlaps source/archive'):
            rebuild(self.prior, self.source / 'bad-output', self.source)
        self.assertFalse((self.source / 'bad-output').exists())
        self.jpeg.write_bytes(b'changed test input')
        with self.assertRaisesRegex(ReviewError, 'Source/cache changed'):
            rebuild(self.prior, self.output, self.source)
        self.assertFalse(self.output.exists())

    def test_saved_jpeg_choice_is_never_silently_discarded(self):
        self.delivery(covered=False)
        oid = next(oid for oid, o in self.old.objects.items() if o['candidates'][0]['members'][0]['path'] == str(self.jpeg))
        self.save(self.old, oid, selected_candidate_id='source')
        with self.assertRaisesRegex(ReviewError, 'Saved JPEG decision'):
            rebuild(self.prior, self.output, self.source)
        self.assertFalse(self.output.exists())


if __name__ == '__main__':
    unittest.main()
