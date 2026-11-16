# Review of commit 6974e72 (`Archive-tools/reference_batch.py` v10)

Follow-up to `REFERENCE_BATCH_REVIEW_655bda5.md`. Method: full diff read, tests run, probe scripts against v10.
Not run: Codex, network, the saved real comparisons or the "173 targeted tests" count from the commit message (artifacts outside Git).

## Tests
- `test_reference_batch.py` 100/100, `test_research_routes.py` 6/6.
- `test_workbench.py` 53/54; the TIFF demo failure also occurs on the parent commit in the review sandbox.

## Status of the nine earlier findings
| # | Finding | v10 |
|---|---|---|
| 1 | Whole page excluded on `no-sidebar` etc. | Fixed (R). Wrapper divs with `sidebar-right`, `footer-fixed`, `related-…` still exclude their whole content. |
| 2 | Large Commons originals fail | Fixed via thumbnail fallback (R). Original still tried first: each oversize file costs a 12 MB download and one of 18 attempts. |
| 3 | Cited document bypasses blind guard | Fixed for R + D (R). D-only citation still passes, see N2. |
| 4 | Default budget too small | Partly. Adaptive 150k (500k high); portals/direct stay 30k at low/medium. Unmeasured. |
| 5 | One model slip fails the row | Fixed (R). |
| 6 | Documents-only blind call | Fixed (R). |
| 7 | Rejected refs carried forward | Fixed (R). |
| 8 | Date visible in INPUT wiped | Fixed (R), kept `tentative`. |
| 9 | `raw_result` lost on cached re-run | Fixed (R). |
Minor items (picture duplicates, video sources, stop labels, unused import) also fixed.

## New or remaining findings, most severe first
(R = reproduced by probe, C = from reading code)

- **N1 (R) Pending queue replaces the follow-up search.** Pool of 7: round 2 compares the single leftover image and runs no search. `--max-rounds` is capped at 2, so when round 1 downloads >6 images (>4 in portals mode) the comparison's `next` terms are never searched. In portals mode native crops and clue refinement are skipped too (C). Fix: don't count inspection-only rounds against `max_rounds`, or queue only candidates the shortlist was unsure about.
- **N2 (R) Document-only citation bypasses the blind guard.** Every fetched page yields a document with the same `record_url` as its images. With R1 and R2 `different`, a claim citing only D1 stays `supported`; the row stops `identified`. Fix: a document from a page whose delivered images are not `matching` must not support a claim alone.
- **N3 (R) `photograph_date` not covered by the blind guard.** A `photo_record` date citing R1 stays `supported` when R1 is `different`; a dating run stops `dated`. Predates the commit.
- **N4 (R) Overview and first crop carry the same banner.** Both read "INPUT native detail 1" in adaptive/direct mode (box label dropped from the banner); both go into search and shortlist calls. Fix: label the overview plainly "INPUT".
- **N5** One item over the allowance still stops the whole batch. Suggest `--limit 3` and inspecting the token spread before a full batch.

## Minor
- Compare call in an inspection-only round gets `features=[]`.
- `photograph_date` citing INPUT while `date_basis` cites only R1 fails with "absent reference".
- Oversize failures are cached for 5 minutes only; a resume re-downloads 12 MB.
- `make_crops` is now unused.
