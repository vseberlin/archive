"""Local DocLayout-YOLO regions; OCR and reading-order review remain separate.

Weights must be an explicitly fingerprinted local Source. No model hub resolver
is called. The optional inference package is loaded only in this adapter.
"""
from dataclasses import asdict
from contextlib import contextmanager, ExitStack
import importlib.metadata
import io
import math
import os

from ..contracts import Recipe, Source, Region, encoded, require
from ..geometry import source_region
from ..imaging import RENDER_POLICY
from ..receipts import read_current, read_origin
from ..result_records import origin as dependency_origin

TEXT_LABELS = {'title', 'plain text', 'abandon', 'figure_caption', 'table', 'table_caption', 'table_footnote'}

# Routing consumes retained regions; it never changes the detector recipe or
# labels. Unknown/ambiguous material must not silently become ordinary OCR.
TEXT_SCOPE = 'ordinary-text-v1'


def part_kind(label):
    if label in {'title', 'plain text', 'figure_caption', 'table_caption', 'table_footnote', 'formula_caption'}:
        return 'text'
    if label == 'figure': return 'visual'
    if label == 'table': return 'table'
    if label in {'isolate_formula', 'formula'}: return 'formula'
    if label == 'handwriting': return 'handwriting'
    return 'unresolved'


def visual_selection(blocks):
    """A duplicate table/figure proposal is not an ordinary photograph input."""
    selected=[];excluded=[]
    for figure in (b for b in blocks if b['label']=='figure'):
        r=figure['region'];conflicts=[]
        for table in (b for b in blocks if b['label']=='table'):
            s=table['region']
            area=max(0,min(r['x']+r['width'],s['x']+s['width'])-max(r['x'],s['x']))*max(0,min(r['y']+r['height'],s['y']+s['height'])-max(r['y'],s['y']))
            if area/(r['width']*r['height']+s['width']*s['height']-area)>=.85:conflicts.append(table['index'])
        if conflicts:excluded.append({'block':figure['index'],'table_blocks':conflicts,'reason':'conflicting table/figure region; visual processing withheld'})
        else:selected.append(figure['index'])
    return selected,excluded


def ordinary_blocks(blocks):
    """Keep specialist containers out of OCR, including text detected inside them."""
    ordinary=[]; excluded=[]
    containers=[b for b in blocks if part_kind(b['label']) in {'visual','table','formula','handwriting'}]
    for block in blocks:
        kind=part_kind(block['label'])
        if kind != 'text':
            excluded.append({'block':block['index'], 'reason':'separate '+kind+' processing'})
            continue
        r=block['region']; overlaps=[]
        for container in containers:
            s=container['region']
            area=max(0,min(r['x']+r['width'],s['x']+s['width'])-max(r['x'],s['x'])) * max(0,min(r['y']+r['height'],s['y']+s['height'])-max(r['y'],s['y']))
            if area/(r['width']*r['height']) >= .5: overlaps.append(container['index'])
        if overlaps:
            excluded.append({'block':block['index'], 'reason':'text overlaps specialist/image region',
                             'containers':overlaps})
        else: ordinary.append({**block, 'ocr_eligible':True})
    return ordinary,excluded


@contextmanager
def offline_inference():
    """Isolated extraction workers deny Python network hooks, including telemetry."""
    from unittest.mock import patch
    def denied(*args,**kwargs):raise OSError('Network is disabled during local layout inference')
    with ExitStack() as stack:
        for name in ('socket.create_connection','socket.socket.connect','socket.socket.connect_ex'):
            stack.enter_context(patch(name,side_effect=denied))
        yield


