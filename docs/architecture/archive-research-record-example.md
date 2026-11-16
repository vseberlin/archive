# Synthetic mixed-source research example

14 November 2026. Design evidence for the
[consolidation plan](archive-django-consolidation-plan.md), not an implemented
schema or a statement about WF holdings. All names, descriptions, locators and
times below are invented. No real files, catalogue records or decisions are
created by this example. The current archive instructions still govern.

## Scope and existing-format mapping

A photograph appears to depict apparatus of type B. A report page illustrates
that type and describes a test. A patent describes a potentially related
invention. The reviewer accepts a type-level connection, rejects an alternative
type, and leaves the patent connection unresolved. A later correction narrows
the accepted assertion to the B family, with the variant unknown.

| Concept | Existing representation | Missing capability, proposed only |
| --- | --- | --- |
| Photograph and digital candidate | Prepared `object_id`, `candidate_id`, member path/size/mtime; existing archive keys remain qualified by unit | None for this photograph |
| Machine suggestion and evidence | `model-proposals.json`, result/reference checks, source stamps and receipts | Document identity separate from representation/page; durable locator |
| Human-edited metadata proposal | `decisions.jsonl` → `metadata_proposal.fields`, actor, saved time, source binding, `metadata_adopted=false` | Typed, directed relationship review rather than prose in a title or note |
| Master/same-photo decision | Existing selected/redundant/same/different fields | No change; accepting a type connection never changes image identity |
| Correction | Current metadata saves replace current proposal fields | Preserve prior values/reason and explicit supersession for scoped research reviews |
| Browsing/export | Existing readable records and rebuildable projections | Explicit research export including evidence, correction chain and unadopted status |

Do not insert the following records into the current workbench files: their
validators do not implement these extensions. Names such as `report-C` and
`photo-type` are local labels in this document, not newly assigned archive IDs.
An implementation would need an explicitly approved local key for each claim
and citation, scoped to its project and existing item key. It would preserve
those keys through correction/export; it would not establish a global entity
registry. Existing report/patent catalogue keys must be reused where present.

## Evidence before review

`photo` and `full` reuse the existing synthetic workbench fixture's object and
candidate keys. The other local labels are demonstrative. `fixture:*` bindings
below stand for content bindings to be supplied by actual isolated fixtures;
they are not hashes or usable runtime source stamps. A real record must retain
its original source path and checked size/mtime (and an existing hash when
available). A changed binding cannot be accepted merely because its path matches.

```json
{
  "synthetic": true,
  "items": [
    {"key": "photo", "kind": "photograph", "description": "Apparatus on a bench; no legible serial number"},
    {"key": "report-C", "kind": "report", "description": "Illustrated test report"},
    {"key": "patent-D", "kind": "patent", "description": "A potentially related circuit invention"}
  ],
  "representations": [
    {"item_key": "photo", "candidate_id": "full", "root": "sample", "path": "photo.tiff", "binding": "fixture:photo-v1"},
    {"item_key": "report-C", "root": "sample", "path": "report.pdf", "binding": "fixture:report-v1"},
    {"item_key": "patent-D", "root": "sample", "path": "patent.pdf", "binding": "fixture:patent-v1"}
  ],
  "citations": [
    {"key": "photo-view", "item_key": "photo", "representation_binding": "fixture:photo-v1", "page_sequence": 1, "printed_page_label": null, "region_xywh": [0.1, 0.1, 0.8, 0.8], "observation": "Three controls in a row; no legible maker or serial number"},
    {"key": "report-figure", "item_key": "report-C", "representation_binding": "fixture:report-v1", "page_sequence": 14, "printed_page_label": "12", "figure": "3", "region_xywh": [0.1, 0.2, 0.7, 0.5], "transcription": "Type B apparatus, test arrangement", "transcription_method": "manual, synthetic"},
    {"key": "patent-passage", "item_key": "patent-D", "representation_binding": "fixture:patent-v1", "page_sequence": 2, "printed_page_label": "2", "passage_label": "claim 1", "region_xywh": [0.1, 0.3, 0.8, 0.2], "transcription": "Circuit arrangement with three controls", "transcription_method": "manual, synthetic"}
  ],
  "source_records": [
    {"key": "report-local", "describes": "report-C", "origin": "local report", "derived_from": null},
    {"key": "report-republication", "describes": "report-C", "origin": "catalogue republication", "derived_from": "report-local"}
  ]
}
```

