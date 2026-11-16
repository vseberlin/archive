#!/usr/bin/env python3
"""Write review proposals from keyed public records; never apply archive metadata."""
import csv
import json
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STAMP = datetime.now(timezone.utc).isoformat()

# Direct comparisons made during this research, including crops and a mirrored image.
VISUAL = {45:98361,98:103945,110:103976,139:103590,161:103371,
          227:103490,239:103529,243:103535,244:103536,267:103651,
          272:103648,275:103392,280:103773,281:103919,285:103641,
          291:103926,292:103783,294:103927,253:103549,287:103940}

# Deliberately descriptive captions where no identical public catalogue image was found.
CONTEXT = {
41: ('Eisenbahnbrücke und Spaziergänger am Flussufer', 'Undatiert',
     'Blick über einen Fluss auf eine Eisenbahnbrücke. Auf der Uferpromenade im Vordergrund gehen mehrere Personen; Baumzweige rahmen die Ansicht. Die genaue Brücke ist noch nicht belegt.',
     'Nicht sicher bestimmt; Berlin als Sammlungskontext', 'Eisenbahn; Brücke; Fluss; Uferpromenade; Spaziergänger; Stadtansicht'),
44: ('Artisten an einem Trapez im Zirkus', 'Undatiert',
     'Artisten führen eine Luftnummer an einem Trapez beziehungsweise einem erhöhten Gerüst unter einem Zeltdach vor. Seile und Plattformen heben sich vom dunklen Hintergrund ab. Zirkus, Personen und Anlass sind nicht identifiziert.',
     'Nicht belegt', 'Zirkus; Artistik; Trapez; Gerüst; Vorführung'),
47: ('Besuchergruppe an einem ausgestellten Pkw', 'Undatiert',
     'Eine dicht stehende Gruppe betrachtet einen Pkw im Freien. Im Vordergrund sind Dach und Seitenfenster des Fahrzeugs zu sehen. Personen, Fahrzeugtyp, Ort und Anlass sind noch zu bestimmen.',
     'Nicht belegt', 'Pkw; Besuchergruppe; Besichtigung; Veranstaltung'),
149: ('Geschäft „Linie · Farbe · Form“ und Passanten', 'Undatiert; Aufnahmezeit nicht aus der Vergleichsaufnahme übernommen',
      'Straßenansicht mit Passanten vor einem Geschäft, dessen Schaufenster die Aufschrift „Linie · Farbe · Form“ trägt. Darüber erhebt sich eine mehrgeschossige Gebäudefassade. Ein anderer Kurt-Schwarz-Katalogeintrag verortet das gleichnamige Geschäft Unter den Linden 37; die Zuordnung dieser Ansicht bleibt zu prüfen.',
      'Berlin; vermutlich Unter den Linden 37', 'Einzelhandel; Kunstgewerbe; Schaufenster; Passanten; Linie Farbe Form'),
253: ('Autokorso mit Motorradbegleitung vor Zuschauern', 'Undatiert',
      'Ein Pkw mit Fahnen und Motorradbegleitung fährt vor einer großen Zuschauermenge über eine breite Straße. Im Hintergrund sind der Berliner Fernsehturm und mehrgeschossige Gebäude sichtbar. Anlass, Fahrzeuginsassen und genaues Datum sind nicht belegt.',
      'Berlin-Mitte; genaue Straße noch prüfen', 'Autokorso; Motorrad; Zuschauer; Fahnen; Fernsehturm; Berlin'),
276: ('Offener Wagen eines Autokonvois vor jubelnden Zuschauern', 'Vermutlich 1963; Monat und Ereigniszuordnung noch prüfen',
      'Ein offener Wagen fährt an einer dicht stehenden, mit Fahnen winkenden Menschenmenge vorbei. Historische Fassaden begrenzen die Straße. Benachbarte Serienaufnahme und öffentliche Vergleichsbilder sprechen für einen Zusammenhang mit dem Staatsbesuch Nikita Chruschtschows 1963; ein identischer öffentlicher Bildnachweis fehlt.',
      'Berlin; genauer Standort nicht belegt', 'Autokonvoi; Staatsbesuch; Zuschauer; Fahnen; offener Wagen'),
287: ('Kinder und Erwachsene vor einem Wohnhaus', 'Undatiert',
      'Kinder und Erwachsene halten sich vor dem Eingang eines Wohnhauses auf. Neben ihnen stehen Kinderwagen und weitere Gegenstände. Im Vordergrund befinden sich ein Gehweg und eine kleine Grünfläche. Adresse und Anlass sind nicht belegt.',
      'Nicht sicher bestimmt; Berlin als Sammlungskontext', 'Kinder; Erwachsene; Wohnhaus; Hauseingang; Kinderwagen; Alltag'),
290: ('Feuerwehrmann vor einem rauchenden Gebäude', 'Undatiert',
      'Ein Feuerwehrmann mit Helm und Einsatzkleidung steht im Vordergrund. Hinter ihm ist Rauch an einem beschädigten oder brennenden Gebäude zu sehen. Person, Brandort und Aufnahmedatum sind nicht identifiziert.',
      'Nicht belegt', 'Feuerwehr; Feuerwehrmann; Helm; Rauch; Brand; Einsatz'),
299: ('Festzug internationaler Jugendgruppen in Berlin', 'Vermutlich während der X. Weltfestspiele 1973; genauer Tag nicht bestätigt',
      'Eine Gruppe mit einer hohen Fahne und einem Transparent zieht an Zuschauern entlang einer festlich geschmückten Straße vorbei. Der Zusammenhang mit den X. Weltfestspielen 1973 wird durch die benachbarte Aufnahme N01_T041_003 und den dazugehörigen öffentlichen Katalogeintrag gestützt. Die konkrete Delegation ist nicht belegt.',
      'Berlin; genaue Straße nicht bestätigt', 'Festzug; Jugend; Weltfestspiele; Fahnen; Transparent; Zuschauer'),
296: ('Festliche Zusammenkunft in einem geschmückten Saal', 'Undatiert',
      'Mehrere festlich gekleidete Personen stehen oder sitzen an einem Tisch in einem geschmückten Saal. Links befindet sich eine große dekorative Tafel. Der Dateiname Baecker_39 weist auf einen Bäckerhandwerks-Kontext hin; Anlass, Personen und Fotograf sind nicht belegt.',
      'Nicht belegt', 'Festveranstaltung; Saal; Tisch; Dekoration; Bäckerhandwerk (Dateinamenhinweis)'),
297: ('Ausstellungsstand mit Geräten für Speiseeis', 'Undatiert',
      'Ein Ausstellungsstand zeigt zahlreiche Maschinen und Geräte. Mehrere Schilder tragen die Aufschrift „EIS“, weitere werben für Produkte beziehungsweise Preise. Hersteller, Messe, Ort und Fotograf sind noch zu bestimmen.',
      'Nicht belegt', 'Ausstellung; Messe; Speiseeis; Eismaschinen; Geräte; Werbung'),
298: ('Porträt eines Mannes im dunklen Anzug', 'Undatiert',
      'Ein Mann ist vor hellem Hintergrund in einem dunklen Anzug mit weißem Hemd und Fliege porträtiert. Die Aufnahme besitzt einen dunklen Rand. Name, Anlass, Fotograf und Datum sind nicht belegt.',
      'Nicht belegt', 'Porträt; Mann; Anzug; Fliege; Studioaufnahme'),
301: ('Technischer Arbeitsraum mit großen gläsernen Bauteilen', 'Undatiert',
      'Farbaufnahme eines technischen Arbeitsraums mit großen gläsernen Bauteilen beziehungsweise Gefäßen auf angeschlossenen Gestellen. Im Hintergrund stehen Mitarbeiter vor Mess- und Bediengeräten. Betrieb, Bauteiltypen und Fotograf sind noch nicht belegt.',
      'Nicht belegt', 'Technik; Glas; Prüfstand; Messgeräte; Mitarbeiter; Farbfotografie'),
302: ('Frau an einer technischen Mess- oder Prüfanlage', 'Undatiert',
      'Eine Frau bedient eine technische Anlage mit Schaltern, Anzeigen und Leitungen. Die Farbaufnahme zeigt sie seitlich am Arbeitsplatz. Betrieb, Tätigkeit, Personenname und Fotograf sind nicht belegt.',
      'Nicht belegt', 'Frau; Arbeitsplatz; Messtechnik; Prüfanlage; Schalter; Farbfotografie'),
303: ('Menschen auf Bänken in einer Grünanlage', 'Undatiert',
      'Mehrere Menschen sitzen auf Bänken in einer baumbestandenen Grünanlage. Im Vordergrund befinden sich Rasen und blühende Pflanzen. Ort, Anlass, Personen und Fotograf sind nicht belegt.',
      'Nicht belegt', 'Grünanlage; Parkbank; Erholung; Menschen; Pflanzen; Farbfotografie'),
304: ('Frau neben einem binokularen Mikroskop', 'Undatiert',
      'Farbporträt einer lächelnden Frau neben einem binokularen Mikroskop. Ihr Kopf und das optische Gerät stehen im Mittelpunkt der Aufnahme. Person, Einrichtung, Tätigkeit und Fotograf sind nicht belegt.',
      'Nicht belegt', 'Frau; Porträt; Mikroskop; Optik; Arbeitsplatz; Farbfotografie'),
}
CONTEXT_IDS = {149:[103948],276:[103392,103394,103395],299:[109430]}

