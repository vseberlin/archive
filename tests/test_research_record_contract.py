"""Executable 4b.1 design examples, NOT tests of a production allocator/runner.

Only the invented adjacent JSON is read. No source files, databases, model calls
or writable application state are used. Validation here is fixture-scoped; the
runtime must implement and qualify its own boundaries in the following slice.
"""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
import unittest


FIXTURE = Path(__file__).with_name('research-contract-4b1.json')
ALPHABET = '0123456789ABCDEFGHJKMNPQRSTVWXYZ'


def digest(value):
    raw = json.dumps(value, sort_keys=True, ensure_ascii=False,
                     allow_nan=False, separators=(',', ':')).encode('utf-8')
    return hashlib.sha256(raw).hexdigest()


def require(condition, reason):
    if not condition:
        raise ValueError(reason)


def valid_uid(value):
    match = re.fullmatch(r'ISS-([0-9]{4})-([' + ALPHABET + r']{4})-([' + ALPHABET + r'])', value)
    return bool(match and match[3] == ALPHABET[
        hashlib.sha256((match[1] + match[2]).encode('ascii')).digest()[0] & 31])


def identity_rows(snapshot):
    """Check proposed ownership records; do not mint or reserve anything."""
    entities, requests = {}, set()
    for entity in snapshot['entities']:
        uid = entity['uid']
        require(valid_uid(uid), 'Invalid canonical UID')
        require(uid not in entities, 'Duplicate canonical UID, including retired/reserved')
        request = entity['allocation_request']
        address = (request['scope'], request['key'])
        require(all(isinstance(x, str) and x for x in address), 'Missing allocation request')
        require(address not in requests, 'Duplicate allocation request')
        require(entity['kind'] in {'object', 'representation', 'page', 'region', 'file', 'transfer'},
                'Unknown referent kind')
        require(entity['state'] in {'active', 'reserved', 'retired'}, 'Unknown allocation state')
        require(entity['state'] == 'reserved' or entity['canonical_owner'], 'Missing canonical owner')
        entities[uid] = entity
        requests.add(address)
    return entities


def envelope_rows(rows):
    result = {}
    for row in rows:
        require(row['sha256'] == digest(row['payload']), 'Immutable payload digest differs')
        require(row['sha256'] not in result, 'Duplicate immutable record')
        result[row['sha256']] = row['payload']
    return result


