"""Opt-in local PaddleOCR-VL crop proposals; no automatic text replacement."""
import hashlib
import importlib.metadata
import json
import os
import time
from pathlib import Path

from ..contracts import Recipe, Source, encoded, require
from ..imaging import RENDER_POLICY
from .document_layout import offline_inference
from .scientific_document import render_formula, formula_similarity, table_proposal


def recipe_for(task):
    from ..runner import tool_version
    settings={'dpi':300,'clockwise':0,'threads':2,'max_new_tokens':128,
              'max_pixels':262144,'handwriting':False,'render_check':False,**task.get('settings',{})}
    require(task.get('region') is not None,'Scientific recognition requires an explicit crop')
    require(set(settings)-{'ocr_origin'}=={'dpi','clockwise','threads','max_new_tokens','max_pixels','handwriting','render_check',
                            'kind','model_manifest','selection_basis'},'Unknown scientific region setting')
    require(type(settings['render_check']) is bool,'Invalid render-check selection')
    require(settings['kind'] in ('formula','table'),'Select formula or table recognition')
    require(type(settings['handwriting']) is bool and isinstance(settings['selection_basis'],str)
            and settings['selection_basis'].strip(),'Explicit region selection and handwriting flag required')
    for name,low,high in [('threads',1,4),('max_new_tokens',8,768),('max_pixels',65536,524288),('dpi',150,400)]:
        require(type(settings[name]) is int and low<=settings[name]<=high,'Invalid '+name)
    require(settings['clockwise'] in (0,90,180,270),'Invalid rotation')
    require(len(Source(**settings['model_manifest']).sha256)==64,'Fingerprint the local model manifest')
    return Recipe.create('scientific-region','4',settings=settings,preparation=RENDER_POLICY,
        dependencies={**{n:importlib.metadata.version(n) for n in ('torch','transformers','tokenizers',
            'safetensors','einops','protobuf','Pillow','numpy','opencv-python-headless','matplotlib','torchvision','sentencepiece','huggingface-hub')},
            'pdftoppm':tool_version(['pdftoppm','-v'])},
        inference='cpu float32 eager attention; greedy; mkldnn disabled; Python sockets denied',
        output='block-positioned proposal; original OCR unchanged',renderer='matplotlib-mathtext-subset',
        comparison='element-token-hypotheses-v1; agreement lowers review priority only')


_VERIFIED_MODELS={}


def model_directory(roots,recipe):
    source=Source(**recipe.record()['parameters']['settings']['model_manifest'])
    manifest=roots.source(source,verify_hash=True)
    value=json.loads(manifest.read_text())
    require(value['repository']=='PaddlePaddle/PaddleOCR-VL-1.5','Unqualified scientific model')
    require(isinstance(value['files'],list) and value['files'],'Missing model file inventory')
    directory=manifest.parent/'paddleocr-vl-1.5'
    require(directory.is_dir() and not directory.is_symlink(),'Invalid local model directory')
    signature=[]
    for f in value['files']:
        require(Path(f['name']).name==f['name'] and f['name'] not in ('.','..'),'Invalid model filename')
        path=directory/f['name'];require(path.is_file() and not path.is_symlink(),'Missing or linked model file')
        st=path.stat();signature.append((f['name'],st.st_size,st.st_mtime_ns,st.st_ctime_ns))
    cache_key=(str(directory),source.sha256)
    if _VERIFIED_MODELS.get(cache_key)==signature:return directory
    names=set()
    for f in value['files']:
        name=f['name'];require(Path(name).name==name and name not in ('.','..') and name not in names,'Invalid model filename')
        names.add(name);path=directory/name
        require(path.is_file() and not path.is_symlink(),'Missing or linked model file')
        with path.open('rb') as handle: digest=hashlib.file_digest(handle,'sha256').hexdigest()
        require(digest==f['sha256'],'Model file changed: '+name)
    require({'config.json','model.safetensors','modeling_paddleocr_vl.py','processing_paddleocr_vl.py'}<=names,
            'Incomplete local model')
    require({f.name for f in directory.iterdir() if f.is_file()}<=names,'Unfingerprinted model file')
    _VERIFIED_MODELS.clear();_VERIFIED_MODELS[cache_key]=signature
    return directory


def ocr_reading(roots,work,page,region,settings):
    from ..receipts import read_origin
    row,result,_=read_origin(roots,work,settings['ocr_origin'],page)
    require(result.recipe.extractor=='ocr' and result.region==region,'OCR/model crop binding differs')
    require('character_reading' in row.get('feature',{}),'Character OCR required for token comparison')
    return row['feature']['character_reading']


def validate_inputs(roots,work,task,recipe):
    from ..runner import page_from_record
    from ..contracts import Region
    settings=recipe.record()['parameters']['settings']
    if 'ocr_origin' in settings:ocr_reading(roots,work,page_from_record(task['page']),Region(**task['region']),settings)
    model_directory(roots,recipe)


