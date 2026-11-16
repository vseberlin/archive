#!/usr/bin/env python3
"""Serve one immutable provenance draft for bounded local review."""

from __future__ import annotations

import argparse
import html
import json
import secrets
import sys
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlsplit

if __package__:
    from . import accession_plan as accession_plan_contract
    from . import bundle as bundle_contract
    from .evidence import file_digest
    from .review import (
        REVIEW_FORMAT,
        REVIEW_VERSION,
        ProvenanceReviewError,
        export_reviewed_bundle,
    )
    from .safety import ProtectedEvidenceError, require_safe_output
    from .technical import TechnicalVerificationError
else:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import accession_plan as accession_plan_contract
    import bundle as bundle_contract
    from evidence import file_digest
    from review import (
        REVIEW_FORMAT,
        REVIEW_VERSION,
        ProvenanceReviewError,
        export_reviewed_bundle,
    )
    from safety import ProtectedEvidenceError, require_safe_output
    from technical import TechnicalVerificationError


METADATA_FIELDS = (
    (
        "descriptive_title",
        "Title",
        "title",
        "object_name",
        "objects[0].object_name",
    ),
    (
        "description",
        "Description",
        "description",
        "object_description",
        "objects[0].object_description",
    ),
    (
        "object_type",
        "Object type",
        "object_type",
        "object_type",
        "objects[0].object_type",
    ),
    (
        "material_technique",
        "Material / technique",
        "material_technique",
        "material_technique",
        "objects[0].raw_json.object_material_technique",
    ),
    (
        "dimensions",
        "Dimensions",
        "dimensions",
        "dimensions",
        "objects[0].raw_json.object_dimensions",
    ),
    (
        "creator",
        "Creator / photographer",
        "creator",
        "creator",
        "images[0].creator",
    ),
    (
        "rights_statement",
        "Image rights statement",
        "rights_statement",
        "rights_statement",
        "images[0].rights",
    ),
    (
        "rights_holder",
        "Rights holder",
        "rights_holder",
        "rights_holder",
        "images[0].owner",
    ),
    (
        "credit_line",
        "Credit line",
        "credit_line",
        "credit_line",
        "",
    ),
)
CSS_PATH = Path(__file__).resolve().parents[2] / "workbench.css"
WORKBENCH_VERSION = "0.2.0"


class WorkbenchInputError(ValueError):
    """Raised when a submitted review form is incomplete."""


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace(
        "+00:00",
        "Z",
    )


def _first(form: dict[str, list[str]], key: str) -> str:
    values = form.get(key, [])
    return values[0].strip() if values else ""


def _embedded_rows(
    draft: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], str, str]:
    local_source = next(
        (
            source
            for source in draft["sources"]
            if source["kind"] == "local_catalog_row"
        ),
        None,
    )
    md_source = next(
        (
            source
            for source in draft["sources"]
            if source["kind"] == "museum_digital_snapshot"
        ),
        None,
    )
    local_rows = (
        local_source.get("snapshot", {}).get("objects", [])
        if local_source
        else []
    )
    md_snapshot = md_source.get("snapshot", {}) if md_source else {}
    md_rows = (
        md_snapshot.get("objects", [])
        if md_source
        else []
    )
    md_row = dict(md_rows[0]) if md_rows else {}
    try:
        md_raw = json.loads(str(md_row.get("raw_json") or "{}"))
    except json.JSONDecodeError:
        md_raw = {}
    if isinstance(md_raw, dict):
        md_row["material_technique"] = str(
            md_raw.get("object_material_technique") or ""
        )
        md_row["dimensions"] = str(
            md_raw.get("object_dimensions") or ""
        )
    md_images = md_snapshot.get("images", [])
    md_image = md_images[0] if md_images else {}
    if isinstance(md_image, dict):
        md_row["creator"] = str(md_image.get("creator") or "")
        md_row["rights_statement"] = str(
            md_image.get("rights") or ""
        )
        md_row["rights_holder"] = str(md_image.get("owner") or "")
    md_row["credit_line"] = ""
    return (
        local_rows[0] if local_rows else {},
        md_row,
        str(local_source["source_id"]) if local_source else "",
        str(md_source["source_id"]) if md_source else "",
    )


