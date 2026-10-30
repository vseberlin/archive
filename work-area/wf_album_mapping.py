"""Prepare the existing four album groups for a later WordPress media switch.

Uses a read-only WP Archivset capture, archive membership and existing page/detail
relationships. Writes mapping evidence in the existing work directory only.
"""
import csv
import hashlib
import io
import json
import re
import unicodedata
from collections import Counter,defaultdict
from difflib import SequenceMatcher
from pathlib import Path
from urllib.parse import unquote,urlparse,quote
from urllib.request import urlopen

W=Path('/mnt/volume/iss-archive-work/wf-alben')
U=Path('/mnt/volume/iss-archive/units/wf-alben')
SETS={'16':'ALB-001','17':'ALB-002','18':'ALB-003','19':'ALBUM-NEF'}

def read(p):
    with p.open(encoding='utf-8-sig',newline='') as f:return list(csv.DictReader(f))
def norm(s):return re.sub(r'[^a-z0-9]','',unicodedata.normalize('NFKD',s).encode('ascii','ignore').decode().lower())
def words(s):return re.sub(r'\W+',' ',unicodedata.normalize('NFKC',s).casefold()).strip()
def write(p,rows,fields=None):
    fields=fields or list(rows[0])
    with p.open('w',newline='') as f:
        wr=csv.DictWriter(f,fieldnames=fields,lineterminator='\n');wr.writeheader();wr.writerows(rows)

def compare_images(url,path):
    from PIL import Image,ImageOps,ImageChops,ImageStat
    import numpy as np
    with urlopen(url,timeout=15) as response: source=response.read()
    def thumb(file):
        with Image.open(file) as im:
            im=ImageOps.exif_transpose(im).convert('RGB')
            return im.resize((128,128)),im.size
    a,adim=thumb(io.BytesIO(source));b,bdim=thumb(path)
    def dh(im):
        pix=list(im.convert('L').resize((17,16)).getdata())
        return [pix[y*17+x]>pix[y*17+x+1] for y in range(16) for x in range(16)]
    # Low-frequency image comparison complements the edge test when the web
    # copy has different compression, tone or slight border cropping.
    transform=np.cos(np.pi*np.arange(8)[:,None]*(2*np.arange(32)[None,:]+1)/64)
    def ph(im):
        values=np.asarray(im.convert('L').resize((32,32)),dtype=float)
        coeff=(transform@values@transform.T).ravel()[1:]
        return coeff>np.median(coeff)
    ap=ph(a);ad=dh(a);comparisons=[]
    for cw in (0,90,180,270):
        rotated=b.rotate(-cw)
        dist=sum(x!=y for x,y in zip(ad,dh(rotated)))
        pd=int(np.sum(ap!=ph(rotated)))
        mae=sum(ImageStat.Stat(ImageChops.difference(a,rotated)).mean)/3
        comparisons.append((pd,dist,round(mae,2),cw))
    pd,dist,mae,cw=min(comparisons)
    return dist,mae,adim,bdim,pd,cw

