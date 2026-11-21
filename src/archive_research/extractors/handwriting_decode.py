"""Bounded CTC decoding over retained scores; never overwrite literal readings.

No trained language model or automatic name authority. A frozen lexicon gives
soft word preferences; explicit cursive confusions propose bounded alternatives.
CTC probabilities rank complete strings, including variable-length substitutions.
"""
from dataclasses import asdict
from functools import lru_cache
import importlib.metadata
import json
import math
import re
import time
import unicodedata

from ..contracts import Recipe,Source,Region,encoded,require
from ..receipts import read_origin,read_current
from ..result_records import origin

NEG=-math.inf
MODES={'beam','lexicon','confusions'}
WORD=re.compile(r'[^\W\d_]+',re.UNICODE)
# Seeded by the user's cursive-confusion hypotheses, not the reference answers.
CONFUSIONS=(('n','u',.35),('m','nn',.5),('m','un',.5),('e','i',.35),
            ('r','v',.35),('cl','d',.5),('u','ü',.35),('a','ä',.35),
            ('o','ö',.35),('ß','B',.5),('ß','fs',.5))


def add(a,b):
    if a==NEG:return b
    if b==NEG:return a
    if a<b:a,b=b,a
    return a+math.log1p(math.exp(b-a))


def log_probabilities(logits,alphabet):
    import numpy as np
    a=np.asarray(logits,dtype=np.float64)
    require(isinstance(alphabet,str) and 1<=len(alphabet)<=512 and len(set(alphabet))==len(alphabet),
            'Invalid CTC alphabet')
    require(a.ndim==2 and 1<=a.shape[0]<=2048 and a.shape[1]==len(alphabet)+1 and np.isfinite(a).all(),
            'Invalid or unbounded CTC scores')
    a=a-a.max(axis=1,keepdims=True)
    return a-np.log(np.exp(a).sum(axis=1,keepdims=True))


class Lexicon:
    def __init__(self,frequencies,custom):
        self.counts={}
        for word,count in frequencies:
            word=unicodedata.normalize('NFC',word).lower()
            if word.isalpha() and 2<=len(word)<=40 and count>0:
                self.counts[word]=max(count,self.counts.get(word,0))
        self.custom={unicodedata.normalize('NFC',w).lower() for w in custom}
        self.maximum=max(self.counts.values(),default=1)

    def known(self,word):
        return word.lower() in self.counts or word.lower() in self.custom

    @lru_cache(maxsize=50000)
    def word_score(self,word):
        w=word.lower();count=self.counts.get(w,0)
        # Unknown names remain legal; no hard vocabulary or OOV penalty.
        return max(1.5 if w in self.custom else 0,
                   1+2*math.log1p(count)/math.log1p(self.maximum) if count else 0)

    @lru_cache(maxsize=50000)
    def score(self,text,final=True):
        return sum(self.word_score(m.group()) for m in WORD.finditer(text)
                   if final or m.end()<len(text))

    @lru_cache(maxsize=10000)
    def variants(self,word):
        """At most two explicit confusion edits; never generic spell replacement."""
        rules=[]
        for a,b,cost in CONFUSIONS:
            rules.extend([(a,b,cost),(b,a,cost)])
            if a.islower() and b.islower():
                rules.extend([(a.upper(),b.upper(),cost),(b.upper(),a.upper(),cost)])
        seen={word:0.};frontier={word:0.}
        for _ in range(2):
            proposals={}
            for current,cost in frontier.items():
                for a,b,delta in rules:
                    for pos in range(len(current)-len(a)+1):
                        if current[pos:pos+len(a)]!=a:continue
                        candidate=current[:pos]+b+current[pos+len(a):];total=cost+delta
                        if total<seen.get(candidate,math.inf):
                            proposals[candidate]=min(total,proposals.get(candidate,math.inf))
            # Search bound is independent of recognition/reference text quality.
            ordered=sorted(proposals.items(),key=lambda x:(-self.known(x[0]),x[1],x[0]))[:128]
            frontier=dict(ordered);seen.update(frontier)
        out=[(w,c,'cursive confusion') for w,c in seen.items() if w!=word and self.known(w)]
        for i in range(2,len(word)-1):
            if self.known(word[:i]) and self.known(word[i:]):
                out.append((word[:i]+' '+word[i:],.6,'insert word boundary'))
        return sorted(out,key=lambda x:(x[1],-self.score(x[0]),x[0]))[:16]


