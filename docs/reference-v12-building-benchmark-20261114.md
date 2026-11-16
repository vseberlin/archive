# Six-photo building comparison — 14 November 2026

Published source: [e55c04f](https://github.com/vseberlin/archive/commit/e55c04f306f65c4d3fe6fb5e3e378f0c3ac3930c) on both `main` and `archive/scripts`.

All twelve live trials completed. V12 fixes evidence attribution and previously hidden gallery loss, but this pilot shows **no improvement in building identification accuracy or supported INPUT metadata**. Both versions identified two of the three independently checked buildings. The other three photographs have no established identity and are excluded from the accuracy denominator.

## Paired results

| Measure | V11 baseline | V12 candidate |
| --- | ---: | ---: |
| Correct supported identities, independently checked subset | 2 / 3 | 2 / 3 |
| Supported INPUT metadata fields | 3 | 3 |
| Tentative INPUT metadata fields | 3 | 1 |
| Downloaded reference images, distinct SHA-256 | 42 | 66 |
| Images supplied to completed blind comparisons | 34 | 52 |
| Source documents, distinct record URLs | 7 | 14 |
| Document/page records, summed per trial | 7 | 20 |
| Search queries | 60 | 47 |
| Reported input + output tokens | 2,726,453 | 2,496,256 |

V12 downloaded 24 more images and inspected 18 more. Its reported token total was 8.4% lower. These are observed outcomes of one stochastic trial per version/photo, not causal estimates of efficiency. Token counts include cached input and do not represent monetary cost. The 500,000-token threshold is checked between calls; several trials exceeded it during a call.

| Photo | Independently checked identity | V11 outcome | V12 outcome |
| --- | --- | --- | --- |
| C09-T211_001.tif | Franziskaner-Klosterkirche | Correct supported building; tentative location | Same |
| ELB_13.tif | Pfefferberg | Correct supported building and address | Same |
| M02_T009_002.tif | Schering, Müllerstraße 170–171 | Unresolved; round limit | Unresolved; round limit |
| M02_T009_001.tif | Unknown; Schering remains provisional | Tentative Schering building/address; usage limit | Unresolved; drifted toward truck research; usage limit |
| ELB_31.tif | Unknown courtyard | Unresolved; retrieval failed | Unresolved; retrieval failed |
| ELB_46.tif | Unknown brick passage | Unresolved; usage limit | Unresolved; usage limit |

The supported fields in both versions are the church identity, Pfefferberg identity, and Pfefferberg location. Neither version supported an event or photograph date. V12 retained only the tentative church location; V11 additionally retained the two tentative Schering fields in the partial-facade case. Fewer tentative fields are not automatically an improvement. No false supported identity appeared among the three scored cases; the sample cannot establish a general false-positive rate.

## Harvested metadata quality

The increase from 7 to 14 distinct document URLs is raw retrieval coverage. The 20 V12 document/page records include repeated source URLs across trials and separate pages from a PDF; they are not 20 independent corroborating sources. Image counts deduplicate byte-identical files, not different derivatives or views of the same subject. Completed blind inspection includes compatible, rejected and insufficient references.

Stored catalogue-field types include inventory number, title, artist and material. Inspection found noisy generic extraction: the Bundesarchiv search interface was stored as a `title` fragment, the Pfefferberg site's prose about artists as an `artist` value, and monument inventory-number fragments include adjacent address/history text. These are source snippets requiring validation, not clean INPUT metadata. More raw fields therefore did not produce more supported metadata. No building-construction or source-record date was adopted as a photograph date.

## What the fixes demonstrably changed

- Visual evidence now distinguishes the same photograph, same event, same site/object, and same technical type. A building match cannot by itself support an event; a technical-type match cannot establish an installation location.
- A visible time bound requires a specific observation on INPUT, or an independent reviewer binding. A sign dated only in a reference image cannot date INPUT.
- Gallery candidates deferred before download are retained within explicit bounds, remain unseen until attempted, and can be fetched in up to two continuation batches before further discovery. Parser truncation and queue overflow are reported.
- Footer widgets are excluded without suppressing similarly named page layouts. Cached portal resumes restore previously observed deferred media URLs.
- Access failures and incomplete candidate coverage are explicitly marked.

The live adaptive trials exercised one URL continuation in the partial-facade case and two in the passage case. The former still ended with 34 deferred URLs and a parser report of 49 images beyond its 24-candidate cap. The latter downloaded and inspected 19 images versus 9 in V11 but remained unresolved. Bounded retrieval exposes remaining work; it does not guarantee an identification.

## Method and limits

Six fixed photographs were selected from 13 objects whose saved primary routing group was buildings/places. Each version used the same image, model (`gpt-6-astra`), high reasoning effort, two discovery rounds, native detail crops, and 500,000-token per-item allowance. Each photograph had its own plan, so one item's usage stop could not prevent later cases from running. New prefixes prevented reuse of earlier experimental results. Expected labels were withheld from prompts.

All six paired first-discovery prompts and request fingerprints are byte-identical. Later model responses, web searches and retrieved images diverged. This is an end-to-end paired pilot, not a fixed-reference comparison-quality experiment or a statistically reliable population estimate. The blind-pilot mode removes prior identity hints and initial routing; V12 rerouted the partial-facade photograph toward machinery and spent effort on its truck. Thus cohort membership was controlled, but the live research subject was not fixed.

Ground truth for only three photographs was independently checked by an analyst against actual reference photographs: [church monument record](https://denkmaldatenbank.berlin.de/daobj.php?obj_dok_nr=09011276), [Pfefferberg](https://industriekultur.berlin/ort/pfefferberg-brauerei/), and [Schering facade](https://www.moderne-regional.de/treppen-haeuser/). These labels are not user-approved catalogue entries. The first two scores reflect supported final identities; the unresolved known Schering building is a miss, not a correct identification. Unknown cases remain unscored.

V11 is the exact runner from commit `52ad782ce4275c85f1a1fe1829755f6382d54e0c`. Frozen runner hashes:

- V11: `d6f1f1ef14493f100cb4f3d8489c86b9f49f96d1938f45b99d0ad15ab11434a1`.
- V12 live candidate: `9093a7257de2123503328212a9889cc5bd77ae08b00ab408a7cdbfb825582d15`.

The final release adds two tested follow-ups after freezing the live candidate: portal checkpoint media-URL restoration and coverage flags for access failures. These do not change adaptive retrieval/model decisions. The final release itself was not rerun through twelve additional paid calls. Its SHA is recorded in the published benchmark JSON. The complete relevant offline suite passed **195 tests**.

The first launcher was interrupted while two blind calls continued. Their original outputs, schema validation, usage and fingerprints were recovered; no replacement calls were issued. Their exit codes and elapsed times remain unknown. Detached supervision resumed the experiment and completed all twelve trials. This limitation is recorded in the published benchmark JSON and private recovered receipts.

## Published evidence and reproduction

This report and the selected benchmark measurements are published at the user's
explicit request. Photographs, downloaded websites, raw model outputs, live
review records and workstation paths remain private.

- [Per-photo metrics, settings, scores and source hashes](reference-v12-building-benchmark-20261114.json).
- [Current research runner](../Archive-tools/reference_batch.py).
- [Regression tests](../Archive-tools/test_reference_batch.py).
- [Review findings and implementation explanation](review-52ad782.md).
- [Release-to-pilot patch](reference-v12-building-benchmark-20261114-pilot.patch), reconstructing the exact frozen V12 runner from `e55c04f`.

The JSON uses the report's photo filenames as case labels. Its `scoring` section
records which three cases are eligible for identity accuracy. Raw harvested
catalogue-field types are included for coverage only; they are not accepted
INPUT metadata. Summed per-trial counts and cross-cohort unique counts are
labelled separately. The reported tests apply to release `e55c04f`; this
publication changes documentation and benchmark artifacts only.

To reconstruct historical sources, use two disposable checkouts outside live
review directories. Checkout V11 at `52ad782ce4275c85f1a1fe1829755f6382d54e0c`;
checkout V12 at `e55c04f306f65c4d3fe6fb5e3e378f0c3ac3930c`, then apply the linked
patch **in that disposable V12 checkout**. It removes only the two post-pilot
follow-ups. Verify each runner's SHA-256 against the JSON before execution.
Keep each runner beside its five unchanged helper modules; the original local
wrapper only redirected helper-path resolution for frozen standalone files.

A rerun requires authorized access to the same private photographs and prepared
review work area, the source registry, the configured Codex CLI/model, and the
runner's documented dependencies. These commands are templates, not evidence
that a public clone contains the images. Set `REVIEW_ROOT`, `RESEARCH_ROOT`,
`REGISTRY`, `EXPECTED`, `OBJECT_ID`, and a fresh `PREFIX` in the local environment.
Run from each historical checkout's `Archive-tools` directory:

```sh
python3 -B reference_batch.py prepare \
  --work-root "$REVIEW_ROOT" --research-root "$RESEARCH_ROOT" \
  --registry "$REGISTRY" --prefix "$PREFIX" --object-id "$OBJECT_ID" \
  --model gpt-6-astra --blind-pilot --discovery-mode adaptive \
  --compare-effort high --max-rounds 2 \
  --max-item-tokens 500000 --max-average-tokens 500000 --expected "$EXPECTED"
python3 -B reference_batch.py run \
  --work-root "$REVIEW_ROOT" --research-root "$RESEARCH_ROOT" \
  --registry "$REGISTRY" --prefix "$PREFIX" --model gpt-6-astra --limit 1
```

Map the six filenames to their existing private object IDs. Expected labels use
the existing `{"objects": {"object-id": {...}}}` format and stay outside model
prompts. Use one fresh plan per version/photo and leave native crops enabled.
The original supervisor ran each pair concurrently, alternated version order
between pairs, and waited for both before starting the next photo. It did not
attach proposals or adopt metadata. Do not reuse completed prefixes for a fresh
experiment. Live search/model variation means a rerun may differ even with
identical settings. Repeating all calls is not needed to validate these
published measurements.

For the release's offline regression suite, run in `Archive-tools`:

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest -q \
  test_reference_batch test_research_routes test_photo_clue_research \
  test_codex_image_pilot test_workbench
```

At original pilot completion, the four protected review files were byte-identical
to their pre-test hashes and the source mount was read-only. No catalogue
metadata was adopted and no workbench service was restarted. Raw evidence is
retained privately; its paths and source photographs are not part of this
publication.

The next useful experiment would hold the building subject fixed and supply the
known Schering reference directly, then compare automatic retrieval with that
control. Generic metadata extraction also needs validation before raw field
counts can be called useful enrichment. These are recommendations, not scheduled
additional work.
