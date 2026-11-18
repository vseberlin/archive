"""Mixed retrieval, coverage, paging and source-free queries."""
from copy import deepcopy
from dataclasses import asdict, replace
import hashlib
import json
import unittest
from unittest.mock import patch

import test_research_evidence as fixtures
from archive_research.search_projection import build_projection, query, visual_query, validate, seal
from archive_research import Source


class SearchProjectionTests(unittest.TestCase):
    setUp = fixtures.EvidenceTests.setUp
    receipt = fixtures.EvidenceTests.receipt
    item = fixtures.EvidenceTests.item

    def source(self, name, text):
        path = self.input / name
        path.write_text(text)
        stat = path.stat()
        return Source('synthetic', name, 'records', 'source', name, stat.st_size, stat.st_mtime_ns,
                      hashlib.sha256(path.read_bytes()).hexdigest())

    def catalogue(self):
        source = self.source('catalogue.csv', 'id,title,description\nISS-2026-K7M3-J,Funktechnik,Grüße aus Schöneweide\nlocal:2,Report,Technical apparatus\n')
        return {'source': asdict(source), 'unit': 'synthetic', 'object_field': 'id',
                'fields': {'title': 'title', 'description': 'description'},
                'aliases': {'ISS-2026-K7M3-J': ['WF Forschungsbericht']}}

    def test_mixed_catalogue_text_alias_uid_and_explicit_coverage(self):
        value = build_projection(self.roots, 'artifacts', {'keys': [self.key, 'e'*64], 'catalogues': [self.catalogue()]})
        for text in ('ISS-2026-K7M3-J', 'Gruesse', 'Schoeneweide', 'Forschungsbericht'):
            hit = query(value, text)['hits'][0]
            self.assertEqual(hit['object_uid'], 'ISS-2026-K7M3-J')
            self.assertEqual(hit['kind'], 'catalogue')
        self.assertIn('exact identifier', query(value, 'ISS-2026-K7M3-J')['hits'][0]['match_reasons'])
        self.assertTrue(query(value, 'Invented person')['hits'])
        self.assertEqual(value['coverage']['failed_inputs'], 1)
        self.assertFalse(value['coverage']['whole_archive'])

    def test_missing_source_keeps_retained_text_and_query_never_opens_sources(self):
        (self.input / 'synthetic.png').unlink()
        value = build_projection(self.roots, 'artifacts', {'keys': [self.key]})
        self.assertEqual(value['entries'][0]['availability'], 'offline')
        with patch.object(self.roots, 'source', side_effect=AssertionError('query must not access sources')), \
             patch('pathlib.Path.read_bytes', side_effect=AssertionError('query must not read artifacts')):
            self.assertTrue(query(value, 'Invented')['hits'])

    def test_more_than_2048_receipts_are_not_truncated(self):
        # Real distinct receipt files, sharing only invented source/artifact bytes.
        # Exercise actual readers and verifiers without 2051 model calls.
        from archive_research import Result
        original = json.loads((self.output/(self.key+'.json')).read_text())
        result = Result.from_record(original['result'])
        keys = []
        for i in range(2051):
            current = replace(result, page=replace(result.page, member=i+1))
            key = current.recipe.cache_key(current.page, current.region)
            (self.output/(key+'.json')).write_text(json.dumps({**original, 'result': current.record()}))
            keys.append(key)
        value = build_projection(self.roots, 'artifacts', {'keys': keys})
        result = query(value, 'Invented', limit=20, offset=2040)
        self.assertEqual(result['total_matches'], 2051)
        self.assertEqual(len(result['hits']), 11)
        self.assertIsNone(result['next_offset'])

    def test_same_document_visual_matches_are_available(self):
        value = build_projection(self.roots, 'artifacts', {'keys': [self.key]})
        first = value['entries'][0]; first.update(vector=[1., 0.], space='synthetic')
        second = deepcopy(first); second.update(key='another-page', vector=[.8, .6])
        value['entries'].append(second); seal(value)
        hits = visual_query(value, self.key)
        self.assertEqual(hits['hits'][0]['target'], 'another-page')
        self.assertFalse(hits['same_object_excluded'])

    def test_spelling_lead_is_explicit_and_opt_in(self):
        value = build_projection(self.roots, 'artifacts', {'keys': [], 'catalogues': [self.catalogue()]})
        self.assertEqual(query(value, 'Funktechnk')['hits'], [])
        result = query(value, 'Funktechnk', fuzzy=True)
        self.assertTrue(result['hits'])
        self.assertIn('spelling alternative', result['hits'][0]['match_reasons'])
        self.assertFalse(result['hits'][0]['identity_confirmed'])

    def test_line_end_hyphenation_is_distinct_from_same_line_hyphen(self):
        path = self.output / (self.key + '.json')
        row = json.loads(path.read_text())
        row['words'] = [{'text': 'Funk-', 'region': {'x': .1, 'y': .1, 'width': .3, 'height': .1}},
                        {'text': 'technik', 'region': {'x': .1, 'y': .3, 'width': .3, 'height': .1}}]
        path.write_text(json.dumps(row))
        value = build_projection(self.roots, 'artifacts', {'keys': [self.key]})
        self.assertTrue(query(value, 'Funktechnik')['hits'])
        row['words'][1]['region']['y'] = .1
        path.write_text(json.dumps(row))
        value = build_projection(self.roots, 'artifacts', {'keys': [self.key]})
        self.assertEqual(query(value, 'Funktechnik')['hits'], [])

    def test_corrected_finding_is_searchable_with_review_and_citations(self):
        original = {'case_key': 'case', 'key': 'type', 'revision': 0, 'supersedes_revision': None,
                    'subject_uid': 'ISS-2026-K7M3-J', 'value': 'old exact type', 'review': 'unreviewed',
                    'citation_keys': ['figure'], 'metadata_adopted': False}
        corrected = {**original, 'revision': 1, 'supersedes_revision': 0, 'value': 'B family',
                     'review': 'accepted', 'reviewer': 'Invented reviewer', 'reason': 'Variant unresolved'}
        history = self.source('findings.jsonl', '\n'.join(json.dumps(r) for r in [original, corrected]))
        value = build_projection(self.roots, 'artifacts', {'keys': [], 'findings': [asdict(history)]})
        hit = query(value, 'B family')['hits'][0]
        self.assertEqual(hit['review'], 'accepted')
        self.assertEqual(hit['evidence']['citation_keys'], ['figure'])
        self.assertEqual(query(value, 'old exact type')['hits'], [])

    def test_projection_tampering_and_ambiguous_uid_map_refused(self):
        value = build_projection(self.roots, 'artifacts', {'keys': [self.key]})
        value['entries'][0]['fields']['text'] = 'tampered'
        with self.assertRaisesRegex(ValueError, 'projection changed'):
            validate(value)
        row = {'unit': 'synthetic', 'object_id': 'local', 'uid': 'ISS-2026-K7M3-J'}
        with self.assertRaisesRegex(ValueError, 'Ambiguous'):
            build_projection(self.roots, 'artifacts', {'keys': [], 'identity_bindings': [row, row]})

    def test_catalogue_documentation_without_object_key_remains_unmapped(self):
        source = self.source('notes.csv', 'id,title\n,Documentation without an assigned object\n')
        spec = {'source': asdict(source), 'unit': 'synthetic', 'object_field': 'id', 'fields': {'title': 'title'}}
        value = build_projection(self.roots, 'artifacts', {'keys': [], 'catalogues': [spec]})
        self.assertEqual(value['coverage']['failed_inputs'], 0)
        hit = query(value, 'Documentation')['hits'][0]
        self.assertIsNone(hit['object_uid'])
        self.assertEqual(hit['identity_status'], 'unmapped')


if __name__ == '__main__':
    unittest.main()
