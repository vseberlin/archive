# WF research pilot: approved exception and local review

**Current runtime location after the approved home consolidation:**
`/home/vladimir/archive-local/research-pilot`. Source is now
`/home/vladimir/archive-local/source/Archive-tools/research_web`. Use the current
[operating instructions](../../Archive-tools/research_web/README.md) for commands
and configuration. Operational paths below reflect the approved relocation;
the old home paths and compatibility links are retired. Dated results remain historical. Readable records, source
bindings and accounts were retained; the environment was recreated at its final
path. The private handoff records current process ownership and verification.

15 November 2026. Prepared under the authorised next consolidation slice.
**Approved on 15 November 2026:** the user approved the bounded architecture
exception and first synthetic setup slice ("yes approved go"). Root `AGENTS.md`
and the workflow ADR now record that exception.
It makes stage 2 of the [consolidation plan](archive-django-consolidation-plan.md)
reviewable. The private selection packet is referenced from the current archive
handoff; source paths, rights records and observations stay outside Git.

## Current delivery: real local inspection

The subsequent requested generalization adds an optional physical-museum-object
reference to every real case, using its existing assessment correction history.
Inventory reference/link, last recorded location, source/person/date and
type-versus-specimen relationship can all remain empty. Its independent save
preserves research outcome and reviewer fields, records the operator and reason,
and supports corrections/clearing without losing earlier values. This is a
research reference to the museum inventory, not another inventory or location
authority. No locations or survival/identity claims are prefilled.

The user's correction that only a test case was visible is addressed by loading
the existing 16 selected evidence cases (P01–P08, D01–D04, L01–L04) into the same
application. The homepage defaults to real cases, with a separate synthetic
demonstration link. Fifty-one prepared previews cover 52 source views; P07 keeps
its unreadable TIFF explicit alongside two separately bound readable JPEGs.
Book/photo/report and report/patent links retain their uncertainty and lineage.
All 16 initial observations are unreviewed, without invented curator identity or
review time. The prior nine synthetic history revisions remain unchanged.

The project uses explicit `local_review` mode and exact declared source roots.
Original archive and USB media stay read-only; prepared PNGs stay in the existing
cache. Every real case/view records private-local-use authority and prohibits
external redistribution. Source checks enforce exact size/mtime, root containment
and the existing protected Elements mount guard. No new schema, dependency,
background job, catalogue adoption or archive mutation was introduced.

The real selection is excluded from export and synthetic portable packaging.
The existing package remains the prior synthetic-only checkpoint. All 42
application tests pass, including case-scoped review/history, exact source-root
guards, local-use records, explicit unavailable views and export separation.
This delivery makes real evidence available for review; curator-confirmed
outcomes, external sharing, fixture freeze and independent human/machine
acceptance remain outstanding. Historical slice descriptions below record their
state at each checkpoint and do not supersede this current delivery.

## Result of the readiness inspection

The 24-case specification now has a concrete candidate list: eight photograph
cases, four report cases, four relationship cases, four access failures and four
correction/recovery exercises. These are review tasks, not 24 independent
holdings or a recognition benchmark. Cases intentionally share evidence. One
photo case uses an existing three-candidate prepared comparison solely to test
interface semantics; it is outside the WF thematic relationship. Its member
bindings now match the mounted sources, but the source TIFF remains unreadable
with the inspected decoder. The JPEG fallback and existing archive access image
show the same exposure; this does not certify the unreadable TIFF's content.
One inspected local photograph is a reserve.

The private packet now includes source-bound book/photo/report comparisons and
a visually verified brochure reproduction. One patent and its related report
excerpt have been visually inspected: matching researcher names and technical
subject support a contextual link, but explicit patent citation and implementation
remain unresolved. The other three patent leads remain uninspected. Report
excerpts retain PDF/printed-page offsets and explicit missing-section locators.

