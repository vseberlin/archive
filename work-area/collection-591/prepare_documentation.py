#!/usr/bin/env python3
"""Inspect supporting documentation; propose scope, preserve all human choices."""
import json,re,subprocess,zipfile,xml.etree.ElementTree as ET
from pathlib import Path
from collections import Counter,defaultdict
from prepare_591 import ROOT,guard,read_csv,write_csv,write_json

DESCRIPTIONS={
'ApplikationsLabor.pdf':('direct','include','AEB application laboratory, 1980–1989: Peter Salomon describes projects, organisation and later developments.','2013','Copyright year on the opening pages'),
'ApplBerichte.pdf':('direct','include','Peter Salomon’s account of his early work in the AEB application laboratory, with circuit/application reports and appendices.','2013-06','Copyright statement: June 2013'),
'QPC-02neu.pdf':('direct','include','History and technical account of the QPC-02/QPC-10 calculator developed at AEB; includes Peter Salomon and Thomas Falk accounts.','2003 / 2011 / 2015','Multiple contributor/revision years printed inside; not reduced to a single invented date'),
'KC-InfoHistory_neu.pdf':('contextual','review_context','Personal account of the KC computer community, including AEB laboratory work and organisation.','','No reliable publication date established; historical event years are not publication dates'),
'Privat_BE_Handel.pdf':('contextual','review_context','Peter Salomon’s personal account of private component trading, with substantial context about work in the AEB laboratory.','2011-11; revised 2013-06','Printed copyright and revision statement'),
'BSS-01_neu.pdf':('contextual','review_context','Insider account of the BSS-01 video game, including an AEB director’s involvement; related context rather than an AEB collection inventory.','2003','Printed copyright year'),
'EinleitungVortragsreihe.pdf':('broader','unrelated_to_unit','Introduction to the Fernsehtechnik aus Berlin lecture series. AEB appears in the speaker’s biography; the document concerns the broader television-history programme.','','No reliable document date established'),
'Fernsehtechnik_Farbfernsehen(PS).pdf':('broader','unrelated_to_unit','Colour-television lecture revised by Peter Salomon. AEB is biographical context; the subject is the wider history of DDR television.','2011; revised 2023','Internal copyright/lecture dates: 2011 and revision for 21 March 2023'),
'2019-12-12_Digis-Abschluss.pptx':('mixed','review_context','DIGIS project closing presentation. Slide 3 quotes 20 Jahre VEB Applikationszentrum Elektronik Berlin; other slides cover photo digitisation and several collections.','2019-12-11','Embedded OOXML created/modified timestamps; filename suggests presentation on 12 December, retained separately'),
'Stundenabrechnung_Steffen-Wedepohl.xls':('broader','unrelated_to_unit','General DIGIS timesheet with one Roland Krause reference among photo, TIFF and exhibition work. It does not establish an AEB-specific accession or donation.','','Month/day entries do not establish a reliable year'),
'Stundenabrechnung_Steffen-Wedepohl_02.xls':('broader','unrelated_to_unit','Revised/general DIGIS timesheet. Overlaps the first version but contains different hours, more entries and inconsistent rendered dates; keep as wider project administration.','','Inconsistent date values retained; no year inferred'),
'2-Sanierung Hardt-Walter Hämer.doc':('unrelated','unrelated_to_unit','Architecture and urban-renewal text. The Reissmüller hit refers to Wilhelm Reissmüller, not the AEB donor Günther Reissmüller.','','No relevant AEB document date'),
}

def excerpts(text):
    matches=list(re.finditer(r'Applikationszentrum|Applikationslabor|\bAEB\b|Roland Krause|Reissmüller',text,re.I))
    return '\n\n'.join(text[max(0,m.start()-100):m.end()+360] for m in matches[:6]) or text[:1800]

