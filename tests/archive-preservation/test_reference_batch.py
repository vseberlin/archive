"""Offline tests for work-area/visual-similarity/reference_batch.py.

The deployed helper modules (archive_common, codex_image_pilot,
codex_reference_pilot) live only in the work area, so minimal stand-ins are
installed here. No network, no Codex CLI.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
import tempfile
import types
import unittest
import urllib.error
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from PIL import Image

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / 'work-area' / 'visual-similarity' / 'reference_batch.py'


def install_stubs():
    common = types.ModuleType('archive_common')
    def require(cond, msg):
        if not cond: raise ValueError(msg)
    common.require = require
    common.atomic_write = lambda p, b: Path(p).write_bytes(b)
    common.read_bytes = lambda p: Path(p).read_bytes()
    common.work_root = lambda p: Path(p)
    @contextmanager
    def file_lock(_):
        yield
    common.file_lock = file_lock
    pilot = types.ModuleType('codex_image_pilot')
    pilot.CODEX = 'codex'
    pilot.digest = lambda p: hashlib.sha256(Path(p).read_bytes()).hexdigest()
    ref = types.ModuleType('codex_reference_pilot')
    ref.S = {'type': 'string'}; ref.B = {'type': 'boolean'}; ref.N = {'type': 'number'}
    ref.obj = lambda **p: {'type': 'object', 'properties': p, 'required': list(p), 'additionalProperties': False}
    ref.arr = lambda i: {'type': 'array', 'items': i}
    sys.modules.update(archive_common=common, codex_image_pilot=pilot, codex_reference_pilot=ref)


install_stubs()
SPEC = importlib.util.spec_from_file_location('reference_batch', SCRIPT)
RB = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RB)


def jpeg(path, size=(400, 300), color='gray'):
    Image.new('RGB', size, color).save(path, format='JPEG'); return path


class Args:
    research_root = work_root = registry = None
    prefix = 't'; model = 'm'; limit = 0; pilot_only = False
    compare_effort = 'medium'; max_rounds = 2; max_item_tokens = 10**6; max_average_tokens = 10**6; discovery_web = False


class ReferenceBatchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name)
        reg = self.root/'registry.json'
        reg.write_text(json.dumps({'sources': [{'id': 'ldb', 'domains': ['denkmaldatenbank.berlin.de']}]}))
        args = Args(); args.research_root = args.work_root = self.root; args.registry = reg
        self.runner = RB.Runner(args)
        self.runner.settings = {'compare_effort': 'medium', 'max_rounds': 2, 'max_item_tokens': 10**6,
                                'max_average_tokens': 10**6, 'discovery_web': False}
        self.runner.fetcher.validate = lambda url: None

    def tearDown(self): self.tmp.cleanup()

    def test_crop_boxes_clamped(self):
        boxes = RB.crop_boxes([{'label': 'a', 'x': .95, 'y': -1, 'w': .01, 'h': 3},
                               {'label': 'b', 'x': 'bad', 'y': 0, 'w': .5, 'h': .5}])
        self.assertEqual(len(boxes), 1)
        b = boxes[0]
        self.assertGreaterEqual(b['w'], .15); self.assertLessEqual(b['x']+b['w'], 1); self.assertEqual(b['h'], 1)

    def test_sheet_and_crops(self):
        cands = [{'cid': f'C{i}', 'local_image': str(jpeg(self.root/f'{i}.jpg'))} for i in range(1, 8)]
        sheet = RB.contact_sheet(cands, self.root/'sheet.jpg')
        with Image.open(sheet) as im: self.assertEqual(im.size, (6*RB.TILE, 2*(RB.TILE+30)))
        crops = RB.make_crops(jpeg(self.root/'in.jpg'), [{'label': 'x', 'x': .1, 'y': .1, 'w': .3, 'h': .3}], self.root/'p')
        self.assertEqual(len(crops), 1)
        with Image.open(crops[0]) as im: self.assertEqual(max(im.size[0], im.size[1]-0), 900)

    def test_page_uses_own_caption(self):
        page = RB.Page()
        page.feed('<p>nav</p><figure><img src="a.jpg" alt="Hofansicht"><figcaption>Fabrikhof 1925</figcaption></figure>')
        self.assertEqual(page.images[0], {'src': 'a.jpg', 'alt': 'Hofansicht', 'caption': 'Fabrikhof 1925'})

    def test_transient_failures_not_cached_permanently(self):
        f = self.runner.fetcher
        err = urllib.error.HTTPError('u', 503, 'busy', {}, None)
        with mock.patch.object(RB.urllib.request.OpenerDirector, 'open', side_effect=err):
            with self.assertRaises(urllib.error.HTTPError): f.fetch('https://x/a')
            with self.assertRaises(ValueError): f.fetch('https://x/a')   # same run: not re-requested
        meta = next(self.root.glob('t-http-*.json'))
        self.assertFalse(json.loads(meta.read_text())['permanent'])
        err404 = urllib.error.HTTPError('u', 404, 'gone', {}, None)
        with mock.patch.object(RB.urllib.request.OpenerDirector, 'open', side_effect=err404):
            with self.assertRaises(urllib.error.HTTPError): f.fetch('https://x/b')
        f.failed.clear()   # simulate a new run
        with self.assertRaisesRegex(ValueError, '404'): f.fetch('https://x/b')

    def test_wikidata_and_commons_parsing(self):
        f = self.runner.fetcher
        def fake(url):
            if 'wbsearchentities' in url:
                return json.dumps({'search': [{'id': 'Q1', 'label': 'Kulturbrauerei'}]}).encode(), {}
            if 'wbgetentities' in url:
                return json.dumps({'entities': {'Q1': {'claims': {
                    'P625': [{'mainsnak': {'datavalue': {'value': {'latitude': 52.5, 'longitude': 13.4}}}}],
                    'P373': [{'mainsnak': {'datavalue': {'value': 'Kulturbrauerei'}}}]}}}}).encode(), {}
            return json.dumps({'query': {'pages': {'5': {'pageid': 5, 'title': 'File:K 09040000.jpg', 'imageinfo': [{
                'url': 'https://upload.wikimedia.org/k.jpg', 'thumburl': 'https://upload.wikimedia.org/k900.jpg',
                'descriptionurl': 'https://commons.wikimedia.org/wiki/File:K.jpg', 'mime': 'image/jpeg',
                'extmetadata': {'ImageDescription': {'value': '<b>Hof</b>'}}}]}}}}).encode(), {}
        f.fetch = fake
        place = f.wikidata('Kulturbrauerei')
        self.assertEqual((place['qid'], place['coordinates'], place['commons_category']), ('Q1', [52.5, 13.4], 'Kulturbrauerei'))
        entry = f.commons_geo(52.5, 13.4)[0]
        self.assertEqual((entry['retrieval'], entry['caption'], entry['candidate_image_url']), ('geosearch', 'Hof', 'https://upload.wikimedia.org/k900.jpg'))
        self.assertIn('obj_dok_nr=09040000', entry['linked_records'][0])

    def _process(self, compare_result, picks=('C1',)):
        preview = jpeg(self.root/'preview.jpg')
        item = {'object_id': 'O1', 'preview_path': str(preview), 'preview_sha256': RB.digest(preview), 'title': 't',
                'description': 'Fabrikhof', 'hypotheses': ['Kulturbrauerei'], 'human_note': '', 'human_metadata': {},
                'collection_cautions': [], 'inscriptions': []}
        f = self.runner.fetcher
        f.wikidata = lambda name: {'qid': 'Q1', 'coordinates': [52.5, 13.4], 'commons_category': None, 'query': name}
        n = iter(range(100))
        def entry(kind, *_):
            i = next(n)
            return [{'source_id': 'commons', 'record_url': f'https://commons.wikimedia.org/{kind}{i}', 'title': kind, 'caption': '',
                     'candidate_image_url': f'u{i}', 'linked_records': [], 'retrieval': kind, 'query': ''}]
        f.commons_geo = lambda *a: entry('geo'); f.commons_depicts = lambda *a: entry('dep'); f.commons = lambda *a: entry('txt')
        def image(url):
            p = jpeg(self.root/f'img-{url}.jpg', color=((int(url[1:])*53) % 255, (int(url[1:])*97) % 255, 30))
            return {'image_url': url, 'local_image': str(p), 'image_sha256': RB.digest(p), 'download_receipt': {}, 'image_delivered': False}
        f.image = image
        results = {
            'discover': {'visual_features': ['Schornstein'], 'crop_boxes': [{'label': 'Tor', 'x': 0, 'y': 0, 'w': .5, 'h': .5}],
                         'searchable': True, 'reason': 'r', 'place_names': ['Kulturbrauerei'], 'commons_queries': ['Kulturbrauerei Hof'],
                         'records': [], 'limitations': []},
            'shortlist': {'picks': [{'candidate_id': c, 'verdict': 'possible', 'reason': ''} for c in picks],
                          'next': {'place_names': [], 'commons_queries': []}},
            'compare': compare_result}
        calls = []
        def call(stem, prompt, schema, images=(), web=False, effort='low', timeout=150):
            kind = stem.rsplit('-', 1)[1]; calls.append((stem, [Path(i).name for i in images], effort))
            res = results[kind]
            RB.jsonschema.validate(res, schema)
            return {'result': res, 'errors': [], 'usage': {'total_tokens': 100}}
        self.runner.call = call
        return self.runner.process(item, 1), calls

    def assessment(self, status='tentative', ids=('R1',)):
        return {'status': status, 'value': 0, 'explanation': '', 'reference_ids': list(ids)}

    def compare(self, **kw):
        base = {'comparisons': [{'reference_id': 'R1', 'matches': '', 'differences': '', 'limitation': ''}],
                'location': self.assessment(), 'photograph_date': self.assessment('unresolved', ()), 'hypotheses': [],
                'summary': '', 'limitations': [], 'needs_more_references': False,
                'next': {'place_names': [], 'commons_queries': []}}
        base.update(kw); return base

    def test_process_compares_with_crops_and_labels(self):
        row, calls = self._process(self.compare())
        self.assertEqual(row['status'], 'compared')
        self.assertEqual([c[0] for c in calls], ['001-discover', '001-r1-shortlist', '001-r1-compare'])
        stem, images, effort = calls[-1]
        self.assertEqual(effort, 'medium')
        self.assertEqual(images, ['t-001-input.jpg', 't-001-crop-A.jpg', 't-001-r1-R1.jpg'])
        self.assertTrue(row['references'][0]['image_delivered'])
        self.assertEqual({p['retrieval'] for p in row['rounds'][0]['pool']}, {'geo', 'dep', 'txt'})

    def test_bad_citation_fails_row_not_batch(self):
        row, _ = self._process(self.compare(location=self.assessment('supported', ('R9',))))
        self.assertEqual(row['status'], 'failed')
        self.assertIn('Result check', row['rounds'][0]['comparison']['errors'][-1])

    def test_second_round_from_comparison(self):
        nxt = {'place_names': ['Bötzow-Brauerei'], 'commons_queries': []}
        row, calls = self._process(self.compare(needs_more_references=True, next=nxt))
        self.assertEqual(len(row['rounds']), 2)
        self.assertEqual(row['final_round'], 2)
        # tentative R1 from round 1 is carried into round 2 alongside the new pick
        self.assertEqual(len(row['references']), 2)
        self.assertNotEqual(row['rounds'][0]['pool'][0]['record_url'], row['rounds'][1]['pool'][0]['record_url'])

    def test_hallucinated_shortlist_ids_ignored(self):
        row, calls = self._process(self.compare(), picks=('C99',))
        self.assertEqual(row['status'], 'no_visual_candidate')
        self.assertEqual(row['rounds'][0]['dropped_picks'], ['C99'])


if __name__ == '__main__':
    unittest.main()