The exact protected Elements mount is now present and read-only. Inspected patent
size/mtime match the index; its stored hash was reused without a new content hash.
The existing photo workbench remains stopped. The approved synthetic pilot now
runs separately on loopback port 8785.
The existing 1,083 decision records contain no saved metadata proposals, so
preserved reviewer wording and correction-history exercises must use explicitly
synthetic fixtures. They cannot be described as recovered human decisions.

Stored resource records contain image and PDF reuse labels, separately from
metadata rights. Licence versions, complete attribution, patent permissions and
permission for external redistribution remain unverified. Local inspection under
this task is not permission to publish a bundle. The selected PDFs are excerpts:
a catalogue's full-report page count must not become a claim of digital coverage.

**The candidate list is not frozen or benchmark-ready.** Fifteen evidence cases
have material for direct curator review; the remaining photo case supports only
an explicitly partial comparison and unreadable-source exercise. Eight synthetic
access/correction exercises now have bounded technical execution receipts; these
are separate from curator answers and real-data acceptance.
The private worksheet gives each case a review question and evidence locator;
no curator answers are prefilled. It also records a resource-description versus
visible-reverse discrepancy without changing catalogue metadata.

Before freezing, have a curator confirm the bounded expected outcomes, settle
per-resource intended-use permissions and fixture coverage, and verify any
additional sources actually selected. Missing report sections limit relationship
claims; they do not prevent review of the evidence already present. Synthetic
failure/recovery fixtures remain labelled as such. Preparation and architecture
review can proceed meanwhile. The current private handoff links the maintained
selection packet, evidence notes and curator worksheet.

## Approved exception to the root instructions

The following bounded exception is recorded in `AGENTS.md` and referenced by
the workflow ADR. General restrictions remain in force.

> For the isolated WF research pilot described in
> `docs/architecture/archive-research-pilot-scope.md`, permit Django's ORM,
> built-in admin and authentication, the three dependencies pinned in that scope,
> one pilot SQLite database, and its exact source/runtime directories.
> Permit read-only browsing projections and bounded citation/relationship review
> with compatible correction records and explicit project/root configuration.
> Research assertions and all their corrections remain human-readable files;
> SQLite is a rebuildable research projection plus disposable operator/session
> state. Acceptance of a research assertion never adopts catalogue metadata.
> Initially implement and demonstrate with synthetic fixtures. Real cases may be
> loaded only after their access, bindings and intended-use permissions are
> recorded. Existing archive/source protections and exactly-one-Master rules
> remain unchanged. No concurrent old/new writer may share a live review root.
> This exception permits one manually launched loopback development process for
> the pilot. It does not permit a daemon, public deployment, processing queue,
> new role hierarchy, general metadata manager, automated research, accession,
> source/archive mutation, publication or deletion.

This exception is necessary because the present root instructions explicitly
prohibit ORMs and a general management UI and require approval for new
dependencies, databases, directories and services. The limited admin is a
research browser; generic add/delete/bulk edit and model-field saves are disabled
for projected archival/research records. Bounded review writes use shared
locked operations; generic model saves remain disabled. Built-in account administration is restricted to the local
pilot operator; it conveys no archival authority.

## Approved concrete setup

The source and runtime paths below reflect the approved first slice and its
subsequently approved home consolidation.
The isolated runtime root is
an explicit, narrow write exception outside the current work/archive roots, chosen
to keep the environment and SQLite on the inspected `/home` ext4 filesystem.
The current navigation home is `/home/vladimir/archive-local`; retained earlier
setup material remains on the NTFS/FUSE volume.

