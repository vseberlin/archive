# OCR-first photograph research

`photo_clue_research.py` makes the ten-photo trials repeatable within the existing
reference-research task. It uses installed Pillow and Tesseract; it creates no
directories, services or databases. Outputs are separate research evidence, never
catalogue metadata. Existing pilot scripts and outputs remain untouched.

## Algorithm

1. Check the exact protected source mount, object identity, source stamp when
   supplied, and visually reviewed orientation/polarity recipe. Inspect the
   overview before choosing native-resolution crops. Read primary TIFF frame 0;
   extra frames are not automatically treated as versos.
2. OCR a maximum-4500-pixel overview with German and English, PSM 11. OCR selected
   native crops for signs, posters, book jackets, logos, labels and inscriptions.
   Save PNG, text, TSV coordinates/confidence, recipe, source stamp and receipt.
   Bound each OCR call to 90 seconds and two threads. A timeout is a failure,
   not an empty successful reading. Completed per-photo receipts survive later
   interruption; select remaining inputs into a new plan to continue.
3. Inspect crop pixels and raw OCR. Record separately confirmed text, uncertain
   readings, graphical observations, supplied human corrections and prior model
   hypotheses. Never promote confidence scores or repeated OCR gibberish to facts.
   If useful, request a tighter crop or PSM 6/7 in a new run. No endless retries:
   original defocus cannot be repaired by upscaling. Threshold/rotation experiments
   require a recorded recipe and remain separate from the untouched crop.
4. Form a neutral query before consulting prior hypotheses, then candidate and
   alternative queries. Our bear/panther correction shows why species and pose
   alternatives matter. Prefer distinctive geometry, jacket layout, heraldry or
   poster design over broad scene similarity. Keep rejected candidates and reasons.
5. Generate search packets with the existing source registry. Budget six queries,
   up to four collections and four inspected reference images per photo. Prove one
   opened record plus displayable image before expanding a batch. Search URLs are
   suggestions, not executed searches or working catalogue adapters.
6. A researcher or tool-enabled assistant opens records and views actual images.
   Preserve record/image URLs, local downloaded image, institution, accession ID,
   credits, access date, reference photograph date and inspection note. Record
   matching details, contradictions and occlusions. Mirrors of one accession are
   one evidence chain. Blocked access is `access_limited`, never `no_match`.
7. Assess location, event, photograph date and event date separately. Poster
   creation, building construction and publication dates need explicit reasoning
   before they constrain exposure date. The Kosmos poster match is strong graphic
   evidence; the Noll caption without its image is only a lead. Do not identify
   faces or transfer reference-image participants into the input photograph.
8. Validate and render the separate comparison JSON. Stop at the budget with
   unresolved fields and a specific remaining gap. No automatic metadata adoption.

## Commands and inputs

Use `python3 -B photo_clue_research.py --help`. All output parents must already
exist under `/mnt/volume/iss-archive-work`; existing output files are refused.

The plan is compatible with `openai-reference10-plan.json`: `images` records have
`number`, `id`, `filename`, `source_path_from_manifest`, `preview_path`, and
`recipe` (`visual_checked`, `rotation`, `polarity`). Include `source_stamp` with
`size_bytes` and `mtime_ns` when available to reject stale inputs.

```sh
python3 -B photo_clue_research.py ocr --plan PLAN.json --regions REGIONS.json --output WORK/run-ocr.json --limit 1
python3 -B photo_clue_research.py packets --plan PLAN.json --ocr WORK/run-ocr.json --observations OBSERVATIONS.json --registry SOURCES.json --output WORK/run-packets.json
python3 -B photo_clue_research.py report --comparisons COMPARISONS.json --output WORK/run-report.html
```

Replace `WORK` with an existing absolute work directory. For a one-photo smoke
test, use a one-photo plan for packets too. The November 8 OCR harvest is accepted
directly, so completed OCR need not be rerun.

Regions map existing object IDs to lists:
`{"OBJECT_ID":[{"label":"poster","box":[0.73,0.32,0.985,0.77],"psm":11}]}`.
Coordinates refer to the full image after the recorded orientation recipe.

Observations use `{"records":[{"object_id":"OBJECT_ID", ...}]}`. Store
`confirmed_text`, `uncertain_text`, `graphics`, `human_notes`, `prior_hypotheses`,
and `rejected_candidates` explicitly. `queries` is a list of
`{"source_id":"commons","kind":"neutral","query":"crouching big cat bronze sculpture"}`.
Kinds are `neutral`, `hypothesis`, `alternative`. Raw OCR is preserved in packets
but deliberately never becomes an automatic query. These are assisted research
packets: this command does not invoke another model, browse or spend API tokens.

Comparisons use `metadata_adopted:false` and `records`. Each record has an
`object_id`, `references`, and `assessments` with `location`, `event`,
`photograph_date`, `event_date`. Each assessment has `value` (null if unknown),
`basis`, `reference_ids`, and `reasoning`. Each reference has an `id`,
`record_opened`, `record_url`, `image_inspected`, `image_path`, `inspection_note`,
`matches`, `contradictions`, `not_visible`, `access_status`, and `verdict`.
Include archival provenance and credits as additional fields. `visual_match`
basis requires a linked inspected image and matching details. The validator
checks evidence completeness; it cannot certify that a human's visual judgement
is correct. The HTML renders those assessments and evidence as an audit-readable
report; the earlier illustrated pilot comparisons remain separate.

Validation: `python3 -B -m unittest test_photo_clue_research.py`.
