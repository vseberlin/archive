"""Conservative scanned-page figure candidates, retaining OCR and crop lineage.

Local texture and Tesseract illustration boxes are proposals, not semantic labels.
Existing OCR receipts are mandatory: layout never silently transcribes a page again.
"""
from dataclasses import asdict
import importlib.metadata
import io
import re
import xml.etree.ElementTree as ET

from .contracts import Recipe, Region, encoded, require
from .geometry import source_region, processed_region
from .layout import caption_candidates, union
from .receipts import read_current


def recipe_for(task):
    from .runner import tool_version
    settings = {'dpi': 150, 'clockwise': 0, 'max_side': 1600, 'min_area': .008,
                'max_area': .75, 'max_figures': 32, 'caption_gap': .10,
                **task.get('settings', {})}
    require(set(settings) == {'dpi','clockwise','max_side','min_area','max_area',
                             'max_figures','caption_gap','ocr_origin'}, 'Bind an OCR receipt for scan layout')
    require(task.get('region') is None, 'Scan layout needs a complete page')
    require(type(settings['max_side']) is int and 600 <= settings['max_side'] <= 2400, 'Invalid detection size')
    require(0 < settings['min_area'] < settings['max_area'] <= .85 and
            0 < settings['caption_gap'] <= .25, 'Invalid scan layout bounds')
    require(type(settings['max_figures']) is int and 1 <= settings['max_figures'] <= 64, 'Invalid figure limit')
    require(set(settings['ocr_origin']) == {'key','receipt_sha256'}, 'Invalid OCR origin')
    return Recipe.create('scan-layout', '2', settings=settings,
                         dependencies={**{n:importlib.metadata.version(n) for n in
                                       ('opencv-python-headless','numpy','Pillow')},
                                       'pdftoppm':tool_version(['pdftoppm','-v'])},
                         output='scan-figure-and-caption-candidates')


def validate_inputs(roots, work, task, recipe):
    from .runner import page_from_record
    origin=recipe.record()['parameters']['settings']['ocr_origin']
    row,result,digest=read_current(roots,work,origin['key'])
    require(digest==origin['receipt_sha256'] and result.page==page_from_record(task['page']) and
            result.recipe.extractor=='ocr' and result.region is None, 'OCR origin changed or belongs to another page')
    return row,result


def ocr_layout(raw, result):
    """Keep Tesseract block/line ordering; coordinates always map to source space."""
    root=ET.fromstring(raw); blocks=[]; illustrations=[]; omitted=[]
    def name(n):return n.tag.rsplit('}',1)[-1]
    def region(n):
        return asdict(source_region(tuple(float(n.attrib[k]) for k in ('HPOS','VPOS','WIDTH','HEIGHT')),
                                    result.transform,result.page))
    for node in root.iter():
        if name(node)=='Illustration':
            try: illustrations.append(region(node))
            except ValueError: omitted.append({'reason':'empty or invalid OCR illustration box'})
        if name(node)!='TextBlock':continue
        lines=[]
        for line in node:
            if name(line)!='TextLine':continue
            words=[n.attrib['CONTENT'] for n in line if name(n)=='String' and n.attrib.get('CONTENT','').strip()]
            if words:
                try: lines.append({'text':' '.join(words),'region':region(line),'font':'ocr'})
                except ValueError: omitted.append({'reason':'empty or invalid OCR line box'})
        if not lines:continue
        text=' '.join(l['text'] for l in lines)
        blocks.append({'text':text,'region':asdict(union([Region(**l['region']) for l in lines])),
                       'lines':lines,'labelled':bool(re.match(r'^(?:Abb\.?|Abbildung|Fig(?:ure)?\.?|Bild)\s*\d',text,re.I)),
                       'basis':'machine OCR block; unreviewed'})
    return blocks,illustrations,omitted


def intersection(a,b):
    return max(0,min(a[0]+a[2],b[0]+b[2])-max(a[0],b[0]))*max(0,min(a[1]+a[3],b[1]+b[3])-max(a[1],b[1]))


