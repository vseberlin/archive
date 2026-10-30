"""NEF scope, persistence and source guard tests. All writes use in-memory data."""
import csv
from contextlib import closing
import io
import json
import sqlite3
import threading
import unittest
from unittest.mock import patch
from urllib.request import Request, urlopen
from urllib.error import HTTPError

import nef_workbench as nef


class MemoryConnection:
    def __init__(self, con):
        self.con = con

    def __getattr__(self, name):
        return getattr(self.con, name)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def close(self):
        pass


class NEFTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = nef.NEFWorkbench('test-in-memory')

    def setUp(self):
        self.con = sqlite3.connect(':memory:')
        self.con.row_factory = sqlite3.Row
        with closing(nef.existing.connect(read_only=True)) as source:
            source.backup(self.con)
        self.con.execute('PRAGMA foreign_keys=ON')
        self.exported = {}
        self.mirror = {
            nef.existing.DECISIONS_CSV: nef.existing.decision_rows(self.con),
            nef.existing.DISPOSITIONS_CSV: nef.existing.disposition_rows(self.con),
            nef.existing.DERIVATIVE_RELATIONSHIPS_CSV: nef.read_csv(nef.existing.DERIVATIVE_RELATIONSHIPS_CSV),
        }
        original_read = nef.read_csv

        def read(path):
            if path in self.mirror:
                return [dict(row) for row in self.mirror[path]]
            return original_read(path)

        def export(path, fields, rows):
            # Exercise CSV serialization as well as the actual query projections.
            output = io.StringIO(newline='')
            writer = csv.DictWriter(output, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)
            output.seek(0)
            self.mirror[path] = list(csv.DictReader(output))
            self.exported[path] = self.mirror[path]

        self.patches = [patch.object(nef.existing, 'connect', return_value=MemoryConnection(self.con)),
                        patch.object(nef, 'read_csv', side_effect=read),
                        patch.object(nef.existing, 'atomic_csv', side_effect=export)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.con.close()

    def payload(self, key='md:93484', action='note', **extra):
        row = self.con.execute('SELECT * FROM decisions WHERE record_key=?', (key,)).fetchone()
        return {'record': key, 'action': action, 'note': 'Test comparison note', 'version': nef.version_of(row), **extra}

    def test_scope_and_existing_mirrors(self):
        self.assertTrue(self.app.check()['passed'])
        self.assertEqual(len(self.app.mappings), 63)
        self.assertEqual(len(self.app.order), 24)
        self.assertEqual(len([r for r in self.app.mappings.values() if r['parent_record_key']]), 39)

    def test_note_save_changes_only_one_note_and_mirrors(self):
        before = [dict(r) for r in self.con.execute('SELECT * FROM candidate_dispositions')]
        decisions = {r['record_key']: dict(r) for r in self.con.execute('SELECT * FROM decisions')}
        self.app.save(self.payload())
        self.assertEqual(before, [dict(r) for r in self.con.execute('SELECT * FROM candidate_dispositions')])
        for r in self.con.execute('SELECT * FROM decisions'):
            if r['record_key'] != 'md:93484':
                self.assertEqual(dict(r), decisions[r['record_key']])
        self.assertEqual(set(self.exported), set(self.mirror))
        nef.mirrors_match(self.con)
        self.assertEqual(self.con.execute("SELECT note FROM decisions WHERE record_key='md:93484'").fetchone()[0], 'Test comparison note')

    def test_stale_form_rejected_without_writes(self):
        with self.assertRaisesRegex(nef.ReviewError, 'another window'):
            self.app.save(self.payload(version='old'))
        self.assertFalse(self.exported)

    def test_non_nef_record_and_cross_object_candidate_rejected(self):
        with self.assertRaises(nef.ReviewError):
            self.app.save({'record': 'md:60246', 'action': 'note'})
        with self.assertRaisesRegex(nef.ReviewError, 'existing readable page TIFF'):
            self.app.save(self.payload(action='select', candidate='VASSET-D7AC75015031B17F6B47640826AA7824'))
        self.assertFalse(self.exported)

    def test_website_jpeg_cannot_become_master(self):
        cid = self.con.execute("SELECT candidate_id FROM candidates WHERE record_key='md:93484' AND extension='jpg' LIMIT 1").fetchone()[0]
        with self.assertRaisesRegex(nef.ReviewError, 'JPEG derivatives'):
            self.app.save(self.payload(action='select', candidate=cid))

    def test_detail_cannot_be_promoted(self):
        with self.assertRaisesRegex(nef.ReviewError, 'derivative cannot be promoted'):
            self.app.save(self.payload('md:93486', 'select', candidate='VASSET-74A17E563E529C94380C8ECD68529A46'))

    def test_unresolved_question_preserves_parent_and_children(self):
        before = [tuple(r) for r in self.con.execute('SELECT record_key,decision_state,selected_candidate_id FROM decisions')]
        self.app.save(self.payload('md:93485', 'unresolved'))
        after = [tuple(r) for r in self.con.execute('SELECT record_key,decision_state,selected_candidate_id FROM decisions')]
        self.assertEqual(before, after)
        nef.invariant_check(self.con)
        nef.mirrors_match(self.con)

    def test_selection_marks_all_alternatives_redundant_and_updates_parent_csv(self):
        key = 'md:93485'
        alternative = 'VASSET-6F8ACEC211C99DB9DF612C2042ED4F3F'
        with patch.object(nef, 'guarded_source', return_value=None):
            self.app.save(self.payload(key, 'select', candidate=alternative))
        rows = self.con.execute('SELECT * FROM candidate_dispositions WHERE record_key=?', (key,)).fetchall()
        self.assertEqual(sum(r['disposition'] == 'selected_master' for r in rows), 1)
        self.assertTrue(all(r['disposition'] == ('selected_master' if r['candidate_id'] == alternative else 'redundant') for r in rows))
        relations = self.mirror[nef.existing.DERIVATIVE_RELATIONSHIPS_CSV]
        child = next(r for r in relations if r['derived_record_key'] == 'md:93486')
        self.assertTrue(child['parent_selected_master_path'].endswith('/Geschichte-und-WF-Archiv/NEF/Album_NEF_Seite_07.tiff'))
        nef.invariant_check(self.con)
        nef.mirrors_match(self.con)

    def test_missing_mount_blocks_candidate_preview_and_selection(self):
        cid = 'VASSET-6F8ACEC211C99DB9DF612C2042ED4F3F'
        with patch.object(nef, 'source_mount', return_value={'available': False}):
            with self.assertRaisesRegex(nef.ReviewError, 'exact protected'):
                self.app.image('md:93485', 'candidate', cid)
            with self.assertRaisesRegex(nef.ReviewError, 'exact protected'):
                self.app.save(self.payload('md:93485', 'select', candidate=cid))
        self.assertFalse(self.exported)

    def test_exact_mount_target_and_device_required(self):
        for source, target, fs, options in [('/dev/sdf1','/media/vladimir/Elements1','ntfs3','ro'),
                                          ('/dev/sdg1','/media/vladimir/Elements','ntfs3','ro'),
                                          ('/dev/sdf1','/media/vladimir/Elements','ntfs3','rw'),
                                          ('/dev/sdf1','/media/vladimir/Elements','ext4','ro')]:
            result = type('Result', (), {'stdout': json.dumps({'filesystems': [{'source':source,'target':target,'fstype':fs,'options':options}]})})()
            with patch.object(nef.subprocess, 'run', return_value=result):
                self.assertFalse(nef.source_mount()['available'])

    def test_changed_source_stat_is_rejected(self):
        _, _, candidates = self.app.record('md:93485')
        candidate = next(c for c in candidates if c['extension'] == 'tiff')
        fake_stat = type('Stat', (), {'st_size':candidate['size_bytes']+1, 'st_mtime_ns':candidate['mtime_ns']})()
        with patch.object(nef, 'source_mount', return_value={'available':True}), \
             patch.object(nef.Path, 'resolve', lambda p, **kw:p), \
             patch.object(nef.Path, 'stat', return_value=fake_stat):
            with self.assertRaisesRegex(nef.ReviewError, 'differs'):
                nef.guarded_source(candidate)

    def test_image_requests_cannot_access_arbitrary_paths_or_candidates(self):
        with self.assertRaises(nef.ReviewError):
            self.app.image('../../etc/passwd', 'master')
        with self.assertRaises(nef.ReviewError):
            self.app.image('md:93484', 'candidate', 'VASSET-D7AC75015031B17F6B47640826AA7824')
        with self.assertRaises(nef.ReviewError):
            self.app.image('md:93484', 'master', size=100000)

    def test_archive_and_saved_web_previews_work_without_source(self):
        with patch.object(nef, 'source_mount', return_value={'available':False}):
            for kind in ('master', 'access', 'web'):
                data = self.app.image('md:93484', kind, size=320)
                with nef.Image.open(io.BytesIO(data)) as im:
                    self.assertEqual(im.format, 'JPEG')
                    self.assertLessEqual(max(im.size), 320)


class HTTPGuards(unittest.TestCase):
    def test_cross_origin_and_missing_token_rejected(self):
        server = nef.Server(0, None)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        port = server.server_address[1]
        try:
            for headers in [
                {'Origin':'http://untrusted.example','X-NEF-Token':server.token},
                {'Origin':f'http://127.0.0.1:{port}','X-NEF-Token':'invalid'},
                {'Origin':f'http://127.0.0.1:{port}','X-NEF-Token':server.token,'Host':'untrusted.example'},
            ]:
                req = Request(f'http://127.0.0.1:{port}/api/decision', data=b'{}', headers={'Content-Type':'application/json', **headers})
                with self.assertRaises(HTTPError) as error:
                    urlopen(req, timeout=5)
                self.assertEqual(error.exception.code, 403)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == '__main__':
    unittest.main()