def main():
    members=read(U/'metadata/album-memberships.csv')
    objects={r['record_key']:r for r in read(U/'metadata/objects.csv')}
    wp=json.loads((W/'wordpress-album-source.json').read_text())
    prior={r['wp_member_id']:r for r in read(W/'wordpress-album-mapping.csv')} if (W/'wordpress-album-mapping.csv').exists() else {}
    visual={r['wp_member_id']:r for r in json.loads((W/'album-visual-comparisons.json').read_text())} if (W/'album-visual-comparisons.json').exists() else {}
    maprows=[];used=set();images_checked=0
    for r in wp:
        if r['member_kind']!='object':continue
        album=SETS[r['set_id']]
        stem=Path(unquote(urlparse(r['member_source_url']).path)).stem
        candidates=[m for m in members if m['album_key']==album and norm(m['inventory'])==norm(stem)]
        basis='existing Archivset membership and exact normalized source filename/inventory'
        if not candidates and str(r.get('md_object_id','0')) not in ('0','','None'):
            candidates=[m for m in members if m['album_key']==album and m['md_object_id']==str(r['md_object_id'])]
            basis='existing Archivset membership and museum-digital object ID'
        if not candidates:
            # Contents/cover filenames have historically differed. Use the
            # existing descriptive text to offer candidates, never assign by order.
            caption=words(r.get('caption') or r.get('member_title') or '')
            scored=[]
            for m in members:
                if m['album_key']!=album or m['record_key'] not in objects:continue
                o=objects[m['record_key']]
                score=max(SequenceMatcher(None,caption,words(o.get(k,''))).ratio() for k in ('description','title','image_name'))
                scored.append((score,m))
            scored.sort(key=lambda v:v[0],reverse=True)
            if scored and scored[0][0]>.8 and (len(scored)==1 or scored[0][0]-scored[1][0]>.1):
                candidates=[scored[0][1]];basis=f'existing Archivset album and descriptive-text agreement ({scored[0][0]:.3f}); image check required'
        m=candidates[0] if len(candidates)==1 else {}
        parent=m.get('linked_record_key','') if m.get('member_kind')=='redundant_derived_record' else ''
        o=objects.get(m.get('record_key',''),{})
        asset=o.get('access_relative_path','')
        parent_asset=objects.get(parent,{}).get('access_relative_path','')
        status='matched' if m else 'ambiguous' if candidates else 'no_match_found'
        note=''
        dist=mae=pd=cw=''
        wpurl=''
        if r.get('attachment_file'):
            wpurl='http://192.168.2.31:8082/wp-content/uploads/'+quote(r['attachment_file'])
        if asset and wpurl:
            try:
                previous=prior.get(r['id'],{})
                if previous.get('match_status')=='matched' and previous.get('record_key')==m['record_key'] and previous.get('wp_image_url')==wpurl:
                    dist=int(previous['image_difference_bits_256']);mae=float(previous['image_mean_absolute_difference_255'])
                    pd=previous.get('image_frequency_difference_bits_63','');cw=previous.get('proposed_rotation_cw','0') or '0'
                else:
                    dist,mae,wd,ad,pd,cw=compare_images(wpurl,U/asset)
                images_checked+=1
                if (dist>20 and (pd=='' or int(pd)>8)) or mae>45:status='ambiguous';note='Filename/text candidate needs visual comparison; current and archive images differ.'
                else:note='Image-content comparison supports the link; perceptual comparison is not exact-byte proof. Rotation is a proposed display instruction only.'
            except Exception as e:status='ambiguous';note=type(e).__name__+': '+str(e)
        elif parent:
            note='Existing archive relationship links this cropped-detail record to its full page. No standalone crop access file is accessioned; keep its caption and source identity. Do not replace a crop URL with the full-page URL as if equivalent.'
        elif m:status='ambiguous';note='No current attachment or access file was available for the image check.'
        review=visual.get(r['id'],{})
        if review:
            assert review['record_key']==m.get('record_key') and review['wp_image_url']==wpurl
            assert review['archive_access_relative_path']==asset
            status='matched'
            cw=review['proposed_rotation_cw']
            note=review['comparison_note']
            basis+='; visual comparison of both images by assistant, 2026-09-12'
        if m:used.add(m['record_key'])
        original_label=''
        inventory_label=''
        label_note=''
        if album=='ALBUM-NEF' and m and not parent:
            title=o.get('title','')
            found=re.search(r'\bS\.?\s*(\d+)',title)
            if found:original_label='S. '+found.group(1)
            elif 'einband' in title.casefold():original_label='Einband'
            inventory_number=re.search(r'_Seite_(\d+)$',m['inventory'])
            if inventory_number:inventory_label='S. '+str(int(inventory_number.group(1)))
            if original_label.startswith('S. ') and inventory_label and original_label!=inventory_label:
                label_note='Metadata conflict: archive title says '+original_label+'; inventory says '+inventory_label+'. Both retained; no physical page-number verification.'
        maprows.append({'album_key':album,'wp_set_id':r['set_id'],'wp_member_id':r['id'],
            'wp_object_post_id':r['object_post_id'],'wp_object_key':r.get('wp_object_key',''),
            'wp_position':r['position'],'wp_page_label':r['page_label'],
            'wp_attachment_id':r.get('attachment_id',''),'wp_image_url':wpurl,
            'original_web_source_url':r['member_source_url'],'record_key':m.get('record_key',''),
            'inventory':m.get('inventory',''),'member_kind':m.get('member_kind',''),
            'parent_record_key':parent,'archive_access_relative_path':asset,
            'parent_access_relative_path':parent_asset,'archive_title_page_label':original_label,
            'inventory_page_label':inventory_label,'page_label_note':label_note,
            'caption':r.get('caption') or r.get('member_title',''),'match_status':status,
            'match_basis':basis if m else 'No unique match in the existing album membership and descriptive evidence.',
            'image_difference_bits_256':dist,'image_mean_absolute_difference_255':mae,'notes':note})
        maprows[-1].update({'image_frequency_difference_bits_63':pd,'proposed_rotation_cw':cw})
        maprows[-1]['web_presentation_note']=review.get('web_presentation_note','')
        print(album,r['position'],m.get('inventory','UNMATCHED'),status,dist,flush=True)
    write(W/'wordpress-album-mapping.csv',maprows)
    notshown=[dict(m,notes='Archive member has no unique existing WP image entry in the captured set. Reading position is not inferred.') for m in members if m['record_key'] not in used]
    write(W/'album-members-not-on-current-pages.csv',notshown, list(members[0])+['notes'])
    # The proposed reading sequence inherits the current order after details are
    # attached to their parent. It is explicitly not a completeness certification.
    sequence=[]
    for album in SETS.values():
        primary=[r for r in maprows if r['album_key']==album and r['member_kind']=='accessioned_image']
        for i,r in enumerate(primary,1):
            details=[d for d in maprows if d['parent_record_key']==r['record_key']]
            sequence.append({'album_key':album,'proposed_position':i,'record_key':r['record_key'],
                'inventory':r['inventory'],'archive_title_page_label':r['archive_title_page_label'],
                'inventory_page_label':r['inventory_page_label'],'page_label_note':r['page_label_note'],
                'proposed_page_label':r['inventory'] if r['page_label_note'] else (r['archive_title_page_label'] or r['inventory']),
                'archive_access_relative_path':r['archive_access_relative_path'],
                'proposed_rotation_cw':r['proposed_rotation_cw'],
                'detail_record_keys':json.dumps([d['record_key'] for d in details]),
                'wp_member_id':r['wp_member_id'],'match_status':r['match_status'],
                'order_basis':'Existing WP Archivset order, with separately catalogued details attached to their archived parent page.'})
    write(W/'album-reading-order.csv',sequence)
    summary={'wordpress_objects':len(maprows),'archive_members':len(members),'archive_images':len(objects),
             'mapping_status':dict(Counter(r['match_status'] for r in maprows)),
             'content_compared_images':images_checked,'archive_members_not_on_current_pages':len(notshown),
             'visually_resolved_comparisons':len(visual),
             'web_presentation_notes':sum(bool(r['web_presentation_note']) for r in maprows),
             'page_label_conflicts':sum(bool(r['page_label_note']) for r in maprows),
             'albums':{a:{'wp_objects':sum(r['album_key']==a for r in maprows),
                          'mapped_primary_images':sum(r['album_key']==a for r in sequence),
                          'linked_details':sum(r['album_key']==a and bool(r['parent_record_key']) for r in maprows)} for a in SETS.values()},
             'wordpress_changed':False,'archive_changed':False,'publication_authorized':False,
             'page_order_complete':False}
    (W/'album-mapping-result.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(summary),flush=True)

if __name__=='__main__':main()
