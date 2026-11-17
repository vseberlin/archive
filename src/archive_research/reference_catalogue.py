"""Read-only museum-digital catalogue mapping and visual-vote evaluation helpers."""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable

import numpy as np

VERSION = "0.2.0"
USER_AGENT = "ISS-visual-similarity/0.1"
MD_DATA = "https://berlin.museum-digital.de/data/berlin"
MAX_DOWNLOAD_BYTES = 40 * 1024 * 1024
YEAR_RE = re.compile(r"(?<!\d)(1[6-9]\d\d|20[0-3]\d)(?!\d)")
INVENTORY_RE = re.compile(r"^(.*?)(\d+)\D*$")
FIELDS_MULTI = ("tags", "places", "collections", "people")
DEFAULT_FIELDS = ("object_type", "tags", "places", "collections", "date")



class VisualKnnError(RuntimeError):
    pass


# --------------------------------------------------------------------------- utils

def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def clean(value: Any) -> str:
    return " ".join(str(value or "").split())


def unique(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        value = clean(value)
        if value and value.casefold() not in seen:
            seen.add(value.casefold())
            out.append(value)
    return out


def inside(root: Path, path: Path) -> Path:
    resolved = path.resolve()
    if resolved != root.resolve() and root.resolve() not in resolved.parents:
        raise VisualKnnError(f"output escaped work root: {path}")
    return path


def atomic_write(root: Path, path: Path, writer) -> None:
    inside(root, path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        with temporary.open("x", encoding="utf-8", newline="") as handle:
            writer(handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


# --------------------------------------------------------------- museum-digital

def md_preview_url(item: dict[str, Any]) -> str:
    images = item.get("object_images") if isinstance(item.get("object_images"), list) else []
    images = [row for row in images if isinstance(row, dict) and str(row.get("type") or "image").casefold() == "image"]
    if not images:
        return ""
    image = sorted(images, key=lambda row: (str(row.get("is_main")) != "j", int(row.get("order") or 999)))[0]
    folder = clean(image.get("folder")).strip("/")
    filename = clean(image.get("preview") or image.get("filename_loc"))
    return f"{MD_DATA}/{folder}/{filename}" if folder and filename else ""


def child_values(rows: Any, family: str, key: str) -> list[str]:
    if not isinstance(rows, list):
        return []
    return [
        clean((row.get(family) or {}).get(key))
        for row in rows
        if isinstance(row, dict) and isinstance(row.get(family), dict)
    ]


def named(rows: Any, key: str) -> list[str]:
    if not isinstance(rows, list):
        return []
    return [clean(row.get(key)) for row in rows if isinstance(row, dict)]


def year_range(texts: list[str]) -> tuple[int, int]:
    years = [int(y) for text in texts for y in YEAR_RE.findall(text)]
    return (min(years), max(years)) if years else (0, 0)


def md_record(item: dict[str, Any]) -> dict[str, Any]:
    events = item.get("object_events")
    relation_places = item.get("object_relation_places")
    relation_people = item.get("object_relation_people")
    dates = unique(child_values(events, "time", "time_name"))
    start, end = year_range(dates)
    return {
        "md_object_id": clean(item.get("object_id")),
        "inventory": clean(item.get("object_inventory_number")),
        "title": clean(item.get("object_name")),
        "object_type": clean(item.get("object_type")),
        "tags": unique(named(item.get("object_tags"), "tag_name")),
        "places": unique(child_values(events, "place", "place_name") + child_values(relation_places, "place", "place_name")),
        "collections": unique(named(item.get("object_collection"), "collection_name")),
        "people": unique(
            child_values(events, "people", "people_display_name")
            + child_values(relation_people, "people", "displayname")
        ),
        "date_text": "; ".join(dates),
        "year_start": start,
        "year_end": end,
        "md_url": f"https://berlin.museum-digital.de/object/{clean(item.get('object_id'))}",
        "image_url": md_preview_url(item),
    }


def load_capture(run: Path, expected: int | None) -> dict[str, dict[str, Any]]:
    coverage = json.loads((run / "coverage.json").read_text(encoding="utf-8"))
    if coverage.get("format") != "iss-md-export-discovery-v1":
        raise VisualKnnError(f"unexpected capture format: {coverage.get('format')}")
    if int(coverage.get("request_failures", -1)) != 0:
        raise VisualKnnError("museum-digital capture recorded request failures")
    records: dict[str, dict[str, Any]] = {}
    for path in sorted((run / "raw").glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        values = payload.get("results", {}).get("objects", {})
        iterable = values.values() if isinstance(values, dict) else values if isinstance(values, list) else []
        for item in iterable:
            if isinstance(item, dict) and clean(item.get("object_id")):
                record = md_record(item)
                records[record["md_object_id"]] = record
    captured = int(coverage.get("captured_objects", -1))
    if len(records) != captured or (expected is not None and captured != expected):
        raise VisualKnnError(f"capture count drift: parsed {len(records)}, coverage {captured}, expected {expected}")
    return records


# ----------------------------------------------------------------- embedding

def series_key(inventory: str) -> tuple[str, int] | None:
    match = INVENTORY_RE.match(clean(inventory))
    return (match.group(1).strip().casefold(), int(match.group(2))) if match else None


def confidence_key(support: float, voters: int) -> str:
    if support >= 0.75 and voters >= 3:
        return "high"
    if support >= 0.5 and voters >= 2:
        return "medium"
    return "low"


def weighted_quantile(values: np.ndarray, weights: np.ndarray, q: float) -> float:
    order = np.argsort(values)
    cumulative = np.cumsum(weights[order])
    return float(values[order][np.searchsorted(cumulative, q * cumulative[-1])])


def aggregate(neighbours: list[dict[str, Any]], fields: tuple[str, ...], args) -> list[dict[str, Any]]:
    """Vote over neighbour metadata. Weight = exp((sim - top_sim) / temperature)."""
    usable = [n for n in neighbours if n["similarity"] >= args.min_similarity]
    if not usable:
        return []
    top = usable[0]["similarity"]
    for n in usable:
        n["weight"] = math.exp((n["similarity"] - top) / args.temperature)
    total = sum(n["weight"] for n in usable)
    proposals: list[dict[str, Any]] = []

    for field in fields:
        if field == "date":
            dated = [n for n in usable if n["meta"].get("year_start")]
            if len(dated) < 2:
                continue
            mids = np.array([(n["meta"]["year_start"] + n["meta"]["year_end"]) / 2 for n in dated])
            weights = np.array([n["weight"] for n in dated])
            median = weighted_quantile(mids, weights, 0.5)
            low = int(round(weighted_quantile(mids, weights, 0.25)))
            high = int(round(weighted_quantile(mids, weights, 0.75)))
            near = [n for n, mid in zip(dated, mids) if abs(mid - median) <= args.date_tolerance]
            support = sum(n["weight"] for n in near) / total
            if support < args.min_support:
                continue
            proposals.append({
                "field_key": "date",
                "value": str(low) if low == high else f"{low}–{high}",
                "year_estimate": int(round(median)),
                "support": round(support, 3),
                "voters": len(near),
                "confidence_key": confidence_key(support, len(near)),
                "evidence_md_ids": [n["md_object_id"] for n in near],
            })
            continue

        votes: dict[str, dict[str, Any]] = {}
        for n in usable:
            raw = n["meta"].get(field)
            values = raw if isinstance(raw, list) else ([raw] if raw else [])
            for value in unique(values):
                slot = votes.setdefault(value.casefold(), {"value": value, "weight": 0.0, "ids": []})
                slot["weight"] += n["weight"]
                slot["ids"].append(n["md_object_id"])
        ranked = sorted(votes.values(), key=lambda v: -v["weight"])
        if field not in FIELDS_MULTI:
            ranked = ranked[:1]
        for vote in ranked:
            support = vote["weight"] / total
            if support < args.min_support or len(vote["ids"]) < 2:
                continue
            proposals.append({
                "field_key": field,
                "value": vote["value"],
                "support": round(support, 3),
                "voters": len(vote["ids"]),
                "confidence_key": confidence_key(support, len(vote["ids"])),
                "evidence_md_ids": vote["ids"],
            })
    return proposals


def neighbours_for(row: np.ndarray, ref_ids, ref_matrix, ref_meta, k: int, exclude=None) -> list[dict[str, Any]]:
    sims = ref_matrix @ row
    if exclude is not None:
        sims = np.where(exclude, -2.0, sims)
    top = np.argpartition(-sims, min(k, len(sims) - 1))[:k]
    top = top[np.argsort(-sims[top])]
    return [
        {
            "md_object_id": ref_ids[i],
            "inventory": ref_meta[i].get("inventory", ""),
            "similarity": round(float(sims[i]), 4),
            "md_url": ref_meta[i].get("md_url", ""),
            "meta": ref_meta[i],
        }
        for i in top if sims[i] > -2.0
    ]




def heldout_mask(index, matrix, metadata, *, series_window, near_duplicate_similarity, hide_series):
    """Exact-byte duplicates are always hidden; hard holdout also hides near copies."""
    excluded = np.zeros(len(metadata), dtype=bool)
    excluded[index] = True
    digest = metadata[index].get('preview_sha256')
    if digest:
        excluded |= np.asarray([m.get('preview_sha256') == digest for m in metadata])
    if hide_series:
        own = series_key(metadata[index].get('inventory', ''))
        if own:
            keys = [series_key(m.get('inventory', '')) for m in metadata]
            excluded |= np.asarray([bool(k and k[0] == own[0] and abs(k[1]-own[1]) <= series_window) for k in keys])
        excluded |= (matrix @ matrix[index]) >= near_duplicate_similarity
    return excluded
