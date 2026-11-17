# Local WF research pilot

The approved pilot provides local login and 16 real photograph, report and
relationship review cases, including the published book and patent material.
The homepage shows real cases; the synthetic demonstration has a separate link.
There are 52 source views: 51 prepared images and one explicitly unreadable TIFF.
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
bound evidence; the pilot does not store past source-file versions. Review forms
open citation evidence in another tab to retain unsaved text. Citation browsing
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

Each comparison links to review forms for its own existing assertions. They
allow assertion wording, scope, uncertainty, outcome, supporting citations and
a mandatory reason. Subject/relation keys remain fixed; reviewer and UTC time
come from the server. CSRF-protected saves require the displayed revision and
unchanged source/preview bindings. Earlier JSONL bytes are retained; a new record
names its superseded revision. A stale form is refused with entered text intact.

Save and rebuild hold the same file lock through projection refresh. If history
was saved but projection refresh failed, the UI explicitly says it was saved and
offers **Rebuild browsing**. Do not resubmit the review. Recovery checks the
current readable revision and never writes another decision. A failed write
before replacement preserves the existing history. If replacement succeeded but
storage confirmation failed, the UI also reports saved and requires rebuilding.

Generic research admin add/change/delete remain disabled, including for the local
superuser. Built-in account administration remains available to that user.
All research records retain `metadata_adopted=false`. The bounded 16-case load is
complete; arbitrary imports, new assertions/relations and production deployment
remain outside the current scope. No real case received an automated review.

For the synthetic demonstration, **Download unadopted research JSON** exports evidence, full revision history and
current assertions together. It explicitly omits source media, credentials and
the browsing database; it is not a portable visual demonstration or an accession
input. Export requires current projection and source bindings. The CLI export
below writes the same format to `exports/synthetic-research.json`, replacing the
previous explicit export. Select `--case photo` in the mixed runtime. Generic
mixed/real export and portable packaging are refused, keeping real material out
of the synthetic bundle. No automatic exports or jobs run.

## Prepared local selection

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
