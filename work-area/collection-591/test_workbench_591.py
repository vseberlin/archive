import csv,io,json,unittest
from unittest.mock import patch
import workbench_591 as wb

class ReviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.review=wb.Review()

    def test_saved_master_is_one_complete_representation_and_others_redundant(self):
        oid='66129';before=self.review.decisions();writes={}
        with patch.object(wb,'atomic',side_effect=lambda p,b:writes.__setitem__(p.name,b)):
            self.review.save(oid,'md-'+oid,'Test only; writes intercepted')
        decisions=list(csv.DictReader(io.StringIO(writes['master-decisions.csv'].decode())))
        dispositions=list(csv.DictReader(io.StringIO(writes['candidate-dispositions.csv'].decode())))
        self.assertEqual(['md-'+oid],[r['candidate_id'] for r in dispositions if r['md_object_id']==oid and r['disposition']=='selected_master'])
        self.assertTrue(all(r['disposition']=='redundant' for r in dispositions if r['md_object_id']==oid and r['candidate_id']!='md-'+oid))
        self.assertTrue(all(r['deletion_authorized']=='false' for r in decisions+dispositions))
        self.assertEqual(before,self.review.decisions())

    def test_cross_object_or_ineligible_choice_cannot_write(self):
        with patch.object(wb,'atomic') as write:
            with self.assertRaises(AssertionError):self.review.save('66129','md-60895','')
            for c in self.review.candidates:
                if c['eligible']=='false':
                    with self.assertRaises(AssertionError):self.review.save(c['md_object_id'],c['candidate_id'],'')
            write.assert_not_called()

    def test_unresolved_does_not_create_master_or_deletion_authority(self):
        writes={}
        with patch.object(wb,'atomic',side_effect=lambda p,b:writes.__setitem__(p.name,b)):
            self.review.save('66129','','Still comparing')
        rows=list(csv.DictReader(io.StringIO(writes['candidate-dispositions.csv'].decode())))
        self.assertTrue(all(r['disposition']=='unresolved' for r in rows if r['md_object_id']=='66129'))

    def test_complete_md_resource_membership_preserved(self):
        for c in self.review.candidates:
            if c['origin']=='museum_digital':
                members=json.loads(c['members_json'])
                rr=sorted([r for r in self.review.resources if r['md_object_id']==c['md_object_id']],key=lambda r:int(r['sequence']))
                self.assertEqual([r['resource_id'] for r in rr],[m['resource_id'] for m in members])

    def test_preview_refuses_unknown_paths_out_of_range_and_changed_sources(self):
        with patch.object(wb,'render_pdf') as render:
            for key,number in [('file:/etc/passwd',1),('resource:96227',0),('resource:96227',8)]:
                with self.assertRaises((AssertionError,ValueError)):
                    self.review.preview(key,number)
            item=self.review.preview_source('resource:96227');item['size']+=1
            with patch.object(self.review,'preview_source',return_value=item):
                with self.assertRaises(AssertionError):self.review.preview('resource:96227',1)
            render.assert_not_called()

    def test_preview_guard_is_checked_even_for_cached_page(self):
        with patch.object(wb,'guard',side_effect=AssertionError('Mount unavailable')),patch.object(wb,'render_pdf') as render:
            with self.assertRaises(AssertionError):self.review.preview('resource:96227',1)
            render.assert_not_called()

    def test_text_difference_escapes_evidence_and_reports_unequal_text(self):
        rendered=wb.text_difference('same\n<script>old</script>','same\n<img src=x onerror=alert(1)>')
        self.assertIn('class="changed"',rendered)
        self.assertNotIn('<script>',rendered);self.assertNotIn('<img',rendered)
        self.assertIn('&lt;script&gt;',rendered)
        self.assertIn('No differences',wb.text_difference('same','same'))

    def test_form_button_selects_master_and_advances_without_real_writes(self):
        from types import SimpleNamespace
        from urllib.parse import urlencode
        payload=urlencode({'object_id':'66129','candidate_id':'md-66129','note':'HTTP form test; intercepted'}).encode()
        handler=wb.Handler.__new__(wb.Handler);handler.path='/save';handler.headers={'Content-Length':str(len(payload))};handler.rfile=io.BytesIO(payload);handler.server=SimpleNamespace(review=self.review)
        writes={};headers={};statuses=[]
        handler.send_response=statuses.append;handler.send_header=lambda k,v:headers.__setitem__(k,v);handler.end_headers=lambda:None
        with patch.object(wb,'atomic',side_effect=lambda p,b:writes.__setitem__(p.name,b)):handler.do_POST()
        self.assertEqual([303],statuses);self.assertTrue(headers['Location'].startswith('/object?id='))
        self.assertNotIn('/object?id=66129',headers['Location'])
        rows=list(csv.DictReader(io.StringIO(writes['candidate-dispositions.csv'].decode())))
        self.assertEqual(1,sum(r['md_object_id']=='66129' and r['disposition']=='selected_master' for r in rows))

if __name__=='__main__':unittest.main()
