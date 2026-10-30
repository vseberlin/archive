#!/usr/bin/env python3
"""Run the existing Kurt workbench with format-neutral Master-review wording."""

from __future__ import annotations

import csv
import re
import sys
from pathlib import Path


TOOLS = Path("/home/vladimir/wp/tools/archive-preservation")
sys.path.insert(0, str(TOOLS))

import core_workbench  # noqa: E402


_send_html = core_workbench.CoreWorkbenchHandler.send_html
_next_pending_object = core_workbench.next_pending_kurt_object_census
REVIEW_CSV = Path(
    "/mnt/volume/iss-archive-work/kurt-schwarz/work/reconciliation/"
    "unit-reconciliation-v1/lowres-master-review.csv"
)


def review_rows() -> dict[str, dict[str, str]]:
    with REVIEW_CSV.open(newline="", encoding="utf-8") as handle:
        return {row["candidate_id"]: row for row in csv.DictReader(handle)}


def review_candidate_ids() -> set[str]:
    return set(review_rows())


def next_pending_review_candidate(
    candidates: list[dict[str, object]], current_candidate_id: str
) -> str:
    """Keep post-decision navigation inside this bounded review set."""
    candidate_ids = review_candidate_ids()
    scoped = [
        candidate
        for candidate in candidates
        if candidate["candidate_id"] in candidate_ids
    ]
    return _next_pending_object(scoped, current_candidate_id)


core_workbench.next_pending_kurt_object_census = next_pending_review_candidate


def send_html(self, title: str, body: str, *, active: str = "", status: int = 200):
    if title == "Kurt Schwarz object census":
        candidate_ids = review_candidate_ids()
        visible_count = 0

        def keep_review_card(match: re.Match[str]) -> str:
            nonlocal visible_count
            card = match.group(0)
            if any(value in card for value in candidate_ids):
                visible_count += 1
                return card
            return ""

        body = re.sub(
            r'<article class="object-census-card">.*?</article>',
            keep_review_card,
            body,
            flags=re.DOTALL,
        )
        body = re.sub(
            r"Showing \d+ of \d+ matching candidates",
            f"Showing {visible_count} of {visible_count} matching candidates",
            body,
        )
    if title.startswith("Kurt Schwarz census "):
        candidate_id = self.parameters().get("id", "")
        row = review_rows().get(candidate_id)
        if row:
            item = self.kurt_schwarz().object_census_candidate(candidate_id)
            metadata = item.get("selected_metadata") or {}
            e = core_workbench.e
            url = core_workbench.url
            metadata_lines = "".join(
                f"<div><dt>{e(label)}</dt><dd>{e(metadata.get(key, ''))}</dd></div>"
                for key, label in (
                    ("title", "MD title"),
                    ("date_text", "Date"),
                    ("material", "Material"),
                    ("dimensions", "Physical dimensions"),
                )
                if metadata.get(key)
            )
            old_comparison = re.compile(
                r'<section class="object-census-detail">\s*'
                r'<div class="panel object-census-detail__image"><h2>Preservation candidate</h2>.*?'
                r'</section>',
                flags=re.DOTALL,
            )
            comparison = f"""<section class="object-census-detail">
<div class="panel object-census-detail__image"><h2>Higher-resolution Master candidate</h2>
<img src="{url('/kurt-schwarz/object-census-image', id=candidate_id)}" alt="{e(item['filename'])}">
<dl class="facts"><div><dt>File</dt><dd>{e(row['candidate_filename'])}</dd></div>
<div><dt>Pixels</dt><dd>{e(row['candidate_width_px'])} × {e(row['candidate_height_px'])}</dd></div>
<div><dt>Original source</dt><dd><code>{e(row['candidate_source_path'])}</code></dd></div></dl></div>
<div class="panel object-census-detail__image"><h2>Current selected Master · {e(row['target_inventory'])}</h2>
<img src="{url('/kurt-schwarz/image', id=item['target_media_candidate_id'], v=3)}" alt="Current Master for {e(row['target_inventory'])}">
<dl class="facts"><div><dt>File</dt><dd>{e(Path(row['current_surrogate_path']).name)}</dd></div>
<div><dt>Pixels</dt><dd>{e(row['current_width_px'])} × {e(row['current_height_px'])}</dd></div>
<div><dt>Original source</dt><dd><code>{e(row['current_surrogate_path'])}</code></dd></div>
{metadata_lines}</dl></div></section>"""
            body = old_comparison.sub(comparison, body, count=1)
    body = body.replace(
        "TIFF/PSD versus current surrogate",
        "Higher-resolution file versus current surrogate",
    ).replace(
        "Same object — use TIFF/PSD as Master",
        "Same object — use this file as Master",
    ).replace(
        "Same object — use this file as Master",
        "Use higher-resolution candidate as Master — current file becomes Redundant",
    ).replace(
        "Same object — keep current surrogate",
        "Keep current file as Master — higher-resolution candidate becomes Redundant",
    ).replace(
        "Kurt Schwarz full-source object census",
        "Kurt Schwarz higher-resolution Master review",
    )
    return _send_html(self, title, body, active=active, status=status)


core_workbench.CoreWorkbenchHandler.send_html = send_html


if __name__ == "__main__":
    raise SystemExit(core_workbench.main())
