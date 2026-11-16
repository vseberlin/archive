"use strict";
const $ = id => document.getElementById(id);
function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else if (value !== false && value !== undefined) node.setAttribute(key, String(value));
  }
  for (const child of children.flat()) if (child !== null && child !== undefined)
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  return node;
}
let overview, current, draft, dirty = false, saving = false, loadTicket = 0;
let queuePage = 0;
const metadataView = () => $("filter").value === "model_proposals";
let panes = [], combined, mode = "side", linked = true, active = 0, opacity = .5;
const label = id => current.object.candidates.find(c => c.id === id)?.label || id;
const hasPair = (a, b) => draft.different_pairs.some(p => p.includes(a) && p.includes(b));
const pending = o => !o.scope_closed && (!o.selected || o.unresolved_count > 0 || o.metadata_pending || o.review_pending || o.destination_pending || o.identity_pending || o.separate_source_pending || o.tiff_recovery_pending);
const discovery = () => current?.review_scope === 'new_objects_and_tiff_for_jpeg';
function paneCandidates(i) {
  if (metadataView() && current?.model_proposal) return current.object.candidates.filter(c => c.id === current.model_proposal.candidate_id);
  if (!discovery()) return current.object.candidates;
  if (i) return current.object.candidates.filter(c => c.reference_object_id && c.active_comparison);
  const sources = current.object.candidates.filter(c => !c.reference_object_id);
  const masters = sources.filter(c => c.eligible && c.complete);
  return masters.length ? masters : sources.filter(c => c.source_role !== 'derivative');
}
const eligible = c => c.eligible && c.complete && !c.problems.length;
const reasons = {similarity_review:"Unconfirmed similarity", master_upgrade:"Proposed Master upgrade", metadata_review:"Redundant image · metadata question",
  proposed_new_object:"Proposed new object", search_coverage_review:"Search coverage needs review", master_quality_review:"Master quality needs review",
  evidence_unavailable:"Image evidence unavailable", evidence_conflict:"Conflicting evidence", master_assignment_review:"Previous Master assignment proposal"};
function sameComponent(id, extra = []) {
  const connected = new Set(id ? [id] : []);
  const edges = [...(draft.same_pairs || []), ...extra, ...draft.redundant_candidate_ids.map(c => [draft.selected_candidate_id, c])];
  let size;
  do { size = connected.size; for (const edge of edges) if (edge.some(c => connected.has(c))) edge.forEach(c => connected.add(c)); } while (connected.size !== size);
  return connected;
}