def text_selection(blocks, scope=None):
    """Account for tables and suppress duplicate/image-text proposals explicitly.

    Table OCR is literal text only, not verified cell/column structure. Old layout
    receipts may mark tables ineligible; their class still identifies text that
    must be accounted for rather than silently omitted.
    """
    require(scope in (None,TEXT_SCOPE), 'Unknown document text scope')
    selected=[];suppressed=[]
    if scope is not None:
        blocks,suppressed=ordinary_blocks(blocks)
    figures=[b for b in blocks if b['label']=='figure']
    def overlap(left,right):
        x,y,w,h=left['pixel_box'];a,b,c,d=right['pixel_box']
        intersection=max(0,min(x+w,a+c)-max(x,a))*max(0,min(y+h,b+d)-max(y,b))
        return intersection/min(w*h,c*d),intersection/(w*h+c*d-intersection)
    for block in sorted((b for b in blocks if b['ocr_eligible'] or b['label']=='table'),key=lambda b:-b['score']):
        x,y,w,h=block['pixel_box']; duplicate=None
        illustration=next((f for f in figures if overlap(block,f)[1]>=.85),None)
        if block['label']=='abandon' and illustration is not None:
            suppressed.append({'block':block['index'],'duplicate_of':illustration['index'],
                               'reason':'near-identical illustration region; inspect for omitted labels'})
            continue
        for prior in selected:
            contained,iou=overlap(block,prior)
            if contained>=.9 and iou >= (.35 if prior['label']==block['label'] else .85):
                duplicate=prior;break
        if duplicate is None:selected.append(block)
        else:suppressed.append({'block':block['index'],'duplicate_of':duplicate['index']})
    return sorted(b['index'] for b in selected),suppressed


def recipe_for(task):
    from ..runner import tool_version
    if 'region_annotation' in task.get('settings',{}):
        settings={'dpi':300,'clockwise':0,**task['settings']}
        require(task.get('region') is None and set(settings)=={'dpi','clockwise','region_annotation'} and
                settings['dpi']==300 and settings['clockwise']==0, 'Invalid prepared material regions')
        require(len(Source(**settings['region_annotation']).sha256)==64,'Fingerprint material regions')
        return Recipe.create('document-layout','8',settings=settings,preparation=RENDER_POLICY,
            output='explicit material regions and selected handwritten lines; unreviewed',
            dependencies={'Pillow':importlib.metadata.version('Pillow')})
    if task.get('settings',{}).get('refinement'):
        require(task['settings']['refinement'] in ('bordered-tables-v2','bordered-tables-v3'),'Unsupported table refinement recipe')
        settings={'dpi':300,'clockwise':0,**task['settings']}
        require(task.get('region') is None and
                set(settings)-{'normalization'}=={'dpi','clockwise','refinement','layout_origin','ocr_origin'},
                'Table refinement needs exact layout and positioned text origins')
        require(settings['dpi']==300 and settings['clockwise'] in (0,90,180,270),'Invalid table refinement preparation')
        require(settings.get('normalization') in (None,'local-red-v1'),'Unknown layout normalization')
        grouped=settings['refinement']=='bordered-tables-v3'
        return Recipe.create('document-layout','7' if grouped else '6',settings=settings,preparation=RENDER_POLICY,
            dependencies={**{n:importlib.metadata.version(n) for n in ('numpy','Pillow','opencv-python-headless')},
                          'pdftoppm':tool_version(['pdftoppm','-v'])},
            table_policy=('bordered-tables-v3: supported columns and conservative wrapped label/unit grouping' if grouped else
                          'bordered-tables-v2: supported borders and column evidence; retain conflicting table/figure labels'),
            output='retained document regions plus unreviewed table structure')
    settings = {'dpi': 300, 'clockwise': 0, 'imgsz': 1024, 'confidence': .2,
                'max_regions': 200, 'threads': 2, **task.get('settings', {})}
    require(task.get('region') is None, 'Document layout needs a full page')
    require(set(settings) - {'normalization'} == {'dpi','clockwise','imgsz','confidence','max_regions','threads','model'},
            'Unknown document layout setting')
    if 'normalization' in settings:
        require(settings['normalization']=='local-red-v1','Unknown layout normalization')
    require(len(Source(**settings['model']).sha256) == 64, 'Fingerprint local layout weights')
    require(type(settings['imgsz']) is int and 640 <= settings['imgsz'] <= 1600 and settings['imgsz'] % 32 == 0,
            'Invalid layout input size')
    require(0 < settings['confidence'] < 1 and type(settings['max_regions']) is int and
            1 <= settings['max_regions'] <= 300 and type(settings['threads']) is int and
            1 <= settings['threads'] <= 8, 'Invalid layout bounds')
    return Recipe.create('document-layout', '4', settings=settings, preparation=RENDER_POLICY, cpu_backend='pytorch-native-mkldnn-disabled',
        network='python-socket-connections-denied',
        dependencies={**{n:importlib.metadata.version(n) for n in
                         ('doclayout-yolo','torch','torchvision','numpy','Pillow','opencv-python-headless')},
                      'pdftoppm':tool_version(['pdftoppm','-v'])},
        reading_order='not inferred; region numbers are geometric locators', output='document-regions')


