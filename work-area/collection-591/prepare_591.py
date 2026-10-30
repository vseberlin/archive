#!/usr/bin/env python3
"""Capture and compare Collection 591 evidence in one existing work directory."""
import csv, hashlib, json, os, re, sqlite3, subprocess, sys, time, urllib.request, urllib.parse
from pathlib import Path
from collections import Counter
from PIL import Image, ImageOps

ROOT=Path(__file__).resolve().parent
SOURCE=Path('/media/vladimir/Elements/restore/archive')
DOC_INDEX=Path('/mnt/volume/iss-archive-work/document-index/document-index.sqlite')
VIS_INDEX=Path('/home/vladimir/archive-visual-index/visual-index.sqlite')

def guard():
    r=json.loads(subprocess.check_output(['findmnt','-J','-T',str(SOURCE),'-o','SOURCE,TARGET,FSTYPE,OPTIONS']))['filesystems']
    assert len(r)==1 and r[0]['source']=='/dev/sdf1' and r[0]['target']=='/media/vladimir/Elements' and r[0]['fstype']=='ntfs3' and 'ro' in r[0]['options'].split(',')
    return r[0]

def write_csv(name,rows):
    assert rows
    with (ROOT/name).open('w',newline='') as f:
        w=csv.DictWriter(f,fieldnames=list(rows[0]),quoting=csv.QUOTE_ALL);w.writeheader();w.writerows(rows)

def write_json(name,data): (ROOT/name).write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n')
def read_csv(name):
    p=ROOT/name
    if not p.exists():return []
    with p.open() as f:return list(csv.DictReader(f))
def run(args,timeout=60):
    p=subprocess.run(args,capture_output=True,timeout=timeout,env={**os.environ,'OMP_THREAD_LIMIT':'1'})
    if p.returncode:raise RuntimeError(p.stderr.decode(errors='replace')[:400])
    return p.stdout