def detect(image, text_boxes, illustration_boxes, settings):
    """Bounded texture proposals plus independent ALTO illustration proposals."""
    import cv2 as cv
    import numpy as np
    cv.setNumThreads(2)
    gray=cv.cvtColor(np.asarray(image.convert('RGB')),cv.COLOR_RGB2GRAY)
    scale=min(1.,settings['max_side']/max(gray.shape)); h,w=[max(1,round(v*scale)) for v in gray.shape]
    gray=cv.resize(gray,(w,h),interpolation=cv.INTER_AREA)
    # Local contrast is insensitive to the uniform yellow paper of old reports.
    ink=cv.adaptiveThreshold(gray,255,cv.ADAPTIVE_THRESH_GAUSSIAN_C,cv.THRESH_BINARY_INV,31,12)
    for x,y,bw,bh in text_boxes:
        x0,y0=max(0,int(x*w)-2),max(0,int(y*h)-2)
        ink[y0:min(h,int((y+bh)*h)+3),x0:min(w,int((x+bw)*w)+3)]=0
    density=cv.boxFilter(ink.astype(np.float32)/255,-1,(15,15))
    mask=(density>.23).astype(np.uint8)*255
    mask=cv.morphologyEx(mask,cv.MORPH_CLOSE,np.ones((13,13),np.uint8))
    mask=cv.morphologyEx(mask,cv.MORPH_OPEN,np.ones((9,9),np.uint8))
    contours,_=cv.findContours(mask,cv.RETR_EXTERNAL,cv.CHAIN_APPROX_SIMPLE)
    candidates=[{'box':(x/w,y/h,bw/w,bh/h),'basis':['local-texture']} for x,y,bw,bh in
                (cv.boundingRect(c) for c in contours)]
    candidates += [{'box':b,'basis':['tesseract-illustration']} for b in illustration_boxes]
    accepted=[];omitted=[]
    for item in sorted(candidates,key=lambda c:-c['box'][2]*c['box'][3]):
        x,y,bw,bh=item['box'];area=bw*bh
        if area < settings['min_area']:continue
        reason=('near-whole-page candidate' if area>settings['max_area'] else
                'thin rule or border candidate' if min(bw,bh)<.035 or max(bw/bh,bh/bw)>10 else '')
        if reason:omitted.append({'reason':reason,'display_box':item['box']});continue
        duplicate=next((a for a in accepted if intersection(a['box'],item['box'])/min(area,a['box'][2]*a['box'][3])>.8),None)
        if duplicate:
            duplicate['basis']=sorted(set(duplicate['basis']+item['basis']));continue
        if len(accepted)>=settings['max_figures']:
            omitted.append({'reason':'figure limit reached','display_box':item['box']});continue
        accepted.append(item)
    return sorted(accepted,key=lambda c:(c['box'][1],c['box'][0])),omitted


def extract(image_path,page,transform,roots,settings,temporary,key,work):
    from PIL import Image
    row,result,_=read_current(roots,work,settings['ocr_origin']['key'])
    raw=next(a for a in result.artifacts if a.kind=='machine-ocr-alto')
    blocks,illustrations,ocr_omitted=ocr_layout(roots.artifact(raw).read_bytes(),result)
    def display(r):
        b=processed_region(Region(**r),transform,page)
        return b.x,b.y,b.width,b.height
    text_boxes=[display(w['region']) for w in row['words'] if
                w.get('confidence') is not None and w['confidence']>=.4 and len(w['text'])>=2]
    figures=[];extra=[]
    with Image.open(image_path) as image:
        candidates,omitted=detect(image,text_boxes,[display(r) for r in illustrations],settings)
        omitted.extend(ocr_omitted)
        for i,candidate in enumerate(candidates):
            x,y,w,h=candidate['box'];box=(x*image.width,y*image.height,w*image.width,h*image.height)
            region=asdict(source_region(box,transform,page));name=key+f'.figure-{i}.png'
            crop=image.crop((round(box[0]),round(box[1]),round(box[0]+box[2]),round(box[1]+box[3])))
            buf=io.BytesIO();crop.save(buf,format='PNG');extra.append((name,buf.getvalue(),f'layout-figure-crop-{i}'))
            figures.append({'region':region,'crop':name,'basis':candidate['basis'],
                            'caption_candidates':caption_candidates(region,blocks,settings['caption_gap']),
                            'identity_confirmed':False})
    feature={'figures':figures,'text_blocks':blocks,'omitted':omitted,
             'coverage':{'figures_prepared':len(figures),'semantic_layout_complete':False,
                         'ocr_words':len(row['words']),'detection_complete':True},
             'ocr_origin':settings['ocr_origin'],
             'limitations':['Texture and OCR illustration boxes are candidates: tables, text and borders can be false positives; smooth or faint figures can be missed.',
                            'Caption links use proximity only. OCR errors and wrong reading orientation remain visible.',
                            'No candidates does not establish that a page has no figures.']}
    return (encoded(feature)+'\n').encode(),row['words'],feature,extra