def validate_inputs(roots, work, task, recipe):
    if 'region_annotation' in recipe.record()['parameters']['settings']:
        from ..runner import page_from_record
        material_annotation(roots,recipe,page_from_record(task['page']))
        return
    if recipe.record()['parameters']['settings'].get('refinement'):
        from ..runner import page_from_record
        refinement_inputs(roots,work,page_from_record(task['page']),recipe.record()['parameters']['settings'])
        return
    roots.source(Source(**recipe.record()['parameters']['settings']['model']), verify_hash=True)


def extract(path, page, transform, roots, recipe, temporary, key, work=None):
    if 'region_annotation' in recipe.record()['parameters']['settings']:
        return prepared_material_regions(path,page,transform,roots,recipe,key)
    if recipe.record()['parameters']['settings'].get('refinement'):
        return refine_tables(path,page,transform,roots,recipe,key,work)
    with offline_inference():
        return _extract(path,page,transform,roots,recipe,temporary,key)


def material_annotation(roots,recipe,page):
    import json
    from ..result_records import content_page
    from ..runner import page_from_record
    source=Source(**recipe.record()['parameters']['settings']['region_annotation'])
    value=json.loads(roots.source(source,verify_hash=True).read_text())
    require(value.get('format')=='archive-material-regions-1' and value.get('metadata_adopted') is False,
            'Invalid material region annotation')
    require(content_page(page_from_record(value['page']))==content_page(page),'Material regions belong to another page')
    blocks=value['blocks'];require(isinstance(blocks,list) and 1<=len(blocks)<=100,'Bounded material regions required')
    for i,b in enumerate(blocks):
        require(b['index']==i and b['label'] in TEXT_LABELS|{'figure','handwriting','formula','unresolved'},'Invalid material part')
        r=Region(**b['region'])
        lines=b.get('lines',[]);require(isinstance(lines,list) and len(lines)<=64,'Bounded lines required')
        require(not lines or b['label']=='handwriting','Lines require handwriting region')
        for line in lines:
            s=Region(**line['region']);require(isinstance(line['id'],str) and line['id'].strip(),'Line identity required')
            require(s.x>=r.x-1e-8 and s.y>=r.y-1e-8 and s.x+s.width<=r.x+r.width+1e-8 and
                    s.y+s.height<=r.y+r.height+1e-8,'Line outside handwritten part')
    ids=[l['id'] for b in blocks for l in b.get('lines',[])];require(len(ids)==len(set(ids)),'Duplicate line identity')
    for link in value.get('caption_links',[]):
        require(set(link)=={'text_block','image_block','status'} and link['status']=='proposed' and
                type(link['text_block']) is int and type(link['image_block']) is int and
                0<=link['text_block']<len(blocks) and 0<=link['image_block']<len(blocks) and
                blocks[link['image_block']]['label']=='figure' and
                part_kind(blocks[link['text_block']]['label']) in ('text','handwriting'),'Invalid caption proposal')
    return value