def decision_from_form(
    form: dict[str, list[str]],
    draft: dict[str, Any],
    decided_at: str | None = None,
) -> dict[str, Any]:
    """Convert one explicit browser form into the frozen review document."""

    if _first(form, "confirm_export") != "yes":
        raise WorkbenchInputError(
            "Confirm that export creates a new non-replaceable review."
        )
    actor = _first(form, "actor")
    if not actor:
        raise WorkbenchInputError("Reviewer name is required.")
    decided_at = decided_at or utc_now()
    local, md, local_source_id, md_source_id = _embedded_rows(draft)

    identity_outcome = _first(form, "identity_outcome")
    if identity_outcome not in {
        "same_entity",
        "component_of",
        "not_same",
        "defer",
    }:
        raise WorkbenchInputError("Choose one identity outcome.")
    identity_reason = _first(form, "identity_reason")

    metadata_decisions: list[dict[str, Any]] = []
    for (
        field,
        _label,
        local_key,
        md_key,
        md_locator,
    ) in METADATA_FIELDS:
        choice = _first(form, f"metadata_{field}_choice") or "omit"
        if choice == "omit":
            continue
        reason = _first(form, f"metadata_{field}_reason")
        value = ""
        action = ""
        source_id = ""
        source_locator = ""
        if choice == "local":
            value = str(local.get(local_key) or "")
            action = "accept_local"
            source_id = local_source_id
            source_locator = f"objects[0].{local_key}"
        elif choice == "museum_digital":
            value = str(md.get(md_key) or "")
            action = "accept_museum_digital"
            source_id = md_source_id
            source_locator = md_locator
        elif choice == "corrected":
            value = _first(form, f"metadata_{field}_corrected")
            action = "corrected"
        elif choice == "defer":
            action = "defer"
        else:
            raise WorkbenchInputError(
                f"Unsupported metadata choice for {field}."
            )
        if action != "defer" and not value:
            raise WorkbenchInputError(
                f"Selected {field} value is empty."
            )
        if action == "accept_local" and not source_id:
            raise WorkbenchInputError("Local evidence snapshot is unavailable.")
        if action == "accept_museum_digital" and not source_id:
            raise WorkbenchInputError("MD evidence snapshot is unavailable.")
        metadata_decisions.append(
            {
                "field": field,
                "value": value,
                "action": action,
                "source_id": source_id,
                "source_locator": source_locator,
                "reason": reason,
                "actor": actor,
                "decided_at": decided_at,
            }
        )

    file_outcome = _first(form, "file_outcome")
    if file_outcome not in {
        "verified_local_master",
        "best_available_surrogate",
        "missing_master",
        "defer",
    }:
        raise WorkbenchInputError("Choose one file outcome.")
    selected_file_id = _first(form, "selected_file_id")
    if file_outcome in {
        "verified_local_master",
        "best_available_surrogate",
    } and not selected_file_id:
        raise WorkbenchInputError("Choose the reviewed source file.")
    if file_outcome in {"missing_master", "defer"}:
        selected_file_id = ""
    if (
        file_outcome == "verified_local_master"
        and _first(form, "technical_acknowledged") != "yes"
    ):
        raise WorkbenchInputError(
            "Acknowledge the read-only TIFF technical verification."
        )
    file_reason = _first(form, "file_reason")

    return {
        "format": REVIEW_FORMAT,
        "version": REVIEW_VERSION,
        "case_id": draft["case_id"],
        "draft_bundle_digest": bundle_contract.bundle_digest(draft),
        "reviewed_at": decided_at,
        "identity": {
            "outcome": identity_outcome,
            "reason": identity_reason,
            "actor": actor,
            "decided_at": decided_at,
            "evidence_source_ids": [
                source["source_id"]
                for source in draft["sources"]
            ],
        },
        "metadata_decisions": metadata_decisions,
        "file_decision": {
            "outcome": file_outcome,
            "selected_file_id": selected_file_id,
            "reason": file_reason,
            "actor": actor,
            "decided_at": decided_at,
        },
    }


def _value(value: Any) -> str:
    if value in (None, ""):
        return "—"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)


def _badge(value: Any) -> str:
    text = str(value or "unknown")
    css = text.replace("_", "-")
    return (
        f'<span class="badge badge--{html.escape(css, quote=True)}">'
        f"{html.escape(text.replace('_', ' '))}</span>"
    )


def _evidence_table(title: str, row: dict[str, Any]) -> str:
    body = "".join(
        "<tr><th>"
        + html.escape(str(key))
        + "</th><td>"
        + html.escape(_value(value))
        + "</td></tr>"
        for key, value in row.items()
    )
    return (
        f"<section class='panel'><h2>{html.escape(title)}</h2>"
        f"<div class='table-wrap'><table>{body}</table></div></section>"
    )


