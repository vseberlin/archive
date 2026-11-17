"""Bounded reference-preview experiment; master images enter through shared receipts."""
from __future__ import annotations
import argparse
import csv
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import time
import tempfile
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from io import BytesIO
from typing import Any
from urllib.request import Request, urlopen
import numpy as np
from PIL import Image, ImageOps
from .reference_catalogue import (VERSION, USER_AGENT, MD_DATA, MAX_DOWNLOAD_BYTES,
    DEFAULT_FIELDS, VisualKnnError, clean, unique, inside, atomic_write, utc_now,
    load_capture, aggregate, neighbours_for, series_key, heldout_mask)
from .imaging import display_rgb
from .contracts import Source, encoded, require
from . import storage
from .sources import Roots
from .extractors.visual_features import Encoder, recipe_for, space_id


def configured_recipe(args):
    require(args.model_source is not None, 'Select a fingerprinted local model Source JSON')
    model = json.loads(args.model_source.read_text())
    return recipe_for({'settings': {'model':model, 'threads':args.threads, 'framing':args.framing}})


class Embedder:
    def __init__(self, args):
        self.recipe = configured_recipe(args)
        self.name = space_id(self.recipe)
        self.encoder = Encoder(Roots.from_toml(args.roots), self.recipe)

    def embed(self, images):
        return self.encoder.encode_images(images)


def open_image(data):
    with Image.open(BytesIO(data)) as opened:
        require(getattr(opened, 'n_frames', 1) == 1, 'Reference preview must be one image; use selected page receipts for masters')
        require(opened.width * opened.height <= 12_000_000, 'Reference preview exceeds 12 megapixels; use shared preparation')
        rgb, _ = display_rgb(opened)
        oriented = ImageOps.exif_transpose(rgb)
        rgb.close()
        return oriented


def cache_path(url, cache):
    return cache / (hashlib.sha256(url.encode()).hexdigest()[:24] + Path(url).suffix.lower()[:6])