async function api(path, options) {
  const response = await fetch(path, options);
  const result = await response.json();
  if (!response.ok) {
    const failure = Error(result.error || "Request failed");
    failure.httpStatus = response.status;
    throw failure;
  }
  return result;
}
function error(e) { $("error").hidden = false; $("error").textContent = e.message || String(e); }
function confirmLeave() { return !dirty || confirm("Discard the unsaved decisions on this object?"); }
function visible() {
  const q = $("search").value.toLowerCase().trim(), filter = $("filter").value;
  const route = metadataView() ? $("research-group")?.value || "" : "";
  return overview.objects.filter(o => [o.id, o.title, o.note, o.model_proposal_title].join(" ").toLowerCase().includes(q) &&
    (!route || o.research_route === route) &&
    (filter === "all" || filter === "matches" && o.candidate_count > 1 || filter === "alone" && o.candidate_count === 1 || filter === "pending" && pending(o) ||
     filter === "model_proposals" && o.has_model_proposal ||
     filter === "master" && !o.selected || filter === "reviewed" && !pending(o) || filter === (overview.review_scope ? o.review_kind : o.review_reason)));
}
function renderQueue() {
  if ($("research-group-label")) $("research-group-label").hidden = !metadataView();
  const rows = visible();
  queuePage = Math.min(queuePage, Math.max(0, Math.ceil(rows.length / 100) - 1));
  const shown = rows.slice(queuePage * 100, (queuePage + 1) * 100);
  $("counts").textContent = metadataView() ? `${rows.length} proposals · ${rows.filter(o => o.metadata_proposal_saved).length} saved` : `${rows.length} shown · ${overview.objects.filter(pending).length} need review · ${overview.objects.length} total`;
  $("queue").replaceChildren(...shown.map(o => el("button", {class: "queue-item", "aria-current": String(current?.object.id === o.id), onclick: () => navigate(o.id)},
    el("strong", {}, metadataView() ? o.model_proposal_title || "Untitled proposal" : o.id), metadataView() ? null : o.title,
    !metadataView() && o.has_model_proposal ? el("small", {class: "proposal-label"}, `AI proposal · ${o.model_proposal_title || "Untitled"}`) : null,
    el("small", {}, metadataView() ? (o.metadata_proposal_saved ? "Saved proposal" : "Ready to edit") : o.scope_closed ? "Existing archive object · outside discovery" : o.tiff_recovery_pending ? "TIFF for substitute JPEG Master" : o.archive_resolved ? "Covered by archive Master" : !o.selected ? "Source identity unresolved" :
      o.unresolved_count ? `Master selected · ${o.unresolved_count} unresolved` : o.identity_pending ? "Archive identity needs review" :
      o.separate_source_pending ? "Different source needs its own object" : o.destination_pending ? "Destination unresolved" : o.metadata_pending ? "Metadata needs review" : o.review_pending ? reasons[o.review_reason] || "Proposal needs review" : "Candidates reviewed"))));
  if (!rows.length) $("queue").append(el("p", {class: "muted"}, "No objects match this filter."));
  if (rows.length > 100) $("queue").append(el("div", {class: "toolbar"},
    el("button", {disabled: queuePage === 0, onclick: () => { queuePage--; renderQueue(); }}, "Earlier cases"),
    el("span", {class: "muted"}, `${queuePage * 100 + 1}–${Math.min(rows.length, (queuePage + 1) * 100)}`),
    el("button", {disabled: (queuePage + 1) * 100 >= rows.length, onclick: () => { queuePage++; renderQueue(); }}, "More cases")));
  const position = rows.findIndex(o => o.id === current?.object.id);
  if ($("previous-object")) $("previous-object").disabled = position <= 0;
  if ($("next-object")) $("next-object").disabled = !rows.length || position >= rows.length - 1;
}
function navigateStep(step) {
  const rows = visible(), index = rows.findIndex(o => o.id === current?.object.id);
  const target = rows[index + step];
  if (target) navigate(target.id);
}
async function refresh(ask = true) {
  if (saving || ask && !confirmLeave()) return;
  try {
    const initialLoad = !overview;
    overview = await api("/api/overview");
    if (!$("research-group") && overview.research_route_options) {
      const select = el("select", {id:"research-group", onchange:()=>{queuePage=0;renderQueue();}},
        el("option", {value:""}, "All research groups"),
        ...overview.research_route_options.map(o=>el("option", {value:o.id}, o.label)));
      $("counts").before(el("label", {id:"research-group-label"}, "Research group", select));
    }
    if (initialLoad && overview.review_scope === 'new_objects_and_tiff_for_jpeg') $("filter").replaceChildren(
      ...[['pending','Needs review'],['proposed_new_object','New-object candidates'],['newness_review','Check whether already in archive'],
          ['tiff_for_jpeg_review','TIFF for substitute JPEG'],['reviewed','Outside pending review'],['all','All prepared evidence']]
        .map(([value,label])=>el('option',{value},label)));
    if (initialLoad && overview.archive_masters_fixed) $("filter").value = 'pending';
    if (overview.model_proposal_count && !$("filter").querySelector('option[value="model_proposals"]'))
      $("filter").append(el("option", {value:"model_proposals"}, `AI metadata proposals (${overview.model_proposal_count})`));
    if (initialLoad && overview.model_proposal_count && (!new URLSearchParams(location.search).get("filter") || new URLSearchParams(location.search).get("filter") === "model_proposals"))
      $("filter").value = 'model_proposals';
    $("unit-title").textContent = overview.title;
    $("unit-status").textContent = `${overview.read_only ? "Read-only" : "Review"} · ${overview.actor}`;
    renderQueue();
    const requested = new URLSearchParams(location.search).get("object");
    const id = current?.object.id || requested || visible()[0]?.id || overview.objects[0]?.id;
    if (id) await navigate(id, false);
    else $("review").replaceChildren(el("p", {class: "empty"}, "No unresolved review cases were prepared. See the preparation summary and confirmed-copy report."));
  } catch (e) { error(e); }
}
async function navigate(id, ask = true) {
  if (saving || ask && !confirmLeave()) return;
  const ticket = ++loadTicket;
  try {
    const result = await api("/api/object?id=" + encodeURIComponent(id));
    if (ticket !== loadTicket) return;
    current = result; draft = structuredClone(result.decision); dirty = false;
    draft.same_pairs ||= []; draft.destination_unit ||= "";
    const position = visible().findIndex(o => o.id === id);
    if (position >= 0) queuePage = Math.floor(position / 100);
    $("error").hidden = true;
    history.replaceState(null, "", "?" + new URLSearchParams({object: id, filter: $("filter").value}));
    renderObject(); renderQueue();
  } catch (e) { error(e); }
}
function changed(message = "Unsaved decisions") {
  dirty = true;
  $("save-status").textContent = message;
  $("save-status").className = "";
}
function selectMaster(id) {
  if (current.archive_masters_fixed && id && !current.object.candidates.find(c=>c.id===id)?.reference_object_id) {
    const refs = current.object.candidates.filter(c=>c.reference_object_id && sameComponent(id).has(c.id));
    if (refs.length > 1) { error(Error('Choose the retained archive Master for this confirmed group.')); return; }
    if (refs.length === 1 && !current.object.candidates.find(c=>c.id===id)?.tiff_recovery_source) id = refs[0].id;
  }
  if (draft.selected_candidate_id === id) return;
  const previous = draft.selected_candidate_id;
  if (previous) {
    for (const cid of draft.redundant_candidate_ids) {
      const pair = [previous, cid].sort();
      if (!draft.same_pairs.some(p => p.includes(previous) && p.includes(cid))) draft.same_pairs.push(pair);
    }
  }
  draft.selected_candidate_id = id;
  draft.redundant_candidate_ids = [];
  if (id) draft.redundant_candidate_ids = [...sameComponent(id)].filter(cid => cid !== id).sort();
  const selected = current.object.candidates.find(c => c.id === id);
  if (selected?.archive_unit) draft.destination_unit = selected.archive_unit;
  changed(id && draft.redundant_candidate_ids.length
    ? "Master changed · confirmed copies marked redundant"
    : "Master changed · review remaining candidates");
  renderDecisions();
}
function removePair(a, b) {
  draft.different_pairs = draft.different_pairs.filter(p => !(p.includes(a) && p.includes(b)));
}
function different(a, b) {
  if (!a || !b || a === b || current.read_only) return;
  if (sameComponent(a).has(b)) {
    error(Error("These files are marked as the same photograph. Clear the same-photo or redundant-copy decision first."));
    return;
  }
  if (!hasPair(a, b)) draft.different_pairs.push([a, b].sort());
  changed(); renderDecisions();
}
function samePhoto(a, b) {
  if (!a || !b || a === b || current.read_only || sameComponent(a).has(b)) return;
  if (draft.different_pairs.some(([x,y]) => sameComponent(x, [[a,b]]).has(y))) {
    error(Error("This would conflict with a different-photo decision. Clear that rejection first.")); return;
  }
  draft.same_pairs.push([a,b].sort());
  const reference = current.object.candidates.find(c => [a,b].includes(c.id) && c.archive_unit);
  if (reference && !draft.destination_unit) draft.destination_unit = reference.archive_unit;
  if (current.archive_masters_fixed && reference) {
    const selected = current.object.candidates.find(c => c.id === draft.selected_candidate_id);
    if (discovery() && selected?.tiff_recovery_source && current.scope.tiff_reference_ids.includes(reference.id) &&
        sameComponent(selected.id).has(reference.id)) {
      draft.redundant_candidate_ids = [...sameComponent(selected.id)].filter(cid => cid !== selected.id &&
        !current.object.candidates.find(c => c.id === cid)?.reference_object_id).sort();
      changed('Same photograph confirmed · TIFF replacement proposed'); renderDecisions();
      return;
    }
    if (draft.selected_candidate_id !== reference.id) selectMaster(reference.id);
    else { changed('Same photograph confirmed · archive Master retained'); renderDecisions(); }
    return;
  }
  changed("Same photograph confirmed · select the retained or proposed Master"); renderDecisions();
}
function relation(cid, value) {
  if (value === "different_photo" && sameComponent(cid).has(draft.selected_candidate_id)) {
    error(Error("Clear the same-photo or redundant-copy relationship before rejecting this pair.")); renderDecisions(); return;
  }
  draft.redundant_candidate_ids = draft.redundant_candidate_ids.filter(x => x !== cid);
  removePair(cid, draft.selected_candidate_id);
  if (value === "redundant") draft.redundant_candidate_ids.push(cid);
  if (value === "different_photo") draft.different_pairs.push([cid, draft.selected_candidate_id].sort());
  changed(); renderDecisions();
}
function renderDecisions() {
  const root = $("decisions"), readOnly = current.read_only, selected = draft.selected_candidate_id;
  root.replaceChildren(el("h3", {}, discovery() ? "Source identity and new-object decision" : "Master and candidate decisions"),
    el("p", {class: "muted"}, discovery() ? 'Determine whether the source is already represented in the archive. Existing archive Masters are references; selecting one does not replace it.' : "Select one complete Master. Other candidates stay unresolved until reviewed; confirmed same-photo copies are marked redundant automatically."),
    el("label", {}, el("input", {type: "radio", name: "master", checked: !selected, disabled: readOnly, onchange: () => selectMaster("")}), " Master unresolved"));
  if (current.archive_masters_fixed) root.append(el('p', {class:'muted'},
    discovery() ? 'Identify new objects. Existing md objects stay reconciled. Only a genuine TIFF for a documented substitute JPEG Master can enter replacement review.' : 'Existing archive Masters and metadata stay fixed. Confirmed copies do not need another Master or metadata review.'));
  if (overview.destination_units?.length) {
    const destination = el("select", {"aria-label":"Destination archive unit", disabled:readOnly || Boolean(current.object.candidates.find(c=>c.id===selected)?.reference_object_id),
      onchange:e=>{ draft.destination_unit=e.target.value; changed(); }},
      el("option",{value:""},"Destination unresolved"), overview.destination_units.map(u=>el("option",{value:u.path},u.label)));
    destination.value = draft.destination_unit;
    root.append(el("label",{},"Destination archive unit",destination), el("p",{class:"muted"},"A destination is a proposal. Saving does not accession files or replace an archive Master."));
  }
  if (current.object.recommended_candidate_id && !current.archive_resolved) root.append(el("p",{class:"muted"},`Source Master preference: ${label(current.object.recommended_candidate_id)}. Usable TIFF first; otherwise the highest available JPEG resolution.`));
  for (const c of current.object.candidates) {
    if (discovery() && c.reference_object_id && !c.active_comparison) continue;
    const chosen = selected === c.id;
    const card = el("article", {class: "candidate" + (chosen ? " chosen" : "")},
      el("label", {}, el("input", {type: "radio", name: "master", checked: chosen, disabled: readOnly || !eligible(c), "aria-label": `Select ${c.label || c.id} as Master`, onchange: () => selectMaster(c.id)}),
        el("span", {}, c.label || c.id), el("span", {class: "badge"}, current.scope?.closed && !c.reference_object_id ? 'Existing archive source' : chosen ? c.reference_object_id && current.archive_masters_fixed ? "Retained archive Master" : draft.decided_at ? "Selected Master" : "Proposed Master" : c.reference_object_id ? "Existing archive Master" : c.source_role === "derivative" ? "Derivative" : c.eligible && c.complete ? "Master candidate" : "Comparison only")));
    if (c.evidence) card.append(el("p", {}, typeof c.evidence === "string" ? c.evidence : JSON.stringify(c.evidence)));
    if (c.technical) card.append(el("p",{class:"muted"},`${c.technical.format} · ${c.technical.width || "?"} × ${c.technical.height || "?"} source pixels${c.technical.is_surrogate ? " · manufactured surrogate" : ""}`));
    if (draft.previous_master_ids.includes(c.id)) card.append(el("p", {class: "muted"}, "Provenance: previously selected as Master in this work area."));
    for (const problem of c.problems) card.append(el("p", {class: "notice"}, problem));
    const buttons = el("div", {class: "choices"});
    for (let i = 0; i < 2; i++) if (paneCandidates(i).some(p=>p.id===c.id)) buttons.append(el("button", {onclick: () => choosePane(i, c.id)}, `Compare in ${i ? "B" : "A"}`));
    card.append(buttons);
    if (!chosen && selected) {
      const choice = el("select", {"aria-label": `Decision for ${c.label || c.id}`, disabled: readOnly || c.problems.length > 0, onchange: e => relation(c.id, e.target.value)},
        el("option", {value: "unresolved"}, "Unresolved"), el("option", {value: "redundant"}, c.reference_object_id && current.archive_masters_fixed ? "Same photograph · retain archive Master" : "Redundant copy of selected Master"),
        el("option", {value: "different_photo"}, "Different photo from selected Master"));
      choice.value = sameComponent(selected).has(c.id) ? "redundant" : hasPair(c.id, selected) ? "different_photo" : "unresolved";
      card.append(el("label", {class: "relation"}, `Compared with ${label(selected)}`, choice));
    }
    card.append(el("details", {}, el("summary", {}, "Source paths and prepared evidence"),
      el("pre", {class: "source-record"}, JSON.stringify(Object.fromEntries(Object.entries(c).filter(([key]) => key !== "problems")), null, 2))));
    root.append(card);
  }
  const pairs = el("section", {class: "panel"}, el("h3", {}, "Rejected matches · different photos"));
  if (!draft.different_pairs.length) pairs.append(el("p", {class: "muted"}, "No rejected pairs recorded."));
  for (const [a, b] of draft.different_pairs) pairs.append(el("div", {class: "toolbar"},
    el("span", {}, `${label(a)} ↔ ${label(b)}`),
    el("button", {disabled: readOnly, onclick: () => { removePair(a, b); changed(); renderDecisions(); }}, "Clear rejection")));
  $("pairs").replaceChildren(pairs);
  const positives = el("section",{class:"panel"},el("h3",{},"Confirmed same-photo relationships"));
  if (!draft.same_pairs.length) positives.append(el("p",{class:"muted"},"No explicit same-photo pairs recorded."));
  for (const [a,b] of draft.same_pairs) positives.append(el("div",{class:"toolbar"},el("span",{},`${label(a)} ↔ ${label(b)}`),
    el("button",{disabled:readOnly,onclick:()=>{draft.same_pairs=draft.same_pairs.filter(p=>!(p.includes(a)&&p.includes(b)));changed();renderDecisions();}},"Clear same-photo decision")));
  $("pairs").append(positives);
  const available = new Set(current.object.candidates.filter(c => !c.problems.length).map(c => c.id));
  const matches = (current.object.matches || []).filter(m => !(hasPair(m.a, m.b) && available.has(m.a) && available.has(m.b)) &&
    (!discovery() || paneCandidates(0).some(c=>c.id===m.a) && paneCandidates(1).some(c=>c.id===m.b)));
  $("matches").replaceChildren(el("h3", {}, "Suggested comparisons"),
    el("p", {class: "muted"}, "Perceptual hashes and SIFT geometry are comparison evidence. Review the images before deciding; an unsupported SIFT match does not prove different photographs."));
  if (!matches.length) $("matches").append(el("p", {class: "muted"}, "No remaining prepared suggestions. All candidates remain available in A and B."));
  const groups = new Map();
  for (const match of matches) {
    const key = JSON.stringify([match.a, match.b].sort());
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(match);
  }
  for (const evidence of groups.values()) {
    const match = evidence[0];
    const summary = evidence.map(m => m.engine === 'photo_identity' ? (m.status === 'verified'
      ? 'Native raster verified after orientation/polarity normalization'
      : `Image identity still needs confirmation: ${m.reason || 'native pixel values differ'}`) : m.engine === "cached-pda" ? `p/d/a distances ${m.phash_distance}/${m.dhash_distance}/${m.ahash_distance} · ${m.polarity}` :
      m.engine === "sift" ? `SIFT: ${m.status === "supported" ? "geometry supported" : m.status === "not_supported" ? "geometry not supported" : m.status === "insufficient_features" ? "insufficient detail" : "unavailable"} · ${m.inliers || 0} consistent points · ${m.variant}` :
      m.engine === "czkawka" ? `Czkawka ${m.algorithm}/${m.hash_size}/${m.resize_filter}: distance ${m.difference}${m.max_difference !== undefined ? ` (limit ${m.max_difference})` : ""}` : m.engine === "metadata" ? `Metadata: ${m.basis}` : m.engine || m.algorithm || "Prepared comparison");
    $("matches").append(el("div", {}, el("button", {onclick: () => { choosePane(0, match.a); choosePane(1, match.b); }}, `${label(match.a)} ↔ ${label(match.b)}`),
      el("p", {class: "muted"}, summary.join(" · ")),
      el("details", {}, el("summary", {}, "Comparison evidence"), el("pre", {class: "source-record"}, JSON.stringify(evidence, null, 2)))));
  }
  if (discovery()) {
    const excluded = current.object.candidates.filter(c=>c.reference_object_id && !c.active_comparison);
    if (excluded.length) $("matches").append(el('details', {}, el('summary', {}, `${excluded.length} historical suggestions excluded from active comparison`),
      el('p', {class:'muted'}, 'Their failed or unsupported search evidence is retained. Exclusion does not assert a different photograph.'),
      el('pre', {class:'source-record'}, JSON.stringify(excluded.map(c=>({id:c.id,label:c.label,members:c.members,
        evidence:(current.object.matches||[]).filter(m=>m.a===c.id||m.b===c.id)})),null,2))));
  }
  updatePairButton();
}
function renderObject() {
  document.body.classList.toggle("metadata-mode", metadataView());
  $("unit-title").textContent = metadataView() ? "Metadata review" : overview.title;
  if (metadataView() && current.model_proposal) return renderMetadataEditor();
  const o = current.object, readOnly = current.read_only;
  panes = []; mode = "side"; linked = true; active = 0; opacity = .5;
  const root = $("review");
  root.replaceChildren(el("div", {class: "heading"}, el("div", {}, el("span", {class: "eyebrow"}, o.id), el("h2", {}, o.title || o.id)),
    el("div", {class: "toolbar"}, el("button", {id: "previous-object", onclick: () => navigateStep(-1)}, "← Previous"),
      el("button", {id: "next-object", onclick: () => navigateStep(1)}, "Next →"))));
  if (o.description) root.append(el("p", {}, o.description));
  if (current.model_proposal_warning) root.append(el("p", {class:"notice"}, current.model_proposal_warning));
  if (current.scope?.closed) root.append(el('p', {class:'notice'}, `Existing archive object${current.scope.md_object_id ? ' · '+current.scope.md_object_id.split('#').pop() : ''}. Outside new-object discovery; its Master and metadata stay fixed.`));
  else if (current.scope?.tiff_recovery_pending) root.append(el('p', {class:'notice'}, 'Genuine TIFF candidate for a documented substitute JPEG Master. Review identity and the TIFF proposal; saving does not replace the archive file.'));
  else if (discovery() && current.scope?.kind === 'proposed_new_object') root.append(el('p', {class:'notice'}, 'New-object candidate. No supported archive comparison remains in this prepared search; this does not establish exhaustive absence.'));
  else if (current.archive_resolved) root.append(el('p', {class:'notice'}, 'Covered by the retained archive Master. Metadata differences remain available as evidence.'));
  else if (reasons[o.review_reason]) root.append(el("p",{class:"notice"},reasons[o.review_reason]));
  if (["proposed_new_object","search_coverage_review"].includes(o.review_reason)) root.append(el("p",{class:"muted"},"No corresponding object was established within this search. Review coverage and metadata before accession."));
  if (o.review_reason === "no_candidate_found_within_search_settings") root.append(el("p", {class: "notice"}, "No candidate found within the search settings. This does not establish that the photograph is absent from the archive or belongs to this collection."));
  if (o.search_coverage) root.append(el("details", {}, el("summary", {}, "Search coverage and remaining gaps"), el("pre", {class: "source-record"}, JSON.stringify(o.search_coverage, null, 2))));
  if (readOnly && !current.scope?.closed) root.append(el("p", {class: "notice"}, "This unit is completed or read-only. Saved decisions are available for inspection."));
  if (!overview.projection_agrees) root.append(el("p", {class: "muted"}, "The CSV summary has not been generated or needs rebuilding. Saved decisions remain available."));
  const toolbar = el("div", {class: "toolbar"});
  for (const [key, name] of [["side", "Side by side"], ["overlay", "Overlay"], ["difference", "Difference"]])
    toolbar.append(el("button", {"data-mode": key, "aria-pressed": String(key === mode), onclick: () => setMode(key)}, name));
  toolbar.append(el("span", {class: "spacer"}), el("label", {}, el("input", {type: "checkbox", checked: true, onchange: e => linked = e.target.checked}), "Link views"),
    el("button", {onclick: () => changeZoom(1 / 1.4), "aria-label": "Zoom out"}, "−"),
    el("button", {onclick: () => changeZoom(1.4), "aria-label": "Zoom in"}, "+"),
    el("button", {onclick: resetView}, "Fit"), el("button", {onclick: () => panes.forEach((_, i) => loadImage(i, 3200))}, "Load detail"));
  root.append(toolbar, el("p", {id: "same-file-warning", class: "notice", hidden: true},
    "Both panes show the same source file. This is a page/view comparison, not evidence of another archive copy."));
  const comparison = el("div", {class: "compare", id: "comparison"}); root.append(comparison);
  if (o.candidates.length) comparison.append(makePane(0), makePane(1));
  else comparison.append(el("p", {}, "No candidates supplied. Leave the object unresolved."));
  combined = el("canvas", {"aria-label": "Combined image comparison"}); bindCanvas(combined, 2);
  root.append(el("div", {class: "stage combined", id: "combined", hidden: true}, combined),
    el("label", {id: "opacity-label", hidden: true}, "Overlay amount ", el("input", {type: "range", min: 0, max: 100, value: 50, oninput: e => { opacity = Number(e.target.value) / 100; drawAll(); }})),
    el("p", {class: "muted"}, "Wheel to zoom; drag to pan. Unlink to align crops. TIFF views show the selected page/composite; they do not verify Photoshop layers."),
    el("button", {id: "reject-pair", disabled: readOnly, onclick: () => different(panes[0]?.cid, panes[1]?.cid)}, "A and B are different photos"),
    el("button", {id: "confirm-pair", disabled: readOnly, onclick: () => samePhoto(panes[0]?.cid, panes[1]?.cid)}, "A and B are the same photograph"),
    el("section", {class: "panel", id: "matches"}),
    el("details", {}, el("summary", {}, "PDF page text"), el("button", {onclick: showText}, "Read displayed PDF pages"), el("div", {id: "text-result", class: "text-result"})),
    el("section", {class: "panel", id: "metadata-comparison"}),
    el("section", {class: "panel", id: "decisions"}), el("div", {id: "pairs"}));
  const metadata = el("section", {class: "panel"}, el("h3", {}, "Descriptive metadata"));
  if (Object.keys(o.metadata || {}).length) metadata.append(el("pre", {class: "source-record"}, JSON.stringify(o.metadata, null, 2)));
  else if (o.candidates.some(c => c.metadata_errors?.length))
    metadata.append(el("p", {}, "Metadata unavailable · extraction failed for an inspected file. This does not establish that metadata is absent."));
  else if (o.candidates.some(c => Object.keys(c.metadata || {}).length || c.prior_review_evidence?.some(e => e.record?.metadata || e.record?.title)))
    metadata.append(el("p", {}, "No descriptive metadata extracted from the inspected files. Candidate metadata and prior research remain available alongside the comparisons."));
  else metadata.append(el("p", {}, "Metadata unknown · no descriptive metadata supplied for this object."));
  metadata.append(el("p", {class: "muted"}, "Prepared metadata is evidence. Selecting a Master or rejecting a match does not transfer metadata between photographs."));
  if (o.metadata_review_required && !current.archive_resolved && !(discovery() && current.scope?.md_object_id)) metadata.append(el("label", {}, el("input", {type: "checkbox", checked: draft.metadata_reviewed, disabled: readOnly,
    onchange: e => { draft.metadata_reviewed = e.target.checked; changed(); }}), " Metadata question reviewed; record the conclusion in the note."));
  const note = el("textarea", {id: "note", maxlength: 2000, disabled: readOnly, oninput: e => { draft.note = e.target.value; changed(); }}); note.value = draft.note;
  const ai = renderModelProposal();
  if (ai) root.insertBefore(ai, $("metadata-comparison"));
  root.append(metadata, el("section", {class: "panel"}, el("label", {for: "note"}, "Short review note"), note),
    el("section", {class: "panel decision-bar"}, el("p", {}, el("strong", {id: "save-status", role: "status", class: "good"}, draft.decided_at ? `Saved by ${draft.actor} · ${new Date(draft.decided_at).toLocaleString()}` : "No decision saved yet")),
      el("button", {id: "save", class: "primary", disabled: readOnly, onclick: save}, "Save decisions")));
  if (o.candidates.length) {
    const first = paneCandidates(0).find(c => c.id === draft.selected_candidate_id) || paneCandidates(0)[0];
    choosePane(0, first?.id || ''); choosePane(1, paneCandidates(1).find(c => c.id !== first?.id && c.id === draft.selected_candidate_id)?.id || paneCandidates(1).find(c=>c.id!==first?.id)?.id || "");
  }
  renderDecisions(); requestAnimationFrame(drawAll);
}
function proposalFields(proposal) {
  if (proposal.suggested_fields) return structuredClone(proposal.suggested_fields);
  const r = proposal.result, text = c => {
    if (!c) return "";
    if (typeof c === "string") return c;
    const value = c.display || c.value || c.text || "";
    return value ? value + (c.basis === "hypothesis" || c.basis === "visual_estimate" || c.uncertain_parts ? " (tentative)" : "") : "";
  };
  const list = values => (values || []).map(text).filter(Boolean).join("; ");
  return {title: text(r.title), description: text(r.description), date: text(r.date),
    places: list(r.places), creator: text(r.creator), subjects: list(r.subjects),
    inscriptions: list(r.inscriptions), notes: "", ...proposal.consolidated_proposal};
}
function referenceConclusion(result) {
  return [["depicted_object","Object"],["location","Location"],["event","Event"],["photograph_date","Photograph date"]]
    .map(([key,title]) => {
      const claim = result[key] || {};
      const status = ["supported","tentative","unresolved","contradicted"].includes(claim.status) ? claim.status : "unresolved";
      return `${title}: ${status}${claim.value && status !== "unresolved" ? " — " + claim.value : ""}`;
    }).join("; ");
}
function locationHypotheses(proposal) {
  if (proposal.reference_check?.status === "compared")
    return (proposal.reference_check.result?.hypotheses || [])
      .filter(h => h.status === "tentative" || h.status === "unresolved")
      .map(h => ({title:h.candidate, reasoning:h.assessment}));
  // Reviewed summaries replace superseded leads; original leads remain evidence.
  if (proposal.consolidated_proposal) return [];
  return (proposal.result.research_leads || []).filter(v => typeof v === "string" && v.trim()).map(value => {
    const clean = value.replace(/^\d+\.\s*/, "");
    const parts = clean.split(/\s+[—–]\s+/);
    return {title: parts[0], reasoning: parts.slice(1).join(" — ")};
  });
}
function renderMetadataEditor() {
  panes = []; mode = "side"; linked = false; active = 0;
  const proposal = current.model_proposal, saved = draft.metadata_proposal;
  const fields = saved?.fields || proposalFields(proposal);
  draft.metadata_proposal = {...saved, fields: structuredClone(fields)};
  const disabled = current.read_only || Boolean(proposal.source_problems?.length);
  const root = $("review");
  root.replaceChildren(el("div", {class:"heading"}, el("h2", {}, "Metadata proposal"),
    el("div", {class:"toolbar"}, el("button", {id:"previous-object", onclick:()=>navigateStep(-1)}, "← Previous"),
      el("button", {id:"next-object", onclick:()=>navigateStep(1)}, "Next →"))));
  const picture = el("section", {class:"proposal-picture"});
  const pane = makePane(0), p = panes[0];
  const stage = pane.querySelector(".stage");
  pane.replaceChildren(stage, p.status);
  const candidate = current.object.candidates.find(c => c.id === proposal.candidate_id);
  if (candidate.members.length > 1 || candidate.members.some(m => m.pages > 1))
    pane.append(el("div", {class:"pages"}, p.memberSelect, el("label", {}, "Page ", p.pageInput)));
  picture.append(el("div", {class:"toolbar"},
    el("button", {onclick:()=>changeZoom(1 / 1.4), "aria-label":"Zoom out"}, "−"),
    el("button", {onclick:()=>changeZoom(1.4), "aria-label":"Zoom in"}, "+"),
    el("button", {onclick:resetView}, "Fit"), el("button", {onclick:()=>loadImage(0,3200)}, "Load detail")), pane,
    el("p", {class:"muted"}, "Scroll to zoom · drag to pan"));
  const form = el("section", {class:"proposal-form", "aria-label":"Editable metadata"});
  form.append(el("p", {class:"muted"}, "Edit and save a proposal for later catalogue use."));
  if (saved?.fields && proposal.reference_check?.status === "compared") {
    form.append(el("button", {disabled, onclick:()=>{
      draft.metadata_proposal.fields = proposalFields(proposal);
      renderMetadataEditor(); changed("Latest research loaded into the draft. Review and save to keep it.");
    }}, "Use latest research proposal"));
  }
  if (current.research_routing) {
    const route = current.research_routing, options = current.research_route_options;
    const label = key => options.find(o=>o.id===key)?.label || key;
    const override = saved?.research_routing;
    draft.metadata_proposal.research_routing = override || null;
    const primary = el("select", {id:"research-route", disabled},
      el("option", {value:""}, "Automatic"), ...options.map(o=>el("option", {value:o.id}, o.label)));
    primary.value = override?.primary || "";
    const secondary = el("select", {id:"research-secondary", multiple:true, size:4, disabled:disabled || !override},
      ...options.map(o=>el("option", {value:o.id, selected:(override?.secondary || route.secondary).includes(o.id)}, o.label)));
    const saveRoute = () => {
      secondary.disabled = disabled || !primary.value;
      draft.metadata_proposal.research_routing = primary.value ? {primary:primary.value,
        secondary:[...secondary.selectedOptions].map(o=>o.value).filter(k=>k!==primary.value).slice(0,3)} : null;
      changed("Unsaved research route");
    };
    primary.addEventListener("change", saveRoute); secondary.addEventListener("change", saveRoute);
    form.append(el("details", {}, el("summary", {}, "Research route · " + label(route.primary)),
      el("p", {class:"muted"}, `${route.confidence} confidence · ${route.secondary.map(label).join("; ") || "No secondary tags"}`),
      el("label", {for:"research-route"}, "Primary research route"), primary,
      el("label", {for:"research-secondary"}, "Secondary routes (up to three)"), secondary,
      el("p", {class:"muted"}, "Guides future research; does not identify the photograph. Save below to keep an override.")));
  }
  if (disabled) form.append(el("p", {class:"notice"}, current.read_only ? "This object is read-only." : "Source unavailable or changed. Saving is disabled until the source is checked."));
  for (const [key, title] of [["title","Title"],["description","Description"],["date","Date of photograph"],
    ["places","Location"],["creator","Photographer"],["subjects","Subjects"],["inscriptions","Inscriptions"],["notes","Uncertainty / reviewer note"]]) {
    const input = el(["description","subjects","inscriptions","notes"].includes(key) ? "textarea" : "input",
      {id:"meta-"+key, maxlength:12000, disabled, placeholder: key === "date" || key === "places" ? "Unknown" : "",
        oninput:e=>{draft.metadata_proposal.fields[key]=e.target.value; changed("Unsaved metadata changes");}});
    input.value = fields[key] || "";
    form.append(el("label", {for:"meta-"+key}, title), input);
    if (key === "places") {
      const check = proposal.reference_check;
      if (check?.status === "compared" && check.result)
        form.append(el("p", {class:"reference-conclusion"}, "Reference comparison: " + referenceConclusion(check.result)));
      else if (check?.status === "access_limited")
        form.append(el("p", {class:"muted"}, "Reference images could not be retrieved; these hypotheses remain unverified."));
      const hypotheses = locationHypotheses(proposal);
      if (hypotheses.length) form.append(el("section", {class:"location-hypotheses", "aria-label":"Unconfirmed research hypotheses"},
        el("p", {class:"muted"}, "Research hypotheses · unconfirmed"),
        ...hypotheses.map(h => h.reasoning
          ? el("details", {}, el("summary", {}, h.title), el("p", {}, h.reasoning))
          : el("p", {}, h.title))));
    }
  }
  const sources = el("details", {}, el("summary", {}, "Sources and uncertainty"));
  if (proposal.consolidated_proposal) {
    sources.append(el("p", {}, proposal.consolidation_note || "Includes reviewed findings."));
  } else {
    for (const [name,key] of [["Title","title"],["Description","description"],["Date","date"],["Photographer","creator"]]) {
      const c = proposal.result[key];
      if (c?.evidence || c?.uncertainty) sources.append(el("p", {}, name + ": " + [c.evidence,c.uncertainty].filter(Boolean).join(" ")));
    }
    for (const c of proposal.result.places || []) if (c.evidence || c.uncertainty)
      sources.append(el("p", {}, [c.value,c.evidence,c.uncertainty].filter(Boolean).join(" · ")));
  }
  if (draft.note) sources.append(el("p", {}, "Earlier review note: " + draft.note));
  for (const url of proposal.ocr_research?.reference_research?.sources || []) {
    try { if (["https:","http:"].includes(new URL(url).protocol)) sources.append(el("p", {}, el("a", {href:url,target:"_blank",rel:"noopener noreferrer"}, new URL(url).hostname))); } catch {}
  }
  for (const ref of proposal.reference_check?.references || []) {
    try { if (["https:","http:"].includes(new URL(ref.record_url).protocol)) sources.append(el("p", {},
      el("a", {href:ref.record_url,target:"_blank",rel:"noopener noreferrer"}, ref.id + " · " + new URL(ref.record_url).hostname))); } catch {}
  }
  sources.append(el("p", {class:"muted"}, "Photograph: " + proposal.source_path.split("/").pop()));
  form.append(sources);
  root.append(el("div", {class:"metadata-editor"}, picture, form),
    el("section", {class:"panel decision-bar"}, el("p", {id:"save-status",role:"status"},
      saved?.saved_at ? `Saved by ${saved.actor} · ${new Date(saved.saved_at).toLocaleString()}` : "Proposal ready to edit"),
      el("button", {id:"save",class:"primary",disabled,onclick:()=>saveMetadata(false)}, "Save"),
      el("button", {disabled,onclick:()=>saveMetadata(true)}, "Save and next")));
  choosePane(0, proposal.candidate_id);
  requestAnimationFrame(drawAll);
}
async function saveMetadata(next) {
  if (saving || current.read_only || current.model_proposal.source_problems?.length) return;
  saving = true;
  let acknowledged = false;
  const controls = [...document.querySelectorAll("#review input,#review textarea,#review select,#review button")].map(n=>[n,n.disabled]);
  controls.forEach(([n])=>n.disabled=true); $("save-status").textContent="Saving…";
  try {
    const result = await api("/api/metadata", {method:"POST",headers:{"Content-Type":"application/json","X-Review-Token":overview.token},
      body:JSON.stringify({object_id:current.object.id,revision:current.metadata_revision,fields:draft.metadata_proposal.fields,
        research_routing:draft.metadata_proposal.research_routing || null})});
    acknowledged = true; current=result; draft=structuredClone(result.decision); dirty=false;
    overview=await api("/api/overview"); renderObject(); renderQueue(); $("error").hidden=true;
    if (result.warning) error(Error(result.warning));
    else if (next) { saving=false; navigateStep(1); }
  } catch(e) {
    error(e); $("save-status").textContent=acknowledged ? "Saved. View refresh failed; reload to refresh." : "Save not confirmed. Your edits remain open.";
  } finally { saving=false; controls.forEach(([n,d])=>{if(n.isConnected)n.disabled=d;}); }
}

