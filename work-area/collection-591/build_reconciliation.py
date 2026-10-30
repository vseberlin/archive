#!/usr/bin/env python3
"""Build bounded, human-reviewed Master candidates; never promote a proposal."""
import csv,json,re,sqlite3,sys
from pathlib import Path
from collections import defaultdict,Counter
from prepare_591 import ROOT,SOURCE,DOC_INDEX,guard,inspect,write_csv,write_json,read_csv

def words(s):
    stop={'der','die','das','des','den','dem','und','von','vom','zur','zum','mit','für','im','in','am','an','auf','ein','eine','einer','eines','veb','aeb','teil','berlin'}
    return {w for w in re.findall(r'[a-zäöüß0-9]+',s.lower()) if len(w)>2 and w not in stop}

def main():
    guard();capture=json.loads((ROOT/'capture-summary.json').read_text());assert capture['resources']==109 and not capture['errors']
    objects=list(json.loads((ROOT/'md-export.json').read_text())['results']['objects'].values());resources=read_csv('md-resources.csv');by_object=defaultdict(list)
    for r in resources:by_object[r['md_object_id']].append(r)
    evidence={}
    for p in [ROOT/'local-path-index-evidence.json',*ROOT.glob('local-content-*.json')]:
        for r in json.loads(p.read_text()):
            if r['source_path'].startswith(str(SOURCE)+'/'):evidence[r['source_path']]=r
    con=sqlite3.connect(f'file:{DOC_INDEX}?mode=ro&immutable=1',uri=True);con.row_factory=sqlite3.Row
    exact=[]
    for resource in resources:
        rows=con.execute('SELECT document_id,source_path,filename,extension,size_bytes,mtime_ns,content_sha256,observed_state,extraction_state,parser,substr(header_text,1,14000) header_text FROM documents WHERE content_sha256=?',(resource['sha256'],)).fetchall()
        for row in rows:
            r=dict(row)
            if not r['source_path'].startswith(str(SOURCE)+'/'):continue
            evidence[r['source_path']]=r;exact.append({'md_object_id':resource['md_object_id'],'resource_id':resource['resource_id'],'source_path':r['source_path'],'sha256':r['content_sha256']})
    con.close();write_json('exact-hash-matches.json',exact)
    write_json('local-search-evidence.json',list(evidence.values()))
    candidates=[];comparisons=[];matched_paths=set();object_rows=[]
    for obj in sorted(objects,key=lambda o:(o['object_inventory_number'],int(o['object_id']))):
        oid=str(obj['object_id']);rr=sorted(by_object[oid],key=lambda r:int(r['sequence']));members=[]
        for r in rr:
            p=Path(r['work_path']);members.append({'source_path':str(p),'size_bytes':p.stat().st_size,'mtime_ns':p.stat().st_mtime_ns,'sha256':r['sha256'],'resource_id':r['resource_id'],'sequence':r['sequence']})
        candidates.append({'md_object_id':oid,'inventory':obj['object_inventory_number'],'candidate_id':'md-'+oid,'origin':'museum_digital','label':'Complete MD representation (recorded resource order)','members_json':json.dumps(members,ensure_ascii=False),'match_status':'matched','match_basis':'Captured authoritative MD object and complete ordered resource set','quality_note':'Current authoritative content baseline; all MD resources retained together.','eligible':'true','preview_path':rr[0]['preview_path'],'deletion_authorized':'false'})
        md_text=' '.join(json.loads(Path(r['technical_path']).read_text())['text_sample'] for r in rr)
        title_tokens=words(obj['object_name']);md_tokens=words(md_text)
        found=[]
        for path,local in evidence.items():
            if local['extension']!='.pdf':continue
            exact_resources=[r for r in rr if r['sha256']==local['content_sha256']]
            text=local['header_text'];tokens=words(text[:10000]);overlap=len(title_tokens & tokens)/max(1,len(title_tokens))
            # Explicit internal-content correction: local AEB_62 contains Arbeitsordnung,
            # not the MD AEB_062 industrial-interconnection document.
            corrected=oid=='66129' and local['filename']=='AEB_62.pdf' and 'arbeitsordnun' in text.lower()
            direct=oid=='60895' and local['filename'].startswith('AEB_040-') and 'elektronikhandel' in text.lower()
            semantic=overlap>=.8 and len(title_tokens)>=4 and len(tokens&md_tokens)>=12
            if not (exact_resources or corrected or direct or semantic):continue
            p=Path(path)
            try:unchanged=(p.stat().st_size,p.stat().st_mtime_ns)==(int(local['size_bytes']),int(local['mtime_ns']))
            except OSError:unchanged=False
            if not unchanged:continue
            tech=inspect(p,'local-'+local['document_id'],ocr=False)
            md_pdfs=[r for r in rr if r['type']=='text']
            complete_single=len(rr)==1 and tech['pages']==int(rr[0]['pages'])
            status='matched' if exact_resources or corrected or direct else 'ambiguous'
            note='Byte-identical to a captured MD resource; no resolution advantage.' if exact_resources else 'Content lead; compare pages and scan quality before selecting.'
            if corrected:note='Filename says AEB_62, but internal Arbeitsordnung content matches MD AEB_063. The first-page comparison confirms the same cover. MD has the cleaner crop and handwritten AEB-063 marking; both scans have 150 ppi raster images. MD remains the recommendation.'
            if direct and not exact_resources:note='Same Versorgungsprogramm and 7-page extent; compare deskew, watermark and text layer. Native raster resolution alone does not establish an improvement.'
            if oid=='60895' and local['filename']=='AEB_040-Versorgungsprogramm.pdf' and (ROOT/'AEB-040-page-comparison.json').exists():note='All seven pages of the original and MD PDF render pixel-identically at 1600 px long edge; both contain 300 ppi images. No visual/resolution advantage is established; the MD PDF also supplies an internal text layer. See AEB-040-page-comparison.json.'
            if not complete_single:note+=' This file does not establish coverage of the complete MD resource set.'
            derivative=bool(re.search(r'(?i)(?:^|[-_])(dsb|wz|ocr)(?:[-_.]|$)',p.name))
            if derivative and not exact_resources:note+=' Existing derivative marker: retain as comparison evidence, not a proposed Master.'
            eligible=tech['state']=='readable' and complete_single and status=='matched' and not (derivative and not exact_resources)
            cid='local-'+oid+'-'+local['document_id']
            candidates.append({'md_object_id':oid,'inventory':obj['object_inventory_number'],'candidate_id':cid,'origin':'protected_usb','label':p.name,'members_json':json.dumps([{'source_path':path,'size_bytes':local['size_bytes'],'mtime_ns':local['mtime_ns'],'sha256':local['content_sha256'],'resource_id':exact_resources[0]['resource_id'] if exact_resources else '', 'sequence':1}],ensure_ascii=False),'match_status':status,'match_basis':'Stored SHA-256 equality and unchanged indexed size/mtime' if exact_resources else 'Internal text/OCR plus page-count comparison; filename conflict retained' if corrected else 'Internal content and document extent; source name is only supporting evidence','quality_note':note,'eligible':str(eligible).lower(),'preview_path':tech['preview_path'],'deletion_authorized':'false'})
            comparisons.append({'md_object_id':oid,'inventory':obj['object_inventory_number'],'candidate_id':cid,'source_path':path,'source_sha256':local['content_sha256'],'local_pages':str(tech['pages']),'md_pdf_pages':';'.join(r['pages'] for r in md_pdfs),'local_median_ppi':str(tech['median_ppi']),'md_median_ppi':';'.join(r['median_ppi'] for r in md_pdfs),'local_size_bytes':str(local['size_bytes']),'md_size_bytes':';'.join(r['size_bytes'] for r in md_pdfs),'match_status':status,'comparison_note':note,'deletion_authorized':'false'})
            matched_paths.add(path);found.append(cid)
        object_rows.append({'md_object_id':oid,'inventory':obj['object_inventory_number'],'title':obj['object_name'],'description':obj['object_description'],'object_url':'https://berlin.museum-digital.de/object/'+oid,'md_resources':str(len(rr)),'local_candidates':str(len(found)),'metadata_authority':'museum-digital during reconciliation','recommended_candidate_id':'md-'+oid,'recommendation_basis':'Preserve authoritative MD content unless a complete local representation is verified better; no automatic Master selection.','metadata_json':json.dumps(obj,ensure_ascii=False)})
    write_csv('objects.csv',object_rows);write_csv('candidates.csv',candidates)
    if comparisons:write_csv('quality-comparisons.csv',comparisons)
    # Human decisions survive a search refresh. Existing candidate identifiers are stable.
    old={r['md_object_id']:r for r in read_csv('master-decisions.csv')}
    decisions=[old.get(r['md_object_id'],{'md_object_id':r['md_object_id'],'inventory':r['inventory'],'selected_candidate_id':'','decision':'unresolved','actor':'','decided_at':'','note':'','deletion_authorized':'false'}) for r in object_rows]
    write_csv('master-decisions.csv',decisions)
    d={r['md_object_id']:r for r in decisions}
    write_csv('candidate-dispositions.csv',[{'md_object_id':r['md_object_id'],'candidate_id':r['candidate_id'],'disposition':('selected_master' if d[r['md_object_id']]['selected_candidate_id']==r['candidate_id'] else 'redundant') if d[r['md_object_id']]['selected_candidate_id'] else 'unresolved','source_paths_json':json.dumps([m['source_path'] for m in json.loads(r['members_json'])],ensure_ascii=False),'deletion_authorized':'false'} for r in candidates])
    context=[]
    for path,r in evidence.items():
        if path in matched_paths:continue
        text=r['header_text'].lower()
        relevance='collection_context_lead' if any(x in text[:6000] for x in ['applikationszentrum','applikationslabor','applikationsberichte']) and r['extension']=='.pdf' else 'broader_collection_or_search_hit'
        context.append({**r,'relevance':relevance,'match_status':'ambiguous','review_note':'Search evidence only; no unit ownership, Master selection or drain inclusion inferred.'})
    write_json('local-context-review.json',context)
    visual=json.loads((ROOT/'visual-search-summary.json').read_text()) if (ROOT/'visual-search-summary.json').exists() else {'status':'search_in_progress'}
    summary={'unit':'collection-591','name':'Sammlung AEB','md_objects':68,'md_resources':109,'candidate_representations':len(candidates),'objects_with_local_candidates':sum(int(r['local_candidates'])>0 for r in object_rows),'local_candidate_paths':len(matched_paths),'exact_hash_matches':len(exact),'indexed_document_paths_screened':len(evidence),'local_context_paths':len(context),'visual_search':visual,'metadata_authority':'museum-digital','authority_after_approved_accession':'local archive; MD is a projection','master_selections':sum(bool(r['selected_candidate_id']) for r in decisions),'accession_performed':False,'deletion_authorized':False}
    write_json('reconciliation-summary.json',summary);print(json.dumps(summary,indent=2),flush=True)

if __name__=='__main__':main()
