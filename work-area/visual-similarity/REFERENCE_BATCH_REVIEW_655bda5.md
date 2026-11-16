# Review of commit 655bda5 (`Archive-tools/reference_batch.py`, v7 -> v9)

Scope: full diff read, `test_reference_batch.py` run (87/87 pass), small probe scripts against the code.
Not run: Codex, network, the pilot artifacts quoted in the commit message (they are outside Git).
Other Archive-tools suites show 47 failures in files this commit does not touch, mostly missing `exiftool`/`vips` in the review sandbox; not diagnosed further.

## What is solid
- Caption-blind comparison runs first; the downgrade to `tentative` is enforced in code.
- Paired detail crops come from hash-checked originals.
- `date_basis` keeps historical context out of the photograph date.
- Stop reasons, `retryable`, URL provenance recorded explicitly.
- `direct` mode separates comparison quality from retrieval.
- Commons category pages resolve through the media API.

## Findings, most severe first
Status: R = reproduced by probe, C = from reading the code, N = not measured.

1. **(R) Whole pages excluded on common WordPress classes.** `Page` applies the regex
   `(?:^|[-_\s])(?:related|recommendations?|suggest|footer|sidebar)(?:[-_\s]|$)` to every element's class/id.
   `<body class="no-sidebar">`, `<html class="has-sidebar">`, Genesis `content-sidebar-wrap` each yield zero images.
   Fix: never exclude `html`/`body`/`main`/`article`; match whole class tokens.
2. **(C) Large Commons files fail.** `Fetcher.commons_api` sets `candidate_image_url` to the full original; `Fetcher.image` caps fetches at 12 MB with no thumbnail fallback.
   In the categories browsed on 9 Nov: ~9/51 Kulturbrauerei and 3/12 Pfefferberg files exceed the cap.
   Fix: fetch the 1800 px thumbnail for the pool, the original only for paired crops.
3. **(R) A cited document bypasses the blind guard.** Every fetched HTML page also yields a `catalogue_record` document.
   A `supported` claim citing R1 + D1 stays `supported` although the blind verdict on R1 is `different`. Direct pilots carry no documents, so the six controls never exercised this.
   Fix: if a claim cites any R image, require one cited R image to be `matching`.
4. **(N) Default budget too small.** Defaults stay at 30k tokens per item / low effort, while a round is now up to five model calls; the v3 pilot used 78-111k per item with fewer calls.
   Likely `usage_limit` on the first item, which stops the batch.
5. **(R) One model slip fails the row.** The blind check must cover every reference exactly once (up to 8); one invalid shortlist ID has the same effect.
   Fix: missing reference -> `insufficient`; drop unknown IDs.
6. **(R) Documents-only rounds still pay for a blind call**, with only the INPUT image attached. Fix: skip when there are no reference images.
7. **(R) Rejected references are carried forward.** `retain_evidence` always keeps two refs even when none was supported/tentative; they enter the next search prompt and are re-compared. Fix: drop those the blind check called `different`.
8. **(R) A date readable in the INPUT is wiped.** `date_basis` needs an R/D ID, so a banner reading "1. Mai 1972" ends `unresolved` and is filed as "Date context only". Fix: let `visible_time_bound` cite the input and stay `tentative`.
9. **(R) `raw_result` lost on cached re-run.** The receipt is overwritten with the downgraded result; resume / `--retry-failed` then copies it as "raw". Fix: `result.setdefault('raw_result', ...)`.

## Minor
- Non-WordPress `<picture>`/srcset elements yield the same photo twice.
- `<video><source>` URLs become image candidates.
- Pools over 6 go back through a 256 px contact sheet.
- `stop_reason` says `identified` for location and dating goals too.
- `ROUTES` import unused.

## Design note
Discovery is still per-image web search; there is no shared candidate list of sites (gazetteer from Wikidata/Denkmaldatenbank/Commons, CLIP kNN, site contact sheets). That would need the AGENTS.md approval gates (new directory, dependency, `query.wikidata.org` in the registry).

## Suggested order
1-3 first (retrieval and guard correctness), then 4-9, each with a regression test in `Archive-tools/test_reference_batch.py`.
