"""PDF image placements and tentative nearby captions using installed Poppler.

This discovers embedded raster figures, not all semantic figures. A full-page scan
is retained as an explicit segmentation gap. Caption proximity never proves a link.
"""
from dataclasses import asdict
import io
import importlib.metadata
import re
import subprocess
import xml.etree.ElementTree as ET

from ..imaging import RENDER_POLICY
from ..contracts import Recipe, Region, encoded, require
from ..geometry import inverse_rotation, source_region, processed_region
from ..preparation import pdf_info


def recipe_for(task):
    from ..runner import tool_version
    settings = {'dpi': 150, 'clockwise': 0, 'max_figures': 32, 'min_area': .002,
                'scan_area': .85, 'caption_gap': .12, **task.get('settings', {})}
    require(task['page']['kind'] == 'pdf' and task.get('region') is None, 'PDF layout needs a complete page')
    require(set(settings) == {'dpi','clockwise','max_figures','min_area','scan_area','caption_gap'},
            'Unknown layout setting')
    require(settings['clockwise'] == 0, 'Layout uses the embedded PDF orientation')
    require(type(settings['max_figures']) is int and 1 <= settings['max_figures'] <= 64, 'Invalid figure limit')
    require(0 < settings['min_area'] < settings['scan_area'] <= 1 and 0 < settings['caption_gap'] <= .25,
            'Invalid layout bounds')
    return Recipe.create('pdf-layout', '2', settings=settings, preparation=RENDER_POLICY,
                         dependencies={**{n:tool_version([n,'-v']) for n in ('pdftohtml','pdftotext','pdftoppm')},
                                       'Pillow':importlib.metadata.version('Pillow')},
                         output='raster-placements-and-caption-leads')


def union(regions):
    x, y = min(r.x for r in regions), min(r.y for r in regions)
    return Region(x, y, min(1., max(r.x+r.width for r in regions))-x,
                  min(1., max(r.y+r.height for r in regions))-y)


def text_blocks(lines):
    """Group nearby aligned lines, preserving literal text and per-line coordinates."""
    # Poppler splits a caption's label/body when its font changes mid-line.
    merged=[]
    for line in sorted(lines,key=lambda l:(l['region']['y'],l['region']['x'])):
        r=Region(**line['region']); match=None
        for candidate in reversed(merged):
            b=Region(**candidate['region'])
            if abs(r.y-b.y)<.004 and min(abs(r.x-b.x-b.width),abs(b.x-r.x-r.width))<.02:
                match=candidate;break
        if match is None: merged.append(dict(line));continue
        if r.x < b.x: match['text']=line['text']+' '+match['text']
        else: match['text']+=' '+line['text']
        match['font']=','.join(sorted(set(match['font'].split(','))|{line['font']}))
        match['region']=asdict(union([b,r]))
    blocks = []
    for line in sorted(merged, key=lambda l: (l['region']['y'], l['region']['x'])):
        r = Region(**line['region']); candidates = []
        for i, block in enumerate(blocks):
            last = block['lines'][-1]; b = Region(**last['region'])
            gap = r.y-(b.y+b.height)
            if (set(last['font'].split(',')) & set(line['font'].split(',')) and abs(r.x-b.x) < .035 and
                    -.002 <= gap <= max(.012, b.height*.8) and len(block['lines']) < 40):
                candidates.append((abs(gap), i))
        if candidates:
            blocks[min(candidates)[1]]['lines'].append(line)
        else:
            blocks.append({'lines': [line]})
    for block in blocks:
        block['region'] = asdict(union([Region(**l['region']) for l in block['lines']]))
        block['text'] = ' '.join(l['text'] for l in block['lines'])
        block['labelled'] = bool(re.match(r'^(?:Abb\.?|Abbildung|Fig(?:ure)?\.?|Bild)\s*\d',
                                         block['text'], re.I))
    return blocks


def caption_candidates(figure, blocks, max_gap):
    f = Region(**figure); found = []
    for index, block in enumerate(blocks):
        b = Region(**block['region'])
        ox = max(0, min(f.x+f.width,b.x+b.width)-max(f.x,b.x))
        oy = max(0, min(f.y+f.height,b.y+b.height)-max(f.y,b.y))
        dx = max(f.x-b.x-b.width, b.x-f.x-f.width, 0)
        dy = max(f.y-b.y-b.height, b.y-f.y-f.height, 0)
        if ox > 0 and oy > 0:  # Overlaid OCR within a scan is not an external caption.
            continue
        aligned = (ox/min(f.width,b.width) >= .25 or oy/min(f.height,b.height) >= .25)
        if aligned and dx+dy <= max_gap:
            found.append({'block': index, 'distance': dx+dy, 'basis': 'proximity',
                          'labelled': block['labelled'], 'identity_confirmed': False})
    return sorted(found, key=lambda r: (not r['labelled'], r['distance'], r['block']))[:3]