def fetch(url: str, cache: Path) -> bytes:
    from urllib.parse import urlparse
    parsed = urlparse(url)
    require(parsed.scheme == 'https' and parsed.hostname == 'berlin.museum-digital.de', 'Only selected institutional preview URLs permitted')
    target = cache_path(url, cache)
    require(not target.is_symlink(), 'Redirected preview cache')
    if target.is_file():
        return target.read_bytes()
    error = ""
    for attempt in range(3):
        try:
            with urlopen(Request(url, headers={"User-Agent": USER_AGENT}), timeout=60) as response:
                data = response.read(MAX_DOWNLOAD_BYTES + 1)
            if len(data) > MAX_DOWNLOAD_BYTES:
                raise VisualKnnError("download exceeds bound")
            descriptor, temporary = tempfile.mkstemp(prefix='.preview-',dir=cache)
            try:
                with os.fdopen(descriptor,'wb') as handle:
                    handle.write(data)
                os.replace(temporary, target)
            finally:
                Path(temporary).unlink(missing_ok=True)
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
    source_key TEXT NOT NULL,      -- capture record hash or shared receipt digest
    vector BLOB,
    status TEXT NOT NULL,          -- 'ok' or 'error'
    error TEXT NOT NULL DEFAULT '',
    meta_json TEXT NOT NULL DEFAULT '{}',
    indexed_at TEXT NOT NULL,
    PRIMARY KEY (side, item_id, model)
);
"""


def connect(work_root: Path) -> sqlite3.Connection:
    from .common import work_root as checked_root
    work_root = checked_root(work_root)
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


def embed_jobs(connection, embedder: Embedder, side: str, jobs: list[dict[str, Any]], loader, batch: int, workers: int, progress=None) -> dict[str, int]:
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
                try:
                    vectors = embedder.embed([image for _, image in good])
                finally:
                    for _, image in good:
                        image.close()
                for (job, _), vector in zip(good, vectors):
                    store(connection, side, job["id"], embedder.name, job["source_key"], vector, "", job["meta"])
                counts["embedded"] += len(good)
            connection.commit()
            done = min(start + batch, len(jobs))
            if progress is not None: progress(counts, done)
            print(f"\r{side}: {done}/{len(jobs)} ({counts['errors']} errors)", end="", file=sys.stderr, flush=True)
    print(file=sys.stderr)
    return counts


def _safe(loader, job):
    try:
        return loader(job), ""
    except Exception as exception:  # noqa: BLE001 - recorded per item
        return None, clean(f"{type(exception).__name__}: {exception}")[:500]


def cmd_index_reference(args) -> int:
    records = load_capture(args.capture, args.expected_objects)
    capture_sha = hashlib.sha256(encoded(records).encode()).hexdigest()
    previous = args.work_root/'reference-run.json'
    if previous.exists():
        require(json.loads(previous.read_text())['capture_sha256'] == capture_sha,
                'Capture changed; select a fresh reference work directory')
    connection = connect(args.work_root)
    embedder = Embedder(args)
    done = {row[0]:(row[1],json.loads(row[2])) for row in connection.execute(
        "SELECT item_id,source_key,meta_json FROM embeddings WHERE side='ref' AND model=? AND status='ok'",(embedder.name,))}
    cache = inside(args.work_root, args.work_root / "cache/md-preview")
    cache.mkdir(parents=True, exist_ok=True)
    jobs = []
    for md_id, record in records.items():
        if not record['image_url']:
            continue
        source_key = hashlib.sha256(encoded(record).encode()).hexdigest()
        path = cache_path(record['image_url'], cache)
        prior = done.get(md_id)
        if prior and prior[0] == source_key and path.is_file() and not path.is_symlink():
            if hashlib.sha256(path.read_bytes()).hexdigest() == prior[1].get('preview_sha256'):
                continue
        meta = {**record, 'object_id':'md:'+md_id, 'unit':'museum-digital:berlin:29',
                'representation':record['image_url'], 'source_role':'published-preview',
                'capture_record_sha256':source_key, 'metadata_adopted':False}
        jobs.append({'id':md_id, 'source_key':source_key, 'url':record['image_url'], 'meta':meta})
    def loader(job):
        data = fetch(job['url'], cache)
        job['meta']['preview_sha256'] = hashlib.sha256(data).hexdigest()
        job['meta']['preview_path'] = str(cache_path(job['url'],cache).relative_to(args.work_root))
        return open_image(data)
    pending = len(jobs)
    candidates = sum(bool(r['image_url']) for r in records.values())
    if args.limit:
        jobs = jobs[: args.limit]
    manifest = {'model':embedder.name, 'recipe':embedder.recipe.record(),
                'capture':str(args.capture.resolve()),
                'capture_sha256':capture_sha,
                'records':len(records), 'with_preview_url':candidates,
                'without_preview_url':len(records)-candidates,
                'reused':candidates-pending, 'selected':len(jobs),
                'unselected_pending':pending-len(jobs), 'status':'running',
                'metadata_adopted':False, 'started_at':utc_now()}
    storage.write_json(args.work_root, 'reference-run.json', manifest)
    print(f"reference: {len(records)} records, {len(jobs)} to embed with {embedder.name}", file=sys.stderr)
    def progress(counts, completed):
        manifest.update(counts, processed=completed, updated_at=utc_now())
        storage.write_json(args.work_root, 'reference-run.json', manifest)
    counts = embed_jobs(connection, embedder, "ref", jobs, loader, args.batch, args.workers, progress)
    manifest.update(counts, completed_at=utc_now(),
                    status='partial' if counts['errors'] or pending > len(jobs) else 'complete')
    manifest['indexed'] = manifest['reused'] + counts['embedded']
    storage.write_json(args.work_root, 'reference-run.json', manifest)
    # Rebuildable projection: retain catalogue provenance and vectors outside SQLite.
    def export(handle):
        for row in connection.execute("SELECT item_id,vector,status,error,meta_json FROM embeddings WHERE side='ref' AND model=? ORDER BY item_id", (embedder.name,)):
            handle.write(encoded({'id':row[0], 'model':embedder.name,
                'vector':np.frombuffer(row[1],dtype=np.float16).astype(float).tolist() if row[1] else None,
                'status':row[2], 'error':row[3], 'metadata':json.loads(row[4])})+'\n')
    atomic_write(args.work_root, args.work_root/'reference-records.jsonl', export)
    connection.close()
    print(json.dumps(manifest))
    return 2 if counts['errors'] else 0


def cmd_index_new(args):
    from .receipts import read_current
    roots = Roots.from_toml(args.roots)
    recipe = configured_recipe(args); name = space_id(recipe)
    selection = json.loads(args.selection.read_text())
    keys = selection['keys'] if isinstance(selection, dict) else selection
    require(isinstance(keys, list) and 1 <= len(keys) <= 2048 and len(set(keys)) == len(keys), 'Explicit selected visual receipts required')
    connection = connect(args.work_root)
    count = 0
    with connection:
        for key in keys:
            row, result, digest = read_current(roots, 'artifacts', key)
            require(result.recipe.extractor == 'visual' and space_id(result.recipe) == name, 'Visual receipt model space differs')
            vector = np.asarray(row['feature']['vector'], dtype=np.float32)
            require(len(vector) == recipe.record()['parameters']['dimensions'] and np.isfinite(vector).all() and abs(float(np.linalg.norm(vector))-1) < 1e-4, 'Invalid visual receipt vector')
            meta = {'source':result.record()['page']['source'], 'page':result.record()['page'],
                    'region':result.record()['region'], 'receipt_key':key, 'receipt_sha256':digest,
                    'metadata_adopted':False}
            store(connection, 'new', key, name, digest, vector, '', meta)
            count += 1
    connection.close()
    print(json.dumps({'imported_visual_receipts':count, 'master_reads':0, 'metadata_adopted':False}))
    return 0


def cmd_evaluate(args) -> int:
    """Catalogue agreement under explicit holdouts; not independent identity accuracy."""
    connection = connect(args.work_root)
    name = space_id(configured_recipe(args))
    ids, matrix, meta = load_matrix(connection, "ref", name)
    if len(ids) < 50:
        raise VisualKnnError(f"only {len(ids)} reference embeddings for {name}")
    rng = np.random.default_rng(args.seed)
    sample = rng.choice(len(ids), size=min(args.sample, len(ids)), replace=False)
    fields = tuple(args.fields)
    report: dict[str, Any] = {"model": name, "reference": len(ids), "sample": int(len(sample)), "settings": settings(args), "created_at": utc_now(),
        "metadata_adopted":False, "near_duplicate_similarity":args.near_duplicate_similarity,
        "reference_run":json.loads((args.work_root/"reference-run.json").read_text()),
        "limitations":["Catalogue agreement, not independent identification ground truth.",
                       "Cosine exclusion is a conservative leakage control, not proof of duplicate identity."]}

    for mode in ("all_neighbours", "series_hidden"):
        stats: dict[str, dict[str, Any]] = {f: {"truth": 0, "proposed": 0, "correct": 0, "by_conf": {}} for f in fields}
        date_errors: list[float] = []
        top1: list[float] = []
        excluded_counts = []
        for index in sample:
            exclude = heldout_mask(index, matrix, meta, series_window=args.series_window,
                                   near_duplicate_similarity=args.near_duplicate_similarity,
                                   hide_series=(mode == 'series_hidden'))
            excluded_counts.append(int(exclude.sum())-1)
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
            "excluded_other_records_mean": float(np.mean(excluded_counts)),
            "exact_byte_duplicates_hidden": True,
            "near_duplicates_hidden": mode == 'series_hidden',
            "date_mae_years": round(float(np.mean(date_errors)), 2) if date_errors else None,
            "top1_similarity_quartiles": [round(float(q), 3) for q in np.quantile(top1, [0.25, 0.5, 0.75])] if top1 else None,
        }
    path = args.work_root / f"eval-{re.sub(r'[^A-Za-z0-9_.-]+', '_', name)}.json"
    atomic_write(args.work_root, path, lambda h: (json.dump(report, h, ensure_ascii=False, indent=2, sort_keys=True), h.write("\n")))
    connection.close()
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
    name = space_id(configured_recipe(args))
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
            "metadata_adopted": False,
            "source_receipt": meta,
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
    connection.close()
    print(json.dumps({"images": len(records), "proposal_rows": len(rows), "jsonl": str(jsonl_path), "csv": str(csv_path)}))
    return 0


def settings(args) -> dict[str, Any]:
    return {k: getattr(args, k) for k in ("k", "min_similarity", "min_support", "temperature", "date_tolerance", "series_window", "fields") if hasattr(args, k)}


# --------------------------------------------------------------------- cli

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--work-root", type=Path, required=True, help="e.g. /mnt/volume/iss-archive-work/visual-similarity")
    parser.add_argument('--roots', type=Path, required=True)
    parser.add_argument('--model-source', type=Path, required=True, help='Existing local model Source record JSON')
    parser.add_argument('--framing', choices=['letterbox','center'], default='letterbox')
    parser.add_argument('--threads', type=int, default=2)
    commands = parser.add_subparsers(dest="command", required=True)

    def embedding_options(p):
        p.add_argument("--batch", type=int, default=16)
        p.add_argument("--workers", type=int, default=2, help="parallel preview downloads/decodes")

    def vote_options(p):
        p.add_argument("--k", type=int, default=12)
        p.add_argument("--min-similarity", type=float, default=0.5)
        p.add_argument("--min-support", type=float, default=0.34)
        p.add_argument("--temperature", type=float, default=0.05)
        p.add_argument("--date-tolerance", type=float, default=5.0)
        p.add_argument("--series-window", type=int, default=20)
        p.add_argument("--fields", nargs="+", default=list(DEFAULT_FIELDS),
                       choices=["object_type", "tags", "places", "collections", "date"])

    p = commands.add_parser("index-reference")
    p.add_argument("--capture", type=Path, required=True, help="MD capture run dir with coverage.json and raw/")
    p.add_argument("--expected-objects", type=int, default=11170)
    p.add_argument("--limit", type=int, default=0)
    embedding_options(p)
    p.set_defaults(func=cmd_index_reference)

    p = commands.add_parser('index-new', help='Import shared visual receipts; never scan masters')
    p.add_argument('--selection', type=Path, required=True)
    p.set_defaults(func=cmd_index_new)

    p = commands.add_parser("evaluate")
    p.add_argument("--near-duplicate-similarity", type=float, default=.95)
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
        from .common import work_root, file_lock
        args.work_root = work_root(args.work_root)
        require(1 <= args.threads <= 8, 'Threads outside bounded range')
        if hasattr(args, 'batch'):
            require(1 <= args.batch <= 32 and 1 <= args.workers <= 4, 'Invalid bounded batch/workers')
        if hasattr(args, 'k'):
            require(1 <= args.k <= 100 and args.temperature > 0 and 0 <= args.min_support <= 1, 'Invalid voting bounds')
        if hasattr(args, 'limit'):
            require(args.limit >= 0, 'Negative preview limit')
        if hasattr(args, 'sample'):
            require(1 <= args.sample <= 2000 and 0 < args.near_duplicate_similarity <= 1, 'Invalid evaluation bounds')
        with file_lock(args.work_root / '.reference.lock'):
            return args.func(args)
    except (VisualKnnError, ValueError, OSError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