def prefix_beam(logp,alphabet,*,width=32,token_limit=12,token_margin=12.,lexicon=None,weight=0.):
    """Separate blank/nonblank prefix masses; deterministic bounded pruning."""
    import numpy as np
    beams={'':(0.,NEG)}
    for frame in logp:
        chosen=sorted(range(len(frame)),key=lambda c:(-frame[c],c))[:token_limit]
        chosen=[c for c in chosen if frame[c]>=frame.max()-token_margin]
        chosen=sorted(set(chosen)|{0});next_beams={}
        def update(prefix,blank=NEG,nonblank=NEG):
            old=next_beams.get(prefix,(NEG,NEG))
            next_beams[prefix]=(add(old[0],blank),add(old[1],nonblank))
        for prefix,(pb,pn) in beams.items():
            total=add(pb,pn);update(prefix,blank=total+float(frame[0]))
            for c in chosen:
                if c==0:continue
                char=alphabet[c-1];prob=float(frame[c])
                if prefix.endswith(char):
                    update(prefix,nonblank=pn+prob)
                    update(prefix+char,nonblank=pb+prob)
                else:update(prefix+char,nonblank=total+prob)
        def ranking(item):
            text,masses=item
            return (-(add(*masses)+(weight*lexicon.score(text,False) if lexicon else 0)),text)
        beams=dict(sorted(next_beams.items(),key=ranking)[:width])
    return [{'text':text,'search_log_probability':add(*masses)} for text,masses in
            sorted(beams.items(),key=lambda x:(-(add(*x[1])+(weight*lexicon.score(x[0]) if lexicon else 0)),x[0]))]


def ctc_scores(logp,texts,alphabet):
    """Exact forward probability across all CTC alignments for each candidate."""
    import numpy as np
    mapping={c:i+1 for i,c in enumerate(alphabet)};valid=[];result=[NEG]*len(texts)
    for i,text in enumerate(texts):
        if all(c in mapping for c in text) and len(text)<=len(logp):valid.append((i,text))
    if not valid:return result
    width=2*max(len(t) for _,t in valid)+1
    labels=np.zeros((len(valid),width),dtype=np.int64);lengths=[]
    for row,(_,text) in enumerate(valid):
        labels[row,1:2*len(text):2]=[mapping[c] for c in text];lengths.append(2*len(text)+1)
    mask=np.arange(width)[None,:]<np.asarray(lengths)[:,None]
    skips=np.zeros_like(mask);skips[:,2:]=(labels[:,2:]!=0)&(labels[:,2:]!=labels[:,:-2])
    alpha=np.full(labels.shape,NEG);alpha[:,0]=0.
    for frame in logp:
        one=np.full_like(alpha,NEG);one[:,1:]=alpha[:,:-1]
        two=np.full_like(alpha,NEG);two[:,2:]=alpha[:,:-2];two[~skips]=NEG
        alpha=np.logaddexp(np.logaddexp(alpha,one),two)+frame[labels];alpha[~mask]=NEG
    for row,(index,text) in enumerate(valid):
        end=lengths[row]-1
        result[index]=float(np.logaddexp(alpha[row,end],alpha[row,end-1])) if text else float(alpha[row,0])
    return result


def candidates_from_confusions(texts,lexicon,limit):
    candidates={text:(0.,'beam candidate') for text in texts}
    for text in texts:
        words=list(WORD.finditer(text))
        for word in words:
            for replacement,cost,reason in lexicon.variants(word.group()):
                candidate=text[:word.start()]+replacement+text[word.end():]
                if cost<candidates.get(candidate,(math.inf,''))[0]:candidates[candidate]=(cost,reason)
        for a,b in zip(words,words[1:]):
            if text[a.end():b.start()]==' ' and lexicon.known(a.group()+b.group()):
                candidate=text[:a.end()]+text[b.start():]
                if .6<candidates.get(candidate,(math.inf,''))[0]:candidates[candidate]=(.6,'remove word boundary')
    base=set(texts)
    ordered=sorted(candidates,key=lambda t:(t not in base,-(lexicon.score(t)-candidates[t][0]),t))
    return [(t,*candidates[t]) for t in ordered[:limit]]


