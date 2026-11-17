# Review of commit 64be40c (six-photo V11/V12 benchmark publication)

Follow-up to `REFERENCE_BATCH_REVIEW_52ad782.md`. Scope: the published report, benchmark JSON and pilot patch.
Not checkable: the trial results themselves (photos and receipts are private). The V12 code diff was only skimmed, not probed.

## Verified
- Runner SHA-256: V11 (52ad782), the release (e55c04f) and e55c04f + pilot patch all reproduce the three hashes in the JSON.
- The twelve trial rows sum to the published totals (tokens, queries, images, documents, field counts).
- 141 tests pass in `test_reference_batch`, `test_research_routes`, `test_photo_clue_research`, `test_codex_image_pilot`. `test_workbench` was not rerun, so the "195 tests" figure is unconfirmed.
- The report's conclusion (no accuracy gain; 2/3 scored identities in both versions) matches the data.

## Observations beyond the report
- About 80% of V12 tokens (1.99M of 2.50M) went to the four photos that ended unresolved; the two solved photos stopped after one round at about 250-260k tokens.
- V12 spent 2.5M tokens to fetch 15 pages (about 166k per page). Retrieval is the expensive, narrow stage.
- Gallery continuation can crowd out the follow-up search: ELB_46 ran one discovery, two continuations and five comparison batches on 19 images, then hit the usage limit before a second search (same shape as N1).
- Labels are biased toward solvable cases (Pfefferberg was labelled after an earlier assisted search solved it); 2/3 overstates real accuracy.
- Blind mode strips routing as well as identity hints, which explains the truck drift in one V12 trial.

## Suggestions, in order
1. Run the fixed-reference control the report proposes (known Schering reference in `direct` mode) to tell retrieval misses from comparison misses.
2. Build a labelled set of 30-50 photos from catalogued material; keep unknowns as a separate leads set.
3. Log the stage at which each labelled case is lost: site proposed, page fetched, correct image downloaded, inspected, judged matching.
4. Replace model-driven retrieval with a local candidate index (see the Denkmal / museum-digital / Commons notes in `REFERENCE_BATCH_REVIEW_52ad782.md`).
5. End a continuation when a page's first comparison batch is all `different`, so the follow-up search still runs.
6. Keep the subject class (e.g. "building") in blind mode; it is not an identity hint.
7. Try low effort as a first pass; publish tokens per stage to see whether the easy cases need high effort.

## Design note: local reference index from the ISS collections (no model training)
Not run; based on reading `visual_knn.py` on the unmerged branch `claude/visual-similarity-knn` (open_clip ViT-B-32 on CPU, 11,170 museum-digital previews, leave-one-out `evaluate` with a `series_hidden` mode, kNN proposals). Its accuracy is unmeasured. Contents of the TFA, Paragon and Kurt Schwarz collections were not inspected.

1. Run `evaluate` first; `series_hidden` precision per field shows what pretrained embeddings can do on this material. If weak, change nothing else yet.
2. Index sites, not only images: group records by place/building from metadata; per site keep a card (name, address, photo count, date range) and 3-4 representative previews. Rank sites from the neighbours of an INPUT photo, pass them as R references to the existing blind comparison and paired crops. The index proposes; the comparison verifies.
3. Re-rank the top ~20 with pretrained local feature matching and geometric verification (OpenCV SIFT + RANSAC, or LightGlue), since global embeddings are weak at same-building-different-decade.
4. Add SQLite full-text search over titles, tags, places and captions for inputs with banner or caption text.
5. Use the same data as the benchmark: leave one out, hide the same series and near-duplicates (>0.95 similarity), record top-1 and top-3 site recall (suggestion 2 above).
6. Integrate into `reference_batch.py` as a first discovery source with `local_index` provenance; web search only when no confident site.

Limits: the index only knows what the collections cover (sites like Pfefferberg need Commons/Denkmal); accuracy measured on ISS-skewed material will not transfer to unknown photos from elsewhere; it is unknown how many records carry a usable place, or whether DDB holds anything museum-digital lacks.
Approvals (AGENTS.md): `open_clip_torch`/`torch` dependencies, work directory, SQLite cache. Rights check: comparison calls upload reference images to the model; confirm this is acceptable for Kurt Schwarz and Paragon material.