| Purpose | Approved location or setting |
| --- | --- |
| Maintained application source | `/home/vladimir/archive-local/source/Archive-tools/research_web/` |
| Template directories | `research_web/templates/research_web/` beneath that source location |
| Shared operations and tests | Existing `Archive-tools/` directory; extract only the first real shared consumer boundary |
| Private runtime root | `/home/vladimir/archive-local/research-pilot/` |
| Virtual environment | `/home/vladimir/archive-local/research-pilot/.venv/` and its package-managed descendants |
| Authoritative isolated research files | `/home/vladimir/archive-local/research-pilot/records/` |
| Explicitly permitted sample copies | `/home/vladimir/archive-local/research-pilot/sample/` |
| Disposable viewing/processing cache | `/home/vladimir/archive-local/research-pilot/cache/` |
| Explicit unadopted research exports | `/home/vladimir/archive-local/research-pilot/exports/` |
| One SQLite database | `/home/vladimir/archive-local/research-pilot/pilot.sqlite3` |
| Project configuration | `/home/vladimir/archive-local/research-pilot/project.toml` |
| Machine configuration and local secret | `/home/vladimir/archive-local/research-pilot/machine.toml`, mode 0600; never Git |
| Operator and network | Vladimir; built-in Django login; loopback `127.0.0.1:8785` only |

Permit only these new project directories; environment-managed descendants are
the dependency installation exception, not permission to add application layers.
Do not place runtime files in the source repository or introduce a second source
checkout. The hub README links to canonical source and docs; no further
workspace relocation or second plan is authorised here.

Installed dependency pins, rechecked against official package metadata on
15 November 2026:

```text
Django==5.2.19
asgiref==3.13.0
sqlparse==0.6.1
```

Django 5.2 LTS is selected for this bounded pilot. Package metadata accepts the
observed Python 3.13.5; system SQLite is 3.46.1. `venv`, `ensurepip`, `fcntl`
and Poppler are present. None of the three packages is installed in system
Python. Existing Pillow 11.1.0 and jsonschema 4.19.2 observations do not imply
availability in the isolated environment. The framework runs with these three
dependencies; add no optional OCR/embedding processor. If a later
shared comparison needs Pillow/jsonschema in the new environment, propose and
pin that small addition before installing it rather than enabling system-site
packages or silently reusing the incomplete old workbench environment.

The isolated environment now contains exactly these three application packages;
`pip check` and Django system checks pass; the subsequent synthetic review
slice has 23 passing application tests. No optional
OCR/embedding dependency was added. This is not a certification of the system
Python patch level.

Implemented and tested launch contract:

```sh
ARCHIVE_RESEARCH_PROJECT=/home/vladimir/archive-local/research-pilot/project.toml \
ARCHIVE_RESEARCH_MACHINE=/home/vladimir/archive-local/research-pilot/machine.toml \
PYTHONPATH=/home/vladimir/archive-local/source/src:/home/vladimir/archive-local/source/Archive-tools \
/home/vladimir/archive-local/research-pilot/.venv/bin/python -B \
  /home/vladimir/archive-local/source/Archive-tools/research_web/manage.py \
  runserver 127.0.0.1:8785 --noreload
```

This is the current launch contract, not a claim that the pilot is running.
Read the sole private handoff and check that the port is free before starting.
Use one manually owned process, explicit allowed hosts, CSRF protection and
authentication. Keep credentials and signing material in the private runtime
root. No systemd unit, container, reverse proxy or startup automation is proposed.

Setup checks exercised cross-process lock exclusion, atomic replacement and
SQLite rollback on the runtime filesystem. Login, loopback binding and an owned
process stop/restart were verified. Browser acceptance covered three loaded
images, citation/history display, read-only research administration, logout and
a 390-pixel layout. Connected browser controls failed before navigation; existing
local Chrome/Playwright ran in a separate headless profile. Physical-device
acceptance was not performed. The existing workbench remains untouched.

## Minimal data and write boundary

Reuse the [synthetic example](archive-research-record-example.md) to specify the
first implementation. Its selected JSON blocks seed invented fixtures; this is
not a supported general import format.

- Reuse qualified unit/object keys, existing candidate keys and representation
  bindings. Citation/claim keys are local to one project and item, not a global
  entity registry. Keep page sequence, printed label, figure/region and original
  representation binding distinct. Observe PDF rotation when mapping normalized
  regions when real cases are introduced; the real candidate sample contains
  rotated pages. The current synthetic fixture uses rotation zero only.