def decode(logits,alphabet,settings,lexicon=None):
    from .handwriting import ctc_decode
    logp=log_probabilities(logits,alphabet);raw=ctc_decode(logp.argmax(-1),alphabet)
    mode=settings['mode'];use_lexicon=mode!='beam';weight=settings['lexicon_weight'] if use_lexicon else 0.
    beams=prefix_beam(logp,alphabet,width=settings['beam_width'],token_limit=settings['token_limit'],
                      token_margin=settings['token_margin'],lexicon=lexicon if use_lexicon else None,weight=weight)
    texts=list(dict.fromkeys([raw,*[b['text'] for b in beams[:settings['nbest']]]]))
    candidates=(candidates_from_confusions(texts,lexicon,settings['candidate_limit']) if mode=='confusions'
                else [(t,0.,'beam candidate') for t in texts])
    probs=ctc_scores(logp,[t for t,_,_ in candidates],alphabet);ranked=[]
    for (text,cost,reason),prob in zip(candidates,probs):
        if not math.isfinite(prob):continue
        lexical=lexicon.score(text) if use_lexicon else 0.
        ranked.append({'text':text,'ctc_log_probability':prob,'lexicon_score':lexical,
                       'confusion_cost':cost,'candidate_basis':reason,
                       'score':prob+weight*lexical-settings['confusion_weight']*cost})
    ranked.sort(key=lambda r:(-r['score'],r['text']));require(ranked,'No valid decoding candidates')
    best=ranked[0]
    return {'raw_text':raw,'text':best['text'],'changed':best['text']!=raw,
            'candidates':ranked[:settings['nbest']],'candidates_scored':len(ranked),
            'score_margin':best['score']-ranked[1]['score'] if len(ranked)>1 else None,
            'score_is_calibrated_confidence':False,'review_required':True}


DEFAULTS={'dpi':300,'clockwise':0,'mode':'beam','beam_width':32,'token_limit':12,'token_margin':12.,'nbest':8,
          'lexicon_weight':.8,'confusion_weight':1.,'candidate_limit':64}


def recipe_for(task):
    s={**DEFAULTS,**task.get('settings',{})};lex=s['mode'] in ('lexicon','confusions')
    require(task.get('region') is None and s['mode'] in MODES,'Invalid handwriting decoding mode')
    require(s['dpi']==300 and s['clockwise']==0,'Decoding preserves original page geometry')
    require(set(s)==set(DEFAULTS)|{'reading_origin'}|({'dictionary','custom_lexicon'} if lex else set()),
            'Bind only explicit scores and decoding inputs')
    for name,low,high in [('beam_width',2,128),('token_limit',2,256),('nbest',1,16),('candidate_limit',16,128)]:
        require(type(s[name]) is int and low<=s[name]<=high,'Invalid decoding bound: '+name)
    require(s['nbest']<=s['beam_width'] and s['candidate_limit']>=s['nbest']+1,'Invalid candidate bounds')
    for name,low,high in [('token_margin',1,30),('lexicon_weight',0,3),('confusion_weight',0,5)]:
        require(type(s[name]) in (float,int) and low<=s[name]<=high,'Invalid decoding weight: '+name)
    for name in ('dictionary','custom_lexicon') if lex else ():
        require(len(Source(**s[name]).sha256)==64,'Fingerprint decoding vocabulary')
    return Recipe.create('handwriting-decode','1',settings=s,
        dependencies={'numpy':importlib.metadata.version('numpy')},
        algorithm='CTC prefix beam; exact candidate forward rescoring; optional soft unigram lexicon and bounded cursive confusions',
        confusions=[list(x) for x in CONFUSIONS],
        output='derived line alternatives; raw parent retained; no automatic correction or calibrated confidence')


