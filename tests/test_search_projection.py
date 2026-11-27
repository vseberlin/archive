"""Mixed retrieval, coverage, paging and source-free queries."""
from copy import deepcopy
from dataclasses import asdict, replace
import hashlib
import json
import unittest
from collections import Counter, defaultdict
from unittest.mock import patch

import test_research_evidence as fixtures
from archive_research.search_projection import build_projection, query, visual_query, validate, seal
from archive_research import Source


class SearchProjectionTests(unittest.TestCase):
    setUp = fixtures.EvidenceTests.setUp
    receipt = fixtures.EvidenceTests.receipt
    item = fixtures.EvidenceTests.item

    def text_projection(self, *fields):
        value = build_projection(self.roots, 'artifacts', {'keys': []})
        from archive_research.search_projection import base_entry, tokens
        postings = defaultdict(list)
        for i, row in enumerate(fields):
            entry = base_entry(str(i), 'extraction', {'unit': 'synthetic', 'object_id': str(i)}, {}, row, {})
            counts = Counter(tokens(' '.join(entry['fields'].values())))
            entry['token_count'] = sum(counts.values())
            value['entries'].append(entry)
            for word, count in counts.items():
                postings[word].append([i, count])
        value['postings'] = dict(postings)
        return seal(value)

    def test_formula_search_retains_disagreement_and_engine_provenance(self):
        from archive_research.extractors.scientific_tokens import compare_readings
        key=self.receipt('scientific-region','test',[])
        path=self.output/(key+'.json');row=json.loads(path.read_text())
        comparison=compare_readings('Sb2S7',r'S b_{2} S_{3}')
        row['feature']={'token_comparison':comparison};path.write_text(json.dumps(row))
        value=build_projection(self.roots,'artifacts',{'keys':[key]})
        entry=value['entries'][0]
        self.assertNotIn('formula_normalized',entry['fields'])
        self.assertFalse(entry['metadata_adopted'])
        self.assertEqual(entry['evidence']['formula_comparison']['review_priority'],'review')
        for q in ('Sb2S3','Sb₂S₃','Sb2S7'):
            self.assertEqual(query(value,q)['total_matches'],1)
        self.assertEqual(query(value,'Sb2S9')['total_matches'],0)
        row['feature']['token_comparison']=compare_readings('Sb2S3','Sb_{2}S_{3}')
        path.write_text(json.dumps(row))
        value=build_projection(self.roots,'artifacts',{'keys':[key]})
        self.assertEqual(value['entries'][0]['fields']['formula_unicode'],'Sb₂S₃')
        self.assertTrue(value['entries'][0]['evidence']['review_required'])

    def test_documentary_names_initials_titles_and_common_surname_noise(self):
        value = self.text_projection({'text': 'Dr.-Ing. H. Müller'}, {'text': 'Müller, H.'},
                                     {'text': 'Karl Müller'}, {'text': 'Müller'},
                                     {'text': 'Hans arbeitet. Müller fehlt.'})
        hits = query(value, 'Hans Müller')['hits']
        self.assertEqual({h['target'] for h in hits}, {'0', '1', '4'})
        for hit in hits:
            self.assertFalse(hit['identity_confirmed'])
        self.assertIn('name initial', next(h for h in hits if h['target'] == '0')['match_reasons'])
        self.assertEqual(query(value, 'Dr.-Ing. Hans Müller')['total_matches'], 3)
        self.assertEqual(query(value, 'hans müller')['total_matches'], 3)
        full = self.text_projection({'text': 'Hans Müller'}, {'text': 'Heinz Müller'}, {'text': 'Karl Müller'})
        self.assertEqual(query(full, 'Müller, H.')['total_matches'], 2)
        unrelated = self.text_projection({'text': 'H. ist eine Abkürzung. Müller forscht.'})
        self.assertFalse(query(unrelated, 'Hans Müller')['hits'])

    def test_ocr_alternatives_even_when_exact_word_exists_elsewhere(self):
        value = self.text_projection({'text': 'Müller Kathode'}, {'text': 'Mii11er Kathode'},
                                     {'text': 'Müller Verwaltung'})
        self.assertEqual(query(value, 'Müller Kathode')['total_matches'], 1)
        hits = query(value, 'Müller Kathode', fuzzy=True)['hits']
        self.assertEqual([h['target'] for h in hits], ['0', '1'])
        self.assertIn('OCR alternative', hits[1]['match_reasons'])
        self.assertNotIn('OCR alternative', hits[0]['match_reasons'])
        self.assertEqual(query(value, 'Mii11er Kathode', fuzzy=True)['total_matches'], 2)

    def test_ocr_confusions_are_opt_in_and_do_not_expand_type_numbers(self):
        for original, ocr in [('Kammer', 'Karnmer'), ('Zelle', 'Ze11e'),
                              ('Leiter', 'Lciter'), ('Kathode', 'Katbode')]:
            with self.subTest(original=original):
                value = self.text_projection({'text': ocr})
                self.assertFalse(query(value, original)['hits'])
                self.assertIn('OCR alternative', query(value, original, fuzzy=True)['hits'][0]['match_reasons'])
        value = self.text_projection({'text': 'AB1235'})
        self.assertFalse(query(value, 'AB1234', fuzzy=True)['hits'])

    def test_compounds_below_exact_and_identifier_terms_required(self):
        value = self.text_projection({'text': 'Bildröhrenentwicklung an Röhre B43'},
                                     {'text': 'Bildröhre B43'}, {'text': 'Bildröhre B44'})
        hits = query(value, 'Bildröhre B43')['hits']
        self.assertEqual([h['target'] for h in hits], ['1', '0'])
        self.assertIn('compound/stem', hits[1]['match_reasons'])
        self.assertFalse(query(value, 'Bildröhre Entwicklung B45', fuzzy=True)['hits'])

    def test_majority_ranks_coverage_first_and_reports_missing_terms(self):
        value = self.text_projection({'text': 'Kathode Entwicklung Bericht'},
                                     {'text': 'Kathode Entwicklung'}, {'text': 'Kathode'})
        hits = query(value, 'Kathode Entwicklung Bericht')['hits']
        self.assertEqual([h['target'] for h in hits], ['0', '1'])
        self.assertEqual(hits[1]['missing_terms'], ['bericht'])
        self.assertIn('2/3 query terms matched', hits[1]['match_reasons'])

    def test_coverage_priority_also_holds_for_long_queries(self):
        terms = ['Thema' + a + b for a in 'opqstu' for b in 'opqst']
        value = self.text_projection({'text': ' '.join(t + 'entwicklung' for t in terms)},
                                     {'text': ' '.join(terms[:-1])})
        hits = query(value, ' '.join(terms))['hits']
        self.assertEqual([h['target'] for h in hits], ['0', '1'])

    def test_author_position_outranks_passing_mention(self):
        value = self.text_projection({'text': 'Hans Müller'}, {'author': 'Hans Müller'},
                                     {'text': 'Bearbeiter: Hans Müller'})
        hits = query(value, 'Hans Müller')['hits']
        self.assertEqual({h['target'] for h in hits[:2]}, {'1', '2'})
        self.assertIn('author/Bearbeiter position', hits[0]['match_reasons'])

    def test_caption_snippet_original_offsets_and_dehyphenation(self):
        text = 'Unrelated article. ' * 100 + 'Bildunterschrift: Pumpstraße im Werk.'
        value = self.text_projection({'text': text})
        hit = query(value, 'Pumpstrasse')['hits'][0]
        self.assertIn('Pumpstraße', hit['snippet'])
        self.assertLessEqual(len(hit['snippet']), 500)
        span = hit['snippet_highlights'][0]
        self.assertEqual(hit['snippet'][span['start']:span['end']], 'Pumpstraße')
        value = self.text_projection({'text': 'Die Bildröh-\nrenentwicklung.'})
        hit = query(value, 'Bildröhre')['hits'][0]
        detail = hit['term_matches'][0]
        self.assertEqual(detail['word'], 'Bildröh-\nrenentwicklung')
        self.assertEqual(''.join(p['text'] for p in hit['snippet_parts']), hit['snippet'])

    def test_canonical_uid_is_never_a_partial_match(self):
        value = self.text_projection({'identifier': 'ISS-2026-K7M3-J'}, {'identifier': 'ISS-2026-K7M3-K'})
        hits = query(value, 'ISS-2026-K7M3-J', fuzzy=True)['hits']
        self.assertEqual([h['target'] for h in hits], ['0'])

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

    def linked_catalogue(self, text):
        source = self.source('linked.csv', text)
        return {'source': asdict(source), 'unit': 'synthetic', 'object_field': 'id',
                'fields': {'title': 'title', 'creator': 'creator', 'description': 'description'},
                'path_fields': ['path'], 'group_objects': True, 'label': 'Invented catalogue'}

    def test_catalogue_pages_share_metadata_but_keep_exact_file_links(self):
        from archive_research.search_projection import catalogue_lookup
        spec = self.linked_catalogue('id,title,creator,description,path\nobj1,Report,Ada Example,Recorded context,masters/page1.tif\nobj1,Report,Ada Example,Recorded context,masters/page2.tif\n')
        value = build_projection(self.roots, 'artifacts', {'keys': [], 'catalogues': [spec]})
        self.assertEqual(len(value['entries']), 1)
        entry = value['entries'][0]
        self.assertEqual(len(entry['evidence']['catalogue_rows']), 2)
        lookup = catalogue_lookup(value)
        self.assertEqual(lookup[('source', 'masters/page2.tif')][0]['fields']['creator'], 'Ada Example')
        self.assertFalse(lookup.get(('different-root', 'masters/page2.tif')))
        self.assertFalse(lookup.get(('source', 'another/page2.tif')))
        hit = query(value, 'Ada Example')['hits'][0]
        self.assertIn('credited creator', hit['match_reasons'])
        self.assertFalse(hit['identity_confirmed'])

    def test_conflicting_catalogue_descriptions_and_unassigned_rows_are_not_merged(self):
        from archive_research.search_projection import catalogue_lookup
        spec = self.linked_catalogue('id,title,creator,description,path\nobj1,First title,Ada,First account,masters/a.tif\nobj1,Other title,Ada,Conflicting account,access/a.jpg\n,Unassigned,,Unknown,documents/one.pdf\n,Unassigned,,Unknown,documents/two.pdf\n')
        value = build_projection(self.roots, 'artifacts', {'keys': [], 'catalogues': [spec]})
        self.assertEqual(len(value['entries']), 4)
        lookup = catalogue_lookup(value)
        self.assertEqual({e['fields']['description'] for e in lookup[('source', 'masters/a.tif')]}, {'First account', 'Conflicting account'})
        self.assertEqual(len(lookup[('source', 'documents/one.pdf')]), 1)

    def test_explicit_object_metadata_joins_by_existing_key_and_retains_provenance(self):
        spec = self.linked_catalogue('id,title,creator,description,path\nobj1,Report,Ada,Recorded context,masters/a.tif\n')
        extra = self.source('objects.csv', 'key,inscription\nobj1,Uncertain handwritten surname\nobj2,Must not leak into obj1\n')
        spec['object_metadata'] = {'source': asdict(extra), 'object_field': 'key', 'fields': {'inscription': 'inscription'}}
        value = build_projection(self.roots, 'artifacts', {'keys': [], 'catalogues': [spec]})
        entry = value['entries'][0]
        self.assertEqual(entry['fields']['inscription'], 'Uncertain handwritten surname')
        self.assertEqual(entry['evidence']['object_metadata']['row_number'], 2)
        self.assertTrue(query(value, 'handwritten surname')['hits'])
        self.assertFalse(query(value, 'Must leak')['hits'])

    def test_catalogue_refresh_preserves_extractions_findings_and_fails_before_partial_update(self):
        from archive_research.search_projection import refresh_catalogues
        value = build_projection(self.roots, 'artifacts', {'keys': [self.key], 'catalogues': [self.catalogue()]})
        before = deepcopy(value)
        spec = self.linked_catalogue('id,title,creator,description,path\nobj1,Report,Ada,Recorded context,masters/a.tif\n')
        updated = refresh_catalogues(self.roots, value, [spec])
        self.assertEqual(updated['entries'][0], before['entries'][0])
        self.assertEqual(updated['review_feedback'], before['review_feedback'])
        self.assertEqual(value, before)
        self.assertEqual(updated['coverage']['catalogue_rows'], 1)
        bad = deepcopy(spec); bad['source']['sha256'] = '0'*64
        with self.assertRaises(ValueError):
            refresh_catalogues(self.roots, value, [spec, bad])
        self.assertEqual(value, before)

    def test_catalogue_representation_traversal_and_ambiguous_metadata_are_refused(self):
        from archive_research.search_projection import refresh_catalogues
        value = build_projection(self.roots, 'artifacts', {'keys': []})
        spec = self.linked_catalogue('id,title,creator,description,path\nobj1,Report,Ada,Context,../other/a.tif\n')
        with self.assertRaisesRegex(ValueError, 'within its unit'):
            refresh_catalogues(self.roots, value, [spec])
        spec = self.linked_catalogue('id,title,creator,description,path\nobj1,Report,Ada,Context,masters/a.tif\n')
        extra = self.source('objects.csv', 'key,inscription\nobj1,One\nobj1,Other\n')
        spec['object_metadata'] = {'source': asdict(extra), 'object_field': 'key', 'fields': {'inscription': 'inscription'}}
        with self.assertRaisesRegex(ValueError, 'Ambiguous supplementary'):
            refresh_catalogues(self.roots, value, [spec])

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

    def test_decomposed_claim_is_replaced_by_independent_searchable_pairs(self):
        base = {'case_key': 'case', 'revision': 0, 'supersedes_revision': None, 'subject': 'photo',
                'citation_keys': ['photo', 'publication'], 'metadata_adopted': False}
        records = [{**base, 'key': 'combined', 'value': 'Overbroad combined claim', 'review': 'unreviewed',
                    'component_assertions': ['one', 'two']},
                   {**base, 'key': 'one', 'value': 'Confirmed newspaper reproduction', 'review': 'accepted'},
                   {**base, 'key': 'two', 'value': 'Rejected diary reproduction', 'review': 'rejected'}]
        history = self.source('pairs.jsonl', '\n'.join(json.dumps(r) for r in records))
        value = build_projection(self.roots, 'artifacts', {'keys': [], 'findings': [asdict(history)]})
        self.assertEqual([e['evidence']['key'] for e in value['entries']], ['one'])
        self.assertEqual(query(value, 'combined')['hits'], [])
        self.assertEqual(query(value, 'diary')['hits'], [])

    def test_case_filter_can_be_scoped_to_one_history(self):
        record = {'case_key': 'parked', 'key': 'outside', 'value': 'Shared term', 'subject': 'photo',
                  'revision': 0, 'supersedes_revision': None, 'review': 'accepted',
                  'citation_keys': ['photo'], 'metadata_adopted': False}
        inside = {**record, 'case_key': 'assessed', 'key': 'inside'}
        local = asdict(self.source('local.jsonl', '\n'.join(json.dumps(r) for r in (record, inside))))
        foreign = asdict(self.source('foreign.jsonl', json.dumps(record)))
        value = build_projection(self.roots, 'artifacts', {'keys': [], 'findings': [local, foreign]})
        options = {'finding_cases': {'assessed'}}
        self.assertEqual(query(value, 'shared', **options)['total_matches'], 1)
        hits = query(value, 'shared', **options, finding_history=(local['root'], local['path']))['hits']
        self.assertEqual({(h['evidence']['history']['path'], h['evidence']['key']) for h in hits},
                         {(local['path'], 'inside'), (foreign['path'], 'outside')})

    def test_review_refresh_reuses_sources_and_keeps_rejection_feedback(self):
        from archive_research.search_projection import refresh_findings
        from archive_research.review_feedback import entry_locator
        row = {'case_key': 'case', 'key': 'reproduction', 'revision': 0, 'supersedes_revision': None,
               'subject': 'archive-photo', 'value': 'Distinctive reproduction proposal', 'review': 'unreviewed',
               'citation_keys': ['page', 'photo'], 'metadata_adopted': False}
        source = self.source('review.jsonl', json.dumps(row))
        value = build_projection(self.roots, 'artifacts', {'keys': [self.key], 'findings': [asdict(source)]})
        retained = deepcopy(value['entries'][0])
        pair = {'query': entry_locator(retained), 'candidate': entry_locator(retained)}
        pair['candidate'] = {**pair['candidate'], 'page': 9}
        rejected = {**row, 'revision': 1, 'supersedes_revision': 0, 'review': 'rejected', 'reproduction_pair': pair}
        with patch.object(self.roots, 'source', side_effect=AssertionError('No source rescan')):
            updated = refresh_findings(value, asdict(source), [rejected])
        self.assertEqual(updated['entries'][0], retained)
        self.assertFalse(query(updated, 'Distinctive reproduction')['hits'])
        self.assertEqual(updated['review_feedback'][0]['review'], 'rejected')
        corrected = {**rejected, 'review': 'accepted', 'revision': 2, 'supersedes_revision': 1}
        updated = refresh_findings(updated, asdict(source), [corrected])
        self.assertEqual(query(updated, 'Distinctive reproduction')['hits'][0]['review'], 'accepted')
        self.assertEqual(len(updated['review_feedback']), 1)
        self.assertEqual(updated['review_feedback'][0]['review'], 'accepted')

    def test_refresh_remaps_multiple_histories_like_a_full_rebuild(self):
        from archive_research.search_projection import refresh_findings, tokens
        from archive_research.review_feedback import entry_locator

        def history(name, records):
            return asdict(self.source(name, '\n'.join(json.dumps(row) for row in records)))

        common = {'case_key': 'case', 'revision': 0, 'supersedes_revision': None,
                  'subject': 'photo', 'citation_keys': ['page', 'photo'], 'metadata_adopted': False}
        locator = entry_locator(build_projection(self.roots, 'artifacts', {'keys': [self.key]})['entries'][0])
        pair = {'query': locator, 'candidate': {**locator, 'page': 9}}
        removed = {**common, 'key': 'pair', 'value': 'obsolete signal signal', 'review': 'accepted',
                   'reproduction_pair': pair}
        corrected = {**common, 'key': 'reading', 'value': 'erasure signal', 'review': 'unreviewed'}
        retained = {**common, 'key': 'steady', 'value': 'Invented signal', 'review': 'unreviewed'}
        other = {**common, 'key': 'other', 'value': 'Other signal signal signal', 'review': 'unresolved',
                 'reproduction_pair': pair}
        records = [removed, corrected, retained]
        first = history('first.jsonl', records)
        second = history('second.jsonl', [other])
        selection = {'keys': [self.key], 'catalogues': [self.catalogue()], 'findings': [first, second]}
        value = build_projection(self.roots, 'artifacts', selection)

        rejected = {**removed, 'revision': 1, 'supersedes_revision': 0, 'review': 'rejected'}
        new_text = {**corrected, 'revision': 1, 'supersedes_revision': 0, 'review': 'accepted',
                    'value': 'updated signal signal'}
        status_only = {**retained, 'revision': 1, 'supersedes_revision': 0, 'review': 'accepted'}
        accepted_again = {**rejected, 'revision': 2, 'supersedes_revision': 1, 'review': 'accepted'}
        for append, current, changed in [([rejected, new_text, status_only],
                                          [rejected, new_text, status_only], new_text),
                                         ([accepted_again], [accepted_again, new_text, status_only], accepted_again)]:
            with self.subTest(review=current[0]['review']):
                records.extend(append)
                updated_source = history('first.jsonl', records)
                original = deepcopy(value)
                with patch('archive_research.search_projection.tokens', wraps=tokens) as tokenize:
                    updated = refresh_findings(value, updated_source, current)
                tokenize.assert_called_once()
                self.assertIn(changed['value'], tokenize.call_args.args[0])
                self.assertEqual(value, original)
                self.assertIs(updated['entries'][0], value['entries'][0])
                self.assertIs(updated['postings']['funktechnik'], value['postings']['funktechnik'])
                # Refresh preserves retained entry order and appends the updated history's findings.
                rebuilt = build_projection(self.roots, 'artifacts', {**selection, 'findings': [second, updated_source]})
                rebuilt['selection']['findings'] = [updated_source, second]
                rebuilt['refreshed_at'] = value['refreshed_at']
                rebuilt['findings_refreshed_at'] = updated['findings_refreshed_at']
                self.assertEqual(validate(updated), seal(rebuilt))
                self.assertEqual(query(updated, 'Invented signal'), query(rebuilt, 'Invented signal'))
                self.assertEqual(updated['review_feedback'][-1]['review'], current[0]['review'])
                value = updated

    def test_visual_query_applies_exact_pair_review_before_pagination(self):
        from archive_research.review_feedback import entry_locator
        value = build_projection(self.roots, 'artifacts', {'keys': [self.key]})
        query_entry = value['entries'][0]
        query_entry.update(vector=[1., 0.], space='synthetic')
        for key, page, vector in [('rejected', 1, [1., 0.]), ('new', 2, [.9, .1]), ('accepted', 3, [.5, .5])]:
            entry = deepcopy(query_entry);entry.update(key=key, vector=vector)
            entry['evidence']['page']['index'] = page
            value['entries'].append(entry)
        value['review_feedback'] = [{'pair': {'query': entry_locator(query_entry), 'candidate': entry_locator(e)},
                                    'review': e['key']} for e in value['entries'][1:] if e['key'] != 'new']
        seal(value)
        result = visual_query(value, self.key, limit=1)
        self.assertEqual(result['hits'][0]['target'], 'accepted')
        self.assertEqual(result['hidden_rejected'], 1)
        self.assertEqual(visual_query(value, self.key, offset=1)['hits'][0]['target'], 'new')
        self.assertEqual(visual_query(value, self.key, show_rejected=True)['hits'][-1]['target'], 'rejected')

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
