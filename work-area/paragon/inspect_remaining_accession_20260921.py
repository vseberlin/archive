import json,csv
from collections import defaultdict
from pathlib import Path
from PIL import Image,ImageOps,ImageDraw,ImageFont
W=Path(__file__).parent;U=Path('/mnt/volume/iss-archive/units/paragon')
F=json.loads((W/'remaining-accession-review-20260921.json').read_text())['files']; O={o['archival_label']:o for o in csv.DictReader((U/'metadata/objects.csv').open())}
groups=defaultdict(list)
for f in F: groups[Path(f['path']).stem].append(f)
font=ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',15)
def put(canvas,p,x,y,w,h):
 with Image.open(p) as im:
  im.draft('RGB',(w,h));im=ImageOps.exif_transpose(im);im.thumbnail((w,h));canvas.paste(im.convert('RGB'),(x+(w-im.width)//2,y+(h-im.height)//2))
G=list(groups.values())
for page in range((len(G)+7)//8):
 canvas=Image.new('RGB',(1500,8*280),'white');d=ImageDraw.Draw(canvas)
 for j,g in enumerate(G[page*8:page*8+8]):
  f=g[0];y=j*280;d.line((0,y,1500,y),fill='black');label=f['pending']['existing_catalogue_association'];obj=O.get(label)
  d.text((5,y+3),f"[{','.join(str(f['index']) for f in g)}] {Path(f['path']).stem} | association: {label or '-'}",font=font,fill='black')
  for k,z in enumerate(g[:2]):
   put(canvas,z['path'],k*500,y+24,495,232);d.text((k*500+5,y+257),f"{z['state']}, {z['source']['width']} x {z['source']['height']}, {Path(z['path']).suffix}",font=font,fill='black')
  if obj:
   put(canvas,U/obj['access_file'].splitlines()[0],1000,y+24,495,232);d.text((1005,y+257),obj['descriptive_title'][:57],font=font,fill='black')
 canvas.save(W/f'remaining-accession-contact-{page+1:02}.jpg',quality=90)
print('groups',len(G),'pages',(len(G)+7)//8)
