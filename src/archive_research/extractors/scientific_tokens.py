"""Conservative R&D token hypotheses; raw readings and ambiguities stay visible."""
from dataclasses import asdict
import itertools
import re
import statistics
import xml.etree.ElementTree as ET

from ..contracts import require
from ..geometry import source_region

ELEMENTS=set('H He Li Be B C N O F Ne Na Mg Al Si P S Cl Ar K Ca Sc Ti V Cr Mn Fe Co Ni Cu Zn Ga Ge As Se Br Kr Rb Sr Y Zr Nb Mo Tc Ru Rh Pd Ag Cd In Sn Sb Te I Xe Cs Ba La Ce Pr Nd Pm Sm Eu Gd Tb Dy Ho Er Tm Yb Lu Hf Ta W Re Os Ir Pt Au Hg Tl Pb Bi Po At Rn Fr Ra Ac Th Pa U Np Pu Am Cm Bk Cf Es Fm Md No Lr Rf Db Sg Bh Hs Mt Ds Rg Cn Nh Fl Mc Lv Ts Og'.split())
SUBSCRIPT=str.maketrans('0123456789','₀₁₂₃₄₅₆₇₈₉')
UNITS=set('lx cd nm µm K °C mA kV Ω % µA V A mV mm cm m s ms Hz kHz MHz Pa Torr W kW'.split())
UNIT_ALIASES={'μm':'µm','um':'µm','μA':'µA','uA':'µA','Ix':'lx','1x':'lx','|x':'lx'}


def quantity(raw):
    match=re.fullmatch(r'\s*([+−-]?\d+(?:[.,]\d+)?)\s*([^\d\s].*|1x)\s*',raw)
    if not match:return None
    value,unit=match.groups();unit=unit.strip();normalized=UNIT_ALIASES.get(unit,unit)
    if normalized not in UNITS:return None
    return {'raw':raw,'value':value.replace(',','.'),'unit':normalized,
            'normalized':value.replace(',','.')+' '+normalized,'route':'text',
            'repairs':[] if normalized==unit else [unit+' → '+normalized],
            'review_required':normalized!=unit}


def quantity_route(raw):
    # A damaged formula can start with a digit (e.g. 5b2S3). Only a recognized
    # quantity may bypass the recognizer; unknown units remain unresolved.
    return quantity(raw)


def _plain(raw,latex):
    text=raw.strip().translate(str.maketrans('₀₁₂₃₄₅₆₇₈₉','0123456789'))
    if latex:
        for left,right in [('$$','$$'),('\\[','\\]'),('\\(','\\)'),('$','$')]:
            if text.startswith(left) and text.endswith(right):text=text[len(left):-len(right)].strip();break
        text=re.sub(r'\\(?:mathrm|text|operatorname)\{([A-Za-z0-9\s]+)\}',r'\1',text)
        text=re.sub(r'_\{\s*(\d+)\s*\}',r'\1',text)
        text=re.sub(r'_(\d)',r'\1',text)
    # Case is evidence: CO is not Co. Never infer a missing count or symbol case.
    return re.sub(r'\s+','',text)


def _parse(text):
    parts=list(re.finditer(r'([A-Z][a-z]?)([1-9]\d*)?',text))
    if not parts or ''.join(m[0] for m in parts)!=text:return None
    if any(m[1] not in ELEMENTS for m in parts):return None
    return [{'element':m[1],'count':m[2] or '1','explicit_count':m[2] is not None} for m in parts]