def _visual_cards(draft: dict[str, Any]) -> str:
    comparisons = [
        file
        for file in draft["files"]
        if file["role"]
        in {"local_derivative", "museum_digital_derivative"}
    ]
    return "".join(
        f"""<article class="visual-card">
<header><p class="eyebrow">{html.escape(str(file["role"]).replace("_", " "))}</p>
<h3>{html.escape(str(file["file_id"]))}</h3></header>
<a class="visual-card__image" href="/evidence?file_id={quote(str(file["file_id"]), safe="")}">
<img loading="lazy" src="/evidence?file_id={quote(str(file["file_id"]), safe="")}"
 alt="{html.escape(str(file["role"]), quote=True)}"></a>
<div class="visual-card__body"><details><summary>Technical evidence</summary>
<p class="technical">{html.escape(str(file["content_digest"]))}<br>
{html.escape(str(file["locator"]))}</p></details></div></article>"""
        for file in comparisons
    )


def _shell(
    draft: dict[str, Any],
    body: str,
    *,
    reviewed: dict[str, Any] | None,
) -> str:
    actor = (
        str(reviewed["identity"]["actor"])
        if reviewed
        else "review not exported"
    )
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(str(draft["case_id"]))} · ISS Archive</title>
<link rel="stylesheet" href="/assets/workbench.css">
</head>
<body>
<header class="site-header">
  <a class="brand" href="/">ISS <span>Archive</span></a>
  <span class="actor">Case: {html.escape(str(draft["inventory_label"]))} · {html.escape(actor)}</span>
</header>
<nav class="nav" aria-label="Case review">
  <a class="nav__link is-active" href="#overview">Overview</a>
  <a class="nav__link" href="#visual-evidence">Visual evidence</a>
  <a class="nav__link" href="#decisions">Decisions</a>
  <a class="nav__link" href="#accession-plan">Accession plan</a>
  <a class="nav__link" href="#technical-evidence">Technical evidence</a>
</nav>
<main class="main">{body}</main>
<footer class="footer">Provenance restoration workbench {WORKBENCH_VERSION} · localhost only · source evidence remains read-only</footer>
</body></html>"""


def _reviewed_body(
    draft: dict[str, Any],
    reviewed: dict[str, Any],
    plan: dict[str, Any] | None,
    *,
    notice: str,
    error: str,
) -> str:
    messages = ""
    if notice:
        messages += (
            f'<div class="notice notice--safe">{html.escape(notice)}</div>'
        )
    if error:
        messages += (
            f'<div class="notice notice--critical">{html.escape(error)}</div>'
        )
    requirements = plan["implementation_requirements"] if plan else []
    plan_state = plan["plan_state"] if plan else "not created"
    plan_class = "safe" if plan else "yellow"
    metadata_rows = "".join(
        "<tr>"
        f"<th>{html.escape(str(item['field']).replace('_', ' '))}</th>"
        f"<td>{html.escape(str(item['value']))}</td>"
        f"<td>{html.escape(str(item['action']).replace('_', ' '))}</td>"
        f"<td class='technical'>{html.escape(str(item['source_id']))}<br>"
        f"{html.escape(str(item['source_locator']))}</td>"
        "</tr>"
        for item in reviewed["metadata_decisions"]
    )
    selected = next(
        file
        for file in reviewed["files"]
        if file["file_id"] == reviewed["readiness"]["selected_file_id"]
    )
    gate_rows = "".join(
        "<li><strong>"
        + html.escape(str(gate["key"]).replace("_", " "))
        + "</strong><span>"
        + html.escape(str(gate["detail"]))
        + "</span></li>"
        for gate in requirements
    )
    operations = ""
    proposed = ""
    plan_digest_text = ""
    if plan:
        operations = "".join(
            "<tr>"
            f"<td>{operation['sequence']}</td>"
            f"<td><code>{html.escape(str(operation['operation']))}</code></td>"
            f"<td>{_badge(operation['state'])}</td>"
            "</tr>"
            for operation in plan["proposed_catalog_operations"]
        )
        proposed = (
            "<dl>"
            "<dt>Primary and independent relative path</dt>"
            f"<dd class='technical'>{html.escape(str(plan['proposed_archive']['primary_relative_path_template']))}</dd>"
            "<dt>Master operation</dt>"
            f"<dd>{html.escape(str(plan['proposed_archive']['master_operation']))}</dd>"
            "<dt>Embedding profile</dt>"
            f"<dd>{html.escape(str(plan['proposed_archive']['embedding_profile']))}</dd>"
            "</dl>"
        )
        plan_digest_text = accession_plan_contract.plan_digest(plan)
    raw_evidence = json.dumps(
        {
            "reviewed_lineage": reviewed["lineage"],
            "sources": reviewed["sources"],
            "files": reviewed["files"],
            "file_decision": reviewed["file_decision"],
            "plan_bindings": plan["bindings"] if plan else {},
        },
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )
    return f"""
