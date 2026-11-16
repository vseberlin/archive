exec(open('/mnt/volume/iss-archive-work/paragon/inspect_remaining_accession_20260921.py').read().split('for page in range')[0])
pairs=[(26,'VLV-1081'),(36,'VLV-1020-1'),(51,'VLV-1004'),(21,'VLV-1004-2')]

for page in range(1):
 canvas=Image.new('RGB',(1800,2400),'white');d=ImageDraw.Draw(canvas)
 for j,(idx,k) in enumerate(pairs[page*4:page*4+4]):
  f=F[idx-1];p=F[k-1]['path'] if isinstance(k,int) else str(U/O[k]['access_file'].splitlines()[0]);y=j*600
  d.text((5,y+3),f'Source {idx} | Target {k}',font=font,fill='black');put(canvas,f['path'],0,y+30,890,560);put(canvas,p,900,y+30,890,560)
 canvas.save(W/f'remaining-serial-check-{page+1:02}.jpg',quality=93)
print('done')
