"""Bounded local line recognition, separate from mixed-page region extraction.

Model files are explicitly downloaded and fingerprinted before this adapter runs.
No catalogue hints, reference transcriptions, remote code or network inference.
"""
from dataclasses import asdict
import hashlib
import importlib.metadata
import io
import json
import math
from pathlib import Path
import shutil
import tempfile
import time

from ..contracts import Recipe,Source,Region,encoded,require
from ..imaging import RENDER_POLICY
from ..geometry import processed_region
from ..receipts import read_origin
from .document_layout import offline_inference

BACKENDS={'dehtr-v2','trocr-small-de','tesseract-control'}


def recipe_for(task):
    from ..runner import tool_version,tessdata_fingerprints
    s={'dpi':300,'clockwise':0,'threads':2,'max_new_tokens':96,**task.get('settings',{})}
    require(task.get('region') is None and s.get('backend') in BACKENDS,'Select a supported line recognizer')
    required={'dpi','clockwise','threads','max_new_tokens','backend','layout_origin'}
    require(set(s)==required|({'model_manifest'} if s['backend']!='tesseract-control' else set()),'Invalid handwriting settings')
    require(s['dpi']==300 and s['clockwise']==0 and type(s['threads']) is int and 1<=s['threads']<=4 and
            type(s['max_new_tokens']) is int and 16<=s['max_new_tokens']<=128,'Invalid bounded handwriting settings')
    deps={'Pillow':importlib.metadata.version('Pillow')}
    if s['backend']=='tesseract-control':deps.update(tesseract=tool_version(['tesseract','--version']),tessdata=tessdata_fingerprints('deu'))
    else:
        require(len(Source(**s['model_manifest']).sha256)==64,'Fingerprint model manifest')
        for n in (('numpy','onnxruntime') if s['backend']=='dehtr-v2' else ('numpy','torch','transformers','tokenizers','safetensors')):
            deps[n]=importlib.metadata.version(n)
    return Recipe.create('handwriting','1',settings=s,preparation=RENDER_POLICY,dependencies=deps,
        preprocessing='selected source lines; RGB crop; ONNX aspect-preserving white padding; TrOCR bound processor',
        inference='offline CPU; two threads by default; greedy; no dictionary or reference hints',
        output='literal line text with source region; not word positions or verified full-page transcription')


def selected_lines(roots,work,page,s):
    row,parent,_=read_origin(roots,work,s['layout_origin'],page)
    require(parent.recipe.extractor=='document-layout','Handwriting requires document regions')
    i=s['layout_origin']['block'];require(type(i) is int and 0<=i<len(row['feature']['blocks']),'Invalid handwritten part')
    block=row['feature']['blocks'][i]
    require(block['label']=='handwriting' and 1<=len(block.get('lines',[]))<=64,'Select a handwritten part with explicit lines')
    r=Region(**block['region']);ids=[]
    for line in block['lines']:
        q=Region(**line['region']);ids.append(line['id'])
        require(q.x>=r.x-1e-8 and q.y>=r.y-1e-8 and q.x+q.width<=r.x+r.width+1e-8 and
                q.y+q.height<=r.y+r.height+1e-8,'Line outside selected part')
    require(len(ids)==len(set(ids)),'Duplicate handwritten line')
    return block['lines']


def model_files(roots,s):
    if s['backend']=='tesseract-control':return {}
    path=roots.source(Source(**s['model_manifest']),verify_hash=True);value=json.loads(path.read_text())
    repo={'dehtr-v2':'naeyn/de-htr-web-v2','trocr-small-de':'EpsilonGreedy/TrOCR_small_german_handwritten'}[s['backend']]
    require(value['repository']==repo and len(value['revision'])==40,'Unbound handwriting model revision')
    files={}
    for f in value['files']:
        require(Path(f['name']).name==f['name'] and Path(f['path']).name==f['path'] and
                f['name'] not in files and f['name'] not in ('.','..'),'Unsafe model member')
        p=path.parent/f['path'];require(p.is_file() and not p.is_symlink(),'Missing or redirected model member')
        with p.open('rb') as h:sha=hashlib.file_digest(h,'sha256').hexdigest()
        require(sha==f['sha256'] and p.stat().st_size==f['size_bytes'],'Model member changed')
        files[f['name']]=p
    required={'config.json','model.onnx'} if s['backend']=='dehtr-v2' else {'config.json','model.safetensors','preprocessor_config.json','tokenizer.json','tokenizer_config.json'}
    require(required<=files.keys(),'Incomplete local handwriting model')
    return files


def validate_inputs(roots,work,task,recipe):
    from ..runner import page_from_record
    s=recipe.record()['parameters']['settings'];selected_lines(roots,work,page_from_record(task['page']),s);model_files(roots,s)


def ctc_decode(ids,alphabet):
    out=[];previous=0
    for i in ids:
        i=int(i);require(0<=i<=len(alphabet),'CTC class outside model alphabet')
        if i and i!=previous:out.append(alphabet[i-1])
        previous=i
    return ''.join(out)


