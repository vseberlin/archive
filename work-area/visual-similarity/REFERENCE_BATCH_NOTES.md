# reference_batch.py v2 — why headless lagged AI-assisted, and what changed

Bucket: `SUPPORT_INDEX` research tooling. Outputs stay unadopted proposals
(`metadata_adopted: false`); nothing writes to units, catalogues, sources or the
archive. Existing v1 plans/receipts remain evidence; v2 refuses to run a v1 plan
(`settings` missing) — prepare a new prefix.

## Diagnosis (v1)

AI-assisted research is iterative and visual: look at the photo → search →
inspect results → search again → zoom into details. v1 did one blind text-only
plan, regex reference selection and a single low-effort comparison.

1. **Planner never saw the photo.** `discover` had no `--image`; it only
   rewrote earlier text hypotheses into `intitle:` queries.
2. **`intitle:` Commons search is the weakest Commons retrieval.** File titles
   rarely carry the building name; every word must occur in the title.
3. **References chosen by regex, not by looking.** `rank_records` counted
   title words; the first 3 downloadable images were compared.
4. **No second round.** A miss ended the item.
5. **Comparison at minimum effort** (`low`/`low`, "one short sentence",
   "< 1000 tokens") — the one step that needs careful visual reasoning.
6. **No zoom.** One downscaled preview vs 900 px thumbnails.
7. **Caption anchoring.** Captions preceded the visual comparison.

Bugs / operations:

- `check_result` and the planner `records` guard raised inside `process` → one
  bad model citation stopped the whole batch (`stopped_error`).
- All fetch failures cached permanently → a timeout killed that URL/query for
  every later run; combined with "3 consecutive access failures" a network blip
  could stop the batch and poison the cache.
- Web limit machinery was dead code (`web=False` hard-wired).
- Round-robin gave ~1 reference per hypothesis — too thin for `supported`.
- `'total': 101` hard-coded.
- HTML record captions were the first 1000 chars of page text (mostly nav).

## Changes (v2)

| # | Change | Where |
|---|--------|-------|
| 1 | Discover gets the labelled INPUT image; returns `visual_features` (from the image, before reading saved evidence), `crop_boxes`, `place_names`, plain `commons_queries` | `discover_prompt`, `DISCOVERY` |
| 2 | Wikidata resolves `place_names` → P625 coordinates + P373 Commons category; retrieval = Commons **geosearch** (250 m) + **category members** + **depicts** (`haswbstatement:P180`) + text search + linked institution records. `www.wikidata.org` added to the fetch allowlist (not to the saved registry, so plan/registry equality still holds) | `Fetcher.wikidata`, `commons_*`, `gather` |
| 3 | Up to 24 candidates → labelled contact sheet → low-effort **visual shortlist** call (pixels only, no captions) → ≤ 4 references. Hallucinated `C` IDs are dropped and logged | `contact_sheet`, `SHORTLIST`, `process` |
| 4 | Optional **second round** (`--max-rounds 2`, default) from shortlist/compare `next` queries; tentatively/supported-cited references carry over (≤ 2); seen records/hashes excluded | `process` |
| 5 | Comparison effort configurable, default **medium**, 300 s timeout; brevity caps removed | `--compare-effort` |
| 6 | Up to 3 **zoomed INPUT detail crops** sent to comparison; every image has its **label burned into a banner** (INPUT, INPUT detail A, R1 …) | `make_crops`, `labelled` |
| 7 | Comparison schema/prompt orders **visual comparisons first** (pixels only), caption-informed assessments second | `COMPARISON`, `compare_prompt` |
| 8 | `check_result` / invented records → row `failed` / dropped with note; batch continues | `process` |
| 9 | Cache only permanent HTTP 4xx (not 408/425/429); transient failures recorded with `permanent: false`, refetched next run, never re-requested in the same run. Pre-v2 error records are treated as transient | `Fetcher.fetch` |
| 10 | `--discovery-web`: live web search in discovery (≤ 4 calls), records restricted to registry domains | `discover_prompt`, `call` |
| 11 | HTML records use the image's own `alt`/`figcaption`; page text only as fallback with caution | `Page`, `record` |
| 12 | Token stops per item (`--max-item-tokens`, default 120k) and average (`--max-average-tokens`, default 80k), stored in the plan; `total` from the plan | `Runner.run` |
| 13 | **Benchmark**: `prepare --pilot-only` plans only the 10 AI-assisted pilot images; `run` then never touches the sidecar; `benchmark` writes a side-by-side with empty score fields for hand scoring | `prepare`, `benchmark` |

Not done (needs approval per AGENTS.md — new dependency/venv in this runner):
ranking candidates by CLIP similarity with the `visual_knn.py` embeddings
(`claude/visual-similarity-knn` branch). The visual shortlist call covers the
same need without new dependencies; CLIP pre-ranking would cut its cost.

## Calls per item

`discover` (low, image) → `r1-shortlist` (low, 2 images) → `r1-compare`
(medium, ≤ 8 images) → optionally `r2-shortlist` → `r2-compare`. Every call is
a fresh `codex exec`, receipted, never retried automatically.

## Suggested order

```sh
cd /mnt/volume/iss-archive-work/visual-similarity
# 1. measure the gap on the AI-assisted pilot images first
python3 reference_batch.py prepare --pilot-only --prefix reference-bench-20261109-v2
python3 reference_batch.py run --prefix reference-bench-20261109-v2
python3 reference_batch.py benchmark --prefix reference-bench-20261109-v2
#    score benchmark.json by hand; compare with a --compare-effort low / --max-rounds 1 plan
# 2. then the 101 remaining images
python3 reference_batch.py prepare --prefix reference101-20261109-v2
python3 reference_batch.py run --prefix reference101-20261109-v2 --limit 3   # smoke
python3 reference_batch.py run --prefix reference101-20261109-v2
```

Expected impact, high → low: geosearch/category retrieval > visual shortlist >
image in discovery > comparison effort > second round. Change one setting per
benchmark plan to confirm.

## Tests

`tests/archive-preservation/test_reference_batch.py` — offline, with stand-ins
for the work-area helper modules (`archive_common`, `codex_image_pilot`,
`codex_reference_pilot`, which are not in this repository). Not tested against
the real Codex CLI, Wikidata or Commons.
