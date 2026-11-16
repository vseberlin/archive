#!/usr/bin/env python3
"""Bounded content comparison of FEB import tables; source files are read-only."""
import csv
import io
import json
import re
import posixpath
import sys
import html
from datetime import datetime, timedelta
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from pathlib import Path
from zipfile import ZipFile
import collection_551_workbench as core

ROOT = core.ROOT
NS = '{http://schemas.openxmlformats.org/spreadsheetml/2006/main}'

def norm(value):
    return re.sub(r'\s+', ' ', value).strip()

def colnum(ref):
    n = 0
    for c in re.match('[A-Z]+', ref)[0]: n = n * 26 + ord(c) - 64
    return n - 1

def tables(path):
    if path.suffix.lower() == '.csv':
        raw = path.read_bytes()
        for encoding in ['utf-8-sig', 'cp1252', 'latin1']:
            try: text = raw.decode(encoding); break
            except UnicodeDecodeError: pass
        delimiter = max([';', ',', '\t'], key=lambda d: text.splitlines()[0].count(d))
        rows = [{'row':i, 'cells':{str(k):v for k,v in enumerate(row) if v}, 'formulas':{}} for i,row in enumerate(csv.reader(io.StringIO(text), delimiter=delimiter),1)]
        yield 'CSV', rows, {'encoding':encoding,'delimiter':delimiter}
        return
    with ZipFile(path) as z:
        strings = []
        if 'xl/sharedStrings.xml' in z.namelist():
            strings = [''.join(t.text or '' for t in si.iter(NS+'t')) for si in ET.fromstring(z.read('xl/sharedStrings.xml')).findall(NS+'si')]
        book = ET.fromstring(z.read('xl/workbook.xml'))
        rels = {r.attrib['Id']:r.attrib['Target'] for r in ET.fromstring(z.read('xl/_rels/workbook.xml.rels'))}
        for sheet in book.find(NS+'sheets'):
            rid=sheet.attrib['{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id']
            target=rels[rid]; target=target.lstrip('/') if target.startswith('/') else posixpath.normpath('xl/'+target)
            rows=[]; tail=False
            with z.open(target) as handle:
                for event,node in ET.iterparse(handle, events=['end']):
                    if node.tag != NS+'row':continue
                    cells={};formulas={};n=int(node.get('r','0'))
                    for cell in node.findall(NS+'c'):
                        value=cell.find(NS+'v');value=value.text if value is not None and value.text else ''
                        if cell.get('t')=='s' and value:value=strings[int(value)]
                        elif cell.get('t')=='inlineStr':value=''.join(t.text or '' for t in cell.iter(NS+'t'))
                        f=cell.find(NS+'f');key=str(colnum(cell.get('r')))
                        if value:cells[key]=value
                        if f is not None:formulas[key]=f.text or '[shared formula]'
                    if cells or formulas:rows.append({'row':n,'cells':cells,'formulas':formulas})
                    node.clear()
            yield sheet.attrib['name'],rows,{'date1904':book.find(NS+'workbookPr').get('date1904','0') if book.find(NS+'workbookPr') is not None else '0'}

