# Global archive index and multimodal RAG: audit comparison and proposal

Date: 17 November 2026
Repository: `vseberlin/archive`, `main` at `2944d9c`
Compares: `archive-indexing-audit-2026-11-17.md` (the attached review) with an independent audit of the same snapshot.
Goal: reusable global indexes for cross-referencing, plus preparation of multimodal RAG prompts.

Method: code read on `main`, focused test suites run, probes against the package. Not available: private holdings, runtime configuration, model weights, Elements, `vips`. Nothing was executed against real data or sent to a model.

## Bottom line

I agree with the attached review's decision: **do not start a whole-archive, all-extractors run yet.** Both reviews find the same foundations sound: recipe-keyed receipts, raw outputs retained, atomic writes, unadopted proposals, and source-bound evidence packets.

Both reviews treat this as an extraction-pipeline problem. For the stated goal, three things are missing that neither extraction fixes nor the attached review names:

1. **The catalogue layer is not indexed.** CATALOG.csv, the 11,170 museum-digital records, and the described Kurt Schwarz, Paragon and TFA collections are the richest cross-reference signal you have, and the index sees only OCR text and vectors.
2. **The index is not usable at archive scale for two reasons.** It fails closed as a whole when any one source changes or goes offline (probe below). Its cache keys orphan every receipt when a file is touched, moved or re-embedded with metadata.
3. **There is no shared identity across the research systems.** The document pipeline, the photo web research, the pHash index and the kNN branch each keep their own IDs and storage, so their results cannot be joined or cited together.

Fixing these is cheaper than the extraction run, and they decide whether the run is reusable.

## 0. Consistency alarm: two unrelated histories on GitHub

`origin/main` and `origin/archive/scripts` have **no common ancestor**. Your local `archive/scripts` now tracks `vseberlin/archive:main` (AGENTS.md on `main` says so), so the remote `archive/scripts` branch is an orphaned old line.

- Since 14 November my review commits (`2b0598a`, `2564e37`) went to that orphaned branch. `main` has the earlier three reviews but not the 64be40c review or the local-index design note.
- The two copies of `Archive-tools/reference_batch.py` differ only in `DEFAULT_REGISTRY` (`~/Downloads/…` vs `~/archive-local/…`).
- Done 17 November: the two files unique to that branch (`docs/review-64be40c.md`, `work-area/visual-similarity/REFERENCE_BATCH_REVIEW_64be40c.md`) were ported to `main` with this report, and the remote `archive/scripts` branch was deleted at the user's request. Every other file differing between the branches was older on `archive/scripts` (path updates only). Its last commit was `2564e37`.

## 1. Attached review findings, verified

| # | Attached finding | My verification | Verdict |
|---|---|---|---|
| 1 | Index capped at 2,048 receipts; refresh re-reads and re-hashes every artifact | Confirmed in code (`retrieval.py:15`, `receipts.py`) | Agree. Also see N1: refresh fails closed on any single change. |
| 2 | Partial receipts reused even when work was unfinished | Confirmed in code (`runner.cached`: reuses `complete` and `partial`; `text-regions` marks unfinished scans `partial`) | Agree, including the nuance: empty-but-finished OCR should stay reused. |
| 3 | OCR key omits traineddata fingerprints and `TESSDATA_PREFIX` | Confirmed in code (`recipe_for` records Tesseract version and language names only) | Agree. Release-blocking for bulk OCR. |
| 4 | Process per task, full plan re-read per child, summary rewrite per attempt, no worker timeout | Confirmed in code (`pipeline.extract`: `subprocess.run` without `timeout`; child calls `load_plan` on the full plan) | Agree. |
| 5 | Each extractor renders and stores its own full PNG | Confirmed (`runner.extract_one` writes `<key>.png` for every task, including `pdf-text`) | Agree. Same as my earlier finding 2. |
| 6 | Visual adapter fixed to ViT-B-32 224 px centre crop | Confirmed (`visual_features.recipe_for`) | Agree. Add the text-to-image need (section 4). |
| 7 | Scanned layout heuristic; only Trafo, FEB, Bibliothek member mappings qualified | Confirmed (`units.py` qualifies three units) | Agree. The member-mapping gap is a coverage blocker in its own right (N6). |
| 8 | Old document index cache keyed by SHA only, 3-page default | Confirmed in code (`document_index.py:341`) | Agree. Keep it as header discovery only. |
| 9 | pHash scheduler counts feature rows instead of required IDs | Confirmed in code (`visual_indexer.py:333`) | Agree. |