def inspect(path,key,ocr=False):
    cached=ROOT/(key+'-technical.json')
    if cached.exists():
        previous=json.loads(cached.read_text())
        if previous['state']=='readable':return previous
        if 'tesseract' in previous.get('error','') and Path(previous['preview_path']).exists():
            previous['text_sample']=run(['tesseract',previous['preview_path'],'stdout','-l','deu+eng','--psm','3'],45).decode(errors='replace')[:16000]
            previous.update(state='readable',error='',inspection=previous['inspection']+'; partial first-page OCR retried with one OpenMP thread')
            write_json(cached.name,previous);return previous
    r={'state':'readable','pages':1,'width':0,'height':0,'min_ppi':'','median_ppi':'','text_sample':'','inspection':'','preview_path':str(ROOT/(key+'-review.jpg')),'error':''}
    try:
        if path.suffix.lower()=='.pdf':
            info=run(['pdfinfo',str(path)]).decode(errors='replace')
            r['pages']=int(re.search(r'^Pages:\s+(\d+)',info,re.M)[1]);r['inspection']='pdfinfo; embedded raster resolution; first three pages internal text'
            listing=run(['pdfimages','-list',str(path)]).decode(errors='replace')
            ppi=[]
            for line in listing.splitlines():
                t=line.split()
                if len(t)>=14 and t[0].isdigit() and t[2]=='image':
                    try:ppi.append(min(float(t[12]),float(t[13])))
                    except ValueError:pass
            if ppi:r['min_ppi']=min(ppi);r['median_ppi']=sorted(ppi)[len(ppi)//2]
            r['text_sample']=run(['pdftotext','-f','1','-l',str(min(3,r['pages'])),'-layout',str(path),'-']).decode(errors='replace')[:16000]
            Path(r['preview_path']).write_bytes(run(['pdftoppm','-f','1','-l','1','-singlefile','-scale-to','1400','-jpeg',str(path)],120))
        else:
            with Image.open(path) as im:
                im=ImageOps.exif_transpose(im);r['width'],r['height']=im.size;im.load();im.thumbnail((1400,1400));im.convert('RGB').save(r['preview_path'],quality=85)
            r['inspection']='Pillow full image decode; native dimensions'
        if ocr and len(re.sub(r'\W','',r['text_sample']))<80:
            try:
                r['text_sample']=run(['tesseract',r['preview_path'],'stdout','-l','deu+eng','--psm','3'],45).decode(errors='replace')[:16000]
                r['inspection']+='; partial first-page OCR (review evidence only)'
            except subprocess.TimeoutExpired:
                r['inspection']+='; partial OCR timed out; visual/PDF decode remains readable'
        with Image.open(r['preview_path']) as im:im.verify()
    except Exception as e:r['state']='error';r['error']=str(e)
    write_json(cached.name,r);return r

def capture():
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from threading import Lock
    guard();data=json.loads((ROOT/'md-export.json').read_text());objects=list(data['results']['objects'].values());assert len(objects)==data['results']['total']==68
    existing={r['resource_id']:r for r in read_csv('md-resources.csv')};rows=[]
    throttle=Lock();last_request=[0.0]
    def fetch_one(obj,resource):
        rid=str(resource['quell_id']);oid=str(obj['object_id']);key=f'md-{oid}-resource-{rid}'
        name=resource['filename_loc'];url=name if name.startswith(('https://','http://')) else 'https://asset.museum-digital.org/berlin/'+urllib.parse.quote(resource['folder'].strip('/')+'/'+name)
        suffix='.pdf' if resource['type']=='text' else Path(urllib.parse.urlparse(name).path).suffix.lower()
        path=ROOT/(key+suffix)
        old=existing.get(rid)
        if old and path.exists() and path.stat().st_size==int(old['size_bytes']):sha=old['sha256']
        else:
            last=None
            for attempt in range(3):
                try:
                    with throttle:
                        time.sleep(max(0,.6-(time.monotonic()-last_request[0])))
                        last_request[0]=time.monotonic()
                    with urllib.request.urlopen(urllib.request.Request(url,headers={'User-Agent':'ISS archive discovery; contact: Industriesalon Schoeneweide'}),timeout=50) as response:payload=response.read()
                    path.write_bytes(payload);sha=hashlib.sha256(payload).hexdigest();break
                except Exception as error:last=error;time.sleep(1+attempt)
            else:raise last
            time.sleep(.6)
        tech=inspect(path,key,ocr=True)
        return {'md_object_id':oid,'inventory':obj['object_inventory_number'],'resource_id':rid,'sequence':str(resource.get('order','')),'type':resource['type'],'description':resource.get('beschreibung',''),'source_url':url,'work_path':str(path),'size_bytes':str(path.stat().st_size),'sha256':sha,'pages':str(tech['pages']),'width':str(tech['width']),'height':str(tech['height']),'median_ppi':str(tech['median_ppi']),'state':tech['state'],'preview_path':tech['preview_path'],'technical_path':str(ROOT/(key+'-technical.json')),'rights':resource.get('rights',''),'owner':resource.get('owner',''),'deletion_authorized':'false'}
    jobs=[(o,r) for o in objects for r in sorted(o.get('object_images',[]),key=lambda x:int(x.get('order') or 0))]
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures=[pool.submit(fetch_one,o,r) for o,r in jobs]
        for future in as_completed(futures):
            rows.append(future.result())
            rows.sort(key=lambda r:(int(r['md_object_id']),int(r['sequence'])))
            write_csv('md-resources.csv',rows)
            print(f'MD resources captured and inspected: {len(rows)}/109',flush=True)
    write_json('capture-summary.json',{'objects':len(objects),'resources':len(rows),'types':dict(Counter(r['type'] for r in rows)),'errors':[r['resource_id'] for r in rows if r['state']!='readable'],'source_guard':guard(),'accession_performed':False,'deletion_authorized':False})

if __name__=='__main__':capture()
