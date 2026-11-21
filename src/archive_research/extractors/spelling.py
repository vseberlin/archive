"""Dictionary correction sidecars; literal positioned OCR is never replaced."""
from copy import deepcopy
from functools import lru_cache
import importlib.metadata
import json
import re
import unicodedata

from ..contracts import Recipe, Region, Source, encoded, require
from ..receipts import read_origin
from .scientific_document import intersects
from .scientific_tokens import ELEMENTS, UNITS

ARCHIVE_TERMS=('VEB Kombinat WF TRO KWO BGL FDJ Schöneweide Oberschöneweide '
               'Niederschöneweide Johannisthal Adlershof Köpenick Karlshorst '
               'Wilhelminenhofstraße Ostendstraße Reinbeckstraße Edisonstraße '
               'Rathenaustraße Siemensstraße Spreestraße').split()
LINE_HYPHENS='-\u00ad‐‑‒–—'


def lower(text):
    # Do not conflate German ß with ss: that changes the edit distance.
    return unicodedata.normalize('NFC',text).lower()


class Lexicon:
    def __init__(self,frequencies,custom):
        from symspellpy import SymSpell
        self.engine=SymSpell(max_dictionary_edit_distance=2,prefix_length=7)
        self.protected={lower(t) for t in [*ARCHIVE_TERMS,*UNITS,*custom.get('terms',[]),*custom.get('names',[])]}
        self.names={lower(t) for t in custom.get('names',[])}
        self.unsafe_targets=self.names|{lower(t) for t in UNITS}|{lower(t) for t in ELEMENTS}
        self.unsafe_targets.update(lower(t) for t in ARCHIVE_TERMS if t!='Kombinat')
        self.unsafe_targets.update(lower(t) for t in custom.get('terms',[]) if t.isupper())
        for term,count in frequencies:
            if term.isalpha():self.engine.create_dictionary_entry(lower(term),count)
        for term in self.protected:self.engine.create_dictionary_entry(term,1)

    @lru_cache(maxsize=8192)
    def compound(self,text):
        """Exact parts only. Preserve the original word; never insert spaces."""
        if not 8<=len(text)<=64:return ()
        @lru_cache(None)
        def split(pos,depth):
            if depth==4:return None
            for end in range(pos+4,len(text)+1):
                part=text[pos:end]
                if part not in self.engine.words:continue
                if end==len(text):return (part,)
                for link in ('','s','es','n','en','er'):
                    if text.startswith(link,end):
                        tail=split(end+len(link),depth+1)
                        if tail:return (part+link,*tail)
            return None
        parts=split(0,0)
        return parts if parts and len(parts)>1 else ()

    @lru_cache(maxsize=8192)
    def suggestions(self,text):
        from symspellpy import Verbosity
        return tuple((s.term,s.distance,s.count) for s in
                     self.engine.lookup(text,Verbosity.ALL,max_edit_distance=2))


def correct(words,lexicon,protected_regions=(),protected_word_indices=()):
    for box in protected_regions:Region(**box)
    require(all(type(i) is int and 0<=i<len(words) for i in protected_word_indices),'Invalid protected word index')
    protected_ids={id(words[i]) for i in protected_word_indices}
    def protected(w):
        raw=w['text'];plain=raw.rstrip(LINE_HYPHENS)
        return (id(w) in protected_ids or any(intersects(w['region'],r) for r in protected_regions) or
                any(c.isnumeric() for c in raw) or
                lower(plain) in lexicon.protected or plain in ELEMENTS or
                (plain.isupper() and len(plain)>1) or
                re.search(r'[µμ°=<>+−/_]',raw) is not None)
    # Explicit block/line positions only; never infer a join between columns.
    line_keys=[(w.get('block_index'),w.get('line_index')) for w in words]
    units=[];i=0
    while i<len(words):
        start=i;indices=[i];text=words[i]['text'];joined=False
        while text and i+1<len(words) and text[-1] in LINE_HYPHENS:
            a,b=words[i],words[i+1];ka,kb=line_keys[i:i+2]
            left=re.fullmatch(r'[^\w]*([^\W\d_]+)',text[:-1])
            right=re.fullmatch(r'([^\W\d_]+)[^\w]*',b['text'])
            if (None in ka or None in kb or ka[0]!=kb[0] or kb[1]!=ka[1]+1 or
                protected(a) or protected(b) or not left or not right or
                not right[1][0].islower() or
                lower(right[1]) in ('und','oder','bzw','sowie')):break
            # Adjacent flattened tokens must be the last/first words of their lines.
            if (i and line_keys[i-1]==kb) or (i+2<len(words) and line_keys[i+2]==ka):break
            text=text[:-1]+b['text'];i+=1;indices.append(i);joined=True
        units.append({'raw_indices':indices,'raw_texts':[words[j]['text'] for j in indices],
                      'regions':[deepcopy(words[j]['region']) for j in indices],
                      'text':text,'joined_line_hyphen':joined,
                      'block_index':words[start].get('block_index'),'line_index':words[start].get('line_index'),
                      'status':'unchanged','candidates':[]})
        i+=1
    for unit in units:
        text=unit['text'];match=re.fullmatch(r'([^\w]*)([^\W\d_]+)([^\w]*)',text)
        if text and text[-1] in LINE_HYPHENS:
            unit.update(status='review',reason='unjoined line/hyphen fragment');continue
        if any(protected(words[j]) for j in unit['raw_indices']) or not match:
            unit['status']='protected';continue
        prefix,term,suffix=match.groups();key=lower(term)
        if key in lexicon.protected or term in ELEMENTS:
            unit['status']='protected';continue
        if key in lexicon.engine.words:
            unit['status']='known';continue
        parts=lexicon.compound(key)
        if parts:
            unit.update(status='compound_preserved',compound_parts=list(parts));continue
        candidates=lexicon.suggestions(key)
        unit['candidates']=[{'term':t,'distance':d,'frequency':n} for t,d,n in candidates]
        nearest=[c for c in unit['candidates'] if c['distance']==1]
        case_ok=term.islower() or term.istitle()
        if len(nearest)==1 and case_ok and nearest[0]['term'] not in lexicon.unsafe_targets:
            replacement=nearest[0]['term']
            if term.istitle():replacement=replacement[0].upper()+replacement[1:]
            unit.update(text=prefix+replacement+suffix,status='auto_distance_1',before=text)
        else:
            unit.update(status='review',reason='ambiguous, distance 2, protected target, unsupported case, or no candidate')
    text='';previous=None
    for unit in units:
        key=(unit['block_index'],unit['line_index'])
        separator='' if previous is None else ('\n\n' if key[0]!=previous[0] else '\n' if key!=previous else ' ')
        text+=separator+unit['text'];previous=key
    return {'corrected_tokens':units,'corrected_text':text,
            'review_items':[i for i,u in enumerate(units) if u['status']=='review'],
            'literal_ocr_replaced':False,'metadata_adopted':False,
            'review_required':True,'next_stage':'review; no external/model dispatch',
            'policy':'unique distance-1 ordinary text only; exact compound parts; raw boxes retained'}


