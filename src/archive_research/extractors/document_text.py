"""Assemble selected layout-bound OCR receipts into source-positioned page ALTO.

The explicit order is a proposal, not an accuracy claim. An optional fingerprinted
annotation records checked article groups and caption links without naming faces.
"""
from dataclasses import asdict
import json
import importlib.metadata
import xml.etree.ElementTree as ET

from ..contracts import Recipe, Source, Region, encoded, require
from ..geometry import source_region, processed_region
from ..imaging import RENDER_POLICY
from ..receipts import read_origin
from .document_layout import text_selection
from .document_evaluation import inside

NS='http://www.loc.gov/standards/alto/ns-v4#'


def geometric_order(blocks, text_scope=None):
    """Order coherent regions by whitespace cuts, without inheriting OCR interleaving.

    Horizontal gaps separate caption rows and full-width headers/footers first.
    Otherwise a vertical gutter separates columns. Overlapping regions retain a
    deterministic geometric proposal, not a claim of verified semantic order.
    """
    selected, _ = text_selection(blocks,text_scope)
    by_index = {b['index']: b for b in blocks}

    def arrange(indices):
        if len(indices) < 2:
            return indices
        for axis, extent in (('y', 'height'), ('x', 'width')):
            ordered = sorted(indices, key=lambda i: by_index[i]['region'][axis])
            groups = [[]]; end = -1.
            for i in ordered:
                r = by_index[i]['region']
                if groups[-1] and r[axis] > end + .002:
                    groups.append([])
                groups[-1].append(i)
                end = max(end, r[axis] + r[extent])
            if len(groups) > 1:
                return [i for group in groups for i in arrange(group)]
        return sorted(indices, key=lambda i: (by_index[i]['region']['y'], by_index[i]['region']['x'], i))

    return arrange(selected)


def propose_order(blocks,words,text_scope=None):
    """Use whole-page OCR order as anchors; place missing regions geometrically.

    This preserves Tesseract's article/column choices, including its mistakes.
    Missing headings are inserted before the nearest horizontally overlapping
    block below them. Unanchored sidebars remain explicit uncertainties.
    """
    selected,_=text_selection(blocks,text_scope); first={}
    for pos,word in enumerate(words):
        matches=[i for i in selected if inside(word,blocks[i]['region'])]
        if matches:
            i=min(matches,key=lambda i:blocks[i]['region']['width']*blocks[i]['region']['height'])
            first.setdefault(i,pos)
    order=sorted(first,key=first.get); missing=[i for i in selected if i not in first];trailing=[]
    for i in sorted(missing,key=lambda i:blocks[i]['region']['y'],reverse=True):
        r=blocks[i]['region']; candidates=[]
        for j in order:
            s=blocks[j]['region'];overlap=max(0,min(r['x']+r['width'],s['x']+s['width'])-max(r['x'],s['x']))
            if s['y']>=r['y'] and overlap/min(r['width'],s['width'])>=.25:
                candidates.append((s['y']-r['y'],abs(s['x']-r['x']),j))
        if candidates:
            target=min(candidates)[-1];order.insert(order.index(target),i)
        else:trailing.append(i)
    order.extend(sorted(trailing,key=lambda i:(blocks[i]['region']['y'],blocks[i]['region']['x'])))
    return order,{'basis':'whole-page OCR anchors plus geometric insertion; unreviewed',
                  'unanchored_blocks':missing,'anchored_blocks':len(first)}


def recipe_for(task):
    from ..runner import tool_version
    settings={'dpi':300,'clockwise':0, **task.get('settings',{})}
    required={'dpi','clockwise','layout_origin','ocr_origins','block_order'}
    require(task.get('region') is None and required<=set(settings)<=required|{'annotation','order_origin','order_method','text_scope'},
            'Invalid document text settings')
    if 'text_scope' in settings:
        from .document_layout import TEXT_SCOPE
        require(settings['text_scope']==TEXT_SCOPE,'Unknown document text scope')
    if 'order_method' in settings:
        require(settings['order_method']=='geometric-cuts-v1' and 'order_origin' not in settings,
                'Choose one explicit reading-order method')
    order=settings['block_order']
    require(isinstance(order,list) and order and all(type(i) is int and i>=0 for i in order)
            and len(order)==len(set(order)), 'Invalid document block order')
    require(isinstance(settings['ocr_origins'],list) and len(settings['ocr_origins'])==len(order),
            'Select one OCR receipt per text block')
    if 'annotation' in settings:
        require(len(Source(**settings['annotation']).sha256)==64, 'Fingerprint layout annotation')
    return Recipe.create('document-text','2',settings=settings,preparation=RENDER_POLICY,
                         dependencies={'Pillow':importlib.metadata.version('Pillow'),
                                       'pdftoppm':tool_version(['pdftoppm','-v'])},
                         output='page-alto-v4-and-structured-text', reading_order_verified=False)


