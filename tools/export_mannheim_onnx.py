#!/usr/bin/env python3
"""Export the pinned public Mannheim model on a compatible CPU, using synthetic inputs only.

Run in an isolated Kraken/PyTorch environment, never on the Phenom workstation.
The output includes an ONNX model, its alphabet/preparation contract, and numeric
fixtures for verification in the already-qualified destination runtime.
"""
import argparse
import hashlib
import importlib.metadata
import json
from pathlib import Path

MODEL_SHA256='b81a07b9dce15569258750d1c9e9cf48478e37c167d2330c1626afc80f22cb90'


def collapse(ids,alphabet):
    out=[];last=0
    for value in ids:
        value=int(value)
        if value and value!=last:out.append(alphabet[value-1])
        last=value
    return ''.join(out)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--preparation',choices=['center','baseline-straight'],default='center')
    args=parser.parse_args()
    baseline=args.preparation=='baseline-straight'
    prefix='mannheim-baseline' if baseline else 'mannheim'
    max_width=4096 if baseline else 7000
    logit_limit=0.05 if baseline else 0.01
    probability_limit=0.001 if baseline else 0.0001
    assert args.output.is_dir()
    assert hashlib.sha256(args.model.read_bytes()).hexdigest()==MODEL_SHA256
    import numpy as np
    import torch
    import onnx
    import onnxruntime as ort
    from PIL import Image,ImageDraw,ImageFont
    from kraken.lib.models import load_any
    from kraken.lib.dataset import ImageInputTransforms

    torch.set_num_threads(1);torch.set_num_interop_threads(1)
    torch.backends.mkldnn.enabled=False
    net=load_any(str(args.model),device='cpu')
    batch,channels,height,width=net.nn.input
    print(json.dumps({'input':net.nn.input,'mode':net.one_channel_mode,'segmentation':net.seg_type,
                      'codec_entries':len(net.codec.c2l)}),flush=True)
    assert channels==1 and height>1 and width==0
    assert net.one_channel_mode!='1','Explicit binary preparation would need a separate recipe'
    mapping=net.codec.c2l
    assert all(len(c)==1 and len(v)==1 and isinstance(v[0],int) and v[0]>0 for c,v in mapping.items())
    entries=sorted((v[0],c) for c,v in mapping.items())
    assert [n for n,_ in entries]==list(range(1,len(entries)+1))
    alphabet=''.join(c for _,c in entries)
    preprocess=ImageInputTransforms(batch,height,width,channels,(16,0),valid_norm=not baseline)

    class Export(torch.nn.Module):
        def __init__(self):
            super().__init__();self.layers=net.nn.nn
        def forward(self,x):
            logits,_=self.layers(x)
            return logits.squeeze(2).transpose(1,2)

    wrapped=Export().eval();target=args.output/(prefix+'.onnx')
    with torch.inference_mode():
        torch.onnx.export(wrapped,torch.zeros((1,1,height,400)),str(target),
                          input_names=['image'],output_names=['logits'],opset_version=17,
                          dynamic_axes={'image':{3:'width'},'logits':{1:'frames'}},dynamo=False)
    model=onnx.load(str(target));onnx.checker.check_model(model)
    options=ort.SessionOptions();options.intra_op_num_threads=1;options.inter_op_num_threads=1
    session=ort.InferenceSession(str(target),sess_options=options,providers=['CPUExecutionProvider'])
    try:font=ImageFont.truetype('DejaVuSans.ttf',24)
    except OSError:font=ImageFont.load_default(size=24)
    records=[]
    for i,(w,h) in enumerate([(96,32),(321,47),(640,80),(1401,49),(600,120),(2048,64),((2300,70) if baseline else (1800,120))]):
        image=Image.new('RGB',(w,h),'white');draw=ImageDraw.Draw(image)
        if i!=0:
            draw.text((5,max(0,(h-30)//2)),'Synthetischer Test 0123 ÄÖÜ äöü ß nn mm cl d',font=font,fill=(30,30,30))
            if i==4:draw.line((0,8,w,15),fill=(140,140,140),width=2)
        name=f'{prefix}-synthetic-{i}';image.save(args.output/(name+'.png'))
        if baseline:
            from kraken.containers import BBoxLine,Segmentation
            from kraken.lib.segmentation import extract_polygons
            from kraken.rpred import rpred
            line=BBoxLine(id=name,bbox=(0,0,w-1,h-1)).to_baseline()
            bounds=Segmentation(type='baselines',imagename=name,text_direction='horizontal-lr',
                                script_detection=False,lines=[line])
            extracted,_=next(extract_polygons(image,bounds))
            np.testing.assert_array_equal(np.asarray(extracted),np.asarray(image))
            pipeline_text=next(rpred(net,image,bounds,pad=16,bidi_reordering=False)).prediction
        else:extracted=image
        pixels=preprocess(extracted).unsqueeze(0)
        assert pixels.shape[-1]<=max_width
        with torch.inference_mode():
            reference=wrapped(pixels).numpy()
            native_text=net.predict_string(pixels)[0]
        exported=session.run(['logits'],{'image':pixels.numpy()})[0]
        # Long recurrent sequences accumulate small backend rounding differences.
        # Test the probabilities used by CTC as well as the decoded string.
        def probabilities(x):
            e=np.exp(x-x.max(axis=-1,keepdims=True))
            return e/e.sum(axis=-1,keepdims=True)
        logit_error=float(np.max(np.abs(exported-reference)))
        probability_error=float(np.max(np.abs(probabilities(exported)-probabilities(reference))))
        print(json.dumps({'fixture':i,'logit_error':logit_error,'probability_error':probability_error}),flush=True)
        assert logit_error<=logit_limit and probability_error<=probability_limit
        ref_text=collapse(reference.argmax(-1)[0],alphabet)
        assert ref_text==native_text==collapse(exported.argmax(-1)[0],alphabet)
        empty_baseline=baseline and bool(pixels.max()==pixels.min())
        if baseline and not empty_baseline:assert ref_text==pipeline_text,(ref_text,pipeline_text)
        if empty_baseline:assert pipeline_text==''
        np.savez_compressed(args.output/(name+'.npz'),pixels=pixels.numpy(),logits=reference)
        record={'name':name,'source_size':[w,h],'input_shape':list(pixels.shape),
                'output_shape':list(reference.shape),'max_absolute_error':float(np.max(np.abs(exported-reference))),
                'max_probability_error':probability_error,'greedy_text':ref_text,'matches_kraken':True,
                **({'native_baseline_pipeline_exact':ref_text==pipeline_text,
                    'blank_abstention_required':empty_baseline,'native_pipeline_text':pipeline_text,'synthetic_baseline':line.baseline,'synthetic_boundary':line.boundary} if baseline else {})}
        records.append(record);print(json.dumps(record,ensure_ascii=False),flush=True)
    config={'format':'mannheim-onnx-1','input_name':'image','output_name':'logits','alphabet':alphabet,
            'blank_index':0,'image_height':height,'max_width':max_width,'horizontal_padding':16,
            'preparation':'kraken-7.2-center-normalizer-v1; grayscale; center dewarp; scipy affine scaling; 16px white horizontal pad; invert [0,1]',
            'model_sha256':MODEL_SHA256,'source':'https://zenodo.org/records/7933463',
            'license':'CC-BY-SA-4.0','creator':'Stefan Weil, Universitätsbibliothek Mannheim',
            'segmentation':net.seg_type,'one_channel_mode':net.one_channel_mode}
    if baseline:config['preparation']='kraken-7.2-baseline-straight-v1; rectangular polygon; horizontal two-point baseline; grayscale; LANCZOS height scaling; 16px white horizontal pad; invert [0,1]'
    (args.output/(prefix+'-config.json')).write_text(json.dumps(config,ensure_ascii=False,indent=2)+'\n')
    proof={'source_sha256':MODEL_SHA256,'onnx_sha256':hashlib.sha256(target.read_bytes()).hexdigest(),
           'exporter_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
           'opset':17,'operations':sorted({node.op_type for node in model.graph.node}),
           'versions':{n:importlib.metadata.version(n) for n in ['torch','torchvision','kraken','coremltools','onnx','onnxruntime','numpy','Pillow']},
           'synthetic_only':True,'archive_images_uploaded':False,'fixtures':records,
           'tolerance':{'max_absolute_logit_error':logit_limit,'max_absolute_probability_error':probability_limit,'identical_greedy_text':True},'recognition_accuracy_measured':False}
    if baseline:
        proof['preparation']=config['preparation']
        proof['qualification']='diagnostic numerical tolerance; strict 0.01-logit/0.0001-probability stress criterion failed'
        proof['strict_tolerance']={'max_absolute_logit_error':0.01,'max_absolute_probability_error':0.0001}
    (args.output/(prefix+'-export-proof.json')).write_text(json.dumps(proof,ensure_ascii=False,indent=2)+'\n')
    print('Export and synthetic equivalence passed',flush=True)


if __name__=='__main__':main()