def prepared_material_regions(path,page,transform,roots,recipe,key):
    from copy import deepcopy
    from PIL import Image
    from ..geometry import processed_region
    value=material_annotation(roots,recipe,page);blocks=deepcopy(value['blocks']);extra=[]
    with Image.open(path) as image:
        for block in blocks:
            r=processed_region(Region(**block['region']),transform,page)
            x,y=math.floor(r.x*image.width),math.floor(r.y*image.height)
            right,bottom=math.ceil((r.x+r.width)*image.width),math.ceil((r.y+r.height)*image.height)
            out=io.BytesIO();image.crop((x,y,right,bottom)).save(out,format='PNG')
            name=key+f".block-{block['index']}.png"
            block.update(pixel_box=[x,y,right-x,bottom-y],score=1.0,ocr_eligible=part_kind(block['label'])=='text',crop=name,
                         selection_basis='explicit source-bound annotation; not detector confidence')
            extra.append((name,out.getvalue(),'document-region-crop'))
    feature={'blocks':blocks,'caption_links':value.get('caption_links',[]),'reading_order':None,
        'coverage':{'detection_complete':False,'semantic_layout_complete':False,'mode':'explicit-selected-regions',
                    'selected_lines':sum(len(b.get('lines',[])) for b in blocks)},
        'limitations':['Source-bound region and caption proposals await operator checking.',
                       'Selected handwritten lines do not establish whole-page coverage.']}
    return (encoded(feature)+'\n').encode(),[],feature,extra


def refinement_inputs(roots,work,page,settings):
    layout,lr,_=read_origin(roots,work,settings['layout_origin'],page)
    reading,rr,_=read_origin(roots,work,settings['ocr_origin'],page)
    require(lr.recipe.extractor=='document-layout' and lr.region is None,'Expected full-page document layout')
    base=lr.recipe.record()['parameters']['settings']
    require('refinement' not in base,'Refine the original layout; do not stack table refinements')
    require(all(base.get(k)==settings.get(k) for k in ('dpi','clockwise','normalization')),
            'Table refinement must retain original layout preparation')
    require(rr.recipe.extractor in ('ocr','pdf-text','document-text') and rr.region is None,
            'Table recovery needs full-page positioned text')
    return layout,lr,reading


def table_refinement_task(roots,work,layout_key,reading_key):
    """Prepare one existing-runner task; no detector or OCR inference here."""
    layout,lr,ld=read_current(roots,work,layout_key)
    reading,rr,rd=read_current(roots,work,reading_key)
    settings=lr.recipe.record()['parameters']['settings']
    selected={k:settings[k] for k in ('dpi','clockwise','normalization') if k in settings}
    selected.update(refinement='bordered-tables-v3',layout_origin=dependency_origin(roots,work,layout_key,ld),
                    ocr_origin=dependency_origin(roots,work,reading_key,rd))
    refinement_inputs(roots,work,lr.page,selected)
    return {'id':layout_key+'-tables','extractor':'document-layout','page':asdict(lr.page),'settings':selected}


