"""Build read-only review reports from completed comparison evidence.

No catalogue, decisions, source, or accession writes. No new directories.
"""
import base64
import csv
import html
import io
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import retained_visual_review as r

r.guard()
W, U = r.W, r.U
data = json.loads((W / 'retained-visual-comparison.json').read_text())
preliminary = json.loads((W / 'retained-local-metadata-candidates.json').read_text())
evidence = {x['source_path']: x for x in json.loads((W / 'drain-evidence.json').read_text())}
retained = list(csv.DictReader((W / 'retain-review.csv').open()))
retained_paths = {x['source_path'] for x in retained}
by_path = {p: g for f in data['families'] for g in f['groups'] for p in g['paths']}
family_by_path = {p: f['review_row'] for f in data['families'] for g in f['groups'] for p in g['paths']}
refs = {}
for ref in data['references']:
    refs.setdefault(ref['inventory'], []).append(ref)
workbook = json.loads((W / 'retained-local-workbook-evidence.json').read_text())[0]['source']
now = datetime.now(timezone.utc).isoformat()

# Review conclusions are proposals. No field below selects or replaces a Master.
notes = {
 'VLV-1017': ('Bireka ticket-printing machine and operator', '1955/56, according to the workbook', 'Distinct printed photograph, labelled Bild 8. The archived machine illustrations VLV-1039 and VLV-1114 show different views.', 'Local row identifies the Kroll engineering appendix and Sammlung Seiffert. Review the complete printed page as the candidate object.', 'VLV-1039'),
 'VLV-1019-1': ('Packaging department', '1980s, according to the workbook', 'Distinct department view; no identical image found in the archived atlas. The unreadable VLV-1019 file is a separate unresolved source.', 'An existing local row gives subject, institutional context and period. Do not use this photograph as proof of what the unreadable VLV-1019 contains.', 'VLV-1064'),
 'VLV-1025': ('Women exercising during a work break', 'c.1955, according to the workbook', 'Outdoor group doing exercises, visually distinct from the archived staff and workplace photographs.', 'Sammlung Seiffert is named in the local description. Two exact-copy paths exist; available resolution is modest.', None),
 'VLV-1028': ('Kasimir Bong at a Perfecta cutting machine', '1950s, according to the workbook', 'Compared explicitly with VLV-1135-2: same named worker, different machine, viewpoint and photograph. VLV-1135-2 shows a tall rotary press.', 'TIFF is only 654 × 466. The workbook says the physical object is missing; the digital image exists. Person and machine identification come from the workbook.', 'VLV-1135-2'),
 'VLV-1029': ('Paragon anniversary staff group', '1923, according to the workbook', 'Distinct large group photograph with a 25-year anniversary wreath. Two local crops of the same photograph need one Master choice.', 'The year and anniversary interpretation are local metadata assertions; do not derive an exact date solely from the wreath.', None),
 'VLV-1036': ('Worker at a packaging machine', '1975, according to the workbook', 'Related to VLV-1037, but a different exposure: the foreground worker, colleague and paper positions differ. Neither was found in the archive atlas.', 'Keep 1036 and 1037 as separate proposed photograph identities; compare each crop pair for one Master.', None),
 'VLV-1037': ('Bernd Neitzel and colleague at a heat-sealing machine', '1975, according to the workbook', 'Different exposure from VLV-1036, with different poses and paper-stack positions; a shared machine or event does not make it the same photograph.', 'The personal name is supplied by the local workbook. Two crop versions need comparison.', None),
 'VLV-1038': ('Two workers at a continuous-paper machine', 'c.1985, according to the workbook', 'Distinct photograph of two workers beside stacked continuous paper; no identical archived image found.', 'Local metadata supplies the process, institution and approximate period. Compare the wider and narrower JPEG versions.', None),
 'VLV-1041': ('Vomag ticket rotary press, Bild 6', 'c.1955, according to the workbook', 'Printed machine photograph is different from the archived Bireka and Endlosdruck illustrations.', 'The JPEG has a different page canvas/orientation from the TIFF. Inspect the complete content before selecting one Master.', 'VLV-1039'),
 'VLV-1042': ('Paragon 1,000-Reichsmark share certificate', 'Printed: June 1925; workbook: c.1931', 'Distinct certificate image, number 000306; no identical accessioned image found. A smaller retained Paragon-Aktie 1925.jpg appears to reproduce the same certificate.', 'Conditional: reconcile the printed June 1925 date with the workbook date; establish the reproduction source and holding/provenance. The old workbook marks this row as not being worked on further. That is historical evidence, not a current accession decision.', None),
 'VLV-1043': ('Woman working at a computer workstation', 'c.1985, according to the workbook', 'Different exposure from archived VLV-1027: here the woman operates the computer; there she holds a floppy disc. A 5640 × 4272 TIFF survives.', 'Local workbook links the scene to Vordruck-Leitverlag and Sammlung Seiffert. TIFF/JPEG orientation differs; displayed previews honour orientation.', 'VLV-1027'),
 'VLV-1044': ('Staff group for May Day', 'Mid-1950s, according to the workbook', 'Distinct group below the Es lebe der 1. Mai banner; no identical photograph found in the archive atlas.', 'Subject, event and approximate period are available. Existing TIFF is small, 800 × 569.', None),
 'VLV-1045': ('Staff celebration at Riviera', '1960s, according to the workbook', 'Three-photograph montage with order numbers, distinct from the archived carrier layouts.', 'Treat the existing montage as one proposed object for this review. Do not invent three component identifiers from this recommendation.', None),
 'VLV-1046': ('Vordruck-Leitverlag canteen', 'c.1985, according to the workbook', 'Different kitchen view from the posed canteen group on VLV-1131-1; the room and staff arrangement differ.', 'Local description establishes institution and approximate date. One modest-resolution JPEG is available.', 'VLV-1131-1'),
 'VLV-1047': ('Full rectangular staff-party photograph', 'Conflict: workbook c.1985; carrier context 1973/74', 'Same exposure as the lower-middle circular photograph on carrier VLV-1148, but the local rectangular version preserves substantial content outside that circle.', 'Supporting-image placement review, not an entirely new scene. Reconcile the local c.1985 date with the carrier caption for year-end 1973 / the 1974 plan; compare the retained VLV-1148 raw sections too.', 'VLV-1148'),
 'VLV-1048': ('Military-band concert in the factory hall', 'Mid-1950s, according to the workbook', 'Distinct event photograph: musicians and audience in the hall, rather than the ordinary production view in the archive.', 'Sammlung Seiffert and the event context are supplied by the local row; no exact day should be invented.', 'VLV-1134-1'),
 'VLV-1049': ('English rotary press', 'Workbook c.1946/47', 'Same photograph is already accessioned under VLV-1069. The inventory-number gap is not missing visual content.', 'Do not propose a second object. Retain the old local label as provenance/alias evidence if reconciled later.', 'VLV-1069'),
 'VLV-1057': ('Mismatched label: ticket specimen page', 'Unresolved', 'The actual VLV-1057.jpg is Druckmuster 41 with ticket specimens, while its workbook row describes a woman with a floppy disc. The separately retained VLB-1057.jpg is the computer image already archived as VLV-1027.', 'Do not attach the computer metadata to this ticket page. Its specimens correspond to archived VLV-1085, VLV-1163, VLV-1087 and VLV-1162; page context may be useful, but it is not an established new photograph object.', 'VLV-1027'),
 'VLV-1065': ('Roll-cutting machine under an old label', 'Workbook c.1946/47; archived description differs', 'Same photograph as archived VLV-1111. The local title Drent-Endlosdruckmaschine conflicts with the more specific description.', 'Do not propose a duplicate object or silently replace the archived metadata with the old row.', 'VLV-1111'),
 'VLV-1095-1': ('Invitation interior', '1 May 1962, according to the workbook', 'Already preserved as a complementary Master member of the accessioned VLV-1095 invitation. The retired component label creates a false gap.', 'No additional object or duplicate accession is recommended.', 'VLV-1095'),
 'VLV-1099': ('Monthly production plan', 'August 1958, according to the local description', 'The named 574 × 800 preview visually matches IMG_20190815_154034 scans, including 3455 × 4812 TIFF versions. No matching accessioned object found.', 'Compare the TIFF versions and the 3840 × 5120 camera JPEG before choosing one Master. The local description says August 1958; an intermediate title says 1956. Preserve that conflict rather than copying 1956 into the final date.', None),
 'VLV-1101': ('Weiterverarbeitung department group', 'c.1950, according to the workbook', 'Distinct group with a Stalin portrait and Soviet-Union slogan, absent from the reviewed archive atlas.', 'The old workbook marks the physical item as missing. This review locates digital copies only; it does not establish the present physical holding.', None),
 'VLV-1102': ('Five women at a staff celebration', 'Late 1940s, according to the workbook', 'Distinct indoor group photograph; a 3128 × 2003 TIFF survives in addition to small JPEG copies.', 'The local row supplies Paragon / Sammlung Seiffert provenance and a broad period. Personal identities remain unspecified.', None),
 'VLV-1103-1': ('Kurt Timm and two colleagues at a four-spindle drill', 'Photograph date unspecified', 'Explicitly compared with VLV-1103: same machine and men, different exposure and actions. In this photograph they look down and work at the machine.', 'The local description names Timm and his 1953–1970 tenure. That tenure is not an exact photograph date. TIFF and JPEG are both only 800 × 574.', 'VLV-1103'),
 'VLV-1113': ('Goebel ticket rotary-press technical drawing', 'Undated', 'Large 4960 × 3507 drawing with visible Goebel / Darmstadt manufacturer information, distinct from the archived machine photographs.', 'The local row is historically marked as a draft. The visible manufacturer, drawing subject and Sammlung Seiffert attribution provide useful minimum metadata; leave an unsupported date unknown.', 'VLV-1114'),
 'VLV-1019': ('Packaging department: unreadable source', '1980s, according to the workbook', 'The exact retained VLV-1019.jpg cannot be decoded, so its visual identity and relationship to VLV-1019-1 are unconfirmed.', 'Hold for source recovery or an independently proved matching copy. A metadata row alone is insufficient to recommend this unreadable file.', None),
}
excluded = {'VLV-1019', 'VLV-1049', 'VLV-1057', 'VLV-1065', 'VLV-1095-1'}
priority = ['VLV-1043', 'VLV-1113', 'VLV-1099', 'VLV-1102', 'VLV-1103-1', 'VLV-1028', 'VLV-1017', 'VLV-1041', 'VLV-1019-1', 'VLV-1025', 'VLV-1029', 'VLV-1036', 'VLV-1037', 'VLV-1038', 'VLV-1044', 'VLV-1045', 'VLV-1046', 'VLV-1048', 'VLV-1101', 'VLV-1042', 'VLV-1047']

