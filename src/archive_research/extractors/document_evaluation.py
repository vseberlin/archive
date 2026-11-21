"""Bounded transcription and order diagnostics over existing receipts only.

Region-filtered CER/WER isolate recognition. Article inversions separately measure
reading order. Neither metric establishes complete-page accuracy or name identity.
"""
import re
import unicodedata


def normalize(text):
    text=unicodedata.normalize('NFKC',text)
    text=re.sub(r'(?<=\w)[-\u00ad][^\S\n]*\n[^\S\n]*(?=\w)','',text)
    for char in '„“”':text=text.replace(char,'"')
    for char in '—–':text=text.replace(char,'-')
    return ' '.join(text.split())


def distance(a,b):
    if len(a)<len(b):a,b=b,a
    previous=list(range(len(b)+1))
    for i,left in enumerate(a,1):
        current=[i]
        for j,right in enumerate(b,1):
            current.append(min(current[-1]+1,previous[j]+1,previous[j-1]+(left!=right)))
        previous=current
    return previous[-1]


def inside(word,region):
    r=word['region'];x=r['x']+r['width']/2;y=r['y']+r['height']/2
    return region['x']<=x<=region['x']+region['width'] and region['y']<=y<=region['y']+region['height']


def positioned_text(words):
    lines=[];line=[];previous=None
    for word in words:
        box=word['region']
        if previous and abs(box['y']-previous['y']) > max(box['height'],previous['height'])*.5:
            lines.append(' '.join(line));line=[]
        line.append(word['text']);previous=box
    if line:lines.append(' '.join(line))
    return '\n'.join(lines)


def score(annotation,words):
    regions=[]
    for check in annotation['transcriptions']:
        expected=normalize(check['text'])
        actual=normalize(positioned_text([w for w in words if inside(w,check['region'])]))
        regions.append({'block':check['block'],'expected':expected,'actual':actual,
                        'reference_characters':len(expected),'character_errors':distance(expected,actual),
                        'reference_words':len(expected.split()),'word_errors':distance(expected.split(),actual.split())})
    nc=sum(r['reference_characters'] for r in regions);nw=sum(r['reference_words'] for r in regions)
    return {'regions':regions,'cer':sum(r['character_errors'] for r in regions)/nc if nc else None,
            'wer':sum(r['word_errors'] for r in regions)/nw if nw else None,
            'reference_characters':nc,'reference_words':nw,
            'scope':'selected visually transcribed regions; development sample; not full-page accuracy'}


def article_order(words,blocks,indices):
    sequence=[]
    for word in words:
        matches=[i for i in indices if inside(word,blocks[i]['region'])]
        if len(matches)==1:sequence.append(indices.index(matches[0]))
    pairs=inversions=0
    for pos,left in enumerate(sequence):
        for right in sequence[pos+1:]:
            if left!=right:
                pairs+=1;inversions+=left>right
    return {'cross_block_word_pairs':pairs,'inversions':inversions,
            'inversion_rate':inversions/pairs if pairs else None,
            'missing_blocks':[i for rank,i in enumerate(indices) if rank not in sequence],
            'scope':'word pairs within the selected article; caption evaluated separately'}


def article_contiguity(order,groups):
    """Count interruptions by unrelated articles, which pairwise order misses."""
    owners={block:group['label'] for group in groups for block in group['blocks']}
    runs=[]
    for block in order:
        owner=owners.get(block)
        if owner is not None and (not runs or runs[-1]!=owner):runs.append(owner)
    return {'article_runs':{g['label']:runs.count(g['label']) for g in groups},
            'interleaved_articles':[g['label'] for g in groups if runs.count(g['label'])>1],
            'scope':'article block sequence; captions and page furniture excluded'}
