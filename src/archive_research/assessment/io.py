"""Bounded Responses request preparation and citation validation; no network calls.

Prepared requests stay local. Structural citation validity is not historical truth.
"""
import base64
import hashlib
import io
import json
import re

from .prepare import prepare_assessment
from ..contracts import encoded, require
from ..evidence import evidence_image, references, refresh_packet


def schema():
    string={'type':'string'}
    refs={'type':'array','items':{'type':'integer'}}
    def obj(properties):return {'type':'object','properties':properties,'required':list(properties),'additionalProperties':False}
    observation=obj({'claim':string,'evidence':refs,'basis':{'type':'string','enum':['visible','literal-text','inference']},'uncertainty':string})
    hypothesis=obj({'claim':string,'supports':refs,'contradicts':refs,'uncertainty':string})
    return obj({'observations':{'type':'array','items':observation},
                'hypotheses':{'type':'array','items':hypothesis},
                'unresolved':{'type':'array','items':string},'next_local_search':string})


def prepare_request(roots,work,packet,profiles,*,model,max_output_tokens=2500,max_side=1600,attach_evidence=None):
    from PIL import Image
    require(isinstance(model,str) and re.fullmatch('[a-zA-Z0-9._-]{1,100}',model), 'Explicit model identifier required')
    require(type(max_output_tokens) is int and 256<=max_output_tokens<=8000, 'Bound output tokens')
    require(type(max_side) is int and 512<=max_side<=2048, 'Bound attachment dimensions')
    draft=prepare_assessment(roots,work,packet,profiles)
    current=refresh_packet(roots,work,packet,profiles)
    attached=list(range(len(current['evidence']))) if attach_evidence is None else attach_evidence
    references(attached,len(current['evidence']))
    require(attached,'Select at least one image for multimodal assessment')
    # Filesystem locators, original paths and model recipes are unnecessary for the API.
    context={k:draft['context'][k] for k in ('case_key','question','profiles','caption_links',
                'cross_references','hypotheses','code_readings','omissions','limitations')}
    context['source_groups']=[{'evidence':g['evidence']} for g in draft['context']['source_groups']]
    context['evidence']=[{'evidence':i,**{k:e[k] for k in ('label','kind','region','text','lineage','attribution','limitations')},
                        'page':{k:e['page'][k] for k in ('member','kind','index','printed_label')},
                        'web_context':e.get('web_context'),'image_attached':i in attached}
                       for i,e in enumerate(current['evidence'])]
    content=[{'type':'input_text','text':encoded(context)}];attachments=[];total=0
    for i,e in enumerate(current['evidence']):
        if i not in attached:continue
        raw=evidence_image(roots,work,e)
        with Image.open(io.BytesIO(raw)) as original:
            image=original.convert('RGB');before=list(image.size)
            image.thumbnail((max_side,max_side),Image.Resampling.LANCZOS)
            output=io.BytesIO();image.save(output,format='JPEG',quality=90,optimize=True)
            data=output.getvalue();total+=len(data)
            require(total<=20_000_000,'Prepared attachments exceed local 20 MB limit')
            attachments.append({'evidence':i,'source_sha256':hashlib.sha256(raw).hexdigest(),
                                'transport_sha256':hashlib.sha256(data).hexdigest(),'original_size':before,
                                'transport_size':list(image.size),'bytes':len(data),'display_region':e['display_region']})
        content.extend([{'type':'input_text','text':f'Evidence {i}: {e["label"]}; selected region: {encoded(e["display_region"])}'},
                        {'type':'input_image','image_url':'data:image/jpeg;base64,'+base64.b64encode(data).decode(),'detail':'high'}])
    request={'model':model,'store':False,'max_output_tokens':max_output_tokens,
             'instructions':draft['instructions'],'input':[{'role':'user','content':content}],
             'text':{'format':{'type':'json_schema','name':'archive_assessment','strict':True,'schema':schema()}}}
    receipt={'format':'archive-research-transport-1','packet_sha256':draft['packet_sha256'],
             'request_sha256':hashlib.sha256(encoded(request).encode()).hexdigest(),
             'model':model,'max_output_tokens':max_output_tokens,'max_side':max_side,'attachments':attachments,
             'attached_evidence':attached,'omitted_image_evidence':[i for i in range(len(current['evidence'])) if i not in attached],
             'image_bytes':total,'request_bytes':len(encoded(request).encode()),
             'external_submission_authorized':False,'status':'prepared locally; not submitted',
             'limitations':['Resizing can reduce small-text legibility; original bound views remain available locally.',
                            'store=false does not establish account retention controls or material sharing permission.']}
    return request,receipt


def validate_response(response, packet, transport):
    require(hashlib.sha256(encoded(packet).encode()).hexdigest()==transport['packet_sha256'], 'Response packet binding changed')
    require(response.get('status')=='completed' and not response.get('error') and not response.get('incomplete_details'),
            'Assessment failed or incomplete')
    require(response.get('model')==transport['model'] or
            str(response.get('model','')).startswith(transport['model']+'-'), 'Unexpected response model')
    provider='responses-api'
    if response.get('format')=='archive-research-codex-response-1':
        from .codex import completed_result
        require(response.get('provider')=='codex-chatgpt' and response.get('request_sha256')==transport['request_sha256'],
                'Codex request binding changed')
        value=response['assessment']
        require(completed_result(response['events'],value)==response['usage'],'Codex usage changed')
        provider='codex-chatgpt'
    else:
        messages=[]
        for item in response.get('output',[]):
            if item.get('type')=='reasoning':continue
            require(item.get('type')=='message' and item.get('role')=='assistant','Unexpected model output or tool use')
            for part in item.get('content',[]):
                require(part.get('type')=='output_text','Refusal or unsupported response content')
                messages.append(part['text'])
        require(len(messages)==1,'Expected exactly one assessment object')
        value=json.loads(messages[0])
    require(isinstance(value,dict) and set(value)==set(schema()['properties']), 'Invalid assessment fields')
    count=len(packet['evidence'])
    def nonempty(value):return isinstance(value,str) and bool(value.strip()) and len(value)<=10000
    for group,fields,ref_fields in [('observations',{'claim','evidence','basis','uncertainty'},['evidence']),
                                   ('hypotheses',{'claim','supports','contradicts','uncertainty'},['supports','contradicts'])]:
        require(isinstance(value[group],list) and len(value[group])<=50,'Invalid assessment list')
        for row in value[group]:
            require(isinstance(row,dict) and set(row)==fields and nonempty(row['claim']) and nonempty(row['uncertainty']),
                    'Claims need explicit uncertainty and known fields')
            for field in ref_fields:references(row[field],count)
            require(any(row[f] for f in ref_fields),'Uncited assertion refused')
            if group=='observations':require(row['basis'] in ('visible','literal-text','inference'),'Unknown observation basis')
    require(isinstance(value['unresolved'],list) and all(nonempty(s) for s in value['unresolved']) and
            nonempty(value['next_local_search']), 'Unresolved questions and next search required')
    return {'format':'archive-research-assessment-1','metadata_adopted':False,'provider':provider,
            'packet_sha256':transport['packet_sha256'],'request_sha256':transport['request_sha256'],
            'response_id':response.get('id'),'model':response['model'],'usage':response.get('usage'),
            'assessment':value,'status':'unreviewed proposal; citation ranges checked',
            'limitations':['Citation validation checks references, not whether the evidence supports the claim.',
                           'Independent curator review and accounting for duplicate sources remain required.']}