<section class="page-heading" id="overview">
  <p class="eyebrow">One-way provenance restoration</p>
  <h1>{html.escape(str(draft["inventory_label"]))}</h1>
  <p>The human review is frozen. This page presents the evidence and the dry-run
  accession boundary; it performs no archive or catalog write.</p>
</section>
{messages}
<section class="health-banner health-banner--{plan_class}">
  <div><p class="eyebrow">Dry-run execution</p><h2>{html.escape(plan_state.replace("_", " ").title())}</h2></div>
  <div>{_badge(reviewed["readiness"]["state"])} {_badge(reviewed["identity"]["outcome"])}</div>
</section>
<section class="stats">
  <div><strong>{html.escape(str(len(reviewed["metadata_decisions"])))}</strong><span>accepted metadata fields</span></div>
  <div><strong>{html.escape(str(len(reviewed["sources"])))}</strong><span>bound evidence sources</span></div>
  <div><strong>{html.escape(str(len(reviewed["files"])))}</strong><span>revalidated evidence files</span></div>
  <div><strong>{html.escape(str(len(requirements)))}</strong><span>implementation requirements</span></div>
</section>
<section class="visual-case" id="visual-evidence">
  <div class="visual-case__heading"><div><p class="eyebrow">Identity evidence</p>
  <h2>Captured visual comparison</h2>
  <p>Both images are local captured evidence and are rehashed before every response.</p></div>
  {_badge(reviewed["identity"]["outcome"])}</div>
  <div class="visual-strip">{_visual_cards(reviewed) or '<div class="empty">No captured comparison pair.</div>'}</div>
</section>
<section id="decisions">
  <div class="section-heading"><h2>Accepted decisions</h2>
  <p>Decisions remain traceable to exact evidence. Human comments are optional and are not used as gates.</p></div>
  <div class="two-column two-column--detail">
    <section class="panel"><h2>Identity</h2>
      <dl><dt>Outcome</dt><dd>{_badge(reviewed["identity"]["outcome"])}</dd>
      <dt>Reviewer</dt><dd>{html.escape(str(reviewed["identity"]["actor"]))}</dd>
      <dt>Decided</dt><dd>{html.escape(str(reviewed["identity"]["decided_at"]))}</dd></dl>
    </section>
    <section class="panel"><h2>Selected preservation source</h2>
      <dl><dt>File</dt><dd>{html.escape(str(selected["file_id"]))}</dd>
      <dt>Role</dt><dd>{_badge(selected["role"])}</dd>
      <dt>Bytes</dt><dd>{html.escape(str(selected["size_bytes"]))}</dd>
      <dt>SHA-256</dt><dd class="technical">{html.escape(str(selected["content_digest"]))}</dd></dl>
    </section>
  </div>
  <section class="field-decision"><h3>Canonical metadata</h3>
    <div class="table-wrap"><table><thead><tr><th>Field</th><th>Accepted value</th><th>Decision</th><th>Exact source</th></tr></thead>
    <tbody>{metadata_rows}</tbody></table></div>
  </section>
</section>
<section id="accession-plan">
  <div class="section-heading"><h2>Dry-run accession plan</h2>
  <p>The plan is deterministic, bound to the exact parent, capture, review, and evidence digests, and authorizes no writes or repeated curatorial review.</p></div>
  {(
      '<div class="notice notice--yellow"><strong>No plan loaded.</strong><p>Create the bounded dry-run plan before accession implementation.</p></div>'
      if not plan
      else f'<div class="notice notice--safe"><strong>The completed review stands.</strong><p>{len(requirements)} engineering requirement(s) are carried into implementation; no second curatorial review is requested.</p></div>'
  )}
  {f'<section class="panel"><h3>Implementation requirements</h3><ul class="condition-list">{gate_rows}</ul></section>' if requirements else ''}
  {f'<div class="two-column"><section class="panel"><h3>Proposed filesystem</h3>{proposed}</section><section class="panel"><h3>Plan binding</h3><p class="technical">{html.escape(plan_digest_text)}</p><p>Archive writes: <strong>no</strong><br>Catalog writes: <strong>no</strong><br>Repeat curatorial review: <strong>no</strong><br>Guarded write command: <strong>required</strong></p></section></div>' if plan else ''}
  {f'<section class="panel"><h3>Proposed operation order</h3><div class="table-wrap"><table><thead><tr><th>Step</th><th>Guarded operation</th><th>State</th></tr></thead><tbody>{operations}</tbody></table></div></section>' if plan else ''}