def validate_fixture(value):
    require(value['synthetic'] is True, 'Only invented fixtures are supported')
    entities = identity_rows(value['identity_snapshot'])
    contents = set()
    for sample in value['input_samples']:
        require(sample['sha256'] == hashlib.sha256(sample['text'].encode('utf-8')).hexdigest(),
                'Sample content digest differs')
        contents.add(sample['sha256'])
    for binding in value['bindings']:
        for field, kind in (('object_uid', 'object'), ('representation_uid', 'representation'),
                            ('page_uid', 'page'), ('file_uid', 'file'), ('transfer_uid', 'transfer')):
            uid = binding[field]
            require(uid is None or uid in entities and entities[uid]['kind'] == kind
                    and entities[uid]['state'] == 'active', 'Binding referent kind or state differs')
        require(binding['object_uid'] is not None or
                binding['file_uid'] is not None and binding['transfer_uid'] is not None,
                'Unresolved object needs an incoming receipt referent')
        require(binding['source']['sha256'] in contents, 'Binding content is unverified in fixture')
    computations = envelope_rows(value['computations'])
    results = envelope_rows(value['results'])
    for computation in computations.values():
        require(computation['input']['sha256'] in contents, 'Computation input is unbound')
        for dependency in computation['dependencies']:
            parent = results.get(dependency['result_sha256'])
            require(parent is not None, 'Missing immutable parent result')
            require(dependency['output_sha256'] in {o['sha256'] for o in parent['outputs']},
                    'Missing consumed parent output')
    for result in results.values():
        require(result['computation_sha256'] in computations, 'Missing computation')
        envelope_rows(result['outputs'])
        require(not result['completion']['retryable'] or result['completion']['status'] != 'complete',
                'Unfinished work cannot be complete')
    for attempt in value['attempts']:
        require(attempt['result_sha256'] in results and
                results[attempt['result_sha256']]['computation_sha256'] == attempt['computation_sha256'],
                'Attempt points to another computation')
    citations = {}
    for citation in value['citations']:
        require(citation['key'] not in citations, 'Duplicate citation key')
        require(citation['sha256'] == digest({k: v for k, v in citation.items() if k != 'sha256'}),
                'Immutable citation digest differs')
        result = results.get(citation['result_sha256'])
        require(result is not None, 'Missing cited result')
        outputs = {o['sha256']: o['payload'] for o in result['outputs']}
        require(citation['output_sha256'] in outputs, 'Missing cited output')
        computation = computations[result['computation_sha256']]
        require(citation['source_sha256'] == computation['input']['sha256'] and
                citation['technical_view'] == computation['input']['technical_view'],
                'Citation source differs')
        require(citation['literal_reading'] == outputs[citation['output_sha256']]['text'],
                'Fixture literal reading differs')
        for field, kind in (('object_uid', 'object'), ('representation_uid', 'representation'),
                            ('page_uid', 'page')):
            require(citation[field] in entities and entities[citation[field]]['kind'] == kind,
                    'Citation referent differs')
        citations[citation['key']] = citation
    current = {}
    for finding in value['findings']:
        address = (finding['case_key'], finding['key'])
        previous = current.get(address)
        require(finding['revision'] == (previous['revision'] + 1 if previous else 0) and
                finding['supersedes_revision'] == (previous['revision'] if previous else None),
                'Broken correction linkage')
        require(not previous or all(finding[k] == previous[k] for k in ('subject_uid', 'relation')),
                'Correction changes assertion identity')
        require(finding['subject_uid'] in entities and entities[finding['subject_uid']]['kind'] == 'object',
                'Finding subject differs')
        require(finding['citation_keys'] and set(finding['citation_keys']) <= citations.keys(),
                'Missing finding citation')
        require(finding['citation_digests'] == {key: citations[key]['sha256']
                                               for key in finding['citation_keys']},
                'Finding citation version differs')
        require(finding['metadata_adopted'] is False, 'Research does not adopt metadata')
        require(finding['review'] in {'unreviewed', 'accepted', 'rejected', 'unresolved'}, 'Invalid review')
        require(finding['review'] == 'unreviewed' or
                all(finding[k] for k in ('reviewer', 'reviewed_at', 'reason', 'scope', 'uncertainty')),
                'Reviewed finding lacks attribution or scope')
        current[address] = finding
    return current