- One readable evidence file describes selected items, representations, citation
  locators and source lineage. One readable assertion-history file retains the
  complete scoped review revisions and correction reasons. The first slice fixes
  `records/evidence.json` and `records/assertions.jsonl` with bounded synthetic
  validation; it does not import this planning packet. Existing `decisions.jsonl` is not extended
  or populated by the pilot; Master decisions stay read-only initially.
- Store only cases/evidence/assertion browsing projections plus the required
  built-in authentication/session tables in the single SQLite database. Avoid
  one table for every conceptual noun. Rebuild archival/research rows from files;
  recreate the operator account separately when rebuilding an empty database.
  An account/session is operational state, not an archival decision.
- Under the existing lock pattern: reread authoritative records, check submitted
  revision and source bindings, validate, replace the readable decision file,
  then refresh the index. A failed projection refresh reports saved-but-stale.
  Withhold stale indexed results until rebuilt; never repeat the decision write.
- Keep project root names and relative paths portable; machine root mappings,
  executables and secrets remain in machine configuration. Original provenance
  survives remapping. Moving files cannot waive binding or source-mount checks.
- Display accepted, rejected and unresolved assertions separately. Every review
  and export remains `metadata_adopted=false`. No export enters the trusted
  reference corpus, catalogue or accession workflow automatically.

## Delivered first slice and next boundary

The narrow exception is applied and the approved skeleton/environment are in
place. One synthetic browse/login/read-only comparison path works in the browser.
`archive_common.content_revision` is shared with the old workbench and hashes
exact captured bytes; the existing lock/read helpers are reused. All 58 old
workbench/import regression tests pass.

Authoritative files are `records/evidence.json` and `records/assertions.jsonl`;
the latter starts with seven invented revisions and retains subsequent reviews.
One SQLite `ResearchCase` table is a rebuildable projection alongside built-in
account/session tables. Research admin add/change/delete remain disabled even
for the local superuser; bounded review forms are the only research write UI. Fixtures are visibly synthetic, unrotated, and remain unadopted.
Missing/changed media are withheld; stale projections are withheld until rebuild.
See the [operating instructions](../../Archive-tools/research_web/README.md).

The user's next "ok next" approved the bounded synthetic review/correction/
rebuild/export slice. It is now implemented in the same directories and database
without new dependencies or schema changes. Forms edit only the three existing
assertions, require a reason, preserve prior JSONL bytes and append a superseding
revision with authenticated actor and UTC time. Subject/relation keys are fixed.
All saves check the submitted snapshot token and all source/preview bindings
under the same lock that serializes projection rebuilds.

A failure after saving history explicitly reports saved-but-stale and offers a
revision-checked rebuild; it never repeats the decision write. Stale forms retain
entered text without saving. A JSON download or explicit CLI export includes
readable evidence, all corrections and current accepted/rejected/unresolved
assertions, while declaring source-media omissions and `metadata_adopted=false`.
Application tests cover pre-write failure, post-replacement confirmation failure,
projection failure/recovery, stale edits, source changes, permissions/CSRF,
lock exclusion through projection and export/rebuild agreement.

Introduce the real sample only when selection/permissions gaps above are closed.
The full 24-case list remains unfrozen; implementing these bounded behaviors is
not acceptance of all planned access/correction exercises.

The subsequent citation-navigation slice makes the existing citation keys
clickable from source cards, assertions, history and review forms. A viewer
highlights the recorded region on the prepared image and separates sequence,
printed page, figure/passage and source identity. Protected source links serve
only bound synthetic PDF/PNG files; PDFs carry a one-based page fragment whose
handling depends on the browser viewer. Generated links check the displayed
snapshot revision, so an older page cannot silently load newer evidence. History
links explicitly open current bound evidence, not stored historical source
versions. Review links open another tab and retain unsaved form text.

All 30 application tests pass, including source-read authentication, stale-link
refusal, changed/missing bindings, unknown/escaping paths, supported formats and
positive unrotated regions. This slice changes no readable research records,
source fixtures, SQLite schema or dependencies. PDF rotation and regenerated OCR were not part of that navigation-only
checkpoint; the subsequent end-to-end acceptance below covers them.

