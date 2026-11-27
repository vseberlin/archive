# Local WF research pilot

The approved pilot provides local login and the original 16 real photograph,
report and relationship review cases, plus three connected research baseline jobs.
These include published book and patent material, publication photographs,
caption-based names, Brigadebuch events and cross-unit photographic evidence.
The homepage shows real cases; the synthetic demonstration has a separate link.
There are 69 source views: 68 prepared images and one explicitly unreadable TIFF.
Bounded assertion review/correction, readable history and recovery share the
existing authority files. Citation links open a region viewer and bound source.
Generic research administration remains read-only.
All real observations start unreviewed. Review and export remain proposals;
neither adopts catalogue metadata nor authorizes changes to archive holdings.
See the application's [design contracts](../README.md#design-contracts) for
identity, evidence and storage boundaries. Private operating approvals and case
records are not distributed with the application.

## Run with explicit configuration

Activate the selected private Python environment. The installed
`iss-archive-research[web]` package includes the application,
templates and invented fixture data. `pyproject.toml` pins the web dependencies. Poppler supplies fixture PNGs.
Project and machine TOML files must already exist in the private runtime root.
A real-evidence project selects `mode = "local_review"`; a standalone synthetic
demonstration selects `mode = "synthetic"`. The machine file supplies `runtime_root`
and a private random `secret_key` of at least 50 characters. Do not copy that file
or `operator-login.txt` into Git.

```sh
export ARCHIVE_RESEARCH_PROJECT=/path/to/project.toml
export ARCHIVE_RESEARCH_MACHINE=/path/to/machine.toml
archive-research web check
archive-research web serve
```

Check port 8785 before starting one manually owned process. The launcher refuses
other runserver arguments. Stop the owned foreground process with Ctrl-C.
Open <http://127.0.0.1:8785/> and read credentials locally from
the configured runtime's `operator-login.txt` (mode 0600 on Linux).
`archive-research web init` initializes an explicitly configured fresh runtime;
it is not part of the normal launch of this existing pilot. This is a local
development process; there is no service or startup automation.

## Unified review workspace

The text panel follows both displayed pages, with fixed Source and Compare with
columns. Explicitly selected OCR blocks retain their source coordinates in the
shared search projection. “Show passage on page” highlights that region on the
corresponding full-page preview; cropped previews do not reuse full-page boxes.
Separately prepared and crop-specific cached readings retain priority. A full-page
cache copied from an indexed extraction follows the currently selected reading
for that exact source page, including its blocks, when the page is reused in a
different case. An older cache must not mask a later selected assembly.
Unreviewed text and possible omissions
are labelled in plain language; extractor settings remain outside the workbench.
A refreshed projection is required to make new extraction results visible.

Explicitly prepared profile examples use the same source-bound cases and review
history. Their per-view cached readings can retain bounded table rows, raw header
text and unassigned words. The shared text panel renders escaped cells, labels
unrecovered words and warns that text-line bands are not verified logical rows.
It does not infer header spans, repair numbers or accept findings. Source/preview
binding changes withhold the cached reading; no processing runs during requests.


The Existing catalogue panel follows the two displayed sources. It shows their
prepared titles, dates, creators, descriptions, subjects, recorded relationships
and inscriptions where available, with catalogue attribution. Links use the
catalogue's exact representation path and named root. Descriptions remain
available without OCR or an online source; missing links and differing accounts
are explicit. This context uses the shared research index and does not adopt or
rewrite a finding. Technical provenance remains in the prepared index and logs.

**Presentation rule, agreed 20 November: one question, one fixed comparison,
one answer.** The workbench is for answering research questions. It presents
relevant evidence, proposed findings, contradictions, uncertainty and review
decisions. Detail, full-page and zoom controls inspect the assigned evidence;
they do not change the question or introduce unrelated sources.

