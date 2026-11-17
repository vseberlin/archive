"""One explicitly authorized Responses request, with no retries or redirects."""
import hashlib
import json
from urllib.request import Request,build_opener,HTTPRedirectHandler

from ..contracts import encoded,require


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self,req,fp,code,msg,headers,newurl):
        raise ValueError('API redirect refused')


def submit_once(request,transport,*,authorized_request_sha256,api_key,opener=None):
    data=encoded(request).encode();digest=hashlib.sha256(data).hexdigest()
    require(digest==transport['request_sha256']==authorized_request_sha256,
            'Authorize the exact prepared request before submission')
    require(isinstance(api_key,str) and api_key.strip(),'Configure OPENAI_API_KEY locally')
    require(request.get('store') is False and request['model']==transport['model'] and
            request['max_output_tokens']==transport['max_output_tokens'],'Transport settings changed')
    require(len(data)<=30_000_000,'Request exceeds bounded payload size')
    call=Request('https://api.openai.com/v1/responses',data=data,method='POST',
                 headers={'Content-Type':'application/json','Authorization':'Bearer '+api_key})
    with (opener or build_opener(NoRedirect())).open(call,timeout=120) as response:
        body=response.read(8_000_001)
        require(len(body)<=8_000_000,'Response exceeds bounded payload size')
    return json.loads(body)