def extract(image_path, page, transform, roots, settings, temporary, key):
    from PIL import Image
    from .pdf_text import extract as text_extract
    path = roots.source(page.source)
    width, height, rotation, _ = pdf_info(path, page.index)
    require(abs(width-page.width) < .01 and abs(height-page.height) < .01, 'Changed PDF geometry')
    # An explicit work prefix is essential: pdftohtml otherwise writes beside its input.
    prefix = temporary/'layout'
    process = subprocess.run(['pdftohtml','-f',str(page.index),'-l',str(page.index),'-xml',
                              '-hidden','-noroundcoord','-zoom','1',str(path),str(prefix)],
                             capture_output=True, timeout=180)
    require(process.returncode == 0, 'PDF layout extraction failed')
    raw = prefix.with_suffix('.xml').read_bytes(); document = ET.fromstring(raw)
    pages = document.findall('page'); require(len(pages) == 1, 'Expected one PDF layout page')
    dw, dh = (height,width) if rotation in (90,270) else (width,height)
    mapping = inverse_rotation(width,height,dw,dh,rotation,'pdf-points')
    def region(node):
        x,y,w,h = (float(node.attrib[n]) for n in ('left','top','width','height'))
        # Mirrored image placement matrices can produce negative XML widths.
        x0,y0,x1,y1=max(0,min(x,x+w)),max(0,min(y,y+h)),min(dw,max(x,x+w)),min(dh,max(y,y+h))
        return source_region((x0,y0,x1-x0,y1-y0),mapping,page)
    lines, placements, omitted = [], [], []
    for node in pages[0]:
        if node.tag not in ('text','image'): continue
        try: r = region(node)
        except (ValueError,KeyError):
            omitted.append({'kind':node.tag,'reason':'unsupported or outside-page geometry'}); continue
        if node.tag == 'text':
            value=''.join(node.itertext()).strip()
            if value: lines.append({'text':value,'region':asdict(r),'font':node.get('font','')})
        else:
            placements.append(asdict(r))
    blocks=text_blocks(lines); figures=[]; extra=[(key+'.layout.xml',raw,'raw-poppler-layout')]
    with Image.open(image_path) as rendered:
        for placement in placements:
            r=Region(**placement); area=r.width*r.height
            reason=('full-page raster needs segmentation' if area >= settings['scan_area'] else
                    'below minimum figure area' if area < settings['min_area'] else
                    'figure limit reached' if len(figures) >= settings['max_figures'] else '')
            if reason:
                omitted.append({'kind':'image','region':placement,'reason':reason});continue
            # Repeated placements at the same coordinates (masks/overlays) share a crop.
            if any(f['region'] == placement for f in figures):continue
            shown=processed_region(r,transform,page)
            box=(round(shown.x*rendered.width),round(shown.y*rendered.height),
                 round((shown.x+shown.width)*rendered.width),round((shown.y+shown.height)*rendered.height))
            buf=io.BytesIO(); rendered.crop(box).save(buf,format='PNG'); index=len(figures)
            name=key+f'.figure-{index}.png'
            extra.append((name,buf.getvalue(),f'layout-figure-crop-{index}'))
            figures.append({'region':placement,'crop':name,'caption_candidates':caption_candidates(placement,blocks,settings['caption_gap'])})
    _, words=text_extract(roots,page)
    result={'figures':figures,'text_blocks':blocks,'omitted':omitted,
            'coverage':{'embedded_images_seen':len(placements),'figures_prepared':len(figures),
                        'semantic_layout_complete':False,'native_text_present':bool(words)},
            'limitations':['Embedded rasters only; vector drawings and scanned-page segmentation are not covered.',
                           'Nearby text is a caption candidate, never an adopted figure-caption relationship.']}
    roots.source(page.source)
    return (encoded(result)+'\n').encode(),words,result,extra


def visual_tasks(roots, work, key, model):
    """Explicitly derive bounded visual crop tasks, bound to the layout receipt."""
    from ..receipts import read_current
    from ..result_records import origin as dependency_origin
    row,result,digest=read_current(roots,work,key)
    require(result.recipe.extractor in ('pdf-layout','scan-layout'),'Expected a layout receipt')
    return [{'id':key+f'-figure-{i}','extractor':'visual','page':asdict(result.page),
             'region':figure['region'],'settings':{'model':model,'region_origin':{
                 **dependency_origin(roots,work,key,digest),'figure':i}}}
            for i,figure in enumerate(row['feature']['figures'])]