def formula(raw,*,latex=False,ocr=False):
    text=_plain(raw,latex);result={'raw':raw,'plain':text,'alternatives':[],
        'supported_grammar':'simple element/count sequences only','review_required':True}
    if not text or len(text)>64:return result
    options=[]
    for i,c in enumerate(text):
        replacement={'(':'2',',':'2','Z':'2','O':'0','l':'1'}.get(c) if ocr else None
        # Do not reinterpret the l in a valid Cl/Al/... element as a count.
        if c=='l' and i and text[i-1:i+1] in ELEMENTS:replacement=None
        options.append([c,replacement] if replacement else [c])
    if sum(len(v)>1 for v in options)>6:
        result['limit_reached']=True;return result
    seen=set()
    for choice in itertools.product(*options):
        candidate=''.join(choice);tokens=_parse(candidate)
        if tokens is None or candidate in seen:continue
        seen.add(candidate)
        repairs=[{'offset':i,'from':a,'to':b} for i,(a,b) in enumerate(zip(text,candidate)) if a!=b]
        result['alternatives'].append({'normalized':candidate,'tokens':tokens,
            'unicode':''.join(t['element']+(t['count'].translate(SUBSCRIPT) if t['explicit_count'] else '') for t in tokens),
            'repairs':repairs})
    return result


def compare_readings(ocr_raw,latex,*,subscript_evidence=None):
    left=formula(ocr_raw,ocr=True);right=formula(latex,latex=True)
    a={v['normalized'] for v in left['alternatives']};b={v['normalized'] for v in right['alternatives']}
    exact_unique=len(a)==len(b)==1 and a==b
    repairs=left['alternatives'][0]['repairs'] if len(a)==1 else []
    # A confusion hypothesis is not independent corroboration without position evidence.
    supported=not repairs or all((subscript_evidence or {}).get(p['offset'],False) for p in repairs)
    lower=exact_unique and supported
    return {'tesseract':left,'vision':right,'shared_alternatives':sorted(a&b),
        'review_priority':'lower' if lower else 'review',
        'reason':'unique token agreement' if lower else 'disagreement, ambiguity or unsupported confusion repair',
        'token_agreement':exact_unique,'repair_geometry_supported':supported,
        'search_normalized':next(iter(a)) if lower else None,
        'display_unicode':left['alternatives'][0]['unicode'] if lower else None,
        'review_required':True,'metadata_adopted':False}


def positioned_hocr(raw,page,transform):
    root=ET.fromstring(raw)
    def has(n,cl):return cl in n.get('class','').split()
    def prop(n,name):
        for v in n.get('title','').split(';'):
            bits=v.strip().split()
            if bits and bits[0]==name:return list(map(float,bits[1:]))
        return None
    pages=[n for n in root.iter() if has(n,'ocr_page')]
    require(len(pages)==1 and prop(pages[0],'bbox')==[0,0,transform.width,transform.height], 'hOCR geometry differs')
    words=[];lines=[]
    line_classes={'ocr_line','ocr_header','ocr_caption','ocr_textfloat'}
    for line in (n for n in root.iter() if line_classes.intersection(n.get('class','').split())):
        line_box=prop(line,'bbox');baseline=prop(line,'baseline');indices=[]
        for word in (n for n in line.iter() if has(n,'ocrx_word')):
            chars=[]
            for c in (n for n in word.iter() if has(n,'ocrx_cinfo')):
                b=prop(c,'x_bboxes');require(b and len(b)==4,'Missing character box')
                x0,y0,x1,y1=b
                value=''.join(c.itertext()).strip()
                if x1<=x0 or y1<=y0:
                    chars.append({'text':value,'pixel_box':None,'region':None,'below_baseline':False,
                                  'small_glyph':False,'baseline_delta_pixels':None});continue
                chars.append({'text':value,'pixel_box':[x0,y0,x1-x0,y1-y0],
                    'region':asdict(source_region((x0,y0,x1-x0,y1-y0),transform,page))})
            if chars:
                b=prop(word,'bbox');x0,y0,x1,y1=b
                indices.append(len(words));words.append({'text':''.join(c['text'] for c in chars),
                    'characters':chars,'line':len(lines),'pixel_box':[x0,y0,x1-x0,y1-y0],
                    'region':asdict(source_region((x0,y0,x1-x0,y1-y0),transform,page))})
        heights=[c['pixel_box'][3] for i in indices for c in words[i]['characters'] if c['text'].isalpha() and c['pixel_box']]
        typical=statistics.median(heights) if heights else 0
        for i in indices:
            for c in words[i]['characters']:
                if c['pixel_box'] is None:continue
                x,y,w,h=c['pixel_box'];base=(line_box[3]+baseline[1]+baseline[0]*(x+w/2-line_box[0])) if baseline else None
                c['below_baseline']=base is not None and typical>0 and y+h-base>.15*typical
                c['small_glyph']=typical>0 and h<.9*typical
                c['baseline_delta_pixels']=None if base is None else y+h-base
        lines.append({'pixel_box':line_box,'baseline':baseline,'words':indices})
    return {'words':words,'lines':lines,'geometry_basis':'Tesseract hOCR character boxes and line baselines; unreviewed'}


