"""Pinned Mannheim input preparation without importing Kraken or PyTorch.

Center normalization adapted from Kraken 7.2, kraken/lib/lineest.py and
ImageInputTransforms. Kraken contributors, Apache-2.0; see ../kraken-LICENSE.txt.
Changes: functional interface, bounded input/output, PIL/NumPy tensor conversion.
This preserves upstream normalization, including its blank-image behaviour;
it is not a blank detector or an accuracy qualification.
"""
import warnings

from ..contracts import require

MODEL_SHA256='b81a07b9dce15569258750d1c9e9cf48478e37c167d2330c1626afc80f22cb90'
PREPARATION='kraken-7.2-center-normalizer-v1; grayscale; center dewarp; scipy affine scaling; 16px white horizontal pad; invert [0,1]'
BASELINE_PREPARATION='kraken-7.2-baseline-straight-v1; rectangular polygon; horizontal two-point baseline; grayscale; LANCZOS height scaling; 16px white horizontal pad; invert [0,1]'


def validate_config(config):
    require(config.get('format')=='mannheim-onnx-1' and config.get('image_height')==120 and
            config.get('max_width')==(4096 if config.get('preparation')==BASELINE_PREPARATION else 7000) and config.get('horizontal_padding')==16 and
            config.get('blank_index')==0 and config.get('model_sha256')==MODEL_SHA256 and
            config.get('preparation') in (PREPARATION,BASELINE_PREPARATION) and config.get('input_name')=='image' and
            config.get('output_name')=='logits','Unsupported Mannheim ONNX input contract')
    alphabet=config.get('alphabet')
    require(isinstance(alphabet,str) and len(alphabet)==294 and len(set(alphabet))==294,
            'Unsupported Mannheim alphabet')


def prepare(image,config):
    """Reproduce the pinned Kraken grayscale/center-normalization pipeline."""
    validate_config(config)
    import numpy as np
    from PIL import Image,ImageOps
    from scipy.ndimage import affine_transform,gaussian_filter,uniform_filter
    require(0<image.width<=20000 and 0<image.height<=2048 and image.width*image.height<=8000000,
            'Selected line exceeds bounded Mannheim source dimensions')
    if config['preparation']==BASELINE_PREPARATION:
        # A rectangular polygon with a horizontal two-point baseline takes
        # Kraken's zero-rotation fast path. Its extracted image equals the crop;
        # this is verified against extract_polygons and rpred on synthetic data.
        # No automatic baseline detection or curved-line rectification is implied.
        require(image.width>=6 and image.height>=2,'Straight baseline crop is too small')
        width=int(image.width*120/image.height)
        require(0<width and width+32<=config['max_width'],'Selected line exceeds qualified Mannheim input width')
        normalized=image.convert('L').resize((width,120),Image.Resampling.LANCZOS)
        normalized=ImageOps.expand(normalized,border=(16,0,16,0),fill=255)
        pixels=np.asarray(normalized,dtype=np.float32)*np.float32(1.0/255)
        return (pixels.max()-pixels)[None,None]
    line=np.asarray(image.convert('L'));h,w=line.shape
    ink=np.amax(line)-line
    with np.errstate(invalid='ignore'):
        ink=ink*1.0/np.amax(ink)
    smoothed=gaussian_filter(ink,(h*.5,h*1.0),mode='constant')
    smoothed+=.001*uniform_filter(smoothed,(h*.5,w),mode='constant')
    center=np.asarray(gaussian_filter(np.argmax(smoothed,axis=0),h*.3),'i')
    deltas=np.abs(np.arange(h)[:,None]-center[None,:])
    radius=int(1+4*np.mean(deltas[ink!=0]))
    fill=np.amax(line)
    padded=np.vstack([fill*np.ones((h,w)),line,fill*np.ones((h,w))])
    center=center+h
    dewarped=np.asarray([padded[center[i]-radius:center[i]+radius,i] for i in range(w)],dtype=np.float32).T
    if dewarped.shape[0]==0:dewarped=line
    scale=120.0/dewarped.shape[0];width=int(scale*w)
    require(0<width and width+32<=config['max_width'],'Selected line exceeds qualified Mannheim input width')
    with warnings.catch_warnings():
        warnings.simplefilter('ignore',UserWarning)
        scaled=affine_transform(1.0*dewarped,np.ones(2)/scale,order=1,
                                output_shape=(120,width),mode='constant',cval=fill)
    normalized=Image.fromarray(np.asarray(scaled,dtype=np.float32)).convert('L')
    normalized=ImageOps.expand(normalized,border=(16,0,16,0),fill=255)
    # torchvision ToDtype(scale=True) multiplies by 1/255 in float32.
    pixels=np.asarray(normalized,dtype=np.float32)*np.float32(1.0/255)
    return (pixels.max()-pixels)[None,None]