def refine_tables(path,page,transform,roots,recipe,key,work):
    from copy import deepcopy
    from html import escape
    from PIL import Image
    from .ruled_table import bordered_tables
    settings=recipe.record()['parameters']['settings']
    layout,_,reading=refinement_inputs(roots,work,page,settings)
    feature=deepcopy(layout['feature']);blocks=feature['blocks']
    require(all(b['index']==i for i,b in enumerate(blocks)),'Invalid retained layout block indices')
    tables,rejected=bordered_tables(path,page,transform,reading['words'],blocks,
                                   group_wrapped_rows=settings['refinement']=='bordered-tables-v3')
    extra=[]
    with Image.open(path) as image:
        for number,table in enumerate(tables):
            index=table.get('existing_block',len(blocks));x,y,w,h=table['pixel_box'];buf=io.BytesIO()
            image.crop((x,y,x+w,y+h)).save(buf,format='PNG')
            crop=key+f'.block-{index}.png';extra.append((crop,buf.getvalue(),'document-region-crop'))
            structure=table['structure']
            structure.update(region=table['region'],ocr_origin=settings['ocr_origin'],
                             source_word_indices=table['source_word_indices'],
                             header_text=' '.join(reading['words'][i]['text'] for i in structure['header_word_indices']),
                             unassigned_words=[{'index':i,**reading['words'][i]} for i in structure['unassigned_word_indices']])
            name=key+f'.table-{number}.json';html_name=key+f'.table-{number}.html'
            extra.append((name,(encoded(structure)+'\n').encode(),'unreviewed-table-structure'))
            html=['<!doctype html><meta charset="utf-8"><title>Unreviewed table</title>',
                  '<p>Proposed geometric text bands and columns. Raw OCR; wrapped rows and spans require review. Missing OCR words do not establish an empty source cell.</p>',
                  '<p>Header/context, unreviewed: '+escape(structure['header_text'])+'</p>',
                  '<table border="1">']
            for row in range(structure['rows']):
                html.append('<tr>'+''.join('<td>'+escape(c['text'] if c['text'] else '[no OCR words]')+'</td>' for c in structure['cells'] if c['row']==row)+'</tr>')
            html.append('</table>')
            if settings['refinement']=='bordered-tables-v3':
                html.insert(1,'<style>td{white-space:pre-wrap;vertical-align:top;padding:8px}</style>')
                html[2]='<p>Proposed rows and columns; wrapped labels and units may share a cell. Raw OCR and row grouping require review. Missing OCR words do not establish an empty source cell.</p>'
            html.append('<p>Unassigned source text, unreviewed: '+escape(' '.join(w['text'] for w in structure['unassigned_words']))+'</p>')
            extra.append((html_name,'\n'.join(html).encode(),'unreviewed-table-html'))
            table['table_structure']={'json':name,'html':html_name,'rows':structure['rows'],
                                      'columns':structure['columns'],'review_required':True}
            if 'existing_block' not in table:
                blocks.append({'index':index,'label':'table','region':table['region'],'pixel_box':table['pixel_box'],
                               'ocr_eligible':False,'score':0.,'crop':crop,
                               'basis':structure['basis'],'table_structure':table['table_structure']})
            table['block']=index
    feature['table_recovery']={'layout_origin':settings['layout_origin'],'ocr_origin':settings['ocr_origin'],
                               'policy':settings['refinement'],'tables':tables,'rejected_frames':rejected,
                               'accuracy_qualified':False}
    feature['coverage']['semantic_layout_complete']=False
    feature.setdefault('limitations',[]).append('Bordered-table recovery is an unreviewed development recipe; unbordered and broken-frame tables remain unresolved.')
    return (encoded(feature)+'\n').encode(),[],feature,extra


def block_origins(roots,work,key,row,result,digest):
    """Unchanged parts keep their original computation dependency after refinement."""
    current=dependency_origin(roots,work,key,digest)
    settings=result.recipe.record()['parameters']['settings']
    if not settings.get('refinement'):return [current for _ in row['feature']['blocks']]
    base,_,_=refinement_inputs(roots,work,result.page,settings)
    old=base['feature']['blocks'];new=row['feature']['blocks']
    require(new[:len(old)]==old,'Refinement changed retained document parts')
    return [settings['layout_origin'] for _ in old]+[current for _ in new[len(old):]]