The attached review's "What future changes should cost" table and its benchmark design are good; I would adopt both.

## 2. Where I disagree or rate differently

- **Cache key binds mtime, path, root and role: I rate this release-blocking; the attached review lists it under "additional limitations".** Probe on `contracts.Recipe.cache_key`: changing mtime, path, root name, `original_path`, `role` or filling in `sha256` later each changes the key. Your plan includes moving to `/mnt/volume/archive-system`, and accession embeds TIFF metadata, which rewrites master bytes with identical pixels. Every one of these orphans every receipt. This is the most direct cause of "re-run next week". Fix: key on existing identity (unit, object, representation, member, page/frame) plus content hash; keep mtime and size only as change detection.
- **Benchmark before vectors, not before OCR alone.** Embeddings are cheap to regenerate from retained prepared views; OCR and rendering are the expensive steps. Freeze render and OCR recipes first, treat vectors as disposable projections.
- **The release condition misses identity and the catalogue layer.** Without them a globally indexed corpus still cannot answer "what else in the archive shows this object or place".

## 3. New findings

**N1 (High) The index fails closed as a whole.** `refresh_index` rebuilds and requires byte-identical equality. Probe: index of 5 receipts, one source made unavailable (as when Elements is unmounted) → `Index inputs changed; rebuild explicitly`. At archive scale some source is always offline or touched, so search would always be refused. Fix: per-entry state (`current`, `stale`, `source_offline`) in the projection; refuse only when the specific evidence is opened or cited.

**N2 (High) Catalogue metadata is outside the index.** `retrieval.build_index` indexes extracted words and vectors only. CATALOG.csv rows, museum-digital fields (title, places, dates, persons, keywords, GND/Wikidata links where present) and unit READMEs never enter search or packets. For your described collections these are better than any OCR. Fix: a `catalogue` record type in the same projection, sourced from existing unit files with their row hash as binding, never written back.

**N3 (High) Lexical search is not fit for historical German.** `text_search` is an all-tokens substring match on `' '.join(words)`, unranked. Probe:
- `Werkstatt` does not find OCR text `Werk- statt` (line-break hyphenation);
- `Schöneweide` does not find `Schoeneweide`, and the reverse;
- `KWO` does not find `Kabelwerk Oberspree` (no alias layer).
Fix: index-time dehyphenation from ALTO line ends, dual folding (`ö→o` and `ö→oe`, `ß→ss`, long s), SQLite FTS5 with BM25 (available in the stdlib sqlite3 here, version 3.53.1), plus an alias table (section 4).

**N4 (High) No shared identity across research systems.** `archive_research` uses `Source(unit, object_id, representation)`. `units.selected_members` builds `object_id = namespace#record_key` and falls back to the **file path** as representation ID when none exists, which contradicts the repo's own portability guardrail 1. `reference_batch.py` keys items by workbench object IDs in its own research root and receipt format. `visual_index.py` (pHash SQLite) and the `visual_knn.py` branch keep their own IDs. Fix: one opaque evidence ID scheme reused by all of them, `unit/object/representation/member/page[/region]`, taken from existing catalogue identities; paths only as locators.

**N5 (Medium/High) The index unit is the page receipt, not a citable passage or region.** RAG retrieval works best on small, citable chunks: a caption, a paragraph, a figure crop. The region machinery exists (`Region`, `caption_links`, layout figures) but `build_index` emits one entry per receipt. Fix: index entries at passage/region level, each pointing back to receipt + region; page entries stay for context.