def source_record(path):
    assert path in retained_paths
    e = evidence[path]
    g = by_path.get(path, {})
    return dict(path=path, stored_sha256=e['source_sha256'], size_bytes=e['source_size_bytes'], mtime_ns=e['source_mtime_ns'], width=g.get('width'), height=g.get('height'), visual_family=family_by_path.get(path))

findings = []
for c in preliminary:
    inv = c['inventory']
    title, date, distinction, qualification, comparison_inv = notes[inv]
    paths = set(c['paths'])
    # Expand exact stored-hash copies, not loose display-family membership.
    for p in list(paths):
        paths.update(by_path.get(p, {}).get('paths', []))
    if inv == 'VLV-1099':
        paths.update(p for p in by_path if 'IMG_20190815_154034' in p)
    paths = sorted(paths)
    readable = [p for p in paths if p in by_path]
    ranked = sorted(readable, key=lambda p: (-(by_path[p]['width'] * by_path[p]['height']), Path(p).suffix.lower() not in ['.tif', '.tiff'], p))
    display = ranked[0] if ranked else None
    if inv == 'VLV-1099':
        display = next(p for p in paths if p.endswith('/Paragon_Tif/IMG_20190815_154034.tif') and '/WF/' in p)
    comparison = refs.get(comparison_inv, [])[:1]
    comparison_kind = 'identified relationship or explicit distinction'
    if not comparison and readable:
        near = by_path[readable[0]]['nearest'][0]
        comparison = refs[near['inventory']][:1]
        comparison_kind = 'visual search lead only; not an identified match'
    classification = 'plausible_additional_object'
    if inv in excluded: classification = 'not_recommended_as_additional_object'
    if inv == 'VLV-1042': classification = 'conditional_additional_object'
    if inv == 'VLV-1047': classification = 'fuller_photo_of_existing_carrier_image'
    findings.append(dict(inventory=inv, title=title, proposed_classification=classification, date_evidence=date, visual_finding=distinction, qualifications=qualification, metadata_source=dict(path=workbook, sheet='Tabelle1', row=c['workbook_row'], values=c['metadata']), source_candidates=[source_record(p) for p in paths], display_source=display, comparison_kind=comparison_kind, archived_comparisons=[{k: v for k, v in x.items() if k in ['inventory', 'path', 'title', 'description', 'year']} for x in comparison], public_md_inventory_search='No result in live collection 593 export (216 public records) or exact inventory searches, checked 2026-09-17; private/unpublished records not checked.', selected_master=None, decision='unresolved', accession_authorized=False))