</section>
<details class="panel" id="technical-evidence"><summary>Exact technical evidence and lineage</summary>
<pre class="technical">{html.escape(raw_evidence)}</pre></details>
"""


def evidence_file(
    draft: dict[str, Any],
    file_id: str,
) -> tuple[Path, str]:
    """Resolve and revalidate one captured comparison image."""

    matches = [
        file
        for file in draft["files"]
        if file["file_id"] == file_id
        and file["role"]
        in {"local_derivative", "museum_digital_derivative"}
    ]
    if len(matches) != 1:
        raise WorkbenchInputError("comparison image is unavailable")
    file = matches[0]
    path = Path(str(file["locator"]))
    if (
        not path.is_absolute()
        or path.is_symlink()
        or not path.is_file()
    ):
        raise WorkbenchInputError(
            "comparison image path is unavailable"
        )
    if path.stat().st_size != file["size_bytes"]:
        raise WorkbenchInputError(
            "comparison image size no longer matches the draft"
        )
    if file_digest(path) != file["content_digest"]:
        raise WorkbenchInputError(
            "comparison image digest no longer matches the draft"
        )
    return path, str(file["media_type"])


def render_page(
    draft: dict[str, Any],
    output_path: Path,
    csrf_token: str,
    *,
    reviewed: dict[str, Any] | None = None,
    plan: dict[str, Any] | None = None,
    notice: str = "",
    error: str = "",
    submitted_form: dict[str, list[str]] | None = None,
) -> str:
    """Render only embedded evidence; never query SQLite or museum-digital."""

    if reviewed is not None:
        return _shell(
            draft,
            _reviewed_body(
                draft,
                reviewed,
                plan,
                notice=notice,
                error=error,
            ),
            reviewed=reviewed,
        )

    submitted_form = submitted_form or {}

    def submitted(name: str) -> str:
        return _first(submitted_form, name)

    def selected(name: str, value: str) -> str:
        return " selected" if submitted(name) == value else ""

    def checked(name: str, value: str = "yes") -> str:
        return " checked" if submitted(name) == value else ""

    local, md, _local_source_id, _md_source_id = _embedded_rows(draft)
    digest = bundle_contract.bundle_digest(draft)
    rows = []
    for file in draft["files"]:
        rows.append(
            "<tr>"
            f"<td>{html.escape(str(file['file_id']))}</td>"
            f"<td>{html.escape(str(file['role']))}</td>"
            f"<td>{html.escape(str(file['size_bytes']))}</td>"
            f"<td><code>{html.escape(str(file['content_digest']))}</code></td>"
            f"<td><code>{html.escape(str(file['locator']))}</code></td>"
            "</tr>"
        )
    file_options = "".join(
        "<option value='"
        + html.escape(str(file["file_id"]), quote=True)
        + "'"
        + selected("selected_file_id", str(file["file_id"]))
        + ">"
        + html.escape(f"{file['file_id']} — {file['role']}")
        + "</option>"
        for file in draft["files"]
    )
    roles = {str(file["role"]) for file in draft["files"]}
    positive_identity_available = (
        "museum_digital_derivative" in roles
        and bool(
            roles
            & {
                "local_master",
                "local_master_candidate",
                "local_derivative",
            }
        )
    )
    positive_disabled = "" if positive_identity_available else " disabled"
    identity_warning = (
        ""
        if positive_identity_available
        else (
            "<p class='error'>Positive identity is disabled: this draft has "
            "cached MD image metadata but no captured, hashed MD derivative "
            "bytes. Defer or reject until that evidence is captured in a new "
            "draft.</p>"
        )
    )
    metadata_sections = []
    for (
        field,
        label,
        local_key,
        md_key,
        _md_locator,
    ) in METADATA_FIELDS:
        local_raw = str(local.get(local_key) or "")
        md_raw = str(md.get(md_key) or "")
        local_value = _value(local_raw)
        md_value = _value(md_raw)
        local_disabled = "" if local_raw else " disabled"
        md_disabled = "" if md_raw else " disabled"
        metadata_sections.append(
            f"""
            <section class="field-decision">
              <h3>{html.escape(label)}</h3>
              <div class="table-wrap"><table><thead><tr><th>Source</th><th>Value</th></tr></thead>
              <tbody><tr><th>Local</th><td>{html.escape(local_value)}</td></tr>
              <tr><th>museum-digital</th><td>{html.escape(md_value)}</td></tr></tbody></table></div>
              <label>Choice
                <select name="metadata_{html.escape(field)}_choice">
                  <option value="omit"{selected(f"metadata_{field}_choice", "omit")}>No decision in this review</option>
                  <option value="local"{selected(f"metadata_{field}_choice", "local")}{local_disabled}>Accept local</option>
                  <option value="museum_digital"{selected(f"metadata_{field}_choice", "museum_digital")}{md_disabled}>Accept museum-digital</option>
                  <option value="corrected"{selected(f"metadata_{field}_choice", "corrected")}>Enter corrected value</option>
                  <option value="defer"{selected(f"metadata_{field}_choice", "defer")}>Defer explicitly</option>
                </select>
              </label>
              <label>Corrected value
                <textarea name="metadata_{html.escape(field)}_corrected">{html.escape(submitted(f"metadata_{field}_corrected"))}</textarea>
              </label>
              <label>Comment (optional)
                <textarea name="metadata_{html.escape(field)}_reason">{html.escape(submitted(f"metadata_{field}_reason"))}</textarea>
              </label>
            </section>
            """
        )

    messages = ""
    if notice:
        messages += f"<p class='notice'>{html.escape(notice)}</p>"
    if error:
        messages += (
            "<section class='notice notice--error' role='alert'>"
            "<h2>Review was not exported</h2>"
            f"<p>{html.escape(error)}</p>"
            "<p>Your submitted choices remain filled in below. Correct the "
            "indicated field and export again.</p></section>"
        )
    output_state = (
        "already exists; restart with a new output path"
        if output_path.exists()
        else "not created"
    )
    raw_evidence = json.dumps(
        {
            "sources": draft["sources"],
            "files": draft["files"],
        },
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )
    body = f"""
