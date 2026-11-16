exec(open('/mnt/volume/iss-archive-work/paragon/inspect_remaining_accession_20260921.py').read().split('for page in range')[0])
pairs=[(4,'VLV-1052'),(5,'VLV-1051-1'),(6,'VLV-1051'),(7,'VLV-1050'),(9,'VLV-1161'),(14,'VLV-1120-1'),(15,'VLV-1013-1'),(20,'VLV-1050-1'),(27,'VLV-1058'),(28,'VLV-1058'),(37,'VLV-1021'),(38,'VLV-1021-1'),(42,'VLV-1053'),(43,'VLV-1052-2'),(44,'VLV-1098'),(46,'VLV-1098'),(47,'VLV-1053-1'),(48,'VLV-1161-1'),(49,'VLV-1161'),(61,'VLV-1007-2'),(63,'VLV-1007-1'),(64,'VLV-1055-1'),(65,'VLV-1055'),(66,'VLV-1007-2'),(67,'VLV-1013-1'),(68,'VLV-1013-2'),(139,'VLV-1144-3')]
for page in range((len(pairs)+3)//4):
 canvas=Image.new('RGB',(1800,4*600),'white');d=ImageDraw.Draw(canvas)
 for j,(idx,k) in enumerate(pairs[page*4:page*4+4]):
  f=F[idx-1];obj=O[k];y=j*600
  d.text((5,y+3),f"Source {idx} {Path(f['path']).stem}  | ARCHIVE {k}",font=font,fill='black')
  put(canvas,f['path'],0,y+30,890,560);put(canvas,U/obj['access_file'].splitlines()[0],900,y+30,890,560)
 canvas.save(W/f'remaining-target-comparison-{page+1:02}.jpg',quality=93)
print('done',len(pairs))
