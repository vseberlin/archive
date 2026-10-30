"""Read-only source comparison; writes review evidence only in this work directory."""
import csv, json, math, re, sqlite3, subprocess, sys
from pathlib import Path
from collections import defaultdict, Counter
from datetime import datetime, timezone
import numpy as np
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from PIL import Image, ImageOps, ImageDraw, ImageFont
from scipy.fft import dctn

W=Path(__file__).parent
U=Path('/mnt/volume/iss-archive/units/paragon')
S=Path('/media/vladimir/Elements/restore/archive')
KEYS=['phash-dct-64-v2','dhash-64-v2','ahash-64-v2']
def readcsv(p): return list(csv.DictReader(p.open()))
def dump(p,v): p.write_text(json.dumps(v,ensure_ascii=False,indent=2)+'\n')
def guard():
    f=subprocess.check_output(['findmnt','-rn','-T',str(S),'-o','SOURCE,TARGET,FSTYPE,OPTIONS'],text=True).split()
    assert f[:3]==['/dev/sdf1','/media/vladimir/Elements','ntfs3'] and 'ro' in f[3].split(',')
@lru_cache(maxsize=1000)
def preview(p,size=600):
    with Image.open(p) as im:
        im.draft('RGB',(size,size))
        im=ImageOps.exif_transpose(im)
        im.thumbnail((size,size))
        return im.convert('RGB')
def image_feature(im):
    im=im.convert('L')
    def bits(a): return format(int(''.join('1' if b else '0' for b in a.flat),2),'016x')
    a=np.asarray(im.resize((8,8),Image.Resampling.LANCZOS),dtype=float)
    d=np.asarray(im.resize((9,8),Image.Resampling.LANCZOS),dtype=float)
    q=dctn(np.asarray(im.resize((32,32),Image.Resampling.LANCZOS),dtype=float),type=2,norm='ortho')[:8,:8]
    return dict(zip(KEYS,[bits(q>np.median(q.flat[1:])),bits(d[:,1:]>d[:,:-1]),bits(a>a.mean())]))
def feature(p): return image_feature(preview(p,64))
def distance(a,b): return [(int(a[k],16)^int(b[k],16)).bit_count() for k in KEYS]
def refine():
    guard();d=json.loads((W/'retained-visual-comparison.json').read_text());refs=d['references']
    for r in refs:
        im=preview(r['path'],64)
        r['rotation_features']=[image_feature(im.rotate(a,expand=True)) for a in [0,90,180,270]]
    for f in d['families']:
        for g in f['groups']:
            near=[]
            for r in refs:
                dd=min((distance(g['features'],v) for v in r['rotation_features']),key=sum)
                near.append(dict(inventory=r['inventory'],path=r['path'],distance=dd,score=sum(dd),title=r['title']))
            near.sort(key=lambda n:n['score']);g['nearest']=near[:5]
            exact=[r for r in refs if r['inventory'] in g['inventories'] or r['inventory']==Path(g['paths'][0]).stem]
            g['catalogue_counterparts']=[dict(inventory=r['inventory'],path=r['path'],title=r['title']) for r in exact]
        f['groups'].sort(key=lambda g:(not any(Path(p).suffix.lower() in ['.jpg','.jpeg'] for p in g['paths']),-(g['width']*g['height'])))
        f['display_path']=next((p for p in f['groups'][0]['paths'] if Path(p).suffix.lower() in ['.jpg','.jpeg']),f['groups'][0]['paths'][0])
    d['families'].sort(key=lambda f:(-f['groups'][0]['nearest'][0]['score'],f['display_path']))
    for i,f in enumerate(d['families']):f['review_row']=i+1
    dump(W/'retained-visual-comparison.json',d)
    print('Rotation-aware comparison refined',flush=True)
