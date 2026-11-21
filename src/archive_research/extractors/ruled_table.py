"""Ruled-cell geometry and literal cell OCR; forms bypass table recognition."""
from dataclasses import asdict,replace
import importlib.metadata
import io
import re
from statistics import median
from ..contracts import Recipe,Region,encoded,require
from ..imaging import RENDER_POLICY
from ..geometry import source_region


def classify_document(text):
    folded=text.casefold()
    form=(('vorschlag' in folded and 'dokumentationsdienst' in folded) or
          ('dokumentationsdienst' in folded and re.search(r'datum\s+geprüft',folded) is not None) or
          sum(s in folded for s in ('plan-nr','geprüft','nichtzutreffendes','einverstanden mit','unterschrift'))>=2)
    fields=[]
    if form:
        for index,line in enumerate(text.splitlines()):
            m=re.match(r'\s*(Plan[- ]?Nr\.?|Datum|geprüft|Bearbeiter|Thema(?: der Arbeit)?)\s*:\s*(.*)',line,re.I)
            if m:fields.append({'key':m[1],'raw_value':m[2],'line':index,'review_required':True})
            elif re.fullmatch(r'\s*Datum\s+geprüft\s*',line,re.I):
                for label in ('Datum','geprüft'):
                    fields.append({'key':label,'raw_value':None,'line':index,
                                   'value_status':'not established by OCR','review_required':True})
    return {'kind':'form' if form else 'unresolved','fields':fields,
            'basis':'explicit form wording; no table inferred from a border', 'review_required':True}


