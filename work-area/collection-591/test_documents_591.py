import copy,csv,io,unittest
from unittest.mock import patch
import documents_591 as docs
import workbench_591 as core

class DocumentationTests(unittest.TestCase):
    def test_one_original_in_duplicate_group_and_other_copies_redundant(self):
        group=next(g for g in docs.grouped().values() if len(g)>1);writes=[]
        with patch.object(core,'atomic',side_effect=lambda p,b:writes.append((p,b))):docs.save(core,group[0]['document_id'],'included',group[-1]['source_path'],'Test only')
        rows=list(csv.DictReader(io.StringIO(writes[0][1].decode())));changed=[r for r in rows if r['content_sha256']==group[0]['content_sha256']]
        self.assertEqual(1,sum(r['decision']=='included' for r in changed));self.assertTrue(all(r['deletion_authorized']=='false' for r in changed))
        self.assertEqual(group[-1]['source_path'],next(r['source_path'] for r in changed if r['decision']=='included'))

    def test_unrelated_and_noise_hide_without_source_writes(self):
        group=next(iter(docs.grouped().values()))
        for action,decision,prefix in [('unrelated','excluded','Unrelated to Collection 591:'),('redundant','redundant','Noise:')]:
            writes=[]
            with patch.object(core,'atomic',side_effect=lambda p,b:writes.append((p,b))):docs.save(core,group[0]['document_id'],action,'','Test only')
            self.assertEqual(core.ROOT/'documentation-review.csv',writes[0][0])
            rows=list(csv.DictReader(io.StringIO(writes[0][1].decode())));changed=[r for r in rows if r['content_sha256']==group[0]['content_sha256']]
            self.assertTrue(docs.resolved(changed));self.assertTrue(all(r['decision']==decision and r['review_note'].startswith(prefix) for r in changed))

    def test_cross_group_source_cannot_be_included(self):
        group=next(iter(docs.grouped().values()))
        with patch.object(core,'atomic') as write:
            with self.assertRaises(AssertionError):docs.save(core,group[0]['document_id'],'included','/etc/passwd','')
            write.assert_not_called()

    def test_double_original_is_rejected(self):
        group=copy.deepcopy(next(g for g in docs.grouped().values() if len(g)>1))
        for r in group:r['decision']='included'
        with patch.object(docs,'grouped',return_value={'test':group}):
            with self.assertRaises(AssertionError):docs.status()

    def test_byte_distinct_inventories_remain_separate_with_comparison(self):
        inventories=[g for g in docs.grouped().values() if core.Path(g[0]['source_path']).name.startswith('Archiv Bücher und Zeitungen')]
        self.assertGreater(len(inventories),1)
        row=inventories[0][0]
        self.assertTrue(all(g[0]['content_sha256']!=row['content_sha256'] for g in docs.related_groups(row)))
        rendered=docs.detail(core,core.Review(),{'id':[row['document_id']],'view':['broader']}).decode()
        self.assertIn('Differences between these versions',rendered)
        self.assertIn('Review the comparison document separately',rendered)

    def test_next_document_keeps_scope_and_finishes_empty_view(self):
        groups=copy.deepcopy(docs.grouped());first=docs.visible_groups('aeb')[0][0]
        for group in groups.values():
            for r in group:
                if r['relationship'] not in {'broader','unrelated'}:r.update(decision='excluded',decided_at='test')
        with patch.object(docs,'grouped',return_value=groups):
            location=docs.next_location(core,first['document_id'],'aeb')
            self.assertTrue(location.startswith('/documents?view=aeb'))
            self.assertEqual([],docs.visible_groups('aeb'))
            self.assertTrue(docs.visible_groups('broader'))

    def test_later_skips_current_without_resolving_it(self):
        first=docs.visible_groups('aeb')[0][0];before=copy.deepcopy(docs.grouped())
        location=docs.next_location(core,first['document_id'],'aeb')
        self.assertTrue(location.startswith('/document?'))
        self.assertNotIn('id='+first['document_id']+'&',location)
        self.assertEqual(before,docs.grouped())

    def test_not_relevant_form_preserves_sources_and_advances_within_view(self):
        from urllib.parse import urlencode
        row=docs.visible_groups('broader')[0][0]
        payload=urlencode({'id':row['document_id'],'action':'unrelated','source':'','note':'Form test; intercepted','view':'broader'}).encode()
        handler=core.Handler.__new__(core.Handler);handler.path='/document-save';handler.headers={'Content-Length':str(len(payload))};handler.rfile=io.BytesIO(payload)
        writes={};headers={};statuses=[]
        handler.send_response=statuses.append;handler.send_header=lambda k,v:headers.__setitem__(k,v);handler.end_headers=lambda:None
        with patch.object(core,'atomic',side_effect=lambda p,b:writes.__setitem__(p.name,b)):handler.do_POST()
        self.assertEqual([303],statuses);self.assertIn('view=broader',headers['Location'])
        rows=list(csv.DictReader(io.StringIO(writes['documentation-review.csv'].decode())))
        changed=[r for r in rows if r['content_sha256']==row['content_sha256']]
        self.assertTrue(all(r['decision']=='excluded' and r['deletion_authorized']=='false' for r in changed))

if __name__=='__main__':unittest.main()