def _union(chars,pad=0):
    x=min(c['pixel_box'][0] for c in chars);y=min(c['pixel_box'][1] for c in chars)
    right=max(c['pixel_box'][0]+c['pixel_box'][2] for c in chars);bottom=max(c['pixel_box'][1]+c['pixel_box'][3] for c in chars)
    left=max(0,x-pad);top=max(0,y-pad)
    return [left,top,right+pad-left,bottom+pad-top]


def candidates(reading,page,transform):
    words=reading['words'];quantities=[];found=[];unlocated=[]
    for line in reading['lines']:
        indices=line['words']
        for start in range(len(indices)):
            for size in (1,2,3):
                selected=indices[start:start+size]
                if len(selected)!=size:continue
                raw=' '.join(words[i]['text'] for i in selected);q=quantity(raw)
                if q:
                    chars=[{'pixel_box':words[i]['pixel_box']} for i in selected]
                    quantities.append({**q,'region':asdict(source_region(_union(chars),transform,page)), 'words':selected})
        for start in range(len(indices)):
            for size in range(1,min(6,len(indices)-start)+1):
                selected=indices[start:start+size];chosen=[words[i] for i in selected]
                if size>1 and not all(re.fullmatch(r'[A-Z][a-z]?|[0-9,ZOl]+',w['text']) for w in chosen):continue
                if size>1 and any(b['pixel_box'][0]-(a['pixel_box'][0]+a['pixel_box'][2])>max(a['pixel_box'][3],b['pixel_box'][3]) for a,b in zip(chosen,chosen[1:])):continue
                chars=[c for w in chosen for c in w['characters']]
                stop=next((i for i,c in enumerate(chars) if c['text'] in ('-','–','—')),len(chars));chars=chars[:stop]
                while chars and chars[-1]['text'] in ('.',';',':',')'):chars.pop()
                raw=''.join(c['text'] for c in chars)
                symbols=[m[0] for m in re.finditer(r'[A-Z][a-z]?',raw) if m[0] in ELEMENTS]
                if len(symbols)<2 or not any(c.isnumeric() or c in '(,Z' for c in raw):continue
                if any(set(selected)&set(q['words']) for q in quantities):continue
                if not chars:continue
                if any(c['pixel_box'] is None for c in chars):
                    unlocated.append({'raw':raw,'words':selected,'reason':'OCR character has no usable box'});continue
                box=_union(chars,2);box[2]=min(box[2],transform.width-box[0]);box[3]=min(box[3],transform.height-box[1])
                found.append({'raw':raw,'words':selected,'characters':chars,'pixel_box':box,
                    'region':asdict(source_region(box,transform,page)),'formula':formula(raw,ocr=True),
                    'geometry_signal':any(c['below_baseline'] for c in chars),
                    'selection_basis':'automatic OCR element/count token candidate; character-box union', 'review_required':True})
    # Keep maximal overlapping word sequences, without converting them to accepted formulas.
    selected=[]
    for c in sorted(found,key=lambda c:(-len(c['words']),-len(c['raw']))):
        if any(set(c['words'])&set(p['words']) for p in selected):continue
        c['index']=len(selected);selected.append(c)
    return {'formula_candidates':selected,'unlocated_formula_candidates':unlocated,'quantities':quantities,
        'limitations':['OCR omissions cannot be recovered from absent character boxes.',
                       'Simple formulas with at least two element symbols only; geometry is supporting evidence, not proof.']}
