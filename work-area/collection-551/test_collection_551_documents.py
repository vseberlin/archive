"""Bounded documentation review checks; all decision writes are intercepted."""
import csv
import copy
import io
import json
import unittest
from pathlib import Path
from unittest import mock

import collection_551_workbench as core
from collection_551_documents import Documents, url


class DocumentsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.docs = Documents(core.Workbench('Documentation test'))

    def setUp(self):
        patcher = mock.patch.object(core, 'accession_receipt', return_value={})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.docs.reload()

    def test_accession_blocks_document_changes_without_writes(self):
        with mock.patch.object(core, 'accession_receipt', return_value={'accession_performed': True}), mock.patch.object(core, 'atomic_replace') as write:
            with self.assertRaisesRegex(core.WorkbenchError, 'accessioned'):
                self.docs.save(self.docs.rows[0]['document_id'], 'needs_review', '', '')
            write.assert_not_called()

    def test_sources_are_grouped_by_stored_content_hash(self):
        originals = [r for r in self.docs.rows if r['source_path'].startswith(str(core.SOURCE_ROOT)+'/')]
        self.assertEqual(94, len(originals))
        self.assertEqual(56, len({r['content_sha256'] for r in originals}))
        for group in self.docs.groups.values():
            self.assertLessEqual(sum(r['decision'] == 'included' for r in group), 1)
            self.assertTrue(all(r['deletion_authorized'] == 'false' for r in group))

    def test_original_file_url_encodes_source_parameter(self):
        self.assertEqual('/file?path=a+b%26c', url('/file', path='a b&c'))

    def test_pending_view_hides_final_choices_but_preserves_proposals_and_unresolved(self):
        docs = copy.copy(self.docs)
        docs.groups = copy.deepcopy(docs.groups)
        docs.rows = [r for group in docs.groups.values() for r in group]
        for r in docs.rows:
            r.update(decision='redundant', decided_at='2026-09-09T00:00:00Z', review_note='')
        groups = list(docs.groups.values())
        proposal, unresolved, noise = groups[:3]
        proposal[0].update(decision='included', decided_at='')
        unresolved[0].update(decision='needs_review')
        for r in noise:
            r['review_note'] = 'Noise: damaged file.'
        expected = {proposal[0]['content_sha256'], unresolved[0]['content_sha256']}
        self.assertEqual(expected, {r['content_sha256'] for r in docs.visible_items()})
        self.assertEqual(len(groups), len(docs.visible_items(status='all')))
        self.assertEqual([noise[0]['content_sha256']], [r['content_sha256'] for r in docs.visible_items(status='Noise')])
        for group in (proposal, unresolved):
            for r in group:
                r.update(decision='redundant', decided_at='2026-09-09T00:00:00Z')
        with mock.patch.object(docs, 'reload'):
            self.assertIn('No documents to review', docs.index())
            self.assertNotIn('class="panel doc-card"', docs.index())

    def test_unresolved_readable_document_is_not_described_as_damaged(self):
        row = next(r for r in self.docs.rows if r['condition'] == 'readable')
        row = dict(row, decision='needs_review')
        self.assertNotIn('Damaged candidate', self.docs.short(row))

    def test_signed_page_compares_with_full_application_page_fifteen(self):
        row = next(r for r in self.docs.items() if r['content_sha256'].startswith('c93eba333b4b'))
        body = self.docs.detail(row['document_id'])
        self.assertIn('page 15', body)
        self.assertIn('name="compare_page" value="15"', body)
        self.assertIn('Partial evidence only', body)

    def test_choice_changes_only_identical_copies_and_invalidates_preparation_first(self):
        docs = self.docs
        group = next(g for g in docs.groups.values() if len(g) > 1 and any(r['decision'] == 'included' for r in g))
        selected = next(r for r in group if r['decision'] != 'included')
        before = {r['document_id']: dict(r) for r in docs.rows}
        with mock.patch.object(core, 'source_guard'), mock.patch.object(docs, 'source'), mock.patch.object(core, 'atomic_replace') as write:
            docs.save(group[0]['document_id'], 'included', selected['document_id'], 'Retain this identical source copy.')
        self.assertEqual(2, write.call_count)
        self.assertEqual(docs.root / 'accession-preparation.json', write.call_args_list[0].args[0])
        self.assertFalse(json.loads(write.call_args_list[0].args[1])['preparation_complete'])
        self.assertEqual(docs.csv_path, write.call_args_list[1].args[0])
        rows = list(csv.DictReader(io.StringIO(write.call_args_list[1].args[1].decode())))
        changed = [r for r in rows if r['content_sha256'] == selected['content_sha256']]
        self.assertEqual([selected['document_id']], [r['document_id'] for r in changed if r['decision'] == 'included'])
        for r in rows:
            self.assertEqual('false', r['deletion_authorized'])
            if r['content_sha256'] != selected['content_sha256']:
                self.assertEqual(before[r['document_id']], {k:r[k] for k in before[r['document_id']]})

    def test_foreign_copy_and_unsupported_actions_cannot_write(self):
        docs = self.docs
        first, second = docs.items()[:2]
        with mock.patch.object(core, 'source_guard'), mock.patch.object(core, 'atomic_replace') as write:
            with self.assertRaises(ValueError):
                docs.save(first['document_id'], 'included', second['document_id'], '')
            with self.assertRaises(ValueError):
                docs.save(first['document_id'], 'delete', first['document_id'], '')
            with self.assertRaises(ValueError):
                docs.save('unknown', 'needs_review', '', '')
            write.assert_not_called()

    def test_unrelated_save_excludes_all_copies_from_both_drain_outputs_and_survives_reprojection(self):
        docs = self.docs
        drain_paths = {r['source_path'] for r in docs.workbench.drain_base}
        group = next(g for g in docs.groups.values() if any(r['source_path'] in drain_paths for r in g))
        identifier = group[0]['document_id']
        group_paths = {r['source_path'] for r in group}
        before = {r['document_id']: dict(r) for r in docs.rows}
        files = {}
        original_read = core.read_csv
        original_bytes, original_exists = Path.read_bytes, Path.exists

        def read_csv(path):
            if path in files:
                return list(csv.DictReader(io.StringIO(files[path].decode())))
            return original_read(path)

        def read_bytes(path):
            return files[path] if path in files else original_bytes(path)

        def exists(path):
            return path in files or original_exists(path)

        with mock.patch.object(core, 'source_guard'), mock.patch.object(docs, 'source'), \
             mock.patch.object(core, 'read_csv', side_effect=read_csv), \
             mock.patch.object(core, 'atomic_replace', side_effect=lambda p,b: files.__setitem__(p,b)), \
             mock.patch.object(Path, 'read_bytes', read_bytes), mock.patch.object(Path, 'exists', exists):
            docs.save(identifier, 'unrelated', '', 'Overall Industriesalon inventory.')
            updated = read_csv(docs.csv_path)
            for row in updated:
                if row['source_path'] in group_paths:
                    self.assertTrue(core.document_is_unrelated(row))
                    self.assertEqual('false', row['deletion_authorized'])
                    self.assertFalse(row['proposed_relative_path'])
                else:
                    self.assertEqual(before[row['document_id']], row)
            for path in [core.DRAIN_CSV, docs.root/'accession-source-disposition.csv']:
                self.assertFalse(group_paths & {r['source_path'] for r in read_csv(path)})
            self.assertEqual(docs.workbench.drain_base, read_csv(core.DRAIN_EVIDENCE_CSV))
            self.assertFalse(group_paths & {r['source_path'] for r in docs.workbench.drain_for(docs.workbench.decisions())})
            self.assertNotIn(identifier, {r['document_id'] for r in docs.visible_items()})
            self.assertEqual('Unrelated', docs.state(docs.get(identifier)))
            self.assertIn('value="unrelated" checked', docs.detail(identifier))
            # Reopening a mistaken exclusion restores the original active drain rows.
            docs.save(identifier, 'needs_review', '', 'Reopened for review.')
            expected = (drain_paths | {r['source_path'] for r in core.noise_document_rows()}) - core.unrelated_document_paths()
            self.assertEqual(expected, {r['source_path'] for r in read_csv(core.DRAIN_CSV)})
            self.assertIn(identifier, {r['document_id'] for r in docs.visible_items()})
            self.assertTrue(all(p in {docs.csv_path, core.DRAIN_CSV, core.DRAIN_EVIDENCE_CSV,
                                    docs.root/'accession-source-disposition.csv', docs.root/'accession-preparation.json'} for p in files))

    def test_noise_is_in_active_drain_without_claiming_coverage_or_deletion(self):
        drain = {r['source_path']:r for r in self.docs.workbench.drain_for(self.docs.workbench.decisions())}
        for doc in core.noise_document_rows():
            row = drain[doc['source_path']]
            self.assertEqual('documentation_noise', row['classification'])
            self.assertEqual('false', row['covered_by_selected_master'])
            self.assertEqual('false', row['deletion_authorized'])
            self.assertEqual(doc['review_note'], row['note'])

    def test_final_document_is_pinned_and_sources_remain_accessible(self):
        finals = [r for r in self.docs.items() if self.docs.state(r)=='Final document']
        self.assertEqual(1, len(finals))
        final = finals[0]
        self.assertEqual(self.docs.root/'FEB-Import-konsolidiert.csv', self.docs.source(final))
        body = self.docs.index()
        self.assertEqual(1, body.count('class="panel final-document"'))
        self.assertIn('Preview final document', body)
        drain_paths={r['source_path'] for r in self.docs.workbench.drain_for(self.docs.workbench.decisions())}
        self.assertNotIn(final['source_path'],drain_paths)


if __name__ == '__main__':
    unittest.main()
