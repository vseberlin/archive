# Archive application: re-audit and applied fixes

Date: 17 November 2026
Base: `main` at `9946d70`. Follows
[review-archive-index-20261117.md](review-archive-index-20261117.md) and
[review-f1deef4-reference-evaluation-20261117.md](review-f1deef4-reference-evaluation-20261117.md).

The operator's conclusion after the diagnostics run stands: the reference index
gives a real signal only for tags; place suggestions barely beat label frequency.
More evaluation tooling would not change the decision. This pass therefore applies
the fixes that decide whether an expensive master run can be reused, instead of
adding diagnostics.

## 1. State before this commit

| Item | Status at 9946d70 |
|---|---|
| 16-bit greyscale clipping | Fixed in f1deef4 |
| Shared prepared views | Fixed in f1deef4 |
| Cache key bound to path, root, mtime, role, labels | **Open** (audit §2, F1) |
| Unfinished text-region / face work reused as final | **Open** (attached audit #2, F3) |
| Tesseract traineddata not bound | **Open** (attached audit #3) |
| Face scans of large masters never complete | **Open** (F3, reproduced by operator) |
| 16-bit MinIsWhite rendered as negative | **Open** (F6, reproduced by operator) |
| Greyscale stored as RGB | **Open** (F7) |
| Dark 16-bit scans: range not recorded | **Open** (F5) |
| Index fail-closed, 2,048 cap, no catalogue/FTS layer | **Open** (audit N1–N3, F4) |
| Path-derived representation IDs for some units | **Open** (audit N4) |
| Document/PDF previews excluded by media type | Found by operator; decision pending |

## 2. Fixes applied in this commit

| Fix | Change | Files |
|---|---|---|
| Cache key v2 | Key binds unit/object/representation, member/page geometry and, with a stored SHA-256, content. Root, path, mtime, `original_path`, role, printed label and logical page no longer enter the key. Without a stored hash the locator and stamp stay bound. | `contracts.py` (`page_binding`, `CACHE_KEY_VERSION`, `key_matches`) |
| Rebinding instead of re-extraction | When the same identity and content appear at a new locator or stamp, the stored hash is verified once, the receipt is refiled with the current page under the v2 key and records `rebound_from`; no render, no extraction. Receipts under v1 keys are found and refiled. All key checks (`receipts`, `evidence`, web extraction) accept v1 or v2. | `runner.py`, `receipts.py`, `evidence.py`, `web/extraction.py` |
| Prepared views keyed by binding | Prepared-view binding uses the same identity/content binding. The receipt stores the verified source locator/stamp. Stored content hashes are checked before first preparation and after a binding change, including legacy prepared receipts without a verified binding. Unchanged bindings reuse the verification. | `prepared_views.py` |
| Retry semantics | Text-region/line-OCR coverage records requested/completed recognition variants and a stop reason. Deadline omissions and errors are retried, including older receipts inferred from missing variants within the recognition limit. Faces stopped by timeout are retried; deterministic limits (tile/region/candidate caps, face view budget) remain reusable. | `runner.py`, `extractors/text_regions.py`, `extractors/line_ocr.py` |
| Traineddata binding | OCR and text-region recipes record the SHA-256 of each language's `.traineddata`, resolved as Tesseract resolves it (`TESSDATA_PREFIX` included); names and hashes only, so moving identical files changes nothing. | `runner.py`, `extractors/text_regions.py` |
| Face budget | Orientation is searched on the whole-image view; tiles run upright plus the orientation in which the overview found faces. 8000×6000 now needs 92 views (was 356, always partial); 10000×7000 needs 121. Coverage records `stopped: budget | timeout`. Recipe version 2. | `extractors/faces.py` |
| 16-bit MinIsWhite | Inverted when TIFF tag 262 is 0. | `imaging.py` |
| Dark scans | Observed min/max recorded in the transform step; still no stretch. | `imaging.py` |
| Greyscale storage | Greyscale sources stored as 8-bit `L`; `display_rgb` converts for RGB consumers; faces convert per view. | `imaging.py`, `preparation.py`, `extractors/faces.py` |

Render policy is now version 3.

### One-time effect, by design

Render policy v3, traineddata binding and face recipe v2 change every recipe. All
existing pilot receipts are therefore recomputed once on their next run. They stay
readable as evidence (their keys still validate). This is deliberate: it bundles
every invalidating change before the long run instead of after it. From here on, a
dependency or policy change invalidates only what it changes. Byte-identical moves
and touches preserve keys when identity and a stored hash remain stable; reuse
verifies that hash at the new binding. Metadata embedding changes the whole-file
SHA-256 even when pixels are unchanged, so it still invalidates affected keys.
An obsolete hash must be rejected, not used to reuse a pre-edit render.

### Tests

Updated: `test_archive_research` (key binding, v1 lookup), `test_face_regions`
(budget/timeout states, orientation adoption on an 8000×6000 view),
`test_image_depth` (MinIsWhite, range note, `L` storage),
`test_positioned_extraction` (move + touch rebinding without render or
extraction, v1 receipt refiling, stale stored hash refused, retry semantics).
Workstation validation after the follow-up fixes: **149 core tests and 67 Django
tests passed** using the source checkout with the existing processing/web venvs;
Django used isolated synthetic project/machine configuration. Five added regression
tests failed against `40422a2` before the fixes. They cover stale hashes across
extractors, first preparation, move/touch and legacy prepared-receipt verification,
and deadlines after detection or between successful variants, enhancements and
regions. Completed and region-capped results remain reusable; older incomplete
receipts are retried. No archive-wide scan or live runtime installation was used
for this validation.

## 3. Still open before an archive-wide master scan

1. **Stored hashes.** Key v2 protects receipts only where the page's source has a
   SHA-256. Plans for the long run should carry the catalogue hashes; unhashed
   sources still re-extract after a touch or move.
2. **Path-derived representation IDs (N4).** For units without
   `representation_id`, `units.selected_members` uses the file location as
   representation; a move still changes identity and therefore the key. Supply
   representation IDs for those units first.
3. **Derived region tasks.** Visual tasks bound to a parent layout receipt carry
   the parent's receipt digest in their settings; rebinding the parent changes that
   digest, so derived crops are re-embedded once (cheap, no master decode).
4. **Index at scale (N1–N3, F4).** Per-entry staleness instead of whole-index
   refusal, SQLite FTS5 with German folding and dehyphenation, catalogue rows,
   passage/region entries, lifting the 2,048 cap.
5. **Environment freeze.** Pillow, Tesseract, pdftoppm and OpenCV versions are in
   every recipe; an upgrade mid-run invalidates what follows. Freeze the
   environment for the run.
6. **48-bit RGB TIFFs** are still refused; count them before the run.
7. **Reference index use.** Tags as reviewable leads; do not present place
   suggestions as visual evidence. Decide whether the 3,582 document previews
   become a separate stratum.

## 4. Order

Items 1–2 (data, no code) and 5 (environment) before the run; item 4 before the
extracted corpus is searched globally. Items 3, 6 and 7 do not block.
