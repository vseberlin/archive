#!/usr/bin/env python3
"""Prepare or validate locally; submit once only with exact-request authorization."""
import argparse
import json
import os
from pathlib import Path
import sys

sys.dont_write_bytecode=True
from archive_research.sources import Roots
from archive_research.storage import lock,write_json,target
from archive_research.contracts import require
from archive_research.assessment.io import prepare_request,validate_response
from archive_research.profiles import research_profiles


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--roots',type=Path,required=True);p.add_argument('--packet',required=True)
    p.add_argument('--output',required=True);sub=p.add_subparsers(dest='action',required=True)
    prepare=sub.add_parser('prepare');prepare.add_argument('--model',required=True)
    prepare.add_argument('--request-output',required=True);prepare.add_argument('--max-output-tokens',type=int,default=2500)
    prepare.add_argument('--omit-image',type=int,action='append',default=[],help='Keep source text/context but omit this evidence image')
    validate=sub.add_parser('validate');validate.add_argument('--response',required=True);validate.add_argument('--transport',required=True)
    submit=sub.add_parser('submit');submit.add_argument('--request',required=True);submit.add_argument('--transport',required=True)
    submit.add_argument('--response-output',required=True);submit.add_argument('--authorized-request-sha256',required=True)
    submit.add_argument('--provider',choices=['responses-api','codex-chatgpt'],default='responses-api')
    submit.add_argument('--codex',type=Path,help='Existing Codex CLI signed in with ChatGPT')
    args=p.parse_args(argv);roots=Roots.from_toml(args.roots);root=roots.root('artifacts','work')
    require(args.packet!=args.output,'Output must not replace packet')
    with lock(root):
        packet=json.loads(target(root,args.packet).read_text())
        if args.action=='prepare':
            require(args.request_output not in (args.packet,args.output),'Request output must be distinct')
            from archive_research.evidence import references
            references(args.omit_image,len(packet['evidence']))
            request,receipt=prepare_request(roots,'artifacts',packet,research_profiles(packet['selection']['profiles']),
                                           model=args.model,max_output_tokens=args.max_output_tokens,
                                           attach_evidence=[i for i in range(len(packet['evidence'])) if i not in args.omit_image])
            write_json(root,args.request_output,request);write_json(root,args.output,receipt)
            print(json.dumps({k:receipt[k] for k in ('status','request_bytes','image_bytes','external_submission_authorized')}))
        elif args.action=='validate':
            require(args.output not in (args.response,args.transport),'Preserve raw response and transport')
            from archive_research.evidence import refresh_packet
            refresh_packet(roots,'artifacts',packet,research_profiles(packet['selection']['profiles']))
            value=validate_response(json.loads(target(root,args.response).read_text()),packet,
                                    json.loads(target(root,args.transport).read_text()))
            write_json(root,args.output,value);print(json.dumps({'status':value['status']}))
        else:
            from archive_research.assessment.http import submit_once
            from archive_research.contracts import encoded
            from archive_research.evidence import refresh_packet
            import hashlib
            require(len({args.packet,args.request,args.transport,args.output,args.response_output})==5,'Submission paths must be distinct')
            require(not target(root,args.output).exists() and not target(root,args.response_output).exists(),
                    'Submission output exists; inspect it before any explicit new attempt')
            refresh_packet(roots,'artifacts',packet,research_profiles(packet['selection']['profiles']))
            request=json.loads(target(root,args.request).read_text());transport=json.loads(target(root,args.transport).read_text())
            require(hashlib.sha256(encoded(packet).encode()).hexdigest()==transport['packet_sha256'],'Packet changed')
            require(hashlib.sha256(encoded(request).encode()).hexdigest()==transport['request_sha256']==args.authorized_request_sha256,
                    'Authorize the exact prepared request')
            rebuilt,_=prepare_request(roots,'artifacts',packet,research_profiles(packet['selection']['profiles']),
                                      model=transport['model'],max_output_tokens=transport['max_output_tokens'],
                                      max_side=transport['max_side'],attach_evidence=transport['attached_evidence'])
            require(encoded(rebuilt)==encoded(request),'Prepared request no longer matches current evidence')
            key=os.environ.get('OPENAI_API_KEY')
            if args.provider=='responses-api':require(bool(key),'Configure OPENAI_API_KEY locally')
            else:require(args.codex and args.codex.is_file(),'Provide existing Codex executable')
            attempt={'format':'archive-research-api-attempt-1','metadata_adopted':False,'provider':args.provider,
                     'packet_sha256':transport['packet_sha256'],'request_sha256':transport['request_sha256'],
                     'status':'submission started; outcome unknown until response is retained'}
            write_json(root,args.output,attempt)
            try:
                if args.provider=='codex-chatgpt':
                    from archive_research.assessment.codex import submit_once as codex_submit
                    response=codex_submit(request,transport,authorized_request_sha256=args.authorized_request_sha256,
                                          executable=args.codex,root=root,prefix=Path(args.response_output).stem)
                else:
                    response=submit_once(request,transport,authorized_request_sha256=args.authorized_request_sha256,api_key=key)
                write_json(root,args.response_output,response)
                validated=validate_response(response,packet,transport)
                write_json(root,args.output,validated)
                print(json.dumps({'status':validated['status'],'usage':validated['usage']}))
            except Exception as error:
                # No retry: a timeout can occur after billing. Keep the attempt visible.
                attempt.update(status='submission or validation failed; no automatic retry',error_type=type(error).__name__)
                write_json(root,args.output,attempt)
                raise RuntimeError(attempt['status']+' ('+type(error).__name__+')') from None


if __name__=='__main__':main()