def recipe_for(task):
    settings={'dpi':300,'clockwise':0,'protected_regions':[],**task.get('settings',{})}
    require(set(settings)=={'dpi','clockwise','protected_regions','ocr_origin','dictionary','custom_lexicon'},'Explicit dictionary and OCR origin required')
    require(type(settings['dpi']) is int and 150<=settings['dpi']<=400 and settings['clockwise'] in (0,90,180,270),'Invalid spelling view')
    for name in ('dictionary','custom_lexicon'):require(Source(**settings[name]).sha256,'Fingerprint the spelling lexicon')
    for r in settings['protected_regions']:Region(**r)
    return Recipe.create('spelling','3',settings=settings,
        dependencies={n:importlib.metadata.version(n) for n in ('symspellpy','editdistpy')},
        output='derived text, multiple source boxes and review candidates; literal OCR unchanged')


def reading(roots,work,page,region,settings):
    row,result,_=read_origin(roots,work,settings['ocr_origin'],page)
    require(result.recipe.extractor in ('ocr','document-text') and result.region==region,'Spelling OCR binding differs')
    return row,result


def lexicon(roots,settings):
    paths={n:roots.source(Source(**settings[n]),verify_hash=True) for n in ('dictionary','custom_lexicon')}
    custom=json.loads(paths['custom_lexicon'].read_text())
    require(set(custom)<= {'terms','names','provenance'} and all(isinstance(custom.get(n,[]),list) and
        all(isinstance(t,str) and t.isalpha() for t in custom.get(n,[])) for n in ('terms','names')),'Lexicon entries must be alphabetic tokens')
    frequencies=[]
    for line in paths['dictionary'].read_text(encoding='utf-8-sig').splitlines():
        term,count=line.rsplit(maxsplit=1);require(count.isdecimal(),'Invalid dictionary frequency')
        frequencies.append((term,int(count)))
    return Lexicon(frequencies,custom)


def validate_inputs(roots,work,task,recipe):
    from ..runner import page_from_record
    settings=recipe.record()['parameters']['settings']
    reading(roots,work,page_from_record(task['page']),Region(**task['region']) if task.get('region') else None,settings)
    for name in ('dictionary','custom_lexicon'):roots.source(Source(**settings[name]),verify_hash=True)


def extract(path,page,transform,roots,recipe,temporary,key,work,encoders=None):
    settings=recipe.record()['parameters']['settings']
    row,result,_=read_origin(roots,work,settings['ocr_origin'],page)
    reading(roots,work,page,result.region,settings)
    cache_key=('spelling',settings['dictionary']['sha256'],settings['custom_lexicon']['sha256'])
    dictionary=encoders.get(cache_key) if encoders is not None else None
    if dictionary is None:
        if encoders is not None:encoders.clear()
        Lexicon.compound.cache_clear();Lexicon.suggestions.cache_clear()
        dictionary=lexicon(roots,settings)
        if encoders is not None:encoders[cache_key]=dictionary
    regions=list(settings['protected_regions']);indices=set()
    # Padding around a formula crop can touch the next line. Prefer exact word
    # membership only when ALTO and hOCR word sequences demonstrably agree.
    character_words=row.get('feature',{}).get('character_reading',{}).get('words',[])
    aligned=[w['text'] for w in character_words]==[w['text'] for w in row['words']]
    for name in ('formula_candidates','quantities'):
        for c in row.get('feature',{}).get('scientific_candidates',{}).get(name,[]):
            if aligned and 'words' in c:indices.update(c['words'])
            else:regions.append(c['region'])
    feature=correct(row['words'],dictionary,regions,indices);feature['ocr_origin']=settings['ocr_origin']
    feature['lexicon_sources']={n:settings[n] for n in ('dictionary','custom_lexicon')}
    return (encoded(feature)+'\n').encode(),deepcopy(row['words']),feature,[]