class RecordContractTests(unittest.TestCase):
    def setUp(self):
        self.fixture = json.loads(FIXTURE.read_text(encoding='utf-8'))

    def test_complete_fixture_and_frozen_check_symbol(self):
        self.assertTrue(valid_uid('ISS-2026-K7M3-J'))
        self.assertFalse(valid_uid('ISS-2026-K7M3-0'))
        current = validate_fixture(self.fixture)
        self.assertEqual(current[('synthetic-case', 'type-finding')]['revision'], 2)

    def test_retired_and_reserved_ids_still_occupy_the_namespace(self):
        for state in ('retired', 'reserved'):
            value = deepcopy(self.fixture['identity_snapshot'])
            row = deepcopy(next(r for r in value['entities'] if r['state'] == state))
            row.update(state='active', canonical_owner={'namespace': 'other', 'record': 'new'})
            row['allocation_request']['key'] = 'new-request'
            value['entities'].append(row)
            with self.subTest(state=state), self.assertRaisesRegex(ValueError, 'Duplicate canonical UID'):
                identity_rows(value)

    def test_one_request_cannot_claim_two_uids(self):
        self.fixture['identity_snapshot']['entities'][1]['allocation_request'] = deepcopy(
            self.fixture['identity_snapshot']['entities'][0]['allocation_request'])
        with self.assertRaisesRegex(ValueError, 'Duplicate allocation request'):
            validate_fixture(self.fixture)

    def test_incoming_equal_bytes_do_not_resolve_the_object(self):
        existing, incoming = self.fixture['bindings']
        self.assertEqual(existing['source']['sha256'], incoming['source']['sha256'])
        self.assertIsNone(incoming['object_uid'])
        incoming['object_uid'] = incoming['file_uid']
        with self.assertRaisesRegex(ValueError, 'Binding referent kind'):
            validate_fixture(self.fixture)

    def test_binding_changes_preserve_computation_and_historical_evidence(self):
        template = self.fixture['computations'][0]['payload']
        original_results = deepcopy(self.fixture['results'])
        original_citations = deepcopy(self.fixture['citations'])
        for scenario in self.fixture['scenarios']:
            with self.subTest(scenario=scenario['name']):
                self.assertNotEqual(scenario['before'], scenario['after'])
                for binding in (scenario['before'], scenario['after']):
                    descriptor = deepcopy(template)
                    descriptor['input'] = {'sha256': binding['source']['sha256'],
                                           'technical_view': binding['technical_view']}
                    self.assertEqual(digest(descriptor), scenario['expected_computation_sha256'])
                self.assertEqual(scenario['retained_result_sha256'], original_results[0]['sha256'])
        self.assertEqual(self.fixture['results'], original_results)
        self.assertEqual(self.fixture['citations'], original_citations)

    def test_new_pixels_frame_or_recipe_require_new_computation(self):
        original = self.fixture['computations'][0]
        for change in ('content', 'frame', 'recipe'):
            value = deepcopy(original['payload'])
            if change == 'content':
                value['input']['sha256'] = hashlib.sha256(b'changed scan').hexdigest()
            elif change == 'frame':
                value['input']['technical_view'].update(kind='tiff', index=1)
            else:
                value['recipe']['version'] = 'different'
            with self.subTest(change=change):
                self.assertNotEqual(digest(value), original['sha256'])

    def test_reocr_keeps_old_citation_and_selectively_changes_dependents(self):
        old, new, child = self.fixture['results']
        self.assertNotEqual(old['sha256'], new['sha256'])
        self.assertEqual(self.fixture['citations'][0]['result_sha256'], old['sha256'])
        computation = deepcopy(self.fixture['computations'][2]['payload'])
        self.assertEqual(computation['dependencies'][0]['result_sha256'], old['sha256'])
        computation['dependencies'][0].update(result_sha256=new['sha256'],
                                             output_sha256=new['payload']['outputs'][0]['sha256'])
        self.assertNotEqual(digest(computation), child['payload']['computation_sha256'])
        validate_fixture(self.fixture)

    def test_attempt_timing_is_not_result_identity(self):
        previous = deepcopy(self.fixture['results'])
        self.fixture['attempts'][0]['elapsed_seconds'] = 90.0
        validate_fixture(self.fixture)
        self.assertEqual(previous, self.fixture['results'])

    def test_changed_result_is_not_historical_evidence(self):
        self.fixture['results'][0]['payload']['outputs'][0]['payload']['text'] = 'Changed reading'
        with self.assertRaisesRegex(ValueError, 'Immutable payload digest'):
            validate_fixture(self.fixture)

    def test_missing_parent_is_refused(self):
        self.fixture['results'].pop(0)
        with self.assertRaisesRegex(ValueError, 'Missing immutable parent'):
            validate_fixture(self.fixture)

    def test_changed_source_binding_is_not_verified(self):
        self.fixture['bindings'][0]['source']['sha256'] = hashlib.sha256(b'changed').hexdigest()
        with self.assertRaisesRegex(ValueError, 'Binding content is unverified'):
            validate_fixture(self.fixture)

    def test_historical_citation_cannot_silently_switch_to_new_ocr(self):
        citation = self.fixture['citations'][0]
        new = self.fixture['results'][1]
        citation.update(result_sha256=new['sha256'], output_sha256=new['payload']['outputs'][0]['sha256'])
        with self.assertRaisesRegex(ValueError, 'Immutable citation digest'):
            validate_fixture(self.fixture)

    def test_consistent_new_citation_cannot_replace_a_findings_original(self):
        citation = self.fixture['citations'][0]
        citation['printed_label'] = 'new label'
        citation['sha256'] = digest({k: v for k, v in citation.items() if k != 'sha256'})
        with self.assertRaisesRegex(ValueError, 'Finding citation version'):
            validate_fixture(self.fixture)

    def test_correction_retains_prior_wording_and_rejects_lost_revision(self):
        self.assertEqual(self.fixture['findings'][1]['value'], 'Type B')
        self.assertEqual(self.fixture['findings'][2]['value'], 'B family; variant unresolved')
        self.fixture['findings'].pop(1)
        with self.assertRaisesRegex(ValueError, 'Broken correction linkage'):
            validate_fixture(self.fixture)

    def test_review_cannot_adopt_metadata(self):
        self.fixture['findings'][-1]['metadata_adopted'] = True
        with self.assertRaisesRegex(ValueError, 'Research does not adopt'):
            validate_fixture(self.fixture)


if __name__ == '__main__':
    unittest.main()
