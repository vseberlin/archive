# Review: archive photo retrieval and visual verification

Reviewed commit: [655bda5](https://github.com/vseberlin/archive/commit/655bda539ba65d496d966b3598fb33c12ea901ca)  
Review date: 2026-11-14  
Scope: `Archive-tools/reference_batch.py` v9, its regression tests, the shared research routing helper, and workbench integration at this commit.

This document records a source review requested by the repository owner. Proposed corrections below have not been implemented by this documentation commit.

## Assessment

The commit materially improves the workflow. It fixes the earlier two-images-per-page bottleneck, preserves explicitly discovered image URLs, permits unseen gallery candidates in later rounds, creates requested crops before comparison, carries retained references into discovery, and makes partial failures retryable.

Four remaining implementation gaps can suppress correct matches or leave conclusions more confident than their verified evidence supports. A separate selection limitation remains relevant to recognition quality.

The recommendations concern the existing research runner and its reporting. They do not require a new service, workflow engine, database, or archive structure.

## Findings

### R1 — Verification downgrades do not consistently reach the displayed conclusion

**Priority:** P1 — misleading conclusions can survive verification.

**Evidence:** [compare_verified(), lines 1030 onward](https://github.com/vseberlin/archive/blob/655bda539ba65d496d966b3598fb33c12ea901ca/Archive-tools/reference_batch.py#L1030), [retain_evidence()](https://github.com/vseberlin/archive/blob/655bda539ba65d496d966b3598fb33c12ea901ca/Archive-tools/reference_batch.py#L726), and [workbench summary rendering](https://github.com/vseberlin/archive/blob/655bda539ba65d496d966b3598fb33c12ea901ca/Archive-tools/workbench.js#L442).

The post-processing guard can downgrade `depicted_object` and `location` from supported to tentative, and clear an unsupported `photograph_date`. It leaves `summary` and `hypotheses` unchanged.

A model response can therefore retain a supported hypothesis and a confident summary even after its corresponding structured assessment has been downgraded. The workbench displays the summary directly. Supported hypotheses also receive high weight when selecting evidence to retain for another round.

**Suggested correction:** apply verification consistently to corresponding hypotheses and construct the displayed conclusion from the final assessments. Preserve the original model response separately.

**Regression check:** provide a response with a supported object, a supported hypothesis, a confident summary and a historical-context date, while blind comparison reports no distinctive match. Verify that the final assessments, hypothesis statuses, retained-evidence ranking and displayed conclusion agree. The unmodified response should remain available as raw evidence.

### R2 — Selecting the largest image has no fallback when that variant cannot be fetched

**Priority:** P2 — usable references can be lost.

**Evidence:** [Commons image selection](https://github.com/vseberlin/archive/blob/655bda539ba65d496d966b3598fb33c12ea901ca/Archive-tools/reference_batch.py#L439), [the 12 MB download ceiling](https://github.com/vseberlin/archive/blob/655bda539ba65d496d966b3598fb33c12ea901ca/Archive-tools/reference_batch.py#L389), and [gather() download handling](https://github.com/vseberlin/archive/blob/655bda539ba65d496d966b3598fb33c12ea901ca/Archive-tools/reference_batch.py#L1283).

Commons retrieval now selects the original image and records the available 1800-pixel thumbnail in `image_variants`. Downloads still reject responses exceeding 12 MB. On failure, `gather()` records an issue and proceeds to the next candidate; it does not try another known variant of the same photograph.

A large scan, or an inaccessible original with a working derivative, can therefore be excluded despite having a usable reference image.

**Suggested correction:** try observed variants in descending quality order until one succeeds. Preserve the requested image, the delivered variant and the reason for fallback. Record whether native-resolution reference cropping is available; a derivative should not be represented as the original.

**Regression check:** make the original exceed the size limit while an observed derivative succeeds. Confirm that the photograph reaches comparison once, with accurate resolution and provenance. Also cover an unavailable original and ensure no unobserved URL is constructed.

### R3 — Blind verification omits already available INPUT detail crops

**Priority:** P2 — a correct identification can be downgraded because the decisive pixels were not supplied to the verification call.

**Evidence:** [compare_verified(), lines 975–1013](https://github.com/vseberlin/archive/blob/655bda539ba65d496d966b3598fb33c12ea901ca/Archive-tools/reference_batch.py#L975).

The function receives `crops`, but the first blind call receives only the INPUT overview and the reference overviews. Existing INPUT detail crops are supplied to the later caption-informed comparison.

If discovery has already isolated a small inscription or architectural feature, the final comparison can see useful detail that the blind stage never inspected. The blind stage's paired-crop mechanism helps only when it independently requests the necessary detail.

**Suggested correction:** include existing INPUT detail crops in blind verification with neutral labels. Avoid introducing candidate names or discovery interpretations through the crop labels.

**Regression check:** supply an INPUT detail crop before comparison and assert that it reaches the first blind call. Use a small positive/negative image control where the distinguishing feature is legible only in the crop; verify that metadata is withheld from this stage.

### R4 — Image URLs observed through tool actions are not admitted consistently

**Priority:** P2 — valid discovered media can be rejected.

**Evidence:** [adaptive discovery prompt](https://github.com/vseberlin/archive/blob/655bda539ba65d496d966b3598fb33c12ea901ca/Archive-tools/reference_batch.py#L1371), [image provenance construction](https://github.com/vseberlin/archive/blob/655bda539ba65d496d966b3598fb33c12ea901ca/Archive-tools/reference_batch.py#L1454), and [record() image admission](https://github.com/vseberlin/archive/blob/655bda539ba65d496d966b3598fb33c12ea901ca/Archive-tools/reference_batch.py#L555).

The prompt allows URLs observed in search results or tool actions. Page URLs support both sources. The image flag `image_observed_in_search`, however, checks only search-result URLs.

If a specific image was opened by a web tool but does not appear in the fetched page's static HTML, `record()` rejects it. Since the exception aborts the record call, otherwise usable evidence from that page is also lost.

**Suggested correction:** preserve an independent `image_requested_by_tool` state and admit that URL to guarded retrieval without labelling it as a search result or a successful fetch. Keep retrieval success and source-page association explicit.

**Regression check:** supply an allowed page plus an image URL present only in a completed tool action, with no corresponding image in the static HTML. Verify that guarded image retrieval is attempted and that the provenance states remain distinct.

## Remaining recognition-quality limitation

### Q1 — Small contact sheets can still exclude the decisive reference permanently

**Evidence:** [contact_sheet()](https://github.com/vseberlin/archive/blob/655bda539ba65d496d966b3598fb33c12ea901ca/Archive-tools/reference_batch.py#L203), [gather() seen tracking](https://github.com/vseberlin/archive/blob/655bda539ba65d496d966b3598fb33c12ea901ca/Archive-tools/reference_batch.py#L1290), and [visual_selection()](https://github.com/vseberlin/archive/blob/655bda539ba65d496d966b3598fb33c12ea901ca/Archive-tools/reference_batch.py#L1526).

Pools exceeding six pictures are filtered through a contact sheet using 256-pixel tiles. Rejected candidates do not receive full-resolution comparison. They have already been marked seen during download, so revisiting the page does not automatically give those candidates another detailed inspection.

This is a quality risk rather than a demonstrated recognition failure at this commit. A correct architectural or machinery match may depend on details that cannot be judged at tile resolution.

**Suggested correction:** distinguish downloaded candidates from candidates inspected at a useful resolution. Permit uncertain shortlist candidates to receive a larger view within the existing budget.

**Validation:** compare the same decisive reference in a small pool and a pool exceeding six pictures. Record whether it reaches detailed comparison, rather than measuring only whether the run completes or returns valid JSON.

## Validation limits

This review was performed through the GitHub connector against the exact commit above. The Python execution environment was unavailable, so the reviewer did not independently rerun the tests or execute a live recognition pilot.

The commit message reports 159 passing targeted regression tests, six direct controls and a two-round adaptive trial. Those are reported results; the live artifacts are outside Git and were not independently reproduced during this review.

Several inspected workflow tests inherit settings without `visual_verification`, while production preparation enables it. The visual-selection test supplies a mocked successful selection; it checks propagation of that selection, not the model's ability to recognize the correct photograph on the contact sheet.

## Recommended next verification

1. Add focused regression coverage for R1–R4 and include a workflow test with production verification enabled.
2. Compare direct-reference and adaptive runs on the same small set of photographs.
3. Record whether the decisive reference was discovered, downloaded, selected, inspected at useful resolution and correctly assessed.
4. Compare correctness and token use before another full batch.

The purpose is to distinguish discovery, selection and comparison failures. Increasing the source registry or research budget alone would not establish which stage is losing the useful evidence.


## Implementation follow-up — v10

R1–R4 and Q1 are addressed in the existing runner and workbench. Final fields,
hypotheses and displayed conclusions now agree; image-plus-document claims
cannot bypass visual verification. Observed derivative fallback, neutral INPUT
crops, distinct tool-action image provenance and pending full-size inspection
have focused regression coverage with production verification enabled.

The companion review's layout exclusion, documents-only calls, rejected-image
carryover, tentative INPUT dates, cached raw-result preservation, invalid IDs,
picture/video parsing, default allowances and goal-specific stop labels are also
covered. Original reviewer findings above remain as the historical review.

Validation: 173 targeted tests passed, including browser-JavaScript behavior
through the existing test harness. Seven saved real comparison results retained
their supported/unresolved field assessments and raw outputs. An injected failure
of an original image led to a real download of its observed smaller variant.
No new paid model recognition run was performed; recognition accuracy and the
higher default research allowances have not received a new live batch benchmark.