by_inv = {c['inventory']: c for c in findings}
candidates = [by_inv[i] for i in priority]
non_candidates = [c for c in findings if c['inventory'] in excluded]
carriers = []
for inv in ['VLV-1148', 'VLV-1151', 'VLV-1155', 'VLV-1156']:
    paths = sorted(p for p in retained_paths if '/Rohscans 03/' in p and Path(p).name.startswith(inv + '-'))
    carrier_ref = refs[inv][0]
    carriers.append(dict(inventory=inv, title=carrier_ref['title'], description=carrier_ref['description'], overview=carrier_ref['path'], source_sections=[source_record(p) for p in paths], finding='Additional high-resolution carrier coverage for review; the complete low-resolution carrier is already accessioned. Overlapping sections and grouped photographs are not automatically separate objects.', decision='unresolved', selected_master=None, accession_authorized=False))
assert len(candidates) == 21 and len(non_candidates) == 5
assert sum(len(c['source_sections']) for c in carriers) == 16

path_notes = {}
for c in findings:
    for s in c['source_candidates']:
        path_notes[s['path']] = dict(inventory=c['inventory'], proposal=c['proposed_classification'], explanation=c['visual_finding'])
for c in carriers:
    for s in c['source_sections']:
        path_notes[s['path']] = dict(inventory=c['inventory'], proposal='carrier_coverage_review', explanation=c['finding'])