def detect_cells(path):
    import cv2
    import numpy as np
    from PIL import Image
    with Image.open(path) as im:a=np.array(im.convert('L'))
    binary=cv2.adaptiveThreshold(a,255,cv2.ADAPTIVE_THRESH_MEAN_C,cv2.THRESH_BINARY_INV,31,10)
    h,w=a.shape
    horizontal=cv2.morphologyEx(binary,cv2.MORPH_OPEN,cv2.getStructuringElement(cv2.MORPH_RECT,(max(25,w//20),1)))
    vertical=cv2.morphologyEx(binary,cv2.MORPH_OPEN,cv2.getStructuringElement(cv2.MORPH_RECT,(1,max(25,h//20))))
    grid=cv2.bitwise_or(horizontal,vertical)
    # Only close tiny raster gaps; do not invent absent ruling lines.
    grid=cv2.morphologyEx(grid,cv2.MORPH_CLOSE,np.ones((3,3),np.uint8))
    count,labels,stats,_=cv2.connectedComponentsWithStats((grid==0).astype(np.uint8),8)
    cells=[]
    for x,y,cw,ch,area in stats[1:]:
        if x<=1 or y<=1 or x+cw>=w-1 or y+ch>=h-1:continue
        if cw<20 or ch<15 or area<.7*cw*ch:continue
        cells.append({'pixel_box':[int(x),int(y),int(cw),int(ch)],'geometry':'closed ruled region'})
    cells.sort(key=lambda c:(c['pixel_box'][1],c['pixel_box'][0]))
    return cells,grid


def grouped_body_rows(words, rows, first, bounds, height):
    """Conservative proposals for ruled property/value tables, retaining line IDs.

    A new record needs a label and values in at least two other columns.
    Only nearby label-column continuations attach to it. Ambiguity keeps the
    original text bands; tiny vertical-rule OCR remains explicitly unassigned.
    """
    groups=[]
    for ri in range(first,len(rows)):
        significant=[i for i in rows[ri] if not (
            words[i]['region']['height'] < height*.3 and words[i]['text'].strip() in ('|','!'))]
        if not significant:continue
        columns={sum(words[i]['region']['x']+words[i]['region']['width']/2>=b
                     for b in bounds[1:-1]) for i in significant}
        top=min(words[i]['region']['y'] for i in significant)
        bottom=max(words[i]['region']['y']+words[i]['region']['height'] for i in significant)
        if 0 in columns and len(columns)>=3:
            groups.append({'lines':[(ri,significant)],'bottom':bottom})
        elif groups and columns=={0} and top-groups[-1]['bottom']<=height*2.5:
            groups[-1]['lines'].append((ri,significant));groups[-1]['bottom']=bottom
        else:
            return None
    if len(groups)<3 or not any(len(g['lines'])>1 for g in groups):return None
    return groups


def aligned_table(words, region, column_boundaries=None, *, group_wrapped_rows=False):
    """Propose columns inside a border from repeated positioned OCR, not prose.

    No OCR edits, inferred merged cells or semantic row labels. Empty positions
    and tokens crossing a proposed gutter remain explicit review cases.
    """
    from .document_evaluation import inside
    indices=[i for i,w in enumerate(words) if inside(w,region)]
    if len(indices)<12:return None
    height=median(words[i]['region']['height'] for i in indices)
    if height<=0:return None
    bands=[]
    for i in sorted(indices,key=lambda i:words[i]['region']['y']+words[i]['region']['height']/2):
        r=words[i]['region'];cy=r['y']+r['height']/2
        if not bands or abs(cy-median(b[0] for b in bands[-1]))>height*.65:bands.append([])
        bands[-1].append((cy,i))
    rows=[sorted((i for _,i in band),key=lambda i:words[i]['region']['x']) for band in bands]
    numeric=[]
    for ri,row in enumerate(rows):
        if len(row)<3:continue
        for i in row:
            token=words[i]['text'].strip()
            if re.fullmatch(r'\d[\d.,—–~\-]*',token):
                r=words[i]['region'];numeric.append((r['x']+r['width'],ri,i))
    # Align numeric right edges; do not count several numbers in one row as
    # independent evidence. A framed heading/photograph is not a data table.
    clusters=[]
    for x,ri,i in sorted(numeric):
        if not clusters or x-median(v[0] for v in clusters[-1])>region['width']*.015:clusters.append([])
        clusters[-1].append((x,ri,i))
    numeric_rows={ri for _,ri,_ in numeric}
    anchors=[c for c in clusters if len({ri for _,ri,_ in c})>=max(4,len(numeric_rows)*.5)]
    if column_boundaries is not None:
        cols={sum(x>b for b in column_boundaries[1:-1]) for x,_,_ in numeric}
        if len(numeric)<4 or len(numeric_rows)<2 or len(cols)<2:return None
        body_indices=sorted(numeric_rows)
    else:
        if not anchors:return None
        body_indices=sorted({ri for c in anchors for _,ri,_ in c})
    first,last=min(body_indices),max(body_indices)
    body=rows[first:last+1]
    # Require conspicuous repeated gaps, then use their common intersection.
    gaps=[]
    for row in body:
        rr=[words[i]['region'] for i in row]
        gaps.append([(a['x']+a['width'],b['x']) for a,b in zip(rr,rr[1:])
                     if b['x']-(a['x']+a['width'])>=region['width']*.015])
    edges=sorted({x for row in gaps for gap in row for x in gap})
    segments=[]
    for left,right in zip(edges,edges[1:]):
        mid=(left+right)/2
        support=sum(any(a<=mid<=b for a,b in row) for row in gaps)
        if support>=max(4,len(body)*.75):
            if segments and abs(segments[-1][1]-left)<1e-9:segments[-1][1]=right
            else:segments.append([left,right])
    gutters=(column_boundaries[1:-1] if column_boundaries is not None else
             [(a+b)/2 for a,b in segments if b-a>=region['width']*.015])
    if not gutters or len(gutters)>11:return None
    bounds=[region['x'],*gutters,region['x']+region['width']]
    # A numeric column and at least one preceding field must recur together.
    if column_boundaries is None and not any(any(g<x for g in gutters) for c in anchors for x,_,_ in c):return None
    cells=[];crossing=[]
    for ri,row in enumerate(body):
        top=min(words[i]['region']['y'] for i in row)
        bottom=max(words[i]['region']['y']+words[i]['region']['height'] for i in row)
        for ci,(left,right) in enumerate(zip(bounds,bounds[1:])):
            selected=[i for i in row if left<=words[i]['region']['x']+words[i]['region']['width']/2<right]
            crossing.extend(i for i in selected if words[i]['region']['x']<left or
                            words[i]['region']['x']+words[i]['region']['width']>right)
            cells.append({'row':ri,'column':ci,'region':{'x':left,'y':top,'width':right-left,'height':bottom-top},
                          'word_indices':selected,'text':' '.join(words[i]['text'] for i in selected),
                          'no_source_words':not selected,'review_required':True})
    assigned=[i for c in cells for i in c['word_indices']]
    require(len(assigned)==len(set(assigned)),'Table word assigned twice')
    structure={'rows':len(body),'columns':len(bounds)-1,'cells':cells,'column_boundaries':bounds,
            'header_word_indices':[i for row in rows[:first] for i in row],
            'unassigned_word_indices':sorted(set(indices)-set(assigned)),
            'gutter_crossing_word_indices':sorted(set(crossing)),
            'numeric_anchor_columns':len(anchors),'geometry_verified':False,'table_structure_verified':False,
            'basis':('closed border plus supported column rulings and numeric rows' if column_boundaries is not None else
                     'closed border plus repeated numeric alignment and whitespace gutters'),
            'limitations':['Rows are geometric text bands, not verified logical records.',
                           'Wrapped cells, spans and header associations require review.',
                           'Cell text reuses raw positioned OCR; missing source words cannot be recovered by geometry.']}
    if group_wrapped_rows:
        groups=grouped_body_rows(words,rows,first,bounds,height) if column_boundaries is not None else None
        structure['row_grouping']='text-bands'
        if groups:
            structure['text_rows']={'rows':len(body),'cells':cells}
            grouped=[]
            for ri,group in enumerate(groups):
                all_indices=[i for _,line in group['lines'] for i in line]
                top=min(words[i]['region']['y'] for i in all_indices)
                bottom=max(words[i]['region']['y']+words[i]['region']['height'] for i in all_indices)
                for ci,(left,right) in enumerate(zip(bounds,bounds[1:])):
                    lines=[[i for i in line if left<=words[i]['region']['x']+words[i]['region']['width']/2<right]
                           for _,line in group['lines']]
                    selected=[i for line in lines for i in line]
                    grouped.append({'row':ri,'column':ci,
                        'region':{'x':left,'y':top,'width':right-left,'height':bottom-top},
                        'word_indices':selected,'text':'\n'.join(' '.join(words[i]['text'] for i in line) for line in lines if line),
                        'line_word_indices':[line for line in lines if line],
                        'no_source_words':not selected,'review_required':True})
            assigned=[i for c in grouped for i in c['word_indices']]
            require(len(assigned)==len(set(assigned)),'Grouped table word assigned twice')
            structure.update(rows=len(groups),cells=grouped,row_grouping='aligned-values-with-label-continuations-v1',
                unassigned_word_indices=sorted(set(indices)-set(assigned)),
                gutter_crossing_word_indices=sorted(i for c in grouped for i in c['word_indices']
                    if words[i]['region']['x']<c['region']['x'] or
                    words[i]['region']['x']+words[i]['region']['width']>c['region']['x']+c['region']['width']),
                limitations=['Logical rows are proposals from aligned values and nearby label/unit continuations.',
                             'Original OCR and text-band cells are retained; headers, spans and cell readings require review.',
                             'No spelling, numeric or unit correction is inferred.'])
    return structure


def bordered_tables(path, page, transform, words, blocks, *, group_wrapped_rows=False):
    """Recover missed bordered tables; retain rejected frames for diagnosis."""
    from .document_evaluation import inside
    from ..geometry import processed_region
    prepared=[{**w,'region':asdict(processed_region(Region(**w['region']),transform,page))} for w in words]
    import cv2
    import numpy as np
    cells,mask=detect_cells(path);tables=[];rejected=[]
    # A partially ruled table may have closed header cells but unclosed body
    # cells. Inspect the enclosing connected ruling as well as individual holes.
    contours,_=cv2.findContours(mask,cv2.RETR_EXTERNAL,cv2.CHAIN_APPROX_SIMPLE)
    for contour in contours:
        x,y,w,h=cv2.boundingRect(contour)
        if w<40 or h<40:continue
        part=mask[y:y+h,x:x+w]>0
        dx,dy=max(4,round(w*.025)),max(4,round(h*.025))
        support=[np.any(part[:dy,:],axis=0).mean(),np.any(part[-dy:,:],axis=0).mean(),
                 np.any(part[:,:dx],axis=1).mean(),np.any(part[:,-dx:],axis=1).mean()]
        if min(support)>=.6:
            cells.append({'pixel_box':[x,y,w,h],'geometry':'connected ruling with four supported borders',
                          'border_support':[float(s) for s in support]})
    cells.sort(key=lambda c:-(c['pixel_box'][2]*c['pixel_box'][3]))
    text=' '.join(w['text'] for w in words)
    if classify_document(text)['kind']=='form':
        return [],[{'reason':'explicit form wording; table recovery skipped'}]
    for cell in cells:
        region=asdict(source_region(tuple(cell['pixel_box']),transform,page))
        reason=None;existing_block=None
        if not .015<=region['width']*region['height']<=.8:reason='frame outside bounded table area'
        candidates=[*blocks,*({'label':'table','region':t['region']} for t in tables)]
        for block in sorted(candidates,key=lambda b:b['label']!='table'):
            if block['label'] not in ('figure','table'):continue
            a=region;b=block['region']
            area=max(0,min(a['x']+a['width'],b['x']+b['width'])-max(a['x'],b['x']))*max(0,min(a['y']+a['height'],b['y']+b['height'])-max(a['y'],b['y']))
            if area/(a['width']*a['height'])>=.5:
                iou=area/(a['width']*a['height']+b['width']*b['height']-area)
                if block['label']=='table' and 'index' in block and iou>=.75:
                    existing_block=block['index']
                elif block['label']=='figure' and existing_block is not None and iou>=.85:
                    # Keep the contradictory raw figure label, but a duplicate
                    # box must not veto measured ruling/column evidence.
                    continue
                else:reason='frame already belongs to a figure or detected table'
        x,y,w,h=cell['pixel_box']
        displayed={'x':x/transform.width,'y':y/transform.height,'width':w/transform.width,'height':h/transform.height}
        boundaries=None
        if not reason and cell['geometry']=='connected ruling with four supported borders':
            # Keep the source's long vertical rules even when body rows are
            # unruled and numeric ranges are centered rather than right-aligned.
            band=mask[y:y+h,x:x+w]
            expanded=cv2.dilate(band,cv2.getStructuringElement(cv2.MORPH_RECT,(max(3,round(w*.025)),1)))
            support=np.mean(expanded>0,axis=0)
            runs=[]
            for px in np.where(support>=.6)[0]:
                if not runs or px>runs[-1][-1]+1:runs.append([])
                runs[-1].append(int(px))
            cuts=[(x+sum(run)/len(run))/transform.width for run in runs
                  if run[0]>w*.04 and run[-1]<w*.96]
            if 2<=len(cuts)<=11:boundaries=[displayed['x'],*cuts,displayed['x']+displayed['width']]
        structure=None if reason else aligned_table(prepared,displayed,boundaries,group_wrapped_rows=group_wrapped_rows)
        if structure is None:rejected.append({**cell,'region':region,'reason':reason or 'insufficient repeated table alignment'});continue
        structure['column_boundaries_space']='prepared-page-normalized'
        for item in [*structure['cells'],*structure.get('text_rows',{}).get('cells',[])]:
            r=item['region']
            item['region']=asdict(source_region((r['x']*transform.width,r['y']*transform.height,
                                                r['width']*transform.width,r['height']*transform.height),transform,page))
        tables.append({**cell,'region':region,'structure':structure,
                       **({'existing_block':existing_block} if existing_block is not None else {}),
                       'source_word_indices':[i for i,w in enumerate(words) if inside(w,region)]})
    return tables,rejected


def uncovered_words(words,regions):
    """Expose OCR words lost inside the detected extent; not a completeness test."""
    if not regions:return []
    extent={'x':min(r['x'] for r in regions),'y':min(r['y'] for r in regions)}
    extent['width']=max(r['x']+r['width'] for r in regions)-extent['x']
    extent['height']=max(r['y']+r['height'] for r in regions)-extent['y']
    def contains(r,w):
        b=w['region'];x=b['x']+b['width']/2;y=b['y']+b['height']/2
        return r['x']<=x<=r['x']+r['width'] and r['y']<=y<=r['y']+r['height']
    return [w for w in words if contains(extent,w) and not any(contains(r,w) for r in regions)]


def recipe_for(task):
    from ..runner import tool_version
    settings={'dpi':300,'clockwise':0,'languages':'deu+eng','max_cells':64,**task.get('settings',{})}
    require(set(settings)=={'dpi','clockwise','languages','max_cells','ocr_origin'},'Explicit OCR origin required')
    require(type(settings['max_cells']) is int and 1<=settings['max_cells']<=100,'Invalid cell limit')
    require(type(settings['dpi']) is int and 150<=settings['dpi']<=400 and settings['clockwise'] in (0,90,180,270),'Invalid table preparation')
    from ..runner import tessdata_fingerprints
    return Recipe.create('ruled-table','3',settings=settings,preparation=RENDER_POLICY,
        dependencies={**{n:importlib.metadata.version(n) for n in ('Pillow','numpy','opencv-python-headless')},
            'tesseract':tool_version(['tesseract','--version']),'tessdata':tessdata_fingerprints(settings['languages']),
            'pdftoppm':tool_version(['pdftoppm','-v'])},output='closed cell regions and word/character OCR; unreviewed')


def reading(roots,work,page,region,settings):
    from ..receipts import read_origin
    row,result,_=read_origin(roots,work,settings['ocr_origin'],page)
    require(result.recipe.extractor=='ocr' and result.region==region,'Table/text source binding differs')
    require('character_reading' in row.get('feature',{}),'Character OCR required for form/table routing')
    return row


def validate_inputs(roots,work,task,recipe):
    from ..runner import page_from_record
    reading(roots,work,page_from_record(task['page']),Region(**task['region']) if task.get('region') else None,
            recipe.record()['parameters']['settings'])


def extract(path,page,transform,roots,recipe,temporary,key,work):
    from PIL import Image
    from ..receipts import read_origin
    from .ocr import extract_characters
    settings=recipe.record()['parameters']['settings'];row,result,_=read_origin(roots,work,settings['ocr_origin'],page)
    cr=row['feature']['character_reading'];text='\n'.join(' '.join(cr['words'][i]['text'] for i in line['words']) for line in cr['lines'])
    route=classify_document(text);extra=[]
    feature={'routing':route,'raw_text':text,'review_required':True,'metadata_adopted':False,'cells':[]}
    if route['kind']=='form':
        feature['kind']='form';feature['table_recognition_skipped']=True
        return (encoded(feature)+'\n').encode(),row['words'],feature,extra
    cells,mask=detect_cells(path);buffer=io.BytesIO();Image.fromarray(mask).save(buffer,format='PNG')
    extra.append((key+'.ruling-mask.png',buffer.getvalue(),'detected-ruling-mask'))
    feature.update(kind='ruled_table' if cells else 'unresolved',detected_cells=len(cells),
        limit_reached=len(cells)>settings['max_cells'],geometry_verified=False,
        table_structure_verified=False,
        geometry_limit='Closed regions may contain several logical rows; absent rules are not inferred.',
        fallback='vision candidate only after confirming an unruled data table' if not cells else None)
    words=[]
    with Image.open(path) as image:
        for i,cell in enumerate(cells[:settings['max_cells']]):
            x,y,w,h=cell['pixel_box'];a,b,c,d,e,f=transform.to_source
            tr=replace(transform,width=w,height=h,to_source=(a,b,a*x+b*y+c,d,e,d*x+e*y+f),steps=transform.steps+('ruled-cell crop',))
            crop=temporary/'cell.png';image.crop((x,y,x+w,y+h)).save(crop,dpi=(settings['dpi'],settings['dpi']))
            alto,hocr,ww,ff=extract_characters(crop,page,tr,temporary,languages=settings['languages'],psm=6,timeout=90)
            for suffix,content,kind in [('.alto.xml',alto,'cell-ocr-alto'),('.hocr.html',hocr,'cell-ocr-character-hocr')]:
                extra.append((key+f'.cell-{i}'+suffix,content,kind))
            region=asdict(source_region((x,y,w,h),transform,page))
            feature['cells'].append({**cell,'region':region,'text':' '.join(v['text'] for v in ww),'words':ww,
                                     'character_reading':ff['character_reading'],'review_required':True})
            words.extend(ww)
    feature['unassigned_source_words']=uncovered_words(row['words'],[c['region'] for c in feature['cells']])
    feature['coverage_warning']=('Source OCR words fall between detected regions; table extraction is incomplete.'
        if feature['unassigned_source_words'] else 'No completeness claim: source OCR can itself omit text.')
    return (encoded(feature)+'\n').encode(),words,feature,extra