def inputs(roots,work,page,settings,profile=None):
    layout,lr,_=read_origin(roots,work,settings['layout_origin'],page)
    require(lr.recipe.extractor=='document-layout','Expected document layout')
    if profile:
        from ..profiles import ocr_profile
        preparation = ocr_profile(profile['name'])['layout_preparation']
        expected = None if preparation == 'original' else preparation
        require(lr.recipe.record()['parameters']['settings'].get('normalization') == expected,
                'Layout preparation differs from the selected material profile')
    blocks=layout['feature']['blocks']; order=settings['block_order']
    scope=settings.get('text_scope')
    selected,_=text_selection(blocks,scope); expected=set(selected)
    require(set(order)==expected,'Assembly must cover every detected text block exactly once')
    # Refinement appends specialist containers without changing existing parts.
    # Their OCR may retain the original parent so geometry-only correction does
    # not force another recognition pass.
    layout_settings=lr.recipe.record()['parameters'].get('settings',{})
    original_count=0;original_digest=None
    if layout_settings.get('refinement'):
        from .document_layout import refinement_inputs
        original,_,_=refinement_inputs(roots,work,page,layout_settings)
        old=original['feature']['blocks'];original_count=len(old)
        require(blocks[:original_count]==old,'Refinement changed retained document parts')
        original_digest=layout_settings['layout_origin']['receipt_sha256']
    if settings.get('order_method') == 'geometric-cuts-v1':
        require(order == geometric_order(blocks,scope), 'Order differs from its declared geometric method')
    if 'order_origin' in settings:
        anchor,ar,_=read_origin(roots,work,settings['order_origin'],page)
        require(ar.recipe.extractor=='ocr' and ar.region is None,'Order anchors need whole-page OCR')
        require(order==propose_order(blocks,anchor['words'],scope)[0],'Order differs from its declared OCR anchors')
    readings={}
    for origin in settings['ocr_origins']:
        row,result,_=read_origin(roots,work,origin,page)
        require(result.recipe.extractor=='ocr','Expected OCR receipt')
        if profile:
            used=result.recipe.record()['parameters'].get('ocr_profile',{})
            require(all(used.get(k)==profile[k] for k in ('name','version')),
                    'Assembly OCR does not use the selected material profile')
        parent=result.recipe.record()['parameters']['settings'].get('layout_origin',{})
        index=parent.get('block')
        require(index in expected and index not in readings and
                parent.get('receipt_sha256') in {settings['layout_origin']['receipt_sha256'],
                    original_digest if type(index) is int and index<original_count else settings['layout_origin']['receipt_sha256']} and
                result.region==Region(**blocks[index]['region']), 'OCR is not bound to the selected layout block')
        artifact=next(a for a in result.artifacts if a.kind=='machine-ocr-alto')
        readings[index]=(roots.artifact(artifact).read_bytes(),result.transform,origin)
    annotation={}
    if 'annotation' in settings:
        annotation=json.loads(roots.source(Source(**settings['annotation']),verify_hash=True).read_text())
        require(annotation['layout_receipt_sha256']==settings['layout_origin']['receipt_sha256'] and
                annotation['block_order']==order, 'Annotation differs from selected layout/order')
        for link in annotation.get('caption_links',[]):
            require(type(link['figure']) is int and 0<=link['figure']<len(blocks) and
                    blocks[link['figure']]['label']=='figure' and link['caption'] in expected,
                    'Invalid caption relationship')
        for group in annotation.get('article_groups',[]):
            require(set(group['blocks'])<=expected,'Unknown article block')
    return blocks,readings,annotation


def validate_inputs(roots,work,task,recipe):
    from ..runner import page_from_record
    parameters=recipe.record()['parameters']
    inputs(roots,work,page_from_record(task['page']),parameters['settings'],parameters.get('ocr_profile'))


