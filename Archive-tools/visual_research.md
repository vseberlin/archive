# Visual metadata research

`visual_research.py` is a SUPPORT_INDEX tool for provisional new-object cases.
It implements the retrieval-first slice discussed alongside draft commit
66a4467: local reference images, preserved evidence, no metadata voting.

The existing workbench decides membership and retains decision authority.
Preparation selects `proposed_new_object` cases through its shared scope rules.
A saved selection supplies the research image; otherwise exactly one eligible
source must exist. Choosing that research view does not save a Master decision.

All generated data stays under the explicitly supplied `iss-archive-work` root.
Source and archive payloads remain read-only. No accession, metadata embedding,
identity assignment, deletion or publication is performed. Existing archive
catalogues are read through `catalogues.read_unit`; catalogued access images
are preferred over decoding full Masters. This first pass covers the explicitly
supplied units, not every historical museum-digital record.

## Commands

Use the existing archive-tools environment for preparation, and the isolated
research environment with compatible CPU torch/open_clip for embedding/reporting.

```sh
ROOT=/mnt/volume/iss-archive-work/visual-similarity
SCRIPT=/home/vladimir/archive/Archive-tools/visual_research.py
/mnt/volume/iss-archive-work/archive-tools-venv/bin/python -B "$SCRIPT" --work-root "$ROOT" prepare \
  --review /mnt/volume/iss-archive-work/avantmedia-master-review-20261103 \
  --unit /mnt/volume/iss-archive/units/tfa \
  --unit /mnt/volume/iss-archive/units/kurt-schwarz --expected 111
"$ROOT/venv/bin/python" -B "$SCRIPT" --work-root "$ROOT" embed --threads 2 --batch 8
"$ROOT/venv/bin/python" -B "$SCRIPT" --work-root "$ROOT" report --k 6
```

Preparation records the review revision, source stamps, existing candidate
metadata, prior evidence, exact reference catalogue rows and catalogue stamps.
libvips produces 1024 px longest-edge previews without upscaling, applies EXIF
orientation and converts to sRGB. Decode warnings fail the affected item.
Primary composites do not certify embedded layers or complete TIFF structure.

Inspect `contact-*.jpg` before embedding. An optional `--views views.json` maps
existing object IDs to `{ "invert": true, "rotate": 90, "checked": true }`.
Each checked entry must also carry `source_stamp` copied from its manifest row;
a changed source invalidates the check. Rotation is counterclockwise. Only explicitly inspected views should be marked
checked; polarity correction concerns disposable research images only.
Changing a view changes its preview and embedding cache keys.

`manifest.json` lists successful previews and explicit errors. Any preview error
blocks embedding. `progress.json` and separate command logs show completion or
failure; launching a process does not establish completion. Repeated preparation
reuses unchanged previews; embedding resumes from individually cached vectors.
A lock prevents two invocations from writing the same work root concurrently.

The model is OpenCLIP ViT-B-32 / laion2b_s34b_b79k. Its own resize/crop preprocessing
is recorded with package versions. The 1024 px research view remains available
for later methods; it is not the model's native input resolution. CPU inference
uses a bounded batch and thread count. Downloaded weights belong in the work root.

`neighbours.json` retains rankings for local archive references and other current
candidates. `report.html` is a static local contact-sheet report with source and
metadata evidence. Cosine scores are not probabilities, identity proof or
metadata confidence. No dates, places, people or collection labels are copied.
DEXPERT/date estimation and human field adoption remain separate future work.

The research manifest and embedding manifest must agree. Stale embeddings are
rejected at report time. Evidence is a snapshot: if review decisions change,
rerun preparation and embedding before treating the candidate list as current.

After the first preparation, apply inspected view settings without rereading the
whole reference catalogue:

```sh
/mnt/volume/iss-archive-work/archive-tools-venv/bin/python -B "$SCRIPT" --work-root "$ROOT" views --views "$ROOT/views.json"
"$ROOT/venv/bin/python" -B "$SCRIPT" --work-root "$ROOT" run --views "$ROOT/views.json" --threads 2 --batch 8 --k 6 --background
```