def prepare():
    guard()
    rr=readcsv(W/'retain-review.csv'); ev={r['source_path']:r for r in json.loads((W/'drain-evidence.json').read_text())}
    vs=json.loads((W/'retained-visual-index-evidence.json').read_text())
    for r in rr:
        st=Path(r['source_path']).stat(); e=ev[r['source_path']]
        assert (st.st_size,st.st_mtime_ns)==(e['source_size_bytes'],e['source_mtime_ns'])
    refs=[]
    for o in readcsv(U/'metadata/objects.csv'):
        for p in o['access_file'].splitlines():
            if not p: continue
            refs.append(dict(inventory=o['archival_label'],path=str(U/p),title=o['descriptive_title'],description=o['description'],year=o['date_year'],features=feature(U/p)))
    byhash=defaultdict(list)
    nonimages=[]
    for r in rr:
        if Path(r['source_path']).suffix.lower() not in ['.jpg','.jpeg','.tif','.tiff','.png']:nonimages.append(r);continue
        byhash[r['source_sha256']].append(r)
    groups=[];errors=[]
    for sha,rs in byhash.items():
        p=rs[0]['source_path']
        try:
            v=vs.get(p)
            if not v:
                with Image.open(p) as im: width,height=im.size
                v=dict(width=width,height=height,features=feature(p))
            near=[]
            for ref in refs:
                dd=distance(v['features'],ref['features'])
                near.append(dict(inventory=ref['inventory'],path=ref['path'],distance=dd,score=sum(dd),title=ref['title']))
            near.sort(key=lambda x:(x['score'],x['distance'][0]))
            groups.append(dict(sha256=sha,paths=[r['source_path'] for r in rs],inventories=sorted({r['inventory'] for r in rs if r['inventory']}),width=v['width'],height=v['height'],features=v['features'],nearest=near[:5]))
        except Exception as e: errors.append(dict(paths=[r['source_path'] for r in rs],error=str(e)))
    # Loose display families only: every byte-distinct member stays in the evidence.
    families=[]
    for g in groups:
        found=None
        for f in families:
            r=f['groups'][0]
            if abs(g['width']/g['height']-r['width']/r['height'])>.025:continue
            d=distance(g['features'],r['features'])
            if d[0]<=4 and d[1]<=6 and d[2]<=6:found=f;break
        if found is None: families.append(dict(groups=[g]))
        else: found['groups'].append(g)
    # Put likely new or differently framed content first.
    families.sort(key=lambda f:(-f['groups'][0]['nearest'][0]['score'],f['groups'][0]['paths'][0]))
    for i,f in enumerate(families): f['review_row']=i+1
    dump(W/'retained-visual-comparison.json',dict(checked_at=datetime.now(timezone.utc).isoformat(),retained_paths=len(rr),unique_image_hashes=len(byhash),families=families,references=refs,nonimages=nonimages,errors=errors,method='Stored source hashes with size/mtime validation; existing perceptual hashes, with Pillow feature fallback. Similarity and display families are discovery aids, not identity or redundancy decisions.'))
    print(dict(paths=len(rr),hashes=len(byhash),families=len(families),errors=errors,references=len(refs)),flush=True)

FONT=ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',15)
SMALL=ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',12)
def box(canvas,p,x,y,w,h):
    try:
        im=preview(p,max(w,h)); im.thumbnail((w,h));canvas.paste(im,(x+(w-im.width)//2,y+(h-im.height)//2))
    except Exception as e: ImageDraw.Draw(canvas).text((x+5,y+20),'DECODE ERROR '+str(e)[:65],font=SMALL,fill='red')
def sheets():
    guard();data=json.loads((W/'retained-visual-comparison.json').read_text())
    families=data['families'];page_size=20
    def render(page):
        subset=families[page*page_size:(page+1)*page_size]
        canvas=Image.new('RGB',(1440,1850),'white');dr=ImageDraw.Draw(canvas)
        dr.text((12,10),f'Paragon retained review {page+1}: LEFT source / RIGHT nearest archive image (similarity is only a lead)',font=FONT,fill='black')
        for j,f in enumerate(subset):
            g=f['groups'][0]; x=(j%4)*360; y=45+(j//4)*360
            p=f.get('display_path',g['paths'][0]); near=g['nearest'][0]
            if g.get('catalogue_counterparts'): near=dict(g['catalogue_counterparts'][0],distance='catalogue label')
            dr.rectangle((x,y,x+359,y+355),outline='#aaa')
            dr.text((x+5,y+4),f"{f['review_row']:03}  {Path(p).name[:29]}",font=FONT,fill='black')
            dr.text((x+5,y+25),f"{len(f['groups'])} hashes / {sum(len(t['paths']) for t in f['groups'])} paths; {g['width']}x{g['height']}",font=SMALL,fill='#444')
            box(canvas,p,x+4,y+46,173,240);box(canvas,near['path'],x+182,y+46,173,240)
            dr.text((x+5,y+290),str(Path(p).parent.name)[:46],font=SMALL,fill='#444')
            dr.text((x+5,y+311),f"Nearest: {near['inventory']}; distances {near['distance']}",font=SMALL,fill='black')
        path=W/f'retained-comparison-{page+1:02}.jpg';canvas.save(path,quality=90)
        print(path,flush=True)
    with ThreadPoolExecutor(max_workers=4) as pool:list(pool.map(render,range(math.ceil(len(families)/page_size))))
if __name__=='__main__':
    {'prepare':prepare,'refine':refine,'sheets':sheets}[sys.argv[1]]()