**N6 (Medium/High) Member mapping covers 3 of 14 units.** Without qualified member order for the other units, a "global" run is silently partial. Fix: per-unit mapping adapters first, and a coverage report (expected members/pages vs done vs failed vs intentionally omitted) as a projection.

**N7 (Medium) Four visual stacks with incompatible spaces.** `visual_index.py` (pHash), `Archive-tools/visual_research.py`, `visual_knn.py` (open_clip, unmerged branch, MD previews in its own SQLite) and `archive_research.extractors.visual_features`. Each has its own preprocessing and cache. Fix: one visual projection per model space, fed from the shared prepared views; retire the others to evidence.

**N8 (Medium) Retrieval does not feed packets.** Search hits must be hand-copied into a packet selection (1–32 entries). That is right for authority, but for RAG preparation you want a generated *proposed* selection from hybrid search, which the human then edits. The packet and assessment format already handle the rest well (untrusted-content instructions, attachment hashing, 20 MB cap, `store=false`, JSON schema).

**N9 (Medium) The prompt carries no token budget.** `prepare_request` includes full OCR text of every evidence entry plus up to 32 images at up to 2048 px. A packet of long report pages can exceed the model context or cost far more than needed. Fix: per-entry text budget (region text first, page text truncated with a pointer), and a reported token estimate in the transport receipt.

**N10 (Low) OCR languages.** Ordinary OCR defaults to `deu+eng`; there is no Fraktur (`deu_latf`) and no Russian, while `line-ocr` already handles `rus`. GDR and Soviet tube material and pre-1945 prints need both. Decide in the benchmark.

## 4. Target design: one projection, several record types

Keep the contracts. Add one rebuildable projection (SQLite plus vector matrices) over readable authorities, under the already approved visual-similarity work root or an approved new index directory.

| Layer | Content | Source of truth | Rebuild cost |
|---|---|---|---|
| Identity | Evidence IDs `unit/object/representation/member/page[/region]` | Existing CATALOG.csv and manifests | Minutes |
| Prepared views | One render per content hash + page + geometry: ~2048 px long-edge for vision, 300 dpi grayscale for text | Masters, read once | Hours, done once |
| Text | Native PDF text, ALTO OCR words with boxes, dehyphenated passages | Receipts | Minutes from receipts |
| Catalogue | Titles, descriptions, places, dates, persons, keywords, authority links | CATALOG.csv, museum-digital capture | Minutes |
| Authorities and aliases | Places, organisations, persons, object types; `KWO = Kabelwerk Oberspree`, `WF`, `TRO` | Your entries plus museum-digital/GND/Wikidata IDs already in records | Manual, small |
| Visual vectors | One matrix per model space (`space_id` = model hash + preprocessing) | Prepared views | Hours per model, disposable |
| Links | Proposed cross-references with basis (text, visual, catalogue, geometric) and score | Retrieval runs | Disposable |
| Coverage | Expected vs done vs failed vs omitted, per unit | Manifests + receipts | Seconds |

Retrieval for a RAG question or a new photo:

1. Hybrid candidate search: BM25 over text and catalogue, plus vectors, merged with reciprocal-rank fusion. Catalogue matches weigh highest for your described collections.
2. Expand through authorities: same place, organisation or person IDs.
3. Re-rank image pairs with geometric verification (existing SIFT/RANSAC) on the top 20 only.
4. Emit a *proposed* packet selection (N8), with region-level evidence and lineage, for human editing.
5. Existing `packet` → `assess` path unchanged.

This is the same design as my 14 November local-index note (site cards, top-k per site, holdout benchmark), generalised from photos to all material.

## 5. Tooling

No new dependency needed:
- **SQLite FTS5** for text and catalogue search: stdlib, BM25 ranking, `unicode61 remove_diacritics`. Add a second folded column for `oe/ae/ue/ss` variants.
- **numpy matrices** (`.npy`, float16, memory-mapped) per vector space: brute-force cosine over 100k–1M vectors at 512–1024 dimensions is fast enough on CPU. numpy is already pinned in the `embeddings` extra.
- **Existing SIFT/RANSAC** for verification.

