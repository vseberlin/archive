"""Generalized reconciliation contract, using generated files only."""
import argparse
from contextlib import redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

from archive_common import ReviewError
from catalogues import EmptyIndex, read_unit
from consolidation import Evidence, assigned_md_id
from matching import stamp
from search import prepare
from workbench import EDIT_FIELDS, Workbench


class ContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='archive-contract-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'incoming'; self.source.mkdir()
        self.units = []
        self.specs = []
        self.args = argparse.Namespace(source=[self.source], against=self.specs, work_root=self.root / 'review',
            archive_masters_fixed=False,
            index=None, czkawka=None, prior_review=[], relations=[], source_hashes=[], hash_missing=False,
            destination=None, source_extensions='tif,tiff,jpg,jpeg', master_extensions='tif,tiff,jpg,jpeg',
            max_candidates=5, threads=2)

    def picture(self, path, size=(80, 60), caption=None):
        path.parent.mkdir(parents=True, exist_ok=True)
        image = Image.new('RGB', size, (40, 80, 130))
        kwargs = {'tiffinfo': {270: caption}} if caption else {}
        image.save(path, **kwargs)
        return path

    def unit(self, name='unit', extension='tif', size=(80, 60), metadata=None, surrogate=False, hashed=True, record_key='same-key'):
        unit = self.root / name; unit.mkdir()
        master = self.picture(unit / ('master.' + extension), size)
        obj = {'record_key': record_key, 'metadata': metadata or {},
               'master': {'path': master.name, 'is_surrogate': surrogate,
                          'sha256': hashlib.sha256(master.read_bytes()).hexdigest() if hashed else ''}}
        spec = self.root / (name + '.json')
        spec.write_text(json.dumps({'format': 'archive-tools-unit-1', 'unit_root': str(unit), 'objects': [obj]}))
        self.units.append(unit); self.specs.append(spec)
        return master

    def relate(self, source, other, kind='same_photo', stale=False):
        path = self.root / 'relationships.jsonl'
        row = {'format': 'archive-tools-relation-1', 'confirmed': True, 'basis': 'synthetic human confirmation',
               'relation': kind, 'source': {'path': str(source), **stamp(source)}, 'other': {'path': str(other), **stamp(other)}}
        if stale:
            row['source']['mtime_ns'] -= 1
        with path.open('a') as handle:
            handle.write(json.dumps(row) + '\n')
        self.args.relations = [path]

    def run_prepare(self, name=None):
        if name:
            self.args.work_root = self.root / name
        with redirect_stdout(io.StringIO()):
            summary = prepare(self.args)
        return summary, Workbench(self.args.work_root, 'synthetic reviewer')

    def save(self, app, oid, **changes):
        snap = app.snapshot(oid)
        app.save({'object_id': oid, 'revision': snap['revision'],
                  **{k: snap['decision'][k] for k in EDIT_FIELDS}, **changes})

    def test_multiple_folders_units_overlap_and_missing_hashes(self):
        first = self.unit('first', hashed=False)
        second = self.unit('second', 'jpg', (20, 15))
        a = self.source / 'nested' / 'a.tif'; a.parent.mkdir(); shutil.copyfile(first, a)
        other = self.root / 'other'; other.mkdir(); shutil.copyfile(second, other / 'b.jpg')
        self.args.source = [self.source, a.parent, other]
        self.args.hash_missing = True
        before = {p: (stamp(p), p.read_bytes()) for d in [self.source, other, *self.units] for p in d.rglob('*') if p.is_file()}
        summary, app = self.run_prepare()
        self.assertEqual(summary['source_count'], 2)
        self.assertEqual(summary['counts'], {'redundant': 2})
        self.assertFalse(app.objects)
        rows = [json.loads(line) for line in (self.args.work_root / 'source-dispositions.jsonl').read_text().splitlines()]
        self.assertEqual(len(rows[0]['source_roots']), 2)
        self.assertNotEqual(rows[0]['archive_object_ids'], rows[1]['archive_object_ids'])
        self.assertTrue(all(not r['deletion_authorized'] for r in rows))
        for path, (observed, content) in before.items():
            self.assertEqual((stamp(path), path.read_bytes()), (observed, content))

    def test_confirmed_tiff_upgrades_jpeg_preserving_object_identity(self):
        master = self.unit(extension='jpg')
        tiff = self.picture(self.source / 'original.tif')
        crop = self.picture(self.source / 'crop.jpg', (20, 15))
        self.relate(tiff, master); self.relate(crop, tiff)
        summary, app = self.run_prepare()
        self.assertEqual(summary['counts'], {'master_upgrade': 1})
        oid = next(iter(app.objects)); snap = app.snapshot(oid)
        selected = app.candidates[oid][snap['decision']['selected_candidate_id']]
        self.assertEqual(selected['members'][0]['path'], str(tiff))
        self.assertEqual(snap['selection_context']['action'], 'proposed_master_upgrade')
        self.assertEqual(snap['selection_context']['archive_object_id'], str(self.units[0]) + '#same-key')
        self.assertEqual(len(snap['decision']['redundant_candidate_ids']), 1)
        self.assertTrue(app.snapshot()['objects'][0]['review_pending'])

    def test_fixed_archive_keeps_jpeg_master_despite_larger_tiff_and_metadata(self):
        self.args.archive_masters_fixed = True
        master = self.unit(extension='jpg', size=(40, 30))
        photo = self.picture(self.source / 'original.tif', (80, 60), caption='Additional source information')
        self.relate(photo, master)
        before = master.read_bytes()
        result, app = self.run_prepare()
        self.assertEqual(result['counts'], {'redundant': 1})
        self.assertFalse(app.objects)
        self.assertTrue(app.config['archive_masters_fixed'])
        self.assertEqual(master.read_bytes(), before)
        row = json.loads((self.args.work_root / 'source-dispositions.jsonl').read_text())
        self.assertEqual(row['retained_master_path'], str(master))
        self.assertTrue(row['evidence'])

    def test_discovery_prepares_confirmed_tiff_for_md_substitute_jpeg_review(self):
        self.args.archive_masters_fixed = True
        master = self.unit(extension='jpg', surrogate=True, record_key='md:7')
        photo = self.picture(self.source / 'original.tif')
        self.relate(photo, master)
        result, app = self.run_prepare()
        self.assertEqual(result['counts'], {'tiff_for_jpeg_review':1})
        snap = app.snapshot(next(iter(app.objects)))
        self.assertTrue(snap['scope']['tiff_recovery_pending'])
        self.assertFalse(snap['scope']['closed'])
        self.assertEqual(snap['selection_context']['action'], 'retain_archive_master')

    def test_known_md_assignment_skips_image_search_without_claiming_pixel_identity(self):
        self.args.archive_masters_fixed = True
        master = self.unit(record_key='md:7')
        source = self.picture(self.source / 'known.tif', size=(32,21))
        old = self.root / 'legacy'; old.mkdir()
        (old/'tiff-scan-summary.json').write_text(json.dumps({'unit':str(self.units[0])}))
        (old/'tiff-scan-masters.jsonl').write_text(json.dumps({'record_key':'md:7','master_path':str(master),
            'master_stamp':stamp(master)})+'\n')
        common={'source_path':str(source),'source_stamp':stamp(source)}
        (old/'tiff-scan-results.jsonl').write_text(json.dumps({**common,'finding':'known_object_image_gap',
            'orientation_matches':[{'record_key':'md:7','passes_orientation_screen':False}]})+'\n')
        (old/'tiff-review-decisions.jsonl').write_text(json.dumps({**common,'action':'assign_source',
            'object_record_key':'md:7','metadata_assignment_confirmed':True})+'\n')
        self.args.prior_review=[old]
        result, app = self.run_prepare()
        self.assertEqual(result['counts'], {'existing_md_object':1})
        snap=app.snapshot(next(iter(app.objects)))
        self.assertTrue(snap['scope']['closed'])
        self.assertTrue(snap['read_only'])
        self.assertFalse(snap['archive_resolved'])
        self.assertEqual(snap['decision']['same_pairs'], [])
        self.assertEqual(snap['object']['matches'], [])
        self.args.prior_review=[self.args.work_root]
        result, reopened=self.run_prepare('reuse-known-md')
        reused=reopened.snapshot(next(iter(reopened.objects)))
        self.assertEqual(result['counts'], {'existing_md_object':1})
        self.assertTrue(reused['scope']['closed'])
        self.assertEqual(reused['decision']['same_pairs'], [])

    def test_legacy_md_binding_does_not_require_a_current_master(self):
        source=self.picture(self.source/'helicopter.tif')
        legacy=self.root/'legacy-alias';legacy.mkdir()
        (legacy/'tiff-scan-summary.json').write_text(json.dumps({'unit':'unit'}))
        (legacy/'tiff-scan-masters.jsonl').write_text('')
        (legacy/'tiff-scan-results.jsonl').write_text('')
        record={'source_path':str(source),'source_stamp':stamp(source),'action':'assign_source',
                'object_record_key':'md:101385','metadata_key':'md:101385','master_key':'md:101436',
                'metadata_assignment_confirmed':True}
        (legacy/'tiff-review-decisions.jsonl').write_text(json.dumps(record)+'\n')
        evidence=Evidence([{'path':str(source),'stamp':stamp(source)}],[])
        evidence.import_legacy_avantmedia(legacy)
        self.assertEqual(assigned_md_id(list(evidence.proposals.values())),'unit#md:101385')
        self.assertFalse(evidence.confirmed)

    def test_genuine_jpeg_upgrades_manufactured_tiff_surrogate(self):
        surrogate = self.unit(surrogate=True)
        jpeg = self.picture(self.source / 'original.jpg')
        self.relate(jpeg, surrogate)
        result, _ = self.run_prepare()
        self.assertEqual(result['counts'], {'master_upgrade': 1})

    def test_largest_confirmed_jpeg_new_proposal_needs_explicit_destination(self):
        self.unit()
        low = self.picture(self.source / 'low.jpg', (20, 15))
        high = self.picture(self.source / 'high.jpg', (80, 60))
        self.relate(low, high)
        result, app = self.run_prepare()
        self.assertEqual(result['counts'], {'search_coverage_review': 1})
        oid = next(iter(app.objects)); obj = app.objects[oid]
        selected = obj['recommended_candidate_id']
        self.assertEqual(app.candidates[oid][selected]['members'][0]['path'], str(high))
        other = next(c['id'] for c in obj['candidates'] if c['id'] != selected)
        self.save(app, oid, selected_candidate_id=selected, redundant_candidate_ids=[other])
        self.assertTrue(app.snapshot()['objects'][0]['destination_pending'])
        self.save(app, oid, destination_unit=str(self.units[0]))
        self.assertFalse(app.snapshot()['objects'][0]['destination_pending'])
        self.assertEqual(app.snapshot(oid)['selection_context']['action'], 'proposed_new_object')

    def test_false_perceptual_lead_and_same_filename_never_confirm_identity(self):
        master = self.unit()
        Image.new('RGB', (80, 60), (200, 30, 60)).save(self.source / master.name)
        with patch('search.visual_matches', return_value=[{'target_id': str(self.units[0]) + '#same-key',
                  'engine': 'cached_hash', 'phash_distance': 0}]):
            result, app = self.run_prepare()
        self.assertEqual(result['counts'], {'similarity_review': 1})
        snap = app.snapshot(next(iter(app.objects)))
        self.assertEqual(snap['decision']['selected_candidate_id'], '')
        self.assertFalse(snap['decision']['redundant_candidate_ids'])

    def test_human_same_photo_confirmation_turns_source_selection_into_upgrade(self):
        self.unit(extension='jpg')
        self.picture(self.source / 'original.tif')
        tid = str(self.units[0]) + '#same-key'
        with patch('search.visual_matches', return_value=[{'target_id': tid, 'engine': 'cached_hash', 'phash_distance': 1}]):
            _, app = self.run_prepare()
        oid = next(iter(app.objects))
        self.save(app, oid, selected_candidate_id='source', same_pairs=[['source', 'archive:' + tid]],
                  destination_unit=str(self.units[0]))
        self.assertEqual(app.snapshot(oid)['selection_context']['archive_object_id'], tid)
        self.assertEqual(app.snapshot(oid)['selection_context']['action'], 'proposed_master_upgrade')
        with self.assertRaisesRegex(ReviewError, 'different photos|Conflicting same-photo'):
            self.save(app, oid, different_pairs=[['source', 'archive:' + tid]])

    def test_legacy_proposals_do_not_invent_same_photo_or_retained_master(self):
        master = self.unit()
        targets, _ = read_unit(self.specs[0], EmptyIndex())
        old = self.root / 'legacy'; old.mkdir()
        (old / 'tiff-scan-summary.json').write_text(json.dumps({'unit': str(self.units[0])}))
        (old / 'tiff-scan-masters.jsonl').write_text(json.dumps({'record_key': 'same-key', 'master_path': str(master),
            'master_stamp': stamp(master), 'stored_final_sha256': targets[0]['sha256']}) + '\n')
        sources, findings, decisions = [], [], []
        for action in ('new_object', 'assign_source', 'redundant'):
            photo = self.picture(self.source / (action + '.tif'))
            source = {'path': str(photo), 'stamp': stamp(photo)}; sources.append(source)
            common = {'source_path': str(photo), 'source_stamp': stamp(photo)}
            findings.append({**common, 'finding': 'existing_kurt_image_match',
                             'strong_matches': [{'record_key': 'same-key', 'distance': 0}],
                             'orientation_matches': [{'record_key':'same-key','passes_orientation_screen':False}]})
            decisions.append({**common, 'action': action, 'object_record_key': 'same-key'})
        for filename, rows in [('tiff-scan-results.jsonl', findings), ('tiff-review-decisions.jsonl', decisions)]:
            (old / filename).write_text(''.join(json.dumps(r) + '\n' for r in rows))
        e = Evidence(sources, targets); e.import_review(old)
        self.assertFalse(e.confirmed)
        self.assertEqual(len(e.proposals), 2)
        self.assertEqual(e.proposals[str(self.source / 'assign_source.tif')]['assigned_object_id'], targets[0]['id'])
        self.assertTrue(e.issues[str(self.source / 'redundant.tif')])
        self.assertEqual(sum(len(v) for v in e.leads.values()), 3)
        self.assertEqual(sum(bool(x.get('previous_comparison')) for records in e.context.values() for x in records),3)

    def test_stale_confirmation_ignored_and_contradictions_quarantined(self):
        master = self.unit()
        photo = self.picture(self.source / 'photo.jpg')
        self.relate(photo, master, stale=True)
        result, _ = self.run_prepare()
        self.assertEqual(result['counts'], {'search_coverage_review': 1})
        self.assertEqual(result['prior_evidence']['stale_or_out_of_scope_relationships'], 1)
        self.relate(photo, master)
        self.relate(photo, master, 'different_photo')
        result, app = self.run_prepare('conflict')
        self.assertEqual(result['counts'], {'evidence_conflict': 1})
        self.assertFalse(app.snapshot(next(iter(app.objects)))['decision']['selected_candidate_id'])

    def test_metadata_question_independent_and_saved_answer_reusable(self):
        master = self.unit()
        photo = self.picture(self.source / 'photo.tif', caption='Unique source caption')
        self.relate(photo, master, 'covered_by')
        result, app = self.run_prepare()
        self.assertEqual(result['counts'], {'metadata_review': 1})
        oid = next(iter(app.objects))
        self.save(app, oid, metadata_reviewed=True, note='Caption retained in review evidence; no embedding.')
        previous = self.args.work_root
        self.args.prior_review = [previous]
        self.args.relations = []
        result, app = self.run_prepare('second-review')
        self.assertEqual(result['counts'], {'redundant': 1})
        self.assertFalse(app.objects)

    def test_catalogued_resized_derivative_exact_copy_covered(self):
        master = self.unit()
        access = self.picture(master.parent / 'access.jpg', (20, 15))
        spec = self.specs[0]; doc = json.loads(spec.read_text())
        doc['objects'][0]['derivatives'] = [{'path': 'access.jpg', 'sha256': hashlib.sha256(access.read_bytes()).hexdigest()}]
        spec.write_text(json.dumps(doc))
        shutil.copyfile(access, self.source / 'resized.jpg')
        self.args.hash_missing = True
        result, _ = self.run_prepare()
        self.assertEqual(result['counts'], {'redundant': 1})

    def test_unchanged_pixel_provenance_reuses_original_digest(self):
        master = self.unit()
        photo = self.picture(self.source / 'original.tif', caption='Before metadata embedding')
        spec = self.specs[0]; doc = json.loads(spec.read_text())
        doc['objects'][0]['master'].update(original_source_path=str(photo),
            source_sha256=hashlib.sha256(photo.read_bytes()).hexdigest(), basis='pixels_and_structure_unchanged')
        spec.write_text(json.dumps(doc)); self.args.hash_missing = True
        result, _ = self.run_prepare()
        self.assertEqual(result['counts'], {'metadata_review': 1})
        row = json.loads((self.args.work_root / 'source-dispositions.jsonl').read_text())
        self.assertEqual(row['disposition'], 'redundant')

    def test_recommendations_in_otherwise_saved_file_are_not_human_decisions(self):
        master = self.unit(extension='jpg')
        photo = self.picture(self.source / 'original.tif')
        self.relate(photo, master)
        _, app = self.run_prepare()
        rows, _ = app.read_decisions()
        path = self.root / 'unsaved-recommendations.jsonl'
        path.write_text('\n'.join(json.dumps(row) for row in rows.values()))
        targets, _ = read_unit(self.specs[0], EmptyIndex())
        evidence = Evidence([{'path': str(photo), 'stamp': stamp(photo)}], targets)
        evidence.import_workbench(path)
        self.assertFalse(evidence.confirmed)
        self.assertFalse(evidence.proposals)

    def test_unknown_layout_multiple_members_and_output_overlap_fail(self):
        master = self.unit()
        self.picture(self.source / 'photo.jpg')
        self.args.work_root = self.source / 'review'
        with self.assertRaisesRegex(ReviewError, 'overlaps'):
            self.run_prepare()
        self.assertFalse(self.args.work_root.exists())
        spec = self.specs[0]; doc = json.loads(spec.read_text())
        doc['objects'].append(doc['objects'][0])
        spec.write_text(json.dumps(doc))
        with self.assertRaisesRegex(ReviewError, 'ordered-representation'):
            read_unit(spec, EmptyIndex())

    def test_wrong_archive_digest_fails_before_publication(self):
        master = self.unit()
        other = self.picture(self.source / 'incoming.tif', (60, 80))
        spec = self.specs[0]; doc = json.loads(spec.read_text())
        doc['objects'][0]['master']['sha256'] = hashlib.sha256(other.read_bytes()).hexdigest()
        spec.write_text(json.dumps(doc)); self.args.hash_missing = True
        with self.assertRaisesRegex(ReviewError, 'hash disagrees'):
            self.run_prepare()
        self.assertFalse((self.args.work_root / 'workbench-input.json').exists())


if __name__ == '__main__':
    unittest.main()