def _extract(path, page, transform, roots, recipe, temporary, key):
    # Keep third-party configuration/cache writes inside the disposable work area.
    os.environ['YOLO_CONFIG_DIR'] = str(temporary)
    os.environ['MPLCONFIGDIR'] = str(temporary)
    os.environ['YOLO_AUTOINSTALL'] = 'false'
    import torch
    from doclayout_yolo import YOLOv10
    from PIL import Image
    settings = recipe.record()['parameters']['settings']
    model_path = roots.source(Source(**settings['model']), verify_hash=True)
    torch.set_num_threads(settings['threads'])
    torch.backends.mkldnn.enabled = False
    model = YOLOv10(str(model_path))
    result = model.predict(str(path), imgsz=settings['imgsz'], conf=settings['confidence'],
                           device='cpu', max_det=settings['max_regions'], verbose=False, save=False)[0]
    found = []
    for x0,y0,x1,y1,score,label in result.boxes.data.cpu().tolist():
        require(all(math.isfinite(v) for v in (x0,y0,x1,y1,score,label)), 'Invalid detector output')
        x0,y0 = max(0,math.floor(x0)),max(0,math.floor(y0))
        x1,y1 = min(transform.width,math.ceil(x1)),min(transform.height,math.ceil(y1))
        if x1 <= x0 or y1 <= y0: continue
        found.append({'region':asdict(source_region((x0,y0,x1-x0,y1-y0),transform,page)),
                      'pixel_box':[x0,y0,x1-x0,y1-y0], 'label':result.names[int(label)], 'score':score})
    found.sort(key=lambda r:(r['pixel_box'][1],r['pixel_box'][0]))
    extra=[]
    with Image.open(path) as image:
        for index,block in enumerate(found):
            block['index']=index
            block['ocr_eligible']=block['label'] in TEXT_LABELS
            x,y,w,h=block['pixel_box']; out=io.BytesIO()
            image.crop((x,y,x+w,y+h)).save(out,format='PNG')
            block['crop']=key+f'.block-{index}.png'
            extra.append((block['crop'],out.getvalue(),'document-region-crop'))
    feature={'blocks':found, 'coverage':{'detection_complete':True,
                'limit_reached':len(found)>=settings['max_regions'], 'semantic_layout_complete':False},
             'reading_order':None, 'caption_links':[],
             'limitations':['Region labels and boundaries are unreviewed proposals.',
                            'No article grouping, caption relationship or reading order is inferred.']}
    return (encoded(feature)+'\n').encode(),[],feature,extra


def ocr_tasks(roots, work, key, *, psms=(4,6), languages='deu', ocr_profile=None, text_scope=None):
    row,result,digest=read_current(roots,work,key)
    require(result.recipe.extractor=='document-layout','Expected document layout')
    selected,_=text_selection(row['feature']['blocks'],text_scope)
    origins=block_origins(roots,work,key,row,result,digest)
    tasks = [{'id':f'{key}-block-{b["index"]}-psm-{psm}', 'extractor':'ocr', 'page':asdict(result.page),
             'region':b['region'], 'settings':{'dpi':300,'clockwise':result.recipe.record()['parameters']['settings']['clockwise'],
                'psm':psm,'languages':languages, 'layout_origin':{
                    **origins[b['index']],'block':b['index']}}}
            for b in row['feature']['blocks'] if b['index'] in selected for psm in psms]
    if ocr_profile is not None:
        from ..profiles import resolve_ocr_task
        tasks = [resolve_ocr_task({**task, 'ocr_profile': ocr_profile}) for task in tasks]
    return tasks


