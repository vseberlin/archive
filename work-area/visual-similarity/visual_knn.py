#!/usr/bin/env python3
"""Propose metadata for new images from visually similar museum-digital records.

SUPPORT_INDEX tool (see docs/architecture/archive-application-structure.md).
It reads the completed institution museum-digital capture
(format iss-md-export-discovery-v1), embeds each record's display preview,
embeds a list of new images, and writes kNN-based *proposals* with their
neighbour evidence. Proposals are review evidence only: this tool never writes
to a unit, a catalogue, CATALOG.csv, or museum-digital.

Everything it writes stays under --work-root:

    index.sqlite            rebuildable embedding cache (delete freely)
    cache/md-preview/       downloaded MD preview images (delete freely)
    eval-<model>.json       leave-one-out evaluation receipt
    proposals-<run>.jsonl   one record per new image, full evidence
    proposals-<run>.csv     one row per proposed field value, for review

Commands:
    index-reference  embed MD capture previews (resumable)
    index-new        embed new images from a CSV of image_id,path (resumable)
    evaluate         leave-one-out accuracy on the reference set
    propose          write proposals for every indexed new image
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import re
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from typing import Any, Iterable
from urllib.request import Request, urlopen

import numpy as np
from PIL import Image, ImageFile, ImageOps

VERSION = "0.1.0"
USER_AGENT = "ISS-visual-similarity/0.1"
MD_DATA = "https://berlin.museum-digital.de/data/berlin"
MAX_DOWNLOAD_BYTES = 40 * 1024 * 1024
YEAR_RE = re.compile(r"(?<!\d)(1[6-9]\d\d|20[0-3]\d)(?!\d)")
INVENTORY_RE = re.compile(r"^(.*?)(\d+)\D*$")
FIELDS_MULTI = ("tags", "places", "collections", "people")
DEFAULT_FIELDS = ("object_type", "tags", "places", "collections", "date")

ImageFile.LOAD_TRUNCATED_IMAGES = True
Image.MAX_IMAGE_PIXELS = 400_000_000


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

class Embedder:
    """open_clip on CPU. 'pixel' is a dependency-free smoke-test backend only."""

    def __init__(self, model: str, pretrained: str, threads: int) -> None:
        self.name = f"{model}:{pretrained}" if model != "pixel" else "pixel"
        self.model_name = model
        if model == "pixel":
            return
        import open_clip
        import torch

        torch.set_num_threads(max(1, threads))
        self.torch = torch
        self.model, _, self.preprocess = open_clip.create_model_and_transforms(model, pretrained=pretrained, device="cpu")
        self.model.eval()

    def embed(self, images: list[Image.Image]) -> np.ndarray:
        if self.model_name == "pixel":
            rows = []
            for image in images:
                grey = np.asarray(image.convert("L").resize((16, 16), Image.Resampling.BILINEAR), dtype=np.float32).ravel()
                hist = np.histogram(grey, bins=16, range=(0, 255))[0].astype(np.float32)
                rows.append(np.concatenate([grey - grey.mean(), hist * 4]))
            vectors = np.stack(rows)
        else:
            with self.torch.no_grad():
                batch = self.torch.stack([self.preprocess(image) for image in images])
                vectors = self.model.encode_image(batch).float().numpy()
        norms = np.linalg.norm(vectors, axis=1, keepdims=True)
        return (vectors / np.maximum(norms, 1e-12)).astype(np.float32)


def open_image(data: bytes) -> Image.Image:
    with Image.open(BytesIO(data)) as opened:
        opened.draft("RGB", (448, 448))  # fast JPEG downscale; no-op for other formats
        image = ImageOps.exif_transpose(opened).convert("RGB")
        image.thumbnail((448, 448), Image.Resampling.BICUBIC)
        return image


def fetch(url: str, cache: Path) -> bytes:
    target = cache / (hashlib.sha256(url.encode()).hexdigest()[:24] + Path(url).suffix.lower()[:6])
    if target.is_file():
        return target.read_bytes()
    error = ""
    for attempt in range(3):
        try:
            with urlopen(Request(url, headers={"User-Agent": USER_AGENT}), timeout=60) as response:
                data = response.read(MAX_DOWNLOAD_BYTES + 1)
            if len(data) > MAX_DOWNLOAD_BYTES:
                raise VisualKnnError("download exceeds bound")
            temporary = target.with_name(f".{target.name}.tmp-{os.getpid()}")
            temporary.write_bytes(data)
            os.replace(temporary, target)
            return data
        except Exception as exception:  # noqa: BLE001 - recorded, retried
            error = clean(f"{type(exception).__name__}: {exception}")[:500]
            time.sleep(0.5 * (attempt + 1))
    raise VisualKnnError(error)


# ------------------------------------------------------------------- index

SCHEMA = """
CREATE TABLE IF NOT EXISTS embeddings (
    side TEXT NOT NULL,            -- 'ref' or 'new'
    item_id TEXT NOT NULL,         -- md_object_id or caller image_id
    model TEXT NOT NULL,
    source_key TEXT NOT NULL,      -- image URL or path|size|mtime; change => re-embed
    vector BLOB,
    status TEXT NOT NULL,          -- 'ok' or 'error'
    error TEXT NOT NULL DEFAULT '',
    meta_json TEXT NOT NULL DEFAULT '{}',
    indexed_at TEXT NOT NULL,
    PRIMARY KEY (side, item_id, model)
);
"""


def connect(work_root: Path) -> sqlite3.Connection:
    work_root.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(inside(work_root, work_root / "index.sqlite"))
    connection.executescript(SCHEMA)
    return connection


def known_keys(connection: sqlite3.Connection, side: str, model: str) -> dict[str, str]:
    return {
        row[0]: row[1]
        for row in connection.execute(
            "SELECT item_id, source_key FROM embeddings WHERE side=? AND model=? AND status='ok'", (side, model)
        )
    }


def store(connection, side, item_id, model, source_key, vector, error, meta) -> None:
    connection.execute(
        "INSERT OR REPLACE INTO embeddings VALUES (?,?,?,?,?,?,?,?,?)",
        (
            side, item_id, model, source_key,
            None if vector is None else vector.astype(np.float16).tobytes(),
            "error" if error else "ok", error,
            json.dumps(meta, ensure_ascii=False, sort_keys=True), utc_now(),
        ),
    )


def load_matrix(connection, side: str, model: str) -> tuple[list[str], np.ndarray, list[dict[str, Any]]]:
    rows = connection.execute(
        "SELECT item_id, vector, meta_json FROM embeddings WHERE side=? AND model=? AND status='ok' ORDER BY item_id",
        (side, model),
    ).fetchall()
    if not rows:
        return [], np.zeros((0, 1), np.float32), []
    ids = [row[0] for row in rows]
    matrix = np.stack([np.frombuffer(row[1], dtype=np.float16) for row in rows]).astype(np.float32)
    matrix /= np.maximum(np.linalg.norm(matrix, axis=1, keepdims=True), 1e-12)
    return ids, matrix, [json.loads(row[2]) for row in rows]


def embed_jobs(connection, embedder: Embedder, side: str, jobs: list[dict[str, Any]], loader, batch: int, workers: int) -> dict[str, int]:
    counts = {"embedded": 0, "errors": 0}
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        for start in range(0, len(jobs), batch):
            chunk = jobs[start:start + batch]
            loaded = list(pool.map(lambda job: _safe(loader, job), chunk))
            good = [(job, image) for job, (image, error) in zip(chunk, loaded) if image is not None]
            for job, (image, error) in zip(chunk, loaded):
                if image is None:
                    store(connection, side, job["id"], embedder.name, job["source_key"], None, error, job["meta"])
                    counts["errors"] += 1
            if good:
                vectors = embedder.embed([image for _, image in good])
                for (job, _), vector in zip(good, vectors):
                    store(connection, side, job["id"], embedder.name, job["source_key"], vector, "", job["meta"])
                counts["embedded"] += len(good)
            connection.commit()
            done = min(start + batch, len(jobs))
            print(f"\r{side}: {done}/{len(jobs)} ({counts['errors']} errors)", end="", file=sys.stderr, flush=True)
    print(file=sys.stderr)
    return counts


def _safe(loader, job):
    try:
        return loader(job), ""
    except Exception as exception:  # noqa: BLE001 - recorded per item
        return None, clean(f"{type(exception).__name__}: {exception}")[:500]


# ------------------------------------------------------------- aggregation

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


# ---------------------------------------------------------------- commands

def cmd_index_reference(args) -> int:
    records = load_capture(args.capture, args.expected_objects)
    connection = connect(args.work_root)
    embedder = Embedder(args.model, args.pretrained, args.threads)
    done = known_keys(connection, "ref", embedder.name)
    cache = inside(args.work_root, args.work_root / "cache/md-preview")
    cache.mkdir(parents=True, exist_ok=True)
    jobs = []
    for md_id, record in records.items():
        if not record["image_url"]:
            continue
        if done.get(md_id) == record["image_url"]:
            continue
        meta = {key: value for key, value in record.items() if key != "image_url"}
        jobs.append({"id": md_id, "source_key": record["image_url"], "meta": meta})
    if args.limit:
        jobs = jobs[: args.limit]
    print(f"reference: {len(records)} records, {len(jobs)} to embed with {embedder.name}", file=sys.stderr)
    counts = embed_jobs(connection, embedder, "ref", jobs, lambda job: open_image(fetch(job["source_key"], cache)), args.batch, args.workers)
    print(json.dumps({"model": embedder.name, **counts}))
    return 0


def cmd_index_new(args) -> int:
    connection = connect(args.work_root)
    embedder = Embedder(args.model, args.pretrained, args.threads)
    done = known_keys(connection, "new", embedder.name)
    jobs = []
    with args.manifest.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            item_id, path = clean(row.get(args.id_column)), Path(clean(row.get(args.path_column)))
            if not item_id or not path.is_file():
                print(f"skip: {item_id or '?'} {path}", file=sys.stderr)
                continue
            stat = path.stat()
            source_key = f"{path}|{stat.st_size}|{int(stat.st_mtime)}"
            if done.get(item_id) == source_key:
                continue
            jobs.append({"id": item_id, "source_key": source_key, "meta": {"path": str(path), **{k: clean(v) for k, v in row.items() if k}}})
    print(f"new: {len(jobs)} to embed with {embedder.name}", file=sys.stderr)
    counts = embed_jobs(connection, embedder, "new", jobs, lambda job: open_image(Path(job["meta"]["path"]).read_bytes()), args.batch, args.workers)
    print(json.dumps({"model": embedder.name, **counts}))
    return 0


def model_name(args) -> str:
    return "pixel" if args.model == "pixel" else f"{args.model}:{args.pretrained}"


def cmd_evaluate(args) -> int:
    """Leave-one-out on the reference set, twice: all neighbours, and with the
    query's inventory series (same prefix, number within --series-window) hidden.
    The second number is the honest estimate for a photo from an unknown Konvolut."""
    connection = connect(args.work_root)
    name = model_name(args)
    ids, matrix, meta = load_matrix(connection, "ref", name)
    if len(ids) < 50:
        raise VisualKnnError(f"only {len(ids)} reference embeddings for {name}")
    keys = [series_key(m.get("inventory", "")) for m in meta]
    rng = np.random.default_rng(args.seed)
    sample = rng.choice(len(ids), size=min(args.sample, len(ids)), replace=False)
    fields = tuple(args.fields)
    report: dict[str, Any] = {"model": name, "reference": len(ids), "sample": int(len(sample)), "settings": settings(args), "created_at": utc_now()}

    for mode in ("all_neighbours", "series_hidden"):
        stats: dict[str, dict[str, Any]] = {f: {"truth": 0, "proposed": 0, "correct": 0, "by_conf": {}} for f in fields}
        date_errors: list[float] = []
        top1: list[float] = []
        for index in sample:
            exclude = np.zeros(len(ids), bool)
            exclude[index] = True
            if mode == "series_hidden":
                own = keys[index]
                if own:
                    exclude |= np.array([bool(k and k[0] == own[0] and abs(k[1] - own[1]) <= args.series_window) for k in keys])
            found = neighbours_for(matrix[index], ids, matrix, meta, args.k, exclude)
            if found:
                top1.append(found[0]["similarity"])
            for proposal in aggregate(found, fields, args):
                field = proposal["field_key"]
                bucket = stats[field]["by_conf"].setdefault(proposal["confidence_key"], {"proposed": 0, "correct": 0})
                if field == "date":
                    truth = meta[index]
                    if not truth.get("year_start"):
                        continue
                    error = abs(proposal["year_estimate"] - (truth["year_start"] + truth["year_end"]) / 2)
                    date_errors.append(error)
                    hit = error <= args.date_tolerance
                else:
                    raw = meta[index].get(field)
                    truth_values = {v.casefold() for v in (raw if isinstance(raw, list) else [raw] if raw else [])}
                    hit = proposal["value"].casefold() in truth_values
                stats[field]["proposed"] += 1
                stats[field]["correct"] += int(hit)
                bucket["proposed"] += 1
                bucket["correct"] += int(hit)
            for field in fields:
                raw = meta[index].get("year_start") if field == "date" else meta[index].get(field)
                stats[field]["truth"] += int(bool(raw))
        for field, s in stats.items():
            s["precision"] = round(s["correct"] / s["proposed"], 3) if s["proposed"] else None
            for b in s["by_conf"].values():
                b["precision"] = round(b["correct"] / b["proposed"], 3) if b["proposed"] else None
        report[mode] = {
            "fields": stats,
            "date_mae_years": round(float(np.mean(date_errors)), 2) if date_errors else None,
            "top1_similarity_quartiles": [round(float(q), 3) for q in np.quantile(top1, [0.25, 0.5, 0.75])] if top1 else None,
        }
    path = args.work_root / f"eval-{re.sub(r'[^A-Za-z0-9_.-]+', '_', name)}.json"
    atomic_write(args.work_root, path, lambda h: (json.dump(report, h, ensure_ascii=False, indent=2, sort_keys=True), h.write("\n")))
    print_eval(report)
    print(f"receipt: {path}", file=sys.stderr)
    return 0


def print_eval(report: dict[str, Any]) -> None:
    for mode in ("all_neighbours", "series_hidden"):
        part = report[mode]
        print(f"\n[{mode}]  top1 sim q25/50/75 = {part['top1_similarity_quartiles']}  date MAE = {part['date_mae_years']} y")
        print(f"  {'field':<12} {'proposed':>8} {'prec':>6}   high       medium     low")
        for field, s in part["fields"].items():
            conf = "  ".join(
                f"{(s['by_conf'].get(c) or {}).get('precision') or '-':>5}/{(s['by_conf'].get(c) or {}).get('proposed', 0):<4}"
                for c in ("high", "medium", "low")
            )
            print(f"  {field:<12} {s['proposed']:>8} {s['precision'] or '-':>6}   {conf}")


def cmd_propose(args) -> int:
    connection = connect(args.work_root)
    name = model_name(args)
    ref_ids, ref_matrix, ref_meta = load_matrix(connection, "ref", name)
    new_ids, new_matrix, new_meta = load_matrix(connection, "new", name)
    if not ref_ids or not new_ids:
        raise VisualKnnError(f"nothing to compare for {name}: {len(ref_ids)} reference, {len(new_ids)} new")
    run = args.run or datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    jsonl_path = args.work_root / f"proposals-{run}.jsonl"
    csv_path = args.work_root / f"proposals-{run}.csv"
    for path in (jsonl_path, csv_path):
        if path.exists():
            raise VisualKnnError(f"refusing to overwrite {path}")
    records, rows = [], []
    for item_id, vector, meta in zip(new_ids, new_matrix, new_meta):
        found = neighbours_for(vector, ref_ids, ref_matrix, ref_meta, args.k)
        proposals = aggregate(found, tuple(args.fields), args)
        duplicate = found[0] if found and found[0]["similarity"] >= args.duplicate_similarity else None
        records.append({
            "image_id": item_id,
            "path": meta.get("path", ""),
            "model": name,
            "possible_md_duplicate": {k: duplicate[k] for k in ("md_object_id", "inventory", "similarity", "md_url")} if duplicate else None,
            "proposals": proposals,
            "neighbours": [{k: n[k] for k in ("md_object_id", "inventory", "similarity", "md_url")} for n in found],
            "settings": settings(args),
            "created_at": utc_now(),
            "status": "proposed",
        })
        if duplicate:
            rows.append({"image_id": item_id, "field_key": "possible_md_duplicate", "value": duplicate["md_object_id"],
                         "support": duplicate["similarity"], "voters": 1, "confidence_key": "",
                         "evidence": duplicate["md_url"], "path": meta.get("path", "")})
        for p in proposals:
            rows.append({"image_id": item_id, "field_key": p["field_key"], "value": p["value"], "support": p["support"],
                         "voters": p["voters"], "confidence_key": p["confidence_key"],
                         "evidence": " ".join(f"md:{i}" for i in p["evidence_md_ids"]), "path": meta.get("path", "")})

    atomic_write(args.work_root, jsonl_path, lambda h: [h.write(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n") for r in records])
    fields = ["image_id", "field_key", "value", "support", "voters", "confidence_key", "evidence", "path"]

    def write_csv(handle):
        writer = csv.DictWriter(handle, fieldnames=fields, quoting=csv.QUOTE_ALL)
        writer.writeheader()
        writer.writerows(rows)

    atomic_write(args.work_root, csv_path, write_csv)
    print(json.dumps({"images": len(records), "proposal_rows": len(rows), "jsonl": str(jsonl_path), "csv": str(csv_path)}))
    return 0


def settings(args) -> dict[str, Any]:
    return {k: getattr(args, k) for k in ("k", "min_similarity", "min_support", "temperature", "date_tolerance", "series_window", "fields") if hasattr(args, k)}


# --------------------------------------------------------------------- cli

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--work-root", type=Path, required=True, help="e.g. /mnt/volume/iss-archive-work/visual-similarity")
    parser.add_argument("--model", default="ViT-B-32", help="open_clip model, or 'pixel' for a smoke test")
    parser.add_argument("--pretrained", default="laion2b_s34b_b79k")
    commands = parser.add_subparsers(dest="command", required=True)

    def embedding_options(p):
        p.add_argument("--batch", type=int, default=16)
        p.add_argument("--workers", type=int, default=4, help="parallel image downloads/decodes")
        p.add_argument("--threads", type=int, default=os.cpu_count() or 2, help="torch CPU threads")

    def vote_options(p):
        p.add_argument("--k", type=int, default=12)
        p.add_argument("--min-similarity", type=float, default=0.5)
        p.add_argument("--min-support", type=float, default=0.34)
        p.add_argument("--temperature", type=float, default=0.05)
        p.add_argument("--date-tolerance", type=float, default=5.0)
        p.add_argument("--series-window", type=int, default=20)
        p.add_argument("--fields", nargs="+", default=list(DEFAULT_FIELDS),
                       choices=["object_type", "tags", "places", "collections", "people", "date"])

    p = commands.add_parser("index-reference")
    p.add_argument("--capture", type=Path, required=True, help="MD capture run dir with coverage.json and raw/")
    p.add_argument("--expected-objects", type=int, default=11170)
    p.add_argument("--limit", type=int, default=0)
    embedding_options(p)
    p.set_defaults(func=cmd_index_reference)

    p = commands.add_parser("index-new")
    p.add_argument("--manifest", type=Path, required=True, help="CSV with image id and absolute path")
    p.add_argument("--id-column", default="image_id")
    p.add_argument("--path-column", default="path")
    embedding_options(p)
    p.set_defaults(func=cmd_index_new)

    p = commands.add_parser("evaluate")
    p.add_argument("--sample", type=int, default=400)
    p.add_argument("--seed", type=int, default=47)
    vote_options(p)
    p.set_defaults(func=cmd_evaluate)

    p = commands.add_parser("propose")
    p.add_argument("--run", default="")
    p.add_argument("--duplicate-similarity", type=float, default=0.95)
    vote_options(p)
    p.set_defaults(func=cmd_propose)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except VisualKnnError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
