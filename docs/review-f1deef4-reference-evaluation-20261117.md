# Review: commit f1deef4 and the museum-digital reference evaluation

Date: 17 November 2026
Scope: commit [`f1deef4`](https://github.com/vseberlin/archive/commit/f1deef494c967ea9659486e93cce7e30aac3a1cc)
(application 0.3.0) and the private report *Museum-digital visual reference index:
methods and evaluation* (run of 17 November 2026, 7,588 previews, 400 queries).
Follows [review-archive-index-20261117.md](review-archive-index-20261117.md).

Method: code read, focused tests, probes against the package with synthetic TIFFs.
The evaluation report was read as supplied; its counts were checked arithmetically.
Not available: private holdings, the run's work directory, `torch`, `cv2`, `vips`.
No real image was processed and nothing was adopted.

## 1. Commit f1deef4

Tests run here: `test_image_depth` (4), `test_face_regions` (3),
`test_positioned_extraction` (13), `test_research_retrieval` (6) pass.
`test_reference_integration` and `test_archive_research` each have one error from
`torch`/`vips` missing in the review sandbox.

### Done well

- 16-bit greyscale conversion is linear 0–65535 → 0–255 without clipping, for
  little- and big-endian files; float and other high-depth input is refused, not
  guessed. Probe: a 16-bit ramp maps to 0..255 with no white-out.
- One prepared view per page/geometry is shared across extractors; the render
  policy enters every recipe and vector space, so a conversion change separates
  old from new results.
- The OpenCLIP encoder loads once per batch from hash-checked local safetensors;
  no download at run time. Letterbox framing is the default.
- Reference evaluation hides byte-identical previews in both modes and, in
  `series_hidden`, cosine ≥ 0.95.
- Face detection is tiled at native detail, tries four rotations and stores no
  identity.
- The plan states that receipts from 16-bit sources predating the fix need an
  affected-source audit. Correct: they are invalid.

### Still blocking an archive-wide master scan

| # | Finding | Evidence | Suggested fix |
|---|---|---|---|
| F1 | Re-run trap unchanged | `prepared_views.prepared_view` keys on `asdict(page)` (includes path, mtime, root, role) plus Pillow version; receipts likewise | Key on identity + content hash (plan item 1); keep stamps for change detection only; record Pillow but do not key on it, or key only on the render-policy version |
| F2 | 16-bit-per-channel colour (48-bit RGB) TIFFs refused | Probe: `ValueError: Unsupported high-depth image` | Safe, but count such masters before the run; add an explicit per-channel linear mapping as policy version 3 if they exist |
| F3 | Face scans on large masters never complete | Defaults 1024 tile, 256 overlap, 4 rotations, `max_tiles` 256: 4000×3000 → 84 runs, complete; 8000×6000 → 356, partial; 10000×7000 → 472, partial. Partial face receipts are never reused, so these are retried and left unfinished on every run; the 240 s timeout likely hits first on the workstation CPU | Rotations on the overview only, tiles at 0° unless the overview detects rotation; or scale `max_tiles` and timeout with tile count; record a bounded "budget exhausted" as finished-uncertain, not retryable |
| F4 | Retrieval and `reference index-new` capped at 2,048 receipts; catalogue, FTS and passage layers only planned | `retrieval.py`, `reference.cmd_index_new` | Plan items 4–5 before extraction at scale |

### Medium

| # | Finding | Evidence | Suggested fix |
|---|---|---|---|
| F5 | Dark 16-bit scans stay dark | Probe: a scan using 0–4000 maps to 0–15 | Record actual min/max/percentiles in the transform step; offer a percentile stretch as a separate render-policy recipe, never silently |
| F6 | 16-bit MinIsWhite TIFFs render as negatives | Probe: value 60000 shown as 233 (Pillow inverts 8-bit MinIsWhite, not 16-bit) | Read tag 262; invert when 0; add a regression test |
| F7 | Greyscale sources stored as RGB PNG | `display_rgb` returns RGB for L and uint16 grey | Store `L` for greyscale prepared views (about a third of the bytes); convert to RGB only in the consumer |
| F8 | ~~EXIF orientation lost for 16-bit references~~ **Withdrawn.** | Inferred from code order in `reference.open_image`, not probed; the operator's test did not reproduce it under the pinned Pillow 11.1 | None |

Operator follow-up (17 November): F3 (face budget on large masters) and F6
(16-bit MinIsWhite polarity) were reproduced on the workstation.

## 2. The reference evaluation

The run is clean (7,588 embeddings, 0 errors, 38.6 minutes) and the report is
careful about its limits. All percentages checked match their counts. The
interpretation below is what the report's own numbers support and what they do not.

### What the numbers show

- **Object type: real but easy.** 90.25 % against 69.25 % for the frequency
  baseline under the stricter holdout. `Foto` alone is 71 % of the corpus.
- **Dates beat the pool median** (MAE 3.57 vs 6.75 years). The fair baseline is the
  median date of the query's (or the predicted) collection. If TFA dates cluster,
  most of the gain may be collection recognition.
- **Places cannot be judged yet.** `Berlin-Oberschöneweide` is on 78 % of records;
  77.88 % place agreement can be prevalence alone, and the 95 % high tier may be
  largely that label proposed with confidence.
- **Collections is close to leakage.** Telling TFA from Kurt Schwarz by
  photographic style is easy and says little about identifying an unknown photograph.
- **The encoder mostly sees style and category.** Median preview edge 200 px; 74 %
  at or below the 224 px input. Consistent with strong collection/date and weaker tags.
- **Related photographs remain visible.** `series_hidden` hides ±20 inventory
  numbers; median top-1 similarity is still 0.836. A session spanning more than 20
  numbers stays in the pool.

### What is missing before any accuracy statement

1. Prevalence baselines for tags, places and collections at matched output count.
2. Precision without the most common labels per field.
3. Results per collection (TFA/WF, Kurt Schwarz, other).
4. Collection-conditioned date baselines.
5. Holdout sensitivity: wider windows and a session holdout.
6. The 32 % of records without a selected preview URL: mapper gap or genuinely
   missing images.
7. A domain-gap test: real queries will be master renders, not 200 px previews.
   Take 50–100 masters that have a published copy, render and downscale them like
   the previews, and check whether their own record or its neighbours rank first.
8. Curator relevance on about 50 queries × top 5 (same photograph, same place or
   object, same type, unrelated): roughly 250 judgements, the only measure that
   matters for cross-referencing and RAG.

Items 1–6 and 8 are implemented in this commit (section 3); item 7 needs masters
and the shared receipts, and remains to do.

### Results on the real index (operator run, 17 November)

The diagnostics were run against an isolated copy of the completed index; the
visual totals reproduced the original evaluation exactly. Under `series_hidden`:

| Measure | Visual method | Matched prevalence baseline |
|---|---:|---:|
| Tags | 65.15 % | 51.64 % |
| Places | 77.88 % | 76.21 % |
| High-support places | 95.32 % | 94.49 % |

Tags carry a real visual signal (+13.5 points). **Place suggestions gain little
over label frequency** (+1.7, high tier +0.8): they should not be presented as
visual evidence of location.

The coverage gap is a mapper choice, not missing images: all 3,582 records without
a selected preview are document/PDF records whose image entries have a text media
type but JPEG preview fields. `md_preview_url` filters them by type. They can be
indexed as page previews if wanted, but should be a separate stratum (documents,
not photographs) so they do not dilute or inflate photograph results.
`preview-gaps` now reports them as `non_image_type_with_image_preview:<type>`.

### Decision

Keep OpenCLIP ViT-B-32 with letterbox framing as the baseline and use the index
for high-support, reviewable leads, as the report recommends. Compare other
encoders (DINOv2/v3, SigLIP2) on this same harness only once items 1–4 exist;
otherwise model comparisons may be measuring prevalence. The master-scan gates
(F1, F3, F4 and the audit's identity/catalogue items) remain open.

## 3. Diagnostics added

`src/archive_research/reference_diagnostics.py`, wired as
`archive-research reference-diagnostics`. It reads only `index.sqlite` (and the
capture for `preview-gaps`), decodes no image, loads no model and never
overwrites a receipt. Tests: `tests/test_reference_diagnostics.py` (5).

| Command | Output | Answers |
|---|---|---|
| `diagnose --work-root W --run L` | `diagnostics-L.json` + console table | Items 1–5: matched-count prevalence baselines (all and high tier), precision without the N most common labels (`--common-labels`, default 5), per-stratum results (`--strata`, default TFA, Kurt Schwarz), date MAE for visual vs pool median vs own-collection median vs predicted-collection median, holdout sweep over `--windows` (default 20 100 500) with and without the session proxy |
| `preview-gaps --capture C` | JSON to stdout | Item 6: why records have no selected preview URL (no images, non-image types, missing folder, missing filename with another usable image) |
| `review-export --work-root W --run L` | `review-L.csv` | Item 8: top-k neighbours under the ±20 + near-duplicate holdout, with an empty `judgement` column |
| `review-score --review JUDGED.csv` | JSON to stdout | hit@1/3/5 and precision@1/3/5, strict (same photograph or same place/object) and lenient (plus same type) |

Defaults reproduce the original evaluation's sample (seed 47, 400 queries) and
voting settings, so the visual figures under `series_hidden` should match the
report's; a mismatch indicates a mapping difference worth investigating.

Interpretation notes:

- The matched-count baseline proposes, per query, as many of the eligible pool's
  most frequent labels as the visual method proposed. Visual precision only means
  something relative to it.
- `true_collection_median` uses the query's own collection, an upper bound for
  context-only dating, not a deployable predictor.
- The session proxy (identical date text within identical collections) can be far
  too broad where catalogues use date ranges; check `excluded_other_records` in each
  sweep row before reading its precision.
- Corrections after operator review (same day): `review-score` refuses queries
whose exported top-k rows are not all judged (`--allow-partial` scores only fully
judged queries and prefixes metric names with `partial_`); date baselines report
paired MAE on identical queries beside the unpaired figures; the rare-label
query count is incremented; the index is opened read-only (`mode=ro`) and a
missing index is refused without creating a file.
- On a synthetic 4,530-record index the full diagnosis took 1.4 s; the real run
  should take seconds to a minute.

## 4. Suggested order

1. Done: `diagnose` and `preview-gaps` on the completed work root (section 2).
2. Decide whether to index the 3,582 document previews as a separate stratum;
   stop presenting place suggestions as visual evidence.
3. Curator review via `review-export` (about two hours).
4. Domain-gap test (item 7) once 50–100 master renders exist as shared receipts.
5. Only then: encoder comparison on the same harness.
6. In parallel, independent of the evaluation: F1, F3, F6, F7 before any master scan.