errors = {p: x['error'] for x in data['errors'] for p in x['paths']}
nonimages = {x['source_path'] for x in data['nonimages']}
path_findings = []
for row in retained:
    p = row['source_path']
    rec = dict(source_record(p), original_drain_state=row['drain_state'], deletion_authorized=False)
    rec.update(path_notes.get(p, dict(inventory=row['inventory'], proposal='no_additional_candidate_identified_in_this_screen', explanation='Represented in the visual-family screening; no further metadata-supported additional object identified in this pass. This is not proof of redundancy or absence of unique detail.')))
    if p in errors:
        rec.update(proposal='decode_unresolved', explanation=errors[p], visually_reviewed=False)
    elif p in nonimages:
        rec.update(proposal='nonimage_document_or_metadata', explanation='Retained document or metadata version; outside the photograph-candidate recommendation.', visually_reviewed=False)
    else:
        rec['visually_reviewed'] = 'Representative of this display family reviewed; individual exact-hash copies were not all opened separately.'
        rec['contact_sheet'] = f"retained-comparison-{(rec['visual_family'] - 1) // 20 + 1:02}.jpg"
    path_findings.append(rec)
assert len(path_findings) == len({x['path'] for x in path_findings}) == 1132
assert len(by_path) + len(errors) + len(nonimages) == 1132