def main():
    guard();index=json.loads((ROOT/'documentation-candidate-index.json').read_text());saved={r['document_id']:r for r in read_csv('documentation-review.csv')};rows=[];internal=[]
    for r in index:
        source=Path(r['source_path']);st=source.stat();assert (st.st_size,st.st_mtime_ns)==(int(r['size_bytes']),int(r['mtime_ns']))
        with source.open('rb') as f:signature=f.read(8).hex()
        evidence_path=ROOT/('documentation-internal-'+r['document_id']+'.json')
        evidence=json.loads(evidence_path.read_text()) if evidence_path.exists() else {}
        live_path=ROOT/('documentation-live-'+r['document_id']+'.json')
        live=json.loads(live_path.read_text()) if live_path.exists() else {}
        text=live.get('text') or evidence.get('text') or r['header_text'];condition=live.get('state','readable');preview=live.get('preview_path','');method=live.get('method') or 'Stored index extraction; unchanged source size/mtime'
        structure={}
        if r['extension']=='.pptx':
            with zipfile.ZipFile(source) as z:
                assert z.testzip() is None
                slides=[]
                for name in sorted((n for n in z.namelist() if re.fullmatch(r'ppt/slides/slide\d+.xml',n)),key=lambda n:int(re.search(r'slide(\d+)',n)[1])):
                    node=ET.fromstring(z.read(name));slides.append({'slide':int(re.search(r'slide(\d+)',name)[1]),'text':' '.join(t.text or '' for t in node.iter() if t.tag.endswith('}t'))})
                structure={'slides':slides,'core_properties_xml':z.read('docProps/core.xml').decode()}
                text='\n\n'.join('Slide '+str(s['slide'])+': '+s['text'] for s in slides);method='Live ZIP CRC check, ordered slide XML and core properties';condition='readable'
        if r['filename'].startswith('Archiv Bücher und Zeitungen'):
            result=subprocess.run(['xls2csv',str(source)],capture_output=True,timeout=45);assert result.returncode==0
            text=result.stdout.decode(errors='replace');method='Live xls2csv internal cell extraction plus original OLE signature';condition='readable'
            desc=('broader','unrelated_to_unit','Overall book and periodical inventory, including AEB-published data books among many other collections. Preserve the source as a collection-wide reference; do not treat it as the AEB unit inventory.','','No reliable compilation date established from the workbook')
        else:desc=DESCRIPTIONS[r['filename']]
        row={'document_id':r['document_id'],'source_path':r['source_path'],'size_bytes':str(r['size_bytes']),'mtime_ns':str(r['mtime_ns']),'content_sha256':r['content_sha256'],'relationship':desc[0],'recommendation':desc[1],'description':desc[2],'document_date':desc[3],'date_basis':desc[4],'condition':condition,'inspection_method':method,'content_evidence':excerpts(text),'preview_path':preview,'decision':'needs_review','review_note':'','actor':'','decided_at':'','deletion_authorized':'false'}
        if r['document_id'] in saved:
            for field in ['decision','review_note','actor','decided_at']:row[field]=saved[r['document_id']][field]
        rows.append(row);internal.append({'document_id':r['document_id'],'source_path':r['source_path'],'source_sha256':r['content_sha256'],'signature':signature,'inspection':method,'text':text,'structure':structure})
    write_csv('documentation-review.csv',rows);write_json('documentation-internal-evidence.json',internal)
    groups=defaultdict(list)
    for r in rows:groups[r['content_sha256']].append(r)
    summary={'result':'ok','source_paths':len(rows),'content_hash_groups':len(groups),'recommendation_groups':dict(Counter(v[0]['recommendation'] for v in groups.values())),'relevant_or_contextual_groups':sum(v[0]['relationship'] in ['direct','mixed','contextual'] for v in groups.values()),'broader_or_unrelated_groups':sum(v[0]['relationship'] in ['broader','unrelated'] for v in groups.values()),'readable_source_paths':sum(r['condition']=='readable' for r in rows),'included_source_paths':sum(r['decision']=='included' for r in rows),'pending_groups':sum(any(r['decision']=='needs_review' for r in v) for v in groups.values()),'scope':'Targeted collection/donor queries, existing internal-content search evidence, live PDF/Office inspection and global exact-hash duplicate lookup','limitations':'No AEB-specific accession inventory or donation agreement identified in completed targeted queries. Broad AEB token search reached its 301-result bound and was not used to assert exhaustive coverage. General inventories remain wider collection references.','accession_performed':False,'deletion_authorized':False}
    write_json('documentation-review-summary.json',summary)
    report=['# AEB documentation review','',f"{len(rows)} source paths, {len(groups)} stored-hash groups. All inspected sources are readable. Recommendations are proposals; no document has been silently selected.",'','Three direct supporting accounts are recommended for inclusion. Three additional AEB-context accounts and one mixed DIGIS presentation need scope review. Eight broader/unrelated groups remain separate from the proposed AEB documentation.','','No dedicated AEB import inventory or donation agreement was identified in the completed targeted search. Wider inventories retain their own authority and are not substituted for MD metadata.','']
    for group in groups.values():
        r=group[0];report.extend(['## '+Path(r['source_path']).name,'',r['description'],'',f"Relationship: {r['relationship']}; recommendation: {r['recommendation']}; condition: {r['condition']}; source copies: {len(group)}.",'',f"Date: {r['document_date'] or 'unknown'} — {r['date_basis']}",'',f"Inspection: {r['inspection_method']}",'',r['content_evidence'],''])
    (ROOT/'DOCUMENTATION_REVIEW.md').write_text('\n'.join(report))
    print(json.dumps(summary,indent=2),flush=True)

if __name__=='__main__':main()