Page sequence is one-based in the bound representation; printed labels are
independent. Regions use normalized x/y/width/height on the unrotated page.
The page/figure/region still resolves if OCR is regenerated. A new PDF ordering
or image content requires a new checked binding and explicit citation mapping.
An unavailable file produces an unavailable citation, not a silent fallback.
The two source records above are one evidence lineage, not two confirmations.

The three directed assertions before review are:

```json
[
  {"key": "photo-type", "revision": 0, "subject": "photo", "relation": "depicts_type", "value": "Type B", "scope": "product type, not individual apparatus", "citation_keys": ["photo-view", "report-figure"], "uncertainty": "tentative visual correspondence", "review": "unreviewed", "reviewer": null, "reviewed_at": null, "supersedes_revision": null, "reason": "Candidate from comparison", "metadata_adopted": false},
  {"key": "alternative-type", "revision": 0, "subject": "photo", "relation": "depicts_type", "value": "Type X", "scope": "product type, not individual apparatus", "citation_keys": ["photo-view", "report-figure"], "uncertainty": "alternative hypothesis", "review": "unreviewed", "reviewer": null, "reviewed_at": null, "supersedes_revision": null, "reason": "Alternative candidate", "metadata_adopted": false},
  {"key": "type-patent", "revision": 0, "subject": "Type B", "relation": "possibly_implements_invention_described_by", "value": "patent-D", "scope": "technical relation, not proof of manufacture or use", "citation_keys": ["report-figure", "patent-passage"], "uncertainty": "documentary link missing", "review": "unreviewed", "reviewer": null, "reviewed_at": null, "supersedes_revision": null, "reason": "Similar arrangement only", "metadata_adopted": false}
]
```

## After review

The complete first human revisions are below. The revision-0 records remain
evidence, but are no longer the current review for these claim keys. This local
revision number illustrates correction linkage; it does not replace the
workbench's input/decision/sidecar revision token or source-binding validation.

```json
[
  {"key": "photo-type", "revision": 1, "subject": "photo", "relation": "depicts_type", "value": "Type B", "scope": "product type, not individual apparatus", "citation_keys": ["photo-view", "report-figure"], "uncertainty": "serial number and individual identity unresolved", "review": "accepted", "reviewer": "Synthetic reviewer", "reviewed_at": "2026-11-14T12:00:00Z", "supersedes_revision": 0, "reason": "Control layout agrees with the report figure", "metadata_adopted": false},
  {"key": "alternative-type", "revision": 1, "subject": "photo", "relation": "depicts_type", "value": "Type X", "scope": "product type, not individual apparatus", "citation_keys": ["photo-view", "report-figure"], "uncertainty": "rejected only for these bound views", "review": "rejected", "reviewer": "Synthetic reviewer", "reviewed_at": "2026-11-14T12:01:00Z", "supersedes_revision": 0, "reason": "Type X's proposed control layout conflicts with the photographed layout", "metadata_adopted": false},
  {"key": "type-patent", "revision": 1, "subject": "Type B", "relation": "possibly_implements_invention_described_by", "value": "patent-D", "scope": "technical relation, not proof of manufacture or use", "citation_keys": ["report-figure", "patent-passage"], "uncertainty": "documentary link missing", "review": "unresolved", "reviewer": "Synthetic reviewer", "reviewed_at": "2026-11-14T12:02:00Z", "supersedes_revision": 0, "reason": "The report does not cite the patent; similar controls are insufficient", "metadata_adopted": false}
]
```

Acceptance endorses only the stated assertion. It does not prove that the
photograph shows the reported test event, identify a physical apparatus, assign
a manufacturer, or establish a photograph date. Rejection is also scoped: it
does not prohibit Type X in other photographs or with changed evidence.

## After correction

