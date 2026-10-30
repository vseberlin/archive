#!/usr/bin/env python3
"""Summarize the TIFF-only comparison; no accession or disposition writes."""
import json
from collections import Counter
from pathlib import Path

import compare_all_tiffs as scan

ROOT = Path(__file__).resolve().parent


def main():
    scan.workbench.mount_guard()
    records = [json.loads(l) for l in (ROOT / 'tiff-scan-results.jsonl').read_text().splitlines()]
    masters = [json.loads(l) for l in (ROOT / 'tiff-scan-masters.jsonl').read_text().splitlines()]
    summary = json.loads((ROOT / 'tiff-scan-summary.json').read_text())
    pixels = {r['source_path']: r for r in [json.loads(l) for l in (ROOT / 'tiff-residual-pixel-evidence.jsonl').read_text().splitlines()]}
    inventories = {m['inventory']:m['record_key'] for m in masters if m['inventory']}
    additions = {
        'A04_T85_001.tif': ('Filmtheater Kosmos bei Nacht',
            'No matching image found across all current Masters after polarity, rotation, mirror and crop searches. '
            'Direct TIFF inspection confirms the Kosmos night scene; the related existing Kosmos record is a different photograph.'),
        'K06_F076_002.tif': ('Blick von oben in eine Produktionshalle des TRO',
            'No matching image found across all current Masters. The direct TIFF view differs from related '
            'md:103830: this is a wide elevated view of the hall; the existing Master is a closer transformer-assembly view. '
            'Historical inventory lead KS-7-BZ_0781-6 is absent from the current object/alias catalogue.'),
        'N01_T041_002.tif': ('Eröffnungsparade der Weltfestspiele mit Fahnenträgern',
            'No matching image found across all current Masters. Direct comparison with md:109430 shows a distinct '
            'exposure of the same parade: flag bearers lead the foreground here; the existing Master shows the delegation leaders.'),
    }
    for r in records:
        keys = set(r['catalogue_record_keys'] + r['name_record_keys'])
        keys.update(inventories[k] for k in r.get('historical_inventory_leads', []) if k in inventories)
        r['name_record_keys'] = sorted(keys - set(r['catalogue_record_keys']))
        evidence = pixels.get(r['source_path'])
        r['residual_pixel_matches'] = evidence.get('matches', []) if evidence else []
        pixel_good = [m for m in r['residual_pixel_matches'] if m['passes_secondary_checks'] or
                      (m['record_key'] in keys and m['phash_distance']<=10 and m['dhash_distance']<=8 and m['ahash_distance']<=8)]
        oriented = [m for m in r.get('orientation_matches', []) if m['passes_orientation_screen']]
        image_matches = r['strong_matches'] + pixel_good + oriented
        r['image_matched_record_keys'] = sorted({m['record_key'] for m in image_matches})
        r['metadata_link_differs_from_image_match'] = bool(keys and image_matches and not keys.intersection(r['image_matched_record_keys']))
        if not r['feature']['hashes']:
            r['finding'] = 'unreadable_tiff'
            r['review_note'] = 'The saved index records an unreadable TIFF. No perceptual comparison can establish its image coverage; keep it out of the new-addition shortlist.'
        elif image_matches:
            r['finding'] = 'existing_kurt_image_match'
            r['review_comparison_keys'] = [m['record_key'] for m in sorted(image_matches, key=lambda m:m.get('score',999))]
            r['review_note'] = 'Image similarity links this TIFF to a current Kurt Schwarz Master. It is not proposed as a new object. This does not select a better Master or authorize redundancy/deletion.'
        elif r.get('prior_alias_decisions'):
            r['finding'] = 'prior_alias_image_conflict'
            r['review_comparison_keys'] = [m['canonical_record_key'] for m in r['prior_alias_decisions']]
            r['review_note'] = 'An existing human decision aliases this historical record to another archive object. The TIFF image search does not find the expected photograph among current Masters. Review that existing decision; do not create a new object or reverse it automatically.'
        elif r.get('prior_census_decisions'):
            prior = r['prior_census_decisions'][0]['decision']
            r['finding'] = 'prior_curator_redundant' if prior['disposition'] == 'redundant' else 'prior_curator_hold'
            r['review_note'] = 'This source image was already considered in the Kurt Schwarz census. Preserve the recorded curator decision; it is not a newly discovered object.'
        elif r['other_unit_matches'] or '1642368_Glasplatten/' in r['source_relative_path']:
            r['finding'] = 'other_collection'
            r['review_note'] = 'Glass-plate/Baecker collection context or an existing other-unit image match. No Kurt Schwarz addition is proposed; authorship/placement is not inferred from the Avantmedia folder.'
        elif keys:
            r['finding'] = 'known_object_image_gap'
            r['review_comparison_keys'] = sorted(keys)
            r['review_note'] = 'An existing Kurt Schwarz object/inventory is linked, but the full-unit search found no sufficiently close current Master image, including the residual TIFF crop/rotation search. Review image coverage and source binding; do not create a duplicate object or replace a Master automatically.'
        elif r['filename'] in additions:
            r['finding'] = 'possible_new_kurt_object'
            r['review_title'], r['review_note'] = additions[r['filename']]
            r['review_note'] += ' Proposed addition only; human object/Master confirmation remains required.'
        else:
            r['finding'] = 'unmatched_needs_examination'
            r['review_note'] = 'No reliable match or existing decision resolved this TIFF; retain it for explicit review.'
        assert not r['selected_master_path'] and not r['deletion_authorized']
    summary['findings'] = dict(Counter(r['finding'] for r in records))
    summary['residual_tiffs_decoded_for_variant_search'] = len(pixels)
    summary['residual_decoder_errors'] = [p for p,r in pixels.items() if r.get('error')]
    summary['new_addition_candidates'] = [r['source_path'] for r in records if r['finding']=='possible_new_kurt_object']
    summary['catalogue_links_are_not_image_coverage'] = True
    summary['scope_result'] = 'Three proposed new objects, existing-image matches, known-object coverage questions, existing curator/alias decisions, other-collection scans and unreadable TIFFs are all separately recorded.'
    assert all(scan.stamp(Path(p)) == s for p,s in summary['input_stamps'].items())
    assert all(scan.stamp(Path(r['source_path'])) == r['source_stamp'] for r in records)
    assert all(scan.stamp(Path(r['master_path'])) == r['master_stamp'] for r in masters)
    (ROOT/'tiff-scan-results.jsonl').write_text(''.join(json.dumps(r,ensure_ascii=False)+'\n' for r in records))
    (ROOT/'tiff-scan-summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n')
    lines=['# Avantmedia TIFF-only comparison with current Kurt Schwarz Masters','',
        'Scope: all 1,639 TIFF files in the Avantmedia source folder, 424,280,384,274 bytes. '
        'All 1,677 Avantmedia JPEGs and other non-TIFF files were excluded from the candidate set. '
        'Image views and residual comparisons decode the TIFF itself; no Avantmedia JPEG derivative is used.', '',
        'The protected USB mount was verified. The 1,935 current Masters were bound through the current file catalogue and SQLite. '
        '1,934 unchanged source-image feature sets were reused through their recorded current Master representations '
        '(verified exact-copy or pixel-preserving metadata rewrite). The single missing feature set was computed '
        'from its current JPEG Master in memory. No shared index, source or archive file was modified.', '',
        '1,634 existing TIFF pHash/dHash/aHash sets were reused; five TIFFs have recorded decoding failures. '
        'The search includes negative/positive polarity and orientation/mirror variants. Seventy-five remaining TIFFs '
        'were sampled/decoded in memory for independent edge crops, rotation and mirror searches against all 1,935 targets. '
        'These are similarity searches, not proofs of byte identity or permission to change Masters.', '',
        '| Finding | TIFF paths |','| --- | ---: |']
    labels={'existing_kurt_image_match':'Image match/strong similarity to an existing Master',
            'known_object_image_gap':'Known object; expected image coverage requires review',
            'prior_alias_image_conflict':'Existing alias decision requires image review',
            'possible_new_kurt_object':'Proposed genuinely new object',
            'other_collection':'Other collection context or match',
            'prior_curator_hold':'Existing curator hold',
            'prior_curator_redundant':'Existing curator redundant decision',
            'unreadable_tiff':'Unreadable TIFF',
            'unmatched_needs_examination':'Unresolved residual'}
    lines += [f'| {labels[k]} | {v} |' for k,v in summary['findings'].items()]
    lines += ['', '## Three proposed additions', '',
        '| TIFF | Subject | Existing-record evidence |','| --- | --- | --- |']
    for r in records:
        if r['finding']=='possible_new_kurt_object':
            lines += [f'| {r["filename"]} | {r["review_title"]} | '+
                      ('Historical inventory '+', '.join(r.get('historical_inventory_leads',[]))+'; no current object/alias' if r.get('historical_inventory_leads') else 'No current object/inventory link found')+' |']
    lines += ['', 'The three candidates are distinct in the inspected related-frame comparisons and have no close image '
        'match in this full-unit search. They are reviewable addition candidates, not accessioned or human-selected Masters.', '',
        '## Existing objects and decisions needing attention', '',
        'Catalogue membership is not proof that the expected image is present. In the directly inspected '
        '`A01_T04_001.tif` case, the source shows children around a table while the current Master for '
        '`md:96927` / `KS-6-BZ_0190-1` shows a boat scene. The known-object coverage list records similar '
        'search failures as questions, not blanket assertions that every listed Master is wrong.', '',
        'The two helicopter scans `E03_T011_006.tif`, `E03_T011_008.tif` and the elderly-group scan '
        '`E09_T064_004.tif` have historical inventory links and existing human alias decisions. Those '
        'decisions remain intact and require image review; the files are not counted as new objects.', '',
        '`Dia 00055.tif`, `Dia 00056.tif`, `Dia 00057.tif` already have metadata-review holds; '
        '`Dia 00084.tif` already has a redundant decision. They were rebound to the recorded sources by '
        'filename, dimensions and all three matching perceptual hashes, without applying a new disposition.', '',
        '## Readable evidence and review', '',
        '- [TIFF comparison workbench](http://127.0.0.1:8774/tiff-scan)',
        '- `tiff-scan-results.jsonl`: every TIFF, saved hashes, current Master matches, prior decisions and findings.',
        '- `tiff-scan-masters.jsonl`: exactly 1,935 current Masters, final stored hashes and indexed-source provenance.',
        '- `tiff-residual-pixel-evidence.jsonl`: residual crop/rotation/mirror comparisons; no image files generated.',
        '- `tiff-scan-summary.json`: counts, scope, algorithm limitations and unchanged-file checks.', '',
        'The earlier 305-photo preparation groups were preliminary catalogue links. This full TIFF scan supersedes '
        'their novelty assessment while retaining the accepted metadata and its qualifications. No accession, '
        'Master replacement, deletion, metadata embedding, directory creation or archive mutation took place.', '']
    (ROOT/'TIFF_COMPARISON.md').write_text('\n'.join(lines))
    print(json.dumps(summary['findings'],indent=2))


if __name__=='__main__':
    main()