class Encoder:
    """One model per bounded extraction worker, never a background service."""
    def __init__(self,directory,settings,temporary):
        started=time.monotonic()
        os.environ['HF_HUB_OFFLINE']='1';os.environ['TRANSFORMERS_OFFLINE']='1'
        os.environ['HF_HUB_DISABLE_TELEMETRY']='1';os.environ['HF_MODULES_CACHE']=str(temporary/'hf-modules')
        os.environ['TOKENIZERS_PARALLELISM']='false';os.environ['MPLCONFIGDIR']=str(temporary/'mpl')
        with offline_inference():
            import torch
            from transformers import AutoModelForCausalLM,AutoProcessor
            torch.set_num_threads(settings['threads']);torch.backends.mkldnn.enabled=False
            self.processor=AutoProcessor.from_pretrained(str(directory),trust_remote_code=True,local_files_only=True,use_fast=False)
            self.processor.image_processor.max_pixels=settings['max_pixels']
            self.processor.image_processor.min_pixels=min(147456,settings['max_pixels'])
            self.model=AutoModelForCausalLM.from_pretrained(str(directory),trust_remote_code=True,
                local_files_only=True,torch_dtype=torch.float32,attn_implementation='eager').eval()
        self.load_seconds=time.monotonic()-started

    def recognize(self,path,settings):
        from PIL import Image
        import torch
        with offline_inference():
            start=time.monotonic()
            with Image.open(path) as original:image=original.convert('RGB')
            prompt='Formula Recognition:' if settings['kind']=='formula' else 'Table Recognition:'
            inputs=self.processor.apply_chat_template([{'role':'user','content':[
                {'type':'image','image':image},{'type':'text','text':prompt}]}],
                tokenize=True,add_generation_prompt=True,return_dict=True,return_tensors='pt')
            prepared=time.monotonic()
            with torch.inference_mode():
                output=self.model.generate(**inputs,max_new_tokens=settings['max_new_tokens'],do_sample=False,use_cache=True)
            generated=time.monotonic();tokens=output[0,inputs['input_ids'].shape[1]:]
            raw=self.processor.decode(tokens,skip_special_tokens=True);image.close()
            return raw,len(tokens),{'input_preparation_seconds':prepared-start,'generation_seconds':generated-prepared}


def extract(path,page,transform,roots,recipe,temporary,key,*,encoders=None,work='artifacts'):
    from .scientific_tokens import compare_readings,quantity_route
    settings=recipe.record()['parameters']['settings'];extra=[]
    feature={'kind':settings['kind'],'selection_basis':settings['selection_basis'],
             'review_required':True,'metadata_adopted':False,'literal_ocr_replaced':False,
             'handwriting_flag':settings['handwriting'],'word_coordinates':None}
    if settings['handwriting']:
        feature.update(raw_output=None,recognition_skipped='Handwritten additions require separate review')
        return (encoded(feature)+'\n').encode(),[],feature,extra
    reading=None
    if 'ocr_origin' in settings:
        from ..receipts import read_origin
        _,result,_=read_origin(roots,work,settings['ocr_origin'],page)
        reading=ocr_reading(roots,work,page,result.region,settings)
        ocr_raw=' '.join(w['text'] for w in reading['words'])
        q=quantity_route(ocr_raw)
        from .ruled_table import classify_document
        routing=classify_document(ocr_raw)
        if q or routing['kind']=='form':
            feature.update(raw_output=None,recognition_skipped='quantity/form stays in text path',
                           text_route=q or routing)
            return (encoded(feature)+'\n').encode(),[],feature,extra
    directory=model_directory(roots,recipe)
    identity=('scientific-region',settings['model_manifest']['sha256'],settings['threads'],settings['max_pixels'])
    encoder=encoders.get(identity) if encoders is not None else None;loaded=encoder is None
    if loaded:
        if encoders is not None:encoders.clear()
        encoder=Encoder(directory,settings,temporary)
        if encoders is not None:encoders[identity]=encoder
    raw,count,timing=encoder.recognize(path,settings)
    timing.update(model_load_seconds=encoder.load_seconds if loaded else 0.,model_loaded_this_crop=loaded)
    feature.update(raw_output=raw,generated_tokens=count,token_limit_reached=count>=settings['max_new_tokens'],timing=timing)
    if settings['kind']=='formula':
        feature['latex']=raw
        if reading:
            chars=[c for w in reading['words'] for c in w['characters']]
            evidence={i:c['below_baseline'] for i,c in enumerate(chars)}
            feature['ocr_origin']=settings['ocr_origin']
            feature['token_comparison']=compare_readings(''.join(c['text'] for c in chars),raw,subscript_evidence=evidence)
        if settings.get('render_check'):
            try:
                rendered=render_formula(raw);extra.append((key+'.formula-render.png',rendered,'formula-render-proposal'))
                feature['render_check']=formula_similarity(path,rendered)
            except (ValueError,RuntimeError) as error:
                feature['render_check']={'review_required':True,'renderable':False,'reason':str(error)[:500]}
    else:feature['table']=table_proposal(raw)
    return (encoded(feature)+'\n').encode(),[],feature,extra