Gated candidates (AGENTS.md approval; benchmark before adopting; I have not tested any on your hardware or material):
- **Visual instance retrieval:** DINOv2 or DINOv3 small, as the attached review proposes.
- **Text-to-image retrieval for RAG questions** ("photos of the Kabelwerk canteen"): a SigLIP2 model via open_clip. CLIP ViT-B-32 already does this weakly.
- **Multilingual text embeddings** for semantic passage search in German OCR: only if BM25 plus aliases fall short in the benchmark.
- **OCR:** tessdata_best `deu`, `deu_latf`, `rus` against current; a modern recognizer (PP-OCRv5) as the attached review suggests; a vision-model transcription on a small weak-text subset as upper bound.
- **Layout:** PP-StructureV3 on mixed pages, as the attached review suggests.
- **Vector store:** sqlite-vec or FAISS only if numpy brute force measures too slow. Not expected below a few million vectors.

## 6. Sequenced plan

Each step is small and testable; together they are the release gate for the long run.

1. **Repository line.** Port the two orphaned reviews to `main`; retire remote `archive/scripts`.
2. **Identity and cache key (N4, section 2).** Content-and-identity cache key; no path-derived representation IDs; regression tests for touch, move, root rename, metadata embedding.
3. **Receipt semantics (attached 2, 3).** `finished/uncertain` vs `unfinished/retryable`; bind traineddata and tessdata hashes.
4. **Prepared views (attached 5).** One render per content hash/page/geometry, shared by all extractors.
5. **Batch worker (attached 4).** One process per chunk, plan validated once, model loaded once, real deadline, per-item receipts kept.
6. **Projection (attached 1, N1–N3, N5).** SQLite FTS5 + catalogue rows + passages/regions + per-entry staleness + vector matrices; queries never re-hash artifacts.
7. **Member mappings and coverage report (N6).** For every unit in scope.
8. **Benchmark** of 150–250 items (attached review's design), using your catalogued collections as ground truth, holding out series and epochs. Choose OCR language set, render sizes, visual model(s).
9. **Freeze** the render and OCR recipes, then start the long run per unit, in chunks, with resume tested on the first chunk.
10. **RAG bridge (N8, N9).** Proposed packet selection from hybrid search; prompt token budget.

## 7. Risks

- **Re-run risk:** highest from cache key and render choice (steps 2 and 4), not from new models.
- **Silent partial coverage:** the run "completes" over 3 qualified units; mitigated by step 7.
- **Disk and file count:** about three flat files per task on the NTFS volume; shard directories by key prefix before scale.
- **Rights:** assessment uploads images to an external model. `store=false` does not settle rights for Kurt Schwarz and Paragon material; decide per collection.
- **Benchmark leakage:** near-duplicates and same-series items in both tuning and test inflate accuracy; the attached review's grouping rule handles it.
- **Scope creep against AGENTS.md:** the projection is a rebuildable index, not a metadata authority; keep catalogue rows read-only and keep proposals unadopted.

## 8. Verification performed

- Ran on `main`: `test_archive_research` (25, one error from missing `vips` in my sandbox), `test_research_units` (4), `test_positioned_extraction` (12), `test_research_retrieval` (6), `test_research_layout` (5), `test_scan_layout` (7), `test_text_regions` (15), `test_line_ocr` (5), `test_research_evidence` (12). All others pass.
- Probes: cache-key sensitivity (8 cases), whole-index fail-closed with one offline source, German text search (hyphenation, umlaut folding, abbreviation), FTS5 availability and diacritic folding, branch ancestry.
- Attached findings 2, 3, 4, 8, 9 were confirmed by reading the cited code, not by rerunning the attached review's own reproductions.
- No measurement of throughput, OCR or retrieval accuracy; no real holdings.