summary = dict(retained_paths=1132, unique_retained_hashes=len({x['source_sha256'] for x in retained}), image_paths=1121, unique_image_hashes=868, decodable_image_paths=len(by_path), decodable_image_hash_groups=sum(len(f['groups']) for f in data['families']), visual_display_families=len(data['families']), visual_family_contact_sheets=28, archive_access_images=len(data['references']), archive_represented_objects=len({x['inventory'] for x in data['references']}), metadata_linked_initial_labels=26, plausible_additional_objects=19, conditional_additional_objects=1, fuller_photo_of_existing_carrier_image=1, excluded_or_unresolved_labels=5, raw_carrier_section_paths=16, carrier_families=4, undecodable_image_paths=len(errors), retained_nonimage_paths=len(nonimages), public_md_collection_records=216)
r.dump(W / 'retained-accession-candidates.json', dict(checked_at=now, purpose='Review proposals only; no new selections, accession, catalogue writes or drain changes.', summary=summary, candidates=candidates, not_recommended_as_additional_objects=non_candidates, carrier_coverage=carriers, limitations=['All paths were screened; visual review used 557 family representatives and 26 enlarged metadata-linked cases, not every byte-distinct file at full resolution.', 'Similarity grouping is a display aid, not a deduplication or object-identity decision.', 'No public MD result does not prove absence from private or unpublished MD records.', 'The 20 undecodable files remain unresolved.', 'Other unnamed scans can contain context or unique detail despite no additional metadata-supported object being identified in this pass.']))
r.dump(W / 'retained-review-path-findings.json', dict(checked_at=now, method='Per-path trace to stored hash, display family, contact sheet and review recommendation. Does not change authoritative disposition.', paths=path_findings))

report = [
 '# Paragon retained-file visual review — 17 September 2026', '',
 '**Result: 19 plausible additional objects with usable local metadata, one conditional share-certificate candidate, and one fuller photograph already represented as a circular crop on an existing carrier.** These are proposals for human review, not accession decisions.', '',
 '[Open the visual comparison gallery](retained-visual-review.html). [Structured candidate evidence](retained-accession-candidates.json). [All 1,132 source-path findings](retained-review-path-findings.json).', '',
 '## Scope and evidence', '',
 '- Screened all **1,132 retained paths**: 1,121 image paths and 11 document/metadata paths. The image paths contain 868 stored hash groups; 848 groups / 1,101 paths decode successfully. The remaining 20 image paths are unresolved.',
 '- Reviewed **557 visual display families** across 28 contact sheets against **242 accessioned access images representing 239 objects**, and reviewed the complete seven-sheet archive atlas. Enlarged all 26 metadata-linked inventory gaps. Similar-looking family members were grouped only for display; this does not establish their identity or redundancy.',
 '- Reused existing checksums and visual-index features. Source size/mtime were checked against the stored hash evidence. The protected Elements mount is exactly `/dev/sdf1 /media/vladimir/Elements ntfs3 ro`.',
 '- Metadata comes principally from the accessioned `Bildtexte Paragon stand 05.02.2020.xlsx`, sheet `Tabelle1`, with exact row references below. Historical workbook statements, names, dates and physical-holding notes remain attributed evidence.',
 '- On 2026-09-17, fetched all **216 public records** in [museum-digital collection 593](https://berlin.museum-digital.de/index.php?t=sammlung&gesusa=593), and searched each of the 26 inventory labels. None of those labels returned a public record. The control query VLV-1003 returned its known record. Some images nevertheless exist under other identifiers, as visual comparison shows. Private/unpublished MD records were not checked.', '',
 '## Plausible additional objects', '',
 '| Local label | Subject / available source | Why it adds content | Metadata row |',
 '| --- | --- | --- | --- |',
]
def md_escape(s): return str(s).replace('|', '\\|').replace('\n', ' ')
for c in candidates:
    if c['proposed_classification'] != 'plausible_additional_object': continue
    s = next(s for s in c['source_candidates'] if s['path'] == c['display_source'])
    report.append(f"| {c['inventory']} | {md_escape(c['title'])}; {s['width']} × {s['height']} {Path(s['path']).suffix[1:].upper()} | {md_escape(c['visual_finding'])} | {c['metadata_source']['row']} |")
report += ['', 'The displayed source is a useful comparison candidate, **not a selected Master**. A larger image or TIFF extension alone does not establish the best Master. Exactly one Master must be chosen for each resolved object, with other candidates recorded as redundant without authorizing deletion.', '', '## Qualifications and two additional review cases', '']
for c in candidates:
    report += [f"- **{c['inventory']}** — {c['date_evidence']}. {c['qualifications']}"]
