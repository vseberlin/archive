"""Offline transport bounds and rejection of fabricated/uncited model references."""
import copy
import hashlib
import json
import unittest
from unittest.mock import Mock,MagicMock
from archive_research.assessment_http import submit_once,NoRedirect

import test_research_evidence as fixtures
from archive_research.assessment_io import prepare_request,validate_response
from archive_research.contracts import encoded
from research_routes import research_profiles


class AssessmentIOTests(unittest.TestCase):
    def setUp(self):
        self.fixture=fixtures.EvidenceTests();self.fixture.setUp();self.addCleanup(self.fixture.doCleanups)
        self.packet=self.fixture.packet()
        self.request,self.receipt=prepare_request(self.fixture.roots,'artifacts',self.packet,
                                  research_profiles(self.fixture.selection['profiles']),model='test-model')
        self.value={'observations':[{'claim':'Invented text','evidence':[1],'basis':'literal-text','uncertainty':'Synthetic only'}],
                    'hypotheses':[{'claim':'Tentative association','supports':[0,1],'contradicts':[],'uncertainty':'Not established'}],
                    'unresolved':['Source attribution'],'next_local_search':'Look for another source.'}

    def response(self,value=None):
        return {'id':'synthetic-response','model':'test-model','status':'completed','usage':{'input_tokens':10},
                'output':[{'type':'message','role':'assistant','content':[{'type':'output_text','text':json.dumps(value or self.value)}]}]}

    def test_preparation_is_bounded_and_omits_filesystem_paths(self):
        self.assertFalse(self.request['store']);self.assertFalse(self.receipt['external_submission_authorized'])
        self.assertEqual(len(self.receipt['attachments']),2)
        self.assertNotIn(str(self.fixture.base),encoded(self.request))
        self.assertNotIn('original_path',encoded(self.request))
        self.assertEqual(self.receipt['request_sha256'],hashlib.sha256(encoded(self.request).encode()).hexdigest())
        self.assertLess(self.receipt['image_bytes'],100000)

    def test_edited_derived_packet_text_is_rebuilt_from_the_receipt(self):
        packet=copy.deepcopy(self.packet);packet['evidence'][0]['text']='fabricated derived reading'
        request,_=prepare_request(self.fixture.roots,'artifacts',packet,research_profiles(self.fixture.selection['profiles']),model='test-model')
        self.assertNotIn('fabricated derived reading',encoded(request))

    def test_explicit_image_omission_retains_only_labelled_text_context(self):
        request,receipt=prepare_request(self.fixture.roots,'artifacts',self.packet,research_profiles(self.fixture.selection['profiles']),model='test-model',attach_evidence=[0])
        self.assertEqual(receipt['omitted_image_evidence'],[1])
        content=request['input'][0]['content']
        self.assertEqual(sum(p['type']=='input_image' for p in content),1)
        self.assertFalse(json.loads(content[0]['text'])['evidence'][1]['image_attached'])

    def test_valid_citations_remain_unreviewed(self):
        value=validate_response(self.response(),self.packet,self.receipt)
        self.assertFalse(value['metadata_adopted']);self.assertIn('unreviewed',value['status'])

    def test_unknown_duplicate_boolean_or_missing_references_rejected(self):
        for refs in ([99],[True],[0,0],[]):
            v=copy.deepcopy(self.value);v['observations'][0]['evidence']=refs
            with self.subTest(refs=refs),self.assertRaises(ValueError):
                validate_response(self.response(v),self.packet,self.receipt)

    def test_incomplete_refusal_tool_use_wrong_model_and_changed_packet_rejected(self):
        for update in ({'status':'incomplete'},{'model':'other'},
                       {'output':[{'type':'web_search_call'}]},
                       {'output':[{'type':'message','role':'assistant','content':[{'type':'refusal'}]}]}):
            response=self.response();response.update(update)
            with self.assertRaises(ValueError):validate_response(response,self.packet,self.receipt)
        packet=copy.deepcopy(self.packet);packet['question']='Changed'
        with self.assertRaises(ValueError):validate_response(self.response(),packet,self.receipt)

    def test_submit_requires_exact_request_and_sends_once_to_fixed_endpoint(self):
        opener=MagicMock();response=Mock();response.read.return_value=json.dumps(self.response()).encode()
        opener.open.return_value.__enter__.return_value=response
        with self.assertRaises(ValueError):submit_once(self.request,self.receipt,authorized_request_sha256='0'*64,api_key='synthetic-key',opener=opener)
        opener.open.assert_not_called()
        result=submit_once(self.request,self.receipt,authorized_request_sha256=self.receipt['request_sha256'],api_key='synthetic-key',opener=opener)
        self.assertEqual(result['id'],'synthetic-response');opener.open.assert_called_once()
        self.assertEqual(opener.open.call_args.args[0].full_url,'https://api.openai.com/v1/responses')
        with self.assertRaises(ValueError):NoRedirect().redirect_request(None,None,302,'',{},'https://elsewhere.invalid')

    def test_network_failure_is_not_retried(self):
        opener=Mock();opener.open.side_effect=TimeoutError()
        with self.assertRaises(TimeoutError):submit_once(self.request,self.receipt,authorized_request_sha256=self.receipt['request_sha256'],api_key='synthetic-key',opener=opener)
        opener.open.assert_called_once()

    def test_claims_require_uncertainty_and_known_schema(self):
        v=copy.deepcopy(self.value);v['observations'][0]['uncertainty']=''
        with self.assertRaises(ValueError):validate_response(self.response(v),self.packet,self.receipt)
        v=copy.deepcopy(self.value);v['adopt_metadata']=True
        with self.assertRaises(ValueError):validate_response(self.response(v),self.packet,self.receipt)

    def codex_response(self):
        return {'format':'archive-research-codex-response-1','provider':'codex-chatgpt',
                'model':'test-model','status':'completed','request_sha256':self.receipt['request_sha256'],
                'assessment':self.value,'usage':{'input_tokens':10},
                'events':[{'type':'item.completed','item':{'type':'agent_message','text':json.dumps(self.value)}},
                          {'type':'turn.completed','usage':{'input_tokens':10}}]}

    def test_codex_result_keeps_provider_and_proposal_status(self):
        value=validate_response(self.codex_response(),self.packet,self.receipt)
        self.assertEqual(value['provider'],'codex-chatgpt')
        self.assertFalse(value['metadata_adopted'])

    def test_codex_disabled_host_notice_allowed_only_before_turn(self):
        from archive_research.assessment_codex import DISABLED_HOST_NOTICE
        response=self.codex_response()
        notice={'type':'item.completed','item':{'type':'error','message':DISABLED_HOST_NOTICE}}
        response['events'].insert(0,notice)
        validate_response(response,self.packet,self.receipt)
        response['events'].insert(0,{'type':'turn.started'})
        with self.assertRaises(ValueError):validate_response(response,self.packet,self.receipt)
        response['events'].pop(0);notice['item']['message']='Unexpected error'
        with self.assertRaises(ValueError):validate_response(response,self.packet,self.receipt)

    def test_codex_tool_calls_incomplete_turns_and_mismatches_are_rejected(self):
        for alteration in ('tool','incomplete','changed_request','changed_result','usage'):
            response=copy.deepcopy(self.codex_response())
            if alteration=='tool':response['events'].insert(0,{'type':'item.started','item':{'type':'command_execution'}})
            if alteration=='incomplete':response['events'].pop()
            if alteration=='changed_request':response['request_sha256']='0'*64
            if alteration=='changed_result':response['assessment']['next_local_search']='altered'
            if alteration=='usage':response['usage']['input_tokens']=11
            with self.subTest(alteration=alteration),self.assertRaises(ValueError):
                validate_response(response,self.packet,self.receipt)

    def test_codex_rejects_unauthorized_request_before_launch(self):
        from archive_research.assessment_codex import submit_once as codex_submit
        from unittest.mock import patch
        with patch('archive_research.assessment_codex.subprocess.run') as run:
            with self.assertRaises(ValueError):
                codex_submit(self.request,self.receipt,authorized_request_sha256='0'*64,
                             executable='/unused',root=self.fixture.base,prefix='unused')
            run.assert_not_called()


if __name__=='__main__':unittest.main()