def inspect():
    core.source_guard()
    review=core.read_csv(ROOT/'accession-document-review.csv');groups=defaultdict(list)
    for r in review:groups[r['content_sha256']].append(r)
    sources=[];data=[]
    for digest,group in groups.items():
        row=next((r for r in group if r['decision']=='included'),group[0]);p=Path(row['source_path'])
        if p.suffix.lower() not in {'.xlsx','.csv'} or row['condition']!='readable' or core.document_is_unrelated(row):continue
        st=p.stat();assert st.st_size==int(row['size_bytes']) and st.st_mtime_ns==int(row['mtime_ns'])
        source={'sha256':digest,'source_path':str(p),'document_id':row['document_id'],'copies':[r['source_path'] for r in group],'sheets':[]}
        for name,rows,props in tables(p):
            header=next((r for r in rows[:12] if 'inventory_number' in [norm(v).lower() for v in r['cells'].values()]),None)
            import_table=bool(header and 'object_title' in header['cells'].values())
            summary={'sheet':name,'rows':len(rows),'import_table':import_table,'properties':props}
            source['sheets'].append(summary)
            if not import_table:continue
            fields={k:norm(v).lower().replace('tag ','tag') for k,v in header['cells'].items()}
            summary['headers']=fields;summary['duplicate_headers']=[v for v,n in Counter(fields.values()).items() if n>1]
            summary['ids']=[]
            for r in rows:
                if r['row']<=header['row']:continue
                values=defaultdict(list)
                for k,v in r['cells'].items():
                    field=fields.get(k,'__unlabelled_'+k)
                    if norm(v):values[field].append(v)
                if not values:continue
                identifier=norm(values.get('inventory_number',[''])[0])
                summary['ids'].append(identifier)
                data.append({'sha256':digest,'sheet':name,'row':r['row'],'inventory_number':identifier,'values':dict(values),'formulas':r['formulas']})
        sources.append(source)
    result={'sources':sources,'rows':data}
    core.atomic_replace(ROOT/'FEB-import-series-analysis.json',(json.dumps(result,ensure_ascii=False)+'\n').encode())
    for s in sources:
        for sh in s['sheets']:
            if sh['import_table']:
                ids=sh['ids'];print(s['sha256'][:10],Path(s['source_path']).name,repr(sh['sheet']),len(ids),'rows',len(set(ids)),'IDs',ids[:2],ids[-2:],'duplicate columns',sh['duplicate_headers'])
    print('Extracted',len(data),'import rows from',sum(any(sh['import_table'] for sh in s['sheets']) for s in sources),'distinct source files. Full cell text retained.')

BASES = ['93e61835c5', 'b2c47e1873', '3fc55d338e', '207a4c5273']
MONTHS = ['Januar','Februar','März','April','Mai','Juni','Juli','August','September','Oktober','November','Dezember']

def date_value(value):
    value=norm(value)
    if re.fullmatch(r'\d{5}(?:\.0+)?',value) and 10000 <= float(value) <= 40000:
        dt=datetime(1899,12,30)+timedelta(days=float(value))
        return f'{MONTHS[dt.month-1]} {dt.year}' if dt.day==1 else dt.strftime('%d.%m.%Y')
    return value

def equivalent(field, value):
    value=norm(value)
    if field=='commissioned_when1':
        value=date_value(value)
        for i,month in enumerate(MONTHS,1):
            m=re.fullmatch(re.escape(month[:3])+r'[a-zä]*\.?\s*(\d{2}|\d{4})',value,re.I)
            if m:
                year=int(m[1]);year=year+1900 if year<100 else year
                return f'{year:04}-{i:02}'
    if field=='object_dimensions':return value.casefold()
    return value

def write_csv(name, rows, fields=None):
    if fields is None:fields=list(rows[0]) if rows else ['note']
    stream=io.StringIO();writer=csv.DictWriter(stream,fieldnames=fields,delimiter=';',quoting=csv.QUOTE_ALL)
    writer.writeheader();writer.writerows(rows)
    core.atomic_replace(ROOT/name, ('\ufeff'+stream.getvalue()).encode('utf-8'))

