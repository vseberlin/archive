# Follow-up review: photo research v10

Reviewed commit: [6974e72](https://github.com/vseberlin/archive/commit/6974e7223cf36f291e9538e88dd63d600a2ff6f9)  
Review date: 2026-11-14  
Scope: photo retrieval, visual verification, pending-reference inspection, and workbench reporting.  
Previous review: [review-655bda5.md](review-655bda5.md).

This records the review of the exact commit above. Later commits on the branch were not re-reviewed as part of publishing this document. The suggested corrections below are recommendations, not changes implemented by this documentation commit.

## Assessment

The previous fixes are largely in place and the reference-runner tests pass. Observed image variants are tried after download failures, neutral INPUT detail crops reach blind verification, summaries and hypotheses are synchronized with final assessments, tool-action image URLs are admitted with separate provenance, and cached raw responses are preserved.

Downloaded candidates omitted from the contact-sheet shortlist now enter a queue for detailed inspection. Two remaining gaps affect adaptive follow-up and the strength of documentary evidence accepted by the verification guard.

## Findings

### F1 — Pending-image inspection consumes the remaining discovery round

**Priority:** P2 — adaptive follow-up can be prevented by the size of the first result pool.

**Evidence:** [process_adaptive() loop and inspection-only branch](https://github.com/vseberlin/archive/blob/6974e7223cf36f291e9538e88dd63d600a2ff6f9/Archive-tools/reference_batch.py#L1516), [pending-candidate selection](https://github.com/vseberlin/archive/blob/6974e7223cf36f291e9538e88dd63d600a2ff6f9/Archive-tools/reference_batch.py#L1580), and [the two-round CLI limit](https://github.com/vseberlin/archive/blob/6974e7223cf36f291e9538e88dd63d600a2ff6f9/Archive-tools/reference_batch.py#L1964).

The same loop counter limits both web discovery and inspection of downloaded candidates. When the first pool contains more than six photographs, uninspected candidates cause the second iteration to skip discovery. The CLI permits at most two iterations.

A deterministic reproduction used eight candidate images, production visual verification, sufficient token budget, and comparisons that remained unresolved while requesting a concrete follow-up query:

1. Round one searched and inspected six photographs.
2. Round two inspected the remaining two without searching.
3. The run stopped at `round_limit`, even though `needs_more_references=true` and a specific next query remained available.

Observed result:

```json
{
  "search_calls": 1,
  "rounds": 2,
  "stop_reason": "round_limit",
  "pending": 0,
  "unanswered_query": ["Berlin chimney diamond brick ornament"]
}
```

The queue fixes permanent exclusion of downloaded pictures, but under this common condition it uses the only iteration available for adaptive follow-up. Increasing the token allowance cannot restore the missing discovery iteration.

**Suggested correction:** process bounded inspection batches within a discovery round, or count discovery rounds separately from inspection batches. Continue to enforce token and call limits. Avoid turning this into an unbounded retry loop.

**Regression check:** start with eight images, inspect all of them, then request a concrete new search. With two discovery rounds allowed and sufficient remaining budget, assert that a second discovery call occurs. Separately verify termination when the goal is met, the budget is exhausted, or no actionable question remains.

### F2 — A document-only citation can still bypass a rejected image from the same page

**Priority:** P2 — the verification guard can preserve a supported identification without independently bound documentary evidence.

**Evidence:** [verify_assessments(), field checks](https://github.com/vseberlin/archive/blob/6974e7223cf36f291e9538e88dd63d600a2ff6f9/Archive-tools/reference_batch.py#L793) and [check_result()](https://github.com/vseberlin/archive/blob/6974e7223cf36f291e9538e88dd63d600a2ff6f9/Archive-tools/reference_batch.py#L1399).

The new guard catches a supported claim citing rejected image `R1` together with document `D1`. If the claim cites only `D1`, its cited-image set is empty and the presence of a document citation is sufficient to avoid downgrade.

The reproduction supplied:

- a reference image `R1` with blind verdict `different`;
- general documentary text `D1` from the same source page;
- a model response claiming a supported object identification using only `D1`.

Both `verify_assessments()` and `check_result()` accepted the identification as `supported`.

This is a reproduced safeguard gap using a constructed model response. It does not show that a live model necessarily makes the mistake. Independently bound documentary evidence must remain usable: for example, a record demonstrably describing the INPUT photograph can establish identity even when an incidental comparison image does not match.

**Suggested correction:** distinguish a document explicitly bound to INPUT from general candidate-page context. Do not let a generic page qualify as independent proof solely because the model omits its rejected R citation. Preserve legitimate documentary identification when that binding is established.

**Regression check:** cover both cases: an unrelated candidate article with a rejected image must not establish identity; an independently bound photograph record may establish identity with an explicit explanation of the binding.

## Verified improvements

| Area | Review result |
| --- | --- |
| Original-image download failure | Observed alternative variants are attempted, with fallback and resolution information retained. |
| INPUT crops | Existing detail crops reach blind verification with neutral banners. |
| Reporting | Final fields, hypothesis statuses and displayed conclusions are reconciled; the workbench builds its conclusion from final fields. |
| Tool-action image URLs | Admitted separately from search-result and page-association evidence. |
| Cached responses | Raw model responses are preserved when verification is reapplied. |
| Contact-sheet omissions | Downloaded candidates are queued for larger inspection, subject to F1 above. |

## Validation performed

The exact source files were fetched through the GitHub connector and tested in an isolated local copy.

| Check | Result |
| --- | --- |
| `python -m unittest test_reference_batch -q` | 100 tests passed. |
| `python -m unittest test_workbench -q` | 53 passed; one error out of 54 tests. JavaScript behavior checks passed. |
| F1 focused reproduction | One discovery call across two iterations; a requested follow-up query remained unanswered. |
| F2 focused reproduction | Document-only citation retained `supported` despite the same page's image being rejected. |
| Cached Osthafen page extraction | Machinery photograph retained as the observed 2000-pixel image URL, third in an eight-image pool. |
| Cached Pfefferberg page extraction | Useful courtyard photograph ranked first in a five-image pool. |

The workbench failure was `test_demo_has_working_tiff_pdf_and_jpeg_previews`, in unchanged `create_demo()` code at `workbench.py:819`. Saving the multipage TIFF raised `TypeError: function takes at most 4 arguments (18 given)` under the local Pillow 13.0.0 installation. It also failed when run alone. This review did not establish the underlying library cause; the failing application code was unchanged by the reviewed commit.

The cached-page checks exercised extraction and selection with image downloads stubbed. They establish preservation of the reference URLs, not successful fresh downloads or live recognition.

No new paid recognition run was performed. The commit author's broader reported validation was not independently reproduced in full.

## Operational change: token allowances

[Preparation defaults](https://github.com/vseberlin/archive/blob/6974e7223cf36f291e9538e88dd63d600a2ff6f9/Archive-tools/reference_batch.py#L876) increase the adaptive per-image and batch-average thresholds from 30,000 tokens to:

- 150,000 for low or medium effort;
- 500,000 for high effort.

These are between-call stopping thresholds, not predicted consumption or hard caps. An individual call can take total usage beyond the threshold before the next check. Set explicit allowances during pilots and measure accuracy alongside token use.

## Recommended next step

Address F1 first because it directly limits investigation of new visual clues. Add the documentary-binding cases from F2, then compare direct-reference and adaptive runs on the same small image set. The successful mechanical tests do not yet establish the recognition accuracy of a new live batch.