<section class="page-heading" id="overview">
<p class="eyebrow">One-way provenance restoration</p>
<h1>{html.escape(str(draft["inventory_label"]))}</h1>
<p>Resolve identity first, then metadata and the preservation source. The export is a new immutable review; comments are optional.</p>
</section>
{messages}
<section class="health-banner health-banner--yellow">
  <div><p class="eyebrow">Review state</p><h2>Draft</h2></div>
  <div>{_badge(draft["readiness"]["state"])}</div>
</section>
<section class="panel">
  <p><b>Case:</b> {html.escape(str(draft["case_id"]))}</p>
  <p><b>Draft digest:</b> <code>{html.escape(digest)}</code></p>
  <p><b>Output:</b> <code>{html.escape(str(output_path))}</code>
     ({html.escape(output_state)})</p>
  <p>This page uses only the embedded draft snapshots. It does not query the
     retained SQLite index or museum-digital.</p>
</section>
<section class="visual-case" id="visual-evidence">
<div class="visual-case__heading"><div><p class="eyebrow">Step 1</p><h2>Captured visual comparison</h2>
<p>Images are served from captured paths and rehashed before every response. No remote image is loaded.</p></div></div>
<div class="visual-strip">{_visual_cards(draft) or '<div class="empty">No captured comparison pair.</div>'}</div>
</section>
<form class="form" method="post" action="/export" id="decisions">
<input type="hidden" name="csrf_token" value="{html.escape(csrf_token, quote=True)}">
<section class="decision-step">
<h2>Reviewer</h2>
<label>Name <input name="actor" value="{html.escape(submitted("actor"), quote=True)}" required></label>
</section>
<section class="decision-step">
<p class="eyebrow">Step 2</p><h2>Identity</h2>
{identity_warning}
<label>Outcome
<select name="identity_outcome" required>
  <option value="">Choose…</option>
  <option value="same_entity"{selected("identity_outcome", "same_entity")}{positive_disabled}>Same entity</option>
  <option value="component_of"{selected("identity_outcome", "component_of")}{positive_disabled}>Component of the local carrier</option>
  <option value="not_same"{selected("identity_outcome", "not_same")}>Not the same</option>
  <option value="defer"{selected("identity_outcome", "defer")}>Defer</option>
