# Review of photo research v11: accuracy and reference harvesting

Reviewed commit: [52ad782ce4275c85f1a1fe1829755f6382d54e0c](https://github.com/vseberlin/archive/commit/52ad782ce4275c85f1a1fe1829755f6382d54e0c)  
Review date: 2026-11-14  
Previous review: [review-6974e72.md](review-6974e72.md)

This review covers the specified commit. The publication branch had advanced to
`f6042b3e3a996d103e556dbba23e066bf84d6428`; subsequent changes were not re-reviewed
for this document. This commit records findings and recommendations only.

## Assessment

The two previous findings are addressed: pending-image inspection no longer
consumes the next discovery round, and general documents cannot independently
support identification without a matching page image or a reviewer-supplied INPUT
binding. The new regressions cover those paths.

The next priorities are retrieval coverage and field-specific evidence support.
Passing mechanical checks does not establish higher live recognition accuracy.

## Remaining findings

### F1 — A site match can still support an unrelated event

Priority: P2.

Source: [reference_batch.py, verify_assessments](https://github.com/vseberlin/archive/blob/52ad782ce4275c85f1a1fe1829755f6382d54e0c/Archive-tools/reference_batch.py#L784).

A matching reference image makes documents from the same page contextual evidence.
The same support predicate is then used for depicted object, location and event.
It does not distinguish a match to a building from a match to an event occurring
there.

Offline reproduction using the existing test fixtures:

- R1 is a visually matching building photograph.
- D1 is text from the same page mentioning an opening ceremony in 1964.
- The constructed model result claims that INPUT depicts that ceremony and cites D1.
- After `verify_assessments()` and `check_result()`, the event remains
  `supported`.

This is a guard test with constructed model output, not an observed live
misidentification. The failure is that the guard accepts evidence which establishes
the site but does not link INPUT to the ceremony.

Recommendation: distinguish same photograph, same object/site, same technical type
and same event in the evidence relationship. An object/site match must not
automatically qualify an event claim. Preserve the independent reviewer-binding
route for genuinely bound documentary evidence.

Regression: a matching facade plus a page describing several historical events
must not support one event without an INPUT-specific link. Include a positive case
where event evidence actually applies to the photographed scene.

### F2 — A time clue found only in a reference image can still date INPUT

Priority: P2.

Source: [reference_batch.py, visible_time_bound handling](https://github.com/vseberlin/archive/blob/52ad782ce4275c85f1a1fe1829755f6382d54e0c/Archive-tools/reference_batch.py#L807).

The visible-time-bound branch accepts a reference with a strong visual match even
when the dated feature has not been demonstrated in INPUT.

Offline reproduction:

- R1 depicts the same building and receives a matching verdict.
- The constructed date basis explicitly says that a 1972 sign is visible only in
  a later reference image.
- The proposed INPUT date is "After 1972".
- Verification downgrades the date from supported to tentative but retains the
  value; `check_result()` accepts it.

A tentative label does not make this transferred date a valid INPUT time bound.
Matching permanent architecture does not transfer temporary signs, posters or
later alterations.

Recommendation: require a visible time clue to be demonstrated in INPUT, with a
specific observation or crop, or independently bind the evidence to the exact
photograph. Reference-only clues should remain contextual limitations.

Regression: the same building before and after a dated sign was installed must
not transfer the sign's lower date bound to the earlier image.

### F3 — Candidate caps leave uninspected gallery images unreported

Priority: P2, retrieval completeness.

Source: [reference_batch.py, gather](https://github.com/vseberlin/archive/blob/52ad782ce4275c85f1a1fe1829755f6382d54e0c/Archive-tools/reference_batch.py#L1391).

The pending queue protects downloaded pool candidates. Candidates excluded before
that point are not included in it. The current limits include eight images per
record, twelve images overall, and eighteen image-fetch attempts per gather call.

Offline reproduction with fifteen distinct image URLs from one gallery and
successful stubbed downloads:

```json
{
  "offered": 15,
  "retained": 8,
  "issues": [],
  "last_retained": "https://museum.example/7"
}
```

The seven remaining URLs do not reach the pending-inspection queue. A later search
can rediscover the gallery and reach further images because previously seen URLs
are skipped, but continuation is not guaranteed or recorded as incomplete coverage.

Recommendation: retain a bounded deferred-URL list and counts/reasons for
truncation. When inspected candidates fail, allow remaining candidates from a
promising record to use the next available retrieval budget before repeating web
discovery. Keep existing cost limits. Distinguish "no match among inspected
images" from a fully inspected record.

Regression: put the relevant image ninth in a gallery; verify that its deferred
status is reported and that bounded continuation can reach it.

## Validation performed

Executed at the reviewed source version:

```sh
python -m unittest test_reference_batch test_research_routes test_photo_clue_research -q
```

Result: 128 tests passed, including 113 reference-runner tests.

The three additional cases above were executed using production verification and
gather functions with test fixtures and stubbed image downloads. No paid model
calls, protected collection access or catalogue writes were performed.

An initial broader command also named `test_preparation_guards`, but that module
could not import because the isolated review copy omitted dependencies used by
`search.py`. This is a review-environment limitation, not a demonstrated source
regression. The final 128-test command excludes that module.

The commit author's complete 186-test validation and seven saved-response replays
were not independently reproduced. No live recognition-accuracy estimate follows
from this review.

## Accuracy improvements, in priority order

1. **Measure retrieval separately from interpretation.** Use 10–15 independently
   identified photographs plus genuinely unresolved examples. Compare direct
   supplied-reference controls with automatic retrieval for the same inputs.
   Keep expected answers out of prompts. Record whether a useful reference was
   found, downloaded, inspected and correctly interpreted, as well as incorrect
   supported claims, unresolved results and token use.
2. **Search existing local reference metadata before web discovery.** The existing
   image collection can serve as a reference corpus without becoming a model
   training project. Return a small candidate set, preserving originating records
   and distinguishing prior catalogue assertions from independent confirmation.
3. **Add one structured external catalogue adapter.** Resolve records and their
   associated media explicitly, then feed the existing candidate pipeline. Test
   coverage on the difficult Berlin examples before expanding providers.
4. **Improve candidate diversity and continuation.** Avoid allowing repeated views
   of one candidate to occupy the whole pool. Preserve alternative hypotheses and
   expose deferred candidates within explicit limits.

Success criteria should include useful-reference retrieval and false supported
identifications, not merely longer descriptions, more links or more resolved
fields. Do not increase token allowances across all 111 images before the small
comparison identifies the actual bottleneck.

## Harvesting recommendation

### DDB: use API v2, not an OAI-PMH connector

The current [DDB interface documentation](https://pro.deutsche-digitale-bibliothek.de/daten-nutzen/schnittstellen),
read on 2026-11-14, states that its former OAI-PMH service has been discontinued.
It recommends API v2 and says its principal public functions are accessible
without an API key. API v1 is scheduled for shutdown on 2026-12-31.

Older indexed documentation still describes OAI-PMH and API-key requirements.
Use the current page and the linked
[API v2 documentation](https://labs.deutsche-digitale-bibliothek.de/app/ddbapi/?url=https://api.deutsche-digitale-bibliothek.de/2/q/openapi).
The official page itself notes documentation discrepancies, so endpoint behavior
must be tested during adapter implementation. This review did not execute a live
DDB API query or verify coverage for the user's specific photographs.

Proposed bounded integration:

- Query for relevant records and cache the responses.
- Preserve originating institution, inventory identifier, record URL, image URL,
  caption, the meaning of each date, rights and retrieval provenance.
- Normalize records into the current candidate representation before `gather()`,
  with access from both adaptive and portal modes.
- Preserve record-to-image relationships rather than assuming all page text
  describes every illustration.
- Deduplicate republished records by their originating identity. DDB, Europeana
  and an institution's own copy are not automatically independent corroboration.
- Never convert harvested metadata automatically into a reviewer-approved INPUT
  document binding.

This is a recommendation for an adapter, not authorization to add a new service,
database or broad harvesting subsystem.

### Sickle: only for a verified relevant OAI-PMH source

[Sickle's documentation](https://sickle.readthedocs.io/en/latest/oaipmh.html)
describes a metadata-harvesting client, not a keyword or visual search engine.
Its built-in metadata mapping targets Dublin Core; richer formats need additional
parsing.

The [Staatsbibliothek's documented OAI-PMH and IIIF interfaces](https://lab.sbb.berlin/dc/)
offer a concrete potential integration for selected publications and documentary
material. That is evidence of an available interface, not proof that its
collections contain useful matches for these photographs. Check sample records
and associated images before harvesting a collection.

Keep OAI modification timestamps separate from photograph exposure dates.
Preserve useful richer metadata and explicit media relationships rather than
flattening them into ambiguous title/date/link fields.

## Recommended next step

Address F1 and F2, expose the retrieval truncation in F3, and establish the small
direct-versus-automatic benchmark. Then test a bounded DDB v2 adapter against the
same examples. Add Sickle only after a specific relevant OAI-PMH collection has
demonstrated useful records and usable linked images.

## Implementation follow-up — v12, 14 November 2026

F1–F3 are addressed. Visual comparisons now declare their relationship to INPUT;
site matches cannot support event claims, and technical-type matches cannot
support installation locations. A visible time bound requires a specific INPUT
observation or independent reviewer binding. Reference-only date clues remain
limitations rather than proposed photograph dates.

Candidates deferred by per-record, pool or attempt limits remain available for
up to two bounded retrieval continuations, before further discovery. Receipts
record deferred URLs, overflow and the parser's 24-image truncation. Unattempted
URLs do not become seen. Cached portal resumes restore observed media URLs, and
access failures mark coverage incomplete. Exact footer-widget tokens are
excluded without suppressing layout containers.

Validation: 195 relevant offline tests passed, including field-specific support,
INPUT dating, overflow accounting, adaptive/portal continuation, cached CDN
resume and failed-access coverage. A fresh paired six-photo building pilot
completed all twelve live trials with identical high-effort settings and identical
first-discovery prompts. Both versions correctly supported two of three
independently checked identities; three unknown identities were left unscored.
V12 downloaded/inspected more references (66/52 versus 42/34), but both produced
three supported INPUT fields. This is evidence of improved retrieval coverage,
not improved recognition accuracy. One V12 trial drifted from a facade toward a
truck; generic harvested metadata also included noisy source text. These remain
limitations. Live-web/model variation and the small sample preclude causal or
population claims. Private receipts, images and the full comparison report stay
outside Git; no catalogue metadata was adopted.

The live candidate was frozen before the final portal-resume and access-coverage
follow-ups. Those two changes were tested offline and do not change adaptive
retrieval or model decisions. No new adapter or harvesting subsystem was added.
