#!/usr/bin/env python3
"""Prepare read-only caption choices for the existing TIFF comparison workbench."""
import csv
import json
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
UNIT = Path('/mnt/volume/iss-archive/units/kurt-schwarz')
SOURCE = '/media/vladimir/Elements/restore/archive/Avantmedia/'


def main():
    stamp = datetime.now(timezone.utc).isoformat()
    public = {}
    for name in ['web-catalogue-evidence.jsonl', 'metadata-review-web-evidence.jsonl']:
        for line in (ROOT / name).read_text().splitlines():
            row = json.loads(line)
            if 'record' in row:
                public['md:' + str(row['record']['object_id'])] = row
    records = {}
    with (UNIT / 'CATALOG.csv').open() as handle:
        for row in csv.DictReader(handle):
            if row['file_role'] != 'preservation_master':
                continue
            key = row['record_key']
            records[key] = {k: row[k] for k in ['record_key', 'inventory', 'title', 'description',
                                               'date_text', 'creator', 'places', 'tags']}
            records[key].update(basis='Current accession catalogue; assignment requires visual review',
                                catalogue_url=f"https://berlin.museum-digital.de/object/{row['md_object_id']}" if row['md_object_id'] else '',
                                current_master_path=str(UNIT / row['master_relative_path']))
    # Preserve former identities as explicit choices; never silently reverse a curator alias.
    aliases = list(csv.DictReader((UNIT / 'db-restore/manifests/object-aliases.csv').open()))
    for alias in aliases:
        key = alias.get('alias_record_key', '')
        if key and key not in records:
            records[key] = dict(record_key=key, inventory=alias.get('alias_inventory', ''),
                                title=alias.get('alias_title', ''), description='', date_text='',
                                creator='', places='', tags='', current_master_path='',
                                basis='Former catalogue identity; existing alias decision requires explicit review',
                                catalogue_url=f"https://berlin.museum-digital.de/object/{key.split(':')[-1]}",
                                prior_alias_decision=alias)
    for key, record in records.items():
        capture = public.get(key)
        if not capture:
            continue
        obj = capture['record']
        record['public_record'] = dict(title=obj['object_name'], description=obj['object_description'],
                                      inventory=obj['object_inventory_number'], retrieved_at=capture['retrieved_at'],
                                      evidence_url=capture['url'])
        if not record['title'] or record.get('prior_alias_decision'):
            record['title'] = obj['object_name']
            record['description'] = obj['object_description']
            events = obj.get('object_events', [])
            record['date_text'] = '; '.join(e.get('time', {}).get('time_name', '') for e in events)
            record['creator'] = '; '.join(e.get('people', {}).get('people_name', '') for e in events)
            record['places'] = '; '.join(e.get('place', {}).get('place_name', '') for e in events)
            record['tags'] = '; '.join(t['tag_name'] for t in obj.get('object_tags', []))
        images = obj.get('object_images', [])
        if images:
            im = next((i for i in images if i.get('is_main') == 'j'), images[0])
            record['public_image_url'] = 'https://berlin.museum-digital.de/data/berlin/' + im['folder'] + '/' + im['preview']
        record['public_text_differs'] = (record['title'] != obj['object_name'] or
                                         ' '.join(record['description'].split()) != ' '.join(obj['object_description'].split()))

    proposals = [
        dict(source_relative_path='1624762/Ersatzoriginale/A04_T85_001.tif',
             title='Filmtheater Kosmos bei Nacht zum Festival des sowjetischen Films',
             date_text='November 1972; vermutlich im Umfeld der Festivaleröffnung am 2. November',
             description='Nächtliche Außenansicht des Filmtheaters Kosmos an der Karl-Marx-Allee in Berlin. Vor dem beleuchteten Eingang stehen Besucher und Fahnen. Rechts wirbt ein Schaukasten für den sowjetischen Spielfilm „Bändigung des Feuers“ im Rahmen des Festivals des sowjetischen Films.',
             places='Berlin; Friedrichshain; Karl-Marx-Allee; Filmtheater Kosmos',
             tags='Kino; Nachtaufnahme; sowjetischer Film; Filmfestival; Bändigung des Feuers; DDR; 1972',
             basis='Motiv und Plakat im TIFF; zeitgenössische Veranstaltungsnachrichten. Kein exakter öffentlicher Bildnachweis gefunden.',
             uncertainty='Der Festivalbeginn am 2. November 1972 ist belegt; der genaue Aufnahmetag dieses Fotos ist nicht belegt.',
             evidence=[dict(url='https://www.nd-archiv.de/ausgabe/1972-10-31', label='Neues Deutschland, 31.10.1972', supports='Festivalzeitraum und Eröffnungsfilm'),
                       dict(url='https://www.nd-archiv.de/ausgabe/1972-11-01', label='Neues Deutschland, 01.11.1972', supports='Ankündigung der Eröffnung am folgenden Donnerstag'),
                       dict(url='https://api.deutsche-digitale-bibliothek.de/binary/7bf5b7b8-ee90-4378-b985-cdeeb7c4fcc0.pdf', label='TRO-Betriebszeitung, Seite 3, DDB', supports='Eröffnung am 2. November und Film Bändigung des Feuers')]),
        dict(source_relative_path='1640136_MF/Ersatzoriginale/K06_F076_002.tif',
             title='Transformatorenwerk Oberschöneweide (TRO): Blick von oben in eine Produktionshalle',
             date_text='Vermutlich 1970; Monat nicht gesichert',
             description='Blick von einem erhöhten Standort in eine hohe, durch große Fenster belichtete Werkhalle des Transformatorenwerks Oberschöneweide. Zu sehen sind Fertigungsplätze und Bauteile für den Transformatorenbau. Die Aufnahme zeigt einen weiteren Blickwinkel als die bereits katalogisierten Aufnahmen der Serie KS-7-BZ_0781.',
             places='Berlin; Oberschöneweide; Transformatorenwerk Karl Liebknecht (TRO)',
             tags='Industrie; Transformatorenbau; Produktionshalle; Fabrik; Elektrotechnik; DDR; 1970',
             historical_inventory_lead='KS-7-BZ_0781-6',
             basis='Historische lokale Inventarzuordnung KS-7-BZ_0781-6 und Motivvergleich mit der öffentlich katalogisierten TRO-Serie. Kein exakter öffentlicher Bildnachweis für Bild 6 gefunden.',
             uncertainty='Jahr aus dem Serienzusammenhang erschlossen. Nachbaraufnahmen nennen teils Juni, teils August 1970; deshalb wird kein Monat übertragen. Die historische Inventarnummer ist ein Beleg, keine neue Inventarvergabe.',
             evidence=[dict(url='https://berlin.museum-digital.de/object/103826', label='Industriesalon: TRO, Bild 1', supports='Werkhalle, Serienkontext, Juni 1970'),
                       dict(url='https://berlin.museum-digital.de/object/103827', label='Industriesalon: TRO, Bild 2', supports='Gleiche Inventarserie; August 1970 zeigt Monatsunsicherheit'),
                       dict(url='https://berlin.museum-digital.de/object/103830', label='Industriesalon: TRO, Bild 5', supports='Produktionshalle für Leistungstransformatoren; verwandtes, anderes Bild'),
                       dict(url='https://www.industriesalon.de/wp-content/uploads/2021/03/TRO_Chronik.pdf', label='Industriesalon: TRO-Chronik', supports='Betriebsgeschichte; kein Datierungsbeleg für diese Aufnahme')]),
        dict(source_relative_path='1714004_KB/Ersatzoriginale/N01_T041_002.tif',
             title='Fahnenträger bei der Eröffnungsparade der X. Weltfestspiele in Ost-Berlin',
             date_text='Wahrscheinlich 28. Juli 1973 (Serien- und Ereigniskontext)',
             description='Fahnenträger führen einen Festzug an; dahinter gehen Teilnehmer einer internationalen Jugenddelegation. An beiden Straßenseiten verfolgen zahlreiche Zuschauer die Parade. Die Aufnahme gehört wahrscheinlich zum Zug zum Stadion der Weltjugend am Eröffnungstag der X. Weltfestspiele der Jugend und Studenten.',
             places='Berlin; Mitte; vermutlich Strecke Friedrichstraße–Chausseestraße zum Stadion der Weltjugend',
             tags='X. Weltfestspiele 1973; Jugend; Festzug; Fahnenträger; Fahnen; Zuschauer; internationale Delegationen; DDR',
             basis='Motivvergleich und unmittelbarer Scankontext zu N01_T041_003_b / md:109430 und N01_T041_001 / md:109433. Eigenständiger Moment mit Fahnenträgern; keine Gleichsetzung mit dem Nachbarfoto.',
             uncertainty='Tag und Route aus Nachbaraufnahmen erschlossen, kein exakter öffentlicher Bildnachweis. Deren Titel und Ereignisfelder nennen 28.07.1973, während ein allgemeiner Beschreibungsschluss Anfang August nennt; dieser Widerspruch bleibt dokumentiert. Einzelpersonen und genauer Straßenabschnitt sind nicht sicher bestimmt.',
             evidence=[dict(url='https://berlin.museum-digital.de/object/109430', label='Industriesalon: Weltfestspiele, Bild 2-A', supports='Parade und Route; Titel/Ereignisdatum 28.07.1973; anderes Einzelbild'),
                       dict(url='https://berlin.museum-digital.de/object/109433', label='Industriesalon: Weltfestspiele, Bild 2-D', supports='Zuschauer derselben Eröffnungsparade; Datierungswiderspruch im Beschreibungsschluss'),
                       dict(url='https://www.stasi-mediathek.de/embed/eroeffnungsfeier-der-x-weltfestspiele-der-jugend-und-studenten-1973-in-ost-berlin/blatt/16/embed/1/', label='Bundesarchiv: Eröffnungsfeier am 28.07.1973', supports='Unabhängiger Ereignisbeleg; andere Fotos und anderer Urheber')]),
    ]
    for p in proposals:
        p.update(source_path=SOURCE+p['source_relative_path'], creator='Kurt Schwarz', researched_at=stamp,
                 creator_basis='Vom Nutzer bestätigte Kurt-Schwarz-Provenienz und lokaler Serienzusammenhang',
                 user_confirmed_new=True, metadata_reviewed=False, selected_master_path='',
                 accession_authorized=False, public_exact_image_match=False)
    (ROOT/'new-object-metadata-proposals.jsonl').write_text(''.join(json.dumps(p,ensure_ascii=False)+'\n' for p in proposals))
    (ROOT/'tiff-review-metadata.json').write_text(json.dumps(records,ensure_ascii=False))
    print(json.dumps({'existing_metadata_choices':len(records),'researched_new_objects':len(proposals),'public_records_available':len(public)}))


if __name__ == '__main__':
    main()