def consolidate():
    core.source_guard()
    a=json.loads((ROOT/'FEB-import-series-analysis.json').read_text())
    source_by_hash={s['sha256']:s for s in a['sources']}
    for source in a['sources']:
        if not any(s['import_table'] for s in source['sheets']):continue
        assert Path(source['source_path']).is_file()
    grouped=defaultdict(list)
    for r in a['rows']:grouped[r['sha256'][:10]].append(r)
    selected=[r for pref in BASES for r in grouped[pref]]
    assert len(selected)==1929 and len({r['inventory_number'] for r in selected})==1929
    data={};provenance=[];corrections=[];issues=[]
    for r in selected:
        inv=r['inventory_number'];out={}
        for field,values in r['values'].items():
            distinct=list(dict.fromkeys(norm(v) for v in values if norm(v)))
            assert len(distinct)==1,(inv,field,distinct)
            out[field]=distinct[0]
            if len(values)>1:
                corrections.append({'inventory_number':inv,'field':field,'change':'Identical duplicate columns collapsed','before':json.dumps(values,ensure_ascii=False),'after':distinct[0]})
        before=out.get('commissioned_when1','');after=date_value(before)
        if after!=before:
            out['commissioned_when1']=after
            corrections.append({'inventory_number':inv,'field':'commissioned_when1','change':'Excel serial date rendered as calendar text; four-digit years left unchanged','before':before,'after':after})
        # These five stray cells do not form a resource: no locator or media type,
        # and n is not a MIME type. The cleaner template confirms only resource 1.
        if inv=='FEB-008-0051':
            held={k:v for k,v in out.items() if re.fullmatch('resource_.*3',k)}
            assert held.get('resource_fileformat3')=='n' and 'resource_location3' not in held
            for k in held:del out[k]
            corrections.append({'inventory_number':inv,'field':'resource_*3','change':'Stray third-resource cells excluded from active metadata; original values retained here','before':json.dumps(held,ensure_ascii=False),'after':''})
        if not re.fullmatch(r'FEB-[A-Z0-9]+-[0-9A-Za-z]+(?:-[0-9A-Za-z]+)*',inv) or inv.count('FEB')>1:
            issues.append({'inventory_number':inv,'field':'inventory_number','issue':'Unusual or malformed identifier retained verbatim; no identity guessed','value':inv})
        if not out.get('object_description'):
            issues.append({'inventory_number':inv,'field':'object_description','issue':'Description missing from selected final-form source','value':''})
        for n in range(1,4):
            if out.get(f'resource_location{n}') and out.get(f'resource_fileformat{n}')!='application/pdf':
                issues.append({'inventory_number':inv,'field':f'resource_fileformat{n}','issue':'Resource MIME type needs review','value':out.get(f'resource_fileformat{n}','')})
        data[inv]=out
        source=source_by_hash[r['sha256']]
        provenance.append({'inventory_number':inv,'source_sha256':r['sha256'],'source_path':source['source_path'],'sheet':r['sheet'],'row':r['row']})
    # All predecessor rows remain traceable. Same identifier alone does not merge
    # a different report, nor do possible title matches create identity aliases.
    comparisons=[];earlier=[];classification=Counter()
    selected_refs={(r['sha256'],r['sheet'],r['row']) for r in selected}
    for r in a['rows']:
        ref=(r['sha256'],r['sheet'],r['row'])
        if ref in selected_refs:continue
        inv=r['inventory_number'];source=source_by_hash[r['sha256']]
        base={'inventory_number':inv,'source_path':source['source_path'],'source_sha256':r['sha256'],'sheet':r['sheet'],'row':r['row']}
        if inv not in data:
            state='incomplete predecessor row' if not r['values'].get('object_title') else 'earlier identifier; no automatic reassignment'
            earlier.append({**base,'classification':state,'values_json':json.dumps(r['values'],ensure_ascii=False)})
            classification[state]+=1;continue
        changed=False
        for field,values in r['values'].items():
            if field=='inventory_number':continue
            for val in dict.fromkeys(norm(v) for v in values if norm(v)):
                current=data[inv].get(field,'')
                if equivalent(field,val)==equivalent(field,current):continue
                changed=True
                state='historical variant; final-form source retained'
                if current and norm(current.encode('cp1252','replace').decode('cp1252'))==val:
                    state='lossy CSV export; intact Unicode workbook value retained'
                comparisons.append({**base,'field':field,'consolidated_value':current,'other_value':val,'assessment':state})
        classification['variant row' if changed else 'equivalent row']+=1
    fields=['inventory_number','object_type','object_title','object_description','object_material_technique','object_dimensions','object_publication','collection_name1','abode_regular','rights_annotation','copyright','rights_of_use']
    fields += sorted({k for row in data.values() for k in row}-set(fields),key=lambda s:(s.startswith(('image_','resource_')),s))
    final=[{k:r.get(k,'') for k in fields} for r in data.values()]
    assert all(not k.startswith('__unlabelled') for k in fields)
    write_csv('FEB-Import-konsolidiert.csv',final,fields)
    write_csv('FEB-Import-Quellen.csv',provenance)
    write_csv('FEB-Import-Varianten.csv',comparisons)
    write_csv('FEB-Import-Fruehere-Zeilen.csv',earlier)
    write_csv('FEB-Import-Korrekturen.csv',corrections)
    write_csv('FEB-Import-Pruefpunkte.csv',issues)
    sources=[]
    for s in a['sources']:
        imports=[sh for sh in s['sheets'] if sh['import_table']]
        if imports:
            sources.append({'source_sha256':s['sha256'],'source_path':s['source_path'],'copies':len(s['copies']),'import_rows':sum(len(sh['ids']) for sh in imports),'role':'selected final-form batch' if s['sha256'][:10] in BASES else 'compared equivalent export or predecessor','encoding':next((sh['properties'].get('encoding') for sh in imports if sh['properties'].get('encoding')),'OOXML Unicode')})
    write_csv('FEB-Import-Dateivergleich.csv',sources)
    summary={'created_at':core.now(),'rows':len(final),'unique_inventory_numbers':len(data),'fields':len(fields),'source_files':len(sources),'source_paths':sum(s['copies'] for s in sources),'source_import_rows':len(a['rows']),'selected_batches':[{'sha_prefix':k,'rows':len(grouped[k]),'file':Path(source_by_hash[grouped[k][0]['sha256']]['source_path']).name} for k in BASES],'predecessor_classifications':dict(classification),'variant_cells':len(comparisons),'retained_earlier_rows':len(earlier),'corrections':len(corrections),'check_items':len(issues),'damaged_sources_used':False,'original_decisions_changed':False,'accession_performed':False,'import_executed':False}
    core.atomic_replace(ROOT/'FEB-Import-Zusammenfassung.json',(json.dumps(summary,ensure_ascii=False,indent=2)+'\n').encode())
    md=f'''# FEB — konsolidierte Importserie

Stand: {summary['created_at']}. **Eine konsolidierte Arbeitsfassung mit {len(final):,} eindeutigen Inventarnummern.**

Die Daten vereinen vier inhaltlich geprüfte Importchargen: 41 erste Einträge, 13 Einträge mit Medienverweisen, 1.107 FEB-008-Einträge und 768 Einträge weiterer FEB-Teilserien. Der Dateiname „erste 40“ enthält tatsächlich 41 Datensätze. Drei verschieden gehashte 1.107-Zeilen-Arbeitsmappen haben identische Tabelleninhalte. Es wurde kein neuer Archiv-Master ausgewählt und kein MD-Import ausgeführt.

## Dateien

- `FEB-Import-konsolidiert.csv`: vollständige Haupttabelle, UTF-8 mit BOM, Semikolon; {len(fields)} eindeutige Spalten.
- `FEB-Import-Quellen.csv`: Quelldatei, bestehender SHA-256, Blatt und Originalzeile für jeden Hauptdatensatz.
- `FEB-Import-Varianten.csv`: {len(comparisons):,} abweichende ältere Feldwerte; die gewählten Werte bleiben nachvollziehbar.
- `FEB-Import-Fruehere-Zeilen.csv`: {len(earlier)} ältere Zeilen ohne identische Inventarnummer in der Haupttabelle. Keine automatische Identitätszuordnung anhand ähnlicher Titel.
- `FEB-Import-Korrekturen.csv`: {len(corrections)} dokumentierte technische Bereinigungen.
- `FEB-Import-Pruefpunkte.csv`: {len(issues)} ausdrücklich offene Prüfhinweise.
- `FEB-Import-Dateivergleich.csv`: 15 unterschiedliche Quelldateien, ihre Kopien und Rolle.

## Auswahl und Bereinigung

Die final formatierten XLSX-Tabellen bilden die Basis, weil sie eindeutige Inventarnummern, vollständige Beschreibungsfelder und intakte Unicode-Zeichen enthalten. Die vergleichbaren CSV-Exporte verloren unter anderem griechische und kyrillische Zeichen; der angebliche UTF-8-Export ist Windows-1252. Seine beschädigten Textwerte ersetzen die intakten XLSX-Werte nicht.

Leerräume wurden vereinheitlicht. Excel-Datumszahlen wurden als Kalendertext ausgegeben; reine Jahreszahlen bleiben Jahreszahlen. Die zweimal benannte, inhaltsgleiche Spalte `resource_media_type3` wurde zusammengeführt. Fünf verrutschte Zellen eines nicht belegten dritten Ressourcenblocks bei FEB-008-0051 wurden aus der Haupttabelle genommen und vollständig im Korrekturprotokoll erhalten. Das lesbare Musterdokument bestätigt dort nur Ressource 1.

Abweichende alte Beschreibungen, Standorte, Schlagworte, Publikations- und Rechteangaben werden nicht als neue Tatsachen übernommen; sie bleiben im Variantenverzeichnis. Ungewöhnliche Inventarnummern bleiben unverändert und sind markiert. Dieses Dokument konsolidiert historische Importvorlagen; es ersetzt nicht die bereits gespeicherten Master- und Metadatenentscheidungen für die 79 MD-Objekte.

## Umfang und Grenzen

Die vorhandenen Dokumentindex-Einträge wurden mit den vollständigen internen Tabellen abgeglichen. Die gezielte DOC/DOCX/PDF-Suche nach Importfeldern und Importformulierungen ergab keine zusätzliche FEB-Importtabelle; die PDF-Treffer waren sachfremde TRO-Berichte. Antragsunterlagen sind keine Tabellenrevisionen. Beschädigte und als sammlungsfremd ausgeschlossene Dokumente wurden nicht übernommen.

Die Haupttabelle ist die konsolidierte Arbeitsfassung. Vor einem tatsächlichen Import sind die Prüfhinweise sowie der Umgang mit bestehenden MD-Datensätzen und historischen Medienpfaden zu entscheiden. Quellen, Dokumententscheidungen, Drainlisten und Archivdateien wurden nicht verändert.
'''
    core.atomic_replace(ROOT/'FEB-Import-README.md',md.encode())
    # One portable, bounded review view, served by the existing workbench.
    from urllib.parse import urlencode
    link=lambda name:'/file?'+urlencode({'path':str(ROOT/name)})
    body='<section class="summary"><h1>FEB — konsolidierte Importserie</h1><p><strong>1.929 eindeutige Inventarnummern</strong> · 15 verglichene Dateien · vier Hauptchargen</p><p>Historische Importvorlagen zusammengeführt; bestehende MD-Entscheidungen und Quelldateien bleiben unverändert.</p>'
    for name,label in [('FEB-Import-konsolidiert.csv','Konsolidierte CSV herunterladen'),('FEB-Import-README.md','Ergebnis und Auswahlregeln'),('FEB-Import-Pruefpunkte.csv','Offene Prüfhinweise'),('FEB-Import-Varianten.csv','Ältere Feldvarianten'),('FEB-Import-Fruehere-Zeilen.csv','Frühere Zeilen'),('FEB-Import-Quellen.csv','Herkunft je Datensatz')]:
        body+='<p><a href="'+html.escape(link(name))+'">'+label+'</a></p>'
    body+='</section><section class="panel"><h2>In der Tabelle suchen</h2><input id="search" type="text" placeholder="Inventarnummer, Titel oder Datum"><p id="count"></p><table><thead><tr><th>Inventarnummer</th><th>Titel</th><th>Datum</th><th>Quelle / Zeile</th></tr></thead><tbody id="rows"></tbody></table></section>'
    previews=[{'id':r['inventory_number'],'title':r['object_title'],'date':r.get('commissioned_when1',''),'source':Path(p['source_path']).name+': '+str(p['row'])} for r,p in zip(final,provenance)]
    encoded=json.dumps(previews,ensure_ascii=False).replace('<','\\u003c')
    body+='<script>const data='+encoded+';const q=document.getElementById("search"),out=document.getElementById("rows"),count=document.getElementById("count");function show(){const found=data.filter(r=>Object.values(r).join(" ").toLowerCase().includes(q.value.toLowerCase()));out.replaceChildren();for(const r of found.slice(0,100)){const tr=document.createElement("tr");for(const k of ["id","title","date","source"]){const td=document.createElement("td");td.textContent=r[k];tr.appendChild(td);}out.appendChild(tr);}count.textContent=found.length+" Treffer; bis zu 100 Zeilen angezeigt.";}q.addEventListener("input",show);show();</script>'
    core.atomic_replace(ROOT/'FEB-Import-Vergleich.html',core.page('FEB — konsolidierte Importserie',body))
    print(json.dumps(summary,ensure_ascii=False,indent=2))

if __name__=='__main__':
    consolidate() if '--consolidate' in sys.argv else inspect()