def inputs(roots,work,page,s):
    row,parent,_=read_origin(roots,work,s['reading_origin'],page)
    require(parent.recipe.extractor=='handwriting' and
            parent.recipe.record()['parameters']['settings'].get('retain_scores') is True,
            'Decoding requires retained CTC scores, not an already corrected reading')
    artifacts={a.path:a for a in parent.artifacts if a.kind=='handwritten-line-ctc-scores'}
    require(1<=len(row['feature']['lines'])<=64,'Invalid selected line coverage')
    for line in row['feature']['lines']:
        Region(**line['region']);require(isinstance(line.get('scores'),dict) and
            line['scores'].get('path') in artifacts,'Missing bound score artifact')
    return row,artifacts


def load_lexicon(roots,s):
    if s['mode']=='beam':return None
    path=roots.source(Source(**s['dictionary']),verify_hash=True)
    require(path.stat().st_size<=10000000,'Dictionary exceeds bounded input')
    frequencies=[]
    for line in path.read_text().splitlines():
        word,count=line.rsplit(maxsplit=1);frequencies.append((word,int(count)))
    require(len(frequencies)<=150000,'Dictionary exceeds bounded entries')
    custom=json.loads(roots.source(Source(**s['custom_lexicon']),verify_hash=True).read_text())
    terms=custom.get('terms',[])+custom.get('names',[])
    require(len(terms)<=5000 and all(isinstance(x,str) and 1<=len(x)<=80 for x in terms),'Invalid custom lexicon')
    return Lexicon(frequencies,terms)


def validate_inputs(roots,work,task,recipe):
    from ..runner import page_from_record
    s=recipe.record()['parameters']['settings'];inputs(roots,work,page_from_record(task['page']),s);load_lexicon(roots,s)


def task_for(roots,work,key,mode,**settings):
    row,result,digest=read_current(roots,work,key)
    require(result.recipe.extractor=='handwriting','Select original handwriting evidence')
    return {'id':key+'-decode-'+mode,'extractor':'handwriting-decode','page':asdict(result.page),
            'settings':{**settings,'mode':mode,'reading_origin':origin(roots,work,key,digest)}}


def extract(path,page,transform,roots,recipe,temporary,key,work,encoders=None):
    import numpy as np
    s=recipe.record()['parameters']['settings'];row,artifacts=inputs(roots,work,page,s);lexicon=load_lexicon(roots,s)
    readings=[];words=[];start=time.monotonic()
    for line in row['feature']['lines']:
        started=time.monotonic()
        with np.load(roots.artifact(artifacts[line['scores']['path']]),allow_pickle=False) as scores:
            require(set(scores.files)=={'logits','alphabet'},'Invalid score bundle')
            alphabet=scores['alphabet'].item();decoded=decode(scores['logits'],alphabet,s,lexicon)
        require(decoded['raw_text']==line['text'],'Retained scores do not reproduce literal parent reading')
        reading={**line,**decoded,'decoding_seconds':time.monotonic()-started,'parent_origin':s['reading_origin']}
        readings.append(reading)
        if reading['text']:words.append({'text':reading['text'],'region':line['region'],'confidence':None})
    feature={'mode':s['mode'],'lines':readings,'elapsed_seconds':time.monotonic()-start,
        'literal_parent_replaced':False,'metadata_adopted':False,'review_required':True,
        'coverage':{'selected_lines':len(readings),'full_page_accuracy_qualified':False},
        'text_blocks':[{'index':i,'label':'handwriting','region':r['region'],'text':r['text'],
                        'lines':[{'text':r['text'],'region':r['region']}]} for i,r in enumerate(readings)],
        'limitations':['Frozen vocabulary is a soft preference, not proof of a name or historical spelling.',
                       'One bounded confusion event per line candidate; up to two edits within one token.',
                       'Top-k and beam pruning can omit the correct candidate. Scores are not calibrated confidence.']}
    return (encoded(feature)+'\n').encode(),words,feature,[]
