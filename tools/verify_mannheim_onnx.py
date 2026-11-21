#!/usr/bin/env python3
"""Check exported synthetic fixtures with the qualified local ONNX runtime.

PYTHONPATH=src <qualified-python> tools/verify_mannheim_onnx.py --inputs ... --report ...
Does not import Kraken/PyTorch or read archive images.
"""
import argparse
import hashlib
import importlib.metadata
import json
from pathlib import Path
import resource
import time
import sys


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--inputs',type=Path,required=True)
    parser.add_argument('--report',type=Path,required=True)
    parser.add_argument('--prefix',choices=['mannheim','mannheim-baseline'],default='mannheim')
    args=parser.parse_args()
    prefix=args.prefix
    import numpy as np
    import onnxruntime as ort
    from PIL import Image
    from archive_research.extractors.handwriting import ctc_decode
    from archive_research.extractors.mannheim_onnx import prepare,validate_config
    proof=json.loads((args.inputs/(prefix+'-export-proof.json')).read_text())
    config=json.loads((args.inputs/(prefix+'-config.json')).read_text());validate_config(config)
    model=args.inputs/(prefix+'.onnx')
    assert hashlib.sha256(model.read_bytes()).hexdigest()==proof['onnx_sha256']
    options=ort.SessionOptions();options.intra_op_num_threads=1;options.inter_op_num_threads=1
    session=ort.InferenceSession(str(model),sess_options=options,providers=['CPUExecutionProvider'])
    start=time.monotonic();records=[]
    for item in proof['fixtures']:
        name=item['name']
        assert Path(name).name==name
        with Image.open(args.inputs/(name+'.png')) as image, np.load(args.inputs/(name+'.npz'),allow_pickle=False) as fixture:
            pixels=prepare(image,config);reference=fixture['logits']
            np.testing.assert_array_equal(pixels,fixture['pixels'])
            logits=session.run(['logits'],{'image':pixels})[0]
            def probabilities(x):
                e=np.exp(x-x.max(-1,keepdims=True));return e/e.sum(-1,keepdims=True)
            logit_error=float(np.max(np.abs(logits-reference)))
            probability_error=float(np.max(np.abs(probabilities(logits)-probabilities(reference))))
            assert logit_error<=proof['tolerance']['max_absolute_logit_error']
            assert probability_error<=proof['tolerance']['max_absolute_probability_error']
            text=ctc_decode(logits.argmax(-1)[0],config['alphabet'])
            assert text==item['greedy_text']
            record={'name':name,'pixels_exact':True,'text_exact':True,'input_shape':list(pixels.shape),
                    'max_logit_error':logit_error,'max_probability_error':probability_error,
                    'strict_tolerance_passed':logit_error<=0.01 and probability_error<=0.0001}
            records.append(record);print(json.dumps(record),flush=True)
    result={'status':'passed','qualification':proof.get('qualification','strict synthetic fixture tolerance'),
            'strict_tolerance_passed':all(r['strict_tolerance_passed'] for r in records),'onnx_sha256':proof['onnx_sha256'],'fixtures':records,
            'versions':{n:importlib.metadata.version(n) for n in ('numpy','onnxruntime','Pillow','scipy')},
            'elapsed_seconds':time.monotonic()-start,'max_rss_kib':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
            'threads':1,'archive_images_uploaded':False,'kraken_or_torch_imported':any(n in sys.modules for n in ('kraken','torch'))}
    assert not result['kraken_or_torch_imported']
    args.report.write_text(json.dumps(result,indent=2)+'\n')


if __name__=='__main__':main()