def main():
    import workbench
    workbench.mount_guard()
    rows=list(csv.DictReader((ROOT/'metadata-proposals.csv').open(encoding='utf-8-sig')))
    candidates=list(csv.DictReader((ROOT/'candidates.csv').open(encoding='utf-8-sig')))
    records={str(x['record']['object_id']):x['record'] for x in map(json.loads,(ROOT/'web-catalogue-evidence.jsonl').read_text().splitlines())}
    matches=list(map(json.loads,(ROOT/'web-match-evidence.jsonl').read_text().splitlines()))
    assert len(rows)==len(candidates)==len(matches)==305
    fields=list(rows[0])+[f for f in ('date_basis','researched_at') if f not in rows[0]]
    corrections=[]
    for r in rows:
        oid=r.get('catalogue_object_id')
        if oid and not r.get('researched_at') and r['proposed_title']!=records[oid]['object_name']:
            corrections.append({'source_relative_path':r['source_relative_path'],'catalogue_object_id':oid,
                'previous_title':r['proposed_title'],'correct_title_for_id':records[oid]['object_name'],
                'reason':'Existing proposal caption belonged to another object; corrected by object_id, never by row order.'})
    if corrections:
        (ROOT/'metadata-caption-corrections.jsonl').write_text(''.join(json.dumps(x,ensure_ascii=False)+'\n' for x in corrections))
    for i,(row,candidate,match) in enumerate(zip(rows,candidates,matches)):
        assert row['source_relative_path']==candidate['source_relative_path']==match['source_relative_path']
        row['researched_at']=STAMP
        best=match['matches'][0] if match['matches'] else None
        oid=str(VISUAL[i]) if i in VISUAL else str(best['object_id']) if best and best['score']<=40 else ''
        if oid:
            obj=records[oid]
            # All copied fields come from this exact keyed record.
            events=[x for x in obj.get('object_events',[]) if x.get('event_type')==10]
            event=events[0] if events else {}
            date=event.get('time',{}).get('time_name','')
            if event.get('event_time_certain')=='n' and date:date='Vermutlich '+date
            image=next((x for x in obj.get('object_images',[]) if x.get('is_main')=='j'),obj['object_images'][0])
            ref=next((x for x in match['matches'] if str(x['object_id'])==oid),None)
            row.update(catalogue_object_id=oid,catalogue_inventory_number=obj.get('object_inventory_number',''),
                proposed_title=obj['object_name'],proposed_description=obj.get('object_description',''),
                proposed_date=date or 'Undatiert im öffentlichen Katalog',
                proposed_creator=event.get('people',{}).get('people_name','Kurt Schwarz'),
                proposed_place=event.get('place',{}).get('place_name',''),
                proposed_tags='; '.join(x['tag_name'] for x in obj.get('object_tags',[])),
                catalogue_url=f'https://berlin.museum-digital.de/object/{oid}',
                catalogue_image_url='https://berlin.museum-digital.de/data/berlin/'+image['folder']+'/'+image['preview'],
                licence=image.get('rights',''),
                match_strength='visually checked catalogue lead' if i in VISUAL else 'strong visual lead' if best['score']<=24 else 'visual lead - human check',
                proposal_basis=('Motiv und Bilddetails wurden direkt mit dem öffentlichen Katalogbild verglichen. Beschnitt oder Orientierung können abweichen. ' if i in VISUAL else 'Bildähnlichkeitsvergleich mit dem öffentlichen Kurt-Schwarz-Katalog, einschließlich Drehung und Beschnitt. ')+
                ('Die Avantmedia-Ansicht ist gegenüber dem Katalogbild gespiegelt. ' if i in (161,253,287) else '')+
                'Titel, Beschreibung, Datierung, Ort und Schlagwörter stammen gemeinsam aus genau diesem Katalogdatensatz. Die abschließende Zuordnung und Freigabe bleiben der menschlichen Prüfung vorbehalten.',
                evidence_urls=f'https://berlin.museum-digital.de/object/{oid}; https://berlin.museum-digital.de/json/object/{oid}',
                date_basis='Datierung aus dem Katalog; für diese Datei gültig, sofern die Bildzuordnung bestätigt wird.')
            for key,value in zip(('phash_distance','dhash_distance','ahash_distance'),ref['distances'] if ref else ('','','')):row[key]=str(value)
            if i==287:
                row['proposed_title']='Junge Familien vor Neubaueingang — Schwarzweißfassung'
                row['proposed_description']+=' Die Avantmedia-Datei zeigt eine schwarzweiße, gespiegelte Fassung des Motivs; die verlinkte Katalogfassung ist farbig. Das Verhältnis der Fassungen ist vor einer Übernahme zu klären.'
            if oid=='103428':
                row['proposed_date']='Juli 1967 laut Titel/Beschreibung; Ereignisfeld nennt 1960–1964 — Konflikt offen'
                row['date_basis']='Quellenkonflikt: Der öffentliche Katalog widerspricht sich zwischen Titel/Beschreibung und strukturiertem Datumsfeld. Keine der Angaben wurde stillschweigend verworfen.'
            elif oid=='103392':
                row['proposed_date']='Vor 28.06.1963 laut Titel; Beschreibung nennt vor 28.07.1963 — Konflikt offen'
                row['date_basis']='Quellenkonflikt: Titel nennt Juni, Beschreibung Juli; das Ereignisfeld gibt 28.06.1963 ohne Einschränkung an. Besuch und genauer Aufnahmetag müssen geprüft werden.'
        elif i==0:
            row['proposal_basis']='Vom Nutzer vorgeschlagene Identifizierung, anhand der Bildansicht geprüft. Der zeitgenössische ND-Bericht vom 03.11.1972 bestätigt die Festivaleröffnung im Kosmos mit „Bändigung des Feuers“. Er identifiziert diese einzelne Aufnahme nicht; der genaue Aufnahmetag bleibt offen.'
            row['date_basis']='November 1972 ist eine kontextgestützte Datierung, kein belegter Aufnahmetag.'
            row['evidence_urls']='https://www.nd-archiv.de/artikel/96439.festival-des-sowjetischen-films.html; https://www.nd-archiv.de/ausgabe/1972-09-13; https://api.deutsche-digitale-bibliothek.de/binary/7bf5b7b8-ee90-4378-b985-cdeeb7c4fcc0.pdf'
        else:
            assert i in CONTEXT, (i,row['filename'],best)
            title,date,description,place,tags=CONTEXT[i]
            for key in ('catalogue_object_id','catalogue_inventory_number','catalogue_url','catalogue_image_url','phash_distance','dhash_distance','ahash_distance','licence'):row[key]=''
            row.update(proposed_title=title,proposed_date=date,proposed_description=description,proposed_place=place,proposed_tags=tags,
                proposed_creator='Unbekannt; Kurt-Schwarz-Zuordnung für diese Datei nicht belegt' if i in (296,297,298,301,302,303,304) else 'Kurt Schwarz (Sammlungszuordnung des Nutzers)',
                match_strength='visual description - identity unconfirmed',
                proposal_basis='Vorsichtige Beschreibung der sichtbaren Aufnahme. Im Abgleich mit 1.661 öffentlichen Kurt-Schwarz-Datensätzen wurde kein identischer Bildnachweis gefunden. Such- und Ähnlichkeits-Leads wurden nicht als Identitätsbeleg übernommen.'+
                (' Die verlinkten Katalogbilder belegen nur einen möglichen Zusammenhang, nicht diese Aufnahme.' if i in CONTEXT_IDS else '')+
                (' Diese Datei liegt außerhalb der eindeutig zugeordneten Kurt-Schwarz-Serien; Urheberschaft und Sammlungszugehörigkeit bitte ausdrücklich prüfen.' if i in (296,297,298,301,302,303,304) else ''),
                evidence_urls='; '.join(f'https://berlin.museum-digital.de/object/{x}' for x in CONTEXT_IDS.get(i,[])),
                date_basis='Keine unabhängig gesicherte Datierung dieser Aufnahme. Vermutungen sind ausdrücklich als solche bezeichnet.')
    temporary=ROOT/'metadata-proposals.csv.tmp'
    with temporary.open('w',newline='',encoding='utf-8') as f:
        w=csv.DictWriter(f,fieldnames=fields);w.writeheader();w.writerows(rows)
    temporary.replace(ROOT/'metadata-proposals.csv')
    summary={'researched_at':STAMP,'candidates':len(rows),'public_catalogue_records':len(records),
        'catalogue_proposals':sum(bool(r['catalogue_object_id']) for r in rows),
        'direct_visual_comparisons':len(VISUAL),'descriptive_or_context_proposals':sum(not r['catalogue_object_id'] for r in rows),
        'corrected_existing_caption_joins':sum(1 for _ in (ROOT/'metadata-caption-corrections.jsonl').open()),'all_have_title_and_description':all(r['proposed_title'] and r['proposed_description'] for r in rows),
        'unverified_authorship_candidates':[r['filename'] for r in rows if r['proposed_creator'].startswith('Unbekannt')],
        'catalogue_date_conflicts':[r['filename'] for r in rows if r['date_basis'].startswith('Quellenkonflikt')],
        'source_or_archive_modified':False,'accession_performed':False,'master_decisions_changed':False,
        'method':'Public catalogue export and 1,661 catalogue-image comparisons, rotation/crop/mirror follow-up, direct visual checks of borderline leads, DDB and contemporary newspaper context searches. Metadata joined strictly by object_id.',
        'limits':'Catalogue matches remain review proposals. Descriptive captions do not establish dates, people or places. No claim that all 305 are distinct new objects or Kurt Schwarz originals.'}
    (ROOT/'metadata-research-summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(summary,ensure_ascii=False,indent=2))

if __name__=='__main__':main()