</select></label>
<label>Comment (optional) <textarea name="identity_reason">{html.escape(submitted("identity_reason"))}</textarea></label>
</section>
<section>
<div class="section-heading"><p class="eyebrow">Step 3</p><h2>Metadata</h2>
<p>Choose a source field by field. Leaving a field undecided is explicit and visible.</p></div>
{''.join(metadata_sections)}
</section>
<section class="decision-step">
<p class="eyebrow">Step 4</p><h2>Preservation file</h2>
<label>Outcome
<select name="file_outcome" required>
  <option value="">Choose…</option>
  <option value="verified_local_master"{selected("file_outcome", "verified_local_master")}>Verify selected TIFF as local master</option>
  <option value="best_available_surrogate"{selected("file_outcome", "best_available_surrogate")}>Select derivative as best available surrogate</option>
  <option value="missing_master"{selected("file_outcome", "missing_master")}>No usable master or surrogate</option>
  <option value="defer"{selected("file_outcome", "defer")}>Defer file decision</option>
</select></label>
<label>Source file <select name="selected_file_id">
  <option value=""{selected("selected_file_id", "")}>None</option>{file_options}
</select></label>
<label><input type="checkbox" name="technical_acknowledged" value="yes"{checked("technical_acknowledged")}>
  Run and acknowledge SHA-256/stat, ExifTool, and ImageMagick read-only
  verification before promoting a TIFF.</label>
<label>Comment (optional) <textarea name="file_reason">{html.escape(submitted("file_reason"))}</textarea></label>
</section>
<section class="decision-step" id="accession-plan">
<p class="eyebrow">Step 5</p><h2>Export review</h2>
<p>Export creates one new reviewed bundle. It cannot replace this draft or an
existing reviewed bundle, and it performs no archive ingest.</p>
<label><input type="checkbox" name="confirm_export" value="yes"{checked("confirm_export")} required>
  I confirm these decisions and the non-replaceable export.</label>