def compose(page,transform,blocks,readings,order,annotation=None,text_scope=None):
    """Rebuild hierarchy and IDs; transform every line and word through source space."""
    ET.register_namespace('',NS)
    def sub(parent,tag,**attrs): return ET.SubElement(parent,'{'+NS+'}'+tag,{k:str(v) for k,v in attrs.items()})
    root=ET.Element('{'+NS+'}alto',SCHEMAVERSION='4.4')
    desc=sub(root,'Description');sub(desc,'MeasurementUnit').text='pixel'
    info=sub(desc,'sourceImageInformation');sub(info,'fileName').text=page.source.representation
    reading_order=sub(root,'ReadingOrder');ordered=sub(reading_order,'OrderedGroup',ID='reading-order')
    layout=sub(root,'Layout');p=sub(layout,'Page',ID='page',PHYSICAL_IMG_NR=page.index if page.kind=='pdf' else page.member,
        WIDTH=transform.width,HEIGHT=transform.height)
    space=sub(p,'PrintSpace',HPOS=0,VPOS=0,WIDTH=transform.width,HEIGHT=transform.height)
    def coords(region):
        r=processed_region(region,transform,page)
        return dict(zip(('HPOS','VPOS','WIDTH','HEIGHT'),
                        (round(r.x*transform.width,3),round(r.y*transform.height,3),
                         round(r.width*transform.width,3),round(r.height*transform.height,3))))
    def local(node):return node.tag.rsplit('}',1)[-1]
    words=[]; text_blocks=[]
    for index in order:
        block=blocks[index]; raw,crop_transform,origin=readings[index]
        bid=f'block-{index}'
        sub(ordered,'ElementRef',ID=f'order-{index}',REF=bid)
        b=sub(space,'TextBlock',ID=bid,**coords(Region(**block['region'])))
        lines=[]
        for line in (n for n in ET.fromstring(raw).iter() if local(n)=='TextLine'):
            line_words=[n for n in line if local(n)=='String' and n.get('CONTENT','').strip()]
            if not line_words: continue
            def mapped(n):return source_region(tuple(float(n.attrib[k]) for k in ('HPOS','VPOS','WIDTH','HEIGHT')),crop_transform,page)
            lid=f'{bid}-line-{len(lines)}'; lr=mapped(line)
            l=sub(b,'TextLine',ID=lid,**coords(lr)); values=[]
            for wi,word in enumerate(line_words):
                wr=mapped(word); text=word.attrib['CONTENT'];values.append(text)
                attrs={'CONTENT':text,**({'WC':word.attrib['WC']} if 'WC' in word.attrib else {})}
                sub(l,'String',ID=f'{lid}-word-{wi}',**coords(wr),**attrs)
                words.append({'text':text,'region':asdict(wr),
                    'block_index':index, 'line_index':len(lines), 'word_index':wi,
                    'confidence':float(word.attrib['WC']) if 'WC' in word.attrib else None})
            lines.append({'text':' '.join(values),'region':asdict(lr)})
        text_blocks.append({'index':index,'label':block['label'],'region':block['region'],'lines':lines,
                            'text':'\n'.join(l['text'] for l in lines),'ocr_origin':origin})
    for block in blocks:
        if block['label']=='figure':
            sub(space,'Illustration',ID=f'figure-{block["index"]}',**coords(Region(**block['region'])))
    feature={'text_blocks':text_blocks,'block_order':order,
             'suppressed_duplicates':text_selection(blocks)[1] if text_scope is None else [],
             'reading_order_basis':'explicit annotation' if annotation else 'geometric proposal; unreviewed',
             'annotation':annotation or {},'coverage':{'detected_text_blocks':len(order),
                 'nonempty_text_blocks':sum(bool(b['text']) for b in text_blocks), 'full_page_accuracy_qualified':False}}
    if text_scope is not None:
        feature['text_scope']=text_scope
        feature['unassembled_parts']=text_selection(blocks,text_scope)[1]
        feature['coverage']['detected_parts']=len(blocks)
    return ET.tostring(root,encoding='utf-8',xml_declaration=True),words,feature


def extract(page,transform,roots,recipe,work,key):
    settings=recipe.record()['parameters']['settings']
    blocks,readings,annotation=inputs(roots,work,page,settings,recipe.record()['parameters'].get('ocr_profile'))
    raw,words,feature=compose(page,transform,blocks,readings,settings['block_order'],annotation,settings.get('text_scope'))
    if settings.get('order_method'):
        feature['reading_order_basis']='horizontal-first whitespace cuts between coherent regions; unreviewed'
        feature['order_method']=settings['order_method']
    if 'order_origin' in settings:
        anchor,_,_=read_origin(roots,work,settings['order_origin'],page)
        feature['order_proposal']=propose_order(blocks,anchor['words'],settings.get('text_scope'))[1]
        feature['reading_order_basis']=feature['order_proposal']['basis']
        feature['order_origin']=settings['order_origin']
    return raw,words,feature,[(key+'.document-text.json',(encoded(feature)+'\n').encode(),'structured-document-text')]
