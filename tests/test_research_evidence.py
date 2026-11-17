"""Bounded packet behavior on invented media, with no archive reads or network."""
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT/'src'), str(ROOT/'Archive-tools')]
from archive_research import Artifact, Page, Recipe, Region, Result, Root, Roots, Source, Transform
from archive_research.evidence import build_packet, code_candidates, evidence_image, refresh_packet
from research_routes import research_profiles
from archive_research.assessment import prepare_assessment


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        from PIL import Image
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.input = self.base/'input'; self.input.mkdir()
        self.output = self.base/'output'; self.output.mkdir()
        self.roots = Roots({'source': Root(self.input, 'read'), 'artifacts': Root(self.output, 'work')})
        path = self.input/'synthetic.png'; Image.new('RGB', (400, 200), 'white').save(path)
        info = path.stat()
        source = Source('synthetic', 'invented', 'original', 'source', path.name,
                        info.st_size, info.st_mtime_ns, role='synthetic')
        self.page = Page(source, 1, 'image', 0, 400, 200)
        self.key = self.receipt('ocr', '1', ['IO', '10', 'Invented', 'person', 'second', 'from', 'left'])
        self.selection = {'format': 'archive-research-selection-1', 'metadata_adopted': False,
                          'case_key': 'synthetic', 'question': 'Which reading is supported?',
                          'profiles': ['equipment', 'tubes', 'people'], 'evidence': [
                              self.item('scene'), self.item('caption')],
                          'caption_links': [{'caption': 1, 'subject': 0, 'basis': 'proximity',
                                             'quote': '', 'position': '', 'note': 'Invented adjacency only.'}],
                          'hypotheses': [{'claim': 'Unresolved type reading', 'supports': [1], 'contradicts': [0]}],
                          'code_readings': [{'evidence': 1, 'literal_reading': 'IO',
                                             'dictionary': [{'value': '10', 'evidence': 1}]}],
                          'omissions': ['Synthetic fixture, no real identity.']}

    def receipt(self, extractor, version, tokens):
        recipe = Recipe.create(extractor, version)
        key = recipe.cache_key(self.page)
        prepared = self.output/(key+'.png')
        prepared.write_bytes((self.input/'synthetic.png').read_bytes())
        raw = self.output/(key+'.txt'); raw.write_text(' '.join(tokens))
        artifacts = tuple(Artifact('artifacts', p.name, kind, hashlib.sha256(p.read_bytes()).hexdigest())
                          for p, kind in [(prepared, 'processed-page'), (raw, 'synthetic-text')])
        result = Result(self.page, recipe, 'complete', .1, artifacts,
                        transform=Transform(400, 200, (1, 0, 0, 0, 1, 0)))
        value = {'result': result.record(), 'display_rotation': 0,
                 'words': [{'text': token, 'region': asdict(Region(.05+i*.1, .2, .08, .1)), 'confidence': .8}
                           for i, token in enumerate(tokens)],
                 'artifact_stamps': {p.name: {'size_bytes': p.stat().st_size, 'mtime_ns': p.stat().st_mtime_ns}
                                     for p in (prepared, raw)}}
        (self.output/(key+'.json')).write_text(json.dumps(value))
        return key

    def item(self, kind):
        return {'key': self.key, 'label': 'Invented '+kind, 'kind': kind,
                'lineage': 'Same invented page.', 'attribution': 'Synthetic test.'}

    def packet(self):
        return build_packet(self.roots, 'artifacts', self.selection,
                            research_profiles(self.selection['profiles']))

    def test_combined_profiles_preserve_text_and_distinguish_proximity(self):
        packet = self.packet()
        self.assertEqual(list(packet['profiles']), ['equipment', 'tubes', 'people'])
        self.assertEqual(packet['caption_links'][0]['status'], 'proximity only')
        self.assertFalse(packet['caption_links'][0]['identity_confirmed'])
        self.assertEqual(packet['code_readings'][0]['literal_reading'], 'IO')
        self.assertEqual(packet['code_readings'][0]['candidates'][0]['value'], '10')
        self.assertIn('IO', packet['evidence'][1]['text'])
        self.assertFalse(packet['external_submission_authorized'])

    def test_ocr_variants_share_one_source_group(self):
        second = self.receipt('ocr', '2', ['IO'])
        self.selection['evidence'].append({**self.item('detail'), 'key': second})
        packet = self.packet()
        self.assertEqual(len(packet['source_groups']), 1)
        self.assertEqual(packet['source_groups'][0]['evidence'], [0, 1, 2])

    def test_cross_references_validate_citations_without_promoting_identity(self):
        link = {'source': 1, 'target': 0, 'basis': 'textual', 'quote': 'Invented person',
                'note': 'Name co-occurrence, not depicted identity.'}
        self.selection['cross_references'] = [link]
        result = self.packet()['cross_references'][0]
        self.assertFalse(result['identity_confirmed'])
        link['quote'] = 'invented absent quotation'
        with self.assertRaisesRegex(ValueError, 'quote absent'):
            self.packet()
        link.update(basis='visual', quote='', target=19)
        with self.assertRaisesRegex(ValueError, 'references'):
            self.packet()
        link['target'] = 0
        self.assertEqual(self.packet()['cross_references'][0]['status'], 'unreviewed lead')

    def test_assessment_draft_binds_images_text_regions_and_omissions(self):
        packet = self.packet()
        draft = prepare_assessment(self.roots, 'artifacts', packet, research_profiles(self.selection['profiles']))
        self.assertEqual(draft['context']['evidence'][1]['text'], packet['evidence'][1]['text'])
        self.assertEqual(draft['context']['omissions'], self.selection['omissions'])
        self.assertEqual(draft['attachments'][0]['artifact']['sha256'],
                         hashlib.sha256((self.output/(self.key+'.png')).read_bytes()).hexdigest())
        self.assertFalse(draft['external_submission_authorized'])
        self.assertIn('untrusted', draft['instructions'])

    def test_assessment_refuses_changed_receipt_or_image(self):
        packet = self.packet()
        path = self.output/(self.key+'.json'); original = path.read_bytes()
        row = json.loads(original); row['words'][0]['confidence'] = .1
        path.write_text(json.dumps(row))
        with self.assertRaisesRegex(ValueError, 'receipt changed'):
            prepare_assessment(self.roots, 'artifacts', packet, research_profiles(self.selection['profiles']))
        path.write_bytes(original)
        (self.output/(self.key+'.png')).write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError, 'unavailable'):
            prepare_assessment(self.roots, 'artifacts', packet, research_profiles(self.selection['profiles']))

    def test_crop_selection_preserves_source_word_positions(self):
        self.selection['evidence'][0]['region'] = asdict(Region(0, 0, .15, 1))
        item = self.packet()['evidence'][0]
        self.assertEqual(item['text'], 'IO')
        self.assertEqual(item['words'][0]['region']['x'], .05)

    def test_invented_caption_quote_and_unordered_person_refused(self):
        self.selection['evidence'][0]['kind'] = 'person'
        link = self.selection['caption_links'][0]
        link.update(basis='explicit-caption', quote='Invented person', position='')
        with self.assertRaisesRegex(ValueError, 'position'):
            self.packet()
        link['position'] = 'second from left'; link['quote'] = 'No such caption'
        with self.assertRaisesRegex(ValueError, 'quote'):
            self.packet()
        link['quote'] = 'Invented person second from left'
        self.assertFalse(self.packet()['caption_links'][0]['identity_confirmed'])

    def test_missing_source_withholds_media_and_marks_link_unavailable(self):
        packet = self.packet()
        (self.input/'synthetic.png').unlink()
        current = refresh_packet(self.roots, 'artifacts', packet, research_profiles(self.selection['profiles']))
        self.assertFalse(current['evidence'][0]['available'])
        self.assertEqual(current['caption_links'][0]['status'], 'unavailable')
        with self.assertRaises(ValueError):
            evidence_image(self.roots, 'artifacts', packet['evidence'][0])

    def test_changed_receipt_and_profile_withhold_old_packet(self):
        packet = self.packet()
        path = self.output/(self.key+'.json')
        value = json.loads(path.read_text()); value['words'][0]['text'] = 'IO changed'
        path.write_text(json.dumps(value))
        with self.assertRaisesRegex(ValueError, 'receipt changed'):
            refresh_packet(self.roots, 'artifacts', packet, research_profiles(self.selection['profiles']))

    def test_invalid_citation_and_path_refused(self):
        self.selection['hypotheses'][0]['supports'] = [99]
        with self.assertRaisesRegex(ValueError, 'references'):
            self.packet()
        self.selection['hypotheses'][0]['supports'] = [0]
        self.selection['evidence'][0]['key'] = '../secret'
        with self.assertRaisesRegex(ValueError, 'receipt key'):
            self.packet()

    def test_dictionary_only_suggests_documented_character_ambiguities(self):
        result = code_candidates('IO', [{'value': '10', 'evidence': 0}, {'value': '1Q', 'evidence': 1}])
        self.assertEqual([r['value'] for r in result['candidates']], ['10'])
        self.assertEqual(len(result['candidates'][0]['changes']), 2)

    def test_changed_image_digest_refuses_same_stamp_tampering(self):
        import os
        packet = self.packet(); path = self.output/(self.key+'.png'); info = path.stat()
        path.write_bytes(b'x'*info.st_size); os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns))
        with self.assertRaisesRegex(ValueError, 'image changed'):
            evidence_image(self.roots, 'artifacts', packet['evidence'][0])


if __name__ == '__main__':
    unittest.main()
