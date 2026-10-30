exec(open('/mnt/volume/iss-archive-work/paragon/inspect_remaining_accession_20260921.py').read().split('for page in range')[0])
pairs=[(4,20),(5,'VLV-1050-1'),(7,31),(1,62),(50,'VLV-1022'),(8,25),(16,'VLV-1006'),(17,'VLV-1006-1'),(33,36),(24,26),(26,29),(15,68),(2,'VLV-1013'),(9,49),(3,21),(27,28)]
for page in range(4):
 canvas=Image.new('RGB',(1800,2400),'white');d=ImageDraw.Draw(canvas)
 for j,(idx,k) in enumerate(pairs[page*4:page*4+4]):
  f=F[idx-1];p=F[k-1]['path'] if isinstance(k,int) else str(U/O[k]['access_file'].splitlines()[0]);y=j*600
  d.text((5,y+3),f'Source {idx} | Target {k}',font=font,fill='black');put(canvas,f['path'],0,y+30,890,560);put(canvas,p,900,y+30,890,560)
 canvas.save(W/f'remaining-detail-comparison-{page+1:02}.jpg',quality=93)
print('done')