report += ['', '## Apparent gaps that are not additional-object recommendations', '', '| Label | Finding |', '| --- | --- |']
for c in non_candidates: report.append(f"| {c['inventory']} | {md_escape(c['visual_finding'])} {md_escape(c['qualifications'])} |")
report += ['', '## Existing carriers with retained higher-resolution sections', '', 'These **16 files across four existing carriers are not 16 new objects**. Review the carrier and overlapping sections together to establish distinct coverage and placement. No new component identifiers or Master members are created by this report.', '']
for c in carriers:
    report += [f"- **{c['inventory']}**: {len(c['source_sections'])} raw sections — " + ', '.join('`' + Path(s['path']).name + '`' for s in c['source_sections']) + '.']
report += ['', 'VLV-1130-5 has already been accessioned in the earlier authorized step and is not proposed again here. Other raw sections may also add edges or context; the four groups above are priority coverage cases, not an exhaustive declaration that every other section is redundant.', '', '## Limits and retained exceptions', '',
 'The 11 nonimage paths consist of nine metadata-version paths and two document/research paths; they remain retained. Unassigned print-sample pages and unnamed scans were screened visually, but this pass did not establish sufficient identity and metadata for further standalone objects. Some preserve page or carrier context. For example, the VLV-1057 ticket page contains specimens already represented separately; context should not be confused with a new photograph. The unnamed IMG_20190409 scans and invoice-front/back attribution still need object-level reconciliation before any new-object claim.', '',
 'The 20 decode failures remain unresolved, including the exact VLV-1019 source. Decode failure, absent labels and perceptual distance do not prove genuine loss, uniqueness or safe deletion.', '',
 'No source files, archive objects, Masters, catalogue records, saved decisions or drain states were changed. No accession, publication or deletion was performed. Review illustrations exist only in the work area.', '',
 '## Review files', '',
 '- `retained-accession-candidates.json`: proposed classifications, full local metadata, exact source paths, reused hashes, dimensions and archived comparisons.',
 '- `retained-review-path-findings.json`: all 1,132 retained source paths, including contact-sheet references and unresolved errors.',
 '- `retained-visual-comparison.json`: 868 image-hash-group screening evidence, 557 decodable display families and all 242 archived references.',
 '- `retained-comparison-01.jpg` through `retained-comparison-28.jpg`: complete visual-family comparison contact sheets.',
 '- `retained-archive-atlas-01.jpg` through `retained-archive-atlas-07.jpg`: complete accessioned access-image atlas.',
 '- `retained-candidates-detail-01.jpg` through `retained-candidates-detail-07.jpg`: enlarged initial candidate comparisons.',
 '- `retained-focused-metadata-comparison.jpg` and `retained-cutting-machine-comparison.jpg`: explicit date/context and same-person/different-machine checks.',
 '- `retained-md-current-collection.json` and `retained-md-inventory-search.json`: timestamped live MD search evidence.', '',
 '## Exact undecodable source paths', '',
]
for p, err in errors.items(): report.append(f'- `{p}` — {err}')
(W / 'RETAINED_VISUAL_REVIEW.md').write_text('\n'.join(report) + '\n')

def esc(value): return html.escape(str(value))
def link(path, label=None):
    return f'<a href="{quote(str(path), safe="/:.")}">{esc(label or path)}</a>'
def embedded(path, caption):
    if not path: return '<figure><div class="missing">Source cannot be decoded</div><figcaption>' + esc(caption) + '</figcaption></figure>'
    im = r.preview(path, 1000).copy()
    im.thumbnail((960, 760))
    buf = io.BytesIO(); im.save(buf, format='JPEG', quality=86)
    return '<figure><img loading="lazy" src="data:image/jpeg;base64,' + base64.b64encode(buf.getvalue()).decode() + '" alt="' + esc(caption) + '"><figcaption>' + esc(caption) + '</figcaption></figure>'