function renderModelProposal() {
  const proposal = current.model_proposal;
  if (!proposal) return null;
  const r = proposal.result;
  const basis = {visible:"Visible observation", inscription:"Inscription", supplied_metadata:"Owner-supplied context",
    hypothesis:"Hypothesis", unknown:"Unknown", visual_estimate:"Visual estimate"};
  const claim = value => el("div", {class:"metadata-value"},
    el("span", {}, value?.value ?? "Unknown"),
    el("small", {class:"muted"}, basis[value?.basis] || value?.basis || ""),
    value?.evidence ? el("p", {}, value.evidence) : null,
    value?.uncertainty ? el("p", {class:"muted"}, value.uncertainty) : null);
  const section = el("section", {class:"panel model-proposal", id:"model-proposal"},
    el("h3", {}, "AI metadata proposal · unreviewed"),
    el("p", {class:"notice"}, "Research suggestion, not accepted catalogue metadata. Berlin and Kurt Schwarz were supplied by the owner; specific places and dates require review."),
    el("p", {class:"path"}, `For ${label(proposal.candidate_id)} · ${proposal.source_path}`));
  if (proposal.sculpture_identification) {
    const identification = proposal.sculpture_identification;
    section.append(el("h4", {}, "Sculpture identification · supplied by reviewer"),
      el("p", {}, `${identification.artist} — ${identification.title}`),
      el("pre", {class:"source-record"}, JSON.stringify(identification,null,2)),
      el("p", {class:"notice"}, "This identification supersedes the earlier bear/panther search leads. The sculpture’s creation and acquisition dates do not establish the photograph’s date or location."));
  }
  const ocr = proposal.ocr_research;
  if (ocr) {
    const status = {complete:"OCR processing complete", partial:"Partial OCR results", pending_or_failed:"OCR incomplete"};
    const panel = el("section", {class:"ocr-research"},
      el("h4", {}, status[ocr.status] || "OCR status unknown"),
      el("p", {class:"notice"}, "Raw OCR is unverified and may contain noise. Completion does not mean the text is correct or adopted."),
      el("p", {class:"muted"}, `Evidence snapshot: ${ocr.snapshot_at}${ocr.recovery ? " · Recovery running at snapshot time" : ""}`));
    if (ocr.human_followup) panel.append(el("p", {}, ocr.human_followup));
    if (ocr.reviewed_clues) panel.append(el("details", {}, el("summary", {}, "Reviewed textual and graphical clues"),
      el("pre", {class:"source-record"}, JSON.stringify(ocr.reviewed_clues,null,2))));
    if (ocr.reference_research) panel.append(el("details", {}, el("summary", {}, "Reference comparison findings · proposals"),
      el("pre", {class:"source-record"}, JSON.stringify(ocr.reference_research,null,2))));
    for (const run of ocr.runs || []) panel.append(el("details", {},
      el("summary", {}, `${run.label} · ${run.returncode === 0 ? "processed" : "incomplete"}${run.text_truncated ? " · excerpt" : ""}`),
      run.error ? el("p", {class:"notice"}, run.error) : null,
      el("pre", {class:"source-record"}, run.raw_text_excerpt || "No text recovered in this result."),
      el("p", {class:"path"}, run.image_path || "")));
    panel.append(el("details", {}, el("summary", {}, "OCR attempts and evidence paths"),
      el("pre", {class:"source-record"}, JSON.stringify({receipt:ocr.receipt_path, attempts:ocr.attempts},null,2))));
    section.append(panel);
  }
  if (proposal.source_problems?.length) section.append(el("p", {class:"notice"},
    "Source is unavailable or has changed since preparation. Recheck the image before using this proposal."));
  const date = {...r.date, value:r.date?.display};
  const body = el("tbody", {});
  for (const [name,value] of [["Title",r.title],["Description",r.description],["Date of photograph",date],["Photographer",r.creator]])
    body.append(el("tr", {}, el("th", {scope:"row"}, name), el("td", {}, claim(value))));
  body.append(el("tr", {}, el("th", {scope:"row"}, "Places"), el("td", {}, r.places?.length ? r.places.map(claim) : "No specific place proposed")));
  section.append(el("table", {class:"proposal-fields"}, body));
  if (r.research_leads?.length) section.append(el("h4", {}, "Research leads · not verified"), el("ul", {}, r.research_leads.map(v=>el("li", {}, v))));
  const details = el("details", {}, el("summary", {}, "Subjects, inscriptions and other proposed fields"));
  for (const [name,key] of [["Subjects","subjects"],["Inscriptions","inscriptions"],["Events","events"],
    ["Object type","object_type"],["Material","material"],["Technique","technique"],["Dimensions","dimensions"],
    ["Rights","rights"],["Rights holder","rights_holder"],["Limitations","limitations"]]) {
    const value = r[key];
    details.append(el("h4", {}, name));
    if (Array.isArray(value)) details.append(value.length ? el("div", {}, value.map(v => typeof v === "string" ? el("p", {}, v) :
      v.basis ? claim(v) : el("pre", {class:"source-record"}, JSON.stringify(v,null,2)))) : el("p", {class:"muted"}, "Not recorded"));
    else details.append(claim(value));
  }
  section.append(details, el("p", {class:"muted"},
    `${proposal.model} · ${proposal.reasoning_effort} reasoning · ${proposal.usage?.total_tokens?.toLocaleString() || "Unknown"} tokens · ${proposal.elapsed_seconds}s · ${proposal.preview_size.join(" × ")} px research preview`),
    el("details", {}, el("summary", {}, "Full proposal and provenance"), el("pre", {class:"source-record"}, JSON.stringify(proposal,null,2))),
    el("p", {class:"muted"}, "Record your assessment in the existing review note below. Saving a review does not adopt these proposed values."));
  return section;
}
function renderMetadataComparison() {
  const root = $("metadata-comparison"); if (!root) return;
  const a = current.object.candidates.find(c => c.id === panes[0]?.cid), b = current.object.candidates.find(c => c.id === panes[1]?.cid);
  root.replaceChildren(el("h3", {}, "Metadata · displayed A/B pair"));
  const appendContext = () => {
    for (const c of new Set([a, b].filter(Boolean))) if (c.prior_review_evidence?.length)
      root.append(el("details", {}, el("summary", {}, `Prior review and research · ${label(c.id)}`), el("pre", {class: "source-record"}, JSON.stringify(c.prior_review_evidence, null, 2))));
  };
  if (!a || !b) { root.append(el("p", {}, "No second file selected for metadata comparison. Source metadata is shown below.")); appendContext(); return; }
  const prepared = (current.object.metadata_comparisons || []).find(c => [a.id, b.id].includes(c.a) && [a.id, b.id].includes(c.b));
  const states = {agreement: "Agreement", archive_only: "Archive value only", source_only: "Source value only", different_values: "Different values · review", unknown: "Unknown on both sides", unavailable: "Evidence unavailable"};
  const displayValues = entries => entries?.length ? el("div", {}, entries.map(v => el("div", {class: "metadata-value"},
    el("span", {}, typeof v.value === "object" ? JSON.stringify(v.value) : String(v.value)),
    el("small", {class: "path muted"}, `${v.source || "Prepared evidence"}${v.field ? " · " + v.field : ""}`)))) : el("span", {class: "muted"}, "Unknown");
  if (prepared) {
    const swapped = prepared.a !== a.id;
    root.append(el("table", {}, el("thead", {}, el("tr", {}, ["Field", "A · " + label(a.id), "B · " + label(b.id), "Comparison"].map(t => el("th", {}, t)))),
      el("tbody", {}, prepared.fields.map(row => el("tr", {}, el("th", {}, row.field), el("td", {}, displayValues(swapped ? row.archive : row.source)),
        el("td", {}, displayValues(swapped ? row.source : row.archive)), el("td", {}, states[row.status] || row.status))))));
  } else {
    root.append(el("p", {class: "muted"}, a.id === b.id ? "The same candidate is displayed on both sides." : "No prepared field comparison for this pair."),
      el("div", {class: "text-result"}, [a, b].map((c, i) => el("div", {}, el("strong", {}, `${i ? "B" : "A"} · ${label(c.id)}`),
        el("pre", {class: "source-record"}, Object.keys(c.metadata || {}).length ? JSON.stringify(c.metadata, null, 2) : "Metadata unknown")))));
  }
  appendContext();
}
function makePane(i) {
  const p = {cid: "", member: 0, page: 1, edge: 1200, image: null, ticket: 0, zoom: 1, x: 0, y: 0, inverted: false}; panes[i] = p;
  p.select = el("select", {"aria-label": `Candidate ${i ? "B" : "A"}`, onchange: e => choosePane(i, e.target.value)},
    i ? el("option", {value: ""}, "No comparison selected") : null,
    paneCandidates(i).map(c => el("option", {value: c.id}, c.label || c.id)));
  p.memberSelect = el("select", {"aria-label": `Member ${i ? "B" : "A"}`, onchange: e => { p.member = Number(e.target.value); p.page = 1; updatePages(i); loadImage(i); }});
  const selectPage = value => {
    if (!Number.isInteger(value) || value < 1 || value > Number(p.pageInput.max) || value === p.page) return;
    p.page = value; updatePages(i); loadImage(i);
  };
  p.pageInput = el("input", {type: "number", min: 1, step: 1, value: 1, "aria-label": `Page ${i ? "B" : "A"}`,
    oninput: e => selectPage(Number(e.target.value)),
    onchange: e => { selectPage(Math.max(1, Math.min(Math.trunc(Number(e.target.value)) || 1, Number(p.pageInput.max)))); updatePages(i); }
  });
  p.status = el("p", {class: "muted", role: "status"}); p.canvas = el("canvas", {"aria-label": `Image ${i ? "B" : "A"}`}); bindCanvas(p.canvas, i);
  p.identity = el("p", {class: "path muted"});
  p.empty = el("p", {class: "empty", hidden: true}, "No comparison candidate");
  p.invert = el("input", {type: "checkbox", "aria-label": `Invert display ${i ? "B" : "A"}`, onchange: e => { p.inverted = e.target.checked; drawAll(); }});
  return el("section", {class: "pane"}, el("h3", {}, i ? "B · Compare" : "A · Compare"), p.select,
    el("div", {class: "pages"}, p.memberSelect, el("label", {}, "Page ", p.pageInput)),
    p.identity, el("label", {}, p.invert, " Invert display for negative/positive comparison"),
    el("div", {class: "stage"}, p.canvas, p.empty), p.status);
}
function updatePairButton() {
  if (!$("same-file-warning")) return;
  const a = panes[0]?.cid, b = panes[1]?.cid;
  $("same-file-warning").hidden = !a || !b ||
    current.object.candidates.find(c => c.id === a).members[panes[0].member].path !==
    current.object.candidates.find(c => c.id === b).members[panes[1].member].path;
  document.querySelectorAll('[data-mode="overlay"],[data-mode="difference"]').forEach(button => button.disabled = !a || !b);
  if ((!a || !b) && mode !== "side") setMode("side");
  $("reject-pair").disabled = current.read_only || !a || !b || !$("same-file-warning").hidden || a === b || hasPair(a, b) ||
    current.object.candidates.some(c => [a, b].includes(c.id) && c.problems.length);
  $("confirm-pair").disabled = current.read_only || !a || !b || !$("same-file-warning").hidden || sameComponent(a).has(b) ||
    current.object.candidates.some(c => [a,b].includes(c.id) && c.problems.length);
}
function choosePane(i, cid) {
  const p = panes[i]; if (!p) return;
  if (cid && !paneCandidates(i).some(c=>c.id===cid)) return;
  p.cid = cid; p.select.value = cid; p.member = 0; p.page = 1; p.zoom = 1; p.x = p.y = 0;
  const c = current.object.candidates.find(c => c.id === cid);
  p.memberSelect.disabled = p.invert.disabled = !c;
  p.canvas.hidden = !c; p.empty.hidden = Boolean(c);
  if (!c) {
    p.ticket++; p.image = null; p.memberSelect.replaceChildren(); p.pageInput.value = 1; p.pageInput.disabled = true;
    p.empty.textContent = current.object.candidates.length < 2 ? "No comparison candidate" : "No comparison selected";
    p.identity.textContent = "No second file selected";
    p.status.textContent = current.object.candidates.length < 2 ? "No comparison candidate was prepared for this source." : "Choose a candidate to compare.";
    if (discovery() && i === 1 && !paneCandidates(1).length) {
      p.empty.textContent = 'No active archive comparison';
      p.status.textContent = current.scope?.closed ? 'This existing archive object is outside new-object discovery.' : 'No supported archive comparison remains in the prepared evidence. Review the source as a possible new object.';
    }
    updatePairButton(); renderMetadataComparison(); drawAll(); return;
  }
  p.memberSelect.replaceChildren(...c.members.map((m, j) => el("option", {value: j}, `${j + 1}. ${m.path.split("/").pop()}`)));
  updatePages(i); loadImage(i); updatePairButton(); renderMetadataComparison();
}
function updatePages(i) {
  const p = panes[i], member = current.object.candidates.find(c => c.id === p.cid).members[p.member];
  p.pageInput.value = p.page; p.pageInput.max = member.pages; p.pageInput.disabled = member.pages === 1;
  const candidate = current.object.candidates.find(c => c.id === p.cid);
  p.identity.textContent = `${candidate.reference_object_id ? "Existing archive Master" : "Source file"} · ${member.path}`;
  updatePairButton();
  $("text-result")?.replaceChildren();
}
function mediaURL(p, route = "/media") {
  return route + "?" + new URLSearchParams({object: current.object.id, candidate: p.cid, member: p.member, page: p.page, edge: p.edge});
}
async function loadImage(i, edge = 1200) {
  const p = panes[i]; if (!p?.cid) return;
  const displayKey = `${current.object.id}/${p.cid}/${p.member}/${p.page}`;
  if (p.displayKey !== displayKey) {
    p.displayKey = displayKey; p.inverted = false; p.invert.checked = false;
  }
  p.edge = edge; p.image = null;
  const ticket = ++p.ticket, objectID = current.object.id, url = mediaURL(p);
  p.status.textContent = "Loading preview…"; drawAll();
  const img = new Image();
  img.onload = () => {
    if (ticket !== p.ticket || current.object.id !== objectID || panes[i] !== p) return;
    p.image = img; p.status.textContent = metadataView() ? "Image ready" : `${img.naturalWidth} × ${img.naturalHeight} preview pixels · page ${p.page}`; drawAll();
  };
  img.onerror = async () => {
    if (ticket !== p.ticket || current.object.id !== objectID || panes[i] !== p) return;
    p.status.textContent = "Preview unavailable";
    try {
      const response = await fetch(url);
      if (!response.ok) { const data = await response.json(); if (ticket === p.ticket && panes[i] === p) p.status.textContent = data.error; }
    } catch { /* Leave the visible unavailable message. */ }
  };
  img.src = url;
}
function setMode(value) {
  mode = value;
  document.querySelectorAll("[data-mode]").forEach(b => b.setAttribute("aria-pressed", String(b.dataset.mode === value)));
  $("combined").hidden = value === "side";
  $("comparison").querySelectorAll(".stage").forEach(s => s.hidden = value !== "side");
  $("opacity-label").hidden = value !== "overlay"; drawAll();
}
function resetView() { for (const p of panes) { p.zoom = 1; p.x = p.y = 0; } drawAll(); }
function changeZoom(factor) {
  for (const i of linked ? [0, 1] : [active]) { const p = panes[i]; if (p) p.zoom = Math.min(12, Math.max(.5, p.zoom * factor)); }
  drawAll();
}
function bindCanvas(canvas, index) {
  let drag = null;
  canvas.addEventListener("wheel", e => { e.preventDefault(); if (index < 2) active = index; changeZoom(e.deltaY < 0 ? 1.15 : 1 / 1.15); }, {passive: false});
  canvas.addEventListener("pointerdown", e => { if (index < 2) active = index; drag = {x: e.clientX, y: e.clientY}; canvas.setPointerCapture(e.pointerId); });
  canvas.addEventListener("pointermove", e => {
    if (!drag) return;
    for (const i of linked ? [0, 1] : [active]) {
      const p = panes[i]; if (!p) continue;
      p.x += (e.clientX - drag.x) / canvas.clientWidth; p.y += (e.clientY - drag.y) / canvas.clientHeight;
    }
    drag = {x: e.clientX, y: e.clientY}; drawAll();
  });
  for (const event of ["pointerup", "pointercancel"]) canvas.addEventListener(event, () => drag = null);
  canvas.addEventListener("dblclick", resetView);
}
function draw(canvas, index) {
  if (!canvas?.clientWidth || !canvas.clientHeight) return;
  const ratio = Math.min(devicePixelRatio || 1, 2), w = canvas.width = Math.round(canvas.clientWidth * ratio), h = canvas.height = Math.round(canvas.clientHeight * ratio);
  const ctx = canvas.getContext("2d"); ctx.fillStyle = "#e1e5dd"; ctx.fillRect(0, 0, w, h);
  const paint = (i, alpha = 1, difference = false) => {
    const p = panes[i]; if (!p?.image) return;
    const im = p.image, scale = Math.min(w / im.naturalWidth, h / im.naturalHeight) * p.zoom, iw = im.naturalWidth * scale, ih = im.naturalHeight * scale;
    ctx.globalAlpha = alpha; ctx.globalCompositeOperation = difference ? "difference" : "source-over";
    ctx.filter = p.inverted ? "invert(1)" : "none";
    ctx.drawImage(im, (w - iw) / 2 + p.x * w, (h - ih) / 2 + p.y * h, iw, ih);
    ctx.filter = "none";
  };
  if (index < 2) paint(index); else { paint(0); paint(1, mode === "overlay" ? opacity : 1, mode === "difference"); }
  ctx.globalAlpha = 1; ctx.globalCompositeOperation = "source-over";
}
function drawAll() { panes.forEach((p, i) => draw(p.canvas, i)); if (mode !== "side") draw(combined, 2); }
async function showText() {
  const displayed = panes.map((pane, index) => ({pane, index})).filter(x => x.pane.cid);
  const target = $("text-result"), signature = displayed.map(x => mediaURL(x.pane, "/text")).join("|");
  target.replaceChildren(el("p", {}, "Reading embedded text…"));
  try {
    const texts = await Promise.all(displayed.map(async ({pane}) => {
      const response = await fetch(mediaURL(pane, "/text"));
      if (!response.ok) throw Error((await response.json()).error);
      return response.text();
    }));
    if (signature !== panes.filter(p => p.cid).map(p => mediaURL(p, "/text")).join("|") || !target.isConnected) return;
    target.replaceChildren(...texts.map((t, i) => el("div", {}, el("strong", {}, displayed[i].index ? "B · Page text" : "A · Page text"), el("pre", {}, t || "No embedded text; OCR was not run."))));
  } catch (e) { target.replaceChildren(el("p", {class: "notice"}, e.message)); }
}
async function save() {
  if (saving || current.read_only) return;
  saving = true;
  let acknowledged = false;
  const controls = [...document.querySelectorAll("#review input,#review select,#review textarea,#review button")].map(n => [n, n.disabled]);
  controls.forEach(([n]) => n.disabled = true); $("save-status").textContent = "Saving…";
  try {
    const result = await api("/api/decision", {method: "POST", headers: {"Content-Type": "application/json", "X-Review-Token": overview.token}, body: JSON.stringify({
      object_id: current.object.id, revision: current.revision, selected_candidate_id: draft.selected_candidate_id,
      redundant_candidate_ids: draft.redundant_candidate_ids, different_pairs: draft.different_pairs, note: draft.note,
      metadata_reviewed: Boolean(draft.metadata_reviewed),
      same_pairs: draft.same_pairs, destination_unit: draft.destination_unit
    })});
    acknowledged = true;
    current = result; draft = structuredClone(result.decision); dirty = false;
    overview = await api("/api/overview"); renderObject(); renderQueue(); $("error").hidden = true;
    if (result.warning) error(Error(result.warning));
  } catch (e) {
    error(e);
    $("save-status").textContent = acknowledged ? "Decisions saved. View refresh failed; reload to refresh the page."
      : e.httpStatus ? `Save error: ${e.message}. Your draft is still open.`
      : "Save could not be confirmed. Reload to check the saved decisions.";
  }
  finally { controls.forEach(([n, disabled]) => { if (n.isConnected) n.disabled = disabled; }); saving = false; }
}
$("search").addEventListener("input", () => { queuePage = 0; if (overview) renderQueue(); });
$("filter").addEventListener("change", () => {
  if (saving || !confirmLeave()) { $("filter").value = new URLSearchParams(location.search).get("filter") || "all"; return; }
  queuePage = 0;
  if (overview) { renderQueue(); const id = visible()[0]?.id; if (id) navigate(id, false); }
});
$("reload").addEventListener("click", () => refresh());
window.addEventListener("resize", drawAll);
window.addEventListener("beforeunload", e => { if (dirty) { e.preventDefault(); e.returnValue = ""; } });
refresh(false);