## Sources for the pinned environment

- [Django downloads and LTS support](https://www.djangoproject.com/download/).
- [Django 5.2 Python compatibility](https://docs.djangoproject.com/en/5.2/faq/install/).
- [Django 5.2 SQLite requirements](https://docs.djangoproject.com/en/5.2/ref/databases/#sqlite-notes).
- Official release metadata: [Django 5.2.19](https://pypi.org/pypi/Django/5.2.19/json),
  [asgiref 3.13.0](https://pypi.org/pypi/asgiref/3.13.0/json),
  [sqlparse 0.6.1](https://pypi.org/pypi/sqlparse/0.6.1/json).

The private inspection packet records actual host measurements and the inspected
patent's page access. Framework execution is now verified with synthetic fixtures;
full report coverage, curator approval and external reuse clearance remain open.

## End-to-end technical acceptance — 15 November 2026

The user requested continuation without stopping between slices. The same
approved synthetic runtime now supports normalized citation mapping for clockwise
0/90/180/270-degree prepared PDF pages, optional bound OCR receipts, explicit
unavailable external-reference notes, and read-only cases. No new dependency,
review authority, role, database schema or background process was introduced.

OCR is an explicit CLI operation using an existing Tesseract executable. Missing
or failed processing preserves usable manual citations. Successful regeneration
only replaces derived cache; evidence, revision tokens, citations and reviewer
history remain unchanged. Stale receipts are withheld. The application does not
perform network reference retrieval or PDF/OCR processing inside requests.

The explicit synthetic package includes source, pinned requirements, invented
media and readable records. Secrets, accounts, SQLite and caches are omitted.
Fresh configuration selects a private unprotected root beside its project file;
there is no workstation-specific root literal in Python. GNU tar/PAX preserves
source nanosecond timestamps. A local isolated extraction reproduced the initial
export byte-for-byte, created a fresh operator and rebuilt its projection.

**36 application tests passed.** A browser run in that isolated copy checked a
90-degree PDF region, absent OCR, two actual OCR regenerations with stable
citations/history, an injected unavailable external reference, review and
correction, all three review outcomes, unadopted export and a read-only save
refusal. Its restart retained 11 revisions and the exact export. The original
runtime retains its nine revisions and original source/evidence bytes. Browser
screenshots include desktop and 390-pixel layouts; physical-device and native
PDF viewer fragment behavior remain unverified.

| Existing exercise | Executed technical evidence | Limit |
| --- | --- | --- |
| A01 protected mount absent/wrong | Shared guard rejects absent, wrong-device and writable mounts before stat; synthetic application refuses protected paths outright | No real protected-source importer enabled |
| A02 changed source | Source/preview binding failures withhold citations and saves | No original file changed |
| A03 external reference unavailable | Injected unavailable note remains visible and exported beside local evidence | No external retrieval or image-inspection claim |
| A04 OCR absent | Missing executable keeps manual citation usable; actual regeneration twice preserves records | OCR is unreviewed, no accuracy benchmark |
| C01 stale edit | Old form rejected without losing entered wording or newer record | Isolated synthetic writes |
| C02 correction retained | Prior bytes/history retained across correction and OCR regeneration | No real historical reviewer proposals or new model importer |
| C03 projection failure | Saved-but-stale reported; rebuilding does not repeat the save | Forced failure test, not a production incident |
| C04 restart/rebuild/export | Fresh-root recovery and restarted browser export agree, with accepted/rejected/unresolved preserved | Same hardware, automated operator |

This completes local synthetic technical acceptance. The 24-case real sample is
not frozen, and no curator answers, intended-use permissions, independent human
usability results or second-machine acceptance have been invented. Those require
the named reviewer/resource decisions in the private worksheet. No migration,
old-writer retirement, public deployment or real-media bundle is authorized by
these technical results. The private handoff links exact receipts and package.