parts = ['''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Paragon — retained image review</title>
<style>
:root{color-scheme:light;font-family:system-ui,sans-serif;color:#202a29;background:#f1f0e9}body{margin:0 auto;max-width:1240px;padding:28px}h1{font-size:36px;letter-spacing:-1px;margin-bottom:12px}h2{font-size:25px;margin:42px 0 16px}p,li{line-height:1.6}a{color:#156354;text-underline-offset:3px}.intro{max-width:880px}.metrics{display:flex;gap:12px;flex-wrap:wrap;margin:24px 0}.metric{background:#163f38;color:white;border-radius:8px;padding:16px 22px}.metric strong{font-size:26px;display:block}.metric span{font-size:13px}nav{display:flex;gap:22px;flex-wrap:wrap;padding:18px 0;border-top:1px solid #bdc9bd;border-bottom:1px solid #bdc9bd}.card{background:#fff;border:1px solid #d5dbd2;border-radius:10px;padding:24px;margin:22px 0}.card h3{font-size:23px;margin:5px 0 12px}.tag{font-size:12px;letter-spacing:.04em;text-transform:uppercase;color:#426658}.pair{display:grid;grid-template-columns:1fr 1fr;gap:20px}figure{margin:10px 0}figure img{width:100%;height:410px;object-fit:contain;background:#f5f5f1}figcaption{font-size:13px;line-height:1.5;padding-top:8px;color:#526059}.note{border-left:3px solid #c5ad58;background:#faf8ec;padding:12px 16px}.meta{font-size:14px;color:#526059}.paths{font-size:13px;overflow-wrap:anywhere}.paths li{margin:12px 0}summary{cursor:pointer;font-weight:600;padding:10px 0}.missing{height:260px;display:grid;place-items:center;background:#f6e9e4;color:#a43222}.sheetlinks{display:flex;gap:12px;flex-wrap:wrap}.sheetlinks a{border:1px solid #cad3c8;padding:8px 13px;border-radius:4px}.limits{background:#e9e7db;padding:16px 22px;border-radius:8px}footer{margin:36px 0;font-size:13px;color:#526059}@media(max-width:760px){body{padding:16px}.pair{grid-template-columns:1fr}.card{padding:16px}figure img{height:330px}h1{font-size:29px}}
</style><body><header><p class="tag">Paragon · review snapshot · 17 September 2026</p><h1>Retained images worth another look</h1><p class="intro">19 plausible additions have a distinct image and usable local description. One share certificate needs metadata clarification, and one fuller photograph belongs with an existing carrier. The comparisons below explain each recommendation.</p><div class="metrics"><div class="metric"><strong>1,132</strong><span>retained paths screened</span></div><div class="metric"><strong>557</strong><span>visual families reviewed</span></div><div class="metric"><strong>242</strong><span>archived access images compared</span></div><div class="metric"><strong>19 + 2</strong><span>candidates and qualified cases</span></div></div><nav><a href="#candidates">19 possible additions</a><a href="#qualified">Two qualified cases</a><a href="#carriers">Carrier sections</a><a href="#excluded">False gaps / unresolved</a><a href="#evidence">Complete evidence</a></nav><p class="meta">Proposals only. Master choices, accessioned files, catalogue and drain states are unchanged. Larger files shown here have not been selected as Masters.</p></header><main><h2 id="candidates">19 possible additional objects</h2>''']
def candidate_card(c):
    out = [f'<article class="card" id="{esc(c["inventory"])}"><span class="tag">{esc(c["proposed_classification"].replace("_", " "))}</span><h3>{esc(c["inventory"])} · {esc(c["title"])}</h3><div class="pair">']
    out.append(embedded(c['display_source'], 'Retained source for comparison; no Master selected'))
    if c['archived_comparisons']:
        ref = c['archived_comparisons'][0]
        out.append(embedded(ref['path'], f"Archive {ref['inventory']} · {ref['title']} — {c['comparison_kind']}"))
    out += ['</div><p><strong>Why it is here.</strong> ' + esc(c['visual_finding']) + '</p>', '<p class="note">' + esc(c['qualifications']) + '</p>', '<p class="meta">Date evidence: ' + esc(c['date_evidence']) + '. Local workbook: Tabelle1, row ' + str(c['metadata_source']['row']) + '.</p>', '<details><summary>Source files and original metadata</summary><p>' + esc(c['metadata_source']['values'].get('object_description', '')) + '</p><p class="paths">Metadata: ' + link(workbook, Path(workbook).name) + '</p><ul class="paths">']
    for s in c['source_candidates']:
        dims = f"{s['width']} × {s['height']}" if s['width'] else 'undecodable'
        out.append('<li>' + link(s['path']) + '<br>' + esc(dims) + ' · ' + esc(s['stored_sha256']) + '</li>')
    out.append('</ul></details></article>')
    return ''.join(out)
