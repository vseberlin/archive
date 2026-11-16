"""Decision invariants, using real evidence but never real curator decisions."""
import json
import io
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import tiff_review as review


class ReviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rows = [json.loads(s) for s in (review.ROOT/'tiff-scan-results.jsonl').read_text().splitlines()]
        cls.masters = {r['record_key']:r for r in (json.loads(s) for s in (review.ROOT/'tiff-scan-masters.jsonl').read_text().splitlines())}
        cls.metadata, cls.proposals = review.inputs()
        cls.child = next(r for r in cls.rows if r['filename']=='A01_T04_001.tif')
        cls.child2 = next(r for r in cls.rows if r['filename']=='A01_T04_002.tif')
        cls.new = next(r for r in cls.rows if r['filename']=='A04_T85_001.tif')

    def payload(self, row=None, action='assign_source', key='md:96927', master='md:96927'):
        return dict(source_path=(row or self.child)['source_path'], action=action, metadata_key=key,
                    master_key=master, note='', previous_revision='fixture')

    def make(self, p, existing=None):
        return review.make_decision(p, self.rows, self.masters, self.metadata, self.proposals, existing or {}, 'fixture')

    def test_reassign_preserves_different_former_master(self):
        d = self.make(self.payload())
        self.assertEqual(d['selected_master_path'],self.child['source_path'])
        self.assertEqual(d['unresolved_candidates'],[self.masters['md:96927']['master_path']])
        self.assertEqual(d['redundant_candidates'],[])
        self.assertFalse(d['applied_to_archive'])
        self.assertEqual(d['metadata']['title'],self.metadata['md:96927']['title'])

    def test_one_master_per_object(self):
        first = self.make(self.payload())
        with self.assertRaisesRegex(ValueError,'different proposed Master'):
            self.make(self.payload(self.child2),{first['source_path']:first})

    def test_archive_image_reassignment_tracks_both_objects(self):
        d = self.make(self.payload(action='assign_master',master='md:96928'))
        self.assertEqual(d['selected_master_path'],self.masters['md:96928']['master_path'])
        self.assertEqual({r['record_key'] for r in d['affected_existing_objects']},{'md:96927','md:96928'})
        self.assertEqual(d['redundant_candidates'],[])

    def test_confirm_requires_same_object_record(self):
        with self.assertRaisesRegex(ValueError,'Confirm requires'):
            self.make(self.payload(action='confirm_existing',master='md:96928'))
        d = self.make(self.payload(action='confirm_existing'))
        self.assertEqual(d['redundant_candidates'],[self.child['source_path']])

    def test_selected_file_cannot_also_be_redundant(self):
        first = self.make(self.payload())
        second = self.make(self.payload(self.child2,action='redundant')|{'note':'Rejected'})
        second['redundant_candidates']=[first['selected_master_path']]
        with self.assertRaisesRegex(ValueError,'cannot also be rejected'):
            review.validate_set({'a':first,'b':second})

    def test_new_caption_is_bound_to_exact_tiff(self):
        d = self.make(self.payload(self.new,action='new_object',key=self.new['source_path']))
        self.assertEqual(d['metadata']['source_path'],self.new['source_path'])
        self.assertTrue(d['metadata_assignment_confirmed'])
        with self.assertRaisesRegex(ValueError,'For a new object'):
            self.make(self.payload(self.new,action='new_object'))

    def test_stale_unknown_unreadable_and_note_guards(self):
        for patch_value, message in [({'previous_revision':'stale'},'another tab'),
                                     ({'source_path':'/etc/passwd'},'Unknown source'),
                                     ({'action':'delete'},'supported action'),
                                     ({'note':'x'*1001},'1000 characters')]:
            with self.subTest(patch_value=patch_value),self.assertRaisesRegex(ValueError,message):
                self.make(self.payload()|patch_value)
        unreadable = next(r for r in self.rows if r['finding']=='unreadable_tiff')
        with self.assertRaisesRegex(ValueError,'unreadable'):
            self.make(self.payload(unreadable,action='new_object',key=''))

    def test_prior_decisions_allow_blank_note(self):
        rows = [next(r for r in self.rows if r['finding']==finding)
                for finding in ('prior_alias_image_conflict', 'prior_curator_hold')]
        alias_key = next(k for k, m in self.metadata.items() if m.get('prior_alias_decision'))
        for row in rows:
            for key in ('md:96927', alias_key):
                with self.subTest(source=row['source_path'], key=key):
                    d = self.make(self.payload(row, key=key))
                    self.assertEqual(d['note'], '')
                    self.assertEqual(d['selected_master_path'], row['source_path'])

    def test_reject_and_skip_do_not_select_master(self):
        for action in ['redundant','unresolved']:
            d=self.make(self.payload(action=action))
            self.assertEqual(d['note'], '')
            self.assertEqual(d['selected_master_path'],'')
            self.assertEqual(d['metadata'],{})

    def test_source_stamp_guard(self):
        with patch.object(review, 'verify_stamp', side_effect=ValueError('Image changed')):
            with self.assertRaisesRegex(ValueError,'Image changed'):
                self.make(self.payload())

    def test_file_roundtrip_and_rebuildable_existing_db_index(self):
        # A flat temporary text file in the permitted work root; no test database file.
        with tempfile.NamedTemporaryFile(dir=review.ROOT,suffix='.test-jsonl') as handle:
            path=Path(handle.name)
            _,rev=review.load(path)
            p=self.payload(action='unresolved')|{'previous_revision':rev,'note':'Test only'}
            d,newrev=review.save(p,self.rows,self.masters,path)
            saved,loadedrev=review.load(path)
            self.assertEqual(newrev,loadedrev)
            self.assertEqual(saved,{self.child['source_path']:d})
            con=sqlite3.connect(':memory:')
            try:
                review.index_rows(con,saved)
                self.assertTrue(review.index_agrees(con,saved))
                con.execute('DELETE FROM tiff_review_decisions')
                self.assertFalse(review.index_agrees(con,saved))
                review.index_rows(con,saved)
                self.assertTrue(review.index_agrees(con,saved))
            finally:
                con.close()
            with self.assertRaisesRegex(ValueError,'another tab'):
                review.save(p,self.rows,self.masters,path)

    def test_authenticated_handler_saves_readable_and_sqlite_fixture(self):
        import workbench
        from types import SimpleNamespace
        with tempfile.NamedTemporaryFile(dir=review.ROOT,suffix='.test-jsonl') as handle:
            path=Path(handle.name)
            _,rev=review.load(path)
            payload=self.payload(action='redundant')|{'previous_revision':rev}
            raw=json.dumps(payload).encode()
            handler=object.__new__(workbench.Handler)
            handler.path='/api/tiff-review'
            handler.server=SimpleNamespace(server_port=8774,token='test-token')
            handler.headers={'Host':'127.0.0.1:8774','Origin':'http://127.0.0.1:8774','X-Review-Token':'test-token','Content-Length':str(len(raw)),'Content-Type':'application/json'}
            handler.rfile=io.BytesIO(raw)
            replies=[]
            handler.json=lambda body,status=200: replies.append((status,body))
            original_save=review.save
            con=sqlite3.connect(':memory:')
            try:
                with patch.object(review,'save',side_effect=lambda p,c,m:original_save(p,c,m,path)),patch.object(review,'rebuild_index',side_effect=lambda:review.index_rows(con,review.load(path)[0])):
                    handler.do_POST()
                self.assertEqual(replies[0][0],200)
                self.assertTrue(replies[0][1]['ok'])
                self.assertEqual(replies[0][1]['decision']['note'], '')
                self.assertTrue(review.index_agrees(con,review.load(path)[0]))
            finally:
                con.close()


class PreviewTests(unittest.TestCase):
    def test_stored_rows_and_orientation_match_standard_tiff_decode(self):
        from PIL import Image, ImageOps, TiffImagePlugin
        import workbench
        original = Image.new('RGB', (12, 8))
        original.putdata([(x * 20, y * 30, (x + y) * 10)
                          for y in range(8) for x in range(12)])
        for orientation in range(1, 9):
            with self.subTest(orientation=orientation), tempfile.NamedTemporaryFile(
                    dir=review.ROOT, suffix='.test.tif') as handle:
                path = Path(handle.name)
                tags = TiffImagePlugin.ImageFileDirectory_v2()
                tags[274] = orientation
                original.save(path, format='TIFF', compression='raw', tiffinfo=tags)
                before = path.read_bytes()
                with Image.open(path) as decoded:
                    expected = ImageOps.exif_transpose(decoded).convert('RGB')
                actual = workbench.read_uncompressed_tiff_preview(
                    path, edge=100, allowed_root=review.ROOT)
                self.assertEqual(actual.size, expected.size)
                self.assertEqual(actual.tobytes(), expected.tobytes())
                self.assertEqual(path.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
