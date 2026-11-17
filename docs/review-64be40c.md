# Review of V12 benchmark and curated-collection reference proposal

Reviewed commit: [64be40c8fa18a8225a24e43cb4e9e3988097a611](https://github.com/vseberlin/archive/commit/64be40c8fa18a8225a24e43cb4e9e3988097a611)  
Review date: 2026-11-14  
Previous review: [review-52ad782.md](review-52ad782.md)

The reviewed commit publishes the six-photo benchmark; its V12 implementation is
in parent release e55c04f. Findings below concern that source snapshot. At
publication the branch had advanced to 2b0598ace82766cf6184bd032e78274c70ae12b0; later code
was not re-reviewed here. This commit changes documentation only.

The collection proposal incorporates the user's subsequent clarification that
TFA, Kurt Schwarz and Paragon contain substantial, richly described holdings in
museum-digital and DDB. That is user-supplied collection context, not an audit of
individual records or their coverage.

## Assessment and validation

V12 addresses the previous event-attribution, reference-only date and hidden
gallery-truncation findings. The benchmark still reports two correct supported
identities out of three independently checked buildings for both V11 and V12.
The three unknown photographs appropriately remain outside that denominator.

Verified independently:

- Per-trial sums agree with published token, query, downloaded-image,
  inspected-image, document-record and supported/tentative-field totals.
- The release runner matches SHA-256
  `fd78c03b30576961e42c5c2b427ffffaa558b54e9022631e99fd9798236e38f9`.
- Applying the published release-to-pilot patch in a disposable copy reconstructs
  SHA-256 `9093a7257de2123503328212a9889cc5bd77ae08b00ab408a7cdbfb825582d15`.
- The specified suite ran 195 tests: 194 passed, one errored in
  `test_demo_has_working_tiff_pdf_and_jpeg_previews`. The error occurs in unchanged
  `workbench.create_demo()` while writing a multipage TIFF:
  `TypeError: function takes at most 4 arguments (18 given)`.
  This is the same local error observed in earlier reviews; its root cause is
  not established by this review.
- The additional failure-accounting reproduction below ran in both research modes.

Command:

```sh
PYTHONDONTWRITEBYTECODE=1 python -B -m unittest -q \
  test_reference_batch test_research_routes test_photo_clue_research \
  test_codex_image_pilot test_workbench
```

The private photographs and unpublished raw model receipts were not available for
independent rescoring. No new paid recognition calls were made. Arithmetic and
source reconstruction do not independently validate the source observations or
ground-truth labels.

## F1 — Selected candidates disappear from uninspected coverage on comparison failure

Priority: P2.

Sources:
[adaptive selection](https://github.com/vseberlin/archive/blob/64be40c8fa18a8225a24e43cb4e9e3988097a611/Archive-tools/reference_batch.py#L1689),
[adaptive coverage calculation](https://github.com/vseberlin/archive/blob/64be40c8fa18a8225a24e43cb4e9e3988097a611/Archive-tools/reference_batch.py#L1740).
The portal path has the analogous selection-before-completion behavior.

The runner removes selected image hashes from pending candidates before a valid
visual comparison has completed. If that call fails, the selected images are not
restored and coverage can appear complete.

Production-path reproduction using existing synthetic fixtures and a simulated
visual timeout:

| Mode | Downloaded candidates | Visual result | Uninspected count | coverage_incomplete |
| --- | ---: | --- | ---: | --- |
| adaptive | 4 | timeout | 0 | false |
| portals | 4 | timeout | 0 | false |

Adaptive status is failed/comparison_failed. Portal status is failed and has no
stop_reason in this first-batch case. The primary finding is false complete
coverage, not loss of the explicit failure status.

Recommendation: distinguish selected/attempted images from successfully inspected
images. Remove candidates from uninspected coverage only after a valid visual
assessment, or restore them on failure. Define visual-inspection completion
separately from the later metadata synthesis: a successful visual call should not
be counted as uninspected merely because synthesis fails.

Add regressions for failed visual calls and budget exhaustion between selection
and visual inspection, in both modes.

## Highest-value accuracy experiments

### 1. Direct Schering control

The V12 known-Schering case used ten search queries and 414,995 reported tokens,
but inspected only one image. This suggests retrieval weakness, without proving
that interpretation would succeed on the correct reference.

Supply the independently checked Schering reference already cited in the benchmark
to the existing direct mode. Keep the input, target, model and comparison settings
fixed. If direct comparison succeeds, inspect why automatic retrieval failed to
find or deliver that reference. If it fails, inspect framing, resolution and
comparison behavior before expanding the corpus.

### 2. Separate the requested target from search routing

The partial-facade trial drifted toward identifying its truck. Blind preparation
clears initial routing, while the goal remains generic identification.

A neutral target such as "identify the building; the vehicle can supply supporting
clues" does not reveal the building's identity. Preserve that task independently
of candidate names and routing. Allow strategy changes which help investigate the
target, but do not let a correct incidental-object identification complete a
different task.

### 3. Balance gallery continuation against new discovery

[Continuation precedes discovery](https://github.com/vseberlin/archive/blob/64be40c8fa18a8225a24e43cb4e9e3988097a611/Archive-tools/reference_batch.py#L1613)
whenever pending/deferred candidates remain and continuation is allowed. The
passage case spent 516,812 reported tokens in five comparison batches and two
continuations while completing only one discovery round.

Test one continuation before returning to discovery, or a reserved discovery
budget. Continue a gallery when another view can answer a concrete unresolved
detail; defer it when inspected images consistently reject its subject. Do not
discard deferred URLs or assume uninspected images are irrelevant.

### 4. Measure the retrieval-to-verification chain

For each known case record:

- whether a useful reference record was found;
- whether its relevant image URL was extracted;
- whether the image was downloaded and successfully inspected;
- whether visual interpretation established the relationship;
- whether final verification retained the correct claim.

These measurements diagnose failure better than raw counts alone. The reported
8.4% token reduction is one stochastic paired observation, not a causal efficiency
estimate. More images or raw metadata fragments do not establish better accuracy.

## Curated TFA, Kurt Schwarz and Paragon as a reference corpus

### Principle: retrieve evidence, without training

Use the existing descriptions and photographs as a searchable evidence collection.
The corpus supplies collection-specific names, places, equipment, events and
historical context at query time. No language-model fine-tuning or image-model
training is required.

An optional pretrained encoder computes fixed image vectors once, then for each new
query image. This is inference, not training. Similarity retrieves candidates;
the catalogue metadata explains those candidates; visual and documentary checks
determine which claims actually apply to INPUT.

Existing KNN experiments do not contradict this approach, but embedding similarity
alone is not a promised solution. KNN is a ranking mechanism. The proposed benefit
comes from using rich text, identifiers and documented relationships alongside
visual ranking, with explicit verification.

### Start with local authoritative exports

Prefer the already reconciled local catalogue and image mappings where available.
Use museum-digital exports and provider interfaces to fill missing descriptions or
media links. Do not scrape web pages to reconstruct metadata already present in a
structured export.

Treat a museum-digital record republished by DDB as one originating record with two
publication links, not two independent witnesses. Preserve conflicting values and
their provenance rather than silently overwriting them.

Keep one canonical object record with its individual image associations. Preserve:

- existing object/inventory identifiers and originating institution;
- collection membership and publication identifiers/URLs;
- full original description, title, controlled subjects, places and creator;
- date text and date meaning: exposure, event, depicted-object creation, or record
  modification, with uncertainty retained;
- explicit series/session/album relationships where documented;
- image identity, local derivative or observed image URL, and image-specific caption;
- rights and source-update information.

Collection membership, nearby filenames or shared creator are search clues, not
proof that two photographs share a place or event. An object record containing
several photographs needs image-level links wherever their metadata differs.

Readable exported records remain the durable basis. Retrieval indexes and vectors
are rebuildable. Do not add an independent metadata authority or separate service.

### Combine complementary retrieval channels

| Channel | Input | What it contributes | Main limitation |
| --- | --- | --- | --- |
| Identifiers and existing hashes | Inventory clues, exact/near-copy pixels | Existing record or duplicate candidates | Does not identify a new viewpoint by itself |
| Lexical catalogue search | OCR, signs, description terms, names | Precise labels and historical terminology | Sparse or wrong query wording can miss records |
| Optional visual embedding search | Full image and relevant crop | Candidate views despite missing captions | Generic visual similarity is not identity |
| Documented relationships | Known series/session/site links | Nearby contextual references | Relations must be recorded, not guessed from filename order |

Use explicit historical/current-name aliases and preserve German terms. Approximate
dates and collection membership should generally rank results rather than
categorically exclude otherwise plausible references.

For lexical search, SQLite FTS5 with ranked BM25 queries is an option within an
approved rebuildable catalogue, not a reason to add a database service. Structured
fields such as places, tags, creator and equipment names should be searchable with
appropriate weighting. Query expansion should preserve uncertain OCR alternatives,
not turn one guessed name into a mandatory filter.

For visual retrieval, an evaluated fixed pretrained image encoder, for example an
OpenCLIP checkpoint, can generate one vector per reference image. For a corpus on
the order discussed, start with a simple stored vector matrix and exact similarity
ranking rather than a vector database service. Encoding throughput must be measured
on the actual hardware; no runtime estimate or accuracy guarantee is made here.
Use the same checkpoint/preprocessing for reference and query vectors and recompute
only changed images. Do not assume every checkpoint handles German text or historic
industrial imagery equally well.

Rank text and visual channels independently, merge by rank rather than adding
incomparable raw scores, deduplicate originating records, and keep alternative
candidates. A pilot could retrieve 20–50 candidates locally and inspect a diverse
4–8 first; these are tunable starting bounds, not measured optimal settings.

Do not send the entire catalogue to the model. Supply the shortlist's reference
images, relevant catalogue passages, source identifiers and explicit relationship
types to the existing comparison stage. Use web discovery for remaining gaps.

### Existing metadata matcher is not a discovery ranker

[MetadataIndex](https://github.com/vseberlin/archive/blob/64be40c8fa18a8225a24e43cb4e9e3988097a611/Archive-tools/matching.py#L158)
is a useful reconciliation helper, but should not be reused unchanged as the new
discovery search:

- its text tokenization uses title, description and inventory fields;
- other structured fields are not included unless repeated in those texts;
- non-identifier matches require at least three shared words and 0.6 weighted
  source-word coverage.

A controlled test with a target title "Kosmos Berlin" and the identical title query
returned an empty list. This illustrates a mismatch of retrieval purpose, not a
request to weaken reconciliation or Master-selection rules.

Provide a distinct permissive discovery ranking path while keeping conservative
reconciliation decisions intact. Feed its candidate records into the research
runner before web discovery, in both adaptive and portal modes.

### Keep evidence transfer explicit

A reference can establish different things:

- same photograph: potential route to its catalogue description/date, after the
  exact-record relationship is verified;
- same building/site: supports identity or location, not the other photo's date;
- same machine type: supports type/function, not factory location or operator;
- same event/session: supports contextual leads only to the extent the link is
  established.

Newly generated hypotheses must not automatically become trusted corpus records.
Otherwise the system will retrieve its own guesses as apparent corroboration.
An analyst-approved record may be used as catalogue evidence, but an unreviewed
proposal retains its provisional status.

### Evaluate before indexing everything

Begin with an inspectable export sample and a varied reference subset. Confirm that
images map to the correct records and that descriptions and structured fields
survive normalization. Then evaluate the entire available curated corpus where
practical; do not tune on only easy matches.

Use two separate tests:

1. **Record rediscovery:** withhold query captions/identifiers and measure whether
   the exact known record is retrieved. This is useful for duplicates and imports.
2. **New-view discovery:** exclude the query's exact photograph and its derivatives,
   then look for other views of the site/object. Include unrelated and unresolved
   queries to check false matches. For generalization tests, split by documented
   session/object groups where appropriate.

Measure useful-reference recall at a fixed shortlist size, subsequent verified
identifications, false supported claims and model-token use. An exact-copy success
does not demonstrate recognition of a new photograph.

Recommended implementation order:

1. Sample and validate the existing catalogue-to-image mappings.
2. Add local lexical retrieval and feed its references into the existing comparison.
3. Measure on held-out examples and the Schering control.
4. Add a pretrained visual retrieval channel only if it recovers missed references.
5. Add external harvesting selectively where the curated collections lack coverage.

The immediate priority shifts from general external harvesting to exploiting
already described holdings. No corpus was imported, new dependency installed,
model trained, index built or catalogue modified as part of this review.

## Technical references

- [SQLite FTS5](https://sqlite.org/fts5.html): full-text querying and BM25 ranking.
- [OpenCLIP](https://github.com/mlfoundations/open_clip): pretrained image/text
  encoders and inference examples.

Documentation checked on 2026-11-14. These references establish available
capabilities, not demonstrated performance on the user's collections.