def onnx_input(image,config):
    import numpy as np
    from PIL import Image
    require(config['format_version']==2 and config['image_height']==64 and config['max_width']==1024 and
            config['input_channels']==1 and config['blank_index']==0,'Unsupported HTR input contract')
    scale=min(64/image.height,1024/image.width)
    w,h=max(1,round(image.width*scale)),max(1,round(image.height*scale))
    canvas=Image.new('L',(max(4,math.ceil(w/4)*4),64),255)
    canvas.paste(image.convert('L').resize((w,h),Image.Resampling.BILINEAR),(0,(64-h)//2))
    return np.asarray(canvas,dtype=np.float32)[None,None]/255.,h<64


def ended_with_eos(token,eos):
    return int(token) in (eos if isinstance(eos,(list,tuple)) else [eos])


def load_model(files,s,temporary):
    if s['backend']=='dehtr-v2':
        import onnxruntime as ort
        config=json.loads(files['config.json'].read_text());opts=ort.SessionOptions()
        opts.intra_op_num_threads=s['threads'];opts.inter_op_num_threads=1
        session=ort.InferenceSession(str(files['model.onnx']),sess_options=opts,providers=['CPUExecutionProvider'])
        return session,config
    if s['backend']=='trocr-small-de':
        import torch
        from transformers import AutoImageProcessor,AutoTokenizer,TrOCRProcessor,VisionEncoderDecoderModel
        torch.set_num_threads(s['threads']);torch.backends.mkldnn.enabled=False
        # Private model files have prefixed names. Materialize only safe local
        # data files under the runner's disposable directory for Transformers.
        # Native temporary storage avoids NTFS directory cleanup races. Only
        # public model data is staged here; archive crops stay in the work root.
        with tempfile.TemporaryDirectory(dir='/tmp',prefix='archive-handwriting-model-') as local:
            directory=Path(local)
            for name,p in files.items():
                if name.endswith(('.json','.model','.safetensors')):shutil.copyfile(p,directory/name)
            # This snapshot embeds an image_processor dict in processor_config;
            # load its two components explicitly instead of passing that dict as
            # a processor instance (incompatible with Transformers 4.55).
            processor=TrOCRProcessor(
                image_processor=AutoImageProcessor.from_pretrained(directory,local_files_only=True,use_fast=False),
                tokenizer=AutoTokenizer.from_pretrained(directory,local_files_only=True))
            model=VisionEncoderDecoderModel.from_pretrained(directory,local_files_only=True,use_safetensors=True,
                                                           attn_implementation='eager').eval()
        return model,processor
    return None


def extract(path,page,transform,roots,recipe,temporary,key,work,encoders=None):
    from PIL import Image
    from .ocr import extract as tesseract
    from ..geometry import source_region
    from ..contracts import Transform
    s=recipe.record()['parameters']['settings'];lines=selected_lines(roots,work,page,s);files=model_files(roots,s)
    cache=encoders if encoders is not None else {};model_key=('handwriting',s['backend'],s.get('model_manifest',{}).get('sha256'),s['threads'])
    readings=[];words=[];extra=[];start=time.monotonic()
    with offline_inference():
        if model_key not in cache:cache[model_key]=load_model(files,s,temporary)
        model=cache[model_key]
        with Image.open(path) as image:
            for index,line in enumerate(lines):
                r=processed_region(Region(**line['region']),transform,page)
                x,y=math.floor(r.x*image.width),math.floor(r.y*image.height)
                right,bottom=math.ceil((r.x+r.width)*image.width),math.ceil((r.y+r.height)*image.height)
                crop=image.crop((x,y,right,bottom)).convert('RGB');out=io.BytesIO();crop.save(out,format='PNG')
                name=f'{key}.line-{index}.png';extra.append((name,out.getvalue(),'handwritten-line-crop'))
                started=time.monotonic();warnings=[];truncated=False
                if s['backend']=='dehtr-v2':
                    session,config=model;pixels,reduced=onnx_input(crop,config)
                    logits=session.run([config['output_name']],{config['input_name']:pixels})[0]
                    text=ctc_decode(logits.argmax(-1)[0],config['alphabet'])
                    if reduced:warnings.append('Long line scaled to fit model width; character height reduced.')
                elif s['backend']=='trocr-small-de':
                    import torch
                    net,processor=model;pixels=processor(images=crop,return_tensors='pt').pixel_values
                    with torch.inference_mode():ids=net.generate(pixels,max_new_tokens=s['max_new_tokens'],num_beams=1,do_sample=False)
                    text=processor.batch_decode(ids,skip_special_tokens=True)[0]
                    truncated=not ended_with_eos(ids[0,-1],net.generation_config.eos_token_id)
                    if truncated:warnings.append('Token limit reached; transcription may be incomplete.')
                else:
                    p=temporary/'handwriting-line.png';p.write_bytes(out.getvalue())
                    # Text only: the exact line region, not inferred word positions,
                    # is published for all three methods on the same geometry.
                    a,b,c,d,e,f=transform.to_source
                    mapped=Transform(crop.width,crop.height,(a,b,a*x+b*y+c,d,e,d*x+e*y+f),transform.steps)
                    _,ww=tesseract(p,page,mapped,languages='deu',psm=7,timeout=60)
                    text=' '.join(w['text'] for w in ww)
                item={'id':line['id'],'region':line['region'],'text':text,'crop':name,
                      'elapsed_seconds':time.monotonic()-started,'warnings':warnings,'truncated':truncated,
                      'geometry':'selected line; no word or character boxes','review_required':True}
                readings.append(item)
                if text:words.append({'text':text,'region':line['region'],'confidence':None})
    feature={'backend':s['backend'],'lines':readings,'elapsed_seconds':time.monotonic()-start,
             'coverage':{'selected_lines':len(lines),'completed_lines':len(readings),'full_page_accuracy_qualified':False},
             'text_blocks':[{'index':i,'label':'handwriting','region':r['region'],'text':r['text'],
                             'lines':[{'text':r['text'],'region':r['region']}]} for i,r in enumerate(readings)],
             'limitations':['Explicitly selected lines only; region/line selection is not automatically qualified.',
                            'Literal unreviewed model output; no formula recognition or inferred word geometry.']}
    return (encoded(feature)+'\n').encode(),words,feature,extra