for c in candidates:
    if c['proposed_classification'] == 'plausible_additional_object': parts.append(candidate_card(c))
parts.append('<h2 id="qualified">Two qualified cases</h2>')
for c in candidates:
    if c['proposed_classification'] != 'plausible_additional_object': parts.append(candidate_card(c))
parts.append('<h2 id="carriers">Four carriers: 16 raw sections to review together</h2><p>The whole carrier is already archived. These sections may supply better detail or additional coverage. Their filenames do not establish 16 new objects.</p>')
for c in carriers:
    parts.append(f'<article class="card"><h3>{esc(c["inventory"])} · {len(c["source_sections"])} sections</h3><div class="pair">' + embedded(c['source_sections'][0]['path'], 'Example retained raw section') + embedded(c['overview'], 'Accessioned complete carrier overview') + '</div><p>' + esc(c['description']) + '</p><details><summary>All retained section files</summary><ul class="paths">')
    for s in c['source_sections']:
        parts.append('<li>' + link(s['path'], Path(s['path']).name) + f' · {s["width"]} × {s["height"]} · visual family {s["visual_family"]}</li>')
    parts.append('</ul></details></article>')
parts.append('<h2 id="excluded">Five apparent gaps: no additional-object recommendation</h2>')
for c in non_candidates: parts.append(candidate_card(c))
parts.append('''<h2 id="evidence">Complete evidence and limits</h2><div class="limits"><p>All 1,132 paths were screened using stored hashes and technical evidence. Visual inspection covered 557 family representatives and 26 enlarged metadata-linked cases, against all 242 archived access images. Individual copies and every byte-distinct variant were not all inspected at full resolution.</p><p>20 image paths cannot be decoded and remain unresolved. Eleven retained paths are documents or metadata versions. A similarity score, missing label or decode failure is not evidence that a file can be deleted.</p><p>The live public museum-digital collection export contained 216 records on 17 September 2026. None of the 26 preliminary local labels returned a public search result, but several images were found visually under other labels. Private or unpublished MD records were not checked.</p></div><p><a href="RETAINED_VISUAL_REVIEW.md">Written report</a> · <a href="retained-accession-candidates.json">Candidate metadata and provenance</a> · <a href="retained-review-path-findings.json">Every retained path</a> · <a href="retained-md-inventory-search.json">Live MD queries</a></p><h3>All retained-family comparison sheets</h3><div class="sheetlinks">''')
for n in range(1,29): parts.append(f'<a href="retained-comparison-{n:02}.jpg">{n:02}</a>')
parts.append('</div><h3>Complete archived-image atlas</h3><div class="sheetlinks">')
for n in range(1,8): parts.append(f'<a href="retained-archive-atlas-{n:02}.jpg">{n:02}</a>')
parts.append('</div><h3>Enlarged initial comparisons</h3><div class="sheetlinks">')
for n in range(1,8): parts.append(f'<a href="retained-candidates-detail-{n:02}.jpg">{n:02}</a>')
parts.append('</div><p><a href="retained-focused-metadata-comparison.jpg">Carrier / date / production-plan comparison</a> · <a href="retained-cutting-machine-comparison.jpg">Kasimir Bong: different machines and photographs</a></p></main><footer>Review illustrations and reports are stored only in the existing Paragon work directory. Source and archive files remain unchanged.</footer></body></html>')
(W / 'retained-visual-review.html').write_text('\n'.join(parts))
print(json.dumps(summary, indent=2))
print('Written: RETAINED_VISUAL_REVIEW.md, retained-visual-review.html, retained-accession-candidates.json, retained-review-path-findings.json', flush=True)