def downstream_plan(roots, work, key, *, profile, visual_model=None, languages='deu+eng', handwriting=None):
    """Prepare finite downstream tasks and an accounting of every retained part.

    No inference, caption guessing, source mutation or index refresh. Existing
    OCR component recipes remain reusable; the routing selection is independent.
    """
    from copy import deepcopy
    from ..profiles import ocr_profile as get_profile
    row,result,digest=read_current(roots,work,key)
    require(result.recipe.extractor=='document-layout','Expected document layout')
    preset=get_profile(profile)
    preparation=result.recipe.record()['parameters']['settings'].get('normalization','original')
    require(preparation==preset['layout_preparation'],'Layout preparation differs from the selected material profile')
    blocks=row['feature']['blocks']
    require(all(b['index']==i for i,b in enumerate(blocks)), 'Invalid document block indices')
    for block in blocks: Region(**block['region'])
    selected,excluded=text_selection(blocks,TEXT_SCOPE)
    # Clean print can also consume explicitly extracted text parts. Its existing
    # OCR component supports crops; this does not change its full-page baseline.
    tasks=ocr_tasks(roots,work,key,psms=(6,),languages=languages,
                    ocr_profile={'name':profile,'version':preset['version']},text_scope=TEXT_SCOPE)
    if handwriting is not None:
        require(set(handwriting)<= {'backend','model_manifest'} and 'backend' in handwriting,'Invalid handwriting processor selection')
        for block in blocks:
            if block['label']=='handwriting' and block.get('lines'):
                tasks.append({'id':f"{key}-handwriting-{block['index']}",'extractor':'handwriting',
                    'page':asdict(result.page),
                    'settings':{**deepcopy(handwriting),'layout_origin':{**dependency_origin(roots,work,key,digest),'block':block['index']}}})
    if visual_model is not None:
        from .layout import visual_tasks
        model=Source(**visual_model)
        require(len(model.sha256)==64,'Fingerprint the local visual model')
        roots.source(model,verify_hash=True)
        tasks.extend(visual_tasks(roots,work,key,visual_model))
    parts=[]
    excluded_by_index={e['block']:e for e in excluded}
    _,visual_exclusions=visual_selection(blocks)
    excluded_by_index.update({e['block']:e for e in visual_exclusions})
    for block in blocks:
        index=block['index'];kind=part_kind(block['label'])
        structure=next((t['table_structure'] for t in row['feature'].get('table_recovery',{}).get('tables',[])
                        if t['block']==index),block.get('table_structure'))
        assigned=[t['id'] for t in tasks if (t['settings'].get('layout_origin',{}).get('block')==index
                    or t['settings'].get('region_origin',{}).get('block')==index)]
        parts.append({'block':index,'label':block['label'],'kind':kind,'region':deepcopy(block['region']),
                      'crop':block.get('crop'),'tasks':assigned,
                      **({'table_structure':deepcopy(structure)} if structure else {}),
                      'status':'planned' if assigned else 'retained_without_processing',
                      'reason':('separate visual processing; no model selected' if kind=='visual' and not assigned and
                                index not in {e['block'] for e in visual_exclusions} else
                                excluded_by_index.get(index,{}).get('reason',
                                    'duplicate text proposal; retained for review' if index in excluded_by_index else 'ordinary text processing')),
                      'routing_detail':excluded_by_index.get(index),
                      'protected_regions':[deepcopy(b['region']) for b in blocks if part_kind(b['label']) in {'formula','table','handwriting'}] if kind=='text' else []})
    return {'format':'archive-research-plan-1','metadata_adopted':False,'tasks':tasks,
            'material_profile':{'name':profile,'version':preset['version']},
            'text_scope':TEXT_SCOPE,'layout_origin':dependency_origin(roots,work,key,digest),
            'page':asdict(result.page),'parts':parts,
            'context':{'caption_links':deepcopy(row['feature'].get('caption_links',[])),
                       'article_groups':deepcopy(row['feature'].get('article_groups',[])),
                       'reading_order':deepcopy(row['feature'].get('reading_order')),
                       'status':'retained extraction proposals; no new associations inferred'},
            'coverage':{'detected_parts':len(parts),'ordinary_text_parts':len(selected),
                        'retained_without_processing':sum(not p['tasks'] for p in parts),
                        'detector':deepcopy(row['feature'].get('coverage',{})),
                        'full_page_accuracy_qualified':False},
            'limitations':['Routing cannot find or repair omitted or misclassified regions.',
                           'Tables, formulas, handwriting and ambiguous regions require separate qualification.',
                           'Inline scientific material may remain in raw text; protect it in downstream correction.',
                           'An empty task list means no eligible processing, not an empty source page.']}


def validate_ocr(roots, work, task, recipe):
    from ..runner import page_from_record
    origin=recipe.record()['parameters']['settings']['layout_origin']
    require(set(origin) in ({'key','block','receipt_sha256'}, {'key','block','receipt_sha256','evidence_sha256'}),
            'Invalid layout origin')
    row,result,digest=read_origin(roots,work,origin,page_from_record(task['page']))
    require(result.recipe.extractor=='document-layout' and digest==origin['receipt_sha256'], 'Layout origin changed')
    blocks=row['feature']['blocks']; index=origin['block']
    require(type(index) is int and 0 <= index < len(blocks) and
            (blocks[index]['ocr_eligible'] or blocks[index]['label']=='table' or
             part_kind(blocks[index]['label'])=='text'), 'Unknown text block')
    require(task.get('region') and Region(**task['region'])==Region(**blocks[index]['region']),
            'OCR crop differs from layout block')