Source and Compare with are fixed by the question. A dropdown is permitted only
when that question requires several report pages, and then selects only the
relevant pages of the assigned report. It cannot select another object, report or
comparison candidate. Give each candidate comparison its own named question.
Questions must ask for an archival answer; page counts and "images inspected"
are evidence limits or activities, not questions or answers. Show relevant gaps
once and make clear which proposed answer Confirm will save.

Processing details belong in existing technical logs and run receipts, not in
the workbench, including collapsed panels. Keep engine/model details, settings,
retries, timings, raw scores and processing failures out of the research view.
Show their research consequence only when relevant: "Transcription available;
uncertain passages need review" or "No transcription available", rather than
"Tesseract returned no words". Preserve source attribution, review history,
alternative readings and evidence limitations; do not imply a draft is verified.
The [canonical presentation contract](../../../docs/architecture/archive-django-consolidation-plan.md#production-research-view-and-experimental-inspection)
and [R/D decisions and acceptance checklist](../../../docs/architecture/archive-django-consolidation-plan.md#agreed-r-and-d-workbench-specification--20-november)
own the requirements and case-specific implementation status. Record future
workbench decisions there. R02's fixed bindings and shared metadata context are
delivered; R01/R03 candidate navigation, D-case restructuring and remaining
technical UI wording still need implementation. Documentation does not mark them
complete.

The case URL keeps a filtered case list, two evidence panes, page text and the
finding form on one screen. The anchor and comparison selectors use only the
case's existing bound views in the legacy layout; their presence does not satisfy
the fixed-question contract. Replace them as specified above. Image/Compare and Enlarge workspace change the
presentation without changing the draft. Zoom fits each prepared image before
magnifying it; the original PDF/image remains available through its source link.
Text/OCR, recorded context, source details and history share an inline drawer.
Existing valid OCR or exact-page text in the selected search projection is reused;
missing, stale or unusable text remains explicit. No extraction runs in HTTP.

Confirm, Reject and Leave unresolved save through the existing review operation.
A reviewer note is optional. Scope, uncertainty and supporting citations remain
editable under an expandable section. Next after decision preserves queue filters
and advances only after a successful save, prioritizing another unreviewed finding
in the same case. Save errors retain the draft; saved-but-stale recovery never
advances or repeats the write. Leaving an edited page warns about unsaved changes.
Drafts live in the current page only; no separate draft store was introduced.

This slice does not implement candidate clustering, overlays, new relationships,
automatic metadata adoption or the full document-preparation gate. Full-page OCR
coverage and quality qualification remain preparation responsibilities. The
workspace reports missing text and does not label these views ready for review.
The standalone review and citation URLs remain available for existing links.

### Questions with fixed evidence

A case may bind existing findings to `finding_views` in its existing evidence
record. Each binding contains `question`, `source_view`, `evidence_view`,
`source_context` and `evidence_context`. Both displayed sources are fixed for
that question; selecting another question changes the finding and its evidence
together. Each context list permits one existing full-page view of that same
source page, exposed as Detail / Full page buttons. Anchors must refer to pages
already cited by the finding. Unrelated sources and unknown findings are refused.
These are presentation bindings, not reproduction decisions or new findings.
Existing review forms, citation choices, stale-draft protection and history apply.

## Documentary people and manual attributions

Cases with explicitly prepared `documentary_people` expose a People link. The
candidate list filters names and documented event/date/department/attendance or
catalogue context; it does not read image features or rank people by appearance.
Each candidate needs a documentary rationale, source-view citations and uncertainty.
Documented portraits need their own attribution text and supporting citations.
Missing portraits are shown explicitly. This is a bounded case selection, not a
global person index or an assertion that all attendees appear in a photograph.

Cases with `documentary_overview` open an event/document view first. It shows
distinct photographs with attributed human count observations and a source-by-source
name table. Full names, surname-only references and absence from a selected passage
remain distinct. Each row links an existing `names_event_participant` finding;
confirmation uses the existing review endpoint without requiring a photo position.
The finding must cite every displayed name source. Document quality notes and other
uncertain readings remain visible. Counts are not attendance totals or identity links.

For optional photo attribution, the reviewer chooses a displayed photograph, describes a person's position, selects
a candidate (or leaves the name unresolved), supplies reasoning/uncertainty and
selects supporting sources. The operation appends a `person_attribution` assertion
to the existing JSONL history under the existing record lock. It retains the
candidate's documentary rationale, portrait context, exact view descriptions and
bindings separately from the reviewer's source choices and attribution reason.
Reviewer/time, accepted/rejected/unresolved outcome and corrections use the existing
history and projection. Earlier versions remain readable. Stale forms and changed
sources are refused; a saved-but-unindexed result uses the existing rebuild flow.

No name is assigned automatically. No facial embeddings, person similarity scores,
biometric matching or additional database is introduced. These manual research
attributions remain unadopted and real-material export remains disabled.

The review page keeps the photograph beside a scrollable evidence/decision pane,
with photo navigation, inline document/portrait/context panels, zoom, a manual
position marker, history and Save & next. Draft text is retained per target in
this browser tab's session storage, never as a saved review. Its original revision
is retained too: restoring an older draft cannot silently bypass stale-form checks.
If browser storage is unavailable, unsaved navigation warns instead. The prototype's
invented detector boxes, hard-coded reviewer, external scripts and browser-only
review history are not used.

### Photograph matches and search feedback

The shared case workspace renders explicit `reproduction_pairs` with one fixed
source in the original two-pane layout. A candidate-only dropdown controls the
right image and its per-pair Match / Not a match / Uncertain controls below. Switching
candidates displays their saved state; saving retains the candidate and current
context finding. The controls submit a bounded decision and optional note; the server generates the
finding and citations from the configured views. No candidate decision changes
another candidate or an event/context finding. This view requires no people data
and has no case-ID branches. Notes retain their original revision in tab drafts.
Both previews share one grid row so their window heights stay equal. Compact
decision toggles sit outside the preview grid, below the candidate. Optional notes
live in the shared Comparison note drawer tab and remain associated with their
pair's form; saved history appears only in the existing History tab.

A combined finding may reference its individual pairs through `component_assertions`.
Its earlier wording/history remains readable, but only the individual pairs are
active in search and review queues. The summary is derived from current pair
decisions; no automatic aggregate approval or inferred transitive link is saved.
Combined findings cannot be independently reviewed after decomposition.

An explicit case `reproduction_pairs` mapping links each reproduction finding to
its query/source view and candidate photograph. The Photograph matches tab uses
the existing review operation; caption-name findings remain separately reviewable.
Confirm/reject/unresolved save exact source stamps, physical page and normalized
region for both sides. This is photograph-reproduction feedback, never a person
blacklist. A change of page, region or source version is a different comparison.

Reviewed matches sort ahead of new leads. Rejected pairs are hidden by default
and remain available through Show rejected, history and corrections. The same
feedback applies to the selected search projection's visual query in either
direction, before pagination. Contradictory reviews remain visible. Raw similarity
scores are unchanged; no retraining or new extraction occurs.

Confirm saves the readable decision and updates the SQLite case view. The next
workbench search that needs findings brings the selected search index up to date
from the latest saved history. Several decisions can share one refresh. The
explicit rebuild action also refreshes search. Tools that read the durable index
file directly see its most recently published snapshot.

Case pages use verified catalogue and OCR data from the selected base index.
The process reuses those data across case views while the file binding remains
current. A search refresh failure leaves the saved decisions available in the
case history. Correct the index problem and retry the search.

## Prepared page search and image leads

The project may explicitly select `extraction_roots`, `extraction_plan` and
`visual_leads`, pointing to private files in one approved work directory. The
shared [extraction package](../README.md) must be importable
for these optional routes. No extra Python dependency or database is introduced.

`/research/` searches existing page receipts across units and opens exact word
regions on bound processed pages. Source role, representation member, physical
page/frame and printed label stay distinct. No-text, failed and offline results
remain explicit. Search covers only the selected trial, not the entire archive.
Existing comparison links preserve the case's review/correction workflow; search
never edits assertions or adopts machine text. Native PDF text and new OCR remain
separate results. Image/source links carry a snapshot revision and withhold stale
or changed bindings. HTTP requests never render, extract or rerun a model.

`/research/leads/` shows a bounded projection of retained visual neighbours.
Similarity remains a lead. Each side requires its original source to be available;
the old preview is not silently substituted when the source is offline.

`/research/packet/` presents one locally prepared, profile-guided evidence packet.
Set `research_packet` to its flat JSON filename in the extraction `artifacts`
root. The [package guide](../README.md#source-regions-and-local-evidence-packets)
documents its CLI and explicit selection format. Whole-page views, detail regions,
caption links, competing hypotheses and original OCR variants remain separate.
The four combinable profiles belong to the existing subject routes. Manual
person/face regions do not identify people; documentary links remain proposals.
Packet/source/image routes require the existing operator login and are read-only.
Changed receipts require a rebuild; missing sources withhold their images. No
model or external submission runs here, and research history is never updated.
Whole-page entries with an attached automatic text-discovery receipt show
candidate outlines, retained crops and independent readings. Coverage distinguishes
completed scanning from unreadable, conflicting or unattempted recognition. Views
without discovery say so explicitly; empty whole-page OCR is not marking coverage.
Candidates with retained enhancement variants offer “Compare image preparations”
and list the enhancement beside each independent OCR attempt. Preview routes
accept only variants bound to the selected candidate's current receipt; unknown,
missing or changed variants are refused. Original crops remain visible.
The scanner runs only through the explicit extraction CLI, never a page request.
An attached `line-ocr` receipt instead shows explicitly selected full lines,
their labels and each recognizer's output. It never presents manual regions as
automatic whole-image coverage. Cyrillic-capable Tesseract and Latin/digit CRNN
readings remain distinct; the page does not select or silently correct a result.

The 16 November private trial report and acceptance limits are linked from the
current handoff. Automated request tests and static coordinate figures passed;
the user confirmed the requested browser word highlight after automation failed.
This acceptance is specific to that hit, not all OCR text or all browser flows.

The retained 16-case `web selection prepare --plan PLAN` command prepares only
that explicit selection. Image decoding uses the current interpreter (the
`images` extra), or an explicit `--image-python /path/to/qualified/python`.
It has no hard-coded workstation interpreter and does not run during HTTP requests.

## Authority and limits

`records/evidence.json` and `records/assertions.jsonl` are authoritative readable
research records. History is scoped by case and retains the nine existing
synthetic revisions, followed by 16 initial unreviewed real observations.
`sample/` contains invented PDFs and PNGs, visibly labelled synthetic. Real
previews live in `cache/`; their exact source bindings point into the declared
archive and protected Elements roots. Original media are not copied or changed.
Every real case and view records private local use and prohibits external redistribution.
Page sequence, printed page, figure, region, source binding and lineage remain
distinct. The running demonstration retains its three original views; isolated
acceptance also exercises rotated prepared PDFs without changing those records.

## Inspect a citation

Open a source card, an assertion's **Reason and citations**, or the history's
**Revision evidence and scope**. Each citation opens its prepared page with an
amber outline for the recorded normalized region. Coordinates are x/y/width/
height from the top-left of the unrotated image, scaled with the image at any
viewport width. The outline identifies a recorded region, not a recognition
result. Prepared images may reflect clockwise PDF rotation of 0/90/180/270
degrees; the viewer maps the unrotated coordinates accordingly. Empty regions
and nonorthogonal rotations are refused. Preparation must supply the correct
PDF rotation and a matching rendered preview; requests do not infer it.

The viewer separates sequence page, printed label, figure/passage and source
identity. **Open source PDF at page …** serves the bound PDF with a `#page=N`
fragment; the browser's PDF viewer determines fragment handling. **Open prepared
image at full size** serves the existing PNG. No rendering, OCR or dependency
installation runs inside requests. Source access requires the same login/staff
check as comparison and is restricted to the exact bound file. Source views
support PDF/PNG/JPEG; readable TIFF originals are downloads. The declared
unreadable TIFF is withheld without substituting either separate JPEG view.

Generated citation, image and source links carry the displayed record revision.
A later change withholds the old link instead of silently displaying a new
snapshot; reload the comparison to get current links. Canonical URLs without a
revision open the current snapshot. History links explicitly resolve current
bound evidence; the pilot does not store past source-file versions. The unified workspace
switches bound evidence and text in place, retaining the draft. Full-source and
standalone citation links open another tab. Citation browsing
does not alter evidence, history or exports.

Optional OCR is displayed separately from manual observations and assertions.
The explicit CLI command below runs an existing Tesseract executable on the
bound prepared image and writes `cache/<citation>-ocr.json`. Missing/failed OCR
leaves the image and manual citation usable. A receipt is withheld if its source,
preview, page or rotation binding no longer matches. Regeneration changes no
record revision, citation locator or reviewed text. A machine configuration may
select `tesseract`; the default is the existing executable on PATH. No processor
is installed automatically. OCR is unreviewed machine text, with no accuracy claim.

An optional `external_reference` unavailable note in evidence is displayed and
exported as such; no external fetch or silent fallback is performed. A case with
`read_only = true` in its JSON evidence permits browsing/export and refuses both
form and direct review saves. There is no new workflow/role or completion button.

## Record and projection authority

`pilot.sqlite3` contains one rebuildable `ResearchCase` projection table plus
Django account/session support tables. The projection table uses `--run-syncdb`;
only built-in apps use migrations. There is no custom migration history or
automatic schema upgrade. A later projection schema change needs an explicit
recreation step; ordinary rebuild only refreshes rows and preserves accounts.

The shared file lock captures evidence and assertion bytes together. The same
captured bytes produce the revision token. Stale projections withhold browsing
until rebuilt. Missing or changed source/preview bindings withhold that image;
there is no fallback. Relative paths remain inside their declared root. External
roots are accepted only in local-review mode with the exact machine mappings;
protected-media access first requires the exact device, mount and read-only
filesystem options enforced by the deployment guard. Keep those values in private
operator configuration; generic examples do not override the guard.
SQLite never becomes the authority for research assertions.

Each comparison includes the review form for its existing assertion, beside the
source and selected evidence. It allows finding wording, scope, uncertainty,
outcome, supporting citations and an optional reviewer note. Blank notes remain
empty in the readable history; the application does not invent a reason. Subject/relation keys remain fixed; reviewer and UTC time
come from the server. CSRF-protected saves require the displayed revision and
unchanged source/preview bindings. Earlier JSONL bytes are retained; a new record
names its superseded revision. A stale form is refused with entered text intact.

A successful workspace save computes its destination from the case queue and selected finding.
A single review updates the changed SQLite case payload and stamps the other
cases with the shared revision under the research lock. Search-index publication
takes place on demand as described above.

If the readable decision is saved and a projection fails, the response identifies which
projection needs attention. **Rebuild browsing** recovers a stale projection from the current
history. A missing or invalid base search index first requires restoration or regeneration.
If storage fails after an attempted replacement, the writer checks the exact history bytes.
A matching new history is reported as saved. When the check cannot establish the outcome,
the operator is asked to inspect the finding history before taking further action.

Generic research admin add/change/delete remain disabled, including for the local
superuser. Built-in account administration remains available to that user.
All research records retain `metadata_adopted=false`. The original 16-case load is
complete. Explicitly selected research-baseline additions use the preparation
operation below; arbitrary imports and production deployment remain outside scope. No real case received an automated review.

For the synthetic demonstration, **Download unadopted research JSON** exports evidence, full revision history and
current assertions together. It explicitly omits source media, credentials and
the browsing database; it is not a portable visual demonstration or an accession
input. Export requires current projection and source bindings. The CLI export
below writes the same format to `exports/synthetic-research.json`, replacing the
previous explicit export. Select `--case photo` in the mixed runtime. Generic
mixed/real export and portable packaging are refused, keeping real material out
of the synthetic bundle. No automatic exports or jobs run.

## Prepared local selection

An explicitly requested research baseline can append 1–8 named cases through
`web selection prepare-additions --plan PLAN`, then
`web selection apply-additions`. Preparation records the current research revision;
application refuses changed reviews, duplicate case keys and invalid citations
before replacing either readable authority file. Existing case data and history
are retained; all new findings start unreviewed. Each finding supplies its own
relationship, evidence citations, rationale and uncertainty. This extends the
existing preparation operation; it does not discover or import arbitrary corpora.

Image views may select a normalized `region_xywh` with `preview_crop=true` and
an explicit `preview_clockwise` rotation. The prepared crop retains its original
source binding, full-source link and source coordinates. Text shown beside it is
labelled page context; a crop does not establish caption association. Source
orientation requiring a separate mapping is refused. Crops are research working
views and never new archival derivatives or Master candidates.

Every real case has an optional **Physical museum object** section. Its separate
form records an existing inventory/object reference, catalogue URL, last recorded
location, source/person, date and relationship to the depicted object. All object
fields may remain empty; blank means no object is recorded, not proof of loss or
survival. The relationship distinguishes a type match from the same specimen.

Saving or clearing a reference appends a correction to the case's existing
assessment history, with its own author/date/reason. It preserves the assertion
wording, research outcome and reviewer identity. A later research review retains
the reference. Stale forms, read-only cases, login/staff/CSRF protection and
saved-but-stale recovery use the existing contracts. No new inventory authority,
database/table or files are introduced. Physical location notes do not require
the source media to be online; they do not change source bindings or catalogue
metadata. Reference fields start blank until supplied by an operator.

`load_selection.py` prepares the exact existing P01–P08, D01–D04 and L01–L04
selection from a private plan. The one-time apply step has already completed;
it refuses reloading those cases so saved reviews cannot be overwritten.
Prepared receipts and source details remain private in the runtime cache.
Preparation uses existing Poppler and workstation Pillow, outside HTTP requests.
The application gains no dependency or additional database. Machine source roots
use the `archive` and `elements` keys under `[source_roots]` and must match the
exact deployment allowlist. Consult local operator instructions for those paths;
a fresh clone does not include a configured or authorized real-evidence runtime.

This is local inspection, not a frozen benchmark or permission to redistribute.
Case review outcomes, missing report sections and independent human acceptance
remain open. PDF sequence pages and printed labels remain distinct; real regions
currently describe whole pages/images, not inferred figure crops.

## Rebuild and verify

With the environment variables above:

```sh
archive-research web rebuild
archive-research web export --case photo
archive-research web ocr --view report-figure
archive-research web storage-check
archive-research web test -v1
```

Storage checks exercise cross-process lock exclusion, atomic replacement and
SQLite rollback on the actual runtime filesystem. Application tests use isolated
synthetic files and a test database. Private browser screenshots and receipts
live in `cache/`; they are not catalogue evidence. Restart does not reset records.

## Portable synthetic package

In a synthetic-only runtime, `archive-research web package` writes `exports/synthetic-pilot.tar.gz` containing
the application, pinned requirements, invented sample files, readable history
and standalone setup instructions. Credentials, machine configuration, database,
venv and caches are omitted. GNU tar extraction preserves PAX nanosecond sample
timestamps; the receiving operator runs `archive-research web configure` to create new private
configuration and secrets, then `archive-research web init` to rebuild SQLite and create a
fresh account. The existing package is the prior synthetic acceptance snapshot;
it has not been regenerated from the mixed runtime. Configuration refuses secret overwrite and protected/symlinked
roots. Settings require explicit private configuration beside its selected
runtime, without a workstation-specific Python path edit.

Local relocation, new-account setup, actual OCR regeneration and the complete
review/correction/export/restart flow were verified in an isolated cache runtime.
This is technical portability evidence on one workstation, not a second human's
usability result or verification on different hardware. Real sample clearance,
curator outcomes and independent operator/machine acceptance remain required.
