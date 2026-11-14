# Review of commit 52ad782 (`Archive-tools/reference_batch.py` v11) and harvesting options

Follow-up to `REFERENCE_BATCH_REVIEW_6974e72.md`. Method: full diff read, tests run (`test_reference_batch.py` 113/113, `test_research_routes.py` 6/6), probe scripts against v11.
Not run: Codex, network. Harvest endpoints could not be fetched from the review sandbox (allowlist), so they are not tested live.

## Status of N1-N5
| Finding | v11 |
|---|---|
| N1 Pending queue replaces follow-up search | Fixed (R). Pool of 7: two searches run; features survive inspection rounds. |
| N2 Document-only citation bypasses blind guard | Fixed (R). Page documents need a matching page image or a reviewer binding. |
| N3 `photograph_date` outside blind guard | Fixed (R). Unbound reference-photo date goes to limitations. |
| N4 Overview and crop share a banner | Fixed (R). Overview reads `INPUT`. |
| N5 One item over allowance stops batch | Unchanged by decision. |

## Consequences to watch
1. Cost per item rises: every downloaded image now gets a full-size comparison. Simulation with two pools of 12: 2 searches, 2 shortlists, 4 comparison batches (up to 3 model calls each). With N5, one heavy item can end a batch early. Portals mode keeps the 30k allowance.
2. A dating run cannot finish unattended: `photo_record` always needs a reviewer binding, so `--research-goal dating` runs to the round limit. A "same photograph" blind verdict would close this safely.
3. Only exact class tokens are excluded now; `div.footer-widgets` images pass (R). Minor; `<footer>` tags still excluded.

## Harvesting options (sources from web search; verify before relying)
| Source | Interface | Use |
|---|---|---|
| Denkmalkarte Berlin | WFS at gdi.berlin.de/services/wfs/denkmale, Datenlizenz Deutschland Namensnennung 2.0 | Site list with geometry, >12,000 monuments. Best base for a building candidate list. |
| museum-digital | OAI-PMH at /oai per subdomain; LIDO and OAI-DC; queries replace sets (e.g. berlin.museum-digital.de/oai/place:61?verb=ListRecords&metadataPrefix=lido) | Place-filtered harvest. |
| ZLB digital | OAI-PMH digital.zlb.de/viewer/oai; MARC21, METS, OAI-DC | Address books/printed matter; less for photographs. |
| Europeana | OAI-PMH, datasets as EDM RDF/XML | Whole datasets only; poor for place filtering. |
| DDB | API with account key | OAI endpoint not confirmed. |
| Stadtmuseum Berlin | none found | No public API documentation found. |
Not checked: Bundesarchiv, Landesarchiv, Deutsche Fotothek. Not confirmed: whether museum-digital LIDO records carry image links, or how much Schöneweide material exists.

## Suggested order for accuracy
1. Measure per stage on 10-20 known photos: found, downloaded, shortlisted, inspected, judged correctly. v11 has had no accuracy run.
2. Local candidate index from Denkmal WFS, museum-digital place harvest and Commons categories; discovery picks from known sites instead of free web search.
3. Add a "same photograph" verdict so catalogue records/dates can count without manual binding.
4. Let an over-budget item skip to the next; keep the batch average as the batch stop.
5. Consider an embedding ranker (e.g. SigLIP) only after 1-2 and only if the benchmark shows the shortlist is the weak stage.

The harvester needs no new dependency (WFS and OAI-PMH are XML over HTTP; use `Fetcher`, paged under the 12 MB cap; do not use Sickle, it bypasses Fetcher's guards). AGENTS.md approvals needed: index directory outside Git, `gdi.berlin.de` in the registry.