A closer reading shows that figure 3 describes the B family but does not
distinguish its variants. Only `photo-type` changes; the other revision-1
records remain current. Preserve the earlier wording and its reason, and add:

```json
{
  "key": "photo-type",
  "revision": 2,
  "subject": "photo",
  "relation": "depicts_type",
  "value": "B family; variant unresolved",
  "scope": "product family, not exact variant or individual apparatus",
  "citation_keys": ["photo-view", "report-figure"],
  "uncertainty": "variant, serial number and individual identity unresolved",
  "review": "accepted",
  "reviewer": "Synthetic reviewer",
  "reviewed_at": "2026-11-14T12:10:00Z",
  "supersedes_revision": 1,
  "reason": "Figure 3 supports the family only; earlier wording was too specific",
  "metadata_adopted": false
}
```

This is the minimal scoped history requirement to assess in the architecture
revision. It does not authorise event sourcing, a general audit subsystem or
archive versioning. Current workbench metadata saves still replace the current
proposal fields; this document must not be presented as delivered functionality.

## Unadopted research export

An export is a self-contained readable bundle with the exact evidence object
above and all seven assertion revisions (three before review, three after
review, and one correction). The following index selects current records without
discarding their earlier versions. These names are illustrative bundle members,
not new directories or a prescribed archive layout.

```json
{
  "synthetic": true,
  "purpose": "research review only",
  "metadata_adopted": false,
  "accession_authorized": false,
  "deletion_authorized": false,
  "evidence_member": "evidence.json",
  "assertion_history_member": "assertions.jsonl",
  "current": [
    {"key": "photo-type", "revision": 2, "review": "accepted"},
    {"key": "alternative-type", "revision": 1, "review": "rejected"},
    {"key": "type-patent", "revision": 1, "review": "unresolved"}
  ],
  "included": ["readable evidence and citation records", "all assertion revisions and reasons"],
  "omitted": ["source images and PDFs", "credentials", "disposable OCR and browsing indexes"],
  "citation_access": "requires separately permitted source files and verified root mapping"
}
```

The export retains accepted, rejected and unresolved meanings separately.
Omitted source files are explicitly declared; this illustrative export alone is
not a portable, visually verifiable demonstration. A permitted real sample must
include or provide access to the bound sources. Rebuilding SQLite must recover
the current rows and their history from these readable records; it must not
promote accepted research into adopted catalogue metadata or trusted training
evidence. No exporter or database is implemented in this slice.

## Bounded pilot selection specification

Target 24 isolated cases, with each selected once in the primary group below.
This fixes an evaluation scope, not a claim that 24 eligible records are ready.
Keep exact source selections and permissions in the private work area.

| Primary group | Cases | Required coverage |
| --- | ---: | --- |
| Existing photo comparisons | 8 | Multiple candidates, known negative pair, unresolved identity, preserved saved reviewer wording |
| Report-page comparisons | 4 | Image-to-page, page-to-page, printed labels differing from sequence, OCR regenerated without breaking citation |
| Patent/relationship cases | 4 | Coherent photograph/report/patent example, type versus individual, unresolved invention relation, republication lineage |
| Access/binding failures | 4 | Missing source, changed content, unavailable external reference, optional processor absent |
| Correction/recovery cases | 4 | Stale edit, correction retained, projection failure reported, restart/rebuild/export agreement |

Current availability evidence is limited: the live review exposes 1,083 cases
and 111 proposals; document and visual support indexes exist. Neither their
presence nor a keyword match establishes a coherent WF report/patent set or
reuse permission. Real mixed-source selection remains pending curator/source
inspection; do not substitute an unrelated photo for the required relationship.
The synthetic example can establish the record contract first. There is no
authorised model batch, bulk OCR run, real review import or accession here.

Before implementing the pilot: verify the selected files/citation locators,
document permission to use them, record expected outcomes independently of model
proposals, and freeze the 24-case selection. Then obtain the scoped architecture
revision described in the plan, covering relationship/citation/history records,
portable configuration and the Django/ORM pilot. The current record example is
reviewable preparation for that revision, not its approval.