The detached `run` command embeds and then generates the report. It records its
PID in `launch.json` and writes `run.log`; `progress.json` must reach `complete`
before the report is considered finished. It is an ordinary bounded process,
not a persistent service. All candidate views must be marked visually checked
before embedding. Model oneDNN operations are disabled for this Phenom II CPU;
NumPy is pinned to an import-tested compatible version in requirements-visual.txt.

A detached run waits for any active preparation to release the work-root lock,
then applies the inspected views, embeds and reports. It fails explicitly if
preparation did not produce a usable manifest or candidate view checks are missing.

## Evaluate the cached reference embeddings

```sh
OPENBLAS_NUM_THREADS=2 /mnt/volume/iss-archive-work/archive-tools-venv/bin/python -B -u "$SCRIPT" \
  --work-root "$ROOT" evaluate --sample 400 --seed 47 --name evaluation-400-seed47-baselines
```

`evaluate` delegates to the bounded `visual_evaluation.py` helper. It reads only
work-area snapshots and cached vectors; no source decoding, model inference,
metadata adoption or review save occurs. Results are HTML, summary JSON and
per-query JSONL. Existing named receipts are not overwritten.

The sample is deterministic and proportionate across archive units. All three modes
exclude the held-out identity, repeated MD identity and known matching payload
or preview hashes. `all_neighbours` permits other series members; `series_hidden`
also hides inventory neighbours within 20 main numbers and explicitly numbered
title sequences. Frame suffixes such as `KS-5-BZ_0568-2` do not replace the main
inventory number. This is a leakage-reduction heuristic, not verified series
membership or proof that all near duplicates have been excluded. `unit_hidden`
excludes the query’s entire unit from neighbours and all permitted baselines;
there is no known-unit prior in this mode. With two reference units this tests
two directions of domain transfer, not arbitrary unknown-photo generalization.

Settings are fixed before evaluation: 12 neighbours, minimum cosine .5,
exponential weight temperature .05, minimum weighted support .34 and at least
two voters. Strong/medium/weak bands describe neighbour support, not confidence.
The comparison baselines use label prevalence and median years from the same
allowed pool, either globally or restricted to the known archive unit. Their
predictions obey the same support gates. Paired date MAE uses identical queries
on which both methods produced a date. Every field is also compared with
output-matched priors: the same query cases and number of proposed labels,
ranked by prevalence without consulting query truth. This distinguishes ranking
value from abstention and output volume. Reports show all, high, medium and
high+medium support tiers, coverage and exploratory paired bootstrap intervals.
High means support >=.75 with >=3 voters; medium >=.5 with >=2 voters.

Metrics preserve coverage and abstentions. Tag/place overlap, recall, macro recall,
Jaccard and support-band agreement measure **catalogue agreement**, not human-rated
visual relevance. Missing labels are unscorable; disagreement can reflect incomplete
cataloguing. `object_type` is near-constant and mostly absent for Kurt Schwarz,
so high agreement there is not evidence of useful classification.

Date scoring accepts only unambiguous years in the current `date_text` field:
a year, numeric day/month/year, or German month/date with year. Approximate dates,
decades and ranges are excluded rather than converted to false exact years.
These are recorded catalogue years, not independently verified shooting dates.
Date error on this sample is not comparable to a dating model's external benchmark.

The evaluation does not establish accuracy on the 111 unlabelled candidates.
People and collection membership are deliberately excluded from automatic voting.

The evaluation also writes `<name>-field-policy.json`. Pilot review eligibility
requires positive series-hidden gain against both matched priors, a positive
exploratory 95% lower bound, and at least 20 paired queries. TFA permits tags,
dates and places at high/medium support only if they pass (high-only fallback
is tested). Kurt Schwarz permits only high-support tags if they pass; places
and dates remain disabled. Object type is disabled. These are review proposals,
not metadata authority, and require independently known unit membership.

For heterogeneous units, visual neighbours cannot supply dates. Route dating to
verso OCR or documented Konvolut context, preserving source, raw text, date role
and uncertainty. An explicitly chosen known-unit median fallback must be marked
`source_kind: unit_median_fallback`, `observed_date: false`, and reviewed. This
is a routing contract; OCR/date extraction is not implemented by this evaluator.
Unknown-unit cases remain research leads. Policy selection uses the same pilot
sample, not an independent confirmation set; query bootstrap intervals can be
optimistic where series correlations remain.