<button class="button" type="submit">Validate evidence and export reviewed bundle</button>
</section>
</form>
<details class="panel" id="technical-evidence"><summary>Exact embedded evidence</summary>
<div class="two-column two-column--detail">
{_evidence_table("Local catalog object", local)}
{_evidence_table("Cached museum-digital object", md)}
</div>
<h3>Linked source files</h3>
<div class="table-wrap"><table><thead><tr><th>ID</th><th>Role</th><th>Bytes</th><th>SHA-256</th><th>Path</th></tr></thead>
<tbody>{''.join(rows)}</tbody></table></div>
<pre class="technical">{html.escape(raw_evidence)}</pre></details>
"""
    return _shell(draft, body, reviewed=None)


class ReviewApplication:
    def __init__(
        self,
        draft_path: Path,
        output_path: Path,
        plan_path: Path | None = None,
    ) -> None:
        self.draft_path = draft_path.expanduser().resolve()
        self.output_path = require_safe_output(output_path)
        self.draft = bundle_contract.read_bundle(self.draft_path)
        if self.draft["lineage"]["state"] != "draft":
            raise WorkbenchInputError("workbench requires one draft bundle")
        self.reviewed = (
            bundle_contract.read_bundle(self.output_path)
            if self.output_path.exists()
            else None
        )
        if self.reviewed is not None and (
            self.reviewed["case_id"] != self.draft["case_id"]
            or self.reviewed["lineage"]["draft_bundle_digest"]
            != bundle_contract.bundle_digest(self.draft)
        ):
            raise WorkbenchInputError(
                "existing reviewed output is not bound to this draft"
            )
        self.plan_path = (
            plan_path.expanduser().resolve()
            if plan_path is not None
            else None
        )
        self.plan = (
            accession_plan_contract.read_accession_plan(self.plan_path)
            if self.plan_path is not None
            else None
        )
        if self.plan is not None:
            if self.reviewed is None:
                raise WorkbenchInputError(
                    "accession plan requires an existing reviewed bundle"
                )
            reviewed_binding = self.plan["bindings"]["reviewed_bundle"]
            if (
                self.plan["case_id"] != self.reviewed["case_id"]
                or reviewed_binding["bundle_digest"]
                != bundle_contract.bundle_digest(self.reviewed)
                or reviewed_binding["file_digest"]
                != file_digest(self.output_path)
            ):
                raise WorkbenchInputError(
                    "accession plan is not bound to the current reviewed bundle"
                )
        self.csrf_token = secrets.token_urlsafe(32)
        self.notice = ""

    def export(self, form: dict[str, list[str]]) -> str:
        if _first(form, "csrf_token") != self.csrf_token:
            raise WorkbenchInputError("Review session token is invalid.")
        decision = decision_from_form(form, self.draft)
        reviewed, digest = export_reviewed_bundle(
            self.draft_path,
            self.output_path,
            decision,
        )
        self.notice = (
            f"Reviewed bundle exported: {digest}; "
            f"readiness={reviewed['readiness']['state']}."
        )
        self.reviewed = reviewed
        return self.notice


def _handler(application: ReviewApplication) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "ISSProvenanceReview/1"

        def _send(
            self,
            status: int,
            body: str,
            content_type: str = "text/html; charset=utf-8",
        ) -> None:
            payload = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'none'; style-src 'self'; "
                "img-src 'self'; form-action 'self'; base-uri 'none'; "
                "frame-ancestors 'none'",
            )
            self.end_headers()
            self.wfile.write(payload)

        def _send_file(self, path: Path, media_type: str) -> None:
            payload = path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", media_type)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Disposition", "inline")
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self) -> None:
            path = urlsplit(self.path).path
            if path == "/health":
                self._send(200, "ok\n", "text/plain; charset=utf-8")
                return
            if path == "/assets/workbench.css":
                self._send_file(CSS_PATH, "text/css; charset=utf-8")
                return
            if path == "/evidence":
                parameters = parse_qs(urlsplit(self.path).query)
                try:
                    evidence_path, media_type = evidence_file(
                        application.draft,
                        _first(parameters, "file_id"),
                    )
                    self._send_file(evidence_path, media_type)
                except (WorkbenchInputError, OSError) as error:
                    self._send(
                        404,
                        str(error) + "\n",
                        "text/plain; charset=utf-8",
                    )
                return
            if path != "/":
                self._send(404, "Not found\n", "text/plain; charset=utf-8")
                return
            self._send(
                200,
                render_page(
                    application.draft,
                    application.output_path,
                    application.csrf_token,
                    reviewed=application.reviewed,
                    plan=application.plan,
                    notice=application.notice,
                ),
            )

        def do_POST(self) -> None:
            if urlsplit(self.path).path != "/export":
                self._send(404, "Not found\n", "text/plain; charset=utf-8")
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = 0
            if length <= 0 or length > 131072:
                self._send(400, "Invalid form size\n", "text/plain; charset=utf-8")
                return
            form = parse_qs(
                self.rfile.read(length).decode("utf-8"),
                keep_blank_values=True,
            )
            try:
                application.export(form)
            except (
                WorkbenchInputError,
                ProvenanceReviewError,
                ProtectedEvidenceError,
                TechnicalVerificationError,
                bundle_contract.ProvenanceBundleError,
                OSError,
            ) as error:
                print(
                    "provenance-review export rejected: "
                    f"{type(error).__name__}: {error}",
                    file=sys.stderr,
                    flush=True,
                )
                self._send(
                    400,
                    render_page(
                        application.draft,
                        application.output_path,
                        application.csrf_token,
                        reviewed=application.reviewed,
                        plan=application.plan,
                        error=str(error),
                        submitted_form=form,
                    ),
                )
                return
            self.send_response(303)
            self.send_header("Location", "/")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()

        def log_message(self, format: str, *args: Any) -> None:
            print(
                f"provenance-review {self.client_address[0]} "
                + (format % args),
                file=sys.stderr,
            )

    return Handler


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--draft", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--plan", type=Path)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8770)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    if arguments.host not in {"127.0.0.1", "::1", "localhost"}:
        print("review workbench must bind to loopback", file=sys.stderr)
        return 3
    try:
        application = ReviewApplication(
            arguments.draft,
            arguments.output,
            arguments.plan,
        )
        server = ThreadingHTTPServer(
            (arguments.host, arguments.port),
            _handler(application),
        )
    except (
        WorkbenchInputError,
        accession_plan_contract.AccessionPlanError,
        ProtectedEvidenceError,
        bundle_contract.ProvenanceBundleError,
        OSError,
    ) as error:
        print(str(error), file=sys.stderr)
        return 3
    print(
        f"provenance review: http://{arguments.host}:{arguments.port}/ "
        f"case={application.draft['case_id']} "
        f"output={application.output_path}",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
